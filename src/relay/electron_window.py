"""鸭型替代 ``webview.Window`` —— 侧栏 / 悬浮球 / 主窗口共用的 Electron 后端。

不再用 pywebview/WebView2 渲染，而是常驻一个 Electron 子进程
（``electron_app/*_main.js`` + ``*_preload.js``），由本控制器通过
**TCP loopback JSON-RPC** 驱动。对外暴露 Duck-typed 面与 ``webview.Window``
一致（``evaluate_js``/``move``/``show``/``hide``/``resize``/``destroy``/
``width``/``height``/``x``/``y``/``get_size``/``events``/``native=None``）。

``ElectronWindowBase`` 汇集三窗口共用的全部机制（找 electron.exe、拉起子进程、
reader 线程、JSON-RPC 收发、事件派发、api-request 上行）。三个子类只声明差异：

* ``ElectronPanelWindow``  —— 实时侧栏（``panel_main.js``，面板实心卡片）。
* ``ElectronBallWindow``   —— 悬浮球（``ball_main.js``，透明 + hover 穿透，
  见 ``electron_ball_window.py``）。
* ``ElectronMainWindow``   —— 主窗口（``main_main.js``，Phase 1 由
  ``ElectronAppDriver`` 驱动），补主窗能力：shown/minimized/restored 事件、
  minimize/maximize/restore/is_maximized/load_url/set_icon/
  resize(w,h,fix_bits)/get_screen。

关键取舍：
* ``native`` 恒为 ``None`` —— 使 WinForms 互操作路径（``_sync_panel_owner``/
  ``_sync_panel_topmost``/``_hide_panel_taskbar``）安全 no-op，``_resize_safe``
  自动落回 ``w.resize()``。Electron 侧用 ``skipTaskbar``/``setAlwaysOnTop``
  近似替代去任务栏/置顶语义。
* ``evaluate_js`` 必须同步返回（``gui.py`` 依赖其返回值做 probe）。
* 上行：renderer 的 ``window.pywebview.api.<name>(...)`` 经
  ``ipcRenderer.invoke("uplink")`` 转发到 Electron 主进程，主进程再经 TCP
  ``api-request`` 消息交给本控制器，控制器调用 ``api_handler``（通常是
  ``gui.Api`` 实例）并把结果经 ``api-reply`` 回填。
* **用 TCP 而非 stdin/stdout**：Electron 作为 GUI 子进程，主进程 stdin 在
  detached 启动时收不到管道字节（已实测），TCP loopback 最稳。

线程模型：本控制器内部起一个 **reader 线程**（accept + 读 JSON 行）。reader
线程只做两件事——把 ``reply`` 同步 resolve（否则 ``_call`` 的 ``ev.wait()``
永远等不到），以及把 ``event``/``api-request`` 扔给独立线程派发（避免
handler 再回调 ``evaluate_js`` 时阻塞 reader 读 reply 造成死锁）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any, Callable, Optional


def _find_electron_exe() -> Optional[str]:
    """定位 electron.exe（不含 .cmd shim，Popen 直接用 EXE）。

    优先级：环境变量 RELAY_ELECTRON_EXE > 包内 node_modules > scratch 开发
    残留 > PATH。生产打包可用 RELAY_ELECTRON_EXE 指向随附的 electron.exe。
    """
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
    """Electron 应用集目录：*_main.js / *_preload.js 所在处。"""
    return os.path.join(os.path.dirname(__file__), "electron_app")


class _EventList:
    """迷你事件列表：``+=`` 注册 handler，``__call__`` 依序触发（含参数）。

    ``is_set()`` 对齐 pywebview 的 ``Event.is_set()`` 语义 = 「事件已触发过」：
    首次 ``_trigger`` 置 fired 位。gui.py 的 ``_dock_getter`` 依赖
    ``events.shown.is_set()`` 判断主窗是否已显示（窗口未显示前跳过 dock 定位）。
    """

    def __init__(self) -> None:
        self._handlers: list[Callable[..., Any]] = []
        self._fired = False
        self._lock = threading.Lock()

    def __iadd__(self, fn: Callable[..., Any]) -> "_EventList":
        with self._lock:
            if not any(h is fn for h in self._handlers):
                self._handlers.append(fn)
            # 事件已触发过后才绑定的 handler：不回放会永久漏掉（Electron 侧
            # did-finish-load 在 reader 线程立即派发，早于 gui.py 的 += 绑定，
            # _on_panel_loaded 就永远不跑 → SSE 订阅线程不启动 → 悬浮球下
            # 侧栏不再弹出）。对已 fired 的事件立即回放一次，语义对齐 pywebview
            # （事件排队到 webview.start() 后 handler 必能收到）。
            replay = bool(self._fired and not getattr(fn, "_replayed", False))
        if replay:
            try:
                fn()
            except Exception:
                pass
            try:
                setattr(fn, "_replayed", True)
            except Exception:
                pass
        return self

    def __isub__(self, fn: Callable[..., Any]) -> "_EventList":
        with self._lock:
            if any(h is fn for h in self._handlers):
                self._handlers.remove(fn)
        return self

    def _trigger(self, *args: Any) -> Any:
        with self._lock:
            self._fired = True
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
            return self._fired


class ElectronWindowBase:
    """三窗口共享的 Electron 后端骨架（构造预读 url/宽高；``start()`` 拉进程）。

    子类职责：声明 ``main_js_name`` / 事件名表 / ``_extra_start_args()``。
    所有方法线程安全（可被 panel_pool 的 op-worker 线程调用）。
    """

    def __init__(
        self,
        url: str,
        *,
        theme_bg: str | None = None,
        width: int = 400,
        height: int = 900,
        electron_exe: str | None = None,
        app_root: str | None = None,
        api_handler: Callable[[str, list[Any]], Any] | None = None,
        event_names: tuple[str, ...] = ("loaded", "closing", "moved", "resized"),
        main_js_name: str | None = None,
        args_log_tag: str = "electron-args",
    ) -> None:
        self._url = url
        self._theme_bg = theme_bg
        self._width = int(width)
        self._height = int(height)
        self._electron_exe = electron_exe or _find_electron_exe()
        self._app_root = app_root or _find_app_root()
        self._api_handler = api_handler
        self._main_js_name = main_js_name
        self._args_log_tag = args_log_tag
        self._proc: Optional[subprocess.Popen] = None
        self._srv: Optional[socket.socket] = None
        self._sock: Optional[socket.socket] = None
        self._seq = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()   # 串行化 _send，防多线程 sendall 交错撕裂 JSON 行
        self._closed = threading.Event()
        self._connected = threading.Event()  # TCP 连接建立后置位
        self._reader: Optional[threading.Thread] = None

        self.events = SimpleNamespace(**{n: _EventList() for n in event_names})
        self.native = None  # 恒 None -> WinForms 互操作路径安全 no-op
        self._last_x = 0
        self._last_y = 0
        self._last_w = int(width)
        self._last_h = int(height)

    # -- 子类差异点 --
    def _extra_start_args(self) -> list[str]:
        """start() 里 ``--url`` 与 ``--port`` 之间追加的 argv。默认宽高。"""
        return [f"--w={self._width}", f"--h={self._height}"]

    # -- lifecycle --
    def start(self) -> None:
        if self._proc is not None:
            return
        exe = self._electron_exe
        if not exe or not os.path.isfile(exe):
            raise FileNotFoundError(f"electron.exe not found: {exe}")
        # 监听 loopback 随机端口，Electron 连过来。
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        port = self._srv.getsockname()[1]

        # 关键：**必须传 *_main.js 的绝对路径**，不能只传 electron_app 目录。
        # Electron 启动一个**目录**时只认该目录 package.json 的 "main" 字段
        # （现在是 panel_main.js）—— 球窗会错误加载面板主进程（v0.183 实测）。
        # 传绝对 js 路径则 Electron 直接跑该脚本，绕开 package.json。
        main_js = os.path.join(self._app_root, self._main_js_name or "")
        if not os.path.isfile(main_js):
            raise FileNotFoundError(f"{self._main_js_name} not found: {main_js}")
        args = [
            exe,
            main_js,
            f"--url={self._url}",
            *self._extra_start_args(),
            f"--port={port}",
        ]
        # 诊断：把实际下发的 URL argv 写到 stderr 文件，便于排错
        try:
            with open(os.path.join(os.environ.get("TEMP", "/tmp"),
                                   f"{self._args_log_tag}-{os.getpid()}.log"), "w") as f:
                f.write(" ".join(repr(a) for a in args) + "\n")
        except Exception:
            pass
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # stderr 必须重定向（DEVNULL 或文件），不能留 PIPE 不排空 —— Electron
        # 会写 disk_cache/Gpu 等警告到 stderr，PIPE 撑满会阻塞主进程导致假死。
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        # 接受连接（在 reader 线程里 accept，避免阻塞 start）。
        self._reader = threading.Thread(target=self._serve, daemon=True)
        self._reader.start()

    def _serve(self) -> None:
        """accept → 进入读循环。进程退出/连接断开会结束本线程。"""
        srv = self._srv
        if srv is None:
            return
        try:
            srv.settimeout(30.0)
            conn, _ = srv.accept()
        except Exception:
            return
        # 用阻塞式 socket + makefile 顺序读；**绝不给 conn 设 timeout**，
        # 否则空闲间隙（两行 JSON 之间）会以 socket.timeout 中断读循环。
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
            pass  # 进程退出/对端关闭时 reader 线程正常结束，不算异常
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
            pass  # 预留
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
        if ev is not None:
            try:
                ev._trigger(*args)
            except Exception:
                pass
        if name == "moved" and len(args) >= 2:
            self._last_x, self._last_y = int(args[0]), int(args[1])
        elif name == "resized" and len(args) >= 2:
            self._last_w, self._last_h = int(args[0]), int(args[1])
        elif name in ("closing", "closed"):
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
        # 进程已终止（destroy / 窗口已被 OS 关闭）时快速失败，不空等 8s。
        if self._closed.is_set():
            raise RuntimeError(f"{method} on closed window")
        # TCP 连接尚未建立（start() 后立即 move/show 的竞态）—— 等最多 3s，
        # 仍没连上则快速失败 defer 到 loaded 之后，而非空等 reply 8s 卡住调用线程。
        if not self._connected.wait(3.0):
            raise ConnectionError(f"{method} before electron connected")
        seq = self._next_seq()
        ev = threading.Event()
        with self._lock:
            p = {"event": ev, "result": None, "ok": False, "error": None}
            self._pending[seq] = p
        self._send({"kind": "call", "seq": seq, "method": method, "args": list(args)})
        ok = ev.wait(timeout=8.0)
        # _resolve_call 会把 _pending[seq] pop 掉，因此这里直接用捕获的 p。
        if not ok:
            with self._lock:
                self._pending.pop(seq, None)
            raise TimeoutError(f"{type(self).__name__}.{method} timed out")
        if not p.get("ok"):
            # 老版本 reply(seq, ok, result) 把错误写在 result；error 兜底读它。
            err = p.get("error") or p.get("result")
            raise RuntimeError(f"{method} failed: {err}")
        return p.get("result")

    # -- duck-typed webview.Window surface --
    def evaluate_js(self, js: str) -> Any:
        return self._call("evaluate_js", js)

    def move(self, x: int, y: int) -> None:
        self._call("move", int(x), int(y))

    def show(self) -> None:
        self._call("show")

    def hide(self) -> None:
        self._call("hide")

    def resize(self, w: int, h: int) -> None:
        self._call("resize", int(w), int(h))
        self._last_w, self._last_h = int(w), int(h)

    def destroy(self) -> None:
        if self._proc is not None:
            try:
                # quit 是 fire-and-forget：Electron 收到即 app.quit()，不会回
                # reply，用 _call 会白白等 8s 超时。
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
                if sys.platform == "win32":
                    # 优雅退出失败 → taskkill /F /T 杀整个 electron 进程树
                    # （含 gpu/renderer 等子进程）。只 kill 主进程会把子进程
                    # 留成孤儿 —— 重启后旧球进程残留，与新球并存 = 双球。
                    # 复用 server.py / gui.py 的既有 taskkill 范式。
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True,
                            check=False,
                            timeout=10.0,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except Exception:
                        try:
                            proc.kill()   # 最后兜底
                        except Exception:
                            pass
                else:
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


class ElectronPanelWindow(ElectronWindowBase):
    """鸭型替代 ``webview.Window`` —— 实时侧栏的 Electron 后端（panel_main.js）。

    ``panel_pool.py`` 公开面零改动：只多了一个 ``start()``（panel_pool 已调用）。
    """

    def __init__(
        self,
        url: str,
        *,
        theme_bg: str = "#f5f5f7",
        width: int = 400,
        height: int = 900,
        electron_exe: str | None = None,
        app_root: str | None = None,
        api_handler: Callable[[str, list[Any]], Any] | None = None,
    ) -> None:
        super().__init__(
            url,
            theme_bg=theme_bg,
            width=width,
            height=height,
            electron_exe=electron_exe,
            app_root=app_root,
            api_handler=api_handler,
            event_names=("loaded", "closing", "moved", "resized"),
            main_js_name="panel_main.js",
            args_log_tag="electron-args",
        )

    def _extra_start_args(self) -> list[str]:
        args = [f"--w={self._width}", f"--h={self._height}"]
        if self._theme_bg:
            args.insert(0, f"--bg={self._theme_bg}")
        return args


class ElectronMainWindow(ElectronWindowBase):
    """鸭型替代 ``webview.Window`` —— 主窗口的 Electron 后端（main_main.js）。

    Phase 1 由 ``ElectronAppDriver`` 驱动。主窗差异：
    * 事件含 shown / minimized / restored（gui.py 绑定 + ``_dock_getter`` 的
      ``events.shown.is_set()``）。
    * ``closing`` 是「决定藏托盘 or 真退出」的关口（Electron close 被
      preventDefault 时窗口未必真关）—— 不置 ``_closed``，真关掉由
      ``closed`` 事件置位。
    * 新增 minimize/maximize/restore/is_maximized/load_url/set_icon/
      resize(w,h,fix_bits)/get_screen。
    * userData 指到持久目录（localStorage 跨重启），不能像面板丢 tmp。
    """

    def __init__(
        self,
        url: str,
        *,
        theme_bg: str = "#f5f5f7",
        width: int = 1180,
        height: int = 900,
        electron_exe: str | None = None,
        app_root: str | None = None,
        api_handler: Callable[[str, list[Any]], Any] | None = None,
        user_data_dir: str | None = None,
    ) -> None:
        super().__init__(
            url,
            theme_bg=theme_bg,
            width=width,
            height=height,
            electron_exe=electron_exe,
            app_root=app_root,
            api_handler=api_handler,
            event_names=("loaded", "closing", "closed", "moved", "resized",
                         "minimized", "restored", "shown"),
            main_js_name="main_main.js",
            args_log_tag="electron-main-args",
        )
        self._user_data_dir = user_data_dir
        self._last_icon: str | None = None
        self._closing_thread: Optional[threading.Thread] = None  # destroy() 等它跑完 teardown

    def _extra_start_args(self) -> list[str]:
        args = [f"--w={self._width}", f"--h={self._height}"]
        if self._theme_bg:
            args.insert(0, f"--bg={self._theme_bg}")
        if self._user_data_dir:
            args.append(f"--user-data={self._user_data_dir}")
        return args

    def _fire_event(self, name: str, args: list[Any]) -> None:
        if name == "closing":
            # 主窗 closing = Electron close 事件被 preventDefault 时发的「决定
            # 关口」（藏托盘 or 真退出），窗口**还在** —— 只触发 handler，不置
            # _closed（否则后续 _call 全判「closed window」）。真关掉由 closed
            # 事件经基类置位。
            # 记录当前派发线程：_on_closing 的 teardown（server.stop() 可能
            # taskkill 阻塞 ~10s）跑在这条线程上，destroy() 要等它跑完再强杀，
            # 否则进程退出把 daemon 线程截断 → 子中继被孤儿化（pywebview 下
            # 该 teardown 是同步的，这里补回同一保证）。
            self._closing_thread = threading.current_thread()
            ev = getattr(self.events, name, None)
            if ev is not None:
                try:
                    ev._trigger(*args)
                except Exception:
                    pass
            return
        super()._fire_event(name, args)

    # -- 主窗新增能力 --
    def minimize(self) -> None:
        self._call("minimize")

    def maximize(self) -> None:
        self._call("maximize")

    def restore(self) -> None:
        self._call("restore")

    def is_maximized(self) -> bool:
        return bool(self._call("is_maximized"))

    def set_icon(self, path: str) -> None:
        self._last_icon = str(path)
        self._call("set_icon", str(path))

    def load_url(self, url: str) -> None:
        self._call("load_url", str(url))

    def get_screen(self) -> tuple[int, int]:
        """主屏 DIP 尺寸，替代 pywebview 的 ``webview.screen``。"""
        try:
            s = self._call("get_screen")
            if s and s.get("width") and s.get("height"):
                return (int(s["width"]), int(s["height"]))
        except Exception:
            pass
        return (0, 0)

    def resize(self, w: int, h: int, fix_bits: int | None = None) -> None:
        w, h = int(w), int(h)
        # gui.Api.resize_window 传的是 webview.window.FixPoint(Flag) 枚举 ——
        # 不是 IntFlag，int() 会炸，用 .value 取位集。
        fb = int(getattr(fix_bits, "value", fix_bits) or 0)
        if fb & (4 | 8):
            # FixPoint 锚定缩放：实测 WinForms 只认 EAST(4)/SOUTH(8) 两位 ——
            # 保持对应对边固定，其余位忽略。EAST：右缘固定 → x 平移 old_w-new_w；
            # SOUTH：下缘固定 → y 平移 old_h-new_h。一次 set_bounds 到位，避免
            # setPosition+setSize 两次触发的 resize 中间态。
            cx = int(self._call("get_x"))
            cy = int(self._call("get_y"))
            x, y = cx, cy
            if fb & 4:
                x = cx + (self._last_w - w)
            if fb & 8:
                y = cy + (self._last_h - h)
            self._call("set_bounds", x, y, w, h)
            self._last_x, self._last_y = x, y
        else:
            self._call("resize", w, h)
        self._last_w, self._last_h = w, h

    def quit_async(self) -> None:
        """fire-and-forget 发 quit —— Electron 收到即 app.quit()，不等 reply/
        closing 事件，也不 terminate。frozen os._exit 路径用（避免孤儿进程），
        之后立刻 os._exit 也没事，electron 自己会退。"""
        try:
            self._send({"kind": "call", "seq": self._next_seq(),
                        "method": "quit", "args": []})
        except Exception:
            pass

    def destroy(self) -> None:
        # 主窗退出：先发 quit 让 Electron app.quit() → close 事件 → emit
        # closing/closed → Python _on_closing 跑 teardown（stop server）。给
        # 事件落地留 2s，到点没关（异常）再兜底强杀。panel/ball 的 destroy
        # 仍是直接发 quit + terminate（它们的 closing = 真关闭，无 teardown）。
        if self._proc is not None:
            self.quit_async()
            self._closed.wait(2.0)
            # 等 _on_closing 的 teardown（server.stop 最坏 taskkill ~10s）跑完
            # 再收尾 —— 别让进程退出把 daemon 派发线程截断（子中继孤儿化）。
            ct = self._closing_thread
            if ct is not None and ct is not threading.current_thread() and ct.is_alive():
                ct.join(timeout=12.0)
            self._terminate()


class ElectronAppDriver:
    """Phase 1 主窗驱动：单个 electron.exe (main_main.js) + 一条 TCP 连接。

    持有一个 ``ElectronMainWindow``。``start()`` 拉起窗口并**阻塞**到进程退出
    （hide→tray 时窗口在但进程仍在，只有真退出/崩溃才返回 —— 对齐
    ``webview.start()`` 的语义）。为 Phase 2 预留 ``{wid: window}`` 注册表
    与按 wid 路由（球/侧栏并入同一 electron.exe 进程树）。
    """

    def __init__(self, window: ElectronMainWindow | None = None) -> None:
        self.window = window
        self._wins: dict[int, ElectronWindowBase] = {}   # Phase 2: wid -> window
        self._running = threading.Event()

    def start(self) -> None:
        if self.window is None:
            raise RuntimeError("ElectronAppDriver.start without window")
        self.window.start()
        # 等 TCP 连接建立（electron 冷启动建 userData/加载可能 >3s），再 show。
        # 直接 show 的话 _call 内部 _connected.wait(3.0) 可能先超时抛
        # ConnectionError，窗口就停在隐藏态且没有托盘 —— 首次启动即失败。
        self.window._connected.wait(10.0)
        # 连接建立后首次显示 —— create 时 show:false 离屏（防闪烁），等价
        # pywebview webview.start() 的自动 show。
        try:
            self.window.show()
        except Exception:
            pass  # best-effort：show 失败（进程未就绪）由后续 loaded 流程兜底
        # 阻塞等 Electron 进程退出。hide→tray 时窗口隐藏但进程活着 → 不返回。
        proc = self.window._proc
        if proc is not None:
            try:
                proc.wait()
            except Exception:
                pass
        self._running.clear()
