"""鸭型替代 ``webview.Window`` —— 实时悬浮球的 Electron 后端。

与 ``electron_window.ElectronPanelWindow`` 同构：常驻一个 Electron 子进程
（``electron_app/ball_main.js`` + ``ball_preload.js``），由本控制器通过 TCP
loopback JSON-RPC 驱动。对外暴露 Duck-typed 面（``evaluate_js``/``move``/
``show``/``hide``/``destroy``/``width``/``height``/``x``/``y``/``events``/
``native=None``），因此 ``ball_layer2.BallLayer2`` 的公开 API 面几乎零改动即可
切到 Electron 后端。

关键取舍（与 ElectronPanelWindow 一致）：
* ``native`` 恒为 ``None`` —— WinForms 互操作路径（``_sync_panel_owner``/
  ``_sync_panel_topmost``/``_hide_panel_taskbar``/``MoveWindow``）安全 no-op，
  窗口几何/置顶全交给 Electron 侧（``setPosition``/``setBounds``/
  ``setAlwaysOnTop``）。
* 透明 + 点击穿透由 Electron 侧 ``transparent:true`` + ``setIgnoreMouseEvents``
  处理 —— 真正的桌面真透（球区露出桌面），不再是 SetWindowRgn/颜色键控死路。
* ``set_ignore(bool)`` 暴露给上层切换「吞点击 / 穿透」：球被拖动/点击时需先用
  ``set_ignore(False)`` 开吞，否则 mousedown 会穿透到后面窗口。

线程模型：与 ElectronPanelWindow 一致 —— reader 线程只同步 resolve ``reply``，
``event``/``api-request`` 扔到独立线程派发（避免 handler 回调 ``evaluate_js``
时阻塞 reader 读 reply 死锁）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from types import SimpleNamespace
from typing import Any, Callable, Optional


def _find_electron_exe() -> Optional[str]:
    env = os.environ.get("RELAY_ELECTRON_EXE")
    if env and os.path.isfile(env):
        return env
    cands = [
        os.path.join(os.path.dirname(__file__), "electron_app", "node_modules",
                     "electron", "dist", "electron.exe"),
        os.path.join(os.path.dirname(__file__), "..", "..", "scratch",
                     "electron_ball", "node_modules", "electron", "dist",
                     "electron.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    import shutil
    return shutil.which("electron")


def _find_app_root() -> str:
    # v0.195：便携附件包（frozen onefile + 旁置 relay_assets/）场景下，可
    # 用 RELAY_ELECTRON_APP_ROOT 显式指定 electron_app 目录；未设时退回
    # 源码树内的 electron_app（与 _find_electron_exe 的 RELAY_ELECTRON_EXE
    # 对齐，便于附件包/单 exe 两种形态复用同一套 ball 渲染资产）。
    env = os.environ.get("RELAY_ELECTRON_APP_ROOT")
    if env and os.path.isdir(env):
        return env
    return os.path.join(os.path.dirname(__file__), "electron_app")


class _EventList:
    def __init__(self) -> None:
        self._handlers: list[Callable[..., Any]] = []
        self._lock = threading.Lock()

    def __iadd__(self, fn: Callable[..., Any]) -> "_EventList":
        with self._lock:
            if not any(h is fn for h in self._handlers):
                self._handlers.append(fn)
        return self

    def __isub__(self, fn: Callable[..., Any]) -> "_EventList":
        with self._lock:
            if any(h is fn for h in self._handlers):
                self._handlers.remove(fn)
        return self

    def _trigger(self, *args: Any) -> Any:
        with self._lock:
            hs = list(self._handlers)
        last = None
        for h in hs:
            try:
                last = h(*args)
            except Exception:
                pass
        return last

    def is_set(self) -> bool:
        with self._lock:
            return len(self._handlers) > 0


class ElectronBallWindow:
    """鸭型替代 ``webview.Window``，后端常驻一个透明 Electron 子进程球窗。

    构造即预读 url/宽高；``start()`` 拉起子进程并建立 TCP 连接。
    所有方法线程安全（可被 panel_pool 的 op-worker / ball 轮询线程调用）。
    """

    def __init__(
        self,
        url: str,
        *,
        width: int = 56,
        height: int = 56,
        electron_exe: str | None = None,
        app_root: str | None = None,
        api_handler: Callable[[str, list[Any]], Any] | None = None,
    ) -> None:
        self._url = url
        self._width = int(width)
        self._height = int(height)
        self._electron_exe = electron_exe or _find_electron_exe()
        self._app_root = app_root or _find_app_root()
        self._api_handler = api_handler
        self._proc: Optional[subprocess.Popen] = None
        self._srv: Optional[socket.socket] = None
        self._sock: Optional[socket.socket] = None
        self._seq = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._connected = threading.Event()  # TCP 连接建立后置位
        self._reader: Optional[threading.Thread] = None

        self.events = SimpleNamespace(
            loaded=_EventList(),
            closing=_EventList(),
            moved=_EventList(),
            resized=_EventList(),
            dragend=_EventList(),  # Electron 主进程拖动结束（落盘）
            clicked=_EventList(),  # Electron 主进程 drag-end 判「点击」（无位移）→ 切 S1/S2
        )
        self.native = None  # 恒 None -> WinForms 互操作路径安全 no-op
        self._last_x = 0
        self._last_y = 0
        self._last_w = int(width)
        self._last_h = int(height)

    # -- lifecycle --
    def start(self) -> None:
        if self._proc is not None:
            return
        exe = self._electron_exe
        if not exe or not os.path.isfile(exe):
            raise FileNotFoundError(f"electron.exe not found: {exe}")
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        port = self._srv.getsockname()[1]

        # 关键：**必须传 ball_main.js 的绝对路径**，不能只传 electron_app 目录。
        # Electron 启动一个**目录**时只认该目录 package.json 的 "main" 字段
        # （现在是 panel_main.js）—— 球窗会错误加载面板主进程：窗口
        # transparent:false（不透明）、没有 hover 穿透循环、没有 drag 状态机、
        # 没有 capture —— 直接导致「透明错误 + 点击拖动错误 + capture
        # unknown-method」（v0.183 实测）。传绝对 js 路径则 Electron 直接跑
        # 该脚本，绕开 package.json。
        main_js = os.path.join(self._app_root, "ball_main.js")
        if not os.path.isfile(main_js):
            raise FileNotFoundError(f"ball_main.js not found: {main_js}")
        args = [
            exe,
            main_js,
            f"--url={self._url}",
            f"--w={self._width}",
            f"--h={self._height}",
            f"--port={port}",
        ]
        try:
            with open(os.path.join(os.environ.get("TEMP", "/tmp"),
                                   f"electron-ball-args-{os.getpid()}.log"), "w") as f:
                f.write(" ".join(repr(a) for a in args) + "\n")
        except Exception:
            pass
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._serve, daemon=True)
        self._reader.start()

    def _serve(self) -> None:
        srv = self._srv
        if srv is None:
            return
        try:
            srv.settimeout(30.0)
            conn, _ = srv.accept()
        except Exception:
            return
        self._sock = conn
        self._connected.set()
        import io
        f = conn.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        try:
            for raw in f:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                self._dispatch(m)
        except Exception:
            pass
        finally:
            self._on_peer_closed()

    def _dispatch(self, m: dict[str, Any]) -> None:
        kind = m.get("kind")
        if kind == "reply":
            self._resolve_call(m)
        elif kind == "event":
            threading.Thread(target=self._fire_event,
                             args=(m.get("name"), m.get("args") or []),
                             daemon=True).start()
        elif kind == "log":
            pass
        elif kind == "api-request":
            threading.Thread(target=self._handle_api_request,
                             args=(m,), daemon=True).start()

    def _resolve_call(self, m: dict[str, Any]) -> None:
        seq = m.get("seq")
        with self._lock:
            p = self._pending.pop(seq, None)
        if p is None:
            return
        p["event"].set()
        p["result"] = m.get("result")
        p["ok"] = bool(m.get("ok", False))
        p["error"] = m.get("error")

    def _fire_event(self, name: str, args: list[Any]) -> None:
        ev = getattr(self.events, name, None)
        if ev is None:
            return
        try:
            ev._trigger(*args)
        except Exception:
            pass
        if name == "moved" and len(args) >= 2:
            self._last_x, self._last_y = int(args[0]), int(args[1])
        elif name == "resized" and len(args) >= 2:
            self._last_w, self._last_h = int(args[0]), int(args[1])
        elif name == "closing":
            self._closed.set()

    def _handle_api_request(self, m: dict[str, Any]) -> None:
        token = m.get("token")
        call = m.get("call") or {}
        method = call.get("method")
        args = call.get("args") or []
        result, error = None, None
        if self._api_handler is not None and method:
            try:
                # handler 兼容两种形态：可调用（(method, args) -> result）或
                # 具名对象（getattr(handler, method)(*args)，如 gui.Api 实例）。
                if callable(self._api_handler):
                    result = self._api_handler(method, args)
                else:
                    fn = getattr(self._api_handler, method, None)
                    if fn is None:
                        error = "no-method:" + method
                    else:
                        result = fn(*args)
            except Exception as exc:
                error = str(exc)
        else:
            error = "no-api-handler"
        self._send({"kind": "api-reply", "token": token, "result": result, "error": error})

    def _on_peer_closed(self) -> None:
        self._closed.set()
        self._connected.clear()

    # -- JSON-RPC primitives --
    def _send(self, obj: dict[str, Any]) -> None:
        s = self._sock
        if s is None:
            return
        data = (json.dumps(obj, default=str) + "\n").encode("utf-8")
        with self._send_lock:
            try:
                s.sendall(data)
            except Exception:
                pass

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _call(self, method: str, *args: Any) -> Any:
        if self._closed.is_set():
            raise RuntimeError(f"{method} on closed window")
        # TCP 连接尚未建立（_create_ball 后立即 show/move 的高频竞态）——
        # 等最多 3s；仍没连上则快速失败，让上层 defer 到 _on_loaded 补做，
        # 而不是空等 reply 8s 超时卡住调用线程。
        if not self._connected.wait(3.0):
            raise ConnectionError(f"{method} before electron connected")
        seq = self._next_seq()
        ev = threading.Event()
        with self._lock:
            p = {"event": ev, "result": None, "ok": False, "error": None}
            self._pending[seq] = p
        self._send({"kind": "call", "seq": seq, "method": method, "args": list(args)})
        ok = ev.wait(timeout=8.0)
        if not ok:
            with self._lock:
                self._pending.pop(seq, None)
            raise TimeoutError(f"ElectronBallWindow.{method} timed out")
        if not p.get("ok"):
            err = p.get("error") or p.get("result")  # 旧版 reply(seq,ok,result) 把错误写在 result
            raise RuntimeError(f"{method} failed: {err}")
        return p.get("result")

    # -- duck-typed webview.Window surface --
    def evaluate_js(self, js: str) -> Any:
        return self._call("evaluate_js", js)

    def capture(self) -> bytes:
        """抓 Electron 窗口自身渲染为 PNG（绕过桌面/离屏验证真实合成像素）。
        capturePage 是在渲染进程合成结果上截图，与屏幕显示无关（桌面全黑也能验证）。"""
        b64 = self._call("capture")
        if isinstance(b64, str) and b64:
            import base64
            return base64.b64decode(b64)
        return b""

    def move(self, x: int, y: int) -> None:
        self._call("move", int(x), int(y))

    def show(self) -> None:
        self._call("show")

    def hide(self) -> None:
        self._call("hide")

    def resize(self, w: int, h: int) -> None:
        self._call("resize", int(w), int(h))
        self._last_w, self._last_h = int(w), int(h)

    def set_topmost(self, topmost: bool) -> None:
        self._call("set_topmost", bool(topmost))

    def set_ignore(self, want_ignore: bool) -> None:
        """切换球窗「吞点击 / 穿透」。拖动/点击前先 set_ignore(False) 开吞。"""
        self._call("set_ignore", bool(want_ignore))

    def set_expanded(self, expanded: bool) -> None:
        """v0.184：展开态（球+侧栏 / 磁吸 dock）→ 停 hover 穿透循环 + 全窗口吞
        点击；收起态 → 恢复球帽 hover 判定（球内吞/球外穿）。"""
        self._call("set_expanded", bool(expanded))

    def destroy(self) -> None:
        if self._proc is not None:
            try:
                self._send({"kind": "call", "seq": self._next_seq(),
                            "method": "quit", "args": []})
            except Exception:
                pass
            self._terminate()

    def _terminate(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._closed.set()

    @property
    def width(self) -> int:
        return int(self._call("get_width"))

    @property
    def height(self) -> int:
        return int(self._call("get_height"))

    @property
    def x(self) -> int:
        return int(self._call("get_x"))

    @property
    def y(self) -> int:
        return int(self._call("get_y"))

    def get_size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def hwnd(self) -> int:
        """Electron 窗口原生 HWND（getNativeWindowHandle），供 Win32 抓窗验证。"""
        return int(self._call("get_hwnd"))
