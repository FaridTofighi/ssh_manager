"""Terminal macros: what is recorded, how it is replayed, and the encrypted store (no GTK).

A macro is a named list of *steps* sent to the program in a terminal:

* ``{"type": "text", "text": "...", "paste": bool, "secret": bool}``: typed or pasted text;
* ``{"type": "key", "key": "Enter"}``: a named key (``seq`` holds the bytes when they are
  not the key's usual ones, e.g. an arrow in application cursor mode);
* ``{"type": "raw", "hex": "1b4f50", "secret": bool}``: bytes without a name;
* ``{"type": "delay", "ms": 250}``: a pause.

Steps of an unknown type are kept (a newer version may have written them) but not played.

:class:`MacroRecorder` turns what a terminal sent to its program (VTE's ``commit``
text, with a timestamp) into steps; :class:`MacroPlayer` sends steps back through
a scheduler (the GTK code passes ``GLib.timeout_add``; tests a fake clock).

:class:`MacroStore` keeps the macros in ``macros.enc``. Everything (names,
shortcuts, steps) is encrypted with a key derived from the master password:
PBKDF2-HMAC-SHA256 (:data:`DEFAULT_ITERATIONS`, a salt of its own) and Fernet.
Only a small header is plain text; Fernet does not authenticate it, so the
encrypted payload repeats it and a mismatch is rejected (no downgrade by editing
the header). The file is written atomically with mode 0600 (one encrypted
``macros.enc.bak`` is kept), and is never overwritten when it cannot be opened.

Nothing in this module logs or puts into an exception message the content of a
macro, a derived key or a password.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

FILE_NAME = "macros.enc"
FORMAT = "ssh-manager-macros"
VERSION = 1
KDF = "pbkdf2-sha256"
CIPHER = "fernet"
DEFAULT_ITERATIONS = 600_000
MIN_ITERATIONS = 600_000
SALT_BYTES = 16

MAX_MACROS = 200
MAX_STEPS = 5000
MAX_TEXT_BYTES = 1024 * 1024
MAX_NAME = 80
MAX_DESCRIPTION = 2000

MIN_GAP_MS = 50  # keystrokes closer than this are one text step; shorter pauses are not kept
MAX_GAP_MS = 5000
FAST_STEP_MS = 100
TIMINGS = ("recorded", "fast")

STEP_TYPES = ("text", "key", "raw", "delay")

# Named keys: the bytes they are sent as (the first) and the other sequences recorded as the same key.
KEYS: Dict[str, Tuple[str, ...]] = {
    "Enter": ("\r",),
    "Tab": ("\t",),
    "Backspace": ("\x7f",),
    "Escape": ("\x1b",),
    "Delete": ("\x1b[3~",),
    "Insert": ("\x1b[2~",),
    "Up": ("\x1b[A", "\x1bOA"),
    "Down": ("\x1b[B", "\x1bOB"),
    "Right": ("\x1b[C", "\x1bOC"),
    "Left": ("\x1b[D", "\x1bOD"),
    "Home": ("\x1b[H", "\x1bOH", "\x1b[1~"),
    "End": ("\x1b[F", "\x1bOF", "\x1b[4~"),
    "PageUp": ("\x1b[5~",),
    "PageDown": ("\x1b[6~",),
}
for _code in range(1, 27):  # Ctrl+A … Ctrl+Z, except the bytes Tab and Enter send
    if _code not in (0x09, 0x0D):
        KEYS[f"Ctrl+{chr(0x40 + _code)}"] = (chr(_code),)
KEY_NAMES: Tuple[str, ...] = tuple(KEYS)
_SEQUENCE_TO_KEY: Dict[str, str] = {seq: name for name, seqs in KEYS.items() for seq in seqs}
_ESCAPE_SEQUENCES = sorted((s for s in _SEQUENCE_TO_KEY if s.startswith("\x1b") and len(s) > 1), key=len, reverse=True)
_ERASE = ("\x7f", "\x08")  # Backspace and Ctrl+H both erase the previous character in a line
_PASTE_START, _PASTE_END = "\x1b[200~", "\x1b[201~"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1bO.")


class MacroError(Exception):
    """A validation problem. ``code`` names it; the UI turns it into a message (never with step content)."""

    def __init__(self, code: str, detail: str = "") -> None:
        Exception.__init__(self, code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class MacroFileError(Exception):
    """``macros.enc`` cannot be opened (``kind``: damaged, unsupported, wrong_key, tampered, unreadable)."""

    def __init__(self, kind: str) -> None:
        Exception.__init__(self, kind)
        self.kind = kind


class MacroWriteError(Exception):
    """``macros.enc`` could not be written (the macros stay in memory)."""


# --------------------------------------------------------------------------- model
def _now() -> float:
    return round(time.time(), 3)


@dataclass
class Macro:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    shortcut: List[str] = field(default_factory=list)
    description: str = ""
    steps: List[dict] = field(default_factory=list)
    timing: str = "recorded"
    confirm_before_run: bool = False
    created: float = field(default_factory=_now)
    modified: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "shortcut": list(self.shortcut), "description": self.description,
                "steps": copy.deepcopy(self.steps), "timing": self.timing,
                "confirm_before_run": self.confirm_before_run, "created": self.created, "modified": self.modified}

    @classmethod
    def from_dict(cls, data: dict) -> "Macro":
        """Unknown fields are ignored; wrong types fall back to the defaults (no content is logged)."""

        if not isinstance(data, dict):
            raise MacroError("not_a_macro")
        macro = cls()
        if isinstance(data.get("id"), str) and _ID_RE.fullmatch(data["id"]):
            macro.id = data["id"]  # it becomes part of an action name: win.run-macro('<id>')
        if isinstance(data.get("name"), str):
            macro.name = data["name"]
        shortcut = data.get("shortcut")
        if isinstance(shortcut, str):
            shortcut = [shortcut]
        if isinstance(shortcut, list):
            macro.shortcut = [s for s in shortcut if isinstance(s, str) and s]
        if isinstance(data.get("description"), str):
            macro.description = data["description"]
        steps = data.get("steps")
        if isinstance(steps, list):
            macro.steps = [dict(step) for step in steps if isinstance(step, dict) and isinstance(step.get("type"), str)]
        if data.get("timing") in TIMINGS:
            macro.timing = data["timing"]
        if isinstance(data.get("confirm_before_run"), bool):
            macro.confirm_before_run = data["confirm_before_run"]
        for name in ("created", "modified"):
            if isinstance(data.get(name), (int, float)) and not isinstance(data.get(name), bool):
                setattr(macro, name, float(data[name]))
        return macro

    def copy(self) -> "Macro":
        return Macro.from_dict(self.to_dict())

    @property
    def has_secrets(self) -> bool:
        return any(step.get("secret") for step in self.steps)


def step_bytes(step: dict) -> Optional[bytes]:
    """What a ``text`` / ``key`` / ``raw`` step sends (``None``: a delay or an unknown step)."""

    kind = step.get("type")
    if kind == "text":
        return str(step.get("text", "")).encode("utf-8")
    if kind == "key":
        seq = step.get("seq")
        if isinstance(seq, str) and seq:
            try:
                return bytes.fromhex(seq)
            except ValueError:
                return None
        sequences = KEYS.get(str(step.get("key", "")))
        return sequences[0].encode("utf-8") if sequences else None
    if kind == "raw":
        try:
            return bytes.fromhex(str(step.get("hex", "")))
        except ValueError:
            return None
    return None


def text_size(steps: Iterable[dict]) -> int:
    return sum(len(str(step.get("text", "")).encode("utf-8")) + len(str(step.get("hex", ""))) // 2 for step in steps)


def duration_ms(steps: Sequence[dict], timing: str = "recorded") -> int:
    if timing == "fast":
        return FAST_STEP_MS * max(0, len([s for s in steps if s.get("type") in ("text", "key", "raw")]) - 1)
    return sum(int(step.get("ms", 0)) for step in steps if step.get("type") == "delay")


def validate_steps(steps: Sequence[dict]) -> None:
    if len(steps) > MAX_STEPS:
        raise MacroError("too_many_steps", str(MAX_STEPS))
    if text_size(steps) > MAX_TEXT_BYTES:
        raise MacroError("too_much_text", str(MAX_TEXT_BYTES))
    for step in steps:
        kind = step.get("type")
        if kind == "text" and not isinstance(step.get("text"), str):
            raise MacroError("bad_step")
        if kind == "key" and str(step.get("key", "")) not in KEYS and not step.get("seq"):
            raise MacroError("bad_step")
        if kind == "raw" and step_bytes(step) is None:
            raise MacroError("bad_step")
        if kind == "delay":
            ms = step.get("ms")
            if not isinstance(ms, int) or isinstance(ms, bool) or not 0 <= ms <= 3_600_000:
                raise MacroError("bad_step")


# ------------------------------------------------------------------------- recorder
def parse_input(data: str, *, secret: bool = False) -> List[dict]:
    """Steps for ``data`` the terminal sent at once (one keystroke, or what an input method committed)."""

    steps: List[dict] = []
    text: List[str] = []

    def flush_text() -> None:
        if text:
            steps.append(_text_step("".join(text), secret=secret))
            text.clear()

    i = 0
    while i < len(data):
        char = data[i]
        if char == "\x1b" and i + 1 < len(data):
            match = next((s for s in _ESCAPE_SEQUENCES if data.startswith(s, i)), None)
            if match is not None:
                flush_text()
                steps.append(_key_step(_SEQUENCE_TO_KEY[match], match))
                i += len(match)
                continue
            other = _CSI_RE.match(data, i)
            end = other.end() if other else i + 2  # a CSI / SS3 sequence, else Alt+<char>
            flush_text()
            steps.append(_raw_step(data[i:end].encode("utf-8"), secret=secret))
            i = end
            continue
        if char in _SEQUENCE_TO_KEY:
            flush_text()
            steps.append(_key_step(_SEQUENCE_TO_KEY[char], char))
        elif char == "\x08":
            flush_text()
            steps.append(_key_step("Ctrl+H", char))
        elif char.isprintable() or char == " ":
            text.append(char)
        else:
            flush_text()
            steps.append(_raw_step(char.encode("utf-8"), secret=secret))
        i += 1
    flush_text()
    return steps


def _text_step(text: str, *, secret: bool = False, paste: bool = False) -> dict:
    step = {"type": "text", "text": text}
    if paste:
        step["paste"] = True
    if secret:
        step["secret"] = True
    return step


def _key_step(name: str, sequence: str) -> dict:
    step = {"type": "key", "key": name}
    if KEYS[name][0] != sequence:
        step["seq"] = sequence.encode("utf-8").hex()
    return step


def _raw_step(data: bytes, *, secret: bool = False) -> dict:
    step = {"type": "raw", "hex": data.hex()}
    if secret:
        step["secret"] = True
    return step


def is_erase(step: dict) -> bool:
    return step.get("type") == "key" and step_bytes(step) in tuple(e.encode() for e in _ERASE)


def normalize_steps(steps: Sequence[dict]) -> List[dict]:
    """Backspace right after typed text removes its last character; no leading / trailing pauses."""

    out: List[dict] = []
    for step in steps:
        if is_erase(step):
            index = len(out) - 1
            pause = index >= 0 and out[index].get("type") == "delay"
            if pause:
                index -= 1
            target = out[index] if index >= 0 else None
            if target is not None and target.get("type") == "text" and not target.get("paste") and target["text"]:
                target["text"] = target["text"][:-1]
                if pause:
                    out.pop()  # the pause before the Backspace belonged to the mistake
                if not target["text"]:
                    out.pop(index)
                continue
        out.append(dict(step))
    merged: List[dict] = []
    for step in out:  # pauses next to each other (after removed steps) become one
        if step.get("type") == "delay" and merged and merged[-1].get("type") == "delay":
            merged[-1]["ms"] = min(MAX_GAP_MS, merged[-1]["ms"] + step["ms"])
        else:
            merged.append(step)
    while merged and merged[0].get("type") == "delay":
        merged.pop(0)
    while merged and merged[-1].get("type") == "delay":
        merged.pop()
    return merged


class MacroRecorder:
    """Collects what one terminal sends to its program (in memory only; :meth:`clear` forgets it)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._steps: List[dict] = []
        self._last_time: Optional[float] = None
        self._last_merge_time: Optional[float] = None
        self.started = clock()

    def add(self, data: str, *, secret: bool = False, paste: bool = False, at: Optional[float] = None) -> None:
        if not data:
            return
        now = self._clock() if at is None else at
        if not paste and _PASTE_START in data:  # bracketed paste markers (VTE versions that report them)
            before, _sep, rest = data.partition(_PASTE_START)
            pasted, _sep, after = rest.partition(_PASTE_END)
            for part, is_paste in ((before, False), (pasted, True), (after, False)):
                if part:
                    self.add(part, secret=secret, paste=is_paste, at=now)
            return
        new = [_text_step(data, secret=secret, paste=True)] if paste else parse_input(data, secret=secret)
        if not new:
            return
        gap = None if self._last_time is None else (now - self._last_time) * 1000.0
        self._last_time = now
        first = new[0]
        last = self._steps[-1] if self._steps else None
        if (gap is not None and gap < MIN_GAP_MS and last is not None and first.get("type") == "text"
                and last.get("type") == "text" and not first.get("paste") and not last.get("paste")
                and bool(first.get("secret")) == bool(last.get("secret"))):
            last["text"] += first["text"]
            new = new[1:]
        elif gap is not None and gap >= MIN_GAP_MS and self._steps:
            self._steps.append({"type": "delay", "ms": int(min(MAX_GAP_MS, round(gap)))})
        self._steps.extend(new)

    @property
    def step_count(self) -> int:
        return len([s for s in self._steps if s.get("type") != "delay"])

    def elapsed(self) -> float:
        return max(0.0, self._clock() - self.started)

    def steps(self) -> List[dict]:
        return normalize_steps(self._steps)

    def clear(self) -> None:
        for step in self._steps:
            step.clear()
        self._steps.clear()
        self._last_time = None


# --------------------------------------------------------------------------- player
class MacroPlayer:
    """Sends ``steps`` one after another through ``schedule(ms, callback) -> id`` (never blocks).

    ``sink`` has ``feed(bytes)`` and ``paste(str)``. ``on_progress(index, total)`` is called
    before each step, ``on_finished(completed: bool)`` once at the end (or after :meth:`stop`).
    """

    def __init__(self, steps: Sequence[dict], sink, schedule: Callable[[int, Callable[[], bool]], int],
                 cancel: Callable[[int], None], *, timing: str = "recorded",
                 on_progress: Callable[[int, int], None] = lambda _i, _n: None,
                 on_finished: Callable[[bool], None] = lambda _done: None) -> None:
        self._steps = [step for step in steps if timing != "fast" or step.get("type") != "delay"]
        self._sink = sink
        self._schedule = schedule
        self._cancel = cancel
        self._timing = timing
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._index = 0
        self._source: Optional[int] = None
        self.running = False

    @property
    def total(self) -> int:
        return len(self._steps)

    @property
    def index(self) -> int:
        return self._index

    def start(self) -> None:
        if self.running:
            raise MacroError("already_running")
        self.running = True
        self._source = self._schedule(0, self._tick)

    def stop(self) -> None:
        if not self.running:
            return
        if self._source is not None:
            self._cancel(self._source)
            self._source = None
        self._finish(False)

    def _finish(self, completed: bool) -> None:
        self.running = False
        self._source = None
        self._on_finished(completed)

    def _tick(self) -> bool:
        self._source = None
        if not self.running:
            return False
        if self._index >= len(self._steps):
            self._finish(True)
            return False
        step = self._steps[self._index]
        self._index += 1
        self._on_progress(self._index, len(self._steps))
        wait = 0
        kind = step.get("type")
        if kind == "delay":
            wait = max(0, int(step.get("ms", 0)))
        elif kind == "text" and step.get("paste"):
            self._sink.paste(str(step.get("text", "")))
        else:
            data = step_bytes(step)
            if data:
                self._sink.feed(data)
        if not self.running:  # the sink stopped us (the session ended while sending)
            return False
        if self._index >= len(self._steps):
            self._finish(True)
            return False
        if self._timing == "fast":
            wait = FAST_STEP_MS
        self._source = self._schedule(wait, self._tick)
        return False


# ---------------------------------------------------------------------------- crypto
def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    """The Fernet key (PBKDF2-HMAC-SHA256, 32 bytes, url-safe base64)."""

    from cryptography.hazmat.primitives import hashes  # type: ignore
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # type: ignore

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _header(salt: bytes, iterations: int) -> dict:
    return {"format": FORMAT, "version": VERSION, "kdf": KDF, "iterations": iterations,
            "salt": base64.b64encode(salt).decode("ascii"), "cipher": CIPHER}


_AUTHENTICATED_FIELDS = ("format", "version", "kdf", "iterations", "salt")


def read_header(raw: bytes) -> Tuple[dict, bytes, int]:
    """The header, the salt and the iteration count of a ``macros.enc`` (``MacroFileError`` when unusable)."""

    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MacroFileError("damaged") from None
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise MacroFileError("damaged")
    version = header.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise MacroFileError("damaged")
    if version > VERSION or header.get("kdf") != KDF or header.get("cipher") != CIPHER:
        raise MacroFileError("unsupported")
    iterations = header.get("iterations")
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < MIN_ITERATIONS:
        raise MacroFileError("tampered")
    try:
        salt = base64.b64decode(str(header.get("salt", "")), validate=True)
    except (binascii.Error, ValueError):
        raise MacroFileError("damaged") from None
    if len(salt) < SALT_BYTES or not isinstance(header.get("data"), str):
        raise MacroFileError("damaged")
    return header, salt, iterations


def encrypt_payload(macros: Sequence[Macro], key: bytes, salt: bytes, iterations: int) -> bytes:
    from cryptography.fernet import Fernet  # type: ignore

    header = _header(salt, iterations)
    inner = {"header": {name: header[name] for name in _AUTHENTICATED_FIELDS},
             "macros": [macro.to_dict() for macro in macros]}
    token = Fernet(key).encrypt(json.dumps(inner, separators=(",", ":")).encode("utf-8"))
    header["data"] = token.decode("ascii")
    return (json.dumps(header, indent=1) + "\n").encode("utf-8")


def decrypt_payload(raw: bytes, key: bytes) -> List[Macro]:
    from cryptography.fernet import Fernet, InvalidToken  # type: ignore

    header, _salt, _iterations = read_header(raw)
    try:
        plain = Fernet(key).decrypt(header["data"].encode("ascii"))
    except (InvalidToken, ValueError, TypeError):
        raise MacroFileError("wrong_key") from None
    try:
        inner = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MacroFileError("damaged") from None
    finally:
        plain = b""
    if not isinstance(inner, dict) or not isinstance(inner.get("header"), dict):
        raise MacroFileError("damaged")
    if any(inner["header"].get(name) != header.get(name) for name in _AUTHENTICATED_FIELDS):
        raise MacroFileError("tampered")  # the plain header was edited (e.g. fewer iterations)
    macros: List[Macro] = []
    for entry in inner.get("macros", []) if isinstance(inner.get("macros"), list) else []:
        try:
            macros.append(Macro.from_dict(entry))
        except MacroError:
            LOGGER.warning("macros: an unreadable entry was skipped")
    return macros


def _write_private(path: Path, data: bytes) -> None:
    """``data`` to ``path`` atomically: a 0600 temporary file in the same folder, fsync, rename."""

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


# ----------------------------------------------------------------------------- store
class MacroStore:
    """The macros of ``macros.enc``: unlocked with the master password, saved encrypted.

    ``unlock(password)`` derives the key once (call it off the GTK thread: it takes a few
    hundred milliseconds); afterwards only the derived key is kept. ``disabled`` is set when
    the user kept a file that could not be opened: nothing is written then.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._macros: Dict[str, Macro] = {}
        self._key: Optional[bytes] = None
        self._salt: Optional[bytes] = None
        self._iterations = DEFAULT_ITERATIONS
        self._seen_digest: Optional[str] = None  # the file as last read or written (external changes)
        self._deleted: Dict[str, float] = {}
        self.disabled = False

    # ----------------------------------------------------------- state
    @property
    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    @property
    def unlocked(self) -> bool:
        return self._key is not None and not self.disabled

    @property
    def iterations(self) -> int:
        return self._iterations

    def file_exists(self) -> bool:
        return self.path.exists()

    def lock(self) -> None:
        """Forget the key and the macros (the app is closing)."""

        self._key = None
        self._macros.clear()

    # -------------------------------------------------------- unlocking
    def unlock(self, password: str) -> None:
        """Open the file (or prepare a new one) with ``password``; ``MacroFileError`` leaves the file untouched."""

        raw = self._read_raw()
        if raw is None:
            salt, iterations = os.urandom(SALT_BYTES), DEFAULT_ITERATIONS
            key = derive_key(password, salt, iterations)
            self._install(key, salt, iterations, [], None)
            return
        _header_dict, salt, iterations = read_header(raw)
        key = derive_key(password, salt, iterations)
        macros = decrypt_payload(raw, key)
        self._install(key, salt, iterations, macros, hashlib.sha256(raw).hexdigest())

    def _install(self, key: bytes, salt: bytes, iterations: int, macros: List[Macro], digest: Optional[str]) -> None:
        self._key, self._salt, self._iterations = key, salt, iterations
        self._macros = {macro.id: macro for macro in macros}
        self._seen_digest = digest
        self.disabled = False

    def _read_raw(self) -> Optional[bytes]:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise MacroFileError("unreadable") from None

    def start_empty(self) -> Optional[Path]:
        """Move a file that cannot be opened aside (``macros.enc.damaged-<time>``) and start without macros."""

        moved = None
        if self.path.exists():
            moved = self.path.with_name(f"{self.path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(self.path, moved)
        self._macros.clear()
        self._seen_digest = None
        self.disabled = False
        return moved

    def keep_damaged(self) -> None:
        """Continue without macros this session; the file is never touched."""

        self._macros.clear()
        self._key = None
        self.disabled = True

    # ------------------------------------------------------------ queries
    def list_sorted(self) -> List[Macro]:
        return sorted(self._macros.values(), key=lambda m: (m.name.casefold(), m.created))

    def get(self, macro_id: str) -> Optional[Macro]:
        return self._macros.get(macro_id)

    def __len__(self) -> int:
        return len(self._macros)

    def name_taken(self, name: str, ignoring: Optional[str] = None) -> bool:
        wanted = name.strip().casefold()
        return any(m.name.strip().casefold() == wanted for m in self._macros.values() if m.id != ignoring)

    def validate(self, macro: Macro) -> None:
        name = macro.name.strip()
        if not name:
            raise MacroError("empty_name")
        if len(name) > MAX_NAME:
            raise MacroError("long_name", str(MAX_NAME))
        if self.name_taken(name, ignoring=macro.id):
            raise MacroError("duplicate_name")
        if len(macro.description) > MAX_DESCRIPTION:
            raise MacroError("long_description", str(MAX_DESCRIPTION))
        if macro.timing not in TIMINGS:
            raise MacroError("bad_timing")
        if not [s for s in macro.steps if s.get("type") != "delay"]:
            raise MacroError("no_steps")
        validate_steps(macro.steps)
        if macro.id not in self._macros and len(self._macros) >= MAX_MACROS:
            raise MacroError("too_many_macros", str(MAX_MACROS))

    # ------------------------------------------------------------ changes
    def add(self, macro: Macro) -> Macro:
        macro.name = macro.name.strip()
        self.validate(macro)
        macro.created = macro.modified = _now()
        self._macros[macro.id] = macro
        return macro

    def update(self, macro: Macro) -> Macro:
        if macro.id not in self._macros:
            raise MacroError("unknown_macro")
        macro.name = macro.name.strip()
        self.validate(macro)
        macro.modified = max(_now(), self._macros[macro.id].modified + 0.001)
        self._macros[macro.id] = macro
        return macro

    def delete(self, macro_id: str) -> Optional[Macro]:
        removed = self._macros.pop(macro_id, None)
        if removed is not None:
            self._deleted[macro_id] = _now()
        return removed

    def restore(self, macro: Macro) -> None:
        """Put back a macro whose save failed (the caller retries later)."""

        self._macros[macro.id] = macro
        self._deleted.pop(macro.id, None)

    # ------------------------------------------------------------- saving
    def save(self) -> None:
        """Encrypt and write; merges what another SSH Manager wrote meanwhile (newest ``modified`` wins)."""

        if self.disabled or self._key is None or self._salt is None:
            raise MacroWriteError("locked")
        self._merge_external()
        data = encrypt_payload(self.list_sorted(), self._key, self._salt, self._iterations)
        self._write(data)

    def _merge_external(self) -> None:
        try:
            self._merge_external_file()
        except MacroFileError as exc:  # damaged meanwhile: never overwrite what cannot be read
            raise MacroWriteError(f"external_{exc.kind}") from None

    def _merge_external_file(self) -> None:
        raw = self._read_raw()
        if raw is None or hashlib.sha256(raw).hexdigest() == self._seen_digest:
            return
        _header_dict, salt, iterations = read_header(raw)
        if salt != self._salt or iterations != self._iterations:
            raise MacroWriteError("changed_key")  # another instance changed the master password
        for theirs in decrypt_payload(raw, self._key):  # type: ignore[arg-type]
            ours = self._macros.get(theirs.id)
            if theirs.id in self._deleted and self._deleted[theirs.id] >= theirs.modified:
                continue
            if ours is None or theirs.modified > ours.modified:
                self._macros[theirs.id] = theirs

    def _write(self, data: bytes) -> None:
        folder = self.path.parent
        try:
            created = not folder.exists()
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created or folder.stat().st_mode & 0o077:
                os.chmod(folder, 0o700)
            old = self._read_raw()
            if old is not None:
                _write_private(self.backup_path, old)
            _write_private(self.path, data)
        except (OSError, MacroFileError) as exc:
            raise MacroWriteError(type(exc).__name__) from None
        self._seen_digest = hashlib.sha256(data).hexdigest()

    def rekey(self, new_password: str) -> None:
        """Re-encrypt with a key derived from ``new_password`` (new salt). On failure the old file stays valid."""

        if self.disabled or self._key is None:
            raise MacroWriteError("locked")
        salt = os.urandom(SALT_BYTES)
        key = derive_key(new_password, salt, self._iterations)
        if not self.path.exists() and not self._macros:
            self._key, self._salt = key, salt
            return
        self._merge_external()
        data = encrypt_payload(self.list_sorted(), key, salt, self._iterations)
        old = self._read_raw()
        try:
            folder = self.path.parent
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            # The backup first (an old one needs the old password): if the main file cannot be
            # written afterwards, it still opens with the old password.
            _write_private(self.backup_path, data)
            _write_private(self.path, data)
        except OSError as exc:
            if old is not None:
                try:
                    _write_private(self.backup_path, old)
                except OSError:
                    pass
            raise MacroWriteError(type(exc).__name__) from None
        self._key, self._salt = key, salt
        self._seen_digest = hashlib.sha256(data).hexdigest()

    def remove_file_if_empty(self) -> bool:
        """Delete ``macros.enc`` (and its backup) when it holds no macro; ``False`` when macros exist."""

        if self._macros or (self.path.exists() and not self.unlocked):
            return False
        for path in (self.path, self.backup_path):
            path.unlink(missing_ok=True)
        return True


def default_macros_path() -> Path:
    from .settings import _default_config_dir

    return _default_config_dir() / FILE_NAME


__all__ = [
    "Macro", "MacroError", "MacroFileError", "MacroWriteError", "MacroRecorder", "MacroPlayer", "MacroStore",
    "KEYS", "KEY_NAMES", "parse_input", "normalize_steps", "step_bytes", "duration_ms", "derive_key",
    "encrypt_payload", "decrypt_payload", "read_header", "default_macros_path",
]
