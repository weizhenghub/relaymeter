"""Generate ``electron_app/main_preload.js`` from ``web/app.js``.

主窗口 preload 桥不能像 pywebview 一样靠 Python 侧反射动态注入 ——
``contextBridge.exposeInMainWorld`` 无法克隆 Proxy，方法必须**显式枚举**。
本脚本 grep 出 app.js 真正调用的全部桥方法，生成 makeCall 表，防漂移。

提取源（两个都要，缺一不可）：
  1. ``_call("<method>", ...)`` 的首参 —— app.js 全部 93 个 wrapper 的落点。
  2. ``window.pywebview.api.<method>(...)`` 的直接调用 —— 跨过 ``_call`` 的
     特殊方法（``_diag_write_free_pos`` / ``opencode_*``）。

用法：
    python src/relay/electron_app/gen_preload.py              # 重新生成
    python src/relay/electron_app/gen_preload.py --check      # 校验无漂移（退出码）

每次改 app.js 增删桥方法后必须重跑一次（或让 GUI 启动时以 --check 校验）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]          # .../Usage_stats
_APP_JS = _ROOT / "src" / "relay" / "web" / "app.js"
_OUT = Path(__file__).resolve().parent / "main_preload.js"

# _call("method", ...) —— 允许方法名换行（\s 含 \n）
_RE_CALL = re.compile(r'_call\(\s*["\']([a-zA-Z0-9_]+)["\']')
# window.pywebview.api.method(...) 直接调用
_RE_DIRECT = re.compile(r'window\.pywebview\.api\.([a-zA-Z0-9_]+)')

_HEADER = """\
// 由 gen_preload.py 自动生成 —— 手工修改会被覆盖。
// 重跑：python src/relay/electron_app/gen_preload.py
//
// Electron 主窗口 preload：给 index.html 的 app.js 一个 `window.pywebview.api.*`
// 兼容桥。app.js 期望 `await window.pywebview.api.<name>(...)` 返回 Promise。
// 方法集 = web/app.js 里 `_call("…")` 首参 + 直接 `window.pywebview.api.X` 调用
// （gen_preload.py 提取，共 %(count)d 个）。每次 app.js 增删桥方法后重跑本脚本。
//
// 注意：**不能用 Proxy** —— contextBridge.exposeInMainWorld 无法克隆
// new Proxy({}, ...)（"An object could not be cloned."），必须显式枚举。
const { contextBridge, ipcRenderer } = require("electron");

function makeCall(method) {
  return (...args) => ipcRenderer.invoke("uplink", { method, args });
}

const api = {
%(methods)s};

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
"""


def extract_methods() -> set[str]:
    src = _APP_JS.read_text(encoding="utf-8")
    called = set(_RE_CALL.findall(src))
    direct = set(_RE_DIRECT.findall(src))
    return called | direct


def render(methods: set[str]) -> str:
    lines = []
    for name in sorted(methods):
        lines.append(f"  {name}: makeCall(\"{name}\"),\n")
    return _HEADER % {"count": len(methods), "methods": "".join(lines)}


def main() -> int:
    if not _APP_JS.exists():
        print(f"[gen_preload] MISSING {_APP_JS}", file=sys.stderr)
        return 1
    methods = extract_methods()
    if not methods:
        print("[gen_preload] no methods extracted — pattern drift?", file=sys.stderr)
        return 1
    rendered = render(methods)

    if "--check" in sys.argv:
        if _OUT.exists() and _OUT.read_text(encoding="utf-8") == rendered:
            print(f"[gen_preload] OK — {len(methods)} methods, no drift")
            return 0
        print(
            f"[gen_preload] DRIFT — app.js has {len(methods)} methods but "
            f"main_preload.js is out of date; rerun without --check",
            file=sys.stderr,
        )
        return 1

    # newline=""：不把 \n 转成 \r\n —— preload 是 LF 家族，混进 CRLF 会被
    # Electron/Node 解析时当不可见字符，且 git diff 会全行红。open 显式传，
    # 兼容旧版 Path.write_text 不支持 newline= 参数。
    with open(_OUT, "w", encoding="utf-8", newline="") as fh:
        fh.write(rendered)
    print(f"[gen_preload] wrote {_OUT.name} with {len(methods)} methods")
    return 0


if __name__ == "__main__":
    sys.exit(main())
