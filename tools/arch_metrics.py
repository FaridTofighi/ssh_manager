#!/usr/bin/env python3
"""Architecture metrics of ``ophion/`` from the source text alone (``ast``; nothing is imported, no GTK).

Usage::

    tools/arch_metrics.py                       # JSON on stdout
    tools/arch_metrics.py --out FILE            # write it
    tools/arch_metrics.py --check BASELINE      # ratchets against a baseline (exit 1 on a regression)

What is measured (the refactor's baseline, ``tests/refactor/metrics_baseline.json``):

* ``loc``: physical lines per module; per class its lines and method count; functions over 100 lines.
* ``imports``: the import graph of ``ophion.*`` modules with three edge kinds: ``module`` (runtime, at module
  level), ``function`` (runtime, inside a function or method) and ``type_checking`` (under
  ``if TYPE_CHECKING:``). Runtime cycles (strongly connected components) use the first two kinds only;
  ``type_checking`` edges never run.
* ``core_gtk``: modules outside ``ophion/gtkui`` that import ``gi`` or ``ophion.gtkui`` at module level
  (``core_gtk_any_level``: at any level, inside functions too).
* ``counts``: ``except Exception`` handlers and those whose body is only ``pass``; modules using
  ``threading.Thread``; ``Thread`` / ``idle_add`` / ``timeout_add`` uses; ``win._<name>`` reads in
  ``gtkui/controllers`` per name; ``getattr(<…manager>, "store", None)`` calls.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "ophion"
LONG_FUNCTION = 100
BIG_CLASS = 600


# ----------------------------------------------------------------- modules
def module_files(root: Path = ROOT) -> Dict[str, Path]:
    """``{dotted name: path}`` of every module of the package (``__init__`` is the package itself)."""

    found: Dict[str, Path] = {}
    for path in sorted((root / PACKAGE).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def _resolve(module: str, is_package: bool, level: int, target: Optional[str]) -> str:
    """The absolute name of a relative import (``from ..x import y`` in ``module``)."""

    if level == 0:
        return target or ""
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([target] if target else []))


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _imports(tree: ast.Module, module: str, is_package: bool, known: Set[str]) -> List[Tuple[str, str]]:
    """``[(target module, kind), …]`` with kind ``module`` / ``function`` / ``type_checking``."""

    edges: List[Tuple[str, str]] = []

    def targets(node: ast.AST) -> Iterable[str]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(module, is_package, node.level, node.module)
            for alias in node.names:
                full = f"{base}.{alias.name}" if base else alias.name
                yield full if full in known else base  # "from pkg import submodule" is an edge to it

    def visit(node: ast.AST, kind: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for target in targets(child):
                    edges.append((target, kind))
            elif isinstance(child, ast.If) and _is_type_checking(child.test):
                for stmt in child.body:
                    visit(ast.Module(body=[stmt], type_ignores=[]), "type_checking")
                for stmt in child.orelse:
                    visit(ast.Module(body=[stmt], type_ignores=[]), kind)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(child, "type_checking" if kind == "type_checking" else "function")
            else:
                visit(child, kind)

    visit(tree, "module")
    return edges


def _owner(name: str, known: Set[str]) -> Optional[str]:
    """The package module an imported name belongs to (``ophion.a.b.C`` → ``ophion.a.b``)."""

    while name:
        if name in known:
            return name
        name = name.rpartition(".")[0]
    return None


def _sccs(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Strongly connected components with more than one module (Tarjan, iterative)."""

    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    result: List[List[str]] = []
    counter = 0
    for start in sorted(graph):
        if start in index:
            continue
        work = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
    return sorted(result)


# ------------------------------------------------------------------- counts
def _is_exception_handler(handler: ast.ExceptHandler) -> bool:
    kind = handler.type
    return isinstance(kind, ast.Name) and kind.id == "Exception"


def _thread_uses(tree: ast.Module) -> Tuple[bool, int]:
    """``(uses threading.Thread, number of Thread / idle_add / timeout_add references)``."""

    uses_thread = False
    count = 0
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        if name in {"Thread", "idle_add", "timeout_add", "timeout_add_seconds"}:
            count += 1
            if name == "Thread":
                uses_thread = True
    return uses_thread, count


def _win_private(tree: ast.Module) -> Counter:
    """``win._name`` / ``self.win._name`` reads: the window's private API used by a controller."""

    found: Counter = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_") or node.attr.startswith("__"):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id == "win":
            found[node.attr] += 1
        elif isinstance(value, ast.Attribute) and value.attr == "win":
            found[node.attr] += 1
    return found


def _store_getattr(tree: ast.Module) -> int:
    count = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "store"):
            subject = node.args[0]
            text = ast.unparse(subject)
            if text.endswith("manager"):
                count += 1
    return count


# ------------------------------------------------------------------ measure
def measure(root: Path = ROOT) -> dict:
    files = module_files(root)
    known = set(files)
    loc: Dict[str, int] = {}
    classes: Dict[str, dict] = {}
    long_functions: List[dict] = []
    edges: Dict[str, Dict[str, str]] = defaultdict(dict)  # module -> {target: strongest kind}
    core_gtk: Dict[str, List[str]] = {}
    core_gtk_any: Set[str] = set()
    except_total = except_pass = thread_refs = 0
    thread_modules: List[str] = []
    win_private: Counter = Counter()
    store_getattr = 0
    strength = {"type_checking": 0, "function": 1, "module": 2}

    for name, path in files.items():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        loc[name] = source.count("\n") + (0 if source.endswith("\n") or not source else 1)
        is_package = path.name == "__init__.py"
        for target, kind in _imports(tree, name, is_package, known):
            owner = _owner(target, known)
            if owner and owner != name:
                previous = edges[name].get(owner)
                if previous is None or strength[kind] > strength[previous]:
                    edges[name][owner] = kind
            gtk = target == "gi" or target.startswith("gi.") or target.startswith(f"{PACKAGE}.gtkui")
            if gtk and not name.startswith(f"{PACKAGE}.gtkui"):
                core_gtk_any.add(name)
                if kind == "module":
                    core_gtk.setdefault(name, [])
                    if target not in core_gtk[name]:
                        core_gtk[name].append(target)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                classes[f"{name}.{node.name}"] = {"lines": node.end_lineno - node.lineno + 1, "methods": len(methods)}
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = node.end_lineno - node.lineno + 1
                if length > LONG_FUNCTION:
                    long_functions.append({"module": name, "function": node.name, "line": node.lineno,
                                           "lines": length})
            elif isinstance(node, ast.ExceptHandler) and _is_exception_handler(node):
                except_total += 1
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    except_pass += 1
        uses_thread, refs = _thread_uses(tree)
        thread_refs += refs
        if uses_thread:
            thread_modules.append(name)
        if name.startswith(f"{PACKAGE}.gtkui.controllers"):
            win_private.update(_win_private(tree))
        store_getattr += _store_getattr(tree)

    runtime = {module: {t for t, kind in targets.items() if kind != "type_checking"}
               for module, targets in edges.items()}
    module_level = {module: {t for t, kind in targets.items() if kind == "module"} for module, targets in edges.items()}
    graph = {module: {target: kind for target, kind in sorted(targets.items())} for module, targets in sorted(edges.items())}
    return {
        "modules": len(files),
        "loc_total": sum(loc.values()),
        "loc": dict(sorted(loc.items())),
        "classes": dict(sorted(classes.items())),
        "long_functions": sorted(long_functions, key=lambda f: (f["module"], f["line"])),
        "imports": graph,
        "runtime_sccs": _sccs(runtime),
        "module_level_sccs": _sccs(module_level),
        "core_gtk": dict(sorted(core_gtk.items())),
        "core_gtk_any_level": sorted(core_gtk_any),
        "counts": {
            "except_exception": except_total,
            "except_exception_pass": except_pass,
            "thread_modules": len(thread_modules),
            "thread_idle_timeout_uses": thread_refs,
            "win_private": dict(win_private.most_common()),
            "win_private_total": sum(win_private.values()),
            "store_getattr": store_getattr,
        },
        "thread_modules": sorted(thread_modules),
    }


# ------------------------------------------------------------------ ratchet
def check(current: dict, baseline: dict) -> List[str]:
    """The refactor's ratchets (§2.7 (4)); every returned line is a regression."""

    problems: List[str] = []
    if len(current["runtime_sccs"]) > len(baseline["runtime_sccs"]):
        problems.append(f"runtime import cycles: {len(current['runtime_sccs'])} > {len(baseline['runtime_sccs'])}")
    biggest = max((len(c) for c in current["runtime_sccs"]), default=0)
    if biggest > max((len(c) for c in baseline["runtime_sccs"]), default=0):
        problems.append(f"a runtime import cycle grew to {biggest} modules")
    if current["module_level_sccs"]:
        problems.append(f"module-level import cycles: {current['module_level_sccs']}")
    if len(current["core_gtk"]) > len(baseline["core_gtk"]):
        added = sorted(set(current["core_gtk"]) - set(baseline["core_gtk"]))
        problems.append(f"core modules importing GTK: {len(current['core_gtk'])} > {len(baseline['core_gtk'])} {added}")
    if current["counts"]["except_exception_pass"] > baseline["counts"]["except_exception_pass"]:
        problems.append(f"'except Exception: pass': {current['counts']['except_exception_pass']} > "
                        f"{baseline['counts']['except_exception_pass']}")
    for name, now in current["classes"].items():
        before = baseline["classes"].get(name)
        if before is not None and now["lines"] > before["lines"]:
            problems.append(f"class {name} grew: {now['lines']} > {before['lines']} lines")
        elif before is None and now["lines"] > BIG_CLASS:
            problems.append(f"new class {name} has {now['lines']} lines (> {BIG_CLASS})")
    for attr, now in current["counts"]["win_private"].items():
        before = baseline["counts"]["win_private"].get(attr, 0)
        if now > before:
            problems.append(f"win.{attr} in controllers: {now} > {before}")
    return problems


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", type=Path, metavar="BASELINE")
    args = parser.parse_args(argv)
    current = measure()
    if args.check:
        problems = check(current, json.loads(args.check.read_text(encoding="utf-8")))
        for problem in problems:
            print(f"arch_metrics: {problem}")
        print(f"arch_metrics: {'FAILED' if problems else 'ok'} ({len(problems)} regressions)")
        return 1 if problems else 0
    text = json.dumps(current, indent=1, sort_keys=False, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
