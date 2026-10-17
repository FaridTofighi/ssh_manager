"""SSH Manager application package."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ConfigManager": ("ssh_manager.config", "ConfigManager"),
    "GroupConfig": ("ssh_manager.config", "GroupConfig"),
    "HostEntry": ("ssh_manager.config", "HostEntry"),
    "load_config_tree": ("ssh_manager.config", "load_config_tree"),
    "parse_ssh_config_file": ("ssh_manager.config", "parse_ssh_config_file"),
    "split_config_directive": ("ssh_manager.config", "split_config_directive"),
    "ConnectionLauncher": ("ssh_manager.launcher", "ConnectionLauncher"),
    "SecureStore": ("ssh_manager.secure_store", "SecureStore"),
    "SecureStoreError": ("ssh_manager.secure_store", "SecureStoreError"),
    "InMemoryKeyring": ("ssh_manager.secure_store", "InMemoryKeyring"),
    "ConnectionState": ("ssh_manager.connection_state", "ConnectionState"),
    "ConnectionStateMachine": ("ssh_manager.connection_state", "ConnectionStateMachine"),
    "PasswordDialog": ("ssh_manager.password_dialog", "PasswordDialog"),
    "Connector": ("ssh_manager.connector", "Connector"),
    "TryResult": ("ssh_manager.connector", "TryResult"),
    "AppSettings": ("ssh_manager.settings", "AppSettings"),
    "CredentialStore": ("ssh_manager.credentials", "CredentialStore"),
    "Credential": ("ssh_manager.credentials", "Credential"),
    "build_ssh_command": ("ssh_manager.ssh_command", "build_ssh_command"),
    "build_connection_option_args": ("ssh_manager.ssh_command", "build_connection_option_args"),
    "build_scp_command": ("ssh_manager.host_utils", "build_scp_command"),
    "host_config_key": ("ssh_manager.host_utils", "host_config_key"),
    "host_connection_params": ("ssh_manager.host_utils", "host_connection_params"),
    "parse_args": ("ssh_manager.main", "parse_args"),
    "run_app": ("ssh_manager.main", "run_app"),
    "main": ("ssh_manager.main", "main"),
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
