// Electron 悬浮球 preload：给 ghost_panel.html 一个 `window.pywebview.api.*`
// 兼容桥。球窗渲染 ghost_panel.html（球帽 + 实时侧栏单窗），因此 live_panel.js
// 会用到的 9 个侧栏上行 + 球帽点击的 ballClicked 都要暴露。
//
// 注意：不能用 Proxy（contextBridge 无法克隆 Proxy，抛 "An object could not be
// cloned."），这里显式定义实际上行方法。
const { contextBridge, ipcRenderer } = require("electron");

function makeCall(method) {
  return (...args) => ipcRenderer.invoke("uplink", { method, args });
}

const api = {
  // 球帽点击 -> Python Api.ballClicked -> pool.ball_clicked（展开/收起面板）
  ballClicked: makeCall("ballClicked"),
  // --- live_panel.js 用到的 9 个侧栏上行（feature-detected，缺省降级） ---
  get_live_panel_api_key: makeCall("get_live_panel_api_key"),
  live_panel_layout: makeCall("live_panel_layout"),
  get_live_panel_layout_hint: makeCall("get_live_panel_layout_hint"),
  get_live_panel_min_cols: makeCall("get_live_panel_min_cols"),
  get_live_panel_tools_always: makeCall("get_live_panel_tools_always"),
  get_live_panel_list_font: makeCall("get_live_panel_list_font"),
  get_live_panel_concurrent: makeCall("get_live_panel_concurrent"),
  panel_close: makeCall("panel_close"),
  get_main_geometry: makeCall("get_main_geometry"),
};

contextBridge.exposeInMainWorld("pywebview", { api });

// ---- 拖拽桥（contextIsolation 下渲染进程摸不到 ipcRenderer，走这里） ----
// ghost_ball.html 的 mousedown/mouseup 调 ballDrag.start()/end()，主进程
// ball_main.js 的 ipcMain.on("drag-start"/"drag-end") 轮询光标判定拖动/点击。
// v0.184：start 带来源参数（"cap"=球帽点击/拖拽，"panel"=顶栏拖拽区）。
// 主进程 drag-end 无位移时：仅 "cap" 发 clicked（展开/收起切换），"panel"
// 无位移 = 顶栏点击，不发（否则点顶栏会误触发收起/展开）。
contextBridge.exposeInMainWorld("ballDrag", {
  start: (src) => ipcRenderer.send("drag-start", src),
  end:   () => ipcRenderer.send("drag-end"),
});
