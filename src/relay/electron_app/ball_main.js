// Electron 悬浮球主进程：Python(ElectronBallWindow) 通过 TCP socket JSON-RPC 驱动。
//
// 透明 + 点击穿透（复用 scratch/electron_ball 已验证配方）：
//   * window 参数：transparent:true, frame:false, hasShadow:false  => 真透桌面
//   * setIgnoreMouseEvents(bool, {forward:true})  => 球内吞点击 / 球外穿透
//
// 通道（panel_main.js 同构，顶层 kind 判别）：
//   Python -> Electron (TCP):
//     {kind:"call", seq, method, args:[...]}
//     {kind:"api-reply", token, result, error}
//   Electron -> Python (TCP):
//     {kind:"reply", seq, ok, result}
//     {kind:"event", name, args}
//     {kind:"api-request", token, call:{method,args}}
//     {kind:"log", level, message}
//
// 启动：electron.exe <本目录> --url=<file://...> --w=<px> --h=<px> --port=<n>
const { app, BrowserWindow, ipcMain, screen } = require("electron");
const path = require("path");
const net = require("net");
const fs = require("fs");
const os = require("os");

const ARGS = parseArgs(process.argv.slice(1));

try {
  const ud = path.join(os.tmpdir(), "relay-ball-" + process.pid);
  fs.mkdirSync(ud, { recursive: true });
  app.setPath("userData", ud);
} catch (e) {}

const URL = ARGS.url;
const INIT_W = parseInt(ARGS.w || "56", 10);
const INIT_H = parseInt(ARGS.h || "56", 10);
const PORT = parseInt(ARGS.port || "0", 10);

let win = null;
let loaded = false;
let _apiSeq = 0;
let sock = null;

// 悬停/穿透开关（复用 electron_ball 的 hover 轮询思路）：光标在球窗矩形内时
// 吞点击（可拖动/点击），在球外穿透（不挡桌面）。拖动中不切穿透。
const HOVER_MS = 25;
let hoverTimer = null;

// ---- 拖动 / 点击状态机（electron_ball 同款，主进程驱动） ----
const DRAG_PX = 4;
const DRAG_MS = 12;
let dragTimer = null;
let dragStartCursor = null;
let dragStartWin = null;
let dragMoved = false;
let dragStartSrc = "cap";  // v0.184："cap"=球帽（无位移=切换），"panel"=顶栏（无位移=无操作）

// ---- 连接 ----
function connect() {
  sock = net.connect(PORT, "127.0.0.1", () => {
    console.log("[ball-main] connected to :" + PORT);
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
  sock.on("error", (e) => console.log("[ball-main] socket error " + e.message));
  sock.on("close", () => { sock = null; });
}

function send(obj) { if (sock) try { sock.write(JSON.stringify(obj) + "\n"); } catch (e) {} }
function reply(seq, ok, result, error) { send({ kind: "reply", seq, ok, result, error }); }
function emitEvent(name, args) { send({ kind: "event", name, args }); }
function apiRequest(token, call) { send({ kind: "api-request", token, call }); }

function int(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : 0;
}

function setIgnore(wantIgnore) {
  if (!win) return;
  if (win._ignore === wantIgnore) return;
  // 拖动 / 点击中不切穿透（避免中断交互），由 Python 侧在合适时机调 set_ignore。
  win.setIgnoreMouseEvents(wantIgnore, { forward: true });
  win._ignore = wantIgnore;
}

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
      case "reload":
        // v0.203：重载渲染层 —— 设置页切「实时流侧栏」开关时重载
        // ghost_panel.html（HTML/CSS 改动即时生效，不重建窗口）。
        win.webContents.reload();
        reply(m.seq, true, null);
        break;
      case "set_ignore": setIgnore(!!a[0]); reply(m.seq, true, null); break;
      case "set_expanded":
        // v0.184：展开态（球+侧栏 / 磁吸 dock）—— 停 hover 穿透循环 + 全窗口
        // 吞点击（侧栏要交互）；收起态恢复 hover 判定（球帽内吞、球外穿）。
        if (!!a[0]) { stopHoverLoop(); setIgnore(false); }
        else { startHoverLoop(); }
        reply(m.seq, true, null);
        break;
      case "get_width":  reply(m.seq, true, Math.round(win.getBounds().width)); break;
      case "get_height": reply(m.seq, true, Math.round(win.getBounds().height)); break;
      case "get_x": reply(m.seq, true, Math.round(win.getPosition()[0])); break;
      case "get_y": reply(m.seq, true, Math.round(win.getPosition()[1])); break;
      case "get_hwnd":
        { const b = win.getNativeWindowHandle(); const v = b.readInt32LE(0); reply(m.seq, true, v); }
        break;
      case "evaluate_js":
        if (!loaded) { reply(m.seq, false, "not-loaded"); break; }
        win.webContents.executeJavaScript(String(a[0]))
          .then((r) => reply(m.seq, true, r))
          .catch((e) => reply(m.seq, false, String((e && e.message) || e)));
        break;
      case "capture":
        win.webContents.capturePage()
          .then((img) => reply(m.seq, true, img.toPNG().toString("base64")))
          .catch((e) => reply(m.seq, false, String((e && e.message) || e)));
        break;
      default:
        reply(m.seq, false, "unknown-method:" + m.method);
    }
  } catch (e) {
    reply(m.seq, false, String((e && e.message) || e));
  }
}

function createWindow() {
  win = new BrowserWindow({
    width: INIT_W,
    height: INIT_H,
    x: -32000,             // 初始离屏，Python 定位后再 show
    y: -32000,
    frame: false,
    transparent: true,      // 真透桌面（electron_ball 已验证配方）
    // v0.184：容器要在收起(56×56) / 展开(球+侧栏) / 磁吸 dock 之间反复
    // resize —— Windows 下 transparent + resizable:false 窗口二次 setSize
    // 会静默失败（首次大改生效后不再变）。必须 resizable:true。
    resizable: true,
    hasShadow: false,
    alwaysOnTop: true,      // 顶置由 Python set_topmost 控制（default true）
    skipTaskbar: true,
    show: false,
    backgroundColor: "#00000000", // 全透明底
    webPreferences: {
      preload: path.join(__dirname, "ball_preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // 不监听 win.on("move") 回播 —— 程序化 setPosition（初始离屏/refresh_geometry）
  // 也会触发，会污染 Python 侧 _pos（被初始 -32000 覆盖）。moved 只由用户拖动
  // 时 tickDrag / drag-end 显式 emit。
  win.on("closed", () => emitEvent("closing", []));
  win.webContents.on("did-finish-load", () => {
    loaded = true;
    emitEvent("loaded", []);
  });
  win.webContents.on("console-message", (e) => {
    try { send({ kind: "log", level: e.level, message: e.message }); } catch (_) {}
  });

  win.loadURL(URL);

  // 初始穿透 —— 球被拖动/点击时，Python 侧会 set_ignore(false) 打开吞点击。
  // 开局先穿透，等 python evaluate_js 触达 ready 后由 hover 循环接管。
  setIgnore(true);
  startHoverLoop();
}

// ---- 悬停判定：光标在窗口矩形内 => 吞点击，在外 => 穿透 ----
// 这是 electron_ball 的核心交互：真正的"球内点击球外穿透"由窗口矩形判定。
function startHoverLoop() {
  if (hoverTimer) return;
  hoverTimer = setInterval(hoverTick, HOVER_MS);
}

function stopHoverLoop() {
  if (hoverTimer) { clearInterval(hoverTimer); hoverTimer = null; }
}

function hoverTick() {
  if (!win) return;
  if (dragMoved || dragTimer) return;   // 拖动中不切穿透
  const b = win.getBounds();
  const cur = screen.getCursorScreenPoint();
  const inside = cur.x >= b.x && cur.x < b.x + b.width && cur.y >= b.y && cur.y < b.y + b.height;
  setIgnore(!inside);
}

// ---- 拖动：mousedown 由渲染进程发起，主进程轮询光标跟手 ----
ipcMain.on("drag-start", (e, src) => {
  if (!win) return;
  stopDragTimer();
  dragStartSrc = (src === "panel") ? "panel" : "cap";
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
    // 拖动中实时通知 Python 侧栏跟随（_ball_drag -> _relayout）
    const [x, y] = win.getPosition();
    emitEvent("moved", [Math.round(x), Math.round(y)]);
  }
}

function stopDragTimer() {
  if (dragTimer) { clearInterval(dragTimer); dragTimer = null; }
}

ipcMain.on("drag-end", (e) => {
  stopDragTimer();
  const clicked = !dragMoved;   // 无位移 = 点击
  const [x, y] = win ? win.getPosition() : [0, 0];
  const src = dragStartSrc;
  dragStartSrc = "cap";
  dragMoved = false;
  dragStartCursor = null;
  dragStartWin = null;
  if (clicked) {
    // 纯点击：仅球帽（"cap"）算「切换」—— 发 TCP clicked 事件回 Python
    // （pool.ball_clicked 切 收起/展开）。顶栏点击无位移 = 无操作，不发。
    if (src === "cap") emitEvent("clicked", [Math.round(x), Math.round(y)]);
  } else {
    // 拖动结束：发 dragend 事件（Python _persist_ball_pos 落盘回写 settings）
    emitEvent("dragend", [Math.round(x), Math.round(y)]);
  }
  // 拖动结束后重新开始悬停判定
  setTimeout(() => { dragMoved = false; }, 0);
});

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

app.whenReady().then(() => {
  connect();
  createWindow();
  console.log("[ball-main] ready url=" + URL + " port=" + PORT);
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
