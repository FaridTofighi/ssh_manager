"""User-customisable keyboard shortcuts: the defaults of :data:`ACTIONS` plus the user's changes.

:class:`ShortcutRegistry` is the single place that knows which accelerators an
action has. Settings store only the differences from the defaults
(``AppSettings.shortcuts``: ``{"<action-name>": ["<Primary><Shift>c", …]}``; an
empty list means "disabled by the user"). Unknown action names and
accelerators GTK cannot parse are ignored with a warning; one bad entry never
invalidates the others.

Accelerators are compared in GTK's canonical form (``Gtk.accelerator_name``),
so ``<Control>c`` and ``<Primary>c`` are the same shortcut. Listeners are told
after every change (the window re-applies the accelerators, refreshes menus and
the shortcuts window). GTK is used only to parse and name accelerators.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from .shortcuts import ACTIONS, ActionSpec  # noqa: E402

LOGGER = logging.getLogger(__name__)


def canonical(accel: str) -> str:
    """GTK's canonical name of ``accel``, or ``""`` when GTK cannot parse it."""

    if not isinstance(accel, str) or not accel.strip():
        return ""
    key, mods = Gtk.accelerator_parse(accel.strip())
    if not key:
        return ""
    return Gtk.accelerator_name(key, mods)


def is_customizable(spec: ActionSpec) -> bool:
    """A real window action whose accelerators the user may change (not a parameterised one)."""

    return not spec.parameter


class ShortcutRegistry:
    """Effective accelerators of every action: defaults, then the user's overrides."""

    def __init__(self, specs: Sequence[ActionSpec] = tuple(ACTIONS), overrides: Optional[Dict[str, object]] = None) -> None:
        self._specs: Dict[str, ActionSpec] = {spec.name: spec for spec in specs if is_customizable(spec)}
        self._defaults: Dict[str, Tuple[str, ...]] = {
            name: tuple(c for c in (canonical(a) for a in spec.accels) if c) for name, spec in self._specs.items()}
        self._overrides: Dict[str, Tuple[str, ...]] = {}
        self._listeners: List[Callable[[], None]] = []
        if overrides:
            self.from_dict(overrides, notify=False)

    # ------------------------------------------------------------- queries
    @property
    def names(self) -> List[str]:
        return list(self._specs)

    def spec(self, name: str) -> Optional[ActionSpec]:
        return self._specs.get(name)

    def defaults(self, name: str) -> Tuple[str, ...]:
        return self._defaults.get(name, ())

    def effective_accels(self, name: str) -> Tuple[str, ...]:
        if name in self._overrides:
            return self._overrides[name]
        return self._defaults.get(name, ())

    def is_default(self, name: str) -> bool:
        return name not in self._overrides

    def conflicts(self, accel: str, ignoring: Optional[str] = None) -> List[str]:
        """Actions (other than ``ignoring``) that already use ``accel``."""

        wanted = canonical(accel)
        if not wanted:
            return []
        return [name for name in self._specs if name != ignoring and wanted in self.effective_accels(name)]

    # ------------------------------------------------------------- changes
    def set_accels(self, name: str, accels: Iterable[str]) -> None:
        """Replace ``name``'s accelerators (``[]`` disables it); invalid entries are dropped."""

        if name not in self._specs:
            LOGGER.warning("shortcuts: unknown action %r ignored", name)
            return
        cleaned: List[str] = []
        for accel in accels:
            value = canonical(accel)
            if not value:
                LOGGER.warning("shortcuts: %r for %s is not a valid accelerator; ignored", accel, name)
            elif value not in cleaned:
                cleaned.append(value)
        if tuple(cleaned) == self._defaults[name]:
            self._overrides.pop(name, None)
        else:
            self._overrides[name] = tuple(cleaned)
        self._notify()

    def reset(self, name: str) -> None:
        if self._overrides.pop(name, None) is not None:
            self._notify()

    def reset_all(self) -> None:
        if self._overrides:
            self._overrides.clear()
            self._notify()

    # ---------------------------------------------------------- persistence
    def to_dict(self) -> Dict[str, List[str]]:
        """Only the differences from the defaults."""

        return {name: list(accels) for name, accels in self._overrides.items()}

    def from_dict(self, data: object, *, notify: bool = True) -> None:
        self._overrides.clear()
        if not isinstance(data, dict):
            if data is not None:
                LOGGER.warning("shortcuts: the settings entry is not a mapping; defaults are used")
        else:
            for name, accels in data.items():
                if name not in self._specs:
                    LOGGER.warning("shortcuts: unknown action %r in the settings ignored", name)
                    continue
                if isinstance(accels, str):
                    accels = [accels]
                if not isinstance(accels, list):
                    LOGGER.warning("shortcuts: the entry for %s is not a list; ignored", name)
                    continue
                valid = []
                for accel in accels:
                    value = canonical(accel) if isinstance(accel, str) else ""
                    if value:
                        if value not in valid:
                            valid.append(value)
                    else:
                        LOGGER.warning("shortcuts: %r for %s is not a valid accelerator; ignored", accel, name)
                if not valid and accels:
                    continue  # nothing usable: keep the defaults rather than disabling the action
                if tuple(valid) != self._defaults[name]:
                    self._overrides[name] = tuple(valid)
        if notify:
            self._notify()

    # ------------------------------------------------------------ listeners
    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # one failing view never stops the others
                LOGGER.exception("shortcuts: a listener failed")

    # -------------------------------------------------------------- applying
    def apply(self, application: Optional[Gtk.Application], available: Iterable[str]) -> None:
        """Set the effective accelerators of every ``available`` action on ``application`` (live)."""

        if application is None:
            return
        for name in available:
            spec = self._specs.get(name)
            if spec is not None:
                application.set_accels_for_action(spec.detailed_name, list(self.effective_accels(name)))


__all__ = ["ShortcutRegistry", "canonical", "is_customizable"]
