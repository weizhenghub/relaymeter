"""v0.109：实时栏（live panel）—— 单窗口「主栏区 + 网格区」合并模型。

背景：
    v0.104–106 用 cascade 弹出实现并发（每个并发请求一个独立 WebView2
    窗口），观感差且一直做不好（重叠 / 漂移 / 隐藏不 paint / 泄漏 / 卡死）。
    v0.108 改成 always_one + 1 个独立网格窗口 —— 功能对，但两个窗口之间
    有视觉间隔（实际是两个 OS 窗口，用户要求合并）。

    v0.109 用户拍板：**永远只有 1 个 OS 窗口**。
      * 窗口内 = 主栏区（panel_width，左）+ 网格区（右，仅并发时出现）。
      * 空闲 / 单请求 → 窗口 panel_width 宽，只显示主栏区（全部内容）。
      * 并发 ≥2 → 窗口宽度向右侧延长到 2×panel_width，右侧出现网格区，
        并发请求②③④… 各占内部一个「流块」（思考块 / 正文块），纵向堆叠。
        请求只产生哪种流就只建哪种块；1 请求最多 2 块（思考+正文分开）。
      * done → 块保留 destroy_after_done_sec 秒（可读）后清除；全部清空
        → 窗口收窄回 panel_width，网格区隐藏（其余块左移补位，flex 紧凑）。
      * 两侧样式与 v0.108 完全一致 —— 只是共享同一个 OS 窗口。

线程模型：
    全部 GUI 操作（create/move/show/hide/destroy/resize）走 worker 队列
    非阻塞派发（v0.106）—— SSE 消费线程绝不阻塞在 WebView2 调用上。

依赖：
    pywebview（webview.create_window / move / show / hide / destroy / resize）。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

_logger = logging.getLogger("relay.gui.panel_pool")

# v0.209：球拖动事件静默多久算「dragend 丢失」→ 自动解冻几何。拖动期间
# ball_main 每 12ms 发一个 moved，正常远小于本阈值；事件全停（鼠标被系统
# 抢走 / 窗口被隐藏 / IPC 断）才触发，作为防死锁兜底。
_BALL_DRAG_STALE_SEC = 1.0


def _ensure_pool_file_log() -> None:
    """把 pool 的球相关 INFO 也写进 ball-debug.log（GUI 无 --diag 时默认丢）。"""
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
        _logger._ball_file_ready = True
    except Exception:
        pass


_ensure_pool_file_log()

if TYPE_CHECKING:
    import webview


class PanelPool:
    """并发实时栏池。生命周期跟主 App 同步（启动预建 always_one → 退出销毁）。"""

    def __init__(
        self,
        *,
        api,                                # gui.Api 实例（js_api）
        url: str,                           # live_panel.html 的 file:// URL
        theme_bg: str,                      # 背景色（hex 字符串）
        panel_width: int = 420,
        panel_height: int = 900,
        dock_getter: Callable[[], tuple[int, int, int, int]] | None = None,
        # dock_getter 返回主窗 (x, y, w, h)。用于贴右。
        screen_getter: Callable[[], tuple[int, int]] | None = None,
        # v0.136：docked_getter 返回侧栏当前是否处于 dock（贴右）状态。
        # True → 位置/高度跟随主窗；False（用户拖离磁吸区，浮动）→ 只改
        # 宽度（延展/收窄），不把窗口拽回磁吸位置。缺省按 True 处理（dock）。
        docked_getter: Callable[[], bool] | None = None,
        # v0.130：screen_getter 返回屏幕工作区 (w, h)。用于把自动延展窗口宽度
        # clamp 到屏幕范围内（无则用 webview.screen 兜底，仍无则 6 列上限）。
        destroy_after_done_sec: int = 10,
        # v0.111：阶段感知 stale 阈值（秒）—— 从 GUI 设置注入，可在设置页
        # 改。默认 wait=90 / thinking=60 / gap=20 / text=10。
        stale_wait_secs: float = 90.0,
        stale_thinking_secs: float = 60.0,
        stale_gap_secs: float = 20.0,
        stale_text_secs: float = 10.0,
        # v0.184：容器窗口 URL（ghost_panel.html，悬浮球 + 侧栏一体渲染层）。
        # 缺省回退 self._url（live_panel.html，非透明、无球帽，仅兜底）。
        # ball_size 是球帽边长（px），= 容器收起时窗口边长。
        float_ball_url: Optional[str] = None,
        ball_size: int = 56,
    ) -> None:
        self._api = api
        self._url = url
        self._theme_bg = theme_bg
        self._panel_width = panel_width
        self._panel_height = panel_height
        self._dock_getter = dock_getter
        self._screen_getter = screen_getter
        self._docked_getter = docked_getter
        self._float_ball_url = float_ball_url
        self._ball_size = max(24, int(ball_size))
        self._destroy_after_done_sec = max(1, int(destroy_after_done_sec))
        # v0.134：done-clear 超时从 GUI 设置注入（设置页可调），覆盖构造参数。
        _st = self._settings()
        if _st is not None:
            self._destroy_after_done_sec = max(
                1, int(getattr(_st, "relay_live_panel_done_clear_timeout", self._destroy_after_done_sec) or self._destroy_after_done_sec))

        # v0.130：统一 rid 模型 —— 不再区分 always/grid 槽。所有 active 请求
        # 都在同一窗口内，前端按 rid 动态建容器（thinkstream/puretext/tool）。
        #   _rids          —— 当前持有容器的 rid 集合（到达序由前端 _ridOrder 管）。
        #   always_one_*   —— 只保留窗口显隐状态（visibility 不随 rid 分槽）。
        self.always_one_window: Optional["webview.Window"] = None
        self.always_one_visible: bool = False
        self.always_one_manually_hidden: bool = False
        # v0.130：统一模型下 rid 不再分槽，always_one_rid 恒为 ""（保留属性
        # 兼容 toggle_all_visible / hide_panel 里的旧判断，等价于不成立）。
        self.always_one_rid: str = ""
        self._rids: set[str] = set()

        # v0.130：自动延展（宽度）状态 —— 前端布局后上报列数，Python 决定窗口宽。
        #   _auto_extend   —— 开关（设置页「自动延展侧栏」，启动时从 settings 读）。
        #   _js_width_cols —— 前端最近上报的列数（≥1）。
        #   _max_cols      —— 列数上限（按屏幕工作区余宽算，兜底 ≤6）。
        self._auto_extend = True
        _st = self._settings()
        if _st is not None:
            self._auto_extend = bool(getattr(_st, "relay_gui_live_panel_auto_extend", True))
        self._js_width_cols: int = 1
        self._max_cols: int = 6

        # 全局"一键全部隐藏"标志（不影响 settings，只动可见性）。
        self._all_hidden: bool = False

        # v0.109：窗口当前几何缓存 —— 用于 resize 只在宽高变化时执行
        #（避免每次 _relayout 都触发一次 native resize 抖动）。
        self._last_panel_w: int = 0
        self._last_panel_h: int = 0
        # v0.145：宽度补间动画序号 —— 新一轮动画/宽度再变化时 seq+1，旧动画
        # 线程检测到序号不符即静默退出（新线程从当前实际宽起步覆盖）。
        self._width_anim_seq: int = 0
        # v0.184.2：补间动画在飞标志 —— 收起时球帽翻转延迟到动画结束（串行动画
        # 顺序），期间重入 _apply_geometry（主窗 move/resize 触发）不得提前 flush。
        self._resize_anim_active: bool = False
        # v0.184.2：延迟球帽翻转 —— 展开→收起（S2 或流清空）时先缩回球帽，
        # 动画结束后再 ghostSetState（_flush_deferred_ball_state 落地）。
        self._deferred_ball_state: Optional[str] = None
        # v0.198.1：延迟收起渲染 —— 收起（S2 / 流清空）时**先**把窗口缩成球帽，
        # 动画结束后再 ghostSetExpanded(false) + set_expanded(false)。否则收起动画
        # 一开始就 ghostSetExpanded(false) 会触发 CSS `body.collapsed #ghost-cap
        # { flex: 0 0 100% }`，把球帽 SVG 拉满整个侧栏窗口 —— 出现"球先放大到
        # 侧栏大小再收缩"。动画期间保持展开布局（球帽恒 56px 左上、面板被窗口
        # 收缩自然压没），由 _flush_deferred_render 统一落地。
        self._deferred_expanded_render: Optional[bool] = None
        # v0.136：浮动位置缓存 —— 侧栏被拖离磁吸区后（docked_getter=False），
        # 隐藏再显示时需要恢复到用户拖到的位置而不是(-32000,-32000)屏外。
        self._float_pos: Optional[tuple[int, int]] = None

        # v0.184：悬浮球 + 侧栏合并为**单容器窗口**（ElectronBallWindow +
        # ghost_panel.html，见 electron_ball_window.py / electron_app/ball_main.js）。
        # 容器本身即球锚点：
        #   _float_ball_enabled —— 悬浮球开关。开=球模式（容器收起=球帽方、
        #                          展开=球帽+侧栏）；关=磁吸模式（容器 dock
        #                          贴主窗右缘、球帽隐藏）。
        #   _ball_pos           —— 球帽锚点 (x, y)（= 容器收起时左上角；
        #                          展开时球帽恒容器左上角）。
        #   _show_sidebar_on_stream —— v0.183 还原：点球帽循环切换 S1↔S2。
        #     True=S1（启动侧边栏显示，ghost-flow）：有请求流来 → 球帽→flow +
        #          自动展开容器成「球帽+侧栏」；请求流清空后收回球帽。
        #     False=S2（始终隐藏侧边栏，ghost-done）：有请求流来 → 球帽→done，
        #          **不**主动展开；用户可手动点面板 surface 内的 X/按钮强制收起。
        #   _expanded           —— 容器当前几何态（True=展开成球帽+侧栏 / False=
        #                         收回球帽）。由 _show_sidebar_on_stream + 当前
        #                         是否有 rid 综合驱动；manual（用户手动强制）覆
        #                         盖后保持直到下一个相反条件。
        self._float_ball_enabled: bool = False
        self._ball_pos: Optional[tuple[int, int]] = None
        self._show_sidebar_on_stream: bool = True  # S1 默认
        self._expanded: bool = False
        # v0.205：引导教程侧栏聚焦 —— 教程阶段在容器左侧临时开一条引导带
        # （指引卡片悬浮在悬浮球左侧）。非 0 时容器几何左扩并加宽；教程
        # 结束归零，几何完全复原。只影响教程演示，不影响真实请求布局。
        self._tour_guide_width: int = 0
        # v0.208：教程收起演示专用态 —— 收起时只隐藏面板 surface + 窗口缩到
        # 「引导带+球帽」，**球帽保持 56px 不占满窗**（走 ghostSetTourCollapsed，
        # 不用 body.collapsed —— 后者会 `#ghost-cap{width:100%}` 把球帽拉成整窗
        # 大球，展开时出现"球先变大再展开"）。教程结束复原。
        self._tour_collapsed: bool = False
        # v0.170：悬浮球置顶（与侧栏同层级）。默认 True（沿用 v0.165 行为），
        # 由设置页「悬浮球置顶」开关控制；改时同步球 + 侧栏（gui._sync_panel_topmost）。
        self._float_ball_topmost: bool = True
        _st = self._settings()
        if _st is not None:
            self._float_ball_enabled = bool(getattr(_st, "relay_gui_float_ball", False))
            self._float_ball_topmost = bool(getattr(_st, "relay_gui_float_ball_topmost", True))
            _bx = int(getattr(_st, "relay_gui_float_ball_x", -32000) or -32000)
            _by = int(getattr(_st, "relay_gui_float_ball_y", -32000) or -32000)
            if _bx > -1000 and _by > -1000:
                self._ball_pos = (_bx, _by)

        # done 后延迟清除定时器：rid -> Timer（always_one 不需要，常驻）。
        self._destroy_timers: dict[str, threading.Timer] = {}
        self._lock = threading.RLock()

        # orphan-watchdog 定时器。
        self._watchdog_timer: Optional[threading.Timer] = None
        self._watchdog_stop = threading.Event()

        # v0.167：磁吸「只在松手时落地」—— 拖动期间只做预览，不真改 dock/float
        # 状态。
        #   _ball_dragging  —— 球被按住拖动期间 True；球松手后置 False。
        #                     期间 _update_panel_snap 直接 return（用户需求
        #                     「拖动悬浮球时不磁吸」），连带抑制面板跟随球
        #                     移动时被 easy_drag 派发 moved 触发的误磁吸。
        #   _panel_dragging —— 侧栏被按住拖动期间 True；moved 事件静默 ~200ms
        #                     后自动置 False（拖动结束的判定：pywebview 无释
        #                     放事件，靠事件间隔兜底）。期间 _update_panel_snap
        #                     把意图缓存到 _pending_snap，不真改状态。
        #   _pending_snap   —— {'x', 'y', 'dock': bool} 或 None。拖动结束
        #                     后由 _apply_pending_snap 一次性落地。
        #   _drag_settle_timer —— 侧栏拖动结束检测（moved 静默定时器）。
        self._ball_dragging: bool = False
        # v0.209：最后一次球拖动事件的时间戳（dragstart / moved 刷新）。拖动
        # 期间面板内容在变（列数变 → 宽度补间）会在 op-worker 上继续跑几何，
        # 与 ball_main 的 setPosition 互相拉扯 → 卡顿漂移。冻结期间靠本时间戳
        # 自愈：超过 _BALL_DRAG_STALE_SEC 没有新事件 = dragend 丢失，自动解冻，
        # 免得几何被永久冻结（侧栏再也收不回来）。
        self._ball_drag_ts: float = 0.0
        self._panel_dragging: bool = False
        self._pending_snap: Optional[dict] = None
        self._drag_settle_timer: Optional[threading.Timer] = None

        # 池已 ready 标志（启动完成）
        self._ready = False

        # v0.106：窗口操作 worker —— 解决"某个 WebView2 窗口操作卡死 →
        # SSE 消费线程永久阻塞 → 所有窗口泄漏"的问题。窗口操作全部丢进单
        # worker 队列（fire-and-forget，非阻塞），SSE 消费线程只碰锁 + 状态
        # + 入队。worker 若被某条操作卡死，watchdog 检测后丢弃旧 worker 换新。
        self._op_queue: "queue.Queue" = queue.Queue(maxsize=512)
        self._op_worker: Optional[threading.Thread] = None
        self._op_progress = time.monotonic()
        self._worker_stale_secs = 60.0
        # 每个 rid 最近一次收到事件的时间（stale 自愈用）。
        self._last_event: dict[str, float] = {}
        # v0.110：**阶段感知** stale 阈值。delta 是累积文本，只要流在推进
        # （思考/正文出字、或上游心跳 chunk）就会不断刷新 _last_event；
        # 完全无事件超过对应阶段阈值 → 判定连接中断（上游中途截断不广播
        # done）。到点后 watchdog 推「超时 done」给前端（主栏 badge 变出错、
        # 网格块清除收窄窗口）—— 兜底"一直流式中 / 窗口清不掉"。
        #
        # v0.111 四档（按 rid 当前阶段选阈值，_stage 见 _note_stage）：
        #   wait（思考等待）—— 无任何字：90s。请求刚发出去还没出字，
        #     慢上游/排队可能沉默较久，最宽容。
        #   thinking（思考出字中）—— thinking 有、正文无：60s。长思考可能
        #     间歇停顿，但仍给 60s 兜底。
        #   gap（思考→正文衔接）—— v0.111 新增：收到思考完成信号
        #     （thinking_done，思考块 content_block_stop / </think>）但正文
        #     还没出：20s。思考一旦结束，之后的静默就是衔接卡死而非思考暂停，
        #     可以收紧。**无 thinking_done 信号的上游（如纯 reasoning_content
        #     流）退化为 thinking 档** —— 与 v0.110 的合并行为一致。
        #   text（正文出字中）—— 正文已有：10s。正文流一旦开始通常稳定
        #     输出，停 10s 基本可判断了，快速回收。
        self._stale_wait_secs = max(1.0, float(stale_wait_secs))
        self._stale_thinking_secs = max(1.0, float(stale_thinking_secs))
        self._stale_gap_secs = max(1.0, float(stale_gap_secs))
        self._stale_text_secs = max(1.0, float(stale_text_secs))
        # rid -> 当前阶段：""（未定/思考等待）| "thinking" | "gap" | "text"。
        # 由 push_event 累积文本 + thinking_done 信号单向推进
        # （wait→thinking→gap→text，不回退）。
        self._stage: dict[str, str] = {}

    # ------------------------------------------------------------------
    # v0.106：窗口操作 worker
    # ------------------------------------------------------------------
    def _enqueue_op(self, fn: Callable[[], None]) -> None:
        """把窗口操作入队（非阻塞，满了丢弃 —— 宁可掉帧也不要阻塞调用方）。
        fn 必须不碰 pool 的 _lock（worker 线程不持有它）。"""
        try:
            self._op_queue.put_nowait(fn)
        except Exception:
            pass  # 队列满 / worker 未启动 → 丢

    def _ensure_op_worker(self) -> None:
        if self._op_worker is not None and self._op_worker.is_alive():
            return
        def _loop():
            while True:
                try:
                    fn = self._op_queue.get(timeout=30.0)
                except Exception:
                    continue
                try:
                    fn()
                except Exception:
                    _logger.debug("window op failed", exc_info=True)
                finally:
                    self._op_progress = time.monotonic()
        t = threading.Thread(target=_loop, name="panel-pool-op-worker", daemon=True)
        t.start()
        self._op_worker = t

    # ------------------------------------------------------------------
    # 启动：预建 always_one（grid 懒创建，见 assign）
    # ------------------------------------------------------------------
    def start(self, max_windows: int) -> None:
        """启动池：建容器窗口（悬浮球 + 侧栏一体，见 assign / ball_clicked）。"""
        with self._lock:
            try:
                # v0.184：**单容器窗口** —— 悬浮球 + 侧栏合并（ElectronBallWindow
                # + ghost_panel.html + electron_app/ball_main.js）。native 恒 None
                # → WinForms 互操作路径安全 no-op。transparent:true + hover 穿透
                # + drag 状态机都在 Electron 主进程；容器收起=球帽方、展开=球帽+
                # 侧栏、磁吸 dock 贴主窗右缘。api_handler=self._api（gui.Api 实例）
                # —— ghost_panel.html 内嵌 live_panel.js 的 9 个侧栏上行都在它上面。
                from .electron_ball_window import ElectronBallWindow
                w = ElectronBallWindow(
                    url=(self._float_ball_url or self._url),
                    width=self._ball_size,
                    height=self._ball_size,
                    api_handler=self._api,
                )
                w.start()
                self.always_one_window = w
                try:
                    w.events.closing += self._on_always_one_closing
                except Exception:
                    _logger.debug("always_one closing hook failed", exc_info=True)
                try:
                    # 球帽点击（drag-end 无位移）→ 切收起/展开。
                    w.events.clicked += self.ball_clicked
                    # 容器拖动：dragstart 立刻冻结几何；moved 实时更新球位；
                    # dragend 落盘 settings。
                    w.events.dragstart += self.begin_ball_drag
                    w.events.moved += self._ball_drag
                    w.events.dragend += self._persist_ball_pos
                except Exception:
                    _logger.debug("container drag hooks failed", exc_info=True)
                # v0.109：单窗口 —— 主栏区 + 网格区都在容器窗口里，网格区仅通过
                # 「窗口宽度向右延长」显隐（_relayout 管理）。
                self._ready = True
                # 重排位置（窗口 dock 到主窗右缘）
                self._relayout()
                # v0.163：恢复「始终开启」开关控制启动显隐。v0.125 曾无条件
                # 展开侧栏（验证思考流容器），用户新需求明确：无请求不显示。
                #   ON → 启动即显示（持久面板）；
                #   OFF → 不显示，首个请求流到达时由 assign() 弹出。
                # v0.166：gate 收紧为 _should_show_on_startup —— 必须主开关 ON
                # + 磁吸 + always_one 才启动强显（原来用 _should_keep_idle_open
                # 漏了主开关，且悬浮球模式下也强显，用户反馈「没开实时流侧栏
                # 侧栏却在」）。
                # 注意：ON 时即使这里因几何未知 show 失败，_on_panel_loaded
                # 也会补 dock+show。
                if self._should_show_on_startup() and not self.always_one_manually_hidden:
                    self._show_always_one()
                # 启动窗口操作 worker（非阻塞派发）
                self._ensure_op_worker()
                # v0.165e：球可见性统一走 refresh_ball_visibility（磁吸/悬浮球
                # 互斥 + 主开关 gate）。失败静默 —— 球只是锚点增强。
                self.refresh_ball_visibility()
                # 启动 orphan-watchdog（30s 一次扫 stale grid / always_one）
                self._watchdog_stop.clear()
                self._schedule_watchdog()
            except Exception:
                _logger.exception("PanelPool.start failed")
                # 失败时不抛 —— GUI 仍能跑，只是并发功能缺失。

    # v0.203：设置页「实时流侧栏」开关切换时重载容器渲染层 —— Electron
    # webContents.reload 重新加载 ghost_panel.html（HTML/CSS 改动：红叉、
    # 命中区等即时生效），不重建窗口（always_one_window 引用稳定，测试与
    # 运行中 show/hide 行为不受影响）。
    def reload_container(self) -> None:
        """重载 always_one 容器窗口的渲染层（不销毁窗口本体）。"""
        with self._lock:
            w = self.always_one_window
        # 池从未 start（测试环境 / 窗口已关）→ 无窗可重载，静默 no-op。
        if w is None:
            return
        try:
            w.reload()
            _logger.info("reload_container reloaded always_one renderer")
        except Exception:
            _logger.debug("reload_container reload failed", exc_info=True)

    def stop(self) -> None:
        """退出：取消所有 timer，destroy 所有窗口。"""
        with self._lock:
            self._watchdog_stop.set()
            if self._watchdog_timer is not None:
                try:
                    self._watchdog_timer.cancel()
                except Exception:
                    pass
                self._watchdog_timer = None
            for t in self._destroy_timers.values():
                try:
                    t.cancel()
                except Exception:
                    pass
            self._destroy_timers.clear()
            # 清掉 op 队列，worker 线程是 daemon，随进程退出。
            try:
                while not self._op_queue.empty():
                    self._op_queue.get_nowait()
            except Exception:
                pass
            if self.always_one_window is not None:
                try:
                    self.always_one_window.destroy()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # v0.165：悬浮球 —— 桌面锚点。侧栏出现时从球位置展开。
    # ------------------------------------------------------------------
    @property
    def ball_mode(self) -> bool:
        """球模式生效：开关开。容器即球 —— 球模式收起=球帽锚点，磁吸=dock。"""
        return self._float_ball_enabled

    def _default_ball_pos(self) -> tuple[int, int]:
        """球默认位置：主窗的**右上角**（球锚在主窗内，v0.195 由用户指定）。

        之前版本是主窗右缘**外侧**（mx + mw + 8, my + 8），挂在主窗外面
        像独立悬浮图标；现在改主窗内右上角（mx + mw - 球宽 - 8, my + 8），
        让球在主窗口自己的几何内出现，与主窗视觉融为一体。偏移 8px
        留出边框呼吸感，与原外侧 8px 同款。

        几何未知时回 (300, 200) 兜底（同旧版）。持久化位置（用户拖动
        后）走的是 _ball_pos 路径，不经过本函数 —— 不影响已拖过的用户。
        """
        try:
            if self._dock_getter is not None:
                mx, my, mw, mh = self._dock_getter()
                if mw > 0 and mh > 0:
                    return (mx + mw - self._ball_size - 8, my + 8)
        except Exception:
            pass
        return (300, 200)

    def _set_ball_visible(self, on: bool) -> None:
        """球模式：球帽可见；磁吸：球帽隐藏（ghostSetBallVisible → body.no-ball）。"""
        w = self.always_one_window
        if w is None:
            return
        self._ensure_op_worker()
        self._enqueue_op(
            lambda ww=w, v=bool(on): ww.evaluate_js(
                f"ghostSetBallVisible({str(bool(on)).lower()})"))

    def _apply_expanded_state(self) -> None:
        """把当前 _expanded 落到渲染层 + 主进程 + 几何（同帧下发）：
        ghostSetExpanded（侧栏 surface 显隐）+ set_expanded（hover 停启 /
        吞点击），随后 _relayout 驱动 resize/move。收起/展开过渡由
        _anim_resize 宽高同变补间。

        v0.198.1：**展开立即渲染，收起延迟渲染**。展开（True）面板要立刻
        出来，同帧 ghostSetExpanded(true)；收起（False）把面板隐藏延迟到收起
        动画 on_done（_flush_deferred_render）一起做 —— 否则动画一开始就
        ghostSetExpanded(false) 会让球帽被 CSS 拉满窗，出现"球先放大到侧栏
        大小再收缩"。"""
        w = self.always_one_window
        if w is None:
            return
        expanded = self._expanded
        self._ensure_op_worker()
        if expanded:
            # 展开：立即显示面板 + 停 hover（球帽 + 侧栏同帧出现，动画扩窗）。
            self._deferred_expanded_render = None
            self._enqueue_op(lambda ww=w: ww.evaluate_js("ghostSetExpanded(true)"))
            self._enqueue_op(lambda ww=w: ww.set_expanded(True))
        else:
            # 收起：渲染层延迟到窗口缩成球帽后再落地（见 _flush_deferred_render）。
            # 期间 body 保持展开布局 —— 球帽恒 56px 左上，面板被窗口收缩压没，
            # 不会出现"球帽被拉满窗"。
            self._deferred_expanded_render = False
        self._relayout()

    def set_tour_guide_width(self, width: int) -> None:
        """v0.205：引导教程侧栏聚焦 —— 设教程引导带宽度（容器左扩 px）。

        教程章节2 步骤3 播放时前端上报引导带宽度（非 0 = 展开引导模式，
        容器几何左扩 + 加宽，指引卡片悬浮在悬浮球左侧）；0 = 结束复原。
        只改几何，不动 _expanded —— 教程开始先 _expanded=True 再 set
        guide 宽度，容器从球位展开成「引导带 + 球帽 + 侧栏」。

        渲染层同步：把引导带宽度写成 CSS 变量 + 置 data-tour-guide="1"
        （ghost_panel.html 的 #tour-guide-band 据此撑开，把球帽推到带右）。
        """
        with self._lock:
            w = max(0, int(width or 0))
            if w == self._tour_guide_width:
                return
            self._tour_guide_width = w
            ww = self.always_one_window
            if ww is not None:
                js = (
                    f"document.documentElement.style.setProperty('--tour-guide-w','{w}px');"
                    f"document.getElementById('tour-guide-band')&&"
                    f"document.getElementById('tour-guide-band').setAttribute('data-tour-guide','{1 if w else 0}');"
                )
                self._enqueue_op(lambda wj=js: ww.evaluate_js(wj))
            self._relayout()

    def set_tour_collapsed(self, collapsed: bool) -> dict:
        """v0.208：教程收起/展开演示 —— 只隐藏面板 surface + 窗口缩到
        「引导带+球帽」，**球帽保持 56px 不占满**。

        不用 _apply_expanded_state / body.collapsed —— 后者会在收起 on_done
        时 `ghostSetExpanded(false)` 触发 `body.collapsed #ghost-cap{width:100%}`，
        把球帽拉成整个窗口大球；再展开时球帽从大缩回 56px，观感就是
        「展开时悬浮球先变大再展开」（用户反馈）。本方法走 ghostSetTourCollapsed
        （只隐藏 surface，球帽仍 ball_size），收起期间指引卡留在引导带内可见。
        """
        with self._lock:
            self._tour_collapsed = bool(collapsed)
            if collapsed:
                self._expanded = False
            else:
                self._expanded = True
            w = self.always_one_window
            if w is None:
                return {"ok": False, "available": False}
            self._ensure_op_worker()
            # ⚠ 展开必须同时清 body.collapsed + ghostSetExpanded(true) —— 否则
            # 教程开始前残留的 `body.collapsed #ghost-cap{width:100%}` 会把球帽
            # 拉成整个窗口大球（"悬浮球全程巨大"，用户反馈）。收起只走
            # tour-collapsed（球帽 56px 不占满），不动 body.collapsed。
            if collapsed:
                js = "ghostSetTourCollapsed(true)"
            else:
                js = ("document.body.classList.remove('collapsed'); "
                      "ghostSetExpanded(true); ghostSetTourCollapsed(false)")
            self._enqueue_op(lambda ww=w, j=js: ww.evaluate_js(j))
            self._relayout()   # 按 _tour_collapsed 重算几何（带+球 / 带+球+面板）
            return {"ok": True, "available": True}

    def refresh_ball_visibility(self) -> None:
        """主开关(实时流侧栏)/一键隐藏变化后重算容器显隐。

        v0.184：容器即球 —— 球模式容器可见 iff 主开关开 + 非一键隐藏
        （_ball_should_show）；磁吸模式无球，显隐由 always_one 逻辑管
        （_show_always_one / _hide_always_one）。多线程安全（持锁）。"""
        with self._lock:
            w = self.always_one_window
            if w is None or not self._float_ball_enabled:
                return
            if self._ball_should_show():
                self._set_ball_visible(True)
                if not self.always_one_visible:
                    self._ensure_op_worker()
                    self._enqueue_op(lambda ww=w: ww.show())
                    self.always_one_visible = True
                self._apply_geometry(force_apply=True)
            else:
                if self.always_one_visible:
                    self._hide_container()

    def _hide_container(self) -> None:
        """整体隐藏容器（连球帽一起）—— 一键全部隐藏 / 主开关关。"""
        w = self.always_one_window
        if w is None or not self.always_one_visible:
            return
        self._width_anim_seq += 1
        self._resize_anim_active = False  # 隐藏 = 取消在飞动画，标记复位
        self._ensure_op_worker()
        self._enqueue_op(lambda ww=w: ww.hide())
        self._enqueue_op(lambda ww=w: ww.move(-32000, -32000))
        self.always_one_visible = False

    def _persist_ball_pos(self, *args) -> None:
        """容器被拖走（Electron drag-end）→ 记录新位置 + 持久化到 settings。

        v0.184：容器左上角即球帽锚点（收起态窗口=球帽，展开态球帽恒左上）。
        dragend 事件带 (x, y)（ball_main.js 已换算成屏幕点）；缺参时读窗口
        当前位兜底。拖动结束清 _ball_dragging（_apply_geometry 恢复接管）。

        v0.209：解冻放在**最前**、且与位置解析解耦 —— 下面读位失败会走
        早退分支，若解冻留在后面就漏掉，几何得等 1s 过期才恢复（表现为点完
        球帽要愣一下才展开）。dragend 已改为点击也发，所以这里必然跑到。
        """
        if not self._float_ball_enabled:
            return
        with self._lock:
            self._ball_dragging = False
            self._ball_drag_ts = 0.0
            x, y = None, None
            if len(args) >= 2:
                try:
                    x, y = int(args[0]), int(args[1])
                except Exception:
                    x = y = None
            if x is None:
                w = self.always_one_window
                if w is None:
                    return
                try:
                    x, y = int(w.x), int(w.y)
                except Exception:
                    return
            sw, sh = self._screen_size() or (0, 0)
            if sw > 0 and sh > 0:
                x = max(0, min(x, sw - 8))
                y = max(0, min(y, sh - 8))
            self._ball_pos = (x, y)
            s = self._settings()
            if s is not None:
                try:
                    s.relay_gui_float_ball_x = x
                    s.relay_gui_float_ball_y = y
                except Exception:
                    pass
            if self.always_one_visible and not self._all_hidden:
                self._relayout()

    def _drag_freeze_geometry(self) -> bool:
        """v0.209：拖动中 → 几何操作全部让位 ball_main 的 setPosition。

        直接读属性（不取锁）：调用点可能已在持锁路径内，RLock 下虽可重入但
        没必要。带过期判定 —— 事件流断了（dragend 丢失）即自动解冻，否则
        几何被永久冻结、侧栏再也收不回来。
        """
        if not self._ball_dragging:
            return False
        return (time.monotonic() - self._ball_drag_ts) < _BALL_DRAG_STALE_SEC

    def begin_ball_drag(self, *args) -> None:
        """v0.209：球/容器拖拽**开始**（Electron mousedown 广播）→ 立刻冻结几何。

        必须在第一个 moved 事件之前置位。旧实现只在 _ball_drag（首个 ≥4px
        位移的 moved）里置位，于是"按下即拖"的头 4px 内 _apply_geometry 仍会
        执行 —— 若侧栏此刻正好发生容器变化（列数变 → 宽度补间，或队列里残留
        的 move），窗口会被拽回旧球位，与 tickDrag 的 setPosition 互相拉扯，
        表现为拖拽卡顿 + 漂移（用户反馈）。

        同时中止在飞的宽度补间：_anim_resize 会在 ~700ms 内持续 setSize，
        拖动期间窗口被反复 resize 观感同样是卡顿。seq 自增让补间线程下一帧
        退出；因没有后继补间接管，须手工复位 _resize_anim_active，否则
        _apply_geometry 里「补间不在飞才 flush 延迟渲染」的分支永远进不去
        （收起动画的延迟隐藏就落不了地）—— 复位后由拖动结束的 _relayout 补上。
        """
        if not self._float_ball_enabled:
            return
        with self._lock:
            self._ball_dragging = True
            self._ball_drag_ts = time.monotonic()
            if self._resize_anim_active:
                self._width_anim_seq += 1
                self._resize_anim_active = False

    def _ball_drag(self, *args) -> None:
        """拖动容器过程中的实时跟随：更新球位缓存（不落盘，dragend 落盘）。

        v0.184：容器本身由 ball_main.js 轮询光标移动；这里只同步 _ball_pos
        供展开定位，并置 _ball_dragging=True —— 拖动期间 _apply_geometry 见
        此标志直接 return（几何归 ball_main 管，防 _relayout 反扑把容器拽回
        旧球位）。v0.209：同时刷新 _ball_drag_ts（冻结自愈用）；置位本身仍
        保留为兜底，主路径已提前到 dragstart 的 begin_ball_drag。"""
        if not self._float_ball_enabled:
            return
        if len(args) >= 2:
            try:
                x, y = int(args[0]), int(args[1])
            except Exception:
                return
            with self._lock:
                self._ball_dragging = True
                self._ball_drag_ts = time.monotonic()
                self._ball_pos = (int(x), int(y))

    # ------------------------------------------------------------------
    # v0.167：磁吸「只在松手时落地」—— 拖动期间只做预览，不真改 dock/float
    # ------------------------------------------------------------------
    def preview_snap(self, x: int, y: int, would_dock: bool) -> None:
        """侧栏拖动期间缓存磁吸意图。gui._update_panel_snap 在 _panel_dragging
        期间调本方法；松手时（_apply_pending_snap）一次性按最后缓存的意图落地。
        即使多次缓存，只有最后一次生效（每次覆盖）。
        """
        with self._lock:
            self._pending_snap = {"x": int(x), "y": int(y), "dock": bool(would_dock)}

    def apply_pending_snap(self) -> bool:
        """侧栏拖动结束（settle 定时器到点）→ 执行最近一次缓存的磁吸意图。
        同时清 _panel_dragging 防重入；无可应用意图（None）则 no-op。"""
        with self._lock:
            pending = self._pending_snap
            self._pending_snap = None
            self._panel_dragging = False
        if pending is None:
            return False
        # 不在这里直接改 dock/float —— 走 gui 路径（与 _update_panel_snap 一致）
        # 让 gui 重新算主窗几何等。注：调用方（gui 拖动结束 hook）负责传 pending
        # 进 _update_panel_snap 的真路径，本方法只负责状态翻转。
        return True

    def begin_panel_drag(self) -> None:
        """侧栏拖动开始（gui._on_panel_moved 第一次到 + 当前非球拖动）。
        取消 pending_settle 定时器，置 _panel_dragging=True。"""
        with self._lock:
            self._panel_dragging = True
            if self._drag_settle_timer is not None:
                try:
                    self._drag_settle_timer.cancel()
                except Exception:
                    pass
                self._drag_settle_timer = None

    def arm_drag_settle(self, *, after_ms: float = 200.0) -> None:
        """重置/启动侧栏拖动结束定时器。每次 _on_panel_moved 都重新 arm，
        moved 静默 over after_ms（默认 200ms）→ 视为松手 → 落地 pending。"""
        with self._lock:
            if self._drag_settle_timer is not None:
                try:
                    self._drag_settle_timer.cancel()
                except Exception:
                    pass
            t = threading.Timer(after_ms / 1000.0, self._drag_settle)
            t.daemon = True
            self._drag_settle_timer = t
            t.start()

    def _drag_settle(self) -> None:
        """拖动结束定时器到点 —— 应用最后缓存的磁吸意图。"""
        with self._lock:
            # 定时器已 firing，清引用让 arm_drag_settle 下次可起新 timer。
            self._drag_settle_timer = None
        # 通知 gui 把暂存的 pending 落地（gui 持有 snap 执行的入口）。
        cb = getattr(self, "_on_drag_settle", None)
        if cb is not None:
            try:
                cb()
            except Exception:
                _logger.debug("drag settle callback failed", exc_info=True)
        else:
            # 无 gui 回调兜底：直接清标志，pending 留作下次常规 snap 触发。
            with self._lock:
                self._panel_dragging = False
                self._pending_snap = None

    def set_drag_settle_callback(self, cb) -> None:
        """gui 注册一个回调 —— settle 定时器到点调它，把 pending 实际落地。"""
        self._on_drag_settle = cb

    def ball_dragging(self) -> bool:
        """外部（gui._update_panel_snap）查询球是否处于拖动状态 —— True 则不磁吸。

        v0.209：带过期自愈 —— dragend 丢失（鼠标被系统抢走 / 窗口隐藏 /
        IPC 断）时，事件流停 >1s 即解除，否则面板磁吸会永久失效。
        """
        with self._lock:
            if not self._ball_dragging:
                return False
            if (time.monotonic() - self._ball_drag_ts) >= _BALL_DRAG_STALE_SEC:
                self._ball_dragging = False
                return False
            return True

    def panel_dragging(self) -> bool:
        """外部查询侧栏是否处于拖动状态 —— True 则 snap 只缓存不执行。"""
        with self._lock:
            return self._panel_dragging

    def pending_snap(self):
        """外部查询当前暂存意图（用于 settle 时落地）。"""
        with self._lock:
            return self._pending_snap

    def set_float_ball_pos_from_panel(self, x: int, y: int) -> None:
        """球模式：容器被拖/磁吸脱开 → 球位跟随到新左上角，保持「球=锚点」不变式。
        由 gui._update_panel_snap 调，x/y = 容器左上角（logical）。

        v0.184：容器即球 —— 只同步 _ball_pos + 落盘；几何由 _apply_geometry
        球分支接管（无独立球层可 move）。"""
        x, y = int(x), int(y)
        if x < -1000 or y < -1000:
            return
        with self._lock:
            self._ball_pos = (x, y)
            s = self._settings()
            if s is not None:
                try:
                    s.relay_gui_float_ball_x = x
                    s.relay_gui_float_ball_y = y
                except Exception:
                    pass

    def _set_ball_state(self, mode: str) -> None:
        """推球帽形态（idle / flow / s2）给容器渲染层（ghostSetState）。"""
        w = self.always_one_window
        if w is None:
            return
        self._ensure_op_worker()
        self._enqueue_op(lambda ww=w, j=f"ghostSetState('{mode}')": ww.evaluate_js(j))

    def _flush_deferred_render(self) -> None:
        """落地延迟的收起渲染层变化：先隐藏面板（ghostSetExpanded(false) +
        set_expanded(false)），再翻转球帽（ghostSetState）。

        v0.184.2 起作 _anim_resize 的 on_done（收起动画收尾后）以及无动画
        路径（起始即已收起 / 首次几何 / 打断后重入）调用；v0.198.1 扩展为
        同时落地面板隐藏 —— 保证串行动画顺序「先窗口缩成球帽，再隐藏面板
        + 翻转球帽」，不再把球帽拉满窗。"""
        with self._lock:
            expanded_render = self._deferred_expanded_render
            self._deferred_expanded_render = None
            state = self._deferred_ball_state
            self._deferred_ball_state = None
        w = self.always_one_window
        if w is None:
            return
        self._ensure_op_worker()
        if expanded_render is not None:
            on = bool(expanded_render)
            self._enqueue_op(
                lambda ww=w, o=on: ww.evaluate_js(
                    f"ghostSetExpanded({str(o).lower()})"))
            self._enqueue_op(lambda ww=w, o=on: ww.set_expanded(o))
        if state is not None:
            self._set_ball_state(state)

    def ball_clicked(self, *args) -> None:
        """球帽被点击（Electron drag-end 无位移 → clicked 事件）→ 切 S1↔S2。

        v0.184.1 还原 v0.183 语义：点球帽在「启动侧边栏显示」(S1/ghost-flow)
        ↔「始终隐藏侧边栏」(S2/ghost-done) 之间循环；**不直接切展开/收起几何**，
        几何由 _show_sidebar_on_stream + 当前是否有 rid 共同驱动（assign /
        _clear_rid / set_float_ball 各处统一判定）。仅球模式有效。

        行为细节：
          * S1 → S2：若当前有 rid 且 _expanded=True（之前因流展开）—— 先执行
            **收起逆动画**回球帽，动画结束再翻转成 done（串行动画顺序，不两套
            同时做）；否则直接翻转 done。S2 期间来新流只切 done 不展。
          * S2 → S1：球帽→flow；若当前有 rid 则立即展开成「球帽+侧栏」；否则保
            持收回态、等下个 rid 触发自动展开。
        """
        with self._lock:
            if not self._float_ball_enabled:
                return
            if self.always_one_window is None or not self.always_one_visible:
                return
            if self._all_hidden:
                return
            self._show_sidebar_on_stream = not self._show_sidebar_on_stream
            # 球帽形态由 _sync_geometry_to_mode 统一判定（展开→收起时延迟翻转
            # 到收起动画结束后，保证串行动画顺序，不再提前 flip）。
            self._sync_geometry_to_mode()
            _logger.info("ball_clicked → S%s expanded=%s rids=%s",
                         1 if self._show_sidebar_on_stream else 2,
                         self._expanded, bool(self._rids))

    def _sync_geometry_to_mode(self) -> None:
        """按 _show_sidebar_on_stream + _rids 决定 _expanded 当前应有的几何态。

        单一来源真理：assign / _clear_rid / set_float_ball / ball_clicked / 一键
        全部隐藏 收尾 任何「状态变化」点都走这里同步 _expanded + 几何 + 球帽态。

        球帽形态判定：
          S1 + 有流 → flow  + 展开成「球帽+侧栏」（用户反馈"启动侧边栏显示"）
          S1 + 无流 → idle  + 收回球帽（默认安静态）
          S2 + 有流 → done  + 收回球帽（始终隐藏，但球帽倒立反馈"压制中"）
          S2 + 无流 → done  + 收回球帽（球帽仍倒立 —— S2 是用户显式切的偏好，
                                       不该因流消失就回 idle 失去视觉提示）

        一键全部隐藏（_all_hidden=True）→ 球帽 hide + 几何收回，形态不重写
        （球帽看不见了，外部没意义）。"""
        w = self.always_one_window
        if w is None:
            return
        with self._lock:
            prev_expanded = self._expanded
            has_stream = bool(self._rids) and not self._all_hidden
            target_expanded = False
            ball_state: Optional[str] = None
            if self._all_hidden:
                # 一键全藏：球帽已被 hide，几何也收回；不再下发 ghostSetState
                # （重新显示时由 refresh_ball_visibility 决定形态）。
                target_expanded = False
                ball_state = None
            elif self._show_sidebar_on_stream:
                # S1
                if has_stream:
                    target_expanded = True
                    ball_state = "flow"
                else:
                    target_expanded = False
                    ball_state = "idle"
            else:
                # S2：球帽持续保持 done（翻转），不随流来去切换。
                target_expanded = False
                ball_state = "done"
            self._expanded = target_expanded
            # v0.184.2 串行动画：展开→收起（切 S2 / 流清空 / 磁吸转球）时，
            # 球帽翻转延迟到收起补间动画结束后再做 —— 先「侧栏逆动画回球」，
            # 再「翻转球帽」。其余形态（展开 flow / 已收起切 done）仍立即翻转。
            defer_flip = (
                ball_state == "done"
                and prev_expanded
                and not target_expanded
            )
            self._deferred_ball_state = ball_state if defer_flip else None
        if ball_state is not None and not defer_flip:
            self._set_ball_state(ball_state)
        self._apply_expanded_state()

    def set_float_ball(self, enabled: bool) -> None:
        """设置页开关变化：开 → 球模式（容器=球帽锚点，球帽可见）；关 → 磁吸
        （强制展开成侧栏、dock 贴主窗右缘、球帽隐藏）。

        v0.184：容器即球 —— 不再建/毁独立球层，只切模式 + 重排几何。
        """
        enabled = bool(enabled)
        with self._lock:
            if enabled == self._float_ball_enabled:
                return
            self._float_ball_enabled = enabled
            w = self.always_one_window
            if w is None:
                return
            self._ensure_op_worker()
            if enabled:
                # 球模式：球帽可见；位置用已存球位（无则主窗右缘兜底）。
                # 几何按 S1/S2 + 当前 rid 状态统一判定（_sync_geometry_to_mode）。
                if self._ball_pos is None:
                    self._ball_pos = self._default_ball_pos()
                self._set_ball_visible(True)
                if (self._live_panel_setting() and not self._all_hidden
                        and not self.always_one_visible):
                    self._enqueue_op(lambda ww=w: ww.show())
                    self.always_one_visible = True
                self._sync_geometry_to_mode()
            else:
                # 磁吸模式：强制展开成侧栏 + 球帽隐藏 + dock 贴右。
                self._expanded = True
                self._set_ball_visible(False)
                if (self._live_panel_setting() and not self._all_hidden
                        and not self.always_one_visible):
                    self._enqueue_op(lambda ww=w: ww.show())
                    self.always_one_visible = True
                self._apply_expanded_state()

    def set_float_ball_topmost(self, enabled: bool) -> None:
        """v0.184：设置页「悬浮球置顶」开关 → 容器（球/侧栏一体）运行时切置顶。
        容器即球，置顶直接作用于容器窗口（磁吸模式下同窗无球帽，一并置顶
        与旧「球与侧栏同层级」语义一致）。"""
        enabled = bool(enabled)
        with self._lock:
            if enabled == self._float_ball_topmost:
                return
            self._float_ball_topmost = enabled
            w = self.always_one_window
            if w is not None:
                self._ensure_op_worker()
                self._enqueue_op(lambda ww=w, v=enabled: ww.set_topmost(v))

    # ------------------------------------------------------------------
    # 配置变化：响应 set_live_panel_concurrent / set_live_panel_max /
    # set_live_panel_always_one。pool 自己决定如何对齐。
    # ------------------------------------------------------------------
    def enforce_concurrent_off(self) -> None:
        """允许并发 → 关。v0.130：清除全部 rid 容器（窗口收窄回单列）。"""
        with self._lock:
            for rid in list(self._rids):
                self._clear_rid(rid)

    def set_concurrent(self, enabled: bool) -> None:
        """推送「允许并发」开关到侧栏前端（setConcurrent），让前端在
        非并发模式固定显示三容器骨架（1 端点 + 1 完整内容 + 1 工具）。
        """
        enabled = bool(enabled)
        with self._lock:
            if self.always_one_window is not None:
                self._ensure_op_worker()
                js = f"setConcurrent({str(enabled).lower()})"
                w = self.always_one_window
                self._enqueue_op(lambda ww=w, j=js: ww.evaluate_js(j))

    def enforce_max(self) -> None:
        """调低 max：v0.108 网格模型下并发不再受窗口数上限（向右延展）。
        保持 no-op，兼容设置页调用。"""

    def enforce_always_one(self) -> None:
        """「磁吸状态下始终开启一个」开关变化（v0.165e 更名）。
        仅磁吸(dock)模式生效；悬浮球模式 no-op（球 S1/S2 控制侧栏显隐）。

        v0.153 fix：关掉开关只 hide，**不**置 always_one_manually_hidden ——
        该 flag 专指「用户手动 X 掉侧栏」，一旦置位 _apply_geometry 恒 return
        False，assign()（流式新请求）调 _show_always_one 也弹不出来，侧栏就
        再也不会重渲染。idle 隐藏本就由 watchdog 负责（那里会把该 flag 复位）。"""
        with self._lock:
            if self._float_ball_enabled:
                return  # 球模式：always_one 不生效
            enabled = self._always_one_setting()
            if not enabled:
                self._hide_always_one()
            else:
                self.always_one_manually_hidden = False
                if not self._all_hidden:
                    self._show_always_one()

    # ------------------------------------------------------------------
    # 主入口：事件 → 分配 / 路由
    # ------------------------------------------------------------------
    def assign(self, rid: str) -> str:
        """请求 rid 来了：统一加入 _rids（前端按 rid 动态建容器），返回 "ok"。"""
        with self._lock:
            if not self._ready or not rid:
                return ""
            if rid in self._rids:
                return "ok"
            self._rids.add(rid)
            self._cancel_destroy_timer(rid)
            self._stage.pop(rid, None)
            # v0.165f：非并发模式 —— 关闭「允许并发」后只保留 1 个活跃 rid。
            # 新 rid 到达时清掉其它旧 rid（前端随之只保留 1 个完整内容容器）。
            if not self._concurrent_setting():
                for old in [r for r in self._rids if r != rid]:
                    self._clear_rid(old)
            if not self._all_hidden:
                # v0.163：始终开启 OFF（自动显隐模式）下，手动 X 标志不粘滞
                # —— 新请求流到达就重新弹出（用户需求：有请求流时侧栏出现）。
                # 该 flag 只对「始终开启 ON」的持久面板生效（X 掉就一直关着）；
                # 自动模式下 X 只是"本次先收起来"，下个请求流照常弹出。
                # v0.165e：球模式（浮动）同属"自动弹出"，X 也应随新请求复位。
                if not self._always_one_setting() or self._float_ball_enabled:
                    self.always_one_manually_hidden = False
                # v0.184：球模式 —— 有流即按 S1/S2 决定展开/形态：
                #   S1（启动侧栏）：球帽→flow + 展开成「球帽+侧栏」
                #   S2（始终隐藏）：球帽→done，不展开（容器保持收回）
                # 磁吸模式 —— 常规弹出侧栏。
                if self._float_ball_enabled:
                    if self._live_panel_setting():
                        self._sync_geometry_to_mode()
                else:
                    self._show_always_one()
            _logger.debug("assign rid=%s → unified (active=%s)", rid, len(self._rids))
            return "ok"

    def push_event(self, rid: str, ev: dict) -> int:
        """把一个 SSE 事件推给绑 rid 的统一容器（relayLiveEvent，per-rid 路由）。
        ev 是事件 dict（assistant_text/thinking_text 为累积文本）。
        v0.130：不再区分 always/grid —— 一律 relayLiveEvent；宽度由前端布局后
        reportWidth 上报（本函数不再 _relayout）。返回 1 已路由 / 0 未绑定。"""
        with self._lock:
            self._last_event[rid] = time.monotonic()
            self._note_stage(rid, ev)
            if rid not in self._rids:
                return 0
            try:
                payload = json.dumps(ev, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return 0
            if self.always_one_window is None:
                return 0
            self._ensure_op_worker()
            js = f"relayLiveEvent({payload})"
            w = self.always_one_window
            self._enqueue_op(lambda ww=w, j=js: ww.evaluate_js(j))
            return 1

    def push_event_always_one(self, payload_js: str) -> int:
        """事件没绑 rid（snapshot） → 推给 always_one（如果存在）。"""
        with self._lock:
            if self.always_one_window is None:
                return 0
            self._ensure_op_worker()
            self._enqueue_op(lambda w=self.always_one_window, js=payload_js: w.evaluate_js(js))
            return 1

    def _push_rid_timeout(self, rid: str) -> None:
        """v0.109/v0.130：上游中途截断不广播 done 时，由 watchdog 推「超时 done」。

        调用前端 relayLiveTimeout(rid, msg)：只把该 rid 容器的 badge 从「流式中」
        翻成「出错」+ 状态文本，**不动**已累积的正文/思考内容（保留可读）。
        与正常 relayLiveEvent(done) 分开 —— done 会清正文，超时不该清。"""
        if self.always_one_window is None:
            return
        msg = "连接中断（无结束符）"
        js = f"relayLiveTimeout({json.dumps(rid)}, {json.dumps(msg, ensure_ascii=False)})"
        self._ensure_op_worker()
        self._enqueue_op(lambda w=self.always_one_window, j=js: w.evaluate_js(j))

    def release(self, rid: str) -> None:
        """请求 rid done：容器保留 destroy_after_done_sec 秒（可读）后清除。
        10s 内同 rid 重新 assign 会 _cancel_destroy_timer 取消。
        v0.130：统一模型 —— 不再区分 always/grid，一律延迟 _clear_rid。"""
        with self._lock:
            if rid not in self._rids:
                return
            self._schedule_clear(rid)
            _logger.info(
                "release rid=%s → clear scheduled in %ss",
                rid, self._destroy_after_done_sec,
            )

    def _schedule_clear(self, rid: str) -> None:
        """给 rid 挂一个 destroy_after_done_sec 的延迟清除定时器（同 rid 复用会取消）。"""
        self._cancel_destroy_timer(rid)
        t = threading.Timer(self._destroy_after_done_sec, self._clear_rid, args=(rid,))
        t.daemon = True
        self._destroy_timers[rid] = t
        t.start()

    def _clear_rid(self, rid: str) -> None:
        """release 延迟到点：清掉该 rid 的容器（relayLiveClear）+ 重算宽度。"""
        with self._lock:
            self._destroy_timers.pop(rid, None)
            if rid not in self._rids:
                return  # 已清 / 10s 内被复用走 assign 移除
            self._rids.discard(rid)
            self._last_event.pop(rid, None)
            self._stage.pop(rid, None)
            if self.always_one_window is not None:
                self._ensure_op_worker()
                js = f"relayLiveClear({json.dumps(rid)})"
                w = self.always_one_window
                self._enqueue_op(lambda ww=w, j=js: ww.evaluate_js(j))
            _logger.info("clear rid=%s remaining=%s", rid, sorted(self._rids))
            self._relayout()  # 重算宽度（前端随后上报新列数，窗口收窄）
            # v0.163：自动显隐模式 —— 最后一个 rid 清掉后立即收起/隐藏侧栏
            # （不等 watchdog 下一 tick），实现「请求均结束后消失」。
            # v0.184：球模式 = 收起成球帽（容器保留，_hide_always_one 球分支）；
            # 磁吸模式 = 整体隐藏。_should_keep_idle_open 恒 False（v0.176）。
            if (not self._rids
                    and not self._all_hidden
                    and not self._should_keep_idle_open()
                    and self.always_one_visible):
                _logger.info("auto-hiding panel (all rids cleared, idle)")
                self._hide_always_one()
                self.always_one_manually_hidden = False
            # v0.184.1：全部清空 → 球帽回 idle + 容器收回（按 S1/S2 一致）。
            if not self._rids and self._float_ball_enabled:
                self._sync_geometry_to_mode()

    # ------------------------------------------------------------------
    # v0.110/v0.111：阶段感知 stale —— 状态机（wait → thinking → gap → text）
    # ------------------------------------------------------------------
    def _note_stage(self, rid: str, ev: dict) -> None:
        """根据累积文本 + thinking_done 信号推进 rid 的阶段。单向，不回退。

        优先级从高到低：
          text —— 正文一旦出现就是正文阶段（即使 thinking 还在续）。
          gap —— 思考已完成信号（thinking_done）但正文还没出：思考→正文
                 衔接期，静默即衔接卡死，用 gap 档收紧阈值。
          thinking —— 有思考文本、无正文（可能思考暂停，也可能还在思考）。
          皆空 → wait（思考等待）。

        累积文本非空后不会变空，thinking_done 也只会从 False→True，所以
        直接赋值即可（text 分支覆盖前态，wait 分支用 setdefault 不覆盖）。"""
        if ev.get("assistant_text"):
            self._stage[rid] = "text"
        elif ev.get("thinking_done"):
            self._stage[rid] = "gap"
        elif ev.get("thinking_text"):
            self._stage[rid] = "thinking"
        else:
            self._stage.setdefault(rid, "wait")

    def _stage_threshold(self, rid: str) -> float:
        """当前阶段对应的 stale 阈值（秒）。未定 / 未知 rid → wait 档。"""
        stage = self._stage.get(rid, "wait")
        if stage == "text":
            return self._stale_text_secs
        if stage == "gap":
            return self._stale_gap_secs
        if stage == "thinking":
            return self._stale_thinking_secs
        return self._stale_wait_secs

    # ------------------------------------------------------------------
    # orphan-watchdog —— SSE done 事件未广播时的兜底清理
    #
    # 异常场景：proxy 异常路径跳过 done broadcast → rid 容器永久残留。
    # 10s 扫一次：_rids 里超过 _stage_threshold(rid) 无事件（阶段感知，
    # v0.110）且不在 _destroy_timers 等待中的 → 推「超时 done」+ 延迟清容器。
    # ------------------------------------------------------------------
    def _schedule_watchdog(self) -> None:
        if self._watchdog_stop.is_set():
            return
        try:
            # v0.109：tick 从 30s 缩到 10s —— stale 判定（25s 无事件）最坏
            # 约 35s 落地，避免"断流后窗口长时间流式中/容器清不掉"。
            t = threading.Timer(10.0, self._watchdog_tick)
            t.daemon = True
            self._watchdog_timer = t
            t.start()
        except Exception:
            _logger.debug("watchdog schedule failed", exc_info=True)

    def _watchdog_tick(self) -> None:
        try:
            # v0.106：worker 卡死检测 —— 窗口操作 worker 超过阈值无进展
            # 说明被某条 WebView2 操作卡住。丢弃旧 worker，换新的。
            if (self._op_worker is not None
                    and not self._op_worker.is_alive()
                    or time.monotonic() - self._op_progress > self._worker_stale_secs):
                _logger.warning(
                    "watchdog replacing wedged window-op worker "
                    "(progress %.0fs ago)", time.monotonic() - self._op_progress,
                )
                self._op_worker = None
                self._op_progress = time.monotonic()
                self._ensure_op_worker()
            with self._lock:
                pending_rids = set(self._destroy_timers.keys())
                # v0.130：统一 rid stale 自愈 —— 上游中途截断不广播 done 时，
                # 容器永不 release → 一直占位。到点推「超时 done」（badge 变
                # 出错，内容保留可读）再延迟清容器收窄。
                for rid in list(self._rids):
                    if rid in pending_rids:
                        continue  # 已在延迟清除等待中
                    last = self._last_event.get(rid, 0.0)
                    if time.monotonic() - last > self._stage_threshold(rid):
                        _logger.info(
                            "watchdog clearing stale rid=%s (no event %.0fs)",
                            rid, time.monotonic() - last,
                        )
                        self._push_rid_timeout(rid)
                        self._schedule_clear(rid)
                # v0.106：窗口 idle 行为按 _should_keep_idle_open 决定 ——
                # 仅「磁吸 + 始终开启」空闲保留显示；否则空闲隐藏（球模式恒隐藏，
                # 由球 S1/S2 控制）。v0.208：教程引导进行中（_tour_guide_width>0，
                # 章节2 步骤3 侧栏演示展开）时绝不 idle 收起 —— 悬浮窗要在用户
                # 手动点「下一步」前一直保持展开、动画持续；除非还开着 key 防
                # 意外，也顺带跳过 _hide（教程自己会上报宽度 0 复原）。
                if (self.always_one_window is not None
                        and self.always_one_visible
                        and not self._rids
                        and not self._all_hidden
                        and not self._should_keep_idle_open()
                        and not (self._tour_guide_width > 0)):
                    _logger.info("watchdog hiding idle panel (no active rid)")
                    self._hide_always_one()
                    self.always_one_manually_hidden = False
                elif (self.always_one_window is not None
                        and not self.always_one_visible
                        and not self._rids
                        and not self._all_hidden
                        and not self.always_one_manually_hidden
                        and self._should_keep_idle_open()):
                    self._show_always_one()
        except Exception:
            _logger.debug("watchdog tick failed", exc_info=True)
        finally:
            self._schedule_watchdog()

    # ------------------------------------------------------------------
    # 可见性 toggle（一键全部）
    # ------------------------------------------------------------------
    def toggle_all_visible(self) -> bool:
        with self._lock:
            self._all_hidden = not self._all_hidden
            if self._all_hidden:
                # 一键全部隐藏：收回容器（连球帽一起）。
                # 注意：不切 S1/S2 flag——只是临时全藏，下次恢复按 flag 走。
                self._hide_container()
            else:
                if (self.always_one_window is not None
                        and not self.always_one_manually_hidden):
                    if self._should_keep_idle_open() or self.always_one_rid:
                        self._show_always_one()
                # 恢复显示：球模式容器按可见性规则回来（主开关 gate）。
                self.refresh_ball_visibility()
                self._relayout()  # 恢复几何（含网格区宽度）
            return not self._all_hidden

    # ------------------------------------------------------------------
    # 手动 X 关掉面板（仅 hide，事件继续推）
    # ------------------------------------------------------------------
    def hide_panel(self, rid: Optional[str]) -> None:
        with self._lock:
            # v0.109：单窗口 —— X 只关整个窗口（always_one），无独立网格区 X。
            if not rid or rid == "" or rid == self.always_one_rid:
                if self.always_one_window is not None:
                    self._hide_always_one()
                    self.always_one_manually_hidden = True

    # ------------------------------------------------------------------
    # 内部：定时器 / 显隐
    # ------------------------------------------------------------------
    def _cancel_destroy_timer(self, rid: str) -> None:
        t = self._destroy_timers.pop(rid, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

    def _target_width(self) -> int:
        """v0.130：窗口目标宽度。auto_extend ON → 前端上报列数（clamp 到
        [1, _max_cols]）× panel_width；OFF → 单列 panel_width。"""
        if not self._auto_extend:
            return self._panel_width
        cols = min(max(self._js_width_cols, 1), self._max_cols)
        return cols * self._panel_width

    def _anim_resize(self, w, to_w: int, to_h: int, *, on_done: Optional[Callable] = None) -> None:
        """v0.184：窗口宽高**同变**补间动画（延展/收窄/展开/收起平滑，不瞬跳）。

        从窗口**当前实际宽高**起，ease-out（x^4）曲线分 32 步（v0.149 从
        14 加密）渐进 resize 到 (to_w, to_h)（总时长 ~700ms）。v0.184 把
        v0.145 的「只动宽、高一步到位」扩展成双轴：收起/展开是宽高同时变，
        需要同补间（dock 贴右的高度跟随主窗仍由 _apply_geometry 直接
        _resize_safe 或仅高度变化走直 resize —— 见其调用处）。

        每帧 resize 走 worker 队列（复用 v0.106 非阻塞派发）。新一轮动画
        启动 / 宽高再次变化时 _width_anim_seq+1，旧线程检测到序号不符即
        静默退出；新线程从最新实际宽高起步，覆盖中间态。动画不依赖 _lock
        （线程不持有 pool 锁），隐藏窗口的 resize 是 no-op 无害。

        v0.184.2：on_done —— 动画**正常收尾**（含起始即已到目标尺寸的早退
        路径）后调用，用于把延迟的球帽翻转落地（串行动画顺序：先收侧栏逆
        动画回球，再翻转）。被新一轮动画接管 / 隐藏打断时**不**调用（新状
        态接管结果，_deferred_ball_state 由后续 _apply_geometry flush）。
        """
        seq = self._width_anim_seq + 1
        self._width_anim_seq = seq
        try:
            cur_w = int(getattr(w, "width", 0) or self._last_panel_w or self._panel_width)
            cur_h = int(getattr(w, "height", 0) or self._last_panel_h or self._panel_height)
        except Exception:
            cur_w, cur_h = to_w, to_h
        if abs(to_w - cur_w) < 2 and abs(to_h - cur_h) < 2:
            self._enqueue_op(lambda ww=w, cw=to_w, ch=to_h: self._resize_safe(ww, cw, ch))
            if on_done is not None:
                on_done()
            return

        steps = 32
        dt = 0.022
        self._resize_anim_active = True

        def _ease_out(x: float) -> float:
            x = max(0.0, min(1.0, x))
            # 四次方缓出：起速更陡、收速更缓，延展「舒展」感更强。
            return 1 - (1 - x) ** 4

        def _run() -> None:
            dw = to_w - cur_w
            dh = to_h - cur_h
            for i in range(1, steps + 1):
                if self._width_anim_seq != seq:
                    return  # 被新一轮动画接管
                if self._drag_freeze_geometry():
                    # v0.209：拖动开始 → 中止补间，几何交给 ball_main 的
                    # setPosition。无后继补间接管，手工复位 active 标志，让
                    # 拖动结束的 _relayout 能正常补上延迟渲染。
                    self._resize_anim_active = False
                    return
                w_now = int(cur_w + dw * _ease_out(i / steps))
                h_now = int(cur_h + dh * _ease_out(i / steps))
                self._enqueue_op(
                    lambda ww=w, cw=w_now, ch=h_now: self._resize_safe(ww, cw, ch))
                time.sleep(dt)
            # 收尾精调（浮点累计误差归零）
            self._enqueue_op(
                lambda ww=w, cw=to_w, ch=to_h: self._resize_safe(ww, cw, ch))
            try:
                if on_done is not None:
                    on_done()
            finally:
                self._resize_anim_active = False

        threading.Thread(target=_run, daemon=True).start()

    def _resize_safe(self, w, to_w: int, to_h: int) -> None:
        """v0.171：z-order 中性 resize —— 宽度补间动画期间不把侧栏提到 Z 序顶端。

        背景：pywebview 的 winforms.resize()（winforms.py:616）用
        ``SetWindowPos(handle, None, ..., 64)`` —— 64 = SWP_SHOWWINDOW，
        hWndInsertAfter=None(=HWND_TOP)，且**无 SWP_NOZORDER**。这导致侧栏
        **每帧 resize 都被提到 Z 序顶**。当悬浮球同样置顶（v0.170，球恒
        WS_EX_TOPMOST）时，球的 top-keep（ball_layer._topkeep_tick，300ms）
        又把球压回最上 —— 两个机制交替胜出：宽度动画（_anim_resize，32 帧×
        22ms≈700ms）期间球在「悬浮在侧栏上面」↔「被侧栏遮挡」间反复闪烁。

        本函数用 SetWindowPos(SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE) 做
        z-order 中性 resize：不搬位置、不告示任何窗口、不抢焦点、**不改变
        Z 序** —— 侧栏不会被顺带提升到顶，球恒在上层（其 top-keep 已保证）。

        实现要点：
          * 用**私有** WinDLL 实例（不用共享的 ctypes.windll.user32）并显式
            argtypes —— ball_layer v0.170 注：共享实例设 argtypes 会让
            pywebview 的 move()（传 None 给 cx/cy）立刻抛 ArgumentError。
          * logical → physical 换算与 pywebview 一致（乘 _scale）。
          * 位置用 fix_point=NORTH|WEST（主语义，resize 不动左上角）——
            读当前物理 Left/Top 作 (x,y)。
          * 任何失败回退 w.resize()（宁可回到旧闪烁也要尺寸正确）。
        """
        try:
            native = getattr(w, "native", None)
            if native is None:
                w.resize(to_w, to_h)
                return
            import ctypes
            from ctypes import wintypes
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.SetWindowPos.restype = wintypes.BOOL
            u.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint,
            ]
            scale = float(getattr(native, "_scale", 1.0) or 1.0)
            # pythonnet 3.x 的 IntPtr 不支持 int()，必须 ToInt64()（见 ball_layer._handle_int）。
            h = getattr(native, "Handle", None)
            hwnd = int(h.ToInt64()) if h is not None else 0
            if hwnd == 0:
                w.resize(to_w, to_h)
                return
            x_phys = int(int(getattr(native, "Left", 0) or 0))
            y_phys = int(int(getattr(native, "Top", 0) or 0))
            w_phys = int(to_w * scale)
            h_phys = int(to_h * scale)
            # SWP_NOMOVE(0x2) | SWP_NOZORDER(0x4) | SWP_NOACTIVATE(0x10)
            u.SetWindowPos(hwnd, None, x_phys, y_phys, w_phys, h_phys, 0x16)
        except Exception:
            try:
                w.resize(to_w, to_h)
            except Exception:
                _logger.debug("resize_safe failed", exc_info=True)

    def _screen_size(self) -> Optional[tuple[int, int]]:
        """屏幕工作区 (w, h)。优先 screen_getter；次之 webview.screen；都无 → None。"""
        if self._screen_getter is not None:
            try:
                return self._screen_getter()
            except Exception:
                pass
        try:
            import webview
            s = webview.screen
            return (int(s.width), int(s.height))
        except Exception:
            return None

    def _apply_geometry(self, *, force_apply: bool = False) -> bool:
        """容器窗口几何：球模式（收起=球帽方 / 展开=球帽+侧栏）或磁吸 dock。

        * 球模式：收起 = (ball_size, ball_size) 贴球位；展开 = 从球位向右下
          延长成 (ball_size + side_w, panel_height)，右/下出屏整体 clamp 保
          整窗在屏内（球帽随窗左移，不做内部镜像）。
        * 磁吸模式：dock 贴主窗右缘 (mx+mw, my)，高跟随主窗 (mh)，宽按
          _target_width（列数 × panel_width）并 clamp 屏幕余宽。
        宽高变化只在真正改变时 resize；首次几何（_last_panel_w==0）直 resize
        不动画（避免启动从 56×56 弹跳生长）。拖动中（_ball_dragging）几何归
        ball_main 管，本方法直接返回防反扑。

        隐藏态（一键隐藏 / 手动 X）非 show 调用：跳过原生 move/resize，几何
        留给 _show_always_one(force_apply=True) / refresh_ball_visibility。
        """
        if self._all_hidden or self.always_one_manually_hidden:
            return False
        if not self.always_one_visible and not force_apply:
            return True
        w = self.always_one_window
        if w is None:
            return False
        screen = self._screen_size()
        sw, sh = screen or (0, 0)
        # ---- 球模式：容器 = 球帽 + 侧栏 ----
        if self._float_ball_enabled:
            if self._drag_freeze_geometry():
                return True  # 拖动中 ball_main 管几何
            if self._ball_pos is None:
                self._ball_pos = self._default_ball_pos()
            bx, by = self._ball_pos
            # v0.208：教程收起演示（_tour_collapsed）走 else 分支 → 窗口「引导带
            # + 球帽」、球帽 56px 不占满；非收起展开才走全展开。
            if self._expanded and not self._tour_collapsed:
                side_w = self._target_width()
                if sw > 0:
                    max_w = max(self._panel_width, sw - bx - 4)
                    side_w = min(side_w, max_w)
                h = min(self._panel_height, sh) if sh > 0 else self._panel_height
                width = self._ball_size + side_w
                # v0.205：教程阶段容器左侧临时开引导带（指引卡片悬浮在悬浮
                # 球左侧）。引导带在**球帽左边** —— 窗口左缘左移 guide px、
                # 宽度加 guide px（球帽仍在原 bx 处，见 ghost_panel 布局：
                # #ghost-cap 在 #ghost-root 左侧）。宽度由 JS 上报
                # （set_tour_guide_width）；出屏 clamp 保整窗在屏内。
                guide = max(0, int(getattr(self, "_tour_guide_width", 0) or 0))
                if guide:
                    width += guide
                    if sw > 0 and width > sw:
                        width = sw
                # 出屏 clamp：优先球锚定（容器左上=球位）；左侧引导带放不下
                # 或右侧放不下则整体平移保整窗在屏内；下界同理上移。
                xx = bx - guide
                if sw > 0 and xx + width > sw:
                    xx = max(0, sw - width)
                if xx < 0:
                    xx = 0
                yy = by
                if sh > 0 and yy + h > sh:
                    yy = max(0, sh - h)
            else:
                # v0.208：仅**教程收起演示**（_tour_collapsed）保留「引导带 + 球帽」
                # 宽度、面板高度 —— 指引卡继续悬浮在球帽左侧可见（窗口不缩成
                # 56px 裁掉 fixed 卡）。**普通收起**（_tour_collapsed=False）仍缩
                # 成 ball_size 方 —— 否则 body.collapsed 会把球帽拉成整窗大球，
                # 出现"悬浮球全程巨大"（用户反馈）。
                guide = max(0, int(getattr(self, "_tour_guide_width", 0) or 0))
                if self._tour_collapsed and guide:
                    width = self._ball_size + guide
                    if sw > 0 and width > sw:
                        width = sw
                    h = min(self._panel_height, sh) if sh > 0 else self._panel_height
                    xx = bx - guide
                    if sw > 0 and xx + width > sw:
                        xx = max(0, sw - width)
                    if xx < 0:
                        xx = 0
                    yy = by
                    if sh > 0 and yy + h > sh:
                        yy = max(0, sh - h)
                else:
                    width = self._ball_size
                    h = self._ball_size
                    xx, yy = bx, by
            self._ensure_op_worker()
            # v0.209：几何操作包一层「执行瞬间仍在拖动则丢弃」。本分支顶部的
            # _drag_freeze_geometry 只拦"新的"几何计算；已被之前某次容器变化
            # 入队、排在 op-worker 队列里的 move/resize 仍会执行 —— 那正是拖动
            # 中被拽回旧球位的漂移来源（队列有延迟，入队时还没拖动）。
            # 只包 move/resize：hide / show / evaluate_js 不包，隐藏必须随时生效。
            def _geom(fn):
                def _run():
                    if self._drag_freeze_geometry():
                        return
                    fn()
                return _run
            first = self._last_panel_w == 0
            if width != self._last_panel_w or h != self._last_panel_h:
                self._last_panel_w = width
                self._last_panel_h = h
                if first:
                    self._enqueue_op(_geom(
                        lambda ww=w, cw=width, ch=h: self._resize_safe(ww, cw, ch)))
                    self._flush_deferred_render()
                else:
                    # 收起动画收尾后再隐藏面板 + 翻转球帽（串行动画顺序）；
                    # 无延迟渲染时 _flush_deferred_render 是 no-op。
                    self._anim_resize(w, width, h,
                                      on_done=self._flush_deferred_render)
            else:
                # 尺寸未变（含收起动画在飞期间的主窗 move/resize 重入）：无新
                # 动画。补间不在飞时落地延迟渲染 —— 收起已到位 / 动画被打断
                # 后恢复都能正确补上面板隐藏 + 球帽翻转。
                if not self._resize_anim_active:
                    self._flush_deferred_render()
            self._enqueue_op(_geom(lambda ww=w, x=xx, y=yy: ww.move(xx, yy)))
            return True
        # ---- 磁吸模式 ----
        if not self._dock_getter:
            return False
        try:
            mx, my, mw, mh = self._dock_getter()
        except Exception:
            return False
        if mw <= 0 or mh <= 0:
            return False
        width = self._target_width()
        # clamp 到主窗右缘之后的屏幕余宽（贴右，不顶出屏）。守卫 sw>0：
        # 屏幕尺寸不可用时跳过 clamp（JS 侧 LP_MAX_COLS 兜底 ≤6 列）。
        if sw > 0:
            max_w = max(self._panel_width, sw - (mx + mw) - 4)
            width = min(width, max_w)
        self._ensure_op_worker()
        docked = bool(self._docked_getter()) if self._docked_getter is not None else True
        if docked:
            self._enqueue_op(lambda ww=w, xx=mx + mw, yy=my: ww.move(mx + mw, my))
            first = self._last_panel_w == 0
            if width != self._last_panel_w or mh != self._last_panel_h:
                if first:
                    self._last_panel_w = width
                    self._last_panel_h = mh
                    self._enqueue_op(
                        lambda ww=w, cw=width, ch=mh: self._resize_safe(ww, cw, ch))
                elif width != self._last_panel_w:
                    # 列数变化 → 宽度补间动画（平滑延展/收窄）。
                    self._last_panel_w = width
                    self._last_panel_h = mh
                    self._anim_resize(w, width, mh)
                else:
                    # 仅主窗高度变化 → 直接 resize（高度不延迟动画）。
                    self._last_panel_h = mh
                    self._enqueue_op(
                        lambda ww=w, cw=width, ch=mh: self._resize_safe(ww, cw, ch))
        else:
            # 浮动（非 dock）：不 move、不跟主窗高度 —— 只在列数变化时原地
            # 改宽。窗口可见时顺带刷新浮动位置缓存（隐藏恢复用）。
            if self.always_one_visible:
                self._float_pos = (
                    int(getattr(w, "x", 0) or 0),
                    int(getattr(w, "y", 0) or 0),
                )
            cur_w = int(getattr(w, "width", 0) or self._last_panel_w or self._panel_width)
            cur_h = int(getattr(w, "height", 0) or self._last_panel_h or self._panel_height)
            if width != cur_w:
                self._last_panel_w = width
                self._last_panel_h = cur_h
                self._anim_resize(w, width, cur_h)
        return True

    def _show_always_one(self) -> None:
        # v0.176：主开关 gate —— 关闭时侧栏绝不弹出（SSE 线程常跑，请求
        # 到达也只在隐藏的窗口里推事件，不 show）。
        if not self._live_panel_setting():
            return
        # v0.106：show/move 走 worker 非阻塞派发，状态同步更新。
        # 关键：show 之前必须先 move 到 dock 位置，否则 hidden 窗口落在
        # 默认位置（屏幕中央）。move+show 合并到同一 worker op 内顺序保证。
        # v0.107：几何未知时直接 return —— 真正的 dock+show 由 loaded 事件
        # （_on_panel_loaded → _dock_panel(force=True)）补上。
        # v0.136：浮动解耦 —— 浮动状态下被隐藏后重新显示，先移回上次浮动
        # 位置再 show；docked 态由 _apply_geometry 贴右，无需额外 move。
        if self.always_one_window is not None and not self.always_one_visible:
            w = self.always_one_window
            self._ensure_op_worker()
            # v0.166：show 路径强制应用几何（否则 _apply_geometry 见隐藏态
            # 且非 force_apply 会跳过原生 move/resize，show 前永不定位）。
            if not self._apply_geometry(force_apply=True):
                return
            # v0.184：球模式下 _apply_geometry 已把容器 move 到球位置，浮动
            # 恢复（_float_pos）不适用，跳过避免覆盖球定位。
            if not (self._float_ball_enabled and self._ball_pos is not None):
                docked = bool(self._docked_getter()) if self._docked_getter is not None else True
                if not docked and self._float_pos is not None:
                    fx, fy = self._float_pos
                    self._enqueue_op(lambda ww=w, xx=fx, yy=fy: ww.move(fx, fy))
            self._enqueue_op(lambda ww=w: ww.show())
            self.always_one_visible = True
        # v0.184：球模式显示 = 球帽可见（磁吸模式球帽隐藏由 set_float_ball
        # /_apply_expanded_state 管）。
        if self._float_ball_enabled:
            self._set_ball_visible(True)

    def _hide_always_one(self) -> None:
        """隐藏侧栏面：磁吸 → 整体隐藏容器；球模式 → 收起成球帽（容器保留）。"""
        if self._float_ball_enabled:
            # 球模式：收起侧栏 = 收起成球帽（容器本身是锚点，保留可见）。
            if self._expanded:
                self._expanded = False
                self._apply_expanded_state()
            return
        if self.always_one_window is not None and self.always_one_visible:
            # v0.166：折叠时失效在飞的宽度补间动画 —— 动画线程每 22ms 连发
            # resize()（SWP_SHOWWINDOW 会把隐藏窗重新显示），不失效的话
            # 折叠后残留的 resize op 会把侧栏又弹出来。_width_anim_seq+1 让
            # _anim_resize._run 下一迭代检测到序号不符即静默退出。
            self._width_anim_seq += 1
            self._resize_anim_active = False  # 折叠 = 取消在飞动画，标记复位
            w = self.always_one_window
            self._ensure_op_worker()
            self._enqueue_op(lambda ww=w: ww.hide())
            self._enqueue_op(lambda ww=w: ww.move(-32000, -32000))
            self.always_one_visible = False

    # ------------------------------------------------------------------
    # 布局：单窗口贴主窗右缘，宽度随前端上报列数动态（自动延展）
    # ------------------------------------------------------------------
    def _relayout(self) -> None:
        """v0.130/v0.136：重排唯一窗口几何。

        * dock 态：位置 → (mx+mw, my)，**高度跟随主窗**（mh，顶/底对齐）；
          宽度 → _target_width()：auto_extend OFF = panel_width；ON = 前端
          上报列数 clamp [1, _max_cols] × panel_width，再 clamp 到屏幕余宽。
        * 浮动态（v0.136）：不 move、不跟主窗高度，只在列数变化时原地改宽
          （见 _apply_geometry）。
        宽度变化只在真正改变时 resize（_apply_geometry）。
        全部 move/resize/show 走 worker 非阻塞派发（_relayout 可能在任意
        线程 / 锁内被调用，绝不能在窗口操作上阻塞）。
        """
        self._apply_geometry()

    def refresh_geometry(self) -> None:
        """主窗 moved/resized 回调：重排窗口（位置 + 动态宽度）。"""
        with self._lock:
            self._relayout()

    def set_float_pos(self, x: int, y: int) -> None:
        """v0.136：记录浮动位置（gui 检测到侧栏被拖离磁吸区时上报）。

        浮动状态下窗口被隐藏（X / 全部隐藏 / 主窗最小化）后重新显示时，
        _show_always_one 用它把窗口恢复到用户拖到的位置。"""
        with self._lock:
            self._float_pos = (int(x), int(y))

    # ------------------------------------------------------------------
    # v0.130：自动延展（宽度）控制 —— 设置页 / JS 上报入口
    # ------------------------------------------------------------------
    def set_auto_extend(self, enabled: bool) -> None:
        """自动延展开关变化：置位 + 通知前端 setAutoExtend + 重排窗口。"""
        with self._lock:
            enabled = bool(enabled)
            if enabled == self._auto_extend:
                return
            self._auto_extend = enabled
            if self.always_one_window is not None:
                self._ensure_op_worker()
                js = f"setAutoExtend({str(enabled).lower()})"
                w = self.always_one_window
                self._enqueue_op(lambda ww=w, j=js: ww.evaluate_js(j))
            self._relayout()

    def set_done_clear_timeout(self, seconds: float) -> None:
        """v0.134：设置页改「完成清除超时」→ 实时更新 done 后自动清除间隔。
        正在跑的定时器不受影响（到点用旧值），下个 release 生效。"""
        self._destroy_after_done_sec = max(1, int(seconds))

    def request_width(self, cols: int) -> None:
        """前端布局后上报列数（live_panel_layout）→ 重算窗口宽。"""
        with self._lock:
            cols = max(1, int(cols))
            if cols == self._js_width_cols:
                return
            self._js_width_cols = cols
            _logger.info("request_width cols=%d → target=%d", cols, self._target_width())
            self._relayout()

    def set_max_cols(self, n: int) -> None:
        """给出列数上限（按屏幕工作区余宽算，兜底 ≤6）。"""
        self._max_cols = max(1, int(n))

    # ------------------------------------------------------------------
    # settings 读取（每次都从 settings 现读，避免副本陈旧）
    # ------------------------------------------------------------------
    def _settings(self):
        return getattr(self._api._app, "settings", None) if getattr(self._api, "_app", None) else None

    def _concurrent_setting(self) -> bool:
        s = self._settings()
        return bool(getattr(s, "relay_gui_live_panel_concurrent", True)) if s else True

    def _always_one_setting(self) -> bool:
        s = self._settings()
        return bool(getattr(s, "relay_gui_live_panel_always_one", True)) if s else True

    def _live_panel_setting(self) -> bool:
        """主开关「实时流侧栏」。球是侧栏锚点的一部分 —— 主开关关则球也关。"""
        s = self._settings()
        return bool(getattr(s, "relay_gui_live_panel", False)) if s else False

    def _should_keep_idle_open(self) -> bool:
        """侧栏空闲时是否保持显示。

        v0.165e：悬浮球（浮动）模式下 always_one 不生效 —— 空闲即隐藏，由球
        S1/S2 控制侧栏显隐。两模式互斥：球开 = 浮动，球关 = 磁吸。
        v0.176：**恒 False** —— 两个模式都改成「有请求才弹、空闲收起」。
        dock（球关）模式下侧栏固定贴右但无请求时不常驻；always_one 配置值
        自此中性化（设置页开关已移除，保留代码不删）。
        """
        return False

    def _should_show_on_startup(self) -> bool:
        """启动时侧栏应否强显：主开关(实时流侧栏) ON 且 磁吸模式 且 始终开启一个。

        v0.166：三处启动强显（PanelPool.start / gui._on_loaded / gui._on_panel_loaded）
        原先用 `live_panel OR always_one` —— always_one 默认 True 时，即使主开关关
        或处于悬浮球模式，侧栏也被强显（用户反馈「没开实时流侧栏侧栏却在」）。统一
        网关到这里：
          * 主开关 OFF → 启动不显示（无请求时不占屏）；
          * 悬浮球模式 → 侧栏空闲隐藏，由球 S1/S2 控制（不强显）；
          * 磁吸 + always_one ON → 常驻显示；磁吸 + always_one OFF → 首个请求到达再弹。
        """
        return self._live_panel_setting() and self._should_keep_idle_open()

    def _ball_should_show(self) -> bool:
        """球应否可见：开关开 + 主开关(实时流侧栏)开 + 非「一键全部隐藏」。"""
        return (self._float_ball_enabled and self._live_panel_setting()
                and not self._all_hidden)

    # ------------------------------------------------------------------
    # always_one closing hook（用户点 X → 仅 hide 不销毁）
    # ------------------------------------------------------------------
    def _on_always_one_closing(self) -> bool:
        try:
            if self.always_one_window is not None:
                self.always_one_window.hide()
            self.always_one_visible = False
            self.always_one_manually_hidden = True
        except Exception:
            pass
        return True


# 兼容：把 start_live_panel_stream 那条 SSE 推送路径从单窗口改成池。
def attach_sse_push_to_pool(app, pool: PanelPool) -> None:
    """替换 app._sse_push_to_panel：把 evaluate_js 改为统一 relayLiveEvent 路由。

    v0.130：ev dict 原样传递，窗口侧一律 relayLiveEvent(per-rid 路由)；
    无 rid 的 snapshot 走 push_event_always_one 兜底。
    """
    import json as _json

    def _push(ev: dict) -> None:
        rid = ev.get("request_id") if isinstance(ev, dict) else None
        kind = ev.get("type") if isinstance(ev, dict) else None
        # snapshot 事件可能没 request_id → 推给 always_one 兜底。
        if not rid and kind == "snapshot":
            try:
                payload = _json.dumps(ev, ensure_ascii=False, default=str)
                js = f"relayLiveEvent({payload})"
                pool.push_event_always_one(js)
            except Exception:
                pass
            return
        if not rid:
            # 无 rid 其它事件 → 静默丢
            return
        try:
            n = pool.push_event(rid, ev)
            if n == 0:
                # rid 没绑窗口 —— 任何带 rid 的事件都尝试分配（不只是 start）。
                # delta 分配 → 立即 show + 推送；done 分配 → 短命块。
                if kind in ("start", "delta", "done"):
                    slot = pool.assign(rid)
                    _logger.info("assign kind=%s rid=%s → slot=%r", kind, rid, slot)
                    if slot:
                        pool.push_event(rid, ev)
            if kind == "done":
                # 无论推送是否成功，done 都调度 release（延迟清 rid 容器）。
                _logger.info("done rid=%s n=%d", rid, n)
                pool.release(rid)
        except Exception:
            _logger.debug("sse push failed kind=%s rid=%s", kind, rid, exc_info=True)

    app._sse_push_to_panel = _push  # type: ignore[attr-defined]


def attach_main_moved_to_pool(app, pool: PanelPool) -> None:
    """主窗 moved/resized 钩到 pool 重排。"""
    try:
        # pywebview 6.2.x 的 moved/resized 事件带 2 个位置参数
        # （winforms.py 用 set(x, y) / set(w, h) 触发）。1 参 lambda 会因
        # event.py 的 inspect.signature 分派收到 2 参 → TypeError，每次移动
        # 窗口都抛，进而触发 watchdog 的 worker 换血风暴。用 *args 吞掉。
        app.window.events.moved += lambda *args, **kwargs: pool.refresh_geometry()
        app.window.events.resized += lambda *args, **kwargs: pool.refresh_geometry()
    except Exception:
        _logger.debug("attach_main_moved_to_pool failed", exc_info=True)
