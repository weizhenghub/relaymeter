// 由 gen_preload.py 自动生成 —— 手工修改会被覆盖。
// 重跑：python src/relay/electron_app/gen_preload.py
//
// Electron 主窗口 preload：给 index.html 的 app.js 一个 `window.pywebview.api.*`
// 兼容桥。app.js 期望 `await window.pywebview.api.<name>(...)` 返回 Promise。
// 方法集 = web/app.js 里 `_call("…")` 首参 + 直接 `window.pywebview.api.X` 调用
// （gen_preload.py 提取，共 99 个）。每次 app.js 增删桥方法后重跑本脚本。
//
// 注意：**不能用 Proxy** —— contextBridge.exposeInMainWorld 无法克隆
// new Proxy({}, ...)（"An object could not be cloned."），必须显式枚举。
const { contextBridge, ipcRenderer } = require("electron");

function makeCall(method) {
  return (...args) => ipcRenderer.invoke("uplink", { method, args });
}

const api = {
  _diag_write_free_pos: makeCall("_diag_write_free_pos"),
  apply_upstream: makeCall("apply_upstream"),
  cleanup_messages: makeCall("cleanup_messages"),
  clear_logs: makeCall("clear_logs"),
  connectivity_test: makeCall("connectivity_test"),
  create_upstream: makeCall("create_upstream"),
  fetch_conversation: makeCall("fetch_conversation"),
  fetch_requests: makeCall("fetch_requests"),
  get_advanced_switch: makeCall("get_advanced_switch"),
  get_agent_aliases: makeCall("get_agent_aliases"),
  get_autostart: makeCall("get_autostart"),
  get_changelog: makeCall("get_changelog"),
  get_error_analysis: makeCall("get_error_analysis"),
  get_float_ball: makeCall("get_float_ball"),
  get_float_ball_topmost: makeCall("get_float_ball_topmost"),
  get_live_panel: makeCall("get_live_panel"),
  get_live_panel_always_one: makeCall("get_live_panel_always_one"),
  get_live_panel_auto_extend: makeCall("get_live_panel_auto_extend"),
  get_live_panel_concurrent: makeCall("get_live_panel_concurrent"),
  get_live_panel_done_clear_timeout: makeCall("get_live_panel_done_clear_timeout"),
  get_live_panel_ep_list_vh: makeCall("get_live_panel_ep_list_vh"),
  get_live_panel_frameless: makeCall("get_live_panel_frameless"),
  get_live_panel_gap_timeout: makeCall("get_live_panel_gap_timeout"),
  get_live_panel_list_font: makeCall("get_live_panel_list_font"),
  get_live_panel_max: makeCall("get_live_panel_max"),
  get_live_panel_min_cols: makeCall("get_live_panel_min_cols"),
  get_live_panel_text_timeout: makeCall("get_live_panel_text_timeout"),
  get_live_panel_thinking_timeout: makeCall("get_live_panel_thinking_timeout"),
  get_live_panel_tools_always: makeCall("get_live_panel_tools_always"),
  get_live_panel_tools_cap: makeCall("get_live_panel_tools_cap"),
  get_live_panel_tools_clear_timeout: makeCall("get_live_panel_tools_clear_timeout"),
  get_passthrough_mode: makeCall("get_passthrough_mode"),
  get_passthrough_upstreams: makeCall("get_passthrough_upstreams"),
  get_recent_uas: makeCall("get_recent_uas"),
  get_relay_settings: makeCall("get_relay_settings"),
  get_show_io_map: makeCall("get_show_io_map"),
  get_snapshot: makeCall("get_snapshot"),
  get_status: makeCall("get_status"),
  get_storage_info: makeCall("get_storage_info"),
  get_ua_rules: makeCall("get_ua_rules"),
  get_vision_models: makeCall("get_vision_models"),
  move_storage: makeCall("move_storage"),
  opencode_get_config: makeCall("opencode_get_config"),
  opencode_start: makeCall("opencode_start"),
  opencode_stop: makeCall("opencode_stop"),
  passthrough_overview: makeCall("passthrough_overview"),
  probe_upstream: makeCall("probe_upstream"),
  refresh_now: makeCall("refresh_now"),
  reload_gui: makeCall("reload_gui"),
  remove_upstream: makeCall("remove_upstream"),
  rename_passthrough_upstream: makeCall("rename_passthrough_upstream"),
  resize_window: makeCall("resize_window"),
  restart_server: makeCall("restart_server"),
  save_agent_aliases: makeCall("save_agent_aliases"),
  save_changelog: makeCall("save_changelog"),
  save_quick_switch: makeCall("save_quick_switch"),
  save_ua_rules: makeCall("save_ua_rules"),
  set_autostart: makeCall("set_autostart"),
  set_error_analysis: makeCall("set_error_analysis"),
  set_float_ball: makeCall("set_float_ball"),
  set_float_ball_topmost: makeCall("set_float_ball_topmost"),
  set_live_panel: makeCall("set_live_panel"),
  set_live_panel_always_one: makeCall("set_live_panel_always_one"),
  set_live_panel_auto_extend: makeCall("set_live_panel_auto_extend"),
  set_live_panel_concurrent: makeCall("set_live_panel_concurrent"),
  set_live_panel_done_clear_timeout: makeCall("set_live_panel_done_clear_timeout"),
  set_live_panel_ep_list_vh: makeCall("set_live_panel_ep_list_vh"),
  set_live_panel_frameless: makeCall("set_live_panel_frameless"),
  set_live_panel_gap_timeout: makeCall("set_live_panel_gap_timeout"),
  set_live_panel_list_font: makeCall("set_live_panel_list_font"),
  set_live_panel_max: makeCall("set_live_panel_max"),
  set_live_panel_min_cols: makeCall("set_live_panel_min_cols"),
  set_live_panel_text_timeout: makeCall("set_live_panel_text_timeout"),
  set_live_panel_thinking_timeout: makeCall("set_live_panel_thinking_timeout"),
  set_live_panel_tools_always: makeCall("set_live_panel_tools_always"),
  set_live_panel_tools_cap: makeCall("set_live_panel_tools_cap"),
  set_live_panel_tools_clear_timeout: makeCall("set_live_panel_tools_clear_timeout"),
  set_passthrough_mode: makeCall("set_passthrough_mode"),
  set_show_io_map: makeCall("set_show_io_map"),
  set_theme: makeCall("set_theme"),
  set_upstream_default_model: makeCall("set_upstream_default_model"),
  set_upstream_model: makeCall("set_upstream_model"),
  set_vision_models: makeCall("set_vision_models"),
  start_server: makeCall("start_server"),
  stats_aggregate: makeCall("stats_aggregate"),
  stats_daily: makeCall("stats_daily"),
  stats_model_daily: makeCall("stats_model_daily"),
  stop_server: makeCall("stop_server"),
  test_error_analysis: makeCall("test_error_analysis"),
  test_upstream: makeCall("test_upstream"),
  toggle_all_panels: makeCall("toggle_all_panels"),
  toggle_theme: makeCall("toggle_theme"),
  update_advanced_switch: makeCall("update_advanced_switch"),
  update_relay_settings: makeCall("update_relay_settings"),
  update_upstream_quota: makeCall("update_upstream_quota"),
  vacuum_storage: makeCall("vacuum_storage"),
  window_close: makeCall("window_close"),
  window_minimize: makeCall("window_minimize"),
  window_toggle_maximize: makeCall("window_toggle_maximize"),
};

contextBridge.exposeInMainWorld("pywebview", { api });

// ---- 桥就绪信号 ----
// app.js 在 DOMContentLoaded 里才挂 pywebviewready 监听（app.js:12606），
// preload 早于页面脚本执行，直接 dispatch 会丢事件。等 DOMContentLoaded 的
// 全部监听（含 app.js 的）跑完再派发 —— 用 setTimeout(0) 排到宏任务队尾，
// 保证 app.js 的监听已挂上（app.js 还留了 1.5s 兜底，双保险）。
function fireReady() {
  try { window.dispatchEvent(new Event("pywebviewready")); } catch (e) {}
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", function () { setTimeout(fireReady, 0); });
} else {
  setTimeout(fireReady, 0);
}

// ---- 拖拽桥（contextIsolation 下渲染进程摸不到 ipcRenderer，走这里） ----
// 主窗标题栏拖动：app.js 在可拖动区 mousedown/mouseup 调 panelDrag.start()/end()，
// 主进程 main_main.js 的 ipcMain.on("drag-start"/"drag-end") 轮询光标跟手。
contextBridge.exposeInMainWorld("panelDrag", {
  start: () => ipcRenderer.send("drag-start"),
  end:   () => ipcRenderer.send("drag-end"),
});
