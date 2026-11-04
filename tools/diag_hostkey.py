#!/usr/bin/env python3
"""Host-key diagnostics for one host (standard library only; prints, changes nothing).

Usage::

    python3 tools/diag_hostkey.py HOST [-p PORT] [-l USER] [-F SSH_CONFIG]

It reports what decides whether ``ssh`` / ``sshpass -e ssh`` trust HOST's key on
this computer: the tool versions, ``$HOME``, the known_hosts files ssh will read
(size, entries, hashed or not), the system ``ssh_config`` options that matter,
the server's key fingerprint next to what known_hosts holds for it, and how (a)
``ssh -o BatchMode=yes HOST true`` and (b) ``sshpass -e ssh HOST true`` (with a
dummy password, never printed) end: exit status and the last 300 characters of
their output. Nothing is written: an unknown key is never accepted (BatchMode
refuses it, and sshpass stops at ssh's question).
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")
OPTIONS = ("stricthostkeychecking", "hashknownhosts", "verifyhostkeydns", "updatehostkeys", "userknownhostsfile",
           "globalknownhostsfile", "hostkeyalias", "hostkeyalgorithms")
DUMMY = "diag-dummy-" + os.urandom(6).hex()  # sent to sshpass in its environment only; never printed


def run(argv, timeout=20, env=None, stdin=subprocess.DEVNULL):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=stdin)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except FileNotFoundError:
        return None, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        return "timeout", out.decode("utf-8", "replace") if isinstance(out, bytes) else out


def tail(text: str, size: int = 300) -> str:
    text = ANSI.sub("", text or "").replace(DUMMY, "***")
    return text[-size:].strip()


def section(title: str) -> None:
    print(f"\n== {title}")


def effective(host: str, port: int, user: str, config: list) -> dict:
    argv = ["ssh", "-G"] + config + (["-p", str(port)] if port else []) + (["-l", user] if user else []) + [host]
    _code, out = run(argv, timeout=10)
    values: dict = {}
    for line in out.splitlines():
        key, _sep, value = line.partition(" ")
        if key in OPTIONS or key in ("hostname", "port", "user"):
            values.setdefault(key, value)
    return values


def known_hosts_files(values: dict) -> list:
    files = []
    for key in ("userknownhostsfile", "globalknownhostsfile"):
        for item in values.get(key, "").split():
            files.append(Path(os.path.expanduser(item)))
    return files


def describe_known_hosts(path: Path) -> str:
    try:
        lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"unreadable ({exc.strerror})"
    hashed = sum(1 for line in lines if line.split()[0].startswith("|1|"))
    return f"{path.stat().st_size} bytes, {len(lines)} entries, {hashed} hashed"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Host-key diagnostics (prints only)")
    parser.add_argument("host")
    parser.add_argument("-p", "--port", type=int, default=0)
    parser.add_argument("-l", "--user", default="")
    parser.add_argument("-F", "--config", default="", help="ssh_config file to use (like ssh -F)")
    args = parser.parse_args(argv)
    config = ["-F", args.config] if args.config else []

    section("tools")
    print("ssh:", run(["ssh", "-V"])[1].strip())
    sshpass = shutil.which("sshpass")
    print("sshpass:", sshpass or "not installed", "·", (run(["sshpass", "-V"])[1].splitlines() or [""])[0])
    print("HOME:", os.environ.get("HOME", "(unset)"))

    section("effective ssh options for " + args.host)
    values = effective(args.host, args.port, args.user, config)
    for key in ("hostname", "port", "user") + OPTIONS:
        if key in values:
            print(f"  {key} {values[key]}")

    section("known_hosts files ssh reads")
    for path in known_hosts_files(values):
        print(f"  {path}: {describe_known_hosts(path)}")

    section("system ssh_config")
    for name in ["/etc/ssh/ssh_config"] + sorted(glob.glob("/etc/ssh/ssh_config.d/*")):
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = [line.strip() for line in text.splitlines()
                if line.strip().lower().startswith(("stricthostkeychecking", "hashknownhosts", "verifyhostkeydns",
                                                    "updatehostkeys", "userknownhostsfile", "host ", "match "))]
        print(f"  {name}: " + ("; ".join(hits) if hits else "(nothing relevant)"))

    section("server key vs known_hosts")
    hostname = values.get("hostname", args.host)
    port = int(values.get("port", args.port or 22))
    _code, scan = run(["ssh-keyscan", "-T", "5", "-p", str(port), hostname], timeout=15)
    keys = [line for line in scan.splitlines() if line and not line.startswith("#")]
    if keys:
        listing = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(keys) + "\n", capture_output=True,
                                 text=True, timeout=10).stdout
        for line in listing.splitlines():
            print("  server offers:", line)
    else:
        print("  ssh-keyscan:", tail(scan) or "no key read")
    name = hostname if port == 22 else f"[{hostname}]:{port}"
    for path in known_hosts_files(values):
        if not path.exists():
            continue
        code, found = run(["ssh-keygen", "-F", name, "-f", str(path)], timeout=10)
        entries = [line for line in found.splitlines() if line and not line.startswith("#")]
        if entries:
            listing = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(entries) + "\n",
                                     capture_output=True, text=True, timeout=10).stdout
            print(f"  {path} holds {name}:", "; ".join(listing.split("\n")[:-1]) or "(entries)")
        else:
            print(f"  {path}: no entry for {name}")

    common = config + ["-o", "ConnectTimeout=5"] + (["-p", str(args.port)] if args.port else []) + \
        (["-l", args.user] if args.user else []) + [args.host, "true"]
    section("(a) ssh -o BatchMode=yes HOST true")
    code, out = run(["ssh", "-o", "BatchMode=yes"] + common, timeout=20)
    print(f"  exit status: {code}\n  output: {tail(out)!r}")

    section("(b) sshpass -e ssh HOST true (dummy password, never printed)")
    if sshpass:
        env = dict(os.environ, SSHPASS=DUMMY)
        code, out = run(["sshpass", "-e", "ssh"] + common, timeout=25, env=env)
        print(f"  exit status: {code}" + ("  (6 = sshpass: host public key is unknown)" if code == 6 else ""))
        print(f"  output: {tail(out)!r}")
    else:
        print("  skipped: sshpass is not installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
