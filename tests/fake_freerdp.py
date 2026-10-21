"""A fake FreeRDP client for tests: records what it was given, then plays a scripted run.

:func:`install` writes an executable ``xfreerdp3`` (or any name) into a
directory. When started it appends one JSON record to ``$FAKE_RDP_RECORD``:
``argv``, ``env``, ``stdin`` (everything read until EOF) and its own
``/proc/self/cmdline`` — so tests can prove a password never reaches argv or
the environment. ``$FAKE_RDP_SCRIPT`` (JSON) scripts the run: ``lines``
(stdout), ``stderr``, ``gap`` (seconds between lines), ``sleep`` (after the
lines), ``child`` (start a long-lived child in the same process group and
record its pid) and ``exit`` (the exit code).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

BODY = r'''
import json, os, subprocess, sys, time
script = json.loads(os.environ.get("FAKE_RDP_SCRIPT") or "{}")
record_path = os.environ.get("FAKE_RDP_RECORD")
if len(sys.argv) > 1 and sys.argv[1] in ("/version", "--version"):
    print("This is FreeRDP version %s (n/a)" % script.get("version", "3.15.0"))
    sys.exit(0)
if len(sys.argv) > 1 and sys.argv[1] == "/help":
    sys.stdout.write(open(os.environ["FAKE_RDP_HELP"]).read())
    sys.exit(0)
data = b""
try:
    data = sys.stdin.buffer.read()
except Exception:
    pass
child = None
if script.get("child"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
if record_path:
    with open(record_path, "a") as handle:
        handle.write(json.dumps({"argv": sys.argv, "env": dict(os.environ), "stdin": data.decode("utf-8", "replace"),
                                 "cmdline": open("/proc/self/cmdline", "rb").read().decode("utf-8", "replace"),
                                 "pid": os.getpid(), "child": child.pid if child else 0}) + "\n")
for line in script.get("lines", []):
    print(line, flush=True)
    time.sleep(script.get("gap", 0))
for line in script.get("stderr", []):
    print(line, file=sys.stderr, flush=True)
time.sleep(script.get("sleep", 0))
sys.exit(script.get("exit", 0))
'''


def install(directory: Path, name: str = "xfreerdp3") -> Path:
    path = Path(directory) / name
    path.write_text(f"#!{sys.executable}\n" + BODY)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def records(path: Path) -> list:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def env_for(record: Path, **script) -> dict:
    return {"FAKE_RDP_RECORD": str(record), "FAKE_RDP_SCRIPT": json.dumps(script),
            "FAKE_RDP_HELP": str(Path(__file__).parent / "fixtures" / "freerdp" / "help-3.15.txt")}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie is not alive
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False
