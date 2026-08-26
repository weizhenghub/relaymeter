# -*- coding: utf-8 -*-
"""PyInstaller frozen entry point (relay-gui.spec).

PyInstaller runs the spec's ``Analysis`` script as a *top-level* ``__main__``
with no package context (``__package__ is None``). ``src/relay/__main__.py``
uses relative imports (``from .config import ...``), so it can't be used as the
entry script directly — the frozen app would raise ``attempted relative import
with no known parent package``.

This thin launcher re-exports ``relay.__main__`` as a normal submodule so its
``__package__`` is ``relay`` and the relative imports resolve. All CLI dispatch
(``serve`` / ``gui`` / ``stats``, ``--listen``, ``--profile``) is unchanged —
it just lives in ``relay.__main__.main()``.
"""

import sys

from relay.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
