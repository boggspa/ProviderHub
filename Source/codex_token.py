#!/usr/bin/env python3
"""Command-backed authentication helper. Output goes only to the Codex client."""
import os
from pathlib import Path
import re
import sys


def main():
    if len(sys.argv) != 2:
        raise ValueError("Expected the Provider Hub state directory.")
    root = Path(sys.argv[1])
    path = root / "gateway-token"
    if not root.is_absolute() or root.is_symlink() or path.is_symlink():
        raise ValueError("Invalid Provider Hub credential location.")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Provider Hub credential permissions are invalid.")
    value = path.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
        raise ValueError("Provider Hub credential is invalid.")
    print(value)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Provider Hub local authentication is unavailable. Open Provider Hub and start its gateway.", file=sys.stderr)
        sys.exit(1)
