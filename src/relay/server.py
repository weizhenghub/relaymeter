"""Subprocess wrapper around the uvicorn relay server.

The GUI needs to start/stop the relay independently from the webview
window's lifetime — it owns the server in a child process so the
window can monitor ``is_running`` / ``pid`` via simple polling. Restart
goes through ``stop()`` + ``start()``.

Run uvicorn in its own process so:

* an exception in the GUI never crashes the relay
* the relay keeps running when the GUI window is closed (the user can
  reopen ``relay-gui`` to monitor it)
* a fresh ``ServerProcess`` picks up edits to ``.env`` / ``upstreams.json``
  between restarts without restarting the GUI

Output is captured to a per-instance log file under ``.relay-logs/``
so the user can "打开日志" from the topbar without it being interleaved
with stdout from the GUI process itself.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .config import Settings, _project_root


log = logging.getLogger("relay.server")


class ServerProcess:
    """Owns one ``uvicorn relay.main:app`` child process.

    Methods are safe to call repeatedly: starting an already-running
    process is a no-op, stopping a dead process is a no-op. Errors are
    swallowed at the boundary so the GUI can survive transient failures
    (port busy, missing python on PATH, etc.) and surface them to the
    user via the topbar instead of a crash dialog.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._proc: Optional[subprocess.Popen] = None
        self._log_path: Optional[Path] = None
        # Guards start/stop/restart against concurrent callers (JS bridge
        # thread + main thread autostart/shutdown can race). RLock so
        # ``restart()`` can call ``stop()``+``start()`` without deadlocking.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start uvicorn if it isn't already running.

        Returns True if the process is alive after the call (so callers
        can use ``if not server.start(): show_error(...)``). Spawns the
        process detached from this one — the GUI process closing does
        NOT kill the relay, only ``stop()`` does.
        """
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self.is_running:
            return True
        log_path = self._log_path_for("start")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_path = None
        try:
            log_fh = open(log_path, "ab") if log_path else subprocess.DEVNULL
        except OSError:
            log_fh = subprocess.DEVNULL
        env = os.environ.copy()
        # Match the GUI process's env so the relay sees the same .env
        # values without us having to re-parse them here.
        try:
            # PyInstaller 冻结态没有 ``-m uvicorn``，改走同 exe 的
            # ``serve`` 子命令（relay.__main__ 分发 → in-process uvicorn）。
            # 源码态保持原样：独立解释器 + uvicorn CLI。
            if getattr(sys, "frozen", False):
                cmd = [
                    sys.executable,
                    "serve",
                    "--listen",
                    f"{self.settings.host}:{self.settings.port}",
                ]
            else:
                cmd = [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "relay.main:app",
                    "--host",
                    self.settings.host,
                    "--port",
                    str(self.settings.port),
                    "--log-level",
                    "info",
                    # ``--no-access-log`` keeps the relay log file small
                    # — token counts dominate the noise floor, not HTTP
                    # lines.
                    "--no-access-log",
                ]
            self._proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=log_fh,
                stdin=subprocess.DEVNULL,
                env=env,
                # New process group so we can kill the relay cleanly
                # via ``taskkill /F /T /PID <pid>`` on Windows without
                # dragging the GUI down with it. v0.11.2 also suppresses
                # the console window: under a pythonw-booted GUI the
                # relay would otherwise flash a black console on start
                # and on every restart.
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
                if sys.platform == "win32"
                else 0,
            )
        except OSError as exc:
            log.warning("failed to start uvicorn: %s", exc)
            self._proc = None
            return False
        self._log_path = log_path
        # Poll briefly so the caller gets a real "is alive" signal — the
        # process can still fail during interpreter startup (bad import,
        # missing dependency). 0.3s is enough for the spawn to complete
        # without making ``start()`` feel sluggish in the GUI.
        time.sleep(0.3)
        return self.is_running

    def stop(self) -> bool:
        """Stop the uvicorn subprocess. No-op if it isn't running.

        Tries a graceful terminate first, then force-kills after a short
        grace period so a hung handler doesn't lock the port. On Windows
        we use ``taskkill /F /T`` because ``Popen.terminate`` only sends
        a CTRL_BREAK_EVENT to the new process group, which uvicorn
        ignores.

        Returns True when the child is confirmed gone (or was never
        running). Returns False when the kill failed or timed out — in
        that case ``self._proc`` is kept so ``is_running`` stays honest
        and the caller can surface the failure instead of leaving a
        zombie relay behind.
        """
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> bool:
        proc = self._proc
        if proc is None:
            return True
        if proc.poll() is not None:
            # Already exited.
            self._proc = None
            return True
        stopped = False
        try:
            if sys.platform == "win32":
                # /F = force, /T = tree (children too). CREATE_NO_WINDOW
                # keeps taskkill from flashing a console under a
                # pythonw-booted GUI.
                res = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=10.0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if res.returncode != 0:
                    log.warning(
                        "taskkill /F /T /PID %s failed rc=%s stderr=%s",
                        proc.pid, res.returncode,
                        res.stderr.decode("utf-8", "replace").strip(),
                    )
                else:
                    stopped = True
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                    stopped = True
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
                    stopped = True
        except subprocess.TimeoutExpired:
            log.warning("stop(): taskkill for pid=%s timed out", proc.pid)
        except OSError as exc:
            log.warning("failed to stop uvicorn pid=%s: %s", proc.pid, exc)
        if stopped:
            self._proc = None
        else:
            log.warning("stop(): pid=%s may still be running", proc.pid)
        return stopped

    def restart(self) -> bool:
        """Stop + start. Returns the post-start alive flag."""
        with self._lock:
            self._stop_locked()
            return self._start_locked()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True if the child process is still alive.

        ``poll()`` returns ``None`` while the process is running and the
        exit code once it has terminated. We treat ``None`` as running.
        """
        proc = self._proc
        if proc is None:
            return False
        return proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        """PID of the uvicorn subprocess, or None if it isn't running."""
        proc = self._proc
        if proc is None:
            return None
        return proc.pid if proc.poll() is None else None

    @property
    def log_path(self) -> Optional[Path]:
        """Path to the per-instance uvicorn log file (when configured)."""
        return self._log_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _log_path_for(self, verb: str) -> Path:
        """Return a fresh log path under ``.relay-logs/``.

        ``verb`` is appended so successive runs don't overwrite each
        other's logs — easier to debug "what did the relay do right
        before this crash?" without grepping a single concatenated file.
        """
        root = _project_root()
        ts = time.strftime("%Y%m%d-%H%M%S")
        return root / ".relay-logs" / f"uvicorn-{verb}-{ts}.log"


__all__ = ["ServerProcess"]
