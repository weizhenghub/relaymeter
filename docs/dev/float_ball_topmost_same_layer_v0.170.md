# 侧边栏与悬浮球同层级 + 悬浮球置顶（v0.170）开发文档

## 1. 用户的初始指令

> 1、侧边栏始终要和悬浮球在同一层级。
> 2、在设置中增加"悬浮球指置顶"

### 场景拆解

- **目标 1**：侧边栏与悬浮球恒同层级。悬浮球自 v0.165 起恒 `WS_EX_TOPMOST`（永远置顶），而侧栏此前从没置顶 —— 浮动(球)模式下侧栏解耦主窗（`RemoveOwnedForm`）后是普通桌面窗，会被其它窗口盖住，球却永远在最上面，两窗层级不一致。
- **目标 2**：设置页新增「悬浮球置顶」开关，控制球与侧栏的置顶行为（默认 ON = 保持 v0.169 现状）。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 侧栏与球恒同层级（要么一起置顶，要么一起降级） | 指令 1 |
| B | 设置页新增「悬浮球置顶」开关（默认 ON） | 指令 2 |
| C | 开关持久化（config + .env + 桥 + 设置页初始值） | 指令 2 |

### 隐含但需要确认的点

- 球当前**无条件**置顶（`ball_layer.py` 硬编码 `f.TopMost=True` + `WS_EX_TOPMOST`）。「悬浮球置顶」开关应控制球与侧栏**一起**置顶/降级 —— 保持「恒同层级」不变式。关闭 = 两者都降到普通层级。
- 侧栏置顶不能用 `panel_native.TopMost = False/True` 直接设 —— 对照 `_hide_panel_taskbar` 的教训：`ShowInTaskbar` setter 触发 RecreateHandle（句柄重建）会跨线程死锁。`TopMost` setter 走 `SetWindowPos`，本不重建句柄，但侧栏是 WebView2 子窗口，稳妥起见用纯 ctypes `SetWindowPos(HWND_TOPMOST/NOTOPMOST)` + `BeginInvoke` 异步派发到 UI 线程。
- 同步时机：所有走 `_sync_panel_owner` 的模式切换点（`set_float_ball` / `_dock_panel` / `_update_panel_snap` 两分支 / `_on_panel_loaded`）+ 新开关 setter。置顶同步要放在 bound 早退**之前** —— bound 状态没变也要重算置顶。

---

## 3. 分析需求后得出的开发路径

### 核心方案

**侧栏置顶恒等于「悬浮球置顶」开关**：球 `BallLayer.set_topmost(bool)` 运行时切换（构造参数 `topmost` 默认 True，保持 v0.165 行为）；侧栏 `_sync_panel_topmost()` 读 `relay_gui_float_ball_topmost`，`BeginInvoke` 派发 UI 线程 `SetWindowPos(HWND_TOPMOST/HWND_NOTOPMOST)`。两者要么一起置顶、要么一起降级。

### 开发路径

```
#1  config.py: relay_gui_float_ball_topmost: bool = True       (B/C)
#2  ball_layer.py: topmost 构造参数 + set_topmost 运行时切换     (A)
#3  panel_pool.py: _float_ball_topmost + set_float_ball_topmost (A/B)
#4  gui.py: get/set_float_ball_topmost 桥 + snapshot 键          (B/C)
#5  gui.py: _sync_panel_topmost + _sync_panel_owner 入口调用     (A)
#6  app.js: 设置页开关 + i18n + 桥 + change handler + init      (B/C)
#7  index.html: cache ?v=20260825-04 → ?v=20260825-05           (cache)
```

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 置顶载体 | 球 `f.TopMost` / `SetWindowPos`（WinForms 内部）；侧栏纯 ctypes `SetWindowPos(HWND_TOPMOST/NOTOPMOST)` | 一步同时改 WS_EX_TOPMOST 样式 + 重排 Z 序，不触发句柄重建 |
| 侧栏派发 | `BeginInvoke` 异步（绝不 `Invoke` 阻塞） | 历史 RecreateHandle 跨线程 SendMessage 死锁教训 |
| 同步时机 | `_sync_panel_owner` 入口无条件先调 `_sync_panel_topmost`（在 bound 早退之前） | 覆盖所有模式切换点，bound 未变也重算置顶 |
| 默认值 | True（球与侧栏继续置顶） | 行为与 v0.169 完全一致，只有用户显式关闭才降级 |
| 未建球 | 只记标志 `_float_ball_topmost`，`_create_ball` 用它初始化 | 建球在 webview.start 后异步，设置可能早于建球 |
| 范围限定 | 仅球 + 侧栏；主窗不动 | 需求只说球与侧栏同层级，不拉主窗 |

---

## 4. 实现中遇到的问题

### 问题 1（致命）：`int(panel_native.Handle)` 抛 TypeError → 侧栏从未置顶

初版 `_sync_panel_topmost` 用纯 ctypes `SetWindowPos`，取句柄写 `hwnd = int(panel_native.Handle)`。但 **pythonnet 3.x 的 `IntPtr` 不支持 `int()`**（实测 `int(IntPtr(12345))` → `TypeError: int() argument must be ... not 'IntPtr'`，必须 `ToInt64()` —— 这正是 `ball_layer._handle_int` 特意先 `h.ToInt64()` 的原因）。于是 `_apply` 每次都在取句柄那行抛异常、被 `except Exception` 吞掉 → 侧栏从未被置顶。用户实测确认：「悬浮球悬浮在 cmd 上面，侧边栏直接被 cmd 盖住」。

**解法**：弃用 ctypes 取句柄，改用 WinForms 属性 `panel_native.TopMost = bool(topmost)` —— 与球 `f.TopMost = True` 同一机制（本机已验证可用），无需碰句柄，天然规避 `int(IntPtr)`。且属性会更新 form 的 TopMost 缓存，跨 show/hide 持久；ctypes 改样式不更新缓存，后续 WinForms 操作可能把置顶刷掉。

### 问题 2：`TopMost` setter 会不会像 `ShowInTaskbar` 那样触发 RecreateHandle 死锁

`_hide_panel_taskbar` 的教训：WebView2 窗口 handle 已建且已显示时，`ShowInTaskbar = False` setter 触发 RecreateHandle（跨线程 SendMessage → UI 消息泵未泵 → 死锁）。但 `Form.TopMost` setter 内部只调 `SetWindowPos(HWND_TOPMOST/NOTOPMOST)`，**不重建句柄** —— 与球 `f.TopMost = True` 完全一致，本机已验证。仍走 `BeginInvoke` 异步派发（绝不 `Invoke` 阻塞，防死锁纪律不变）。

### 问题 3：dock 模式被误拔成置顶（行为回归风险）

初版 `_sync_panel_topmost` 无条件让侧栏置顶恒等于开关（默认 ON）→ 磁吸(dock)模式（无球）下侧栏也被拔成置顶，飘在所有窗口上面 —— 这是 v0.169 没有的行为回归。

**解法**：`float_mode = not self._panel_docked`（与 `set_float_ball` 里 `_panel_docked = not enabled` 口径一致），仅浮动(球)模式 + 开关 ON 才置顶；dock 模式恒不置顶（侧栏跟随主窗 owned 层级）。

### 问题 4：置顶同步点分散

`_sync_panel_owner` 在 `set_float_ball` / `_dock_panel` / `_update_panel_snap` 两分支 / `_on_panel_loaded` 都有调用，但 dock/float 切换不必然改变 bound 状态（例如 dock 态重 dock），若只在 bound 变化时同步置顶会漏。

**解法**：`_sync_panel_topmost()` 放在 `_sync_panel_owner` 的 bound 早退**之前** —— bound 是否变化都先重算置顶。这样所有走 `_sync_panel_owner` 的路径自动带上置顶同步，无需逐个 call site 加。

### 问题 5：球 Z 序被侧栏盖住（侧栏层级反而在球之上）

置顶同步生效后，侧栏被 pywebview `resize()` 用 `hWndInsertAfter=NULL`（=HWND_TOP）反复抬到 TopMost band 顶部；而球 `WS_EX_NOACTIVATE` 永不主动抬顶 → 侧栏反盖球（用户反馈「侧边栏层级反而在悬浮球之上，直接盖住了它」）。

**解法**：`ball_layer.py` 新增 Z 序保活定时器 `_topkeep_timer`（WinForms Timer, 300ms → `_topkeep_tick` → `SetWindowPos(HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)`），球恒居 TopMost band 顶，侧栏再 resize 也压不过球。`set_topmost(bool)`/`destroy()` 联动启停。

### 问题 6：侧栏启动白屏（老生常谈）+ 整页白屏无 UI（ERR_FILE_NOT_FOUND 回归）

「启动白，有内容恢复」—— `live_panel.html` 静态默认 `<html data-theme="light">`，深色/日间主题下侧栏首帧渲染用 light 的 `--root-bg: #f5f5f7`（近白），直到 `_on_panel_loaded` 的异步 `setTheme` 才切到正确底色（dark=#0a0a0a / day=#fdf6ec）。

**解法**：`gui.py` 构造 `live_panel_url` 时追加 `#theme=<当前主题>`；`live_panel.html` 头部加同步脚本读 hash 参数、首帧前设 `data-theme`（先于 `<link>`，CSSOM 生效前 data-theme 已就位）。页面第一帧就用正确主题色，不再白到异步 setTheme 才恢复。

**⚠ 回归教训**：初版曾把主题参数写成顶层 URL 的 `?query`（`live_panel.html?theme=day&v=…`）→ WebView2 对顶层 `file://` 导航把 `?query` 当**文件系统路径** → `ERR_FILE_NOT_FOUND` → **整页白屏完全无 UI**（用户截图实锤，正是「启动白屏无ui侧栏」）。**顶层 `file://` 导航不可带 `?query`**，必须用 `#fragment`。子资源 query（`<script src="...?v=NNN">` / `<link href="...?v=NNN">`）不受此限—— 它们是相对子资源，不是顶层导航，照常做缓存 bust。

### 问题 7：`SetWindowPos.argtypes` 毒害共享 user32 → 侧栏不跟随球 + 主窗无法拖动

Z 序保活 `_topkeep_tick` 需要 `SetWindowPos(HWND_TOPMOST, ...)`，初版在**共享** `ctypes.windll.user32` 实例上设了 `SetWindowPos.argtypes`（cx/cy 声明成 `c_int`）。但 pywebview 的 `winforms.py` `move()` 也走同一个 `windll.user32.SetWindowPos`，它对 cx/cy 传 `None`（依赖 ctypes **未设 argtypes** 的宽松转换）→ 一旦在共享实例上设了 argtypes，pywebview 每次 `ww.move()` 都抛 `ctypes.ArgumentError: argument 5: NoneType` → 侧栏所有 move 失败、卡死固定位置不跟随球（用户反馈「侧边栏又没跟随悬浮窗了，直接卡在固定位置」）；主窗/侧栏都是 frameless + easy_drag，拖动靠 pywebview `move()` → 主窗同样无法拖动（用户反馈「主窗口没法拖动移动位置了」）。

**解法**：`_load_user32()` 改用**私有** `ctypes.WinDLL("user32", use_last_error=True)` 实例（模块级 `_user32` 缓存）。argtypes 只作用于球自己的 `SetWindowPos(HWND_TOPMOST)` 调用（传整数 0，`c_int` 合法），不再毒害 pywebview 共享的 `windll.user32`。已验证：`ctypes.WinDLL("user32")` 与 `ctypes.windll.user32` 是不同 CDLL 对象、argtypes 互不影响。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| `int(IntPtr)` TypeError → 侧栏没置顶 | 改用 `panel_native.TopMost = bool` 属性（同球机制，不碰句柄） | gui.py |
| TopMost setter 死锁风险 | `Form.TopMost` 只走 SetWindowPos 不重建句柄 + `BeginInvoke` 异步 | gui.py |
| dock 模式被误拔置顶 | `float_mode = not self._panel_docked` gate，dock 恒不置顶 | gui.py |
| 同步点分散 | `_sync_panel_owner` 入口无条件先调 `_sync_panel_topmost`（在 bound 早退之前） | gui.py |
| 球被侧栏盖住 | ball_layer `_topkeep_timer` 300ms `SetWindowPos(HWND_TOPMOST)` 保活 | ball_layer.py |
| SetWindowPos.argtypes 毒害共享 user32 → 侧栏不跟随球 + 主窗无法拖动 | `_load_user32` 改私有 `WinDLL("user32")`，argtypes 隔离不毒害 pywebview | ball_layer.py |
| 侧栏启动白屏 | 侧栏 URL 带 `#theme=` + 头部同步脚本首帧设 `data-theme` | gui.py / live_panel.html |

---

## 6. 是否完全遵循规划路径开发

**完全遵循**，无方案级偏离。

- 实施策略 = 侧栏置顶恒等于开关 + 球运行时切换，与规划一致。
- 默认值 = True（保持 v0.169 行为），与规划一致。
- 同步时机 = `_sync_panel_owner` 入口 + setter，与规划一致。
- 载体 = 球 `f.TopMost`/`SetWindowPos` + 侧栏 ctypes `SetWindowPos`，与规划一致。

### 补充决策（规划未明说）

- **侧栏置顶用 WinForms `TopMost` 属性而非 ctypes**：规划时担心 RecreateHandle 想用 ctypes；实测 `int(IntPtr)` 直接 TypeError 让 ctypes 路线静默失败，且 `Form.TopMost` setter 本就只走 SetWindowPos 不重建句柄 → 最终用属性（与球完全一致），更简单更稳。
- **浮动模式 gate**：规划只说"球与侧栏恒同层级"，没明确 dock 模式；补了 `not _panel_docked` gate，dock 模式不被「悬浮球置顶」误拔成置顶。
- **`_sync_panel_topmost` 每次真派发**：规划未明说；属性重复设同态无副作用，为省复杂度不缓存 last 态，每次直接 BeginInvoke（开销极小）。

### 重大调整：无。

---

## 7. 最终实现点

### `src/relay/config.py`

1. **`relay_gui_float_ball_topmost: bool = True`**：悬浮球置顶（球与侧栏一起保持 `WS_EX_TOPMOST`）。`.env` 键 `RELAY_GUI_FLOAT_BALL_TOPMOST=1/0`。

### `src/relay/ball_layer.py`

2. **构造器 `topmost=True` 参数**：`_create_and_show` 条件设 `f.TopMost` + `WS_EX_TOPMOST`（不再硬编码）。
3. **`set_topmost(bool)` 公开 API**：锁内改 `_topmost` + dispatch `_apply_topmost`（UI 线程 `f.TopMost = True/False`，WinForms 内部走 `SetWindowPos`，不重建句柄）。
4. **Z 序保活 `_topkeep_timer`**（修复球被侧栏盖住）：300ms WinForms Timer，`_topkeep_tick` 调 `SetWindowPos(HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)`，球恒居 TopMost band 顶。`_create_and_show` 若 `_topmost` 则 `_topkeep_start()`；`_apply_topmost` 联动启停；`destroy`/`_teardown` 停。
5. **`_load_user32` 用私有 `ctypes.WinDLL("user32", use_last_error=True)`**（修复 argtypes 毒害 pywebview）：模块级 `_user32` 缓存，`UpdateLayeredWindow`/`SetWindowPos`/`WindowFromPoint` 等 argtypes 只设在这个私有实例上；pywebview `move()` 走共享 `windll.user32.SetWindowPos` 且对 cx/cy 传 `None`，共享实例保持无 argtypes 的宽松转换，不被毒害。

### `src/relay/panel_pool.py`

4. **`_float_ball_topmost`**：构造时从 settings 读（默认 True）。
5. **`_create_ball` 传 `topmost=self._float_ball_topmost`** 给 BallLayer。
6. **`set_float_ball_topmost(enabled)`**：锁内改标志；已建球则 `ball.set_topmost(enabled)`（未建只记标志）。

### `src/relay/gui.py`

7. **桥 `get_float_ball_topmost` / `set_float_ball_topmost`**：仿 `get/set_live_panel_frameless`，`update_env_var` 写 `.env`；setter 里 `pool.set_float_ball_topmost` + `_sync_panel_topmost()`。
8. **`_sync_panel_topmost()`**：`float_mode = not self._panel_docked` 判浮动；仅浮动 + `relay_gui_float_ball_topmost` ON 才置顶。`BeginInvoke` 派发 UI 线程 `panel_native.TopMost = bool(topmost)`（WinForms 属性，与球 `f.TopMost` 同机制；**不碰句柄，规避 `int(IntPtr)` TypeError**）。
9. **`_sync_panel_owner` 入口无条件先调 `_sync_panel_topmost`**（放在 bound 早退之前）—— 所有模式切换点自动带上。
10. **snapshot 加 `"float_ball_topmost"` 键**（设置页首帧初始值）。
11. **侧栏 URL 带 `#theme=`**（修复启动白屏，⚠ 必须 hash 不可 query）：`live_panel_url = live_panel.html.as_uri() + "#theme=" + self.theme_name`。

### `src/relay/web/live_panel.html`

12. **头部同步脚本**（修复启动白屏）：首帧前读 `location.hash` 的 `#theme=` 设 `document.documentElement.setAttribute("data-theme", t)`，先于 `<link>` 生效。

### `src/relay/web/app.js`

13. **设置页悬浮球开关下加「悬浮球置顶」toggle**：`prefs-live-panel-float-ball-topmost`，`${snap && snap.float_ball_topmost !== false ? "checked" : ""}`（默认显示 ON）。
14. **i18n 四语言**（zh/en/zh-TW/ja/ko）：`悬浮球置顶` / `悬浮球与侧栏始终置顶，不被其它窗口遮挡`。
15. **桥 `getFloatBallTopmost` / `setFloatBallTopmost`** + change handler + `refreshPrefsDynamic` 初始值。

### `src/relay/web/index.html`

16. **cache**：`app.js?v=20260825-04` → `?v=20260825-05`。

### ⚠ 关键约束（底层，勿再踩）

- **侧栏置顶用 WinForms `TopMost` 属性，不要 ctypes `SetWindowPos` + `int(Handle)`**：pythonnet 3.x 的 `IntPtr` 不支持 `int()`（抛 TypeError，实测确认）—— 这是 v0.170 初版「球置顶、侧栏没置顶」的根因。取句柄一律 `h.ToInt64()`（见 `ball_layer._handle_int`），或干脆用属性不碰句柄。
- **`Form.TopMost` setter 不触发 RecreateHandle**：内部只走 `SetWindowPos`，与球 `f.TopMost = True` 同一机制（本机已验证）。会重建句柄的是 `ShowInTaskbar`（见 `_hide_panel_taskbar` 死锁教训）。一切窗体操作仍走 `BeginInvoke` 异步。
- **仅浮动(球)模式才置顶**：dock 模式侧栏是主窗 owned 窗体，跟随主窗层级；用 `not self._panel_docked` gate，否则 dock 侧栏被「悬浮球置顶」误拔成置顶（行为回归）。
- **置顶同步必须在 bound 早退之前**：否则 dock/float 切换但 bound 未变时，侧栏置顶不跟随球。
- **默认 True**：行为与 v0.169 一致（球恒置顶、侧栏 dock 态随主窗）；只有用户显式关掉「悬浮球置顶」才让两者一起降级。
- **球未建时 set 只记标志**：建球在 webview.start 后异步，设置可能早于建球；`_create_ball` 用标志初始化。
- **球 Z 序需保活**：`WS_EX_NOACTIVATE` 的球永不自动抬顶，pywebview `resize()` 的 `HWND_TOP` 会把侧栏抬到 band 顶反盖球 —— 必须周期 `SetWindowPos(HWND_TOPMOST)` keep-alive（300ms 开销极小）。
- **侧栏白屏根因是首帧主题未就位**：`<html data-theme>` 静态默认 light，深色/日间先美白到异步 `setTheme`。URL 带 `#theme=` + 头部同步脚本可让第一帧就用正确主题。
- **⚠ 顶层 `file://` 导航不可带 `?query`**：WebView2/Chromium 把 query 当文件系统路径 → `ERR_FILE_NOT_FOUND` → 整页白屏无 UI（v0.170 实测回归）。传启动参数一律用 `#fragment`（客户端读 `location.hash`）。子资源 query（`<script/?v=NNN>`、`<link/?v=NNN>`）是相对子资源、非顶层导航，照常可用做缓存 bust。
- **⚠ `ctypes.windll.user32` 是共享实例，绝不在上面设 `argtypes`**：pywebview 的 `winforms.py` `move()`/`resize()` 走同一个 `windll.user32.SetWindowPos`，它对 cx/cy 传 `None`（依赖未设 argtypes 的宽松转换）。一旦设了 `SetWindowPos.argtypes`（cx/cy 声明 `c_int`），pywebview 每次 move 都抛 `ctypes.ArgumentError: argument 5: NoneType` → 侧栏/主窗（frameless easy_drag 都靠 move）全部无法移动。要设 argtypes 的 Win32 调用一律走**私有** `ctypes.WinDLL("user32", use_last_error=True)` 实例（见 `ball_layer._load_user32`）。

### 行为验收清单（手动测试项）

- [ ] 开悬浮球 → 侧栏浮动展开 → 打开一个全屏/高窗口盖住侧栏位置 → 侧栏仍在最上面（与球同层级）
- [ ] 关「悬浮球置顶」→ 球与侧栏一起被高窗口盖住（一起降级）
- [ ] 重开「悬浮球置顶」→ 两者一起回到最上面
- [ ] 磁吸(dock)模式开/关「悬浮球置顶」→ 侧栏贴主窗右缘跟随主窗层级不变（主窗置顶逻辑不受影响）
- [ ] 重启 GUI → 开关状态保留（`RELAY_GUI_FLOAT_BALL_TOPMOST` 写 .env）
- [ ] 深色/日间主题重启 → 侧栏首帧即是正确底色，无「先白后变」闪烁
- [ ] 球盖侧栏、侧栏盖球交替 resize → 球恒在侧栏之上（Z 序保活生效）
- [ ] `py_compile` 修改的 Python 文件 + `node --check` app.js 通过

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（+4 行：`relay_gui_float_ball_topmost`） |
| `src/relay/ball_layer.py` | 改（构造器 `topmost` 参数 + `set_topmost`/`_apply_topmost` + `_create_and_show` 条件置顶 + Z 序保活 `_topkeep_timer` + `_load_user32` 私有 WinDLL） |
| `src/relay/panel_pool.py` | 改（`_float_ball_topmost` + `_create_ball` 传参 + `set_float_ball_topmost`） |
| `src/relay/gui.py` | 改（桥 2 个 + `_sync_panel_topmost` + `_sync_panel_owner` 入口调用 + snapshot 键 + 侧栏 URL `#theme=`） |
| `src/relay/web/app.js` | 改（设置页开关 + i18n + 桥 + change handler + init） |
| `src/relay/web/index.html` | 改（JS cache `?v=20260825-04` → `?v=20260825-05`） |
| `src/relay/web/live_panel.html` | 改（头部同步脚本设 `data-theme`） |
