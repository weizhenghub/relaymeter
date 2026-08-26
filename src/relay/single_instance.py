"""Single-instance protection for the relay GUI.

Two complementary Windows primitives:

1. **Named mutex** ``Local\\RelayGUI`` — when the first GUI starts, it
   creates the mutex. Subsequent starts see ``ERROR_ALREADY_EXISTS`` and
   know they are a duplicate. The mutex is released by the kernel when
   the owning process (or thread) closes all handles to it — i.e. on
   normal exit, crash, or taskkill /F. No cleanup code needed.

2. **Named pipe** ``\\\\.\\pipe\\RelayGUI`` — when a duplicate sees the
   mutex is taken, it connects to this pipe and writes a one-line
   command (e.g. ``"show"``) before exiting. The owning GUI polls the
   pipe via ``ConnectNamedPipe`` in a background thread, reads the
   command, and dispatches it on the Tk main thread.

Both names are scoped to the **per-session** namespace (``Local\\``,
no ``Global\\`` prefix) so different Windows users on the same box can
each run their own GUI without conflict. ``Global\\`` requires
``SeCreateGlobalPrivilege`` which normal users don't have.

Why ``Local\\`` and not just ``RelayGUI``? Without the namespace prefix
Windows resolves the name against the session's base namespace, which
is what we want — but being explicit avoids confusion when reading the
code and matches the documented pattern in
``https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-createmutexw``.

The module is platform-aware: on non-Windows the helpers degrade to a
fallback that always returns "first instance" (no protection), so the
GUI still runs. The CLI subcommands (``--install-autostart`` etc.)
don't touch this — they exit before Tk is built anyway.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional


MUTEX_NAME = "Local\\RelayGUI"
PIPE_NAME = r"\\.\pipe\RelayGUI"


# ---------------------------------------------------------------------------
# Mutex — first-instance detection
# ---------------------------------------------------------------------------

class SingleInstanceGuard:
    """Owns the ``Local\\RelayGUI`` mutex for the lifetime of the process.

    Create one at GUI startup. If ``acquired`` is True, this is the only
    GUI instance; proceed normally. If False, another GUI already owns
    the mutex and the caller should send a command (e.g. ``"show"``) to
    the named pipe and exit.

    Keeping the instance alive (don't ``del`` it, don't close the
    handle manually) is enough to hold the mutex. The kernel releases
    the mutex automatically when the process exits, even on
    ``taskkill /F``.
    """

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle = None
        self.acquired = False

        if sys.platform != "win32":
            # No-op on non-Windows. The caller will treat this as
            # "first instance" and run normally.
            self.acquired = True
            return

        import ctypes
        from ctypes import wintypes

        CreateMutexW = ctypes.windll.kernel32.CreateMutexW
        CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        CreateMutexW.restype = wintypes.HANDLE
        GetLastError = ctypes.windll.kernel32.GetLastError
        GetLastError.argtypes = []
        GetLastError.restype = wintypes.DWORD

        ERROR_ALREADY_EXISTS = 183  # 0xB7

        # bInitialOwner=False: we don't need the calling thread to own
        # it; we just need the named mutex to exist. The mutex is
        # released when all handles to it close (process exit).
        handle = CreateMutexW(None, False, name)
        if not handle:
            # CreateMutexW returns NULL on failure (very unusual — out
            # of memory, invalid name, etc.). Treat as "first
            # instance" rather than blocking the GUI from starting.
            self.acquired = True
            return
        if GetLastError() == ERROR_ALREADY_EXISTS:
            # Duplicate — close our handle so we don't leak the
            # reference, and signal the caller to send a command and
            # exit.
            ctypes.windll.kernel32.CloseHandle(handle)
            self.acquired = False
            return
        self._handle = handle
        self.acquired = True

    def __enter__(self):
        """Enter the context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager, releasing the mutex."""
        self.release()

    def release(self) -> None:
        """Explicitly close our handle. Optional — process exit does
        the same. Mainly useful for tests."""
        if self._handle is not None:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
            self.acquired = False


# ---------------------------------------------------------------------------
# Named pipe — second-instance command channel
# ---------------------------------------------------------------------------

class CommandListener:
    """Listen on the relay-GUI named pipe for commands from duplicate
    processes.

    Spawns a single background daemon thread that loops on
    ``ConnectNamedPipe`` / ``ReadFile`` and calls ``on_command(line)``
    once per line received. The callback is invoked from the listener
    thread; the GUI is expected to marshal back to the Tk main thread
    with ``root.after(0, ...)``.

    Each accepted connection is closed before the next ``ConnectNamedPipe``
    call. This keeps the protocol trivial — one command per connection,
    terminated by a newline — and avoids any framing issues around
    overlapping writes from different duplicates.

    The listener can be stopped by calling ``stop()`` (e.g. from a Tk
    quit handler). It will reject new connections after that and the
    daemon thread will exit at the next ``ConnectNamedPipe`` return.
    """

    def __init__(
        self,
        name: str = PIPE_NAME,
        on_command: Callable[[str], None] = lambda line: None,
    ) -> None:
        self._name = name
        self._on_command = on_command
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pipe_handle = None
        self.started = False

        if sys.platform != "win32":
            # No-op on non-Windows.
            return

        self._start_thread()

    def _start_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._serve_forever,
            name="relay-gui-pipe",
            daemon=True,
        )
        self._thread.start()
        self.started = True

    def _serve_forever(self) -> None:
        import ctypes
        from ctypes import wintypes

        # Lazily import the things we need so the module loads on
        # non-Windows too.
        kernel32 = ctypes.windll.kernel32

        CreateNamedPipeW = kernel32.CreateNamedPipeW
        CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,           # lpName
            wintypes.DWORD,             # dwOpenMode
            wintypes.DWORD,             # dwPipeMode
            wintypes.DWORD,             # nMaxInstances
            wintypes.DWORD,             # nOutBufferSize
            wintypes.DWORD,             # nInBufferSize
            wintypes.DWORD,             # nDefaultTimeOut
            wintypes.LPVOID,            # lpSecurityAttributes
        ]
        CreateNamedPipeW.restype = wintypes.HANDLE

        ConnectNamedPipe = kernel32.ConnectNamedPipe
        ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        ConnectNamedPipe.restype = wintypes.BOOL

        DisconnectNamedPipe = kernel32.DisconnectNamedPipe
        DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        DisconnectNamedPipe.restype = wintypes.BOOL

        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        ReadFile = kernel32.ReadFile
        ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        ReadFile.restype = wintypes.BOOL

        GetLastError = kernel32.GetLastError
        GetLastError.restype = wintypes.DWORD

        # dwOpenMode: PIPE_ACCESS_INBOUND (1) — server reads, client writes.
        # FILE_FLAG_OVERLAPPED (0x40000000) intentionally NOT set —
        # we use blocking ConnectNamedPipe so the loop is simple.
        # PIPE_REJECT_REMOTE lives in dwPipeMode, NOT dwOpenMode.
        # dwPipeMode: PIPE_TYPE_BYTE (0) | PIPE_WAIT (0) | PIPE_REJECT_REMOTE (8)
        # — refuse remote clients; only same-machine GUI duplicates
        # should be sending.
        PIPE_ACCESS_INBOUND = 0x00000001
        PIPE_TYPE_BYTE = 0x00000000
        PIPE_WAIT = 0x00000000
        PIPE_REJECT_REMOTE = 0x00000008
        PIPE_UNLIMITED_INSTANCES = 255
        NMPWAIT_USE_DEFAULT_WAIT = 0x00000000

        open_mode = PIPE_ACCESS_INBOUND
        pipe_mode = PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE
        # INVALID_HANDLE_VALUE: restype=HANDLE (c_void_p) turns the
        # Win32 -1 into a huge positive int (2^64-1 on 64-bit), so the
        # plain ``if not handle`` check below would MISS it and send an
        # invalid handle into ConnectNamedPipe → an infinite hot loop
        # that never creates a visible pipe.
        invalid_handle = ctypes.c_void_p(-1).value

        while not self._stop_event.is_set():
            try:
                handle = CreateNamedPipeW(
                    self._name,
                    open_mode,
                    pipe_mode,
                    PIPE_UNLIMITED_INSTANCES,
                    4096,
                    4096,
                    NMPWAIT_USE_DEFAULT_WAIT,
                    None,
                )
            except Exception:
                handle = None

            if not handle or handle == invalid_handle:
                # v0.93: a single failure used to kill the listener for
                # the life of the process — the pipe name can be
                # transiently unavailable right after a sibling GUI is
                # force-killed, leaving the surviving GUI running but
                # unreachable, so a second launch could never ask it to
                # quit ("no instance answering the pipe"). Retry with a
                # short pause instead of bailing; the GUI self-heals.
                if self._stop_event.wait(0.25):
                    return
                continue

            # Publish the handle BEFORE calling ConnectNamedPipe so that
            # stop() can close it from the main thread and unblock the
            # call. CloseHandle on a pipe handle that's blocked in
            # ConnectNamedPipe returns ERROR_INVALID_HANDLE in the
            # blocked thread.
            self._pipe_handle = handle

            # Blocking ConnectNamedPipe — sleeps until a client connects
            # or the pipe is closed. We don't pass an OVERLAPPED struct
            # so this is genuinely blocking.
            try:
                ok = ConnectNamedPipe(handle, None)
            except Exception:
                ok = False

            # Detach from the shared field before processing — by the
            # time we exit the with-ReadFile block below, the handle is
            # closed and any stop() call must not double-close it.
            local_handle = handle
            handle = None
            self._pipe_handle = None

            if self._stop_event.is_set():
                # We're shutting down. Close and exit.
                try:
                    CloseHandle(local_handle)
                except Exception:
                    pass
                return

            if not ok:
                # ERROR_PIPE_CONNECTED (535) is normal — client
                # connected between CreateNamedPipe and ConnectNamedPipe.
                err = GetLastError()
                if err != 535:
                    # Anything else is unexpected. Close and retry.
                    try:
                        CloseHandle(local_handle)
                    except Exception:
                        pass
                    continue

            # Read until newline or EOF. Clients send one short line
            # like "show\n" and disconnect.
            buf = bytearray()
            bytes_read = wintypes.DWORD(0)
            try:
                while True:
                    chunk = (ctypes.c_ubyte * 256)()
                    ok = ReadFile(local_handle, chunk, 256, ctypes.byref(bytes_read), None)
                    if not ok or bytes_read.value == 0:
                        break
                    buf.extend(bytes(chunk[: bytes_read.value]))
                    if b"\n" in buf:
                        break
            except Exception:
                pass

            try:
                DisconnectNamedPipe(local_handle)
                CloseHandle(local_handle)
            except Exception:
                pass
            self._pipe_handle = None

            if not buf:
                continue
            line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                self._on_command(line)
            except Exception:
                # Never let a callback crash the listener thread.
                pass

    def stop(self) -> None:
        """Signal the daemon thread to exit and unblock it.

        Called from the GUI's quit path. The daemon thread checks
        ``_stop_event`` at the top of its serve loop, AND its current
        ``ConnectNamedPipe`` call is unblocked by closing the pipe
        handle. Two scenarios:

        1. Thread is in the top-of-loop check or between iterations:
           stop_event is observed on the next iteration, thread exits
           cleanly.

        2. Thread is blocked in ``ConnectNamedPipe`` waiting for a
           client: closing ``self._pipe_handle`` from this thread
           makes the kernel return ``ERROR_INVALID_HANDLE`` from
           ``ConnectNamedPipe`` so the thread loops back, observes
           stop_event, and exits.

        The thread is daemon=True, so even if it doesn't observe the
        signal promptly (e.g. the CreateNamedPipeW call happens to be
        in flight at this exact moment), the interpreter shutdown at
        process exit will collect it. We don't join here because
        ``CreateNamedPipeW`` is uninterruptible — joining would block
        until something else wakes it.
        """
        self._stop_event.set()
        # Closing the pipe handle unblocks any pending ConnectNamedPipe
        # with ERROR_INVALID_HANDLE so the daemon thread can notice the
        # stop_event and exit on its next iteration.
        handle = self._pipe_handle
        if handle is not None:
            self._pipe_handle = None
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Duplicate-side helper — used by the second-instance process
# ---------------------------------------------------------------------------

def _acquire_exclusive(
    name: str = MUTEX_NAME,
    timeout: float = 10.0,
    notify: Callable[[], None] | None = None,
) -> SingleInstanceGuard:
    """Poll the named mutex until it's free, then own it.

    Used when a duplicate launch wants to take over: the existing GUI is
    told to quit (via the named pipe) and this waits for the kernel to
    release the mutex — the old process's handles are all closed on exit,
    which is exactly what destroys a named mutex — before creating a
    fresh guard. The process that wins the poll is the sole GUI.

    ``notify`` is invoked right before each poll (when the mutex is still
    held) so the caller can re-ask the existing instance to quit. v0.93:
    without this, a single ``send_command`` at launch could fire before a
    racing sibling's pipe existed and never be retried, needlessly
    falling back to force-killing a healthy instance.

    Raises RuntimeError if the old instance hasn't exited within
    ``timeout`` seconds (e.g. it ignored the quit command).
    """
    import time

    deadline = time.time() + timeout
    while True:
        if notify is not None:
            try:
                notify()
            except Exception:
                pass
        guard = SingleInstanceGuard(name)
        if guard.acquired:
            return guard
        # acquired=False → __init__ already closed the duplicate handle.
        if time.time() >= deadline:
            raise RuntimeError(
                "existing relay-gui instance did not exit within "
                f"{timeout:.0f}s"
            )
        time.sleep(0.25)


def send_command(command: str, pipe_name: str = PIPE_NAME, timeout_ms: int = 2000) -> bool:
    """Open the named pipe, write ``command + '\\n'``, and disconnect.

    Returns True on success, False if the pipe is unavailable (no GUI
    listening, wrong permissions, etc.). Used by the second GUI
    instance: it sees the mutex is taken, calls this with
    ``command="show"`` so the existing GUI un-hides its window, and
    then exits.

    We use a short timeout rather than blocking forever — if the user
    double-clicks a stale shortcut the second process should fail
    fast and the GUI launcher should let the user see the error rather
    than hang.
    """
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE

    WriteFile = kernel32.WriteFile
    WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    WriteFile.restype = wintypes.BOOL

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    WaitNamedPipeW = kernel32.WaitNamedPipeW
    WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    WaitNamedPipeW.restype = wintypes.BOOL

    # GENERIC_WRITE (0x40000000), no sharing, OPEN_EXISTING (3).
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF  # -1 as HANDLE

    # WaitNamedPipeW succeeds when a pipe instance is available. This
    # avoids the ERROR_PIPE_BUSY (231) "all pipe instances are busy"
    # race when the server has just been created and hasn't called
    # ConnectNamedPipe yet.
    try:
        if not WaitNamedPipeW(pipe_name, timeout_ms):
            return False
    except Exception:
        return False

    try:
        handle = CreateFileW(
            pipe_name,
            GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
    except Exception:
        return False

    if handle == INVALID_HANDLE_VALUE or not handle:
        return False

    payload = (command + "\n").encode("utf-8")
    written = wintypes.DWORD(0)
    try:
        ok = WriteFile(handle, payload, len(payload), ctypes.byref(written), None)
    except Exception:
        ok = False
    finally:
        try:
            CloseHandle(handle)
        except Exception:
            pass

    return bool(ok) and written.value == len(payload)