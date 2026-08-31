"""v0.179：悬浮球 —— 纯 pywebview 幽灵球（替换 GDI BallLayer）。

背景：v0.165 用 GDI+ 自绘（ball_layer.py 的 BallLayer，UpdateLayeredWindow 逐
像素 alpha）做悬浮球，因当时 WebView2 透明两次实测失败（LWA_COLORKEY→黑底、
TransparencyKey→白底椭圆，根因是 WebView2 的 D3D 合成表面不参与颜色键控）。
v0.178 实验（scratch/webview_ball_demo/mini_ghost_ball_keyed.py）找到真透明
配方后，v0.179 用纯 pywebview 幽灵球替换 GDI：

  * 透明：create_window(transparent=True) → WebView2 DefaultBackgroundColor=
    Transparent；**在 events.loaded（不是 shown！）里 sleep(2) 等合成首帧后**
    设窗体 BackColor=TransparencyKey=#010203 —— shown 里设会 RecreateHandle
    隐藏窗口；loaded 里设但太早（WebView2 未合成）键控不生效。球外 #010203
    被键控掉 → 真透桌面（demo 逐像素截图验证）。
  * 拖动/点击：全局轮询状态机（对照 ball_layer._mouse_tick，AHK Floatyball
    同款）。GetAsyncKeyState 读左键 + GetWindowRect 判光标在窗体矩形内
    （WebView2 下窗体挂 Chrome_WidgetWin_0 整幅子窗，WindowFromPoint 比对
    窗体 HWND 恒 False）+ 球内圆命中判定。拖动用 **MoveWindow**（绝对坐标）：
    SetWindowPos(SWP_NOZORDER) 在这类 WinForms+WebView2 窗体返回 TRUE 却
    静默不动（demo 实测）。松开未移动 → 点击 → on_click（生产=pool.ball_clicked
    切 S1<->S2，侧栏收起/展开，**不做形态循环**）。

公开 API 与 BallLayer 完全一致（panel_pool 零改动复用）：
  start/destroy/show/hide/move_to/set_state/set_topmost + available/position
  + 构造参数 main_native/ball_size/scale_getter/theme_bg/on_click/on_move_end/
    on_move/topmost/sidebar_handle_getter。

线程模型：窗口经 pywebview 全局事件循环（webview.start() 已在 App.run 跑，
跟 panel_pool 建 always_one_window 同机制）；输入轮询是独立 daemon 线程；
状态/显隐/定位经 webview.Window API（pywebview 内部线程安全）调用。任何原生
失败只记录日志并置 available=False —— 球静默降级为「无」，绝不做成"错看"
的方窗/白底（宁可无球，不要错球）。

坐标口径：与 BallLayer 一致 —— 全程 logical 像素（panel_pool 的 _ball_pos
也是 logical）。WebView2 窗口原生缩放由 WinForms 处理，window.move 直接用
logical 即可（demo 实测拖动跟随光标正确）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import math
import threading
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent / "web"
_GHOST_BALL_URL = (_WEB_DIR / "ghost_ball.html").as_uri()

_VK_LBUTTON = 0x01
_DRAG_CLICK_PX = 4
_POLL_MS = 15
# Z 序保活节拍（毫秒）—— 侧栏/主窗同 TopMost，其 resize/show 用
# SetWindowPos(SWP_SHOWWINDOW→HWND_TOP) 会压过球；本保活周期把球压回最顶
# （置顶时）或把侧栏压到球下方（非置顶时），保证「球 > 侧栏 > 其它」。
_TOP_KEEP_MS = 300
# 透明键控色（与 demo / float_ball.html 一致）。窗体 BackColor=TransparencyKey
# 都设成这个 #010203，WebView2 透明区露出的基色被键控掉。
_KEY_RGB = (1, 2, 3)
# 透明键控延时（秒）：等 WebView2 合成首帧稳定后再设，否则键控不生效。
_KEY_DELAY_S = 2.0

_MOUSE_IDLE, _MOUSE_PRESSED, _MOUSE_DRAGGING = 0, 1, 2

_user32 = None
_load_lock = threading.Lock()


def _load_user32():
    """私有 user32 实例（避免污染 pywebview 共享实例的 argtypes，同 ball_layer）。"""
    global _user32
    if _user32 is not None:
        return _user32
    with _load_lock:
        if _user32 is not None:
            return _user32
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetCursorPos.restype = wt.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        u.GetWindowRect.restype = wt.BOOL
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.MoveWindow.restype = wt.BOOL
        u.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wt.BOOL]
        u.SetWindowPos.restype = wt.BOOL
        u.SetWindowPos.argtypes = [
            wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        _user32 = u
        return u


def _handle_int(h):
    """.NET IntPtr → int（pythonnet 3.x 下 int(h) 会抛，用 ToInt64 兜底）。"""
    try:
        return int(h.ToInt64())
    except Exception:
        try:
            return int(h)
        except Exception:
            return 0


class BallLayer2:
    """纯 pywebview 幽灵球。线程安全公开 API（任意线程可调，内部经 webview
    Window API 派发到 UI 线程）。

    与 BallLayer（GDI）同名同签名的替换层 —— panel_pool 只认这组方法/回调，
    换 import 即可切换实现。
    """

    def __init__(self, *, main_native, ball_size, scale_getter, theme_bg,
                 on_click, on_move_end, on_move=None, topmost=True,
                 sidebar_handle_getter=None):
        # main_native 保留参数兼容（BallLayer 用它作 UI 派发器；pywebview
        # 全局事件循环已代劳），仅用于拿 native 窗口句柄/事件。
        self._main_native = main_native
        self._ball_size = max(24, int(ball_size))
        self._scale_getter = scale_getter or (lambda: 1.0)
        self._theme_bg = theme_bg or "#1e1e2e"
        self._on_click = on_click                # () -> None
        self._on_move_end = on_move_end          # (logical_x, logical_y) -> None
        self._on_move = on_move                  # (logical_x, logical_y) -> None（拖动中节流）
        self._topmost = bool(topmost)
        self._sidebar_handle_getter = sidebar_handle_getter  # () -> int | 0

        self._lock = threading.RLock()
        self._win = None                         # webview.Window
        self._pos = (0, 0)                       # logical
        self._mode = "idle"
        self._visible = False
        self._available = False
        # 输入状态机（全局轮询线程，见 _mouse_tick）
        self._mouse_state = _MOUSE_IDLE
        self._down_screen = None
        self._down_win = None
        self._win_w = 0
        self._win_h = 0
        self._last_follow_ts = 0.0
        # 轮询 / 保活线程
        self._poll_thread = None
        self._topkeep_thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # 线程安全公开 API（与 BallLayer 同名同签）
    # ------------------------------------------------------------------
    def start(self, logical_x: int, logical_y: int) -> None:
        with self._lock:
            self._pos = (int(logical_x), int(logical_y))
        self._create_and_show()

    def destroy(self) -> None:
        self._stop.set()
        try:
            if self._win is not None:
                self._win.destroy()
        except Exception:
            _logger.debug("ball2 destroy window failed", exc_info=True)
        with self._lock:
            self._win = None
            self._available = False

    def show(self) -> None:
        with self._lock:
            self._visible = True
        try:
            if self._win is not None:
                self._win.show()
        except Exception:
            _logger.debug("ball2 show failed", exc_info=True)

    def hide(self) -> None:
        with self._lock:
            self._visible = False
        try:
            if self._win is not None:
                self._win.hide()
        except Exception:
            _logger.debug("ball2 hide failed", exc_info=True)

    def move_to(self, logical_x: int, logical_y: int) -> None:
        with self._lock:
            self._pos = (int(logical_x), int(logical_y))
            x, y = self._pos
        try:
            if self._win is not None:
                self._win.move(x, y)
        except Exception:
            _logger.debug("ball2 move_to failed", exc_info=True)

    def set_state(self, mode: str) -> None:
        if mode not in ("idle", "flow", "s2"):
            mode = "idle"
        with self._lock:
            if self._mode == mode:
                return
            self._mode = mode
        _logger.info("ball2 set_state -> %s", mode)
        try:
            if self._win is not None:
                self._win.evaluate_js(f"window.ballSetState && window.ballSetState('{mode}');")
        except Exception:
            _logger.debug("ball2 set_state eval failed", exc_info=True)

    def set_topmost(self, topmost: bool) -> None:
        topmost = bool(topmost)
        with self._lock:
            if self._topmost == topmost:
                return
            self._topmost = topmost
        _logger.info("ball2 set_topmost -> %s", topmost)
        try:
            if self._win is not None:
                self._win.on_top = topmost
        except Exception:
            _logger.debug("ball2 set_topmost failed", exc_info=True)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def position(self) -> tuple[int, int]:
        with self._lock:
            return self._pos

    # ------------------------------------------------------------------
    # 建窗 / 显示
    # ------------------------------------------------------------------
    def _create_and_show(self) -> None:
        if self._win is not None:
            return
        try:
            import webview
            with self._lock:
                x, y = self._pos
                size = self._ball_size
            w = webview.create_window(
                title="悬浮球",
                url=_GHOST_BALL_URL,
                width=size, height=size,
                min_size=(size, size),
                x=x, y=y,
                resizable=False,
                frameless=True,
                easy_drag=False,
                on_top=self._topmost,
                transparent=True,
                background_color=self._theme_bg,
            )
            self._win = w
            # 透明键控：loaded（不是 shown）里 sleep 后设，否则隐藏窗口/键控无效。
            w.events.loaded += self._on_loaded
            self._available = True
            # 常驻输入轮询（拖动/点击）+ Z 序保活。
            self._start_threads()
            _logger.info("ball2 create_window at %s,%s size=%s", x, y, size)
        except Exception:
            _logger.debug("ball2 create failed", exc_info=True)
            with self._lock:
                self._win = None
                self._available = False

    def _on_loaded(self) -> None:
        """页面加载完成后，稍等 WebView2 合成首帧，再设窗体 TransparencyKey。
        shown 里设会隐藏窗口；loaded 里设但太早（WebView2 未完成合成）键控不生效。
        sleep 2s 等合成稳定后再设 → 键控色 #010203 被键控掉 → 球外真透桌面。"""
        def _apply():
            try:
                time.sleep(_KEY_DELAY_S)
                win = self._win
                native = getattr(win, "native", None)
                if native is None:
                    return
                import clr
                clr.AddReference("System.Drawing")
                from System.Drawing import Color
                key = Color.FromArgb(*_KEY_RGB)
                native.BackColor = key
                native.TransparencyKey = key
                _logger.info("ball2 TransparencyKey=#010203 set")
            except Exception as exc:
                _logger.debug("ball2 TransparencyKey failed: %r", exc)
        threading.Thread(target=_apply, daemon=True, name="relay-ball2-key").start()

    def _start_threads(self) -> None:
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="relay-ball2-poll")
            self._poll_thread.start()
        if self._topkeep_thread is None or not self._topkeep_thread.is_alive():
            self._topkeep_thread = threading.Thread(
                target=self._topkeep_loop, daemon=True, name="relay-ball2-topkeep")
            self._topkeep_thread.start()

    # ------------------------------------------------------------------
    # 输入轮询：拖动 + 点击（AHK Floatyball 同款状态机）
    # ------------------------------------------------------------------
    def _ball_hwnd(self) -> int:
        try:
            win = self._win
            native = getattr(win, "native", None)
            if native is None:
                return 0
            return _handle_int(native.Handle)
        except Exception:
            return 0

    def _over_ball(self) -> bool:
        """光标是否在本球窗矩形内（WebView2 下不能用 WindowFromPoint==hwnd，
        窗体挂 Chrome_WidgetWin_0 整幅子窗，恒 False；改判 GetWindowRect）。"""
        try:
            r = wt.RECT()
            if not _load_user32().GetWindowRect(self._ball_hwnd(), ctypes.byref(r)):
                return False
            pt = wt.POINT()
            if not _load_user32().GetCursorPos(ctypes.byref(pt)):
                return False
            return r.left <= pt.x < r.right and r.top <= pt.y < r.bottom
        except Exception:
            return False

    def _over_ball_inner(self) -> bool:
        """球内圆命中：光标在窗体矩形内 且 距窗中心 ≤ 0.48 边长（球是圆）。"""
        try:
            r = wt.RECT()
            _load_user32().GetWindowRect(self._ball_hwnd(), ctypes.byref(r))
            w = r.right - r.left
            pt = wt.POINT()
            if not _load_user32().GetCursorPos(ctypes.byref(pt)):
                return False
            if not (r.left <= pt.x < r.right and r.top <= pt.y < r.bottom):
                return False
            cx, cy = r.left + w / 2, r.top + w / 2
            return math.hypot(pt.x - cx, pt.y - cy) <= w * 0.48
        except Exception:
            return False

    def _cursor(self) -> wt.POINT:
        pt = wt.POINT()
        _load_user32().GetCursorPos(ctypes.byref(pt))
        return pt

    def _key_down(self, vk: int) -> bool:
        try:
            return bool(_load_user32().GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    def _mouse_tick(self) -> None:
        hwnd = self._ball_hwnd()
        if not hwnd:
            return
        down = self._key_down(_VK_LBUTTON)
        over = self._over_ball()

        if self._mouse_state == _MOUSE_IDLE:
            if down and over and self._over_ball_inner():
                self._mouse_state = _MOUSE_PRESSED
                self._down_screen = self._cursor()
                r = wt.RECT()
                _load_user32().GetWindowRect(hwnd, ctypes.byref(r))
                self._down_win = (r.left, r.top)
                self._win_w = r.right - r.left
                self._win_h = r.bottom - r.top
        elif self._mouse_state == _MOUSE_PRESSED:
            cur = self._cursor()
            moved = (abs(cur.x - self._down_screen.x) >= _DRAG_CLICK_PX or
                     abs(cur.y - self._down_screen.y) >= _DRAG_CLICK_PX)
            if not down:
                if over:
                    threading.Thread(target=self._invoke_click,
                                     daemon=True).start()
                self._mouse_state = _MOUSE_IDLE
            elif moved:
                self._mouse_state = _MOUSE_DRAGGING
        elif self._mouse_state == _MOUSE_DRAGGING:
            cur = self._cursor()
            if not down:
                # 拖拽结束 → 持久化（logical 坐标）。
                with self._lock:
                    lx, ly = self._pos
                threading.Thread(target=self._on_move_end,
                                 args=(int(lx), int(ly)), daemon=True).start()
                self._mouse_state = _MOUSE_IDLE
            else:
                # 按住中 → MoveWindow（绝对坐标；SetWindowPos NOZORDER 在这类
                # 窗体返回 TRUE 却静默不动）。同时更新 logical _pos 供回调。
                dx = cur.x - self._down_screen.x
                dy = cur.y - self._down_screen.y
                nx, ny = self._down_win[0] + dx, self._down_win[1] + dy
                _load_user32().MoveWindow(hwnd, nx, ny, self._win_w, self._win_h, True)
                with self._lock:
                    self._pos = (nx, ny)
                    lx, ly = self._pos
                # 拖动中实时通知侧栏跟随（节流 ~20fps）。
                if self._on_move is not None:
                    now = time.monotonic()
                    if now - self._last_follow_ts >= 0.05:
                        self._last_follow_ts = now
                        threading.Thread(target=self._invoke_move,
                                         args=(int(lx), int(ly)),
                                         daemon=True).start()
                time.sleep(_POLL_MS / 1000.0)

    def _invoke_click(self) -> None:
        try:
            self._on_click()
        except Exception:
            _logger.debug("ball2 on_click callback raised", exc_info=True)

    def _invoke_move(self, x: int, y: int) -> None:
        try:
            self._on_move(int(x), int(y))
        except Exception:
            _logger.debug("ball2 on_move callback raised", exc_info=True)

    def _poll_loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._mouse_tick()
                time.sleep(_POLL_MS / 1000.0)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Z 序保活（对照 ball_layer._topkeep_tick）
    # ------------------------------------------------------------------
    def _topkeep_loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._topkeep_tick()
                time.sleep(_TOP_KEEP_MS / 1000.0)
        except Exception:
            pass

    def _topkeep_tick(self) -> None:
        h = self._ball_hwnd()
        if not h:
            return
        try:
            u = _load_user32()
            if self._topmost:
                # 置顶：球压 TopMost band 最顶 —— 球 > 侧栏 > 其它窗口。
                u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
            else:
                # 非置顶：把侧栏压到球**下方**（SetWindowPos(侧栏, 球)）。
                sh = 0
                try:
                    sh = int(self._sidebar_handle_getter()) if self._sidebar_handle_getter else 0
                except Exception:
                    sh = 0
                if sh and sh != h:
                    u.SetWindowPos(sh, h, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
        except Exception:
            _logger.debug("ball2 topkeep failed", exc_info=True)
