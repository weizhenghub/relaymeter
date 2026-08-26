// Token Manager — frontend logic.
//
// Polls the Python Api bridge every 500 ms and renders the result in
// place. No build step, no framework — vanilla DOM manipulation keeps
// the binary footprint trivial and lets us iterate without a bundler.
//
// Sections (in DOM order):
//   .topbar-status / .topbar-actions / .hero / .grid (5 cards)
//
// Snapshot shape (see src/relay/gui.py -> App._rebuild_snapshot):
//   {
//     ts: float,
//     by_platform:   {platform: {requests, input_tokens, output_tokens, …}},
//     by_model:      {model:    {requests, input_tokens, output_tokens, …}},
//     by_upstream:   {upstream: {counts: {window: n}, 5h_release: float|null}},
//     recent:        [{ts, platform, model, input_tokens, output_tokens, …}],
//     live:          [{state, model, started_at, …}],
//     upstreams:     {platform: [{name, url, note}]},
//     active_per_platform: {platform: name},
//   }

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // Bridge wrapper
  // -------------------------------------------------------------------------

  const api = {
    /** Returns null when not running inside WebView2 (e.g. file:// dev). */
    get bridge() {
      if (typeof window === "undefined") return null;
      if (window.pywebview && window.pywebview.api) return window.pywebview.api;
      return null;
    },

    // Single bridge-call shape: locate the bridge, swallow rejections,
    // apply the per-method fallback (default null). Every wrapper below
    // collapses to this one line.
    // v0.44：去掉 OK 路径上的 console.log（每 500ms 一次 × 2 个 method
    // = 4 行/秒刷屏 F12 控制台，干扰用户看真错误）。FAIL 路径保留，
    // 是真要看的错误。bridge-missing 警告保留（异常启动场景有用）。
    // v0.9：桥调用加超时。pywebview 桥在 Python 线程卡死 / 桥挂起时会
    // 永不 resolve —— 轮询自链式调度依赖上一次 resolve，一旦 pending
    // 整个 UI 轮询静默死亡。Promise.race 超时后走 fallback，让 tick
    // 照常推进。用户主动动作（启/停/重启，可能带 taskkill+轮询）传更
    // 大的 timeoutMs。
    async _call(method, args = [], fallback = null, timeoutMs = 3000) {
      const b = this.bridge;
      if (!b) {
        console.warn(`[bridge] api.<${method}> — window.pywebview missing`);
        return fallback;
      }
      try {
        return await Promise.race([
          b[method](...args),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error(`bridge ${method} timed out after ${timeoutMs}ms`)), timeoutMs)
          ),
        ]);
      } catch (e) {
        console.error(`[bridge] api.${method} failed`, e);
        return fallback;
      }
    },

    status()       { return this._call("get_status"); },
    snapshot()     { return this._call("get_snapshot"); },
    toggleTheme()  { return this._call("toggle_theme"); },
    // 启/停/重启可能含 taskkill 接管 + 端口回读（最长几秒），不能用
    // 3s 轮询超时把它掐断。
    start()        { return this._call("start_server", [], null, 20000); },
    stop()         { return this._call("stop_server", [], null, 20000); },
    restart()      { return this._call("restart_server", [], null, 20000); },
    refresh()      { return this._call("refresh_now"); },
    // v0.11.3: settings-page preferences.
    setTheme(name)         { return this._call("set_theme", [name]); },
    getAutostart()         { return this._call("get_autostart"); },
    setAutostart(enabled)  { return this._call("set_autostart", [enabled]); },
    getRelaySettings()     { return this._call("get_relay_settings"); },
    updateRelaySettings(p){ return this._call("update_relay_settings", [p]); },
    // v0.11.21: 内外转换显示开关（设置页 + snapshot）。
    getShowIoMap()        { return this._call("get_show_io_map"); },
    setShowIoMap(enabled) { return this._call("set_show_io_map", [enabled]); },
    // v0.89: 实时流侧栏开关（设置页 + 顶栏图标共用后端）。
    getLivePanel()        { return this._call("get_live_panel"); },
    setLivePanel(enabled) { return this._call("set_live_panel", [enabled]); },
    // v0.104：实时栏管理三件套（与顶层开关共享同一份 .env 字段）。
    getLivePanelConcurrent() { return this._call("get_live_panel_concurrent"); },
    setLivePanelConcurrent(enabled) { return this._call("set_live_panel_concurrent", [enabled]); },
    getLivePanelMax()        { return this._call("get_live_panel_max"); },
    setLivePanelMax(n)       { return this._call("set_live_panel_max", [n]); },
    getLivePanelAlwaysOne()  { return this._call("get_live_panel_always_one"); },
    setLivePanelAlwaysOne(enabled) { return this._call("set_live_panel_always_one", [enabled]); },
    // v0.130：自动延展侧栏（宽度）。
    getLivePanelAutoExtend() { return this._call("get_live_panel_auto_extend"); },
    setLivePanelAutoExtend(enabled) { return this._call("set_live_panel_auto_extend", [enabled]); },
    // v0.165：悬浮球（侧栏从球位置展开）。
    getFloatBall()        { return this._call("get_float_ball"); },
    setFloatBall(enabled) { return this._call("set_float_ball", [enabled]); },
    // v0.170：悬浮球置顶（球与侧栏同层级）。
    getFloatBallTopmost()        { return this._call("get_float_ball_topmost"); },
    setFloatBallTopmost(enabled) { return this._call("set_float_ball_topmost", [enabled]); },
    // v0.111：实时栏阶段感知 stale 超时三档（秒）。
    getLivePanelThinkingTimeout() { return this._call("get_live_panel_thinking_timeout"); },
    setLivePanelThinkingTimeout(sec) { return this._call("set_live_panel_thinking_timeout", [sec]); },
    getLivePanelGapTimeout()   { return this._call("get_live_panel_gap_timeout"); },
    setLivePanelGapTimeout(sec) { return this._call("set_live_panel_gap_timeout", [sec]); },
    getLivePanelTextTimeout()  { return this._call("get_live_panel_text_timeout"); },
    setLivePanelTextTimeout(sec) { return this._call("set_live_panel_text_timeout", [sec]); },
    // v0.134：完成清除超时 / 工具清除超时（秒，滚轮选择器）。
    getLivePanelDoneClearTimeout() { return this._call("get_live_panel_done_clear_timeout"); },
    setLivePanelDoneClearTimeout(sec) { return this._call("set_live_panel_done_clear_timeout", [sec]); },
    getLivePanelToolsClearTimeout() { return this._call("get_live_panel_tools_clear_timeout"); },
    setLivePanelToolsClearTimeout(sec) { return this._call("set_live_panel_tools_clear_timeout", [sec]); },
    // v0.134：工具调用上限 / 端点列表高度（数字输入）。
    getLivePanelToolsCap() { return this._call("get_live_panel_tools_cap"); },
    setLivePanelToolsCap(n) { return this._call("set_live_panel_tools_cap", [n]); },
    getLivePanelEpListVh() { return this._call("get_live_panel_ep_list_vh"); },
    setLivePanelEpListVh(vh) { return this._call("set_live_panel_ep_list_vh", [vh]); },
    // v0.135：工具常驻开关 / 最小列数 / 列表字号档位。
    getLivePanelToolsAlways() { return this._call("get_live_panel_tools_always"); },
    setLivePanelToolsAlways(on) { return this._call("set_live_panel_tools_always", [on]); },
    getLivePanelMinCols() { return this._call("get_live_panel_min_cols"); },
    setLivePanelMinCols(n) { return this._call("set_live_panel_min_cols", [n]); },
    getLivePanelListFont() { return this._call("get_live_panel_list_font"); },
    setLivePanelListFont(v) { return this._call("set_live_panel_list_font", [v]); },
    // v0.113c：实时流侧栏「无边框」开关。
    getLivePanelFrameless()    { return this._call("get_live_panel_frameless"); },
    setLivePanelFrameless(on)  { return this._call("set_live_panel_frameless", [on]); },
    // v0.113n：存储管理 / 存储位置管理（设置页「存储管理」区块）。
    getStorageInfo()     { return this._call("get_storage_info"); },
    cleanupMessages(days, target){ return this._call("cleanup_messages", [days, target || "both"]); },
    vacuumStorage(target)      { return this._call("vacuum_storage", [target || "both"]); },
    clearLogs()          { return this._call("clear_logs"); },
    moveStorage(kind, path) { return this._call("move_storage", [kind, path]); },
    // v0.104：顶栏「实时流」按钮新语义 = 一键全部显示/隐藏。
    toggleAllPanels()        { return this._call("toggle_all_panels"); },
    // v0.11.18 高级切换（实验性）。
    getAdvancedSwitch()      { return this._call("get_advanced_switch"); },
    updateAdvancedSwitch(p)  { return this._call("update_advanced_switch", [p]); },
    // v0.113o 报错分析（小模型分类错误类型 + 给用户提示）。
    getErrorAnalysis()    { return this._call("get_error_analysis"); },
    setErrorAnalysis(p)   { return this._call("set_error_analysis", [p]); },
    testErrorAnalysis()   { return this._call("test_error_analysis"); },
    // v0.188 支持图片的模型（/models/api.json modalities）。
    getVisionModels()     { return this._call("get_vision_models"); },
    setVisionModels(p)    { return this._call("set_vision_models", [p]); },
    // Frameless window controls — Python side is snake_case; routed
    // through ``_call`` so the same bridge-missing guard covers them.
    windowMinimize()       { return this._call("window_minimize"); },
    windowToggleMaximize() { return this._call("window_toggle_maximize", [], false); },
    windowClose()          { return this._call("window_close"); },
    // v0.11.4: 开发用 —— 重新加载页面，读取磁盘上的最新代码。
    reloadGui()            { return this._call("reload_gui"); },
    // Drag-resize from window edges. width/height are CSS pixels
    // (== pywebview's logical pixels); fixBits is the FixPoint Flag
    // bitset: 1=NORTH, 2=WEST, 4=EAST, 8=SOUTH — pick the corner to
    // anchor. Falls back to null silently if the bridge is missing,
    // which is the right behaviour for browser-direct `file://` dev.
    resizeWindow(width, height, fixBits) {
      return this._call("resize_window", [width, height, fixBits], null);
    },
    // apply_upstream returns { ok:false, error:… } on bridge failure so
    // the caller can distinguish "couldn't reach Python" from a clean
    // {"ok":false,"error":"upstream not in config"} reply.
    // v0.65: optional ``model`` parameter — the sidebar dropdown lists
    // every (upstream, model) pair separately, so the picked model rides
    // along with the switch and Python persists both at once.
    async applyUpstream(platform, name, model = null) {
      return this._call(
        "apply_upstream",
        [platform, name, model],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // v0.65: 单独写 model 字段。``model=null`` 清除映射。
    async setUpstreamModel(platform, name, model) {
      return this._call(
        "set_upstream_model",
        [platform, name, model],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // v0.74: 单独写 default_model 字段。HTTP 端点是
    // PUT /api/upstreams/{platform}/{name}/default-model, body
    // {default_model: "..." | null}。目前 GUI 没单独用它
    // (default_model 走 updateUpstreamQuota 一并保存),保留以备后续
    // sidebar / 单独面板需要。
    async setUpstreamDefaultModel(platform, name, default_model) {
      return this._call(
        "set_upstream_default_model",
        [platform, name, default_model],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // Same {ok,error} contract as applyUpstream. ``payload`` carries
    // quota_5h / model_multipliers / allowed_models; Python validates
    // and persists it into upstreams.json.
    async updateUpstreamQuota(platform, name, payload) {
      return this._call(
        "update_upstream_quota",
        [platform, name, payload],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // v0.70：整盘替换 sidebar "快捷切换"列表（Settings 页增/删/改）。
    // Python 端走 replace_quick_switch helper,沿用 update_upstream_quota
    // 的 save_upstreams_json + reload_settings 链路,无需新加 HTTP 端点。
    async saveQuickSwitch(items) {
      return this._call(
        "save_quick_switch",
        [items],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // v0.155：平台别名映射（原始工具名 → 显示名，纯展示层）。
    async getAgentAliases() {
      return this._call("get_agent_aliases", [], {});
    },
    async saveAgentAliases(aliases) {
      return this._call(
        "save_agent_aliases",
        [aliases],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // v0.157：UA 归类规则（整串 UA → 平台名）。中继进程在请求入口消费。
    async getUaRules() {
      return this._call("get_ua_rules", [], {});
    },
    async saveUaRules(rules) {
      return this._call(
        "save_ua_rules",
        [rules],
        { ok: false, error: "bridge unavailable" }
      );
    },
    async getRecentUas(limit) {
      return this._call("get_recent_uas", [limit], []);
    },
    // v0.46："新建上游" 表单背后。payload 字段见 Python add_upstream，
    // 校验失败 Python 端返 {ok:false, error:"..."}。
    async createUpstream(platform, payload) {
      return this._call(
        "create_upstream",
        [platform, payload],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // v0.12：新建上游自动探测（wire/端点/认证/模型/key 有效性）。
    // 走 GUI 桥 /api/probe_upstream，可能耗时（探测一次约 20s）。
    async probeUpstream(url, api_key, model) {
      return this._call(
        "probe_upstream", [url, api_key, model || ""],
        { error: "bridge_unavailable" }, 25000
      );
    },
    // v0.12.1：新建上游"测试"按钮 —— 后台线程探测，日志实时推回
    // （window.relayProbeLog / relayProbeDone）。立即返回 started。
    // v0.95+（P2）：model 用表单里填的真实模型名，避免占位名被严格
    // 校验模型名的上游（DeepSeek V4 等）400 拒。
    testUpstream(url, api_key, model) {
      return this._call(
        "test_upstream", [url, api_key, model || ""],
        { error: "bridge_unavailable" }, 8000
      );
    },
    // v0.84：新建上游"连通性测试"按钮 —— 严格按当前表单配置（wire /
    // 鉴权 / 模型）构造真实消息发到上游。后台线程跑，日志经
    // relayProbeLog 实时推回，结束推 relayProbeDone。立即返回 started。
    connectivityTest(url, api_key, wire, auth_style, model) {
      return this._call(
        "connectivity_test", [url, api_key, wire, auth_style, model],
        { error: "bridge_unavailable" }, 8000
      );
    },
    // v0.64：上游详情卡片右上角 "×"，删掉整条 upstream 配置。
    // active 上游不允许删 —— 见 Python 端 remove_upstream 的注释，
    // 那种情况下 GUI 直接不渲染 × 按钮。
    async removeUpstream(platform, name) {
      return this._call(
        "remove_upstream",
        [platform, name],
        { ok: false, error: "bridge unavailable" }
      );
    },
    // v0.36 — paginated history + per-request conversation. Routed
    // through the bridge (not ``fetch('/requests')``) because pywebview
    // loads index.html via a ``file://`` URI; ``window.location.origin``
    // is "null" so fetch() silently fails. Same fallback semantics as
    // the other wrappers — empty list / error dict when the bridge
    // isn't there.
    fetchRequests(limit, beforeId) {
      // v0.9：bridge 失败的 fallback 不再伪装成"空结果"（{items:[],
      // next_before_id:null} 会被 loadHistoryPage 当成"已加载全部"，
      // 历史页从此钉死在"0 条"且永不重试）。带 error 标记，由调用方
      // 区分"桥不可用"与"真的没有更多行"。
      return this._call(
        "fetch_requests", [limit, beforeId],
        { error: "bridge_unavailable", items: [], next_before_id: null }
      );
    },
    fetchConversation(id) {
      return this._call("fetch_conversation", [id], { error: "bridge_unavailable", request_id: id });
    },
    // v0.101：统计页聚合 / 每日 —— 直接走 pywebview 桥（file:// origin 下
    // fetch() 拿不到中继，必须走桥；桥直接读 relay.db，路径最短）。
    statsAggregate(dim, range, top, mode) {
      return this._call(
        "stats_aggregate", [dim, range, top, mode || "relay"],
        { error: "bridge_unavailable" },
      );
    },
    statsDaily(days) {
      return this._call("stats_daily", [days], { error: "bridge_unavailable" });
    },
    // v0.153：单模型 30 天调用分布 —— 按 model × 日聚合（GUI 桥直读 relay.db）。
    statsModelDaily(days) {
      return this._call("stats_model_daily", [days], { error: "bridge_unavailable" });
    },
    // 完全透传模式桥接
    getPassthroughMode() {
      return this._call("get_passthrough_mode", [], { passthrough_mode: false });
    },
    passthroughOverview(range) {
      return this._call("passthrough_overview", [range || "30d"], { error: "bridge_unavailable" });
    },
    setPassthroughMode(enabled) {
      return this._call("set_passthrough_mode", [!!enabled], { error: "bridge_unavailable" });
    },
    getPassthroughUpstreams() {
      return this._call("get_passthrough_upstreams", [], { upstreams: [] });
    },
    renamePassthroughUpstream(url, modelFieldName, apiKeyAlias, displayName) {
      return this._call(
        "rename_passthrough_upstream",
        [url, modelFieldName, apiKeyAlias, displayName],
        { ok: false, error: "bridge_unavailable" },
      );
    },
  };

  // -------------------------------------------------------------------------
  // Tiny DOM helpers
  // -------------------------------------------------------------------------

  /** Format an integer with Western comma separators (1,234,567). */
  function fmtNum(n) {
    n = Number(n || 0);
    if (!isFinite(n)) return "—";
    return n.toLocaleString("en-US");
  }

  /** Format a token count with full Western comma separators
   *  (e.g. 62,847,193). Compact "62.8M" notation is intentionally not
   *  used — the UI prefers clarity over compactness. */
  function fmtTokens(n) {
    n = Number(n || 0);
    if (!isFinite(n)) return "—";
    return n.toLocaleString("en-US");
  }

  /** Convert a positive integer to the formal "大写" digit form
   *  (壹贰叁肆伍陆柒捌玖) used in financial Chinese. No grouping
   *  units (万/亿) — the digits run together so the eye can map
   *  each ASCII digit to its Chinese glyph one-for-one. */
  const _CN_UPPER_DIGITS = ["零", "壹", "贰", "叁", "肆", "伍", "陆", "柒", "捌", "玖"];
  function toFormalCn(n) {
    n = Number(n || 0);
    if (!isFinite(n) || n === 0) return "";
    const digits = String(Math.abs(Math.floor(n)));
    let out = "";
    for (const ch of digits) out += _CN_UPPER_DIGITS[Number(ch)];
    return out;
  }

  /** Format a unix timestamp as HH:MM:SS. */
  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }

  function $(id) { return document.getElementById(id); }
  function setText(id, value) { const el = $(id); if (el) el.textContent = value; }

  // -------------------------------------------------------------------------
  // Topbar
  // -------------------------------------------------------------------------

  // v0.40 性能：topbar 12+ 次 setText + querySelectorAll 每 500ms 跑
  // 一次，但 status 字段几秒甚至几分钟才变一次。sig 短路避免重复
  // 写入和不必要的 style/className 切换。
  let lastStatusSig = null;
  // v0.67：kill 旧进程到新 PID 就绪之间有秒级窗口，这期间 get_status
  // 可能仍然报告旧 PID 在跑（还没被 poll() 检测到已死）或者短暂拿
  // 不到端口——两种情况轮询画出来的都还是绿灯，用户完全看不出发生
  // 过重启。这个标志在 restartRelay() 期间为 true，期间 renderStatus
  // 直接跳过，不让下一次 500ms 轮询把手动画的"重启中…"过渡态盖掉。
  let isRestarting = false;
  function renderStatus(s) {
    if (isRestarting) return;
    // 透传模式标记：renderAll 每 tick 从 snapshot 写入，renderStatus
    // 拼进 sig 与 label，切换时立即反映到顶部状态栏。
    const pt = !!(window._ptMode);
    // Bridge may return null while pywebview is still wiring up the
    // window — never leave the topbar stuck on the HTML placeholder
    // "加载中…". Show a degraded "unknown" state instead.
    const sig = s
      ? (s.running ? "1" : "0") + "|" + (s.owner || "") + "|" + (s.pid || "") + "|" + (s.port || "") + "|" + (pt ? "PT" : "")
      : "null";
    if (sig === lastStatusSig) return;
    lastStatusSig = sig;
    if (!s) {
      // 顶栏文案不在 applyLang 选择器内，模板直接出译文。
      setText("status-label", t("状态未知"));
      setText("status-meta", t("等待 bridge 响应…"));
      const btn = $("btn-toggle");
      if (btn) { btn.disabled = true; btn.title = t("等待 bridge 响应"); }
      const btnRestart = $("btn-restart");
      if (btnRestart) { btnRestart.disabled = true; btnRestart.title = t("等待 bridge 响应"); }
      document.querySelectorAll(".status-dot").forEach(dot => {
        dot.classList.remove("dot-running");
        dot.classList.add("dot-stopped");
      });
      return;
    }
    // "external" = relay started by someone else (autostart entry, a
    // terminal, an earlier GUI session). We monitor it but never offer
    // to stop it — killing :8088 kills the session it carries.
    const external = s.owner === "external";
    // labelZh 保持中文原文：侧栏由 applyLang 翻译（含 recapture），顶栏
    // 用 t() 单独出译文。分开处理避免 applyLang 首见捕获到译文。
    const labelZh = s.running ? (external ? "运行中（外部）" : "运行中") : "已停止";
    const hint = "中继由外部进程启动，本窗口仅监控";
    // 透传模式：状态栏追加（透传模式）标记
    const labelWithPt = pt ? (labelZh + "（透传模式）") : labelZh;
    setText("status-label", pt ? (t(labelZh) + t("（透传模式）")) : t(labelZh));
    setText("status-meta", s.running
      ? `PID ${s.pid || "—"} · ${t("端口")} ${s.port}${external ? " · " + t("非本窗口启动") : ""}`
      : t("点击启动以启动中继"));
    const btn = $("btn-toggle");
    if (btn) {
      btn.textContent = s.running ? "停止" : "启动";
      // Disabled rather than hidden so the topbar layout doesn't jump
      // between polls; the tooltip explains why it's inert.
      btn.disabled = external;
      btn.title = external ? hint : "";
    }
    const btnRestart = $("btn-restart");
    if (btnRestart) {
      // v0.65：重启按钮在 external 状态下**不能**禁用 —— 这正是
      // 用户想要接管的场景（外部启动的中继占着 8088）。toggle
      // 按钮（启/停）仍然禁用外部停止，那是无确认的破坏性动作；
      // 重启带"接管"语义，用户已经主动选择了它，按下必须生效。
      btnRestart.disabled = false;
      btnRestart.title = external ? "接管 8088：杀掉外部中继并启动本窗口的进程" : "";
    }
    // Sidebar status card（保持中文原文，applyLang 负责翻译）
    setText("sidebar-status-text", labelWithPt);
    setText("sidebar-status-meta", s.running
      ? `PID ${s.pid || "—"} · :${s.port}`
      : "中继未运行");
    // Status dot color via class
    document.querySelectorAll(".status-dot").forEach(dot => {
      dot.classList.toggle("dot-running", !!s.running);
      dot.classList.toggle("dot-stopped", !s.running);
    });
    // v0.120a：hero-title 根据当前模式切换 —— 透传模式显示「透传模式」，
    // 否则显示「转换模式」。原来固定写「中继」现在按模式切换。
    const heroTitle = document.querySelector(".hero-title");
    if (heroTitle) heroTitle.textContent = pt ? "透传模式" : "转换模式";
  }

  // v0.67：统一的"重启"入口 —— 手动点重启按钮和切换上游/模型后的强制
  // 重启都走这里，保证过渡态视觉一致。流程：立刻把 isRestarting 置 true
  // （挡住 500ms 轮询的 renderStatus 把过渡态盖掉）、手动把顶栏 + 侧栏
  // 文案改成"重启中…"、把所有 .status-dot 涂成橙色脉冲的
  // dot-restarting，再等 api.restart() 返回。无论成功失败都要在
  // finally 里把 isRestarting 落回 false、清掉 lastStatusSig 强制下一次
  // renderStatus 全量重画，并主动拉一次 status/snapshot——不然要等到下
  // 一个 500ms tick 才恢复,过渡态会看起来卡住。
  async function restartRelay() {
    if (isRestarting) return;
    isRestarting = true;
    setText("status-label", t("重启中…"));
    setText("status-meta", t("正在接管端口") + " 8088…");
    setText("sidebar-status-text", "重启中…");
    setText("sidebar-status-meta", "正在重启…");
    const btn = $("btn-toggle");
    if (btn) btn.disabled = true;
    const btnRestart = $("btn-restart");
    if (btnRestart) btnRestart.disabled = true;
    document.querySelectorAll(".status-dot").forEach(dot => {
      dot.classList.remove("dot-running", "dot-stopped");
      dot.classList.add("dot-restarting");
    });
    let restartErr = null;
    try {
      const res = await api.restart();
      if (res && res.error) {
        restartErr = res.error;
        alert("重启失败：" + res.error);
      }
    } catch (e) {
      // v0.93：捕获 bridge / restart_server 抛出的异常，记下来 + 报警。
      // 之前 catch-all 都在 gui.py 里 silent swallow 了，这里是 JS 端最后
      // 一道防线 —— 给用户明确的反馈，不要静默。
      restartErr = String(e && e.message || e);
      console.error("restartRelay bridge call failed:", e);
      alert("重启失败：" + restartErr);
    } finally {
      isRestarting = false;
      // v0.93：清掉**所有**视图 sig —— 不只 lastStatusSig。restart 之后
      // active_per_platform 必然变化，sidebar / 上游页 / 卡片都得重建。
      // 之前只清 lastStatusSig 靠 500ms tick 顺带渲染，但切换 active 这条
      // 路径上 tick 偶尔会被节流 / 跳过，用户看到"切换器变了其它没变"。
      lastStatusSig = null;
      lastSidebarSig = null;
      lastUpstreamsSig = null;
      lastActivePerPlatform = {};
      autoswitchToasts.clear();
      _quotaBarsCache = null;
      _quotaBarsOwner = null;
      document.querySelectorAll(".status-dot").forEach(dot => {
        dot.classList.remove("dot-restarting");
      });
      // 主动拉一次 snapshot + status 并全量重渲，不等 500ms tick。
      try {
        const [snap, status] = await Promise.all([api.snapshot(), api.status()]);
        if (snap) lastSnap = snap;
        if (status) lastStatus = status;
        renderAll(lastSnap, lastStatus);
        renderSidebar(lastSnap, lastStatus);
        renderSidebarQuota(lastSnap);
        renderUpstreamsView($("card-upstreams-detail-body"), lastSnap, lastStatus);
        renderActiveView(lastSnap, lastStatus);
        if (status) renderStatus(status);
      } catch (e) {
        console.error("restartRelay post-refresh failed:", e);
      }
      // 顶栏按钮恢复可用（不管 restart 成不成功）
      if (btn) btn.disabled = false;
      if (btnRestart) btnRestart.disabled = false;
    }
    return restartErr ? { error: restartErr } : { ok: true };
  }

  // -------------------------------------------------------------------------
  // Cards
  // -------------------------------------------------------------------------

  function renderToday(body, snap) {
    if (!snap) {
      body.innerHTML = '<div class="card-empty">暂无数据</div>';
      return;
    }
    // 今日用量：只读 today 桶，回退到 by_platform 是为了老 GUI
    // 版本仍能渲染（不会显示空数据）。
    const totals = snap.by_platform_today || snap.by_platform || {};
    let reqs = 0, inT = 0, outT = 0, errs = 0;
    Object.values(totals).forEach(p => {
      reqs += p.requests || 0;
      inT  += p.input_tokens || 0;
      outT += p.output_tokens || 0;
      errs += p.errors || 0;
    });
    body.innerHTML = `
      <div class="stat-row"><span class="stat-label">请求数</span><span class="stat-value">${fmtNum(reqs)}</span></div>
      <div class="stat-row"><span class="stat-label">输入 tokens</span><span class="stat-value">${fmtTokens(inT)}</span></div>
      <div class="stat-row"><span class="stat-label">输出 tokens</span><span class="stat-value">${fmtTokens(outT)}</span></div>
      <div class="stat-row"><span class="stat-label">错误</span><span class="stat-value ${errs ? 'stat-error' : ''}">${fmtNum(errs)}</span></div>
    `;
  }

  // v0.45：上一次 by_upstream 的 counts 快照。每次 tick 比较各
  // upstream 的 counts（5h/week/month 任一）是否增长，增长的行
  // 触发一次"波纹"动画（与侧栏"实时"同 keyframes）。模块级单例，
  // 不进 window。
  let lastUpstreamCounts = null;
  // v0.51：跨 innerHTML rebuild 保活 bumped 类。
  //
  // renderUpstream 每次 tick 都把 card-upstream-body 整个 innerHTML
  // 重建一遍，而"counts 变化"本身就会让 sig 变化、触发重建。所以一
  // 个 bumped row 的动画生命周期里会被重建至少 4 次 (500ms 一次)，
  // 旧 DOM 节点被替换、动画中断 —— 用户看到的就是"中间位置开始、
  // 左侧位置结束、被中途截断"。
  //
  // 修法：模块级 Set 记住所有"还在波纹窗口内"的 upstream 名，每次
  // 重建时根据这个集合重新挂上 .upstream-row-bumped 类，setTimeout
  // 只负责把名字从集合里摘掉。新 bump 在窗口期内复用同一个 timer，
  // 不会延长窗口、不会重启动画 —— 动画总能跑完一个完整周期。
  const bumpedUpstreams = new Set();
  const bumpRemovers = new Map();  // name -> setTimeout id
  // v0.52：对称的"减少动画"。逻辑与 bumped 完全镜像：模块级 Set
  // 跨 rebuild 保活，timer 在窗口到期时摘除。bumped 与 decreased 互
  // 斥：进 decreased 时先把 row 从 bumpedUpstreams 摘掉（反之亦然），
  // 避免 ::after 上同时挂两个动画类导致不可预测的行为。
  const decreasedUpstreams = new Set();
  const decreaseRemovers = new Map();

  function renderUpstream(body, snap, status) {
    if (!snap) {
      body.innerHTML = '<div class="card-empty">暂无数据</div>';
      return;
    }
    const up = snap.by_upstream || {};
    const entries = Object.entries(up);
    if (!entries.length) {
      body.innerHTML = '<div class="card-empty">暂无请求</div>';
      return;
    }
    // 当前激活的 upstream 集合。只取 anthropic 平台的 active，openai
    // 按用户要求不进高亮集合；未来要加更多平台在这里加分支。
    const activeSet = new Set();
    if (status && status.active_per_platform) {
      const ap = status.active_per_platform;
      if (ap.anthropic) activeSet.add(ap.anthropic);
    }
    const windows = ["5h", "week", "month"];
    // v0.92：排序修正 —— ①当前激活置顶；②其余全部按累计 tokens 降序
    // （消耗量大在前）。去掉 v0.80 引入的"最近调用单席"（last_ts 最大
    // 者独立占位）：用户明确要求只按 active + 消耗量两级排序，"最近调
    // 用"一席会把 token 用量大的上游压下去。
    entries.sort((a, b) => {
      const [na] = a;
      const [nb] = b;
      const rank = (n) => (activeSet.has(n) ? 0 : 1);
      const ra = rank(na), rb = rank(nb);
      if (ra !== rb) return ra - rb;
      return (b[1].total_tokens || 0) - (a[1].total_tokens || 0);
    });
    // 先扫一遍算出本 tick 的新 bump / 新 decrease —— 后面 setTimeout
    // 安排 + 互斥清理都依赖这两个数组。
    const newBumps = [];
    const newDecreases = [];

    // v0.55：decrease 只看 5h，bump 仍看任一窗口。
    //
    // 之前的 windows.some(w => counts[w] < last[w]) 把 week / month
    // 也算进来 —— week 7 天边界、month 30 天边界各自滚动时，对应窗
    // 口的计数也会下降（虽然 snapshot 通常会把那些窗 clamp 到 >0，
    // 但不保证）。结果就是偶现"绿波纹跑到根本没发生 5h 释放的 row
    // 上" —— 也就是用户看到的"绿色定位到错误的 api"。
    //
    // 语义上 decrease 就是"5h 配额释放"事件，week / month 的窗滚
    // 用户根本不关心（前端连 5h 释放时间都不显示给 week/month），
    // 把触发条件收窄到 5h 一个窗，绿波纹只在真正有意义的下降时出
    // 现。
    //
    // 冲突策略（bump && decrease 同时为 true）：
    //   bump 赢 —— 5h 下降 + 其它窗增加 = 一次新请求刚好踩在 5h
    //   边界滚动的那一刻，用户注意力在"我刚发的请求"，bump 是主信
    //   号；decrease 是附带现象。两者画在同一个 row ::after 上时，
    //   button-primary（暗）的 bump 会盖住 --text-success（绿）的
    //   decrease，绿几乎不可见。强制 bump 优先 = 视觉锚点统一，
    //   不会出现"刚才到底是加还是减"的认知断裂。
    //
    // v0.54：in-place row diff。上一版每次 tick 都
    // body.innerHTML = html，把所有 .upstream-row 销毁重建 →
    // CSS ::after 的 live-ripple 动画在新元素上从 0% 重启。
    // cubic-bezier 尾巴很陡，~450ms (30% of 1.5s sweep) 就跑到 98%
    // 处，彩条贴在最左边 —— 正好下次 poll 到达，老 row 被砍掉、新
    // row 从 0% 起跑。2s 窗口里砍 4 次 = "最左边闪 4 下"。
    //
    // 修法：querySelectorAll 拿旧 row Map，按 name diff：
    //   - 旧 row 仍在新数据里 → 复用同一个 DOM 元素，classList 增删
    //     （不替换 className，CSS 动画在原 ::after 上继续跑完），
    //     querySelector 改 .upstream-windows / .upstream-release /
    //     .upstream-name 的文本。
    //   - 全新 upstream → 新建 row（无动画可保活，全新 className）。
    //   - 不在新数据里的旧 row → remove。
    // 最后 body.replaceChildren(fragment) 重排顺序。
    const existingRows = new Map();
    body.querySelectorAll('.upstream-row[data-upstream]').forEach(r => {
      existingRows.set(r.dataset.upstream, r);
    });

    // v0.56：上一版的 fragment.appendChild(row) + body.replaceChildren(fragment)
    // 会把已存在的 row 从 body 摘下来再装回去。Chromium 跨 detach/
    // reattach 在某些情况下会重启 CSS 动画 —— 表现为"动画还是被中途
    // 截断"，跟 v0.54 之前的 bug 同症。
    //
    // 改成完全不挪动已存在的 row：复用同一个 DOM 元素，classList
    // 增删 + targeted querySelector 改内部文本，绝不 detach。只有
    // 全新 row 才 appendChild 到 body 末尾；消失的 row 直接 remove。
    // 这样 ::after 上的 live-ripple / reverse-live-ripple 在整个 2s
    // 窗口里跑同一个元素、同一个 ::after，绝对不被重启。
    //
    // 取舍：顺序按 snapshot 的顺序由插入顺序决定，但已存在 row 的位
    // 置不动 —— 万一 entries 顺序变了（比如删了中间一个再补回来），
    // 视觉上可能跟新顺序不一致。by_upstream 的 key 顺序在 snapshot
    // 里基本稳定（取决于后端怎么排），这种错位很少见。

    for (const [name, data] of entries) {
      const counts = data.counts || {};
      // bump = 任一窗口计数上涨（请求进来三个窗都 +1）
      // v0.11.3: 闪烁偏好关掉时跳过 bump / decrease 检测（波纹类不再挂）。
      const isNewBump = prefs.flash && !!lastUpstreamCounts && windows.some(w => {
        return (counts[w] || 0) > (lastUpstreamCounts[name]?.[w] || 0);
      });
      // v0.55：decrease 只看 5h 一窗。week / month 的窗滚会被错
      // 误触发，详见上方注释。
      const isNewDecrease = prefs.releaseFlash && !!lastUpstreamCounts
        // #9：按 Token 计费的上游取消"5h 配额释放"闪烁（没有窗口滚动语义）
        && data.billing_unit !== "token"
        && (counts["5h"] || 0) < (lastUpstreamCounts[name]?.["5h"] || 0);
      // 一个 row 在波纹的两种情况：
      //   1) 本 tick 刚检测到计数上涨 (isNewBump)
      //   2) 之前检测过、setTimeout 还没到 (bumpedUpstreams 还在)
      // bump 优先于 decrease —— 详见 v0.55 冲突策略注释。
      const isBumped = isNewBump || bumpedUpstreams.has(name);
      const isDecreased = !isBumped && (isNewDecrease || decreasedUpstreams.has(name));
      if (isNewBump) newBumps.push(name);
      if (isNewDecrease && !isNewBump) newDecreases.push(name);

      const isActive = activeSet.has(name);
      const isTokenBilled = data.billing_unit === "token";
      const WIN_LABEL = { "5h": "5h", week: t("周"), month: t("月") };
      // v0.82：按次数计费的上游在名称旁追加累计 token 数字（纯数字，
      // 无 label）。flex 布局下名称可截断（ellipsis）、数字不收缩，
      // 名称过长时优先保住数字。按用量计费上游不做任何变更。
      const nameInner = isTokenBilled
        ? `${escape(name)}${isActive ? '<span class="upstream-active-dot" aria-label="当前激活"></span>' : ''}`
        : `<span class="upstream-name-text" data-i18n-keep>${escape(name)}</span>${isActive ? '<span class="upstream-active-dot" aria-label="当前激活"></span>' : ''}<span class="upstream-name-tokens">${fmtTokens(data.total_tokens || 0)}</span>`;
      // Token 计费的上游：一次请求可能是 100 tokens 也可能是 10 万，
      // 5h/周/月的"请求数"窗口对它没有意义，改显示累计 token 总数。
      const winHtml = isTokenBilled
        ? `<div class="upstream-window upstream-window-tokens">
             <span class="upstream-window-label">${t("累计 tokens")}</span>
             <span class="upstream-window-value">${fmtTokens(data.total_tokens || 0)}</span>
           </div>`
        : windows.map(w => `
        <div class="upstream-window">
          <span class="upstream-window-label">${escape(WIN_LABEL[w] || w)}</span>
          <span class="upstream-window-value">${counts[w] || 0}</span>
        </div>`).join("");
      const relText = data["5h_release_text"] || "—";
      const near = relText === "now" || relText === "<1m";
      const desired = ["upstream-row", "upstream-row-clickable"];
      if (isActive) desired.push("upstream-row-active");
      if (isBumped) desired.push("upstream-row-bumped");
      if (isDecreased) desired.push("upstream-row-decreased");

      let row = existingRows.get(name);
      if (row) {
        // In-place：classList 增删（diff），不动 className，不 detach，
        // 正在跑的 live-ripple / reverse-live-ripple 在原 ::after 上
        // 继续跑完。
        row.draggable = true;
        const current = new Set(row.className.split(/\s+/).filter(Boolean));
        const want = new Set(desired);
        for (const c of want) if (!current.has(c)) row.classList.add(c);
        for (const c of current) if (!want.has(c)) row.classList.remove(c);

        // 改 name + active dot + 按次数计费的 token 数字
        const nameEl = row.querySelector(".upstream-name");
        if (nameEl && nameEl.innerHTML !== nameInner) nameEl.innerHTML = nameInner;

        // 改 counts（5h/week/month 三个窗口）。只在不等时写，减少
        // layout thrash。
        const windowsEl = row.querySelector(".upstream-windows");
        if (windowsEl && windowsEl.innerHTML !== winHtml) windowsEl.innerHTML = winHtml;

        // 改 5h 释放时间 + near 标 —— token 计费的上游没有"5h 释放"
        // 这个概念（累计总量不随窗口滚动释放），整行隐藏。
        const releaseEl = row.querySelector(".upstream-release");
        if (releaseEl) {
          releaseEl.hidden = isTokenBilled;
          if (!isTokenBilled) {
            // v0.154：「5h 释放」标签单独成节点（整 key 翻译），倒计时值
            // 独立 —— 避免"5h 释放 —"合在一个文本节点里被 segment 半截翻
            // 译（释放→釋放），导致跨语言切不回来。
            const labelEl = releaseEl.querySelector(".upstream-release-label");
            if (labelEl) { const lt = t("5h 释放"); if (labelEl.textContent !== lt) labelEl.textContent = lt; }
            const valEl = releaseEl.querySelector(".upstream-release-value");
            if (valEl && valEl.textContent !== relText) valEl.textContent = relText;
            releaseEl.classList.toggle("upstream-release-near", near);
          }
        }

        existingRows.delete(name);
        // 不 detach、不挪动。
      } else {
        // 全新 upstream：新建 row，全 className（动画在新 ::after
        // 上从 0% 起跑 —— 这是期望）。appendChild 到 body 末尾。
        row = document.createElement("div");
        row.className = desired.join(" ");
        row.dataset.upstream = name;
        // v0.11.3: 上游行可拖拽排序。
        row.draggable = true;
        // v0.68：点击行为从"跳转设置编辑"改成"弹按模型拆分详情"——
        // title 文案跟着更新，避免误导。
        row.title = "查看按模型拆分的详细调用情况";
        row.innerHTML = `
          <div class="upstream-name">${nameInner}</div>
          <div class="upstream-windows">${winHtml}</div>
          <div class="upstream-release ${near ? 'upstream-release-near' : ''}" ${isTokenBilled ? "hidden" : ""}><span class="upstream-release-label" data-i18n="5h 释放">${t("5h 释放")}</span> <span class="upstream-release-value">${escape(relText)}</span></div>
        `;
        body.appendChild(row);
      }
    }

    // 不在新数据里的旧 row → 删
    for (const r of existingRows.values()) r.remove();

    // 更新本次快照，留给下次 tick diff
    lastUpstreamCounts = {};
    for (const [name, data] of entries) {
      lastUpstreamCounts[name] = { ...(data.counts || {}) };
    }
    // 新 bump 加进保活集合。timer 只在第一次见到这个名字时安排 —
    // 窗口期内再触发 bump 不重启 timer，动画总能跑完一个完整周期。
    // 2s 与 live-ripple keyframes 总时长对齐。
    for (const name of newBumps) {
      if (bumpedUpstreams.has(name)) continue;
      // 进入 bumped 时把同 row 从 decreased 集合里摘出来 + 摘类，
      // 避免 ::after 上两个动画类并存。
      if (decreasedUpstreams.has(name)) {
        const dt = decreaseRemovers.get(name);
        if (dt) clearTimeout(dt);
        decreaseRemovers.delete(name);
        decreasedUpstreams.delete(name);
        const row = body.querySelector(`[data-upstream="${attr(name)}"]`);
        if (row) row.classList.remove("upstream-row-decreased");
      }
      bumpedUpstreams.add(name);
      // 摘类计时与 live-ripple 动画周期（1.8s）对齐 —— 波纹正好播完
      // 一遍再淡出，不被中途截断。
      const tid = setTimeout(() => {
        bumpedUpstreams.delete(name);
        bumpRemovers.delete(name);
        // 当前 DOM 里的 row 也跟着摘掉类 —— 万一保活集合里剩下的
        // entry 在 tick 间被换掉，这里是最后一次清理机会。
        const row = body.querySelector(`[data-upstream="${attr(name)}"]`);
        if (row) row.classList.remove("upstream-row-bumped");
      }, 1800);
      bumpRemovers.set(name, tid);
    }
    // v0.52：减少动画的镜像逻辑。
    for (const name of newDecreases) {
      if (decreasedUpstreams.has(name)) continue;
      if (bumpedUpstreams.has(name)) {
        const bt = bumpRemovers.get(name);
        if (bt) clearTimeout(bt);
        bumpRemovers.delete(name);
        bumpedUpstreams.delete(name);
        const row = body.querySelector(`[data-upstream="${attr(name)}"]`);
        if (row) row.classList.remove("upstream-row-bumped");
      }
      decreasedUpstreams.add(name);
      const tid = setTimeout(() => {
        decreasedUpstreams.delete(name);
        decreaseRemovers.delete(name);
        const row = body.querySelector(`[data-upstream="${attr(name)}"]`);
        if (row) row.classList.remove("upstream-row-decreased");
      }, 1800);
      decreaseRemovers.set(name, tid);
    }
    // v0.77：自动排序 —— 按上面排序后的 entries 顺序重排行。
    applyUpstreamRowOrder(body, entries);
    // v0.11.15: 全页闪烁 —— 本次 tick 有新 bump 时整窗闪一次。
    if (newBumps.length) triggerWholeFlash();
  }

  function renderPlatform(body, snap) {
    if (!snap) {
      body.innerHTML = '<div class="card-empty">暂无数据</div>';
      return;
    }
    const totals = snap.by_platform || {};
    const entries = Object.entries(totals);
    if (!entries.length) {
      body.innerHTML = '<div class="card-empty">暂无请求</div>';
      return;
    }
    body.innerHTML = entries.map(([k, v]) => {
      // v0.143：key 是 "<platform>:<endpoint>"（旧 anthropic 行 = "anthropic"）。
      // 拆出 platform 和 endpoint 用于徽标配色和 label。
      const hasColon = k.indexOf(":") >= 0;
      const plat = hasColon ? k.slice(0, k.indexOf(":")) : k;
      const endpoint = hasColon ? k.slice(k.indexOf(":") + 1) : null;
      const total = (v.input_tokens || 0) + (v.output_tokens || 0);
      // endpoint === plat 表示这是「单 endpoint 平台」（anthropic），
      // 行为与旧版一致 —— 徽标只挂 platform 类，不挂 endpoint 类。
      const showEndpoint = endpoint && endpoint !== plat;
      const badgeCls = showEndpoint
        ? `platform-badge platform-${escape(plat)} platform-endpoint-${escape(endpoint)}`
        : `platform-badge platform-${escape(plat)}`;
      const label = showEndpoint
        ? `${escape(plat)}·${escape(endpoint.replace(/^openai-/, ""))}`
        : escape(plat);
      return `
        <div class="platform-row">
          <span class="${badgeCls}" data-i18n-keep>${label}</span>
          <span class="platform-meta" data-i18n-keep>${fmtNum(v.requests || 0)} ${t("请求")} · ${fmtTokens(total)} tokens</span>
        </div>`;
    }).join("");
  }

  // v0.155：平台流量卡片 —— 按客户端工具（agent）拆。原始 agent 名落库
  // （claude-code / opencode / codex / 其它…），显示名走 agent_aliases 映射
  // （纯展示层，见 agent.py / config.py 的 agent_aliases 持久化）。v0.156 起
  // 有 UA 的请求动态抽 token 建平台，只有完全无 UA 才归兜底桶「其它」。
  function agentDisplayName(raw) {
    if (!raw) return "其它";
    // v0.159：agent_aliases value 升级为 {name, color} 对象，向后兼容旧
    // string 值（load 时已转对象，但代码层面这里再防一手）。
    const entry = agentAliases && agentAliases[raw];
    if (!entry) return raw;
    if (typeof entry === "string") return entry;   // 兼容意外漏升的旧值
    return entry.name || raw;
  }

  // v0.159：用户手配的徽标底色（CSS color 字符串）。没设 = null → 渲染时
  // 走 css 默认 .agent-{raw} 配色。
  function agentBadgeColor(raw) {
    const entry = agentAliases && agentAliases[raw];
    if (!entry) return null;
    if (typeof entry === "string") return null;
    return entry.color || null;
  }

  // v0.162：per-row 显示模式（"both" | "requests" | "tokens"）。
  // 用户在 modal 里改 → agent_aliases[raw].view。没设 = "both"。
  function agentRowView(raw) {
    const entry = agentAliases && agentAliases[raw];
    if (!entry) return "both";
    if (typeof entry === "string") return "both";
    const v = entry.view;
    return (v === "requests" || v === "tokens" || v === "both") ? v : "both";
  }

  // v0.xxx：平台流量竖状图 —— 总览「平台流量」卡从列表改为 D3 竖向条形图。
  // Y = 各平台 tokens（in + out + cache，口径与旧列表一致），柱色用
  // agent_aliases 用户色 / CSS 默认色；柱顶标缩写 tokens；点击柱打开
  // 原有的编辑 modal（openAgentAliasModal）。数据来自 snapshot.by_agent，
  // 每 tick 由 renderAll 重渲染（同卡其它内容一致）。
  // 平台默认色表（与 .agent-badge.agent-* CSS 同步；缺省兜底灰）。
  const AGENT_BAR_COLORS = {
    "claude-code": "#d97706",
    "opencode": "#10a37f",
    "codex": "#2563eb",
    "openclaw": "#c026d3",
    "ai-sdk": "#8b5cf6",
  };
  function agentBarColor(raw) {
    const user = agentBadgeColor(raw);
    if (user) return user;
    return AGENT_BAR_COLORS[raw] || "#94a3b8";
  }

  // v0.xxx：平台流量卡显示模式 —— 纯列表 / 竖状图 并行，用户可切换。
  // 存 localStorage（每实例独立，但通常只有一张卡，够用）。默认竖状图。
  const AGENT_VIEW_KEY = "agent-card-view-v1";
  function getAgentCardView() {
    try {
      const v = localStorage.getItem(AGENT_VIEW_KEY);
      if (v === "chart" || v === "list") return v;
    } catch (_) {}
    return "chart";
  }
  function setAgentCardView(v) {
    try { localStorage.setItem(AGENT_VIEW_KEY, v); } catch (_) {}
  }

  // 旧的纯列表渲染（v0.159/0.162 逐行徽标 + meta）。保留与竖状图并行。
  function renderAgentList(body, snap) {
    const totals = snap.by_agent || {};
    const entries = Object.entries(totals);
    if (!entries.length) {
      body.innerHTML = '<div class="card-empty">暂无请求</div>';
      return;
    }
    body.innerHTML = entries.map(([k, v]) => {
      const total = (v.input_tokens || 0) + (v.output_tokens || 0)
        + (v.cache_read_input_tokens || 0) + (v.cache_creation_input_tokens || 0);
      const display = agentDisplayName(k);
      const badgeCls = `agent-badge agent-${escape(k)}`;
      const userColor = agentBadgeColor(k);
      // v0.159：用户手配的颜色用 inline style 覆盖 css 默认；整行 cursor=pointer
      // 提示可点击 → 弹出编辑 modal。徽标点击事件委托在 modal open。
      const badgeStyle = userColor
        ? ` style="background:${escape(userColor)}"`
        : "";
      // v0.162：per-row 显示模式（"both" | "requests" | "tokens"）。
      // 用户在点击某平台的 modal 里选，三档分别控制右侧 meta 文案。
      const viewMode = agentRowView(k);
      const meta = (() => {
        if (viewMode === "requests") return `${fmtNum(v.requests || 0)} ${t("请求")}`;
        if (viewMode === "tokens") return `${fmtTokens(total)} tokens`;
        return `${fmtNum(v.requests || 0)} ${t("请求")} · ${fmtTokens(total)} tokens`;
      })();
      return `
        <div class="platform-row" data-agent-row="${escape(k)}"
             title="点击编辑显示名 / 颜色 / 显示模式" style="cursor:pointer">
          <span class="${badgeCls}" data-i18n-keep${badgeStyle}>${escape(display)}</span>
          <span class="platform-meta" data-i18n-keep>${meta}</span>
        </div>`;
    }).join("");
  }

  // 竖状图渲染（v0.xxx）。Y = tokens（in+out+cache），柱色 agent 色，
  // 柱顶标值，点击柱 → 编辑 modal。
  function renderAgentChart(body, snap) {
    const totals = snap.by_agent || {};
    const entries = Object.entries(totals).map(([k, v]) => ({
      raw: k,
      name: agentDisplayName(k),
      color: agentBarColor(k),
      requests: v.requests || 0,
      total: (v.input_tokens || 0) + (v.output_tokens || 0)
        + (v.cache_read_input_tokens || 0) + (v.cache_creation_input_tokens || 0),
    })).filter(d => d.total > 0 || d.requests > 0);
    if (!entries.length) {
      body.innerHTML = '<div class="card-empty">暂无请求</div>';
      return;
    }
    entries.sort((a, b) => b.total - a.total);

    body.innerHTML = "";
    const size = _statsHostSize(body);
    const w = Math.max(200, size.w || 320);
    const h = Math.max(180, size.h || 200);
    const padL = 4, padR = 4, padT = 18, padB = 34;

    const iw = w - padL - padR;
    const ih = h - padT - padB;
    const x = d3.scaleBand()
      .domain(entries.map((_, i) => i))
      .range([0, iw])
      .paddingInner(0.28)
      .paddingOuter(0.1);
    const maxV = d3.max(entries, d => d.total) || 1;
    const y = d3.scaleLinear().domain([0, maxV * 1.08]).range([ih, 0]);

    const svg = d3.select(body).append("svg")
      .attr("viewBox", `0 0 ${w} ${h}`)
      .attr("preserveAspectRatio", "xMidYMid meet")
      .attr("width", "100%").attr("height", "100%");
    const g = svg.append("g").attr("transform", `translate(${padL},${padT})`);

    // 横网格线（4 档）。
    g.selectAll("line.ag-grid").data(y.ticks(4)).enter().append("line")
      .attr("x1", 0).attr("x2", iw).attr("y1", d => y(d)).attr("y2", d => y(d))
      .attr("class", "ag-grid");

    // 柱 + 顶部数值 + 平台名（长名截断，悬停 tooltip 看全名 + 请求数）。
    g.selectAll("rect.ag-bar").data(entries).enter().append("rect")
      .attr("class", "ag-bar")
      .attr("data-agent-row", d => d.raw)
      .attr("x", (_, i) => x(i))
      .attr("width", x.bandwidth())
      .attr("y", d => y(d.total))
      .attr("height", d => Math.max(0, ih - y(d.total)))
      .attr("rx", 3)
      .style("fill", d => d.color)
      .style("fill-opacity", 0.85)
      .style("cursor", "pointer")
      .append("title")
      .text(d => `${d.name}\n${fmtNum(d.requests)} 请求 · ${fmtTokens(d.total)} tokens`);

    g.selectAll("text.ag-val").data(entries).enter().append("text")
      .attr("class", "ag-val")
      .attr("text-anchor", "middle")
      .attr("x", (_, i) => x(i) + x.bandwidth() / 2)
      .attr("y", d => y(d.total) - 4)
      .text(d => fmtTokens(d.total));

    g.selectAll("text.ag-label").data(entries).enter().append("text")
      .attr("class", "ag-label")
      .attr("data-i18n-keep", "")
      .attr("text-anchor", "middle")
      .attr("x", (_, i) => x(i) + x.bandwidth() / 2)
      .attr("y", ih + 16)
      .text(d => (d.name.length > 10 ? d.name.slice(0, 9) + "…" : d.name));
  }

  function renderAgent(body, snap) {
    if (!snap) {
      body.innerHTML = '<div class="card-empty">暂无数据</div>';
      return;
    }
    const view = getAgentCardView();
    // 卡级 class 控制 body 高度：list → 自然高度，chart → 200px。
    const card = body.closest(".glass-card");
    if (card) {
      card.classList.toggle("agent-view-list", view === "list");
      // 同步标题栏切换按钮的 active 态。
      card.querySelectorAll(".agent-view-btn").forEach(b => {
        b.classList.toggle("active", b.dataset.agentView === view);
      });
    }
    if (view === "list") renderAgentList(body, snap);
    else renderAgentChart(body, snap);
  }

  // v0.159：总览「平台流量」点击编辑 modal —— 名字 + 完整色环（hue 0-360
  // + lightness slider + 实时预览 + 重置颜色 / 重置整条 / 保存）。复用
  // #modal-overlay（同 openRequestDetail）+ agentAliases 缓存（与设置
  // 页「平台别名」同一份）。
  //
  // 配色：v0.160 改成离散色板网格点击直选（HSL 滑块太繁琐）。stateColor
  // 直接存 "#rrggbb"。保存时进 agent_aliases[raw] = {name, color}。

  // v0.160：Material Design 风格的 12×8 离散色板（参考用户给的产品截图）。
  // 12 色相列：红/橙/琥珀/黄/青柠/绿/青/天蓝/蓝/紫/品红/粉；8 行色阶：自上而下
  // 由浅至深。展开成 96 个 #rrggbb 串，渲染时填到 .alias-swatch-grid。
  // 调色原则：徽标底色需要可读性（白色文字），所以中间行（40-65% lightness）
  // 饱和度更高，两端做柔和渐变；最末两行偏中性灰，避免压死对比度。
  const __MATERIAL_SWATCHES = [
    // 红 (#ef4444 系族)
    "#fecaca", "#fca5a5", "#f87171", "#ef4444", "#dc2626", "#b91c1c", "#7f1d1d", "#450a0a",
    // 橙 (#f97316)
    "#fed7aa", "#fdba74", "#fb923c", "#f97316", "#ea580c", "#c2410c", "#7c2d12", "#431407",
    // 琥珀 (#f59e0b)
    "#fde68a", "#fcd34d", "#fbbf24", "#f59e0b", "#d97706", "#b45309", "#78350f", "#451a03",
    // 黄 (#eab308)
    "#fef08a", "#fde047", "#facc15", "#eab308", "#ca8a04", "#a16207", "#713f12", "#422006",
    // 青柠 (#84cc16)
    "#d9f99d", "#bef264", "#a3e635", "#84cc16", "#65a30d", "#4d7c0f", "#365314", "#1a2e05",
    // 绿 (#22c55e)
    "#bbf7d0", "#86efac", "#4ade80", "#22c55e", "#16a34a", "#15803d", "#166534", "#14532d",
    // 青 (#14b8a6)
    "#99f6e4", "#5eead4", "#2dd4bf", "#14b8a6", "#0d9488", "#0f766e", "#115e59", "#042f2e",
    // 天蓝 (#0ea5e9)
    "#bae6fd", "#7dd3fc", "#38bdf8", "#0ea5e9", "#0284c7", "#0369a1", "#075985", "#0c4a6e",
    // 蓝 (#3b82f6)
    "#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6", "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a",
    // 紫 (#8b5cf6)
    "#ddd6fe", "#c4b5fd", "#a78bfa", "#8b5cf6", "#7c3aed", "#6d28d9", "#5b21b6", "#4c1d95",
    // 品红 (#d946ef)
    "#f5d0fe", "#f0abfc", "#e879f9", "#d946ef", "#c026d3", "#a21caf", "#86198f", "#701a75",
    // 粉 (#ec4899)
    "#fbcfe8", "#f9a8d4", "#f472b6", "#ec4899", "#db2777", "#be185d", "#9d174d", "#831843",
  ];

  // 给特定 raw key 打开编辑 modal。rawKey 必须是已知 agent（兜底桶"其它"也支持）。
  function openAgentAliasModal(rawKey) {
    if (!rawKey) return;
    const overlay = $("modal-overlay");
    const header = $("modal-header");
    const body = $("modal-body");
    if (!overlay || !header || !body) return;
    const cur = (() => {
      const e = agentAliases && agentAliases[rawKey];
      if (!e) return { name: rawKey, color: null, view: "both" };
      if (typeof e === "string") return { name: e, color: null, view: "both" };   // 防一手
      const v = e.view;
      return {
        name: e.name || rawKey,
        color: e.color || null,
        view: (v === "requests" || v === "tokens" || v === "both") ? v : "both",
      };
    })();
    const initialColor = cur.color || "#d97706";   // 默认橘色（兜底预览）
    let stateName = cur.name;
    let stateColor = cur.color;                    // null = 用 css 默认
    let stateView = cur.view;                      // v0.162：per-row 显示模式

    function renderSwatches() {
      return __MATERIAL_SWATCHES.map(hex => {
        const sel = (hex.toLowerCase() === (stateColor || "").toLowerCase()) ? " is-selected" : "";
        return `<button type="button" class="alias-swatch${sel}" data-color="${hex}" style="background:${hex}" title="${hex}" data-i18n-keep></button>`;
      }).join("");
    }
    function refreshPreview() {
      const preview = body.querySelector(".alias-edit-preview");
      if (preview) {
        if (stateColor) preview.style.background = stateColor;
        else preview.removeAttribute("style");
      }
      // 同步色板上选中环
      body.querySelectorAll(".alias-swatch").forEach(el => {
        const match = (el.getAttribute("data-color") || "").toLowerCase() === (stateColor || "").toLowerCase();
        el.classList.toggle("is-selected", !!match);
      });
    }

    header.innerHTML = `
      <div class="modal-title">
        编辑平台 <span class="modal-id" style="font-family:var(--font-mono);font-size:12px">${escape(rawKey)}</span>
      </div>`;
    body.innerHTML = `
      <div class="alias-edit-preview-wrap">
        <span class="alias-edit-preview" style="background:${escape(initialColor)}" data-i18n-keep>${escape(stateName)}</span>
        <span style="color:var(--text-secondary);font-size:12px" data-i18n-keep>预览</span>
      </div>
      <div style="display:grid;gap:6px">
        <label style="font-size:12px;color:var(--text-secondary)">显示名</label>
        <input type="text" class="alias-edit-name" value="${escape(stateName)}" maxlength="32" data-i18n-keep>
      </div>
      <div style="display:grid;gap:6px">
        <label style="font-size:12px;color:var(--text-secondary)">显示模式</label>
        <div class="consume-switch alias-edit-view" data-i18n-keep>
          <button type="button" class="consume-switch-btn${stateView === "both" ? " active" : ""}" data-view="both">均显示</button>
          <button type="button" class="consume-switch-btn${stateView === "requests" ? " active" : ""}" data-view="requests">显示次数</button>
          <button type="button" class="consume-switch-btn${stateView === "tokens" ? " active" : ""}" data-view="tokens">显示token</button>
        </div>
      </div>
      <div class="alias-swatch-grid" style="margin-top:14px" role="listbox" aria-label="颜色">
        ${renderSwatches()}
      </div>
      <div class="alias-edit-actions">
        <button type="button" class="btn btn-primary alias-edit-save" data-i18n-keep>保存</button>
        <button type="button" class="btn alias-edit-reset-color" data-i18n-keep>重置颜色</button>
        <button type="button" class="btn alias-edit-reset" data-i18n-keep>删除该别名（落回原名）</button>
        <button type="button" class="btn alias-edit-cancel" data-i18n-keep>取消</button>
      </div>`;
    refreshPreview();

    // 色板点击 → 设 stateColor
    body.querySelector(".alias-swatch-grid").addEventListener("click", e => {
      const sw = e.target.closest(".alias-swatch");
      if (!sw) return;
      stateColor = sw.getAttribute("data-color") || null;
      refreshPreview();
    });
    // name input → 实时同步预览文本
    body.querySelector(".alias-edit-name").addEventListener("input", e => {
      stateName = e.target.value;
      const preview = body.querySelector(".alias-edit-preview");
      if (preview) preview.textContent = stateName || rawKey;
    });
    // 显示模式三档 → stateView（实时同步 active 高亮）
    body.querySelector(".alias-edit-view").addEventListener("click", e => {
      const btn = e.target.closest("button[data-view]");
      if (!btn) return;
      const v = btn.dataset.view;
      if (v !== "both" && v !== "requests" && v !== "tokens") return;
      stateView = v;
      body.querySelectorAll(".alias-edit-view button[data-view]").forEach(b => {
        b.classList.toggle("active", b.dataset.view === v);
      });
    });
    // 重置颜色 = 抹掉 color，落回 css 默认
    body.querySelector(".alias-edit-reset-color").addEventListener("click", () => {
      stateColor = null;
      const preview = body.querySelector(".alias-edit-preview");
      if (preview) preview.removeAttribute("style");
      refreshPreview();
    });
    // 取消 = 关 modal
    body.querySelector(".alias-edit-cancel").addEventListener("click", () => closeModal());
    // 删除整条别名 = 落回原名 + 默认颜色
    body.querySelector(".alias-edit-reset").addEventListener("click", () => {
      __commitAgentAlias(rawKey, null);   // null 表示删条目
    });
    // 保存
    body.querySelector(".alias-edit-save").addEventListener("click", () => {
      const finalName = body.querySelector(".alias-edit-name").value.trim();
      __commitAgentAlias(rawKey, {
        name: finalName || rawKey,
        color: stateColor,
        view: stateView,
      });
    });

    overlay.hidden = false;
    document.body.classList.add("modal-open");
    wireModalClose();
  }

  function __commitAgentAlias(rawKey, value) {
    // value = null → 删除该 raw 的别名条目；否则 {name, color}。
    const next = Object.assign({}, agentAliases || {});
    if (value === null) {
      delete next[rawKey];
    } else {
      next[rawKey] = value;
    }
    api.saveAgentAliases(next).then(res => {
      if (res && res.ok) {
        agentAliases = next;
        lastAgentAliasSig = null;       // 让设置页下次重渲染反映新值
        closeModal();
        if (lastSnap) renderAll(lastSnap, lastStatus);
      } else {
        console.error("[__commitAgentAlias] save failed:", res);
      }
    }).catch(err => console.error("[__commitAgentAlias] failed:", err));
  }

  // v0.xxx：平台流量卡 列表/图表 切换。事件委托（按钮在卡标题里，整卡
  // 拖拽时别误触发——按钮是 button，dragstart 不理会）。
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-agent-view]");
    if (!btn) return;
    e.stopPropagation();
    setAgentCardView(btn.dataset.agentView);
    // 完整重绘一次（renderAll 里会同步按钮 active 态并重渲染 agent 卡）。
    if (lastSnap) renderAll(lastSnap, lastStatus);
  });

  // 总览卡事件委托：点某行（或徽标）→ 打开编辑 modal。
  document.addEventListener("click", function (e) {
    const tEl = e.target;
    if (!tEl || !tEl.closest) return;
    const row = tEl.closest("[data-agent-row]");
    if (!row) return;
    const raw = row.getAttribute("data-agent-row");
    if (!raw) return;
    openAgentAliasModal(raw);
  });

  // v0.40 性能：spotlight 每 500ms 跑一次，但 99% 的 tick 期间 model
  // 数据完全没变。7 个 setText + 3 个 style.width 在 idle 状态下是
  // 纯浪费，且每次都触发一次 layout。sig 只覆盖显示相关的字段。
  //
  // v0.9：修复"总token 不动"。旧实现里 tms-total 是**用量最大的那
  // 一个模型**的累计（top.total）——当某个大模型（如 MiniMax-M3）长
  // 期占榜首时，其它上游（如 opencode-go）的消耗永远盖不过它，顶部
  // "总计" 就冻住了，跟"上游状态"里 per-upstream 的累计 tokens 严重
  // 不一致。现在总计/请求数/三段进度条都改成**全模型合计**（真正的
  // 中继总消耗，会随任意上游增长），tms-name 仍保留"用量最大的模型"
  // 这个高亮。sig 里拿掉 ``snap.ts`` —— 数字没变就不用重渲染。
  let lastSpotlightSig = null;
  // v0.10：点击 spotlight 的"总计"数字在两种口径间切换：
  //   "all" = 全模型合计（中继总消耗）
  //   "top" = 用量最大的模型（tms-name 高亮的那一个）
  // 请求数跟着同一口径走。sig 里带上 mode，切模式立即重渲染。
  let spotlightMode = "all";

  // -------------------------------------------------------------------------
  // v0.100：总览入场动画 —— 每次进入「总览」主页，spotlight 数字与进度条
  // 从 0 增长到当前值（cubic-bezier 0.68,0,0.23,1 · 0.5s）。
  //
  // 设计要点（全部无测宽、无咖啡依赖，仅 DOM + rAF）：
  //   * 缓动曲线：cubic-bezier(0.68, 0.00, 0.23, 1.00) —— rAF 里手动求值
  //     （解 x(t)=p 得 t，再算 y(t），牛顿迭代 + 二分兜底）。
  //   * 时长：0.5s。
  //   * 范围：只动 spotlight 5 个数字（tms-total / tms-requests /
  //     tms-input/output/cache-val）+ 3 段进度条宽度（tms-seg-input/output/cache）。
  //   * 触发：setView("overview") 时 pending=true；renderAll overview 分支
  //     渲染完成（renderTopModelSpotlight 之后）同步消费。
  //   * 数字宽度锁定：动画期间给数字 span 设 display:inline-block +
  //     width/minWidth/maxWidth（3 连），防 flex min-width:auto 把宽度按内
  //     容撑开导致 label/单位右移；结束解除锁（display 保持 inline-block 防
  //     跳变）。
  //   * 进度条：动画期间 el.style.transition = "none"（.tms-bar-seg 有
  //     transition: width .35s，会先把"满→0"做成过渡），结束恢复。
  //   * 数字中间帧 Math.round 取整，避免 "12,345.67"。
  //   * 尊重 prefers-reduced-motion: reduce（系统关闭动画时不播）。
  // -------------------------------------------------------------------------
  const _BZ_X1 = 0.68, _BZ_Y1 = 0.00, _BZ_X2 = 0.23, _BZ_Y2 = 1.00;
  function _bezierX(t) {
    const u = 1 - t;
    return 3 * u * u * t * _BZ_X1 + 3 * u * t * t * _BZ_X2 + t * t * t;
  }
  function _bezierDX(t) {
    const u = 1 - t;
    return 3 * u * u * _BZ_X1 + 6 * u * t * (_BZ_X2 - _BZ_X1) + 3 * t * t * (1 - _BZ_X2);
  }
  function _bezierY(t) {
    const u = 1 - t;
    return 3 * u * u * t * _BZ_Y1 + 3 * u * t * t * _BZ_Y2 + t * t * t;
  }
  function _introBezier(p) {
    p = Math.max(0, Math.min(1, p));
    if (p === 0 || p === 1) return p;
    let t = p;
    for (let i = 0; i < 10; i++) {
      const err = _bezierX(t) - p;
      if (Math.abs(err) < 1e-6) break;
      const d = _bezierDX(t);
      const nt = t - err / d;
      if (!(nt >= 0 && nt <= 1)) break;
      t = nt;
    }
    const residual = _bezierX(t) - p;
    if (residual < -1e-3 || residual > 1e-3) {
      let lo = 0, hi = 1;
      for (let i = 0; i < 20; i++) {
        const mid = (lo + hi) / 2;
        if (_bezierX(mid) < p) lo = mid; else hi = mid;
      }
      t = (lo + hi) / 2;
    }
    return _bezierY(t);
  }
  function parseIntroNum(text) {
    if (text == null) return null;
    const s = String(text).replace(/[,，\s]/g, "");
    if (!s || s === "—" || s === "-" || !/^[0-9.]+$/.test(s)) return null;
    return parseFloat(s);
  }
  function animateIntroValue(el, target, formatter, gen, dur = 500) {
    if (!el || target == null) return;
    const finalText = formatter(target);
    el.textContent = finalText;
    const w = el.offsetWidth;
    el.style.display = "inline-block";
    el.style.flex = "0 0 auto";
    el.style.width = w + "px";
    el.style.minWidth = w + "px";
    el.style.maxWidth = w + "px";
    el.style.overflow = "hidden";
    el.style.whiteSpace = "nowrap";
    el.textContent = formatter(0);
    const t0 = performance.now();
    function frame(t) {
      // 被新渲染打断（renderTopModelSpotlight 递增了 generation）——
      // 立即放弃，不再覆盖 DOM，避免旧动画把切档后的新数字/新条覆盖回旧值。
      if (gen !== spotlightAnimGen) return;
      const p = Math.min(1, (t - t0) / dur);
      const e = _introBezier(p);
      el.textContent = formatter(Math.round(target * e));
      if (p < 1) {
        requestAnimationFrame(frame);
      } else {
        el.style.flex = "";
        el.style.width = "";
        el.style.minWidth = "";
        el.style.maxWidth = "";
        el.style.overflow = "";
        el.style.whiteSpace = "";
      }
    }
    requestAnimationFrame(frame);
  }
  function animateIntroWidth(el, targetPct, gen, dur = 500) {
    if (!el) return;
    const prevTransition = el.style.transition || "";
    el.style.transition = "none";
    el.style.width = "0%";
    const t0 = performance.now();
    function frame(t) {
      if (gen !== spotlightAnimGen) {
        // 被打断：恢复 transition，但**不再把宽度改回旧值** ——
        // 新渲染已把条设为新比例，这里只能让路。
        el.style.transition = prevTransition;
        return;
      }
      const p = Math.min(1, (t - t0) / dur);
      const e = _introBezier(p);
      el.style.width = (targetPct * e).toFixed(2) + "%";
      if (p < 1) {
        requestAnimationFrame(frame);
      } else {
        el.style.transition = prevTransition;
      }
    }
    requestAnimationFrame(frame);
  }
  function playOverviewIntro() {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return;
    }
    // 用当前渲染的代际启动动画 —— 切档重渲时 gen 递增，旧动画帧自动失效。
    const gen = spotlightAnimGen;
    const numTargets = [
      ["tms-total", fmtTokens],
      ["tms-requests", fmtNum],
      ["tms-input-val", fmtTokens],
      ["tms-output-val", fmtTokens],
      ["tms-cache-val", fmtTokens],
    ];
    for (const [id, fmt] of numTargets) {
      const el = $(id);
      if (!el) continue;
      const target = parseIntroNum(el.textContent);
      if (target == null || target === 0) continue;
      animateIntroValue(el, target, fmt, gen);
    }
    for (const id of ["tms-seg-input", "tms-seg-output", "tms-seg-cache"]) {
      const el = $(id);
      if (!el) continue;
      const pct = parseFloat(String(el.style.width || "0").replace("%", ""));
      if (!isFinite(pct) || pct <= 0) continue;
      animateIntroWidth(el, pct, gen);
    }
  }
  // v0.100：总览入场动画挂起标志。setView("overview") 置位；renderAll
  // overview 分支渲染完成后同步消费。初值 true 让首帧进总览也播。
  let overviewIntroPending = true;
  // v0.102：spotlight 渲染代际 —— 每次 renderTopModelSpotlight 更新 DOM
  // 都递增，使上一轮入场动画的 rAF 帧失效（否则切档后旧动画会把新数字
  // 和新条覆盖回旧值，表现为"数字和条不对、中文却对"）。
  let spotlightAnimGen = 0;

  function renderTopModelSpotlight(snap) {
    // Promote the heaviest model in the window to a hero "spotlight"
    // strip above the 6-card grid. Three-color stacked bar (input /
    // output / cache) is the same vocabulary Claude Code's own TUI uses,
    // so the colour cues map to something the user already knows.
    const root = $("top-model-spotlight");
    if (!root) return;
    const models = snap && snap.by_model;
    if (!models) {
      // 隐藏也递增代际，停掉上一轮仍在跑的入场动画（避免它继续往隐藏
      // 元素写旧值，等会儿再显示时错位）。
      spotlightAnimGen++;
      root.hidden = true;
      return;
    }
    // ``by_model`` is already sorted DESC by total tokens (see
    // ``tui.fetch_by_model``), so the first entry is the heaviest.
    // Single pass: find the top model AND accumulate relay-wide totals.
    let topName = null, top = null;
    let sumInput = 0, sumOutput = 0, sumCache = 0, sumReq = 0;
    for (const [name, v] of Object.entries(models)) {
      const t = (v.input_tokens || 0) + (v.output_tokens || 0) + (v.cache_tokens || 0);
      if (!top || t > top.total) { topName = name; top = { ...v, total: t }; }
      sumInput += v.input_tokens || 0;
      sumOutput += v.output_tokens || 0;
      sumCache += v.cache_tokens || 0;
      sumReq += v.requests || 0;
    }
    const grandTotal = sumInput + sumOutput + sumCache;
    if (!topName || grandTotal <= 0) { root.hidden = true; return; }
    // v0.10：按当前口径取展示值。进度条（输入/输出/缓存占比）保持全
    // 模型合计 —— 它是比例不是总量，跟口径无关。
    const shownTotal = spotlightMode === "top" ? (top.total || 0) : grandTotal;
    const shownReq   = spotlightMode === "top" ? (top.requests || 0) : sumReq;
    // v0.154：sig 带语言 —— 切语言后即使数字不变也要重渲染，否则
    // tms-total-cn 会停留在中文大写数字（walker 对非字典中文不翻译）。
    const sig = spotlightMode + "|" + I18N.lang + "|" + topName + "|" + shownReq + "|" + shownTotal;
    if (sig === lastSpotlightSig) return;
    lastSpotlightSig = sig;
    // 递增代际：使上一轮入场动画的 rAF 帧立即失效（它可能还在把旧的
    // 数字/条写回 DOM）。
    spotlightAnimGen++;
    root.hidden = false;
    setText("tms-name",     topName);
    setText("tms-requests", fmtNum(shownReq));
    setText("tms-total",    fmtTokens(shownTotal));
    // v0.154：中文大写数字是简体中文界面专属设计元素 —— 非 zh（含 zh-TW/
    // ja/ko）用西文逗号格式，避免其它语言界面出现「柒贰伍…」。
    setText("tms-total-cn", I18N.lang === "zh" ? toFormalCn(shownTotal) : fmtTokens(shownTotal));
    const labelEl = $("tms-total-label");
    if (labelEl) labelEl.textContent = spotlightMode === "top" ? "最大模型" : "总计";
    setText("tms-input-val",  fmtTokens(sumInput));
    setText("tms-output-val", fmtTokens(sumOutput));
    setText("tms-cache-val",  fmtTokens(sumCache));
    // Widths in percent — guard against divide-by-zero with ``|| 1`` above.
    const segs = [
      ["tms-seg-input",  sumInput / grandTotal * 100],
      ["tms-seg-output", sumOutput / grandTotal * 100],
      ["tms-seg-cache",  sumCache / grandTotal * 100],
    ];
    segs.forEach(([id, pct]) => {
      const el = $(id);
      if (el) el.style.width = pct.toFixed(2) + "%";
    });
    // v0.102：数字宽度自适应 —— 按当前格式化文本字符数更新 min-width。
    // 数字右对齐后右端贴单位，长度变化只在左端留白，单位/请求数不动。
    ["tms-total", "tms-requests", "tms-input-val", "tms-output-val", "tms-cache-val"].forEach(id => {
      const el = $(id);
      if (el) el.style.minWidth = (String(el.textContent).length + 1) + "ch";
    });
  }

  // v0.102：取当前档位对应的总览数据（relay=快照；透传/全部=合并后的
  // 透传数据）。透传数据未就绪时返回 null（调用方保持现状）。
  function getCurrentOverviewData(snap) {
    const mode = getConsumeMode();
    if (mode === "relay") return snap;
    if (ptOverviewCache) {
      return mode === "passthrough" ? ptOverviewToSnap(ptOverviewCache) : mergeSnap(snap, ptOverviewCache);
    }
    return null;
  }

  // v0.10：单击"总计"数字切换 全模型合计 / 用量最大模型。切换后立即
  // 用最近一次快照重渲染，不用等下一个 poll tick。
  function wireSpotlightToggle() {
    const el = $("tms-total");
    if (!el) return;
    const flip = () => {
      spotlightMode = spotlightMode === "all" ? "top" : "all";
      const ov = lastSnap ? getCurrentOverviewData(lastSnap) : null;
      if (ov) renderTopModelSpotlight(ov);
    };
    el.addEventListener("click", flip);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        flip();
      }
    });
  }

  function renderModels(body, snap) {
    if (!snap) {
      body.innerHTML = '<div class="card-empty">暂无数据</div>';
      return;
    }
    const models = snap.by_model || {};
    const entries = Object.entries(models);
    if (!entries.length) {
      body.innerHTML = '<div class="card-empty">暂无请求</div>';
      return;
    }
    const maxT = Math.max(1, ...entries.map(([, v]) =>
      (v.input_tokens || 0) + (v.output_tokens || 0)));
    body.innerHTML = entries.slice(0, 5).map(([m, v]) => {
      const total = (v.input_tokens || 0) + (v.output_tokens || 0);
      const pct = (total / maxT) * 100;
      return `
        <div class="model-row">
          <div class="model-name" data-i18n-keep>${escape(m)}</div>
          <div class="model-bar"><div class="model-bar-fill" style="width:${pct.toFixed(1)}%"></div></div>
          <div class="model-tokens">${fmtTokens(total)}</div>
        </div>`;
    }).join("");
  }

  function renderRecent(body, snap) {
    if (!snap || !snap.recent || !snap.recent.length) {
      body.innerHTML = '<div class="card-empty">暂无最近请求</div>';
      return;
    }
    body.innerHTML = snap.recent.slice(0, 5).map(r => `
      <div class="recent-row" data-id="${r.id}" tabindex="0" role="button" aria-label="查看请求详情">
        <span class="recent-time">${fmtTime(r.ts)}</span>
        <span class="recent-platform" data-i18n-keep>${escape(r.platform || "—")}</span>
        <span class="recent-model" data-i18n-keep>${escape(r.model || "—")}</span>
        <span class="recent-tokens">${fmtTokens((r.input_tokens || 0) + (r.output_tokens || 0))}</span>
      </div>
    `).join("");
  }

  // Idle-tick short-circuit. The cards derive purely from `snap`, so
  // when ``snap`` hasn't changed since the last render we can skip the
  // ``innerHTML`` rewrites and the chart re-build. ``renderStatus``
  // still runs every tick because the topbar has its own derivations
  // and is cheap.
  let lastCardsSig = null;
  // Single chart.js instance for the 24h token chart. Kept module-local
  // so we can ``.destroy()`` before re-creating on data change — chart.js
  // will leak canvases otherwise (each new Chart() overwrites the parent
  // node but the old instance stays GC-rooted via window resize listeners).
  let _hourChart = null;

  // -------------------------------------------------------------------------
  // v0.102：三极消耗口径开关（仅透传 / 全部 / 仅转换）。
  //
  // 全局状态 ``consume-mode``（localStorage 持久化）驱动总览页数据口径：
  //   relay       → 快照原样（转换模式数据）
  //   passthrough → 透传库 overview（/api/passthrough/overview）
  //   all         → 前端合并两源（model 按 key 相加，upstream 给透传行加
  //                 "[透传] " 前缀，hour 桶对齐后相加，recent 按 ts 合并）
  // 开关部署位置：总览页顶部 + 设置页"透传模式"卡 + 设置页"中继状态"卡
  // （透传模式下该卡隐藏）。统计页工具栏是同一语义的三档按钮，互相联动。
  // -------------------------------------------------------------------------
  const CONSUME_MODES = ["passthrough", "all", "relay"];
  const CONSUME_LABELS = { passthrough: "仅透传", all: "全部", relay: "仅转换" };

  function getConsumeMode() {
    if (!window._consumeMode) {
      let stored = null;
      try { stored = localStorage.getItem("consume-mode"); } catch (e) {}
      window._consumeMode = CONSUME_MODES.includes(stored)
        ? stored
        : (window._ptMode ? "passthrough" : "relay");
    }
    return window._consumeMode;
  }

  function setConsumeMode(mode, opts) {
    if (!CONSUME_MODES.includes(mode)) return;
    window._consumeMode = mode;
    try { localStorage.setItem("consume-mode", mode); } catch (e) {}
    document.querySelectorAll(".consume-switch").forEach(el => {
      el.querySelectorAll("button[data-value]").forEach(b => {
        b.classList.toggle("active", b.dataset.value === mode);
      });
    });
    const silent = opts && opts.silent;
    if (!silent) {
      if (currentView === "overview") {
        // 强制 renderAll 判定为"档位变化"→ 清透传缓存并重拉（即使
        // 上次就是同档，比如用户反复点同档位）。
        lastOverviewMode = null;
        lastCardsSig = null;
        if (lastSnap) renderAll(lastSnap, lastStatus);
      } else if (currentView === "stats") {
        statsState.mode = mode;
        statsState.needRender = true;
        syncStatsModeButtons();
        renderStats();
      }
    }
  }

  function renderConsumeSwitch(container, mode) {
    if (!container) return;
    container.innerHTML = CONSUME_MODES.map(m =>
      `<button type="button" class="consume-switch-btn${mode === m ? " active" : ""}" data-value="${m}">${CONSUME_LABELS[m]}</button>`
    ).join("");
    container.querySelectorAll("button[data-value]").forEach(b => {
      b.addEventListener("click", () => setConsumeMode(b.dataset.value));
    });
  }

  function mountConsumeSwitches() {
    document.querySelectorAll("[data-consume-switch]").forEach(el => {
      renderConsumeSwitch(el, getConsumeMode());
    });
  }

  // ---- 透传 overview 缓存（15s TTL，切档/切模式时强制刷新） ----
  const PT_OVERVIEW_TTL = 15000;
  const PT_RETRY_AFTER_FAIL = 5000;
  let ptOverviewCache = null;
  let ptOverviewFetchedAt = 0;
  let ptOverviewPromise = null;
  let ptFetchFailed = false;
  let ptLastAttemptAt = 0;
  // 上次总览渲染用的档位 —— 变化时丢弃透传缓存强制重拉。
  let lastOverviewMode = null;
  // 上次真正渲染过卡片内容的档位 —— 仅在非加载占位分支更新；切档时
  // 用它判断是否需要淡入过渡（同一档位的常规 tick 刷新不重播动画）。
  let lastRenderedMode = null;

  function fetchPtOverview(force) {
    if (ptOverviewPromise) return ptOverviewPromise;
    if (!force && ptOverviewCache && Date.now() - ptOverviewFetchedAt < PT_OVERVIEW_TTL) {
      return Promise.resolve(ptOverviewCache);
    }
    ptFetchFailed = false;
    ptLastAttemptAt = Date.now();
    ptOverviewPromise = api.passthroughOverview("1d").then(res => {
      ptOverviewPromise = null;
      if (!res || res.error) {
        ptFetchFailed = true;
        return null;
      }
      ptOverviewCache = res;
      ptOverviewFetchedAt = Date.now();
      if (currentView === "overview" && lastSnap) {
        // 数据到了 —— 清 sig 强制用新口径重渲（relay 档不受影响）。
        lastCardsSig = null;
        renderAll(lastSnap, lastStatus);
      }
      return res;
    }).catch(() => {
      ptOverviewPromise = null;
      ptFetchFailed = true;
      return null;
    });
    return ptOverviewPromise;
  }

  // ---- 透传 overview → 快照形状（总览 6 卡可直用） ----
  function ptRowsToObj(rows, keyFn) {
    const o = {};
    (rows || []).forEach(r => { o[keyFn ? keyFn(r) : r.key] = r; });
    return o;
  }

  function ptUpstreamLabel(key) {
    const parts = String(key || "").split("|");
    let host = (parts[0] || "").split("//").pop().split("/")[0];
    return (host || parts[0] || key) + "|" + (parts[1] || "");
  }

  function ptModelRow(r) {
    // 透传行无 cache_tokens；把 cache 计费量拆出来对齐 spotlight 口径
    // （input+output+cache 三段条）。
    const c = (r.cache_read_input_tokens || 0) + (r.cache_creation_input_tokens || 0);
    return { ...r, cache_tokens: c, input_tokens: Math.max(0, (r.input_tokens || 0) - c) };
  }

  function ptOverviewToSnap(pt) {
    const p = pt || {};
    const byUpstream = ptRowsToObj(p.by_upstream, r => ptUpstreamLabel(r.key));
    const byModel = ptRowsToObj(p.by_model, r => r.key);
    Object.keys(byModel).forEach(k => { byModel[k] = ptModelRow(byModel[k]); });
    const recent = (p.recent || []).map(r => ({
      id: r.id,
      ts: r.ts,
      platform: ((r.url || "").split("//").pop().split("/")[0] || "透传"),
      model: r.model || r.model_field_name || "—",
      input_tokens: r.input_tokens || 0,
      output_tokens: r.output_tokens || 0,
    }));
    return {
      by_upstream: byUpstream,
      by_model: byModel,
      by_platform: ptRowsToObj(p.by_platform),
      by_hour: p.by_hour || [],
      recent,
    };
  }

  function addTotals(target, src) {
    [
      "requests", "input_tokens", "output_tokens",
      "cache_read_input_tokens", "cache_creation_input_tokens",
      "errors", "total_tokens", "cache_tokens",
    ].forEach(f => { target[f] = (target[f] || 0) + (src[f] || 0); });
    return target;
  }

  function mergeSnap(relay, pt) {
    const ptSnap = ptOverviewToSnap(pt);
    const out = { by_hour: [], recent: [], by_platform: {}, by_upstream: {}, by_model: {} };
    Object.entries(relay.by_platform || {}).forEach(([k, v]) => { out.by_platform[k] = { ...v }; });
    Object.entries(ptSnap.by_platform).forEach(([k, v]) => {
      if (!out.by_platform[k]) out.by_platform[k] = { key: k };
      addTotals(out.by_platform[k], v);
    });
    // 今日用量：合并 relay + 透传 两边的 today 桶。透传侧目前
    // 没有 by_platform_today 字段（passthrough_db 没有时间窗 helper），
    // 回退到 by_platform 在透传档位显示的就是全量 —— 不影响 relay 档
    // （最常用档位），透传档位的"今日"含义暂时是历史总和，TODO 后续
    // 给 passthrough.db 加同款 since 过滤。
    out.by_platform_today = {};
    const todaySrc = (relay.by_platform_today && Object.keys(relay.by_platform_today).length)
      ? relay.by_platform_today
      : null;
    if (todaySrc) {
      Object.entries(todaySrc).forEach(([k, v]) => { out.by_platform_today[k] = { ...v }; });
    }
    Object.entries(relay.by_upstream || {}).forEach(([k, v]) => { out.by_upstream[k] = { ...v }; });
    Object.entries(ptSnap.by_upstream).forEach(([k, v]) => {
      out.by_upstream["[透传] " + k] = { ...v, key: "[透传] " + k };
    });
    Object.entries(relay.by_model || {}).forEach(([k, v]) => { out.by_model[k] = { ...v }; });
    Object.entries(ptSnap.by_model).forEach(([k, v]) => {
      if (!out.by_model[k]) out.by_model[k] = { key: k };
      addTotals(out.by_model[k], v);
    });
    const hours = new Map();
    (relay.by_hour || []).forEach(b => hours.set(Math.floor(b.hour / 3600) * 3600, { ...b }));
    ptSnap.by_hour.forEach(b => {
      const h = Math.floor(b.hour / 3600) * 3600;
      if (!hours.has(h)) hours.set(h, { hour: h, requests: 0, in_tokens: 0, out_tokens: 0, tokens: 0 });
      const row = hours.get(h);
      addTotals(row, b);
      row.tokens = (row.in_tokens || 0) + (row.out_tokens || 0);
    });
    out.by_hour = [...hours.values()].sort((a, b) => a.hour - b.hour);
    const allRecent = (relay.recent || []).map(r => ({ ...r })).concat(ptSnap.recent);
    out.recent = allRecent.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    return out;
  }

  // v0.102：卡片渲染多实例 —— 卡片可删除/新建，同一 data-card 可能有
  // 0..N 个 .card-body，渲染函数逐实例调用。
  function forEachCardBody(type, fn) {
    document.querySelectorAll(`.glass-card[data-card="${type}"] .card-body`).forEach(body => fn(body));
  }

  // ---- v0.102 总览卡片管理（localStorage 增删；顺序仍走拖拽） ----
  // v0.xxx：统计页 4 张 D3 图也接进同一套卡片管理 —— isStats 标记为
  // 统计卡，默认不显示（getCardsConfig 只默认开快照卡）。
  const CARD_DEFS = [
    { key: "hourly", title: "Token 消耗 · 近 24h", cls: "card-chart-row" },
    { key: "upstream", title: "上游状态", cls: "" },
    { key: "today", title: "今日用量", cls: "" },
    { key: "platform", title: "协议分布", cls: "" },
    { key: "agent", title: "平台流量", cls: "" },
    { key: "models", title: "模型分布", cls: "" },
    { key: "recent", title: "最近活动", cls: "" },
    { key: "stats-treemap", title: "统计 · 用量树状图", cls: "card-chart-row", isStats: true },
    { key: "stats-pie", title: "统计 · 用量饼图", cls: "card-chart-row", isStats: true },
    { key: "stats-agent", title: "统计 · 30天平台流量", cls: "", isStats: true },
    { key: "stats-model-daily", title: "统计 · 单模型30天分布", cls: "card-chart-row", isStats: true },
  ];
  let cardsConfig = null;

  function getCardsConfig() {
    if (!cardsConfig) {
      let arr = null;
      try { arr = JSON.parse(localStorage.getItem("overview-cards-v1")); } catch (e) {}
      // 默认只开快照卡（7 张）；统计卡默认隐藏，需在「管理卡片」里勾选。
      if (!Array.isArray(arr) || !arr.length) arr = CARD_DEFS.filter(d => !d.isStats).map(d => d.key);
      cardsConfig = arr.filter(k => CARD_DEFS.some(d => d.key === k));
      if (!cardsConfig.length) cardsConfig = CARD_DEFS.filter(d => !d.isStats).map(d => d.key);
    }
    return cardsConfig;
  }

  function saveCardsConfig() {
    try { localStorage.setItem("overview-cards-v1", JSON.stringify(cardsConfig)); } catch (e) {}
    lastCardsSig = null;
  }

  function createCardEl(key) {
    const def = CARD_DEFS.find(d => d.key === key);
    if (!def) return null;
    const el = document.createElement("div");
    el.className = "glass-card" + (def.cls ? " " + def.cls : "");
    el.dataset.card = key;
    el.draggable = true;
    // hourly 图表卡的 body 需要 card-body-chart（高度/定位），新建卡同样补上，
    // 否则图表高度为 0 看不见。
    const bodyCls = key === "hourly" ? "card-body card-body-chart" : "card-body";
    // 统计卡的 body 走 stats-chart-host（D3 svg 容器 + min-height）。
    const bodyCls2 = def.isStats ? "card-body stats-chart-host" : bodyCls;
    let inner = `<div class="card-title">` +
      `<span>${def.title}</span>` +
      `<button type="button" class="card-close-btn" data-close-card="${key}" title="移除卡片" aria-label="移除卡片">×</button>` +
      `</div>`;
    // model-daily 卡在标题下方带「全部/各模型」chips 条 + 模型下拉。
    if (key === "stats-model-daily") {
      inner += `<div class="stats-md-bar">` +
        `<div class="stats-md-bar-label">单模型 30 天调用分布</div>` +
        `<div class="stats-md-bar-chips" data-md-chips="1"></div>` +
        `<select class="stats-md-select" data-md-sel="1" aria-label="选择模型"></select>` +
        `</div>`;
    }
    inner += `<div class="${bodyCls2}" id="card-${key}-body">` +
      `<div class="card-empty">加载中…</div>` +
      `</div>`;
    // 尺寸拖拽边缘（v0.xxx）：底部/右侧/右下角 三条全宽命中区。
    // **必须直接 child of .glass-card**，贴卡片视觉外边；如果塞在 body
    // 里会落在 padding 之内，用户点"卡片边缘"实际点的是 padding，命中
    // 不到 → 仍走 HTML5 drag → 把整张卡拖走。
    // draggable=false 防止整卡 HTML5 拖拽重排跟边缘拖拽抢。
    inner += `<div class="card-edge card-edge-bottom" data-card-edge-h="1" draggable="false" title="拖拽调整卡片高度"></div>` +
      `<div class="card-edge card-edge-right" data-card-edge-w="1" draggable="false" title="拖拽调整卡片宽度（列数，1.0–3.0）"></div>` +
      `<div class="card-edge card-edge-corner" data-card-edge-br="1" draggable="false" title="拖拽调整卡片大小"></div>`;
    el.innerHTML = inner;
    return el;
  }

  // ---- 总览卡片尺寸持久化（localStorage，按 key 存列跨度/高度档位） ----
  // 纯前端 UI 偏好，与 .env 后端配置无关。
  const CARD_SIZE_KEY = "overview-card-sizes-v1";
  // v0.187：布局模式双槽持久化。
  //
  // 背景：v0.186 把自由模式的 {x,y} 塞进 CARD_SIZE_KEY 的同一条记录里
  // （`{span, height, free:{x,y}}`），两套模式共用一份 span/height。后果是
  // 在自由模式拉大卡片、切回默认模式后默认模式的尺寸也被改掉了，回不去。
  // v0.187 拆成两个独立槽，各自记全套 span/height（自由槽再多带 x/y）：
  //
  //   CARD_SIZE_KEY    (overview-card-sizes-v1)  默认模式 {key:{span,height}}
  //   FREE_LAYOUT_KEY  (overview-free-layout-v1) 自由模式 {key:{span,height,x,y}}
  //   LAYOUT_MODE_KEY  (overview-layout-mode-v1) 全局标量 "default" | "free"
  //
  // 旧 key 继续当默认模式槽 —— 老用户的默认布局零丢失，不需要迁移。自由槽
  // 里 x/y **扁平存**（不再套一层 `free:{}`），少一级解引用。
  const FREE_LAYOUT_KEY = "overview-free-layout-v1";
  const LAYOUT_MODE_KEY = "overview-layout-mode-v1";

  let _layoutMode = (() => {
    try {
      return localStorage.getItem(LAYOUT_MODE_KEY) === "free" ? "free" : "default";
    } catch (e) { return "default"; }
  })();
  function isFreeMode() { return _layoutMode === "free"; }
  function saveLayoutMode() {
    try { localStorage.setItem(LAYOUT_MODE_KEY, _layoutMode); } catch (e) {}
  }
  // 当前模式对应的槽 key —— savedSize/getCardSize/setCardSize 全部经由这里
  // 路由，所以 applyCardSize / 边缘 resize / 列跨度按钮等既有调用点一行都
  // 不用改，就自动读写"当前模式那一槽"。
  function _slotKey() { return isFreeMode() ? FREE_LAYOUT_KEY : CARD_SIZE_KEY; }
  function _readSlot(key) {
    let map = {};
    try { map = JSON.parse(localStorage.getItem(key || _slotKey())) || {}; } catch (e) {}
    return map && typeof map === "object" ? map : {};
  }
  function _writeSlot(map, key) {
    try { localStorage.setItem(key || _slotKey(), JSON.stringify(map)); } catch (e) {}
  }

  // v0.187 一次性迁移：把 v0.186 遗留在默认槽里的 `free:{x,y}` 搬进自由槽，
  // 并从默认槽剥掉 free 字段。幂等 —— 跑完默认槽再没有 free，第二次进来
  // 直接空转。不动 span/height（两槽各自保留自己那份）。
  //
  // ⚠ 判据用 `"free" in rec` 而不是 `rec.free` 的真值：v0.186 的
  // setCardSize 对"默认模式的卡"也会显式写 `free: null`，这类记录同样得
  // 剥掉 key（只是没有坐标可搬）。用真值判断会把 `free: null` 漏在默认槽
  // 里 —— 破坏"默认槽永远不含 free"的不变式，且下次迁移仍被判定为"有旧
  // 数据"，幂等性也丢了。
  function _migrateFreeFieldOnce() {
    const legacy = _readSlot(CARD_SIZE_KEY);
    const keys = Object.keys(legacy).filter(
      k => legacy[k] && typeof legacy[k] === "object" && "free" in legacy[k]);
    if (!keys.length) return;
    const freeSlot = _readSlot(FREE_LAYOUT_KEY);
    for (const k of keys) {
      const rec = legacy[k];
      const f = rec.free;
      // 自由槽已有该卡记录时不覆盖（用户在 v0.187 里已经调过了）。
      // f 为 null / 坐标非法 → 只剥 key，没东西可搬。
      if (!freeSlot[k] && f && Number.isFinite(f.x) && Number.isFinite(f.y)) {
        freeSlot[k] = {
          span: rec.span, height: rec.height, x: f.x, y: f.y,
        };
      }
      delete rec.free;
    }
    _writeSlot(freeSlot, FREE_LAYOUT_KEY);
    _writeSlot(legacy, CARD_SIZE_KEY);
  }
  _migrateFreeFieldOnce();

  // 列跨度：21 档，1.0 → 3.0 步长 0.1（21 个端点包含 3.0）。
  // 之前写 `i < 20` 循环只跑到 i=19 → 最后一项是 2.9 而不是 3.0，CDP
  // 实测拖到底 dx=700 也只能吸到 2.9，到不了"满 3 列"。改成 `i <= 20`
  // 21 项闭合到 3.0。
  //
  // 之前被迫只用 [1,2,3] 整数三档 —— 那是过渡方案，现在总览布局已经换
  // 成 flex（见 styles 里 .view[data-view="overview"] > .grid 注释）：
  // CSS Grid 的 `grid-column: span <integer>` 只接整数，小数静默丢；
  // 改 flex 后卡片走 width: calc(var(--ov-span) / 3 * (100% - 32px))，
  // 任何小数 span 都能落地。所以恢复 fine-grained，拖起来跟高度那侧
  // （31 档 20px 步长）一样的"跟手"手感。
  const OVERVIEW_SPANS = (() => {
    const out = [];
    // v0.189：100 档 —— 1.0 → 3.0 步长 0.02（101 端点 = 100 间隔）。原先
    // 21 档（0.1 步长）拖 0.1 列才跳一档，偏"一格一格"。细化到 0.02 后跟
    // 高度侧一样几乎连续。round 到 2 位小数，表值参与 includes / _spanEq
    // 才能精确匹配（浮点尾差靠 _spanEq 的 0.005 容差兜底）。
    for (let i = 0; i <= 100; i++) out.push(Math.round((1 + i * 0.02) * 100) / 100);
    return out;
  })();
  // 高度档位：160 → 760 步长 6 px，100 档（101 端点 = 100 间隔）。原先
  // 31 档（20px 步长）要拖 10px 才过一档；100 档后跟列跨度那侧一样"卡高
  // 跟着鼠标连续走，停哪儿锁哪儿"。持久化侧 getCardSize/setCardSize 对
  // height 只校验 > 0，不查表，所以换档位表不会让已存的旧高度失效。
  const OVERVIEW_HEIGHTS = (() => {
    const out = [];
    for (let h = 160; h <= 760; h += 6) out.push(h);
    return out;
  })();

  // 单张卡有没有存过尺寸（span=0 表示没存过 → 沿用类自带的列布局，
  // 如 .card-chart-row 的 span 2）。
  // v0.187：读**当前模式**那一槽（_slotKey 路由）。
  function savedSize(key) {
    return _readSlot()[key] || null;
  }
  // 0.1 精度比较（避免 1.1 在 round 出来变成 1.1000000000000001）
  // v0.189：容差从 0.05 缩到 0.005。列跨度档位细化到 0.02 后相邻档差
  // 0.02 < 旧 0.05 容差 —— 不缩的话 _spanEq 会把相邻两档判成同一档，
  // find() 永远吸到更小那档。0.005 < 半档 0.01，且远大于浮点尾差（表值
  // 都 round 到 2 位小数），相邻档精确区分、同档精确命中。
  function _spanEq(a, b) { return Math.abs(a - b) < 0.005; }
  function getCardSize(key) {
    const s = savedSize(key);
    let span = 0;
    if (s && typeof s.span === "number") {
      const m = OVERVIEW_SPANS.find(v => _spanEq(v, s.span));
      span = m || 0;
    }
    // v0.187：自由坐标只在自由槽里有意义，且**扁平存**在记录上（x/y 直接
    // 挂 s，不再套 free:{}）。默认模式恒返回 free=null —— 默认槽里根本没有
    // 坐标字段，卡永远走 flex 流。Number.isFinite 兜底防 localStorage 被
    // 手改成 NaN / 字符串时崩。
    let free = null;
    if (isFreeMode() && s && Number.isFinite(s.x) && Number.isFinite(s.y)) {
      free = { x: s.x, y: s.y };
    }
    return {
      span,
      height: s && s.height > 0 ? s.height : 0,
      free,
    };
  }
  // v0.187：写**当前模式**那一槽。
  //
  // ⚠ free 参数语义（这里是 v0.186 那个"自由模式下 resize 一下卡就掉回默认
  // 模式"bug 的根因所在）：调用方常常只想改 span/height，压根不传第 4 参
  // （app.js 里边缘 resize 收尾、列跨度按钮两处就是这样）。v0.186 把
  // `undefined` 一律归一化成 null 并写回，等于**顺手把坐标擦了**，卡就退出
  // 了自由模式。v0.187 改成：
  //   free === undefined  → 保留槽里已有的 x/y（只更新 span/height）
  //   free === null       → 显式清掉坐标
  //   free === {x,y}      → 写入新坐标
  function setCardSize(key, span, height, free) {
    const map = _readSlot();
    const m = OVERVIEW_SPANS.find(v => _spanEq(v, span));
    const prev = map[key] || {};
    const rec = {
      span: m || 1,
      height: height > 0 ? height : 0,
    };
    if (free === undefined) {
      // 未传 → 沿用旧坐标（自由槽才会有；默认槽本来就没有，等于空操作）
      if (Number.isFinite(prev.x) && Number.isFinite(prev.y)) {
        rec.x = prev.x;
        rec.y = prev.y;
      }
    } else if (free && Number.isFinite(free.x) && Number.isFinite(free.y)) {
      rec.x = free.x;
      rec.y = free.y;
    }
    // free === null → 不写 x/y，坐标被清掉
    map[key] = rec;
    _writeSlot(map);
  }
  // 没存过的卡用类自带的自然跨度（.card-chart-row → 2，其它 → 1）。
  function naturalSpan(card) {
    return card && card.classList.contains("card-chart-row") ? 2 : 1;
  }
  function applyCardSize(card) {
    const key = card && card.dataset ? card.dataset.card : null;
    if (!key) return;
    const s = savedSize(key);
    // 每张总览卡都写 --ov-span：存过就用存的，没存过用自然跨度
    // （chart 卡 2 / 普通卡 1）。CSS 统一走 `span var(--ov-span)`，
    // 覆盖类自带跨度才能被拖拽改掉。
    let spanVal;
    if (s && typeof s.span === "number") {
      const m = OVERVIEW_SPANS.find(v => _spanEq(v, s.span));
      spanVal = m || naturalSpan(card);
    } else {
      spanVal = naturalSpan(card);
    }
    card.style.setProperty("--ov-span", String(spanVal));
    // 高度落在 .card-body 上（图表容器随高度缩放），整卡高度 = 标题 + body。
    const body = card.querySelector(".card-body");
    if (body) {
      if (s && s.height > 0) {
        body.style.setProperty("--ov-h", s.height + "px");
        // 显式高度优先于 stats body 的 min-height:220px / 默认 160px。
        body.style.minHeight = "0";
      } else {
        body.style.removeProperty("--ov-h");
        body.style.minHeight = "";
      }
    }
    // v0.186：自由模式 —— 跟 span/height 一起应用，刷新后位置还原。
    // v0.187：坐标扁平存在记录上（s.x / s.y），且只在自由模式生效 ——
    // 默认模式下恒传 null，卡老老实实待在 flex 流里。
    applyCardFreePosition(card, isFreeMode() ? s : null);
  }

  // v0.186：自由模式应用层。把 {x, y}（相对 .grid 容器）写到 inline
  // style，加 .card-free-positioned 类。free=null 时清掉 inline top/left
  // —— 卡回到默认模式（flex 流）。
  //
  // x/y 是相对 .grid 的偏移（不是 viewport）—— 滚动 grid 时卡跟着走。
  // 拖动期间实时写 viewport 偏移会有抖动；存 grid-relative 一劳永逸。
  //
  // v0.187：入参从"嵌套的 free 子对象"变成"自由槽的整条记录"（x/y 扁平挂
  // 在上面）。函数体只读 .x/.y，两种形状都吃得下，所以逻辑一行没动。
  function applyCardFreePosition(card, free) {
    if (!card) return;
    const x = free && Number.isFinite(free.x) ? free.x : null;
    const y = free && Number.isFinite(free.y) ? free.y : null;
    if (x === null || y === null) {
      card.classList.remove("card-free-positioned");
      card.style.removeProperty("top");
      card.style.removeProperty("left");
      // z-index 不清 —— 用户可能手动叠放过，希望保留层级。
      return;
    }
    card.classList.add("card-free-positioned");
    // v0.187.1：应用时也钳一遍 —— 救回两类卡：
    //  (a) v0.186/187 期间已经被拖进侧栏底下、存盘时是负坐标的卡（不钳的话
    //      每次 reload 都还原到那个点不到的位置，用户只能清 localStorage）；
    //  (b) 窗口从宽变窄后，原本在右侧的卡落到容器外。
    // 必须在 add 类之后钳 —— 加类才切到 absolute，clientWidth 才是最终布局。
    const p = _clampFreePos(card, x, y);
    card.style.top = p.y + "px";
    card.style.left = p.x + "px";
    // 临时诊断：每次 apply 完打一次点
    _diagDump("apply:" + (card.dataset.card || "?"));
  }

  // v0.187.2 临时诊断：把自由模式状态 dump 到磁盘，让用户不用手动跑 JS。
  // 数据写到 .relay-logs/diag-free-pos.json（latest wins）。
  function _diagDump(reason) {
    try {
      if (!(window.pywebview && window.pywebview.api && window.pywebview.api._diag_write_free_pos)) return;
      const grid = document.querySelector('.view[data-view="overview"] .grid');
      const gr = grid && grid.getBoundingClientRect();
      const cards = Array.from(document.querySelectorAll('.glass-card[data-card]')).map(el => ({
        key: el.dataset.card,
        hasFreeClass: el.classList.contains("card-free-positioned"),
        inlineLeft: el.style.left,
        inlineTop: el.style.top,
        computedPos: getComputedStyle(el).position,
        offsetW: el.offsetWidth,
        offsetH: el.offsetHeight,
        offsetL: el.offsetLeft,
        offsetT: el.offsetTop,
      }));
      const payload = JSON.stringify({
        reason,
        ts: new Date().toISOString(),
        mode: localStorage.getItem("overview-layout-mode-v1"),
        free: JSON.parse(localStorage.getItem("overview-free-layout-v1") || "{}"),
        grid: gr ? { left: Math.round(gr.left), top: Math.round(gr.top), width: Math.round(gr.width), clientW: grid.clientWidth, clientH: grid.clientHeight } : null,
        sidebarRect: (() => { const s = document.querySelector(".sidebar"); if (!s) return null; const r = s.getBoundingClientRect(); return { left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width) }; })(),
        cards,
      });
      // 不 await —— 不阻塞 UI；失败也无所谓
      window.pywebview.api._diag_write_free_pos(payload).catch(() => {});
    } catch (e) { /* 诊断失败不阻塞 */ }
  }

  // v0.187.1：自由模式位置钳制 —— 把卡限制在 .grid 容器内。
  //
  // 起因（用户反馈「自由也太自由了，咋还移动到左边的菜单栏上面去了？」）：
  // v0.186/187 的自由拖动对 left/top 完全不设边界，负数也照写。卡一旦被拖出
  // .grid 左边界就滑到侧栏底下 —— 而 .sidebar 是 position:sticky + z-index:500
  // （见 styles 里 v0.118 注释），层级远高于卡片，卡被完全盖住**再也点不到、
  // 拖不回来**，只能清 localStorage 才能救。上边界同理（滑到顶栏后面）。
  //
  // v0.187.2：用户复测仍报「蔓延到侧栏」。代码层钳到 x≥0 已生效（实测负
  // 坐标会被拉回 0），但视觉上「x=0 紧贴 .grid 左边缘」≈「紧贴 .sidebar
  // 右边缘」—— 中间只隔 .main 的 padding，看着像蔓延。改成左/上各留
  // SAFE_MARGIN 像素的安全距离，让卡片起步位置离侧栏/顶栏**明显有一段
  // 空隙**，而不是仅仅「没滑进去」。同步加在 seed 阶段：自由模式初始化
  // 的第一帧就离侧栏有距离，不会出现「刚切到自由就贴着侧栏」的视觉错位。
  //
  // 钳制策略：卡片左上角必须在 [SAFE_MARGIN, container - SAFE_MARGIN - EDGE_KEEP]
  // 内，保证右/下贴边后容器内仍留 SAFE_MARGIN + EDGE_KEEP 像素宽的可抓区。
  const FREE_EDGE_KEEP = 48;
  const FREE_SAFE_MARGIN = 16;
  function _clampFreePos(card, x, y) {
    const grid = card && card.parentElement;
    if (!grid) return { x, y };
    // clientWidth/Height = 容器内容区尺寸（不含 transform 缩放）。
    // getBoundingClientRect 在 hover 上浮 transform 或页面 zoom 下会带上
    // 缩放，拿它算边界会偏。
    const gw = grid.clientWidth;
    const gh = grid.clientHeight;
    let nx = x, ny = y;
    // 左 / 上：硬边界 + 安全边距 —— 越过就压进侧栏 / 顶栏底下；贴边
    // 则视觉上像蔓延。两个 SAFE_MARGIN 之内都不让进。
    if (nx < FREE_SAFE_MARGIN) nx = FREE_SAFE_MARGIN;
    if (ny < FREE_SAFE_MARGIN) ny = FREE_SAFE_MARGIN;
    // 右 / 下：留 SAFE_MARGIN（视觉） + FREE_EDGE_KEEP（抓取）共 64 像素
    // 安全区，保证卡右/下贴边后鼠标还能在容器内抓到卡。
    // Math.max(SAFE_MARGIN, ...) 兜底极端窄窗（容器比保留量还小）。
    const maxX = Math.max(FREE_SAFE_MARGIN, gw - FREE_SAFE_MARGIN - FREE_EDGE_KEEP);
    const maxY = Math.max(FREE_SAFE_MARGIN, gh - FREE_SAFE_MARGIN - FREE_EDGE_KEEP);
    if (gw > 0 && nx > maxX) nx = maxX;
    if (gh > 0 && ny > maxY) ny = maxY;
    return { x: Math.round(nx), y: Math.round(ny) };
  }

  // v0.187：全局布局模式切换（默认模式 ↔ 自由模式）。
  //
  // v0.186 是逐卡开关（每张卡一个 📌 按钮），用户要「一个按钮统一切换」——
  // 这里改成全局：一次切换，总览页所有卡一起换模式。
  //
  // 首次进自由模式时给自由槽 **seed**：以卡当前在 DOM 里的实测位置（相对
  // .grid 的偏移）作为初始 x/y，span/height 从默认槽复制过来。这样用户切
  // 过去看到的是"布局原样没动，只是现在每张卡都能拖了"，而不是所有卡塌到
  // (0,0) 叠成一坨。seed 只在自由槽为空时做一次，之后自由槽自己记着。
  //
  // ⚠ 顺序问题：seed 必须在 _layoutMode 改成 "free" **之前**读位置（此时
  // 卡还在 flex 流里，getBoundingClientRect 拿到的才是"默认布局下的位置"）；
  // 但必须在改模式**之后**写盘（setCardSize 按当前模式路由槽）。所以下面
  // 先量后切再写，三步不能乱。
  function switchLayoutMode(next) {
    const want = next === "free" ? "free" : "default";
    if (want === _layoutMode) return;
    const grid = document.querySelector('.view[data-view="overview"] .grid');

    // --- 1. 进自由模式且自由槽为空 → 先量当前位置（还在 flex 流里） ---
    let seed = null;
    if (want === "free" && !Object.keys(_readSlot(FREE_LAYOUT_KEY)).length && grid) {
      seed = [];
      const gr = grid.getBoundingClientRect();
      grid.querySelectorAll(".glass-card[data-card]").forEach(card => {
        const key = card.dataset.card;
        if (!key) return;
        const r = card.getBoundingClientRect();
        const prev = _readSlot(CARD_SIZE_KEY)[key] || {};
        seed.push({
          key,
          span: typeof prev.span === "number" ? prev.span : naturalSpan(card),
          height: prev.height > 0 ? prev.height : Math.round(r.height),
          x: r.left - gr.left,
          y: r.top - gr.top,
        });
      });
    }

    // --- 2. 切模式（之后所有读写自动走新槽） ---
    _layoutMode = want;
    saveLayoutMode();

    // --- 3. 写 seed 到自由槽 ---
    if (seed) {
      const map = {};
      for (const s of seed) {
        map[s.key] = { span: s.span, height: s.height, x: s.x, y: s.y };
      }
      _writeSlot(map, FREE_LAYOUT_KEY);
    }

    // --- 3.5 v0.187.2：seed 后做一次 overlap 防御（用户要求"初始不重叠"） ---
    // 默认模式 flex-wrap 本就不重叠 → 理论上永远不该触发。但：
    //  (a) seed 时窗口极窄导致 flex 把卡挤到 grid 边界外，钳回 SAFE_MARGIN
    //      后可能挤一块；或
    //  (b) 极端情况有用户手动改过 span > 1（占 2 列宽）的卡，跟其它卡
    //      高度差大时 flex 行排完后末尾卡可能跟下一行卡重叠。
    // 检测到重叠就把后面那张卡整体往下挪一行（y += 行高 + gap），保证
    // 初始第一帧视觉上不重叠。后续用户怎么拖是用户的事 —— 此函数只兜
    // "切到自由模式那一瞬"。
    if (seed && grid) {
      // 取每张卡的真实 offsetWidth/Height（已经被 --ov-span/--ov-h 决定）
      const cards = [];
      grid.querySelectorAll(".glass-card[data-card]").forEach(el => {
        const key = el.dataset.card;
        if (!key || !(key in _readSlot(FREE_LAYOUT_KEY))) return;
        const rec = _readSlot(FREE_LAYOUT_KEY)[key];
        cards.push({ key, el, x: rec.x, y: rec.y, w: el.offsetWidth, h: el.offsetHeight });
      });
      // 简单的 O(n²) 兜底：扫到重叠就把后一张挪到 y += (前者 h + 12)
      let moved = true;
      for (let pass = 0; pass < 16 && moved; pass++) {
        moved = false;
        for (let i = 0; i < cards.length; i++) {
          for (let j = i + 1; j < cards.length; j++) {
            const a = cards[i], b = cards[j];
            // 两卡矩形相交（允许 1px tolerance 防浮点）
            if (a.x < b.x + b.w - 1 && a.x + a.w - 1 > b.x &&
                a.y < b.y + b.h - 1 && a.y + a.h - 1 > b.y) {
              // 后者往下挪一行（用前者的 height + 12px 安全间距）
              b.y = a.y + a.h + 12;
              const p = _clampFreePos(b.el, b.x, b.y);
              b.x = p.x; b.y = p.y;
              moved = true;
            }
          }
        }
      }
      if (moved || cards.some(c => c.y !== seed.find(s => s.key === c.key).y)) {
        // 有挪动 → 把最终位置写回自由槽
        const m2 = _readSlot(FREE_LAYOUT_KEY);
        for (const c of cards) {
          if (m2[c.key]) { m2[c.key].x = c.x; m2[c.key].y = c.y; }
        }
        _writeSlot(m2, FREE_LAYOUT_KEY);
      }
    }

    // --- 4. 全卡按新槽重新应用尺寸 / 位置 ---
    if (grid) {
      grid.querySelectorAll(".glass-card[data-card]").forEach(applyCardSize);
      // 默认模式才有"顺序"概念（flex 流按 DOM 顺序排）。自由模式下卡是
      // absolute 定位，DOM 顺序不影响视觉，跳过以免无谓地动 DOM。
      if (want === "default") applyCardOrder();
    }

    // --- 5. 卡宽/卡高变了 → D3 图必须按新尺寸重算（sig 没变要先清短路标记） ---
    lastCardsSig = null;
    if (lastSnap) renderAll(lastSnap, lastStatus);
    // 临时诊断：模式切换后打一次点 —— 让用户能直接看到 seed 出的初值
    _diagDump("switch:" + want);
  }

  function ensureOverviewCards() {
    const grid = document.querySelector('.view[data-view="overview"] .grid');
    if (!grid) return;
    const cfg = getCardsConfig();
    const want = new Set(cfg);
    const existing = new Map();
    grid.querySelectorAll(".glass-card[data-card]").forEach(el => existing.set(el.dataset.card, el));
    // 不在配置里的旧卡移除；已有卡补上关闭按钮。已有卡的顺序不动 ——
    // 顺序归拖拽（overview-card-order-v1），这里只管"存在性"。
    existing.forEach((el, k) => {
      if (!want.has(k)) { el.remove(); return; }
      if (el.querySelector(".card-title") && !el.querySelector(".card-close-btn")) {
        const title = el.querySelector(".card-title");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "card-close-btn";
        btn.dataset.closeCard = k;
        btn.title = "移除卡片";
        btn.setAttribute("aria-label", "移除卡片");
        btn.textContent = "×";
        title.appendChild(btn);
      }
    });
    // 配置里有但 DOM 缺失的卡补到末尾（新建）。
    cfg.forEach(k => {
      if (!existing.has(k)) {
        const el = createCardEl(k);
        if (el) {
          // 新卡也回填持久化的列跨度/高度，避免刷新后尺寸丢失。
          applyCardSize(el);
          grid.appendChild(el);
        }
      }
    });
    // 已有卡同步持久化的尺寸（初始化 / 本地存储被外部改动时兜底）。
    grid.querySelectorAll(".glass-card[data-card]").forEach(el => {
      applyCardSize(el);
      // 硬编码在 index.html 里的 7 张快照卡没有边缘命中区（边缘只在
      // createCardEl 里加）—— 这里补注入到卡片本身（不是 body），保证
      // 命中区贴卡片视觉外边而不是 padding 里。
      if (!el.querySelector(":scope > [data-card-edge-h]")) {
        el.insertAdjacentHTML("beforeend",
          `<div class="card-edge card-edge-bottom" data-card-edge-h="1" draggable="false" title="拖拽调整卡片高度"></div>` +
          `<div class="card-edge card-edge-right" data-card-edge-w="1" draggable="false" title="拖拽调整卡片宽度（列数，1.0–3.0）"></div>` +
          `<div class="card-edge card-edge-corner" data-card-edge-br="1" draggable="false" title="拖拽调整卡片大小"></div>`);
      }
    });
  }

  function wireCardsManage() {
    // 关闭按钮：document 级委托，动态创建的卡同样生效。
    document.addEventListener("click", (e) => {
      const btn = e.target.closest(".card-close-btn");
      if (!btn) return;
      const key = btn.dataset.closeCard;
      if (!key) return;
      cardsConfig = getCardsConfig().filter(k => k !== key);
      saveCardsConfig();
      const el = document.querySelector(`.glass-card[data-card="${key}"]`);
      if (el) el.remove();
    });
    const fab = $("btn-cards-manage");
    if (fab) fab.addEventListener("click", openCardsManage);
    const overlay = $("cards-manage-overlay");
    if (!overlay) return;
    const close = $("cards-manage-close");
    if (close) close.addEventListener("click", () => { overlay.hidden = true; });
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) overlay.hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && overlay && !overlay.hidden) overlay.hidden = true;
    });
    const body = $("cards-manage-body");
    if (body) {
      body.addEventListener("change", (e) => {
        const key = e.target && e.target.dataset ? e.target.dataset.cardKey : null;
        if (!key) return;
        const cfg = getCardsConfig();
        if (e.target.checked) {
          if (!cfg.includes(key)) cfg.push(key);
        } else {
          cardsConfig = cfg.filter(k => k !== key);
        }
        saveCardsConfig();
        ensureOverviewCards();
      });
    }
  }

  function openCardsManage() {
    const body = $("cards-manage-body");
    if (!body) return;
    // v0.187：顶部一行全局「布局模式」总开关（默认 / 自由 二选一），
    // 取代 v0.186.1 每行一个 📌 按钮的逐卡开关。下面的列表回到纯
    // checkbox + 卡名。
    const free = isFreeMode();
    const modeRow = `<div class="layout-mode-row">` +
      `<span class="layout-mode-label">布局模式</span>` +
      `<div class="layout-mode-group">` +
      `<button type="button" class="layout-mode-btn${free ? "" : " active"}"` +
      ` data-layout-mode="default"` +
      ` title="默认模式：卡片依次排列、自动换行，可拖拽重排和调整尺寸">默认</button>` +
      `<button type="button" class="layout-mode-btn${free ? " active" : ""}"` +
      ` data-layout-mode="free"` +
      ` title="自由模式：卡片可拖到任意位置、允许叠放，尺寸与位置独立记忆">自由</button>` +
      `</div></div>`;
    const rows = CARD_DEFS.map(d => {
      const on = getCardsConfig().includes(d.key);
      return `<label class="cards-manage-item">` +
        `<input type="checkbox" data-card-key="${d.key}"${on ? " checked" : ""} />` +
        `<span class="cards-manage-title">${d.title}</span>` +
        `</label>`;
    }).join("");
    body.innerHTML = modeRow + rows;
    const overlay = $("cards-manage-overlay");
    if (overlay) overlay.hidden = false;
  }

  // Currently-active sidebar-nav view (one of "overview" / "live" /
  // "history" / "upstreams" / "settings"). The `view[hidden]` CSS rule
  // hides every section whose data-view doesn't match, so the only
  // responsibility of ``setView`` is to flip the attribute and rerun
  // the appropriate renderer with the latest snapshot.
  let currentView = "overview";

  function setView(name) {
    if (!name || currentView === name) return;
    const prevView = currentView;
    currentView = name;
    document.querySelectorAll(".view").forEach(v => {
      v.hidden = v.dataset.view !== name;
    });
    document.querySelectorAll(".nav-item").forEach(n => {
      n.classList.toggle("active", n.dataset.view === name);
    });
    // v0.120a：页面切换方向 —— 按目标页在侧栏菜单的上下位置决定进入方向。
    // 往下切（目标在下方）→ 内容从上方滑入；往上切 → 内容从下方滑入。
    // 方向通过 body[data-view-dir] 暴露给 CSS 的 view-enter 动画。
    const items = Array.from(document.querySelectorAll(".nav-item[data-view]"));
    const prevIdx = items.findIndex(n => n.dataset.view === prevView);
    const nextIdx = items.findIndex(n => n.dataset.view === name);
    const dir = (prevIdx >= 0 && nextIdx >= 0 && nextIdx < prevIdx) ? "up" : "down";
    document.body.dataset.viewDir = dir;
    // v0.120a：hero 大标题只在总览页显示，其他页面整段隐藏
    const hero = document.querySelector(".hero");
    if (hero) hero.hidden = (name !== "overview");
    // v0.11.17：#12 设置页时显示侧栏大项子菜单。
    document.body.classList.toggle("is-settings", name === "settings");
    // v0.113r (sweep)：setView 切换可能让 body.is-settings 变化（显示侧栏
    // 子菜单 nav-sub-item），这些节点上一轮 applyLang 因 display:none
    // 被跳过，现在可见但文本节点已不在 walker 命中范围。setView 末尾
    // 主动 applyLang 一次翻这一批新可见节点。
    applyLang();
    if (name === "settings") {
      // 点击子菜单项 → 滚动到对应 section 并高亮。
      const sub = $("nav-settings-sub");
      if (sub) {
        sub.querySelectorAll(".nav-sub-item").forEach(item => {
          item.onclick = () => {
            const key = item.dataset.settingsSub;
            const target = document.querySelector(`.settings-section[data-card="settings-${key}"]`);
            if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
            sub.querySelectorAll(".nav-sub-item").forEach(i => i.classList.toggle("active", i === item));
          };
        });
      }
    }
    // History is paginated from the server — force a fresh load every
    // time the user enters the view so the top of the list always
    // reflects the newest requests, not whatever was cached from the
    // last visit.
    if (name === "history") resetHistory();
    // v0.100：每次进入总览都重播入场动画。
    if (name === "overview") overviewIntroPending = true;
    // v0.102：进入统计页时把工具栏模式对齐当前三极档位（localStorage），
    // 让总览/设置页的切换在统计页也一致。
    if (name === "stats") {
      statsState.mode = getConsumeMode();
      syncStatsModeButtons();
    }
    // v0.11.3: 离开设置页时让 prefs 卡片作废 —— 下次进来重新渲染 +
    // 重新拉后端值（主题/自启/自动切换），用户改动才会反映。
    if (name !== "settings") prefsRendered = false;
    // v0.110：配置页读 lastSnap/lastStatus（接入地址 + 上游 + 快切）。
    // lastSnap=null 时渲染静态骨架（port 缺省 8088），快照到达后 sig
    // 变化自然重渲染。其他视图仍走原逻辑。
    if (currentView === "config") renderConfigPage();
    // Re-render the just-unhidden view immediately with the last known
    // snapshot, so the user doesn't see a stale frame for one tick.
    // v0.100 修正①：进入 overview 用 renderAll（不在 renderActiveView ——
    // 它对 overview 是 early-return），renderAll 的 overview 分支会立即
    // renderTopModelSpotlight + 同步消费 overviewIntroPending 播动画。
    // 否则 spotlight 渲染要等下一个 500ms tick，入场动画拖 ~0.5s 迟开。
    if (lastSnap) {
      if (currentView === "overview") {
        renderAll(lastSnap, lastStatus);
      } else {
        renderActiveView(lastSnap, lastStatus);
      }
    }
  }

  // Exposed on window because ``App.switch_view`` drives it through
  // ``evaluate_js("setView('live')")``, which evaluates in global scope
  // and cannot see anything trapped inside this IIFE.
  window.setView = setView;

  /** Which platform owns this upstream name, per the last snapshot.
   *  The overview panel is keyed by upstream name alone (it comes from
   *  ``by_upstream``), so the platform has to be recovered from the
   *  config map before we can address the editor. */
  function platformOf(name) {
    const cfg = (lastSnap && lastSnap.upstreams) || {};
    for (const [plat, list] of Object.entries(cfg)) {
      if ((list || []).some(c => c.name === name)) return plat;
    }
    return null;
  }

  /** Jump to the settings editor for one upstream and highlight it.
   *
   *  Deliberately reuses the existing settings form rather than growing
   *  a second inline editor: the validation, save handler and error
   *  reporting all live there, and two copies would drift.
   */
  function focusUpstreamConfig(platform, name) {
    const plat = platform || platformOf(name);
    if (!plat || !name) return;
    setView("settings");
    // The card is (re)rendered synchronously by setView, but only if the
    // config signature changed; either way the node exists by the next
    // frame. rAF also lets the view-switch paint first, so the scroll
    // lands on a laid-out element.
    requestAnimationFrame(() => {
      const sel = `.upstream-config[data-platform="${CSS.escape(plat)}"][data-name="${CSS.escape(name)}"]`;
      const card = document.querySelector(sel);
      if (!card) return;
      card.open = true;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      // Retrigger the flash even if the same card is clicked twice.
      card.classList.remove("upstream-config-flash");
      void card.offsetWidth;
      card.classList.add("upstream-config-flash");
      const quota = card.querySelector(".cfg-quota");
      if (quota) quota.focus();
    });
  }

  // =========================================================================
  // v0.110：配置页 —— 「使用中继」操作指南（结构化、运行时感知）。
  // 不再渲染 README.md；改为从 lastSnap/lastStatus 展示：
  //   接入地址 → 客户端配置示例 → 当前 active 上游 → 已配置上游 → 透传模式。
  // 可复制代码块带「复制」按钮；快捷切换 / 上游行点击走 applyUpstreamDirect
  // （与 sidebar 同链路：apply_upstream → 重启中继）。
  // 数据依赖：snapshot.upstreams / snapshot.active_per_platform /
  //   snapshot.quick_switch / snapshot.passthrough_mode / status.port。
  // =========================================================================
  let lastCfgGuideSig = null;

  function copyText(text) {
    const done = () => {
      const btn = document.activeElement;
      if (btn && btn.classList && btn.classList.contains("cfg-copy")) {
        btn.textContent = "已复制";
        setTimeout(() => { btn.textContent = "复制"; }, 1200);
      }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {
        fallbackCopy(text); done();
      });
    } else {
      fallbackCopy(text); done();
    }
  }

  function fallbackCopy(text) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (_) {}
  }

  function cfgCode(label, code, hlTokens) {
    // v0.154：高亮标记 —— `hlTokens` 是需要在 <code> 内染金的子串列表
    // （如 "auto"、"@@"）。渲染时把命中片段用 <span class="cfg-hl"> 包起来，
    // 便于一眼看出「这一段是中继约定的特殊字符」。命中是字面量子串匹配，
    // 不区分多词相邻；调用方传的 token 应尽量互不重叠。
    let body = escape(code);
    if (Array.isArray(hlTokens) && hlTokens.length) {
      // 按 token 长度倒序，避免短 token 把长 token 切碎。
      const sorted = hlTokens
        .map(String)
        .filter((s) => s.length > 0)
        .sort((a, b) => b.length - a.length);
      sorted.forEach((tok) => {
        const safe = escape(tok);
        const re = new RegExp(safe.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g");
        body = body.replace(re, `<span class="cfg-hl">${safe}</span>`);
      });
    }
    return `<div class="cfg-code">
      <div class="cfg-code-head">
        <span class="cfg-code-label">${escape(label)}</span>
        <button type="button" class="cfg-copy" title="复制到剪贴板">复制</button>
      </div>
      <pre><code>${body}</code></pre>
    </div>`;
  }
  // v0.152：客户端配置 —— 一段等宽代码，行内 `KEY = value`，不换行不错位。
  // rows: [{k, v}]。整块进 <pre>，CSS grid 列对齐（等宽字体保证 = 竖线对齐）。
  function cfgFields(rows) {
    const lines = rows.map(r => `${r.k} = ${r.v}`).join("\n");
    return `<div class="cfg-fields"><pre>${escape(lines)}</pre></div>`;
  }
  // v0.152：两列表格（字段 | 值）—— deepseek 请求地址这类 KV 信息。
  function cfgTable(rows) {
    return `<div class="cfg-table-wrap">
      <table class="cfg-table"><tbody>
        ${rows.map(r => `<tr><td class="cfg-table-k">${escape(r[0])}</td><td class="cfg-table-v">${escape(r[1])}</td></tr>`).join("")}
      </tbody></table>
    </div>`;
  }

  function renderConfigPage() {
    const body = $("card-config-body");
    if (!body) return;
    const snap = lastSnap || {};
    const status = lastStatus || {};
    const port = status.port || 8088;
    const active = snap.active_per_platform || {};
    const upstreams = snap.upstreams || {};
    const qs = Array.isArray(snap.quick_switch) ? snap.quick_switch : [];
    const pt = !!snap.passthrough_mode;
    const base = "http://127.0.0.1:" + port;

    // sig 短路：port / active / 上游配置 / 快切 / 透传模式 变化才重渲染，
    // idle 500ms tick 不重写 innerHTML。
    const sig = JSON.stringify([
      port, active,
      Object.keys(upstreams).map(p => (upstreams[p] || []).map(c => c.name + "|" + c.url)),
      qs.map(q => [q.label, q.platform, q.upstream, q.model]),
      pt,
    ]);
    if (sig === lastCfgGuideSig) return;
    lastCfgGuideSig = sig;

    // 已配置上游：按平台顺序列出，active 标「当前」；点击行切换。
    let upstreamRows = "";
    Object.entries(upstreams).forEach(([plat, list]) => {
      (Array.isArray(list) ? list : []).forEach(c => {
        const isActive = active[plat] === c.name;
        const models = (Array.isArray(c.allowed_models) && c.allowed_models.length)
          ? c.allowed_models.join(" · ")
          : (c.model || "任意模型");
        upstreamRows += `
          <button type="button" class="cfg-upstream-row${isActive ? " cfg-upstream-row-active" : ""}"
                  data-switch-platform="${escape(plat)}" data-switch-name="${escape(c.name)}">
            <span class="cfg-upstream-name">${isActive ? '<span class="cfg-active-badge">当前</span>' : ""}<span data-i18n-keep>${escape(c.name)}</span></span>
            <span class="cfg-upstream-url">${escape(c.url || "")}</span>
            <span class="cfg-upstream-models">${escape(models)}</span>
          </button>`;
      });
    });
    if (!upstreamRows) {
      upstreamRows = '<div class="cfg-empty">未配置上游 —— 到「设置 → 上游配置」新建</div>';
    }

    // 快捷切换按钮组（复用 sidebar 同款 class，点击走 applyUpstreamDirect）。
    // 标题由调用方（高级折叠区）负责，这里只出按钮。
    let qsHtml = "";
    if (qs.length) {
      qsHtml = `<div class="cfg-qs-list">
          ${qs.map(q => {
            const ups = (upstreams[q.platform] || []).find(c => c.name === q.upstream);
            const allowed = ups && Array.isArray(ups.allowed_models) ? ups.allowed_models : [];
            const valid = ups && (allowed.length === 0 || allowed.includes(q.model));
            return `<button type="button" class="quick-switch-btn${valid ? "" : " quick-switch-btn-disabled"}"
                    data-qs-platform="${escape(q.platform)}" data-qs-upstream="${escape(q.upstream)}" data-qs-model="${escape(q.model)}" data-i18n-keep>${escape(q.label)}</button>`;
          }).join("")}
        </div>`;
    }

    // 当前 active 上游一览。
    let activeRows = "";
    Object.entries(active).forEach(([plat, name]) => {
      if (!name) return;
      activeRows += `<div class="cfg-active-row"><span class="cfg-active-plat" data-i18n-keep>${escape(plat)}</span><span class="cfg-active-name" data-i18n-keep>${escape(name)}</span></div>`;
    });
    if (!activeRows) activeRows = '<div class="cfg-empty">尚未配置 active 上游</div>';

    body.innerHTML = `
      <div class="cfg-guide">

        <div class="cfg-guide-block">
          <h2 class="cfg-mode-title">${t("转换模式")} <span class="cfg-mode-tag">${t("默认 · 推荐")}</span></h2>
          <p class="cfg-guide-p">${t("转换模式下，模型、上游的选择完全由中继控制，来自 coding 客户端的所有请求，先到达中继，由中继接管，并向中继中选择的上游发送。")}</p>
          <p class="cfg-guide-p">${t("客户端配置只需要将 URL 地址指向中继，然后将 api-key 和模型配置为")} <strong>auto</strong> ${t("占位符即可。")}</p>
          <h3 class="cfg-guide-h">${t("客户端配置：")}</h3>
          ${cfgFields([
            { k: "BASE_URL",   v: `"${base}/anthropic"` },
            { k: "BASE_URL",   v: `"${base}/openai"` },
            { k: "AUTH_TOKEN ( Api_Key )", v: "auto" },
            { k: "model",      v: "auto" },
          ])}
          <h3 class="cfg-guide-h">${t("示例：")}</h3>
          <h4 class="cfg-guide-h">${t("Deepseek 的请求地址（来自官网信息）")}</h4>
          ${cfgTable([
            ["base_url (OpenAI)", "https://api.deepseek.com"],
            ["base_url (Anthropic)", "https://api.deepseek.com/anthropic"],
            ["api_key", "sk-09752*****5eb8" + t("（示例）")],
            ["model", "deepseek-v4-flash / deepseek-v4-pro / deepseek-v4-flash-vision-exp"],
          ])}
          <p class="cfg-guide-p">${t("不走中继时的请求填写（以 claude code 为例）：")}</p>
          ${cfgCode(t("不走中继时的请求填写"), `{\n  "env": {\n    "ANTHROPIC_AUTH_TOKEN": "sk-09752*****5eb8",\n    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",\n    "API_TIMEOUT_MS": "3000000",\n    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",\n    "ANTHROPIC_MODEL": "deepseek-v4-pro"\n  }\n}`)}
          <p class="cfg-guide-p">${t("走中继时，该请求需要改写为：")}</p>
          ${cfgCode(t("走中继时的请求填写"), `{\n  "env": {\n    "ANTHROPIC_AUTH_TOKEN": "auto",\n    "ANTHROPIC_BASE_URL": "${base}/anthropic",\n    "API_TIMEOUT_MS": "3000000",\n    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",\n    "ANTHROPIC_MODEL": "auto"\n  }\n}`, ["auto"])}
        </div>

        <div class="cfg-guide-block">
          <h2 class="cfg-mode-title">${t("完全透传模式")}</h2>
          <p class="cfg-guide-p">${t("透传模式下，为了统计 token 和保存数据，仍然需要让数据包经过中继，因而 base_url 仍需指向中继。")}</p>
          <p class="cfg-guide-p">${t("这会占用原本指向提供商上游的 url 的位置，对于此问题的解决办法是：将")} <strong>${t("上游 url")}</strong> ${t("和")} <strong>api_key</strong> ${t("都写在 api-key 的填写位置，采用如下格式：")}</p>
          ${cfgFields([
            { k: "BASE_URL",   v: `"${base}/anthropic"` },
            { k: "BASE_URL",   v: `"${base}/openai"` },
            { k: "AUTH_TOKEN ( Api_Key )", v: `"${t("上游地址")} + @@ + Api_Key"` },
            { k: "model",      v: t("实际模型") },
          ])}
          <p class="cfg-guide-p">${t("此数据包到达中继后，中继会从 AUTH_TOKEN ( Api_Key ) 中解析字符，将 Api_Key 字段重写为“@@”标识符后半部分的 key，然后向前半部分 URL 发送数据包。")}</p>
          <h3 class="cfg-guide-h">${t("示例：")}</h3>
          <h4 class="cfg-guide-h">${t("Deepseek 的请求地址（来自官网信息）")}</h4>
          ${cfgTable([
            ["base_url (OpenAI)", "https://api.deepseek.com"],
            ["base_url (Anthropic)", "https://api.deepseek.com/anthropic"],
            ["api_key", "sk-09752*****5eb8" + t("（示例）")],
            ["model", "deepseek-v4-flash / deepseek-v4-pro / deepseek-v4-flash-vision-exp"],
          ])}
          <p class="cfg-guide-p">${t("不走中继时的请求填写（以 claude code 为例）：")}</p>
          ${cfgCode(t("不走中继时的请求填写"), `{\n  "env": {\n    "ANTHROPIC_AUTH_TOKEN": "sk-09752*****5eb8",\n    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",\n    "API_TIMEOUT_MS": "3000000",\n    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",\n    "ANTHROPIC_MODEL": "deepseek-v4-pro"\n  }\n}`)}
          <p class="cfg-guide-p">${t("走中继时，该请求需要改写为：")}</p>
          ${cfgCode(t("走中继时的请求填写"), `{\n  "env": {\n    "ANTHROPIC_AUTH_TOKEN": "sk-09752*****5eb8@@https://api.deepseek.com/anthropic",\n    "ANTHROPIC_BASE_URL": "${base}/anthropic",\n    "API_TIMEOUT_MS": "3000000",\n    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",\n    "ANTHROPIC_MODEL": "deepseek-v4-pro"\n  }\n}`, ["@@"])}
        </div>

        <details class="cfg-adv">
          <summary class="cfg-adv-summary">高级：当前上游 · 快捷切换 · 已配置上游</summary>
          <div class="cfg-adv-body">
            <div class="cfg-guide-block">
              <h3 class="cfg-guide-h">当前 active 上游</h3>
              <p class="cfg-guide-p">「auto」请求会路由到这里。</p>
              ${activeRows}
            </div>
            ${qsHtml ? `
              <div class="cfg-guide-block">
                <h3 class="cfg-guide-h">快捷切换</h3>
                <p class="cfg-guide-p">点击按钮直接切到对应（上游, 模型）。</p>
                ${qsHtml}
              </div>` : ""}
            <div class="cfg-guide-block">
              <h3 class="cfg-guide-h">已配置上游</h3>
              <p class="cfg-guide-p">点击任意上游行立即切换为该平台的 active 上游。</p>
              ${upstreamRows}
            </div>
          </div>
        </details>

      </div>`;
  }

  function wireConfigPage() {
    const body = $("card-config-body");
    if (!body || body.dataset.wired) return;
    body.dataset.wired = "1";
    body.addEventListener("click", (e) => {
      const copyBtn = e.target.closest(".cfg-copy");
      if (copyBtn) {
        const codeEl = copyBtn.closest(".cfg-code").querySelector("code");
        if (codeEl) copyText(codeEl.textContent);
        return;
      }
      const qsBtn = e.target.closest("[data-qs-platform]");
      if (qsBtn) {
        const plat = qsBtn.dataset.qsPlatform, ups = qsBtn.dataset.qsUpstream, model = qsBtn.dataset.qsModel;
        if (!plat || !ups) return;
        applyUpstreamDirect(plat, ups, model || null);
        return;
      }
      const swBtn = e.target.closest("[data-switch-platform]");
      if (swBtn) {
        const plat = swBtn.dataset.switchPlatform, name = swBtn.dataset.switchName;
        if (!plat || !name) return;
        applyUpstreamDirect(plat, name, null);
      }
    });
  }

  function renderActiveView(snap, status) {
    if (currentView === "overview") return; // handled by the overview sig short-circuit
    if (currentView === "live")       renderLive($("card-live-body"), snap);
    if (currentView === "history")    renderHistory($("card-history-body"), snap);
    if (currentView === "upstreams")  renderUpstreamsView($("card-upstreams-detail-body"), snap, status);
    if (currentView === "stats")     renderStats();
    if (currentView === "config")    renderConfigPage();
    if (currentView === "settings") {
      // v0.103：透传上游（自动发现）section 仅在透传模式下显示。
      const ptSection = document.querySelector('.settings-section[data-card="settings-passthrough-upstreams"]');
      if (ptSection) ptSection.hidden = !(snap && snap.passthrough_mode);
      renderSettingsRelay($("card-settings-relay-body"), snap, status);
      renderSettingsConfig($("card-settings-config-body"), snap);
      renderSettingsQuickSwitch($("card-settings-quickswitch-body"), snap);
      renderSettingsAgentAlias($("card-settings-agentalias-body"), snap);
      renderSettingsUaRules($("card-settings-uarules-body"), snap);
      renderSettingsPrefs($("card-settings-prefs-body"), snap);
      renderSettingsPassthrough($("card-settings-relay-mode-body"), snap);
      // v0.113n：存储管理（占用 + 位置）。守卫 + 手动刷新，避免每 tick
      // 做文件 I/O + DB 查询；首次渲染用骨架占位，异步拉取后填充。
      renderSettingsStorage($("card-settings-storage-body"));
      // v0.113o：报错分析（开关 + 模型 + 测试）。守卫 + 异步拉取。
      renderSettingsError($("card-settings-error-body"), snap);
      // v0.190：支持图片的模型 —— 容器已搬到开发者模式组内（#prefs-vision-chip-box），
      // 由 renderSettingsPrefs 内的渲染管线异步填充；这里不再单独调用。
      if (snap && snap.passthrough_mode) {
        renderSettingsPassthroughUpstreams($("card-settings-pt-upstreams-body"));
      }
    }
  }

  // -------------------------------------------------------------------------
  // v0.101：统计页 —— treemap（按工具栏维度拆 token 消耗）+ 今日饼图。
  // 数据走 pywebview 桥 statsAggregate / statsDaily（file:// origin 下
  // fetch() 不可用）。工具栏 range 只影响 treemap；饼图固定"今日"（1d）。
  // -------------------------------------------------------------------------
  const statsState = { range: "30d", dim: "upstream", mode: "relay", loading: false, needRender: false };
  // v0.113u：range/dim 从 localStorage 恢复上次选择 —— 与 mode 已有
  // localStorage 持久化保持一致。mode 由 getConsumeMode() 在 1537 行恢复。
  try {
    const _savedRange = localStorage.getItem("stats-range");
    if (_savedRange === "1d" || _savedRange === "7d" || _savedRange === "30d") {
      statsState.range = _savedRange;
    }
    const _savedDim = localStorage.getItem("stats-dim");
    if (_savedDim === "upstream" || _savedDim === "model") {
      statsState.dim = _savedDim;
    }
  } catch (_) {}
  // 上次真正渲染统计图表的参数签名 —— 参数未变（poll tick 同参刷新）不重播淡入。
  let lastStatsRenderKey = "";
  const STATS_RANGE_LABEL = { "1d": "近 24h", "7d": "近 7 天", "30d": "近 30 天" };

  function cssVar(name, fallback) {
    try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; }
    catch (_) { return fallback; }
  }

  // 主题相关的取色在渲染时实时读 CSS 变量，切主题后重绘即生效。
  const statsPalette = [
    () => cssVar("--platform-anthropic", "#d97706"),
    () => cssVar("--platform-openai", "#10a37f"),
    () => cssVar("--accent", "#f59e0b"),
  ];

  function statsHost(id) {
    const el = $(id);
    if (!el) return null;
    return el;
  }

  function statsEmpty(host, text) {
    if (!host) return;
    host.innerHTML = `<div class="card-empty">${text || "暂无数据"}</div>`;
  }

  function statsError(host, err) {
    if (!host) return;
    host.innerHTML = `<div class="card-empty">${escape(err || "加载失败")}</div>`;
  }

  // 计算属性颜色：key 命中平台/上游配色表则用主题变量，否则走固定色板。
  function statsColor(i, key) {
    if (key === "anthropic" || /anthropic/i.test(key || "")) return statsPalette[0]();
    if (key === "openai" || /openai/i.test(key || "")) return statsPalette[1]();
    const base = [statsPalette[2](), "#8b5cf6", "#ec4899", "#06b6d4", "#f97316", "#84cc16", "#6366f1", "#14b8a6", "#f43f5e", "#a3e635"];
    return base[i % base.length];
  }

  // 仅保留 total_tokens 最高的前 12 项，其余合并为 "other"。
  // rows 已由后端按 total_tokens 降序返回，直接取前 12 即可。
  function top12WithOther(rows) {
    if (!rows || rows.length <= 12) return rows;
    const top = rows.slice(0, 12);
    const rest = rows.slice(12);
    const other = {
      key: "other",
      requests: 0, input_tokens: 0, output_tokens: 0,
      cache_read_input_tokens: 0, cache_creation_input_tokens: 0,
      errors: 0, total_tokens: 0,
    };
    for (const r of rest) {
      other.requests += r.requests || 0;
      other.input_tokens += r.input_tokens || 0;
      other.output_tokens += r.output_tokens || 0;
      other.cache_read_input_tokens += r.cache_read_input_tokens || 0;
      other.cache_creation_input_tokens += r.cache_creation_input_tokens || 0;
      other.errors += r.errors || 0;
      other.total_tokens += r.total_tokens || 0;
    }
    return top.concat(other);
  }

  // v0.113i：zoom 下 getBoundingClientRect() 返回物理像素（已乘 --page-zoom），
  // 若直接拿去设 SVG/CSS 尺寸会被双重放大（视觉宽 = 容器宽 × zoom）超宽被裁。
  // 换算回 CSS 像素：/ zoom。
  function _statsHostSize(host) {
    const r = host.getBoundingClientRect();
    const z = parseFloat(getComputedStyle(document.body).getPropertyValue("--page-zoom")) || 1;
    return { w: r.width / z, h: r.height / z };
  }

  function renderTreemap(data, host) {
    if (!host) return;
    const rows = top12WithOther((data && Array.isArray(data.rows)) ? data.rows : []);
    if (!rows.length) { statsEmpty(host, "暂无数据"); return; }
    host.innerHTML = "";

    const size = _statsHostSize(host);
    const w = Math.max(200, size.w || 320);
    const baseH = Math.max(220, size.h || 220);

    // ---- 底部图例（索引 → 名称），按文字宽度换行，svg 高度随之扩展 ----
    const total = d3.sum(rows, d => d.total_tokens || 0) || 1;
    const legendItems = rows.map((d, i) => ({
      label: d.key,
      color: statsColor(i, d.key),
    }));
    const LEGEND_ROW_H = 16;
    const legendLines = [];
    let line = [];
    let lineW = 0;
    for (const it of legendItems) {
      const itemW = 8 + 4 + it.label.length * 6 + 12;
      if (line.length && lineW + itemW > w - 8) {
        legendLines.push(line);
        line = [];
        lineW = 0;
      }
      line.push(it);
      lineW += itemW;
    }
    if (line.length) legendLines.push(line);
    const legendH = legendLines.length * LEGEND_ROW_H + 4;
    // v0.113i 续：svg 总高度 = 容器 CSS 高度，treemap 与图例共享预算，
    // 再配合 viewBox + CSS 100% 让 svg 精确跟随容器（缩放不溢出，图例完整可见）。
    let treeH = baseH - legendH;
    if (treeH < 60) treeH = 60;
    const totalH = treeH + legendH;

    const root = { name: "root", children: rows.map((r, i) => ({ name: r.key, value: r.total_tokens || 1, i })) };
    const hierarchy = d3.hierarchy(root).sum(d => d.value);
    const treemap = d3.treemap().size([w, treeH]).paddingInner(2).paddingTop(2).paddingOuter(2);
    treemap(hierarchy);

    // 不设固定 px（会双倍放大 / 超出容器），改 viewBox + preserveAspectRatio，
    // 尺寸由 CSS 的 .stats-chart-host svg { width:100%; height:100% } 跟随容器。
    const svg = d3.select(host).append("svg")
      .attr("viewBox", "0 0 " + w + " " + totalH)
      .attr("preserveAspectRatio", "xMidYMid meet");
    const cells = svg.selectAll("rect").data(hierarchy.leaves()).enter().append("rect")
      .attr("x", d => d.x0).attr("y", d => d.y0)
      .attr("width", d => Math.max(0, d.x1 - d.x0))
      .attr("height", d => Math.max(0, d.y1 - d.y0))
      .attr("class", "bar")
      .attr("rx", 3).attr("ry", 3)
      .style("fill", d => statsColor(d.data.i, d.data.name))
      .style("fill-opacity", 0.82);
    // 名称 + token 数：
    //  - 块够大（居中显示两行）：高 > 34 且宽足够放下名称
    //  - 块够高但太窄：名称不放，token 数字改贴右下角（不溢出）
    //  - 块太小：高 ≤ 34，全部隐藏，悬停 tooltip 看完整信息
    const NAME_PX = 7.2;          // 名称字符宽估算
    const NUM_PX  = 6.6;          // 数字字符宽估算（含逗号稍宽）
    const SAFE    = 8;            // 两侧留白总和
    function textLayout(d) {
      const bw = d.x1 - d.x0;
      const bh = d.y1 - d.y0;
      const name = d.data.name || "";
      const num  = fmtTokens(d.value);
      const nameW = name.length * NAME_PX + SAFE;
      const numW  = num.length  * NUM_PX  + SAFE;
      if (bh > 34 && bw >= nameW && bw >= numW) {
        // 两行居中
        return { showName: true,  showNum: true,
                 anchor: "middle", cx: (d.x0 + d.x1) / 2,
                 ny: (d.y0 + d.y1) / 2 - 4, vy: (d.y0 + d.y1) / 2 + 10 };
      }
      if (bh > 34 && bw >= numW) {
        // 单行居中只放数字（挤掉名称）
        return { showName: false, showNum: true,
                 anchor: "middle", cx: (d.x0 + d.x1) / 2,
                 ny: 0, vy: (d.y0 + d.y1) / 2 + 4 };
      }
      if (bh > 20 && bw >= numW) {
        // 太窄：数字贴右下角，绝不溢出
        return { showName: false, showNum: true,
                 anchor: "end", cx: d.x1 - 4,
                 ny: 0, vy: d.y1 - 4 };
      }
      return { showName: false, showNum: false, anchor: "middle", cx: 0, ny: 0, vy: 0 };
    }

    const txt = svg.selectAll("text.t").data(hierarchy.leaves()).enter().append("text")
      .attr("class", "t")
      .attr("data-i18n-keep", "")
      .attr("text-anchor", d => textLayout(d).anchor)
      .attr("x", d => textLayout(d).cx)
      .attr("y", d => textLayout(d).ny)
      .style("fill", "#000")
      .text(d => textLayout(d).showName ? d.data.name : "");
    svg.selectAll("text.v").data(hierarchy.leaves()).enter().append("text")
      .attr("class", "v")
      .attr("text-anchor", d => textLayout(d).anchor)
      .attr("x", d => textLayout(d).cx)
      .attr("y", d => textLayout(d).vy)
      .style("fill", "#000")
      .text(d => textLayout(d).showNum ? fmtTokens(d.value) : "");
    // title tooltip：悬停显示"索引 + 名称 + token 数"
    cells.append("title").text(d => `${d.data.name}\n${fmtTokens(d.value)} tokens`);
    txt.attr("fill", "#000").style("font-weight", "600");

    // 图例：索引 → 名称，逐行渲染
    const legend = svg.append("g").attr("transform", `translate(4,${treeH + 4})`);
    legendLines.forEach((lineArr, li) => {
      let lx = 0;
      lineArr.forEach((it) => {
        legend.append("rect").attr("x", lx).attr("y", li * LEGEND_ROW_H).attr("width", 8).attr("height", 8).attr("rx", 2).style("fill", it.color);
        legend.append("text").attr("data-i18n-keep", "").attr("x", lx + 12).attr("y", li * LEGEND_ROW_H + 8).style("font-size", "11px").text(it.label);
        lx += 8 + 4 + it.label.length * 6 + 12;
      });
    });
  }

  function renderPie(data, host) {
    if (!host) return;
    const rows = top12WithOther((data && Array.isArray(data.rows)) ? data.rows : []);
    if (!rows.length) { statsEmpty(host, "暂无数据"); return; }
    host.innerHTML = "";

    const size = _statsHostSize(host);
    const w = Math.max(180, size.w || 260);
    const total = d3.sum(rows, d => d.total_tokens || 0) || 1;

    // ---- 先计算图例布局（按实际文字宽度换行），再决定 svg 总高度 ----
    // 10px 字号，ASCII ≈5.5px/字符 + 色块(8) + 间距(4) + 项间距(12)
    const legendItems = rows.map((d, i) => ({
      key: d.key,
      label: `${d.key} ${(100 * (d.total_tokens || 0) / total).toFixed(0)}%`,
      color: statsColor(i, d.key),
    }));
    const LEGEND_ROW_H = 18;
    const legendLines = [];
    let line = [];
    let lineW = 0;
    for (const it of legendItems) {
      const itemW = 8 + 4 + it.label.length * 6 + 12;
      if (line.length && lineW + itemW > w - 8) {
        legendLines.push(line);
        line = [];
        lineW = 0;
      }
      line.push(it);
      lineW += itemW;
    }
    if (line.length) legendLines.push(line);
    const legendH = legendLines.length * LEGEND_ROW_H + 4;
    // v0.113i 续：饼图区域 = 容器 CSS 高度 - 图例，svg 总高度 = 容器高度，
    // viewBox + CSS 100% 让 svg 精确跟随容器（缩放不溢出，图例完整可见）。
    const pieH = Math.max(60, (size.h || 220) - legendH);
    const totalH = pieH + legendH;
    const r = Math.min(w, pieH) / 2 - 6;

    // 不设固定 px（会双倍放大 / 超出容器），改 viewBox + preserveAspectRatio，
    // 尺寸由 CSS 的 .stats-chart-host svg { width:100%; height:100% } 跟随容器。
    const svg = d3.select(host).append("svg")
      .attr("viewBox", "0 0 " + w + " " + totalH)
      .attr("preserveAspectRatio", "xMidYMid meet");
    const g = svg.append("g").attr("transform", `translate(${w / 2},${pieH / 2})`);
    const pie = d3.pie().sort(null).value(d => d.total_tokens || 0)(rows);
    const arc = d3.arc().innerRadius(r * 0.45).outerRadius(r - 2);
    const arcs = g.selectAll("path").data(pie).enter().append("path")
      .attr("d", arc)
      .attr("class", "arc")
      .style("fill", (d, i) => statsColor(i, d.data.key))
      .style("stroke", cssVar("--card-bg", "#fff"))
      .style("stroke-width", 1.5);
    arcs.append("title").text(d => `${d.data.key}\n${fmtTokens(d.data.total_tokens)} tokens (${(100 * d.value / total).toFixed(1)}%)`);

    // 圆心：总 token 数
    g.append("text").attr("text-anchor", "middle").attr("dy", "-0.2em")
      .attr("class", "title").style("font-size", "12px").text("总 tokens");
    g.append("text").attr("text-anchor", "middle").attr("dy", "1.1em")
      .style("fill", "var(--text-primary)").style("font-size", "13px").style("font-weight", "700")
      .text(fmtTokens(total));

    // 图例：按 legendLines 逐行渲染，起始 y 在饼图下方
    const legend = svg.append("g").attr("transform", `translate(4,${pieH + 4})`);
    legendLines.forEach((lineArr, li) => {
      let lx = 0;
      lineArr.forEach((it) => {
        legend.append("rect").attr("x", lx).attr("y", li * LEGEND_ROW_H).attr("width", 8).attr("height", 8).attr("rx", 2).style("fill", it.color);
        legend.append("text").attr("data-i18n-keep", "").attr("x", lx + 12).attr("y", li * LEGEND_ROW_H + 8).style("font-size", "11px").text(it.label);
        lx += 8 + 4 + it.label.length * 6 + 12;
      });
    });
  }

  // v0.155：30天按平台流量 —— 固定拉 relay 库 agent 维度（30d），与上方
  // dim/range/模式选择器解耦（agent 字段只存在于转换库）。TTL 30s，避免
  // 每 500ms poll tick 反复打桥。渲染前把原始 agent 名经 agentDisplayName
  // 映射成显示名（与总览「平台流量」卡片一致）。
  let agentStatsCache = null;
  let agentStatsFetchedAt = 0;
  const AGENT_STATS_TTL = 30000;

  async function renderStatsAgent(host) {
    if (!host) return;
    const now = Date.now();
    if (!agentStatsCache || now - agentStatsFetchedAt > AGENT_STATS_TTL) {
      try {
        const d = await api.statsAggregate("agent", "30d", 0, "relay");
        if (d && !d.error) { agentStatsCache = d; agentStatsFetchedAt = now; }
        else if (d && d.error) { statsError(host, d.error); return; }
      } catch (err) {
        console.error("[agent] statsAggregate failed", err);
        return;
      }
    }
    if (!agentStatsCache) { statsEmpty(host, "暂无数据"); return; }
    const data = {
      ...agentStatsCache,
      rows: Array.isArray(agentStatsCache.rows)
        ? agentStatsCache.rows.map(r => ({ ...r, key: agentDisplayName(r.key) }))
        : [],
    };
    renderPie(data, host);
  }

  let statsWired = false;
  function wireStatsToolbar() {
    if (statsWired) return;
    statsWired = true;
    // v0.113u：首帧按 statsState 当前值高亮 active —— 让恢复的
    // range/dim 在切到 stats 视图后立刻可见，而不是 HTML 默认高亮。
    document.querySelectorAll(".stats-range-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.range === statsState.range);
    });
    document.querySelectorAll(".stats-dim-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.dim === statsState.dim);
    });
    const rGroup = $("stats-range-group");
    if (rGroup) rGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".stats-range-btn");
      if (!btn || btn.classList.contains("active")) return;
      rGroup.querySelectorAll(".stats-range-btn").forEach(b => b.classList.toggle("active", b === btn));
      statsState.range = btn.dataset.range;
      // v0.113u：range 切到 localStorage，下次启动恢复。
      try { localStorage.setItem("stats-range", statsState.range); } catch (_) {}
      statsState.needRender = true;
      renderStats();
    });
    const dGroup = $("stats-dim-group");
    if (dGroup) dGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".stats-dim-btn");
      if (!btn || btn.classList.contains("active")) return;
      dGroup.querySelectorAll(".stats-dim-btn").forEach(b => b.classList.toggle("active", b === btn));
      statsState.dim = btn.dataset.dim;
      // v0.113u：dim 切到 localStorage，下次启动恢复。
      try { localStorage.setItem("stats-dim", statsState.dim); } catch (_) {}
      statsState.needRender = true;
      renderStats();
    });
    // 透传模式 tab —— 与 dim/range 同款：点中切 active + 重渲染。
    // v0.102 三档（透传/全部/转换），与三极开关（总览/设置页）联动。
    const mGroup = $("stats-mode-group");
    if (mGroup) mGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".stats-mode-btn");
      if (!btn || btn.classList.contains("active")) return;
      statsState.mode = btn.dataset.mode || "relay";
      window._consumeMode = statsState.mode;
      try { localStorage.setItem("consume-mode", statsState.mode); } catch (err) {}
      mountConsumeSwitches();
      // v0.112f：同步本页三档按钮的 active 高亮 —— 之前漏掉这行，
      // 内容已切换但按钮本体位置不动。
      syncStatsModeButtons();
      statsState.needRender = true;
      renderStats();
    });
  }

  function syncStatsModeButtons() {
    const mGroup = $("stats-mode-group");
    if (!mGroup) return;
    mGroup.querySelectorAll(".stats-mode-btn").forEach(b => {
      b.classList.toggle("active", b.dataset.mode === statsState.mode);
    });
  }

  async function renderStats() {
    wireStatsToolbar();
    const hint = $("stats-range-hint");
    if (hint && STATS_RANGE_LABEL[statsState.range]) {
      const modeLabel = statsState.mode === "passthrough" ? " · 仅透传"
        : statsState.mode === "all" ? " · 全部" : "";
      const rangeZh = STATS_RANGE_LABEL[statsState.range];
      const dimZh = statsState.dim === "model" ? "模型" : "上游";
      // v0.151：所有非 zh 语言都走翻译字典
      const _dict = I18N[I18N.lang] || I18N.ja;
      hint.textContent = I18N.lang !== "zh"
        ? `${_dict[rangeZh] || rangeZh} ${_dict[dimZh] || dimZh}${t(modeLabel)}`
        : `${rangeZh} · 按${dimZh}${modeLabel}`;
    }
    if (statsState.loading) { statsState.needRender = true; return; }
    statsState.loading = true;
    try {
      // 透传模式走独立 stats；dim 限制为 upstream/model/model_field。
      const dimForPt = statsState.dim === "model" ? "model" : "upstream";
      let tree, pie;
      if (statsState.mode === "all") {
        // "全部"档：relay + passthrough 两库前端合并（model 按 key 相加，
        // upstream 拼接并给透传行加 [透传] 前缀）。
        [tree, pie] = await Promise.all([
          statsAll(statsState.dim, statsState.range, 0),
          statsAll(statsState.dim, "1d", 0),
        ]);
      } else {
        const dim = statsState.mode === "passthrough" ? dimForPt : statsState.dim;
        [tree, pie] = await Promise.all([
          api.statsAggregate(dim, statsState.range, 0, statsState.mode),
          api.statsAggregate(dim, "1d", 0, statsState.mode),
        ]);
      }
      // v0.102.2：切档/换参后 treemap + pie 一起淡入。只在参数真正变化
      // 时触发 —— poll tick 每 500ms 也会调 renderStats（currentView=
      // stats 时走 renderActiveView），同参数刷新不重播动画，否则图表会
      // 每半秒闪一次淡入。
      const statsKey = `${statsState.mode}|${statsState.range}|${statsState.dim}`;
      const fadeStats = statsKey !== lastStatsRenderKey;
      lastStatsRenderKey = statsKey;
      if (fadeStats) {
        ["stats-host-treemap", "stats-host-pie"].forEach(id => {
          const h = statsHost(id);
          if (h) {
            h.classList.remove("stats-chart-fade");
            void h.offsetWidth;
            h.classList.add("stats-chart-fade");
          }
        });
      }
      const _treeHost = statsHost("stats-host-treemap");
      const _pieHost = statsHost("stats-host-pie");
      if (tree && tree.error) statsError(_treeHost, tree.error);
      else renderTreemap(tree, _treeHost);
      if (pie && pie.error) statsError(_pieHost, pie.error);
      else renderPie(pie, _pieHost);
      // v0.155：平台流量块 —— 独立于 dim 选择器的固定 30d agent 聚合。
      await renderStatsAgent(statsHost("stats-host-agent"));
      // v0.153：单模型 30 天调用分布 —— 30d 档才拉（1d/7d 档保留默认
      // 空态「选择模型」）。带去抖缓存：桥调用 ~3s 超时，poll tick
      // 每 500ms 触发 renderStats，重复拉会一直打桥；30s TTL 内复用。
      const _mdHost = statsHost("stats-host-model-daily");
      const _mdChips = $("stats-md-chips");
      const _mdSel = $("stats-md-select");
      if (statsState.range === "30d") {
        const _mdNow = Date.now();
        if (!modelDailyCache || _mdNow - modelDailyFetchedAt > MODEL_DAILY_TTL) {
          const _md = await api.statsModelDaily(30);
          if (_md && !_md.error && _md.models) { modelDailyCache = _md; modelDailyFetchedAt = _mdNow; }
          else if (_md && _md.error) statsError(_mdHost, _md.error);
        }
        renderStatsModelDaily(_mdHost, _mdChips, _mdSel);
      } else {
        renderStatsModelDailyReset(_mdHost, _mdChips, _mdSel);
      }
    } catch (err) {
      console.error("renderStats failed", err);
      statsError(statsHost("stats-host-treemap"), "桥不可用");
      statsError(statsHost("stats-host-pie"), "桥不可用");
    } finally {
      statsState.loading = false;
      if (statsState.needRender) {
        statsState.needRender = false;
        // 避免在 finally 里立即递归 -> 下一帧重跑一次选中的 range/dim
        requestAnimationFrame(renderStats);
      }
    }
  }

  // v0.153：单模型 30 天调用分布 —— 缓存（TTL 30s）+ 当前选中模型。
  let modelDailyCache = null;
  let modelDailyFetchedAt = 0;
  let modelDailySelected = "all";   // "all" = 全部模型叠加 / 具体模型名 = 单模型
  const MODEL_DAILY_TTL = 30000;
  // 重渲染恢复 tip 用：tick / 切 chip 重建 hit-rect 后，原 mouseenter handler
  // 丢失 —— 不记录就闪烁消失直到用户动鼠标。
  let mdLastHoverIdx = 0;
  let mdHoverVisible = false;

  function renderStatsModelDailyReset(host, chips, sel) {
    // 非 30d 档：清掉旧图，保留默认占位（图表 host 保持「暂无数据」）。
    if (chips) chips.innerHTML = "";
    if (sel) sel.innerHTML = "";
    if (host) host.innerHTML = '<div class="card-empty">暂无数据</div>';
    mdHoverVisible = false;   // 离开图区后再回来会被新一轮 mouseenter 重新激活。
  }

  function _modelDailyFmt(v) {
    return fmtTokens(v);   // 沿用现有 token 缩写格式（12.3M / 1.2B …）
  }

  function renderStatsModelDaily(host, chips, sel) {
    if (!host) return;
    const data = modelDailyCache;
    if (!data || !data.models || !data.models.length) {
      if (host) host.innerHTML = '<div class="card-empty">暂无数据</div>';
      if (chips) chips.innerHTML = "";
      if (sel) sel.innerHTML = "";
      return;
    }
    // 选中模型名可能已不在缓存里（数据变了）—— 回退到「全部」。
    const validNames = data.models.map(m => m.model);
    if (modelDailySelected !== "all" && !validNames.includes(modelDailySelected)) {
      modelDailySelected = "all";
    }

    // 按钮条：全部 + 各模型。点击切换选中，重建图 + 同步下拉。
    if (chips && !chips.dataset.wired) {
      chips.dataset.wired = "1";
      chips.addEventListener("click", (e) => {
        const btn = e.target.closest(".stats-md-chip");
        if (!btn) return;
        modelDailySelected = btn.dataset.model === "__all__" ? "all" : btn.dataset.model;
        renderStatsModelDaily(host, chips, sel);
      });
    }
    if (chips) {
      const ch = [{
        label: t("全部"),
        model: "__all__",
      }].concat(data.models.map(m => ({ label: m.model, model: m.model })));
      chips.innerHTML = ch.map(c =>
        `<button type="button" class="stats-md-chip${(c.model === "__all__" ? modelDailySelected === "all" : modelDailySelected === c.model) ? " active" : ""}" data-model="${escape(c.model)}">${escape(c.label)}</button>`
      ).join("");
    }

    // 下拉（长模型名兜底）：同步当前选中。
    if (sel) {
      sel.innerHTML = '<option value="all">' + escape(t("全部模型")) + "</option>" +
        data.models.map(m => `<option value="${escape(m.model)}" data-i18n-keep>${escape(m.model)}</option>`).join("");
      sel.value = modelDailySelected === "all" ? "all" : modelDailySelected;
      if (!sel.dataset.wired) {
        sel.dataset.wired = "1";
        sel.addEventListener("change", () => {
          modelDailySelected = sel.value === "all" ? "all" : sel.value;
          renderStatsModelDaily(host, chips, sel);
        });
      }
    }

    const days = (data.days || []).map(d => d.date);
    let series;
    if (modelDailySelected === "all") {
      // 全部：各模型按天叠柱 + 合计线。
      series = data.models.map(m => ({
        name: m.model,
        color: m.color,
        values: (data.series[m.model] || []).map(s => s.total_tokens),
      }));
    } else {
      const m = data.models.find(x => x.model === modelDailySelected) || data.models[0];
      series = [{
        name: m.model,
        color: m.color,
        values: (data.series[m.model] || []).map(s => s.total_tokens),
      }];
    }
    renderStatsModelDailyChart(host, days, series, data.items || []);
  }

  function renderStatsModelDailyChart(host, days, series, items) {
    if (!host || !days || !days.length || !series.length) {
      host.innerHTML = '<div class="card-empty">' + t("暂无数据") + '</div>';
      return;
    }
    // SVG 与 tip 共存于 host；每次重渲染只清 SVG 容器（.md-svg），保留
    // .md-tip 节点 —— 否则 tick 重渲染会把「鼠标停留中的 tip」瞬间清掉，
    // 500ms 后下一个 mousemove 才重建，体验上闪烁。
    let svgHost = host.querySelector(".md-svg");
    if (!svgHost) {
      // 首次渲染 —— 整个 host 初始为空，正常清空。
      host.innerHTML = "";
      svgHost = document.createElement("div");
      svgHost.className = "md-svg";
      svgHost.style.cssText = "position:absolute;inset:0;";
      host.appendChild(svgHost);
    } else {
      svgHost.innerHTML = "";
    }
    const size = _statsHostSize(host);
    const w = Math.max(300, size.w || 640);
    const h = Math.max(240, size.h || 240);

    const n = days.length;
    const padL = 46, padR = 12, padT = 16, padB = 26;
    const iw = w - padL - padR;
    const ih = h - padT - padB;
    const x = d3.scaleBand().domain(days.map((_, i) => i)).range([0, iw]).paddingInner(0.14).paddingOuter(0.02);
    const maxV = d3.max(series, s => d3.max(s.values) || 0) || 1;
    const y = d3.scaleLinear().domain([0, maxV * 1.06]).range([ih, 0]);

    const svg = d3.select(svgHost).append("svg")
      .attr("viewBox", `0 0 ${w} ${h}`)
      .attr("preserveAspectRatio", "xMidYMid meet")
      .attr("width", "100%").attr("height", "100%");
    const g = svg.append("g").attr("transform", `translate(${padL},${padT})`);

    // Y 轴刻度（4 档，缩写）。
    const yTicks = y.ticks(4);
    g.selectAll("line.ygrid").data(yTicks).enter().append("line")
      .attr("x1", 0).attr("x2", iw).attr("y1", d => y(d)).attr("y2", d => y(d))
      .attr("class", "md-grid");
    g.selectAll("text.ytick").data(yTicks).enter().append("text")
      .attr("x", -6).attr("y", d => y(d)).attr("dy", "0.32em")
      .attr("text-anchor", "end").style("font-size", "10px")
      .text(d => fmtTokens(d));

    // X 轴：首/中/尾三个日期标签。
    const labelIdx = [0, Math.floor((n - 1) / 2), n - 1];
    labelIdx.forEach(i => {
      g.append("text").attr("x", x(i) + x.bandwidth() / 2).attr("y", ih + 16)
        .attr("text-anchor", "middle").style("font-size", "10px")
        .text(days[i].slice(5));
    });

    // 柱（全部=多模型叠加 / 单模型=一根）。
    if (series.length === 1) {
      const s = series[0];
      g.selectAll("rect.md-bar").data(days.map((_, i) => i)).enter().append("rect")
        .attr("x", i => x(i)).attr("width", x.bandwidth())
        .attr("y", i => y(s.values[i])).attr("height", i => ih - y(s.values[i]))
        .attr("rx", 2).attr("fill", s.color).attr("fill-opacity", 0.85);
    } else {
      // 多模型叠柱：每根柱拆 N 段，自底向上累加。
      const order = series.map((_, i) => i);
      days.forEach((_, di) => {
        let acc = 0;
        order.forEach(si => {
          const v = series[si].values[di] || 0;
          if (v <= 0) return;
          const y0 = y(acc), y1 = y(acc + v);
          g.append("rect")
            .attr("x", x(di)).attr("width", x.bandwidth())
            .attr("y", y1).attr("height", Math.max(0, y0 - y1))
            .attr("fill", series[si].color).attr("fill-opacity", 0.85);
          acc += v;
        });
      });
    }

    // 浮动 tooltip —— 鼠标移到任意一天时显示当日具体数字。整张卡共用一个
    // <div class="md-tip">，绝对定位到 host 内（host 设 position:relative）。
    // 多模型态显示「日期 + 每模型 tokens + 当天合计」；单模型态显示「日期 +
    // 模型名 + tokens」。位置智能避开右边界（不够放右侧就放左侧）。
    let tip = host.querySelector(".md-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "md-tip";
      tip.style.display = "none";
      host.appendChild(tip);
    }
    // 重渲染不要主动改 display —— 用户鼠标若仍在图上，hover handler
    // 会在 mousemove 时重新填充内容；这里改了反而会让 tip 在每次 tick
    // 闪烁一下。

    const showTip = (i, evt) => {
      mdLastHoverIdx = i;
      mdHoverVisible = true;
      const rows = series
        .map(s => ({ name: s.name, color: s.color, v: s.values[i] || 0 }))
        .filter(r => r.v > 0);
      const total = rows.reduce((a, r) => a + r.v, 0);
      const lines = [
        `<div class="md-tip-date">${days[i]}</div>`,
      ];
      if (rows.length > 1) {
        rows.forEach(r => {
          lines.push(
            `<div class="md-tip-row"><span class="md-tip-dot" style="background:${r.color}"></span>` +
            `<span class="md-tip-name">${escape(r.name)}</span>` +
            `<span class="md-tip-val">${_modelDailyFmt(r.v)}</span></div>`
          );
        });
        lines.push(`<div class="md-tip-total">${t("合计")} ${_modelDailyFmt(total)}</div>`);
      } else if (rows.length === 1) {
        lines.push(
          `<div class="md-tip-row"><span class="md-tip-dot" style="background:${rows[0].color}"></span>` +
          `<span class="md-tip-name">${escape(rows[0].name)}</span>` +
          `<span class="md-tip-val">${_modelDailyFmt(rows[0].v)}</span></div>`
        );
      } else {
        lines.push(`<div class="md-tip-empty">${t("无数据")}</div>`);
      }
      tip.innerHTML = lines.join("");
      tip.style.display = "block";
      // 定位：相对于 host。SVG 用 viewBox + preserveAspectRatio="xMidYMid meet"
      // 会按 host 的 aspect ratio 做等比缩放并居中 —— host 比 svg 宽时左右
      // 可能留白，所以必须用 svg 节点的实际 rect 而非 host 整体 rect。
      const hostRect = host.getBoundingClientRect();
      const svgRect = svg.node().getBoundingClientRect();
      const tipRect = tip.getBoundingClientRect();
      // viewBox → svg 渲染区的缩放系数（x、y 相同，因为 preserveAspectRatio meet）。
      const scale = svgRect.width / w;
      const offX = svgRect.left - hostRect.left;            // svg 在 host 内的偏移
      const offY = svgRect.top - hostRect.top;
      const barCenter = x(i) + x.bandwidth() / 2;
      const cx = offX + (padL + barCenter) * scale;
      const cy = offY + padT * scale + 4;
      let left = cx + 10;
      if (left + tipRect.width + 8 > hostRect.width) left = cx - tipRect.width - 10;
      left = Math.max(4, Math.min(left, hostRect.width - tipRect.width - 4));
      let top = cy;
      if (top + tipRect.height + 4 > hostRect.height) top = hostRect.height - tipRect.height - 4;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    };
    const hideTip = () => {
      mdHoverVisible = false;
      tip.style.display = "none";
    };

    // 透明 hit-rect 覆盖整张图（不含 padding），捕获鼠标移到任意位置。
    // 这样从柱顶到柱底、柱间空隙都能触发提示，比绑到每个 bar 上手感更好。
    g.append("rect")
      .attr("class", "md-hit")
      .attr("x", 0).attr("y", 0).attr("width", iw).attr("height", ih)
      .attr("fill", "transparent")
      .on("mousemove mouseenter", function (evt) {
        const [mx] = d3.pointer(evt, this);
        // 找 mouse x 离哪个 band 中心最近 —— padding outer/inner 都被
        // x.scaleBand 自动处理，bandwidth 中点最近的最稳。
        const bw = x.bandwidth();
        let best = 0, bestDist = Infinity;
        for (let k = 0; k < n; k++) {
          const center = x(k) + bw / 2;
          const d = Math.abs(center - mx);
          if (d < bestDist) { bestDist = d; best = k; }
        }
        showTip(best, evt);
      })
      .on("mouseleave", hideTip);

    // 重渲染恢复：用户鼠标若仍在图区（mdHoverVisible=true），立即按
    // 上次的 idx 重画 tip —— tick 重渲染会重建 hit-rect，原 handler 失效，
    // 不补这一步的话 tip 会闪烁消失直到用户动鼠标。
    if (mdHoverVisible) showTip(mdLastHoverIdx, null);
  }

  // ---- v0.xxx 总览统计卡：统计页 4 张图以卡片形式出现在总览 grid。
  // 每张统计卡用固定默认档（不跟随统计页工具栏）：
  //   treemap    = 30d 按上游 relay
  //   pie        = 1d 按上游 relay
  //   agent      = 30d agent relay（renderStatsAgent 自带 30s TTL）
  //   model-daily= 30d（复用 modelDailyCache 的 30s TTL）
  // 数据节流：treemap/pie 走独立 30s TTL 缓存，避免 500ms poll tick 狂打桥。
  // 渲染节流：数据 key 未变时不重建 SVG（避免每 tick 全量 innerHTML）。
  const OVERVIEW_STATS_TTL = 30000;
  const overviewStatsCache = {};   // kind -> { data, ts }
  const overviewStatsInflight = {}; // kind -> Promise（防并发重复拉桥）

  async function renderOverviewStatsCard(body, kind) {
    if (!body) return;
    // body 就是 .card-body；数据未刷新时跳过重绘（D3 重建 SVG 成本高，
    // 500ms tick 每帧都重建会闪 + 卡顿）。只有 (a) 首次出现 或 (b) TTL
    // 过期拉到新数据 才重建。
    const now = Date.now();
    const cache = overviewStatsCache[kind];
    const fresh = cache && (now - cache.ts < OVERVIEW_STATS_TTL);
    if (fresh && body.dataset.statsRendered === String(cache.ts)) return;
    if (kind === "treemap" || kind === "pie") {
      let data = cache && cache.data;
      if (!fresh) {
        if (!overviewStatsInflight[kind]) {
          const dim = "upstream";
          const range = kind === "pie" ? "1d" : "30d";
          overviewStatsInflight[kind] = (async () => {
            const d = await api.statsAggregate(dim, range, 0, "relay");
            if (d && !d.error) overviewStatsCache[kind] = { data: d, ts: Date.now() };
            return d;
          })();
        }
        const d = await overviewStatsInflight[kind].catch(() => ({ error: "桥不可用" }));
        overviewStatsInflight[kind] = null;
        data = d && !d.error ? d : (overviewStatsCache[kind] && overviewStatsCache[kind].data);
        if (d && d.error) { statsError(body, d.error); return; }
      }
      if (!data || !data.rows || !data.rows.length) { statsEmpty(body, "暂无数据"); return; }
      if (kind === "treemap") renderTreemap(data, body);
      else renderPie(data, body);
      body.dataset.statsRendered = String(now);
      return;
    }
    if (kind === "agent") {
      // agent 数据自带 30s TTL；仅在数据刷新时重绘。
      const agFresh = agentStatsCache && (now - agentStatsFetchedAt < AGENT_STATS_TTL);
      if (agFresh && body.dataset.statsRendered === String(agentStatsFetchedAt)) return;
      if (!overviewStatsInflight["agent"]) {
        overviewStatsInflight["agent"] = (async () => {
          await renderStatsAgent(body);
        })();
      }
      await overviewStatsInflight["agent"].catch(() => {});
      overviewStatsInflight["agent"] = null;
      body.dataset.statsRendered = String(agentStatsFetchedAt);
      return;
    }
    if (kind === "model-daily") {
      const mdFresh = modelDailyCache && (now - modelDailyFetchedAt < MODEL_DAILY_TTL);
      if (mdFresh && body.dataset.statsRendered === String(modelDailyFetchedAt)) return;
      if (!mdFresh) {
        if (!overviewStatsInflight["model-daily"]) {
          overviewStatsInflight["model-daily"] = (async () => {
            const _md = await api.statsModelDaily(30);
            if (_md && !_md.error && _md.models) { modelDailyCache = _md; modelDailyFetchedAt = Date.now(); }
            return _md;
          })();
        }
        const _md = await overviewStatsInflight["model-daily"].catch(() => ({ error: "桥不可用" }));
        overviewStatsInflight["model-daily"] = null;
        if (_md && _md.error) { statsError(body, _md.error); return; }
      }
      if (!modelDailyCache || !modelDailyCache.models || !modelDailyCache.models.length) {
        statsEmpty(body, "暂无数据");
        return;
      }
      const card = body.closest(".glass-card");
      const chips = card ? card.querySelector("[data-md-chips]") : null;
      const sel = card ? card.querySelector("[data-md-sel]") : null;
      renderStatsModelDaily(body, chips, sel);
      body.dataset.statsRendered = String(modelDailyFetchedAt);
      return;
    }
  }

  // v0.102 "全部"档：并发拉 relay + passthrough 聚合，前端合并 rows/total。
  const _AGG_FIELDS = [
    "requests", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
    "errors", "total_tokens",
  ];
  function _mergeAggRow(target, src) {
    _AGG_FIELDS.forEach(f => { target[f] = (target[f] || 0) + (src[f] || 0); });
    return target;
  }
  async function statsAll(dim, range, top) {
    const [r, p] = await Promise.all([
      api.statsAggregate(dim, range, top, "relay"),
      api.statsAggregate(dim === "model" ? "model" : "upstream", range, top, "passthrough"),
    ]);
    if (r && r.error && p && p.error) return r;
    const relayRows = (r && r.rows) || [];
    const ptRows = (p && p.rows) || [];
    let rows;
    if (dim === "model") {
      const map = new Map();
      relayRows.forEach(x => _mergeAggRow(map.get(x.key) || map.set(x.key, { key: x.key }).get(x.key), x));
      ptRows.forEach(x => _mergeAggRow(map.get(x.key) || map.set(x.key, { key: x.key }).get(x.key), x));
      rows = [...map.values()];
    } else {
      rows = relayRows.map(x => ({ ...x }))
        .concat(ptRows.map(x => ({ ...x, key: "[透传] " + x.key })));
    }
    rows.sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0));
    const total = {};
    _AGG_FIELDS.forEach(f => {
      total[f] = (r && r.total ? r.total[f] : 0) + (p && p.total ? p.total[f] : 0);
    });
    return {
      range, dim, top,
      since: r && r.since != null ? r.since : (p ? p.since : null),
      total,
      rows,
    };
  }

  function renderAll(snap, status) {
    // 透传模式：snapshot 驱动，renderStatus 之前先写入 _ptMode，
    // 让顶部状态栏当拍就带上（透传模式）标记。
    if (snap) {
      window._ptMode = !!snap.passthrough_mode;
      if (document.body.classList.contains("passthrough-mode") !== !!snap.passthrough_mode) {
        document.body.classList.toggle("passthrough-mode", !!snap.passthrough_mode);
      }
      // v0.102：首次拿到 snapshot 时若无 localStorage 档位，按当前模式
      // 初始化三极开关（透传 → 仅透传；转换 → 仅转换）。
      if (!window._consumeMode) {
        window._consumeMode = window._ptMode ? "passthrough" : "relay";
        try { localStorage.setItem("consume-mode", window._consumeMode); } catch (e) {}
        mountConsumeSwitches();
      }
    }
    renderStatus(status);
    if (!snap) return;
    // Overview is the only view with the sig-based short-circuit — its
    // 6 cards don't change between every tick when the relay is idle,
    // so skipping the innerHTML round-trips is a real CPU win. The
    // other four views have data that updates per-request (live) or
    // per-second (history), so they always re-render — it's cheap and
    // makes the live panels feel truly live.
    if (currentView === "overview") {
      // v0.102：三极开关数据口径 —— relay=快照原样；passthrough=透传库
      // overview（24h 小时桶 + 区间聚合）；all=前端合并两源。透传档数据
      // 未就绪时显示"加载中"占位（**不再静默回退 relay 快照** —— 否则
      // 用户切档后看到的一直是上一档数据，会误以为开关没生效）。
      const mode = getConsumeMode();
      // 档位变化 → 丢弃旧透传缓存强制重拉（"仅透传/全部"两档各拉一次，
      // relay 档不动缓存）。
      if (mode !== "relay" && mode !== lastOverviewMode) {
        lastOverviewMode = mode;
        ptOverviewCache = null;
      }
      let ov = snap;
      let ptLoading = false;
      if (mode !== "relay") {
        const now = Date.now();
        if (!ptOverviewCache) {
          ptLoading = true;
          // 只在无在途请求、且（首次 / 失败后已过重试间隔）时才发请求，
          // 避免每 500ms tick 反复打后端。
          if (!ptOverviewPromise && (!ptFetchFailed || now - ptLastAttemptAt >= PT_RETRY_AFTER_FAIL)) {
            fetchPtOverview(true);
          }
        } else if (now - ptOverviewFetchedAt > PT_OVERVIEW_TTL) {
          fetchPtOverview(false);
        }
        if (ptOverviewCache) {
          ov = mode === "passthrough" ? ptOverviewToSnap(ptOverviewCache) : mergeSnap(snap, ptOverviewCache);
        }
      }
      // v0.45：by_upstream 的 sig 改成包含 counts 和 active_per_platform，
      // 否则 count 增长 / 激活态变化都不会触发 re-render，波纹和高亮
      // 就动不起来。counts 是一个 {window: n} map，JSON 体积很小。
      const upstreamSig = ov.by_upstream ? JSON.stringify(ov.by_upstream) : "";
      const activeSig = (mode === "relay" && status) ? JSON.stringify(status.active_per_platform || {}) : "";
      // v0.9：不再把 ``snap.ts|0`` 塞进 sig —— 它每秒翻一次，idle 时
      // 也强制 6 张卡 + 图表全量重渲染，注释声称的 "idle-tick
      // short-circuit" 实际只剩一半效果。卡片不依赖秒级时间戳。
      const sig = JSON.stringify([
        mode, ptLoading, ptFetchFailed,
        ov.by_platform && Object.keys(ov.by_platform).length,
        ov.by_model && Object.keys(ov.by_model).length,
        upstreamSig,
        activeSig,
        ov.recent && ov.recent.length,
        ov.recent && ov.recent[0] && ov.recent[0].id,
        ov.by_hour && ov.by_hour.length,
        ov.by_hour && ov.by_hour.length && ov.by_hour[ov.by_hour.length - 1].tokens,
      ]);
      if (sig !== lastCardsSig || cardResizingNow()) {
        lastCardsSig = sig;
        if (cardResizingNow()) {
          // 正在拖卡片尺寸：跳过本轮卡片 innerHTML 重建（SVG 会闪断、
          // 内容跟拖拽打架），等 pointerup 的 renderAll 统一刷新。统计
          // 卡的 TTL 缓存 key 会随 sig 推进，不影响最终一致性。
        } else if (ptLoading) {
          // 透传档数据未就绪：**不碰卡片内容**（保留上一档画面，不闪空），
          // 状态条已显示"加载透传数据…"。数据到达后 fetchPtOverview 会再次
          // 触发 renderAll，届时统一淡入新数据。图表实例也保留 —— 若此
          // 时销毁，数据到后重建 canvas 反而多一次闪。
        } else {
          // 切档（档位与上次渲染不同）时对新渲染的卡片内容做一次淡入，
          // 同一档位内的常规 tick 刷新不重播动画。
          const fade = mode !== lastRenderedMode;
          lastRenderedMode = mode;
          const markFade = (b) => {
            if (!fade) return;
            b.classList.remove("card-fade-in");
            void b.offsetWidth;
            b.classList.add("card-fade-in");
          };
          forEachCardBody("today",    b => { renderToday(b, ov);    markFade(b); });
          forEachCardBody("upstream", b => { renderUpstream(b, ov, mode === "relay" ? status : null); markFade(b); });
          forEachCardBody("platform", b => { renderPlatform(b, ov); markFade(b); });
          forEachCardBody("agent",    b => { renderAgent(b, ov);    markFade(b); });
          forEachCardBody("models",   b => { renderModels(b, ov);   markFade(b); });
          forEachCardBody("recent",   b => { renderRecent(b, ov);   markFade(b); });
          forEachCardBody("hourly",   b => { renderHourChart(b, ov); markFade(b); });
        }
      }
      // Spotlight：ptLoading 时保持上一档画面（不隐藏不闪空），数据到后
      // 直接更新数字/条（数字与条已做宽度自适应，变化是平滑的）。
      renderTopModelSpotlight(ov);
      // 总览统计卡：4 张统计图以卡片形式嵌入总览 grid。每个函数内部
      // 自带 TTL 缓存 + 数据 key 短路由 —— 500ms tick 重复调用是廉价的
      // no-op，只在首次出现 / TTL 过期拉到新数据时才重建 SVG。
      forEachCardBody("stats-treemap",     b => renderOverviewStatsCard(b, "treemap"));
      forEachCardBody("stats-pie",         b => renderOverviewStatsCard(b, "pie"));
      forEachCardBody("stats-agent",       b => renderOverviewStatsCard(b, "agent"));
      forEachCardBody("stats-model-daily", b => renderOverviewStatsCard(b, "model-daily"));
      // v0.100：进入总览时同步消费 pending 播入场动画（不等下个 tick）。
      if (overviewIntroPending) {
        overviewIntroPending = false;
        playOverviewIntro();
      }
    } else {
      renderActiveView(snap, status);
    }
  }

  // -------------------------------------------------------------------------
  // 24h token-consumption chart (chart.js bar, stacked input/output)
  //
  // v0.9：chart 实例只创建一次、之后 ``update('none')`` 原地刷新数据，
  // 不再每个 tick destroy + 重建（旧实现每次换 `<canvas>`、重建
  // ResizeObserver，2 Hz 轮询下是持续的 GC/重排压力 + 可见闪烁）。
  // 主题切换时 colors 变化，用 ``_hourChartTheme`` 标记强制重建一次。
  let _hourChartTheme = null;
  function _destroyHourChart() {
    if (_hourChart) {
      try { _hourChart.destroy(); } catch (_) {}
      _hourChart = null;
      _hourChartTheme = null;
    }
  }
  function renderHourChart(body, snap) {
    if (!body) return;
    if (typeof window.Chart === "undefined") {
      _destroyHourChart();
      // vendor/chart.umd.min.js failed to load — fall back to a plain
      // message so the user knows it's a build artefact problem, not a
      // data problem.
      body.innerHTML = '<div class="card-empty">图表库未加载（vendor/chart.umd.min.js）</div>';
      return;
    }
    const buckets = (snap && snap.by_hour) || [];
    if (!buckets.length) {
      // 有旧实例也要先 destroy —— 否则换成空态文案后旧 Chart 还挂在
      // window resize 监听上，每次转换都漏一个实例。
      _destroyHourChart();
      body.innerHTML = '<div class="card-empty">近 24h 暂无请求</div>';
      return;
    }
    // Zero-fill any missing hours between min and max so the X axis is a
    // contiguous 24h strip. Without this, a quiet relay would render a
    // sparse chart that looks broken (gaps in the bar run).
    const BUCKET = 3600;
    const minH = Math.floor(buckets[0].hour / BUCKET) * BUCKET;
    const maxH = Math.floor(buckets[buckets.length - 1].hour / BUCKET) * BUCKET;
    const byHour = new Map();
    buckets.forEach(b => byHour.set(Math.floor(b.hour / BUCKET) * BUCKET, b));
    const padded = [];
    for (let h = minH; h <= maxH; h += BUCKET) {
      padded.push(byHour.get(h) || { hour: h, requests: 0, in_tokens: 0, out_tokens: 0, tokens: 0 });
    }
    const labels = padded.map(b => {
      const d = new Date(b.hour * 1000);
      return d.getHours().toString().padStart(2, "0") + ":00";
    });
    const inData = padded.map(b => b.in_tokens);
    const outData = padded.map(b => b.out_tokens);
    // Token consumption scaled to whatever is the largest single bucket,
    // capped at a sensible minimum so the chart doesn't auto-zoom to the
    // top of a tiny series and hide relative differences.
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const themeKey = isDark ? "dark" : "light";
    const gridColor = isDark ? "rgba(255,255,255,.08)" : "rgba(0,0,0,.06)";
    const textColor = isDark ? "#aeaeb2" : "#6e6e73";

    // Reuse the live instance: update data + labels, no re-create.
    // 只在其 canvas 仍挂在 DOM 上时复用 —— 占位/视图切换可能已把 canvas
    // 覆盖移除，此时必须销毁重建，否则 update 画在脱离 DOM 的 canvas 上，
    // 图表看起来就像"消失"了。
    if (_hourChart && _hourChartTheme === themeKey
        && _hourChart.canvas && _hourChart.canvas.isConnected) {
      _hourChart.data.labels = labels;
      _hourChart.data.datasets[0].data = inData;
      _hourChart.data.datasets[1].data = outData;
      _hourChart.update("none");
      return;
    }
    // First render, or theme flipped — build a fresh chart (chart.js
    // needs a fresh <canvas> per Chart(); reusing the node works only if
    // the previous instance is .destroy()ed first).
    _destroyHourChart();
    body.innerHTML = '<canvas class="hour-chart-canvas"></canvas>';
    const canvas = body.querySelector(".hour-chart-canvas");
    if (!canvas) return;
    _hourChart = new window.Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "输入 tokens",
            data: inData,
            backgroundColor: "rgba(59, 130, 246, 0.75)",
            borderColor: "rgba(59, 130, 246, 1)",
            borderWidth: 0,
            stack: "tokens",
          },
          {
            label: "输出 tokens",
            data: outData,
            backgroundColor: "rgba(217, 119, 6, 0.75)",
            borderColor: "rgba(217, 119, 6, 1)",
            borderWidth: 0,
            stack: "tokens",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false, // 2 Hz polling — chart.js animations are noise here
        plugins: {
          legend: {
            position: "top",
            align: "end",
            labels: { color: textColor, font: { size: 11 }, boxWidth: 12, boxHeight: 12 },
          },
          tooltip: {
            mode: "index",
            intersect: false,
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${fmtTokens(ctx.parsed.y)}`,
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { color: gridColor, drawBorder: false },
            ticks: { color: textColor, font: { size: 10 }, maxRotation: 0 },
          },
          y: {
            stacked: true,
            grid: { color: gridColor, drawBorder: false },
            ticks: {
              color: textColor,
              font: { size: 10 },
              callback: (v) => fmtTokens(v),
            },
            beginAtZero: true,
          },
        },
      },
    });
    _hourChartTheme = themeKey;
  }

  // -------------------------------------------------------------------------
  // "Live" / "History" / "Upstreams" / "Settings" view renderers
  // -------------------------------------------------------------------------

  // live requests: request_id, started_at, platform, model, phase, bytes_received,
  // content_length, user_text_preview, assistant_text, age_sec
  // phase=streaming 但 age_sec（最后活跃后的空闲秒数）超过此值的视为孤儿。
  // renderLive（实时卡）与 updateLiveNavRipple（侧栏"实时"波纹）共用，
  // 保证"实时卡不显示 ⇔ 波纹不亮"同一语义。
  const STREAMING_STALE_SEC = 120;
  let lastLiveSig = null;
  function renderLive(body, snap) {
    if (!body) return;
    const rows = (snap && snap.live) || [];
    // "正在进行的请求"只显示已经离开上传阶段、往返上游的条目:
    //   - 保留 calling(请求已发出,等上游首个响应包)
    //   - 保留 streaming(SSE 字节正在回流)
    //   - 过滤 uploading(body 还没读完,不需要在这里展示)
    //   - 过滤 done(已完成,留给"最近请求"和"模型输出"历史)
    // 后端 `/live` 不动 —— TUI 还要用 uploading/calling 给"正在上传"面板、
    // streaming/done 给"模型输出"面板,集中过滤在 UI 层最稳。
    const inProgress = rows.filter(r => {
      const phase = (r.phase || "").toLowerCase();
      return phase === "calling" || phase === "streaming";
    });
    // 兜底：phase=streaming 但 age_sec 超过 STREAMING_STALE_SEC 的视为孤儿
    // 不渲染。后端 sweeper 30s 跑一次，理论上不会这么久；这层只在
    // 后端 sweeper 也失灵时给 UI 留个逃生口。calling 不设上限 — 等
    // 上游首个响应包可能要数秒。
    const live = inProgress.filter(r => {
      const phase = (r.phase || "").toLowerCase();
      if (phase !== "streaming") return true;
      return Number(r.age_sec || 0) < STREAMING_STALE_SEC;
    });
    // v0.40 性能修复：idle 状态（无进行中请求）是最常见状态，
    // 原本每 500ms innerHTML 重建一次 100% 等价的 HTML。
    // sig 只覆盖会真正改变显示的字段（id / phase / bytes_received），
    // 不含 age_sec ——age 数字的更新频率跟心跳对齐，下次 snapshot
    // 推送时自然会触发重建（snapshot 间隔 500ms，age 显示够流畅）。
    const sig = live.length
      ? live.map(r => r.id + ":" + r.phase + ":" + r.bytes_received).join(",")
      : "";
    if (sig === lastLiveSig) return;
    lastLiveSig = sig;
    if (!live.length) {
      body.innerHTML = '<div class="card-empty">暂无进行中的请求</div>';
      return;
    }
    body.innerHTML = live.map(r => {
      const age = Number(r.age_sec || 0);
      const ageText = age >= 60 ? `${(age / 60).toFixed(1)}m` : `${age.toFixed(1)}s`;
      const pct = r.content_length > 0
        ? Math.min(100, (r.bytes_received / r.content_length) * 100)
        : 0;
      const phase = (r.phase || "—").toLowerCase();
      const preview = (r.user_text_preview || "").slice(0, 80);
      return `
        <div class="live-row">
          <span class="live-phase live-phase-${escape(phase)}">${escape(phase)}</span>
          <div class="live-meta">
            <span class="live-meta-model" data-i18n-keep>${escape(r.model || "—")} · ${escape(r.platform || "—")}</span>
            <div class="live-progress"><div class="live-progress-fill" style="width:${pct.toFixed(1)}%"></div></div>
            <div class="live-preview">${escape(preview || "(空)")}</div>
          </div>
          <span class="live-age">${ageText}</span>
        </div>`;
    }).join("");
  }

  // -------------------------------------------------------------------
  // History view — paginated, infinite scroll, click-to-detail.
  //
  // v0.36: replaces the old "render all 20 rows from snap.recent at
  // once" path. Now:
  //   * First paint on view-enter fetches GET /requests?limit=100.
  //   * IntersectionObserver watches a sentinel row at the bottom of
  //     the list; when it scrolls into view, the next 100 are fetched
  //     via GET /requests?limit=100&before_id=<cursor>.
  //   * `before_id` uses the SQLite row id (autoincrement) as cursor,
  //     so the same-second multi-request case can't repeat or skip.
  //   * Click on any row opens the detail modal — see
  //     ``openRequestDetail`` below. The overview "最近活动" card also
  //     reuses the same modal via event delegation on its body.
  //   * State resets every time the user enters the view (see
  //     ``setView``), so the top of the list always reflects the
  //     newest requests instead of a stale snapshot.
  // -------------------------------------------------------------------

  let historyState = {
    items: [],
    cursor: null,        // smallest id seen so far; null = first page
    loading: false,      // in-flight guard for IntersectionObserver
    loaded: false,       // initial fetch has resolved at least once
    observer: null,      // IntersectionObserver for the bottom sentinel
    exhausted: false,    // server said next_before_id === null
  };

  function resetHistory() {
    historyState.items = [];
    historyState.cursor = null;
    historyState.loading = false;
    historyState.loaded = false;
    historyState.exhausted = false;
    if (historyState.observer) {
      historyState.observer.disconnect();
      historyState.observer = null;
    }
  }

  function renderHistoryTable(items) {
    const head = `
      <div class="history-row history-row-head">
        <span>时间</span><span>平台</span><span>模型</span>
        <span class="num">入</span><span class="num">出</span>
        <span>上游</span><span>状态</span>
      </div>`;
    if (!items.length) return head;
    return head + items.map(r => {
      const status = r.status_code || (r.error ? "ERR" : "—");
      const errCls = r.error ? "history-error" : "";
      return `
        <div class="history-row" data-id="${r.id}" tabindex="0" role="button" aria-label="查看请求详情">
          <span class="history-time">${fmtTime(r.ts)}</span>
          <span class="history-platform" data-i18n-keep>${escape(r.platform || "—")}</span>
          <span class="recent-model" data-i18n-keep>${escape(r.model || "—")}</span>
          <span class="history-tokens num">${fmtTokens(r.input_tokens || 0)}</span>
          <span class="history-tokens num">${fmtTokens(r.output_tokens || 0)}</span>
          <span class="history-upstream" data-i18n-keep>${escape(r.upstream || "—")}</span>
          <span class="${errCls}">${escape(String(status))}</span>
        </div>`;
    }).join("");
  }

  async function loadHistoryPage(body) {
    if (historyState.loading || historyState.exhausted) return;
    historyState.loading = true;
    // Paint the table head + sentinel immediately so the body is never
    // blank during the fetch. First load shows no rows; subsequent
    // loads keep the rows we've already collected.
    paintHistoryBody(body);
    const sentinel = body.querySelector("#history-sentinel");
    if (sentinel) sentinel.textContent = "加载中…";
    try {
      // Route through the pywebview bridge (`api.fetchRequests`) rather
      // than `fetch("/requests", window.location.origin)` — pywebview
      // loads index.html from a `file://` URI, so origin is "null" and
      // fetch() silently fails. The bridge stays the single point of
      // contact for the GUI regardless of how the page is served.
      const data = await api.fetchRequests(100, historyState.cursor);
      if (!data || data.error) {
        // 桥不可用 / 服务器错误 —— 这跟"真的翻到底"不是一回事，绝不
        // 能置 exhausted。sentinel 提示重试，IntersectionObserver 还在
        // 观察，用户继续滚动就会重试。
        if (sentinel) {
          sentinel.textContent = "加载失败（"
            + ((data && data.error) || "未知错误")
            + "），继续滚动重试";
        }
        return;
      }
      const items = (data && data.items) || [];
      historyState.items.push(...items);
      if (items.length) historyState.cursor = items[items.length - 1].id;
      if (data.next_before_id === null) historyState.exhausted = true;
      paintHistoryBody(body);
      wireHistoryObserver(body);
    } catch (e) {
      if (sentinel) sentinel.textContent = "加载失败: " + (e && e.message || e);
      console.error("[history] loadHistoryPage failed", e);
    } finally {
      historyState.loading = false;
    }
  }

  function paintHistoryBody(body) {
    if (!body) return;
    const tail = historyState.exhausted
      ? `<div class="history-sentinel history-sentinel-end" id="history-sentinel">已加载全部 ${historyState.items.length} 条</div>`
      : `<div class="history-sentinel" id="history-sentinel">↓ 滚动加载更多</div>`;
    body.innerHTML = renderHistoryTable(historyState.items) + tail;
  }

  function wireHistoryObserver(body) {
    if (!body) return;
    if (historyState.observer) historyState.observer.disconnect();
    const sentinel = body.querySelector("#history-sentinel");
    if (!sentinel) return;
    if (historyState.exhausted) return;          // no point watching an "end" sentinel
    // rootMargin 200px — start the fetch before the user reaches the
    // bottom, so the next page is in place by the time they scroll
    // there.
    historyState.observer = new IntersectionObserver((entries) => {
      if (entries.some(e => e.isIntersecting)) loadHistoryPage(body);
    }, { rootMargin: "200px" });
    historyState.observer.observe(sentinel);
  }

  // Recent requests: ts, platform, model, input_tokens, output_tokens, status_code, error.
  //
  // Drives the "历史" view. State is kept in `historyState` (above) so
  // polling ticks don't keep re-rendering and breaking scroll position.
  // The first call (after a `resetHistory()` from `setView`) kicks off
  // the initial 100-row page; subsequent polls in the same view pass
  // through without touching the DOM.
  function renderHistory(body, snap) {
    if (!body) return;
    if (!historyState.loaded) {
      historyState.loaded = true;
      // Fire-and-forget; UI updates as the fetch resolves.
      loadHistoryPage(body);
    }
    // Wire the row-click handler exactly once per body. Delegation so
    // newly-appended rows from loadHistoryPage get clicks for free.
    if (!body.dataset.clickWired) {
      body.dataset.clickWired = "1";
      body.addEventListener("click", (ev) => {
        const row = ev.target.closest(".history-row[data-id]");
        if (!row) return;
        openRequestDetail(parseInt(row.dataset.id, 10));
      });
      body.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        const row = ev.target.closest(".history-row[data-id]");
        if (!row) return;
        ev.preventDefault();
        openRequestDetail(parseInt(row.dataset.id, 10));
      });
    }
  }

  // -------------------------------------------------------------------
  // Request detail modal — opens when a history row or overview
  // "最近活动" row is clicked. Fetches GET /messages/by_request/{id},
  // which returns the request row plus all saved messages (user +
  // assistant text). If `RELAY_SAVE_MESSAGES=0` the messages list comes
  // back empty but the request row is still rendered (so the user can
  // at least see token counts and error info).
  // -------------------------------------------------------------------

  async function openRequestDetail(id) {
    const overlay = $("modal-overlay");
    const header = $("modal-header");
    const body = $("modal-body");
    if (!overlay || !header || !body || !id) return;
    // Show the shell immediately so the user sees the modal *open*;
    // populate contents as the fetch resolves. This avoids a perceived
    // lag on slow relays where /messages/by_request has to scan the
    // messages table.
    overlay.hidden = false;
    header.innerHTML = `
      <div class="modal-title">请求详情 <span class="modal-id">#${id}</span></div>
      <div class="modal-loading">加载中…</div>`;
    body.innerHTML = "";
    document.body.classList.add("modal-open");
    try {
      // Bridge path for the same reason `loadHistoryPage` uses it —
      // pywebview's `file://` origin means `fetch()` can't reach the
      // HTTP server even though uvicorn is up on 127.0.0.1:8088.
      const data = await api.fetchConversation(id);
      if (data && data.error === "not_found") {
        header.innerHTML = `
          <div class="modal-title">请求详情 <span class="modal-id">#${id}</span></div>
          <div class="modal-error">未找到该请求（可能已被清理）</div>`;
        return;
      }
      renderModalContent(header, body, data);
    } catch (e) {
      header.innerHTML = `
        <div class="modal-title">请求详情 <span class="modal-id">#${id}</span></div>
        <div class="modal-error">加载失败: ${escape(String(e && e.message || e))}</div>`;
    }
  }

  function closeModal() {
    const overlay = $("modal-overlay");
    if (!overlay) return;
    overlay.hidden = true;
    document.body.classList.remove("modal-open");
  }

  function renderModalContent(header, body, data) {
    const req = data && data.request ? data.request : {};
    const msgs = (data && data.messages) || [];
    const status = req.status_code || (req.error ? "ERR" : "—");
    const errCls = req.error ? "modal-meta-error" : "";
    header.innerHTML = `
      <div class="modal-title">请求详情 <span class="modal-id">#${req.id}</span></div>
      <div class="modal-meta">
        <span><b>时间</b> ${escape(fmtTime(req.ts))}</span>
        <span><b>平台</b> <span data-i18n-keep>${escape(req.platform || "—")}</span></span>
        <span><b>模型</b> <span data-i18n-keep>${escape(req.model || "—")}</span></span>
        <span><b>上游</b> <span data-i18n-keep>${escape(req.upstream || "—")}</span></span>
        <span><b>入</b> ${fmtTokens(req.input_tokens || 0)}</span>
        <span><b>出</b> ${fmtTokens(req.output_tokens || 0)}</span>
        <span><b>缓存读</b> ${fmtTokens(req.cache_read_input_tokens || 0)}</span>
        <span><b>缓存创</b> ${fmtTokens(req.cache_creation_input_tokens || 0)}</span>
        <span class="${errCls}"><b>状态</b> ${escape(String(status))}</span>
        ${req.error ? `<span class="modal-meta-error"><b>错误</b> ${escape(req.error)}</span>` : ""}
        ${req.request_id ? `<span class="modal-meta-mono"><b>request_id</b> ${escape(req.request_id)}</span>` : ""}
      </div>`;
    if (!msgs.length) {
      body.innerHTML = `<div class="modal-empty">未保存对话内容。<br><br>如需保存消息原文与回复，请把 <code>RELAY_SAVE_MESSAGES=1</code> 写入 .env 后重启。</div>`;
      return;
    }
    body.innerHTML = msgs.map((m, i) => {
      const role = String(m.role || "msg").toLowerCase();
      // v0.89: thinking 单独一类，便于样式区分（推理块通常比正文长，
      // 显示成折叠样式让对话详情重点仍在正文）。
      const roleCls = role === "user" ? "modal-msg-user"
        : role === "assistant" ? "modal-msg-assistant"
        : role === "thinking" ? "modal-msg-thinking"
        : "modal-msg-other";
      const text = m.content || "";
      const json = m.content_json;
      let jsonBlock = "";
      if (json && json !== text) {
        let pretty = json;
        try { pretty = JSON.stringify(JSON.parse(json), null, 2); } catch (e) { pretty = json; }
        jsonBlock = `
          <details class="modal-msg-json">
            <summary>原始 JSON（content_json）</summary>
            <pre>${escape(pretty)}</pre>
          </details>`;
      }
      return `
        <div class="modal-msg ${roleCls}">
          <div class="modal-msg-head">
            <span class="modal-msg-role">${escape(role)}</span>
            <span class="modal-msg-ts">${escape(fmtTime(m.ts || 0))}</span>
          </div>
          <div class="modal-msg-text">${escape(text)}</div>
          ${jsonBlock}
        </div>`;
    }).join("");
  }

  // Wire the modal close affordances exactly once at boot.
  // - X button (id="modal-close")
  // - Click on the dimmed overlay outside the card
  // - Escape key while the modal is open
  function wireModalClose() {
    const overlay = $("modal-overlay");
    const closeBtn = $("modal-close");
    if (!overlay || overlay.dataset.wired) return;
    overlay.dataset.wired = "1";
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    overlay.addEventListener("click", (ev) => {
      // The overlay itself receives the click; the inner card stops
      // propagation via its own listener, so any click that reaches
      // the overlay is "outside the card" and should close.
      if (ev.target === overlay) closeModal();
    });
    // Inner card stops propagation so clicks inside don't bubble up
    // to the overlay and trigger close.
    const card = overlay.querySelector(".modal-card");
    if (card) card.addEventListener("click", (ev) => ev.stopPropagation());
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !overlay.hidden) closeModal();
    });
  }

  // -------------------------------------------------------------------
  // v0.68：上游状态卡片 → 按模型拆分详情 modal
  //
  // 取代原来"点一行跳设置页"（focusUpstreamConfig）的行为——用户明确要
  // 求点击不再跳转设置，改成弹一个小窗口显示该上游内部每个模型的调用
  // 情况。数据同步从 lastSnap.by_upstream_model 里取（gui.py 已经把它
  // 塞进 snapshot），不需要 bridge round-trip，所以不像 openRequestDetail
  // 那样是 async。
  // -------------------------------------------------------------------

  function openUpstreamModels(name) {
    const overlay = $("upstream-models-overlay");
    const title = $("upstream-models-title");
    const body = $("upstream-models-body");
    if (!overlay || !title || !body || !name) return;
    const snap = lastSnap || {};
    const isVirt = name.indexOf("<->") >= 0;
    // 找 name 所属虚拟组（link_resolver 把 name 拼成 "A <-> B <-> C"，
    // 顺序为字典序——前端 reverse 一次解析回成员列表）。
    let members = null;
    if (isVirt) {
      const groups = snap.link_groups || [];
      for (const g of groups) {
        if (Array.isArray(g) && g.length >= 2 && g.slice().sort().join(" <-> ") === name) {
          members = g;
          break;
        }
      }
    }
    // 标题：虚拟 cfg 加「链接上游」标识，普通 cfg 走旧文案。
    title.textContent = isVirt ? `按模型拆分（链接上游）：${name}` : `按模型拆分：${name}`;
    if (isVirt && members) {
      body.innerHTML = renderLinkedUpstreamHTML(name, members, snap);
    } else {
      body.innerHTML = renderPlainUpstreamHTML(name, snap);
    }
    overlay.hidden = false;
    document.body.classList.add("modal-open");
  }

  // 虚拟 cfg 的 modal 内容：顶部「属于上游」+ 聚合统计 + 按成员分块模型表。
  function renderLinkedUpstreamHTML(name, members, snap) {
    const upData = (snap.by_upstream && snap.by_upstream[name]) || {};
    const counts = upData.counts || {};
    const aggHtml = renderAggregateHTML(upData, counts);
    const linkHeader = `
      <div class="upstream-link-header">
        <span class="upstream-link-label">${t("属于上游")}</span>
        <span class="cfg-chip cfg-linked-chip">
          <span class="cfg-chip-link-icon" aria-hidden="true">🔗</span>
          ${escape(name)}
        </span>
      </div>`;
    const membersHtml = members.map((memberName) => {
      const mUp = (snap.by_upstream && snap.by_upstream[memberName]) || {};
      const mCounts = mUp.counts || {};
      const breakdown = (snap.by_upstream_model && snap.by_upstream_model[memberName]) || {};
      const entries = Object.entries(breakdown);
      const subStats = `${t("调用")} ${fmtNum(mCounts.month || 0)} · ${t("token")} ${fmtTokens(mUp.total_tokens || 0)}`;
      let tableHtml;
      if (!entries.length) {
        tableHtml = `<div class="card-empty">${t("该成员暂无调用记录")}</div>`;
      } else {
        // 按请求数倒序 — 最活跃的模型排前。
        entries.sort((a, b) => (b[1].requests || 0) - (a[1].requests || 0));
        const rows = entries.map(([model, d]) => renderModelRow(model, d)).join("");
        tableHtml = `
          <table class="upstream-models-table upstream-link-member-table">
            <thead>
              <tr><th>${t("模型")}</th><th>${t("请求")}</th><th>${t("错误")}</th><th>${t("累计 tokens")}</th><th>${t("加权 cost")}</th></tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>`;
      }
      return `
        <div class="upstream-link-member">
          <div class="upstream-link-member-head">
            <span class="cfg-chip cfg-linked-chip">
              <span class="cfg-chip-link-icon" aria-hidden="true">🔗</span>
              ${escape(memberName)}
            </span>
            <span class="upstream-link-member-stats">${subStats}</span>
          </div>
          ${tableHtml}
        </div>`;
    }).join("");
    return `${linkHeader}${aggHtml}<div class="upstream-link-members">${membersHtml}</div>`;
  }

  // 聚合统计行（counts 三档窗口 + total_tokens）。所有上游都可用，挂在
  // 「属于上游」下方。虚拟 cfg 显示完整聚合，普通 cfg 不显示。
  function renderAggregateHTML(upData, counts) {
    const tot = upData.total_tokens || 0;
    return `
      <div class="upstream-link-aggregate">
        <span class="upstream-link-stat">${t("调用 5h")} <b>${fmtNum(counts["5h"] || 0)}</b></span>
        <span class="upstream-link-stat">${t("调用 周")} <b>${fmtNum(counts.week || 0)}</b></span>
        <span class="upstream-link-stat">${t("调用 月")} <b>${fmtNum(counts.month || 0)}</b></span>
        <span class="upstream-link-stat">${t("累计 token")} <b>${fmtTokens(tot)}</b></span>
      </div>`;
  }

  // 普通 cfg：保留 v0.68 旧表格（无「属于上游」/ 聚合行）。
  function renderPlainUpstreamHTML(name, snap) {
    const breakdown = (snap.by_upstream_model && snap.by_upstream_model[name]) || {};
    const entries = Object.entries(breakdown);
    if (!entries.length) {
      return '<div class="card-empty">该上游暂无调用记录</div>';
    }
    entries.sort((a, b) => (b[1].requests || 0) - (a[1].requests || 0));
    const rows = entries.map(([model, d]) => renderModelRow(model, d)).join("");
    return `
      <table class="upstream-models-table">
        <thead>
          <tr><th>模型</th><th>请求</th><th>错误</th><th>累计 tokens</th><th>加权 cost</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // 单行模型数据 → <tr> HTML（虚拟 / 普通两路径共用，避免双份维护）。
  function renderModelRow(model, d) {
    const tokens = (d.input_tokens || 0) + (d.output_tokens || 0)
                 + (d.cache_read_input_tokens || 0)
                 + (d.cache_creation_input_tokens || 0);
    const costText = d.cost_unit === "tokens"
      ? fmtTokens(d.weighted_cost || 0)
      : fmtNum(d.weighted_cost || 0);
    return `<tr>
      <td>${escape(model)}</td>
      <td>${fmtNum(d.requests || 0)}</td>
      <td>${fmtNum(d.errors || 0)}</td>
      <td>${fmtTokens(tokens)}</td>
      <td>${costText} ${escape(d.cost_unit || "")}</td>
    </tr>`;
  }

  function closeUpstreamModelsModal() {
    const overlay = $("upstream-models-overlay");
    if (!overlay) return;
    overlay.hidden = true;
    document.body.classList.remove("modal-open");
  }

  // Wired once at boot — mirrors wireModalClose()'s X / backdrop / Esc
  // pattern for the request-detail modal, just pointed at the new overlay.
  function wireUpstreamModelsModalClose() {
    const overlay = $("upstream-models-overlay");
    const closeBtn = $("upstream-models-close");
    if (!overlay || overlay.dataset.wired) return;
    overlay.dataset.wired = "1";
    if (closeBtn) closeBtn.addEventListener("click", closeUpstreamModelsModal);
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) closeUpstreamModelsModal();
    });
    const card = overlay.querySelector(".modal-card");
    if (card) card.addEventListener("click", (ev) => ev.stopPropagation());
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !overlay.hidden) closeUpstreamModelsModal();
    });
  }

  // -------------------------------------------------------------------
  // v0.71：自定义 confirmModal / alertModal。
  //
  // 浏览器原生 confirm() / alert() 的视觉（白色小框、白底黑字）跟
  // 我们的玻璃拟态风格完全脱节，user 反馈"删除时顶上弹出的确认窗口
  // 不和谐"。这里动态创建 .modal-overlay / .modal-card 的小型对话框，
  // Promise 接口替换原生 confirm() / alert()。inner card 用
  // stopPropagation 拦冒泡，避免点 body 时把 overlay 自己关掉。
  // -------------------------------------------------------------------

  // 自定义事件序列号 —— 同一时刻只允许一个对话框；新对话框创建时
  // 把旧的关掉，避免连续触发时多个 modal 堆叠。
  let _dialogSeq = 0;
  function _closeDialog(overlay) {
    if (!overlay) return;
    overlay.hidden = true;
    document.body.classList.remove("modal-open");
    // Esc 监听是文档级的全局监听，关闭时移除防止泄漏。
    if (overlay._escHandler) {
      document.removeEventListener("keydown", overlay._escHandler);
      overlay._escHandler = null;
    }
    // 关闭后清理 DOM，避免长期累积。
    setTimeout(() => {
      if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }, 0);
  }

  function alertModal(title, body, opts) {
    return new Promise((resolve) => {
      // 关掉上一个未关的对话框
      const prev = document.querySelector(".modal-overlay[data-dynamic-dialog]");
      if (prev) _closeDialog(prev);
      const seq = ++_dialogSeq;
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.setAttribute("data-dynamic-dialog", String(seq));
      overlay.hidden = false;
      overlay.innerHTML = `
        <div class="modal-card modal-card-dialog" role="dialog" aria-modal="true">
          <button class="modal-close" aria-label="关闭" type="button">×</button>
          <div class="modal-header">
            <div class="modal-title">${escape(title || "")}</div>
          </div>
          <div class="modal-body dialog-body">${escape(body || "")}</div>
          <div class="dialog-actions">
            <button type="button" class="btn btn-primary" data-dialog-ok>确定</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      document.body.classList.add("modal-open");
      const close = () => _closeDialog(overlay);
      const card = overlay.querySelector(".modal-card");
      // inner card 拦冒泡 —— 点 OK 按钮时不触发 overlay 背景关闭
      if (card) card.addEventListener("click", (e) => e.stopPropagation());
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
      });
      overlay.querySelector(".modal-close").addEventListener("click", close);
      overlay.querySelector("[data-dialog-ok]").addEventListener("click", () => { close(); resolve(true); });
      // Esc 关闭
      const escHandler = (ev) => {
        if (ev.key === "Escape" && !overlay.hidden) { close(); resolve(true); }
      };
      document.addEventListener("keydown", escHandler);
      overlay._escHandler = escHandler;
    });
  }

  function confirmModal(title, body, opts) {
    // v0.75：opts 兼容 {ok, cancel} 和 {okText, cancelText} 两种命名,
    // 之前只认 okText,__qsDel 传 ok 时按钮文案就退化成默认的"确定"。
    const okText = (opts && (opts.okText || opts.ok)) || "确定";
    const cancelText = (opts && (opts.cancelText || opts.cancel)) || "取消";
    const danger = !!(opts && opts.danger);  // 红按钮
    return new Promise((resolve) => {
      const prev = document.querySelector(".modal-overlay[data-dynamic-dialog]");
      if (prev) _closeDialog(prev);
      const seq = ++_dialogSeq;
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.setAttribute("data-dynamic-dialog", String(seq));
      overlay.hidden = false;
      overlay.innerHTML = `
        <div class="modal-card modal-card-dialog" role="dialog" aria-modal="true">
          <button class="modal-close" aria-label="关闭" type="button">×</button>
          <div class="modal-header">
            <div class="modal-title">${escape(title || "")}</div>
          </div>
          <div class="modal-body dialog-body">${escape(body || "")}</div>
          <div class="dialog-actions">
            <button type="button" class="btn" data-dialog-cancel>${escape(cancelText)}</button>
            <button type="button" class="btn ${danger ? "btn-danger" : "btn-primary"}" data-dialog-ok>${escape(okText)}</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      document.body.classList.add("modal-open");
      const close = (result) => { _closeDialog(overlay); resolve(result); };
      const card = overlay.querySelector(".modal-card");
      if (card) card.addEventListener("click", (e) => e.stopPropagation());
      overlay.addEventListener("click", (e) => { if (e.target === overlay) close(false); });
      overlay.querySelector(".modal-close").addEventListener("click", () => close(false));
      overlay.querySelector("[data-dialog-cancel]").addEventListener("click", () => close(false));
      overlay.querySelector("[data-dialog-ok]").addEventListener("click", () => close(true));
      const escHandler = (ev) => {
        if (ev.key === "Escape" && !overlay.hidden) close(false);
      };
      document.addEventListener("keydown", escHandler);
      overlay._escHandler = escHandler;
    });
  }

  // v0.70：快捷切换编辑器 modal 的 × / Esc / 背景点击关闭。
  // 模板照 wireUpstreamModelsModalClose():X 按钮 + overlay 背景 click +
  // Esc 三种关法。inner card 用 stopPropagation 拦点击冒泡,避免
  // 点 input / 按钮时把外层 overlay 触发关掉。
  function wireQuickSwitchEditorModalClose() {
    const overlay = $("qs-editor-overlay");
    const closeBtn = $("qs-editor-close");
    if (!overlay || overlay.dataset.wired) return;
    overlay.dataset.wired = "1";
    const close = () => {
      overlay.hidden = true;
      document.body.classList.remove("modal-open");
    };
    if (closeBtn) closeBtn.addEventListener("click", close);
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) close();
    });
    const card = overlay.querySelector(".modal-card");
    if (card) card.addEventListener("click", (ev) => ev.stopPropagation());
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !overlay.hidden) close();
    });
  }

  // Overview's "最近活动" card also needs to open the modal on click.
  // Each row carries data-id (same source of truth as history rows —
  // the SQLite requests.id), so the same openRequestDetail call works
  // for both views. Wire once at boot via the body of the card.
  function wireOverviewRecentClicks() {
    const body = $("card-recent-body");
    if (!body || body.dataset.clickWired) return;
    body.dataset.clickWired = "1";
    body.addEventListener("click", (ev) => {
      const row = ev.target.closest(".recent-row[data-id]");
      if (!row) return;
      openRequestDetail(parseInt(row.dataset.id, 10));
    });
  }

  /** Quota block for one upstream — summary line, utilization bar,
   *  per-model weighted cost, and any unauthorized models. Returns ""
   *  when the upstream has no quota configured, so callers can
   *  unconditionally interpolate it. */
  function quotaBlock(data) {
    const level = data.warning_level || "no_quota";
    const parts = [];

    if (level !== "no_quota" && data.quota_5h) {
      // Bar caps at 100% width even when the weighted cost overshoots
      // the quota — past 100% the level colour carries the signal.
      const util = Number(data.utilization_5h || 0);
      const pct = Math.min(100, Math.max(0, util * 100));
      // v0.66: show "次" or "tokens" unit on the quota summary
      const unit = data.billing_unit === "token" ? t("tokens") : t("次");
      parts.push(`
        <div class="quota-summary">
          <span class="quota-summary-used">${fmtNum(Math.round(data.used_5h || 0))} ${unit} / ${fmtNum(data.quota_5h)}</span>
          <span class="warning-pill" data-level="${escape(level)}">${escape(data.warning_text || "—")}</span>
          <span class="quota-summary-eta">${util >= 1 ? t("额度已用尽") : `${t("预计")} ${escape(data.exhaustion_eta_text || "—")} ${t("用尽")}`}</span>
        </div>
        <div class="quota-bar">
          <div class="quota-bar-fill" data-level="${escape(level)}" style="width:${pct.toFixed(1)}%"></div>
        </div>`);

      const breakdown = data.model_breakdown || {};
      const chips = Object.entries(breakdown)
        .sort((a, b) => b[1] - a[1])
        .map(([m, cost]) =>
          `<span class="quota-breakdown-chip">${escape(m)} <b>${fmtNum(Math.round(cost))} ${unit}</b></span>`
        ).join("");
      if (chips) parts.push(`<div class="quota-breakdown">${chips}</div>`);
    }

    const unauthorized = data.unauthorized_seen || [];
    if (unauthorized.length) {
      parts.push(
        `<div class="quota-unauthorized">⚠ ${t("非允许模型")}: ${escape(unauthorized.join("、"))}</div>`
      );
    }
    return parts.join("");
  }

  let lastUpstreamsSig = null;
  // v0.90：自动切换 toast —— 每次 renderUpstreamsView 检测 active_per_platform
  // 的变化，给"被切走的旧 active"贴一条 toast，3 秒后淡出。toastMap 用
  // {上游名 → {to: 新上游, ts: 加入时刻}} 维护；3 秒后无论 sig 变不变都
  // 自然淡出，下一次 render 自然消失。
  const autoswitchToasts = new Map();
  let lastActivePerPlatform = {};

  // v0.90：阈值常量（写死）。与后端 quota_monitor 默认 relay_quota_switch_at
  // = 0.7 同步（config.py），文案切换时机与真实切换时机一致。设置页不暴露
  // 改阈值的 UI —— 用户要改只能去 .env。
  const AUTOSWITCH_HINT_AT = 0.7;

  /** 前端镜像 quota_monitor.pick_replacement：选 utilization 最低且 < 阈值的
   *  兄弟。pool 为空 = 全部兄弟；非空 = 仅池内。active 自身不参与。 */
  function pickReplacement(byUp, candidates, current, threshold) {
    let best = null, bestUtil = Infinity;
    for (const name of candidates) {
      if (name === current) continue;
      const row = byUp[name];
      if (!row) continue;
      const util = row.utilization_5h == null ? 0 : Number(row.utilization_5h);
      if (util >= threshold) continue;
      if (util < bestUtil) { best = name; bestUtil = util; }
    }
    return best;
  }

  /** 前端 hint 用："如果现在耗尽会切到谁"。**不卡阈值** —— 哪怕兄弟
   *  利用率已 ≥ 阈值也返回，让用户看到确切去向。pool 为空 = 全部兄弟；
   *  非空 = 仅池内；active 自身不参与。无候选返回 null。 */
  function pickHintTarget(byUp, candidates, current) {
    let best = null, bestUtil = Infinity;
    for (const name of candidates) {
      if (name === current) continue;
      const row = byUp[name];
      if (!row) continue;
      const util = row.utilization_5h == null ? 0 : Number(row.utilization_5h);
      if (util < bestUtil) { best = name; bestUtil = util; }
    }
    return best;
  }

  function renderUpstreamsView(body, snap, status) {
    if (!body) return;
    // v0.40 性能修复：upstreams 视图除了 5h_release_text（每 5h 边界跳一次）
    // 和 active 切换外基本不变。整个 cfg+byUp+active 做 JSON 化做 sig，
    // 命中即跳过整段 innerHTML 拼装。配置变更时 sig 自然变化。
    // v0.90：sig 扩展加 autoswitch 三字段 —— 设置页改阈值/池子时上
    // 游页也跟着重渲。
    const byUp = (snap && snap.by_upstream) || {};
    const sig = JSON.stringify([
      byUp,
      (snap && snap.upstreams) || {},
      (status && status.active_per_platform) || {},
      {
        enabled: !!(status && status.autoswitch_enabled),
        pool: (status && status.autoswitch_pool) || [],
        at: (status && status.autoswitch_at) || 0.9,
      },
    ]);
    if (sig === lastUpstreamsSig) return;
    lastUpstreamsSig = sig;
    const cfg = (snap && snap.upstreams) || {};
    const active = (status && status.active_per_platform) || {};
    const switchEnabled = !!(status && status.autoswitch_enabled);
    const switchPool = (status && status.autoswitch_pool) || [];

    // v0.90：检测 active_per_platform 变化 → 给被切走的旧 active 贴 toast。
    // tick 抖动防御：若 prev 与 prev_prev 一致才认为切换是稳定的。
    const curActive = active || {};
    const switchedFrom = {};
    for (const plat of Object.keys(curActive)) {
      const cur = curActive[plat], prev = lastActivePerPlatform[plat];
      if (prev && cur && prev !== cur) {
        switchedFrom[prev] = cur;
      }
    }
    lastActivePerPlatform = { ...curActive };
    const now = Date.now();
    for (const [fromName, toName] of Object.entries(switchedFrom)) {
      autoswitchToasts.set(fromName, { to: toName, ts: now });
      // 3 秒后清掉 —— 无论 sig 变不变都自然淡出
      setTimeout(() => {
        const cur = autoswitchToasts.get(fromName);
        if (cur && cur.to === toName) {
          autoswitchToasts.delete(fromName);
          // 强制重渲染：sig 没变也能更新；用临时 sig 让命中跳过失效。
          lastUpstreamsSig = null;
          if (typeof lastSnap !== "undefined" && lastSnap) renderUpstreamsView(body, lastSnap, lastStatus);
        }
      }, 3000);
    }

    // 平台 → 兄弟名 映射（pickReplacement 需要同平台兄弟）
    const siblingsByPlat = {};
    pooledUpstreams(cfg).forEach(({ plat, cfg: c }) => {
      (siblingsByPlat[plat] = siblingsByPlat[plat] || []).push(c.name);
    });

    const rows = [];
    pooledUpstreams(cfg).forEach(({ plat, cfg: c }) => {
        const data = byUp[c.name] || { counts: {}, "5h_release_text": "—" };
        const counts = data.counts || {};
        const isActive = active[plat] === c.name;
        const relText = data["5h_release_text"] || "—";
        const near = relText === "now" || relText === "<1m";
        const util = Number(data.utilization_5h || 0);
        // v0.64：active 上游不允许删 —— Python 端 remove_upstream
        // 会主动清掉 active 指针，但 GUI 这一层先把按钮禁掉，给用
        // 户一个明确的反馈，省一次往返。
        const removeBtn = isActive
          ? '<span class="upstream-detail-remove-disabled" title="当前激活的上游不能直接删除，请先切换 active">×</span>'
          : `<button class="upstream-detail-remove" type="button"
                   title="${t("删除这条上游配置")}"
                   data-platform="${attr(plat)}" data-upstream="${attr(c.name)}">×</button>`;

        // v0.90：自动切换提示 —— 仅 active 上游行
        // 文案随利用率联动（紧急度）：
        //   util >= 0.7  → "额度即将耗尽，将切换到 xx"（剩余 <30%）
        //   util <  0.7  → "耗尽后将切换到 xx"（剩余 30%~，平静预告）
        // 候选选择用 pickHintTarget（**不卡阈值**）—— 哪怕兄弟利用率
        // 已 ≥ 阈值也返回，让用户看到确切去向。后端真切换仍按阈值过
        // 滤（避免切过去立即又触发）；前端 hint 是"显示"不是"切换"。
        // 无候选 = 池内真的没有任何兄弟 → 显示警告。
        // 阈值常量 AUTOSWITCH_HINT_AT = 0.7 与后端 quota_monitor 默认
        // relay_quota_switch_at 同步（config.py），仅用于决定文案紧急度
        // 与真实切换时机一致。设置页不暴露改阈值的 UI。
        let autoswitchHtml = "";
        if (isActive) {
          autoswitchHtml += `<div class="autoswitch-current">当前使用：<b>${escape(c.name)}</b></div>`;
          if (switchEnabled) {
            const siblings = siblingsByPlat[plat] || [];
            const candidates = switchPool.length
              ? siblings.filter(n => switchPool.includes(n))
              : siblings;
            const target = pickHintTarget(byUp, candidates, c.name);
            if (target) {
              const label = util >= AUTOSWITCH_HINT_AT
                ? "额度即将耗尽，将切换到"
                : "耗尽后将切换到";
              // data-level 与 quota-bar-fill 同步，前端 CSS 用同样的
              // --quota-ok/warn/critical/exhausted 变量，hint 颜色跟 quota
              // 进度条完全一致。level 来自 by_upstream[name].warning_level
              // （tui.py:_classify_warning），不要在前端重新算阈值。
              const level = (data && data.warning_level) || "ok";
              autoswitchHtml += `<div class="autoswitch-hint" data-level="${escape(level)}">${label}<b>${escape(target)}</b></div>`;
            } else {
              // 不论利用率高低，没候选就告警 —— 用户应知道"池子空了"。
              autoswitchHtml += `<div class="autoswitch-hint autoswitch-hint-warn">⚠ 池内无可切换目标，耗尽后保留</div>`;
            }
          }
        }
        const toast = autoswitchToasts.get(c.name);
        const toastHtml = toast
          ? `<div class="autoswitch-toast">已切换到 <b>${escape(toast.to)}</b></div>`
          : "";

        rows.push(`
        <div class="upstream-detail-row upstream-detail-row-card upstream-row-clickable ${isActive ? 'upstream-detail-row-active' : ''}"
             data-upstream="${attr(c.name)}" data-platform="${attr(plat)}"
             title="${t("点击激活此上游；已是当前上游时点击进入编辑")}">
          ${removeBtn}
          ${toastHtml}
          <div class="upstream-detail-name">
            ${isActive ? '<span class="upstream-active-dot" title="当前使用"></span>' : ""}
            <span data-i18n-keep>${escape(c.name)}</span>
            ${isActive ? '<span class="active-tag">ACTIVE</span>' : ""}
            <span class="upstream-detail-platform" data-i18n-keep>${escape(wireGroup(c, plat))}</span>
          </div>
          <div class="upstream-detail-url">${escape(c.url || "—")}</div>
          <div class="upstream-detail-meta">
            <span>${t("5h")}: <b>${fmtNum(counts["5h"] || 0)}</b></span>
            <span>${t("周")}: <b>${fmtNum(counts.week || 0)}</b></span>
            <span>${t("月")}: <b>${fmtNum(counts.month || 0)}</b></span>
            <span class="${near ? 'upstream-release-near' : ''}"><span class="upstream-release-label" data-i18n="5h 释放">${t("5h 释放")}</span> <span class="upstream-release-value">${escape(relText)}</span></span>
          </div>
          ${c.note ? `<div class="upstream-detail-note">${escape(c.note)}</div>` : ""}
          ${autoswitchHtml}
          ${quotaBlock(data)}
        </div>`);
    });
    body.innerHTML = rows.length
      ? rows.join("")
      : '<div class="card-empty">暂无上游配置</div>';
  }

  let lastRelaySig = null;
  function renderSettingsRelay(body, snap, status) {
    if (!body) return;
    // v0.40 性能修复：settings-relay 视图只显示 relay 状态字段
    // （running / pid / port / owner），这些基本不变。sig 不含
    // snap.ts —— 时间戳会在下一次 sig 变化时（基本不变化）一起更新，
    // 用户不会察觉。
    if (!status) {
      if (lastRelaySig !== "__waiting__") {
        lastRelaySig = "__waiting__";
        body.innerHTML = '<div class="card-empty">等待 bridge 响应…</div>';
      }
      return;
    }
    const sig = JSON.stringify([status.running, status.pid, status.port, status.owner]);
    if (sig === lastRelaySig) return;
    lastRelaySig = sig;
    const external = status.owner === "external";
    const tsText = snap ? new Date((snap.ts || 0) * 1000).toLocaleTimeString("zh-CN") : "—";
    // #15：中继状态更紧凑 —— 纯展示信息。
    const wrap = (rows) => `<div class="settings-rows-compact">${rows}</div>`;
    body.innerHTML = wrap(`
      <div class="settings-row"><span class="settings-row-label">运行状态</span><span class="settings-row-value">${status.running ? "运行中" : "已停止"}</span></div>
      <div class="settings-row"><span class="settings-row-label">进程 PID</span><span class="settings-row-value">${status.pid || "—"}</span></div>
      <div class="settings-row"><span class="settings-row-label">监听端口</span><span class="settings-row-value">${status.port}</span></div>
      <div class="settings-row"><span class="settings-row-label">启动来源</span><span class="settings-row-value">${external ? "外部进程" : "本窗口"}</span></div>
      <div class="settings-row"><span class="settings-row-label">日志目录</span><span class="settings-row-value"><code>.relay-logs/</code></span></div>
      <div class="settings-row"><span class="settings-row-label">数据快照</span><span class="settings-row-value">${tsText}</span></div>
      <div class="settings-row"><span class="settings-row-label">额度查询</span><span class="settings-row-value">
        <button class="btn btn-ghost btn-xs" id="btn-quota-query" type="button">查询额度</button>
        <span class="quota-result" id="quota-result">—</span>
      </span></div>
    `);
    const qBtn = body.querySelector("#btn-quota-query");
    if (qBtn) {
      qBtn.addEventListener("click", async () => {
        const out = body.querySelector("#quota-result");
        if (!out) return;
        out.textContent = "查询中…";
        try {
          const res = (window.pywebview && window.pywebview.api && api.getQuota) ? await api.getQuota(null) : null;
          if (!res || res.error) { out.textContent = (res && res.error) || "不可用"; return; }
          const parts = [`${res.provider}`];
          if (res.rolling) parts.push(`滚动 ${res.rolling.usage}% (${res.rolling.reset})`);
          if (res.weekly) parts.push(`周 ${res.weekly.usage}% (${res.weekly.reset})`);
          if (res.monthly) parts.push(`月 ${res.monthly.usage}% (${res.monthly.reset})`);
          if (res.balance) parts.push(`余额 ${res.balance.text}`);
          out.textContent = parts.join(" · ") || "无数据";
        } catch (_) { out.textContent = "失败"; }
      });
    }
    // #14：电脑当前状态（磁盘 / 内存 / CPU / 网络）。异步填充，不阻塞。
    const sysBody = body;
    (async () => {
      try {
        const si = (window.pywebview && window.pywebview.api && api.getSysinfo) ? await api.getSysinfo() : null;
        if (!si) return;
        const rows = [
          ["磁盘", si.disk && si.disk.text],
          ["内存", si.memory && si.memory.text],
          ["CPU", si.cpu && si.cpu.text],
          ["网络", si.net && si.net.text],
        ].filter(([, v]) => v);
        if (rows.length) {
          const frag = document.createElement("div");
          frag.innerHTML = wrap(rows.map(([k, v]) =>
            `<div class="settings-row"><span class="settings-row-label">${k}</span><span class="settings-row-value">${v}</span></div>`
          ).join(""));
          sysBody.appendChild(frag);
        }
      } catch (_) {}
    })();
  }

  // Last-rendered settings-config signature. Same reasoning as
  // ``lastSidebarSig``, but the stakes are higher: this card holds live
  // <input> elements, so an unguarded 2 Hz innerHTML rebuild would wipe
  // whatever the user is halfway through typing. Only the persisted
  // config participates in the signature — quota *usage* ticks every
  // couple of seconds and must not trigger a rebuild.
  let lastConfigSig = null;

  /** Escape a value for use inside a double-quoted HTML attribute. */
  function attr(v) { return escape(String(v == null ? "" : v)); }

  function multiplierRow(model, mult) {
    return `
      <div class="cfg-multipliers-row">
        <input class="cfg-input cfg-mult-name" type="text"
               placeholder="模型名 (如 m3)" value="${attr(model)}" />
        <input class="cfg-input cfg-mult-value" type="number" min="0" step="0.5"
               placeholder="1" value="${attr(mult)}" />
        <button class="cfg-row-remove" type="button" title="删除这一行">×</button>
      </div>`;
  }

  function allowedChip(model) {
    return `
      <span class="cfg-chip" data-model="${attr(model)}">
        ${escape(model)}
        <button class="cfg-chip-remove" type="button" title="移除">×</button>
      </span>`;
  }

  // v0.119：链接上游（合并统计）chip。data-peer 存被链接的上游名，
  // 提交时按同名 .cfg-linked-chip 收集成 list[str] 写到
  // api.updateUpstreamQuota payload.linked_upstreams。
  function linkedChip(peer) {
    return `
      <span class="cfg-chip cfg-linked-chip" data-peer="${attr(peer)}">
        <span class="cfg-chip-link-icon" aria-hidden="true">🔗</span>
        ${escape(peer)}
        <button class="cfg-chip-remove" type="button" title="移除">×</button>
      </span>`;
  }

  // v0.119：弹 modal 让用户从同平台其它上游里多选「要链接的 peer」。
  // onPick(pickedNames) 由调用方负责把名字插入 chip 区。本函数只负责
  // modal 生命周期（打开 / 关闭 / Esc / 点背景关闭）。
  function openLinkUpstreamOverlay({ platform, selfName, existing, onPick }) {
    const snap = lastSnap || {};
    const peers = [];
    const plat = platform || "";
    // v0.120：跨平台搜索 —— 允许链接不同平台但同名的上游
    // （如 anthropic/minimax ↔ openai/minimax）。
    // v0.119 原版仅搜同平台，导致跨平台同名 cfg 无法链接。
    const allPlats = snap.upstreams || {};
    for (const [p, ups] of Object.entries(allPlats)) {
      for (const u of (ups || [])) {
        if (!u || !u.name) continue;
        if (u.name === selfName && p === plat) continue;  // 排除自己（同平台）
        // 同名跨平台：允许多平台同名 cfg 互相链接
        peers.push({ ...u, _platform: p });
      }
    }
    if (!peers.length) {
      const status = document.querySelector(
        `[data-platform="${attr(plat)}"][data-name="${attr(selfName)}"] .cfg-status`
      );
      if (status) {
        status.textContent = "没有其它上游可链接";
        status.className = "cfg-status cfg-status-error";
        setTimeout(() => { status.textContent = ""; status.className = "cfg-status"; }, 3000);
      }
      return;
    }
    const overlayId = "cfg-link-overlay";
    let overlay = document.getElementById(overlayId);
    if (overlay) overlay.remove();
    // v0.120：平台不同时显示平台标签，提示用户链接了跨平台的上游
    const rowHtml = peers.map((u) => {
      const checked = (existing || []).includes(u.name) ? "checked" : "";
      const platLabel = (u._platform && u._platform !== plat)
        ? `<span class="cfg-link-row-platform">[${escape(u._platform)}]</span>` : "";
      return `
        <label class="cfg-link-row">
          <input type="checkbox" class="cfg-link-cb" value="${attr(u.name)}" data-platform="${attr(u._platform || "")}" ${checked} />
          <span class="cfg-link-row-name" data-i18n-keep>${escape(u.name)}</span>
          ${platLabel}
          <span class="cfg-link-row-meta">${escape(u.note || "")}</span>
        </label>`;
    }).join("");
    overlay = document.createElement("div");
    overlay.id = overlayId;
    overlay.className = "modal-overlay cfg-link-overlay";
    overlay.innerHTML = `
      <div class="modal-card cfg-link-card">
        <div class="modal-header">
          <div class="modal-title">新建链接 · ${escape(plat)}</div>
          <button class="modal-close" type="button" aria-label="关闭">×</button>
        </div>
        <div class="modal-body">
          <div class="cfg-link-hint">
            选择要链接到 <b>${escape(selfName)}</b> 的其它上游；
            多个链接会自动合并为一个虚拟统计组（A+B 与 A+C ⇒ A+B+C）。
            <br/>不同平台同名上游也可以链接；不影响路由、活跃状态、计费、quota 配置。
          </div>
          <div class="cfg-link-list">${rowHtml}</div>
        </div>
        <div class="modal-footer">
          <button class="btn cfg-link-cancel" type="button">取消</button>
          <button class="btn btn-primary cfg-link-confirm" type="button">确定</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = () => { overlay.remove(); document.removeEventListener("keydown", onEsc); };
    const onEsc = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onEsc);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    overlay.querySelector(".modal-close").addEventListener("click", close);
    overlay.querySelector(".cfg-link-cancel").addEventListener("click", close);
    overlay.querySelector(".cfg-link-confirm").addEventListener("click", () => {
      const picked = Array.from(overlay.querySelectorAll(".cfg-link-cb:checked"))
        .map(cb => cb.value);
      close();
      if (onPick) onPick(picked);
    });
  }

  function renderSettingsConfig(body, snap) {
    if (!body) return;
    const cfg = (snap && snap.upstreams) || {};
    const active = ((snap && snap.active_per_platform) || {});

    const sig = JSON.stringify(cfg);
    if (sig === lastConfigSig) return;
    lastConfigSig = sig;

    const blocks = pooledUpstreams(cfg).map(({ plat, cfg: c }) => {
        const mults = c.model_multipliers || {};
        const multRows = Object.entries(mults)
          .map(([m, v]) => multiplierRow(m, v))
          .join("");
        const chips = (c.allowed_models || []).map(allowedChip).join("");
        return `
        <details class="upstream-config" data-platform="${attr(plat)}" data-name="${attr(c.name)}">
          <summary>
            ${active[plat] === c.name ? '<span class="active-tag">ACTIVE</span>' : ""}
            <span data-i18n-keep>${escape(c.name)}</span>
            <span class="upstream-config-platform" data-i18n-keep>${escape(wireGroup(c, plat))}</span>
          </summary>
          <div class="upstream-config-body">
            <div class="cfg-field">
              <label class="cfg-label">计费模式</label>
              <select class="cfg-billing-unit">
                <option value="count" ${c.billing_unit !== "token" ? "selected" : ""}>按次数计费</option>
                <option value="token" ${c.billing_unit === "token" ? "selected" : ""}>按 Token 计费</option>
              </select>
            </div>

            <div class="cfg-field">
              <label class="cfg-label">地址 URL</label>
              <div class="cfg-url-display">${escape(c.url || "—")}</div>
            </div>

            <div class="cfg-field">
              <label class="cfg-label">
                auto 兜底模型
                <span class="cfg-hint">客户端发 model="auto" 时用此值替换。留空则按允许模型列表的第一个,再不行原样透传</span>
              </label>
              <input class="cfg-input cfg-default-model" type="text"
                     placeholder="(不填则走允许模型列表 / 透传)"
                     value="${attr(c.default_model)}" />
            </div>

            <div class="cfg-field cfg-token-fields" style="${c.billing_unit === "token" ? "" : "display:none"}">
              <label class="cfg-label">Token 计费字段</label>
              <div class="cfg-token-checkboxes">
                <label class="cfg-checkbox-label">
                  <input type="checkbox" class="cfg-tf-input" data-field="input_tokens"
                    ${(c.token_fields || {})["input_tokens"] !== false ? "checked" : ""} /> input_tokens（输入）
                </label>
                <label class="cfg-checkbox-label">
                  <input type="checkbox" class="cfg-tf-input" data-field="output_tokens"
                    ${(c.token_fields || {})["output_tokens"] !== false ? "checked" : ""} /> output_tokens（输出）
                </label>
                <label class="cfg-checkbox-label">
                  <input type="checkbox" class="cfg-tf-input" data-field="cache_read_input_tokens"
                    ${(c.token_fields || {})["cache_read_input_tokens"] ? "checked" : ""} /> cache_read_input_tokens（缓存命中读取）
                </label>
                <label class="cfg-checkbox-label">
                  <input type="checkbox" class="cfg-tf-input" data-field="cache_creation_input_tokens"
                    ${(c.token_fields || {})["cache_creation_input_tokens"] ? "checked" : ""} /> cache_creation_input_tokens（缓存写入）
                </label>
              </div>
            </div>

            <div class="cfg-field">
              <label class="cfg-label">
                5 小时额度
              </label>
              <input class="cfg-input cfg-quota" type="number" min="0" step="1"
                     placeholder="1500" value="${c.quota_5h == null ? "" : attr(c.quota_5h)}" />
            </div>

            <div class="cfg-field">
              <label class="cfg-label">
                模型倍率
              </label>
              <div class="cfg-multipliers">${multRows}</div>
              <button class="btn btn-ghost cfg-add-mult" type="button">+ 添加倍率</button>
            </div>

            <div class="cfg-field">
              <label class="cfg-label">
                允许的模型
                <span class="cfg-hint">留空 = 不限制。回车添加</span>
              </label>
              <div class="cfg-chips">
                ${chips}
                <input class="cfg-chip-input" type="text" placeholder="输入模型名后回车" />
              </div>
            </div>

            <div class="cfg-field cfg-linked-upstreams-field">
              <label class="cfg-label">
                链接上游（合并统计）
                <span class="cfg-hint">链接后所有统计页视为一个虚拟上游；不影响路由和计费；A+B 与 A+C 自动并为 A+B+C</span>
              </label>
              <div class="cfg-chips cfg-linked-chips">
                ${(c.linked_upstreams || []).map(linkedChip).join("")}
                <button type="button" class="cfg-link-add btn btn-ghost">+ 新建链接</button>
              </div>
            </div>

            <div class="cfg-actions">
              <button class="btn btn-primary cfg-save" type="button">保存</button>
              <span class="cfg-status"></span>
            </div>
          </div>
        </details>`;
    }).join("");

    body.innerHTML = blocks || '<div class="card-empty">未配置上游</div>';
    wireUpstreamConfig(body);
  }

  /** Attach the add/remove/save handlers to a freshly rendered config
   *  card. Called once per rebuild — the sig guard above means that's
   *  only when the persisted config actually changed. */
  function wireUpstreamConfig(body) {
    body.querySelectorAll(".upstream-config").forEach(card => {
      const status = card.querySelector(".cfg-status");
      const setStatus = (text, kind) => {
        if (!status) return;
        status.textContent = text || "";
        if (kind) status.setAttribute("data-kind", kind);
        else status.removeAttribute("data-kind");
      };

      // Editing anything clears a stale "已保存" so the user never sees
      // a success message describing a form they've since changed.
      card.addEventListener("input", () => setStatus(""));

      const multList = card.querySelector(".cfg-multipliers");
      const addMult = card.querySelector(".cfg-add-mult");
      if (addMult && multList) {
        addMult.addEventListener("click", () => {
          multList.insertAdjacentHTML("beforeend", multiplierRow("", ""));
          const rows = multList.querySelectorAll(".cfg-mult-name");
          const last = rows[rows.length - 1];
          if (last) last.focus();
          setStatus("");
        });
      }

      // Row removal is delegated — rows added after wiring still work.
      if (multList) {
        multList.addEventListener("click", (e) => {
          const btn = e.target.closest(".cfg-row-remove");
          if (!btn) return;
          const row = btn.closest(".cfg-multipliers-row");
          if (row) row.remove();
          setStatus("");
        });
      }

      const chipBox = card.querySelector(".cfg-chips");
      const chipInput = card.querySelector(".cfg-chip-input");
      if (chipBox && chipInput) {
        chipInput.addEventListener("keydown", (e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          const value = chipInput.value.trim();
          if (!value) return;
          const existing = Array.from(chipBox.querySelectorAll(".cfg-chip"))
            .map(el => el.getAttribute("data-model"));
          if (!existing.includes(value)) {
            chipInput.insertAdjacentHTML("beforebegin", allowedChip(value));
          }
          chipInput.value = "";
          setStatus("");
        });
        chipBox.addEventListener("click", (e) => {
          const btn = e.target.closest(".cfg-chip-remove");
          if (!btn) return;
          const chip = btn.closest(".cfg-chip");
          if (chip) chip.remove();
          setStatus("");
        });
      }

      // v0.119：链接上游 chip 移除 + 「+ 新建链接」弹 modal 让用户
      // 选同平台其它上游名。链接 chip 状态只动 DOM（与 allowed_models
      // 同款），保存时一次性写回 payload。
      const linkedChipsBox = card.querySelector(".cfg-linked-chips");
      if (linkedChipsBox) {
        linkedChipsBox.addEventListener("click", (e) => {
          const rm = e.target.closest(".cfg-chip-remove");
          if (rm) {
            const chip = rm.closest(".cfg-linked-chip");
            if (chip) chip.remove();
            setStatus("");
            return;
          }
          const addBtn = e.target.closest(".cfg-link-add");
          if (addBtn) {
            const platform = card.getAttribute("data-platform");
            const name = card.getAttribute("data-name");
            openLinkUpstreamOverlay({
              platform,
              selfName: name,
              existing: Array.from(linkedChipsBox.querySelectorAll(".cfg-linked-chip"))
                .map(el => el.getAttribute("data-peer"))
                .filter(Boolean),
              onPick: (picked) => {
                // 去掉重复 + 排除自己；保留现有其它 chip
                const existing = Array.from(linkedChipsBox.querySelectorAll(".cfg-linked-chip"))
                  .map(el => el.getAttribute("data-peer"));
                for (const p of picked) {
                  if (p && p !== name && !existing.includes(p)) {
                    addBtn.insertAdjacentHTML("beforebegin", linkedChip(p));
                  }
                }
                setStatus("");
              },
            });
          }
        });
      }

      // Billing-unit toggle: show/hide token-fields section
      const billingUnitSelect = card.querySelector(".cfg-billing-unit");
      const tokenFieldsSection = card.querySelector(".cfg-token-fields");
      if (billingUnitSelect && tokenFieldsSection) {
        billingUnitSelect.addEventListener("change", () => {
          tokenFieldsSection.style.display =
            billingUnitSelect.value === "token" ? "" : "none";
          setStatus("");
        });
      }

      const save = card.querySelector(".cfg-save");
      if (save) {
        save.addEventListener("click", async () => {
          const platform = card.getAttribute("data-platform");
          const name = card.getAttribute("data-name");

          const quotaRaw = (card.querySelector(".cfg-quota").value || "").trim();
          let quota = null;
          if (quotaRaw !== "") {
            quota = Number(quotaRaw);
            if (!isFinite(quota) || quota < 0) {
              setStatus("额度必须是非负整数", "error");
              return;
            }
            quota = Math.round(quota);
          }

          const multipliers = {};
          let bad = null;
          card.querySelectorAll(".cfg-multipliers-row").forEach(row => {
            const m = (row.querySelector(".cfg-mult-name").value || "").trim();
            const vRaw = (row.querySelector(".cfg-mult-value").value || "").trim();
            if (!m) return;  // blank name = an unfilled row, just skip it
            const v = vRaw === "" ? 1 : Number(vRaw);
            if (!isFinite(v) || v < 0) { bad = m; return; }
            multipliers[m] = v;
          });
          if (bad) {
            setStatus(`模型 ${bad} 的倍率无效`, "error");
            return;
          }

          const allowed = Array.from(card.querySelectorAll(".cfg-chip"))
            .map(el => el.getAttribute("data-model"))
            .filter(Boolean);

          // v0.119：链接上游名（按 .cfg-linked-chip data-peer 收集）。
          // Python apply_quota_edit 会自动 trim / 去重 / 排除自己 / 警告
          // 同平台不存在的 name（懒解析）。
          const linked = Array.from(card.querySelectorAll(".cfg-linked-chip"))
            .map(el => el.getAttribute("data-peer"))
            .filter(Boolean);

          // Billing unit + token fields
          const billing_unit = card.querySelector(".cfg-billing-unit")?.value || "count";
          const token_fields = {};
          if (billing_unit === "token") {
            card.querySelectorAll(".cfg-tf-input").forEach(cb => {
              token_fields[cb.getAttribute("data-field")] = cb.checked;
            });
          }

          // v0.74: default_model — 客户端发 "auto" 时的兜底模型名。
          // 跟 Python apply_quota_edit 一致 —— 空字符串视作 null,
          // 走 cfg.allowed_models[0] 兜底或透传。trim 由 Python 做。
          const defaultModelRaw = (card.querySelector(".cfg-default-model")?.value || "").trim();

          setStatus("保存中…");
          const res = await api.updateUpstreamQuota(platform, name, {
            quota_5h: quota,
            model_multipliers: multipliers,
            allowed_models: allowed,
            linked_upstreams: linked,
            billing_unit: billing_unit,
            default_model: defaultModelRaw || null,
            ...(billing_unit === "token" ? { token_fields: token_fields } : {}),
          });
          if (res && res.ok) {
            setStatus("已保存", "ok");
            // Python rewrote upstreams.json and reloaded Settings, so
            // the next snapshot carries the new values. Drop the sig so
            // the card re-renders from the persisted truth rather than
            // from whatever the DOM happens to hold.
            lastConfigSig = null;
          } else {
            setStatus((res && res.error) || "保存失败", "error");
          }
        });
      }
    });
  }

  // -------------------------------------------------------------------
  // v0.70：Settings 页 "快捷切换" 管理区。
  //
  // 数据源 snapshot.quick_switch（与 sidebar 同一份）,支持新增 / 编辑 /
  // 删除。改完调 api.saveQuickSwitch → Python replace_quick_switch 写
  // upstreams.json 顶层 quick_switch + reload_settings → 下一次 poll
  // (≤500ms) sidebar 和本卡片自动反映。
  //
  // 渲染策略跟 renderSettingsRelay / renderSettingsConfig 一致：签名
  // 短路 + 整块 innerHTML 重建。编辑表单用独立的 #qs-editor-overlay
  // modal（新增/编辑共用），saveQuickSwitchList 统一管保存路径。
  // -------------------------------------------------------------------

  let lastQuickSwitchCfgSig = null;

  // v0.74：把"+ 新增 / 编辑 / 删除"按钮的入口改成 window 上的全局
  // 函数,renderSettingsQuickSwitch 通过内联 onclick 直接调它们。这样
  // 彻底绕开了"事件委托收不到 click"那一类 WebView2 / pywebview 兼容
  // 性 bug —— 内联 onclick 是浏览器派发 click 时的同步属性查找,跟
  // addEventListener 监听器顺序、stopPropagation 都无关。
  window.__qsAdd = function () {
    try { openQuickSwitchEditor(null); }
    catch (err) { console.error("[__qsAdd] failed:", err); }
  };
  window.__qsEdit = function (idx) {
    const cur = (lastSnap && Array.isArray(lastSnap.quick_switch))
      ? lastSnap.quick_switch[idx] : null;
    try { openQuickSwitchEditor(cur || null); }
    catch (err) { console.error("[__qsEdit] failed:", err); }
  };
  window.__qsDel = function (idx) {
    const list = (lastSnap && Array.isArray(lastSnap.quick_switch))
      ? lastSnap.quick_switch.slice() : [];
    const target = list[idx];
    if (!target) return;
    confirmModal(
      "删除快捷项",
      `确定要删除 "${target.label}" 吗？\n这条配置会从 upstreams.json 永久移除。`,
      { ok: "删除", cancel: "取消", danger: true }
    ).then(ok => {
      if (!ok) return;
      list.splice(idx, 1);
      saveQuickSwitchList(list);
    }).catch(err => console.error("[__qsDel] failed:", err));
  };

  function renderSettingsQuickSwitch(body, snap) {
    if (!body) return;
    const qs = (snap && Array.isArray(snap.quick_switch)) ? snap.quick_switch : [];
    // 签名只包含持久化数据 —— modal 里没填完的 input 不参与签名，这样
    // poll tick 不会因为数据没变就把 modal 给"刷没了"。
    const sig = JSON.stringify(qs);
    if (sig === lastQuickSwitchCfgSig) return;
    lastQuickSwitchCfgSig = sig;
    // v0.74：放弃事件委托(参考 wireQuickSwitchCfgButtons 上一版的失败
    // 经验),改用内联 onclick —— WebView2 + pywebview 偶发会让挂在 body
    // 上的 click handler 收不到事件,但内联 onclick 属性是浏览器在
    // 派发 click 时同步查找的,不会被任何外层 stopPropagation / 监听
    // 器顺序问题吞掉。每行按钮带自己的 idx,新增按钮带全局函数调用。
    const rows = qs.map((q, i) => `
      <div class="qs-row" data-qs-row="${i}">
        <span class="qs-row-label">${escape(q.label || "")}</span>
        <span class="qs-row-target" data-i18n-keep>${escape(q.platform || "")} / ${escape(q.upstream || "")} / ${escape(q.model || "")}</span>
        <button type="button" class="btn btn-small" onclick="window.__qsEdit(${i})">编辑</button>
        <button type="button" class="btn btn-small" onclick="window.__qsDel(${i})">删除</button>
      </div>`).join("");
    body.innerHTML = `
      <div class="qs-list">${rows || '<div class="card-empty">暂无快捷项，点下方按钮添加</div>'}</div>
      <div class="qs-actions">
        <button type="button" class="btn btn-primary" onclick="window.__qsAdd()">+ 新增快捷项</button>
      </div>`;
  }

  // -------------------------------------------------------------------
  // v0.155：Settings 页 "平台别名" 管理区。
  //
  // 列出 snapshot.by_agent 出现过的工具（+ 已有别名 + 兜底桶 "其它"），
  // 每行一个可编辑显示名。保存走 api.saveAgentAliases → Python
  // save_agent_aliases 写 upstreams.json 顶层 agent_aliases + reload_settings。
  // 纯展示层映射，原始 agent 名落库不变，改名后总览/统计自动按新名显示
  // （下个 poll tick 渲染时 agentDisplayName 读新的 agentAliases）。
  // -------------------------------------------------------------------
  let lastAgentAliasSig = null;

  window.__agentAliasSave = function () {
    const rows = document.querySelectorAll("[data-agent-alias-row]");
    const aliases = {};
    rows.forEach(r => {
      const raw = r.getAttribute("data-agent-alias-row");
      const input = r.querySelector("input");
      const val = (input ? input.value : "").trim();
      // v0.159：值升级为 {name, color} 对象。v0.162：再加 view 字段。
      // 空 name = 删条目；保留既有 color / view（用户在总览 modal 改过
      // 颜色 / 显示模式 → 设置页只改名字写回来时不丢）。
      if (val && val !== raw) {
        const prev = (agentAliases && agentAliases[raw]) || {};
        const prevName = (typeof prev === "string") ? prev : (prev.name || "");
        const prevColor = (typeof prev === "object" && prev !== null) ? prev.color : null;
        const prevView = (typeof prev === "object" && prev !== null) ? prev.view : null;
        const entry = { name: val, color: prevColor };
        if (prevView === "both" || prevView === "requests" || prevView === "tokens") {
          entry.view = prevView;
        }
        aliases[raw] = entry;
      }
    });
    api.saveAgentAliases(aliases).then(res => {
      if (res && res.ok) {
        agentAliases = aliases;
        lastAgentAliasSig = null;
        if (lastSnap) renderAll(lastSnap, lastStatus);
      }
    }).catch(err => console.error("[__agentAliasSave] failed:", err));
  };

  function renderSettingsAgentAlias(body, snap) {
    if (!body) return;
    const byAgent = (snap && snap.by_agent) ? Object.keys(snap.by_agent) : [];
    const aliasKeys = (agentAliases && typeof agentAliases === "object") ? Object.keys(agentAliases) : [];
    const keys = Array.from(new Set([...byAgent, ...aliasKeys]));
    if (!keys.includes("其它")) keys.push("其它");
    // 签名只含持久化数据（keys + agentAliases），不含 input 值 —— 用户
    // 正在编辑时 poll tick 不会把输入框刷掉。
    const sig = JSON.stringify([keys, agentAliases]);
    if (sig === lastAgentAliasSig) return;
    lastAgentAliasSig = sig;
    const rows = keys.map(k => `
      <div class="qs-row" data-agent-alias-row="${escape(k)}">
        <span class="qs-row-target" data-i18n-keep>${escape(k)}</span>
        <input type="text" class="agent-alias-input" value="${escape(agentDisplayName(k))}" data-i18n-keep>
      </div>`).join("");
    body.innerHTML = `
      <div class="qs-list">${rows || '<div class="card-empty">暂无数据</div>'}</div>
      <div class="qs-actions">
        <button type="button" class="btn btn-primary" onclick="window.__agentAliasSave()">保存别名</button>
      </div>`;
  }

  // -------------------------------------------------------------------
  // v0.157：Settings 页 "UA 归类" 管理区。
  //
  // 用户把真实出现过的 User-Agent 整串归到一个平台名（新建或并入已有）。
  // 规则存 upstreams.json 顶层 ua_rules，中继进程在请求入口 resolve_agent
  // 消费（精确命中即落库该平台名，优先级最高）。数据源：最近 UA 列表来自
  // 桥 get_recent_uas（读 requests.raw_ua）+ 已配置规则 key（含未再出现
  // 的老规则）。留空 = 删除该规则。输入框留空时该行不落盘。
  // -------------------------------------------------------------------
  let lastUaRuleSig = null;
  let uaRuleRecent = [];            // [{ua,last_ts,count}] 最近 UA（30s TTL）
  let uaRuleRecentAt = 0;

  window.__uaRuleSave = function () {
    const rows = document.querySelectorAll("[data-ua-rule-row]");
    const rules = {};
    rows.forEach(r => {
      const ua = r.getAttribute("data-ua-rule-row");
      const input = r.querySelector("input");
      const val = (input ? input.value : "").trim();
      if (val) rules[ua] = val;
    });
    api.saveUaRules(rules).then(res => {
      if (res && res.ok) {
        uaRules = rules;
        lastUaRuleSig = null;
        if (lastSnap) renderAll(lastSnap, lastStatus);
      }
    }).catch(err => console.error("[__uaRuleSave] failed:", err));
  };

  function renderSettingsUaRules(body, snap) {
    if (!body) return;
    // 30s TTL：避免 500ms poll tick 每 tick 都打桥拉最近 UA 列表。
    if (uaRuleRecentAt === 0 || Date.now() - uaRuleRecentAt > 30000) {
      uaRuleRecentAt = Date.now();
      api.getRecentUas(100).then(list => {
        if (Array.isArray(list)) {
          uaRuleRecent = list;
          renderSettingsUaRules(body, snap);   // 拉到后补渲染一次
        }
      }).catch(() => {});
    }
    if (uaRuleRecentAt === 0) return;          // 首次还没拉到，等回调
    const recentSet = new Set(uaRuleRecent.map(r => r.ua));
    const ruleKeys = (uaRules && typeof uaRules === "object") ? Object.keys(uaRules) : [];
    // 行 = 最近 UA ∪ 已配置规则 key（含未在最近列表里的老规则）
    const uas = [...uaRuleRecent.map(r => r.ua), ...ruleKeys.filter(k => !recentSet.has(k))];
    // 已有平台名 → datalist 提示（新建或并入已有都行）
    const platformNames = Array.from(new Set([
      ...ruleKeys.map(k => uaRules[k]),
      ...(snap && snap.by_agent ? Object.keys(snap.by_agent) : []),
      "其它",
    ])).filter(Boolean);
    // 签名只含持久化数据（uas + uaRules），不含 input 值 —— 用户正在
    // 编辑时 poll tick 不会把输入框刷掉。
    const sig = JSON.stringify([uas, uaRules]);
    if (sig === lastUaRuleSig) return;
    lastUaRuleSig = sig;
    const rowHtml = uas.map(ua => {
      const cur = (uaRules && uaRules[ua]) || "";
      return `
        <div class="qs-row" data-ua-rule-row="${escape(ua)}">
          <span class="ua-rule-ua" data-i18n-keep title="${escape(ua)}">${escape(ua)}</span>
          <input type="text" class="ua-rule-input" list="ua-rule-platforms"
                 value="${escape(cur)}" placeholder="归入平台名（可新建）" data-i18n-keep>
        </div>`;
    }).join("");
    const opts = platformNames.map(n => `<option value="${escape(n)}"></option>`).join("");
    body.innerHTML = `
      <datalist id="ua-rule-platforms">${opts}</datalist>
      <div class="qs-list">${rowHtml || '<div class="card-empty">暂无数据（有新请求到达后会自动列出其 User-Agent）</div>'}</div>
      <div class="qs-actions">
        <button type="button" class="btn btn-primary" onclick="window.__uaRuleSave()">保存归类</button>
      </div>`;
  }

  // ---------------------------------------------------------------------
  // v0.11.3 settings page — "界面与行为" prefs
  // ---------------------------------------------------------------------

  const PREFS_KEY = "relay-gui-prefs-v1";
  const prefs = {
    flash: true, releaseFlash: true, wholeFlash: false,
    advancedFlash: false,
    flashEase: "0.615,-0.003,0.325,0.986",
    flashThickness: 3,
    flashWidth: 88,
    flashColorBump: "",
    flashColorDecrease: "",
    hideStatusBar: false,
    launchHidden: false,
    advancedSwitch: false,
    developerMode: false,
    // v0.135：实时栏管理下两个折叠组的折叠状态（纯前端偏好，持久化）。
    livePanelTimeoutAdvanced: false,
    livePanelStyleAdvanced: false,
    // v0.113：总览页无边框 —— 开启后总览页所有卡片去掉玻璃容器裸底悬浮。
    noFrame: false,
  };

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (raw) Object.assign(prefs, JSON.parse(raw));
    } catch (_) {}
    applyPrefsClass();
  }

  function savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (_) {}
    applyPrefsClass();
  }

  function applyPrefsClass() {
    const r = document.documentElement.style;
    // #2：把高级闪烁参数写进 CSS 变量，供波纹渐变 / 缓动使用。
    r.setProperty("--flash-ease", `cubic-bezier(${prefs.flashEase || "0.615,-0.003,0.325,0.986"})`);
    r.setProperty("--flash-blur", `${prefs.flashThickness || 3}px`);
    r.setProperty("--flash-width", `${prefs.flashWidth || 88}%`);
    if (prefs.flashColorBump) r.setProperty("--flash-color-bump", prefs.flashColorBump);
    else r.removeProperty("--flash-color-bump");
    if (prefs.flashColorDecrease) r.setProperty("--flash-color-decrease", prefs.flashColorDecrease);
    else r.removeProperty("--flash-color-decrease");
    // #8：开启全页闪烁则关闭逐条闪烁。
    const perItemFlash = prefs.flash && !prefs.wholeFlash;
    document.body.classList.toggle("prefs-no-flash", !perItemFlash);
    document.body.classList.toggle("prefs-no-release", !prefs.releaseFlash);
    // #5：隐藏顶部运行中状态栏。
    document.body.classList.toggle("hide-status-bar", !!prefs.hideStatusBar);
    // v0.113：总览页无边框 —— body.overview-no-frame 触发 CSS 裸底规则。
    document.body.classList.toggle("overview-no-frame", !!prefs.noFrame);
    // nav "实时" 波纹缓存的 has-stream 状态作废，切回时重新判定。
    lastLiveNavStreaming = null;
  }

  // v0.11.15：全页闪烁 —— 开启后流（任一上游计数增长）时整个窗口做
  // 一次轻微的主色光晕脉冲。给 body 挂 .body-flash 类，CSS 动画跑完
  // 后移除；跟 .upstream-row-bumped 的 2s 波纹同源触发（newBumps）。
  let wholeFlashTimer = null;
  function triggerWholeFlash() {
    if (!prefs.wholeFlash || !document.body) return;
    document.body.classList.remove("body-flash");
    void document.body.offsetWidth; // reflow，保证连续 bump 能重触发
    document.body.classList.add("body-flash");
    if (wholeFlashTimer) clearTimeout(wholeFlashTimer);
    wholeFlashTimer = setTimeout(() => {
      document.body.classList.remove("body-flash");
      wholeFlashTimer = null;
    }, 1500);
  }


  // 完全透传模式开关 + 透传上游列表
  async function renderSettingsPassthrough(body, snap) {
    if (!body) return;
    const enabled = !!(snap && snap.passthrough_mode);
    // v0.102.2：双极按钮渲染到 settings-relay-mode section，含加粗"中继模式"标题。
    body.innerHTML = `
      <div class="settings-section-title settings-heading-bold">中继模式</div>
      <div class="settings-row">
        <span class="settings-row-value">
          <div class="pt-mode-switch" id="pt-mode-switch" data-enabled="${enabled ? "1" : "0"}">
            <span class="pt-mode-slider"></span>
            <button type="button" class="pt-mode-opt ${!enabled ? "pt-mode-opt-active" : ""}" data-mode="relay">转换模式</button>
            <button type="button" class="pt-mode-opt ${enabled ? "pt-mode-opt-active" : ""}" data-mode="passthrough">透传模式</button>
          </div>
        </span>
      </div>
    `;
    const switchEl = body.querySelector("#pt-mode-switch");
    const optRelay = body.querySelector('[data-mode="relay"]');
    const optPt = body.querySelector('[data-mode="passthrough"]');
    if (switchEl) {
      const setActive = (on) => {
        switchEl.dataset.enabled = on ? "1" : "0";
        optRelay.classList.toggle("pt-mode-opt-active", !on);
        optPt.classList.toggle("pt-mode-opt-active", on);
      };
      switchEl.addEventListener("click", async (e) => {
        const btn = e.target.closest(".pt-mode-opt");
        if (!btn) return;
        const wantEnabled = btn.dataset.mode === "passthrough";
        if ((switchEl.dataset.enabled === "1") === wantEnabled) return; // 已在目标态
        const result = await api.setPassthroughMode(wantEnabled);
        if (result && result.error) {
          alert("切换失败: " + result.error);
          return; // 保持当前滑块位置
        }
        setActive(wantEnabled);
        // v0.102：切模式联动三极开关默认档 —— 透传 → "仅透传"，
        // 转换 → "仅转换"（localStorage 覆盖 + 三处开关刷新）。
        setConsumeMode(wantEnabled ? "passthrough" : "relay");
        if (snap) {
          snap.passthrough_mode = wantEnabled; // 立刻反映到 snapshot
          // 透传模式 UI 即时切换（不等 500ms tick）：
          // 状态栏标记 + body class 全局显隐同步应用。
          window._ptMode = !!wantEnabled;
          document.body.classList.toggle("passthrough-mode", !!wantEnabled);
          lastStatusSig = null; // 强制 renderStatus 重画（带上（透传模式））
          renderStatus(lastStatus || null);
        }
      });
    }
  }

  async function renderSettingsPassthroughUpstreams(body) {
    if (!body) return;
    const result = await api.getPassthroughUpstreams();
    const items = (result && result.upstreams) || [];
    if (!items.length) {
      body.innerHTML = '<div class="card-empty">暂无（开启透传模式后，由请求自动发现）</div>';
      return;
    }
    body.innerHTML = items.map((u) => {
      const name = u.display_name || u.upstream_name;
      return `
        <div class="settings-row">
          <span class="settings-row-label">
            ${escape(name)}
          </span>
          <span class="settings-row-value">
            <span class="cfg-hint">${escape(u.url)}</span>
            <span class="cfg-hint">key: ${escape(u.api_key_masked)}</span>
            <span class="cfg-hint">${u.request_count} 次请求</span>
            <button class="btn btn-ghost btn-xs js-rename-pt-upstream"
                    data-url="${escape(u.url)}"
                    data-field="${escape(u.model_field_name)}"
                    data-full-key="${escape(u.api_key_alias)}"
                    data-name="${escape(name)}"
                    type="button">重命名</button>
          </span>
        </div>
      `;
    }).join("");
    body.querySelectorAll(".js-rename-pt-upstream").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const newName = prompt("新名称（留空清除用户命名）:", btn.dataset.name);
        if (newName === null) return; // 取消
        const realKey = btn.dataset.fullKey || "";
        const trimmed = newName.trim();
        const res = await api.renamePassthroughUpstream(
          btn.dataset.url, btn.dataset.field, realKey, trimmed,
        );
        if (res && res.error) alert("重命名失败: " + res.error);
        else renderSettingsPassthroughUpstreams(body);
      });
    });
  }


  // v0.113n：界面语言 —— 纯前端偏好（localStorage 持久化）。
  // 「只加选项 + 钩子」：本轮翻译覆盖设置页（含新「存储管理」区块）+ 设置
  // 区块标题 + 设置子菜单；全量界面翻译是后续会话（钩子已就位：扩 dict +
  // 扩大 I18N_SELECTOR 即可）。字典以**中文原文为 key**（界面硬编码），
  // 切换时对匹配元素逐「直接文本节点」替换（保留子元素）；元素上的
  // __i18nKeys 缓存各文本节点原文供切回还原。用原文做 key 而非 data-i18n
  // 属性：不动各 render 模板，改动面最小、无侵入。
  // v0.113r：全站 i18n —— 覆盖从设置页扩展到全局静态文案。
  //   设置页：settings-* / storage-* / nav-sub-item
  //   全局 chrome：nav-item / 顶栏 / 卡片标题 / 状态栏 / 弹窗 / 配置表单
  //   按钮：.btn / .seg-btn / .wire-btn（含动态切换“测试/测试中…”等，
  //   applyLang 的“内容变了就重新捕获”逻辑保证不误伤动态填充）
  const I18N_SELECTOR = [
    // 设置页
    ".settings-group-label", ".settings-item-title", ".settings-item-hint",
    ".settings-section-title", ".settings-section-subtitle",
    ".storage-card-name", ".storage-hint",
    ".nav-sub-item",
    // 全局 chrome
    ".nav-item", ".logo-subtitle", ".hero-title", ".tms-eyebrow",
    ".tms-legend-item", ".card-title", ".card-eyebrow", ".cards-manage-fab",
    ".stats-toolbar-label", ".status-text", ".status-card-meta",
    ".sidebar-quick-switch-label",
    // 按钮
    ".btn", ".seg-btn", ".wire-btn",
    // 弹窗 / 配置表单 / 空态
    ".modal-title", ".cfg-label", ".cfg-hint", ".cfg-req",
    ".cfg-checkbox-label", ".cfg-advanced-summary",
    ".create-test-log-head", ".create-test-log-clear",
    ".card-empty",
  ].join(", ");
  // v0.172："跟随系统" 选项 —— intent 存用户意图（含 "auto"），
  // 运行期 lang 始终是 5 种具体语言之一。navigator.language 在 pywebview
  // WebView2 = Edge Chromium 下读系统语言（zh-CN / ja-JP / en-US 等），
  // 简化为「只看前缀」映射；前缀不在 5 种 → 兜底 zh。
  function _resolveLang(intent) {
    if (intent !== "auto") return intent;
    let sys = "";
    try { sys = (navigator.language || "").toLowerCase(); } catch (_) {}
    if (!sys) return "zh";
    if (sys.startsWith("zh-tw") || sys.startsWith("zh-hk")) return "zh-TW";
    if (sys.startsWith("zh")) return "zh";      // zh-CN / zh-SG 等归 zh
    if (sys.startsWith("ja")) return "ja";
    if (sys.startsWith("ko")) return "ko";
    if (sys.startsWith("en")) return "en";
    return "zh";
  }
  // 启动探测一次：intent（用户选择）+ lang（解析后具体值）。
  // lang 全程走具体值，auto 解析只发生在启动时 + click 切换时。
  const _intent0 = (() => {
    try {
      const v = localStorage.getItem("lang");
      if (v === "auto" || v === "en" || v === "ja" || v === "ko" || v === "zh-TW") return v;
      return "zh";
    } catch (_) { return "zh"; }
  })();
  const I18N = {
    intent: _intent0,
    lang: _resolveLang(_intent0),
    en: {
      "透传上游（自动发现）": "Passthrough upstreams (auto-discovered)",
      "透传模式开启后，客户端请求自动发现的上游。可重命名，不可删除。": "Upstreams auto-discovered from passthrough requests. Renamable, not deletable.",
      "中继状态": "Relay status",
      "界面与偏好": "Interface & preferences",
      "存储管理": "Storage",
      "上游配置": "Upstream config",
      "快捷切换": "Quick switch",
      "选中的模型可在左下角快速切换为预设": "Selected models can be switched as presets from the bottom-left",
      // 设置页分组标题
      "外观": "Appearance",
      "实时栏管理": "Live panel",
      "数据": "Data",
      "启动": "Start",
      "自动切换": "Auto-switch",
      "动画": "Animations",
      "开发者模式": "Developer",
      // 外观组
      "主题": "Theme",
      "界面的整体配色方案": "Overall color scheme",
      "浅色": "Light",
      "暖白": "Warm",
      "深色": "Dark",
      "语言": "Language",
      "界面显示语言（当前仅设置页生效，全量翻译陆续加入）": "UI language (currently applies to the settings page; full translation coming soon)",
      "简体中文": "简体中文",
      "无边框": "Borderless",
      "开启后总览页所有卡片去掉玻璃容器，裸悬浮在底背景上": "Removes the glass containers from overview cards, leaving them floating on the background",
      "侧栏无边框": "Sidebar borderless",
      "开启后实时流侧栏所有卡片去掉玻璃容器，裸悬浮在底背景上": "Removes glass containers from live-panel cards",
      "实时流侧栏": "Live stream sidebar",
      "开启后右侧出现独立小窗，实时显示当前请求的思考过程和正文，方便调试": "Opens a separate window showing real-time reasoning and stream output for debugging",
      // 实时栏管理组
      "允许并发显示": "Allow concurrent panels",
      "多个并发请求同时各自弹出实时栏": "Each concurrent request gets its own panel",
      "最多显示的并行数量": "Max parallel panels",
      "磁吸状态下始终开启一个": "Always keep one open when docked",
      "磁吸模式下无论有无请求都保留一个空闲实时栏": "In docked mode, keeps one idle panel even without requests",
      "自动延展侧栏": "Auto-extend sidebar",
      "并发请求多时，实时栏窗口自动加宽显示更多并发": "Widens the panel automatically so more concurrent requests are visible",
      "悬浮球": "Floating ball",
      "桌面悬浮球作为侧栏锚点，侧栏从球位置展开": "A desktop floating ball anchors the sidebar, which expands from the ball's position",
      "悬浮球置顶": "Float ball always on top",
      "悬浮球与侧栏始终置顶，不被其它窗口遮挡": "Keeps the floating ball and sidebar on top, never covered by other windows",
      "思考流超时时间": "Thinking timeout",
      "思考流已有内容但无新增，判定为中断的间隔时间": "Seconds without new content before the thinking stream is considered stalled",
      "思考-正文衔接超时时间": "Thinking-to-text gap timeout",
      "收到思考流停止信号，等待正文的超时时间": "Timeout waiting for the text stream after thinking stops",
      "正文流超时时间": "Text stream timeout",
      "正文流已有内容但无新增，判定为中断的间隔时间": "Seconds without new content before the text stream is considered stalled",
      // 数据组
      "保存消息原文与回复": "Save messages",
      "把每次请求的内容和模型回复存进本地数据库，历史页可查看对话；关闭后不再记录新消息": "Stores each request and reply in the local database for the history view; stops recording new messages when off",
      // 启动组
      "开机自启": "Launch at startup",
      "开机登录 Windows 后自动启动中继": "Auto-starts the relay after logging into Windows",
      "启动后隐藏窗口": "Start hidden",
      "登录启动后直接缩到托盘，不弹窗": "Starts minimized to the tray without a window",
      // 自动切换组
      "自动切换 API": "Auto-switch APIs",
      "当前上游额度用完后，自动切换到其他可用的 API": "Switches to another available API when the current upstream quota runs out",
      "允许自动切换的 API": "Switchable APIs",
      "选择可作为切换目标的上游；一个都不勾 = 全部允许": "Pick upstreams allowed as switch targets; none selected = all allowed",
      "高级切换（实验）": "Advanced switching (experimental)",
      // 动画组
      "闪烁": "Flash",
      "调用时闪烁": "Flash on calls",
      "释放闪烁": "Release flash",
      "5小时配额释放动画": "5-hour quota release animation",
      "全页闪烁": "Full-page flash",
      "整个页面为你闪烁": "The whole page flashes for you",
      "高级闪烁功能": "Advanced flash",
      "自定义动画曲线、颜色、粗细和宽度": "Custom animation curve, colors, thickness and width",
      // 开发者模式组
      "隐藏顶部调试栏": "Hide top debug bar",
      "隐藏顶部\"运行中 / 已停止\"指示条，界面更干净": "Hides the top running / stopped indicator bar",
      "内外转换显示": "Show I/O mapping",
      "在顶部状态栏显示\"客户端请求的模型 → 实际调用的模型\"对照": "Shows the client model → actual model mapping in the top status bar",
      // v0.190：开发者模式子项 —— 支持图片的模型（搬到 dev 组后文案）。
      "支持图片的模型": "Vision-capable models",
      "允许 OpenCode 向勾选的模型发图；中继 /models/api.json 会标成 image-capable。点击切换勾选，保存后立即生效。默认全不勾。": "Allow OpenCode to send images to the selected models; relay marks them image-capable in /models/api.json. Click to toggle; save takes effect immediately. Empty by default.",
      // 存储管理区块（v0.113n）
      "存储空间管理": "Storage usage",
      "存储位置管理": "Storage locations",
      "消息数据库 (relay.db)": "Message database (relay.db)",
      "透传数据库 (passthrough.db)": "Passthrough database (passthrough.db)",
      "日志目录": "Log directory",
      "上游配置 (upstreams.json)": "Upstream config (upstreams.json)",
      "消息数据库位置": "Message database location",
      "透传数据库位置": "Passthrough database location",
      "日志目录位置": "Log directory location",
      "上游配置位置": "Upstream config location",
      "修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时需先停止中继。": "Changing a database location updates .env and moves the file; when the relay is running the file is locked, so stop the relay first.",
      "清理消息记录": "Clean up messages",
      "压缩数据库": "Compact database",
      "清空日志": "Clear logs",
      "刷新": "Refresh",
      "修改": "Change",
      // v0.113r：全局 chrome（导航 / 顶栏 / 卡片 / 统计工具栏）
      "总览": "Overview",
      "实时": "Live",
      "历史": "History",
      "上游": "Upstreams",
      "统计": "Stats",
      "设置": "Settings",
      "配置": "Config",
      "本地 AI 用量监控": "Local AI usage monitor",
      "中继": "Relay",
      "用量最大的模型": "Top model",
      "输入": "Input",
      "输出": "Output",
      "缓存": "Cache",
      "Token 消耗 · 近 24h": "Tokens · Last 24h",
      "上游状态": "Upstream status",
      "今日用量": "Today's usage",
      "协议分布": "Protocol breakdown",
      "模型分布": "Model breakdown",
      "最近活动": "Recent activity",
      "正在进行的请求": "Active requests",
      "最近": "Recent",
      "透传模式": "Passthrough mode",
      "Token 消耗树状图": "Token consumption treemap",
      "今日 token 消耗饼图": "Today's token pie",
      "管理卡片": "Manage cards",
      "时间段": "Period",
      "维度": "Dimension",
      "模式": "Mode",
      // v0.113r：状态栏动态文案
      "连接中…": "Connecting…",
      "等待首次轮询": "Waiting for first poll",
      "运行中": "Running",
      "运行中（外部）": "Running (external)",
      "已停止": "Stopped",
      "停止": "Stop",
      "中继未运行": "Relay not running",
      "重启中…": "Restarting…",
      "状态未知": "Status unknown",
      "等待 bridge 响应…": "Waiting for bridge…",
      "等待 bridge 响应": "Waiting for bridge",
      "点击启动以启动中继": "Click to start the relay",
      "端口": "port",
      "非本窗口启动": "not started by this window",
      "（透传模式）": " (passthrough)",
      "运行中（透传模式）": "Running (passthrough)",
      "已停止（透传模式）": "Stopped (passthrough)",
      "正在接管端口": "Taking over port",
      "正在重启…": "Restarting…",
      "请求": "requests",
      "条消息": "messages",
      "条透传请求": "passthrough requests",
      "个文件": "files",
      // v0.113r：按钮
      "重启": "Restart",
      "内外转换": "I/O mapping",
      "内外转换：开": "I/O mapping: on",
      "实时流": "Live stream",
      "实时流：开": "Live stream: on",
      "关闭顶部调试栏": "Hide top debug bar",
      "刷新GUI": "Reload GUI",
      "+ 新建": "+ New",
      "+ 新建上游": "+ New upstream",
      "+ 添加倍率": "+ Add multiplier",
      "+ 新增快捷项": "+ Add quick switch",
      "近 24h": "24h",
      "近 7 天": "7 days",
      "近 30 天": "30 days",
      "按上游": "Upstream",
      "按模型": "Model",
      "仅透传": "Passthrough only",
      "全部": "All",
      "仅转换": "Converted only",
      "全部模型": "All models",
      "测试": "Test",
      "测试中…": "Testing…",
      "连通性测试": "Connectivity test",
      "取消": "Cancel",
      "创建": "Create",
      "保存": "Save",
      "保存高级切换配置": "Save advanced switch config",
      "新增": "Add",
      "编辑": "Edit",
      "删除": "Delete",
      "确定": "OK",
      "复制": "Copy",
      "已复制": "Copied",
      "查询额度": "Query quota",
      "清空": "Clear",
      "无鉴权": "No auth",
      // v0.113r：弹窗 / 配置表单
      "请求详情": "Request detail",
      "按模型拆分详情": "Breakdown by model",
      "快捷切换编辑器": "Quick switch editor",
      "新建上游": "New upstream",
      "预设配置": "Preset",
      "模型": "Model",
      "协议": "Protocol",
      "鉴权头": "Auth header",
      "名称": "Name",
      "允许的模型": "Allowed models",
      "计费模式": "Billing mode",
      "Token 计费字段": "Token billing fields",
      "模型倍率": "Model multiplier",
      "5h 限额": "5h limit",
      "周限额": "Weekly limit",
      "月限额": "Monthly limit",
      "备注": "Note",
      "选预设后只需填名称和 API Key，其余自动配好": "Pick a preset and fill in name + API key; the rest is auto-configured",
      "自动填名称与允许列表": "Auto-fills the name and allowed list",
      "上游说的协议；不选 = 自动跟随客户端入口": "Protocol the upstream speaks; blank = follow the client's entry protocol",
      "发给上游的认证格式；不选 = 跟随协议默认（Anthropic → x-api-key，OpenAI → Bearer）": "Auth format sent upstream; blank = protocol default (Anthropic → x-api-key, OpenAI → Bearer)",
      "唯一，且不能叫 \"active\"": "Unique, and cannot be \"active\"",
      "上游 API endpoint": "Upstream API endpoint",
      "明文保存到 upstreams.json，留空跳过": "Stored in plaintext in upstreams.json; leave blank to skip",
      "留空 = 不限制。回车添加": "Blank = unlimited. Press Enter to add",
      "未列出的模型按 1× 计": "Unlisted models count as 1×",
      "留空 = 不限制": "Blank = unlimited",
      "仅 opencode-go 用：自动换 x-api-key、模型小写、剥 thinking": "opencode-go only: swaps x-api-key, lowercases model names, strips thinking",
      "可选，会显示在上游详情卡片底部": "Optional; shown at the bottom of the upstream detail card",
      "input_tokens（输入）": "input_tokens (input)",
      "output_tokens（输出）": "output_tokens (output)",
      "cache_read_input_tokens（缓存命中读取）": "cache_read_input_tokens (cache read)",
      "cache_creation_input_tokens（缓存写入）": "cache_creation_input_tokens (cache write)",
      "高级功能（计费 / 倍率 / 限额 / 协议适配）": "Advanced (billing / multipliers / limits / protocol)",
      // v0.113r：空态
      "加载上游配置…": "Loading upstream config…",
      "加载中…": "Loading…",
      "暂无进行中的请求": "No active requests",
      "暂无记录": "No records",
      "透传模式不使用配置型上游；请在\"设置 → 透传模式\"查看自动发现的上游。": "Passthrough mode doesn't use configured upstreams; see Settings → Passthrough for auto-discovered ones.",
      "暂无数据": "No data",
      "无数据": "No data",
      "合计": "Total",
      "暂无": "None",
      "未配置上游": "No upstream configured",
      "暂无上游配置": "No upstream config",
      "暂无快捷项，点下方按钮添加": "No quick-switch items; add one with the button below",
      // v0.113r：空态补全（stats 空图 / 上游详情 / 图表库 / 加载失败）
      "暂无请求": "No requests",
      "暂无最近请求": "No recent requests",
      "近 24h 暂无请求": "No requests in the last 24h",
      "加载失败": "Failed to load",
      "桥不可用": "Bridge unavailable",
      "该上游暂无调用记录": "No call records for this upstream",
      "图表库未加载（vendor/chart.umd.min.js）": "Chart library not loaded (vendor/chart.umd.min.js)",
      "暂无上游": "No upstream",
      // v0.113r：存储卡片（无后缀名）+ 报错分析
      "报错分析": "Error analysis",
      "允许小模型分析报错信息": "Let a small model classify errors",
      "出现报错时把错误信息发给所选模型，判断错误类型并给出中文提示（余额不足 / 网络错误 / 达到次数限制等）。": "On error, send the error info to the selected model to classify the type and produce a short Chinese hint (out of balance / network error / rate limit, etc.).",
      "分析模型": "Analysis model",
      "建议选择轻量快速的小模型；留空则使用默认上游的兜底模型。": "Pick a lightweight, fast small model; leave blank to use the default upstream fallback model.",
      "「测试」用一条示例 429 报错真实跑一次分类，验证模型与提示词。保存后立即生效，无需重启中继。": "\"Test\" runs a real classification on a sample 429 error to verify the model and prompt. Saves take effect immediately, no relay restart needed.",
      "消息数据库": "Message database",
      "透传数据库": "Passthrough database",
      "「清理」「压缩」需中继运行中执行。修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时，需先停止中继再迁移。": "Clean / compact require the relay to be running. Changing a database location writes .env and moves the file; when the relay is running the file is locked, so stop the relay before migrating.",
      "压缩": "Compact",
      "修改位置": "Move",
      "清理透传记录": "Clean passthrough records",
      "settings-group.startup": "Startup",
      // v0.113r (sweep)：总览 + 历史表头 + 弹窗选项 + flash 编辑器
      "总计": "Total",
      "请求数": "Requests",
      "输入 tokens": "Input tokens",
      "输出 tokens": "Output tokens",
      "错误": "Errors",
      "上游详情": "Upstream details",
      "界面与偏好": "Interface & preferences",
      "时间": "Time",
      "平台": "Platform",
      "模型": "Model",
      "上游": "Upstream",
      "状态": "Status",
      "入": "In",
      "出": "Out",
      "加载失败（未知错误），继续滚动重试": "Failed to load (unknown error); scroll to retry",
      "近 30 天 · 按上游": "30 days Upstream",
      "近 30 天 · 按模型": "30 days Model",
      "近 7 天 · 按上游": "7 days Upstream",
      "近 7 天 · 按模型": "7 days Model",
      "近 24h · 按上游": "24h Upstream",
      "近 24h · 按模型": "24h Model",
      " · 仅透传": " · passthrough only",
      " · 全部": " · all",
      "转换模式": "Relay mode",
      "透传模式": "Passthrough mode",
      "运行状态": "Status",
      "已停止": "Stopped",
      "进程 PID": "PID",
      "监听端口": "Port",
      "启动来源": "Started by",
      "本窗口": "This window",
      "外部进程": "External process",
      "日志目录": "Log directory",
      "数据快照": "Snapshot",
      "额度查询": "Quota check",
      "缓动曲线 (cubic-bezier)": "Easing curve (cubic-bezier)",
      "颜色 / 粗细 / 宽度": "Color / thickness / width",
      "粗细": "Thickness",
      "宽度": "Width",
      "增长色": "Rising color",
      "留空=主题色": "blank = theme color",
      "释放色": "Falling color",
      "留空=绿色": "blank = green",
      "加载中…": "Loading…",
      "接入地址": "Endpoint",
      "两种模式客户端都连": "Both modes connect to",
      "同一个中继地址": "the same relay address",
      "，只差 api-key 的填法（见下）。按客户端协议选入口：Anthropic 系用": " — only the api-key format differs (see below). Pick the entry by client protocol: Anthropic series uses",
      "，OpenAI 系用": ", OpenAI series uses",
      "base_url（Anthropic 客户端）": "base_url (Anthropic clients)",
      "base_url（OpenAI 客户端）": "base_url (OpenAI clients)",
      "复制": "Copy",
      "当前模式": "Current mode",
      "开关在「设置 → 中继模式」": "Toggle in \"Settings → Relay mode\"",
      "默认 · 推荐": "Default · recommended",
      "是什么：": "What it is:",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填": "The relay does \"routing + protocol conversion\": requests go through the active upstream and usage is recorded. Fill api-key with",
      "或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": " or leave it blank — the relay auto-routes to the active upstream. Fill in a real upstream key to go directly to that upstream.",
      "设置 → 上游配置": "Settings → Upstream config",
      "：添加上游并选好「active」。": ": add an upstream and pick \"active\".",
      "2. 客户端 base_url 指到上面的地址，api-key 填": "2. Point the client's base_url to the address above, and fill api-key with",
      "Codex / OpenAI 客户端": "Codex / OpenAI clients",
      "完全透传模式": "Full passthrough mode",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的": "The relay does not convert or route by active — it forwards your request as-is to the address in api-key:",
      "目标地址": "target address",
      "原样直达 —— 连设置里没配过的上游也能直接用。api-key 格式：": ", as-is — even upstreams not configured in Settings work directly. api-key format:",
      "目标地址@@上游key": "target@@upstream_key",
      "设置 → 中继模式": "Settings → Relay mode",
      "：切到「完全透传」。": ": switch to \"Full passthrough\".",
      "2. 客户端照旧连上面的中继地址，但 api-key 填": "2. The client still connects to the address above, but fill api-key with",
      "，请求路径随便。": " — request path is up to you.",
      "透传 curl 示例": "Passthrough curl example",
      "高级：当前上游 · 快捷切换 · 已配置上游": "Advanced: active upstream · quick switch · configured upstreams",
      "当前 active 上游": "Current active upstream",
      "「auto」请求会路由到这里。": "\"auto\" requests route here.",
      "尚未配置 active 上游": "No active upstream configured",
      "已配置上游": "Configured upstreams",
      "点击任意上游行立即切换为该平台的 active 上游。": "Click any upstream row to switch it to be that platform's active upstream.",
      "未配置上游 —— 到「设置 → 上游配置」新建": "No upstreams — create one in \"Settings → Upstream config\"",
      // 弹窗（create-overlay）选项 + 标签
      "自定义（手动填写全部字段）": "Custom (fill all fields manually)",
      "DeepSeek 官方（Anthropic 协议）": "DeepSeek official (Anthropic protocol)",
      "OpenCode Go 订阅（Anthropic 协议）": "OpenCode Go subscription (Anthropic protocol)",
      "OpenCode Zen 按量付费（OpenAI 协议）": "OpenCode Zen pay-as-you-go (OpenAI protocol)",
      "OpenCode Zen 免费模型（OpenAI 协议）": "OpenCode Zen free models (OpenAI protocol)",
      "HuoShan Agent Plan（Anthropic 协议）": "HuoShan Agent Plan (Anthropic protocol)",
      "— 请选择模型 —": "— pick a model —",
      "按次数计费": "Per-request billing",
      "按 Token 计费": "Per-token billing",
      "OpenCode Go 协议适配": "OpenCode Go protocol adaptation",
      "测试日志": "Test log",
      // v0.113s：quota block 单位 + 状态文案
      "次": "calls",
      "tokens": "tokens",
      "额度已用尽": "Quota exhausted",
      "预计": "ETA ",
      "用尽": "until exhaustion",
      "非允许模型": "Disallowed models",
      "周": "week",
      "月": "month",
      "5h": "5h",
      "累计 tokens": "Total tokens",
      "5h 释放": "5h release",
      "删除这条上游配置": "Delete this upstream config",
      "点击激活此上游；已是当前上游时点击进入编辑": "Click to activate this upstream; click again to edit when it's already active",
      // v0.152：配置页「指南」文案重写 —— JSON 对照 + 为什么用 @@ 流程图
      "转换模式": "Conversion mode",
      "默认 · 推荐": "Default · recommended",
      "是什么：": "What it is:",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填 auto 或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": "The relay handles routing + protocol conversion; requests are forwarded via the active upstream and usage is recorded. Set api-key to auto or leave blank to use the active upstream; set it to a specific upstream's real key to pin to that upstream.",
      "设置 → 上游配置": "Settings → Upstreams",
      "：添加上游并选好「active」。": ": add an upstream and mark it active.",
      "客户端 base_url 指到上面的地址，api-key 填 auto。": "Point the client's base_url at the address above, set api-key to auto.",
      "官方 Claude Code settings.json": "Official Claude Code settings.json",
      "走中继（仅改 2 行）": "Via relay (only 2 lines change)",
      "完全透传模式": "Pure passthrough mode",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的目标地址原样直达 —— 连设置里没配过的上游也能直接用。": "The relay does not convert or route — it forwards requests verbatim to the destination URL embedded in your api-key. Upstreams not configured in Settings work too.",
      "为什么用 @@ 格式？": "Why the @@ format?",
      "透传下 base_url 仍要指向中继（这样中继才能记账），这会占用原本指向提供商上游的 url 字段。解决办法：把上游 url 和 api-key 都塞进 api-key 一栏，格式 = 目标地址 + @@ + 真实 key。": "In passthrough mode the base_url must still point to the relay (so usage is recorded). That leaves no place for the upstream URL. Workaround: pack both upstream URL and api-key into the api-key field as upstream-url + @@ + real-key.",
      "1. 客户端发": "1. Client sends",
      "base_url = 中继地址": "base_url = relay URL",
      "2. 中继拆": "2. Relay splits",
      "拆出": "Extracts",
      "url = 上游地址": "url = upstream URL",
      "原 key": "original key",
      "3. 上游收": "3. Upstream receives",
      "转发到上游": "Forwards to upstream",
      "请求直达，不路由": "Direct, no routing",
      "走中继（透传）": "Via relay (passthrough)",
      "高级：curl 原生调用": "Advanced: raw curl",
      "curl /v1/chat/completions（OpenAI 入口 → DeepSeek）": "curl /v1/chat/completions (OpenAI entry → DeepSeek)",
      "curl /v1/messages（Anthropic 入口 → DeepSeek Anthropic 兼容）": "curl /v1/messages (Anthropic entry → DeepSeek Anthropic-compatible)",
      // v0.152 二改：极简字段列表替代流程图
      "模型、上游全由中继在「设置 → 上游配置」里选，客户端只填这 3 个字段：": "Model and upstream are chosen by the relay in Settings → Upstreams; the client only fills these 3 fields:",
      "Anthropic 客户端": "Anthropic client",
      "OpenAI 客户端": "OpenAI client",
      "上游地址": "upstream URL",
      "真实 key": "real key",
      "实际模型": "actual model",
      "示例：Claude Code settings.json（官方 → 走中继）": "Example: Claude Code settings.json (official → via relay)",
      "官方": "Official",
      "走中继": "Via relay",
      // v0.152 终版：贴 README.docx 原文（关键术语拆出 <strong>，其余整句进 dict）
      "转换模式下，模型、上游的选择完全由中继控制，来自 coding 客户端的所有请求，先到达中继，由中继接管，并向中继中选择的上游发送。": "In conversion mode, model and upstream selection is fully controlled by the relay. All requests from the coding client reach the relay first, are taken over by the relay, and are sent to the upstream selected in the relay.",
      "客户端配置只需要将 URL 地址指向中继，然后将 api-key 和模型配置为": "The client only needs to point its URL at the relay, then set api-key and model to",
      "占位符即可。": " placeholder.",
      "客户端配置：": "Client config:",
      "透传模式下，为了统计 token 和保存数据，仍然需要让数据包经过中继，因而 base_url 仍需指向中继。": "In passthrough mode, traffic must still pass through the relay (to count tokens and save data), so base_url still points at the relay.",
      "这会占用原本指向提供商上游的 url 的位置，对于此问题的解决办法是：将": "This takes over the field originally meant for the upstream URL. Solution: put",
      "上游 url": "upstream URL",
      "和": " and ",
      "都写在 api-key 的填写位置，采用如下格式：": " into the api-key field, in the following format:",
      "此数据包到达中继后，中继会从 AUTH_TOKEN ( Api_Key ) 中解析字符，将 Api_Key 字段重写为“@@”标识符后半部分的 key，然后向前半部分 URL 发送数据包。": "When the packet reaches the relay, it parses AUTH_TOKEN ( Api_Key ), rewrites the Api_Key field to the part after the @@ separator, then sends the packet to the URL in the first half.",
      "示例：": "Example:",
      "Deepseek 的请求地址（来自官网信息）": "DeepSeek request address (from the official docs)",
      "（示例）": " (example)",
      "不走中继时的请求填写（以 claude code 为例）：": "Without the relay (Claude Code as example):",
      "不走中继时的请求填写": "Without the relay",
      "走中继时，该请求需要改写为：": "With the relay, rewrite it as:",
      "走中继时的请求填写": "With the relay",
      // v0.153：relay 状态卡补充 key（查询额度按钮态 / quota 结果 / 系统信息行）
      "查询中…": "Querying…",
      "不可用": "Unavailable",
      "滚动": "rolling",
      "余额": "Balance",
      "失败": "Failed",
      "磁盘": "Disk",
      "内存": "Memory",
      "网络": "Network",
      // v0.154：cfg / stats / overview 补 key
      "地址 URL": "URL",
      "auto 兜底模型": "auto fallback model",
      "客户端发 model=\"auto\" 时用此值替换。留空则按允许模型列表的第一个,再不行原样透传": "Sent when the client requests model=\"auto\". If empty, falls back to the first entry in the allowed-model list; if that also fails, the request passes through unchanged.",
      "5 小时额度": "5-hour quota",
      "输入模型名后回车": "Type a model name and press Enter",
      "链接上游（合并统计）": "Linked upstreams (combined stats)",
      "链接后所有统计页视为一个虚拟上游；不影响路由和计费；A+B 与 A+C 自动并为 A+B+C": "Linked upstreams appear as one virtual upstream on every stats page; routing and billing are unaffected; A+B and A+C automatically merge into A+B+C.",
      "+ 新建链接": "+ New link",
      "任意模型": "Any model",
      "最大模型": "Top model",
      "单模型 30 天调用分布": "Single-model 30-day calls",
      "小": "S",
      "中": "M",
      "大": "L",
      "隐藏整个顶栏（运行状态 + 按钮行），界面更干净；可从顶栏按钮或本开关恢复": "Hides the entire top bar (running state + buttons) for a cleaner UI; restore from the top-bar button or this toggle.",
      "（未配置上游模型）": "(no upstream model configured)",
      "释放": "release",
      "调用": "calls",
    },
    "zh-TW": {
      "透传上游（自动发现）": "透傳上游（自動發現）",
      "透传模式开启后，客户端请求自动发现的上游。可重命名，不可删除。": "透傳模式開啟後，用戶端請求自動發現的上游。可重新命名，不可刪除。",
      "中继状态": "中繼狀態",
      "界面与偏好": "介面與偏好",
      "存储管理": "儲存管理",
      "上游配置": "上游設定",
      "快捷切换": "快速切換",
      "选中的模型可在左下角快速切换为预设": "選中的模型可在左下角快速切換為預設",
      "外观": "外觀",
      "实时栏管理": "即時欄管理",
      "数据": "資料",
      "启动": "啟動",
      "自动切换": "自動切換",
      "动画": "動畫",
      "开发者模式": "開發者模式",
      "主题": "主題",
      "界面的整体配色方案": "介面的整體配色方案",
      "浅色": "淺色",
      "暖白": "暖白",
      "深色": "深色",
      "语言": "語言",
      "界面显示语言（当前仅设置页生效，全量翻译陆续加入）": "介面顯示語言（目前僅設定頁生效，全量翻譯陸續加入）",
      "简体中文": "简体中文",
      "无边框": "無邊框",
      "开启后总览页所有卡片去掉玻璃容器，裸悬浮在底背景上": "開啟後總覽頁所有卡片去掉玻璃容器，裸懸浮在底背景上",
      "侧栏无边框": "側欄無邊框",
      "开启后实时流侧栏所有卡片去掉玻璃容器，裸悬浮在底背景上": "開啟後即時流側欄所有卡片去掉玻璃容器，裸懸浮在底背景上",
      "实时流侧栏": "即時流側欄",
      "开启后右侧出现独立小窗，实时显示当前请求的思考过程和正文，方便调试": "開啟後右側出現獨立小窗，即時顯示當前請求的思考過程和正文，方便偵錯",
      "允许并发显示": "允許並顯示",
      "多个并发请求同时各自弹出实时栏": "多個並發請求同時各自彈出即時欄",
      "最多显示的并行数量": "最多顯示的並行數量",
      "磁吸状态下始终开启一个": "磁吸狀態下始終開啟一個",
      "磁吸模式下无论有无请求都保留一个空闲实时栏": "磁吸模式下無論有無請求都保留一個閒置即時欄",
      "自动延展侧栏": "自動延展側欄",
      "并发请求多时，实时栏窗口自动加宽显示更多并发": "並發請求多時，即時欄視窗自動加寬顯示更多並發",
      "悬浮球": "懸浮球",
      "桌面悬浮球作为侧栏锚点，侧栏从球位置展开": "以桌面懸浮球為側欄錨點，側欄從球位置展開",
      "悬浮球置顶": "懸浮球置頂",
      "悬浮球与侧栏始终置顶，不被其它窗口遮挡": "讓懸浮球與側欄保持置頂，不被其他視窗遮擋",
      "思考流超时时间": "思考流超時時間",
      "思考流已有内容但无新增，判定为中断的间隔时间": "思考流已有內容但無新增，判定為中斷的間隔時間",
      "思考-正文衔接超时时间": "思考-正文銜接超時時間",
      "收到思考流停止信号，等待正文的超时时间": "收到思考流停止訊號，等待正文的超時時間",
      "正文流超时时间": "正文流超時時間",
      "正文流已有内容但无新增，判定为中断的间隔时间": "正文流已有內容但無新增，判定為中斷的間隔時間",
      "保存消息原文与回复": "儲存訊息原文與回覆",
      "把每次请求的内容和模型回复存进本地数据库，历史页可查看对话；关闭后不再记录新消息": "把每次請求的內容和模型回覆存進本地資料庫，歷史頁可查看對話；關閉後不再記錄新訊息",
      "开机自启": "開機自啟",
      "开机登录 Windows 后自动启动中继": "開機登入 Windows 後自動啟動中繼",
      "启动后隐藏窗口": "啟動後隱藏視窗",
      "登录启动后直接缩到托盘，不弹窗": "登入啟動後直接縮到系統匣，不彈窗",
      "自动切换 API": "自動切換 API",
      "当前上游额度用完后，自动切换到其他可用的 API": "當前上游額度用完後，自動切換到其他可用的 API",
      "允许自动切换的 API": "允許自動切換的 API",
      "选择可作为切换目标的上游；一个都不勾 = 全部允许": "選擇可作為切換目標的上游；一個都不勾 = 全部允許",
      "高级切换（实验）": "進階切換（實驗）",
      "闪烁": "閃爍",
      "调用时闪烁": "呼叫時閃爍",
      "释放闪烁": "釋放閃爍",
      "5小时配额释放动画": "5 小時配額釋放動畫",
      "全页闪烁": "全頁閃爍",
      "整个页面为你闪烁": "整個頁面為你閃爍",
      "高级闪烁功能": "進階閃爍功能",
      "自定义动画曲线、颜色、粗细和宽度": "自訂動畫曲線、顏色、粗細和寬度",
      "隐藏顶部调试栏": "隱藏頂部偵錯欄",
      "内外转换显示": "內外轉換顯示",
      // v0.190：开发者模式子项 —— 支持图片的模型。
      "支持图片的模型": "支援圖片的模型",
      "允许 OpenCode 向勾选的模型发图；中继 /models/api.json 会标成 image-capable。点击切换勾选，保存后立即生效。默认全不勾。": "允許 OpenCode 向勾選的模型發送圖片；中繼 /models/api.json 會標成 image-capable。點擊切換勾選，儲存後立即生效。預設全不勾選。",
      "存储空间管理": "儲存空間管理",
      "存储位置管理": "儲存位置管理",
      "消息数据库 (relay.db)": "訊息資料庫 (relay.db)",
      "透传数据库 (passthrough.db)": "透傳資料庫 (passthrough.db)",
      "日志目录": "日誌目錄",
      "上游配置 (upstreams.json)": "上游設定 (upstreams.json)",
      "消息数据库位置": "訊息資料庫位置",
      "透传数据库位置": "透傳資料庫位置",
      "日志目录位置": "日誌目錄位置",
      "上游配置位置": "上游設定位置",
      "修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时需先停止中继。": "修改資料庫位置會寫入 .env 並搬移檔案；中繼執行中檔案被佔用時需先停止中繼。",
      "清理消息记录": "清理訊息記錄",
      "压缩数据库": "壓縮資料庫",
      "清空日志": "清空日誌",
      "刷新": "重新整理",
      "修改": "修改",
      "总览": "總覽",
      "实时": "即時",
      "历史": "歷史",
      "上游": "上游",
      "统计": "統計",
      "设置": "設定",
      "配置": "設定",
      "本地 AI 用量监控": "本地 AI 用量監控",
      "中继": "中繼",
      "用量最大的模型": "用量最大的模型",
      "输入": "輸入",
      "输出": "輸出",
      "缓存": "快取",
      "Token 消耗 · 近 24h": "Token 消耗 · 近 24 小時",
      "上游状态": "上游狀態",
      "今日用量": "今日用量",
      "协议分布": "協議分佈",
      "模型分布": "模型分佈",
      "最近活动": "最近活動",
      "正在进行的请求": "正在進行的請求",
      "最近": "最近",
      "透传模式": "透傳模式",
      "Token 消耗树状图": "Token 消耗樹狀圖",
      "今日 token 消耗饼图": "今日 token 消耗圓餅圖",
      "管理卡片": "管理卡片",
      "时间段": "時間段",
      "维度": "維度",
      "模式": "模式",
      "连接中…": "連線中…",
      "等待首次轮询": "等待首次輪詢",
      "运行中": "執行中",
      "运行中（外部）": "執行中（外部）",
      "已停止": "已停止",
      "停止": "停止",
      "中继未运行": "中繼未執行",
      "重启中…": "重啟中…",
      "状态未知": "狀態未知",
      "等待 bridge 响应…": "等待 bridge 回應…",
      "等待 bridge 响应": "等待 bridge 回應",
      "点击启动以启动中继": "點擊啟動以啟動中繼",
      "端口": "連接埠",
      "非本窗口启动": "非本視窗啟動",
      "（透传模式）": "（透傳模式）",
      "运行中（透传模式）": "執行中（透傳模式）",
      "已停止（透传模式）": "已停止（透傳模式）",
      "正在接管端口": "正在接管連接埠",
      "正在重启…": "正在重啟…",
      "请求": "請求",
      "条消息": "則訊息",
      "条透传请求": "則透傳請求",
      "个文件": "個檔案",
      "重启": "重啟",
      "内外转换": "內外轉換",
      "内外转换：开": "內外轉換：開",
      "实时流": "即時流",
      "实时流：开": "即時流：開",
      "关闭顶部调试栏": "關閉頂部偵錯欄",
      "刷新GUI": "重新整理 GUI",
      "+ 新建": "+ 新建",
      "+ 新建上游": "+ 新建上游",
      "+ 添加倍率": "+ 加入倍率",
      "+ 新增快捷项": "+ 新增快速項",
      "近 24h": "近 24 小時",
      "近 7 天": "近 7 天",
      "近 30 天": "近 30 天",
      "按上游": "按上游",
      "按模型": "按模型",
      "仅透传": "僅透傳",
      "全部": "全部",
      "仅转换": "僅轉換",
      "全部模型": "全部模型",
      "测试": "測試",
      "测试中…": "測試中…",
      "连通性测试": "連通性測試",
      "取消": "取消",
      "创建": "建立",
      "保存": "儲存",
      "保存高级切换配置": "儲存進階切換設定",
      "新增": "新增",
      "编辑": "編輯",
      "删除": "刪除",
      "确定": "確定",
      "复制": "複製",
      "已复制": "已複製",
      "查询额度": "查詢額度",
      "清空": "清空",
      "无鉴权": "無驗證",
      "请求详情": "請求詳情",
      "按模型拆分详情": "按模型拆分詳情",
      "快捷切换编辑器": "快速切換編輯器",
      "新建上游": "新建上游",
      "预设配置": "預設設定",
      "模型": "模型",
      "协议": "協定",
      "鉴权头": "驗證標頭",
      "名称": "名稱",
      "允许的模型": "允許的模型",
      "计费模式": "計費模式",
      "Token 计费字段": "Token 計費欄位",
      "模型倍率": "模型倍率",
      "5h 限额": "5 小時限額",
      "周限额": "週限額",
      "月限额": "月限額",
      "备注": "備註",
      "选预设后只需填名称和 API Key，其余自动配好": "選預設後只需填名稱與 API Key，其餘自動配好",
      "自动填名称与允许列表": "自動填名稱與允許清單",
      "上游说的协议；不选 = 自动跟随客户端入口": "上游使用的協定；不選 = 自動跟隨用戶端入口",
      "发给上游的认证格式；不选 = 跟随协议默认（Anthropic → x-api-key，OpenAI → Bearer）": "發給上游的認證格式；不選 = 跟隨協定預設（Anthropic → x-api-key，OpenAI → Bearer）",
      "上游 API endpoint": "上游 API endpoint",
      "明文保存到 upstreams.json，留空跳过": "明文儲存到 upstreams.json，留空跳過",
      "留空 = 不限制。回车添加": "留空 = 不限制。按 Enter 加入",
      "未列出的模型按 1× 计": "未列出的模型按 1× 計",
      "留空 = 不限制": "留空 = 不限制",
      "仅 opencode-go 用：自动换 x-api-key、模型小写、剥 thinking": "僅 opencode-go 用：自動換 x-api-key、模型小寫、剝 thinking",
      "可选，会显示在上游详情卡片底部": "選填，會顯示在上游詳情卡片底部",
      "input_tokens（输入）": "input_tokens（輸入）",
      "output_tokens（输出）": "output_tokens（輸出）",
      "cache_read_input_tokens（缓存命中读取）": "cache_read_input_tokens（快取命中讀取）",
      "cache_creation_input_tokens（缓存写入）": "cache_creation_input_tokens（快取寫入）",
      "高级功能（计费 / 倍率 / 限额 / 协议适配）": "進階功能（計費 / 倍率 / 限額 / 協定適配）",
      "加载上游配置…": "載入上游設定…",
      "加载中…": "載入中…",
      "暂无进行中的请求": "暫無進行中的請求",
      "暂无记录": "暫無記錄",
      "暂无数据": "暫無資料",
      "无数据": "暫無資料",
      "合计": "合計",
      "暂无": "暫無",
      "未配置上游": "未設定上游",
      "暂无上游配置": "暫無上游設定",
      "暂无快捷项，点下方按钮添加": "暫無快速項，點下方按鈕新增",
      "暂无请求": "暫無請求",
      "暂无最近请求": "暫無最近請求",
      "近 24h 暂无请求": "近 24 小時暫無請求",
      "加载失败": "載入失敗",
      "桥不可用": "橋接不可用",
      "该上游暂无调用记录": "該上游暫無呼叫記錄",
      "图表库未加载（vendor/chart.umd.min.js）": "圖表庫未載入（vendor/chart.umd.min.js）",
      "暂无上游": "暫無上游",
      "报错分析": "錯誤分析",
      "允许小模型分析报错信息": "允許小模型分析錯誤資訊",
      "出现报错时把错误信息发给所选模型，判断错误类型并给出中文提示（余额不足 / 网络错误 / 达到次数限制等）。": "出現錯誤時把錯誤資訊發給所選模型，判斷錯誤類型並給出中文提示（餘額不足 / 網路錯誤 / 達到次數限制等）。",
      "分析模型": "分析模型",
      "建议选择轻量快速的小模型；留空则使用默认上游的兜底模型。": "建議選擇輕量快速的小模型；留空則使用預設上游的兜底模型。",
      "「测试」用一条示例 429 报错真实跑一次分类，验证模型与提示词。保存后立即生效，无需重启中继。": "「測試」用一條範例 429 錯誤真實跑一次分類，驗證模型與提示詞。儲存後立即生效，無需重啟中繼。",
      "消息数据库": "訊息資料庫",
      "透传数据库": "透傳資料庫",
      "「清理」「压缩」需中继运行中执行。修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时，需先停止中继再迁移。": "「清理」「壓縮」需中繼執行中執行。修改資料庫位置會寫入 .env 並搬移檔案；中繼執行中檔案被佔用時，需先停止中繼再遷移。",
      "压缩": "壓縮",
      "修改位置": "修改位置",
      "清理透传记录": "清理透傳記錄",
      "settings-group.startup": "settings-group.startup",
      "总计": "總計",
      "请求数": "請求數",
      "输入 tokens": "輸入 tokens",
      "输出 tokens": "輸出 tokens",
      "错误": "錯誤",
      "上游详情": "上游詳情",
      "界面与偏好": "介面與偏好",
      "时间": "時間",
      "平台": "平台",
      "模型": "模型",
      "上游": "上游",
      "状态": "狀態",
      "入": "入",
      "出": "出",
      "加载失败（未知错误），继续滚动重试": "載入失敗（未知錯誤），繼續捲動重試",
      "近 30 天 · 按上游": "近 30 天 · 按上游",
      "近 30 天 · 按模型": "近 30 天 · 按模型",
      "近 7 天 · 按上游": "近 7 天 · 按上游",
      "近 7 天 · 按模型": "近 7 天 · 按模型",
      "近 24h · 按上游": "近 24 小時 · 按上游",
      "近 24h · 按模型": "近 24 小時 · 按模型",
      " · 仅透传": " · 僅透傳",
      " · 全部": " · 全部",
      "转换模式": "轉換模式",
      "透传模式": "透傳模式",
      "运行状态": "執行狀態",
      "已停止": "已停止",
      "进程 PID": "行程 PID",
      "监听端口": "監聽連接埠",
      "启动来源": "啟動來源",
      "本窗口": "本視窗",
      "外部进程": "外部行程",
      "日志目录": "日誌目錄",
      "数据快照": "資料快照",
      "额度查询": "額度查詢",
      "缓动曲线 (cubic-bezier)": "緩動曲線 (cubic-bezier)",
      "颜色 / 粗细 / 宽度": "顏色 / 粗細 / 寬度",
      "粗细": "粗細",
      "宽度": "寬度",
      "增长色": "增長色",
      "留空=主题色": "留空=主題色",
      "释放色": "釋放色",
      "留空=绿色": "留空=綠色",
      "加载中…": "載入中…",
      "接入地址": "接入地址",
      "两种模式客户端都连": "兩種模式用戶端都連",
      "同一个中继地址": "同一個中繼地址",
      "，只差 api-key 的填法（见下）。按客户端协议选入口：Anthropic 系用": "，只差 api-key 的填法（見下）。按用戶端協定選入口：Anthropic 系用",
      "，OpenAI 系用": "，OpenAI 系用",
      "base_url（Anthropic 客户端）": "base_url（Anthropic 用戶端）",
      "base_url（OpenAI 客户端）": "base_url（OpenAI 用戶端）",
      "复制": "複製",
      "当前模式": "當前模式",
      "开关在「设置 → 中继模式」": "開關在「設定 → 中繼模式」",
      "默认 · 推荐": "預設 · 推薦",
      "是什么：": "是什麼：",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填": "中繼做「路由 + 協定轉換」，請求經 active 上游轉發、用量計入統計。api-key 填",
      "或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": "或留空 = 中繼自動走 active 上游；填某上游的真實 key = 指名直連該上游。",
      "设置 → 上游配置": "設定 → 上游設定",
      "：添加上游并选好「active」。": "：加入上游並選好「active」。",
      "2. 客户端 base_url 指到上面的地址，api-key 填": "2. 用戶端 base_url 指到上面的地址，api-key 填",
      "Codex / OpenAI 客户端": "Codex / OpenAI 用戶端",
      "完全透传模式": "完全透傳模式",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的": "中繼不做轉換、不按 active 路由，把請求按你寫進 api-key 的",
      "目标地址": "目標地址",
      "原样直达 —— 连设置里没配过的上游也能直接用。api-key 格式：": "原樣直達 —— 連設定裡沒配過的上游也能直接用。api-key 格式：",
      "目标地址@@上游key": "目標地址@@上游key",
      "设置 → 中继模式": "設定 → 中繼模式",
      "：切到「完全透传」。": "：切到「完全透傳」。",
      "2. 客户端照旧连上面的中继地址，但 api-key 填": "2. 用戶端照舊連上面的中繼地址，但 api-key 填",
      "，请求路径随便。": "，請求路徑隨便。",
      "透传 curl 示例": "透傳 curl 範例",
      "高级：当前上游 · 快捷切换 · 已配置上游": "進階：當前上游 · 快速切換 · 已設定上游",
      "当前 active 上游": "當前 active 上游",
      "「auto」请求会路由到这里。": "「auto」請求會路由到這裡。",
      "尚未配置 active 上游": "尚未設定 active 上游",
      "已配置上游": "已設定上游",
      "点击任意上游行立即切换为该平台的 active 上游。": "點擊任意上游行立即切換為該平台的 active 上游。",
      "未配置上游 —— 到「设置 → 上游配置」新建": "未設定上游 —— 到「設定 → 上游設定」新建",
      "自定义（手动填写全部字段）": "自訂（手動填寫全部欄位）",
      "DeepSeek 官方（Anthropic 协议）": "DeepSeek 官方（Anthropic 協定）",
      "OpenCode Go 订阅（Anthropic 协议）": "OpenCode Go 訂閱（Anthropic 協定）",
      "OpenCode Zen 按量付费（OpenAI 协议）": "OpenCode Zen 按量計費（OpenAI 協定）",
      "OpenCode Zen 免费模型（OpenAI 协议）": "OpenCode Zen 免費模型（OpenAI 協定）",
      "HuoShan Agent Plan（Anthropic 协议）": "HuoShan Agent Plan（Anthropic 協定）",
      "— 请选择模型 —": "— 請選擇模型 —",
      "按次数计费": "按次數計費",
      "按 Token 计费": "按 Token 計費",
      "OpenCode Go 协议适配": "OpenCode Go 協定適配",
      "测试日志": "測試日誌",
      "次": "次",
      "tokens": "tokens",
      "额度已用尽": "額度已用盡",
      "预计": "預計",
      "用尽": "用盡",
      "非允许模型": "非允許模型",
      "周": "週",
      "月": "月",
      "5h": "5 小時",
      "累计 tokens": "累計 tokens",
      "5h 释放": "5 小時釋放",
      "删除这条上游配置": "刪除這條上游設定",
      "点击激活此上游；已是当前上游时点击进入编辑": "點擊啟動此上游；已是當前上游時點擊進入編輯",
      // v0.152：配置頁「指南」文案重寫 —— JSON 對照 + 為什麼用 @@ 流程圖
      "转换模式": "轉換模式",
      "默认 · 推荐": "預設 · 推薦",
      "是什么：": "是什麼：",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填 auto 或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": "中繼做「路由 + 協定轉換」，請求經 active 上游轉發、用量計入統計。api-key 填 auto 或留空 = 中繼自動走 active 上游；填某上游的真實 key = 指名直連該上游。",
      "设置 → 上游配置": "設定 → 上游配置",
      "：添加上游并选好「active」。": "：新增上游並標記為「active」。",
      "客户端 base_url 指到上面的地址，api-key 填 auto。": "客戶端 base_url 指到上面地址，api-key 填 auto。",
      "官方 Claude Code settings.json": "官方 Claude Code settings.json",
      "走中继（仅改 2 行）": "走中繼（僅改 2 行）",
      "完全透传模式": "完全透傳模式",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的目标地址原样直达 —— 连设置里没配过的上游也能直接用。": "中繼不做轉換、不按 active 路由，把請求按你寫進 api-key 的目標地址原樣直達 —— 連設定裡沒配過的上游也能直接用。",
      "为什么用 @@ 格式？": "為什麼用 @@ 格式？",
      "透传下 base_url 仍要指向中继（这样中继才能记账），这会占用原本指向提供商上游的 url 字段。解决办法：把上游 url 和 api-key 都塞进 api-key 一栏，格式 = 目标地址 + @@ + 真实 key。": "透傳下 base_url 仍要指向中繼（這樣中繼才能記帳），這會佔用原本指向提供商上游的 url 欄位。解決辦法：把上游 url 和 api-key 都塞進 api-key 一欄，格式 = 目標地址 + @@ + 真實 key。",
      "1. 客户端发": "1. 客戶端發",
      "base_url = 中继地址": "base_url = 中繼地址",
      "2. 中继拆": "2. 中繼拆",
      "拆出": "拆出",
      "url = 上游地址": "url = 上游地址",
      "原 key": "原 key",
      "3. 上游收": "3. 上游收",
      "转发到上游": "轉發到上游",
      "请求直达，不路由": "請求直達，不路由",
      "走中继（透传）": "走中繼（透傳）",
      "高级：curl 原生调用": "進階：curl 原始呼叫",
      "curl /v1/chat/completions（OpenAI 入口 → DeepSeek）": "curl /v1/chat/completions（OpenAI 入口 → DeepSeek）",
      "curl /v1/messages（Anthropic 入口 → DeepSeek Anthropic 兼容）": "curl /v1/messages（Anthropic 入口 → DeepSeek Anthropic 相容）",
      // v0.152 二改：極簡欄位列表替代流程圖
      "模型、上游全由中继在「设置 → 上游配置」里选，客户端只填这 3 个字段：": "模型、上游全由中繼在「設定 → 上游配置」裡選，客戶端只填這 3 個欄位：",
      "Anthropic 客户端": "Anthropic 客戶端",
      "OpenAI 客户端": "OpenAI 客戶端",
      "上游地址": "上游地址",
      "真实 key": "真實 key",
      "实际模型": "實際模型",
      "示例：Claude Code settings.json（官方 → 走中继）": "範例：Claude Code settings.json（官方 → 走中繼）",
      "官方": "官方",
      "走中继": "走中繼",
      // v0.152 終版：貼 README.docx 原文（關鍵術語拆出 <strong>，其餘整句進 dict）
      "转换模式下，模型、上游的选择完全由中继控制，来自 coding 客户端的所有请求，先到达中继，由中继接管，并向中继中选择的上游发送。": "轉換模式下，模型、上游的選擇完全由中繼控制，來自 coding 用戶端的所有請求，先到達中繼，由中繼接管，並向中繼中選擇的上游發送。",
      "客户端配置只需要将 URL 地址指向中继，然后将 api-key 和模型配置为": "用戶端配置只需要將 URL 地址指向中繼，然後將 api-key 和模型配置為",
      "占位符即可。": "佔位符即可。",
      "客户端配置：": "用戶端配置：",
      "透传模式下，为了统计 token 和保存数据，仍然需要让数据包经过中继，因而 base_url 仍需指向中继。": "透傳模式下，為了統計 token 和保存數據，仍然需要讓數據包經過中繼，因而 base_url 仍需指向中繼。",
      "这会占用原本指向提供商上游的 url 的位置，对于此问题的解决办法是：将": "這會佔用原本指向提供商上游的 url 的位置，對於此問題的解決辦法是：將",
      "上游 url": "上游 url",
      "和": "和",
      "都写在 api-key 的填写位置，采用如下格式：": "都寫在 api-key 的填寫位置，採用如下格式：",
      "此数据包到达中继后，中继会从 AUTH_TOKEN ( Api_Key ) 中解析字符，将 Api_Key 字段重写为“@@”标识符后半部分的 key，然后向前半部分 URL 发送数据包。": "此數據包到達中繼後，中繼會從 AUTH_TOKEN ( Api_Key ) 中解析字符，將 Api_Key 欄位重寫為「@@」標識符後半部分的 key，然後向前半部分 URL 發送數據包。",
      "示例：": "範例：",
      "Deepseek 的请求地址（来自官网信息）": "Deepseek 的請求地址（來自官網信息）",
      "（示例）": "（範例）",
      "不走中继时的请求填写（以 claude code 为例）：": "不走中繼時的請求填寫（以 claude code 為例）：",
      "不走中继时的请求填写": "不走中繼時的請求填寫",
      "走中继时，该请求需要改写为：": "走中繼時，該請求需要改寫為：",
      "走中继时的请求填写": "走中繼時的請求填寫",
      "查询中…": "查詢中…",
      "不可用": "不可用",
      "滚动": "滾動",
      "余额": "餘額",
      "失败": "失敗",
      "磁盘": "磁碟",
      "内存": "記憶體",
      "网络": "網路",
      // v0.154：cfg / stats / overview 補 key
      "地址 URL": "地址 URL",
      "auto 兜底模型": "auto 兜底模型",
      "客户端发 model=\"auto\" 时用此值替换。留空则按允许模型列表的第一个,再不行原样透传": "用戶端發送 model=\"auto\" 時以此值取代。留空則依允許模型列表的第一個；若再不行則原樣透傳。",
      "5 小时额度": "5 小時額度",
      "输入模型名后回车": "輸入模型名後按 Enter",
      "链接上游（合并统计）": "連結上游（合併統計）",
      "链接后所有统计页视为一个虚拟上游；不影响路由和计费；A+B 与 A+C 自动并为 A+B+C": "連結後所有統計頁視為一個虛擬上游；不影響路由與計費；A+B 與 A+C 自動合併為 A+B+C。",
      "+ 新建链接": "+ 新建連結",
      "任意模型": "任意模型",
      "最大模型": "最大模型",
      "单模型 30 天调用分布": "單模型 30 天調用分布",
      "小": "小",
      "中": "中",
      "大": "大",
      "隐藏整个顶栏（运行状态 + 按钮行），界面更干净；可从顶栏按钮或本开关恢复": "隱藏整個頂欄（運行狀態 + 按鈕列），介面更乾淨；可從頂欄按鈕或本開關恢復。",
      "在顶部状态栏显示\"客户端请求的模型 → 实际调用的模型\"对照": "在頂部狀態欄顯示「客戶端請求的模型 → 實際調用的模型」對照",
      "（未配置上游模型）": "（未設定上游模型）",
      "释放": "釋放",
      "调用": "調用",
    },
    ja: {
      "透传上游（自动发现）": "パススルー上流（自動検出）",
      "透传模式开启后，客户端请求自动发现的上游。可重命名，不可删除。": "Upstreams auto-discovered from passthrough requests. Renamable, not deletable.",
      "中继状态": "中継ステータス",
      "界面与偏好": "UI と設定",
      "存储管理": "ストレージ管理",
      "上游配置": "上流設定",
      "快捷切换": "クイック切替",
      "选中的模型可在左下角快速切换为预设": "Selected models can be switched as presets from the bottom-left",
      "外观": "外観",
      "实时栏管理": "ライブパネル管理",
      "数据": "データ",
      "启动": "起動",
      "自动切换": "自動切替",
      "动画": "アニメーション",
      "开发者模式": "開発者モード",
      "主题": "テーマ",
      "界面的整体配色方案": "Overall color scheme",
      "浅色": "Light",
      "暖白": "Warm",
      "深色": "Dark",
      "语言": "言語",
      "界面显示语言（当前仅设置页生效，全量翻译陆续加入）": "UI 表示言語（現在は設定ページのみ有効、全訳は順次追加）",
      "简体中文": "简体中文",
      "无边框": "枠なし",
      "开启后总览页所有卡片去掉玻璃容器，裸悬浮在底背景上": "Removes the glass containers from overview cards, leaving them floating on the background",
      "侧栏无边框": "サイドバー枠なし",
      "开启后实时流侧栏所有卡片去掉玻璃容器，裸悬浮在底背景上": "Removes glass containers from live-panel cards",
      "实时流侧栏": "ライブストリームサイドバー",
      "开启后右侧出现独立小窗，实时显示当前请求的思考过程和正文，方便调试": "Opens a separate window showing real-time reasoning and stream output for debugging",
      "允许并发显示": "並列表示を許可",
      "多个并发请求同时各自弹出实时栏": "Each concurrent request gets its own panel",
      "最多显示的并行数量": "最大並列表示数",
      "磁吸状态下始终开启一个": "磁吸状態では常に 1 つ開く",
      "磁吸模式下无论有无请求都保留一个空闲实时栏": "磁吸モードではリクエストがなくても 1 つ開いたままにする",
      "自动延展侧栏": "サイドバー自動拡張",
      "并发请求多时，实时栏窗口自动加宽显示更多并发": "Widens the panel automatically so more concurrent requests are visible",
      "悬浮球": "フローティングボール",
      "桌面悬浮球作为侧栏锚点，侧栏从球位置展开": "デスクトップのフローティングボールをサイドバーのアンカーとし、サイドバーがボールの位置から展開されます",
      "悬浮球置顶": "フローティングボールを最前面に表示",
      "悬浮球与侧栏始终置顶，不被其它窗口遮挡": "フローティングボールとサイドバーを常に最前面に表示し、他のウィンドウに隠れないようにします",
      "思考流超时时间": "思考ストリームのタイムアウト",
      "思考流已有内容但无新增，判定为中断的间隔时间": "Seconds without new content before the thinking stream is considered stalled",
      "思考-正文衔接超时时间": "Thinking-to-text gap timeout",
      "收到思考流停止信号，等待正文的超时时间": "Timeout waiting for the text stream after thinking stops",
      "正文流超时时间": "本文ストリームのタイムアウト",
      "正文流已有内容但无新增，判定为中断的间隔时间": "Seconds without new content before the text stream is considered stalled",
      "保存消息原文与回复": "メッセージ原文と返信を保存",
      "把每次请求的内容和模型回复存进本地数据库，历史页可查看对话；关闭后不再记录新消息": "Stores each request and reply in the local database for the history view; stops recording new messages when off",
      "开机自启": "OS 起動時に自動起動",
      "开机登录 Windows 后自动启动中继": "Auto-starts the relay after logging into Windows",
      "启动后隐藏窗口": "起動時にウィンドウを隠す",
      "登录启动后直接缩到托盘，不弹窗": "Starts minimized to the tray without a window",
      "自动切换 API": "API 自動切替",
      "当前上游额度用完后，自动切换到其他可用的 API": "Switches to another available API when the current upstream quota runs out",
      "允许自动切换的 API": "自動切替を許可する API",
      "选择可作为切换目标的上游；一个都不勾 = 全部允许": "Pick upstreams allowed as switch targets; none selected = all allowed",
      "高级切换（实验）": "詳細切替（実験的）",
      "闪烁": "フラッシュ",
      "调用时闪烁": "呼び出し時にフラッシュ",
      "释放闪烁": "解放フラッシュ",
      "5小时配额释放动画": "5-hour quota release animation",
      "全页闪烁": "全ページフラッシュ",
      "整个页面为你闪烁": "The whole page flashes for you",
      "高级闪烁功能": "詳細フラッシュ機能",
      "自定义动画曲线、颜色、粗细和宽度": "Custom animation curve, colors, thickness and width",
      "隐藏顶部调试栏": "上部デバッグバーを隠す",
      "内外转换显示": "I/O マッピング表示",
      // v0.190：开发者模式子项 —— 支持图片的模型。
      "支持图片的模型": "画像対応モデル",
      "允许 OpenCode 向勾选的模型发图；中继 /models/api.json 会标成 image-capable。点击切换勾选，保存后立即生效。默认全不勾。": "OpenCode がチェックを入れたモデルに画像を送れるようにします。リレーの /models/api.json で image-capable としてマークされます。クリックでチェックを切り替え、保存で即時反映。デフォルトはすべて未チェック。",
      "存储空间管理": "ストレージ使用量",
      "存储位置管理": "ストレージ場所",
      "消息数据库 (relay.db)": "メッセージ DB (relay.db)",
      "透传数据库 (passthrough.db)": "パススルー DB (passthrough.db)",
      "日志目录": "ログディレクトリ",
      "上游配置 (upstreams.json)": "上流設定 (upstreams.json)",
      "消息数据库位置": "メッセージ DB の場所",
      "透传数据库位置": "パススルー DB の場所",
      "日志目录位置": "ログディレクトリ",
      "上游配置位置": "上流設定の場所",
      "修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时需先停止中继。": "Changing a database location updates .env and moves the file; when the relay is running the file is locked, so stop the relay first.",
      "清理消息记录": "メッセージ履歴を削除",
      "压缩数据库": "DB を圧縮",
      "清空日志": "ログを消去",
      "刷新": "再読込",
      "修改": "変更",
      "总览": "概要",
      "实时": "ライブ",
      "历史": "履歴",
      "上游": "上流",
      "统计": "統計",
      "设置": "設定",
      "配置": "設定",
      "本地 AI 用量监控": "ローカル AI 使用量モニター",
      "中继": "中継",
      "用量最大的模型": "使用量最多のモデル",
      "输入": "入力",
      "输出": "出力",
      "缓存": "キャッシュ",
      "Token 消耗 · 近 24h": "Tokens · Last 24h",
      "上游状态": "Upstream status",
      "今日用量": "今日の使用量",
      "协议分布": "プロトコル分布",
      "模型分布": "モデル分布",
      "最近活动": "最近の活動",
      "正在进行的请求": "進行中のリクエスト",
      "最近": "最近",
      "透传模式": "パススルーモード",
      "Token 消耗树状图": "Token consumption treemap",
      "今日 token 消耗饼图": "Today's token pie",
      "管理卡片": "カード管理",
      "时间段": "期間",
      "维度": "次元",
      "模式": "モード",
      "连接中…": "接続中…",
      "等待首次轮询": "初回ポーリング待機",
      "运行中": "実行中",
      "运行中（外部）": "実行中（外部）",
      "已停止": "停止中",
      "停止": "停止",
      "中继未运行": "中継は未実行",
      "重启中…": "再起動中…",
      "状态未知": "ステータス不明",
      "等待 bridge 响应…": "bridge 応答待機…",
      "等待 bridge 响应": "bridge 応答待機",
      "点击启动以启动中继": "クリックで中継を起動",
      "端口": "ポート",
      "非本窗口启动": "not started by this window",
      "（透传模式）": " (passthrough)",
      "运行中（透传模式）": "Running (passthrough)",
      "已停止（透传模式）": "Stopped (passthrough)",
      "正在接管端口": "Taking over port",
      "正在重启…": "Restarting…",
      "请求": "requests",
      "条消息": "messages",
      "条透传请求": "passthrough requests",
      "个文件": "files",
      "重启": "再起動",
      "内外转换": "I/O mapping",
      "内外转换：开": "I/O mapping: on",
      "实时流": "ライブストリーム",
      "实时流：开": "ライブストリーム：ON",
      "关闭顶部调试栏": "Hide top debug bar",
      "刷新GUI": "GUI を再読込",
      "+ 新建": "+ 新規",
      "+ 新建上游": "+ 上流を新規",
      "+ 添加倍率": "+ 倍率を追加",
      "+ 新增快捷项": "+ クイック項目を追加",
      "近 24h": "24 時間",
      "近 7 天": "7 日",
      "近 30 天": "30 日",
      "按上游": "上流別",
      "按模型": "モデル別",
      "仅透传": "パススルーのみ",
      "全部": "すべて",
      "仅转换": "変換のみ",
      "全部模型": "すべてのモデル",
      "测试": "テスト",
      "测试中…": "テスト中…",
      "连通性测试": "接続テスト",
      "取消": "キャンセル",
      "创建": "作成",
      "保存": "保存",
      "保存高级切换配置": "Save advanced switch config",
      "新增": "追加",
      "编辑": "編集",
      "删除": "削除",
      "确定": "OK",
      "复制": "コピー",
      "已复制": "コピー済み",
      "查询额度": "クォータ照会",
      "清空": "クリア",
      "无鉴权": "認証なし",
      "请求详情": "リクエスト詳細",
      "按模型拆分详情": "Breakdown by model",
      "快捷切换编辑器": "Quick switch editor",
      "新建上游": "上流を新規作成",
      "预设配置": "プリセット",
      "模型": "モデル",
      "协议": "プロトコル",
      "鉴权头": "認証ヘッダ",
      "名称": "名前",
      "允许的模型": "許可モデル",
      "计费模式": "課金モード",
      "Token 计费字段": "Token 課金フィールド",
      "模型倍率": "モデル倍率",
      "5h 限额": "5 時間制限",
      "周限额": "週間制限",
      "月限额": "月間制限",
      "备注": "メモ",
      "选预设后只需填名称和 API Key，其余自动配好": "Pick a preset and fill in name + API key; the rest is auto-configured",
      "自动填名称与允许列表": "Auto-fills the name and allowed list",
      "上游说的协议；不选 = 自动跟随客户端入口": "Protocol the upstream speaks; blank = follow the client's entry protocol",
      "发给上游的认证格式；不选 = 跟随协议默认（Anthropic → x-api-key，OpenAI → Bearer）": "Auth format sent upstream; blank = protocol default (Anthropic → x-api-key, OpenAI → Bearer)",
      "上游 API endpoint": "Upstream API endpoint",
      "明文保存到 upstreams.json，留空跳过": "Stored in plaintext in upstreams.json; leave blank to skip",
      "留空 = 不限制。回车添加": "Blank = unlimited. Press Enter to add",
      "未列出的模型按 1× 计": "Unlisted models count as 1×",
      "留空 = 不限制": "Blank = unlimited",
      "仅 opencode-go 用：自动换 x-api-key、模型小写、剥 thinking": "opencode-go only: swaps x-api-key, lowercases model names, strips thinking",
      "可选，会显示在上游详情卡片底部": "Optional; shown at the bottom of the upstream detail card",
      "input_tokens（输入）": "input_tokens (input)",
      "output_tokens（输出）": "output_tokens (output)",
      "cache_read_input_tokens（缓存命中读取）": "cache_read_input_tokens (cache read)",
      "cache_creation_input_tokens（缓存写入）": "cache_creation_input_tokens (cache write)",
      "高级功能（计费 / 倍率 / 限额 / 协议适配）": "Advanced (billing / multipliers / limits / protocol)",
      "加载上游配置…": "Loading upstream config…",
      "加载中…": "読み込み中…",
      "暂无进行中的请求": "進行中のリクエストなし",
      "暂无记录": "記録なし",
      "暂无数据": "データなし",
      "无数据": "データなし",
      "合计": "合計",
      "暂无": "なし",
      "未配置上游": "上流未設定",
      "暂无上游配置": "上流設定なし",
      "暂无快捷项，点下方按钮添加": "No quick-switch items; add one with the button below",
      "暂无请求": "リクエストなし",
      "暂无最近请求": "No recent requests",
      "近 24h 暂无请求": "No requests in the last 24h",
      "加载失败": "読み込み失敗",
      "桥不可用": "ブリッジ利用不可",
      "该上游暂无调用记录": "No call records for this upstream",
      "图表库未加载（vendor/chart.umd.min.js）": "Chart library not loaded (vendor/chart.umd.min.js)",
      "暂无上游": "上流なし",
      "报错分析": "エラー分析",
      "允许小模型分析报错信息": "小型モデルにエラー分析を許可",
      "出现报错时把错误信息发给所选模型，判断错误类型并给出中文提示（余额不足 / 网络错误 / 达到次数限制等）。": "On error, send the error info to the selected model to classify the type and produce a short Chinese hint (out of balance / network error / rate limit, etc.).",
      "分析模型": "分析モデル",
      "建议选择轻量快速的小模型；留空则使用默认上游的兜底模型。": "Pick a lightweight, fast small model; leave blank to use the default upstream fallback model.",
      "「测试」用一条示例 429 报错真实跑一次分类，验证模型与提示词。保存后立即生效，无需重启中继。": "\\\\\\\"Test\\\\\\\" runs a real classification on a sample 429 error to verify the model and prompt. Saves take effect immediately, no relay restart needed.",
      "消息数据库": "メッセージ DB",
      "透传数据库": "パススルー DB",
      "「清理」「压缩」需中继运行中执行。修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时，需先停止中继再迁移。": "Clean / compact require the relay to be running. Changing a database location writes .env and moves the file; when the relay is running the file is locked, so stop the relay before migrating.",
      "压缩": "圧縮",
      "修改位置": "場所を変更",
      "清理透传记录": "パススルー履歴を削除",
      "settings-group.startup": "Startup",
      "总计": "合計",
      "请求数": "リクエスト数",
      "输入 tokens": "入力 tokens",
      "输出 tokens": "出力 tokens",
      "错误": "エラー",
      "上游详情": "上流詳細",
      "界面与偏好": "UI と設定",
      "时间": "時刻",
      "平台": "プラットフォーム",
      "模型": "モデル",
      "上游": "上流",
      "状态": "ステータス",
      "入": "入",
      "出": "出",
      "加载失败（未知错误），继续滚动重试": "Failed to load (unknown error); scroll to retry",
      "近 30 天 · 按上游": "30 days Upstream",
      "近 30 天 · 按模型": "30 days Model",
      "近 7 天 · 按上游": "7 days Upstream",
      "近 7 天 · 按模型": "7 days Model",
      "近 24h · 按上游": "24h Upstream",
      "近 24h · 按模型": "24h Model",
      " · 仅透传": " · passthrough only",
      " · 全部": " · all",
      "转换模式": "変換モード",
      "透传模式": "パススルーモード",
      "运行状态": "実行状態",
      "已停止": "停止中",
      "进程 PID": "プロセス PID",
      "监听端口": "リスニングポート",
      "启动来源": "起動元",
      "本窗口": "本ウィンドウ",
      "外部进程": "外部プロセス",
      "日志目录": "ログディレクトリ",
      "数据快照": "スナップショット",
      "额度查询": "クォータ照会",
      "缓动曲线 (cubic-bezier)": "Easing curve (cubic-bezier)",
      "颜色 / 粗细 / 宽度": "Color / thickness / width",
      "粗细": "太さ",
      "宽度": "幅",
      "增长色": "上昇色",
      "留空=主题色": "blank = theme color",
      "释放色": "下降色",
      "留空=绿色": "blank = green",
      "加载中…": "読み込み中…",
      "接入地址": "接続先",
      "两种模式客户端都连": "Both modes connect to",
      "同一个中继地址": "the same relay address",
      "，只差 api-key 的填法（见下）。按客户端协议选入口：Anthropic 系用": " — only the api-key format differs (see below). Pick the entry by client protocol: Anthropic series uses",
      "，OpenAI 系用": ", OpenAI series uses",
      "base_url（Anthropic 客户端）": "base_url (Anthropic clients)",
      "base_url（OpenAI 客户端）": "base_url (OpenAI clients)",
      "复制": "コピー",
      "当前模式": "Current mode",
      "开关在「设置 → 中继模式」": "Toggle in \\\\\\\"Settings → Relay mode\\\\\\\"",
      "默认 · 推荐": "デフォルト · 推奨",
      "是什么：": "What it is:",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填": "The relay does \\\\\\\"routing + protocol conversion\\\\\\\": requests go through the active upstream and usage is recorded. Fill api-key with",
      "或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": " or leave it blank — the relay auto-routes to the active upstream. Fill in a real upstream key to go directly to that upstream.",
      "设置 → 上游配置": "Settings → Upstream config",
      "：添加上游并选好「active」。": ": add an upstream and pick \\\\\\\"active\\\\\\\".",
      "2. 客户端 base_url 指到上面的地址，api-key 填": "2. Point the client's base_url to the address above, and fill api-key with",
      "Codex / OpenAI 客户端": "Codex / OpenAI clients",
      "完全透传模式": "Full passthrough mode",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的": "The relay does not convert or route by active — it forwards your request as-is to the address in api-key:",
      "目标地址": "宛先アドレス",
      "原样直达 —— 连设置里没配过的上游也能直接用。api-key 格式：": ", as-is — even upstreams not configured in Settings work directly. api-key format:",
      "目标地址@@上游key": "target@@upstream_key",
      "设置 → 中继模式": "Settings → Relay mode",
      "：切到「完全透传」。": ": switch to \\\\\\\"Full passthrough\\\\\\\".",
      "2. 客户端照旧连上面的中继地址，但 api-key 填": "2. The client still connects to the address above, but fill api-key with",
      "，请求路径随便。": " — request path is up to you.",
      "透传 curl 示例": "Passthrough curl example",
      "高级：当前上游 · 快捷切换 · 已配置上游": "Advanced: active upstream · quick switch · configured upstreams",
      "当前 active 上游": "Current active upstream",
      "「auto」请求会路由到这里。": "\\\\\\\"auto\\\\\\\" requests route here.",
      "尚未配置 active 上游": "No active upstream configured",
      "已配置上游": "設定済み上流",
      "点击任意上游行立即切换为该平台的 active 上游。": "Click any upstream row to switch it to be that platform's active upstream.",
      "未配置上游 —— 到「设置 → 上游配置」新建": "No upstreams — create one in \\\\\\\"Settings → Upstream config\\\\\\\"",
      "自定义（手动填写全部字段）": "Custom (fill all fields manually)",
      "DeepSeek 官方（Anthropic 协议）": "DeepSeek official (Anthropic protocol)",
      "OpenCode Go 订阅（Anthropic 协议）": "OpenCode Go subscription (Anthropic protocol)",
      "OpenCode Zen 按量付费（OpenAI 协议）": "OpenCode Zen pay-as-you-go (OpenAI protocol)",
      "OpenCode Zen 免费模型（OpenAI 协议）": "OpenCode Zen free models (OpenAI protocol)",
      "HuoShan Agent Plan（Anthropic 协议）": "HuoShan Agent Plan (Anthropic protocol)",
      "— 请选择模型 —": "— pick a model —",
      "按次数计费": "回数課金",
      "按 Token 计费": "Token 課金",
      "OpenCode Go 协议适配": "OpenCode Go protocol adaptation",
      "测试日志": "テストログ",
      "次": "回",
      "tokens": "tokens",
      "额度已用尽": "クォータ枯渇",
      "预计": "推定",
      "用尽": "枯渇",
      "非允许模型": "許可外モデル",
      "周": "週",
      "月": "月",
      "5h": "5h",
      "累计 tokens": "累計 tokens",
      "5h 释放": "5h 解放",
      "删除这条上游配置": "Delete this upstream config",
      "点击激活此上游；已是当前上游时点击进入编辑": "Click to activate this upstream; click again to edit when it's already active",
      // v0.152：設定画面「ガイド」テキスト再構築 —— JSON 比較 + なぜ @@ フォーマット？
      "转换模式": "変換モード",
      "默认 · 推荐": "デフォルト · 推奨",
      "是什么：": "概要：",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填 auto 或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": "中继は「ルーティング + プロトコル変換」を行い、active 上游へ転送、利用量を統計に計上します。api-key に auto を指定または空欄 = 中继が active 上游を自動選択；特定上游の実 key を指定 = その上游に直接接続します。",
      "设置 → 上游配置": "設定 → 上游構成",
      "：添加上游并选好「active」。": "：上游を追加し「active」に設定。",
      "客户端 base_url 指到上面的地址，api-key 填 auto。": "クライアントの base_url を上のアドレスに向け、api-key は auto を指定。",
      "官方 Claude Code settings.json": "公式 Claude Code settings.json",
      "走中继（仅改 2 行）": "中继経由（変更は 2 行のみ）",
      "完全透传模式": "完全パススルーモード",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的目标地址原样直达 —— 连设置里没配过的上游也能直接用。": "中继は変換やルーティングを行わず、api-key に記述した宛先にリクエストをそのまま転送します —— 設定に未登録の上游でも直接利用可能。",
      "为什么用 @@ 格式？": "なぜ @@ 形式？",
      "透传下 base_url 仍要指向中继（这样中继才能记账），这会占用原本指向提供商上游的 url 字段。解决办法：把上游 url 和 api-key 都塞进 api-key 一栏，格式 = 目标地址 + @@ + 真实 key。": "パススルーでも base_url は中继を指す必要があります（利用量記録のため）。これにより、本来 upstream URL を入れる場所が占有されます。解決策：upstream URL と api-key を api-key フィールドにまとめ、形式 = 宛先 URL + @@ + 実 key。",
      "1. 客户端发": "1. クライアント送信",
      "base_url = 中继地址": "base_url = 中继 URL",
      "2. 中继拆": "2. 中继が分離",
      "拆出": "取り出し",
      "url = 上游地址": "url = 上游 URL",
      "原 key": "元の key",
      "3. 上游收": "3. 上游受信",
      "转发到上游": "上游へ転送",
      "请求直达，不路由": "直接転送、ルーティングなし",
      "走中继（透传）": "中继経由（パススルー）",
      "高级：curl 原生调用": "上級：curl ネイティブ呼び出し",
      "curl /v1/chat/completions（OpenAI 入口 → DeepSeek）": "curl /v1/chat/completions（OpenAI 入口 → DeepSeek）",
      "curl /v1/messages（Anthropic 入口 → DeepSeek Anthropic 兼容）": "curl /v1/messages（Anthropic 入口 → DeepSeek Anthropic 互換）",
      // v0.152 二改：極簡欄位列表替代流程圖
      "模型、上游全由中继在「设置 → 上游配置」里选，客户端只填这 3 个字段：": "モデル・アップストリームは中继の「設定 → アップストリーム構成」で選択。クライアントはこの 3 フィールドのみ設定：",
      "Anthropic 客户端": "Anthropic クライアント",
      "OpenAI 客户端": "OpenAI クライアント",
      "上游地址": "アップストリーム URL",
      "真实 key": "実キー",
      "实际模型": "実際のモデル",
      "示例：Claude Code settings.json（官方 → 走中继）": "例：Claude Code settings.json（公式 → 中继経由）",
      "官方": "公式",
      "走中继": "中继経由",
      // v0.152 終版：README.docx 原文に準拠（重要語は <strong>、残りは丸ごと dict）
      "转换模式下，模型、上游的选择完全由中继控制，来自 coding 客户端的所有请求，先到达中继，由中继接管，并向中继中选择的上游发送。": "変換モードでは、モデル・アップストリームの選択はすべて中继が制御します。coding クライアントからのすべてのリクエストは、まず中继に到達し、中继が引き継ぎ、中继で選択されたアップストリームへ送信されます。",
      "客户端配置只需要将 URL 地址指向中继，然后将 api-key 和模型配置为": "クライアントは URL を中继に向け、api-key とモデルを",
      "占位符即可。": "に設定するだけです。",
      "客户端配置：": "クライアント構成：",
      "透传模式下，为了统计 token 和保存数据，仍然需要让数据包经过中继，因而 base_url 仍需指向中继。": "パススルーモードでは、トークン統計とデータ保存のためにパケットを中继経由にする必要があるため、base_url は引き続き中继を指します。",
      "这会占用原本指向提供商上游的 url 的位置，对于此问题的解决办法是：将": "これは元々プロバイダのアップストリーム URL を指す位置を占有します。解決策：",
      "上游 url": "アップストリーム URL",
      "和": "と",
      "都写在 api-key 的填写位置，采用如下格式：": "の両方を api-key 欄に記述し、次の形式にします：",
      "此数据包到达中继后，中继会从 AUTH_TOKEN ( Api_Key ) 中解析字符，将 Api_Key 字段重写为“@@”标识符后半部分的 key，然后向前半部分 URL 发送数据包。": "このパケットが中继に到達すると、中继は AUTH_TOKEN ( Api_Key ) から文字を解析し、Api_Key フィールドを「@@」区切り文字の後半部分の key に書き換え、前半部分の URL へパケットを送信します。",
      "示例：": "例：",
      "Deepseek 的请求地址（来自官网信息）": "Deepseek のリクエストアドレス（公式情報より）",
      "（示例）": "（例）",
      "不走中继时的请求填写（以 claude code 为例）：": "中继を通さない場合のリクエスト記述（claude code を例に）：",
      "不走中继时的请求填写": "中继を通さない場合のリクエスト記述",
      "走中继时，该请求需要改写为：": "中继を通す場合、このリクエストは次のように書き換えます：",
      "走中继时的请求填写": "中继を通す場合のリクエスト記述",
      "查询中…": "照会中…",
      "不可用": "利用不可",
      "滚动": "ローリング",
      "余额": "残高",
      "失败": "失敗",
      "磁盘": "ディスク",
      "内存": "メモリ",
      "网络": "ネットワーク",
      // v0.154：cfg / stats / overview 補 key
      "地址 URL": "URL",
      "auto 兜底模型": "auto フォールバックモデル",
      "客户端发 model=\"auto\" 时用此值替换。留空则按允许模型列表的第一个,再不行原样透传": "クライアントが model=\"auto\" を送ったときにこの値で置き換えます。空欄なら許可モデルリストの先頭を使用し、それも不可ならそのまま転送します。",
      "5 小时额度": "5時間クォータ",
      "输入模型名后回车": "モデル名を入力して Enter を押してください",
      "链接上游（合并统计）": "上流のリンク（統計合算）",
      "链接后所有统计页视为一个虚拟上游；不影响路由和计费；A+B 与 A+C 自动并为 A+B+C": "リンク後、すべての統計ページが 1 つの仮想上流として扱われます。ルーティングと課金には影響しません。A+B と A+C は自動的に A+B+C に統合されます。",
      "+ 新建链接": "+ 新規リンク",
      "任意模型": "任意モデル",
      "最大模型": "最大モデル",
      "单模型 30 天调用分布": "単一モデル 30 日間の呼び出し分布",
      "小": "小",
      "中": "中",
      "大": "大",
      "隐藏整个顶栏（运行状态 + 按钮行），界面更干净；可从顶栏按钮或本开关恢复": "上部バー全体（実行状態 + ボタン行）を隠して画面をすっきりさせます。上部バーのボタンか本スイッチで復元できます。",
      "在顶部状态栏显示\"客户端请求的模型 → 实际调用的模型\"对照": "上部ステータスバーに「クライアントがリクエストしたモデル → 実際に呼び出したモデル」の対照を表示",
      "（未配置上游模型）": "（未設定の上流モデル）",
      "释放": "解放",
      "调用": "呼び出し",
    },
    ko: {
      "透传上游（自动发现）": "패스스루 업스트림 (자동 발견)",
      "透传模式开启后，客户端请求自动发现的上游。可重命名，不可删除。": "Upstreams auto-discovered from passthrough requests. Renamable, not deletable.",
      "中继状态": "릴레이 상태",
      "界面与偏好": "UI 및 설정",
      "存储管理": "저장소 관리",
      "上游配置": "업스트림 설정",
      "快捷切换": "빠른 전환",
      "选中的模型可在左下角快速切换为预设": "Selected models can be switched as presets from the bottom-left",
      "外观": "외관",
      "实时栏管理": "실시간 패널 관리",
      "数据": "데이터",
      "启动": "시작",
      "自动切换": "자동 전환",
      "动画": "애니메이션",
      "开发者模式": "개발자 모드",
      "主题": "테마",
      "界面的整体配色方案": "Overall color scheme",
      "浅色": "Light",
      "暖白": "Warm",
      "深色": "Dark",
      "语言": "언어",
      "界面显示语言（当前仅设置页生效，全量翻译陆续加入）": "UI 표시 언어 (현재는 설정 페이지만 적용, 전체 번역은 순차 추가)",
      "简体中文": "简体中文",
      "无边框": "테두리 없음",
      "开启后总览页所有卡片去掉玻璃容器，裸悬浮在底背景上": "Removes the glass containers from overview cards, leaving them floating on the background",
      "侧栏无边框": "사이드바 테두리 없음",
      "开启后实时流侧栏所有卡片去掉玻璃容器，裸悬浮在底背景上": "Removes glass containers from live-panel cards",
      "实时流侧栏": "실시간 스트림 사이드바",
      "开启后右侧出现独立小窗，实时显示当前请求的思考过程和正文，方便调试": "Opens a separate window showing real-time reasoning and stream output for debugging",
      "允许并发显示": "동시 표시 허용",
      "多个并发请求同时各自弹出实时栏": "Each concurrent request gets its own panel",
      "最多显示的并行数量": "최대 동시 표시 수",
      "磁吸状态下始终开启一个": "자석 모드에서 항상 하나 열기",
      "磁吸模式下无论有无请求都保留一个空闲实时栏": "자석 모드에서는 요청이 없어도 하나를 열어 둠",
      "自动延展侧栏": "사이드바 자동 확장",
      "并发请求多时，实时栏窗口自动加宽显示更多并发": "Widens the panel automatically so more concurrent requests are visible",
      "悬浮球": "플로팅 볼",
      "桌面悬浮球作为侧栏锚点，侧栏从球位置展开": "데스크톱 플로팅 볼이 사이드바 앵커가 되어 사이드바가 볼 위치에서 펼쳐집니다",
      "悬浮球置顶": "플로팅 볼 항상 위에 표시",
      "悬浮球与侧栏始终置顶，不被其它窗口遮挡": "플로팅 볼과 사이드바를 항상 맨 위에 유지하여 다른 창에 가려지지 않게 합니다",
      "思考流超时时间": "사고 스트림 타임아웃",
      "思考流已有内容但无新增，判定为中断的间隔时间": "Seconds without new content before the thinking stream is considered stalled",
      "思考-正文衔接超时时间": "Thinking-to-text gap timeout",
      "收到思考流停止信号，等待正文的超时时间": "Timeout waiting for the text stream after thinking stops",
      "正文流超时时间": "본문 스트림 타임아웃",
      "正文流已有内容但无新增，判定为中断的间隔时间": "Seconds without new content before the text stream is considered stalled",
      "保存消息原文与回复": "메시지 원문과 응답 저장",
      "把每次请求的内容和模型回复存进本地数据库，历史页可查看对话；关闭后不再记录新消息": "Stores each request and reply in the local database for the history view; stops recording new messages when off",
      "开机自启": "부팅 시 자동 시작",
      "开机登录 Windows 后自动启动中继": "Auto-starts the relay after logging into Windows",
      "启动后隐藏窗口": "시작 시 창 숨기기",
      "登录启动后直接缩到托盘，不弹窗": "Starts minimized to the tray without a window",
      "自动切换 API": "API 자동 전환",
      "当前上游额度用完后，自动切换到其他可用的 API": "Switches to another available API when the current upstream quota runs out",
      "允许自动切换的 API": "자동 전환 허용 API",
      "选择可作为切换目标的上游；一个都不勾 = 全部允许": "Pick upstreams allowed as switch targets; none selected = all allowed",
      "高级切换（实验）": "고급 전환 (실험적)",
      "闪烁": "플래시",
      "调用时闪烁": "호출 시 플래시",
      "释放闪烁": "해제 플래시",
      "5小时配额释放动画": "5-hour quota release animation",
      "全页闪烁": "전체 페이지 플래시",
      "整个页面为你闪烁": "The whole page flashes for you",
      "高级闪烁功能": "고급 플래시 기능",
      "自定义动画曲线、颜色、粗细和宽度": "Custom animation curve, colors, thickness and width",
      "隐藏顶部调试栏": "상단 디버그 바 숨기기",
      "内外转换显示": "I/O 매핑 표시",
      // v0.190：开发者模式子项 —— 支持图片的模型。
      "支持图片的模型": "이미지 지원 모델",
      "允许 OpenCode 向勾选的模型发图；中继 /models/api.json 会标成 image-capable。点击切换勾选，保存后立即生效。默认全不勾。": "OpenCode 가 선택한 모델에 이미지를 보낼 수 있도록 허용합니다. 릴레이 /models/api.json 에서 image-capable 로 표시됩니다. 클릭으로 선택을 전환하고, 저장하면 즉시 적용됩니다. 기본값은 모두 미선택.",
      "存储空间管理": "저장소 사용량",
      "存储位置管理": "저장소 위치",
      "消息数据库 (relay.db)": "메시지 DB (relay.db)",
      "透传数据库 (passthrough.db)": "패스스루 DB (passthrough.db)",
      "日志目录": "로그 디렉터리",
      "上游配置 (upstreams.json)": "업스트림 설정 (upstreams.json)",
      "消息数据库位置": "메시지 DB 위치",
      "透传数据库位置": "패스스루 DB 위치",
      "日志目录位置": "로그 디렉터리 위치",
      "上游配置位置": "업스트림 설정 위치",
      "修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时需先停止中继。": "Changing a database location updates .env and moves the file; when the relay is running the file is locked, so stop the relay first.",
      "清理消息记录": "메시지 기록 정리",
      "压缩数据库": "DB 압축",
      "清空日志": "로그 비우기",
      "刷新": "새로 고침",
      "修改": "변경",
      "总览": "개요",
      "实时": "실시간",
      "历史": "기록",
      "上游": "업스트림",
      "统计": "통계",
      "设置": "설정",
      "配置": "설정",
      "本地 AI 用量监控": "로컬 AI 사용량 모니터",
      "中继": "릴레이",
      "用量最大的模型": "가장 많이 사용된 모델",
      "输入": "입력",
      "输出": "출력",
      "缓存": "캐시",
      "Token 消耗 · 近 24h": "Tokens · Last 24h",
      "上游状态": "Upstream status",
      "今日用量": "오늘 사용량",
      "协议分布": "프로토콜 분포",
      "模型分布": "모델 분포",
      "最近活动": "최근 활동",
      "正在进行的请求": "진행 중인 요청",
      "最近": "최근",
      "透传模式": "패스스루 모드",
      "Token 消耗树状图": "Token consumption treemap",
      "今日 token 消耗饼图": "Today's token pie",
      "管理卡片": "카드 관리",
      "时间段": "기간",
      "维度": "차원",
      "模式": "모드",
      "连接中…": "연결 중…",
      "等待首次轮询": "첫 폴링 대기",
      "运行中": "실행 중",
      "运行中（外部）": "실행 중 (외부)",
      "已停止": "중지됨",
      "停止": "중지",
      "中继未运行": "릴레이 미실행",
      "重启中…": "재시작 중…",
      "状态未知": "상태 불명",
      "等待 bridge 响应…": "bridge 응답 대기…",
      "等待 bridge 响应": "bridge 응답 대기",
      "点击启动以启动中继": "클릭하여 릴레이 시작",
      "端口": "포트",
      "非本窗口启动": "not started by this window",
      "（透传模式）": " (passthrough)",
      "运行中（透传模式）": "Running (passthrough)",
      "已停止（透传模式）": "Stopped (passthrough)",
      "正在接管端口": "Taking over port",
      "正在重启…": "Restarting…",
      "请求": "requests",
      "条消息": "messages",
      "条透传请求": "passthrough requests",
      "个文件": "files",
      "重启": "재시작",
      "内外转换": "I/O mapping",
      "内外转换：开": "I/O mapping: on",
      "实时流": "실시간 스트림",
      "实时流：开": "실시간 스트림: 켜짐",
      "关闭顶部调试栏": "Hide top debug bar",
      "刷新GUI": "GUI 새로 고침",
      "+ 新建": "+ 새로 만들기",
      "+ 新建上游": "+ 업스트림 새로 만들기",
      "+ 添加倍率": "+ 배율 추가",
      "+ 新增快捷项": "+ 빠른 항목 추가",
      "近 24h": "24 시간",
      "近 7 天": "7 일",
      "近 30 天": "30 일",
      "按上游": "업스트림별",
      "按模型": "모델별",
      "仅透传": "패스스루만",
      "全部": "전체",
      "仅转换": "변환만",
      "全部模型": "모든 모델",
      "测试": "테스트",
      "测试中…": "테스트 중…",
      "连通性测试": "연결 테스트",
      "取消": "취소",
      "创建": "만들기",
      "保存": "저장",
      "保存高级切换配置": "Save advanced switch config",
      "新增": "추가",
      "编辑": "편집",
      "删除": "삭제",
      "确定": "확인",
      "复制": "복사",
      "已复制": "복사됨",
      "查询额度": "쿼터 조회",
      "清空": "비우기",
      "无鉴权": "인증 없음",
      "请求详情": "요청 상세",
      "按模型拆分详情": "Breakdown by model",
      "快捷切换编辑器": "Quick switch editor",
      "新建上游": "업스트림 새로 만들기",
      "预设配置": "프리셋",
      "模型": "모델",
      "协议": "프로토콜",
      "鉴权头": "인증 헤더",
      "名称": "이름",
      "允许的模型": "허용 모델",
      "计费模式": "과금 모드",
      "Token 计费字段": "Token 과금 필드",
      "模型倍率": "모델 배율",
      "5h 限额": "5 시간 한도",
      "周限额": "주간 한도",
      "月限额": "월간 한도",
      "备注": "메모",
      "选预设后只需填名称和 API Key，其余自动配好": "Pick a preset and fill in name + API key; the rest is auto-configured",
      "自动填名称与允许列表": "Auto-fills the name and allowed list",
      "上游说的协议；不选 = 自动跟随客户端入口": "Protocol the upstream speaks; blank = follow the client's entry protocol",
      "发给上游的认证格式；不选 = 跟随协议默认（Anthropic → x-api-key，OpenAI → Bearer）": "Auth format sent upstream; blank = protocol default (Anthropic → x-api-key, OpenAI → Bearer)",
      "上游 API endpoint": "Upstream API endpoint",
      "明文保存到 upstreams.json，留空跳过": "Stored in plaintext in upstreams.json; leave blank to skip",
      "留空 = 不限制。回车添加": "Blank = unlimited. Press Enter to add",
      "未列出的模型按 1× 计": "Unlisted models count as 1×",
      "留空 = 不限制": "Blank = unlimited",
      "仅 opencode-go 用：自动换 x-api-key、模型小写、剥 thinking": "opencode-go only: swaps x-api-key, lowercases model names, strips thinking",
      "可选，会显示在上游详情卡片底部": "Optional; shown at the bottom of the upstream detail card",
      "input_tokens（输入）": "input_tokens (input)",
      "output_tokens（输出）": "output_tokens (output)",
      "cache_read_input_tokens（缓存命中读取）": "cache_read_input_tokens (cache read)",
      "cache_creation_input_tokens（缓存写入）": "cache_creation_input_tokens (cache write)",
      "高级功能（计费 / 倍率 / 限额 / 协议适配）": "Advanced (billing / multipliers / limits / protocol)",
      "加载上游配置…": "Loading upstream config…",
      "加载中…": "불러오는 중…",
      "暂无进行中的请求": "진행 중인 요청 없음",
      "暂无记录": "기록 없음",
      "暂无数据": "데이터 없음",
      "无数据": "데이터 없음",
      "合计": "합계",
      "暂无": "없음",
      "未配置上游": "업스트림 미설정",
      "暂无上游配置": "업스트림 설정 없음",
      "暂无快捷项，点下方按钮添加": "No quick-switch items; add one with the button below",
      "暂无请求": "요청 없음",
      "暂无最近请求": "No recent requests",
      "近 24h 暂无请求": "No requests in the last 24h",
      "加载失败": "불러오기 실패",
      "桥不可用": "브리지 사용 불가",
      "该上游暂无调用记录": "No call records for this upstream",
      "图表库未加载（vendor/chart.umd.min.js）": "Chart library not loaded (vendor/chart.umd.min.js)",
      "暂无上游": "업스트림 없음",
      "报错分析": "오류 분석",
      "允许小模型分析报错信息": "소형 모델로 오류 분석 허용",
      "出现报错时把错误信息发给所选模型，判断错误类型并给出中文提示（余额不足 / 网络错误 / 达到次数限制等）。": "On error, send the error info to the selected model to classify the type and produce a short Chinese hint (out of balance / network error / rate limit, etc.).",
      "分析模型": "분석 모델",
      "建议选择轻量快速的小模型；留空则使用默认上游的兜底模型。": "Pick a lightweight, fast small model; leave blank to use the default upstream fallback model.",
      "「测试」用一条示例 429 报错真实跑一次分类，验证模型与提示词。保存后立即生效，无需重启中继。": "\\\\\\\"Test\\\\\\\" runs a real classification on a sample 429 error to verify the model and prompt. Saves take effect immediately, no relay restart needed.",
      "消息数据库": "메시지 DB",
      "透传数据库": "패스스루 DB",
      "「清理」「压缩」需中继运行中执行。修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时，需先停止中继再迁移。": "Clean / compact require the relay to be running. Changing a database location writes .env and moves the file; when the relay is running the file is locked, so stop the relay before migrating.",
      "压缩": "압축",
      "修改位置": "위치 변경",
      "清理透传记录": "패스스루 기록 정리",
      "settings-group.startup": "Startup",
      "总计": "합계",
      "请求数": "요청 수",
      "输入 tokens": "입력 tokens",
      "输出 tokens": "출력 tokens",
      "错误": "오류",
      "上游详情": "업스트림 상세",
      "界面与偏好": "UI 및 설정",
      "时间": "시간",
      "平台": "플랫폼",
      "模型": "모델",
      "上游": "업스트림",
      "状态": "상태",
      "入": "입",
      "出": "출",
      "加载失败（未知错误），继续滚动重试": "Failed to load (unknown error); scroll to retry",
      "近 30 天 · 按上游": "30 days Upstream",
      "近 30 天 · 按模型": "30 days Model",
      "近 7 天 · 按上游": "7 days Upstream",
      "近 7 天 · 按模型": "7 days Model",
      "近 24h · 按上游": "24h Upstream",
      "近 24h · 按模型": "24h Model",
      " · 仅透传": " · passthrough only",
      " · 全部": " · all",
      "转换模式": "변환 모드",
      "透传模式": "패스스루 모드",
      "运行状态": "실행 상태",
      "已停止": "중지됨",
      "进程 PID": "프로세스 PID",
      "监听端口": "리스닝 포트",
      "启动来源": "시작 위치",
      "本窗口": "현재 창",
      "外部进程": "외부 프로세스",
      "日志目录": "로그 디렉터리",
      "数据快照": "스냅샷",
      "额度查询": "쿼터 조회",
      "缓动曲线 (cubic-bezier)": "Easing curve (cubic-bezier)",
      "颜色 / 粗细 / 宽度": "Color / thickness / width",
      "粗细": "굵기",
      "宽度": "너비",
      "增长色": "상승 색",
      "留空=主题色": "blank = theme color",
      "释放色": "하강 색",
      "留空=绿色": "blank = green",
      "加载中…": "불러오는 중…",
      "接入地址": "연결 주소",
      "两种模式客户端都连": "Both modes connect to",
      "同一个中继地址": "the same relay address",
      "，只差 api-key 的填法（见下）。按客户端协议选入口：Anthropic 系用": " — only the api-key format differs (see below). Pick the entry by client protocol: Anthropic series uses",
      "，OpenAI 系用": ", OpenAI series uses",
      "base_url（Anthropic 客户端）": "base_url (Anthropic clients)",
      "base_url（OpenAI 客户端）": "base_url (OpenAI clients)",
      "复制": "복사",
      "当前模式": "Current mode",
      "开关在「设置 → 中继模式」": "Toggle in \\\\\\\"Settings → Relay mode\\\\\\\"",
      "默认 · 推荐": "기본값 · 권장",
      "是什么：": "What it is:",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填": "The relay does \\\\\\\"routing + protocol conversion\\\\\\\": requests go through the active upstream and usage is recorded. Fill api-key with",
      "或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": " or leave it blank — the relay auto-routes to the active upstream. Fill in a real upstream key to go directly to that upstream.",
      "设置 → 上游配置": "Settings → Upstream config",
      "：添加上游并选好「active」。": ": add an upstream and pick \\\\\\\"active\\\\\\\".",
      "2. 客户端 base_url 指到上面的地址，api-key 填": "2. Point the client's base_url to the address above, and fill api-key with",
      "Codex / OpenAI 客户端": "Codex / OpenAI clients",
      "完全透传模式": "Full passthrough mode",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的": "The relay does not convert or route by active — it forwards your request as-is to the address in api-key:",
      "目标地址": "대상 주소",
      "原样直达 —— 连设置里没配过的上游也能直接用。api-key 格式：": ", as-is — even upstreams not configured in Settings work directly. api-key format:",
      "目标地址@@上游key": "target@@upstream_key",
      "设置 → 中继模式": "Settings → Relay mode",
      "：切到「完全透传」。": ": switch to \\\\\\\"Full passthrough\\\\\\\".",
      "2. 客户端照旧连上面的中继地址，但 api-key 填": "2. The client still connects to the address above, but fill api-key with",
      "，请求路径随便。": " — request path is up to you.",
      "透传 curl 示例": "Passthrough curl example",
      "高级：当前上游 · 快捷切换 · 已配置上游": "Advanced: active upstream · quick switch · configured upstreams",
      "当前 active 上游": "Current active upstream",
      "「auto」请求会路由到这里。": "\\\\\\\"auto\\\\\\\" requests route here.",
      "尚未配置 active 上游": "No active upstream configured",
      "已配置上游": "설정된 업스트림",
      "点击任意上游行立即切换为该平台的 active 上游。": "Click any upstream row to switch it to be that platform's active upstream.",
      "未配置上游 —— 到「设置 → 上游配置」新建": "No upstreams — create one in \\\\\\\"Settings → Upstream config\\\\\\\"",
      "自定义（手动填写全部字段）": "Custom (fill all fields manually)",
      "DeepSeek 官方（Anthropic 协议）": "DeepSeek official (Anthropic protocol)",
      "OpenCode Go 订阅（Anthropic 协议）": "OpenCode Go subscription (Anthropic protocol)",
      "OpenCode Zen 按量付费（OpenAI 协议）": "OpenCode Zen pay-as-you-go (OpenAI protocol)",
      "OpenCode Zen 免费模型（OpenAI 协议）": "OpenCode Zen free models (OpenAI protocol)",
      "HuoShan Agent Plan（Anthropic 协议）": "HuoShan Agent Plan (Anthropic protocol)",
      "— 请选择模型 —": "— pick a model —",
      "按次数计费": "횟수별 과금",
      "按 Token 计费": "Token 과금",
      "OpenCode Go 协议适配": "OpenCode Go protocol adaptation",
      "测试日志": "테스트 로그",
      "次": "회",
      "tokens": "tokens",
      "额度已用尽": "쿼터 소진",
      "预计": "예상",
      "用尽": "소진",
      "非允许模型": "비허용 모델",
      "周": "주",
      "月": "월",
      "5h": "5h",
      "累计 tokens": "누적 tokens",
      "5h 释放": "5h 해제",
      "删除这条上游配置": "Delete this upstream config",
      "点击激活此上游；已是当前上游时点击进入编辑": "Click to activate this upstream; click again to edit when it's already active",
      // v0.152：설정 화면「가이드」문구 재작성 —— JSON 비교 + 왜 @@ 포맷?
      "转换模式": "변환 모드",
      "默认 · 推荐": "기본 · 권장",
      "是什么：": "개요:",
      "中继做「路由 + 协议转换」，请求经 active 上游转发、用量计入统计。api-key 填 auto 或留空 = 中继自动走 active 上游；填某上游的真实 key = 指名直连该上游。": "중계가「라우팅 + 프로토콜 변환」을 수행하고, 요청은 active 업스트림으로 전달되며 사용량은 통계에 기록됩니다. api-key에 auto 또는 빈 값 = 중계가 active 업스트림 자동 선택; 특정 업스트림의 실제 키 지정 = 해당 업스트림에 직접 연결.",
      "设置 → 上游配置": "설정 → 업스트림 구성",
      "：添加上游并选好「active」。": ": 업스트림을 추가하고「active」로 지정.",
      "客户端 base_url 指到上面的地址，api-key 填 auto。": "클라이언트의 base_url을 위 주소로 지정하고, api-key는 auto.",
      "官方 Claude Code settings.json": "공식 Claude Code settings.json",
      "走中继（仅改 2 行）": "중계 경유 (2줄만 변경)",
      "完全透传模式": "완전 패스스루 모드",
      "中继不做转换、不按 active 路由，把请求按你写进 api-key 的目标地址原样直达 —— 连设置里没配过的上游也能直接用。": "중계는 변환이나 라우팅을 하지 않고, api-key에 작성한 대상 주소로 요청을 그대로 전달합니다 —— 설정에 등록되지 않은 업스트림도 직접 사용 가능.",
      "为什么用 @@ 格式？": "왜 @@ 포맷?",
      "透传下 base_url 仍要指向中继（这样中继才能记账），这会占用原本指向提供商上游的 url 字段。解决办法：把上游 url 和 api-key 都塞进 api-key 一栏，格式 = 目标地址 + @@ + 真实 key。": "패스스루에서도 base_url은 중계를 가리켜야 합니다(사용량 기록을 위해). 이는 원래 업스트림 URL을 넣을 위치를 차지합니다. 해결책: 업스트림 URL과 api-key를 api-key 필드에 합쳐 형식 = 대상 URL + @@ + 실제 키.",
      "1. 客户端发": "1. 클라이언트 전송",
      "base_url = 中继地址": "base_url = 중계 주소",
      "2. 中继拆": "2. 중계 분리",
      "拆出": "분리",
      "url = 上游地址": "url = 업스트림 주소",
      "原 key": "원래 key",
      "3. 上游收": "3. 업스트림 수신",
      "转发到上游": "업스트림으로 전달",
      "请求直达，不路由": "직접 전달, 라우팅 없음",
      "走中继（透传）": "중계 경유 (패스스루)",
      "高级：curl 原生调用": "고급: curl 원시 호출",
      "curl /v1/chat/completions（OpenAI 入口 → DeepSeek）": "curl /v1/chat/completions (OpenAI 진입 → DeepSeek)",
      "curl /v1/messages（Anthropic 入口 → DeepSeek Anthropic 兼容）": "curl /v1/messages (Anthropic 진입 → DeepSeek Anthropic 호환)",
      // v0.152 二改：極簡欄位列表替代流程圖
      "模型、上游全由中继在「设置 → 上游配置」里选，客户端只填这 3 个字段：": "모델·업스트림은 중계의「설정 → 업스트림 구성」에서 선택. 클라이언트는 다음 3개 필드만 설정:",
      "Anthropic 客户端": "Anthropic 클라이언트",
      "OpenAI 客户端": "OpenAI 클라이언트",
      "上游地址": "업스트림 주소",
      "真实 key": "실제 키",
      "实际模型": "실제 모델",
      "示例：Claude Code settings.json（官方 → 走中继）": "예: Claude Code settings.json (공식 → 중계 경유)",
      "官方": "공식",
      "走中继": "중계 경유",
      // v0.152 종판: README.docx 원문에 준거（중요 용어는 <strong>，나머지는 통째로 dict）
      "转换模式下，模型、上游的选择完全由中继控制，来自 coding 客户端的所有请求，先到达中继，由中继接管，并向中继中选择的上游发送。": "변환 모드에서는 모델·업스트림 선택이 모두 중계에 의해 제어됩니다. coding 클라이언트의 모든 요청은 먼저 중계에 도달하고, 중계가 인계받아 중계에서 선택한 업스트림으로 전송합니다.",
      "客户端配置只需要将 URL 地址指向中继，然后将 api-key 和模型配置为": "클라이언트는 URL을 중계로 향하게 하고, api-key와 모델을",
      "占位符即可。": "로 설정하면 됩니다.",
      "客户端配置：": "클라이언트 구성:",
      "透传模式下，为了统计 token 和保存数据，仍然需要让数据包经过中继，因而 base_url 仍需指向中继。": "패스스루 모드에서는 토큰 통계와 데이터 저장을 위해 패킷이 중계를 거쳐야 하므로, base_url은 계속 중계를 가리킵니다.",
      "这会占用原本指向提供商上游的 url 的位置，对于此问题的解决办法是：将": "이는 원래 공급자 업스트림 URL을 가리키던 위치를 차지합니다. 해결책:",
      "上游 url": "업스트림 URL",
      "和": "과(와)",
      "都写在 api-key 的填写位置，采用如下格式：": "를 모두 api-key 입력란에 작성하고, 다음 형식을 사용합니다:",
      "此数据包到达中继后，中继会从 AUTH_TOKEN ( Api_Key ) 中解析字符，将 Api_Key 字段重写为“@@”标识符后半部分的 key，然后向前半部分 URL 发送数据包。": "이 패킷이 중계에 도달하면, 중계는 AUTH_TOKEN ( Api_Key )에서 문자를 파싱하여, Api_Key 필드를「@@」구분자 뒷부분의 key로 재작성하고, 앞부분 URL로 패킷을 전송합니다.",
      "示例：": "예시:",
      "Deepseek 的请求地址（来自官网信息）": "Deepseek 요청 주소 (공식 문서 정보)",
      "（示例）": " (예시)",
      "不走中继时的请求填写（以 claude code 为例）：": "중계를 거치지 않을 때의 요청 작성 (claude code 예시):",
      "不走中继时的请求填写": "중계를 거치지 않을 때의 요청 작성",
      "走中继时，该请求需要改写为：": "중계를 거칠 때, 이 요청을 다음과 같이 재작성합니다:",
      "走中继时的请求填写": "중계를 거칠 때의 요청 작성",
      "查询中…": "조회 중…",
      "不可用": "사용 불가",
      "滚动": "롤링",
      "余额": "잔액",
      "失败": "실패",
      "磁盘": "디스크",
      "内存": "메모리",
      "网络": "네트워크",
      // v0.154：cfg / stats / overview 보충 key
      "地址 URL": "주소 URL",
      "auto 兜底模型": "auto 폴백 모델",
      "客户端发 model=\"auto\" 时用此值替换。留空则按允许模型列表的第一个,再不行原样透传": "클라이언트가 model=\"auto\"를 보내면 이 값으로 대체합니다. 비워 두면 허용 모델 목록의 첫 번째를 사용하고, 그마저도 안 되면 그대로 전달합니다.",
      "5 小时额度": "5시간 쿼터",
      "输入模型名后回车": "모델 이름을 입력하고 Enter",
      "链接上游（合并统计）": "업스트림 연결 (통계 병합)",
      "链接后所有统计页视为一个虚拟上游；不影响路由和计费；A+B 与 A+C 自动并为 A+B+C": "연결 후 모든 통계 페이지가 하나의 가상 업스트림으로 취급됩니다. 라우팅과 과금에는 영향이 없습니다. A+B와 A+C는 자동으로 A+B+C로 병합됩니다.",
      "+ 新建链接": "+ 새 연결",
      "任意模型": "임의 모델",
      "最大模型": "최대 모델",
      "单模型 30 天调用分布": "단일 모델 30일 호출 분포",
      "小": "소",
      "中": "중",
      "大": "대",
      "隐藏整个顶栏（运行状态 + 按钮行），界面更干净；可从顶栏按钮或本开关恢复": "상단 막대 전체(실행 상태 + 버튼 행)를 숨겨 화면을 깔끔하게 만듭니다. 상단 막대 버튼이나 이 스위치로 복원할 수 있습니다.",
      "在顶部状态栏显示\"客户端请求的模型 → 实际调用的模型\"对照": "상단 상태 표시줄에「클라이언트가 요청한 모델 → 실제 호출된 모델」대조 표시",
      "（未配置上游模型）": "(미구성된 업스트림 모델)",
      "释放": "해제",
      "调用": "호출",
    },
  };
  function _i18nKey(s) { return (s || "").replace(/\s+/g, " ").trim(); }
  // v0.151：5 种语言（zh/en/ja/ko/zh-TW）—— 取当前 lang 的字典，无则回退
  function _curDict() { return I18N[I18N.lang] || I18N.ja; }
  // v0.153：跨非 zh 语言重译时，cur/txt 可能是「上一语言」字典的译文
  // （zh-TW/ja/ko 译文含 CJK 汉字），会被重捕的「含中文」正则误判成新中文
  // 源、覆盖 __i18nZh 缓存，导致后续查字典全 miss（表现：en→ja 成功后再
  // 切 ko 卡在 ja）。txt 是 key 在任一非 zh 语言字典里的 value 即视为
  // 已知译文、不得重捕；zh 字典 value 是中文原文本身，不参与判断（否则
  // render 新写的中文会因恰等于某 key 而被误判成译文）。
  function _isKnownTranslation(key, txt) {
    return I18N.en[key] === txt || I18N["zh-TW"][key] === txt
      || I18N.ja[key] === txt || I18N.ko[key] === txt;
  }
  function t(key) { return (I18N.lang !== "zh" && _curDict()[key]) || key; }
  function setSegLang(lang) {
    // v0.172：用 I18N.intent（用户意图，包括 "auto"）做按钮高亮，
    // 而不是 lang（具体语言值）。这样选「跟随系统」时按钮始终亮着，
    // 无论 navigator.language 解析到哪种具体语言。
    const intent = I18N.intent;
    document.querySelectorAll(".seg-lang").forEach((seg) =>
      seg.querySelectorAll(".seg-btn").forEach((b) =>
        b.classList.toggle("active", b.dataset.lang === intent),
      ),
    );
  }
  function applyLang() {
    // v0.151：5 种语言 —— html[lang] 用 BCP-47 标记；body 加 lang-xx 类供
    // CSS 微调（lang-ja 日文字号偏大、lang-ko 空格偏宽等场景预留）。
    const htmlLang = I18N.lang === "zh" ? "zh-CN"
      : I18N.lang === "zh-TW" ? "zh-TW"
      : I18N.lang;
    document.documentElement.lang = htmlLang;
    document.body.classList.remove("lang-en", "lang-ja", "lang-ko", "lang-zh-tw");
    if (I18N.lang !== "zh") {
      document.body.classList.add(`lang-${I18N.lang.toLowerCase()}`);
    }
    setSegLang(I18N.intent);
    // v0.113r (sweep)：两段 —— (1) 元素级 data-i18n 显式映射解决同名 key
    // 撞车（按钮「启动」 vs 分组「启动」）；(2) 全文档叶子文本节点扫描，
    // 命中 dict 即翻译。这样天然覆盖未知选择器与多层嵌套文本，无需逐
    // 元素登记；保留原 I18N_SELECTOR 作锚点（命中元素才扫子树），避免
    // 每 tick 全文档 walker（chart 的 svg 文本里可能含数字/单位，遍历
    // 起来 N 大）。
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const over = el.getAttribute("data-i18n");
      let zh0 = el.__i18nZh;
      const cur = _i18nKey(el.textContent);
      if (!cur) return;
      if (zh0 == null) { zh0 = cur; el.__i18nZh = zh0; }
      const enVal = _curDict()[over];
      // 「内容变了就重新捕获」：仅当 cur 既不是原 zh、也不是已翻出的 enVal、
      // 也不是任一语言已知译文时才视作新原文（render 重写）。cur === enVal
      // 意味着上次刚翻过，不能覆盖 zh0 —— 否则切回 zh 时 zh0=英文，无法还原。
      // v0.153：跨非 zh 语言时 cur 可能是上一语言译文（zh-TW/ja/ko 译文含
      // 汉字），同样不能覆盖 zh0，否则缓存被污染成译文、后续查字典全 miss。
      if (cur !== zh0 && cur !== enVal && /[\u4e00-\u9fff]/.test(cur)
        && !_isKnownTranslation(over, cur)) {
        el.__i18nZh = cur; zh0 = cur;
      }
      if (I18N.lang !== "zh") {
        if (enVal && el.textContent !== enVal) el.textContent = enVal;
      } else if (el.textContent !== zh0) {
        el.textContent = zh0;
      }
    });
    // 全文档叶子翻译：只走 .view 内的可见视图与全局 chrome，跳过 __report、
    // 脚本、style、语言指示按钮（显示「简体中文/English」原文为正例）。
    const roots = [];
    document.querySelectorAll(".view").forEach((v) => { if (!v.hidden) roots.push(v); });
    document.querySelectorAll("body > *").forEach((n) => {
      if (n.id === "__report") return;
      if (n.tagName === "SCRIPT" || n.tagName === "STYLE") return;
      if (!n.matches(".view")) roots.push(n);
    });
    roots.forEach((root) => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(n) {
          if (!n.parentNode || n.parentNode.nodeType !== 1) return NodeFilter.FILTER_REJECT;
          const p = n.parentNode;
          // 跳过被 data-i18n 元素包裹（元素级已处理）
          if (p.closest && p.closest("[data-i18n]")) return NodeFilter.FILTER_REJECT;
          // v0.154：上游 / 模型名称不翻译 —— 名称节点打 data-i18n-keep
          // 标记（混合内容里只包住值）；.cfg-chip 只会放名称类用户数据，
          // 整体跳过，避免「任意模型→任意Model」这类半截翻译。
          if (p.closest && p.closest("[data-i18n-keep], .cfg-chip")) return NodeFilter.FILTER_REJECT;
          const _mdChip = p.closest && p.closest(".stats-md-chip");
          if (_mdChip && _mdChip.dataset.model !== "__all__") return NodeFilter.FILTER_REJECT;
          // 语言指示按钮：data-lang 节点保留自身原文（zh 显示「简体中文」是预期）
          if (p.classList && p.classList.contains("seg-btn") && p.dataset && p.dataset.lang) return NodeFilter.FILTER_REJECT;
          if (p.parentElement && p.parentElement.classList && p.parentElement.classList.contains("seg-lang")) return NodeFilter.FILTER_REJECT;
          // 跳过 <input value> / 之类（nextSibling 不是 textNode 不受影响）
          const txt = (n.nodeValue || "");
          // zh→非 zh 翻译：挑含中文的节点；非 zh→zh 还原：挑有缓存 zh 且当前不是
          // 中文（已被翻成外文）的节点 —— 还原不能用「含中文」过滤，否则
          // 翻后的外文节点永远不被访问、无法还原。
          // v0.153：多语言（5 语言）后必须支持「非 zh → 非 zh」重译 ——
          // 已捕获过中文原文的节点（__i18nZh != null）无论当前是中文还是
          // 上一种语言的译文都放行：译文经循环按 __i18nZh 重查当前语言字典；
          // 仍是中文原文（txt === __i18nZh，比如刚从 zh 还原回中文、或 render
          // 刚写回中文）时同样需要翻译成当前语言，不能 reject。从未捕获过
          // 的节点仍只挑含中文的当候选源。旧过滤只挑「含中文」的节点：首次
          // zh→en 翻完后节点变英文，再切 ja/ko/zh-TW 时被当「无中文」跳过，
          // 永远卡在 en 的译文里。
          if (I18N.lang !== "zh") {
            if (n.__i18nZh == null) {
              if (!txt || !/[\u4e00-\u9fff]/.test(txt)) return NodeFilter.FILTER_REJECT;
            }
          } else {
            if (n.__i18nZh == null) return NodeFilter.FILTER_REJECT;
            if (txt === n.__i18nZh) return NodeFilter.FILTER_REJECT;
          }
          // 可见性判定：hidden 属性 + offsetParent 双判。offsetParent 对
          // position:fixed（modal overlay 等）始终返回 null —— 不算隐藏；
          // 但 layout 内祖先带 [hidden] 时整个子树应跳过。
          let hidden = false;
          for (let a = p; a && a !== document.body; a = a.parentElement) {
            if (a.hidden) { hidden = true; break; }
          }
          if (hidden) return NodeFilter.FILTER_REJECT;
          // display:none 的祖先：walk up 看 computed style（getComputedStyle
          // 在大规模 walker 里偏重；用 offsetParent 仅做弱提示）。
          let offscreen = false;
          for (let a = p; a && a !== document.body; a = a.parentElement) {
            const cs = getComputedStyle(a);
            if (cs.display === "none" || cs.visibility === "hidden") { offscreen = true; break; }
          }
          if (offscreen) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let node;
      while ((node = walker.nextNode())) {
        const txt = node.nodeValue;
        // 缓存策略：__i18nZh = 上次捕获到的「中文原文」（用于 zh→en 翻与
        // en→zh 还原）；脚本动态改写（renderStats/renderTopModelSpotlight
        // 反复写 "暂无数据" / "总计"）会把节点值改为新内容 —— 若新值
        // 仍是中文（render 写的就是中文），视为新原文覆盖缓存；若新值是
        // 英文（applyLang 刚翻出来的），则保留原 zh 不被污染。
        if (node.__i18nZh === undefined || node.__i18nZh == null) {
          node.__i18nZh = txt;
        } else if (txt !== node.__i18nZh && /[\u4e00-\u9fff]/.test(txt)
          && !_isKnownTranslation(_i18nKey(node.__i18nZh), txt)) {
          // render 函数写入了新中文 → 重新捕获。但必须排除译文 —— zh-TW/ja/ko
          // 译文本身含 CJK，会被汉字正则误判成新中文源，把缓存覆盖成译文导致
          // 后续切不回去（表现：en→ja 成功后再切 ko 卡在 ja）。v0.153 把判断
          // 从「当前语言期望译文」（_curDict 只查一种语言，跨语言时 txt 是上
          // 一种语言译文必然不等于当前语言期望、必然误判）升级为「任一非 zh
          // 语言字典里该 key 的已知译文」——是译文则不重捕；render 新写的中文
          // 不在任何译文表里，照常重捕。只有「既不是缓存原文、也不是已知译文」
          // 时才当作新中文源重捕。
          node.__i18nZh = txt;
        }
        if (I18N.lang !== "zh") {
          let out = null;
          const dict = _curDict();
          const whole = _i18nKey(node.__i18nZh);
          const enWhole = dict[whole];
          if (enWhole) { out = enWhole; }
          else {
            let pos = 0, cur = node.__i18nZh;
            while (pos < cur.length) {
              const start = cur.slice(pos).search(/[\u4e00-\u9fff]/);
              if (start < 0) break;
              const absStart = pos + start;
              let best = null;
              for (let n = Math.min(cur.length - absStart, 30); n >= 2; n--) {
                const cand = _i18nKey(cur.substr(absStart, n));
                if (dict[cand]) { best = { len: n, en: dict[cand] }; break; }
              }
              if (!best) { pos = absStart + 1; continue; }
              cur = cur.slice(0, absStart) + best.en + cur.slice(absStart + best.len);
              pos = absStart + best.en.length;
            }
            out = cur !== node.__i18nZh ? cur : null;
          }
          if (out != null && node.nodeValue !== out) node.nodeValue = out;
        } else {
          // 切回 zh：按缓存原文还原
          if (node.nodeValue !== node.__i18nZh) node.nodeValue = node.__i18nZh;
        }
      }
    });
  }

  // v0.113r：异步渲染兜底 —— renderStats（每 tick async 重跑）等渲染函数在
  // await 完成后才写中文空态/图表标题，tick 里的同步 applyLang 追不上，导致
  // stats 空态永远停在中文。这里用 MutationObserver：任何 DOM 文本/结构变更
  // 后（setTimeout 去抖）重跑一次 applyLang。applyLang 幂等 —— 已翻成英文的
  // 节点再跑不产生新 mutation，不会自激循环。去抖用 setTimeout 而非 rAF：
  // headless 探针（--virtual-time-budget）下 rAF 不触发，setTimeout 才保证
  // 兜底在探针里也被验证。
  if (!window.__relayI18nMO) {
    let _reapplyTimer = 0;
    window.__relayI18nMO = new MutationObserver(() => {
      if (_reapplyTimer) return;
      _reapplyTimer = setTimeout(() => {
        _reapplyTimer = 0;
        applyLang();
      }, 0);
    });
    window.__relayI18nMO.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  let prefsRendered = false;
  let _wpGlobalBound = false;
  // v0.169：保留高级设置折叠组里用户改过的值 —— 设置页重渲染（离开后
  // 重入 / 主题切换等触发 body.innerHTML 重建）时，先 snapshot 旧 DOM
  // 上的 wheel-picker 中行（rows[1]）和嵌套 input/checkbox 状态，再
  // 用该快照覆盖 snap 给的默认值；否则 snap 若尚未包含最新值（350ms
  // 防抖 + 500ms 轮询还没到），新 wheel picker 就会"复位到默认"，
  // 用户看上去像被重置了。
  let _prefsDirtySnapshot = null;
  // v0.113k/l/m：自绘「滚轮选择器」—— 设置页超时时间三档。
  // 原生 number input 没有轮盘感；自绘三条值 + 中间高亮带，iOS 滚轮观感。
  // v0.113m：鼠标移入展开、移出自动收缩；不悬停就是一格。
  //   展开态：滚轮（±1）/ 点击上下半区 / 拖动（1px 换 1 值）。
  //   互斥：一次只开一个；mouseleave 150ms 延迟收缩（划过行不闪）。
  // 范围 1-600 秒；变更防抖 350ms 写回（沿用 setter 返回 {seconds} 的口径，
  // 服务端若纠正用 silent set 回写显示）。el.__wpSet(v, silent) 供外部取值。
  // 设计决定：不做惯性滚动（600 档、步进 1，无惯性反而可控）；切值即防抖持久化。
  // v0.167：通用化 —— opts={min, max, unit, valueKey} 可覆盖默认（默认仅用于
  // 超时三档：1-600、"s"、{seconds}）。数组型 spin（如「最多显示的并行数量」
  // 1-8、无单位、{max}）传入 {min:1, max:8, unit:"", valueKey:"max"}。
  function wheelPicker(el, setter, initial, opts) {
    opts = opts || {};
    const MIN = opts.min != null ? opts.min : 1;
    const MAX = opts.max != null ? opts.max : 600;
    const UNIT = opts.unit != null ? opts.unit : "s";
    const VALUE_KEY = opts.valueKey || "seconds";
    const ROW = 24;
    el.innerHTML = "";
    const band = document.createElement("div");
    band.className = "wheel-picker-band";
    el.appendChild(band);
    const rows = [];
    for (let i = 0; i < 3; i++) {
      const r = document.createElement("div");
      r.className = "wheel-picker-row" + (i === 1 ? " is-cur" : "");
      el.appendChild(r);
      rows.push(r);
    }
    const unit = document.createElement("span");
    unit.className = "wheel-picker-unit";
    unit.textContent = UNIT;
    if (!UNIT) unit.style.display = "none";
    el.appendChild(unit);
    el.setAttribute("role", "spinbutton");
    el.setAttribute("aria-valuemin", String(MIN));
    el.setAttribute("aria-valuemax", String(MAX));
    el.setAttribute("aria-expanded", "false");

    let _v = Math.max(MIN, Math.min(MAX, Math.round(initial == null ? 1 : initial)));
    let _timer = null;
    function render() {
      rows[0].textContent = _v > MIN ? String(_v - 1) : "";
      rows[1].textContent = String(_v);
      rows[2].textContent = _v < MAX ? String(_v + 1) : "";
      el.setAttribute("aria-valuenow", String(_v));
      el.title = String(_v) + (UNIT === "s" ? " 秒" : "") + "（悬停展开 / 滚轮或拖动调节）";
    }
    function isOpen() { return el.classList.contains("wp-open"); }
    function closeAll() {
      document.querySelectorAll(".wheel-picker.wp-open").forEach((p) => {
        p.classList.remove("wp-open");
        p.setAttribute("aria-expanded", "false");
      });
    }
    function open() {
      closeAll();
      el.classList.add("wp-open");
      el.setAttribute("aria-expanded", "true");
    }
    function set(v, silent) {
      v = Math.max(MIN, Math.min(MAX, Math.round(v)));
      if (v === _v) return;
      _v = v;
      render();
      if (silent) return;
      if (_timer) clearTimeout(_timer);
      _timer = setTimeout(async () => {
        _timer = null;
        try {
          const res = await setter(_v);
          const rv = res && typeof res[VALUE_KEY] === "number" ? res[VALUE_KEY] : null;
          if (rv != null && rv !== _v) set(rv, true);
        } catch (e) { console.error("[wheel-picker] persist failed", e); }
      }, 350);
    }
    // v0.113m：悬停展开 / 移出自动收缩（150ms 延迟防划过闪）。
    let _leaveTimer = null;
    el.addEventListener("mouseenter", () => {
      if (_leaveTimer) { clearTimeout(_leaveTimer); _leaveTimer = null; }
      open();
    });
    el.addEventListener("mouseleave", () => {
      if (_leaveTimer) clearTimeout(_leaveTimer);
      _leaveTimer = setTimeout(() => {
        _leaveTimer = null;
        closeAll();
      }, 150);
    });
    // 展开态：点击上下半区 ±1（不再负责展开/收起）
    el.addEventListener("click", (e) => {
      if (el._wpSuppressClick) { el._wpSuppressClick = false; return; }
      if (!isOpen()) return;
      const r = el.getBoundingClientRect();
      set(_v + (e.clientY - r.top < r.height / 2 ? -1 : 1));
    });
    el.addEventListener("wheel", (e) => {
      if (!isOpen()) return;
      e.preventDefault();
      set(_v + (e.deltaY > 0 ? 1 : -1));
    }, { passive: false });
    let drag = null;
    el.addEventListener("pointerdown", (e) => {
      if (!isOpen()) return;
      drag = { y: e.clientY, v: _v, moved: false };
      try { el.setPointerCapture(e.pointerId); } catch (err) {}
    });
    el.addEventListener("pointermove", (e) => {
      if (!drag || !isOpen()) return;
      if (Math.abs(e.clientY - drag.y) > 3) drag.moved = true;
      set(drag.v + Math.round((drag.y - e.clientY) / ROW));
    });
    el.addEventListener("pointerup", () => {
      if (drag) { el._wpSuppressClick = drag.moved; drag = null; }
    });
    el.addEventListener("pointercancel", () => { drag = null; });
    // 全局（只绑一次）：点轮盘外部 / Esc 收起所有
    if (!_wpGlobalBound) {
      _wpGlobalBound = true;
      document.addEventListener("click", (e) => {
        if (e.target && e.target.closest && e.target.closest(".wheel-picker")) return;
        closeAll();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        closeAll();
      });
    }
    render();
    el.__wpSet = set;
    return set;
  }

  // v0.169：snapshot 旧 DOM 上的用户值 —— 抓 wheel-picker 中行（rows[1]
  // 的 textContent = 当前 _v）、嵌套数字输入、嵌套 checkbox。重渲染
  // 后 mountTimeoutWheel / refreshPrefsDynamic 会优先用这里的值覆盖 snap
  // 默认值。仅看"高级折叠组"内元素：其它顶层 input 已在每次 render 后从
  // snap/bridge 拉到最新，没必要再覆盖。
  function _snapshotPrefsValues(body) {
    const snap = {};
    if (!body) return snap;
    // wheel-picker 通用 —— 用 data-timeout / data-max 作稳定 key。
    body.querySelectorAll(".wheel-picker").forEach((el) => {
      const cur = el.querySelector(".wheel-picker-row.is-cur");
      const v = cur ? parseInt(cur.textContent, 10) : NaN;
      if (isNaN(v)) return;
      const k = el.dataset.timeout || el.dataset.max;
      if (k) snap["wp:" + k] = v;
    });
    // 嵌套数字输入（高级折叠组内）：tools-cap / ep-list-vh / min-cols
    body.querySelectorAll("#prefs-live-panel-style-advanced-panel input[type=\"number\"]").forEach((el) => {
      const n = parseFloat(el.value);
      if (!isNaN(n) && el.className) snap["num:" + el.className] = n;
    });
    // 嵌套 checkbox：tools-always / no-panel-frame
    body.querySelectorAll("#prefs-live-panel-style-advanced-panel input[type=\"checkbox\"]").forEach((el) => {
      if (el.className) snap["chk:" + el.className] = !!el.checked;
    });
    // seg 三档（list-font）：读 active 那个 dataset.listFont
    const activeFont = body.querySelector("#prefs-live-panel-style-advanced-panel .seg-list-font .seg-btn.active");
    if (activeFont && activeFont.dataset.listFont) snap["seg:list-font"] = activeFont.dataset.listFont;
    return snap;
  }

  function renderSettingsPrefs(body, snap) {
    if (!body || prefsRendered) return;
    prefsRendered = true;
    // v0.169：先 snapshot 旧 DOM 上的高级折叠组用户值（wheel picker /
    // input / checkbox），下次重渲染时优先用快照而非 snap 默认值，避免
    // "刚改的又被刷回默认"的问题。首帧时 body 为空，snapshot 为空对象。
    _prefsDirtySnapshot = _snapshotPrefsValues(body);
    body.innerHTML = `
      <div class="settings-list">

        <div class="settings-group">
          <div class="settings-group-label">外观</div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">主题</div>
              <div class="settings-item-hint">界面的整体配色方案</div>
            </div>
            <div class="settings-item-control">
              <div class="seg seg-theme">
                <button type="button" class="seg-btn" data-theme-name="light">浅色</button>
                <button type="button" class="seg-btn" data-theme-name="day">暖白</button>
                <button type="button" class="seg-btn" data-theme-name="dark">深色</button>
              </div>
            </div>
          </div>
          <!-- v0.113n：语言 —— 纯前端偏好（localStorage），i18n 钩子。 -->
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">语言</div>
              <div class="settings-item-hint">界面显示语言（当前仅设置页生效，全量翻译陆续加入）</div>
            </div>
            <div class="settings-item-control">
              <div class="seg seg-lang">
                <button type="button" class="seg-btn" data-lang="auto">跟随系统</button>
                <button type="button" class="seg-btn" data-lang="zh">简体中文</button>
                <button type="button" class="seg-btn" data-lang="zh-TW">繁體中文</button>
                <button type="button" class="seg-btn" data-lang="en">English</button>
                <button type="button" class="seg-btn" data-lang="ja">日本語</button>
                <button type="button" class="seg-btn" data-lang="ko">한국어</button>
              </div>
            </div>
          </div>
          <!-- v0.113：无边框 —— 开启后总览页所有卡片去掉玻璃容器，裸悬浮
               在底背景上。纯前端偏好（localStorage）。 -->
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">无边框</div>
              <div class="settings-item-hint">开启后总览页所有卡片去掉玻璃容器，裸悬浮在底背景上</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-no-frame" ${prefs.noFrame ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <!-- v0.135：侧栏无边框已挪到「实时栏管理」→「样式高级设置」子组下。 -->
        </div>

        <!-- v0.104 实时栏管理 —— 集中放置所有实时栏相关设置。
             v0.112j：删掉顶部重复的「实时流侧栏」开关（保留「外观」组那
             一份）；本组默认隐藏，只有「外观」组的实时流侧栏开启才显示
             （syncLivePanelMgmtGroup 控制）。
             v0.174：主开关「实时流侧栏」挪到本组最顶；本组默认 visible。 -->
        <div class="settings-group" id="live-panel-mgmt-group">
          <div class="settings-group-label" data-i18n="实时栏管理">实时栏管理</div>
          <!-- v0.89 实时流侧栏开关 —— 与顶栏快捷图标共享同一个后端开关。
               v0.174：从「外观」组挪到本组最顶，作为整个实时栏管理的总开关。
               开启后右侧会出现独立窗口，跟随主窗移动；显示 api-key 明文
               + 入/出向 wire + 流式 token + 思考 + 工具调用。 -->
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">实时流侧栏</div>
              <div class="settings-item-hint">开启后右侧出现独立小窗，实时显示当前请求的思考过程和正文，方便调试</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-live-panel" ${snap && snap.live_panel ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <!-- v0.176：实时流侧栏主开关管理本组所有子项 —— 关闭即整块折叠；
               悬浮球置顶额外由悬浮球开关控制显隐。 -->
          <div id="live-panel-mgmt-sub" ${snap && snap.live_panel ? "" : "hidden"}>
          <!-- v0.165：悬浮球 —— 桌面可拖动小球作为侧栏锚点，侧栏出现时从球位置
               展开。开启后始终开启 OFF 时球常驻桌面。 -->
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="悬浮球">悬浮球</div>
              <div class="settings-item-hint" data-i18n="桌面悬浮球作为侧栏锚点，侧栏从球位置展开">桌面悬浮球作为侧栏锚点，侧栏从球位置展开</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-live-panel-float-ball" ${snap && snap.float_ball ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <!-- v0.170：悬浮球置顶 —— 球与侧栏一起置顶、盖过其它窗口；球>侧栏由相对
               Z 序保活恒成立（与开关无关）。关掉则两者一起降到普通层级（仍球在侧栏之上）。
               v0.176：仅在悬浮球开关开启时显示。 -->
          <div class="settings-item settings-item-nested" id="prefs-live-panel-float-ball-topmost-item" ${snap && snap.float_ball ? "" : "hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="悬浮球置顶">悬浮球置顶</div>
              <div class="settings-item-hint" data-i18n="悬浮球与侧栏始终置顶，不被其它窗口遮挡">悬浮球与侧栏始终置顶，不被其它窗口遮挡</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-live-panel-float-ball-topmost" ${snap && snap.float_ball_topmost !== false ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="允许并发显示">允许并发显示</div>
              <div class="settings-item-hint" data-i18n="多个并发请求同时各自弹出实时栏">多个并发请求同时各自弹出实时栏</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-live-panel-concurrent" ${snap && snap.live_panel_concurrent ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>

          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="最多显示的并行数量">最多显示的并行数量</div>
            </div>
            <div class="settings-item-control">
              <!-- v0.167：改为滚轮选择器（wheelPicker，复用超时三档自绘组件的
                   通用化，1-8 无单位，{max} 口径）。替代原生 <input type=number>，
                   悬停展开、滚轮/点上下/拖动切值。 -->
              <div class="prefs-live-panel-max-input wheel-picker" data-max="max"></div>
            </div>
          </div>
          <!-- v0.175：侧边磁吸彻底移除 —— 「磁吸状态下始终开启一个」选项不再展示，
               仅保留悬浮球相关选项。逻辑代码保留（gui.py 磁吸/dock + app.js always-one
               绑定 + API），未来要恢复磁吸时：取消注释下方块 + 还原 cache stamp。
               <div class="settings-item">
                 <div class="settings-item-info">
                   <div class="settings-item-title" data-i18n="磁吸状态下始终开启一个">磁吸状态下始终开启一个</div>
                   <div class="settings-item-hint" data-i18n="磁吸模式下无论有无请求都保留一个空闲实时栏">磁吸模式下无论有无请求都保留一个空闲实时栏</div>
                 </div>
                 <div class="settings-item-control">
                   <label class="switch">
                     <input type="checkbox" class="prefs-live-panel-always-one" ${snap && snap.live_panel_always_one ? "checked" : ""} />
                     <span class="switch-track"><span class="switch-thumb"></span></span>
                   </label>
                 </div>
               </div>
          -->

          <!-- v0.130：自动延展侧栏（宽度）—— 并发请求多时，实时栏窗口按容器
               列数自动加宽，让更多并发同时可见。 -->
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="自动延展侧栏">自动延展侧栏</div>
              <div class="settings-item-hint" data-i18n="并发请求多时，实时栏窗口自动加宽显示更多并发">并发请求多时，实时栏窗口自动加宽显示更多并发</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-live-panel-auto-extend" ${snap && snap.live_panel_auto_extend ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>


          <!-- v0.135：「等待时间高级设置」开关 —— 默认关闭，收纳 5 个超时项（思考流 /
               思考-正文衔接 / 正文流 / 完成清除 / 工具清除）。开启才展开。
               收纳策略：超时类（按时间触发）归「等待时间」分组，布局/外观类
               归「样式高级设置」（见下）。 -->
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="等待时间高级设置">等待时间高级设置</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-live-panel-timeout-advanced" />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item settings-item-nested adv-switch-panel" id="prefs-live-panel-timeout-advanced-panel" hidden>
            <!-- v0.111：实时栏阶段感知 stale 超时三档。上游流式静默超过对应
                 档位阈值 → 判定连接中断（清窗 / 推超时 done）。单位秒，1-600。
                 v0.113k：三档超时改为自绘滚轮选择器（wheelPicker），替代原生
                 number input —— 滚轮 / 点击上下半区 / 拖动切值。 -->
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="思考流超时时间">思考流超时时间</div>
                <div class="settings-item-hint" data-i18n="思考流已有内容但无新增，判定为中断的间隔时间">思考流已有内容但无新增，判定为中断的间隔时间</div>
              </div>
              <div class="settings-item-control">
                <div class="prefs-live-panel-thinking-input wheel-picker" data-timeout="thinking"></div>
              </div>
            </div>
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="思考-正文衔接超时时间">思考-正文衔接超时时间</div>
                <div class="settings-item-hint" data-i18n="收到思考流停止信号，等待正文的超时时间">收到思考流停止信号，等待正文的超时时间</div>
              </div>
              <div class="settings-item-control">
                <div class="prefs-live-panel-gap-input wheel-picker" data-timeout="gap"></div>
              </div>
            </div>
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="正文流超时时间">正文流超时时间</div>
                <div class="settings-item-hint" data-i18n="正文流已有内容但无新增，判定为中断的间隔时间">正文流已有内容但无新增，判定为中断的间隔时间</div>
              </div>
              <div class="settings-item-control">
                <div class="prefs-live-panel-text-input wheel-picker" data-timeout="text"></div>
              </div>
            </div>
            <!-- v0.134：完成清除超时 / 工具清除超时收纳到等待时间分组下 -->
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="完成清除超时">完成清除超时</div>
                <div class="settings-item-hint" data-i18n="请求完成/出错后，容器保留多少秒再自动清除">请求完成/出错后，容器保留多少秒再自动清除</div>
              </div>
              <div class="settings-item-control">
                <div class="prefs-live-panel-done-clear-input wheel-picker" data-timeout="done-clear"></div>
              </div>
            </div>
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="工具清除超时">工具清除超时</div>
                <div class="settings-item-hint" data-i18n="工具容器在最后更新后多少秒自动清空">工具容器在最后更新后多少秒自动清空</div>
              </div>
              <div class="settings-item-control">
                <div class="prefs-live-panel-tools-clear-input wheel-picker" data-timeout="tools-clear"></div>
              </div>
            </div>
          </div>

          <!-- v0.135：「样式高级设置」开关 —— 默认关闭，收纳 5 个布局/外观项
               （工具调用上限 / 端点列表高度 / 工具常驻 / 列表字号 / 最小列数）。
               「侧栏无边框」挪进这里（原本在「外观」分组，但语义属于侧栏
               视觉样式，分组更连贯）。 -->
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="样式高级设置">样式高级设置</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-live-panel-style-advanced" />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item settings-item-nested adv-switch-panel" id="prefs-live-panel-style-advanced-panel" hidden>
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="工具调用上限">工具调用上限</div>
                <div class="settings-item-hint" data-i18n="工具容器最多保留的调用条数">工具容器最多保留的调用条数</div>
              </div>
              <div class="settings-item-control">
                <input type="number" min="1" max="200" step="1" class="prefs-live-panel-tools-cap-input" style="width:64px" />
              </div>
            </div>
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="端点列表高度">端点列表高度</div>
                <div class="settings-item-hint" data-i18n="端点并发列表最大显示高度（vh）">端点并发列表最大显示高度（vh）</div>
              </div>
              <div class="settings-item-control">
                <input type="number" min="5" max="90" step="1" class="prefs-live-panel-ep-list-vh-input" style="width:64px" />
              </div>
            </div>
            <!-- v0.135：侧栏无边框挪到本分组（语义属于侧栏样式） -->
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="侧栏无边框">侧栏无边框</div>
                <div class="settings-item-hint" data-i18n="开启后实时流侧栏所有卡片去掉玻璃容器，裸悬浮在底背景上">开启后实时流侧栏所有卡片去掉玻璃容器，裸悬浮在底背景上</div>
              </div>
              <div class="settings-item-control">
                <label class="switch">
                  <input type="checkbox" class="prefs-no-panel-frame" ${snap && snap.live_panel_frameless ? "checked" : ""} />
                  <span class="switch-track"><span class="switch-thumb"></span></span>
                </label>
              </div>
            </div>
            <!-- v0.135：列表字号档位（小/中/大 三档；中=默认） -->
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="列表字号档位">列表字号档位</div>
                <div class="settings-item-hint" data-i18n="端点并发列表字号档位（小/中/大）">端点并发列表字号档位（小/中/大）</div>
              </div>
              <div class="settings-item-control">
                <div class="seg seg-list-font">
                  <button type="button" class="seg-btn" data-list-font="small">小</button>
                  <button type="button" class="seg-btn" data-list-font="medium">中</button>
                  <button type="button" class="seg-btn" data-list-font="large">大</button>
                </div>
              </div>
            </div>
            <!-- v0.135：工具常驻开关 —— 开启后无工具调用也保留工具容器占位 -->
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="工具常驻（无调用也占位）">工具常驻（无调用也占位）</div>
                <div class="settings-item-hint" data-i18n="开启后即使无工具调用，工具容器也保留占位；关闭则无调用时整卡隐藏">开启后即使无工具调用，工具容器也保留占位；关闭则无调用时整卡隐藏</div>
              </div>
              <div class="settings-item-control">
                <label class="switch">
                  <input type="checkbox" class="prefs-live-panel-tools-always" ${snap && snap.live_panel_tools_always ? "checked" : ""} />
                  <span class="switch-track"><span class="switch-thumb"></span></span>
                </label>
              </div>
            </div>
            <!-- v0.135：最小列数（auto_extend 模式下也至少 N 列） -->
            <div class="settings-item">
              <div class="settings-item-info">
                <div class="settings-item-title" data-i18n="最小列数">最小列数</div>
                <div class="settings-item-hint" data-i18n="自动延展侧栏时也至少开这么多列">自动延展侧栏时也至少开这么多列</div>
              </div>
              <div class="settings-item-control">
                <input type="number" min="1" max="6" step="1" class="prefs-live-panel-min-cols-input" style="width:64px" />
              </div>
            </div>
          </div>
          </div>
        </div>

        <div class="settings-group">
          <div class="settings-group-label">数据</div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">保存消息原文与回复</div>
              <div class="settings-item-hint">把每次请求的内容和模型回复存进本地数据库，历史页可查看对话；关闭后不再记录新消息</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-save-messages" ${snap && snap.save_messages ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
        </div>

        <div class="settings-group">
          <div class="settings-group-label" data-i18n="settings-group.startup">启动</div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">开机自启</div>
              <div class="settings-item-hint">开机登录 Windows 后自动启动中继</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-autostart" ${snap && snap.autostart_enabled ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">启动后隐藏窗口</div>
              <div class="settings-item-hint">登录启动后直接缩到托盘，不弹窗</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-launch-hidden" ${(snap && snap.start_hidden) || prefs.launchHidden ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
        </div>

        <div class="settings-group">
          <div class="settings-group-label">动画</div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">闪烁</div>
              <div class="settings-item-hint">调用时闪烁</div>
            </div>
            <div class="settings-item-control">
              <label class="switch prefs-flash" id="switch-flash">
                <input type="checkbox" class="prefs-flash" ${prefs.flash ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">释放闪烁</div>
              <div class="settings-item-hint">5小时配额释放动画</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-release" ${prefs.releaseFlash ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">全页闪烁</div>
              <div class="settings-item-hint">整个页面为你闪烁</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-whole-flash" ${prefs.wholeFlash ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">高级闪烁功能</div>
              <div class="settings-item-hint">自定义动画曲线、颜色、粗细和宽度</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-advanced-flash" ${prefs.advancedFlash ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="flash-advanced" id="flash-advanced">
            <div class="flash-advanced-title" data-i18n="缓动曲线 (cubic-bezier)">缓动曲线 (cubic-bezier)</div>
            <div class="cbz-wrap">
              <canvas class="cbz-canvas" id="cbz-canvas" width="200" height="200"></canvas>
              <div class="cbz-readout">
                <div>cubic-bezier(</div>
                <div><code id="cbz-readout">0.615, -0.003, 0.325, 0.986</code></div>
                <div>)</div>
              </div>
            </div>
            <div class="flash-advanced-title" data-i18n="颜色 / 粗细 / 宽度">颜色 / 粗细 / 宽度</div>
            <div class="flash-gradient-editor">
              <div class="flash-gradient-row"><label data-i18n="粗细">粗细</label><input type="range" id="fg-thickness" min="0" max="16" step="1" value="3" /><span class="val" id="fg-thickness-val">3px</span></div>
              <div class="flash-gradient-row"><label data-i18n="宽度">宽度</label><input type="range" id="fg-width" min="40" max="100" step="1" value="88" /><span class="val" id="fg-width-val">88%</span></div>
              <div class="flash-gradient-row"><label data-i18n="增长色">增长色</label><input type="color" id="fg-color-bump" value="#000000" /><span class="val" data-i18n="留空=主题色">留空=主题色</span></div>
              <div class="flash-gradient-row"><label data-i18n="释放色">释放色</label><input type="color" id="fg-color-decrease" value="#000000" /><span class="val" data-i18n="留空=绿色">留空=绿色</span></div>
              <div class="flash-gradient-row"><span class="flash-swatch" id="fg-swatch"></span></div>
            </div>
          </div>
        </div>

        <!-- v0.101：开发者模式 —— 收纳调试/可视化辅助类选项（隐藏状态栏、
             内外转换映射显示等）。关闭主开关时下方两项整体隐藏，避免一般
             用户被无关开关干扰；开启后再单独控制子项。 -->
        <div class="settings-group">
          <div class="settings-group-label">开发者模式</div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title">开发者模式</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-developer-mode" ${prefs.developerMode ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item settings-item-nested" id="prefs-hide-status-row" ${prefs.developerMode ? "" : "hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title">隐藏顶部调试栏</div>
              <div class="settings-item-hint">隐藏整个顶栏（运行状态 + 按钮行），界面更干净；可从顶栏按钮或本开关恢复</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-hide-status" ${prefs.hideStatusBar ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item settings-item-nested" id="prefs-io-map-row" ${prefs.developerMode ? "" : "hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title">内外转换显示</div>
              <div class="settings-item-hint">在顶部状态栏显示"客户端请求的模型 → 实际调用的模型"对照</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-io-map" ${snap && snap.show_io_map ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>

          <!-- v0.174：自动切换 API + 高级切换从原「自动切换」组挪入，作为开发者模式子项。
               自动切换是实验性 quota 调度，不是一般用户日常功能；放开发者模式与「高级闪烁」「内外转换」同级。 -->
          <div class="settings-item settings-item-nested" ${prefs.developerMode ? "" : "hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title">自动切换 API</div>
              <div class="settings-item-hint">当前上游额度用完后，自动切换到其他可用的 API</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-autoswitch" ${snap && snap.autoswitch_enabled ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item settings-item-nested" id="prefs-autoswitch-pool" ${(prefs.developerMode && snap && snap.autoswitch_enabled) ? "" : "hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title">允许自动切换的 API</div>
              <div class="settings-item-hint">选择可作为切换目标的上游；一个都不勾 = 全部允许</div>
            </div>
            <div class="prefs-pool-chips" id="prefs-pool-chips"></div>
          </div>
          <div class="settings-item settings-item-nested" ${prefs.developerMode ? "" : "hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title">高级切换（实验）</div>
            </div>
            <div class="settings-item-control">
              <label class="switch">
                <input type="checkbox" class="prefs-advanced-switch" ${prefs.advancedSwitch ? "checked" : ""} />
                <span class="switch-track"><span class="switch-thumb"></span></span>
              </label>
            </div>
          </div>
          <div class="settings-item settings-item-nested adv-switch-panel" id="advanced-switch-panel" hidden></div>

          <!-- v0.190：支持图片的模型（开发者模式子项）—— 纯 chip 列表，无主开关。
               勾选本身即功能开关（全不勾 = 不允许发图）；随开发者模式显隐。
               chip 渲染目标 = #prefs-vision-chip-box，渲染函数 renderSettingsVision()。 -->
          <div class="settings-item settings-item-nested" id="prefs-vision-row" ${prefs.developerMode ? "" : "hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="支持图片的模型">支持图片的模型</div>
              <div class="settings-item-hint" data-i18n="允许 OpenCode 向勾选的模型发图；中继 /models/api.json 会标成 image-capable。点击切换勾选，保存后立即生效。默认全不勾。">允许 OpenCode 向勾选的模型发图；中继 /models/api.json 会标成 image-capable。点击切换勾选，保存后立即生效。默认全不勾。</div>
            </div>
            <div class="settings-item-control settings-vm-chips" id="prefs-vision-chip-box">
              <div class="card-empty">加载中…</div>
            </div>
            <div class="settings-item-control settings-storage-actions">
              <button type="button" class="btn btn-ghost" id="prefs-vision-save" data-i18n="保存">保存</button>
            </div>
          </div>
        </div>

      </div>
    `;
    // v0.190：开发者模式子项 —— 在 prefs body 整体渲染完成后异步填充
    // vision chip 列表。容器已在开发者模式组里（HTML 模板），此处不重写。
    renderSettingsVision(body, snap);
    wireSettingsPrefs(body, snap);
    // v0.113u：syncLivePanelMgmtGroup 紧跟 wireSettingsPrefs —— 让「外
    // 观」组实时栏开关初始状态与 snapshot 同步，避免「实时栏管理」组首
    // 帧被隐藏、后续又被异步 refreshPrefsDynamic 翻出来造成的可见性抖
    // 动。refreshPrefsDynamic 后续会再调一次本函数（结果一致即可）。
    syncLivePanelMgmtGroup(!!(snap && snap.live_panel));
    refreshPrefsDynamic(body, snap);
    // v0.113n：设置页渲染后应用当前语言（data-zh 替换）。
    applyLang();
  }

  function wireSettingsPrefs(body, snap) {
    const segTheme = body.querySelector(".seg-theme");
    if (segTheme) {
      segTheme.querySelectorAll(".seg-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
          if (btn.classList.contains("active")) return;
          const applied = await api.setTheme(btn.dataset.themeName);
          if (applied) window.setTheme(applied);
          setSegTheme(applied || btn.dataset.themeName);
        });
      });
    }
    // v0.113n：语言切换 —— 纯前端偏好，写入 localStorage + 全页 data-zh
    // 替换（applyLang）。不需要后端桥。
    // v0.172："auto" = 跟随系统，intent 存用户意图 + lang 实时 resolve
    const segLang = body.querySelector(".seg-lang");
    if (segLang) {
      segLang.querySelectorAll(".seg-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const lang = btn.dataset.lang;
          if (lang === I18N.intent || !["auto", "zh", "en", "ja", "ko", "zh-TW"].includes(lang)) return;
          I18N.intent = lang;
          I18N.lang = _resolveLang(lang);
          try { localStorage.setItem("lang", lang); } catch (_) {}
          applyLang();
        });
      });
    }
    const autoStart = body.querySelector(".prefs-autostart");
    if (autoStart) {
      autoStart.addEventListener("change", async () => {
        const res = await api.setAutostart(autoStart.checked);
        autoStart.checked = !!(res && res.enabled);
      });
    }
    const autoSwitch = body.querySelector(".prefs-autoswitch");
    if (autoSwitch) {
      autoSwitch.addEventListener("change", async () => {
        const enabled = autoSwitch.checked;
        const res = await api.updateRelaySettings({ enabled });
        const ok = res && !res.error;
        autoSwitch.checked = ok ? enabled : (res && res.autoswitch_enabled) === true;
        const poolWrap = body.querySelector("#prefs-autoswitch-pool");
        // v0.174：开发者模式子项 —— 关掉开发者模式时强制隐藏
        if (poolWrap) poolWrap.hidden = !autoSwitch.checked || !prefs.developerMode;
        // v0.90：开关关闭后用户期望上游页 hint 立即消失，不等 500ms tick。
        // 主动重置 lastUpstreamsSig 让下一次 renderUpstreamsView 强制重渲
        // （sig 计算含 autoswitch_enabled，下次 tick 自然变化也能触发，
        // 但用户感知有 500ms 延迟；这里抹掉）。
        lastUpstreamsSig = null;
      });
    }
    const flash = body.querySelector(".prefs-flash");
    if (flash) flash.addEventListener("change", () => { prefs.flash = flash.checked; savePrefs(); syncFlashLock(body); });
    const wholeFlash = body.querySelector(".prefs-whole-flash");
    if (wholeFlash) wholeFlash.addEventListener("change", () => {
      prefs.wholeFlash = wholeFlash.checked;
      savePrefs();
      syncFlashLock(body);
      // #1：开启全页闪烁时立刻全页闪一次，让用户马上看到效果。
      if (wholeFlash.checked) triggerWholeFlash();
    });
    const release = body.querySelector(".prefs-release");
    if (release) release.addEventListener("change", () => { prefs.releaseFlash = release.checked; savePrefs(); });
    const hideStatus = body.querySelector(".prefs-hide-status");
    if (hideStatus) hideStatus.addEventListener("change", () => {
      prefs.hideStatusBar = hideStatus.checked;
      savePrefs();
      applyPrefsClass();
      // v0.103：反向同步顶栏「关闭顶部调试栏」按钮文案。
      const btn = document.querySelector("#btn-hide-status-bar");
      if (btn) {
        btn.setAttribute("aria-pressed", hideStatus.checked ? "true" : "false");
        btn.classList.toggle("btn-active", hideStatus.checked);
        btn.textContent = hideStatus.checked ? "显示顶部调试栏" : "关闭顶部调试栏";
      }
    });
    // v0.101：开发者模式主开关 —— 关闭时整体隐藏 nested 子项，开启时显示。
    const devMode = body.querySelector(".prefs-developer-mode");
    if (devMode) {
      devMode.addEventListener("change", () => {
        prefs.developerMode = devMode.checked;
        savePrefs();
        const rows = [
          body.querySelector("#prefs-hide-status-row"),
          body.querySelector("#prefs-io-map-row"),
        ];
        // v0.174：autoswitch/adv-switch 三项 nested 共享开发者模式隐藏。
        // 第三个是 adv-switch-panel，要分别从兄弟节点取，因为本元素不在
        // ``#xxx-row`` 命名约定下。
        const autoSwitchCb = body.querySelector(".prefs-autoswitch");
        if (autoSwitchCb) {
          const autoRow = autoSwitchCb.closest(".settings-item");
          if (autoRow) rows.push(autoRow);
        }
        const advSwitchCb = body.querySelector(".prefs-advanced-switch");
        if (advSwitchCb) {
          const advRow = advSwitchCb.closest(".settings-item");
          if (advRow) rows.push(advRow);
        }
        const advPanel = body.querySelector("#advanced-switch-panel");
        if (advPanel) rows.push(advPanel);
        // v0.190：开发者模式子项 —— 支持图片的模型（纯 chip 列表，无主开关）。
        // 只随开发者模式显隐。
        const visionRow = body.querySelector("#prefs-vision-row");
        if (visionRow) rows.push(visionRow);
        for (const r of rows) if (r) r.hidden = !devMode.checked;
        // autoswitch pool：开发者模式关 → 强 hidden；开着且 autoswitch 关 → hidden；
        // 开着且 autoswitch 开 → visible。
        const poolWrap = body.querySelector("#prefs-autoswitch-pool");
        if (poolWrap) poolWrap.hidden = !(devMode.checked && (autoSwitchCb ? autoSwitchCb.checked : false));
      });
    }
    // v0.113：无边框开关 —— 纯前端偏好，savePrefs 里 applyPrefsClass
    // 会同步 body.overview-no-frame 类。
    const noFrameToggle = body.querySelector(".prefs-no-frame");
    if (noFrameToggle) noFrameToggle.addEventListener("change", () => {
      prefs.noFrame = noFrameToggle.checked;
      savePrefs();
    });
    // v0.113c：侧栏无边框 —— 走后端持久化 + 广播到实时栏窗口。
    const noPanelFrameToggle = body.querySelector(".prefs-no-panel-frame");
    if (noPanelFrameToggle) noPanelFrameToggle.addEventListener("change", async () => {
      const res = await api.setLivePanelFrameless(noPanelFrameToggle.checked);
      noPanelFrameToggle.checked = !!(res && res.enabled);
    });
    const ioMapToggle = body.querySelector(".prefs-io-map");
    if (ioMapToggle) {
      // 初始值由 refreshPrefsDynamic 用 api.getShowIoMap() 实时拉取（不读
      // 快照——快照有 ~1s 延迟，切页回来重渲染会用过期值把开关重置）。
      ioMapToggle.addEventListener("change", async () => {
        const enabled = ioMapToggle.checked;
        const res = await api.setShowIoMap(enabled);
        ioMapToggle.checked = !!(res && res.enabled);
      });
    }
    // v0.89 实时流侧栏开关（与顶栏 btn-live-panel 共用后端，状态互相同步）。
    // v0.112j：开启才显示「实时栏管理」组，关闭则整组隐藏。
    // v0.176：主开关管理组内全部子项 —— 关掉则整块折叠；悬浮球置顶额外
    // 看悬浮球开关。
    const livePanelToggle = body.querySelector(".prefs-live-panel");
    if (livePanelToggle) {
      livePanelToggle.addEventListener("change", async () => {
        const enabled = livePanelToggle.checked;
        const res = await api.setLivePanel(enabled);
        livePanelToggle.checked = !!(res && res.enabled);
        // 顶栏图标状态同步
        syncSidePanelToggle(!!(res && res.enabled));
        // 实时栏管理组按开关显隐
        syncLivePanelMgmtGroup(!!(res && res.enabled));
        syncLivePanelSub();
      });
    }
    // v0.104：允许并发显示
    const concurrentToggle = body.querySelector(".prefs-live-panel-concurrent");
    if (concurrentToggle) {
      concurrentToggle.addEventListener("change", async () => {
        const enabled = concurrentToggle.checked;
        const res = await api.setLivePanelConcurrent(enabled);
        concurrentToggle.checked = !!(res && res.enabled);
      });
    }
    // v0.104：最多显示的并行数量 —— v0.167 由 int input 改为滚轮选择器
    // （wheelPicker 通用化：1-8、无单位、setter 返回 {max}）。防抖写回。
    // v0.169：initMax 优先用旧 DOM 快照（覆盖 snap 默认），同
    // mountTimeoutWheel 的 wpDef 优先级，避免用户刚改就被刷回。
    const maxInput = body.querySelector(".prefs-live-panel-max-input");
    if (maxInput) {
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["wp:max"];
      const fromSnap = (snap && typeof snap.live_panel_max === "number") ? snap.live_panel_max : null;
      const initMax = (typeof dirty === "number") ? dirty : (fromSnap != null ? fromSnap : 3);
      wheelPicker(maxInput, (v) => api.setLivePanelMax(v), initMax,
                  { min: 1, max: 8, unit: "", valueKey: "max" });
    }
    // v0.104：磁吸状态下始终开启一个
    const alwaysOneToggle = body.querySelector(".prefs-live-panel-always-one");
    if (alwaysOneToggle) {
      alwaysOneToggle.addEventListener("change", async () => {
        const enabled = alwaysOneToggle.checked;
        const res = await api.setLivePanelAlwaysOne(enabled);
        alwaysOneToggle.checked = !!(res && res.enabled);
      });
    }
    // v0.130：自动延展侧栏（宽度）
    const autoExtendToggle = body.querySelector(".prefs-live-panel-auto-extend");
    if (autoExtendToggle) {
      autoExtendToggle.addEventListener("change", async () => {
        const enabled = autoExtendToggle.checked;
        const res = await api.setLivePanelAutoExtend(enabled);
        autoExtendToggle.checked = !!(res && res.enabled);
      });
    }
    // v0.165：悬浮球 —— 桌面锚点，侧栏从球位置展开。
    // v0.176：球开关开才显示「悬浮球置顶」子项。
    const floatBallToggle = body.querySelector(".prefs-live-panel-float-ball");
    const topmostItem = body.querySelector("#prefs-live-panel-float-ball-topmost-item");
    if (floatBallToggle) {
      floatBallToggle.addEventListener("change", async () => {
        const enabled = floatBallToggle.checked;
        const res = await api.setFloatBall(enabled);
        floatBallToggle.checked = !!(res && res.enabled);
        if (topmostItem) topmostItem.hidden = !floatBallToggle.checked;
      });
    }
    // v0.170：悬浮球置顶 —— 球与侧栏一起置顶、盖过其它窗口（球>侧栏恒成立）。
    const floatBallTopmostToggle = body.querySelector(".prefs-live-panel-float-ball-topmost");
    if (floatBallTopmostToggle) {
      floatBallTopmostToggle.addEventListener("change", async () => {
        const enabled = floatBallTopmostToggle.checked;
        const res = await api.setFloatBallTopmost(enabled);
        floatBallTopmostToggle.checked = !!(res && res.enabled);
      });
    }
    // v0.135：等待时间高级设置开关 —— 默认关闭，开启才展开 nested 面板。
    // 面板折叠状态由 prefs.livePanelTimeoutAdvanced 持久化（纯前端偏好，
    // 与「开关状态」不是同一概念：开关在不在 × 折叠在不在，XOR）。
    const timeoutAdvToggle = body.querySelector(".prefs-live-panel-timeout-advanced");
    const timeoutAdvPanel = body.querySelector("#prefs-live-panel-timeout-advanced-panel");
    if (timeoutAdvToggle) {
      timeoutAdvToggle.checked = !!prefs.livePanelTimeoutAdvanced;
      if (timeoutAdvPanel) timeoutAdvPanel.hidden = !timeoutAdvToggle.checked;
      timeoutAdvToggle.addEventListener("change", () => {
        if (timeoutAdvPanel) timeoutAdvPanel.hidden = !timeoutAdvToggle.checked;
        prefs.livePanelTimeoutAdvanced = timeoutAdvToggle.checked;
        savePrefs();
      });
    }
    // v0.135：样式高级设置开关 —— 默认关闭，开启才展开 nested 面板。
    const styleAdvToggle = body.querySelector(".prefs-live-panel-style-advanced");
    const styleAdvPanel = body.querySelector("#prefs-live-panel-style-advanced-panel");
    if (styleAdvToggle) {
      styleAdvToggle.checked = !!prefs.livePanelStyleAdvanced;
      if (styleAdvPanel) styleAdvPanel.hidden = !styleAdvToggle.checked;
      styleAdvToggle.addEventListener("change", () => {
        if (styleAdvPanel) styleAdvPanel.hidden = !styleAdvToggle.checked;
        prefs.livePanelStyleAdvanced = styleAdvToggle.checked;
        savePrefs();
      });
    }
    // v0.135：工具常驻开关 —— 开启后无工具调用也保留工具容器占位。
    const toolsAlwaysToggle = body.querySelector(".prefs-live-panel-tools-always");
    if (toolsAlwaysToggle) {
      toolsAlwaysToggle.addEventListener("change", async () => {
        const res = await api.setLivePanelToolsAlways(toolsAlwaysToggle.checked);
        toolsAlwaysToggle.checked = !!(res && res.enabled);
      });
    }
    // v0.135：最小列数（数字输入）—— 1~6，auto_extend 模式下也至少开这么多列。
    const minColsInput = body.querySelector(".prefs-live-panel-min-cols-input");
    if (minColsInput) {
      minColsInput.addEventListener("change", async () => {
        const n = Math.max(1, Math.min(6, parseInt(minColsInput.value, 10) || 1));
        minColsInput.value = String(n);
        const res = await api.setLivePanelMinCols(n);
        if (res && typeof res.value === "number") minColsInput.value = String(res.value);
      });
    }
    // v0.135：列表字号档位（seg 三档：small/medium/large） —— 写到端点并发列表 CSS。
    const listFontSeg = body.querySelector(".seg-list-font");
    if (listFontSeg) {
      listFontSeg.addEventListener("click", async (e) => {
        const btn = e.target.closest(".seg-btn");
        if (!btn) return;
        const v = btn.dataset.listFont || "medium";
        [...listFontSeg.querySelectorAll(".seg-btn")].forEach((b) => b.classList.toggle("active", b === btn));
        const res = await api.setLivePanelListFont(v);
        if (res && typeof res.value === "string") {
          [...listFontSeg.querySelectorAll(".seg-btn")].forEach((b) => b.classList.toggle("active", b.dataset.listFont === res.value));
        }
      });
    }
    // v0.134：工具调用上限 / 端点列表高度（数字输入，change 即时写回）。
    const toolsCapInput = body.querySelector(".prefs-live-panel-tools-cap-input");
    if (toolsCapInput) {
      toolsCapInput.addEventListener("change", async () => {
        const n = Math.max(1, Math.min(200, parseInt(toolsCapInput.value, 10) || 30));
        toolsCapInput.value = String(n);
        const res = await api.setLivePanelToolsCap(n);
        if (res && typeof res.value === "number") toolsCapInput.value = String(res.value);
      });
    }
    const epListVhInput = body.querySelector(".prefs-live-panel-ep-list-vh-input");
    if (epListVhInput) {
      epListVhInput.addEventListener("change", async () => {
        const vh = Math.max(5, Math.min(90, parseFloat(epListVhInput.value) || 30));
        epListVhInput.value = String(vh);
        const res = await api.setLivePanelEpListVh(vh);
        if (res && typeof res.value === "number") epListVhInput.value = String(res.value);
      });
    }
    // v0.113k：滚轮选择器（超时时间三档）—— 自绘，wheel/click/drag 切值，
    // 内部防抖 350ms 写回；初始值由 refreshPrefsDynamic 补。
    // v0.169：def 优先级 —— 旧 DOM 快照 > snap > 默认常量。快照用于
    // 设置页重渲染场景：用户刚改完（350ms 防抖 + 500ms 轮询还没到）就
    // 离开设置页，snap 里仍是旧值，用快照覆盖就避免"被刷回默认"。
    const wpDef = (dataKey, snapKey, fallback) => {
      const fromSnap = (snap && typeof snap[snapKey] === "number") ? snap[snapKey] : null;
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["wp:" + dataKey];
      return (typeof dirty === "number") ? dirty : (fromSnap != null ? fromSnap : fallback);
    };
    const mountTimeoutWheel = (sel, setter, def) => {
      const host = body.querySelector(sel);
      if (!host) return;
      wheelPicker(host, setter, def);
    };
    mountTimeoutWheel(".prefs-live-panel-thinking-input", s => api.setLivePanelThinkingTimeout(s), wpDef("thinking", "live_panel_thinking_timeout", 60));
    mountTimeoutWheel(".prefs-live-panel-gap-input",       s => api.setLivePanelGapTimeout(s),       wpDef("gap",       "live_panel_gap_timeout",       20));
    mountTimeoutWheel(".prefs-live-panel-text-input",      s => api.setLivePanelTextTimeout(s),      wpDef("text",      "live_panel_text_timeout",      10));
    mountTimeoutWheel(".prefs-live-panel-done-clear-input", s => api.setLivePanelDoneClearTimeout(s), wpDef("done-clear", "live_panel_done_clear_timeout", 10));
    mountTimeoutWheel(".prefs-live-panel-tools-clear-input", s => api.setLivePanelToolsClearTimeout(s), wpDef("tools-clear", "live_panel_tools_clear_timeout", 5));
    const saveMsgs = body.querySelector(".prefs-save-messages");
    if (saveMsgs) {
      saveMsgs.addEventListener("change", async () => {
        const res = await api.updateRelaySettings({ save_messages: saveMsgs.checked });
        saveMsgs.checked = (res && !res.error && res.save_messages !== undefined)
          ? !!res.save_messages
          : saveMsgs.checked;
      });
    }
    const launchHidden = body.querySelector(".prefs-launch-hidden");
    if (launchHidden) launchHidden.addEventListener("change", async () => {
      prefs.launchHidden = launchHidden.checked; savePrefs();
      if (window.pywebview && window.pywebview.api && api.setStartHidden) {
        const res = await api.setStartHidden(launchHidden.checked);
        launchHidden.checked = !!(res && res.enabled);
      }
    });
    const advancedSwitch = body.querySelector(".prefs-advanced-switch");
    if (advancedSwitch) {
      advancedSwitch.addEventListener("change", () => {
        prefs.advancedSwitch = advancedSwitch.checked;
        savePrefs();
        const panel = body.querySelector("#advanced-switch-panel");
        if (panel) {
          panel.hidden = !advancedSwitch.checked;
          if (advancedSwitch.checked) loadAdvancedSwitchPanel(panel);
        }
      });
    }
    const advancedFlash = body.querySelector(".prefs-advanced-flash");
    if (advancedFlash) {
      advancedFlash.addEventListener("change", () => {
        prefs.advancedFlash = advancedFlash.checked;
        savePrefs();
        const wrap = body.querySelector("#flash-advanced");
        if (wrap) wrap.classList.toggle("show", prefs.advancedFlash);
      });
    }
    // #2：高级闪烁编辑器 —— cubic-bezier 拖拽 + 颜色/粗细/宽度滑杆。
    initFlashEditors(body);
    // 勾选池 → 立即写回 relay。空池 = 全部允许。
    body.addEventListener("change", (e) => {
      if (!e.target.closest(".prefs-pool-chip")) return;
      const pool = [...body.querySelectorAll(".prefs-pool-chip input")]
        .filter(i => i.checked)
        .map(i => i.dataset.name);
      api.updateRelaySettings({ pool });
      // v0.90：池变化也要立即让上游页 hint 重渲（候选集合变了）。
      lastUpstreamsSig = null;
    });
  }

  // #8：开启全页闪烁后，"闪烁"开关变灰禁用，hover 跟随鼠标提示。
  function syncFlashLock(body) {
    const wrap = body.querySelector("#switch-flash");
    const input = body.querySelector(".prefs-flash");
    if (!wrap || !input) return;
    const locked = !!prefs.wholeFlash;
    wrap.classList.toggle("locked", locked);
    input.disabled = locked;
    if (locked) input.checked = false;
    else if (!input.checked && prefs.flash) input.checked = true;
    let tip = body.querySelector(".disabled-tip");
    if (locked && !tip) {
      tip = document.createElement("div");
      tip.className = "disabled-tip";
      tip.textContent = "开启全页闪烁后，逐条闪烁已禁用";
      document.body.appendChild(tip);
      wrap.addEventListener("mouseenter", () => {
        const r = wrap.getBoundingClientRect();
        tip.style.left = (r.right + 8) + "px";
        tip.style.top = (r.top) + "px";
        tip.classList.add("show");
      });
      wrap.addEventListener("mouseleave", () => tip.classList.remove("show"));
    }
    if (!locked && tip && tip.parentNode) tip.remove();
  }

  // #2：高级闪烁编辑器。
  function initFlashEditors(body) {
    const canvas = body.querySelector("#cbz-canvas");
    const readout = body.querySelector("#cbz-readout");
    const thickness = body.querySelector("#fg-thickness");
    const thicknessVal = body.querySelector("#fg-thickness-val");
    const width = body.querySelector("#fg-width");
    const widthVal = body.querySelector("#fg-width-val");
    const colorBump = body.querySelector("#fg-color-bump");
    const colorDec = body.querySelector("#fg-color-decrease");
    const swatch = body.querySelector("#fg-swatch");

    const applyGradient = () => {
      if (swatch) swatch.style.background =
        `linear-gradient(90deg, transparent, ${colorBump && colorBump.value && colorBump.value !== "#000000" ? colorBump.value : "var(--flash-color-bump)"} 46%, ${colorBump && colorBump.value && colorBump.value !== "#000000" ? colorBump.value : "var(--flash-color-bump)"} 88%)`;
    };

    // 缓动曲线状态
    let bez = null;
    try {
      const p = (prefs.flashEase || "0.615,-0.003,0.325,0.986").split(",").map(Number);
      if (p.length === 4 && p.every(n => !isNaN(n))) bez = p;
    } catch (_) {}
    if (!bez) bez = [0.615, -0.003, 0.325, 0.986];
    let dragPoint = null; // "p1" | "p2"

    const draw = () => {
      if (!canvas || !readout) return;
      const ctx = canvas.getContext("2d");
      const W = canvas.width, H = canvas.height, pad = 20;
      ctx.clearRect(0, 0, W, H);
      // grid
      ctx.strokeStyle = "rgba(0,0,0,0.12)";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i++) {
        const x = pad + (W - pad * 2) * i / 4;
        ctx.beginPath(); ctx.moveTo(x, pad); ctx.lineTo(x, H - pad); ctx.stroke();
        const y = pad + (H - pad * 2) * i / 4;
        ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W - pad, y); ctx.stroke();
      }
      // diagonal
      ctx.strokeStyle = "rgba(0,0,0,0.15)";
      ctx.beginPath(); ctx.moveTo(pad, H - pad); ctx.lineTo(W - pad, pad); ctx.stroke();
      // curve: cubic-bezier P0=(0,0) P1 P2 P3=(1,1)，t ∈ [0,1]
      const px = (x) => pad + (W - pad * 2) * x;
      const py = (y) => pad + (H - pad * 2) * (1 - y);
      const curvePt = (t) => {
        const mt = 1 - t;
        const x = 3*mt*mt*t*bez[0] + 3*mt*t*t*bez[2] + t*t*t;
        const y = 3*mt*mt*t*bez[1] + 3*mt*t*t*bez[3] + t*t*t;
        return [px(x), py(y)];
      };
      ctx.strokeStyle = "var(--button-primary)";
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      for (let t = 0; t <= 1; t += 0.01) {
        const [sx, sy] = curvePt(t);
        if (t === 0) ctx.moveTo(sx, sy);
        else ctx.lineTo(sx, sy);
      }
      ctx.stroke();
      // control points
      for (const [cx, cy, key] of [[bez[0], bez[1], "p1"], [bez[2], bez[3], "p2"]]) {
        ctx.beginPath();
        ctx.arc(px(cx), py(cy), 7, 0, Math.PI * 2);
        ctx.fillStyle = key === "p1" ? "#f59e0b" : "#3b82f6";
        ctx.fill();
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
      }
      readout.textContent = bez.map(v => v.toFixed(3)).join(", ");
      applyGradient();
    };

    const ptFromEvent = (ev) => {
      const rect = canvas.getBoundingClientRect();
      const x = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
      const y = Math.min(1, Math.max(0, 1 - (ev.clientY - rect.top) / rect.height));
      return [x, y];
    };
    canvas.addEventListener("pointerdown", (ev) => {
      const [x, y] = ptFromEvent(ev);
      const d1 = Math.hypot(x - bez[0], y - bez[1]);
      const d2 = Math.hypot(x - bez[2], y - bez[3]);
      dragPoint = d1 <= d2 ? "p1" : "p2";
      canvas.setPointerCapture && canvas.setPointerCapture(ev.pointerId);
      const move = (e) => {
        const [mx, my] = ptFromEvent(e);
        const idx = dragPoint === "p1" ? 0 : 2;
        bez[idx] = mx; bez[idx + 1] = my;
        prefs.flashEase = bez.map(v => v.toFixed(3)).join(",");
        savePrefs();
        draw();
      };
      const up = () => { dragPoint = null; canvas.removeEventListener("pointermove", move); canvas.removeEventListener("pointerup", up); };
      canvas.addEventListener("pointermove", move);
      canvas.addEventListener("pointerup", up);
    });
    if (thickness) {
      thickness.value = prefs.flashThickness;
      if (thicknessVal) thicknessVal.textContent = prefs.flashThickness + "px";
      thickness.addEventListener("input", () => {
        prefs.flashThickness = parseInt(thickness.value, 10);
        if (thicknessVal) thicknessVal.textContent = prefs.flashThickness + "px";
        savePrefs();
      });
    }
    if (width) {
      width.value = prefs.flashWidth;
      if (widthVal) widthVal.textContent = prefs.flashWidth + "%";
      width.addEventListener("input", () => {
        prefs.flashWidth = parseInt(width.value, 10);
        if (widthVal) widthVal.textContent = prefs.flashWidth + "%";
        savePrefs();
      });
    }
    if (colorBump) {
      colorBump.value = prefs.flashColorBump || "#000000";
      colorBump.addEventListener("input", () => {
        prefs.flashColorBump = colorBump.value === "#000000" ? "" : colorBump.value;
        savePrefs();
        applyGradient();
      });
    }
    if (colorDec) {
      colorDec.value = prefs.flashColorDecrease || "#000000";
      colorDec.addEventListener("input", () => {
        prefs.flashColorDecrease = colorDec.value === "#000000" ? "" : colorDec.value;
        savePrefs();
        applyGradient();
      });
    }
    if (canvas) draw();
    const wrap = body.querySelector("#flash-advanced");
    if (wrap) wrap.classList.toggle("show", !!prefs.advancedFlash);
  }

  // v0.11.18 高级切换设置面板。
  const ADV_TYPES = [
    { key: "planning", label: "规划 (planning)" },
    { key: "architecture", label: "架构设计 (architecture)" },
    { key: "refactor", label: "重构 (refactor)" },
    { key: "summary", label: "总结 (summary)" },
    { key: "code_review", label: "代码审查 (code_review)" },
    { key: "debug", label: "调试 (debug)" },
    { key: "complex_reasoning", label: "复杂推理" },
    { key: "research", label: "研究调研 (research)" },
  ];
  let _advModels = [];
  let _advConfig = null;

  // 三个模型选择：每个 option 是 "upstream / model"，保存时写回
  // upstream + model 两个字段，避免同名模型无法区分上游。
  function advSelect(id, label, hint, selUpstream, selModel) {
    const opts = _advModels.map(m => `<option value="${attr(m.upstream)}\u0000${attr(m.model)}" ${(m.upstream === selUpstream && m.model === selModel) ? "selected" : ""} data-i18n-keep>${escape(m.label)}</option>`).join("");
    return `
      <div class="settings-item">
        <div class="settings-item-info">
          <div class="settings-item-title">${label}</div>
          <div class="settings-item-hint">${hint}</div>
        </div>
        <div class="settings-item-control">
          <select class="cfg-input adv-${id}" style="width:auto">${opts}</select>
        </div>
      </div>`;
  }

  function advSelectValue(sel) {
    // 返回 { upstream, model }，用 \u0000 分隔。
    const raw = sel ? String(sel.value) : "";
    const idx = raw.indexOf("\u0000");
    if (idx === -1) return { upstream: null, model: raw || null };
    return { upstream: raw.slice(0, idx) || null, model: raw.slice(idx + 1) || null };
  }

  function renderAdvancedSwitchPanel(panel, data) {
    const cfg = data.config || {};
    const stats = data.stats || {};
    _advModels = data.models || [];
    _advConfig = cfg;
    const fmt = n => (n == null ? "—" : Number(n).toLocaleString());
    panel.innerHTML = `
      ${advSelect("weak", "模型（弱）", "默认处理普通请求", cfg.weak_upstream, cfg.weak_model)}
      ${advSelect("strong", "模型（强）", "复杂任务发到这里", cfg.strong_upstream, cfg.strong_model)}
      ${advSelect("analysis", "模型（分析）", "判定用，默认=弱", cfg.analysis_upstream, cfg.analysis_model)}

      <div class="adv-types-block">
        <div class="settings-item-title">发到强模型的类型</div>
        <div class="settings-item-hint">勾选的任务类型会路由到强模型</div>
        <div class="prefs-pool-chips">
          ${ADV_TYPES.map(t => `
            <label class="prefs-pool-chip">
              <input type="checkbox" class="adv-type" value="${attr(t.key)}"
                ${(cfg.strong_types || []).includes(t.key) ? "checked" : ""} />
              <span>${escape(t.label)}</span>
            </label>`).join("")}
        </div>
      </div>

      <div class="settings-item">
        <div class="settings-item-info">
          <div class="settings-item-title">分发机制</div>
          <div class="settings-item-hint">判定模糊时：优先强模型（激进）还是保守用弱模型</div>
        </div>
        <div class="settings-item-control">
          <label class="switch"><input type="checkbox" class="adv-aggressive" ${cfg.aggressive ? "checked" : ""} /><span class="switch-track"><span class="switch-thumb"></span></span></label>
          <span class="settings-item-hint" style="margin-left:6px">${cfg.aggressive ? "激进" : "保守"}</span>
        </div>
      </div>

      <div class="settings-item">
        <div class="settings-item-info">
          <div class="settings-item-title">根据历史分发学习改进</div>
          <div class="settings-item-hint">预留开关，暂未实现</div>
        </div>
        <div class="settings-item-control">
          <label class="switch"><input type="checkbox" class="adv-learning" ${cfg.learning ? "checked" : ""} /><span class="switch-track"><span class="switch-thumb"></span></span></label>
        </div>
      </div>

      <div class="settings-item">
        <div class="settings-item-info">
          <div class="settings-item-title">调用统计</div>
          <div class="settings-item-hint">弱 / 强 / 分析 三类模型调用次数与 token</div>
        </div>
        <div class="settings-item-control">
          <span class="adv-stats">弱 ${fmt(stats.weak && stats.weak.count)} 次 / ${fmt(stats.weak && stats.weak.tokens)} t · 强 ${fmt(stats.strong && stats.strong.count)} 次 / ${fmt(stats.strong && stats.strong.tokens)} t · 分析 ${fmt(stats.analysis && stats.analysis.count)} 次 / ${fmt(stats.analysis && stats.analysis.tokens)} t</span>
        </div>
      </div>

      <div class="settings-item">
        <div class="settings-item-info"></div>
        <div class="settings-item-control">
          <button class="btn btn-primary adv-save" type="button">保存高级切换配置</button>
          <span class="cfg-status adv-status"></span>
        </div>
      </div>`;
    wireAdvancedSwitchPanel(panel);
  }

  function wireAdvancedSwitchPanel(panel) {
    const aggressive = panel.querySelector(".adv-aggressive");
    if (aggressive) {
      aggressive.addEventListener("change", () => {
        const hint = panel.querySelector(".settings-item-hint");
        if (hint) hint.textContent = aggressive.checked ? "激进" : "保守";
      });
    }
    const save = panel.querySelector(".adv-save");
    const status = panel.querySelector(".adv-status");
    if (save) {
      save.addEventListener("click", async () => {
        const strongTypes = [...panel.querySelectorAll(".adv-type:checked")].map(i => i.value);
        const w = advSelectValue(panel.querySelector(".adv-weak"));
        const s = advSelectValue(panel.querySelector(".adv-strong"));
        const a = advSelectValue(panel.querySelector(".adv-analysis"));
        const payload = {
          enabled: true,
          weak_upstream: w.upstream,
          weak_model: w.model,
          strong_upstream: s.upstream,
          strong_model: s.model,
          analysis_upstream: a.upstream,
          analysis_model: a.model,
          strong_types: strongTypes,
          aggressive: !!(panel.querySelector(".adv-aggressive") || {}).checked,
          learning: !!(panel.querySelector(".adv-learning") || {}).checked,
        };
        if (status) status.textContent = "保存中…";
        const res = await api.updateAdvancedSwitch(payload);
        if (res && res.ok === false) {
          if (status) { status.textContent = res.error || "保存失败"; status.setAttribute("data-kind", "error"); }
        } else {
          if (status) { status.textContent = "已保存"; status.setAttribute("data-kind", "ok"); }
          loadAdvancedSwitchPanel(panel);
        }
      });
    }
  }

  async function loadAdvancedSwitchPanel(panel) {
    try {
      const data = await api.getAdvancedSwitch();
      if (data && data.config) renderAdvancedSwitchPanel(panel, data);
    } catch (_) {
      panel.innerHTML = '<div class="card-empty">加载失败</div>';
    }
  }

  function setSegTheme(name) {
    const seg = document.querySelector(".seg-theme");
    if (!seg) return;
    seg.querySelectorAll(".seg-btn").forEach(b =>
      b.classList.toggle("active", b.dataset.themeName === (name || "light")),
    );
  }

  async function refreshPrefsDynamic(body, snap) {
    if (lastStatus && lastStatus.theme) setSegTheme(lastStatus.theme);

    const autoStart = body.querySelector(".prefs-autostart");
    if (autoStart) {
      const res = await api.getAutostart();
      autoStart.checked = !!(res && res.enabled);
    }
    const launchHidden = body.querySelector(".prefs-launch-hidden");
    if (launchHidden && window.pywebview && window.pywebview.api && api.getStartHidden) {
      const res = await api.getStartHidden();
      if (res && typeof res.enabled === "boolean") {
        launchHidden.checked = res.enabled;
        prefs.launchHidden = res.enabled;
      }
    }
    // v0.12：内外转换开关——实时拉后端值，不读快照（快照延迟会致切页回弹）。
    const ioMap = body.querySelector(".prefs-io-map");
    if (ioMap) {
      const res = await api.getShowIoMap();
      if (res && typeof res.enabled === "boolean") ioMap.checked = res.enabled;
    }
    // v0.113c：侧栏无边框开关初始值 —— 后端持久化，实时拉取。
    // v0.169：旧 DOM 快照优先，避免用户刚勾选/取消被刷回。
    const noPanelFrame = body.querySelector(".prefs-no-panel-frame");
    if (noPanelFrame) {
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["chk:prefs-no-panel-frame"];
      if (typeof dirty === "boolean") {
        noPanelFrame.checked = dirty;
      } else {
        const r = await api.getLivePanelFrameless();
        if (r && typeof r.enabled === "boolean") noPanelFrame.checked = r.enabled;
      }
    }
    // v0.89 实时流侧栏开关初始值。
    // v0.112j：实时栏管理组默认 hidden，按开关状态显隐。
    // v0.176：主开关折叠全部子项（#live-panel-mgmt-sub）。
    const livePanel = body.querySelector(".prefs-live-panel");
    if (livePanel) {
      const res = await api.getLivePanel();
      if (res && typeof res.enabled === "boolean") livePanel.checked = res.enabled;
      syncLivePanelMgmtGroup(!!(res && res.enabled));
      syncLivePanelSub();
    }
    const concurrentToggle = body.querySelector(".prefs-live-panel-concurrent");
    if (concurrentToggle) {
      const r = await api.getLivePanelConcurrent();
      if (r && typeof r.enabled === "boolean") concurrentToggle.checked = r.enabled;
    }
    const maxInput = body.querySelector(".prefs-live-panel-max-input");
    if (maxInput && typeof maxInput.__wpSet === "function") {
      // v0.167：滚轮选择器 —— 用 __wpSet(silent) 从后端真值回写显示，不触发持久化。
      // v0.169：若旧 DOM 快照已有 max 值（用户刚改过、防抖还没到），跳过
      // 后端回写，避免被刷回默认。仅跳过 max 的覆盖，不提前退出函数。
      const maxDirty = _prefsDirtySnapshot && typeof _prefsDirtySnapshot["wp:max"] === "number";
      if (!maxDirty) {
        const r = await api.getLivePanelMax();
        if (r && typeof r.max === "number") maxInput.__wpSet(r.max, true);
      }
    }
    const alwaysOneToggle = body.querySelector(".prefs-live-panel-always-one");
    if (alwaysOneToggle) {
      const r = await api.getLivePanelAlwaysOne();
      if (r && typeof r.enabled === "boolean") alwaysOneToggle.checked = r.enabled;
    }
    // v0.130：自动延展侧栏初始值。
    const autoExtendToggle = body.querySelector(".prefs-live-panel-auto-extend");
    if (autoExtendToggle) {
      const r = await api.getLivePanelAutoExtend();
      if (r && typeof r.enabled === "boolean") autoExtendToggle.checked = r.enabled;
    }
    // v0.165：悬浮球初始值。
    // v0.176：球开关开才显示「悬浮球置顶」子项。
    const floatBallToggle = body.querySelector(".prefs-live-panel-float-ball");
    if (floatBallToggle) {
      const r = await api.getFloatBall();
      if (r && typeof r.enabled === "boolean") floatBallToggle.checked = r.enabled;
      const topmostItem = body.querySelector("#prefs-live-panel-float-ball-topmost-item");
      if (topmostItem) topmostItem.hidden = !floatBallToggle.checked;
    }
    // v0.170：悬浮球置顶初始值。
    const floatBallTopmostToggle = body.querySelector(".prefs-live-panel-float-ball-topmost");
    if (floatBallTopmostToggle) {
      const r = await api.getFloatBallTopmost();
      if (r && typeof r.enabled === "boolean") floatBallTopmostToggle.checked = r.enabled;
    }
    // v0.134：工具调用上限 / 端点列表高度数字输入初始值。
    // v0.169：若旧 DOM 快照已有用户改过的值（嵌套在高级折叠组里），
    // 跳过 snap 覆盖，避免"被刷回默认"。
    const toolsCapInput = body.querySelector(".prefs-live-panel-tools-cap-input");
    if (toolsCapInput) {
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["num:prefs-live-panel-tools-cap-input"];
      if (typeof dirty === "number") toolsCapInput.value = String(dirty);
      else toolsCapInput.value = String((snap && typeof snap.live_panel_tools_cap === "number") ? snap.live_panel_tools_cap : 30);
    }
    const epListVhInput = body.querySelector(".prefs-live-panel-ep-list-vh-input");
    if (epListVhInput) {
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["num:prefs-live-panel-ep-list-vh-input"];
      if (typeof dirty === "number") epListVhInput.value = String(dirty);
      else epListVhInput.value = String((snap && typeof snap.live_panel_ep_list_vh === "number") ? snap.live_panel_ep_list_vh : 30);
    }
    // v0.113k：滚轮选择器三档初始值 —— 优先用 snapshot 实时值，
    // 再回退桥查询。silent set 只更新显示，不触发持久化（值已是后端真值）。
    // v0.169：dataKey 参数 — 若旧 DOM 快照已有该值（说明用户刚改过、
    // 防抖+轮询还没到），跳过 snap/setter 覆盖，避免被刷回默认。
    const initTimeoutWheel = (sel, getter, fallback, def, dataKey) => {
      const host = body.querySelector(sel);
      if (!host || typeof host.__wpSet !== "function") return;
      const hasDirty = dataKey && _prefsDirtySnapshot && typeof _prefsDirtySnapshot["wp:" + dataKey] === "number";
      if (hasDirty) return; // mountTimeoutWheel 已用快照值，不再覆盖
      const fromSnap = (snap && typeof snap[fallback] === "number") ? snap[fallback] : null;
      if (fromSnap != null) { host.__wpSet(fromSnap, true); return; }
      getter().then((r) => {
        if (r && typeof r.seconds === "number") host.__wpSet(r.seconds, true);
      }).catch(() => {});
    };
    initTimeoutWheel(".prefs-live-panel-thinking-input", () => api.getLivePanelThinkingTimeout(), "live_panel_thinking_timeout", 60, "thinking");
    initTimeoutWheel(".prefs-live-panel-gap-input",       () => api.getLivePanelGapTimeout(),       "live_panel_gap_timeout",       20, "gap");
    initTimeoutWheel(".prefs-live-panel-text-input",      () => api.getLivePanelTextTimeout(),      "live_panel_text_timeout",      10, "text");
    initTimeoutWheel(".prefs-live-panel-done-clear-input", () => api.getLivePanelDoneClearTimeout(), "live_panel_done_clear_timeout", 10, "done-clear");
    initTimeoutWheel(".prefs-live-panel-tools-clear-input", () => api.getLivePanelToolsClearTimeout(), "live_panel_tools_clear_timeout", 5, "tools-clear");
    // v0.135：3 个新增设置初始值（开关 / 数字 / 字号档位）。
    // v0.169：旧 DOM 快照优先，避免用户刚改的 checkbox/number 被刷回。
    const toolsAlwaysToggle = body.querySelector(".prefs-live-panel-tools-always");
    if (toolsAlwaysToggle) {
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["chk:prefs-live-panel-tools-always"];
      if (typeof dirty === "boolean") {
        toolsAlwaysToggle.checked = dirty;
      } else {
        const r = await api.getLivePanelToolsAlways();
        if (r && typeof r.enabled === "boolean") toolsAlwaysToggle.checked = r.enabled;
      }
    }
    const minColsInput = body.querySelector(".prefs-live-panel-min-cols-input");
    if (minColsInput) {
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["num:prefs-live-panel-min-cols-input"];
      if (typeof dirty === "number") {
        minColsInput.value = String(dirty);
      } else {
        const r = await api.getLivePanelMinCols();
        if (r && typeof r.value === "number") minColsInput.value = String(r.value);
        else minColsInput.value = String((snap && typeof snap.live_panel_min_cols === "number") ? snap.live_panel_min_cols : 1);
      }
    }
    const listFontSeg = body.querySelector(".seg-list-font");
    if (listFontSeg) {
      // v0.169：旧 DOM 快照优先 — 用户刚选的小/中/大 不会被刷回默认。
      const dirty = _prefsDirtySnapshot && _prefsDirtySnapshot["seg:list-font"];
      if (typeof dirty === "string") {
        listFontSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.listFont === dirty));
      } else {
        const r = await api.getLivePanelListFont();
        const v = (r && typeof r.value === "string") ? r.value : "medium";
        listFontSeg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.listFont === v));
      }
    }
    // v0.12：保存消息开关——从 /api/settings 实时拉后端值。
    const saveMsgs = body.querySelector(".prefs-save-messages");
    if (saveMsgs) {
      const res = await api.getRelaySettings();
      if (res && typeof res.save_messages === "boolean") saveMsgs.checked = res.save_messages;
    }

    const rs = await api.getRelaySettings();
    if (rs && !rs.error) {
      const autoSwitch = body.querySelector(".prefs-autoswitch");
      if (autoSwitch) {
        autoSwitch.checked = !!rs.autoswitch_enabled;
        const poolWrap = body.querySelector("#prefs-autoswitch-pool");
        // v0.174：开发者模式子项 —— 关掉开发者模式时强制隐藏
        if (poolWrap) poolWrap.hidden = !autoSwitch.checked || !prefs.developerMode;
      }
      const chipsEl = body.querySelector("#prefs-pool-chips");
      if (chipsEl) {
        const names = [];
        const cfg = (snap && snap.upstreams) || {};
        for (const list of Object.values(cfg)) {
          for (const c of (list || [])) if (c && c.name) names.push(c.name);
        }
        const poolSet = new Set(rs.autoswitch_pool || []);
        chipsEl.innerHTML = names.length
          ? names.map(n => `
              <label class="prefs-pool-chip">
                <input type="checkbox" data-name="${attr(n)}" ${poolSet.has(n) ? "checked" : ""} />
                <span>${escape(n)}</span>
              </label>`).join("")
          : '<span class="card-empty">暂无上游</span>';
      }
    }

    // v0.11.18 高级切换：开关开着就加载配置面板。
    const advToggle = body.querySelector(".prefs-advanced-switch");
    const advPanel = body.querySelector("#advanced-switch-panel");
    if (advToggle && advPanel) {
      advPanel.hidden = !advToggle.checked;
      if (advToggle.checked) loadAdvancedSwitchPanel(advPanel);
    }
  }

  // v0.113n + v0.113p：存储管理（设置页「存储管理」区块）。
  // v0.113p 重排：每个存储一张卡（名称 / 路径 / 占用 / 就地操作按钮），
  // 不再把占用和位置拆成两组对读；upstreams.json 退化为只读展示行。
  // 只渲染一次（storageRendered 守卫）；「刷新」按钮 + 每次操作后手动
  // 重拉 —— 避免 500ms tick 每帧做文件 I/O + 只读 DB 查询。
  // 占用信息走本地桥 getStorageInfo（relay 停了也能看）；破坏性操作走
  // relay HTTP（cleanupMessages / vacuumStorage / clearLogs / moveStorage），
  // 清理/压缩按 target 只作用于当前卡对应的库。
  let storageRendered = false;
  let _storageInfo = null;
  function _fmtBytes(n) {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i ? v.toFixed(1) : v) + " " + units[i];
  }
  function _fmtTs(ts) {
    if (ts == null) return "—";
    try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return "—"; }
  }
  function _fillStorage(body, info) {
    if (!body || !info) return;
    const setDetail = (key, text) => {
      const el = body.querySelector(`[data-storage-detail="${key}"]`);
      if (el) el.textContent = text;
    };
    const setLoc = (key, path) => {
      const el = body.querySelector(`[data-storage-loc="${key}"]`);
      if (el) el.textContent = path || "—";
    };
    // v0.113r：动态混合文案（数字 + 中文片段）不经过 applyLang，模板里用
    // t() 直接出译文。
    setDetail("db", `${_fmtBytes(info.db.size)} · ${info.db.requests} ${t("请求")} · ${info.db.messages} ${t("条消息")} · ${_fmtTs(info.db.ts_min)} ~ ${_fmtTs(info.db.ts_max)}`);
    setDetail("pt_db", `${_fmtBytes(info.pt_db.size)} · ${info.pt_db.requests} ${t("条透传请求")}`);
    setDetail("logs", `${_fmtBytes(info.logs.size)} · ${info.logs.files} ${t("个文件")}`);
    setDetail("upstreams", `${_fmtBytes(info.upstreams.size)}`);
    setLoc("db", info.db.path);
    setLoc("pt_db", info.pt_db.path);
    setLoc("logs", info.logs.dir);
    setLoc("upstreams", info.upstreams.path);
  }
  async function refreshStorageInfo(body) {
    const info = await api.getStorageInfo();
    if (!info) return;
    _storageInfo = info;
    _fillStorage(body, info);
  }
  async function renderSettingsStorage(body) {
    if (!body || storageRendered) return;
    storageRendered = true;
    body.innerHTML = `
      <div class="settings-list">
        <div class="storage-card">
          <div class="storage-card-head">
            <span class="storage-card-name">消息数据库</span>
            <code class="storage-card-file">relay.db</code>
          </div>
          <div class="storage-card-path" data-storage-loc="db">—</div>
          <div class="storage-card-stats" data-storage-detail="db">加载中…</div>
          <div class="storage-card-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-storage-op="cleanup" data-target="relay">清理消息记录</button>
            <button type="button" class="btn btn-ghost btn-sm" data-storage-op="vacuum" data-target="relay">压缩</button>
            <button type="button" class="btn btn-ghost btn-sm" data-move="relay">修改位置</button>
          </div>
        </div>
        <div class="storage-card">
          <div class="storage-card-head">
            <span class="storage-card-name">透传数据库</span>
            <code class="storage-card-file">passthrough.db</code>
          </div>
          <div class="storage-card-path" data-storage-loc="pt_db">—</div>
          <div class="storage-card-stats" data-storage-detail="pt_db">加载中…</div>
          <div class="storage-card-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-storage-op="cleanup" data-target="passthrough">清理透传记录</button>
            <button type="button" class="btn btn-ghost btn-sm" data-storage-op="vacuum" data-target="passthrough">压缩</button>
            <button type="button" class="btn btn-ghost btn-sm" data-move="passthrough">修改位置</button>
          </div>
        </div>
        <div class="storage-card">
          <div class="storage-card-head">
            <span class="storage-card-name">日志目录</span>
            <code class="storage-card-file">.relay-logs</code>
          </div>
          <div class="storage-card-path" data-storage-loc="logs">—</div>
          <div class="storage-card-stats" data-storage-detail="logs">加载中…</div>
          <div class="storage-card-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-storage-op="logs">清空日志</button>
          </div>
        </div>
        <div class="storage-card storage-card-readonly">
          <div class="storage-card-head">
            <span class="storage-card-name">上游配置</span>
            <code class="storage-card-file">upstreams.json</code>
          </div>
          <div class="storage-card-path" data-storage-loc="upstreams">—</div>
          <div class="storage-card-stats" data-storage-detail="upstreams">加载中…</div>
        </div>
        <div class="storage-hint">「清理」「压缩」需中继运行中执行。修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时，需先停止中继再迁移。</div>
        <div class="storage-card-actions storage-card-actions-footer">
          <button type="button" class="btn btn-ghost btn-sm" data-storage-op="refresh">刷新</button>
        </div>
      </div>
    `;
    body.querySelectorAll("[data-storage-op]").forEach((btn) =>
      btn.addEventListener("click", onStorageOp),
    );
    body.querySelectorAll("[data-move]").forEach((btn) =>
      btn.addEventListener("click", onMoveStorage),
    );
    refreshStorageInfo(body);
    applyLang();
  }

  // 清理记录：自绘弹窗选「保留 N 天 / 清空全部」，返回 days 或 null（取消）。
  // v0.113p：targetName 指明要清哪个库，标题随当前卡联动。
  function pickCleanupDays(targetName) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.setAttribute("data-dynamic-dialog", "storage-cleanup");
      overlay.hidden = false;
      overlay.innerHTML = `
        <div class="modal-card modal-card-dialog" role="dialog" aria-modal="true">
          <button class="modal-close" aria-label="关闭" type="button">×</button>
          <div class="modal-header"><div class="modal-title">清理记录${targetName ? " · " + targetName : ""}</div></div>
          <div class="modal-body dialog-body">
            <div class="settings-row"><span class="settings-row-label">保留最近</span></div>
            <div class="settings-row">
              <span class="seg seg-cleanup">
                <button type="button" class="seg-btn" data-days="7">7 天</button>
                <button type="button" class="seg-btn" data-days="30">30 天</button>
                <button type="button" class="seg-btn" data-days="90">90 天</button>
                <button type="button" class="seg-btn" data-days="180">180 天</button>
              </span>
            </div>
            <div class="settings-row sc-clear-row">
              <button type="button" class="btn btn-danger" data-days="0">清空全部记录</button>
            </div>
          </div>
          <div class="dialog-actions">
            <button type="button" class="btn" data-dialog-cancel>取消</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      document.body.classList.add("modal-open");
      const close = (v) => {
        overlay.hidden = true;
        document.body.classList.remove("modal-open");
        resolve(v);
      };
      const card = overlay.querySelector(".modal-card");
      if (card) card.addEventListener("click", (e) => e.stopPropagation());
      overlay.addEventListener("click", (e) => { if (e.target === overlay) close(null); });
      overlay.querySelector(".modal-close").addEventListener("click", () => close(null));
      overlay.querySelector("[data-dialog-cancel]").addEventListener("click", () => close(null));
      overlay.querySelectorAll("[data-days]").forEach((btn) => {
        btn.addEventListener("click", () => close(parseInt(btn.dataset.days, 10) || 0));
      });
      document.addEventListener("keydown", function esc(e) {
        if (e.key === "Escape" && !overlay.hidden) { close(null); document.removeEventListener("keydown", esc); }
      });
    });
  }

  async function onStorageOp(e) {
    const body = document.getElementById("card-settings-storage-body");
    const op = e.currentTarget.dataset.storageOp;
    const target = e.currentTarget.dataset.target; // "relay" | "passthrough" | undefined
    const targetName = target === "passthrough"
      ? "透传数据库 (passthrough.db)"
      : target === "relay" ? "消息数据库 (relay.db)" : "";
    if (op === "refresh") { await refreshStorageInfo(body); return; }
    if (op === "cleanup") {
      const days = await pickCleanupDays(targetName);
      if (days == null) return;
      const res = await api.cleanupMessages(days, target);
      if (res && res.ok) {
        const parts = [];
        if (res.relay_deleted) parts.push(`${res.relay_deleted} 条中继请求`);
        if (res.passthrough_deleted) parts.push(`${res.passthrough_deleted} 条透传请求`);
        await alertModal("清理完成", "已删除 " + (parts.join(" · ") || "0 条记录"));
      } else {
        await alertModal("清理失败", (res && res.error) || "中继未运行");
      }
    } else if (op === "vacuum") {
      const what = targetName || "relay.db 与 passthrough.db";
      const ok = await confirmModal("压缩数据库", `对 ${what} 执行 VACUUM，回收删除后未释放的空间。期间对应库的请求可能短暂停顿。`, { okText: "压缩", cancelText: "取消" });
      if (!ok) return;
      const res = await api.vacuumStorage(target);
      if (res && res.ok) await alertModal("压缩完成", "数据库已压缩");
      else await alertModal("压缩失败", (res && res.error) || "中继未运行");
    } else if (op === "logs") {
      const ok = await confirmModal("清空日志", "删除 .relay-logs/ 目录下的所有日志文件。", { okText: "清空", cancelText: "取消", danger: true });
      if (!ok) return;
      const res = await api.clearLogs();
      if (res && res.ok) await alertModal("清空完成", `已删除 ${res.removed} 个日志文件`);
      else await alertModal("清空失败", (res && res.error) || "中继未运行");
    }
    await refreshStorageInfo(body);
  }

  async function onMoveStorage(e) {
    const info = _storageInfo;
    if (!info) return;
    const kind = e.currentTarget.dataset.move; // "relay" | "passthrough"
    const current = kind === "relay" ? info.db.path : info.pt_db.path;
    const next = prompt("新的数据库文件完整路径（含文件名，如 D:\\relay\\data.db）:", current);
    if (next == null || !next.trim()) return;
    const res = await api.moveStorage(kind, next.trim());
    const body = document.getElementById("card-settings-storage-body");
    if (res && res.ok) {
      await alertModal("迁移完成", `已迁移到：${res.path}\n\n若中继正在运行，请先在「中继状态」停止中继，再重新启动以使用新位置。`);
    } else {
      await alertModal("迁移失败", (res && res.error) || "未知错误");
    }
    await refreshStorageInfo(body);
  }

  // v0.113o：报错分析设置区块（开关 + 分析模型 + 测试按钮）。守卫只渲
  // 染一次；「保存」才写 upstreams.json（本地不改，跨视图常驻）。
  let errorAnalysisRendered = false;
  async function renderSettingsError(body, snap) {
    if (!body || errorAnalysisRendered) return;
    errorAnalysisRendered = true;
    // v0.113u：先用 snap.error_analysis_* 同步初值填充开关/select，避免
    // 异步 await getErrorAnalysis() 期间首帧空白。然后异步刷新一次权威值。
    const initialEnabled = !!(snap && snap.error_analysis_enabled);
    const initialUpstream = (snap && snap.error_analysis_upstream) || null;
    const initialModel = (snap && snap.error_analysis_model) || null;
    const data = await api.getErrorAnalysis();
    if (!data) return;
    const cfg = data.config || {};
    const models = data.models || [];
    // snap 优先；如果 snap 没值（首帧先渲染、async 还没回），用 cfg 的真值补救
    const enabled = initialEnabled || !!cfg.enabled;
    const selUp = initialUpstream || cfg.upstream || "";
    const selMo = initialModel || cfg.model || "";
    const cur = selUp && selMo ? selUp + "\u0000" + selMo : "";
    body.innerHTML = `
      <div class="settings-list">
        <div class="settings-group">
          <div class="settings-group-label" data-i18n="报错分析">报错分析</div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="允许小模型分析报错信息">允许小模型分析报错信息</div>
              <div class="settings-item-hint" data-i18n="出现报错时把错误信息发给所选模型，判断错误类型并给出中文提示（余额不足 / 网络错误 / 达到次数限制等）。">出现报错时把错误信息发给所选模型，判断错误类型并给出中文提示（余额不足 / 网络错误 / 达到次数限制等）。</div>
            </div>
            <div class="settings-item-control">
              <label class="switch"><input type="checkbox" id="error-analysis-toggle"${enabled ? " checked" : ""}> <span class="switch-track"><span class="switch-thumb"></span></span></label>
            </div>
          </div>
          <div class="settings-item" data-error-analysis-part="model"${enabled ? "" : " hidden"}>
            <div class="settings-item-info">
              <div class="settings-item-title" data-i18n="分析模型">分析模型</div>
              <div class="settings-item-hint" data-i18n="建议选择轻量快速的小模型；留空则使用默认上游的兜底模型。">建议选择轻量快速的小模型；留空则使用默认上游的兜底模型。</div>
            </div>
            <div class="settings-item-control">
              <select id="error-analysis-model" class="cfg-input"></select>
            </div>
          </div>
          <div class="settings-item">
            <div class="settings-item-info">
              <div class="settings-item-hint" data-i18n="「测试」用一条示例 429 报错真实跑一次分类，验证模型与提示词。保存后立即生效，无需重启中继。">「测试」用一条示例 429 报错真实跑一次分类，验证模型与提示词。保存后立即生效，无需重启中继。</div>
            </div>
            <div class="settings-item-control settings-storage-actions">
              <button type="button" class="btn btn-ghost" id="error-analysis-test"${cfg.enabled ? "" : " hidden"} data-i18n="测试">测试</button>
              <button type="button" class="btn btn-ghost" id="error-analysis-save" data-i18n="保存">保存</button>
            </div>
          </div>
        </div>
      </div>
    `;
    // 模型下拉用 DOM API 建 option —— innerHTML 里的 \u0000 会被浏览器
    // 替换成 U+FFFD，破坏 upstream\u0000model 分隔（DOM 属性赋值则保留）。
    const sel = body.querySelector("#error-analysis-model");
    if (sel) {
      if (!models.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "（未配置上游模型）";
        sel.appendChild(opt);
      } else {
        for (const m of models) {
          const opt = document.createElement("option");
          opt.value = m.upstream + "\u0000" + m.model;
          opt.setAttribute("data-i18n-keep", "");
          opt.textContent = m.label;
          if (opt.value === cur) opt.selected = true;
          sel.appendChild(opt);
        }
      }
    }
    body.querySelector("#error-analysis-save").addEventListener("click", onSaveErrorAnalysis);
    body.querySelector("#error-analysis-test").addEventListener("click", onTestErrorAnalysis);
    // v0.113q：开关关闭 → 折叠「分析模型」条目与「测试」按钮（保留「保存」
    // 以便把关闭状态存下来）；打开 → 展开恢复。
    body.querySelector("#error-analysis-toggle").addEventListener("change", onErrorAnalysisToggle);
    applyLang();
  }

  async function onSaveErrorAnalysis() {
    const body = document.getElementById("card-settings-error-body");
    if (!body) return;
    const sel = body.querySelector("#error-analysis-model");
    const toggle = body.querySelector("#error-analysis-toggle");
    let upstream = "", model = "";
    if (sel && sel.value) {
      const i = sel.value.indexOf("\u0000");
      if (i !== -1) { upstream = sel.value.slice(0, i); model = sel.value.slice(i + 1); }
    }
    const res = await api.setErrorAnalysis({
      enabled: toggle ? toggle.checked : false,
      upstream,
      model,
    });
    if (res && res.ok) {
      await alertModal("已保存", "报错分析配置已保存并生效。");
    } else {
      await alertModal("保存失败", (res && res.error) || "bridge 不可达");
    }
  }

  function onTestErrorAnalysis() {
    window.relayErrorAnalysisDone = (result) => {
      if (result && result.ok) {
        alertModal("分析成功", `类别：${result.category || result.type}\n提示：${result.hint || "（空）"}`);
      } else {
        alertModal("分析失败", (result && result.error) || "未知错误");
      }
    };
    api.testErrorAnalysis();
  }

  // v0.190：支持图片的模型 —— 纯多选 chip 列表（无主开关；勾选即功能开关）。
  // 容器 = 开发者模式组内的 #prefs-vision-chip-box，随开发者模式显隐。
  // 无 module-level 守卫 —— prefs 视图每次切换 renderSettingsPrefs 都会整个
  // 重写 prefs body，chip-box 被覆写成新节点；每次进入 prefs 视图重新拉一次
  // chip 数据（视觉无抖动，开销可忽略）。click / save 绑定用节点标记防重复。
  async function renderSettingsVision(prefsBody, snap) {
    if (!prefsBody) return;
    const box = prefsBody.querySelector("#prefs-vision-chip-box");
    if (!box) return;  // 开发者模式关 / 视图未渲染到该容器 —— 静默 return
    // 先用 snap.vision_models 同步填初值，避免异步 getVisionModels 首帧空白。
    const initial = new Set((snap && Array.isArray(snap.vision_models) ? snap.vision_models : []));
    const data = await api.getVisionModels();
    if (!data) {
      box.innerHTML = '<div class="card-empty">（无法读取已声明模型）</div>';
      return;
    }
    // v0.192：models 现在是 {model, label} 富结构（label 带上游前缀，
    // 如 "dp官方 / deepseek-v4-flash-vision-exp"），model 仍是 OpenCode 认
    // 的裸模型 id。data-model 存裸名（保存/判等用），chip 显示 label。
    const all = (data.models || []).map(m => (typeof m === "string" ? { model: m, label: m } : m));
    const allModelSet = new Set(all.map(o => o.model));
    const cur = (data.vision_models || []).filter(m => allModelSet.has(m));
    // snap 优先；snap 没值（异步还没回）用 bridge 权威值补救。
    const selected = new Set(cur.length ? cur : (initial.size ? initial : []));
    const chips = all.map(o => {
      const on = selected.has(o.model);
      return `<span class="cfg-chip cfg-vm-chip${on ? " active" : ""}" data-model="${attr(o.model)}" role="button" tabindex="0">${escape(o.label)}</span>`;
    }).join("");
    box.innerHTML = chips || '<div class="card-empty">（未发现已声明模型）</div>';
    // 点击切换勾选 —— 监听器绑在 chip-box 上（box 每次重写都是新节点，重绑安全）。
    if (!box._visionClickBound) {
      box.addEventListener("click", (e) => {
        const chip = e.target.closest(".cfg-vm-chip");
        if (!chip) return;
        chip.classList.toggle("active");
      });
      box._visionClickBound = true;
    }
    // 保存按钮 —— 同样按 prefs body 维度找；该按钮每次重写都是新节点，
    // 但 prefs body 是稳定的，按 body 维度的 _visionSaveBound 标记复用监听器，
    // 避免重复绑定导致多次保存。
    const saveBtn = prefsBody.querySelector("#prefs-vision-save");
    if (saveBtn && !prefsBody._visionSaveBound) {
      saveBtn.addEventListener("click", onSaveVisionModels);
      prefsBody._visionSaveBound = true;
    }
    applyLang();
  }

  async function onSaveVisionModels() {
    // v0.190：chip 容器从 card-settings-vision-chips 搬到 #prefs-vision-chip-box
    // （开发者模式组内）。
    const box = document.getElementById("prefs-vision-chip-box");
    if (!box) return;
    const selected = Array.from(box.querySelectorAll(".cfg-vm-chip.active"))
      .map(el => el.getAttribute("data-model"))
      .filter(Boolean);
    const res = await api.setVisionModels(selected);
    if (res && res.ok) {
      await alertModal("已保存", "支持图片的模型名单已保存并生效。");
    } else {
      await alertModal("保存失败", (res && res.error) || "bridge 不可达");
    }
  }

  // v0.113q：开关实时折叠/展开「分析模型」条目与「测试」按钮（不保存，
  // 关闭状态经「保存」持久化；本地 state 只在 DOM 层面切换）。
  function onErrorAnalysisToggle() {
    const body = document.getElementById("card-settings-error-body");
    if (!body) return;
    const on = !!body.querySelector("#error-analysis-toggle")?.checked;
    const modelItem = body.querySelector('[data-error-analysis-part="model"]');
    const testBtn = body.querySelector("#error-analysis-test");
    if (modelItem) modelItem.hidden = !on;
    if (testBtn) testBtn.hidden = !on;
  }

  // v0.113o：报错分析结果 toast —— 快照带 error_hints 增量，渲染成右
  // 上角错误提示卡，自动 8s 淡出 + × 关闭；同 id 不重复弹。
  const seenErrorHintIds = new Set();
  function renderErrorHints(snap) {
    const items = (snap && Array.isArray(snap.error_hints)) ? snap.error_hints : [];
    if (!items.length) return;
    let box = document.getElementById("error-hints");
    if (!box) {
      box = document.createElement("div");
      box.id = "error-hints";
      document.body.appendChild(box);
    }
    const dismiss = (card) => {
      if (!card.isConnected || card.classList.contains("error-hint-leave")) return;
      card.classList.add("error-hint-leave");
      setTimeout(() => card.remove(), 300);
    };
    for (const h of items) {
      if (!h || seenErrorHintIds.has(h.id)) continue;
      seenErrorHintIds.add(h.id);
      const card = document.createElement("div");
      card.className = "error-hint error-hint-" + (h.type || "other");
      const title = document.createElement("div");
      title.className = "error-hint-title";
      title.textContent = (h.category || h.type || "错误") + (h.upstream ? " · " + h.upstream : "");
      const detail = document.createElement("div");
      detail.className = "error-hint-detail";
      detail.textContent = h.hint || "";
      const close = document.createElement("button");
      close.className = "error-hint-close";
      close.textContent = "×";
      close.addEventListener("click", () => dismiss(card));
      card.appendChild(title);
      card.appendChild(detail);
      card.appendChild(close);
      box.appendChild(card);
      setTimeout(() => dismiss(card), 8000);
    }
  }

  // 共用保存入口 —— 新增 / 编辑 / 删除都走这里。失败 alert，成功则
  // 把签名作废 + 拉一次 refresh（下一次 poll 自然带新列表）。
  async function saveQuickSwitchList(items) {
    const res = await api.saveQuickSwitch(items);
    if (!res || res.ok === false) {
      // v0.71：替换 alert() 为自定义 modalModal,视觉跟其它对话框一致。
      await alertModal("保存失败", (res && res.error) || "bridge 不可达");
      return;
    }
    // 数据可能因 Python 侧清洗被改动（比如 label 被 trim），签名作废
    // 让下一次 renderSettingsQuickSwitch 强制重建。
    lastQuickSwitchCfgSig = null;
    api.refresh();
  }

  // 编辑器 modal —— 新增 / 编辑共用。existing = null 时是新增。
  // v0.72：跟 sidebar "切换菜单"完全一致 —— 单一 <select> 把
  // (api, model) 展平成单个 option,跟 renderSidebar 那套规则同款:
  //   0 个 allowed_models → 1 个 option(value model 字段空, label 只显示 api)
  //   1 个 allowed_models → 1 个 option(model = 那个)
  //   N 个 allowed_models → N 个 option(label 显示 "api - model")
  // 用户体感 = 跟切上游的下拉一样,无需先选 api 再选 model 两步走。
  function openQuickSwitchEditor(existing) {
    const overlay = $("qs-editor-overlay");
    const body = $("qs-editor-body");
    if (!overlay || !body) return;
    const upstreams = (lastSnap && lastSnap.upstreams) || {};

    // 把 snapshot.upstreams 按 sidebar 同款规则展平。每个 option 的
    // value 是 "platform|name|model?"(model 可空),label 是 sidebar
    // 用的文本样式,这样用户看到的就是同一个下拉。
    const opts = [];
    let firstOptionValue = null;
    Object.keys(upstreams).forEach(plat => {
      (upstreams[plat] || []).forEach(c => {
        const allowed = Array.isArray(c.allowed_models) ? c.allowed_models : [];
        const primaryModel = c.model || null;
        if (allowed.length === 0) {
          // 0 个 allowed_models：1 个 option, model 字段留空
          const val = `${plat}|${c.name}|`;
          const sel = (!firstOptionValue) ? " selected" : "";
          if (!firstOptionValue) firstOptionValue = val;
          opts.push(`<option value="${escape(val)}"${sel} data-i18n-keep>${escape(c.name)}</option>`);
        } else if (allowed.length === 1) {
          // 1 个：1 个 option, model = 那一个
          const val = `${plat}|${c.name}|${allowed[0]}`;
          const sel = (!firstOptionValue) ? " selected" : "";
          if (!firstOptionValue) firstOptionValue = val;
          opts.push(`<option value="${escape(val)}"${sel} data-i18n-keep>${escape(c.name)}</option>`);
        } else {
          // N 个：每个模型一个 option
          allowed.forEach(m => {
            const val = `${plat}|${c.name}|${m}`;
            const sel = (!firstOptionValue) ? " selected" : "";
            if (!firstOptionValue) firstOptionValue = val;
            opts.push(`<option value="${escape(val)}"${sel} data-i18n-keep>${escape(c.name)} - ${escape(m)}</option>`);
          });
        }
      });
    });

    // existing 命中检测 —— 编辑一条已删 upstream 的旧快捷项时,把它
    // 插到顶部并 selected 提示"已删除"。保存时由 Python 端校验拒绝。
    // v0.75：原来 else 分支里 `existing.platform` 是裸的 —— 当
    // existing = null (新增场景) 时,if 条件 `null && ...` 为 falsy,
    // 落到 else,触发 "Cannot read properties of null (reading
    // 'platform')",整个 openQuickSwitchEditor 抛错,modal 永远弹不
    // 出来。修法:把 else 分支改成只在 existing 有值时才尝试标
    // selected,否则保持 opts 原样(默认首个 selected 在前面已经做)。
    if (existing && !opts.some(o => {
      // 反解 value 检查 (platform, upstream, model) 命中
      // option.value 是 escape 过的,这里直接字符串比较即可
      const v = o.match(/value="([^"]+)"/);
      if (!v) return false;
      const [p, n, m] = v[1].split("|");
      return p === existing.platform && n === existing.upstream
        && (m || "") === (existing.model || "");
    })) {
      const staleVal = `${existing.platform}|${existing.upstream}|${existing.model || ""}`;
      opts.unshift(`<option value="${escape(staleVal)}" selected data-i18n-keep>${escape(existing.platform)} / ${escape(existing.upstream)}${existing.model ? ` - ${escape(existing.model)}` : ""} （已删除）</option>`);
      firstOptionValue = staleVal;
    } else if (existing) {
      // existing 命中了 → 把对应那个 option 标 selected
      const targetVal = `${existing.platform}|${existing.upstream}|${existing.model || ""}`;
      for (let i = 0; i < opts.length; i++) {
        if (opts[i].includes(`value="${escape(targetVal)}"`)) {
          opts[i] = opts[i].replace(/<option /, '<option selected ');
          firstOptionValue = targetVal;
          break;
        }
      }
    }

    body.innerHTML = `
      <label class="cfg-field">
        <span class="cfg-label">名称 (label)</span>
        <input class="cfg-input" id="qs-edit-label"
               value="${escape(existing && existing.label || "")}"
               placeholder="例：Sonnet 5" />
      </label>
      <label class="cfg-field">
        <span class="cfg-label">切换目标 (api · model)</span>
        <select class="cfg-input" id="qs-edit-target">
          ${opts.join("") || '<option value="" disabled selected>（未配置任何上游）</option>'}
        </select>
      </label>
      <div class="cfg-actions">
        <button type="button" class="btn" id="qs-edit-cancel">取消</button>
        <button type="button" class="btn btn-primary" id="qs-edit-save">${existing ? "保存" : "新增"}</button>
        <span class="cfg-status" id="qs-edit-status"></span>
      </div>`;
    overlay.hidden = false;
    document.body.classList.add("modal-open");

    const close = () => {
      overlay.hidden = true;
      document.body.classList.remove("modal-open");
    };
    const cancelBtn = $("qs-edit-cancel");
    if (cancelBtn) cancelBtn.onclick = close;

    const saveBtn = $("qs-edit-save");
    if (saveBtn) saveBtn.onclick = async () => {
      // value 格式 "platform|name|model?",反解三个字段
      const raw = $("qs-edit-target") && $("qs-edit-target").value || "";
      const parts = raw.split("|");
      const platform = parts[0] || "";
      const upstream = parts[1] || "";
      const model    = parts[2] || "";   // 0/1 个 allowed_models 时为空字符串
      const item = {
        label:    ($("qs-edit-label") && $("qs-edit-label").value || "").trim(),
        platform: platform.trim(),
        upstream: upstream.trim(),
        model:    model.trim(),
      };
      // 0/1 个 allowed_models 时 model 字段空 → 用 upstream 的默认 model
      // 字段填充,这样保存后 sidebar 选中态依然能命中。如果 upstream
      // 也没 model 字段,fallback 到 platform 对应的 hardcoded 默认值。
      if (!item.model) {
        const upstreamCfg = (upstreams[item.platform] || []).find(c => c.name === item.upstream);
        if (upstreamCfg && upstreamCfg.model) {
          item.model = upstreamCfg.model;
        } else {
          // 真没 model —— 没法构成合法条目,挡住保存。
          await alertModal("字段未选", "所选上游没有可用模型，无法保存");
          return;
        }
      }
      if (!item.label) {
        await alertModal("字段未选", "名称不能为空");
        return;
      }
      const list = (lastSnap && Array.isArray(lastSnap.quick_switch))
        ? lastSnap.quick_switch.slice() : [];
      if (existing) {
        const idx = list.findIndex(q =>
          q.label === existing.label &&
          q.platform === existing.platform &&
          q.upstream === existing.upstream &&
          q.model === existing.model);
        if (idx >= 0) list[idx] = item; else list.push(item);
      } else {
        list.push(item);
      }
      saveBtn.disabled = true;
      const res = await api.saveQuickSwitch(list);
      saveBtn.disabled = false;
      if (!res || res.ok === false) {
        await alertModal("保存失败", (res && res.error) || "bridge 不可达");
        return;
      }
      lastQuickSwitchCfgSig = null;
      api.refresh();
      close();
    };
  }

  /** Wire the "+ 新建" modal: open/close handlers, dynamic chip +
   *  multiplier row builders, and the submit handler that posts to
   *  api.createUpstream. Called once from wireButtons() — the modal
   *  lives in index.html and survives the upstream-list rerenders. */
  function wireUpstreamCreate() {
    const overlay = $("create-overlay");
    const btnClose = $("create-close");
    const btnCancel = $("create-cancel");
    const form = $("create-form");
    const status = $("create-status");
    if (!overlay || !form) return;

    // v0.12：协议三按钮 —— 点选一个 wire（再点一次取消=自动）。
    // v0.12.1：setWire/setAuthStyle 定义在 wireUpstreamCreate 作用域
    // （原在 wireChipsAndMults 内 —— open()/回填引用会 ReferenceError）。
    function setWire(value) {
      const hidden = $("create-wire");
      if (hidden) hidden.value = value || "";
      document.querySelectorAll("#create-wire-btns .wire-btn").forEach(btn => {
        btn.classList.toggle("wire-btn-active", btn.getAttribute("data-wire") === value);
      });
    }
    document.querySelectorAll("#create-wire-btns .wire-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const v = btn.getAttribute("data-wire");
        setWire(btn.classList.contains("wire-btn-active") ? "" : v);
        // v0.79：手动改协议视为放弃预设。
        const ps = $("create-preset");
        if (ps && ps.value) { ps.value = ""; setStatus("预设已清除（手动调整中）"); return; }
        setStatus("");
      });
    });

    // v0.12.1：鉴权头三选一按钮。再点一次取消 = 跟随协议默认。
    function setAuthStyle(value) {
      const hidden = $("create-auth-style");
      if (hidden) hidden.value = value || "";
      document.querySelectorAll("#create-auth-btns .wire-btn").forEach(btn => {
        btn.classList.toggle("wire-btn-active", btn.getAttribute("data-auth") === value);
      });
    }
    document.querySelectorAll("#create-auth-btns .wire-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const v = btn.getAttribute("data-auth");
        setAuthStyle(btn.classList.contains("wire-btn-active") ? "" : v);
        // v0.79：手动改鉴权视为放弃预设。
        const ps = $("create-preset");
        if (ps && ps.value) { ps.value = ""; setStatus("预设已清除（手动调整中）"); return; }
        setStatus("");
      });
    });

    // v0.79：OpenCode 预设配置 —— 依据 docs/opencode-zen-upstreams.md 的
    // 实测结论（端点/协议/鉴权/计费语义），选中后自动填表单。用户只需
    // 输入名称 + API Key（opencode 应用程序同款体验）。
    //   deepseek        : 官方 Anthropic 端点，x-api-key，官方模型自动进允许列表
    //   opencode-go     : Go 订阅段（付费），Anthropic 协议 + adapter + token 计费
    //   opencode-zen    : 按量段，OpenAI Chat 协议 + Bearer（需 Zen 余额）
    //   opencode-zen-free: 免费段，模型下拉自动填 *-free 模型（v0.98.4）
    const UPSTREAM_PRESETS = {
      "deepseek": {
        url: "https://api.deepseek.com/anthropic",
        wire: "anthropic-messages",
        auth: "",                          // 跟随协议默认（x-api-key）
        billing: "count",
        adapter: false,
        models: ["deepseek-v4-flash", "deepseek-v4-pro"],
        note: "DeepSeek 官方（Anthropic 协议，deepseek-v4-flash / deepseek-v4-pro）",
      },
      "opencode-go": {
        url: "https://opencode.ai/zen/go/v1",
        wire: "anthropic-messages",
        auth: "",                    // 跟随协议默认（x-api-key）
        billing: "token",
        adapter: true,
        note: "OpenCode Go 订阅（付费 $5/月；未订阅会报『余额不足』。免费请用 OpenCode Zen 免费模型）",
      },
      "opencode-zen": {
        url: "https://opencode.ai/zen/v1",
        wire: "openai-chat",
        auth: "bearer",
        billing: "count",
        adapter: false,
        note: "OpenCode Zen 按量付费（需 Zen 账户余额）",
      },
      // v0.98.4：opencode-zen-free —— 模型下拉自动填。免费模型名必须带
      // *-free 后缀（服务端 allowAnonymous 跳过余额检查），不带则按付费
      // 模型计费 → 无余额报 401「余额不足」。名称自动拼成 opc-<模型名>。
      "opencode-zen-free": {
        url: "https://opencode.ai/zen/v1",
        wire: "openai-chat",
        auth: "bearer",
        billing: "count",
        adapter: false,
        modelPrefix: "opc-",
        modelOptions: [
          "deepseek-v4-flash-free",
          "hy3-free",
          "laguna-s-2.1-free",
          "mimo-v2.5-free",
          "mimo-v2-pro-free",
          "nemotron-3.5-lightning-free",
          "nemotron-3-ultra-free",
          "kimi-k2.5-free",
          "glm-4.7-free",
          "minimax-m3-free",
        ],
        modelNote: (m) => "OpenCode Zen 免费模型（" + m + "，免费额度可能 429 限流）",
        note: "OpenCode Zen 免费模型（选模型后自动填名称 opc-模型名 + 允许列表）",
      },
      // v0.98.2：HuoShan Agent Plan —— 官方 Anthropic 兼容端点直通
      // （https://ark.cn-beijing.volces.com/api/plan），x-api-key 跟随
      // 协议默认。火山无 /models 查询接口，模型列表由用户确认，
      // 选模型后自动填名称（huoshan-Agent-Plan-模型名）+ 允许列表。
      "huoshan": {
        url: "https://ark.cn-beijing.volces.com/api/plan",
        wire: "anthropic-messages",
        auth: "",                          // 跟随协议默认（x-api-key）
        billing: "count",
        adapter: false,
        modelPrefix: "huoshan-Agent-Plan-",
        modelOptions: [
          "doubao-seed-2.0-lite",
          "doubao-seed-2.0-mini",
          "doubao-seed-2.1-turbo",
          "doubao-seed-evolving",
          "deepseek-v4-flash",
          "deepseek-v4-pro",
          "glm-5.3",
          "kimi-k2.7-code",
          "kimi-k3",
          "minimax-m3",
        ],
        modelNote: (m) => "HuoShan Agent Plan（Anthropic 协议直通，" + m + "）",
        note: "HuoShan Agent Plan（Anthropic 协议直通）",
      },
    };
    const presetSelect = $("create-preset");
    if (presetSelect) {
      presetSelect.addEventListener("change", () => {
        const p = UPSTREAM_PRESETS[presetSelect.value];
        if (!p) {              // "自定义" —— 不动表单，隐藏模型下拉
          const pmf = $("create-preset-model-field");
          if (pmf) pmf.style.display = "none";
          return;
        }
        setWire(p.wire);
        setAuthStyle(p.auth);
        $("create-url").value = p.url;
        $("create-billing-unit").value = p.billing;
        const tfSec = document.querySelector(".cfg-token-fields-create");
        if (tfSec) tfSec.style.display = p.billing === "token" ? "" : "none";
        const adapter = $("create-adapter");
        if (adapter) adapter.checked = p.adapter;
        // note：若当前值是另一个预设填的（或空），跟随切换；用户手填的
        // 值（不等于任何预设 note）不动。
        const noteEl = $("create-note");
        const presetNotes = Object.values(UPSTREAM_PRESETS).map(x => x.note);
        if (!noteEl.value.trim() || presetNotes.includes(noteEl.value.trim())) {
          noteEl.value = p.note;
        }
        // v0.98.4：支持 modelOptions 的预设（opencode-zen-free / huoshan）
        // —— 显示模型下拉（动态填充选项），选模型后自动填名称（按预设
        // modelPrefix）+ 允许列表 chips。
        const pmf = $("create-preset-model-field");
        const modelSel = $("create-preset-model");
        const nameInput = $("create-name");
        if (p.modelOptions && p.modelOptions.length) {
          if (pmf) pmf.style.display = "";
          if (modelSel) {
            modelSel.value = "";
            // 动态重建选项（各预设的模型列表不同）。
            modelSel.innerHTML = "";
            const ph = document.createElement("option");
            ph.value = "";
            ph.textContent = "— 请选择模型 —";
            ph.selected = true;
            modelSel.appendChild(ph);
            for (const m of p.modelOptions) {
              const opt = document.createElement("option");
              opt.value = m;
              opt.textContent = m;
              modelSel.appendChild(opt);
            }
          }
          if (nameInput) nameInput.value = "";
          const chipBox = $("create-chips");
          if (chipBox) {
            chipBox.querySelectorAll(".cfg-chip").forEach(ch => ch.remove());
          }
          if (modelSel) modelSel.focus();
          return;
        }
        if (pmf) pmf.style.display = "none";
        // v0.95：预设可自带允许模型列表（如 deepseek 官方两个模型），
        // 选中后自动填成 chips。清掉上次预设遗留的 chips，输入框保持末尾。
        if (p.models && p.models.length) {
          const chipBox = $("create-chips");
          if (chipBox) {
            chipBox.querySelectorAll(".cfg-chip").forEach(ch => ch.remove());
            const input = chipBox.querySelector("#create-chip-input");
            for (const m of p.models) {
              input?.insertAdjacentHTML("beforebegin", allowedChip(m));
            }
          }
        }
        setStatus("预设已应用：填名称 + API Key 后即可创建", "ok");
        if (nameInput) nameInput.focus();
      });
      // v0.98.2/v0.98.4：支持 modelOptions 的预设的模型下拉 —— 选中模型
      // 后按预设 modelPrefix 自动拼名称 + 允许列表 + note。
      const modelSel = $("create-preset-model");
      if (modelSel) {
        modelSel.addEventListener("change", () => {
          const p = UPSTREAM_PRESETS[presetSelect.value];
          if (!p || !(p.modelOptions && p.modelOptions.length)) return;
          const m = modelSel.value;
          const nameInput = $("create-name");
          if (!m) {
            if (nameInput) nameInput.value = "";
            return;
          }
          if (nameInput) nameInput.value = (p.modelPrefix || "") + m;
          const chipBox = $("create-chips");
          if (chipBox) {
            chipBox.querySelectorAll(".cfg-chip").forEach(ch => ch.remove());
            const input = chipBox.querySelector("#create-chip-input");
            input?.insertAdjacentHTML("beforebegin", allowedChip(m));
          }
          const noteEl = $("create-note");
          const presetNotes = Object.values(UPSTREAM_PRESETS).map(x => x.note);
          if (!noteEl.value.trim() || presetNotes.includes(noteEl.value.trim())) {
            noteEl.value = p.modelNote ? p.modelNote(m) : p.note;
          }
          setStatus("预设已应用：填 API Key 后即可创建", "ok");
        });
      }
    }

    const setStatus = (text, kind) => {
      if (!status) return;
      status.textContent = text || "";
      if (kind) status.setAttribute("data-kind", kind);
      else status.removeAttribute("data-kind");
    };

    const open = () => {
      // Reset the form every time we open, otherwise a half-filled
      // submission sticks around and confuses the next click.
      form.reset();
      // v0.12：预设已移除，reset 后清掉高级功能折叠态 + adapter 勾选。
      const adv = $("create-advanced");
      if (adv) adv.open = false;
      // v0.98.2：重置时隐藏 HuoShan 预设的模型下拉。
      const pmf = $("create-preset-model-field");
      if (pmf) pmf.style.display = "none";
      const adapter = $("create-adapter");
      if (adapter) adapter.checked = false;
      $("create-multipliers").innerHTML = multiplierRow("", "");
      $("create-chips").innerHTML = "";
      // Re-attach the chip input (we just wiped its container above).
      const chipInput = document.createElement("input");
      chipInput.className = "cfg-chip-input";
      chipInput.id = "create-chip-input";
      chipInput.type = "text";
      chipInput.placeholder = "输入模型名后回车";
      $("create-chips").appendChild(chipInput);
      // Reset billing-unit UI
      const tfSection = document.querySelector(".cfg-token-fields-create");
      if (tfSection) tfSection.style.display = "none";
      wireChipsAndMults();
      // Wire billing-unit toggle for this modal (must be after form.reset
      // so the element IDs are present).
      const buSelect = $("create-billing-unit");
      if (buSelect) {
        buSelect.onchange = () => {
          const tfSec = document.querySelector(".cfg-token-fields-create");
          if (tfSec) {
            tfSec.style.display = buSelect.value === "token" ? "" : "none";
          }
        };
      }
      setStatus("");
      // v0.12.1：form.reset 只清 hidden input，按钮高亮要手动清。
      setWire("");
      setAuthStyle("");
      // v0.12.1：测试日志面板每次打开都重置（form.reset 清了输入，
      // 上次的测试日志没意义了）。
      const testPanel = $("create-test-log");
      const testBody = $("create-test-log-body");
      if (testPanel) testPanel.hidden = true;
      if (testBody) testBody.textContent = "";
      const testBtn = $("create-test");
      if (testBtn) { testBtn.disabled = false; testBtn.textContent = "测试"; }
      const connBtn = $("create-connectivity-test");
      if (connBtn) { connBtn.disabled = false; connBtn.textContent = "连通性测试"; }
      _activeProbeBtn = null;
      overlay.hidden = false;
      document.body.classList.add("modal-open");
      // Focus the first field for keyboard users.
      const firstInput = $("create-name");
      if (firstInput) firstInput.focus();
    };

    // v0.12.1：探测结果回填（relayProbeDone 调用 —— 全局作用域）。
    window.setCreateWire = setWire;
    window.setCreateAuthStyle = setAuthStyle;

    // v0.12.1："测试"按钮 —— 用当前表单的 url + api_key 后台探测，
    // 日志经 window.relayProbeLog 实时打印到按钮下方的日志面板。
    const btnTest = $("create-test");
    if (btnTest) {
      btnTest.addEventListener("click", async () => {
        const url = ($("create-url").value || "").trim();
        const key = ($("create-api").value || "").trim();
        if (!url) {
          setStatus("先填 URL 再测试", "error");
          return;
        }
        // v0.95+（P2）：探测请求用表单里的真实模型名，避免占位名被
        // 严格校验模型名的上游 400 拒（与"连通性测试"取法一致）。
        const model = (overlay.querySelector("#create-chips .cfg-chip")?.getAttribute("data-model") || "").trim();
        const panel = $("create-test-log");
        const body = $("create-test-log-body");
        if (panel) panel.hidden = false;
        if (body) body.textContent = "";
        setStatus("测试中…");
        btnTest.disabled = true;
        btnTest.textContent = "测试中…";
        _activeProbeBtn = "create-test";
        const res = await api.testUpstream(url, key, model || null);
        if (res && res.error) {
          if (body) body.textContent = res.error + "\n";
          btnTest.disabled = false;
          btnTest.textContent = "测试";
          _activeProbeBtn = null;
          setStatus(res.error, "error");
        }
      });
    }
    // v0.84："连通性测试"按钮 —— 严格按当前表单配置（协议 / 鉴权 /
    // 模型）构造真实消息发到上游。与"测试"（自动探测判协议）互补。
    const btnConn = $("create-connectivity-test");
    if (btnConn) {
      btnConn.addEventListener("click", async () => {
        const url = ($("create-url").value || "").trim();
        const key = ($("create-api").value || "").trim();
        if (!url) {
          setStatus("先填 URL 再测试", "error");
          return;
        }
        const wire = ($("create-wire").value || "").trim();
        const auth = ($("create-auth-style").value || "").trim();
        // 模型：取"允许的模型"第一个 chip，没有就用空串（后端用占位）。
        const model = (overlay.querySelector("#create-chips .cfg-chip")?.getAttribute("data-model") || "").trim();
        const panel = $("create-test-log");
        const body = $("create-test-log-body");
        if (panel) panel.hidden = false;
        if (body) body.textContent = "";
        setStatus("连通性测试中…");
        btnConn.disabled = true;
        btnConn.textContent = "测试中…";
        _activeProbeBtn = "create-connectivity-test";
        const res = await api.connectivityTest(url, key, wire || null, auth || null, model || null);
        if (res && res.error) {
          if (body) body.textContent = res.error + "\n";
          btnConn.disabled = false;
          btnConn.textContent = "连通性测试";
          _activeProbeBtn = null;
          setStatus(res.error, "error");
        }
      });
    }
    // v0.12.1：日志面板"清空"按钮。
    const btnClearLog = $("create-test-log-clear");
    if (btnClearLog) {
      btnClearLog.addEventListener("click", () => {
        const body = $("create-test-log-body");
        if (body) body.textContent = "";
      });
    }

    const close = () => {
      overlay.hidden = true;
      document.body.classList.remove("modal-open");
    };

    // v0.11.3: 所有 `.js-open-create` 按钮都打开同一个 modal —— 上游页
    // 的 "+ 新建" 和设置页的 "+ 新建上游"。
    document.querySelectorAll(".js-open-create").forEach(btn => {
      btn.addEventListener("click", open);
    });
    if (btnClose) btnClose.addEventListener("click", close);
    if (btnCancel) btnCancel.addEventListener("click", close);
    // Click the dim overlay (outside the card) to close. The existing
    // request-detail modal uses the same pattern.
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    // ESC closes — same affordance as the request-detail modal.
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !overlay.hidden) close();
    });

    /** Hook up the chips + multipliers add/remove for the freshly
     *  opened form. We rebuild these inside open() because the inputs
     *  get wiped every time the form resets. */
    function wireChipsAndMults() {
      const multList = $("create-multipliers");
      const addMult = $("create-add-mult");
      if (addMult && multList) {
        addMult.onclick = () => {
          multList.insertAdjacentHTML("beforeend", multiplierRow("", ""));
          const rows = multList.querySelectorAll(".cfg-mult-name");
          const last = rows[rows.length - 1];
          if (last) last.focus();
          setStatus("");
        };
      }
      if (multList) {
        multList.onclick = (e) => {
          const btn = e.target.closest(".cfg-row-remove");
          if (!btn) return;
          const row = btn.closest(".cfg-multipliers-row");
          if (row) row.remove();
          setStatus("");
        };
      }
      const chipBox = $("create-chips");
      const chipInput = $("create-chip-input");
      if (chipBox && chipInput) {
        chipInput.onkeydown = (e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          const value = chipInput.value.trim();
          if (!value) return;
          const existing = Array.from(chipBox.querySelectorAll(".cfg-chip"))
            .map(el => el.getAttribute("data-model"));
          if (!existing.includes(value)) {
            chipInput.insertAdjacentHTML("beforebegin", allowedChip(value));
          }
          chipInput.value = "";
          setStatus("");
        };
        chipBox.onclick = (e) => {
          const btn = e.target.closest(".cfg-chip-remove");
          if (!btn) return;
          const chip = btn.closest(".cfg-chip");
          if (chip) chip.remove();
          setStatus("");
        };
      }
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submit = $("create-submit");
      // v0.12：单池单 active，无"客户端入口"分组。platform 仅作历史占位
      // （add_upstream 已忽略它），固定传 "anthropic"。
      const platform = "anthropic";
      const name = ($("create-name").value || "").trim();
      const url = ($("create-url").value || "").trim();
      if (!name || !url) {
        setStatus("名称和 URL 必填", "error");
        return;
      }

      // Quota: empty string → null. Non-empty → rounded non-negative int.
      const parseQuota = (id) => {
        const raw = ($(id).value || "").trim();
        if (raw === "") return null;
        const n = Number(raw);
        if (!isFinite(n) || n < 0) return undefined;  // signal invalid
        return Math.round(n);
      };
      const quota_5h = parseQuota("create-quota-5h");
      const quota_week = parseQuota("create-quota-week");
      const quota_month = parseQuota("create-quota-month");
      if (quota_5h === undefined || quota_week === undefined || quota_month === undefined) {
        setStatus("限额必须是非负整数", "error");
        return;
      }

      // Multipliers: skip rows with empty model name. Empty value → 1.
      const multipliers = {};
      let bad = null;
      overlay.querySelectorAll("#create-multipliers .cfg-multipliers-row").forEach(row => {
        const m = (row.querySelector(".cfg-mult-name").value || "").trim();
        const vRaw = (row.querySelector(".cfg-mult-value").value || "").trim();
        if (!m) return;
        const v = vRaw === "" ? 1 : Number(vRaw);
        if (!isFinite(v) || v < 0) { bad = m; return; }
        multipliers[m] = v;
      });
      if (bad) {
        setStatus(`模型 ${bad} 的倍率无效`, "error");
        return;
      }

      const allowed = Array.from(overlay.querySelectorAll("#create-chips .cfg-chip"))
        .map(el => el.getAttribute("data-model"))
        .filter(Boolean);

      const api_val = ($("create-api").value || "").trim();
      const note = ($("create-note").value || "").trim();
      const billing_unit = ($("create-billing-unit").value || "count");
      const token_fields = {};
      if (billing_unit === "token") {
        token_fields["input_tokens"] = $("create-tf-input").checked;
        token_fields["output_tokens"] = $("create-tf-output").checked;
        token_fields["cache_read_input_tokens"] = $("create-tf-cr").checked;
        token_fields["cache_creation_input_tokens"] = $("create-tf-cc").checked;
      }

      const payload = {
        name,
        url,
        api_key: api_val,
        allowed_models: allowed,
        model_multipliers: multipliers,
        quota_5h,
        quota_week,
        quota_month,
        note,
        billing_unit: billing_unit,
        ...(billing_unit === "token" ? { token_fields: token_fields } : {}),
        // v0.12：协议声明（wire）。空串不传（= 自动推断）。endpoint /
        // auth_style 由 wire 默认推导，高级用户可后续在 JSON 里手改。
        ...(($("create-wire").value || "") ? { wire: $("create-wire").value } : {}),
        // v0.12.1：鉴权头显式声明（bearer / x-api-key / none）。
        // 不选 = 跟随协议默认。
        ...(($("create-auth-style").value || "") ? { auth_style: $("create-auth-style").value } : {}),
        // v0.12：opencode-go 协议适配标记（高级功能里的勾选）。
        ...(($("create-adapter") && $("create-adapter").checked) ? {
          requires_anthropic_adapter: true,
          ...(allowed.length === 1 ? { model: allowed[0] } : {}),
        } : {}),
      };

      if (submit) submit.disabled = true;
      setStatus("创建中…");
      const res = await api.createUpstream(platform, payload);
      if (submit) submit.disabled = false;
      if (res && res.ok) {
        setStatus("已创建", "ok");
        // Force the upstream-detail + settings-config cards to re-pull
        // from disk on the next tick, instead of waiting for the sig
        // to drift naturally (which can take several seconds).
        lastUpstreamsSig = null;
        lastConfigSig = null;
        setTimeout(close, 600);
      } else {
        setStatus((res && res.error) || "创建失败", "error");
      }
    });
  }

  // -------------------------------------------------------------------------
  // Sidebar upstream selector (rendered when status is present)
  // -------------------------------------------------------------------------

  // Last-rendered sidebar signature. ``innerHTML`` replacing the whole
  // ``<select>`` at 2 Hz would tear an open dropdown out from under the
  // user twice a second — cache the config+selection signature and only
  // rebuild when it actually changes.
  let lastSidebarSig = null;

  // v0.12.1：单池合并。后端为了兼容旧形状仍返回 {anthropic:[...],
  // openai:[...]}，扁平 upstreams.json 下两段内容相同 —— 按 name 去重
  // 合并成单池 [{plat, cfg}]，前端所有列表（侧栏选择器 / 设置页上游配置
  // / 上游页详情）统一不再按平台分两段。
  // 分组排序：上半 anthropic、下半 openai（按 wire 判，稳定分区保持
  // 组内文件顺序）。
  function wireGroup(cfg, plat) {
    const w = (cfg.wire || "").toLowerCase();
    if (w === "openai-chat" || w === "openai-responses") return "openai";
    if (w === "anthropic-messages") return "anthropic";
    return plat === "openai" ? "openai" : "anthropic";
  }
  function pooledUpstreams(upstreams) {
    const seen = new Set();
    const anth = [];
    const oai = [];
    Object.entries(upstreams || {}).forEach(([plat, list]) => {
      (Array.isArray(list) ? list : []).forEach(c => {
        if (!c || seen.has(c.name)) return;
        seen.add(c.name);
        const item = { plat, cfg: c };
        (wireGroup(c, plat) === "openai" ? oai : anth).push(item);
      });
    });
    return anth.concat(oai);
  }

  function renderSidebar(snap, status) {
    if (!status) return;
    const list = $("sidebar-upstream-list");
    if (!list) return;
    const upstreams = (snap && snap.upstreams) || {};
    const active = status.active_per_platform || {};
    const sig = JSON.stringify({ u: upstreams, a: active });
    if (sig === lastSidebarSig) return;
    // v0.40 性能：重建了 list 内的 bars，quota cache 必须作废。
    _quotaBarsCache = null;
    _quotaBarsOwner = null;
    const items = pooledUpstreams(upstreams);
    // v0.65: 把每个上游按模型拆成多个选项 ——
    //   0 个 allowed_models → 1 项 "api"（不带 model）
    //   1 个 allowed_models → 1 项 "api"（模型隐含,选了之后写入）
    //   N 个 allowed_models → N 项 "api - model_i"
    // 这样就把"切 api"和"选模型"合并到一个下拉里,不需要 modal 弹窗。
    // v0.12.1：按平台分组 —— 上半 anthropic、下半 openai。
    // v0.113e：原生 <select> 的展开列表是 OS 渲染（Windows 白底系统列表框），
    // CSS 无法定制，改成自绘下拉组件（触发按钮 + 玻璃菜单面板），展开后也
    // 贴合整体风格。选项语义不变：value = "plat|name|model"。
    const buildRows = (plat, c) => {
        const activeName = active[plat] || "";
        const allowed = Array.isArray(c.allowed_models) ? c.allowed_models : [];
        const primaryModel = c.model || null;
        // v0.83：active 上游的 model 为 null（未指定，用第一个 allowed）时，
        // 判定 `primaryModel === m` 永远 false → 无项被选中，回退到第一项，
        // 与"上游状态"卡片置顶的 active 脱节。修正：active 且 model 未指定
        // 时默认选中第一个 allowed_models 对应项，回显永远等于卡片 active。
        const isActiveCfg = c.name === activeName;
        const rows = [];
        if (allowed.length === 0) {
          // v0.119 修复：allowed_models 为空时该上游只有「自身」一项，
          // active 即选中 —— 不再要求 model 为空。否则 ollama 这类
          // 配了 model（非空）但没配 allowed_models 的上游，选中态
          // 永远为 false → 回显回退到列表第一项（旧上游）。
          rows.push({ value: plat + "|" + c.name + "|", label: c.name,
                      selected: isActiveCfg });
        } else {
          allowed.forEach((m, i) => {
            rows.push({ value: plat + "|" + c.name + "|" + m,
                        label: c.name + " - " + m,
                        selected: isActiveCfg && (primaryModel === m || (primaryModel == null && i === 0)) });
          });
        }
        return rows;
      };
    const groups = [
      ["anthropic", items.filter(it => wireGroup(it.cfg, it.plat) !== "openai")],
      ["openai", items.filter(it => wireGroup(it.cfg, it.plat) === "openai")],
    ];
    const menuHtml = groups
      .map(([g, its]) => {
        if (!its.length) return "";
        // v0.114：两列并排 —— 每组（anthropic / openai）包成独立列
        // （.upstream-menu-group），CSS grid 把两列排开。
        return `<div class="upstream-menu-group">` +
          `<div class="upstream-menu-group-label">${escape(g)}</div>` +
          its.map(it => buildRows(it.plat, it.cfg).map(r =>
            `<button type="button" class="upstream-menu-item${r.selected ? " active" : ""}"` +
            ` data-upstream-item data-value="${escape(r.value)}">${escape(r.label)}</button>`
          ).join("")).join("") +
          `</div>`;
      })
      .join("");
    // 触发按钮回显当前 active 上游（选中项）；无选中时回退第一条。
    let currentLabel = "";
    groups.forEach(([, its]) => its.forEach(it =>
      buildRows(it.plat, it.cfg).forEach(r => { if (r.selected) currentLabel = r.label; })
    ));
    if (!currentLabel) {
      const flat = [];
      groups.forEach(([, its]) => its.forEach(it => flat.push(...buildRows(it.plat, it.cfg))));
      if (flat.length) currentLabel = flat[0].label;
    }
    // 找到当前选中的 model,拿去给 quota-bar 用 (data-quota-for 还是 api 名,
    // 因为 quota 维度是按 api 算的,不是按 (api, model))
    const selName = active["anthropic"] || active["openai"] || "";
    const html = items.length
      ? `
          <div class="sidebar-upstream-label">上游</div>
          <div class="upstream-select-label">
            <div class="upstream-picker" data-upstream-picker>
              <button type="button" class="upstream-select" data-upstream-trigger title="切换上游">
                <span class="upstream-select-current" data-upstream-current>${escape(currentLabel)}</span>
                <svg class="upstream-select-arrow" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
              <div class="upstream-menu" data-upstream-menu hidden>
                ${menuHtml}
              </div>
            </div>
            <div class="quota-bar quota-bar-micro" data-quota-for="${escape(selName)}" hidden>
              <div class="quota-bar-fill" style="width:0%"></div>
            </div>
          </div>`
      : '<div class="card-empty">未配置上游</div>';
    list.innerHTML = html;
    // v0.12.1：渲染成功才记 sig —— 拼装抛错时不落 sig，下一轮 tick 会
    // 重试，不会把侧栏永久卡在旧内容上。
    lastSidebarSig = sig;
  }

  // v0.113e：自绘上游下拉 —— 全局事件委托，只绑一次。renderSidebar 每次
  // 重渲染都重建 DOM，不能在函数里反复绑监听；这里用 document 级委托兜住
  // 所有实例。行为：点触发按钮开/关；点选项 → 走 applyUpstreamDirect（与
  // 旧 select change 同链路）；点外部 / Esc 关闭。

  // v0.113g：展开/收起动效 —— hidden 只是最终显隐开关，视觉过渡靠 .open
  // class。打开：去 hidden → 强制 reflow 应用初始态（opacity:0/位移）→ 加
  // .open 触发 transition。收起：去 .open → 等过渡结束（180ms）再设 hidden，
  // 避免瞬间消失。快速连点安全：打开会清掉未完成的收起 timer。
  // v0.114：菜单改为 position:fixed 后不再被 sidebar 的 overflow 裁剪，
  // 两列布局可以把面板加宽到 sidebar 之外。打开时用 picker 的视口坐标
  // 定 left / bottom（向上展开，盖在 sidebar 右侧的 main 上方作为浮层）。
  function _upMenuReposition(menu) {
    const picker = menu.closest("[data-upstream-picker]");
    if (!picker) return;
    const rect = picker.getBoundingClientRect();
    menu.style.left = Math.round(rect.left) + "px";
    menu.style.bottom = Math.round(window.innerHeight - rect.top + 6) + "px";
  }
  function _upMenuOpen(menu) {
    if (!menu) return;
    if (menu._upCloseTimer) { clearTimeout(menu._upCloseTimer); menu._upCloseTimer = null; }
    _upMenuReposition(menu);
    menu.hidden = false;
    void menu.offsetWidth;            // 强制 reflow，先让初始态（透明/下移）落地
    // v0.120a：两列等宽 —— 测量 anthropic / openai 两组自然宽，取较窄者为
    // 统一宽度（向短对齐）；原先长的列文字允许溢出边界（overflow 可见）。
    // flex 布局下给两列设相同 width 即等宽；flex:0 0 auto 尊重显式宽度。
    const groups = menu.querySelectorAll(".upstream-menu-group");
    if (groups.length === 2) {
      const w0 = groups[0].getBoundingClientRect().width;
      const w1 = groups[1].getBoundingClientRect().width;
      const target = Math.floor(Math.min(w0, w1));
      if (target >= 40) {
        groups[0].style.width = target + "px";
        groups[1].style.width = target + "px";
      }
    }
    menu.classList.add("open");
  }
  function _upMenuClose(menu) {
    if (!menu || menu.hidden) return;
    if (menu._upCloseTimer) return;   // 已在收起中，幂等
    menu.classList.remove("open");
    menu._upCloseTimer = setTimeout(() => {
      menu._upCloseTimer = null;
      menu.hidden = true;
    }, 180);
  }
  const _upMenuCloseAll = (except) => {
    document.querySelectorAll("[data-upstream-menu].open").forEach(m => {
      if (!except || !m.closest("[data-upstream-picker]").contains(except)) _upMenuClose(m);
    });
  };

  document.addEventListener("click", (e) => {
    const t = e.target;
    const picker = t.closest ? t.closest("[data-upstream-picker]") : null;
    // 先关掉其它所有已打开的下拉（排除本次点击所在的下拉）
    _upMenuCloseAll(picker ? picker.querySelector("[data-upstream-menu]") : null);
    if (!picker) return;
    if (t.closest("[data-upstream-trigger]")) {
      const menu = picker.querySelector("[data-upstream-menu]");
      if (menu.classList.contains("open")) _upMenuClose(menu); else _upMenuOpen(menu);
      return;
    }
    const item = t.closest("[data-upstream-item]");
    if (item) {
      _upMenuClose(picker.querySelector("[data-upstream-menu]"));
      const [plat, name, model] = (item.dataset.value || "").split("|");
      // v0.65/0.93：同旧 select change —— 立刻显示"切换中…"过渡态再走 bridge。
      setText("status-label", "切换中…");
      setText("status-meta", "正在写入新上游…");
      setText("sidebar-status-text", "切换中…");
      setText("sidebar-status-meta", "正在写入…");
      applyUpstreamDirect(plat, name, model || null).catch(err => {
        console.error("applyUpstreamDirect threw:", err);
        alert("切换失败：" + (err && err.message ? err.message : String(err)));
      });
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll("[data-upstream-menu].open").forEach(_upMenuClose);
    }
  });
  // v0.114：fixed 定位依赖视口坐标 —— 窗口 resize 后重算已打开的菜单，
  // 避免菜单还停在旧位置。
  window.addEventListener("resize", () => {
    document.querySelectorAll("[data-upstream-menu].open").forEach(_upMenuReposition);
  });

  // v0.65: 直接调 apply_upstream,不再弹 modal。下拉里所有 (api, model) 组合
  // 都已经展平成单项 option,用户选哪个就把哪个设为 active + model。
  // v0.67：切换后强制重启中继 —— apply_upstream 本身是热切换（不重启
  // 也能生效），但用户明确要求切完必须重启，避免"切了但没跳变/没
  // 真正生效"的观感和残留的旧连接。只在切换成功时触发，失败（ok:false）
  // 不重启，避免把一个好端端在跑的中继因为一次失败的切换请求也炸掉。
  function applyUpstreamDirect(plat, name, model) {
    return api.applyUpstream(plat, name, model).then(res => {
      // v0.93：放宽"成功"判定 —— relay 端 router 返回的 body 没有 "ok" 字段
      // （{"platform": ..., "selected": _public(cfg)}），原版用 res.ok === false
      // 判定失败，但当 res.ok === undefined 时 undefined === false 是 false，
      // 走不到 alert 路径。改为：res 存在且 res.error 缺失 → 视为成功。
      if (!res) {
        console.warn("apply_upstream returned null/undefined");
        return null;
      }
      if (res.error) {
        console.warn("apply_upstream failed:", res);
        alert("切换失败：" + res.error);
        api.refresh();
        return null;
      }
      // 成功路径 —— restartRelay 会改 status-label/sidebar-status-text
      // 显示"重启中…"，这里不要再覆盖。
      return restartRelay().then(() => res);
    });
  }

  // -------------------------------------------------------------------
  // v0.69：sidebar "快捷切换"按钮组。
  //
  // 数据从 snapshot.quick_switch 拿（每条 {label, platform, upstream,
  // model}），由 gui.py 从 upstreams.json 顶层 quick_switch 数组转发。
  // 渲染逻辑：用 snap.upstreams 校验 button 指向的 (platform, upstream)
  // 是否存在、model 是否在 allowed_models 里 —— 不合法就 disabled +
  // tooltip,避免到 select_active 404 才报错。点击按钮直接走 v0.67 的
  // applyUpstreamDirect → restartRelay 链路。
  // -------------------------------------------------------------------

  let lastQuickSwitchSig = null;
  // v0.11.21：内外转换显示 —— topbar 状态条右侧紧凑显示 对内(客户端→
  // 中继)模型 与 对外(中继→上游) 模型/上游名 的映射。
  //
  // 数据：snapshot.live[] 每 500ms 推送，条目带 client_model（对内）、
  // model + upstream（对外，advanced-switch 后的最终目标）。
  // 渲染策略：只画"请求消失后 30 秒内"的条目 —— 每个条目记 ended_at，
  // 超过 30s 清除；还在进行中的（phase=uploading/calling/streaming）
  // 不设 ended_at，一直显示。topbar 空间有限，只显示最新一条。
  const IO_MAP_TTL_MS = 30000;
  const _ioMapEntries = new Map(); // request_id -> {client, outbound, upstream, endedAt}
  let _ioMapSig = null;
  // v0.11.21：菜单栏按钮状态与后端 snapshot 同步 —— 每次 tick 刷新，
  // 保证切页/其他入口改动开关后按钮不会停留在旧状态。
  let _ioMapBtnSig = null;
  function renderIoMapBtn(snap) {
    const btn = $("btn-io-map");
    if (!btn) return;
    const enabled = !!(snap && snap.show_io_map);
    const sig = enabled ? "1" : "0";
    if (sig === _ioMapBtnSig) return;
    _ioMapBtnSig = sig;
    btn.setAttribute("aria-pressed", enabled ? "true" : "false");
    btn.classList.toggle("btn-active", enabled);
    btn.textContent = enabled ? "内外转换：开" : "内外转换";
  }
  // v0.104：顶栏"实时流"按钮 —— 状态 = "面板是否可见"（非 settings），
  // 由 snapshot.live_panel_all_hidden 表达。切页时不会回弹。
  let _livePanelBtnSig = null;
  function renderLivePanelBtn(snap) {
    const btn = $("btn-live-panel");
    if (!btn) return;
    // "可见" = 顶层开关开 && 没有被一键全部隐藏。
    const master = !!(snap && snap.live_panel);
    const allHidden = !!(snap && snap.live_panel_all_hidden);
    const visible = master && !allHidden;
    const sig = visible ? "1" : "0";
    if (sig === _livePanelBtnSig) return;
    _livePanelBtnSig = sig;
    btn.setAttribute("aria-pressed", visible ? "true" : "false");
    btn.classList.toggle("btn-active", visible);
    btn.textContent = visible ? "实时流：开" : "实时流";
  }
  function renderIoMap(snap) {
    const root = $("topbar-io-map");
    if (!root) return;
    const enabled = !!(snap && snap.show_io_map);
    if (!enabled) {
      if (!root.hidden) root.hidden = true;
      _ioMapEntries.clear();
      _ioMapSig = null;
      return;
    }
    root.hidden = false;
    const live = (snap && Array.isArray(snap.live)) ? snap.live : [];
    const now = Date.now();
    // 1. 更新/插入条目：进行中的更新字段并清 endedAt；消失的记 endedAt。
    const seen = new Set();
    for (const r of live) {
      if (!r || !r.request_id) continue;
      seen.add(r.request_id);
      const phase = (r.phase || "").toLowerCase();
      const alive = phase === "uploading" || phase === "calling" || phase === "streaming";
      let e = _ioMapEntries.get(r.request_id);
      if (!e) {
        e = { client: "", outbound: "", upstream: "", endedAt: null };
        _ioMapEntries.set(r.request_id, e);
      }
      e.client = r.client_model || r.model || e.client;
      e.outbound = r.model || e.outbound;
      e.upstream = r.upstream || e.upstream;
      if (alive) {
        e.endedAt = null;
      } else if (e.endedAt === null) {
        e.endedAt = now;
      }
    }
    // 2. 清理：请求消失（不在 live 里）且超过 30s 的条目删除；
    //    请求消失但未到 30s 的补记 endedAt。
    for (const [rid, e] of _ioMapEntries) {
      if (seen.has(rid)) continue;
      if (e.endedAt === null) e.endedAt = now;
      else if (now - e.endedAt >= IO_MAP_TTL_MS) _ioMapEntries.delete(rid);
    }
    // 3. topbar 只显示最新一条（按 startedAt 倒序取第一个存活的）。
    const items = Array.from(_ioMapEntries.values());
    const sig = JSON.stringify(items);
    if (sig === _ioMapSig) return;
    _ioMapSig = sig;
    if (!items.length) {
      root.hidden = true;
      root.innerHTML = "";
      return;
    }
    const e = items[0];
    root.innerHTML =
      `<span class="io-map-chip" title="对内：客户端→中继">${escape(e.client || "—")}</span>` +
      `<span class="io-map-arrow">→</span>` +
      `<span class="io-map-chip io-map-chip-out" title="对外：中继→上游 (${escape(e.upstream || "")})">${escape(e.outbound || "—")}</span>` +
      `<span class="io-map-time" title="${e.endedAt ? "请求结束，保留 30s" : "请求进行中"}">${e.endedAt ? `${Math.max(0, Math.round((e.endedAt - now) / 1000))}s` : "实时"}</span>`;
  }

  function renderQuickSwitch(snap) {
    const root = $("sidebar-quick-switch");
    const list = $("sidebar-quick-switch-list");
    if (!root || !list) return;
    const qs = (snap && Array.isArray(snap.quick_switch)) ? snap.quick_switch : [];
    // 内容空 → 整块隐藏,占位都省掉,跟没配置完全一致
    const sig = JSON.stringify(qs);
    if (sig === lastQuickSwitchSig) return;
    lastQuickSwitchSig = sig;
    if (!qs.length) {
      root.hidden = true;
      list.innerHTML = "";
      return;
    }
    root.hidden = false;
    const upstreams = (snap && snap.upstreams) || {};
    list.innerHTML = qs.map(item => {
      const platformCfg = upstreams[item.platform] || [];
      const ups = platformCfg.find(c => c.name === item.upstream);
      const upstreamExists = !!ups;
      const allowed = ups && Array.isArray(ups.allowed_models) ? ups.allowed_models : [];
      const modelInAllow = upstreamExists && (allowed.length === 0 || allowed.includes(item.model));
      const valid = upstreamExists && modelInAllow;
      const cls = "quick-switch-btn" + (valid ? "" : " quick-switch-btn-disabled");
      let tip = "";
      if (!upstreamExists) {
        tip = `找不到 ${item.platform}/${item.upstream}`;
      } else if (!modelInAllow) {
        tip = `${item.model} 不在 ${item.upstream} 的 allowed_models 里`;
      }
      return `<button type="button" class="${cls}"${tip ? ` title="${escape(tip)}"` : ""} \
              data-qs-platform="${escape(item.platform)}" \
              data-qs-upstream="${escape(item.upstream)}" \
              data-qs-model="${escape(item.model)}">${escape(item.label)}</button>`;
    }).join("");
  }

  // 一次性 wire：click 委托挂在 list 上,button 每次重渲染 innerHTML 替换,
  // 但容器 list 不动,所以委托不丢。
  function wireQuickSwitchClicks() {
    const list = $("sidebar-quick-switch-list");
    if (!list || list.dataset.wired) return;
    list.dataset.wired = "1";
    list.addEventListener("click", (e) => {
      const btn = e.target.closest(".quick-switch-btn");
      if (!btn) return;
      // disabled 按钮虽然有 pointer-events:none,但键盘 / 程序化触发还能
      // 落到这里 —— 显式拦一下。
      if (btn.classList.contains("quick-switch-btn-disabled")) return;
      const plat  = btn.dataset.qsPlatform;
      const name  = btn.dataset.qsUpstream;
      const model = btn.dataset.qsModel;
      if (!plat || !name || !model) return;
      applyUpstreamDirect(plat, name, model);
    });
  }

  /** 侧栏"实时"项的左→右波纹开关。
   *
   *  任何 calling/streaming 的请求都点亮波纹,完成后(下一帧)自然熄灭。
   *  与 renderLive() 的过滤语义保持一致 —— "实时"页里那一列是什么,
   *  侧栏这里就当什么。upload/done 不算"实时在飞",不算亮。
   *
   *  DOM 查询缓存到模块首次调用时,后续每次只 toggle class,
   *  tick 50ms 一次也几乎不耗。
   *  v0.40 性能：缓存上次结果，只有状态变化时才写 DOM。
   */
  let liveNavItem = null;
  let lastLiveNavStreaming = null;
  function updateLiveNavRipple(snap) {
    if (!liveNavItem) {
      liveNavItem = document.querySelector(".nav-item[data-view=\"live\"]");
      if (!liveNavItem) return;
    }
    const rows = (snap && snap.live) || [];
    let hasStream = false;
    for (const r of rows) {
      const phase = (r.phase || "").toLowerCase();
      if (phase === "calling") { hasStream = true; break; }
      // 与 renderLive 同款孤儿过滤：streaming 但 idle 超过 STREAMING_STALE_SEC
      // 的条目实时卡不显示，波纹也必须跟着熄 —— 否则"无请求但波纹在跑"。
      if (phase === "streaming" && Number(r.age_sec || 0) < STREAMING_STALE_SEC) { hasStream = true; break; }
    }
    // v0.11.3: 闪烁偏好关掉时熄灭侧栏"实时"波纹。
    hasStream = prefs.flash && hasStream;
    if (hasStream === lastLiveNavStreaming) return;
    lastLiveNavStreaming = hasStream;
    liveNavItem.classList.toggle("has-stream", hasStream);
  }

  /** Update the sidebar micro-bars in place.
   *
   *  Separate from ``renderSidebar`` on purpose: the sidebar rebuild is
   *  gated on a config signature (so an open <select> dropdown doesn't
   *  get torn out from under the user), but utilization changes every
   *  tick. Mutating width/data-level on the existing nodes gives live
   *  bars without touching the <select> at all.
   *
   *  v0.40 性能：缓存 bars 列表（只在 renderSidebar 重建时重新查找），
   *  避免每次 tick 跑一次 querySelectorAll。同时 per-bar 缓存 pct/level，
   *  即使 utilization 没变也不触发 style 写入。
   */
  let _quotaBarsCache = null;
  let _quotaBarsOwner = null;
  function getQuotaBars(list) {
    if (_quotaBarsOwner === list && _quotaBarsCache) return _quotaBarsCache;
    _quotaBarsCache = list.querySelectorAll("[data-quota-for]");
    _quotaBarsOwner = list;
    return _quotaBarsCache;
  }
  function renderSidebarQuota(snap) {
    const list = $("sidebar-upstream-list");
    if (!list) return;
    const byUp = (snap && snap.by_upstream) || {};
    const bars = getQuotaBars(list);
    for (const bar of bars) {
      const data = byUp[bar.getAttribute("data-quota-for")];
      const level = data && data.warning_level;
      if (!data || !level || level === "no_quota") {
        if (!bar.hidden) bar.hidden = true;
        continue;
      }
      const pct = Math.min(100, Math.max(0, Number(data.utilization_5h || 0) * 100));
      if (bar.hidden) bar.hidden = false;
      const fill = bar.querySelector(".quota-bar-fill");
      if (!fill) continue;
      // Only write to style/attribute when the value really changed.
      // The browser dedupes identical writes but the getter/skip path
      // is still cheaper than the setter+PASS.
      if (fill._lastPct !== pct) {
        fill.style.width = `${pct.toFixed(1)}%`;
        fill._lastPct = pct;
      }
      if (fill.getAttribute("data-level") !== level) {
        fill.setAttribute("data-level", level);
      }
    }
  }

  // -------------------------------------------------------------------------
  // Polling
  // -------------------------------------------------------------------------

  let lastSnap = null;
  let lastStatus = null;
  // v0.155：平台别名映射（原始工具名 → 显示名）。启动时从桥读一次，
  // 保存后由 saveAgentAliases 流程刷新。纯展示层，后端聚合仍返原始名。
  let agentAliases = null;
  async function loadAgentAliases() {
    try {
      const m = await api.getAgentAliases();
      if (m && typeof m === "object") agentAliases = m;
    } catch (e) {
      console.error("[agent] loadAgentAliases failed", e);
    }
  }
  // v0.157：UA 归类规则（整串 UA → 平台名）。中继进程消费，GUI 侧缓存，
  // 保存后由 saveUaRules 流程刷新。
  let uaRules = null;
  async function loadUaRules() {
    try {
      const m = await api.getUaRules();
      if (m && typeof m === "object") uaRules = m;
    } catch (e) {
      console.error("[ua] loadUaRules failed", e);
    }
  }

  // v0.47：交互期间暂停轮询。
  //
  // 拖拽 / 点击窗口时 GPU 在忙（frameless 拖窗 + DOM hover 高亮），
  // 这时候 500ms 一次的 innerHTML rebuild 抢同一组渲染线程，会出现
  // 明显的"卡一下"：鼠标光标抖、窗口边缘闪。
  //
  // 信号：pointerdown = 进入交互态；pointerup / pointercancel /
  // mouseleave = 退出，再加 250 ms 冷却（避免按钮 click 抬手瞬间
  // 又触发一次 tick 把按钮的 :active 高亮抹掉）。
  let pollPaused = false;
  let pollResumeTimer = null;
  function pausePolling() {
    if (pollResumeTimer) { clearTimeout(pollResumeTimer); pollResumeTimer = null; }
    pollPaused = true;
  }
  function scheduleResumePolling() {
    if (pollResumeTimer) clearTimeout(pollResumeTimer);
    pollResumeTimer = setTimeout(() => {
      pollPaused = false;
      pollResumeTimer = null;
    }, 250);
  }

  // v0.12：分发告警（混合态 / 未知 key）—— 后端每次快照带增量告警，
  // 这里渲染成右上角可关闭卡片。配置类错误不自动消失（用户要改配置），
  // 点 × 关闭；同 id 不重复弹。
  const seenAlertIds = new Set();
  function renderDispatchAlerts(snap) {
    const items = (snap && Array.isArray(snap.alerts)) ? snap.alerts : [];
    if (!items.length) return;
    let box = document.getElementById("dispatch-alerts");
    if (!box) {
      box = document.createElement("div");
      box.id = "dispatch-alerts";
      document.body.appendChild(box);
    }
    for (const a of items) {
      if (!a || seenAlertIds.has(a.id)) continue;
      seenAlertIds.add(a.id);
      const isMixed = a.kind === "mixed_auto";
      const card = document.createElement("div");
      card.className = "dispatch-alert" + (isMixed ? " dispatch-alert-mixed" : " dispatch-alert-unknown");
      const title = document.createElement("div");
      title.className = "dispatch-alert-title";
      title.textContent = isMixed ? "客户端配置错误：auto 不能单独出现" : "透传请求：未找到该配置组";
      const detail = document.createElement("div");
      detail.className = "dispatch-alert-detail";
      detail.textContent =
        (isMixed
          ? "api-key 与 model 必须同时为 auto（中继转发）或同时为真实值（透传）。"
          : "收到透传请求，但该 key 未匹配到任何上游，请在中继里新建对应配置组。") +
        " 平台 " + (a.platform || "?") +
        "｜key " + (a.key || "?") +
        (a.model ? "｜模型 " + a.model : "");
      const close = document.createElement("button");
      close.className = "dispatch-alert-close";
      close.textContent = "×";
      close.addEventListener("click", () => card.remove());
      card.appendChild(title);
      card.appendChild(detail);
      card.appendChild(close);
      box.appendChild(card);
    }
  }

  async function tick() {
    if (pollPaused) return;
    try {
      const [snap, status] = await Promise.all([api.snapshot(), api.status()]);
      if (snap) lastSnap = snap;
      if (status) lastStatus = status;
      renderAll(lastSnap, lastStatus);
      renderSidebar(lastSnap, lastStatus);
      renderSidebarQuota(lastSnap);
      renderQuickSwitch(lastSnap);
      renderIoMapBtn(lastSnap);
      renderIoMap(lastSnap);
      renderLivePanelBtn(lastSnap);
      renderDispatchAlerts(lastSnap);
      renderErrorHints(lastSnap);
      updateLiveNavRipple(lastSnap);
      // v0.113r：渲染完成后统一补一遍语言 —— renderStatus/renderIoMapBtn/
      // renderLivePanelBtn 等每 tick 用 setText 覆盖中文文案，这里靠
      // applyLang 的「内容变了就重新捕获」逻辑把新内容再翻一遍，保证动态
      // 文案不会停留在中文（500ms 一次，匹配元素 ~200 个，开销可忽略）。
      applyLang();
    } catch (e) {
      console.error("[poll] tick failed", e);
    }
  }

  // -------------------------------------------------------------------------
  // Theme toggle
  // -------------------------------------------------------------------------

  window.setTheme = function setTheme(name) {
    document.documentElement.setAttribute("data-theme", name || "light");
    // v0.41：背景已切回纯 CSS 静态方案，主题切替不需要通知 canvas。
    // v0.9：但 24h 图表在 JS 里按 data-theme 定配色，切主题后必须让
    // 下一个 renderHourChart 重建 —— 否则 idle 时 sig 不变、图表保持
    // 旧主题颜色。
    _hourChartTheme = null;
    if (currentView === "overview" && lastSnap) renderAll(lastSnap, lastStatus);
  };

  // v0.89：实时流侧栏按钮状态同步入口 —— Python 侧（_on_panel_closing /
  // set_live_panel）调 evaluate_js("syncSidePanelToggle(false)") 把按钮
  // 状态切到一致；JS 侧（btn-live-panel / prefs-live-panel）切了之后也
  // 调这个让两边按钮同步。
  // v0.104：按钮语义改成"一键全部显示/隐藏"，所以"settings enabled
  // && 不在 all_hidden 状态"才显示为「实时流：开」。设置页 checkbox
  // 仍按 enabled 直接刷（顶栏语义细节 vs 设置项真值分开处理）。
  window.syncSidePanelToggle = function syncSidePanelToggle(enabled) {
    const settingsItem = document.querySelector(".prefs-live-panel");
    if (settingsItem) settingsItem.checked = !!enabled;
    // 顶栏按钮交由 renderLivePanelBtn 在下个 snapshot tick 统一刷新。
    _livePanelBtnSig = null;
  };

  // v0.174：syncLivePanelMgmtGroup 退役 —— 主开关「实时流侧栏」已挪到
  // 「实时栏管理」组最顶，组本身默认 visible。函数保留为空定义，所有调
  // 用点不变（兼容历史 wire 顺序），5 调用点之后清理。
  window.syncLivePanelMgmtGroup = function syncLivePanelMgmtGroup() { /* no-op since v0.174 */ };

  // v0.176：主开关折叠「实时栏管理」组全部子项（#live-panel-mgmt-sub）；
  // 「悬浮球置顶」额外看悬浮球开关。读 DOM 当前状态，任意时刻可调。
  window.syncLivePanelSub = function syncLivePanelSub() {
    const lp = document.querySelector(".prefs-live-panel");
    const fb = document.querySelector(".prefs-live-panel-float-ball");
    const sub = document.querySelector("#live-panel-mgmt-sub");
    const topmost = document.querySelector("#prefs-live-panel-float-ball-topmost-item");
    const on = !!(lp && lp.checked);
    if (sub) sub.hidden = !on;
    if (topmost && fb) topmost.hidden = !(on && fb.checked);
  };

  // v0.12.1：新建上游"测试"按钮的实时日志。Python 后台线程经
  // evaluate_js 逐行推来（页面刷新/窗口关闭时这些调用自然消失）。
  window.relayProbeLog = function relayProbeLog(line) {
    const body = document.getElementById("create-test-log-body");
    if (!body) return;
    const t = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    body.textContent += `[${t}] ${line}\n`;
    body.scrollTop = body.scrollHeight;
  };

  // v0.84：当前在跑的测试类型。probe = 自动探测（测试按钮），
  // connectivity = 连通性测试按钮。relayProbeDone 据此恢复对应按钮。
  let _activeProbeBtn = null;

  window.relayProbeDone = function relayProbeDone(result) {
    if (_activeProbeBtn) {
      const btn = document.getElementById(_activeProbeBtn);
      if (btn) {
        btn.disabled = false;
        btn.textContent = _activeProbeBtn === "create-connectivity-test" ? "连通性测试" : "测试";
      }
      _activeProbeBtn = null;
    }
    const status = document.getElementById("create-status");
    const setStatus = (text, kind) => {
      if (!status) return;
      status.textContent = text || "";
      if (kind) status.setAttribute("data-kind", kind);
      else status.removeAttribute("data-kind");
    };
    if (!result) return;
    if (result.error) {
      window.relayProbeLog(`✗ ${result.error}`);
      setStatus(result.error, "error");
      return;
    }
    // v0.84/v0.85：连通性测试的结果（ok / status_code / reply）。
    if (result.ok !== undefined && result.status_code !== undefined) {
      if (result.ok) {
        window.relayProbeLog("✓ 连通性测试通过：收到模型回复");
        setStatus("连通正常", "ok");
        if (result.reply) {
          window.relayProbeLog(`回复内容：${result.reply}`);
        }
      } else {
        window.relayProbeLog(`✗ 连通性测试失败（HTTP ${result.status_code ?? "ERR"}）`);
        setStatus(`HTTP ${result.status_code ?? "ERR"}`, "error");
      }
      // v0.86：末尾展示本次发出的完整原始请求文本（evidence 最后一条
      // 由后端拼接，含方法/URL/头/JSON body）。
      const ev = result.evidence;
      if (Array.isArray(ev) && ev.length) {
        const raw = ev[ev.length - 1];
        if (raw && raw.includes("原始请求")) {
          window.relayProbeLog("\n" + raw);
        }
      }
      return;
    }
    if (result.key_valid) {
      window.relayProbeLog("✓ 探测完成：上游可用");
      setStatus("上游可用", "ok");
    } else {
      window.relayProbeLog("✓ 探测完成：端点可达，但 key 无效");
      setStatus("key 无效", "error");
    }
    if (result.wire && window.setCreateWire) {
      window.setCreateWire(result.wire);
      window.relayProbeLog(`已应用协议：${result.wire}`);
    }
    if (result.auth_style && window.setCreateAuthStyle) {
      window.setCreateAuthStyle(result.auth_style);
      window.relayProbeLog(`已应用鉴权头：${result.auth_style}`);
    }
    if (result.models && result.models.length) {
      window.relayProbeLog(`模型：${result.models.slice(0, 12).join(", ")}`);
    }
  };

  // -------------------------------------------------------------------------
  // Button wiring
  // -------------------------------------------------------------------------

// -------------------------------------------------------------------------
    function wireButtons() {
    const btnToggle = $("btn-toggle");
    if (btnToggle) btnToggle.addEventListener("click", async () => {
      // Guard here too, not just via `disabled` — a stale lastStatus or
      // a programmatic click must not reach stop() on an external relay.
      if (lastStatus && lastStatus.owner === "external") return;
      const res = (lastStatus && lastStatus.running)
        ? await api.stop()
        : await api.start();
      if (res && res.error) alert(res.error);
    });
    const btnRestart = $("btn-restart");
    if (btnRestart) btnRestart.addEventListener("click", async () => {
      // v0.62：去掉 owner==="external" 的早退 —— 按"重启"就是想要它
      // 死。Python 侧 ``restart_server`` 会 netstat 找占 8088 的 PID，
      // taskkill /F /T，再 spawn 我们自己的子进程接管端口。中继的
      // 进行中请求会被一刀切（这正是用户要的语义），下一次请求
      // 走新进程。toggle 按钮（启/停）仍然走原来的 external 拒绝，
      // 那是另一个语义。
      // v0.67：重启按钮改走 restartRelay() 统一入口 —— 除了原来的
      // "重启后强制 refresh + 立刻拉 status" 之外，现在还会在过渡期把
      // 状态灯涂成橙色脉冲、文案改成"重启中…"，让 kill 旧进程到新
      // PID 就绪这段秒级窗口在 GUI 上可见，而不是全程停在绿灯上。
      await restartRelay();
    });
    const btnTheme = $("btn-theme");
    if (btnTheme) btnTheme.addEventListener("click", async () => {
      const newName = await api.toggleTheme();
      if (newName) window.setTheme(newName);
    });
    // v0.11.21：菜单栏"内外转换"开关 —— 切换后端持久化开关，并立即
    // 反映到侧栏映射区。状态由 tick 里 renderIoMapBtn 从 snapshot
    // 同步（后端为准，切页不会回弹）。
    const btnIoMap = $("btn-io-map");
    if (btnIoMap) btnIoMap.addEventListener("click", async () => {
      const next = btnIoMap.getAttribute("aria-pressed") !== "true";
      const res = await api.setShowIoMap(next);
      const ok = !!(res && res.enabled);
      btnIoMap.setAttribute("aria-pressed", ok ? "true" : "false");
      btnIoMap.classList.toggle("btn-active", ok);
      btnIoMap.textContent = ok ? "内外转换：开" : "内外转换";
    });
    // v0.104：顶栏"实时流"快捷图标 —— 新语义 = 一键全部显示/隐藏所有
    // 实时栏。不动 settings 状态。aria-pressed 与按钮文案按"当前是否可
    // 见"同步（renderLivePanelBtn 也会按 snapshot.live_panel 兜底同步）。
    const btnLivePanel = $("btn-live-panel");
    if (btnLivePanel) btnLivePanel.addEventListener("click", async () => {
      const res = await api.toggleAllPanels();
      const visible = !!(res && res.visible);
      btnLivePanel.setAttribute("aria-pressed", visible ? "true" : "false");
      btnLivePanel.classList.toggle("btn-active", visible);
      btnLivePanel.textContent = visible ? "实时流：开" : "实时流";
      // 同步设置页「外观」组的实时流侧栏开关 + 实时栏管理组显隐
      const cb = document.querySelector(".prefs-live-panel");
      if (cb && typeof cb.checked === "boolean") cb.checked = visible;
      syncLivePanelMgmtGroup(visible);
    });
    // v0.103：顶栏「关闭顶部调试栏」—— 复用设置页 prefs.hideStatusBar。
    // 点击后立刻写 localStorage + 应用 body class（applyPrefsClass()），并
    // 同步设置页那个 checkbox（如果用户后切到设置页，状态正确）。
    // v0.113y：关闭语义升级 —— 隐藏整条顶栏（不只是 .topbar-status），按
    // 钮本身也跟着藏，所以「重新打开」只能走设置页开关。
    const btnHideStatusBar = $("btn-hide-status-bar");
    function syncHideStatusBarBtn() {
      if (!btnHideStatusBar) return;
      const on = !!prefs.hideStatusBar;
      btnHideStatusBar.setAttribute("aria-pressed", on ? "true" : "false");
      btnHideStatusBar.classList.toggle("btn-active", on);
      btnHideStatusBar.textContent = on ? "显示顶部调试栏" : "关闭顶部调试栏";
    }
    if (btnHideStatusBar) btnHideStatusBar.addEventListener("click", () => {
      prefs.hideStatusBar = !prefs.hideStatusBar;
      savePrefs();
      syncHideStatusBarBtn();
      // 同步设置页那个 checkbox（即使当前不在设置页，DOM 切换瞬间
      // 也会显示正确；如果用户在设置页才生效，下次切到设置页 render
      // 走的是 prefs.hideStatusBar，也会正确）。
      const cb = document.querySelector(".prefs-hide-status");
      if (cb && cb.checked !== prefs.hideStatusBar) cb.checked = prefs.hideStatusBar;
    });
    // 初次进入时按当前 prefs 同步文案。
    syncHideStatusBarBtn();
    // 卡片尺寸拖拽手柄（右下角锚点 + 列跨度把手）。事件委托挂在
    // document 上，动态增删卡都生效。必须在 render 循环之前调用，
    // 让 resize 态能卡住每 500ms 的 tick 重渲染。
    wireCardResize();
    // v0.11.4: 开发用"刷新GUI"—— 重新加载页面。桥不可用时静默跳过。
    const btnReloadGui = $("btn-reload-gui");
    if (btnReloadGui) btnReloadGui.addEventListener("click", () => api.reloadGui());
    // Window controls (frameless mode). Bridge calls only — when the
    // bridge is missing (browser-direct open) the buttons silently do
    // nothing, same as the rest of the chrome.
    const btnWinMin = $("btn-win-min");
    if (btnWinMin) btnWinMin.addEventListener("click", () => api.windowMinimize());
    const btnWinMax = $("btn-win-max");
    if (btnWinMax) btnWinMax.addEventListener("click", async () => {
      const isMax = await api.windowToggleMaximize() === true;
      btnWinMax.classList.toggle("titlebar-btn-is-maxed", isMax);
      // 镜像到 body 类，wireEdgeResize 用这个判断"最大化时不要拽"。
      document.body.classList.toggle("is-maximized", isMax);
      btnWinMax.title = isMax ? "还原" : "最大化";
    });
    const btnWinClose = $("btn-win-close");
    if (btnWinClose) btnWinClose.addEventListener("click", () => api.windowClose());
    // Sidebar nav items — switch to the requested view; setView handles
    // visibility + active-class + immediate re-render of the target view.
    document.querySelectorAll(".nav-item").forEach(item => {
      item.addEventListener("click", () => setView(item.dataset.view));
    });
    // v0.68：概览页"上游状态"卡片（card-upstream-body）和设置页自己的
    // "上游"详情列表（card-upstreams-detail-body）曾经共用同一个
    // focusUpstreamConfig 跳转逻辑。用户明确要求"上游状态"点击不再跳
    // 设置，改成弹按模型拆分的小窗——但设置页自己的列表本来就在设置
    // 页里，点它跳去编辑卡片仍然合理，所以这里拆成两个独立委托，只
    // 改 card-upstream-body 那一个。
    const upstreamStatusBox = $("card-upstream-body");
    if (upstreamStatusBox) {
      upstreamStatusBox.addEventListener("click", (e) => {
        const row = e.target.closest("[data-upstream]");
        if (!row) return;
        openUpstreamModels(row.dataset.upstream);
      });
    }
    const upstreamDetailBox = $("card-upstreams-detail-body");
    if (upstreamDetailBox) {
      upstreamDetailBox.addEventListener("click", (e) => {
        // v0.64：上游卡片右上角的删除 × 按钮。捕获在 row-click 委
        // 托之前，stopImmediatePropagation 阻止向上冒泡到
        // [data-upstream] 委托（否则点 × 会同时触发 focusUpstreamConfig
        // 跳转到设置面板）。active 行不渲染按钮，所以这里不存在的
        // 情况等于禁删。
        const xBtn = e.target.closest(".upstream-detail-remove");
        if (xBtn) {
          e.stopImmediatePropagation();
          e.preventDefault();
          const plat = xBtn.dataset.platform;
          const name = xBtn.dataset.upstream;
          // 二次确认 —— 删整条 upstream 是不可逆操作（API key
          // 也跟着没了），浏览器原生 confirm() 足够，比自己写
          // modal 轻得多。
          if (!confirm(`确定删除上游 ${plat} / ${name} 吗？\n这条配置（包含 API key）会被永久删除。`)) {
            return;
          }
          // 乐观更新：先把 row 从 DOM 里摘掉，bridge 异步删除
          // 后下一次 snapshot 自然吻合。
          const row = xBtn.closest("[data-upstream]");
          if (row) row.remove();
          api.removeUpstream(plat, name).then(res => {
            if (res && res.ok) {
              // 强制 rebuild，让 sidebar / by_upstream 都立刻反映
              // 新状态（active 切换 / 行消失）。
              api.refresh();
            } else {
              console.warn("remove_upstream failed:", res && res.error);
              // 失败时让下一次 poll 重建回来 —— sig 不变说明配置
              // 没真改，原样渲染回 row。
            }
          });
          return;
        }
        const row = e.target.closest("[data-upstream]");
        if (!row) return;
        // #3：单击行为 —— 未激活的行先激活该上游（支持多个模型时取第一个），
        // 已激活的行才跳去设置编辑。与侧栏"active 且未指定 model 时默认选
        // 第一个 allowed_models"的回显逻辑一致（renderSidebar buildRows）。
        if (row.classList.contains("upstream-detail-row-active")) {
          focusUpstreamConfig(row.dataset.platform, row.dataset.upstream);
          return;
        }
        const cfg = (lastSnap && lastSnap.upstreams) || {};
        const plat = row.dataset.platform;
        const name = row.dataset.upstream;
        let firstModel = null;
        for (const list of Object.values(cfg)) {
          const c = (list || []).find(x => x.name === name);
          if (c) {
            const allowed = Array.isArray(c.allowed_models) ? c.allowed_models : [];
            firstModel = allowed.length ? allowed[0] : (c.model || null);
            break;
          }
        }
        applyUpstreamDirect(plat, name, firstModel);
      });
    }
    // v0.46：新建上游入口。复用 wireUpstreamConfig 的 chips / 倍率
    // helpers（allowedChip / multiplierRow），把表单组装出来，
    // 提交时打 api.createUpstream。
    wireUpstreamCreate();
    // v0.102：三极消耗口径开关（总览 + 设置页）与总览卡片管理。
    mountConsumeSwitches();
    ensureOverviewCards();
    wireCardsManage();
    // v0.47：拖拽 / 点击期间暂停轮询，避开 innerHTML rebuild 跟
    // 拖拽事件抢 GPU。pointerup/cancel + mouseleave 兜底：frameless
    // 拖窗时宿主进程会截走鼠标，pointerup 可能根本不进 WebView。
    window.addEventListener("pointerdown", pausePolling, true);
    window.addEventListener("pointerup", scheduleResumePolling, true);
    window.addEventListener("pointercancel", scheduleResumePolling, true);
    window.addEventListener("mouseleave", scheduleResumePolling, true);
    // 卡片尺寸拖拽：pointer 捕获期内（含 easy_drag 抢窗的窗口拖拽）
    // 同步处理卡片 resize 状态 + 取消拖拽排序的 ghost。
    // 走 mousedown/mousemove/mouseup 而不是 pointer 事件：
    // HTML5 drag 由 mousedown + mousemove 触发，drag 启动判定看的是
    // mousedown.defaultPrevented。pointer 事件是更高级抽象，但 Chromium
    // 在 preventDefault 跨 pointer↔mouse 的同步上不一致，可能漏拦
    // dragstart → grid 的 dragstart listener 抢走整卡拖动。
    window.addEventListener("mousedown", cardResizePointerDown, true);
    window.addEventListener("mousemove", cardResizePointerMove, true);
    window.addEventListener("mouseup", cardResizePointerUp, true);
    window.addEventListener("mouseleave", cardResizePointerUp, true);
    // 失焦兜底：alt-tab 切走可能丢 mouseup。
    window.addEventListener("blur", cardResizePointerUp, true);
    // 拖拽过程里可能有零星 mousemove 但 pointer 没动，500 ms 没
    // 任何新的 pointer 事件就当作结束。
    window.addEventListener("blur", scheduleResumePolling, true);
    // v0.186：自由模式卡拖动 —— 跟 resize 同套 window capture 模式。
    // capture 相位拦截 mousedown 后立即 stopImmediatePropagation 阻断
    // grid 的 HTML5 dragstart 监听器，避免整卡重排抢走自由拖动。
    // mouseleave + blur 同样挂上，alt-tab 切走时兜底收尾。
    window.addEventListener("mousedown", cardFreeDragPointerDown, true);
    window.addEventListener("mousemove", cardFreeDragPointerMove, true);
    window.addEventListener("mouseup", cardFreeDragPointerUp, true);
    window.addEventListener("mouseleave", cardFreeDragPointerUp, true);
    window.addEventListener("blur", cardFreeDragPointerUp, true);
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  // v0.60：总览页 6 张卡可拖拽重排，顺序存 localStorage。
  //
  // 实现要点：
  //   - HTML5 native drag-and-drop（draggable=true 已写在 index.html）
  //   - dragstart 把 data-card id 塞进 dataTransfer，setTimeout(0) 才
  //     加 .dragging —— 否则 ghost 会带上 opacity 0.35。
  //   - dragover 里按"最近卡片中心"算法算 drop target，目标是 .glass-
  //     card 上加 .drop-target（dashed outline）。
  //   - drop 时 insertBefore 到 target 前面，重排后调 saveCardOrder
  //     写 localStorage。
  //   - 跟现有 click 委托无冲突：glass-card 本身没 click handler，点
  //     upstream-row 走 card-upstream-body 的 [data-upstream] 委托；
  //     点"最近活动"行走 wireOverviewRecentClicks 的委托。拖起不放
  //     不会触发 click。
  //   - pollPaused 已经在 pointerdown 上挂好，拖拽期间 GPU 不会被
  //     innerHTML rebuild 抢。
  const CARD_ORDER_KEY = "overview-card-order-v1";

  // -------------------------------------------------------------------------
  // v0.11.3: 上游状态卡片 —— API 行拖拽排序
  // 在 #card-upstream-body 内拖 .upstream-row 调整顺序，顺序存
  // localStorage，renderUpstream 每次重建后 applyUpstreamRowOrder 重新套用。
  // 整卡排序仍走 wireCardDragDrop —— 但只从卡片标题 / 边缘（非 body 区）
  // 拖起，body 里的拖拽一律归行排序。
  // -------------------------------------------------------------------------

  const UPSTREAM_ROW_ORDER_KEY = "upstream-row-order-v1";

  function saveUpstreamRowOrder(body) {
    const order = [...body.querySelectorAll(".upstream-row")].map(r => r.dataset.upstream);
    try { localStorage.setItem(UPSTREAM_ROW_ORDER_KEY, JSON.stringify(order)); } catch (_) {}
  }

  // v0.77：按 renderUpstream 里已排序的 entries 顺序重排行。必须有
  // 排序调用（在 renderUpstream 里主动触发），否则行序不变。跳过
  // 不在渲染列表里的行（新配置 / 已删除），避免把旧 row 留到末尾。
  // v0.78：顺序没变时直接短路 —— v0.56 的教训，避免每 tick 都把已有
  // row detach/re-append（会重启 ::after 的 bump 波纹动画）。
  function applyUpstreamRowOrder(body, entries) {
    if (!entries) return;
    const order = entries.map(e => e[0]);
    const rows = body.querySelectorAll(".upstream-row");
    if (rows.length === order.length &&
        Array.from(rows).every((r, i) => r.dataset.upstream === order[i])) {
      return;
    }
    const rowMap = new Map();
    rows.forEach(r => rowMap.set(r.dataset.upstream, r));
    for (const name of order) {
      const r = rowMap.get(name);
      if (r) {
        body.appendChild(r);  // move to end in sorted order
        rowMap.delete(name);
      }
    }
    // 不在排序列表里的行追加到末尾。
    rowMap.forEach(r => body.appendChild(r));
  }

  function wireUpstreamRowDrag() {
    const body = $("card-upstream-body");
    if (!body || body.dataset.rowDragWired) return;
    body.dataset.rowDragWired = "1";

    let dragRow = null;

    body.addEventListener("dragstart", (e) => {
      const row = e.target.closest(".upstream-row");
      if (!row) return;
      // 行拖拽归自己管，别让 grid 的 dragstart 把它当成卡片拖拽。
      e.stopPropagation();
      dragRow = row;
      try {
        e.dataTransfer.effectAllowed = "move";
        const ghost = document.createElement("canvas");
        ghost.width = ghost.height = 1;
        e.dataTransfer.setDragImage(ghost, 0, 0);
      } catch (_) {}
      setTimeout(() => row.classList.add("dragging"), 0);
    });

    body.addEventListener("dragover", (e) => {
      if (!dragRow) return;
      e.preventDefault();
      try { e.dataTransfer.dropEffect = "move"; } catch (_) {}
      const over = e.target.closest(".upstream-row");
      body.querySelectorAll(".upstream-row.drop-target").forEach(el => el.classList.remove("drop-target"));
      if (over && over !== dragRow) over.classList.add("drop-target");
    });

    body.addEventListener("dragleave", (e) => {
      if (e.target === body) {
        body.querySelectorAll(".upstream-row.drop-target").forEach(el => el.classList.remove("drop-target"));
      }
    });

    body.addEventListener("drop", (e) => {
      if (!dragRow) return;
      e.preventDefault();
      e.stopPropagation();
      const over = e.target.closest(".upstream-row");
      if (over && over !== dragRow) {
        body.insertBefore(dragRow, over);
      } else {
        body.appendChild(dragRow);
      }
      saveUpstreamRowOrder(body);
      clearRowDrag();
    });

    body.addEventListener("dragend", clearRowDrag);

    function clearRowDrag() {
      body.querySelectorAll(".upstream-row.drop-target").forEach(el => el.classList.remove("drop-target"));
      if (dragRow) dragRow.classList.remove("dragging");
      dragRow = null;
    }
  }

  function wireCardDragDrop() {
    const grid = document.querySelector('.view[data-view="overview"] .grid');
    if (!grid || grid.dataset.dragWired) return;
    grid.dataset.dragWired = "1";

    let dragCard = null;

    // v0.61：隔离窗口拖动 vs 卡片拖动。
    // pywebview 在 gui.py 里开了 easy_drag=True，它在 window 上挂了
    // mousedown 监听，凡是 target 不是 button/input/select/a 的就
    // 启动整个 WebView2 窗口拖动。我们的 .glass-card 是 div，命中
    // 它的 mousedown 会被两个监听同时认领：pywebview 拖窗口、HTML5
    // 拖卡片 —— 鼠标一拉整个 GUI 跟着飘。
    // 修法：在 grid 上挂一个 mousedown，target 是 .glass-card（或它
    // 内部非交互子元素）就 stopPropagation，pywebview 的 window 监听
    // 收不到事件，自然不会启动窗口拖动。
    // 注意三件事：
    //   1. 不用 preventDefault —— click 还得正常派发，否则"最近活动"
    //      那张卡的点击就开不出 modal 了。
    //   2. 不用 stopImmediatePropagation —— 当前没人在同一元素上挂
    //      mousedown，将来万一有，也不至于把人误伤。
    //   3. 内部若有 button / input / a，仍然要把事件放上去，不能把
    //      这些交互吞掉。closest() 走的是"我是不是某个可交互父级的
    //      子元素"，命中就放行。
    const INTERACTIVE = "button, input, select, textarea, a, [contenteditable]";
    grid.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;            // 只拦左键
      const card = e.target.closest(".glass-card");
      if (!card) return;                       // 点 grid 空白：交给 pywebview 拖窗口
      if (e.target.closest(INTERACTIVE)) return; // 卡片内按钮：别吞
      e.stopPropagation();
    }, false /* bubble，跟 pywebview 同相位，靠"先到先得"赢 */);

    grid.addEventListener("dragstart", (e) => {
      const card = e.target.closest(".glass-card");
      if (!card) return;
      // v0.11.3: 上游卡的 body 区域（API 行 / 空档）拖拽归行排序
      // 管；整卡排序只从标题 / 边缘（非 body 区）拖起。
      if (card.dataset.card === "upstream" && e.target.closest(".card-body")) return;
      dragCard = card;
      try {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", card.dataset.card || "");
        // 1x1 透明 ghost —— 让自定义视觉（.dragging 半透明 + box-shadow）
        // 主导，不要默认 ghost 跟半透明叠加。
        const ghost = document.createElement("canvas");
        ghost.width = ghost.height = 1;
        e.dataTransfer.setDragImage(ghost, 0, 0);
      } catch (_) {
        // dataTransfer 在某些环境下只读，best-effort 即可。
      }
      // 延后一帧加 .dragging，避开 ghost image 捕获。
      setTimeout(() => card.classList.add("dragging"), 0);
    });

    grid.addEventListener("dragover", (e) => {
      if (!dragCard) return;
      e.preventDefault();
      try { e.dataTransfer.dropEffect = "move"; } catch (_) {}
      const target = findClosestCard(grid, e.clientX, e.clientY);
      grid.querySelectorAll(".drop-target").forEach(el => el.classList.remove("drop-target"));
      if (target && target !== dragCard) target.classList.add("drop-target");
    });

    grid.addEventListener("dragleave", (e) => {
      // 仅在真正离开 grid 时清，不要每个子元素 leave 都清掉。
      if (e.target === grid) {
        grid.querySelectorAll(".drop-target").forEach(el => el.classList.remove("drop-target"));
      }
    });

    grid.addEventListener("drop", (e) => {
      if (!dragCard) return;
      e.preventDefault();
      const target = findClosestCard(grid, e.clientX, e.clientY);
      if (target && target !== dragCard) {
        grid.insertBefore(dragCard, target);
      } else {
        grid.appendChild(dragCard);
      }
      saveCardOrder(grid);
      clearDragState();
    });

    grid.addEventListener("dragend", () => {
      clearDragState();
    });

    function clearDragState() {
      grid.querySelectorAll(".drop-target").forEach(el => el.classList.remove("drop-target"));
      if (dragCard) dragCard.classList.remove("dragging");
      dragCard = null;
    }
  }

  // 找离鼠标位置最近的一张非 dragging 卡。用"卡片中心到鼠标的距离"
  // 而不是 bounding box 边距，避免指针贴近角落时算错。drop 时把它
  // 当作"插到它前面"的参考点。
  function findClosestCard(container, x, y) {
    const cards = container.querySelectorAll(".glass-card:not(.dragging)");
    let best = null;
    let bestDist = Infinity;
    cards.forEach(card => {
      const r = card.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const d = Math.hypot(x - cx, y - cy);
      if (d < bestDist) {
        bestDist = d;
        best = card;
      }
    });
    return best;
  }

  function saveCardOrder(grid) {
    const order = [...grid.querySelectorAll(".glass-card")].map(c => c.dataset.card);
    try { localStorage.setItem(CARD_ORDER_KEY, JSON.stringify(order)); } catch (_) {}
  }

  function applyCardOrder() {
    const grid = document.querySelector('.view[data-view="overview"] .grid');
    if (!grid) return;
    let order = null;
    try {
      const raw = localStorage.getItem(CARD_ORDER_KEY);
      if (raw) order = JSON.parse(raw);
    } catch (_) {}
    if (!order || !Array.isArray(order)) return;
    // 把现有卡片按 order 顺序 appendChild 到末尾 —— appendChild
    // 已存在的节点会移动它。等同于 in-place reorder，不重建 DOM，
    // card-body 内的 innerHTML / 数据 / 动画状态全保留。
    const cardsByKey = new Map();
    grid.querySelectorAll(".glass-card").forEach(c => cardsByKey.set(c.dataset.card, c));
    for (const key of order) {
      const card = cardsByKey.get(key);
      if (card) {
        grid.appendChild(card);
        cardsByKey.delete(key);
      }
    }
    // 没在 order 里的卡片（首次启用 / 新增的卡）追加到末尾。
    cardsByKey.forEach(c => grid.appendChild(c));
  }

  // -------------------------------------------------------------------------
  // Card edge-resize（总览卡片拖拽调整尺寸）
  // -------------------------------------------------------------------------
  // 三条全宽命中区（悬停即变 resize 光标，不用精确点小点）：
  //   * 下沿 .card-edge-bottom —— 高度拖拽。按 dy 分档吸附到
  //     OVERVIEW_HEIGHTS 档位，结果存 --ov-h（CSS 变量，JS 只管数字）。
  //   * 右沿 .card-edge-right —— 列跨度拖拽。按 dx 在 1/2/3 列之间切换，
  //     存 --ov-span。
  //   * 右下角 .card-edge-corner —— 同时拖高度 + 跨度。
  // 尺寸持久化：setCardSize / getCardSize（localStorage overview-card-sizes-v1），
  // 每档只存一次（key + span + height）。图表卡高度联动 --ov-h →
  // _statsHostSize 重绘 SVG；chart.js 卡（hourly）靠 resize 监听重算。
  //
  // easy_drag 冲突规避：pywebview 的窗口拖动监听挂在 window 的 bubble
  // 相位，凡是 target 非 button/input/select/a 的 mousedown 都会启动窗口
  // 拖动。卡 body 的空白区域（div）会被它认领 → 拖卡片改尺寸变成拖整个
  // GUI 窗口。修法：grid 的 bubble 相位 mousedown 里，target 命中卡边缘
  // 手柄就 stopPropagation（比 window 的 bubble 监听先到），让 easy_drag
  // 收不到。拖拽过程用 window 级 pointermove/up（capture 相位），期间
  // 给 body 加 .card-resizing，配合 renderAll 的 `if (window._cardResizing)`
  // 短路跳过每次 tick 的重渲染（不然 500ms 一次的 innerHTML 重建会跟
  // 正在拖的尺寸打架，SVG 闪断）。
  const CARD_RESIZE_GRIP_SIZE = 14;

  // 拖拽期间的全局状态（放在模块作用域，wireCardResize 外），供
  // renderAll 的短路判断使用。
  let _cardResizeActive = false;
  let _cardResize = null;
  function cardResizingNow() { return !!_cardResizeActive; }
  const INTERACTIVE_SEL = "button, input, select, textarea, a, [contenteditable]";

  function wireCardResize() {
    if (window.__cardResizeWired) return;
    window.__cardResizeWired = true;

    // mousedown（bubble，grid 上）：命中卡边缘手柄 → stopPropagation，
    // pywebview 的 easy_drag window 监听收不到，就不会把卡片尺寸拖拽
    // 误判成窗口拖拽。button/input/select/a 一律放行（不拦截）。
    const grid = document.querySelector('.view[data-view="overview"] .grid');
    if (grid) {
      grid.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        const grip = e.target.closest("[data-card-edge-h], [data-card-edge-w], [data-card-edge-br]");
        if (!grip) return;
        e.stopPropagation();
        e.preventDefault();
      }, false);
    }

    // 详情面板（右下角宽高指示 + 列跨度切换按钮），内联样式跟随活动卡。
    function syncResizePanel(card) {
      const panel = document.getElementById("card-resize-panel");
      if (!panel) return;
      if (!card) { panel.hidden = true; return; }
      const key = card.dataset.card;
      const s = getCardSize(key);
      const curSpan = s.span || naturalSpan(card);
      const sw = OVERVIEW_SPANS.map(v =>
        `<button type="button" class="card-span-btn${v === curSpan ? " active" : ""}"` +
        ` data-card-span-set="${v}" data-card="${key}">${v}</button>`).join("");
      panel.querySelector("[data-card-span]").innerHTML = sw;
      panel.querySelector("[data-card-h]").textContent = (s.height > 0 ? s.height : 200) + "px";
      // 记录当前活动卡 key（面板关闭/其它卡操作时判断用）。
      panel.dataset.activeKey = key;
      // 面板定位：当前卡的右上角下方。
      const r = card.getBoundingClientRect();
      panel.style.left = Math.max(8, r.right - 230) + "px";
      panel.style.top = Math.max(8, r.top + 40) + "px";
      panel.hidden = false;
    }

    // 点击列跨度按钮（文档级委托，动态卡同样生效）。
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-card-span-set]");
      if (!btn) return;
      e.stopPropagation();
      const key = btn.dataset.card;
      const span = parseInt(btn.dataset.cardSpanSet, 10) || 1;
      if (!key || !OVERVIEW_SPANS.includes(span)) return;
      const s = getCardSize(key);
      // 只改 span，不传第 4 参 → setCardSize 保留该槽已有的 x/y（v0.187）。
      // v0.186 这里会把坐标一起擦掉，导致自由模式下点一下列跨度、卡就掉回
      // 默认模式。
      setCardSize(key, span, s.height || 0);
      const card = document.querySelector(`.glass-card[data-card="${key}"]`);
      if (card) {
        applyCardSize(card);
        // 列跨度变了 → 卡宽变化，图表要按新宽度重算。强制重渲染。
        lastCardsSig = null;
        if (lastSnap) renderAll(lastSnap, lastStatus);
      }
    });

    // v0.187：「布局模式」总开关 click 委托 —— 全局切换默认 ↔ 自由。
    //
    // v0.186 是逐卡 [data-card-free-toggle]（每卡一个 📌 按钮），v0.187 换成
    // 弹层顶部一组二选一按钮 [data-layout-mode]，一次切全部卡。切换的实际
    // 工作（seed 自由槽 / 重应用尺寸 / 重排 / 重渲染）全在 switchLayoutMode 里。
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-layout-mode]");
      if (!btn) return;
      e.stopPropagation();
      e.preventDefault();
      const want = btn.dataset.layoutMode === "free" ? "free" : "default";
      switchLayoutMode(want);
      // 弹层还开着 —— 同步这一组按钮的 active 态（不用关掉重开）。
      document.querySelectorAll("[data-layout-mode]").forEach(b => {
        b.classList.toggle("active", b.dataset.layoutMode === want);
      });
    });

    // 点击卡体空白（非交互区 / 非可点击行 / 非手柄）→ 弹出尺寸面板。
    // 用 click 而不是 pointerdown：dragstart 的 mousedown+move 也会先触发
    // pointerdown，用 click 才能区分"点一下"和"拖拽重排"。
    //
    // v0.186：自由模式 (.card-free-positioned) 卡不弹 resize 面板 —— 自由
    // 卡 hover 时整体可拖，弹面板会跟拖动手势视觉打架（拖完一张卡就弹
    // 一坨面板，喧闹）。如果用户真想调自由卡的尺寸，可以点右下角边缘
    // 命中区走 resize 路径，调完后再用 📌 按钮把模式状态改回默认模式。
    document.addEventListener("click", (e) => {
      const t = e.target;
      if (!t || !t.closest) return;
      if (e.target.closest("#card-resize-panel")) return;
      if (e.target.closest("[data-card-edge-h], [data-card-edge-w], [data-card-edge-br]")) return;
      const cardEl = e.target.closest('.view[data-view="overview"] .glass-card');
      if (!cardEl) { return; }
      if (e.target.closest(INTERACTIVE_SEL)) return;
      if (e.target.closest("[data-agent-row], .recent-row[data-id], [data-upstream], .platform-row")) return;
      // 自由模式卡不弹面板（理由见上）—— 让用户可以自由拖动不被面板
      // 视觉干扰。如果需要进面板，先点 📌 切回默认模式。
      if (cardEl.classList.contains("card-free-positioned")) return;
      syncResizePanel(cardEl);
    });

    // 关闭详情面板：点面板外区域（上面的 click 处理器没命中卡体时）。
    document.addEventListener("click", (e) => {
      const panel = document.getElementById("card-resize-panel");
      if (!panel || panel.hidden) return;
      if (e.target.closest("#card-resize-panel")) return;
      if (e.target.closest("[data-card-edge-h], [data-card-edge-w], [data-card-edge-br]")) return;
      panel.hidden = true;
    });
    // 点面板内的 × 关闭。
    document.addEventListener("click", (e) => {
      const close = e.target.closest("[data-card-resize-close]");
      if (!close) return;
      e.stopPropagation();
      const panel = document.getElementById("card-resize-panel");
      if (panel) panel.hidden = true;
    });
  }

  // window 级 pointer 处理器（capture 相位，跟 pausePolling 同一批挂载）。
  // 边缘命中区：右下角 > 右沿 > 下沿（corner 优先，命中一个就够）。
  function cardResizePointerDown(e) {
    if (e.button !== 0) return;
    const t = e.target;
    const corner = t && t.closest ? t.closest("[data-card-edge-br]") : null;
    const rEdge = !corner && t && t.closest ? t.closest("[data-card-edge-w]") : null;
    const bEdge = !corner && t && t.closest ? t.closest("[data-card-edge-h]") : null;
    if (!corner && !rEdge && !bEdge) return;
    e.preventDefault();
    // stopImmediatePropagation 而非 stopPropagation：wireEdgeResize 也在
    // window 的 capture 相位挂了 mousedown 监听器，如果用户点了靠近窗口
    // 边的卡片命中区，它会判 detectEdge() 命中窗口边并启动 api.resizeWindow
    // —— 跟我们要的"卡片尺寸拖拽"打架。两个 capture 监听器共享 capture
    // 队列，stopPropagation 只挡 bubble 不挡 capture 队列里的下一个监听器。
    // **只有 stopImmediatePropagation 才能彻底阻断 wireEdgeResize**。
    e.stopImmediatePropagation();
    e.stopPropagation();
    const card = (corner || rEdge || bEdge).closest(".glass-card");
    if (!card) return;
    const key = card.dataset.card;
    const s0 = getCardSize(key);
    const startSpan = s0.span || naturalSpan(card);
    // 起始高度 = 当前 body 实际高度（/ zoom 转 CSS px），并预先吸附到
    // 最近档，这样刚按下手柄（还没拖动）不会"啪"地跳变。
    const body = card.querySelector(".card-body");
    const rect = (body || card).getBoundingClientRect();
    const measuredH = rect.height / _currentPageZoom();
    // 优先用已存的高度档做基准；没存过才用实测高度。
    // 为什么不能直接用实测值：没拖过的卡 body 高度由内容撑开（占位文案
    // 时只有 ~27px，远小于最小档 160），下面 initSnap 会把它吸到 160，
    // 于是 startH(27) 和 height(160) 差了 130px —— move 里 newH =
    // startH + dy 要跑到 170 才跨第一档，用户得拖 140+px 才看见变化，
    // 表现就是"拖动无效"。基准必须跟 st.height 同一坐标系。
    const clampH = (v) => Math.max(
      OVERVIEW_HEIGHTS[0],
      Math.min(OVERVIEW_HEIGHTS[OVERVIEW_HEIGHTS.length - 1], v));
    const baseH = (s0 && s0.height > 0) ? s0.height : clampH(measuredH);
    const initSnap = OVERVIEW_HEIGHTS.reduce((acc, h, i) =>
      Math.abs(h - baseH) < Math.abs(OVERVIEW_HEIGHTS[acc] - baseH) ? i : acc, 0);
    // 列跨度连续拖拽：测量"1 列 = 多少 CSS px"，用作 dx → span 的换算。
    // 取法：找一张自然 span=1 的卡，量它渲染后的宽度 —— 跟当前起始
    // span 的卡在同一行，所以列宽一致。或者直接用 grid 容器宽度 / 3
    // （3 列布局 + gap）。两种都给，最后取一个更稳的。
    const gridEl = card.parentElement; // .grid
    const gridRect = gridEl ? gridEl.getBoundingClientRect() : null;
    const cs = gridEl ? getComputedStyle(gridEl) : null;
    const gap = cs ? (parseFloat(cs.columnGap || cs.gap || "0") || 0) : 0;
    // 从 grid 容器反推列宽：(gridWidth - 2*gap) / 3。
    let colPx = 0;
    if (gridRect && gridRect.width > 0) {
      colPx = Math.max(40, (gridRect.width - gap * 2) / 3);
    }
    // 备份：直接读当前卡在 startSpan 时的实际宽度，作为兜底列宽估算。
    const cardRect0 = card.getBoundingClientRect();
    const colPxFallback = (cardRect0.width + gap) / Math.max(1, startSpan);
    if (!(colPx > 0)) colPx = colPxFallback;
    _cardResize = {
      mode: corner ? "both" : (rEdge ? "w" : "h"),
      card,
      body,
      key,
      startX: e.clientX,
      startY: e.clientY,
      startH: OVERVIEW_HEIGHTS[initSnap],
      startSpan,
      span: startSpan,
      height: OVERVIEW_HEIGHTS[initSnap],
      prevDraggable: card.draggable,
      colPx,           // 1 列 ≈ 多少 CSS px（用于 dx → span 换算）
      saved: false,    // 拖拽过程中是否已写过 localStorage
    };
    _cardResizeActive = true;
    // 关键：临时关掉卡片的 HTML5 draggable，否则浏览器在 mousedown +
    // mousemove 后会启动 dragstart 走 grid 的 dragstart listener，把整
    // 张卡变成"重排拖拽" —— 跟我们想要的"边缘尺寸拖拽"打架。e.preventDefault
    // 单独不够（pointerdown vs mousedown default-prevented 在不同浏览器
    // 不一致；dragstart 是独立事件链，preventDefault 不一定能拦下）。直接
    // 改 draggable 是最稳的。pointerup 时还原。
    card.draggable = false;
    // body class 加 mode 标记（如 card-resizing-w / -h / -both），CSS 端
    // 根据 mode 决定整卡光标，拖拽中鼠标滑到卡任何位置都保持一致。
    document.body.classList.add("card-resizing");
    document.body.classList.add("card-resizing-" + _cardResize.mode);
  }

  // 当前页面 zoom（1 = 无缩放）。body.zoom 会放大 getBoundingClientRect
  // 返回的物理像素，转 CSS px 要除回来 —— 否则在缩放下拖出来的高度
  // 会偏大。与 _statsHostSize 同一套换算。
  function _currentPageZoom() {
    try {
      const z = parseFloat(getComputedStyle(document.body).getPropertyValue("--page-zoom"));
      return (Number.isFinite(z) && z > 0) ? z : 1;
    } catch (_) { return 1; }
  }

  function cardResizePointerMove(e) {
    if (!_cardResize) return;
    e.preventDefault();
    const st = _cardResize;
    const z = _currentPageZoom();
    if (st.mode === "h" || st.mode === "both") {
      const dy = (e.clientY - st.startY) / z;
      // startH 已是档位值，跟 st.height 同坐标系 —— 所以 dy 是"从当前档
      // 出发的位移"，拖 10px 就能跨过 20px 档的中点，立刻见效。
      const newH = Math.max(
        OVERVIEW_HEIGHTS[0],
        Math.min(OVERVIEW_HEIGHTS[OVERVIEW_HEIGHTS.length - 1],
                 Math.round(st.startH + dy)));
      const idx = OVERVIEW_HEIGHTS.reduce((acc, h, i) => Math.abs(h - newH) < Math.abs(OVERVIEW_HEIGHTS[acc] - newH) ? i : acc, 0);
      const target = OVERVIEW_HEIGHTS[idx];
      if (target !== st.height) {
        st.height = target;
        if (st.body) st.body.style.setProperty("--ov-h", target + "px");
        // 高度档位本来就是离散的，每过一档写一次 localStorage 无压力；
        // 但 span 已经移到 pointerup 才写，统一收尾更干净 —— 不在这里写。
      }
    }
    if (st.mode === "w" || st.mode === "both") {
      // 列跨度拖拽：dx 转"列数变化"（浮点），snap 到 OVERVIEW_SPANS 最近
      // 档（20 档 1.0–3.0 步长 0.1）。clamp 到 [1.0, 3.0] 后线性最近邻
      // 搜索。layout 是 flex，卡走 width: calc()，所以小数 span 真的生效。
      // localStorage 只在 pointerup 写一次。
      const dx = (e.clientX - st.startX) / z;
      const floatSpan = st.startSpan + dx / st.colPx;
      const clamped = Math.max(OVERVIEW_SPANS[0], Math.min(OVERVIEW_SPANS[OVERVIEW_SPANS.length - 1], floatSpan));
      const target = OVERVIEW_SPANS.reduce((acc, v) =>
        Math.abs(v - clamped) < Math.abs(acc - clamped) ? v : acc, OVERVIEW_SPANS[0]);
      if (Math.abs(target - st.span) > 0.05) {
        st.span = target;
        st.card.style.setProperty("--ov-span", String(target));
      }
    }
  }

  function cardResizePointerUp(e) {
    if (!_cardResize) return;
    const st = _cardResize;
    _cardResize = null;
    _cardResizeActive = false;
    document.body.classList.remove("card-resizing", "card-resizing-" + st.mode);
    // 还原卡片的 draggable —— 之前 pointerdown 临时关了它，避免 HTML5
    // 拖拽重排跟边缘尺寸拖拽抢。这里恢复成原始值（通常 true，新建卡也 true）。
    if (st.card && typeof st.prevDraggable === "boolean") {
      st.card.draggable = st.prevDraggable;
    }
    // 收尾：把最终的 span / height 一次性写 localStorage。中间过程只改
    // CSS 变量（视觉连续），不写存储 —— 避免拖 500ms 写 30 次。
    // 不传第 4 参 → 保留该槽已有的 x/y（v0.187）：自由模式下拖边缘改尺寸
    // 后，卡还留在原位、还是自由模式。v0.186 这里会连坐标一起清掉。
    setCardSize(st.key, st.span, st.height || 0);
    // 结束拖拽：强制重渲染（数据没变但布局变了 —— D3 图要按新 body
    // 尺寸重算 _statsHostSize；sig 没变，必须清掉短路标记才不走 cache）。
    lastCardsSig = null;
    if (lastSnap) renderAll(lastSnap, lastStatus);
  }

  // =========================================================================
  // v0.186：总览卡片「自由模式」拖动 —— 鼠标按住自由模式卡
  // (.card-free-positioned) 的非边缘命中区拖动，整张卡跟手走。
  //
  // 设计要点：
  //  - 跟 cardResizePointerDown 一样挂 window 级 capture 监听器，避免被
  //    grid 的 HTML5 dragstart 抢走拖动。
  //  - mousedown 命中三件事：(a) 卡必须是自由模式（.card-free-positioned）；
  //    (b) 落点不能在边缘命中区（让位给 resize 拖动）；(c) 落点不能在交
  //    互控件（button/select/...）上，否则点一下应该触发控件交互。
  //  - 临时关 card.draggable = false（mouseup 还原）—— 跟 v0.185 resize 同套路。
  //  - 拖动期间 body 加 .card-dragging-free，CSS 端给 cursor:move !important。
  //  - mouseup 时一次写 localStorage（不拖动期间不写），同时给卡
  //    style.zIndex = ++_zCounter 让它置顶（z-index 局部隔离，只动该卡）。
  // =========================================================================
  let _cardFreeDrag = null;
  let _freeDragZCounter = 1;  // 自增计数器，每次拖完一张 +1

  function cardFreeDragPointerDown(e) {
    // 只有左键、且当前没有别的 resize 拖动在进行才响应（避免互斥打架）
    if (e.button !== 0) return;
    if (_cardResizeActive) return;
    if (_cardFreeDrag) return;  // 已经在拖一张卡
    const t = e.target;
    if (!t || !t.closest) return;
    // 让位给边缘尺寸拖动 —— 命中三段命中区任何一条都早返回，让
    // cardResizePointerDown 处理（resize 优先级更高，因为边缘只 8-20px 窄条）
    if (t.closest("[data-card-edge-h], [data-card-edge-w], [data-card-edge-br]")) return;
    // 命中交互控件 —— 不接管（让 click / native control 自己处理）
    if (t.closest(INTERACTIVE_SEL)) return;
    const card = t.closest(".glass-card.card-free-positioned");
    if (!card) return;
    const key = card.dataset.card;
    if (!key) return;
    e.preventDefault();
    // stopImmediatePropagation：跟 cardResizePointerDown 同套路 —— wireEdgeResize
    // 也在 capture 队列里挂 mousedown，不 stopImmediate 会让它误判为窗口边缘
    // resize 把窗口拖大。
    e.stopImmediatePropagation();
    e.stopPropagation();
    // 起点：相对 .grid 容器的偏移（不是 viewport —— 滚动 grid 时卡跟着走）
    const grid = card.parentElement;
    const gr = grid ? grid.getBoundingClientRect() : { left: 0, top: 0 };
    const cr = card.getBoundingClientRect();
    _cardFreeDrag = {
      card,
      key,
      grid,
      offsetX: e.clientX - cr.left,    // 鼠标相对卡左上角的偏移
      offsetY: e.clientY - cr.top,
      startGridLeft: gr.left,           // mousedown 时 grid 位置（防滚动漂移）
      startGridTop: gr.top,
      startLeft: parseFloat(card.style.left) || 0,  // 卡 mousedown 时相对 grid 的 left
      startTop: parseFloat(card.style.top) || 0,
      prevDraggable: card.draggable,
      startX: e.clientX,                // 调试 / 兜底用
      startY: e.clientY,
    };
    // 临时关 HTML5 拖拽，避免 grid reorder 抢走整卡拖动
    card.draggable = false;
    document.body.classList.add("card-dragging-free");
  }

  function cardFreeDragPointerMove(e) {
    if (!_cardFreeDrag) return;
    const st = _cardFreeDrag;
    e.preventDefault();
    const z = _currentPageZoom();
    // 跟手：新位置 = 起点（grid-relative）+ 鼠标位移
    // 起点是 mousedown 时的 left/top（即 grid-relative 偏移），加上从那
    // 一刻起的鼠标位移（dx/dy）就是新位置。整张卡 absolute 定位后，
    // 滚动 grid 时 grid left/top 会变，但卡本身的 inline left/top 不变，
    // 视觉上卡跟随 grid 移动 —— 跟我们期望一致。
    const dx = (e.clientX - st.startX) / z;
    const dy = (e.clientY - st.startY) / z;
    const p = _clampFreePos(st.card, st.startLeft + dx, st.startTop + dy);
    st.card.style.left = p.x + "px";
    st.card.style.top = p.y + "px";
    // 临时诊断：拖动期间节流打点（每 100ms 一次）—— 不节流会每秒写
    // 几十次文件影响流畅度。
    const now = Date.now();
    if (!st._lastDiag || now - st._lastDiag > 100) {
      st._lastDiag = now;
      _diagDump("drag:" + (st.card.dataset.card || "?"));
    }
  }

  function cardFreeDragPointerUp(e) {
    if (!_cardFreeDrag) return;
    const st = _cardFreeDrag;
    _cardFreeDrag = null;
    document.body.classList.remove("card-dragging-free");
    if (st.card && typeof st.prevDraggable === "boolean") {
      st.card.draggable = st.prevDraggable;
    }
    // 收尾：把当前 (left, top) 写 localStorage —— 这是 grid-relative 偏移，
    // applyCardFreePosition 读出来时直接当 left/top 用。
    const left = parseFloat(st.card.style.left);
    const top = parseFloat(st.card.style.top);
    if (Number.isFinite(left) && Number.isFinite(top)) {
      const s = getCardSize(st.key);
      setCardSize(st.key, s.span, s.height, { x: left, y: top });
      // 置顶：自增 z-counter，让最近拖动的卡盖在所有自由卡之上
      st.card.style.zIndex = String(++_freeDragZCounter);
    }
    // 不强制重渲染 —— 位置变化不影响 D3 图尺寸（除非 resize），signature 不变
    _diagDump("drop:" + (st.card.dataset.card || "?"));
  }

  // -------------------------------------------------------------------------
  // Edge resize
  // -------------------------------------------------------------------------
  // Frameless 窗口没有 OS 边的 resize border —— 用户拽不到东西改大小。
  // 自己实现：mousedown capture 相位上先看 clientX/Y 离窗口边是否在
  // THRESHOLD 像素内，命中就 stopImmediatePropagation 抢过 pywebview
  // easy_drag，window.innerWidth/innerHeight 算新尺寸，桥接到 Python
  // 走 pywebview.window.resize + SetWindowPos。最大化时整个跳过。
  //
  // FixPoint Flag bitset (pywebview 那边拿 FixPoint(int) 还原)：
  //   NORTH=1 / WEST=2 / EAST=4 / SOUTH=8
  // edge 决定哪些位要保留（"不动哪一边" = 锚点）：
  //   右沿 / 下沿 / 右下角 → 锚 NW (1|2 = 3)：top + left 不动
  //   左沿 / 左下角       → 锚 NE (1|4 = 5)：top + right 不动
  //   上沿 / 右上角       → 锚 SW (8|2 = 10)：bottom + left 不动
  //   左上角              → 锚 SE (8|4 = 12)：bottom + right 不动
  const RESIZE_THRESHOLD = 6;
  const RESIZE_MIN_W = 480;
  const RESIZE_MIN_H = 360;
  const EDGE_FIX = { r: 3, b: 3, br: 3, l: 5, bl: 5, t: 10, tr: 10, tl: 12 };
  const EDGE_CURSOR = {
    t: "ns-resize", b: "ns-resize",
    l: "ew-resize", r: "ew-resize",
    tl: "nwse-resize", br: "nwse-resize",
    tr: "nesw-resize", bl: "nesw-resize",
  };

  function detectEdge(x, y) {
    const w = window.innerWidth, h = window.innerHeight;
    const left = x < RESIZE_THRESHOLD;
    const right = x > w - RESIZE_THRESHOLD;
    const top = y < RESIZE_THRESHOLD;
    const bottom = y > h - RESIZE_THRESHOLD;
    if (top && left) return "tl";
    if (top && right) return "tr";
    if (bottom && left) return "bl";
    if (bottom && right) return "br";
    if (top) return "t";
    if (right) return "r";
    if (bottom) return "b";
    if (left) return "l";
    return null;
  }

  function wireEdgeResize() {
    if (window.__edgeResizeWired) return;
    window.__edgeResizeWired = true;

    let active = null;

    // capture 相位：pywebview 的 easy_drag 监听挂在 window 的 bubble
    // 相位，我们在 capture 先到 → stopImmediatePropagation 让它收不到。
    window.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      if (document.body.classList.contains("is-maximized")) return;
      const edge = detectEdge(e.clientX, e.clientY);
      if (!edge) return;
      e.stopImmediatePropagation();
      e.preventDefault();
      active = {
        edge,
        startW: window.innerWidth,
        startH: window.innerHeight,
        startX: e.clientX,
        startY: e.clientY,
      };
      document.body.classList.add("resizing");
      document.body.style.cursor = EDGE_CURSOR[edge] || "";
    }, true);

    // 鼠标移动 + 抬起挂在 window，不挂 document —— WebView2 焦点丢
    // 失（按 alt-tab 切走）时 mousedown 不会卡住。即便用户拖快了鼠标
    // 飞出去，window 内仍能继续收到后续 mousemove。mouseleave 不靠
    // 谱，因为外层 Win32 窗口可能消费事件。
    window.addEventListener("mousemove", (e) => {
      if (active) {
        const dx = e.clientX - active.startX;
        const dy = e.clientY - active.startY;
        let newW = active.startW, newH = active.startH;
        if (active.edge.includes("l")) newW = Math.max(RESIZE_MIN_W, active.startW - dx);
        if (active.edge.includes("r")) newW = Math.max(RESIZE_MIN_W, active.startW + dx);
        if (active.edge.includes("t")) newH = Math.max(RESIZE_MIN_H, active.startH - dy);
        if (active.edge.includes("b")) newH = Math.max(RESIZE_MIN_H, active.startH + dy);
        api.resizeWindow(newW, newH, EDGE_FIX[active.edge]);
      } else if (!document.body.classList.contains("card-resizing")) {
        // **不在 resize 态 + 不在卡片尺寸拖拽中**：纯做 cursor hint。
        // 关键跳过条件：卡片尺寸拖拽期间 cursor 由 body.card-resizing-h /
        // -w / -both 类（CSS !important）决定，wireEdgeResize 不能再用
        // 内联 style 把它清掉 —— 否则用户报告"按下后 cursor 跳回普通样式"。
        // 检测窗口边：光标靠近边就变 resize 形；不在就清掉。
        const edge = detectEdge(e.clientX, e.clientY);
        document.body.style.cursor = edge ? (EDGE_CURSOR[edge] || "") : "";
      }
    });

    window.addEventListener("mouseup", () => {
      if (!active) return;
      active = null;
      document.body.classList.remove("resizing");
      document.body.style.cursor = "";
    });

    // 失焦兜底：alt-tab 切走的时候 mouseup 可能不送达，状态会卡住。
    window.addEventListener("blur", () => {
      if (!active) return;
      active = null;
      document.body.classList.remove("resizing");
      document.body.style.cursor = "";
    });
  }

  function escape(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------

  // pywebview 在 DOMContentLoaded 之后才注入 window.pywebview.api，
  // 必须等 pywebviewready 事件，否则前几轮 tick 全部走 bridge-missing
  // 分支，topbar 卡在「等待 bridge 响应…」。
  function startPolling() {
    if (startPolling._started) return;
    startPolling._started = true;
    // Poll every 500 ms (matches the Python thread cadence). pywebview's
    // WinForms backend has been observed to drop setInterval callbacks
    // after a few seconds on some Windows installs, so we chain
    // setTimeout(self-tick) recursively — the previous version also ran
    // setInterval(tick, 500) in parallel, which doubled every tick's
    // work (two fetches, two innerHTML rebuilds, two chart teardowns).
    // self-rescheduling ticks are self-throttling (an in-flight tick
    // waits for its own await before scheduling the next).
    function scheduleNext() {
      setTimeout(() => { tick().then(scheduleNext).catch(scheduleNext); }, 500);
    }
    loadAgentAliases();
    loadUaRules();
    scheduleNext();
    // Kick off an immediate fetch so the first paint isn't blank.
    tick();
  }

  // v0.112k：Ctrl + 加号 / 减号 / 0 —— 页面缩放（手动实现 body.zoom）。
  // WebView2 生产模式 AreBrowserAcceleratorKeysEnabled=False 会禁用浏览器
  // 内置的 Ctrl+滚轮/加号/减号缩放，但按键事件仍会到达页面（该开关只关
  // 内置动作、不拦事件），所以前端自己缩放，不依赖后端设置。范围 0.5–2.0、
  // 步进 0.1、Ctrl+0 复位；值存 localStorage，重启保持。
  let _pageZoom = 1;
  try {
    const _saved = parseFloat(localStorage.getItem("page-zoom"));
    if (Number.isFinite(_saved)) _pageZoom = _saved;
  } catch (err) {}
  function applyPageZoom() {
    _pageZoom = Math.min(2, Math.max(0.5, _pageZoom));
    try {
      // v0.113d：body.zoom 是纯放大不重排 —— 布局宽高保持在"布局 px"，
      // 放大 Z 倍后容器比视口宽/高 Z 倍（1.5× 时 100vw → 实际 1.5×视口宽），
      // 被 body{overflow:hidden} 裁掉。补偿：body 宽高反除 Z（100vw/Z 布局
      // px，再被 zoom 放大回正好 100vw），并写 --page-zoom 给内部 CSS 里
      // 的 calc(.../var(--page-zoom)) 同步补偿（modal 卡片 max-height、
      // 长列表 max-height、dialog max-width 等 vh/vw 尺寸）。
      document.body.style.zoom = String(_pageZoom);
      document.body.style.setProperty("--page-zoom", String(_pageZoom));
      document.body.style.width = _pageZoom === 1 ? "" : "calc(100vw / " + _pageZoom + ")";
      document.body.style.height = _pageZoom === 1 ? "" : "calc(100vh / " + _pageZoom + ")";
    } catch (err) {}
    try { localStorage.setItem("page-zoom", String(_pageZoom)); } catch (err) {}
  }
  function initPageZoom() {
    applyPageZoom();
    document.addEventListener("keydown", (e) => {
      if (!e.ctrlKey || e.altKey || e.metaKey) return;
      const k = e.key;
      if (k === "+" || k === "=") {
        e.preventDefault();
        _pageZoom = Math.round((_pageZoom + 0.1) * 10) / 10;
        applyPageZoom();
      } else if (k === "-" || k === "_") {
        e.preventDefault();
        _pageZoom = Math.round((_pageZoom - 0.1) * 10) / 10;
        applyPageZoom();
      } else if (k === "0") {
        e.preventDefault();
        _pageZoom = 1;
        applyPageZoom();
      }
    });
  }

  // -------------------------------------------------------------------------
  // Magnetic 磁吸动效 —— 事件委托 + MutationObserver。
  // 自动给 .btn / .switch 挂 .magnetic 类（窗口控制、弹窗关闭等易误触
  // 按钮排除），鼠标靠近时元素轻幅磁吸跟随，离开平滑回弹。动态渲染的
  // 按钮/开关无需手动处理：委托匹配 .magnetic，MutationObserver 负责
  // 给新插入的元素补类。
  // -------------------------------------------------------------------------
  function initMagnetic() {
    const EXCLUDE = ".win-ctrl, .modal-close";
    const SEL =
      ".btn:not(.magnetic), .switch:not(.magnetic), " +
      ".wire-btn:not(.magnetic), .quick-switch-btn:not(.magnetic), " +
      ".consume-switch-btn:not(.magnetic), .cards-manage-fab:not(.magnetic), " +
      ".nav-item:not(.magnetic), .nav-sub-item:not(.magnetic), " +
      ".prefs-pool-chip:not(.magnetic)";
    const arm = (root) => {
      (root || document)
        .querySelectorAll(SEL)
        .forEach((el) => {
          if (el.closest(EXCLUDE)) return;
          el.classList.add("magnetic");
        });
    };
    const reset = (el) => {
      if (!el) return;
      el.style.setProperty("--mag-x", "0px");
      el.style.setProperty("--mag-y", "0px");
    };
    let last = null;
    document.addEventListener("mousemove", (e) => {
      const el = e.target.closest ? e.target.closest(".magnetic") : null;
      if (el !== last) {
        reset(last);
        last = el;
      }
      if (!el) return;
      const r = el.getBoundingClientRect();
      const s = parseFloat(getComputedStyle(el).getPropertyValue("--mag-strength")) || 0.3;
      const cap = Math.min(r.width, r.height) * 0.18;
      const dx = (e.clientX - (r.left + r.width / 2)) * s;
      const dy = (e.clientY - (r.top + r.height / 2)) * s;
      el.style.setProperty("--mag-x", Math.max(-cap, Math.min(cap, dx)).toFixed(1) + "px");
      el.style.setProperty("--mag-y", Math.max(-cap, Math.min(cap, dy)).toFixed(1) + "px");
    });
    document.addEventListener("mouseleave", () => reset(last));
    arm(document);
    if (window.MutationObserver) {
      const mo = new MutationObserver(() => arm(document));
      mo.observe(document.body || document.documentElement, { childList: true, subtree: true });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireButtons();
    initMagnetic();
    initPageZoom();
    wireSpotlightToggle();
    // v0.41：wireAuraCanvas 已删除 —— 背景动效整段下线。
    // v0.36 — request-detail modal: close handlers (X / overlay / Esc)
    // and the overview-card click delegation that opens the modal from
    // the "最近活动" rows. Both functions are idempotent — safe to
    // call on every DOMContentLoaded even if the DOM was already there.
    wireModalClose();
    wireUpstreamModelsModalClose();
    wireOverviewRecentClicks();
    wireQuickSwitchClicks();
    wireQuickSwitchEditorModalClose();
    // v0.110：配置页事件委托（复制 / 快捷切换 / 上游行切换）。
    wireConfigPage();
    // v0.60：拖拽排序 —— 先把 localStorage 里持久化的顺序回填到 DOM，
    // 再挂 dragstart/dragover/drop 监听。两者顺序很重要：applyCardOrder
    // 用 appendChild 把卡片按保存顺序重排一遍（move-to-end 是唯一不破坏
    // Chromium 合成层动画状态的做法），再装监听才不会被自身的拖拽初始化
    // 触发 ghost。
    applyCardOrder();
    wireCardDragDrop();
    // v0.11.3: 上游状态卡内的 API 行拖拽排序。
    wireUpstreamRowDrag();
    // v0.11.3: 闪烁偏好（localStorage）在首帧就应用。
    loadPrefs();
    // v0.113n: 界面语言（localStorage）首帧应用 —— 静态区块/子菜单标题
    // 立即生效；动态渲染的区块各自在 render 后调 applyLang()。
    applyLang();
    wireEdgeResize();
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("pywebviewready", () => {
        startPolling();
      });
    }
    // 兜底：浏览器直开 index.html 时不会触发 pywebviewready，
    // 1.5 s 后强制启动，至少让 UI 活着。
    setTimeout(startPolling, 1500);
  });
})();
// OPENCODE-VIEW JS (managed by opencode_gui/integration/relay_patch.py)
/* OpenCode view (relay integration) — dropped into the relay web app.
 * Expects pywebview Api methods: opencode_get_config / opencode_start /
 * opencode_stop (see relay_patch.py). The generic setView mechanism already
 * flips section hidden/active; this module only wires the view's iframe +
 * buttons + theme sync. */
(function () {
  var V = "opencode"

  function el(id) { return document.getElementById(id) }

  function ocTheme() {
    var t = document.documentElement.dataset.theme || "light"
    return t === "day" ? "relay-day" : t === "dark" ? "relay-dark" : "relay-light"
  }

  function apply(cfg) {
    var frame = el("ocv-frame")
    var status = el("ocv-status")
    if (!cfg || !cfg.url) {
      status.textContent = "未启动"
      frame.removeAttribute("src")
      return
    }
    var theme = ocTheme()
    var plain = new URL(cfg.url)
    if (cfg.auth_token) plain.searchParams.set("auth_token", cfg.auth_token)
    var cur = frame.getAttribute("src")
    var curPlain = ""
    if (cur) {
      var u = new URL(cur)
      u.searchParams.delete("theme")
      curPlain = u.toString()
    }
    if (!cur || curPlain !== plain.toString()) {
      // First load (or server changed): include theme so first paint is right.
      var target = new URL(cfg.url)
      if (cfg.auth_token) target.searchParams.set("auth_token", cfg.auth_token)
      target.searchParams.set("theme", theme)
      frame.src = target.toString()
    } else {
      // Theme-only change: push via postMessage — the opencode app's
      // RelayThemeBridge applies it without reloading, keeping state.
      try { frame.contentWindow && frame.contentWindow.postMessage({ type: "oc-relay-theme", theme: theme }, "*") } catch (_) {}
    }
    status.textContent = "运行中 · " + cfg.url.replace(/^https?:\/\//, "")
    // Reflect the running model channel in the selector.
    var channel = el("ocv-channel")
    if (channel && cfg.model_channel) channel.value = cfg.model_channel
  }

  function refresh() {
    var frame = el("ocv-frame")
    var status = el("ocv-status")
    if (!frame || !status) return
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.opencode_get_config) return
    window.pywebview.api.opencode_get_config().then(apply).catch(function () { status.textContent = "状态读取失败" })
  }

  function wire() {
    var start = el("ocv-start")
    var stop = el("ocv-stop")
    var channel = el("ocv-channel")
    if (start && window.pywebview && window.pywebview.api) {
      start.addEventListener("click", function () {
        var ch = channel ? channel.value : "direct"
        window.pywebview.api.opencode_start(ch).then(refresh).catch(function () {})
      })
      stop.addEventListener("click", function () {
        window.pywebview.api.opencode_stop().then(refresh).catch(function () {})
      })
    }
    // Theme sync: re-apply theme query whenever the relay theme changes.
    if (window.MutationObserver) {
      new MutationObserver(refresh).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      })
    }
    // Refresh whenever the opencode view becomes active.
    var orig = window.setView
    if (orig) {
      window.setView = function (name) {
        orig(name)
        if (name === V) refresh()
      }
    }
    refresh()
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire)
  } else {
    wire()
  }
})()

