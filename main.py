"""Top-level project entry point.

Lets you run the relay without going through the Python module path,
which is friendlier when launching from a desktop shortcut or a non-Python
script.

Usage:
    python main.py                    # default: launch the GUI
    python main.py serve             # start the HTTP relay (no GUI)
    python main.py gui [--no-autostart]
    python main.py stats [--window 5h] [--since N] [--db PATH]
    python main.py --help

This is a thin wrapper over the same dispatch used by
`python -m relay`. See `src/relay/__main__.py` for the implementation.
"""

from __future__ import annotations

import sys
import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path


# Make `relay` importable when invoked as `python main.py` from the project
# root without installing the package.
_PROJECT_ROOT = Path(__file__).resolve().parent
_SRC = _PROJECT_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    from relay.__main__ import main as _dispatch
    return _dispatch()


if __name__ == "__main__":
    raise SystemExit(main())
