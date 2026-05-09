from __future__ import annotations

import sys

try:
    from .cli import run_cli
except ImportError:
    # PyInstaller may execute this file as a top-level script.
    from coverup.cli import run_cli


def launch_gui() -> int:
    try:
        from .gui import launch
    except ImportError:
        from coverup.gui import launch

    return launch()


def launch_cli() -> int:
    return run_cli(sys.argv[1:])


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
