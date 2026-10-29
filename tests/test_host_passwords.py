"""Phase 2: a password specific to one host, stored as a private credential."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ssh_manager.config import HostEntry
from ssh_manager.credentials import CredentialStore, PasswordChoice
from ssh_manager.secure_store import InMemoryKeyring, SecureStore, SecureStoreError

SECRET = "h0st-only pa$$"


class RecordingKeyring(InMemoryKeyring):
    """In-memory keyring that records writes and can be made to fail."""

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

    def keys(self):
        return sorted(username for _service, username in self._store)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "credentials.json"
        self.keyring = RecordingKeyring()
        self.secure = SecureStore(self.keyring)
        self.store = CredentialStore(self.secure, path=self.path)
        self.shared = self.store.add("Ops", "ops", "shared-pw")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reload(self) -> CredentialStore:
        return CredentialStore(self.secure, path=self.path)

    def secret_keys(self):
        return [key for key in self.keyring.keys() if key.startswith("credential:")]

    def assert_no_orphans(self, store=None) -> None:
        store = store or self.store
        known = {f"credential:{c.id}" for c in store.list(include_private=True)}
        self.assertTrue(set(self.secret_keys()) <= known, (self.secret_keys(), known))
        saved = json.loads(self.path.read_text())
        bound = set(saved["bindings"].values())
        for entry in saved["credentials"]:
            if entry.get("private"):
                self.assertIn(entry["id"], bound)

    def test_private_flag_round_trips_and_old_files_load(self) -> None:
        private = self.store.create_private("h1", "ops", SECRET, label="web", based_on=self.shared.id)
        again = self.reload().get(private.id)
        self.assertEqual((again.private, again.based_on, again.name), (True, self.shared.id, "host:web"))
        self.assertFalse(self.reload().get(self.shared.id).private)
        self.path.write_text(json.dumps({  # a file from before the field existed, plus an unknown field
            "credentials": [{"id": "c9", "name": "Old", "username": "bob", "colour": "red"}],
            "bindings": {"h9": "c9"},
        }))
        old = self.reload()
        self.assertFalse(old.get("c9").private)
        self.assertEqual(old.binding_for_host("h9"), "c9")

    def test_private_without_username_loads(self) -> None:
        private = self.store.create_private("h1", "", None, label="web")
        self.assertIsNotNone(self.reload().get(private.id))

    def test_list_hides_private_credentials(self) -> None:
        private = self.store.create_private("h1", "ops", SECRET)
        self.assertEqual([c.id for c in self.store.list()], [self.shared.id])
        self.assertEqual({c.id for c in self.store.list(include_private=True)}, {self.shared.id, private.id})
        self.assertIs(self.store.get(private.id), private)
        self.assertTrue(self.store.is_private(private.id))
        self.assertFalse(self.store.is_private(self.shared.id))

    def test_create_private_binds_once_and_keeps_the_shared_secret(self) -> None:
        self.store.bind_host("h1", self.shared.id)
        private = self.store.create_private("h1", "ops", SECRET, based_on=self.shared.id)
        self.assertEqual(self.store.binding_for_host("h1"), private.id)
        self.assertEqual(self.store.private_for_host("h1"), private)
        self.assertEqual(self.store.get_password(private.id), SECRET)
        self.assertEqual(self.store.get_password(self.shared.id), "shared-pw")

    def test_create_private_twice_updates_instead_of_adding(self) -> None:
        first = self.store.create_private("h1", "ops", SECRET)
        second = self.store.create_private("h1", "ops", "newer")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.list(include_private=True)), 2)
        self.assertEqual(self.store.get_password(first.id), "newer")

    def test_create_private_rolls_back_when_saving_fails(self) -> None:
        before = (self.path.read_bytes(), self.keyring.keys())
        with mock.patch("ssh_manager.credentials.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(SecureStoreError):
                self.store.create_private("h1", "ops", SECRET)
        self.assertEqual((self.path.read_bytes(), self.keyring.keys()), before)
        self.assertIsNone(self.store.binding_for_host("h1"))
        self.assertEqual(len(self.store.list(include_private=True)), 1)

    def test_create_private_rolls_back_when_the_keyring_is_locked(self) -> None:
        before = self.path.read_bytes()
        self.keyring.fail = True
        with self.assertRaises(SecureStoreError):
            self.store.create_private("h1", "ops", SECRET)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.store.binding_for_host("h1"))

    def test_update_private_restores_the_secret_when_saving_fails(self) -> None:
        self.store.create_private("h1", "ops", SECRET)
        private = self.store.private_for_host("h1")
        with mock.patch("ssh_manager.credentials.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(SecureStoreError):
                self.store.update_private("h1", password="other", username="root")
        self.assertEqual(self.store.get_password(private.id), SECRET)
        self.assertEqual(private.username, "ops")

    def test_private_is_deleted_with_its_binding(self) -> None:
        cases = {
            "host deleted / unbound": lambda: self.store.bind_host("h1", None),
            "re-bound to a shared credential": lambda: self.store.bind_host("h1", self.shared.id),
            "bind_many": lambda: self.store.bind_many({"h1": None}),
            "pruned (store host deleted)": lambda: self.store.prune_bindings({"h2"}),
            "rebind onto it": lambda: (self.store.bind_host("h2", self.shared.id), self.store.rebind_host("h2", "h1")),
        }
        for name, action in cases.items():
            with self.subTest(name):
                private = self.store.create_private("h1", "ops", SECRET)
                action()
                self.assertIsNone(self.store.get(private.id))
                self.assertIsNone(self.keyring.get_password(self.secure.service_name, f"credential:{private.id}"))
                self.assertEqual(self.store.get_password(self.shared.id), "shared-pw")
                self.assert_no_orphans()
                self.assert_no_orphans(self.reload())

    def test_group_delete_removes_private_credentials_of_its_hosts(self) -> None:
        inside = self.store.create_private("prod::web::a", "ops", SECRET)
        outside = self.store.create_private("stage::web::b", "ops", SECRET)
        self.store.unbind_prefix("prod::")
        self.assertIsNone(self.store.get(inside.id))
        self.assertIsNotNone(self.store.get(outside.id))
        self.assert_no_orphans()

    def test_failed_save_keeps_the_private_credential(self) -> None:
        private = self.store.create_private("h1", "ops", SECRET)
        with mock.patch("ssh_manager.credentials.write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(SecureStoreError):
                self.store.bind_host("h1", None)
        self.assertEqual(self.store.binding_for_host("h1"), private.id)
        self.assertEqual(self.store.get_password(private.id), SECRET)

    def test_rename_and_move_keep_the_private_binding(self) -> None:
        private = self.store.create_private("prod::web::a", "ops", SECRET)
        self.store.rebind_host("prod::web::a", "prod::web::b")  # rename
        self.store.remap_bindings_prefix("prod::web::", "stage::web::")  # group moved
        self.assertEqual(self.store.private_for_host("stage::web::b"), private)
        self.assertEqual(self.store.get_password(private.id), SECRET)
        self.assert_no_orphans()

    def test_copies_bind_the_shared_credential(self) -> None:
        self.store.create_private("h1", "ops", SECRET, based_on=self.shared.id)
        self.assertEqual(self.store.shared_for_copy("h1"), self.shared.id)
        self.store.bind_host("h2", self.shared.id)
        self.assertEqual(self.store.shared_for_copy("h2"), self.shared.id)
        self.store.create_private("h3", "ops", SECRET)  # no shared credential behind it
        self.assertIsNone(self.store.shared_for_copy("h3"))
        self.store.delete(self.shared.id)  # the shared one is gone: the copy stays unbound
        self.assertIsNone(self.store.shared_for_copy("h1"))
        self.assertIsNotNone(self.store.private_for_host("h1"))

    def test_choice_repr_hides_the_password(self) -> None:
        self.assertNotIn(SECRET, repr(PasswordChoice(True, SECRET)) + str(PasswordChoice(True, SECRET)))


class ControllerTests(unittest.TestCase):
    """CredentialController: resolution and the dialog's Save step (no window)."""

    def setUp(self) -> None:
        from ssh_manager.gtkui.controllers.credentials_controller import CredentialController

        self.tmp = tempfile.TemporaryDirectory()
        self.keyring = RecordingKeyring()
        self.secure = SecureStore(self.keyring)
        self.path = Path(self.tmp.name) / "credentials.json"
        self.store = CredentialStore(self.secure, path=self.path)
        self.shared = self.store.add("Ops", "ops", "shared-pw")
        self.notes = []
        self.win = SimpleNamespace(_credential_store=self.store, _secure_store=self.secure,
                                   _notify=lambda message, *a, **k: self.notes.append(message),
                                   manager=SimpleNamespace(allows_display_names=True))
        self.creds = CredentialController(self.win)
        self.host = HostEntry("web", {"hostname": "web.internal", "user": "ops"}, id="h-web")
        self.store.bind_host("h-web", self.shared.id)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resolution_uses_the_host_password_and_never_writes(self) -> None:
        self.secure.set("web.internal", 22, "ops", "legacy-pw")
        self.assertTrue(self.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET)))
        self.keyring.writes.clear()
        for _ in range(2):  # connecting twice (regression: the old code re-seeded on every connect)
            resolution = self.creds.resolve_password(self.host)
            self.assertEqual((resolution.password, resolution.source), (SECRET, "host"))
        self.assertEqual(self.keyring.writes, [])
        self.assertEqual(self.secure.get("web.internal", 22, "ops"), "legacy-pw")
        self.assertEqual(self.store.get_password(self.shared.id), "shared-pw")
        self.assertEqual(self.creds.host_with_resolved_identity(self.host).options["user"], "ops")

    def test_save_transitions(self) -> None:
        apply = self.creds.apply_host_credentials
        self.assertTrue(apply(self.host, self.shared.id, PasswordChoice(True, SECRET)))
        private = self.store.private_for_host("h-web")
        self.assertEqual((private.username, private.based_on), ("ops", self.shared.id))
        self.assertEqual(self.creds.password_summary(self.host), "host-specific")

        self.assertTrue(apply(self.host, self.shared.id, PasswordChoice(True, None)))  # empty entry: keep it
        self.assertEqual(self.store.get_password(private.id), SECRET)
        self.assertTrue(apply(self.host, self.shared.id, PasswordChoice(True, "changed")))
        self.assertEqual(self.store.get_password(private.id), "changed")
        self.assertTrue(apply(self.host, self.shared.id, PasswordChoice(True, clear=True)))
        self.assertIsNone(self.store.get_password(private.id))
        self.assertEqual(self.creds.resolve_password(self.host).source, "none")  # asked at connect

        self.assertTrue(apply(self.host, self.shared.id, PasswordChoice(False)))  # back to the credential's
        self.assertIsNone(self.store.get(private.id))
        self.assertEqual(self.store.binding_for_host("h-web"), self.shared.id)
        self.assertEqual(self.creds.password_summary(self.host), "credential ‘Ops’")
        self.assertEqual(self.creds.resolve_password(self.host).source, "credential")

    def test_specific_without_password_keeps_not_set(self) -> None:
        self.assertTrue(self.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, None)))
        private = self.store.private_for_host("h-web")
        self.assertIsNotNone(private)
        self.assertIsNone(self.store.get_password(private.id))
        self.assertEqual(self.creds.resolve_password(self.host).source, "none")

    def test_store_error_changes_nothing_and_says_so(self) -> None:
        before = (self.path.read_bytes(), self.keyring.keys())
        self.keyring.fail = True
        self.assertFalse(self.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET)))
        self.assertEqual((self.path.read_bytes(), self.keyring.keys()), before)
        self.assertEqual(self.notes, ["Could not save the password for ‘web’. Nothing was changed."])

    def test_password_store_problem(self) -> None:
        self.assertIsNone(self.creds.password_store_problem())
        with mock.patch.object(self.keyring, "get_password", side_effect=RuntimeError("locked")):
            self.assertEqual(self.creds.password_store_problem(), "The password store is unavailable. Nothing was changed.")

    def test_binding_for_copy(self) -> None:
        self.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET))
        self.assertEqual(self.creds.binding_for_copy(self.host), (self.shared.id, True))


def _display() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore

        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and Gtk.init_check(None)[0]
    except Exception:
        return False


@unittest.skipUnless(_display(), "needs a display")
class HostDialogTests(unittest.TestCase):
    def dialog(self, **kwargs):
        from ssh_manager.credentials import Credential
        from ssh_manager.gtkui.dialogs.host_dialog import HostDialog

        self.shared = Credential("c1", "Ops", "ops")
        self.other = Credential("c2", "Dev", "dev")
        kwargs.setdefault("credentials", [self.shared, self.other])
        kwargs.setdefault("selected_credential_id", "c1")
        dialog = HostDialog(None, HostEntry("web", {"hostname": "web.internal"}), **kwargs)
        self.addCleanup(dialog.destroy)
        return dialog

    def private(self):
        from ssh_manager.credentials import Credential

        return Credential("p1", "host:web", "ops", private=True, based_on="c1")

    def confirm(self, answer):
        patcher = mock.patch("ssh_manager.gtkui.dialogs.host_dialog.confirm", return_value=answer)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_default_uses_the_credential_and_names_it(self) -> None:
        dialog = self.dialog()
        self.assertFalse(dialog.host_specific())
        self.assertIn("Ops", dialog._pw_shared_radio.get_label())
        self.assertFalse(dialog._pw_secret.get_visible())
        self.assertEqual(dialog.password_choice(), PasswordChoice(False))

    def test_private_credentials_never_appear_in_the_combo(self) -> None:
        dialog = self.dialog(credentials=[self.private_cred_for_list()])
        model = dialog._credential_combo.get_model()
        self.assertEqual([row[0] for row in model], ["None"])

    def private_cred_for_list(self):
        from ssh_manager.credentials import Credential

        return Credential("p9", "host:x", "ops", private=True)

    def test_switch_to_specific_shows_the_entry(self) -> None:
        dialog = self.dialog()
        dialog._pw_host_radio.set_active(True)
        self.assertTrue(dialog._pw_secret.get_visible() and dialog._pw_secret.entry.get_visible())
        self.assertEqual(dialog._pw_status.get_text(), "Not set — you will be asked when you connect")
        self.assertEqual(dialog.password_choice(), PasswordChoice(True, None))
        dialog._pw_secret.entry.set_text(SECRET)
        self.assertEqual(dialog.password_choice(), PasswordChoice(True, SECRET))
        self.assertEqual(dialog._pw_secret.entry.get_accessible().get_name(), "Host password")

    def test_saved_password_change_and_clear(self) -> None:
        dialog = self.dialog(private_credential=self.private(), private_has_password=True)
        self.assertTrue(dialog.host_specific())
        self.assertEqual(dialog._pw_status.get_text(), "Saved")
        self.assertFalse(dialog._pw_secret.get_visible())
        self.assertTrue(dialog._pw_change_button.get_visible() and dialog._pw_clear_button.get_visible())
        self.assertEqual(dialog.password_choice(), PasswordChoice(True, None))  # keep
        dialog._pw_change_button.clicked()
        self.assertTrue(dialog._pw_secret.get_visible() and dialog._pw_secret.entry.get_visible())
        dialog._pw_secret.entry.set_text("new")
        self.assertEqual(dialog.password_choice(), PasswordChoice(True, "new"))
        dialog._pw_clear_button.clicked()
        self.assertEqual(dialog._pw_status.get_text(), "Not set — you will be asked when you connect")
        self.assertEqual(dialog.password_choice(), PasswordChoice(True, None, clear=True))

    def test_back_to_the_credential_asks_first(self) -> None:
        dialog = self.dialog(private_credential=self.private(), private_has_password=True)
        ask = self.confirm(False)
        dialog._pw_shared_radio.set_active(True)
        self.assertEqual(ask.call_count, 1)
        self.assertIn("will be deleted", ask.call_args[0][2])
        self.assertTrue(dialog.host_specific())  # declined: unchanged
        ask.return_value = True
        dialog._pw_shared_radio.set_active(True)
        self.assertFalse(dialog.host_specific())
        self.assertEqual(dialog.password_choice(), PasswordChoice(False))

    def test_changing_the_credential_in_specific_mode_asks_first(self) -> None:
        dialog = self.dialog(private_credential=self.private(), private_has_password=True)
        ask = self.confirm(False)
        dialog._credential_combo.set_active_id("c2")
        self.assertEqual(dialog.selected_credential_id(), "c1")  # declined: reverted
        self.assertTrue(dialog.host_specific())
        ask.return_value = True
        dialog._credential_combo.set_active_id("c2")
        self.assertEqual(dialog.selected_credential_id(), "c2")
        self.assertFalse(dialog.host_specific())

    def test_missing_shared_credential_is_explained(self) -> None:
        dialog = self.dialog(selected_credential_id=None, private_credential=self.private(), private_has_password=True)
        self.assertTrue(dialog._pw_shared_hint.get_visible())
        self.assertIn("no longer exists", dialog._pw_shared_hint.get_text())

    def test_locked_store_keeps_the_dialog_open(self) -> None:
        dialog = self.dialog(password_check=lambda: "Saved passwords are locked.")
        dialog.alias_entry.set_text("web")
        dialog._pw_host_radio.set_active(True)
        dialog._pw_secret.entry.set_text(SECRET)
        self.assertFalse(dialog.validate())
        self.assertEqual(dialog.layout.current_page, "credentials")
        self.assertEqual(dialog._pw_row.error.get_text(), "Saved passwords are locked.")

    def test_can_edit_false_disables_the_section(self) -> None:
        dialog = self.dialog(can_edit=False)
        for widget in (dialog._pw_shared_radio, dialog._pw_host_radio, dialog._pw_secret):
            self.assertFalse(widget.get_sensitive())
            self.assertIn("disabled", widget.get_tooltip_text())

    def test_no_credential_bound(self) -> None:
        dialog = self.dialog(selected_credential_id=None)
        self.assertFalse(dialog._pw_shared_radio.get_sensitive())
        self.assertIn("Choose a credential", dialog._pw_shared_hint.get_text())


@unittest.skipUnless(_display(), "needs a display")
class WindowTests(unittest.TestCase):
    """The real window (app-owned inventory): Save/Cancel, lifecycle and every connect path."""

    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.win = self.sandbox.window
        self.keyring = RecordingKeyring()
        self.win._secure_store = SecureStore(self.keyring)
        self.win._credential_store = CredentialStore(self.win._secure_store,
                                                     path=self.sandbox.root / "credentials.json")
        self.shared = self.win._credential_store.add("Ops", "ops", "shared-pw")
        self.host = self.win.tree.find_host_by_alias("web-01")
        self.win.creds.bind_credential(self.host.id, self.shared.id)
        self.win.tree.rebuild(refresh=True)

    def tearDown(self) -> None:
        self.sandbox.close()

    def run_dialog(self, action, answer, edit=lambda d: None):
        from gi.repository import Gtk

        from ssh_manager.gtkui.dialogs.host_dialog import HostDialog

        def fake_run(dialog):
            edit(dialog)
            return answer

        self.win.tree.select_host(self.host.parent_group, self.host.child_group, self.host.alias)
        with mock.patch.object(HostDialog, "run", fake_run), mock.patch.object(Gtk.Dialog, "run", fake_run):
            action()

    def set_specific(self, password):
        def edit(dialog):
            dialog._pw_host_radio.set_active(True)
            dialog._pw_secret.entry.set_text(password)
        return edit

    def private(self):
        return self.win._credential_store.private_for_host(self.host.id)

    def test_save_creates_and_cancel_discards(self) -> None:
        from gi.repository import Gtk

        self.run_dialog(self.win.hosts.edit_selected, Gtk.ResponseType.CANCEL, self.set_specific(SECRET))
        self.assertIsNone(self.private())
        self.run_dialog(self.win.hosts.edit_selected, Gtk.ResponseType.OK, self.set_specific(SECRET))
        private = self.private()
        self.assertEqual(self.win._credential_store.get_password(private.id), SECRET)
        self.assertEqual(self.win._credential_store.get_password(self.shared.id), "shared-pw")
        host = self.win.tree.find_host_by_alias("web-01")
        self.assertNotIn(SECRET, json.dumps(host.options) + json.dumps(host.metadata, default=str))
        self.assertEqual(self.win.creds.password_summary(host), "host-specific")
        from ssh_manager.gtkui.constants import TreeColumn

        key = self.win.tree.make_host_key(host.parent_group, host.child_group, host.alias)
        tooltip = self.win.tree_store.get_value(self.win.tree.host_iters[key], TreeColumn.TOOLTIP)
        self.assertIn("Password: host-specific", tooltip)
        self.assertNotIn(SECRET, tooltip)
        content = self.win.host_page._host_content(host, {}, 0.0)
        facts = {label: value for label, value, _mono in content.facts}
        self.assertEqual(facts["Password"], "host-specific")
        self.assertEqual(dict((label, value) for label, value, _m in content.info)["Auth"], "Host-specific password")
        self.assertNotIn(SECRET, repr(content))

    def test_every_connect_path_uses_the_host_password(self) -> None:
        self.win.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET))
        captured = {}

        def fake_new_terminal(host, profile=None, password_provider=None, password_service=None):
            captured["tab"] = password_provider()
            return mock.Mock()

        with mock.patch.object(self.win.terminal_tabs, "new_terminal", side_effect=fake_new_terminal), \
                mock.patch.object(self.win.sessions, "bind_tab_status"):
            self.win.sessions.open_hosts([self.host])
            tab_host = self.win.creds.host_with_resolved_identity(self.host)  # what a tab carries
            self.win.terminal_tabs.on_duplicate_session(SimpleNamespace(host=tab_host))
            captured["duplicate"] = captured["tab"]
        with mock.patch.object(self.win.sessions, "open_host_paramiko",
                               side_effect=lambda h, profile=None, password_provider=None:
                               captured.__setitem__("paramiko", password_provider())), \
                mock.patch.object(self.win._settings, "use_paramiko_vte", True):
            self.win.sessions.open_hosts([self.host])
        for path in ("tab", "duplicate", "paramiko"):
            self.assertEqual((captured[path].password, captured[path].source), (SECRET, "host"), path)

        from ssh_manager.gtk_terminal import TerminalTab

        tab = TerminalTab(self.host, lambda *a: None, autostart=False,
                          password_provider=lambda: self.win.creds.resolve_password(self.host))
        self.addCleanup(tab.destroy)
        self.assertEqual(tab._stored_stats_password(), SECRET)  # remote stats

    def test_rename_keeps_and_delete_removes_the_host_password(self) -> None:
        from gi.repository import Gtk

        self.win.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET))
        private_id = self.private().id
        self.run_dialog(self.win.hosts.edit_selected, Gtk.ResponseType.OK,
                        lambda d: d.alias_entry.set_text("web-renamed"))
        renamed = self.win.tree.find_host_by_alias("web-renamed")
        self.assertEqual(self.win.creds.resolve_password(renamed).password, SECRET)
        self.host = renamed
        with mock.patch.object(type(self.win), "_confirm", return_value=True):
            self.win.tree.select_host(renamed.parent_group, renamed.child_group, renamed.alias)
            self.win.hosts.delete_selected()
        self.assertIsNone(self.win._credential_store.get(private_id))
        self.assertNotIn(f"credential:{private_id}", self.keyring.keys())

    def test_group_delete_removes_host_passwords(self) -> None:
        self.win.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET))
        private_id = self.private().id
        ok, _msg = self.win.manager.delete_parent_group(self.host.parent_group)
        self.assertTrue(ok)
        self.win.tree.rebuild(refresh=True)
        self.assertIsNone(self.win._credential_store.get(private_id))
        self.assertNotIn(f"credential:{private_id}", self.keyring.keys())

    def test_duplicate_binds_the_shared_credential(self) -> None:
        from gi.repository import Gtk

        self.win.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET))
        notes = []
        with mock.patch.object(self.win, "_notify", side_effect=lambda message, *a, **k: notes.append(message)):
            self.run_dialog(self.win.hosts.duplicate_selected, Gtk.ResponseType.OK)
        copies = [h for h in self.win.tree.host_lookup.values()
                  if h.id != self.host.id and h.option("hostname") == self.host.option("hostname")]
        self.assertEqual(len(copies), 1)
        copy = copies[0]
        self.assertEqual(self.win._credential_store.binding_for_host(copy.id), self.shared.id)
        self.assertIsNotNone(self.private())  # the original keeps its own password
        self.assertTrue(any("host-specific password was not copied" in n for n in notes), notes)
        self.assertFalse(any(SECRET in n for n in notes))

    def test_credential_manager_and_mobaxterm_never_list_private_credentials(self) -> None:
        from ssh_manager.gtkui.dialogs.credentials import CredentialManagerDialog

        self.win.creds.apply_host_credentials(self.host, self.shared.id, PasswordChoice(True, SECRET))
        manager = CredentialManagerDialog(self.win, self.win._credential_store)
        self.addCleanup(manager.destroy)
        self.assertEqual([row[0] for row in manager.load_rows()], [self.shared.id])
        from ssh_manager.gtkui.dialogs.mobaxterm_import import MobaXtermImportDialog

        names = [c.id for c in MobaXtermImportDialog._credentials(SimpleNamespace(_store=self.win._credential_store))]
        self.assertEqual(names, [self.shared.id])


@unittest.skipUnless(_display(), "needs a display")
class FolderInventoryTests(unittest.TestCase):
    """Legacy folder inventory: bindings keyed by ``parent::child::alias`` follow renames."""

    def test_rename_moves_the_private_binding(self) -> None:
        from gi.repository import Gtk

        from ssh_manager.gtkui.dialogs.host_dialog import HostDialog
        from tests.gtk_window_fixture import WindowSandbox

        sandbox = WindowSandbox()
        self.addCleanup(sandbox.close)
        win = sandbox.window
        win._secure_store = SecureStore(RecordingKeyring())
        win._credential_store = CredentialStore(win._secure_store, path=sandbox.root / "credentials.json")
        host = win.tree.find_host_by_alias("web-02")
        win.creds.apply_host_credentials(host, None, PasswordChoice(True, SECRET))
        private_id = win._credential_store.private_for_host("prod::web.conf::web-02").id

        def fake_run(dialog):
            dialog.alias_entry.set_text("web-02b")
            return Gtk.ResponseType.OK

        win.tree.select_host("prod", "web.conf", "web-02")
        with mock.patch.object(HostDialog, "run", fake_run):
            win.hosts.edit_selected()
        self.assertIsNone(win._credential_store.binding_for_host("prod::web.conf::web-02"))
        self.assertEqual(win._credential_store.binding_for_host("prod::web.conf::web-02b"), private_id)
        renamed = win.tree.find_host_by_alias("web-02b")
        self.assertEqual(win.creds.resolve_password(renamed).password, SECRET)


if __name__ == "__main__":
    unittest.main()
