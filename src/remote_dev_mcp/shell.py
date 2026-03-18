from __future__ import annotations

import posixpath
import shlex


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def shell_join(argv: list[str]) -> str:
    return " ".join(shell_quote(part) for part in argv)


def normalize_posix_path(path: str) -> str:
    if path == "":
        return "."
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." and path.startswith("/") else normalized


def is_within_roots(path: str, roots: list[str]) -> bool:
    normalized_path = normalize_posix_path(path)
    for root in roots:
        normalized_root = normalize_posix_path(root)
        if normalized_path == normalized_root:
            return True
        if normalized_path.startswith(normalized_root.rstrip("/") + "/"):
            return True
    return False

