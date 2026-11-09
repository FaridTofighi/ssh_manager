#!/usr/bin/env python3
"""The public (and private) surface of ``ophion/`` and the ``ssh_manager`` shim, from the source (``ast``).

Usage::

    tools/api_surface.py --out tests/refactor/api_surface.json   # the snapshot
    tools/api_surface.py --check tests/refactor/api_surface.json [--allow tests/refactor/api_allowlist.txt]

Per module: every top-level name (classes, functions with their signature, constants and names bound by
imports, since a moved symbol must stay importable from its old path). Per class: methods with their
signature and kind (``method`` / ``staticmethod`` / ``classmethod`` / ``property``), class attributes and the
``self.<name>`` attributes its methods assign. Underscore names are included: tests patch them.

``--check`` fails when a name of the snapshot is missing now, or a signature changed, unless the
allowlist names it (one ``module[:Class].name  # why`` per line).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("ophion", "ssh_manager")


def _signature(node: ast.AST) -> str:
    """A function's parameters as a stable string: names, defaults (as ``=``), ``*``/``**`` and kinds."""

    args = node.args  # type: ignore[attr-defined]
    parts: List[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for index, (arg, default) in enumerate(zip(positional, defaults)):
        parts.append(arg.arg + ("=" if default is not None else ""))
        if args.posonlyargs and index == len(args.posonlyargs) - 1:
            parts.append("/")
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(arg.arg + ("=" if default is not None else ""))
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return "(" + ", ".join(parts) + ")"


def _kind(node: ast.AST) -> str:
    for decorator in getattr(node, "decorator_list", []):
        name = decorator.id if isinstance(decorator, ast.Name) else getattr(decorator, "attr", "")
        if name in {"staticmethod", "classmethod", "property"}:
            return name
        if name in {"setter", "getter", "deleter"}:
            return "property"
    return "method"


def _targets(node: ast.AST) -> Iterable[str]:
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            yield from _targets(element)


def _bound_names(statements: Iterable[ast.stmt]) -> Dict[str, str]:
    """Names a block binds at its own level (also inside ``if``/``try``): ``{name: kind}``."""

    names: Dict[str, str] = {}
    for stmt in statements:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names[stmt.name] = "function" + _signature(stmt)
        elif isinstance(stmt, ast.ClassDef):
            names[stmt.name] = "class"
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for name in _targets(target):
                    names.setdefault(name, "value")
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)) and isinstance(stmt.target, ast.Name):
            names.setdefault(stmt.target.id, "value")
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                names.setdefault(alias.asname or alias.name.split(".")[0], "import")
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name != "*":
                    names.setdefault(alias.asname or alias.name, "import")
        elif isinstance(stmt, ast.If):
            for name, kind in {**_bound_names(stmt.orelse), **_bound_names(stmt.body)}.items():
                names.setdefault(name, kind)
        elif isinstance(stmt, ast.Try):
            blocks = list(stmt.body) + [s for h in stmt.handlers for s in h.body] + list(stmt.orelse) + list(stmt.finalbody)
            for name, kind in _bound_names(blocks).items():
                names.setdefault(name, kind)
        elif isinstance(stmt, ast.With):
            for name, kind in _bound_names(stmt.body).items():
                names.setdefault(name, kind)
    return names


def _class_surface(node: ast.ClassDef) -> Dict[str, str]:
    members: Dict[str, str] = {}
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = _kind(stmt)
            key = stmt.name
            if kind == "property":
                members[key] = "property"
            else:
                members[key] = f"{kind}{_signature(stmt)}"
        elif isinstance(stmt, ast.ClassDef):
            members[stmt.name] = "class"
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for name in _targets(target):
                    members.setdefault(name, "attribute")
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            members.setdefault(stmt.target.id, "attribute")
    instance: Set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for target in targets:
                for element in ([target] if not isinstance(target, (ast.Tuple, ast.List)) else target.elts):
                    if (isinstance(element, ast.Attribute) and isinstance(element.value, ast.Name)
                            and element.value.id == "self"):
                        instance.add(element.attr)
    for name in sorted(instance):
        members.setdefault(name, "instance")
    return dict(sorted(members.items()))


def module_files(root: Path = ROOT) -> Dict[str, Path]:
    found: Dict[str, Path] = {}
    for package in PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            parts = list(path.relative_to(root).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            found[".".join(parts)] = path
    return found


def surface(root: Path = ROOT) -> dict:
    result: Dict[str, dict] = {}
    for name, path in module_files(root).items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        classes = {node.name: _class_surface(node) for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef) and _is_top_level(tree, node)}
        result[name] = {"names": dict(sorted(_bound_names(tree.body).items())), "classes": dict(sorted(classes.items()))}
    return dict(sorted(result.items()))


def _is_top_level(tree: ast.Module, node: ast.ClassDef) -> bool:
    """A class defined at module level, also inside ``if``/``try`` blocks there."""

    def walk(statements):
        for stmt in statements:
            if stmt is node:
                return True
            if isinstance(stmt, ast.If) and (walk(stmt.body) or walk(stmt.orelse)):
                return True
            if isinstance(stmt, ast.Try) and (walk(stmt.body) or walk(stmt.orelse) or walk(stmt.finalbody)
                                              or any(walk(h.body) for h in stmt.handlers)):
                return True
        return False

    return walk(tree.body)


def load_allowlist(path: Path) -> Set[str]:
    allowed: Set[str] = set()
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                allowed.add(entry)
    return allowed


def compare(baseline: dict, current: dict, allowed: Set[str]) -> List[str]:
    """Names of the baseline that are missing or changed now (and are not allowlisted)."""

    problems: List[str] = []
    for module, before in baseline.items():
        now = current.get(module)
        if now is None:
            if module not in allowed:
                problems.append(f"module {module} is gone")
            continue
        for name, kind in before["names"].items():
            key = f"{module}.{name}"
            if key in allowed:
                continue
            if name not in now["names"]:
                problems.append(f"{key} is gone")
            elif kind.startswith("function") and now["names"][name] != kind:
                problems.append(f"{key} changed: {kind} -> {now['names'][name]}")
        for cls, members in before["classes"].items():
            now_members = now["classes"].get(cls)
            if now_members is None:
                if f"{module}:{cls}" not in allowed and f"{module}.{cls}" not in allowed:
                    problems.append(f"class {module}:{cls} is gone")
                continue
            for name, kind in members.items():
                key = f"{module}:{cls}.{name}"
                if key in allowed:
                    continue
                if name not in now_members:
                    problems.append(f"{key} is gone")
                elif kind != "instance" and now_members[name] != kind and now_members[name] != "instance":
                    problems.append(f"{key} changed: {kind} -> {now_members[name]}")
    return problems


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", type=Path, metavar="BASELINE")
    parser.add_argument("--allow", type=Path, default=ROOT / "tests" / "refactor" / "api_allowlist.txt")
    args = parser.parse_args(argv)
    current = surface()
    if args.check:
        problems = compare(json.loads(args.check.read_text(encoding="utf-8")), current, load_allowlist(args.allow))
        for problem in problems:
            print(f"api_surface: {problem}")
        print(f"api_surface: {'FAILED' if problems else 'ok'} ({len(problems)} missing or changed)")
        return 1 if problems else 0
    text = json.dumps(current, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
