"""Decorators shared by the action controllers."""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def requires_editable_inventory(method: F) -> F:
    """Run ``method`` only when the inventory can be changed.

    When the inventory is a single ssh config *file*, the window offers to
    switch to a folder (``SSHManagerWindow.ensure_editable_inventory``) instead
    of letting the action fail; a refusal makes the action a no-op (``False``
    for drop handlers, ``None`` otherwise).
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        guard = getattr(self.win, "ensure_editable_inventory", None)
        if guard is not None and not guard():
            return False if method.__name__.startswith("_drop") else None
        return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
