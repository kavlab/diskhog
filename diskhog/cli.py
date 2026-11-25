"""Command-line entry point for diskhog."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .app import DiskHogApp


def _normalize(path: str) -> str:
    # On Windows a bare "D:" means "current directory on drive D:",
    # but users almost always mean the drive root.
    if os.name == "nt" and len(path) == 2 and path[1] == ":":
        path += "\\"
    return os.path.abspath(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diskhog",
        description=(
            "Scan a disk or directory and explore an interactive tree of "
            "sizes to find and delete space hogs."
        ),
    )
    parser.add_argument(
        "path", nargs="?", default=".",
        help="directory or drive to scan (default: current directory)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args(argv)

    root = _normalize(args.path)
    if not os.path.exists(root):
        print(f"diskhog: path does not exist: {root}", file=sys.stderr)
        return 2
    if not os.path.isdir(root):
        print(f"diskhog: not a directory: {root}", file=sys.stderr)
        return 2

    app = DiskHogApp(root)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
