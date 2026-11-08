"""Verify packaged worker imports in isolation, without starting services."""
import argparse
from pathlib import Path
import subprocess
import sys


def check(worker, python):
    worker = Path(worker).resolve()
    # -I excludes the checkout, cwd, PYTHONPATH and user site packages: a
    # missing shipped module must not be rescued by the developer's machine.
    result = subprocess.run([str(python), "-I", "-B", "-c",
                             "import sys; sys.path.insert(0, sys.argv[1]); "
                             "import gateway, chat_runtime, chat_sessions, chat_host",
                             str(worker)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Packaged worker import check failed:\n" + result.stderr[-8192:])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker", type=Path)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    try:
        check(args.worker, args.python)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, str(exc) + "\n")
    print("Packaged worker imports passed")
