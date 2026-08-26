"""Login-time boot wrapper for the relay GUI (v0.11).

The HKCU\\...\\Run entry runs this module under ``pythonw.exe`` so no
console window flashes at login. It:

* writes a boot log line before doing anything else — a silent boot
  failure is otherwise invisible, because a Run entry has no stdout
  to capture (the old ``cmd /c start`` wrapper died exactly this way);
* anchors the CWD to the project root — Run entries start in
  ``C:\\Windows\\System32``, and the old wrapper's ``/D`` flag that did
  this job is gone;
* forwards to :func:`relay.gui.run`, writing any exception (with
  traceback) to the same boot log so the next login is diagnosable.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _boot_log(message: str) -> None:
    """Append a timestamped line to ``<project_root>/.relay-logs/autostart-boot.log``.

    Never raises: a failure to write the log must not hide the boot
    failure it was supposed to record.
    """
    try:
        log_dir = Path(__file__).resolve().parents[2] / ".relay-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "autostart-boot.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except Exception:
        pass


def main() -> None:
    _boot_log(f"boot pid={os.getpid()} python={sys.executable} cwd={os.getcwd()}")
    # Anchor the CWD before importing anything from the relay package:
    # Run entries start in C:\\Windows\\System32, and Settings keeps a
    # CWD-relative resolution path for config files.
    try:
        root = Path(__file__).resolve().parents[2]
        os.chdir(root)
        _boot_log(f"cwd anchored to {root}")
    except Exception as exc:
        _boot_log(f"cwd anchor failed: {exc!r}")
    try:
        from relay import gui

        gui.run()
    except SystemExit:
        raise
    except BaseException:
        _boot_log("GUI crashed:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()