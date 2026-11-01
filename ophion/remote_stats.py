"""The remote CPU / memory / disk probe of the terminal status bar (no GTK).

A small Python script is sent to the server on stdin (``python3 -``) over a
second SSH connection; it prints one JSON object.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional, Tuple

_REMOTE_STATS_SCRIPT = """
import functools
import json
import functools
import os
import time

def cpu_percent():
    with open('/proc/stat', 'r') as f:
        parts = f.readline().split()
    idle_a = int(parts[4])
    total_a = sum(int(x) for x in parts[1:])
    time.sleep(0.2)
    with open('/proc/stat', 'r') as f:
        parts = f.readline().split()
    idle_b = int(parts[4])
    total_b = sum(int(x) for x in parts[1:])
    idle_delta = idle_b - idle_a
    total_delta = total_b - total_a
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))

def mem_usage():
    info = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            key, val, *_ = line.split()
            info[key.rstrip(':')] = int(val)
    total = info.get('MemTotal', 0)
    available = info.get('MemAvailable', info.get('MemFree', 0))
    used = total - available
    return used / 1024.0, total / 1024.0

def disk_usage():
    st = os.statvfs('/')
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    gb = 1024.0 ** 3
    return used / gb, total / gb

cpu = cpu_percent()
mem_used, mem_total = mem_usage()
disk_used, disk_total = disk_usage()
print(json.dumps({
    'cpu': cpu,
    'mem_used': mem_used,
    'mem_total': mem_total,
    'disk_used': disk_used,
    'disk_total': disk_total
}))
"""


def parse_remote_stats_output(output: str) -> Optional[Dict[str, object]]:
    """Parse the JSON payload produced by the remote stats script."""

    text = output.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_remote_stats_command(
    ssh_args: list[str],
    *,
    password: Optional[str] = None,
    sshpass_path: Optional[str] = None,
) -> Tuple[list[str], Optional[Dict[str, str]]]:
    """Build the auxiliary stats SSH command and optional environment."""

    command = list(ssh_args)
    if not command:
        return [], None

    if password and sshpass_path:
        command[1:1] = ["-o", "ConnectTimeout=6"]
        command.extend(["python3", "-"])
        env = os.environ.copy()
        env["SSHPASS"] = password
        return [sshpass_path, "-e", *command], env

    command[1:1] = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    command.extend(["python3", "-"])
    return command, None
