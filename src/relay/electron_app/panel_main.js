// Electron 实时侧栏主进程：Python(ElectronPanelWindow) 通过 TCP socket JSON-RPC 驱动。
//
// 通道（每行一个 JSON，用顶层 kind 判别）：
//   Python -> Electron (TCP):
//     {kind:"call", seq, method, args:[...]}   请求-应答（Python 等待 reply）
//     {kind:"api-reply", token, result, error} 回填 renderer 的 uplink invoke
//   Electron -> Python (TCP):
//     {kind:"reply", seq, ok, result}          call 的应答
//     {kind:"event", name, args}               窗口事件 -> Python events
//     {kind:"api-request", token, call:{method,args}}  renderer invoke 转发给 Python Api
//     {kind:"log", level, message}             renderer console-message
//
// 为什么不用 stdin/stdout：Electron 作为 GUI 子进程，主进程 stdin 在 detached
// 启动时收不到管道的字节（echo|electron 测试证实）。改走 TCP loopback 最稳。
//
// 启动：electron.exe <本目录> --url=<file://...> --bg=<hex> --w=<px> --h=<px> --port=<n>
const { app, BrowserWindow, ipcMain, screen } = require("electron");
const path = require("path");
const net = require("net");
const fs = require("fs");
const os = require("os");

const ARGS = parseArgs(process.argv.slice(1));

// 把 userData 指到临时目录，避免默认 profile 被系统保护目录挡住
// （_ARGV 起在 CREATE_NO_WINDOW 下缺 userData 会报 "Unable to move the cache"）。
try {
  const ud = path.join(os.tmpdir(), "relay-panel-" + process.pid);
  fs.mkdirSync(ud, { recursive: true });
  app.setPath("userData", ud);
} catch (e) {}
const URL = ARGS.url;                    // live_panel.html 的 file:// URL（含 #theme=）
const BG = ARGS.bg || "#f5f5f7";
const INIT_W = parseInt(ARGS.w || "400", 10);
const INIT_H = parseInt(ARGS.h || "900", 10);
const PORT = parseInt(ARGS.port || "0", 10);

let win = null;
let loaded = false;                       // did-finish-load 后才允许 evaluate_js
let _apiSeq = 0;
let sock = null;                          // 到 Python 的 TCP 连接

// ---- 拖动状态机（复用 ball_main.js 的方案，主进程驱动） ----
// 渲染进程 mousedown 发 drag-start、mouseup 发 drag-end；主进程 12ms 轮询
// 光标，位移 ≥4px 判拖动并 setPosition + emitEvent("moved")，否则判点击。
// 只对「用户拖动」发 moved —— 程序化 setPosition（初始离屏 -32000、dock
// 定位、隐藏）不再回播 moved 事件，避免回灌 Python _apply_geometry 形成
// setPosition -> moved -> _apply_geometry -> setPosition 死循环/位置污染
// （v0.179 曾用 win.on("move") 直发，实测程序化定位也触发，移除）。
const PANEL_DRAG_PX = 4;
const PANEL_DRAG_MS = 12;
let panelDragTimer = null;
let panelDragStartCursor = null;
let panelDragStartWin = null;
let panelDragMoved = false;

// ---- 连接 ----
function connect() {
  sock = net.connect(PORT, "127.0.0.1", () => {
    console.log("[panel-main] connected to :" + PORT);
  });
  let buf = "";
  sock.setEncoding("utf8");
  sock.on("data", (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      let m;
      try { m = JSON.parse(line); } catch (e) { continue; }
      if (m.kind === "api-reply") resolveApi(m.token, m.result, m.error);
      else if (m.kind === "call") handleCall(m);
    }
  });
  sock.on("error", (e) => console.log("[panel-main] socket error " + e.message));
  sock.on("close", () => { sock = null; });
}

function send(obj) { if (sock) try { sock.write(JSON.stringify(obj) + "\n"); } catch (e) {} }
function reply(seq, ok, result) { send({ kind: "reply", seq, ok, result }); }
function emitEvent(name, args) { send({ kind: "event", name, args }); }
function apiRequest(token, call) { send({ kind: "api-request", token, call }); }

function handleCall(m) {
  if (m.method === "quit") { app.quit(); return; }
  try {
    const a = m.args || [];
    switch (m.method) {
      case "show": win.show(); reply(m.seq, true, null); break;
      case "hide": win.hide(); reply(m.seq, true, null); break;
      case "move": win.setPosition(int(a[0]), int(a[1])); reply(m.seq, true, null); break;
      case "resize": win.setSize(int(a[0]), int(a[1])); reply(m.seq, true, null); break;
      case "set_topmost": win.setAlwaysOnTop(!!a[0]); reply(m.seq, true, null); break;
      case "get_width":  reply(m.seq, true, Math.round(win.getContentBounds().width)); break;
      case "get_height": reply(m.seq, true, Math.round(win.getContentBounds().height)); break;
      case "get_x": reply(m.seq, true, Math.round(win.getPosition()[0])); break;
      case "get_y": reply(m.seq, true, Math.round(win.getPosition()[1])); break;
      case "evaluate_js":
        if (!loaded) { reply(m.seq, false, "not-loaded"); break; }
        win.webContents.executeJavaScript(String(a[0]))
          .then((r) => reply(m.seq, true, r))
          .catch((e) => reply(m.seq, false, String((e && e.message) || e)));
        break;
      default:
        reply(m.seq, false, "unknown-method:" + m.method);
    }
  } catch (e) {
    reply(m.seq, false, String((e && e.message) || e));
  }
}

function int(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : 0;
}

function createWindow() {
  win = new BrowserWindow({
    width: INIT_W,
    height: INIT_H,
    x: -32000,             // 初始离屏，Python 定位后再 show
    y: -32000,
    frame: false,
    transparent: false,     // 面板实心卡片，不做真透明
    resizable: true,
    hasShadow: false,
    alwaysOnTop: false,     // 顶置由 Python set_topmost 控制
    skipTaskbar: true,      // 去任务栏位（替代 _hide_panel_taskbar）
    show: false,
    backgroundColor: BG,
    webPreferences: {
      preload: path.join(__dirname, "panel_preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // 注意：不监听 win.on("move") —— 程序化 setPosition（初始离屏 -32000、
  // _apply_geometry dock 定位、隐藏）也会触发，会回灌 Python 造成
  // setPosition→moved→_apply_geometry→setPosition 死循环。moved 只由用户
  // 拖动时 tickDrag / drag-end 显式 emit（见下方拖动状态机）。
  win.on("resize", () => {
    if (!win) return;
    const b = win.getContentBounds();
    emitEvent("resized", [Math.round(b.width), Math.round(b.height)]);
  });
  win.on("closed", () => emitEvent("closing", []));
  win.webContents.on("did-finish-load", () => {
    loaded = true;
    emitEvent("loaded", []);
  });
  win.webContents.on("console-message", (e) => {
    try { send({ kind: "log", level: e.level, message: e.message }); } catch (_) {}
  });

  win.loadURL(URL);
}

// ---- renderer 上行 invoke -> Python Api ----
ipcMain.handle("uplink", (evt, payload) => {
  const { method, args } = payload || {};
  return new Promise((resolve, reject) => {
    const token = ++_apiSeq;
    if (!win._apiPending) win._apiPending = new Map();
    win._apiPending.set(token, { resolve, reject });
    apiRequest(token, { method, args });
  });
});

function resolveApi(token, result, error) {
  if (!win || !win._apiPending) return;
  const p = win._apiPending.get(token);
  if (!p) return;
  win._apiPending.delete(token);
  if (error != null) p.reject(new Error(String(error)));
  else p.resolve(result);
}

// ---- 拖动：mousedown 由渲染进程发起，主进程轮询光标跟手 ----
ipcMain.on("drag-start", () => {
  if (!win) return;
  stopPanelDragTimer();
  const cur = screen.getCursorScreenPoint();
  const [wx, wy] = win.getPosition();
  panelDragStartCursor = { x: cur.x, y: cur.y };
  panelDragStartWin = { x: wx, y: wy };
  panelDragMoved = false;
  panelDragTimer = setInterval(tickPanelDrag, PANEL_DRAG_MS);
});

function tickPanelDrag() {
  if (!win || !panelDragStartCursor || !panelDragStartWin) return;
  const cur = screen.getCursorScreenPoint();
  const dx = cur.x - panelDragStartCursor.x;
  const dy = cur.y - panelDragStartCursor.y;
  if (Math.hypot(dx, dy) >= PANEL_DRAG_PX) panelDragMoved = true;
  if (panelDragMoved) {
    win.setPosition(panelDragStartWin.x + dx, panelDragStartWin.y + dy);
    // 拖动中实时通知 Python（_on_panel_moved -> _update_panel_snap，用于
    // 贴右磁吸的拖回判定）。只有用户拖动才发。
    const [x, y] = win.getPosition();
    emitEvent("moved", [Math.round(x), Math.round(y)]);
  }
}

function stopPanelDragTimer() {
  if (panelDragTimer) { clearInterval(panelDragTimer); panelDragTimer = null; }
}

ipcMain.on("drag-end", () => {
  stopPanelDragTimer();
  panelDragMoved = false;
  panelDragStartCursor = null;
  panelDragStartWin = null;
  // 拖动结束后重新允许 moved（下一轮 drag-start 重新初始化）。
  setTimeout(() => { panelDragMoved = false; }, 0);
});

app.whenReady().then(() => {
  connect();
  createWindow();
  console.log("[panel-main] ready url=" + URL + " port=" + PORT);
});
app.on("window-all-closed", () => app.quit());

// ---- arg parse ----
function parseArgs(argv) {
  const out = {};
  for (const a of argv) {
    const m = String(a).match(/^--([^=]+)=(.*)$/);
    if (m) out[m[1]] = decodeURIComponent(m[2]);
  }
  return out;
}
