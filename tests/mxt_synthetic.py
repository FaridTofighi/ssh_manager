"""Synthetic MobaXterm ``.mxtsessions`` exports for tests (no real data, ever).

``session_value()`` builds one session value; ``ACME`` is a small hand-written
export covering every shape the importer handles; ``make_export()`` generates
large exports (e.g. 1,500 sessions, 800 folders, depth 12) for performance tests.

Run as a script to write a large export: ``python3 -m tests.mxt_synthetic out.mxtsessions``.
"""

from __future__ import annotations

import random
import sys
from typing import List, Optional

# A real export has ~50–64 '%' fields after the type; most are terminal settings.
_TAIL = ["-1", "-1", "", "", "", "", "0", "0", "0", "", "", "-1", "0", "0", "0", "", "1080", "", "0", "0", "1"]


def session_value(host: str, port: str = "22", user: str = "", *, gateway: str = "", gateway_port: str = "",
                  gateway_user: str = "", type_code: int = 0, icon: int = 109, extra_fields: Optional[List[str]] = None,
                  pad: int = 49) -> str:
    fields = [host, port, user, "", "-1", "-1", "", gateway, gateway_port, gateway_user]
    fields += extra_fields if extra_fields is not None else _TAIL
    while len(fields) < pad:
        fields.append("")
    return f"#{icon}#{type_code}%" + "%".join(fields) + "#MobaFont%10%0%0%-1%15%236,236,236%30,30,30"


ACME = "\r\n".join([
    "[Bookmarks]",
    "SubRep=Acme",
    "ImgNum=41",
    "acme bastion=" + session_value("10.0.0.1", "22", "[alice]"),
    "",
    "[Bookmarks_1]",
    "SubRep=Acme\\Team A",
    "ImgNum=220",
    "team-a-gw=" + session_value("10.0.0.2", "22", "alice"),
    "",
    "[Bookmarks_2]",
    "SubRep=Acme\\Team A\\Servers",
    "ImgNum=41",
    "web 01=" + session_value("10.0.0.10", "22", "[alice]"),
    "db (primary)= " + session_value("10.0.0.11", "22", "[alice@corp.local]"),
    "backup=" + session_value("10.0.0.12", "2222", "svc_backup"),
    "behind-jump=" + session_value("10.0.0.13", "22", "bob", gateway="10.0.0.254", gateway_port="22",
                                   gateway_user="alice"),
    "no user=" + session_value("10.0.0.14", "22", ""),
    "windows box=" + session_value("10.0.0.20", "3389", "dev\\alice", type_code=4, icon=91),
    "files=" + session_value("10.0.0.21", "21", "bob", type_code=6, icon=130),
    "",
    "[Bookmarks_3]",
    "SubRep=Acme\\+ Empty container (old)",
    "ImgNum=41",
    "",
    "[Bookmarks_4]",
    "SubRep=Acme\\Team B\\00 - deep\\level 4",
    "ImgNum=41",
    "deep host=" + session_value("10.0.0.30", "22", "[bob]"),
    "",
    "[Bookmarks_5]",
    "SubRep=Beta",
    "ImgNum=41",
    "سرور اصلی=" + session_value("10.0.1.1", "22", "Alice"),
    "",
]) + "\r\n"


def make_export(sessions: int = 1500, folders: int = 800, depth: int = 12, seed: int = 7) -> bytes:
    """A large export: ``folders`` nested up to ``depth``, ``sessions`` spread over them (some RDP/FTP)."""

    rng = random.Random(seed)
    users = ["[alice]", "alice", "[alice@corp.local]", "[bob]", "bob", "svc_backup", ""]
    paths = [["Acme"]]
    while len(paths) < folders:
        parent = rng.choice([p for p in paths if len(p) < depth])
        paths.append(parent + [f"{rng.choice(['team', 'pod', '00 - zone', '+ ops'])} {len(paths)}"])
    lines = []
    per_folder = {index: [] for index in range(len(paths))}
    for number in range(sessions):
        per_folder[rng.randrange(len(paths))].append(number)
    for index, path in enumerate(paths):
        lines.append("[Bookmarks]" if index == 0 else f"[Bookmarks_{index}]")
        lines.append("SubRep=" + "\\".join(path))
        lines.append("ImgNum=41")
        for number in per_folder[index]:
            type_code = 4 if number % 61 == 0 else 6 if number % 197 == 0 else 0
            host = f"10.{number // 65536 % 256}.{number // 256 % 256}.{number % 256}"
            gateway = "10.255.0.1" if number % 150 == 0 else ""
            lines.append(f"host {number}=" + session_value(host, "22", rng.choice(users), gateway=gateway,
                                                           gateway_port="22" if gateway else "1",
                                                           type_code=type_code))
        lines.append("")
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


if __name__ == "__main__":  # pragma: no cover - helper
    target = sys.argv[1] if len(sys.argv) > 1 else "synthetic.mxtsessions"
    with open(target, "wb") as handle:
        handle.write(make_export())
    print(f"wrote {target}")
