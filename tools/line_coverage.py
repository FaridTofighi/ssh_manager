"""Line coverage with the standard library only (``sys.monitoring``, Python 3.12+): a pytest plugin.

``coverage`` is not installed and the refactor adds no dependency, so this records which lines of the
watched files ran (each location reports once, then is disabled: little overhead, threads included)::

    OPHION_COVERAGE_OUT=cov.json tools/run_gtk_tests.py -- env PYTHONPATH=tools python3 -m pytest -p line_coverage tests

The JSON has, per watched file, the executed and executable line counts and per function (qualified
name) ``[executed, executable]``. Executable lines are the line numbers of the compiled code objects.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import types
from pathlib import Path
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parents[1]
WATCHED = ("ophion/gtk_terminal.py", "ophion/gtk_app.py", "ophion/gtkui/controllers/", "ophion/inventory/")
TOOL = getattr(getattr(sys, "monitoring", None), "COVERAGE_ID", 1)

_executed: Dict[str, Set[int]] = {}


def watched_files(root: Path = ROOT) -> List[Path]:
    files: List[Path] = []
    for entry in WATCHED:
        path = root / entry
        files.extend(sorted(path.rglob("*.py")) if entry.endswith("/") else [path])
    return [f for f in files if "__pycache__" not in f.parts]


def _on_line(code: types.CodeType, line: int):
    lines = _executed.get(code.co_filename)
    if lines is not None:
        lines.add(line)
    return sys.monitoring.DISABLE


def start(files: List[Path]) -> None:
    for path in files:
        _executed[str(path)] = set()
    monitoring = sys.monitoring
    monitoring.use_tool_id(TOOL, "ophion-line-coverage")
    monitoring.register_callback(TOOL, monitoring.events.LINE, _on_line)
    monitoring.set_events(TOOL, monitoring.events.LINE)


def _code_lines(code: types.CodeType) -> Set[int]:
    lines = {line for _start, _end, line in code.co_lines() if line is not None}
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            lines |= _code_lines(const)
    return lines


def report(root: Path = ROOT) -> dict:
    result = {}
    for filename, executed in sorted(_executed.items()):
        path = Path(filename)
        source = path.read_text(encoding="utf-8")
        executable = _code_lines(compile(source, filename, "exec"))
        executable -= {node.lineno for node in ast.walk(ast.parse(source))
                       if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)
                       and isinstance(node.value.value, str)}  # docstrings
        functions = {}

        def visit(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = f"{prefix}{child.name}"
                    if not isinstance(child, ast.ClassDef):
                        span = {line for line in executable if child.lineno <= line <= child.end_lineno}
                        functions[name] = [len(span & executed), len(span)]
                    visit(child, name + ".")

        visit(ast.parse(source))
        result[str(path.relative_to(root))] = {"executed": len(executed & executable), "executable": len(executable),
                                              "functions": functions}
    return result


# ------------------------------------------------------------- pytest hooks
def pytest_configure(config) -> None:
    start(watched_files())


def pytest_unconfigure(config) -> None:
    sys.monitoring.set_events(TOOL, 0)
    out = os.environ.get("OPHION_COVERAGE_OUT")
    data = report()
    if out:
        Path(out).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    total = sum(f["executed"] for f in data.values()), sum(f["executable"] for f in data.values())
    sys.stderr.write(f"\nline_coverage: {total[0]}/{total[1]} lines of {len(data)} watched files\n")
