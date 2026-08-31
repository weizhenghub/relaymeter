"""v0.165：悬浮球 —— 原生 GDI+ 分层窗（替换 WebView2 方案）。

背景：用 WebView2 窗口做悬浮球，透明方案在本机两次实测失败：
  * WS_EX_LAYERED + LWA_COLORKEY → 黑底（WebView2 的 D3D 合成表面不参与颜色键控）；
  * transparent=True + TransparencyKey → 白底椭圆（窗体层键控不了 WebView 子 HWND）。
根因是 WebView2 的子 HWND 合成表面无法对桌面做逐像素 alpha。因此改用原生
WinForms 分层窗 + GDI+ 自绘 + UpdateLayeredWindow 逐像素 alpha —— 不依赖任何
WebView 合成，球外像素 alpha=0 直接透出桌面，真·圆形。

本模块自包含：所有 WinForms / GDI+ 对象都活在 pywebview 的单一 STA UI 线程
（经 main_native 的 BeginInvoke 派发）。任何原生调用失败只记录日志并置
available=False —— 球静默降级为「无」，绝不做成"错看"的方窗/白色（宁可无球）。

坐标口径（重要）：本层与侧栏几何全程用 **logical 像素**（panel_pool 的
_ball_pos 也是 logical）—— 只在 UpdateLayeredWindow 边界乘 scale 转 physical。
"""

from __future__ import annotations

import ctypes
import logging
import math
import threading
import time
from ctypes import wintypes

_logger = logging.getLogger(__name__)


def _ensure_ball_file_log() -> None:
    """自包含诊断：GUI 进程默认（无 --diag）不配 root handler，relay.ball_layer
    的 INFO 会丢。这里强制挂一个文件 handler，写 .relay-logs/ball-debug.log，
    不依赖 basicConfig / --diag，保证球的行为可查。幂等。"""
    try:
        if getattr(_logger, "_ball_file_ready", False):
            return
        from pathlib import Path
        _p = Path(__file__).resolve().parents[2] / ".relay-logs" / "ball-debug.log"
        _p.parent.mkdir(parents=True, exist_ok=True)
        _h = logging.FileHandler(_p, encoding="utf-8")
        _h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        _logger.addHandler(_h)
        _logger.setLevel(logging.DEBUG)
        _logger.propagate = False
        _logger._ball_file_ready = True
    except Exception:
        pass


_ensure_ball_file_log()

# ---------------------------------------------------------------------------
# Win32 常量 / 结构
# ---------------------------------------------------------------------------
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_APPWINDOW = 0x00040000

_SW_SHOW = 5
_SW_HIDE = 0

_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_DIB_RGB_COLORS = 0x00
_BI_RGB = 0

# 拖动 vs 点击阈值（物理像素）。
_DRAG_CLICK_PX = 4
# 动画节拍（毫秒）。
_ANIM_MS = 33
# 输入轮询节拍（毫秒）—— AHK Floatyball 的 WatchMouse=30 同款；全局轮询，
# 不依赖分层窗收到 WM 鼠标消息（WS_EX_NOACTIVATE + 逐像素 alpha 分层窗在
# 本机收不到 WM_LBUTTONDOWN 之类，这是「点击没变化」的根因）。
# v0.166：从 30 降到 15 —— 拖动流畅度由它决定（球直接跟光标，每 tick 一次
# UpdateLayeredWindow 重定位）；30ms≈33fps 有明显卡顿感，15ms≈64fps 顺滑。
# WinForms Timer 分辨率下限约 15.6ms，再小会被系统按 15.6 取整，无意义。
_MOUSE_POLL_MS = 15
# 拖动中侧栏跟随节流（毫秒）—— on_move 回调只在这个间隔内最多触发一次，
# 避免每 tick 一个线程/一次 relayout 刷屏；~20fps 足够让侧栏平滑跟随球。
# 注意：这不是球的定位帧率 —— 球定位由 _MOUSE_POLL_MS 决定，此值只控制
# 「侧栏跟着球 relayout」的频率（relayout 成本高于球的净重定位）。
_FOLLOW_MS = 50
_VK_LBUTTON = 0x01
# 鼠标状态机（球内按下 → 拖动/点击判定）。
_MOUSE_IDLE = 0
_MOUSE_PRESSED = 1
_MOUSE_DRAGGING = 2

# v0.170：Z 序保活 —— 球被侧栏（同样 TopMost）的 resize/show 用
# SetWindowPos(SWP_SHOWWINDOW→HWND_TOP) 提到 band 顶后压过，本保活周期
# 把球 SetWindowPos(HWND_TOPMOST) 提回 band 最顶，保证「球 > 侧栏 > 其它」。
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
# 保活节拍（毫秒）。侧栏宽度动画约 22ms/帧，球压回不必更频繁；300ms 足够
# 让球始终在最上，且 SetWindowPos 开销极小。只在 _topmost=True 时运行。
_TOP_KEEP_MS = 300


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


# v0.170：**必须**用私有 user32 实例，不能用共享的 ``ctypes.windll.user32``。
# pywebview 的 winforms.py 也走 ``windll.user32.SetWindowPos``，且它的 move()
# 传 ``None`` 给 cx/cy（依赖 ctypes 未设 argtypes 时的宽松转换）。若我们在
# 共享实例上设 SetWindowPos.argtypes（cx/cy 声明成 c_int），pywebview 的
# move() 会立刻抛 ``ctypes.ArgumentError: argument 5: NoneType`` → 侧栏所有
# move 全部失败、卡在固定位置不跟随球。私有 WinDLL 实例的 argtypes 互不影响。
_user32 = None


def _load_user32():
    global _user32
    if _user32 is not None:
        return _user32
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.UpdateLayeredWindow.restype = wintypes.BOOL
    u.UpdateLayeredWindow.argtypes = [
        wintypes.HWND,
        wintypes.HDC,
        ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(wintypes.SIZE),
        wintypes.HDC,
        ctypes.POINTER(wintypes.POINT),
        wintypes.DWORD,
        ctypes.POINTER(_BLENDFUNCTION),
        wintypes.DWORD,
    ]
    u.GetWindowLongPtrW.restype = ctypes.c_void_p
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_void_p
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    # v0.170：Z 序保活（SetWindowPos(HWND_TOPMOST)）。句柄/insertAfter 都是
    # 指针，**必须**显式 argtypes，否则 64 位下按 c_int 截断 → 保活失效。
    u.SetWindowPos.restype = wintypes.BOOL
    u.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    # 这些返回 GDI/handle 指针，**必须**显式 restype 为指针大小，否则 64 位
    # 下默认按 32 位 c_int 截断 → 句柄值被破坏 → CreateDIBSection 判空失败、
    # UpdateLayeredWindow 拿不到有效缓冲 → 分层窗空白、球不可见。
    u.GetDC.restype = wintypes.HDC
    u.ReleaseDC.restype = ctypes.c_int
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.SetWindowRgn.restype = ctypes.c_int
    u.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    u.ShowWindow.restype = wintypes.BOOL
    # 点击/拖动改用全局轮询（AHK Floatyball 同款）—— 不依赖分层窗收 WM 鼠标
    # 消息。GetAsyncKeyState 取物理按键态；WindowFromPoint 查光标下窗口句柄。
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetCursorPos.restype = wintypes.BOOL
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.WindowFromPoint.restype = wintypes.HWND
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32 = u
    return u


def _handle_int(h):
    """把 .NET 控件的 Handle（System.IntPtr）安全转成 int。
    pythonnet 3.x 里 ``int(f.Handle)`` 会抛 TypeError（IntPtr 不能直接 int()），
    必须用 ToInt64()。这里做兼容兜底。"""
    try:
        return int(h.ToInt64())
    except Exception:
        try:
            return int(h)
        except Exception:
            return 0


def _load_gdi():
    g = ctypes.windll.gdi32
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateDIBSection.restype = wintypes.HBITMAP
    g.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g.DeleteDC.restype = wintypes.BOOL
    g.DeleteObject.restype = wintypes.BOOL
    return g


class BallLayer:
    """原生 GDI+ 分层圆球。线程安全公开 API（任意线程可调）。

    内部所有 WinForms / GDI+ 对象仅活在同一 STA UI 线程；跨线程调用经
    main_native.BeginInvoke 异步派发（绝不 Invoke 阻塞 —— 历史 RecreateHandle
    跨线程 SendMessage 死锁教训）。
    """

    def __init__(self, *, main_native, ball_size, scale_getter, theme_bg,
                 on_click, on_move_end, on_move=None, topmost=True,
                 sidebar_handle_getter=None):
        self._main_native = main_native          # WinForms.Control（dispatcher）
        self._ball_size = max(24, int(ball_size))  # logical 像素
        self._scale_getter = scale_getter        # () -> float（logical→physical）
        self._theme_bg = theme_bg or "#1e1e2e"
        self._on_click = on_click                # () -> None（不传参）
        self._on_move_end = on_move_end          # (logical_x, logical_y) -> None
        self._on_move = on_move                  # (logical_x, logical_y) -> None（拖动中节流回调）
        # v0.170：悬浮球置顶（WS_EX_TOPMOST）。默认 True（沿用 v0.165 行为）；
        # 由设置页「悬浮球置顶」开关控制，侧栏同步跟随，保证球与侧栏恒同层级。
        self._topmost = bool(topmost)
        # v0.176：侧栏句柄 getter —— 非置顶时相对 Z 序保活（把侧栏压到球
        # 下方），保证「球恒在侧栏之上」且不提升球盖过其它窗口。
        self._sidebar_handle_getter = sidebar_handle_getter

        self._lock = threading.RLock()
        self._form = None                        # WinForms.Form
        self._pos = (0, 0)                       # logical
        self._mode = "idle"                      # idle / flow / s2
        self._visible = False
        self._frame = 0
        # 输入状态机（全局轮询，见 _mouse_tick）。AHK Floatyball 同款。
        self._mouse_state = _MOUSE_IDLE
        self._down_screen = None                 # physical(winforms Cursor.Position)
        self._down_loc = None                    # physical（按下瞬间球的物理左上角）
        self._last_follow_ts = 0.0               # 拖动中 on_move 节流时间戳

        self._available = False
        self._timer = None
        self._mouse_timer = None
        # v0.170：Z 序保活 timer —— 周期把球提到 TopMost band 最顶（侧栏
        # 同置顶后其 resize 会用 HWND_TOP 压过球；保活把球压回去）。
        self._topkeep_timer = None
        # cached GDI handles（UI 线程专用）
        self._w_phys = 0
        self._h_phys = 0
        self._dib_bmp = None                     # HBITMAP（CreateDIBSection）
        self._dib_bits = None                    # ctypes buffer（裸像素）
        self._memdc = None

    # ------------------------------------------------------------------
    # 线程安全公开 API
    # ------------------------------------------------------------------
    def start(self, logical_x: int, logical_y: int) -> None:
        with self._lock:
            self._pos = (int(logical_x), int(logical_y))
        self._dispatch(self._create_and_show)

    def destroy(self) -> None:
        self._dispatch(self._teardown)

    def show(self) -> None:
        with self._lock:
            self._visible = True
        self._dispatch(self._show)

    def hide(self) -> None:
        with self._lock:
            self._visible = False
        self._dispatch(self._hide)

    def move_to(self, logical_x: int, logical_y: int) -> None:
        with self._lock:
            self._pos = (int(logical_x), int(logical_y))
        # 仅重定位；内容复用上次缓冲（不做无谓重绘）。_present 内部自会有
        # form 为 None 的保护。
        self._dispatch(self._present, reposition=True, redraw=False)

    def set_state(self, mode: str) -> None:
        if mode not in ("idle", "flow", "s2"):
            mode = "idle"
        with self._lock:
            if self._mode == mode:
                _logger.info("ball set_state %s -> noop (already)", mode)
                return
            self._mode = mode
        _logger.info("ball set_state -> %s (dispatching _apply_mode)", mode)
        self._dispatch(self._apply_mode)

    def set_topmost(self, topmost: bool) -> None:
        """v0.170：运行时切换置顶。与侧栏同层级同步，改设置页开关即时生效。
        仅改标志 + 派发 UI 线程 SetWindowPos(HWND_TOPMOST/NOTOPMOST)（f.TopMost）。"""
        topmost = bool(topmost)
        with self._lock:
            if self._topmost == topmost:
                return
            self._topmost = topmost
        _logger.info("ball set_topmost -> %s (dispatching _apply_topmost)", topmost)
        self._dispatch(self._apply_topmost)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def position(self) -> tuple[int, int]:
        with self._lock:
            return self._pos

    # ------------------------------------------------------------------
    # UI 线程内部
    # ------------------------------------------------------------------
    def _dispatch(self, fn, **kw) -> None:
        """异步派发到 UI 线程（fire-and-forget，绝不 Invoke 阻塞）。
        调用方必须已释放 pool._lock 再进这里。"""
        if self._main_native is None:
            return
        try:
            # 确保 clr/System 已加载（_dispatch 可能在还没跑 _try_clr 的线程上
            # 首先执行；clr.AddReference 是进程级，重复调无害）。
            try:
                import clr
                clr.AddReference("System.Windows.Forms")
            except Exception:
                pass
            from System.Windows.Forms import MethodInvoker
            mi = MethodInvoker(lambda: fn(**kw)) if kw else MethodInvoker(fn)
            if self._main_native.InvokeRequired:
                self._main_native.BeginInvoke(mi)
            else:
                fn(**kw) if kw else fn()
        except Exception:
            _logger.debug("ball dispatch failed", exc_info=True)

    def _try_clr(self):
        try:
            import clr
            clr.AddReference("System.Windows.Forms")
            clr.AddReference("System.Drawing")
            from System.Drawing import (
                Bitmap, Color, Graphics,
                PointF, Rectangle, Size, SolidBrush, Pen,
            )
            from System.Drawing.Drawing2D import GraphicsPath, SmoothingMode, PathGradientBrush
            from System.Drawing.Imaging import PixelFormat, ImageLockMode
            from System.Windows.Forms import (
                Form, FormBorderStyle, FormStartPosition, Cursor,
                MouseEventArgs, CreateParams, MouseButtons, Timer,
            )
            self._System = locals()
            return True
        except Exception:
            _logger.debug("ball clr/System.Drawing import failed", exc_info=True)
            return False

    def _create_and_show(self) -> None:
        if self._form is not None:
            return
        if not self._try_clr():
            self._available = False
            return
        try:
            Sys = self._System
            Form = Sys["Form"]
            f = Form()
            f.Text = "悬浮球"
            f.FormBorderStyle = getattr(Sys["FormBorderStyle"], "None")
            f.StartPosition = Sys["FormStartPosition"].Manual
            f.ShowInTaskbar = False
            if self._topmost:
                f.TopMost = True
            f.MaximizeBox = False
            f.MinimizeBox = False
            f.ResizeRedraw = False
            f.ClientSize = Sys["Size"](self._ball_size, self._ball_size)
            # v0.165c：点击/拖动**不**挂 WinForms Mouse* 事件 —— WS_EX_NOACTIVATE
            # + 逐像素 alpha 分层窗在本机收不到 WM 鼠标消息（「点击没变化」根因）。
            # 改用全局轮询状态机（_mouse_tick，AHK Floatyball 同款）：GetAsyncKeyState
            # 读物理按键态 + WindowFromPoint 查光标下窗口，不依赖分层窗收消息。
            self._form = f
            # 先建 Handle（触发原生窗口），再设 WS_EX_*（无 RecreateHandle）。
            hwnd = _handle_int(f.Handle)
            u = _load_user32()
            ex = u.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
            ex = int(ex)
            ex = (ex & ~_WS_EX_APPWINDOW) | (
                _WS_EX_LAYERED | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE)
            if self._topmost:
                ex |= _WS_EX_TOPMOST
            u.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, ex)
            self._available = True
            # 隐藏态预渲染 + 定位，再 SHOW。
            self._present(reposition=True, redraw=True)
            u.ShowWindow(hwnd, _SW_SHOW)
            # 常驻输入轮询（AHK WatchMouse=30ms 同款）。
            self._mouse_timer_start()
            # v0.170/v0.176：恒启 Z 序保活 —— 置顶时压 TopMost band 最顶，
            # 非置顶时把侧栏压到球下方（相对层级），球始终在侧栏之上。
            self._topkeep_start()
        except Exception:
            _logger.exception("ball create_and_show failed")
            self._available = False
            self._form = None

    def _scale(self) -> float:
        try:
            s = self._scale_getter()
            return float(s) if s else 1.0
        except Exception:
            return 1.0

    def _teardown(self) -> None:
        # 先销毁 cached GDI，再关窗体（同 UI 线程，顺序安全）。
        self._timer_stop()
        self._mouse_timer_stop()
        self._topkeep_stop()
        self._release_gdi()
        if self._form is not None:
            try:
                self._form.Close()
            except Exception:
                pass
            self._form = None
        self._available = False

    def _show(self) -> None:
        if self._form is None:
            return
        try:
            _load_user32().ShowWindow(_handle_int(self._form.Handle), _SW_SHOW)
        except Exception:
            _logger.debug("ball show failed", exc_info=True)

    def _hide(self) -> None:
        if self._form is None:
            return
        try:
            _load_user32().ShowWindow(_handle_int(self._form.Handle), _SW_HIDE)
        except Exception:
            _logger.debug("ball hide failed", exc_info=True)

    def _ensure_gdi(self, w_phys: int, h_phys: int) -> None:
        if self._dib_bmp is not None and self._w_phys == w_phys and self._h_phys == h_phys:
            return
        self._release_gdi()
        try:
            gdi = _load_gdi()
            bmi = _BITMAPINFO()
            bi = bmi.bmiHeader
            bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bi.biWidth = w_phys
            bi.biHeight = -h_phys          # 顶向下（top-down）
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = _BI_RGB
            bits = ctypes.c_void_p()
            hbmp = gdi.CreateDIBSection(
                0, ctypes.byref(bmi), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not hbmp:
                _logger.debug("ball CreateDIBSection failed (hwnd=%r)", int(hbmp or 0))
                return
            memdc = gdi.CreateCompatibleDC(0)
            gdi.SelectObject(memdc, hbmp)
            _logger.info("ball ensure_gdi ok size=%dx%d hbmp=%s memdc=%s",
                         w_phys, h_phys, int(self._dib_bmp or 0), int(self._memdc or 0))
            self._dib_bmp = hbmp
            self._dib_bits = bits
            self._memdc = memdc
            self._w_phys = w_phys
            self._h_phys = h_phys
        except Exception:
            _logger.debug("ball ensure_gdi failed", exc_info=True)

    def _release_gdi(self) -> None:
        try:
            gdi = _load_gdi()
            if self._memdc:
                gdi.DeleteDC(self._memdc)
                self._memdc = None
            if self._dib_bmp:
                gdi.DeleteObject(self._dib_bmp)
                self._dib_bmp = None
            self._dib_bits = None
            self._w_phys = 0
            self._h_phys = 0
        except Exception:
            pass

    def _present(self, reposition: bool, redraw: bool) -> None:
        """唯一调 UpdateLayeredWindow 的函数。reposition=仅定位（复用缓冲），
        redraw=重画内容。都强制在 UI 线程执行。"""
        if self._form is None or not self._available:
            return
        try:
            u = _load_user32()
            scale = self._scale()
            w_phys = max(1, int(self._ball_size * scale))
            h_phys = w_phys
            self._ensure_gdi(w_phys, h_phys)
            if self._dib_bmp is None or self._dib_bits is None:
                return
            if redraw:
                self._redraw_into_dib(w_phys, h_phys)
            with self._lock:
                lx, ly = self._pos
            pt_dst = wintypes.POINT(int(lx * scale), int(ly * scale))
            size = wintypes.SIZE(w_phys, h_phys)
            pt_src = wintypes.POINT(0, 0)
            blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
            hwn = _handle_int(self._form.Handle)
            hdc_scr = u.GetDC(0)
            try:
                _ok = u.UpdateLayeredWindow(
                    hwn, hdc_scr, ctypes.byref(pt_dst), ctypes.byref(size),
                    self._memdc, ctypes.byref(pt_src), 0,
                    ctypes.byref(blend), _ULW_ALPHA)
                _logger.debug("ball present ulw mode=%s redraw=%s ok=%s pos=%s,%s",
                              self._mode, redraw, bool(_ok), int(lx), int(ly))
            finally:
                u.ReleaseDC(0, hdc_scr)
        except Exception:
            _logger.debug("ball present failed", exc_info=True)

    def _redraw_into_dib(self, w_phys: int, h_phys: int) -> None:
        """GDI+ 画 32bppPArgb Bitmap → LockBits → 逐行拷进 DIBSection 缓冲。
        球外 alpha=0（透明）；内容按 _mode 绘制。"""
        try:
            Sys = self._System
            Bitmap = Sys["Bitmap"]
            Graphics = Sys["Graphics"]
            Color = Sys["Color"]
            Rectangle = Sys["Rectangle"]
            SolidBrush = Sys["SolidBrush"]
            SmoothingMode = Sys["SmoothingMode"]
            PathGradientBrush = Sys["PathGradientBrush"]
            GraphicsPath = Sys["GraphicsPath"]
            PixelFormat = Sys["PixelFormat"]
            ImageLockMode = Sys["ImageLockMode"]
            PointF = Sys["PointF"]
            Pen = Sys["Pen"]

            bmp = Bitmap(w_phys, h_phys, PixelFormat.Format32bppPArgb)
            g = Graphics.FromImage(bmp)
            try:
                g.SmoothingMode = SmoothingMode.AntiAlias
                g.Clear(Color.Transparent)
                with self._lock:
                    mode = self._mode
                    frame = self._frame
                self._draw_ball(g, w_phys, h_phys, mode, frame,
                                Color, SolidBrush, PathGradientBrush,
                                GraphicsPath, Rectangle, PointF, Pen)
            finally:
                g.Dispose()

            # LockBits → memcpy 进 DIB（PArgb 已 premultiplied）。
            rect = Rectangle(0, 0, w_phys, h_phys)
            data = bmp.LockBits(rect, ImageLockMode.ReadOnly,
                                PixelFormat.Format32bppPArgb)
            try:
                src = _handle_int(data.Scan0)      # LockBits.Scan0 是 .NET IntPtr
                stride = int(data.Stride)
                dst = int(self._dib_bits.value)
                row = w_phys * 4
                for y in range(h_phys):
                    ctypes.memmove(dst + y * row, src + y * stride, row)
            finally:
                bmp.UnlockBits(data)
            bmp.Dispose()
        except Exception:
            _logger.debug("ball redraw failed", exc_info=True)

    def _draw_ball(self, g, w, h, mode, frame, Color, SolidBrush,
                   PathGradientBrush, GraphicsPath, Rectangle, PointF, Pen):
        """Claude 小幽灵渲染（v0.165e）。

        像素风 terracotta 幽灵，替代之前的「玻璃球」：
          idle —— 幽灵正常站立，静态
          flow —— 幽灵轻微上下浮动（漂浮）+ 呼吸光晕
          s2   —— 幽灵 180° 倒立（脚朝天）+ 脉冲外环 + 右上红心「有流」徽标

        坐标按 min(w,h) 归一化（窗口是方形），幽灵水平填充约 80%、垂直约 78%，
        给 flow 浮动和 s2 脉冲留边。
        """
        cx, cy = w * 0.5, h * 0.5
        s = float(min(w, h))                     # 方形窗边长

        # 幽灵主色（terracotta / 陶土橘）与眼睛深褐。图片实测取色。
        c_body = Color.FromArgb(255, 210, 124, 99)
        c_eye = Color.FromArgb(255, 108, 64, 46)

        def br(a, *rgb):
            return SolidBrush(Color.FromArgb(a, rgb[0], rgb[1], rgb[2]))

        def fill_rr(g, x, y, rw, rh, rad, brush):
            """圆角矩形填充（GDI+ 无内置 FillRoundedRectangle，用 GraphicsPath 拼）。"""
            rad = min(rad, rw * 0.5, rh * 0.5)
            p = GraphicsPath()
            p.AddArc(x, y, 2 * rad, 2 * rad, 180, 90)
            p.AddArc(x + rw - 2 * rad, y, 2 * rad, 2 * rad, 270, 90)
            p.AddArc(x + rw - 2 * rad, y + rh - 2 * rad, 2 * rad, 2 * rad, 0, 90)
            p.AddArc(x, y + rh - 2 * rad, 2 * rad, 2 * rad, 90, 90)
            p.CloseFigure()
            g.FillPath(brush, p)
            p.Dispose()

        # 1) 呼吸光晕（flow / s2）—— 幽灵后面的柔光，随呼吸缩放。
        if mode in ("flow", "s2"):
            pulse = 0.5 + 0.5 * math.sin(frame * 0.16)
            halo_r = s * (0.44 + 0.05 * pulse)
            if mode == "s2":
                halo = br(int(70 + 40 * pulse), 216, 96, 92)
            else:
                halo = br(int(52 + 36 * pulse), 210, 150, 120)
            g.FillEllipse(halo, float(cx - halo_r), float(cy - halo_r),
                          float(2 * halo_r), float(2 * halo_r))
            halo.Dispose()

        # 2) 幽灵主体（含四肢），flow 时上下浮动。s2 时绕中心转 180°（脚朝天）。
        bob = (math.sin(frame * 0.22) * s * 0.02) if mode == "flow" else 0.0

        # 几何（都是 s 的比例）。v0.165f：加宽加胖 —— 身体横向拉宽到 ~74%、
        # 纵向略收，宽大于高（椭圆/矮胖感），眼睛间距随脸宽拉开。
        body_x0, body_x1 = 0.13 * s, 0.87 * s      # 身体左右（加宽）
        body_y0, body_y1 = 0.22 * s, 0.80 * s      # 身体上下（略矮，显胖）
        arm_y0, arm_y1 = 0.47 * s, 0.60 * s        # 手臂上下
        arm_out_l = 0.06 * s; arm_out_r = 0.94 * s # 手臂外缘（短臂，随身体加宽探出）
        eye_w = 0.060 * s
        eye_y0, eye_y1 = 0.35 * s, 0.52 * s        # 眼睛（竖向）
        eye_lx, eye_rx = 0.37 * s, 0.63 * s        # 两眼中心（间距加宽）
        # 裙摆：底部若干个圆弧下凸（幽灵的波浪下摆），等距铺满身体宽度。
        hem_r = 0.055 * s
        hem_cy = body_y1                             # 圆心在身体底边上，只露下半圆
        hem_n = 4
        hem_span = (body_x1 - body_x0) - 2 * hem_r   # 首尾凸块圆心到身体边缘留半圆
        hem_cx = tuple(body_x0 + hem_r + hem_span * i / (hem_n - 1)
                       for i in range(hem_n))

        body_brush = br(255, c_body.R, c_body.G, c_body.B)
        eye_brush = br(255, c_eye.R, c_eye.G, c_eye.B)

        # 施加变换：flow 浮动 / s2 倒立。
        if mode == "s2":
            g.TranslateTransform(float(cx), float(cy))
            g.RotateTransform(180)
            g.TranslateTransform(float(-cx), float(-cy))
        elif bob:
            g.TranslateTransform(0, float(bob))

        # 手臂（短臂，先画在身体后面，被身体覆盖的内端看不见）。
        g.FillRectangle(body_brush, float(arm_out_l), float(arm_y0),
                        float(body_x0 - arm_out_l), float(arm_y1 - arm_y0))
        g.FillRectangle(body_brush, float(body_x1), float(arm_y0),
                        float(arm_out_r - body_x1), float(arm_y1 - arm_y0))
        # 身体（上圆角；底部平，下接裙摆）。
        fill_rr(g, body_x0, body_y0, body_x1 - body_x0, body_y1 - body_y0,
                s * 0.07, body_brush)
        # 裙摆：4 个下凸半圆 + 缝隙之间的平底，组成波浪下摆。半圆上沿与身体同色
        # 融合，下沿露出圆润凸起 —— 比「4 条腿」更像幽灵。
        for hemx in hem_cx:
            g.FillEllipse(body_brush,
                          float(hemx - hem_r), float(hem_cy - hem_r),
                          float(2 * hem_r), float(2 * hem_r))
        # 眼睛（两条竖细条）。
        g.FillRectangle(eye_brush, float(eye_lx - eye_w * 0.5), float(eye_y0),
                        float(eye_w), float(eye_y1 - eye_y0))
        g.FillRectangle(eye_brush, float(eye_rx - eye_w * 0.5), float(eye_y0),
                        float(eye_w), float(eye_y1 - eye_y0))

        # 复位变换（不再影响后续徽标）。
        g.ResetTransform()
        body_brush.Dispose()
        eye_brush.Dispose()

        # 3) s2：脉冲外环 + 右上红心「有流」徽标。
        if mode == "s2":
            scale_pulse = 0.95 + 0.05 * (0.5 + 0.5 * math.sin(frame * 0.22))
            ring_r = s * 0.47 * scale_pulse
            ring = br(60, 216, 96, 92)
            g.FillEllipse(ring, float(cx - ring_r), float(cy - ring_r),
                          float(2 * ring_r), float(2 * ring_r))
            ring.Dispose()
            d = max(5.0, float(w * 0.15))
            bx, by = cx + s * 0.22, cy - s * 0.22
            bg = br(255, 255, 255, 255)
            g.FillEllipse(bg, float(bx - d), float(by - d), float(2 * d), float(2 * d))
            bg.Dispose()
            core = br(255, 255, 72, 72)
            g.FillEllipse(core, float(bx - d * 0.66), float(by - d * 0.66),
                          float(2 * d * 0.66), float(2 * d * 0.66))
            core.Dispose()

    # ------------------------------------------------------------------
    # 鼠标拖动 + 点击 —— 全局轮询状态机（AHK Floatyball 同款）
    # ------------------------------------------------------------------
    # 不挂 WinForms Mouse* 事件（分层窗收不到 WM 鼠标消息）。常驻 _mouse_timer
    # 每 30ms：GetAsyncKeyState 读左键物理态 + WindowFromPoint 查光标下是否本球。
    #   IDLE    → 左键按下且光标在球内 → PRESSED（记下起点）
    #   PRESSED → 松开：未移动过 → 点击；移动超阈值 → DRAGGING
    #   DRAGGING→ 松开：拖拽结束（持久化）；按住中：跟随光标重定位
    def _mouse_timer_start(self) -> None:
        if self._mouse_timer is not None:
            return
        try:
            Timer = self._System["Timer"]
            t = Timer()
            t.Interval = _MOUSE_POLL_MS
            t.Tick += self._mouse_tick
            t.Start()
            self._mouse_timer = t
            _logger.info("ball mouse poller started")
        except Exception:
            _logger.debug("ball mouse timer start failed", exc_info=True)

    def _mouse_timer_stop(self) -> None:
        t = self._mouse_timer
        self._mouse_timer = None
        self._mouse_state = _MOUSE_IDLE
        if t is not None:
            try:
                t.Stop()
                t.Dispose()
            except Exception:
                _logger.debug("ball mouse timer stop failed", exc_info=True)

    def _key_down(self, vk: int) -> bool:
        """物理按键态（不依赖消息队列）。GetAsyncKeyState 高位=按下。"""
        try:
            return bool(_load_user32().GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    def _ball_hwnd(self) -> int:
        try:
            return _handle_int(self._form.Handle)
        except Exception:
            return 0

    def _over_ball(self) -> bool:
        """光标当前是否在本球窗口内（等价 AHK 的 MouseIsHwnd(hBall)）。
        窗口是 WS_EX_TOPMOST，_form.Handle 用真实 hwnd。"""
        try:
            pt = wintypes.POINT()
            u = _load_user32()
            if not u.GetCursorPos(ctypes.byref(pt)):
                return False
            return u.WindowFromPoint(pt) == self._ball_hwnd()
        except Exception:
            return False

    def _mouse_tick(self, sender=None, e=None) -> None:
        try:
            state = self._mouse_state
            down = self._key_down(_VK_LBUTTON)
            over = self._over_ball()
            if state == _MOUSE_IDLE:
                if down and over:
                    self._mouse_state = _MOUSE_PRESSED
                    self._down_screen = self._cursor_pos()
                    with self._lock:
                        self._down_loc = (self._pos[0] * self._scale(),
                                          self._pos[1] * self._scale())
                    _logger.info("ball press over ball (state PRESSED)")
            elif state == _MOUSE_PRESSED:
                cur = self._cursor_pos()
                moved = (cur is not None and self._down_screen is not None and (
                    abs(cur.X - self._down_screen.X) >= _DRAG_CLICK_PX or
                    abs(cur.Y - self._down_screen.Y) >= _DRAG_CLICK_PX))
                if not down:
                    # 松开且未移动 → 点击（AHK：EndWin=hBall → LeftClickAction）。
                    if over:
                        _logger.info("ball click (released w/o move) -> on_click")
                        threading.Thread(target=self._invoke_click,
                                         daemon=True).start()
                    self._mouse_state = _MOUSE_IDLE
                elif moved:
                    _logger.info("ball press->drag")
                    self._mouse_state = _MOUSE_DRAGGING
            elif state == _MOUSE_DRAGGING:
                cur = self._cursor_pos()
                if not down:
                    # 拖拽结束 → 持久化 + relayout（logical 坐标）。
                    _logger.info("ball drag end -> on_move_end")
                    with self._lock:
                        lx, ly = self._pos
                    threading.Thread(target=self._on_move_end,
                                     args=(int(lx), int(ly)), daemon=True).start()
                    self._mouse_state = _MOUSE_IDLE
                elif cur is not None and self._down_screen is not None:
                    # 按住中 → 跟随光标重定位（logical = base + Δ/scale）。
                    scale = self._scale()
                    base = self._down_loc
                    dx = cur.X - self._down_screen.X
                    dy = cur.Y - self._down_screen.Y
                    with self._lock:
                        self._pos = ((base[0] + dx) / scale,
                                     (base[1] + dy) / scale)
                    self._present(reposition=True, redraw=False)
                    # v0.165f：拖动中实时通知侧栏跟随（节流 ~20fps），不再等
                    # 松手才跳。回调在新线程跑（UI 线程不碰 pool._lock）。
                    if self._on_move is not None:
                        now = time.monotonic()
                        if now - self._last_follow_ts >= _FOLLOW_MS / 1000.0:
                            self._last_follow_ts = now
                            with self._lock:
                                lx, ly = self._pos
                            threading.Thread(target=self._invoke_move,
                                             args=(int(lx), int(ly)),
                                             daemon=True).start()
        except Exception:
            _logger.debug("ball mouse_tick failed", exc_info=True)

    def _cursor_pos(self):
        try:
            return self._System["Cursor"].Position
        except Exception:
            return None

    def _invoke_click(self) -> None:
        """在独立线程里跑 on_click，捕获并记录异常（防回调静默失败）。"""
        try:
            self._on_click()
            _logger.info("ball on_click callback returned OK")
        except Exception:
            _logger.exception("ball on_click callback raised")

    def _invoke_move(self, x: int, y: int) -> None:
        """在独立线程里跑 on_move（拖动中实时跟随），捕获并记录异常。"""
        try:
            self._on_move(int(x), int(y))
        except Exception:
            _logger.exception("ball on_move callback raised")

    def _apply_mode(self) -> None:
        if self._form is None:
            _logger.info("ball _apply_mode no form yet")
            return
        with self._lock:
            mode = self._mode
        _logger.info("ball _apply_mode -> %s", mode)
        if mode == "idle":
            self._timer_stop()
        else:
            self._timer_start()
        self._present(reposition=True, redraw=True)

    def _apply_topmost(self) -> None:
        """v0.170：UI 线程切换置顶。f.TopMost = True/False 由 WinForms 内部走
        SetWindowPos(HWND_TOPMOST/NOTOPMOST)，同时维护 WS_EX_TOPMOST，无需
        手动 SetWindowLongPtrW。置顶与否不影响 UpdateLayeredWindow 定位。"""
        if self._form is None:
            _logger.info("ball _apply_topmost no form yet")
            return
        try:
            self._form.TopMost = bool(self._topmost)
            # v0.176：保活恒跑（_create_and_show 已 start），_topkeep_tick 内
            # 按 _topmost 分分支 —— 置顶压 band 最顶；非置顶压侧栏到球下方。
            _logger.info("ball apply_topmost -> %s", bool(self._topmost))
        except Exception:
            _logger.debug("ball apply_topmost failed", exc_info=True)

    # v0.170：Z 序保活 —— 周期把球提到 TopMost band 最顶。侧栏同样置顶后，
    # pywebview 每次 resize/move/show 用 SetWindowPos(SWP_SHOWWINDOW) →
    # hWndInsertAfter=NULL(=HWND_TOP) 会把侧栏提到 TopMost band 顶部，盖过
    # 球（球 WS_EX_NOACTIVATE，从不被激活，Z 序永久落后）。本保活每 300ms
    # 把球 SetWindowPos(HWND_TOPMOST) 压回 band 最顶 —— 球 > 侧栏 > 其它窗口。
    def _topkeep_start(self) -> None:
        if self._topkeep_timer is not None:
            return
        try:
            Timer = self._System["Timer"]
            t = Timer()
            t.Interval = _TOP_KEEP_MS
            t.Tick += self._topkeep_tick
            t.Start()
            self._topkeep_timer = t
            _logger.info("ball top-keep started")
        except Exception:
            _logger.debug("ball topkeep start failed", exc_info=True)

    def _topkeep_stop(self) -> None:
        t = self._topkeep_timer
        self._topkeep_timer = None
        if t is not None:
            try:
                t.Stop()
                t.Dispose()
            except Exception:
                _logger.debug("ball topkeep stop failed", exc_info=True)

    def _topkeep_tick(self, sender=None, e=None) -> None:
        if self._form is None:
            return
        try:
            h = _handle_int(self._form.Handle)
            if not h:
                return
            u = _load_user32()
            if self._topmost:
                # 置顶：球压 TopMost band 最顶 —— 球 > 侧栏 > 其它窗口。
                u.SetWindowPos(
                    h, _HWND_TOPMOST, 0, 0, 0, 0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
            else:
                # v0.176：非置顶 —— 把侧栏压到球**下方**（SetWindowPos(侧栏,
                # 球) = 侧栏紧跟球之下），实现「球恒在侧栏之上」，且不提升球
                # 盖过其它普通窗口（SWP_NOACTIVATE 不动焦点）。句柄取不到则
                # 跳过（侧栏 native 未就绪）。
                sh = 0
                try:
                    sh = int(self._sidebar_handle_getter()) if self._sidebar_handle_getter else 0
                except Exception:
                    sh = 0
                if sh and sh != h:
                    u.SetWindowPos(
                        sh, h, 0, 0, 0, 0,
                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        except Exception:
            _logger.debug("ball topkeep failed", exc_info=True)

    def _timer_start(self) -> None:
        if self._timer is not None:
            return
        try:
            Timer = self._System["Timer"]
            t = Timer()
            t.Interval = _ANIM_MS
            t.Tick += self._tick
            t.Start()
            self._timer = t
        except Exception:
            _logger.debug("ball timer start failed", exc_info=True)

    def _timer_stop(self) -> None:
        t = self._timer
        self._timer = None
        if t is not None:
            try:
                t.Stop()
                t.Dispose()
            except Exception:
                _logger.debug("ball timer stop failed", exc_info=True)

    def _tick(self, sender=None, e=None) -> None:
        with self._lock:
            self._frame += 1
        self._present(reposition=False, redraw=True)
