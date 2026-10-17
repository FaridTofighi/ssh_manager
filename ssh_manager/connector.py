"""Connection orchestration for SSH authentication using Paramiko."""

from __future__ import annotations

import logging
import os
import queue
import shlex
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

import paramiko
from gi.repository import GLib  # type: ignore

from .connection_state import ConnectionState, ConnectionStateMachine
from .secure_store import SecureStore, SecureStoreError

LOGGER = logging.getLogger(__name__)


class HostConfig(Protocol):
    host: str
    port: int
    username: str
    timeout: float
    remember_password_default: bool
    parent_window: Optional[object]


class Settings(Protocol):
    remember_password_default: bool
    host_key_policy: str
    allowed_ciphers: list[str]
    allowed_kex: list[str]


@dataclass
class TryResult:
    ok: bool
    is_auth_error: bool
    message: str
    client: Optional[paramiko.SSHClient] = None


def _split_algorithms(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _option_value(options: Dict[str, Any], *names: str) -> Optional[str]:
    wanted = {name.lower() for name in names}
    for key, value in options.items():
        if str(key).strip().lower() not in wanted:
            continue
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    return None


def _option_bool(options: Dict[str, Any], name: str, default: bool) -> bool:
    value = _option_value(options, name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in {"yes", "true", "1", "on"}:
        return True
    if lowered in {"no", "false", "0", "off"}:
        return False
    return default


def _expand_proxy_command(command: str, host: str, port: int, username: str) -> str:
    """Expand the ssh_config tokens Paramiko does not handle itself."""

    replacements = {"%h": host, "%p": str(port), "%r": username, "%n": host, "%%": "%"}
    out: List[str] = []
    idx = 0
    while idx < len(command):
        token = command[idx : idx + 2]
        if token in replacements:
            out.append(replacements[token])
            idx += 2
        else:
            out.append(command[idx])
            idx += 1
    return "".join(out)


def _jump_proxy_command(jump_spec: str, host: str, port: int) -> Optional[str]:
    """Build an OpenSSH stdio-forward command that reaches host through ProxyJump hops."""

    hops = [hop.strip() for hop in jump_spec.split(",") if hop.strip()]
    if not hops or hops[0].lower() == "none":
        return None
    last = hops[-1]
    destination = last if last.startswith("ssh://") else f"ssh://{last}"
    command = ["ssh", "-o", "BatchMode=yes", "-W", f"{host}:{port}"]
    if len(hops) > 1:
        command.extend(["-J", ",".join(hops[:-1])])
    command.append(destination)
    return shlex.join(command)


def _proxy_command_for(options: Dict[str, Any], host: str, port: int, username: str) -> Optional[str]:
    proxy_command = _option_value(options, "proxycommand", "proxy_command")
    if proxy_command:
        if proxy_command.lower() == "none":
            return None
        return _expand_proxy_command(proxy_command, host, port, username)
    jump = _option_value(options, "proxyjump", "proxy_jump", "jump_host")
    if jump:
        return _jump_proxy_command(jump, host, port)
    return None


def _policy_from_settings(settings: Settings) -> str:
    policy = getattr(settings, "host_key_policy", None)
    if policy is None:
        legacy = getattr(settings, "strict_host_key_checking", "reject")
        if isinstance(legacy, bool):
            return "reject" if legacy else "auto-add"
        policy = legacy
    return "auto-add" if policy == "auto-add" else "reject"


def _disabled_algorithms_for_allowed(
    allowed: List[str],
    preferred: Tuple[str, ...],
) -> List[str]:
    if not allowed or not preferred:
        return []
    allowed_set = set(allowed)
    return [name for name in preferred if name not in allowed_set]


def _connection_algorithm_overrides(
    options: Dict[str, Any],
    settings: Settings,
) -> Dict[str, List[str]]:
    host_ciphers = _split_algorithms(_option_value(options, "ciphers", "cipherspec"))
    host_kex = _split_algorithms(_option_value(options, "kexalgorithms", "kex"))
    allowed_ciphers = host_ciphers or list(getattr(settings, "allowed_ciphers", []) or [])
    allowed_kex = host_kex or list(getattr(settings, "allowed_kex", []) or [])

    disabled: Dict[str, List[str]] = {}
    cipher_disables = _disabled_algorithms_for_allowed(
        allowed_ciphers,
        tuple(getattr(paramiko.Transport, "_preferred_ciphers", ())),
    )
    if cipher_disables:
        disabled["ciphers"] = cipher_disables

    kex_disables = _disabled_algorithms_for_allowed(
        allowed_kex,
        tuple(getattr(paramiko.Transport, "_preferred_kex", ())),
    )
    if kex_disables:
        disabled["kex"] = kex_disables

    return disabled


class Connector:
    """Manage SSH connection flow including password retries."""

    def __init__(
        self,
        password_dialog,
        secure_store: SecureStore,
        settings: Settings,
        state_machine: ConnectionStateMachine,
        *,
        main_invoke: Optional[Callable[[Callable[[], object]], object]] = None,
        main_queue: Optional[Callable[[Callable[..., None], tuple, dict], None]] = None,
    ) -> None:
        self.password_dialog = password_dialog
        self.secure_store = secure_store
        self.settings = settings
        self.state_machine = state_machine
        self._success_callbacks: List[Callable[[paramiko.SSHClient], None]] = []
        self._aborted_callbacks: List[Callable[[str], None]] = []
        self._error_callbacks: List[Callable[[str], None]] = []
        self._warning_callbacks: List[Callable[[str], None]] = []
        self._main_thread = threading.main_thread()
        self._worker_thread: Optional[threading.Thread] = None
        self._current_client: Optional[paramiko.SSHClient] = None
        self._main_invoke = main_invoke or self._default_main_invoke
        self._main_queue = main_queue or self._default_main_queue

    # ---------------------------------------------------------------- callbacks
    def add_connect_success_callback(self, callback: Callable[[paramiko.SSHClient], None]) -> None:
        self._success_callbacks.append(callback)

    def add_aborted_callback(self, callback: Callable[[str], None]) -> None:
        self._aborted_callbacks.append(callback)

    def add_error_callback(self, callback: Callable[[str], None]) -> None:
        self._error_callbacks.append(callback)

    def add_warning_callback(self, callback: Callable[[str], None]) -> None:
        self._warning_callbacks.append(callback)

    # ------------------------------------------------------------------ helpers
    def connect_to_host(self, host_cfg: HostConfig) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            raise RuntimeError("Connection already in progress.")
        if self.state_machine.state != ConnectionState.DISCONNECTED:
            self._cleanup()
            self.state_machine.reset()

        def worker() -> None:
            try:
                self._run_connection(host_cfg)
            finally:
                self._worker_thread = None

        self._worker_thread = threading.Thread(
            target=worker,
            daemon=True,
            name=f"SSHConnector-{host_cfg.host}",
        )
        self._worker_thread.start()

    def _run_connection(self, host_cfg: HostConfig) -> None:
        self._set_state(ConnectionState.CONNECTING)

        host = host_cfg.host
        port = host_cfg.port
        username = host_cfg.username
        timeouts = (
            getattr(self.settings, "connect_timeout", host_cfg.timeout),
            getattr(self.settings, "auth_timeout", host_cfg.timeout),
            getattr(self.settings, "banner_timeout", host_cfg.timeout),
        )

        try:
            stored_password = self.secure_store.get(host, port, username)
        except SecureStoreError as exc:
            LOGGER.warning("SecureStore get failed: %s", exc)
            stored_password = None

        password: Optional[str] = stored_password
        remember_default = getattr(self.settings, "remember_password_default", True)
        remember_flag = True if stored_password is not None else remember_default
        last_error: Optional[str] = None

        while True:
            if password is None:
                def prompt() -> Tuple[Optional[str], bool]:
                    return self.password_dialog.show_for(
                        host_cfg,
                        error_message=last_error,
                    )

                pwd, remember = self._call_on_main_thread(prompt)
                if pwd is None:
                    self._set_state(ConnectionState.ABORTED)
                    self._cleanup()
                    self._emit_aborted("User cancelled authentication.")
                    return
                password = pwd
                remember_flag = remember
                last_error = None

            using_stored_password = stored_password is not None and password == stored_password

            self._set_state(ConnectionState.AUTHENTICATING)
            result = self._try_paramiko(host_cfg, password, timeouts)

            if result.ok:
                self._set_state(ConnectionState.AUTH_SUCCEEDED)
                try:
                    if remember_flag:
                        self.secure_store.set(host, port, username, password)
                    else:
                        self.secure_store.delete(host, port, username)
                except SecureStoreError as exc:
                    LOGGER.warning("SecureStore set/delete failed: %s", exc)
                    self._notify_warning(
                        "Login succeeded but password could not be persisted."
                    )

                self._zeroize(password)
                password = None

                self._current_client = result.client
                self._set_state(ConnectionState.CONNECTED)
                if result.client:
                    self._emit_success(result.client)
                return

            if result.is_auth_error:
                self._set_state(ConnectionState.AUTH_FAILED)
                last_error = "Authentication failed. Please enter your password again."

                if using_stored_password:
                    try:
                        self.secure_store.delete(host, port, username)
                    except SecureStoreError as exc:
                        LOGGER.warning("SecureStore delete failed: %s", exc)
                self._zeroize(password)
                password = None
                stored_password = None
                continue

            # non-auth errors
            self._notify_error(result.message)
            self._set_state(ConnectionState.ABORTED)
            self._zeroize(password)
            self._cleanup()
            self._emit_aborted(result.message)
            return

    # ------------------------------------------------------------------ paramiko
    def _try_paramiko(
        self,
        host_cfg: HostConfig,
        password: str,
        timeouts: Tuple[float, float, float],
    ) -> TryResult:
        host = host_cfg.host
        port = host_cfg.port
        username = host_cfg.username
        options = getattr(host_cfg, "options", {}) if hasattr(host_cfg, "options") else {}

        client = paramiko.SSHClient()
        client.load_system_host_keys()

        policy = _policy_from_settings(self.settings)
        if policy == "auto-add":
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        connect_kwargs: Dict[str, Any] = {}
        disabled_algorithms = _connection_algorithm_overrides(options, self.settings)
        if disabled_algorithms:
            connect_kwargs["disabled_algorithms"] = disabled_algorithms
        identity_file = _option_value(options, "identityfile")
        if identity_file:
            connect_kwargs["key_filename"] = os.path.expanduser(identity_file)
        if _option_bool(options, "compression", False):
            connect_kwargs["compress"] = True

        allow_agent = _option_bool(options, "allowagent", True)
        look_for_keys = _option_bool(options, "lookforkeys", True)
        if _option_bool(options, "identitiesonly", False):
            allow_agent = False
            if identity_file:
                look_for_keys = False

        proxy_command = _proxy_command_for(options, host, port, username)
        if proxy_command:
            try:
                connect_kwargs["sock"] = paramiko.ProxyCommand(proxy_command)
            except (paramiko.SSHException, OSError) as exc:
                client.close()
                return TryResult(
                    ok=False,
                    is_auth_error=False,
                    message=f"Failed to start proxy for {host}: {exc}",
                )

        try:
            connect_timeout, auth_timeout, banner_timeout = timeouts
            client.connect(
                host,
                port=port,
                username=username,
                password=password,
                timeout=connect_timeout,
                auth_timeout=auth_timeout,
                banner_timeout=banner_timeout,
                allow_agent=allow_agent,
                look_for_keys=look_for_keys,
                **connect_kwargs,
            )
            return TryResult(ok=True, is_auth_error=False, message="", client=client)

        except paramiko.AuthenticationException:
            client.close()
            return TryResult(
                ok=False,
                is_auth_error=True,
                message="Authentication failed.",
            )
        except paramiko.BadHostKeyException as exc:
            client.close()
            return TryResult(
                ok=False,
                is_auth_error=False,
                message=f"Host key mismatch for {host}:{port}: {exc}",
            )
        except socket.gaierror:
            client.close()
            return TryResult(
                ok=False,
                is_auth_error=False,
                message=f"DNS lookup failed for {host}",
            )
        except socket.timeout:
            client.close()
            return TryResult(
                ok=False,
                is_auth_error=False,
                message=f"Connection timed out to {host}:{port}",
            )
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            msg = str(exc)
            return TryResult(
                ok=False,
                is_auth_error=False,
                message=msg or "SSH connection failed.",
            )

    # ------------------------------------------------------------------- helpers
    def _set_state(self, state: ConnectionState) -> None:
        try:
            self.state_machine.transition_to(state)
        except ValueError as exc:
            LOGGER.warning("Invalid state transition ignored: %s", exc)

    def _cleanup(self) -> None:
        if self._current_client:
            try:
                self._current_client.close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._current_client = None

    def _zeroize(self, secret: Optional[str]) -> None:
        if not secret:
            return
        try:
            buf = bytearray(secret.encode("utf-8"))
            for idx in range(len(buf)):
                buf[idx] = 0
        except Exception:  # pragma: no cover - best effort
            pass

    def _notify_error(self, message: str) -> None:
        LOGGER.info("Connection error: %s", message)
        def notify() -> None:
            for cb in list(self._error_callbacks):
                cb(message)
        self._queue_main(notify)

    def _notify_warning(self, message: str) -> None:
        LOGGER.info("Connection warning: %s", message)
        def notify() -> None:
            for cb in list(self._warning_callbacks):
                cb(message)
        self._queue_main(notify)

    def _emit_success(self, client: paramiko.SSHClient) -> None:
        def notify() -> None:
            for cb in list(self._success_callbacks):
                cb(client)
        self._queue_main(notify)

    def _emit_aborted(self, reason: str) -> None:
        LOGGER.info("Connection aborted: %s", reason)
        def notify() -> None:
            for cb in list(self._aborted_callbacks):
                cb(reason)
        self._queue_main(notify)

    # ------------------------------------------------------------------- main call
    def _call_on_main_thread(self, fn: Callable[[], Tuple[Optional[str], bool]]) -> Tuple[Optional[str], bool]:
        return self._main_invoke(fn)

    def _default_main_invoke(self, fn: Callable[[], object]) -> object:
        if threading.current_thread() is self._main_thread:
            return fn()

        result_queue: "queue.Queue[Tuple[bool, object]]" = queue.Queue()

        def runner() -> bool:
            try:
                result_queue.put((True, fn()))
            except Exception as exc:  # pragma: no cover - should bubble to worker
                result_queue.put((False, exc))
            return False

        GLib.idle_add(runner)
        success, value = result_queue.get()
        if success:
            return value
        raise value  # type: ignore[misc]

    def _default_main_queue(
        self,
        callback: Callable[..., None],
        args: tuple = (),
        kwargs: Optional[dict] = None,
    ) -> None:
        if kwargs is None:
            kwargs = {}

        def runner() -> bool:
            callback(*args, **kwargs)
            return False

        GLib.idle_add(runner)

    def _queue_main(self, callback: Callable[..., None]) -> None:
        self._main_queue(callback, (), {})
