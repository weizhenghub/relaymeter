// Electron 实时侧栏 preload：给 live_panel.js 一个 `window.pywebview.api.*` 的
// 兼容桥。live_panel.js 期望 `await window.pywebview.api.<name>(...)` 返回
// Promise。它只 feature-detected 调用 9 个方法（其余调用由 live_panel.js 的
// `if (window.pywebview && ...)` / `.catch` 自动降级）。
//
// 注意：**不能用 Proxy** —— contextBridge.exposeInMainWorld 无法克隆一个
// new Proxy({}, ...)，会抛 "An object could not be cloned." 导致整个 preload
// 加载失败。因此这里显式定义侧栏真正用到的 9 个上行方法。
const { contextBridge, ipcRenderer } = require("electron");

function makeCall(method) {
  return (...args) => ipcRenderer.invoke("uplink", { method, args });
}

const api = {
  // --- 侧栏 renderer 实际调用的 9 个上行 ---
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
// live_panel.html 的拖拽区（窗口顶部）mousedown/mouseup 调 panelDrag.start()/end()，
// 主进程 panel_main.js 的 ipcMain.on("drag-start"/"drag-end") 轮询光标跟手。
contextBridge.exposeInMainWorld("panelDrag", {
  start: () => ipcRenderer.send("drag-start"),
  end:   () => ipcRenderer.send("drag-end"),
});
