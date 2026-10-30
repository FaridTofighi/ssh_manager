"""SSH password authentication: resolution, outcome classification, verified-success sentinel (no GTK).

* :class:`PasswordResolution` — which password a connect uses and where it
  came from (bound credential, legacy per-host secret, none).
* :func:`classify_exit` — what a failed ``ssh``/``sshpass`` run means. Only
  :attr:`AuthOutcome.AUTH_FAILED` may lead to a password prompt.
* :func:`sentinel_options` — ``-o PermitLocalCommand=yes -o LocalCommand=…``:
  OpenSSH runs ``LocalCommand`` on the client right after a **successful**
  authentication, so a marker file proves the login worked (a spawned process
  does not). Without a sentinel the app falls back to a heuristic
  (:func:`probably_authenticated`).

Nothing here stores, logs or returns a password except
``PasswordResolution.password`` itself.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from .text_utils import strip_ansi

MAX_ATTEMPTS = 3  # interactive password attempts per user-initiated connect
RETRY_DELAY_S = 1  # at least this long between attempts (no lock-out bursts)
SSHPASS_WRONG_PASSWORD = 5  # sshpass exit codes: 5 wrong password, 6 unknown host key, 3 runtime, 4 bad args
SSHPASS_HOST_KEY = 6
HEURISTIC_DELAY_S = 3  # fallback: alive this long without failure text = probably authenticated


# ------------------------------------------------------------------ resolution
@dataclass(frozen=True)
class PasswordResolution:
    """The password for a connect and its source (``credential`` | ``host`` | ``legacy`` | ``none``)."""

    password: Optional[str]
    source: str = "none"
    credential_id: Optional[str] = None

    def __repr__(self) -> str:  # never print the secret
        state = "set" if self.password else "none"
        return f"PasswordResolution(password=<{state}>, source={self.source!r}, credential_id={self.credential_id!r})"

    __str__ = __repr__


NO_PASSWORD = PasswordResolution(None, "none", None)


# --------------------------------------------------------------- classification
class AuthOutcome(str, Enum):
    OK = "ok"
    AUTH_FAILED = "auth_failed"
    HOSTKEY = "hostkey"
    NETWORK = "network"
    NO_PASSWORD_METHOD = "no_password_method"
    OTP_REQUIRED = "otp_required"
    OTHER = "other"


AUTH_FAILURE_RE = re.compile(
    r"permission\s+denied,\s+please\s+try\s+again|"
    r"permission\s+denied[^\n\r]*\bpassword\b|"
    r"authentication\s+failed",
    re.IGNORECASE,
)
_DENIED_METHODS_RE = re.compile(r"permission\s+denied\s*\(([^)]*)\)", re.IGNORECASE)
_HOSTKEY_RE = re.compile(r"host\s+key\s+verification\s+failed|remote\s+host\s+identification\s+has\s+changed|"
                         r"no\s+matching\s+host\s+key\s+type", re.IGNORECASE)
_NETWORK_RE = re.compile(
    r"connection\s+refused|connection\s+timed\s+out|operation\s+timed\s+out|no\s+route\s+to\s+host|"
    r"could\s+not\s+resolve\s+hostname|name\s+or\s+service\s+not\s+known|temporary\s+failure\s+in\s+name\s+resolution|"
    r"network\s+is\s+unreachable|connection\s+closed\s+by|connection\s+reset\s+by|kex_exchange_identification",
    re.IGNORECASE)
_TOO_MANY_RE = re.compile(r"too\s+many\s+authentication\s+failures", re.IGNORECASE)
# A prompt for a one-time code (at a line end), or explicit two-factor wording — never a bare word
# that could be part of a host name.
_OTP_RE = re.compile(
    r"(?:verification\s+code|one[-\s]time\s+(?:password|code)|\botp(?:\s+code)?|token\s+code|passcode|"
    r"authenticator\s+code)[^\n:]*:\s*$|two[-\s]factor\s+authentication|duo\s+two-factor",
    re.IGNORECASE | re.MULTILINE)
_PASSWORD_PROMPT_RE = re.compile(r"(?:password|passphrase)[^\n:]*:\s*$", re.IGNORECASE)
NO_PASSWORD_METHOD_MESSAGE = "The server does not accept password login for this account"


def _denied_methods(text: str) -> Optional[List[str]]:
    matches = _DENIED_METHODS_RE.findall(text)
    if not matches:
        return None
    return [method.strip().lower() for method in matches[-1].split(",") if method.strip()]


def classify_exit(status: int, output: str) -> AuthOutcome:
    """What a finished ``ssh``/``sshpass`` attempt that did not authenticate means.

    ``status`` is the exit status (``-1`` when killed by a signal); ``output``
    the end of the terminal text. ``AUTH_FAILED`` = sshpass exit 5 or a
    password-rejection message. Host-key, network, public-key-only and OTP
    situations are never ``AUTH_FAILED`` (they must not open a password prompt).
    """

    text = strip_ansi(output or "")
    if status == SSHPASS_WRONG_PASSWORD:
        return AuthOutcome.AUTH_FAILED
    if status == SSHPASS_HOST_KEY or _HOSTKEY_RE.search(text):
        return AuthOutcome.HOSTKEY
    if _OTP_RE.search(text):
        return AuthOutcome.OTP_REQUIRED
    methods = _denied_methods(text)
    if methods is not None and not ({"password", "keyboard-interactive"} & set(methods)):
        return AuthOutcome.NO_PASSWORD_METHOD
    if _TOO_MANY_RE.search(text):
        return AuthOutcome.OTHER
    if AUTH_FAILURE_RE.search(text):
        return AuthOutcome.AUTH_FAILED
    if _NETWORK_RE.search(text):
        return AuthOutcome.NETWORK
    if status == 0:
        return AuthOutcome.OK
    return AuthOutcome.OTHER


_PASSPHRASE_PROMPT_RE = re.compile(r"^Enter passphrase for key '([^'\n]+)': ?$")


def passphrase_prompt(output: str) -> Optional[str]:
    """The key path when ssh's ``Enter passphrase for key '…':`` is the **last** line (ssh waits there)."""

    lines = strip_ansi(output).replace("\r", "").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    match = _PASSPHRASE_PROMPT_RE.match(lines[-1].lstrip())
    return match.group(1) if match else None


class PassphraseFill:
    """A saved key passphrase goes in at most once per connection attempt, and only for the host's own key."""

    def __init__(self, identity: str) -> None:
        self.identity = os.path.realpath(os.path.expanduser(identity)) if identity else ""
        self._done = False

    def should_fill(self, prompt_path: str) -> bool:
        if self._done or not self.identity:
            return False
        return os.path.realpath(os.path.expanduser(prompt_path)) == self.identity

    def filled(self, _prompt_path: str = "") -> None:
        self._done = True

    def reset(self) -> None:
        self._done = False


def ends_with_password_prompt(output: str) -> bool:
    lines = [line for line in strip_ansi(output or "").splitlines() if line.strip()]
    return bool(lines) and bool(_PASSWORD_PROMPT_RE.search(lines[-1]))


def probably_authenticated(output: str, *, alive_s: float, output_after_password: bool) -> bool:
    """Fallback when no sentinel is possible: alive ≥ 3 s, new output, no failure, no password prompt."""

    return (alive_s >= HEURISTIC_DELAY_S and output_after_password
            and not AUTH_FAILURE_RE.search(strip_ansi(output or "")) and not ends_with_password_prompt(output))


# --------------------------------------------------------------------- sentinel
def marker_directory() -> Path:
    """Private directory for success markers (``$XDG_RUNTIME_DIR/ssh-manager``, mode 0700)."""

    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        directory = Path(base) / "ssh-manager"
    else:
        directory = Path(tempfile.gettempdir()) / f"ssh-manager-{os.getuid()}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
        if os.stat(directory).st_uid != os.getuid():
            raise OSError("marker directory is not ours")
    except OSError:
        directory = Path(tempfile.mkdtemp(prefix="ssh-manager-"))  # private, 0700
    return directory


def new_marker_path(directory: Optional[Path] = None) -> Path:
    return (directory or marker_directory()) / f"{uuid.uuid4().hex}.ok"


def _lookup(options: Mapping[str, Any], name: str) -> Optional[str]:
    for key, value in options.items():
        if str(key).strip().lower() == name:
            text = str(value).strip()
            return text or None
    return None


def sentinel_options(options: Mapping[str, Any], marker: Path) -> Tuple[List[str], str]:
    """``(ssh -o arguments, method)``: the success sentinel for this spawn, or ``([], "heuristic")``.

    The marker command is ``touch -- '<path>'`` (works in sh, bash, zsh and
    fish; ``%`` is doubled because ``LocalCommand`` expands ``%`` tokens). A
    host's own ``LocalCommand`` runs after it (``marker; command``); a host that
    sets ``PermitLocalCommand no`` gets no sentinel. Command-line ``-o``
    options come first, so they win over the host's own values.
    """

    permit = (_lookup(options, "permitlocalcommand") or "").lower()
    if permit in ("no", "false"):
        return [], "heuristic"
    touch = "touch -- " + shlex.quote(str(marker)).replace("%", "%%")
    user_command = _lookup(options, "localcommand")
    command = f"{touch}; {user_command}" if user_command else touch
    return ["-o", "PermitLocalCommand=yes", "-o", f"LocalCommand={command}"], "sentinel"


def with_sentinel(argv: List[str], extra: List[str]) -> List[str]:
    """Insert the sentinel options right after ``ssh`` (before any host target)."""

    if not extra:
        return list(argv)
    index = next((i for i, item in enumerate(argv) if os.path.basename(item) == "ssh"), None)
    if index is None:
        return list(argv)
    return argv[: index + 1] + extra + argv[index + 1:]


# ---------------------------------------------------------------- recovery flow
SOURCE_PROMPTED = ("prompt", "retry")  # a password the user typed in this tab


def source_phrase(source: str, credential_name: str = "") -> str:
    """How the dialogs name a rejected password (never the password itself)."""

    if source == "credential":
        return f"saved password (credential ‘{credential_name}’)" if credential_name else "saved password (credential)"
    if source == "host":
        return "saved password (host-specific password)"
    if source in ("legacy", "stored"):
        return "saved password"
    if source == "session":
        return "password entered earlier in this tab"
    if source in SOURCE_PROMPTED:
        return "password you entered"
    return ""


@dataclass(frozen=True)
class PasswordRequest:
    """Text of the password dialog (first prompt or retry after ``AUTH_FAILED``)."""

    alias: str
    target: str  # user@host:port
    attempt: int
    max_attempts: int = MAX_ATTEMPTS
    rejected: str = ""  # source_phrase() of the rejected password; "" = first prompt

    @property
    def title(self) -> str:
        return f"Password for ‘{self.alias}’"

    @property
    def message(self) -> str:
        if self.rejected:
            return f"‘{self.alias}’ rejected the {self.rejected}. Enter the password for {self.target}."
        return f"Enter the password for {self.target}."

    @property
    def attempt_text(self) -> str:
        return f"Attempt {self.attempt} of {self.max_attempts}"


class Decision(str, Enum):
    SAVE = "save"  # store it as the host's own password (a private credential)
    DONT_SAVE = "dont_save"  # this tab only (also: Escape / window close)
    SESSION_ONLY = "session_only"  # the host cannot be edited: this tab only


@dataclass(frozen=True)
class SaveContext:
    """What saving would mean for the host right now (asked at offer time)."""

    can_save: bool = True  # False: can_edit=false, read-only inventory or secrets policy
    reason: str = ""  # why saving is not possible (shown in the dialog)
    credential_name: str = ""  # the shared credential that stays unchanged
    has_private: bool = False  # the host already has its own password


@dataclass(frozen=True)
class SaveOffer:
    """The "Save this password for ‘alias’?" confirmation (text only)."""

    alias: str
    verified: bool  # sentinel (True) or heuristic (False)
    context: SaveContext

    @property
    def title(self) -> str:
        return f"Save this password for ‘{self.alias}’?"

    @property
    def message(self) -> str:
        lead = ("Login succeeded with the password you entered." if self.verified
                else "Login appears to have succeeded with the password you entered.")
        if not self.context.can_save:
            reason = self.context.reason or "This host cannot be edited."
            return f"{lead} {reason} The password can be used for this session only."
        effect = ("Saving it replaces the password saved for this host." if self.context.has_private
                  else "Saving it makes this host use its own password from now on.")
        unchanged = (f"The credential ‘{self.context.credential_name}’ and other hosts are not changed."
                     if self.context.credential_name else "Other hosts are not changed.")
        return f"{lead} {effect} {unchanged}"


class FailureAction(str, Enum):
    PROMPT = "prompt"  # ask again (after RETRY_DELAY_S)
    EXHAUSTED = "exhausted"  # MAX_ATTEMPTS used: stop, keep Reconnect
    STOP = "stop"  # not an authentication failure: never ask


class PasswordFlow:
    """Per-tab password recovery state (no GTK, no I/O, no globals).

    attempt → ``AUTH_FAILED`` → prompt (≤ ``MAX_ATTEMPTS`` per connect) →
    verified success → at most one save offer per tab. Nothing here stores a
    password; the typed one lives in this object until the tab goes away.
    """

    def __init__(self, alias: str, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.alias = alias
        self.max_attempts = max_attempts
        self.attempts = 0  # prompts shown in this user-initiated connect
        self.source = "none"  # where the current attempt's password came from
        self.typed: Optional[str] = None  # the password typed for the current attempt
        self.stored: Optional[str] = None  # the resolved password of this connect (to skip re-offering it)
        self.offered = False  # the save confirmation was shown in this tab (once per tab)
        self.decision: Optional[Decision] = None

    def __repr__(self) -> str:  # never print the secrets
        return f"PasswordFlow(alias={self.alias!r}, attempts={self.attempts}, source={self.source!r})"

    def begin_connect(self) -> None:
        """A user-initiated connect / reconnect: a fresh attempt budget."""

        self.attempts = 0

    def attempt(self, source: str, password: Optional[str], *, stored: Optional[str] = None,
                resolved: bool = False) -> None:
        """Record the password sent by the next spawn (``resolved``: ``stored`` is this connect's resolution)."""

        self.source = source
        self.typed = password if source in SOURCE_PROMPTED else None
        if resolved:
            self.stored = stored

    def on_failure(self, outcome: AuthOutcome) -> FailureAction:
        if outcome is not AuthOutcome.AUTH_FAILED:
            return FailureAction.STOP
        if self.attempts >= self.max_attempts:
            return FailureAction.EXHAUSTED
        return FailureAction.PROMPT

    def can_prompt(self) -> bool:
        return self.attempts < self.max_attempts

    def next_request(self, target: str, rejected: str = "") -> PasswordRequest:
        """Count a prompt and describe it ("Attempt N of 3")."""

        self.attempts += 1
        return PasswordRequest(self.alias, target, self.attempts, self.max_attempts, rejected)

    def on_authenticated(self, method: str, context: SaveContext) -> Tuple[Optional[SaveOffer], str]:
        """``(offer or None, notice)`` after a verified (``sentinel``) or probable (``heuristic``) login.

        Only a password typed in this tab is offered, once per tab, and not
        when it equals the stored one.
        """

        self.attempts = 0
        if self.source not in SOURCE_PROMPTED or not self.typed or self.offered:
            return None, ""
        if self.stored is not None and self.typed == self.stored:
            return None, ""  # it is already saved: say nothing
        self.offered = True
        return SaveOffer(self.alias, method == "sentinel", context), ""

    def decide(self, decision: Decision) -> None:
        self.decision = decision
