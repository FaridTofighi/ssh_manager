"""SSH Manager application package."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ConfigManager": ("ophion.config", "ConfigManager"),
    "GroupConfig": ("ophion.config", "GroupConfig"),
    "HostEntry": ("ophion.config", "HostEntry"),
    "load_config_tree": ("ophion.config", "load_config_tree"),
    "parse_ssh_config_file": ("ophion.config", "parse_ssh_config_file"),
    "split_config_directive": ("ophion.config", "split_config_directive"),
    "ConnectionLauncher": ("ophion.launcher", "ConnectionLauncher"),
    "SecureStore": ("ophion.secure_store", "SecureStore"),
    "SecureStoreError": ("ophion.secure_store", "SecureStoreError"),
    "InMemoryKeyring": ("ophion.secure_store", "InMemoryKeyring"),
    "ConnectionState": ("ophion.connection_state", "ConnectionState"),
    "ConnectionStateMachine": ("ophion.connection_state", "ConnectionStateMachine"),
    "PasswordDialog": ("ophion.password_dialog", "PasswordDialog"),
    "Connector": ("ophion.connector", "Connector"),
    "TryResult": ("ophion.connector", "TryResult"),
    "AppSettings": ("ophion.settings", "AppSettings"),
    "CredentialStore": ("ophion.credentials", "CredentialStore"),
    "Credential": ("ophion.credentials", "Credential"),
    "build_ssh_command": ("ophion.ssh_command", "build_ssh_command"),
    "build_connection_option_args": ("ophion.ssh_command", "build_connection_option_args"),
    "build_scp_command": ("ophion.host_utils", "build_scp_command"),
    "host_config_key": ("ophion.host_utils", "host_config_key"),
    "host_connection_params": ("ophion.host_utils", "host_connection_params"),
    "parse_args": ("ophion.main", "parse_args"),
    "run_app": ("ophion.main", "run_app"),
    "main": ("ophion.main", "main"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
