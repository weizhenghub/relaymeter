"""System tray icon for the relay GUI (v0.21).

Why: closing the window used to mean shutting down the relay — but most
of the time the user just wants the window out of the way while keeping
the proxy running in the background. A pystray icon with "显示窗口 /
退出中继" handles that without making accidental closes catastrophic.

Two threads collaborate here:
  * pystray owns its own message loop, started via ``run_detached`` on
    a daemon thread. Its callbacks fire from that thread.
  * pywebview owns the WinForms message loop on the main thread.
    ``webview.Window.show()`` / ``hide()`` are already thread-safe — they
    marshal back via ``Invoke`` — so the tray callback can call them
    directly without a hand-rolled dispatcher.

Failure modes that this module explicitly avoids:
  * A second tray icon stacking if the user reopens the window several
    times (the same ``Tray`` instance is reused).
  * The tray thread blocking on something that prevents the user from
    quitting (the menu actions are tiny; we don't await network calls).
  * A crash inside the tray taking down the relay (everything is
    wrapped and exceptions are logged).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Callable, Optional

import pystray


_log = logging.getLogger("relay.tray")


def refresh_taskbar() -> None:
    """Broadcast ``TaskbarCreated`` so Windows drops tray icons orphaned by
    relay GUI processes that died without a graceful ``NIM_DELETE`` — e.g. a
    previous instance killed at reboot, via ``taskkill /F``, or by a crash.

    pystray removes its own icon on a clean ``stop()`` (so the single-instance
    "take over" path leaves no ghost), but an abrupt kill leaves the icon
    hanging until the user hovers it. Broadcasting ``TaskbarCreated`` makes
    explorer rebuild its notification list and discard icons whose owner
    window is gone. This is called at every GUI launch so stale icons from a
    prior dead session vanish on the next start. Our own icon is (re)created
    only later, on first hide-to-tray, so it survives the broadcast.

    Windows-only; a no-op (silently) elsewhere.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")
        HWND_BROADCAST = 0xFFFF
        result = wintypes.DWORD(0)
        # SMTO_ABORTIFHUNG (0x2) so a hung shell can't block our startup.
        user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_TASKBARCREATED, 0, 0,
            0x2, 1000, ctypes.byref(result),
        )
        _log.info("broadcast TaskbarCreated to clear stale tray icons")
    except Exception:
        _log.warning("TaskbarCreated broadcast failed", exc_info=True)


def _build_icon_image():
    """v0.111 托盘图标 —— 复用共享图标模块（icon.build_tray_image）的
    深色圆角底 + 琥珀用量弧环 + 白色 T 设计。运行时合成，不依赖资产文件。
    与主窗任务栏图标同款，身份统一。"""
    from .icon import build_tray_image

    return build_tray_image()


class Tray:
    """Owns the lifetime of the system-tray icon.

    Constructed once at App start. The tray is created on demand the
    first time the window is hidden — before then the user hasn't
    shown any interest in background behaviour, so we don't spend a
    thread on the icon.
    """

    def __init__(
        self,
        *,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        title: str = "RelayMeter",
    ) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._title = title
        self._icon: Optional[pystray.Icon] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_started(self) -> None:
        """Create the tray icon if it isn't already. Idempotent."""
        with self._lock:
            if self._icon is not None:
                return
            try:
                icon = pystray.Icon(
                    "usage-stats-relay",
                    icon=_build_icon_image(),
                    title=self._title,
                    menu=pystray.Menu(
                        pystray.MenuItem("显示窗口", lambda _i, _it: self._show()),
                        pystray.Menu.SEPARATOR,
                        pystray.MenuItem("退出中继", lambda _i, _it: self._quit()),
                    ),
                )
                # run_detached spins up pystray's Win32 message loop on
                # a daemon thread. Daemon=True is critical: if the
                # relay exits for any reason, we don't want the tray
                # thread to keep the process alive.
                icon.run_detached()
                self._icon = icon
                _log.info("tray icon started")
            except Exception:
                # Tray is best-effort. If the OS rejects the icon
                # (locked-down session, no shell, etc.) the relay must
                # still work; we just lose the background-mode nicety.
                _log.exception("could not start tray icon")
                self._icon = None

    def stop(self) -> None:
        """Tear the icon down on quit. Safe to call multiple times."""
        with self._lock:
            if self._icon is None:
                return
            try:
                self._icon.stop()
            except Exception:
                _log.warning("tray stop raised", exc_info=True)
            self._icon = None
            _log.info("tray icon stopped")

    @property
    def running(self) -> bool:
        return self._icon is not None

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def _show(self) -> None:
        try:
            self._on_show()
        except Exception:
            _log.exception("tray show callback failed")

    def _quit(self) -> None:
        try:
            self._on_quit()
        except Exception:
            _log.exception("tray quit callback failed")