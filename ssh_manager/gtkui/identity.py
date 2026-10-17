"""Local user identity shown in the top-bar avatar."""

from __future__ import annotations

import getpass
import os


def user_initials(full_name: str = "", username: str = "") -> str:
    """Up to two initials: first and last word of the full name, else the login."""

    words = [w for w in (full_name or "").replace(",", " ").split() if w[:1].isalnum()]
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    if words:
        return words[0][0].upper()
    login = (username or "").strip()
    return login[0].upper() if login and login[0].isalnum() else "U"


def local_user_initials() -> str:
    full_name, login = "", ""
    try:
        import pwd

        entry = pwd.getpwuid(os.getuid())
        full_name, login = entry.pw_gecos.split(",")[0], entry.pw_name
    except (ImportError, KeyError, OSError):
        pass
    if not login:
        try:
            login = getpass.getuser()
        except Exception:
            login = ""
    return user_initials(full_name, login)
