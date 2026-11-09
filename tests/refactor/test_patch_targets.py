"""R6: every string patch target in the tests still resolves (import + attribute lookup).

``patch("ophion.gtk_terminal.TerminalTab._preflight_then_start")`` and friends must keep pointing at
something when code moves (shims keep old paths alive). Whether a patch still *intercepts* the call is
the mutation check of docs/refactor/patch-checks.md, not this test.

Targets patched with ``create=True`` need not exist (the patch creates them); they are listed in
``CREATED`` (the baseline's), so a new one is noticed.
"""

import ast
import importlib
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

# create=True targets at the baseline (docs/refactor/found-bugs.md: this one intercepts nothing).
CREATED = {"ophion.gtkui.controllers.group_actions.prompt_text"}


def patch_targets() -> dict:
    """``{target: {"places": [file:line, …], "create": bool}}`` for every ``patch(…)`` / ``setattr(…)``
    call under ``tests/`` whose first argument is a string naming something in ``ophion``."""

    found: dict = {}
    for path in sorted(TESTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if name not in {"patch", "setattr"} or not isinstance(first, ast.Constant):
                continue
            if not isinstance(first.value, str) or not first.value.startswith("ophion."):
                continue
            create = any(k.arg == "create" and isinstance(k.value, ast.Constant) and k.value.value is True
                         for k in node.keywords)
            entry = found.setdefault(first.value, {"places": [], "create": False})
            entry["places"].append(f"{path.relative_to(TESTS.parent)}:{node.lineno}")
            entry["create"] = entry["create"] or create
    return found


def resolve(target: str):
    """The object ``target`` names: the longest importable module prefix, then attributes."""

    parts = target.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for attribute in parts[cut:]:
            obj = getattr(obj, attribute)
        return obj
    raise ImportError(target)


class PatchTargetTests(unittest.TestCase):
    def test_every_string_patch_target_resolves(self) -> None:
        targets = patch_targets()
        self.assertGreater(sum(len(t["places"]) for t in targets.values()), 150)  # the scan works (183 uses)
        broken = []
        for target, info in sorted(targets.items()):
            if info["create"]:
                continue
            try:
                resolve(target)
            except (ImportError, AttributeError) as exc:
                broken.append(f"{target} ({info['places'][0]}): {type(exc).__name__}: {exc}")
        self.assertEqual(broken, [], "\n".join(broken))

    def test_created_targets_are_the_baselines(self) -> None:
        created = {target for target, info in patch_targets().items() if info["create"]}
        self.assertEqual(created, CREATED)

    def test_a_missing_target_is_reported(self) -> None:
        with self.assertRaises(AttributeError):
            resolve("ophion.gtk_terminal.TerminalTab._no_such_method")


if __name__ == "__main__":
    unittest.main()
