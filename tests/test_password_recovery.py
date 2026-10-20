"""Phase 3: recovery after "Permission denied" and "Save this password for this host?"."""

import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ssh_manager.auth_flow import (
    MAX_ATTEMPTS,
    AuthOutcome,
    Decision,
    FailureAction,
    PasswordFlow,
    PasswordRequest,
    PasswordResolution,
    SaveContext,
    SaveOffer,
    source_phrase,
)
from ssh_manager.config import GroupConfig, HostEntry
from ssh_manager.credentials import CredentialStore
from ssh_manager.secure_store import InMemoryKeyring, SecureStore

SECRET = "typed pa$$ 'w`rd' %s ü "  # spaces (also trailing), quotes, $, %, backtick, Unicode
EDGE_PASSWORDS = [SECRET, " leading", "trailing ", 'dq"uote', "$(rm -rf ~)", "%h%p%%", "`id`", "пароль-密码",
                  "x" * 4096]
TARGET = "ops@web.internal:22"


class RecordingKeyring(InMemoryKeyring):
    def __init__(self) -> None:
        super().__init__()
        self.writes = []
        self.fail = False

    def set_password(self, service, username, password):
        if self.fail:
            raise RuntimeError("keyring locked")
        self.writes.append(("set", username))
        return super().set_password(service, username, password)

    def delete_password(self, service, username):
        self.writes.append(("delete", username))
        return super().delete_password(service, username)


# ----------------------------------------------------------------- pure flow
class FlowTests(unittest.TestCase):
    def flow(self, stored="credential-pw"):
        flow = PasswordFlow("web")
        flow.stored = stored
        flow.attempt("credential", stored)
        return flow

    def test_fail_retry_verified_success_offers_save(self) -> None:
        flow = self.flow()
        self.assertIs(flow.on_failure(AuthOutcome.AUTH_FAILED), FailureAction.PROMPT)
        request = flow.next_request(TARGET, source_phrase("credential", "Ops"))
        self.assertEqual(request.title, "Password for ‘web’")
        self.assertEqual(request.message, "‘web’ rejected the saved password (credential ‘Ops’). "
                                          "Enter the password for ops@web.internal:22.")
        self.assertEqual(request.attempt_text, "Attempt 1 of 3")
        flow.attempt("retry", SECRET)
        offer, notice = flow.on_authenticated("sentinel", SaveContext(credential_name="Ops"))
        self.assertEqual(notice, "")
        self.assertEqual(offer.title, "Save this password for ‘web’?")
        self.assertEqual(offer.message, "Login succeeded with the password you entered. Saving it makes this host "
                                        "use its own password from now on. The credential ‘Ops’ and other hosts "
                                        "are not changed.")
        flow.decide(Decision.SAVE)
        self.assertIs(flow.decision, Decision.SAVE)

    def test_three_failures_stop(self) -> None:
        flow = self.flow()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.assertIs(flow.on_failure(AuthOutcome.AUTH_FAILED), FailureAction.PROMPT)
            self.assertEqual(flow.next_request(TARGET, "x").attempt, attempt)
            flow.attempt("retry", f"wrong{attempt}")
        self.assertIs(flow.on_failure(AuthOutcome.AUTH_FAILED), FailureAction.EXHAUSTED)
        self.assertFalse(flow.can_prompt())
        flow.begin_connect()
        self.assertTrue(flow.can_prompt())

    def test_non_auth_failures_never_prompt(self) -> None:
        flow = self.flow()
        for outcome in AuthOutcome:
            if outcome is not AuthOutcome.AUTH_FAILED:
                self.assertIs(flow.on_failure(outcome), FailureAction.STOP, outcome)

    def test_dont_save_suppresses_further_offers_in_the_tab(self) -> None:
        flow = self.flow()
        flow.attempt("retry", SECRET)
        offer, _ = flow.on_authenticated("sentinel", SaveContext())
        self.assertIsNotNone(offer)
        flow.decide(Decision.DONT_SAVE)
        flow.attempt("retry", "another")
        self.assertEqual(flow.on_authenticated("sentinel", SaveContext()), (None, ""))
        fresh = PasswordFlow("web")
        fresh.attempt("retry", SECRET)
        self.assertIsNotNone(fresh.on_authenticated("sentinel", SaveContext())[0])

    def test_stored_source_success_is_never_offered(self) -> None:
        for source in ("vault", "credential", "host", "legacy", "session", "manual"):
            flow = PasswordFlow("web")
            flow.attempt(source, "pw")
            self.assertEqual(flow.on_authenticated("sentinel", SaveContext()), (None, ""), source)

    def test_typed_password_equal_to_the_stored_one_is_skipped_silently(self) -> None:
        flow = self.flow(stored=SECRET)
        flow.attempt("retry", SECRET)
        self.assertEqual(flow.on_authenticated("sentinel", SaveContext()), (None, ""))

    def test_heuristic_wording(self) -> None:
        flow = self.flow()
        flow.attempt("prompt", SECRET)
        offer, _ = flow.on_authenticated("heuristic", SaveContext())
        self.assertFalse(offer.verified)
        self.assertTrue(offer.message.startswith("Login appears to have succeeded"))
        self.assertIn("Other hosts are not changed.", offer.message)

    def test_vault_overrides_and_is_explained(self) -> None:
        flow = self.flow()
        flow.attempt("retry", SECRET)
        offer, notice = flow.on_authenticated("sentinel", SaveContext(vault_label="Prod vault"))
        self.assertIsNone(offer)
        self.assertIn("vault entry ‘Prod vault’, which overrides", notice)

    def test_read_only_offers_session_only(self) -> None:
        offer = SaveOffer("web", True, SaveContext(can_save=False, reason="The inventory is read-only."))
        self.assertIn("The inventory is read-only. The password can be used for this session only.", offer.message)

    def test_replacing_a_host_password(self) -> None:
        offer = SaveOffer("web", True, SaveContext(has_private=True))
        self.assertIn("replaces the password saved for this host", offer.message)

    def test_first_prompt_and_phrases(self) -> None:
        self.assertEqual(PasswordRequest("web", TARGET, 1).message, "Enter the password for ops@web.internal:22.")
        self.assertEqual(source_phrase("host"), "saved password (host-specific password)")
        self.assertEqual(source_phrase("vault"), "saved password (vault entry)")
        self.assertEqual(source_phrase("legacy"), "saved password")
        self.assertEqual(source_phrase("retry"), "password you entered")

    def test_repr_hides_passwords(self) -> None:
        flow = self.flow(stored=SECRET)
        flow.attempt("retry", SECRET)
        self.assertNotIn(SECRET, repr(flow) + str(flow))


# ---------------------------------------------------------------- controller
class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        from ssh_manager.gtkui.controllers.credentials_controller import CredentialController

        self.tmp = tempfile.TemporaryDirectory()
        self.keyring = RecordingKeyring()
        self.secure = SecureStore(self.keyring)
        self.path = Path(self.tmp.name) / "credentials.json"
        self.store = CredentialStore(self.secure, path=self.path)
        self.shared = self.store.add("Ops", "ops", "shared-pw")
        self.web = HostEntry("web", {"hostname": "web.internal"}, parent_group="Acme", child_group="prod", id="h-web")
        self.db = HostEntry("db", {"hostname": "db.internal"}, parent_group="Acme", child_group="prod", id="h-db")
        self.group = GroupConfig(parent="Acme", name="prod", path=Path(), hosts=[self.web, self.db])
        self.notes = []
        self.rebuilds = []
        self.win = SimpleNamespace(
            _credential_store=self.store, _secure_store=self.secure, _vault=None,
            _notify=lambda message, *a, **k: self.notes.append(message),
            _settings=SimpleNamespace(),
            manager=SimpleNamespace(allows_display_names=True, read_only=False, tree={"Acme": {"prod": self.group}}),
            tree=SimpleNamespace(read_only_keys=set(), make_host_key=lambda p, c, a: f"{p}::{c}::{a}",
                                 rebuild=lambda refresh=False: self.rebuilds.append(refresh)),
        )
        self.creds = CredentialController(self.win)
        self.store.bind_many({"h-web": self.shared.id, "h-db": self.shared.id})
        self.keyring.writes.clear()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_creates_then_updates_the_host_password(self) -> None:
        self.assertEqual(self.creds.save_host_password(self.web, SECRET), ("saved", "Password saved for ‘web’."))
        private = self.store.private_for_host("h-web")
        self.assertEqual((private.username, private.based_on), ("ops", self.shared.id))
        self.assertEqual(self.creds.resolve_password(self.web), PasswordResolution(SECRET, "host", private.id))
        self.assertEqual(self.creds.save_host_password(self.web, "second"), ("saved", "Password saved for ‘web’."))
        self.assertEqual(self.store.private_for_host("h-web").id, private.id)  # updated, not added
        self.assertEqual(self.creds.resolve_password(self.web).password, "second")
        # The shared credential and the other host are untouched.
        self.assertEqual(self.store.get_password(self.shared.id), "shared-pw")
        self.assertEqual(self.creds.resolve_password(self.db), PasswordResolution("shared-pw", "credential",
                                                                                  self.shared.id))
        self.assertEqual(self.creds.password_summary(self.web), "host-specific")
        self.assertTrue(self.rebuilds)

    def test_rename_while_the_dialog_is_open_still_saves_by_id(self) -> None:
        renamed = HostEntry("web-renamed", dict(self.web.options), parent_group="Acme", child_group="prod", id="h-web")
        self.group.hosts[0] = renamed
        status, message = self.creds.save_host_password(self.web, SECRET)
        self.assertEqual((status, message), ("saved", "Password saved for ‘web-renamed’."))
        self.assertEqual(self.creds.resolve_password(renamed).password, SECRET)

    def test_host_deleted_while_the_dialog_is_open(self) -> None:
        self.group.hosts.remove(self.web)
        before = self.path.read_bytes()
        self.assertEqual(self.creds.save_host_password(self.web, SECRET),
                         ("gone", "‘web’ no longer exists; the password was not saved."))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.keyring.writes, [])

    def test_folder_inventory_host_moved_is_gone(self) -> None:
        legacy = HostEntry("web", {"hostname": "web.internal"}, parent_group="Acme", child_group="old")
        self.assertEqual(self.creds.save_host_password(legacy, SECRET)[0], "gone")

    def test_store_error_keeps_everything_and_explains(self) -> None:
        before = self.path.read_bytes()
        self.keyring.fail = True
        status, message = self.creds.save_host_password(self.web, SECRET)
        self.assertEqual(status, "error")
        self.assertNotIn(SECRET, message)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.store.private_for_host("h-web"))
        self.keyring.fail = False  # "Retry save"
        self.assertEqual(self.creds.save_host_password(self.web, SECRET)[0], "saved")

    def test_permissions_and_read_only_inventory_block_saving(self) -> None:
        self.web.metadata["permissions"] = {"can_edit": False}
        context = self.creds.password_save_context(self.web)
        self.assertFalse(context.can_save)
        self.assertEqual(self.creds.save_host_password(self.web, SECRET)[0], "blocked")
        self.web.metadata.clear()
        self.win.manager.read_only = True
        self.assertEqual(self.creds.password_save_context(self.web).reason, "The inventory is read-only.")
        self.assertEqual(self.creds.save_host_password(self.web, SECRET)[0], "blocked")
        self.assertIsNone(self.store.private_for_host("h-web"))

    def test_context_names_the_shared_credential_and_a_vault(self) -> None:
        context = self.creds.password_save_context(self.web)
        self.assertEqual((context.can_save, context.credential_name, context.has_private), (True, "Ops", False))
        self.creds.save_host_password(self.web, SECRET)
        context = self.creds.password_save_context(self.web)
        self.assertEqual((context.credential_name, context.has_private), ("Ops", True))
        self.web.metadata["vault_id"] = "v1"
        self.assertTrue(self.creds.password_save_context(self.web).vault_label)

    def test_describe_password_source(self) -> None:
        self.assertEqual(self.creds.describe_password_source(PasswordResolution("x", "credential", self.shared.id)),
                         "saved password (credential ‘Ops’)")
        self.assertEqual(self.creds.describe_password_source(PasswordResolution("x", "host", "p")),
                         "saved password (host-specific password)")

    def test_in_session_password_change_updates_only_the_host_password(self) -> None:
        self.creds.save_host_password(self.web, SECRET)
        self.secure.set("web.internal", 22, "ops", "legacy-pw")
        self.assertTrue(self.creds.save_detected_password_change(self.web, "changed-in-shell"))
        self.assertEqual(self.creds.resolve_password(self.web).password, "changed-in-shell")
        self.assertEqual(self.secure.get("web.internal", 22, "ops"), "legacy-pw")
        self.assertEqual(self.store.get_password(self.shared.id), "shared-pw")


# ------------------------------------------------------------------ GTK tab
def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


def _pump() -> None:
    from gi.repository import Gtk

    for _ in range(50):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)


class FakeDialog:
    def __init__(self, request, on_done) -> None:
        self.request = request
        self.on_done = on_done
        self.dismissed = False

    def dismiss(self) -> None:
        self.dismissed = True


class FakeService:
    """The CredentialController side of a tab (records saves; scripted results)."""

    def __init__(self, context=None, results=None) -> None:
        self.context = context or SaveContext(credential_name="Ops")
        self.results = list(results or [("saved", "Password saved for ‘web’.")])
        self.saved = []

    def describe_password_source(self, resolution):
        return source_phrase(resolution.source, "Ops")

    def password_save_context(self, host):
        return self.context

    def save_host_password(self, host, password):
        self.saved.append(password)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


@unittest.skipUnless(_display(), "needs a display")
class TabFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}).start()
        mock.patch("ssh_manager.gtk_terminal.shutil.which", return_value="/usr/bin/sshpass").start()
        mock.patch("ssh_manager.gtk_terminal.os.kill").start()
        self.requests = []
        self.offers = []
        mock.patch("ssh_manager.gtk_terminal.PasswordRequestDialog",
                   side_effect=lambda parent, request, on_done: self._new(self.requests, request, on_done)).start()
        mock.patch("ssh_manager.gtk_terminal.SavePasswordDialog",
                   side_effect=lambda parent, offer, on_decision: self._new(self.offers, offer, on_decision)).start()
        self.logs = []

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.tmp.cleanup()

    @staticmethod
    def _new(bucket, request, callback):
        bucket.append(FakeDialog(request, callback))
        return bucket[-1]

    def tab(self, resolution=PasswordResolution("credential-pw", "credential", "c1"), service=None, autostart=True):
        from ssh_manager.gtk_terminal import TerminalTab

        self.service = service if service is not None else FakeService()
        with mock.patch("ssh_manager.gtk_terminal.Vte.Terminal.spawn_async"):
            tab = TerminalTab(HostEntry("web", {"hostname": "web.internal", "user": "ops"}),
                              lambda message, level: self.logs.append(message), autostart=False,
                              password_provider=lambda: resolution, password_service=self.service)
        tab._terminal.spawn_async = mock.Mock()
        tab._paramiko_mode = not autostart  # autostart=False above only kept the constructor from spawning
        self.addCleanup(lambda: tab.get_parent() is None and tab.destroy())
        if autostart:
            tab._begin_connect()
        return tab

    @staticmethod
    def started(tab, pid=4242):
        """Finish the pending spawn; return ``(argv, envv)``."""

        call = tab._terminal.spawn_async.call_args[0]
        call[9](tab._terminal, pid, None, None)
        return call[2], call[3]

    @staticmethod
    def denied(tab, output="Permission denied, please try again.", status=255):
        with mock.patch.object(type(tab), "_auth_failure_output", return_value=output):
            tab._on_child_exited(tab._terminal, status << 8)

    @staticmethod
    def fire_retry(tab):
        from gi.repository import GLib

        GLib.source_remove(tab._retry_timeout_id)
        tab._prompt_retry_after_failure()

    @staticmethod
    def login(tab):
        tab._marker.write_text("")
        tab._poll_marker()

    def test_rejected_credential_then_typed_password_then_save(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.assertEqual(self.requests, [])  # ≥ 1 s later, not at once
        self.fire_retry(tab)
        request = self.requests[-1].request
        self.assertEqual(request.message, "‘web’ rejected the saved password (credential ‘Ops’). "
                                          "Enter the password for ops@web.internal:22.")
        self.assertEqual(request.attempt_text, "Attempt 1 of 3")
        self.requests[-1].on_done(SECRET)
        argv, envv = self.started(tab)
        self.assertIn(f"SSHPASS={SECRET}", envv)
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(self.offers, [])  # nothing before a verified login
        self.login(tab)
        offer = self.offers[-1].request
        self.assertEqual((offer.title, offer.verified), ("Save this password for ‘web’?", True))
        self.assertIsNone(self.offers[-1].on_done(Decision.SAVE))
        self.assertEqual(self.service.saved, [SECRET])
        self.assertIn("Password saved for ‘web’.", self.logs)

    def test_retry_save_after_a_store_error(self) -> None:
        service = FakeService(results=[("error", "Saved passwords are locked."), ("saved", "Password saved for ‘web’.")])
        tab = self.tab(service=service)
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        self.login(tab)
        self.assertEqual(self.offers[-1].on_done(Decision.SAVE), "Saved passwords are locked.")  # stays open
        self.assertTrue(tab.authenticated)  # the session is untouched
        self.assertIsNone(self.offers[-1].on_done(Decision.SAVE))
        self.assertEqual(service.saved, [SECRET, SECRET])

    def test_dont_save_keeps_it_for_the_tab_only_and_never_asks_again(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        self.login(tab)
        self.offers[-1].on_done(Decision.DONT_SAVE)  # also what Escape does
        self.assertEqual(self.service.saved, [])
        tab._set_status("Disconnected", False)
        tab.reconnect()
        _argv, envv = self.started(tab)
        self.assertIn(f"SSHPASS={SECRET}", envv)  # this tab keeps using it
        self.login(tab)
        self.assertEqual(len(self.offers), 1)

    def test_stored_password_that_works_is_never_offered(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.login(tab)
        self.assertEqual((self.requests, self.offers), ([], []))

    def test_first_connect_without_a_stored_password(self) -> None:
        tab = self.tab(resolution=PasswordResolution(None, "none"))
        request = self.requests[-1].request
        self.assertEqual((request.message, request.attempt), ("Enter the password for ops@web.internal:22.", 1))
        tab._terminal.spawn_async.assert_not_called()  # waits for the answer (non-blocking)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        self.login(tab)
        self.assertEqual(len(self.offers), 1)

    def test_cancel_stops_and_keeps_reconnect(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        calls = tab._terminal.spawn_async.call_count
        self.requests[-1].on_done(None)
        self.assertEqual(tab._terminal.spawn_async.call_count, calls)
        self.assertEqual(tab._status_label.get_text(), "Authentication failed")
        self.assertTrue(tab._reconnect_button.get_sensitive())

    def test_typed_password_equal_to_the_stored_one_is_not_offered(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done("credential-pw")
        self.started(tab)
        self.login(tab)
        self.assertEqual(self.offers, [])

    def test_vault_entry_overrides(self) -> None:
        tab = self.tab(service=FakeService(context=SaveContext(vault_label="Prod vault")))
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        self.login(tab)
        self.assertEqual(self.offers, [])
        self.assertTrue(any("overrides" in message for message in self.logs))

    def test_read_only_host_offers_session_only(self) -> None:
        tab = self.tab(service=FakeService(context=SaveContext(can_save=False, reason="The inventory is read-only.")))
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        self.login(tab)
        self.assertFalse(self.offers[-1].request.context.can_save)
        self.offers[-1].on_done(Decision.SESSION_ONLY)
        self.assertEqual(self.service.saved, [])

    def test_no_service_means_no_offer(self) -> None:
        tab = self.tab(service=False)  # an auxiliary session
        tab._password_service = None
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        self.login(tab)
        self.assertEqual(self.offers, [])

    def test_missing_sshpass_never_prompts_or_offers(self) -> None:
        with mock.patch("ssh_manager.gtk_terminal.shutil.which", return_value=None):
            tab = self.tab()
            self.started(tab)
            self.login(tab)
        self.assertEqual((self.requests, self.offers), ([], []))

    def test_reconnect_while_the_retry_dialog_is_open_closes_it(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        dialog = self.requests[-1]
        tab._set_status("Disconnected", False)
        tab.reconnect()
        self.assertTrue(dialog.dismissed)
        self.assertIsNone(tab._password_dialog)

    def test_closing_the_tab_dismisses_dialogs_and_sources(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        dialog = self.requests[-1]
        tab._schedule_retry_prompt()
        tab.destroy()
        self.assertTrue(dialog.dismissed)
        self.assertEqual((tab._retry_timeout_id, tab._auth_watch_source_id), (0, 0))

        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        self.login(tab)
        offer = self.offers[-1]
        tab.destroy()
        self.assertTrue(offer.dismissed)
        self.assertEqual(self.service.saved, [])

    def test_two_failing_tabs_keep_separate_state(self) -> None:
        first, second = self.tab(), self.tab()
        for tab in (first, second):
            self.started(tab)
            self.denied(tab)
            self.fire_retry(tab)
        first_dialog, second_dialog = self.requests[-2], self.requests[-1]
        first_dialog.on_done("first-pw")
        self.assertIsNotNone(second._password_dialog)  # untouched
        self.assertEqual((first._attempts, second._attempts), (1, 1))
        _a, envv = self.started(first)
        self.assertIn("SSHPASS=first-pw", envv)
        second_dialog.on_done("second-pw")
        _a, envv = self.started(second)
        self.assertIn("SSHPASS=second-pw", envv)

    def test_edge_case_passwords_only_travel_through_sshpass(self) -> None:
        for password in EDGE_PASSWORDS:
            with self.subTest(password=password[:20]):
                tab = self.tab()
                self.started(tab)
                self.denied(tab)
                self.fire_retry(tab)
                self.requests[-1].on_done(password)
                argv, envv = self.started(tab)
                self.assertIn(f"SSHPASS={password}", envv)
                self.assertNotIn(password.strip() or password, " ".join(argv))

    def test_disconnect_right_after_login_still_asks(self) -> None:
        tab = self.tab()
        self.started(tab)
        self.denied(tab)
        self.fire_retry(tab)
        self.requests[-1].on_done(SECRET)
        self.started(tab)
        tab._marker.write_text("")
        tab._on_child_exited(tab._terminal, 0)  # logged in, then exited before the next poll
        self.assertEqual(len(self.offers), 1)

    def test_paramiko_backend_offers_after_its_own_login(self) -> None:
        tab = self.tab(autostart=False)
        tab.offer_save_password(SECRET)
        self.assertEqual(self.offers[-1].request.title, "Save this password for ‘web’?")
        self.offers[-1].on_done(Decision.SAVE)
        self.assertEqual(self.service.saved, [SECRET])

    def test_password_never_reaches_logs_or_notifications(self) -> None:
        with self.assertLogs("ssh_manager", level=logging.DEBUG) as captured:
            logging.getLogger("ssh_manager").debug("start")
            service = FakeService(results=[("error", "Could not save the password."), ("saved", "Password saved.")])
            tab = self.tab(service=service)
            self.started(tab)
            self.denied(tab)
            self.fire_retry(tab)
            self.requests[-1].on_done(SECRET)
            self.started(tab)
            self.login(tab)
            self.offers[-1].on_done(Decision.SAVE)
            self.offers[-1].on_done(Decision.SAVE)
        text = "\n".join(captured.output + self.logs + [repr(d.request) for d in self.requests + self.offers])
        self.assertNotIn(SECRET.strip(), text)


@unittest.skipUnless(_display(), "needs a display")
class DialogTests(unittest.TestCase):
    """The real dialogs: text, buttons, default, Escape, accessibility."""

    def test_password_request_dialog(self) -> None:
        from gi.repository import Gtk

        from ssh_manager.gtkui.dialogs.password import PasswordRequestDialog

        answers = []
        request = PasswordRequest("web", TARGET, 2, rejected=source_phrase("credential", "Ops"))
        dialog = PasswordRequestDialog(None, request, answers.append)
        self.assertEqual(dialog.get_title(), "Password for ‘web’")
        self.assertIn("rejected the saved password (credential ‘Ops’)", dialog.message_label.get_text())
        self.assertEqual(dialog.attempt_label.get_text(), "Attempt 2 of 3")
        self.assertEqual(dialog.primary_button.get_label(), "_Retry")
        self.assertFalse(dialog.primary_button.get_sensitive())  # empty = no Retry
        self.assertEqual(dialog.secret.entry.get_accessible().get_name(), "Password for web")
        self.assertFalse(dialog.secret.entry.get_visibility())
        dialog.secret.entry.set_text(SECRET)
        self.assertTrue(dialog.primary_button.get_sensitive())
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(answers, [SECRET])

        dialog = PasswordRequestDialog(None, PasswordRequest("web", TARGET, 1), answers.append)
        self.assertEqual(dialog.primary_button.get_label(), "C_onnect")
        dialog.secret.entry.set_text("typed")
        dialog.emit("close")  # Escape
        _pump()
        self.assertEqual(answers, [SECRET, None])

    def test_save_password_dialog(self) -> None:
        from gi.repository import Gtk

        from ssh_manager.gtkui.dialogs.password import SavePasswordDialog

        decisions = []
        replies = ["Saved passwords are locked.", None]
        offer = SaveOffer("web", True, SaveContext(credential_name="Ops"))
        dialog = SavePasswordDialog(None, offer, lambda d: decisions.append(d) or replies.pop(0))
        self.assertEqual(dialog.primary_button.get_label(), "_Save for This Host")
        self.assertEqual(dialog.dont_save_button.get_label(), "_Don't Save")
        self.assertTrue(dialog.primary_button.has_default() or dialog.primary_button.get_can_default())
        self.assertEqual(dialog.get_accessible().get_name(), "Save this password for ‘web’?")
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(dialog.primary_button.get_label(), "_Retry Save")  # stays open on an error
        self.assertTrue(dialog.error_label.get_visible())
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(decisions, [Decision.SAVE, Decision.SAVE])

        dialog = SavePasswordDialog(None, offer, lambda d: decisions.append(d))
        dialog.emit("close")  # Escape = Don't save
        _pump()
        self.assertEqual(decisions[-1], Decision.DONT_SAVE)

        dialog = SavePasswordDialog(None, SaveOffer("web", True, SaveContext(can_save=False)),
                                    lambda d: decisions.append(d))
        self.assertEqual(dialog.primary_button.get_label(), "_Use for This Session")
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(decisions[-1], Decision.SESSION_ONLY)


@unittest.skipUnless(_display(), "needs a display")
class EndToEndTests(unittest.TestCase):
    """The real window and CredentialController; only the ssh process and the dialogs are faked."""

    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.tmp = tempfile.TemporaryDirectory()
        mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}).start()
        mock.patch("ssh_manager.gtk_terminal.shutil.which", return_value="/usr/bin/sshpass").start()
        mock.patch("ssh_manager.gtk_terminal.os.kill").start()
        self.spawn = mock.patch("ssh_manager.gtk_terminal.Vte.Terminal.spawn_async").start()
        self.requests, self.offers = [], []
        mock.patch("ssh_manager.gtk_terminal.PasswordRequestDialog",
                   side_effect=lambda p, r, cb: self.requests.append(FakeDialog(r, cb)) or self.requests[-1]).start()
        mock.patch("ssh_manager.gtk_terminal.SavePasswordDialog",
                   side_effect=lambda p, o, cb: self.offers.append(FakeDialog(o, cb)) or self.offers[-1]).start()
        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.keyring = RecordingKeyring()
        self.win._secure_store = SecureStore(self.keyring)
        self.win._credential_store = CredentialStore(self.win._secure_store, path=self.sandbox.root / "creds.json")
        self.shared = self.win._credential_store.add("Ops", "ops", "shared-pw")
        self.host = self.win.tree.find_host_by_alias("web-01")
        self.other = self.win.tree.find_host_by_alias("web-02")
        self.win.creds.bind_many({self.host.id: self.shared.id, self.other.id: self.shared.id})
        self.audit = self.sandbox.root / "audit.log"
        self.win._settings.enable_audit_logging = True
        self.win._settings.audit_log_path = str(self.audit)

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.tmp.cleanup()

    def open_tab(self):
        self.win.sessions.open_hosts([self.host])
        tab = self.win.terminal_tabs._tabs[-1]
        call = self.spawn.call_args[0]
        call[9](tab._terminal, 4242, None, None)
        return tab, call

    def test_rejected_credential_prompt_save_and_reconnect_without_prompting(self) -> None:
        notes = []
        self.win._notify = lambda message, *a, **k: notes.append(message)
        tab, call = self.open_tab()
        self.assertIn("SSHPASS=shared-pw", call[3])
        with mock.patch.object(type(tab), "_auth_failure_output", return_value="Permission denied, please try again."):
            tab._on_child_exited(tab._terminal, 255 << 8)
        from gi.repository import GLib

        GLib.source_remove(tab._retry_timeout_id)
        tab._prompt_retry_after_failure()
        self.assertIn("credential ‘Ops’", self.requests[-1].request.message)
        self.requests[-1].on_done(SECRET)
        self.spawn.call_args[0][9](tab._terminal, 4243, None, None)
        tab._marker.write_text("")
        tab._poll_marker()
        self.assertIn("The credential ‘Ops’ and other hosts are not changed.", self.offers[-1].request.message)
        self.assertIsNone(self.offers[-1].on_done(Decision.SAVE))

        store = self.win._credential_store
        self.assertIsNotNone(store.private_for_host(self.host.id))
        self.assertEqual(store.get_password(self.shared.id), "shared-pw")
        self.assertEqual(self.win.creds.resolve_password(self.other).password, "shared-pw")
        self.assertEqual(self.win.creds.password_summary(self.host), "host-specific")

        prompts = len(self.requests)
        _tab2, call = self.open_tab()  # the next connect uses the host's own password, no prompt
        self.assertIn(f"SSHPASS={SECRET}", call[3])
        self.assertEqual(len(self.requests), prompts)

        for _ in range(100):  # the audit line is appended by a background thread
            if self.audit.exists() and "connect" in self.audit.read_text():
                break
            time.sleep(0.02)
        self.assertIn("[connect]", self.audit.read_text())
        everything = "\n".join(notes) + self.audit.read_text()
        everything += (self.sandbox.root / "creds.json").read_text()
        self.assertNotIn(SECRET.strip(), everything)
        host = self.win.tree.find_host_by_alias("web-01")
        self.assertNotIn(SECRET.strip(), repr(host.options) + repr(host.metadata))


class ConnectorTests(unittest.TestCase):
    """Paramiko backend: the same retry dialog (max 3), nothing saved, the UI is told after a login."""

    def connector(self, responses, accept):
        from ssh_manager.connection_state import ConnectionStateMachine
        from ssh_manager.connector import Connector, TryResult
        from tests.test_connector import FakePasswordDialog, Settings

        self.dialog = FakePasswordDialog(responses)
        self.keyring = RecordingKeyring()
        connector = Connector(self.dialog, SecureStore(self.keyring), Settings(), ConnectionStateMachine(),
                              main_invoke=lambda fn: fn(), main_queue=lambda cb, a, k: cb(*a, **(k or {})))
        self.attempts = []

        def fake_try(_cfg, password, _timeouts):
            self.attempts.append(password)
            ok = password == accept
            return TryResult(ok=ok, is_auth_error=not ok, message="", client="c" if ok else None)

        connector._try_paramiko = fake_try
        self.aborted, self.errors, self.verified = [], [], []
        connector.add_aborted_callback(self.aborted.append)
        connector.add_error_callback(self.errors.append)
        connector.add_password_verified_callback(self.verified.append)
        return connector

    def run_connect(self, connector, resolution=None) -> None:
        from tests.test_connector import HostCfg

        cfg = HostCfg()
        cfg.alias = "web"
        connector.connect_to_host(cfg, password_provider=(lambda: resolution) if resolution else None,
                                  describe_source=lambda r: source_phrase(r.source, "Ops"))
        for _ in range(100):
            if connector._worker_thread is None:
                return
            time.sleep(0.02)
        self.fail("connector did not finish")

    def test_three_typed_attempts_then_stop(self) -> None:
        connector = self.connector([("w1", False), ("w2", False), ("w3", False), ("never", False)], accept="ok")
        self.run_connect(connector, PasswordResolution("shared-pw", "credential", "c1"))
        self.assertEqual(self.attempts, ["shared-pw", "w1", "w2", "w3"])
        self.assertEqual([r.attempt for r in self.dialog.requests], [1, 2, 3])
        self.assertEqual(self.dialog.requests[0].rejected, "saved password (credential ‘Ops’)")
        self.assertEqual(self.dialog.requests[0].title, "Password for ‘web’")
        self.assertEqual(self.aborted, ["Authentication failed for web after 3 attempts."])
        self.assertEqual((self.verified, self.keyring.writes), ([], []))

    def test_typed_password_that_works_is_reported_not_saved(self) -> None:
        connector = self.connector([(SECRET, False)], accept=SECRET)
        self.run_connect(connector, PasswordResolution("shared-pw", "credential", "c1"))
        self.assertEqual(self.verified, [SECRET])
        self.assertEqual(self.keyring.writes, [])

    def test_stored_password_that_works_is_not_reported(self) -> None:
        connector = self.connector([], accept="shared-pw")
        self.run_connect(connector, PasswordResolution("shared-pw", "credential", "c1"))
        self.assertEqual((self.verified, self.dialog.requests), ([], []))

    def test_empty_password_cancels(self) -> None:
        connector = self.connector([("", False)], accept="ok")
        self.run_connect(connector)
        self.assertEqual(self.aborted, ["User cancelled authentication."])


@unittest.skipUnless(_display(), "needs a display")
class ParamikoWiringTests(unittest.TestCase):
    def test_verified_password_reaches_the_tab(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox(store=True)
        self.addCleanup(sandbox.close)
        win = sandbox.window
        host = win.tree.find_host_by_alias("web-01")
        captured = {}
        with mock.patch("ssh_manager.gtkui.controllers.session_controller.Connector.connect_to_host",
                        side_effect=lambda cfg, password_provider=None, describe_source=None:
                        captured.update(cfg=cfg, describe=describe_source)):
            win.sessions.open_host_paramiko(host, password_provider=lambda: PasswordResolution(None, "none"))
        tab = win.terminal_tabs._tabs[-1]
        connector = win.sessions.paramiko_connectors[tab]
        self.assertEqual(captured["cfg"].alias, "web-01")
        self.assertFalse(hasattr(captured["cfg"], "remember_password_default"))
        self.assertEqual(connector._verified_callbacks, [tab.offer_save_password])
        offers = []
        with mock.patch("ssh_manager.gtk_terminal.SavePasswordDialog",
                        side_effect=lambda p, o, cb: offers.append(FakeDialog(o, cb)) or offers[-1]):
            connector._verified_callbacks[0](SECRET)
        self.assertEqual(offers[-1].request.title, "Save this password for ‘web-01’?")


if __name__ == "__main__":
    unittest.main()
