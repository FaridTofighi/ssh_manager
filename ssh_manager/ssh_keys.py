"""SSH keys (no GTK): find, read, generate, re-protect, and install public keys on hosts.

* **Reading never asks for a passphrase.** The public half comes from the
  ``.pub`` file, from the unencrypted public section of an OpenSSH-format private
  key, or from an unencrypted PEM key; otherwise it is "unknown (encrypted)"
  until :func:`derive_public_line` is given the passphrase.
* **Generating** uses ``cryptography``: Ed25519 (default), RSA 3072 / 4096, ECDSA
  P-256 / P-384. The private key is OpenSSH format, encrypted with the passphrase
  (``BestAvailableEncryption``, bcrypt KDF); without ``bcrypt`` the traditional
  encrypted PEM for RSA / ECDSA (OpenSSH and Paramiko read it; Paramiko cannot read
  encrypted PKCS#8), and a passphrase-protected Ed25519 key is refused with the reason. Files are created with
  ``os.open(…, O_EXCL, 0o600)`` (never overwritten, never wider permissions), the
  folder 0700, and the key is loaded back with Paramiko before success is reported.
* **Passphrases** are only ever passed in memory: never on a command line, in the
  environment, a log or an error message.
* **authorized_keys**: :func:`authorized_keys_add` appends a key only if its blob is
  not there yet (comments and options are ignored for the comparison); every other
  byte is kept. :func:`deploy_public_key` does that on a server over SSH, and
  :class:`DeployRun` on many servers.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .gtkui.i18n import _

LOGGER = logging.getLogger(__name__)

MAX_KEY_SIZE = 64 * 1024
PRIVATE_HEADERS = {
    b"-----BEGIN OPENSSH PRIVATE KEY-----": "openssh",
    b"-----BEGIN RSA PRIVATE KEY-----": "pem",
    b"-----BEGIN EC PRIVATE KEY-----": "pem",
    b"-----BEGIN DSA PRIVATE KEY-----": "pem",
    b"-----BEGIN PRIVATE KEY-----": "pkcs8",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----": "pkcs8",
}
SKIPPED_NAMES = ("known_hosts", "config", "authorized_keys", "environment")
KIND_DEFAULTS = {"ed25519": 0, "rsa": 3072, "ecdsa": 256}
THREAD_PREFIX = "sshm-keys"


class KeyError_(Exception):  # noqa: N801  (not the builtin KeyError)
    """A clear reason, never a passphrase or key material."""


# ------------------------------------------------------------------ reading
@dataclass(frozen=True)
class KeyInfo:
    path: Path
    key_type: str = ""  # ed25519, rsa, ecdsa, dsa, ed25519-sk … ("" when unknown)
    bits: int = 0
    fingerprint: str = ""  # "" when the public half cannot be read without the passphrase
    comment: str = ""
    encrypted: Optional[bool] = None  # None: unknown
    has_public_file: bool = False
    public_line: str = ""
    format: str = ""  # openssh, pem, pkcs8

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def type_label(self) -> str:
        if not self.key_type:
            return _("unknown (encrypted)") if self.encrypted else _("unknown")
        return f"{self.key_type} {self.bits}" if self.bits and self.key_type != "ed25519" else self.key_type


def _blob(text_or_blob: Union[str, bytes]) -> Optional[bytes]:
    if isinstance(text_or_blob, bytes):
        return text_or_blob
    parts = text_or_blob.split()
    candidates = parts[1:2] if len(parts) >= 2 else parts
    for part in candidates:
        try:
            return base64.b64decode(part, validate=True)
        except (binascii.Error, ValueError):
            return None
    return None


def fingerprint_sha256(pub_line_or_blob: Union[str, bytes]) -> str:
    """``SHA256:…`` like ``ssh-keygen -l`` for a public key line (or its decoded blob); ``""`` if unreadable."""

    import hashlib

    blob = _blob(pub_line_or_blob)
    if not blob:
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def _strings(data: bytes, offset: int = 0, count: int = 64) -> Tuple[List[bytes], int]:
    out: List[bytes] = []
    while len(out) < count and offset + 4 <= len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        offset += 4
        if offset + length > len(data):
            break
        out.append(data[offset:offset + length])
        offset += length
    return out, offset


def _type_and_bits(blob: bytes) -> Tuple[str, int]:
    parts, _offset = _strings(blob, 0, 3)
    if not parts:
        return "", 0
    name = parts[0].decode("ascii", "replace")
    if name == "ssh-ed25519":
        return "ed25519", 256
    if name == "sk-ssh-ed25519@openssh.com":
        return "ed25519-sk", 256
    if name.startswith("ecdsa-sha2-nistp"):
        return "ecdsa", int(name.rsplit("p", 1)[1]) if name.rsplit("p", 1)[1].isdigit() else 0
    if name.startswith("sk-ecdsa"):
        return "ecdsa-sk", 256
    if name == "ssh-rsa" and len(parts) >= 3:
        return "rsa", int.from_bytes(parts[2], "big").bit_length()
    if name == "ssh-dss" and len(parts) >= 2:
        return "dsa", int.from_bytes(parts[1], "big").bit_length()
    return name, 0


def _openssh_public(data: bytes) -> Tuple[Optional[bytes], Optional[bool]]:
    """The public blob and whether it is encrypted, from an OpenSSH private key file (no passphrase needed)."""

    body = b"".join(line.strip() for line in data.splitlines()[1:] if line and not line.startswith(b"-----"))
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return None, None
    magic = b"openssh-key-v1\x00"
    if not raw.startswith(magic):
        return None, None
    parts, offset = _strings(raw, len(magic), 3)  # cipher, kdf, kdf options
    if len(parts) < 3 or offset + 4 > len(raw):
        return None, None
    (count,) = struct.unpack(">I", raw[offset:offset + 4])
    keys, _end = _strings(raw, offset + 4, 1)
    if count < 1 or not keys:
        return None, parts[0] != b"none"
    return keys[0], parts[0] != b"none"


def _public_from_file(path: Path) -> Tuple[str, str]:
    """``(line, comment)`` from ``<key>.pub``."""

    pub = path.with_name(path.name + ".pub")
    try:
        text = pub.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "", ""
    parts = text.split(None, 2)
    if len(parts) < 2 or _blob(text) is None:
        return "", ""
    return " ".join(parts[:2]), parts[2] if len(parts) > 2 else ""


def _header(data: bytes) -> str:
    first = data.split(b"\n", 1)[0].strip()
    return PRIVATE_HEADERS.get(first, "")


def is_private_key_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size > MAX_KEY_SIZE:
            return False
        with open(path, "rb") as handle:
            return bool(_header(handle.read(64)))
    except OSError:
        return False


def read_key_info(path: Path) -> KeyInfo:
    path = Path(path)
    data = path.read_bytes()[:MAX_KEY_SIZE + 1]
    kind = _header(data)
    line, comment = _public_from_file(path)
    encrypted: Optional[bool] = None
    blob = _blob(line) if line else None
    if kind == "openssh":
        found, encrypted = _openssh_public(data)
        blob = blob or found
    elif kind in ("pem", "pkcs8"):
        encrypted = b"ENCRYPTED" in data[:400]
        if blob is None and not encrypted:
            try:
                from cryptography.hazmat.primitives import serialization

                private = serialization.load_pem_private_key(data, password=None)
                text = private.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                         serialization.PublicFormat.OpenSSH).decode("ascii")
                blob = _blob(text)
            except Exception:  # unsupported or damaged: listed, unknown type
                blob = None
    key_type, bits = _type_and_bits(blob) if blob else ("", 0)
    if blob and not line:
        name = _strings(blob, 0, 1)[0]
        line = f"{name[0].decode('ascii', 'replace')} {base64.b64encode(blob).decode('ascii')}" if name else ""
    return KeyInfo(path=path, key_type=key_type, bits=bits, fingerprint=fingerprint_sha256(blob) if blob else "",
                   comment=comment, encrypted=encrypted, has_public_file=path.with_name(path.name + ".pub").exists(),
                   public_line=(f"{line} {comment}".strip() if line else ""), format=kind)


def default_ssh_dir() -> Path:
    return Path("~/.ssh").expanduser()


def scan_ssh_dir(directory: Optional[Path] = None, extra: Iterable[Path] = ()) -> List[KeyInfo]:
    """Private keys in ``~/.ssh`` (plus ``extra`` paths), never asking for a passphrase."""

    directory = Path(directory) if directory is not None else default_ssh_dir()
    found: Dict[Path, KeyInfo] = {}
    candidates: List[Path] = []
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            name = path.name
            if name.endswith(".pub") or name.startswith(SKIPPED_NAMES) or name.startswith("."):
                continue
            candidates.append(path)
    candidates.extend(Path(p).expanduser() for p in extra)
    for path in candidates:
        if path in found or not is_private_key_file(path):
            continue
        try:
            found[path] = read_key_info(path)
        except OSError as exc:
            LOGGER.info("ssh keys: %s cannot be read (%s)", path.name, type(exc).__name__)
    return sorted(found.values(), key=lambda info: info.name.casefold())


# --------------------------------------------------------------- generating
def bcrypt_available() -> bool:
    try:
        import bcrypt  # noqa: F401
    except Exception:
        return False
    return True


def suggest_name(directory: Path, base: str = "id_ed25519") -> str:
    """``base``, else ``base_2``, ``base_3`` … (neither the key nor its ``.pub`` exist)."""

    directory = Path(directory)
    name, index = base, 2
    while (directory / name).exists() or (directory / f"{name}.pub").exists():
        name, index = f"{base}_{index}", index + 1
    return name


def make_private_key(kind: str = "ed25519", bits: int = 0):
    """The key itself (RSA 4096 takes a moment: call it off the GTK thread)."""

    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    if kind == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    if kind == "rsa":
        if bits not in (2048, 3072, 4096):
            raise KeyError_(_("RSA keys are 3072 or 4096 bits."))
        return rsa.generate_private_key(public_exponent=65537, key_size=bits)
    if kind == "ecdsa":
        curves = {256: ec.SECP256R1(), 384: ec.SECP384R1(), 521: ec.SECP521R1()}
        if bits not in curves:
            raise KeyError_(_("ECDSA keys use P-256 or P-384."))
        return ec.generate_private_key(curves[bits])
    raise KeyError_(_("Unknown key type."))


def _serialise(private, passphrase: str) -> Tuple[bytes, str]:
    from cryptography.hazmat.primitives import serialization

    if not passphrase:
        return private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                     serialization.NoEncryption()), "openssh"
    secret = passphrase.encode("utf-8")
    if bcrypt_available():
        return private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                     serialization.BestAvailableEncryption(secret)), "openssh"
    # Without bcrypt: the traditional encrypted PEM (RSA / ECDSA), which OpenSSH and Paramiko both read.
    # (Paramiko cannot read encrypted PKCS#8 at all, and Ed25519 has no traditional PEM form.)
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if isinstance(private, ed25519.Ed25519PrivateKey):
        raise KeyError_(_("A passphrase-protected Ed25519 key needs the bcrypt module (python3-bcrypt). Install "
                          "it, or choose RSA or ECDSA."))
    return private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.BestAvailableEncryption(secret)), "pem"


def public_key_line(private, comment: str = "") -> str:
    from cryptography.hazmat.primitives import serialization

    line = private.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                             serialization.PublicFormat.OpenSSH).decode("ascii")
    return f"{line} {comment}".strip()


def _write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)  # never over an existing file
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(directory: Path) -> None:
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True)
        os.chmod(directory, 0o700)


def _load_check(path: Path, passphrase: str) -> None:
    import paramiko

    try:
        paramiko.PKey.from_path(path, passphrase.encode("utf-8") if passphrase else None)
    except Exception as exc:
        raise KeyError_(_("The new key could not be read back ({error}).").format(error=type(exc).__name__)) from None


def write_private_key(path: Path, private, *, comment: str = "", passphrase: str = "") -> KeyInfo:
    path = Path(path).expanduser()
    pub = path.with_name(path.name + ".pub")
    if path.exists() or pub.exists():
        raise KeyError_(_("{name} already exists; choose another name.").format(name=path.name))
    _ensure_dir(path.parent)
    data, kind = _serialise(private, passphrase)
    line = public_key_line(private, comment)
    try:
        _write_new(path, data, 0o600)
        _write_new(pub, (line + "\n").encode("ascii"), 0o644)
        _load_check(path, passphrase)
    except FileExistsError:
        raise KeyError_(_("{name} already exists; choose another name.").format(name=path.name)) from None
    except BaseException:
        for leftover in (path, pub):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    return replace(read_key_info(path), format=kind)


def generate_key(path: Path, *, kind: str = "ed25519", bits: int = 0, comment: str = "",
                 passphrase: str = "") -> KeyInfo:
    return write_private_key(path, make_private_key(kind, bits or KIND_DEFAULTS.get(kind, 0)), comment=comment,
                             passphrase=passphrase)


def _load_private(path: Path, passphrase: str):
    from cryptography.hazmat.primitives import serialization

    data = Path(path).read_bytes()
    secret = passphrase.encode("utf-8") if passphrase else None
    try:
        if _header(data) == "openssh":
            return serialization.load_ssh_private_key(data, password=secret), _header(data)
        return serialization.load_pem_private_key(data, password=secret), _header(data)
    except (ValueError, TypeError):
        raise KeyError_(_("The passphrase is not correct, or the key cannot be read.")) from None


def derive_public_line(path: Path, passphrase: str) -> str:
    private, _kind = _load_private(path, passphrase)
    return public_key_line(private)


def change_passphrase(path: Path, old: str, new: str) -> None:
    """Re-encrypt the key with ``new`` (empty: no passphrase); atomic, 0600, the format kept when possible."""

    import tempfile

    path = Path(path)
    private, _kind = _load_private(path, old)
    data, _format = _serialise(private, new)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:  # mkstemp creates it 0600
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _load_check(Path(temp), new)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


# --------------------------------------------------------- authorized_keys
_KEY_TOKEN = re.compile(r"(?:^|\s)((?:ssh|ecdsa|sk)-[A-Za-z0-9@.-]+)\s+([A-Za-z0-9+/]+={0,3})(?=\s|$)")


def _blobs(text: str) -> set:
    found = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_TOKEN.search(line)
        if match:
            found.add(match.group(2))
    return found


def authorized_keys_add(text: str, line: str) -> Tuple[str, bool]:
    """``(new text, changed)``: ``line`` appended unless its key blob is already there (other bytes kept)."""

    match = _KEY_TOKEN.search(line)
    if match is None:
        raise KeyError_(_("This is not a public key line."))
    if match.group(2) in _blobs(text):
        return text, False
    newline = "\r\n" if "\r\n" in text else "\n"
    prefix = text if not text or text.endswith("\n") else text + newline
    return prefix + line.strip() + newline, True


# --------------------------------------------------------------- deploying
INSTALLED, ALREADY_PRESENT, AUTH_FAILED, HOST_KEY_CHANGED, HOST_KEY_UNKNOWN = (
    "installed", "already_present", "auth_failed", "host_key_changed", "host_key_unknown")
UNREACHABLE, TIMEOUT, FAILED, QUEUED, RUNNING, CANCELLED = (
    "unreachable", "timeout", "failed", "queued", "running", "cancelled")
DEPLOY_FAILURES = (AUTH_FAILED, HOST_KEY_CHANGED, HOST_KEY_UNKNOWN, UNREACHABLE, TIMEOUT, FAILED)


def deploy_status_label(status: str) -> str:
    return {INSTALLED: _("Installed"), ALREADY_PRESENT: _("Already present"), AUTH_FAILED: _("Auth failed"),
            HOST_KEY_CHANGED: _("Host key changed"), HOST_KEY_UNKNOWN: _("Host key unknown"),
            UNREACHABLE: _("Unreachable"), TIMEOUT: _("Timeout"), FAILED: _("Failed"), QUEUED: _("Queued"),
            RUNNING: _("Running"), CANCELLED: _("Cancelled")}.get(status, status)


@dataclass
class DeployResult:
    key: str = ""
    alias: str = ""
    status: str = QUEUED
    message: str = ""
    verified: bool = False  # a login with only this key worked afterwards

    @property
    def finished(self) -> bool:
        return self.status not in (QUEUED, RUNNING)


def _exec(transport: Any, script: str, timeout: float) -> Tuple[int, str, str]:
    channel = transport.open_session(timeout=timeout)
    channel.settimeout(timeout)
    channel.exec_command("sh -s")
    channel.sendall(script.encode("utf-8"))
    channel.shutdown_write()
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() > deadline:
            channel.close()
            raise TimeoutError("the server did not answer")
        while channel.recv_ready():
            out += channel.recv(65536)
        while channel.recv_stderr_ready():
            err += channel.recv_stderr(65536)
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        time.sleep(0.02)
    return channel.recv_exit_status(), out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


PREPARE = ("umask 077\nmkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
           "chmod 600 ~/.ssh/authorized_keys && cat ~/.ssh/authorized_keys\n")


def deploy_public_key(transport: Any, line: str, *, timeout: float = 20.0) -> DeployResult:
    """Install ``line`` in ``~/.ssh/authorized_keys`` over an authenticated transport (append-only, idempotent)."""

    code, current, err = _exec(transport, PREPARE, timeout)
    if code != 0:
        return DeployResult(status=FAILED, message=_("~/.ssh could not be prepared: {error}").format(
            error=err.strip()[:200] or code))
    _updated, changed = authorized_keys_add(current, line)
    if not changed:
        return DeployResult(status=ALREADY_PRESENT, message=_("The key was already installed."))
    blob = _KEY_TOKEN.search(line).group(2)  # base64: safe inside single quotes
    # Check again and append under a lock: two hosts of the inventory can be one server.
    script = (
        'F="$HOME/.ssh/authorized_keys"\nexec 9>>"$F"\ncommand -v flock >/dev/null 2>&1 && flock 9\n'
        f"if grep -v '^[[:space:]]*#' \"$F\" | grep -q -F -- '{blob}'; then echo SSHM_PRESENT; else\n"
        '  [ -s "$F" ] && [ "$(tail -c 1 "$F" | od -An -tx1 | tr -d \' \')" != "0a" ] && printf \'\\n\' >> "$F"\n'
        f"  cat >> \"$F\" <<'SSHM_KEY_EOF'\n{line.strip()}\nSSHM_KEY_EOF\n  echo SSHM_ADDED\nfi\n"
        'chmod 600 "$F"\n')
    code, out, err = _exec(transport, script, timeout)
    if code != 0:
        return DeployResult(status=FAILED, message=_("authorized_keys could not be written: {error}").format(
            error=err.strip()[:200] or code))
    if "SSHM_PRESENT" in out:
        return DeployResult(status=ALREADY_PRESENT, message=_("The key was already installed."))
    return DeployResult(status=INSTALLED, message=_("Installed."))


def _deploy_classify(exc: BaseException) -> Tuple[str, str]:
    from .multirun import engine as R

    status, message = R._classify(exc)
    mapping = {R.AUTH_FAILED: AUTH_FAILED, R.HOST_KEY_CHANGED: HOST_KEY_CHANGED, R.HOST_KEY_UNKNOWN: HOST_KEY_UNKNOWN,
               R.UNREACHABLE: UNREACHABLE}
    if isinstance(exc, TimeoutError):
        return TIMEOUT, _("The server did not answer in time.")
    return mapping.get(status, FAILED), message


class DeployRun:
    """Install one public key on many hosts (``workers`` at a time), then check that the key alone logs in.

    ``connect(target, timeout)``: the host's usual authenticated transport; ``verify(target, timeout)``: a
    connection with only this key (no agent, no password) — raises if the key login fails.
    """

    def __init__(self, targets: Sequence[Any], line: str, *, connect: Callable[[Any, float], Any],
                 verify: Callable[[Any, float], Any], workers: int = 8, timeout: float = 20.0) -> None:
        self.targets = list(targets)
        self.line = line
        self.connect = connect
        self.verify = verify
        self.workers = max(1, min(32, workers))
        self.timeout = timeout
        self._results = {t.key: DeployResult(t.key, t.alias) for t in self.targets}
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._cancel = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name=f"{THREAD_PREFIX}-deploy").start()

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def results(self) -> List[DeployResult]:
        with self._lock:
            return [replace(self._results[t.key]) for t in self.targets]

    def _set(self, key: str, **changes: Any) -> None:
        with self._lock:
            self._results[key] = replace(self._results[key], **changes)

    def _run(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=f"{THREAD_PREFIX}-worker") as pool:
                for future in [pool.submit(self._one, target) for target in self.targets]:
                    future.result()
        finally:
            self._done.set()

    def _one(self, target: Any) -> None:
        if self._cancel.is_set():
            self._set(target.key, status=CANCELLED)
            return
        self._set(target.key, status=RUNNING)
        transport = None
        try:
            transport = self.connect(target, self.timeout)
            result = deploy_public_key(transport, self.line, timeout=self.timeout)
        except Exception as exc:
            status, message = _deploy_classify(exc)
            LOGGER.debug("ssh keys: deploying to %s failed (%s)", target.alias, type(exc).__name__)
            self._set(target.key, status=status, message=message)
            return
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
        message = result.message
        verified = False
        if result.status in (INSTALLED, ALREADY_PRESENT):
            check = None
            try:
                check = self.verify(target, self.timeout)
                verified = True
                message += " " + _("The key login works.")
            except Exception as exc:
                status, reason = _deploy_classify(exc)
                message += " " + _("But a login with only this key failed: {reason}").format(reason=reason)
            finally:
                if check is not None:
                    try:
                        check.close()
                    except Exception:
                        pass
        self._set(target.key, status=result.status, message=message, verified=verified)


__all__ = ["KeyInfo", "KeyError_", "scan_ssh_dir", "read_key_info", "fingerprint_sha256", "generate_key",
           "make_private_key", "write_private_key", "change_passphrase", "derive_public_line", "public_key_line",
           "authorized_keys_add", "deploy_public_key", "DeployRun", "DeployResult", "deploy_status_label",
           "suggest_name", "bcrypt_available", "default_ssh_dir", "is_private_key_file", "INSTALLED",
           "ALREADY_PRESENT", "AUTH_FAILED", "HOST_KEY_CHANGED", "HOST_KEY_UNKNOWN", "UNREACHABLE", "TIMEOUT",
           "FAILED", "DEPLOY_FAILURES"]
