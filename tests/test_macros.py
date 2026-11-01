"""Terminal macros without GTK: model, validation, encrypted store, recorder, player."""

import base64
import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ophion import macros as M
from ophion.macros import Macro, MacroError, MacroFileError, MacroPlayer, MacroRecorder, MacroStore, MacroWriteError

PASSWORD = "correct horse"
SECRET = "S3cr3t-sentinel-pw"
NAME = "Deploy-sentinel-name"
TEXT = "echo text-sentinel"
SHORTCUT = "<Primary><Alt>F7"


def sample(name=NAME, **kw) -> Macro:
    steps = kw.pop("steps", [{"type": "text", "text": TEXT}, {"type": "key", "key": "Enter"},
                             {"type": "delay", "ms": 300}, {"type": "text", "text": SECRET, "secret": True}])
    return Macro(name=name, shortcut=[SHORTCUT], steps=steps, **kw)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "config"
        self.path = self.folder / M.FILE_NAME

    def store(self, password=PASSWORD) -> MacroStore:
        store = MacroStore(self.path)
        store.unlock(password)
        return store

    def saved(self, *macros) -> MacroStore:
        store = self.store()
        for macro in macros or (sample(),):
            store.add(macro)
        store.save()
        return store


class ModelTests(StoreCase):
    def test_validation_limits(self) -> None:
        store = self.store()
        for macro, code in (
            (sample(name="  "), "empty_name"),
            (sample(name="x" * 81), "long_name"),
            (sample(steps=[{"type": "delay", "ms": 5}]), "no_steps"),
            (sample(steps=[{"type": "text", "text": "a"}] * 5001), "too_many_steps"),
            (sample(steps=[{"type": "text", "text": "a" * (1024 * 1024 + 1)}]), "too_much_text"),
            (sample(timing="slow"), "bad_timing"),
            (sample(steps=[{"type": "key", "key": "Hyper+Q"}]), "bad_step"),
            (sample(steps=[{"type": "delay", "ms": -1}, {"type": "text", "text": "a"}]), "bad_step"),
        ):
            with self.assertRaises(MacroError) as caught:
                store.add(macro)
            self.assertEqual(caught.exception.code, code)
        store.add(sample(name="x" * 80))
        store.add(sample(name="Restart"))
        with self.assertRaises(MacroError) as caught:
            store.add(sample(name=" restart "))  # unique, case-insensitively
        self.assertEqual(caught.exception.code, "duplicate_name")
        with mock.patch.object(M, "MAX_MACROS", 2), self.assertRaises(MacroError) as caught:
            store.add(sample(name="third"))
        self.assertEqual(caught.exception.code, "too_many_macros")

    def test_round_trip_unknown_fields_and_steps_and_order(self) -> None:
        store = self.store()
        future = {"type": "mouse-click", "x": 3}
        store.add(Macro.from_dict({"name": "b", "steps": [{"type": "text", "text": "x"}, future], "colour": "red"}))
        store.add(sample(name="A", description="d", timing="fast", confirm_before_run=True))
        store.save()
        loaded = self.store()
        self.assertEqual([m.name for m in loaded.list_sorted()], ["A", "b"])
        a, b = loaded.list_sorted()
        self.assertEqual((a.shortcut, a.description, a.timing, a.confirm_before_run), ([SHORTCUT], "d", "fast", True))
        self.assertEqual(a.steps, sample().steps)
        self.assertIn(future, b.steps)  # kept, not played
        self.assertIsNone(M.step_bytes(future))

    def test_update_and_delete(self) -> None:
        store = self.saved()
        macro = store.list_sorted()[0].copy()
        macro.name = "Renamed"
        store.update(macro)
        store.save()
        self.assertEqual([m.name for m in self.store().list_sorted()], ["Renamed"])
        store.delete(macro.id)
        store.save()
        self.assertEqual(len(self.store()), 0)


class CryptoTests(StoreCase):
    def test_nothing_readable_in_the_file(self) -> None:
        self.saved()
        raw = self.path.read_bytes()
        header = json.loads(raw)
        self.assertEqual(set(header), {"format", "version", "kdf", "iterations", "salt", "cipher", "data"})
        self.assertEqual((header["kdf"], header["cipher"]), ("pbkdf2-sha256", "fernet"))
        self.assertGreaterEqual(header["iterations"], 600_000)
        token = base64.urlsafe_b64decode(header["data"])
        for sentinel in (SECRET, NAME, TEXT, SHORTCUT):  # long enough never to appear by chance
            for blob in (raw, token, json.dumps(header).encode()):
                self.assertNotIn(sentinel.encode(), blob, sentinel)
                self.assertNotIn(base64.b64encode(sentinel.encode()), blob)

    def test_private_permissions_and_no_temporary_files(self) -> None:
        self.saved()
        self.store().save()  # a second write makes the backup
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.with_name("macros.enc.bak").stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["macros.enc", "macros.enc.bak"])
        self.assertNotIn(SECRET.encode(), self.path.with_name("macros.enc.bak").read_bytes())

    def test_wide_folder_permissions_are_tightened(self) -> None:
        self.folder.mkdir(mode=0o755)
        os.chmod(self.folder, 0o755)
        self.saved()
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)

    def test_header_tampering_is_rejected(self) -> None:
        self.saved()
        original = self.path.read_bytes()
        header = json.loads(original)
        cases = {
            "iterations lowered": dict(header, iterations=1000),
            "iterations changed": dict(header, iterations=header["iterations"] + 1),
            "salt swapped": dict(header, salt=base64.b64encode(os.urandom(16)).decode()),
            "kdf changed": dict(header, kdf="scrypt"),
            "cipher changed": dict(header, cipher="aes-256-gcm"),
            "newer version": dict(header, version=2),
        }
        for label, changed in cases.items():
            self.path.write_text(json.dumps(changed))
            with self.assertRaises(MacroFileError, msg=label):
                self.store()
        # A header copied from another file with fewer iterations but the right password: still refused.
        weak_salt = os.urandom(16)
        weak_key = M.derive_key(PASSWORD, weak_salt, 600_000)
        with mock.patch.object(M, "MIN_ITERATIONS", 1):
            M.encrypt_payload([], weak_key, weak_salt, 600_000)
        inner_mismatch = M.encrypt_payload([sample()], weak_key, weak_salt, 600_000)
        forged = json.loads(inner_mismatch)
        forged["iterations"] = 700_000  # header says more, payload says 600 000
        self.path.write_text(json.dumps(forged))
        with mock.patch.object(M, "derive_key", return_value=weak_key), self.assertRaises(MacroFileError) as caught:
            self.store()
        self.assertEqual(caught.exception.kind, "tampered")
        self.path.write_bytes(original)
        self.assertEqual(len(self.store()), 1)

    def test_wrong_password_truncated_or_garbled_leaves_the_file_alone(self) -> None:
        self.saved()
        original = self.path.read_bytes()
        with self.assertRaises(MacroFileError) as caught:
            self.store("wrong")
        self.assertEqual(caught.exception.kind, "wrong_key")
        self.assertEqual(self.path.read_bytes(), original)
        for broken in (original[: len(original) // 2], b"", b"\x00\xff garbage", original.replace(b'"data": "', b'"data": "AAAA')):
            self.path.write_bytes(broken)
            with self.assertRaises(MacroFileError):
                self.store()
            self.assertEqual(self.path.read_bytes(), broken)  # never overwritten or deleted

    def test_start_empty_moves_the_damaged_file_aside(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"{broken")
        store = MacroStore(self.path)
        with self.assertRaises(MacroFileError):
            store.unlock(PASSWORD)
        moved = store.start_empty()
        self.assertTrue(moved.name.startswith("macros.enc.damaged-"))
        self.assertEqual(moved.read_bytes(), b"{broken")
        self.assertFalse(self.path.exists())

    def test_keep_damaged_never_writes(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"{broken")
        store = MacroStore(self.path)
        store.keep_damaged()
        with self.assertRaises(MacroWriteError):
            store.save()
        self.assertEqual(self.path.read_bytes(), b"{broken")

    def test_the_stored_iteration_count_is_used(self) -> None:
        salt = os.urandom(16)
        key = M.derive_key(PASSWORD, salt, 650_000)
        self.folder.mkdir()
        self.path.write_bytes(M.encrypt_payload([sample()], key, salt, 650_000))
        with mock.patch.object(M, "derive_key", wraps=M.derive_key) as derive:
            store = self.store()
        derive.assert_called_once()
        self.assertEqual(derive.call_args.args[2], 650_000)
        self.assertEqual(store.iterations, 650_000)

    def test_rekey(self) -> None:
        store = self.saved()
        store.rekey("new password")
        self.assertEqual(len(self.store("new password")), 1)
        with self.assertRaises(MacroFileError):
            self.store(PASSWORD)
        backup = MacroStore(self.path.with_name("macros.enc.bak"))
        backup.unlock("new password")  # the backup follows the new password too
        self.assertEqual(len(backup), 1)

    def test_a_failed_rekey_keeps_the_old_file_valid(self) -> None:
        store = self.saved()
        before = self.path.read_bytes()
        real = M._write_private

        def fail_main(path, data):
            if path.name == "macros.enc":
                raise OSError(28, "No space left on device")
            real(path, data)

        with mock.patch.object(M, "_write_private", side_effect=fail_main), self.assertRaises(MacroWriteError):
            store.rekey("new password")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.store(PASSWORD)), 1)
        store.save()  # still the old key in memory
        self.assertEqual(len(self.store(PASSWORD)), 1)

    def test_unwritable_folder_keeps_the_macro_in_memory(self) -> None:
        store = self.store()
        store.add(sample())
        with mock.patch.object(M, "_write_private", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(MacroWriteError):
                store.save()
        self.assertEqual(len(store), 1)
        store.save()
        self.assertEqual(len(self.store()), 1)

    def test_a_file_damaged_by_another_instance_is_not_overwritten(self) -> None:
        store = self.saved()
        store.add(sample(name="pending"))
        self.path.write_bytes(b"{half a file")
        with self.assertRaises(MacroWriteError):
            store.save()
        self.assertEqual(self.path.read_bytes(), b"{half a file")
        self.assertIsNotNone(store.get(store.list_sorted()[0].id))  # still in memory

    def test_two_instances_merge_by_id(self) -> None:
        first = self.saved()
        second = self.store()
        mine = sample(name="from-first")
        first.add(mine)
        first.save()
        theirs = sample(name="from-second")
        second.add(theirs)
        renamed = second.list_sorted()[0].copy()
        second.save()  # merges first's macro
        names = sorted(m.name for m in self.store().list_sorted())
        self.assertEqual(names, sorted([NAME, "from-first", "from-second"]))
        first.delete(mine.id)
        first.save()
        self.assertNotIn("from-first", [m.name for m in self.store().list_sorted()])
        self.assertTrue(renamed)

    def test_no_secret_in_logs_or_exception_text(self) -> None:
        with self.assertLogs("ophion", logging.DEBUG) as logs:
            logging.getLogger("ophion").debug("start")
            store = self.saved()
            for password in ("wrong", SECRET):
                try:
                    self.store(password)
                except MacroFileError as exc:
                    self.assertNotIn(SECRET, str(exc))
            self.path.write_bytes(self.path.read_bytes()[:40])
            try:
                self.store()
            except MacroFileError as exc:
                self.assertNotIn(SECRET, repr(exc))
            store.add(sample(name="other", steps=[{"type": "text", "text": SECRET}]))
            with mock.patch.object(M, "_write_private", side_effect=OSError(5, SECRET)):
                try:
                    store.save()
                except MacroWriteError as exc:
                    self.assertNotIn(SECRET, str(exc))
        text = "\n".join(logs.output)
        for sentinel in (SECRET, TEXT, NAME, PASSWORD):
            self.assertNotIn(sentinel, text)


class RecorderTests(unittest.TestCase):
    def record(self, events):
        recorder = MacroRecorder(clock=lambda: 0.0)
        for at, data, kw in events:
            recorder.add(data, at=at, **kw)
        return recorder

    def test_typing_merges_fast_keystrokes_and_records_pauses(self) -> None:
        r = self.record([(0.0, "e", {}), (0.01, "c", {}), (0.02, "h", {}), (0.5, "o", {}), (9.0, "\r", {})])
        self.assertEqual(r.steps(), [
            {"type": "text", "text": "ech"}, {"type": "delay", "ms": 480}, {"type": "text", "text": "o"},
            {"type": "delay", "ms": 5000}, {"type": "key", "key": "Enter"}])  # gaps clamped to 5 s

    def test_named_keys_raw_and_paste(self) -> None:
        events = [(i * 0.001, data, {}) for i, data in enumerate(
            ["\t", "\x7f", "\x1b", "\x1b[3~", "\x1b[A", "\x1bOB", "\x1b[C", "\x1b[D", "\x1b[H", "\x1b[F",
             "\x1b[5~", "\x1b[6~", "\x03", "\x04", "\x1a", "\x1b[15~", "\x1bx", "\x00"])]
        steps = self.record(events).steps()
        names = [s.get("key") or s["hex"] for s in steps]
        self.assertEqual(names, ["Tab", "Backspace", "Escape", "Delete", "Up", "Down", "Right", "Left", "Home", "End",
                                 "PageUp", "PageDown", "Ctrl+C", "Ctrl+D", "Ctrl+Z", "1b5b31357e", "1b78", "00"])
        self.assertEqual(M.step_bytes(steps[5]), b"\x1bOB")  # application cursor keys replay as recorded
        pasted = self.record([(0, "\x1b[200~a\nb\x1b[201~", {}), (1, "from vte", {"paste": True})]).steps()
        self.assertEqual([(s.get("text"), s.get("paste")) for s in pasted if s["type"] == "text"],
                         [("a\nb", True), ("from vte", True)])

    def test_secret_flag_and_backspace_normalisation(self) -> None:
        steps = self.record([(0.0, "pw", {"secret": True}), (0.01, "x", {}), (0.3, "\x7f", {}), (0.31, "y", {}),
                             (1.0, "a", {}), (1.01, "\x08", {})]).steps()
        self.assertEqual(steps, [{"type": "text", "text": "pw", "secret": True},
                                 {"type": "text", "text": "y"}])
        # Cursor keys are kept as they are: no line-editing guesswork.
        steps = self.record([(0, "ab", {}), (0.001, "\x1b[D", {}), (0.002, "\x7f", {})]).steps()
        self.assertEqual([s["type"] for s in steps], ["text", "key", "key"])

    def test_nothing_recorded_means_no_macro(self) -> None:
        self.assertEqual(self.record([]).steps(), [])
        with self.assertRaises(MacroError):
            store = MacroStore(Path(tempfile.gettempdir()) / "unused-macros.enc")
            store._key = b"x"  # no file is written: validation fails first
            store.add(Macro(name="empty", steps=[]))

    def test_clear_forgets_everything(self) -> None:
        recorder = self.record([(0, SECRET, {"secret": True})])
        recorder.clear()
        self.assertEqual(recorder.steps(), [])


class FakeLoop:
    def __init__(self) -> None:
        self.now = 0
        self.sources = {}
        self.next_id = 1

    def schedule(self, ms, callback):
        source = self.next_id
        self.next_id += 1
        self.sources[source] = (self.now + ms, callback)
        return source

    def cancel(self, source):
        self.sources.pop(source)

    def run(self, until=10_000):
        while self.sources:
            source, (due, callback) = min(self.sources.items(), key=lambda item: (item[1][0], item[0]))
            if due > until:
                return
            del self.sources[source]
            self.now = due
            callback()


class FakeSink:
    def __init__(self, loop):
        self.loop = loop
        self.sent = []

    def feed(self, data):
        self.sent.append((self.loop.now, "feed", data))

    def paste(self, text):
        self.sent.append((self.loop.now, "paste", text))


class PlayerTests(unittest.TestCase):
    STEPS = [{"type": "text", "text": "ls"}, {"type": "delay", "ms": 400}, {"type": "key", "key": "Enter"},
             {"type": "text", "text": "x\ny", "paste": True}, {"type": "future"}, {"type": "raw", "hex": "1b4f50"}]

    def play(self, timing="recorded", **kw):
        loop = FakeLoop()
        sink = FakeSink(loop)
        finished = []
        progress = []
        player = MacroPlayer(self.STEPS, sink, loop.schedule, loop.cancel, timing=timing,
                             on_progress=lambda i, n: progress.append((i, n)), on_finished=finished.append)
        return loop, sink, player, finished, progress

    def test_order_and_recorded_delays(self) -> None:
        loop, sink, player, finished, progress = self.play()
        player.start()
        loop.run()
        self.assertEqual(sink.sent, [(0, "feed", b"ls"), (400, "feed", b"\r"), (400, "paste", "x\ny"),
                                     (400, "feed", b"\x1bOP")])
        self.assertEqual(finished, [True])
        self.assertEqual(progress[-1], (6, 6))

    def test_fast_timing_ignores_recorded_delays(self) -> None:
        loop, sink, player, finished, _ = self.play("fast")
        player.start()
        loop.run()
        self.assertEqual([t for t, _k, _d in sink.sent], [0, 100, 200, 400])  # the unknown step takes its slot too
        self.assertEqual(finished, [True])

    def test_stop_aborts_and_removes_the_timer(self) -> None:
        loop, sink, player, finished, _ = self.play()
        player.start()
        loop.run(until=100)
        player.stop()
        self.assertEqual(loop.sources, {})
        loop.run()
        self.assertEqual(sink.sent, [(0, "feed", b"ls")])
        self.assertEqual(finished, [False])
        player.stop()  # twice is harmless
        self.assertEqual(finished, [False])

    def test_no_overlap(self) -> None:
        loop, _sink, player, _finished, _ = self.play()
        player.start()
        with self.assertRaises(MacroError):
            player.start()

    def test_the_sink_may_stop_playback_while_sending(self) -> None:
        loop, sink, player, finished, _ = self.play()
        sink.feed = lambda data: player.stop()  # the session ends
        player.start()
        loop.run()
        self.assertEqual(finished, [False])
        self.assertEqual(loop.sources, {})


if __name__ == "__main__":
    unittest.main()


class KeyboxMacroTests(StoreCase):
    """Version 2: the keybox's ``macros`` subkey, the keybox id authenticated; version 1 only for migration."""

    def setUp(self) -> None:
        super().setUp()
        from ophion import keybox as K

        self.box = K.Keybox.create("pw", self.folder / "keybox.json", scrypt_n=2 ** 15)
        self.key = self.box.subkey("macros")

    def v2(self) -> MacroStore:
        store = MacroStore(self.path)
        store.unlock_with_key(self.key, self.box.id)
        return store

    def test_round_trip_and_format(self) -> None:
        store = self.v2()
        store.add(sample())
        store.save()
        raw = self.path.read_bytes()
        header = json.loads(raw)
        self.assertEqual((header["version"], header["keybox_id"], header["cipher"]), (2, self.box.id, "aes-256-gcm"))
        self.assertNotIn("kdf", header)  # no per-file key derivation
        for sentinel in (SECRET, NAME, TEXT, SHORTCUT):
            self.assertNotIn(sentinel.encode(), raw)
        again = self.v2()
        self.assertEqual(again.list_sorted()[0].steps, sample().steps)
        again.update(again.list_sorted()[0].copy())
        again.save()
        self.assertTrue(again.on_keybox)
        again.rekey("ignored")  # a version 2 store has nothing to re-key
        self.assertEqual(len(self.v2()), 1)

    def test_refusals_leave_the_file_untouched(self) -> None:
        store = self.v2()
        store.add(sample())
        store.save()
        original = json.loads(self.path.read_text())

        def flip(text):
            data = bytearray(base64.b64decode(text))
            data[0] ^= 1
            return base64.b64encode(bytes(data)).decode()

        for change, kind in ((lambda d: d.update(keybox_id=base64.b64encode(b"y" * 16).decode()), "foreign"),
                             (lambda d: d.update(data=flip(d["data"])), "tampered"),
                             (lambda d: d.update(version=3), "unsupported"),
                             (lambda d: d.update(cipher="fernet"), "unsupported")):
            changed = json.loads(json.dumps(original))
            change(changed)
            self.path.write_text(json.dumps(changed))
            before = self.path.read_bytes()
            with self.subTest(kind=kind), self.assertRaises(MacroFileError) as caught:
                MacroStore(self.path).unlock_with_key(self.key, self.box.id)
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(self.path.read_bytes(), before)

    def test_version_1_is_converted_and_kept_until_clean_up(self) -> None:
        legacy = self.saved()  # version 1, its own password
        with self.assertRaises(MacroFileError) as caught:
            MacroStore(self.path).unlock_with_key(self.key, self.box.id)
        self.assertEqual(caught.exception.kind, "legacy")
        v1_bytes = self.path.read_bytes()
        self.assertEqual(legacy.adopt_key(self.key, self.box.id), 1)
        self.assertEqual(self.path.with_name("macros.enc.pre-keybox").read_bytes(), v1_bytes)
        converted = self.v2()
        self.assertEqual([m.name for m in converted.list_sorted()], [NAME])
        self.assertEqual(converted.list_sorted()[0].steps, sample().steps)

    def test_a_failed_conversion_keeps_version_1(self) -> None:
        legacy = self.saved()
        v1_bytes = self.path.read_bytes()
        real = M._write_private

        def fail_main(path, data):
            if path.name == "macros.enc":
                raise OSError(28, "No space left on device")
            real(path, data)

        with mock.patch.object(M, "_write_private", side_effect=fail_main), self.assertRaises(MacroWriteError):
            legacy.adopt_key(self.key, self.box.id)
        self.assertEqual(self.path.read_bytes(), v1_bytes)
        self.assertEqual(len(self.store()), 1)  # still opens with its password
