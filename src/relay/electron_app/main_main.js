// Electron 主窗口主进程：Python(ElectronMainWindow / ElectronAppDriver) 通过
// TCP socket JSON-RPC 驱动。Phase 1 = 主窗单独一个 electron.exe 进程树。
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
// 与 panel_main.js 的差异（主窗专属，Phase 1）：
//   * userData 指到 %LOCALAPPDATA%\Relay\electron-data（持久化 localStorage，
//     不能像面板那样丢 tmp —— relay-gui-prefs-v1 等必须跨重启保留）。
//   * 单窗、不透明、可缩放、显示在任务栏（skipTaskbar:false）。
//   * 新增窗口控制方法：minimize / maximize / restore / is_maximized /
//     close / set_bounds / set_icon / get_screen。
//   * close -> preventDefault + emit "closing"：Python _on_closing 决定
//     藏托盘还是真退出（替代 pywebview closing 返回 True 取消关闭）。
//   * quit -> quitRequested 置位后 app.quit()（Python destroy / 托盘退出）。
//   * 拖动状态机复用 panel_main.js 的 12ms 光标轮询（renderer 标题栏区域
//     mousedown -> drag-start、mouseup -> drag-end；仅用户拖动发 moved，
//     程序化 setPosition 不回播，避免位置污染/死循环）。
//
// 启动：electron.exe <本目录> --url=<file://...> --w=<px> --h=<px>
//       --port=<n> --user-data=<dir> --bg=<hex>
const { app, BrowserWindow, ipcMain, screen } = require("electron");
const path = require("path");
const net = require("net");
const fs = require("fs");

const ARGS = parseArgs(process.argv.slice(1));

// 任务栏图标归属：Windows 下没有 AppUserModelID 时 dev 态 electron 常显示
// 通用图标，提前设好让 set_icon 生效。
try { app.setAppUserModelId("RelayMeter"); } catch (e) {}

// userData 持久化 —— 必须在 ready 之前设。路径由 Python 传（Relay/electron-data），
// 兜底落到 appData，绝不回落 tmp。
if (ARGS["user-data"]) {
  try {
    fs.mkdirSync(ARGS["user-data"], { recursive: true });
    app.setPath("userData", ARGS["user-data"]);
  } catch (e) {}
} else {
  try {
    app.setPath("userData", path.join(app.getPath("appData"), "relay-meter"));
  } catch (e) {}
}
const URL = ARGS.url;                    // index.html 的 file:// URL
const BG = ARGS.bg || "#f5f5f7";
const INIT_W = parseInt(ARGS.w || "1180", 10);
const INIT_H = parseInt(ARGS.h || "900", 10);
const PORT = parseInt(ARGS.port || "0", 10);

let win = null;
let loaded = false;                      // did-finish-load 后才允许 evaluate_js
let quitRequested = false;               // true 后 close 不再 preventDefault
let _apiSeq = 0;
let sock = null;                         // 到 Python 的 TCP 连接

// ---- 拖动状态机（复用 panel_main.js 的方案，主进程驱动） ----
// renderer mousedown 发 drag-start、mouseup 发 drag-end；主进程 12ms 轮询
// 光标，位移 ≥4px 判拖动并 setPosition + emitEvent("moved")，否则判点击。
// 只对「用户拖动」发 moved —— 程序化 setPosition（初始离屏 -32000、Python
// 定位）不再回播 moved，避免位置污染/死循环。最大化时禁拖（否则 setPosition
// 会把最大化窗口拽回普通尺寸）。
const DRAG_PX = 4;
const DRAG_MS = 12;
let dragTimer = null;
let dragStartCursor = null;
let dragStartWin = null;
let dragMoved = false;

// ---- 连接 ----
function connect() {
  sock = net.connect(PORT, "127.0.0.1", () => {
    console.log("[main-main] connected to :" + PORT);
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
  sock.on("error", (e) => console.log("[main-main] socket error " + e.message));
  sock.on("close", () => { sock = null; });
}

function send(obj) { if (sock) try { sock.write(JSON.stringify(obj) + "\n"); } catch (e) {} }
function reply(seq, ok, result) { send({ kind: "reply", seq, ok, result }); }
function emitEvent(name, args) { send({ kind: "event", name, args }); }
function apiRequest(token, call) { send({ kind: "api-request", token, call }); }

function handleCall(m) {
  if (m.method === "quit") { quitRequested = true; reply(m.seq, true, null); app.quit(); return; }
  try {
    const a = m.args || [];
    switch (m.method) {
      case "show": win.show(); reply(m.seq, true, null); break;
      case "hide": win.hide(); reply(m.seq, true, null); break;
      case "move": win.setPosition(int(a[0]), int(a[1])); reply(m.seq, true, null); break;
      case "resize": win.setSize(int(a[0]), int(a[1])); reply(m.seq, true, null); break;
      // FixPoint 锚定缩放：Python 端算好新 x/y（EAST 保持右缘、SOUTH 保持
      // 下缘），一次 setBounds 到位，避免 setPosition+setSize 两次触发的
      // resize 中间态。
      case "set_bounds": win.setBounds({ x: int(a[0]), y: int(a[1]), width: int(a[2]), height: int(a[3]) }); reply(m.seq, true, null); break;
      case "set_topmost": win.setAlwaysOnTop(!!a[0]); reply(m.seq, true, null); break;
      case "set_icon": win.setIcon(String(a[0])); reply(m.seq, true, null); break;
      // reload_gui 开发工具：导航回 index.html（带 ?_dev=ts 时间戳）。did-finish-load
      // 会再触发一次 -> Python 重新收到 loaded 事件（幂等：重新 setTheme/probe）。
      case "load_url": win.loadURL(String(a[0])); reply(m.seq, true, null); break;
      case "minimize": win.minimize(); reply(m.seq, true, null); break;
      case "maximize": win.maximize(); reply(m.seq, true, null); break;
      case "restore": win.restore(); reply(m.seq, true, null); break;
      case "is_maximized": reply(m.seq, true, win.isMaximized()); break;
      case "close": win.close(); reply(m.seq, true, null); break;
      case "get_screen": {
        const s = screen.getPrimaryDisplay().size;
        reply(m.seq, true, { width: s.width, height: s.height });
        break;
      }
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
    title: "RelayMeter",
    frame: false,          // 无系统标题栏，HTML 自绘玻璃标题栏 + 拖动
    transparent: false,    // 主窗实心背景，不做真透明
    resizable: true,
    hasShadow: true,
    alwaysOnTop: false,    // 顶置由 Python set_topmost 控制
    skipTaskbar: false,    // 显示在任务栏
    show: false,
    // 定位：pywebview 下窗口位置由 webview.start() 自动居中；Electron 必须
    // 显式定位。⚠ 之前 x/y=-32000 离屏创建 + Python 只 show() 不移位 →
    // 窗口停在离屏，合成器不产帧（capturePage 报 UnknownVizError），窗口被
    // 可见化时只有白底 —— 全白屏根因。改 center:true 创建即落在主屏居中
    // （仍 show:false 防未加载闪烁，Python show 时才显示）。
    center: true,
    backgroundColor: BG,   // 与主题底色一致，避免载入前白/黑闪
    webPreferences: {
      preload: path.join(__dirname, "main_preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // v0.205：不节流后台定时器。教程/演示动画全走 setTimeout 节奏
      // （tour 定位 + 2s 切换 + 悬浮球展开），Chromium 默认对隐藏/最小化
      // 窗口把后台 timer 砍到 1Hz（甚至更狠），步骤切换/演示动画在真实
      // GUI 里卡住不动。关掉后 timer 照常按设定毫秒跑（无头探针已如此）。
      backgroundThrottling: false,
    },
  });

  // 主窗关闭不能直接销毁：Python 要决定「藏托盘」还是「真退出」。未请求
  // 退出时 preventDefault，把决定权交给 Python _on_closing（替代 pywebview
  // closing 返回 True 取消关闭的机制）。
  win.on("close", (e) => {
    emitEvent("closing", []);
    if (!quitRequested) e.preventDefault();
  });
  win.on("closed", () => emitEvent("closed", []));
  // 程序化 set_bounds（edge-resize 锚定）也触发 resize —— 这里照发 resized，
  // Python _dock_panel 重贴侧栏，这是期望行为（主窗 resize 侧栏跟随）。
  win.on("resize", () => {
    if (!win) return;
    const b = win.getContentBounds();
    emitEvent("resized", [Math.round(b.width), Math.round(b.height)]);
  });
  win.on("show", () => emitEvent("shown", []));
  win.on("minimize", () => emitEvent("minimized", []));
  win.on("restore", () => emitEvent("restored", []));
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
  if (!win || win.isMaximized()) return;   // 最大化禁止拖动
  stopDragTimer();
  const cur = screen.getCursorScreenPoint();
  const [wx, wy] = win.getPosition();
  dragStartCursor = { x: cur.x, y: cur.y };
  dragStartWin = { x: wx, y: wy };
  dragMoved = false;
  dragTimer = setInterval(tickDrag, DRAG_MS);
});

function tickDrag() {
  if (!win || !dragStartCursor || !dragStartWin) return;
  const cur = screen.getCursorScreenPoint();
  const dx = cur.x - dragStartCursor.x;
  const dy = cur.y - dragStartCursor.y;
  if (Math.hypot(dx, dy) >= DRAG_PX) dragMoved = true;
  if (dragMoved) {
    win.setPosition(dragStartWin.x + dx, dragStartWin.y + dy);
    // 拖动中实时通知 Python（_on_main_moved -> _dock_panel，主窗移动侧栏
    // 贴右跟随）。只有用户拖动才发。
    const [x, y] = win.getPosition();
    emitEvent("moved", [Math.round(x), Math.round(y)]);
  }
}

function stopDragTimer() {
  if (dragTimer) { clearInterval(dragTimer); dragTimer = null; }
}

ipcMain.on("drag-end", () => {
  stopDragTimer();
  dragMoved = false;
  dragStartCursor = null;
  dragStartWin = null;
  // 拖动结束后重新允许 moved（下一轮 drag-start 重新初始化）。
  setTimeout(() => { dragMoved = false; }, 0);
});

app.whenReady().then(() => {
  connect();
  createWindow();
  console.log("[main-main] ready url=" + URL + " port=" + PORT);
});
// 主窗藏托盘时窗口仍在，window-all-closed 只在真退出（quitRequested）时兜底退出。
app.on("window-all-closed", () => {
  if (quitRequested) app.quit();
});

// ---- arg parse ----
function parseArgs(argv) {
  const out = {};
  for (const a of argv) {
    const m = String(a).match(/^--([^=]+)=(.*)$/);
    if (m) out[m[1]] = decodeURIComponent(m[2]);
  }
  return out;
}
