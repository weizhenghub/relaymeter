"""v0.183：悬浮球 —— Electron 后端（替换 pywebview 幽灵球 BallLayer2）。

背景：v0.179 的 BallLayer2 用 pywebview 色键透明（transparent=True + 窗体
BackColor=TransparencyKey=#010203）。这种透明**没有 hit-test 控制** —— OS 把
透明像素上的点击路由到下层窗口，pywebview 无法「球内吞点击」。结果：点球会
穿透到后面窗口（用户反馈的透传 bug）。Electron 的 transparent:true 窗口 +
setIgnoreMouseEvents(bool,{forward:true}) 提供真正的 hit-test：光标在球上 →
吞点击（渲染进程收到 mousedown），在球外 → 穿透。本层把后端换成
ElectronBallWindow（TCP loopback JSON-RPC 驱动 electron_app/ball_main.js）。

替换点（与 BallLayer2 公开 API 完全一致，panel_pool 零改动复用）：
  start/destroy/show/hide/move_to/set_state/set_topmost + available/position
  + 构造参数 ball_size/theme_bg/on_click/on_move_end/on_move/topmost/api_handler。
  main_native/scale_getter/sidebar_handle_getter 不再需要 —— 坐标/置顶/拖拽全
  在 Electron 侧（ball_main.js：拖动 = drag-start 后 12ms 光标轮询 + setPosition，
  点击 = drag-end 无位移 → TCP clicked 事件；置顶 = setAlwaysOnTop）。

交互分工（关键）：
* 点击：渲染进程 ghost_ball.html 的 mousedown（球内圆命中）→ ballDrag.start()
  → 主进程轮询 → mouseup 无位移 → TCP `event("clicked")` → 本层 on_click()
  → pool.ball_clicked() 切 S1<->S2。
* 拖动：主进程 tickDrag 实时 setPosition + `event("moved")` → 本层 on_move()
  节流跟随侧栏；松开 → `event("dragend")` → on_move_end() 落盘位置。
* 穿透：ball_main.js 的 hover 轮询（25ms）按光标在窗口矩形内/外自动切
  setIgnoreMouseEvents —— 球内吞点击（不穿透下层窗口），球外穿透。这正是
  修复用户「点击透传」的核心，不再依赖 pywebview 色键。

线程模型：所有公开方法线程安全（ElectronBallWindow 内部 _call 有 3s 连接
守卫 + 8s 应答超时，可被 panel_pool 的 op-worker / 任意线程调用）。任何原生
失败只记录日志并置 available=False —— 球静默降级为「无」，绝不做成「错看」
的方窗/白底（宁可无球，不要错球）。
"""

from __future__ import annotations

import logging
import threading

_logger = logging.getLogger(__name__)


class BallLayer3:
    """Electron 悬浮球。公开 API 与 BallLayer2 完全一致（panel_pool 只认这组
    方法/回调，换 import 即可切换实现）。

    透明 + 点击穿透由 Electron 侧 transparent:true + setIgnoreMouseEvents
    处理（见 electron_app/ball_main.js）—— 真正的桌面真透 + 球内吞点击。
    """

    def __init__(self, *, ball_size, theme_bg, on_click, on_move_end,
                 on_move=None, topmost=True, api_handler=None, **kwargs):
        # BallLayer2 的 main_native/scale_getter/sidebar_handle_getter 等参数
        # 经 **kwargs 吞掉（签名兼容，Electron 侧不需要）。
        self._ball_size = max(24, int(ball_size))
        self._theme_bg = theme_bg or "#1e1e2e"
        self._on_click = on_click                # () -> None
        self._on_move_end = on_move_end          # (logical_x, logical_y) -> None
        self._on_move = on_move                  # (logical_x, logical_y) -> None（拖动中节流）
        self._topmost = bool(topmost)
        self._api_handler = api_handler

        self._lock = threading.RLock()
        self._win = None                         # ElectronBallWindow
        self._pos = (0, 0)                       # logical
        self._mode = "idle"
        self._visible = False
        self._available = False
        self._destroyed = False

    # ------------------------------------------------------------------
    # 线程安全公开 API（与 BallLayer2 同名同签）
    # ------------------------------------------------------------------
    def start(self, logical_x: int, logical_y: int) -> None:
        with self._lock:
            if self._win is not None or self._destroyed:
                return
            from pathlib import Path
            from .electron_ball_window import ElectronBallWindow
            self._pos = (int(logical_x), int(logical_y))
            # 渲染层：ghost_ball.html（纯幽灵球 + 拖拽接线）。同层 web 目录
            # 的 file:// URL（与 gui.py 的 float_ball_url / BallLayer2 一致）。
            _web_dir = Path(__file__).resolve().parent / "web"
            win = ElectronBallWindow(
                url=(_web_dir / "ghost_ball.html").as_uri(),
                width=self._ball_size,
                height=self._ball_size,
                api_handler=self._api_handler,
            )
            self._win = win
        try:
            win.start()
        except Exception:
            _logger.exception("ball3 start failed")
            with self._lock:
                self._win = None
                self._available = False
            return
        # 事件接线（TCP event -> 回调，线程安全）。
        try:
            win.events.loaded += self._on_loaded
            win.events.clicked += self._on_clicked
            win.events.moved += self._on_moved
            win.events.dragend += self._on_dragend
            win.events.closing += self._on_closing
        except Exception:
            _logger.debug("ball3 event hook failed", exc_info=True)
        # 定位 + 显示（初始在目标位置；_call 有 3s 连接守卫，连不上会抛，
        # 由 _on_loaded 补位 —— 见 ball_layer2 的 _ball_pending 等价逻辑）。
        try:
            win.move(int(logical_x), int(logical_y))
        except Exception:
            _logger.debug("ball3 initial move deferred", exc_info=True)
        try:
            win.set_topmost(self._topmost)
        except Exception:
            _logger.debug("ball3 initial topmost deferred", exc_info=True)
        try:
            win.show()
        except Exception:
            _logger.debug("ball3 initial show deferred", exc_info=True)
        _logger.info("ball3 start dispatched at %s,%s size=%s", logical_x, logical_y, self._ball_size)

    def destroy(self) -> None:
        with self._lock:
            self._destroyed = True
            win = self._win
            self._win = None
            self._available = False
        if win is not None:
            try:
                win.destroy()
            except Exception:
                _logger.debug("ball3 destroy window failed", exc_info=True)

    def show(self) -> None:
        with self._lock:
            self._visible = True
            win = self._win
        if win is not None:
            try:
                win.show()
            except Exception:
                _logger.debug("ball3 show failed", exc_info=True)

    def hide(self) -> None:
        with self._lock:
            self._visible = False
            win = self._win
        if win is not None:
            try:
                win.hide()
            except Exception:
                _logger.debug("ball3 hide failed", exc_info=True)

    def move_to(self, logical_x: int, logical_y: int) -> None:
        with self._lock:
            self._pos = (int(logical_x), int(logical_y))
            win = self._win
        if win is not None:
            try:
                win.move(int(logical_x), int(logical_y))
            except Exception:
                _logger.debug("ball3 move_to failed", exc_info=True)

    def set_state(self, mode: str) -> None:
        if mode not in ("idle", "flow", "s2"):
            mode = "idle"
        with self._lock:
            if self._mode == mode:
                return
            self._mode = mode
            win = self._win
        _logger.info("ball3 set_state -> %s", mode)
        if win is not None:
            try:
                win.evaluate_js(
                    f"window.ballSetState && window.ballSetState({mode!r});"
                )
            except Exception:
                _logger.debug("ball3 set_state eval failed", exc_info=True)

    def set_topmost(self, topmost: bool) -> None:
        topmost = bool(topmost)
        with self._lock:
            if self._topmost == topmost:
                return
            self._topmost = topmost
            win = self._win
        _logger.info("ball3 set_topmost -> %s", topmost)
        if win is not None:
            try:
                win.set_topmost(topmost)
            except Exception:
                _logger.debug("ball3 set_topmost failed", exc_info=True)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def position(self) -> tuple[int, int]:
        with self._lock:
            return self._pos

    # ------------------------------------------------------------------
    # Electron 事件回调（TCP event -> 上层回调）
    # ------------------------------------------------------------------
    def _on_loaded(self, *args) -> None:
        """窗口加载完成 —— 真正可用。补推当前状态 + 位置。"""
        with self._lock:
            self._available = True
            win = self._win
            mode = self._mode
        _logger.info("ball3 loaded -> available")
        if win is not None:
            try:
                win.evaluate_js(
                    f"window.ballSetState && window.ballSetState({mode!r});"
                )
            except Exception:
                _logger.debug("ball3 loaded state push failed", exc_info=True)

    def _on_clicked(self, *args) -> None:
        """drag-end 无位移 → 点击。切 S1<->S2（侧栏收起/展开）。"""
        try:
            self._on_click()
        except Exception:
            _logger.debug("ball3 on_click callback raised", exc_info=True)

    def _on_moved(self, x, y, *args) -> None:
        """拖动中（主进程 tickDrag 实时发）—— 节流跟随侧栏。"""
        x, y = int(x), int(y)
        with self._lock:
            self._pos = (x, y)
        if self._on_move is not None:
            try:
                self._on_move(x, y)
            except Exception:
                _logger.debug("ball3 on_move callback raised", exc_info=True)

    def _on_dragend(self, x, y, *args) -> None:
        """拖动结束 —— 位置落盘。"""
        x, y = int(x), int(y)
        with self._lock:
            self._pos = (x, y)
        try:
            self._on_move_end(x, y)
        except Exception:
            _logger.debug("ball3 on_move_end callback raised", exc_info=True)

    def _on_closing(self, *args) -> None:
        with self._lock:
            self._win = None
            self._available = False
