"""Windows boot autostart for the relay GUI.

Adds / removes a per-user Run entry so the GUI starts automatically at
login. Backed by the stdlib ``winreg`` module — no admin rights needed
because HKCU is per-user.

Since v0.11 the installed form is a bare ``pythonw.exe -m relay.autostart_boot``
invocation: no console window flashes (pythonw has none), no ``cmd /c
start`` wrapper to misparse at login (v0.10's wrapper failed silently
under the Run key — the GUI never appeared, with zero trace), and CWD
anchoring moved into ``relay.autostart_boot`` which chdirs to the project
root and writes a boot log so a failed login boot is diagnosable. The
GUI starts hidden to the system tray; the window pops when the user
clicks the tray icon.

Registry layout
---------------
::

    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
        RelayGUI    REG_SZ  "<pythonw.exe>" -m relay.autostart_boot

Legacy pre-v0.11 entries (``cmd /c start /MIN "" /D ... python -m
relay.gui``) are still detected by ``status_line()`` and are treated as
enabled by ``is_enabled()``.

Task Manager trap
-----------------
``HKCU\\...\\Explorer\\StartupApproved\\Run\\RelayGUI`` (a binary marker
written when the user toggles an entry in Task Manager's Startup tab)
can silently disable an otherwise-correct Run entry: first byte odd
(e.g. ``0x03``) = disabled, even or absent = enabled. ``enable()``
clears the marker; ``is_enabled()`` and ``status_line()`` report it.
The marker is matched by value name, so a re-registered entry with the
same name stays blocked until ``enable()`` (or the marker) is removed.

Limitations
-----------
- Windows only. macOS / Linux use LaunchAgents / ``.desktop`` autostart —
  different mechanisms. ``enable()`` and ``disable()`` raise
  ``RuntimeError`` on non-Windows so the CLI surfaces a clear error
  instead of silently no-op'ing.
- ``sys.executable`` is captured at install time. If you move your
  venv you must re-install (``--install-autostart`` again).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import _project_root


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_VALUE_NAME = "RelayGUI"
_NON_WINDOWS_MSG = (
    "Windows autostart is only supported on Windows. "
    "On macOS/Linux, configure your session manager directly."
)


def _python_exe() -> str:
    """Absolute path to the current Python interpreter, native separators.

    Backslashes are intentional: the Run value is executed directly by
    explorer's Run-key processing, and every entry that demonstrably
    works at login on this machine (OneDrive, GameViewer, Edge) uses a
    quoted backslash path. v0.11's forward-slashed unquoted form was the
    only entry that never ran — shell-style forward slashes are fine for
    ``cmd /c`` wrappers but are the wrong shape for a direct Run value.
    """
    return str(Path(sys.executable).resolve())


def _pythonw_exe() -> str:
    """Absolute path to the windowless Python (``pythonw.exe``) next to
    the current interpreter, native separators.

    The Run entry runs under pythonw so no console window flashes at
    login — this is what makes the ``cmd /c start /MIN`` wrapper from
    v0.10 unnecessary.
    """
    return str(Path(sys.executable).with_name("pythonw.exe").resolve())


def _command_line() -> str:
    """Build the command line written into the Run value.

    Shape: ``"<pythonw.exe>" -m relay.autostart_boot``

    v0.11 replaced the ``cmd /c start /MIN "" /D "<root>" <python> -m
    relay.gui`` wrapper with a bare pythonw invocation. The wrapper
    existed only to (a) hide the console flash — pythonw makes that
    moot — and (b) anchor the CWD via ``/D`` — now done by
    ``relay.autostart_boot.main()`` itself, which chdirs to the project
    root and writes a boot log first thing, so a silent boot failure
    like the one that killed the v0.10 form is diagnosable.

    v0.11.1: the pythonw path is always quoted with backslash separators
    to match the shape of every other Run entry on this machine. The
    earlier unquoted forward-slashed form never executed at login
    (verified across three reboots: no boot log, no process, while
    sibling entries with quoted backslash paths all launched).

    PyInstaller 冻结态没有 pythonw 兄弟进程，exe 默认子命令就是 gui，
    Run 键只写 exe 路径即可。
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{_pythonw_exe()}" -m relay.autostart_boot'


def _points_at_this_python(val: str) -> bool:
    """True iff the registered Run value references this interpreter or
    its pythonw sibling, regardless of slash direction (legacy entries
    may have forward slashes)."""
    normalized = val.replace("/", "\\")
    candidates = (Path(sys.executable).resolve(),)
    if not getattr(sys, "frozen", False):
        candidates += (_pythonw_exe(), _python_exe())
    return any(
        str(exe).replace("/", "\\") in normalized
        for exe in candidates
    )


def _open_key(subkey: str = _RUN_KEY, *, write: bool):
    """Thin wrapper so tests can swap out winreg without the call sites
    needing to know about HKEY_CURRENT_USER plumbing. ``import winreg``
    is deferred to here so the module imports cleanly on non-Windows
    (the GUI is mostly cross-platform even though this piece isn't)."""
    import winreg  # type: ignore[import-not-found]  # Windows-only stdlib

    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, access)


def _startup_approval_blocked() -> bool:
    """True iff Task Manager's Startup tab has RelayGUI marked disabled.

    ``HKCU\\...\\Explorer\\StartupApproved\\Run\\RelayGUI`` is a binary
    blob whose first byte is odd when the entry is disabled (0x02 /
    even = enabled, absent = enabled). Task Manager's "Disable" button
    writes a disabled marker here; the Run entry stays in place, so an
    entry can look correctly installed while silently never starting —
    the exact failure this module bit us with. Best-effort: a missing
    key or value means not blocked.
    """
    if os.name != "nt":
        return False
    try:
        with _open_key(_STARTUP_APPROVED_KEY, write=False) as k:
            try:
                data, _ = winreg_value(k)
            except FileNotFoundError:
                return False
    except OSError:
        return False
    first = data[0] if isinstance(data, (bytes, bytearray)) and data else None
    return first is not None and first % 2 == 1


def _clear_startup_approval() -> None:
    """Delete the StartupApproved\\Run marker for RelayGUI, if present.

    An entry with no marker defaults to enabled, so this is how
    ``enable()`` overrides a previous Task Manager "disable". Without
    it the Run value would be freshly written but the entry would still
    not start at login. Best-effort: a missing key/value is fine.
    """
    if os.name != "nt":
        return
    import winreg  # type: ignore[import-not-found]
    try:
        with _open_key(_STARTUP_APPROVED_KEY, write=True) as k:
            winreg.DeleteValue(k, _VALUE_NAME)
    except (FileNotFoundError, OSError):
        pass


def is_enabled() -> bool:
    """True iff the RelayGUI Run entry exists, points at THIS Python
    (either the current interpreter or its sibling ``pythonw.exe``),
    and is not disabled in Task Manager's Startup tab.

    A stale entry pointing at a moved/renamed venv returns False — the
    entry is effectively dead so re-installing should be a no-op rather
    than a "looks enabled but actually broken" state. Likewise an entry
    that Task Manager has marked disabled (``StartupApproved`` first
    byte odd) returns False even though the Run value is intact.

    Returns False on non-Windows so callers don't have to special-case.
    """
    if os.name != "nt":
        return False
    try:
        with _open_key(write=False) as k:
            try:
                val, _ = winreg_value(k)
            except FileNotFoundError:
                return False
    except OSError:
        return False
    # Treat "stale" (different python path) as disabled. Both the current
    # python and pythonw are accepted so legacy python.exe-based entries
    # still count as enabled.
    if not _points_at_this_python(val):
        return False
    if _startup_approval_blocked():
        return False
    return True


def winreg_value(key):
    """Indirection for tests — monkeypatch this to swap in a mock."""
    import winreg  # type: ignore[import-not-found]
    return winreg.QueryValueEx(key, _VALUE_NAME)


def enable() -> None:
    """Write / overwrite the RelayGUI Run entry with the current python.

    Installs the tray-only boot form: a bare ``pythonw.exe -m
    relay.autostart_boot`` invocation — no console window at login, the
    GUI starts hidden to the system tray, and only the tray icon
    appears. This is what you want for ``HKCU\\...\\Run`` — at login
    Windows would otherwise briefly show a console window and a GUI
    window.

    Also clears the ``StartupApproved`` marker (Task Manager's "disable"
    flag) so an entry previously disabled in the Startup tab is
    re-enabled. Without this, installs could silently fail to start at
    login despite the Run value being present and correct.

    Idempotent — calling twice produces the same registry state.

    Raises RuntimeError on non-Windows.
    """
    if os.name != "nt":
        raise RuntimeError(_NON_WINDOWS_MSG)
    import winreg  # type: ignore[import-not-found]
    cmd = _command_line()
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as k:
        winreg.SetValueEx(k, _VALUE_NAME, 0, winreg.REG_SZ, cmd)
    _clear_startup_approval()


def disable() -> None:
    """Remove the RelayGUI Run entry. No-op if it isn't present.

    Raises RuntimeError on non-Windows.
    """
    if os.name != "nt":
        raise RuntimeError(_NON_WINDOWS_MSG)
    import winreg  # type: ignore[import-not-found]
    try:
        with _open_key(write=True) as k:
            winreg.DeleteValue(k, _VALUE_NAME)
    except FileNotFoundError:
        pass  # already disabled


def _registered_value() -> Optional[str]:
    """Return the literal Run entry value, or ``None`` if absent / unreadable.

    Used by :func:`status_line` to format the displayed command. Since
    v0.10 the only installed form is tray-only, but the status output
    still detects the mode (``start /MIN`` vs bare python) so legacy
    pre-v0.10 entries are correctly labelled.
    ``is_enabled`` doesn't need this — it only checks the python path —
    but the human-facing output should match what's in the registry.
    """
    if os.name != "nt":
        return None
    try:
        with _open_key(write=False) as k:
            try:
                val, _ = winreg_value(k)
            except FileNotFoundError:
                return None
    except OSError:
        return None
    return val


def status_line() -> str:
    """Human-readable status string suitable for the CLI status command.

    Mode label: entries containing ``autostart_boot`` (v0.11+) or the
    legacy ``start /MIN`` wrapper are labelled tray-only; anything else
    is labelled window.
    """
    if os.name != "nt":
        return "Windows autostart: unsupported on this platform."
    val = _registered_value()
    if val is None or not _points_at_this_python(val):
        return "Windows autostart: disabled."
    if _startup_approval_blocked():
        return "Windows autostart: disabled (Task Manager startup tab)."
    mode = "tray-only" if ("autostart_boot" in val or "start /MIN" in val) else "window"
    return f"Windows autostart: enabled ({mode}) ({val})"