# 侧栏自动显隐 + 任务栏去占位 + 协议分布重命名（v0.164）开发文档

## 1. 用户的初始指令

> 1、总览页"平台分布"改为"协议分布"
> 2、侧栏在任务栏中不单独占位
> 3、关闭"始终开启一个（即使无请求）"后，侧栏就不会弹出来了，但是正确的行为是当有请求流时侧栏出现，请求均结束后消失。
>
> （实现后）GUI 卡死哩

### 场景拆解

- **需求 1**：总览卡片「平台分布」改名「协议分布」。v0.143 起该卡已按客户端入口 wire 拆分，名字滞后于语义。
- **需求 2**：侧栏是独立 WebView2 顶层窗口，Windows 任务栏上出现第二个按钮，用户要求只占一个（主窗）。
- **需求 3**：关闭「始终开启」后侧栏完全不弹。期望：有请求流时出现，请求均结束后消失。
- **卡死事故**：需求 2 第一版实现 `panel_native.ShowInTaskbar = False` 直接触发 GUI 假死（`Responding: False`）。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 总览卡片「平台分布」→「协议分布」（CARD_DEFS + i18n + 静态标题） | 指令 1 |
| B | 侧栏不占独立任务栏位（只保留主窗一个任务栏按钮） | 指令 2 |
| C | 关闭「始终开启」后：无请求不显示、有请求流出现、请求均结束消失 | 指令 3 |
| D | GUI 不能因为任务栏修复卡死（回归护栏） | 事故 |

### 隐含但需要确认的点（用户没说，要追问）

- 无新增提问：三个需求语义明确，用户按「直接改」推进；卡死是事故现场，需当场定位修复。

---

## 3. 分析需求后得出的开发路径

### 需求 1（重命名）：纯前端文案

- `app.js` CARD_DEFS 标题 + 4 个 i18n 字典（en / zh-TW / ja / ko）。zh 走 `t()` 回退源串，无需自映射。
- `index.html` 静态标题 + `_probe_persistence.html`（调试副本命中同文案）一并改。
- cache 版本 `?v=` bump（app.js/index.html 改动生效）。

### 需求 3（自动显隐）：根因有两层，分两条路径

**根因 A：`always_one_manually_hidden` 粘滞**

- v0.153 只修了「关开关」路径：`enforce_always_one` 关开关时只 hide、不置 `always_one_manually_hidden`。
- 但**用户手动 X 过侧栏**（`hide_panel` / `_on_always_one_closing` 置 flag=True）后再关「始终开启」→ flag 仍为 True → `assign()`（新请求流）调 `_show_always_one()` 被 `_apply_geometry` 的 `if self._all_hidden or self.always_one_manually_hidden: return False` 拦死 → 侧栏永不弹出。
- 修复：`assign()` 里「始终开启 OFF（自动显隐模式）下清掉该 flag」——手动 X 只对 ON 的持久面板生效（X 掉就一直关着），自动模式下 X 只是「本次先收起来」，下个请求流照常弹出。

**根因 B：v0.125「无条件展开」覆盖启动行为**

- v0.125 用户要求「忽略所有实时流栏相关设置，启动即强制弹出」——这覆盖了「始终开启 OFF → 无请求不显示」的预期。
- 修复：`start()` 恢复按开关控制：ON → 启动即显示（持久面板）；OFF → 不显示，首个请求流到达时由 `assign()` 弹出。

**根因 C：请求结束隐藏时机**

- 原逻辑靠 watchdog 每 10s tick 扫空闲隐藏，最后一个请求结束后侧栏最多多挂 10s。
- 修复：`_clear_rid()` 里当最后一个 rid 清掉后立即 `_hide_always_one()`（自动显隐模式），实现「请求均结束后消失」即时生效。

### 需求 2（任务栏去占位）：第一版踩坑，第二版换实现

**第一版（事故）**：`panel_native.ShowInTaskbar = False` 直接设 → GUI 卡死。

**根因（py-spy 实锤）**：
- UI 线程卡在 `winforms.py create_window` 的 `browser.Show()`（WebView2 初始化），**消息泵尚未启动**。
- `_on_panel_loaded` 跑在 WebView2 的 execute 线程，执行到 `ShowInTaskbar = False`。
- 该 setter 在窗口 handle 已创建（BrowserForm 构造即建）且已显示后设置 → 触发 WinForms `RecreateHandle`（重建句柄）→ 需回主 UI 线程执行。
- UI 线程没在泵消息 → RecreateHandle 的跨线程 SendMessage 无人处理 → execute 线程永久阻塞 → **双向死锁，GUI 假死**。py-spy 栈：Thread-11 卡 `gui.py:3820`（`ShowInTaskbar = False` 那行），MainThread 卡 `winforms.py:834 create_window`。

**第二版（修复）**：`_hide_panel_taskbar()` —— BeginInvoke 异步投递 + Win32 SetWindowLongPtrW 改扩展样式（去 `WS_EX_APPWINDOW`、加 `WS_EX_TOOLWINDOW`）。不触发句柄重建、无闪烁、不阻塞 execute 线程。

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 任务栏去占位实现 | `SetWindowLongPtrW` 改 `WS_EX_TOOLWINDOW` | 不重建句柄（`ShowInTaskbar` setter 会 RecreateHandle → 死锁），无闪烁 |
| 跨线程调用 | `BeginInvoke` 异步投递 | 不阻塞 execute 线程；消息泵启动后自然执行 |
| 手动 X 语义 | ON 持久面板 X=一直关着；OFF 自动模式 X=本次收起 | 自动模式显隐由请求流驱动，X 不该粘滞 |
| 启动显隐 | 恢复按「始终开启」开关控制 | v0.125 无条件展开覆盖了新需求，需回退 |
| 请求结束隐藏 | `_clear_rid` 最后一个清掉即隐藏 | 不等 watchdog 下一 tick，即时消失 |
| 影响面控制 | 只动 `always_one_manually_hidden` 粘滞 + 启动分支 + 结束时机 | ON 模式行为 zero 变化 |

---

## 4. 实现中遇到的问题

### 问题 1：`ShowInTaskbar = False` 触发 GUI 假死（本次事故）

**现象**：用户重启 GUI 后整体卡死（点不动、`Responding: False`），uvicorn 8088 日志完全正常。

**定位**：`py-spy dump` 抓线程栈 —— Thread-11 (execute) 卡在 `relay/gui.py:3820`（正是 `panel_native.ShowInTaskbar = False` 那行），MainThread 卡在 `webview/platforms/winforms.py:834 create_window`。

**根因**：WinForms `Form.ShowInTaskbar` 在 handle 已创建且窗口显示后赋值会触发 `RecreateHandle`（重建句柄），WinForms 内部要求回到创建句柄的线程（UI 线程）。但此时 UI 线程还卡在 `browser.Show()`（WebView2 初始化，消息泵未启动），RecreateHandle 的跨线程 SendMessage 无人泵 → execute 线程永久等待 → 死锁。

**解法**：弃用 `ShowInTaskbar`，改用 Win32 `SetWindowLongPtrW` 直接改扩展样式（去 `WS_EX_APPWINDOW` + 加 `WS_EX_TOOLWINDOW`）。改样式不重建句柄，即时生效无闪烁；且整个操作 `BeginInvoke` 异步投递到 UI 线程，execute 线程不等。

### 问题 2：`always_one_manually_hidden` 粘滞（需求 3 根因）

v0.153 修的是「关开关」路径，但**用户手动 X** 那条路径仍会置 flag。关闭「始终开启」时 flag 若已为 True，`_apply_geometry` 恒 return False → `assign()` 弹不出侧栏。

**解法**：`assign()` 自动显隐模式下无条件清 flag（见 §3 根因 A）。同时 `_clear_rid` 收尾隐藏后也复位 flag，保证「下次请求流弹出」不受历史 X 影响。

### 问题 3：v0.125「无条件展开」与新需求冲突

`start()` 里无条件 `_show_always_one()`，无视「始终开启」开关。新需求「关闭后无请求不显示」无法满足。

**解法**：恢复 `if self._always_one_setting() and not self.always_one_manually_hidden` 门控。注意：ON 时即使这里因几何未知 show 失败，`_on_panel_loaded` 的 guard（`live_panel OR always_one`）会补 dock+show，不会漏显示。

### 问题 4：关闭「始终开启」后结束隐藏时机晚

watchdog 每 10s tick 才扫一次空闲隐藏，最后一个请求结束后侧栏最多多挂 10s，不满足「请求均结束后消失」。

**解法**：`_clear_rid()` 里检测 `not self._rids and not self._always_one_setting()` → 立即隐藏。watchdog 的空闲隐藏逻辑保留作兜底（自动模式 toggle 等场景）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| 需求 1 协议分布 | CARD_DEFS + 4 i18n + 两处静态标题 + cache bump | app.js / index.html / _probe_persistence.html |
| 需求 2 任务栏去占位 | `_hide_panel_taskbar()`：BeginInvoke + SetWindowLongPtrW（WS_EX_TOOLWINDOW） | gui.py |
| 需求 3 自动显隐 | `start()` 恢复开关控制；`assign()` 自动模式清 manual_hidden；`_clear_rid` 最后清空即隐藏 | panel_pool.py |
| 卡死事故 | 弃用 `ShowInTaskbar` setter，改 Win32 扩展样式（不重建句柄） | gui.py |

---

## 6. 是否完全遵循规划路径开发

**部分偏离**，但都是防御性修复，无新增功能。

### 完全按规划（无偏离）：

- 需求 1 重命名：CARD_DEFS + i18n + 静态标题，一次到位。
- 需求 3 三条路径（启动显隐 / assign 弹出 / 结束隐藏）全按用户「有请求流出现、请求均结束消失」的语义实现。

### 偏离之处：

- **(a) 需求 2 第一版直接设 `ShowInTaskbar = False` 卡死 GUI**：这是实现事故，不是用户场景设计问题。根因是 WinForms 属性 setter 在窗口 handle 已建且显示后触发句柄重建 + 跨线程死锁。**改用 Win32 扩展样式方案，属事故修复，不算功能偏离。**
- **(b) `_clear_rid` 顺带复位 `always_one_manually_hidden`**：watchdog 空闲隐藏本来就会复位该 flag，新代码在收尾隐藏时也复位，双保险，纯防御补充。
- **(c) 只 bump index.html 的 cache，没 bump `_probe_persistence.html`**：probe 是调试副本（版本号停在 20260822），非正式分发文件，不维护其 cache。

### 重大调整：无。

---

## 7. 最终实现点

### 前端：需求 1 重命名

1. **`app.js` CARD_DEFS**（1707）：`{ key: "platform", title: "协议分布", cls: "" }`。
2. **`app.js` 4 个 i18n 字典**（5241/5671/6090/6509）：
   - en `"协议分布": "Protocol breakdown"`
   - zh-TW `"协议分布": "協議分佈"`
   - ja `"协议分布": "プロトコル分布"`
   - ko `"协议分布": "프로토콜 분포"`
   - zh 走 `t()` 回退源串（`I18N.lang !== "zh"` 才查字典），无需自映射。
3. **`index.html` 静态标题**（192）：`<div class="card-title">协议分布</div>`。
4. **`_probe_persistence.html` 静态标题**（192）：同改（调试副本，保持一致）。
5. **`index.html` cache**：JS/CSS `?v=20260824-31` → `?v=20260825-01`。

### 后端：需求 3 自动显隐（`panel_pool.py`）

6. **`start()`**：恢复按「始终开启」开关控制启动显隐（移除 v0.125 无条件展开）。
   ```python
   if self._always_one_setting() and not self.always_one_manually_hidden:
       self._show_always_one()
   ```
7. **`assign()`**：`始终开启 OFF`（自动显隐模式）下清掉 `always_one_manually_hidden` —— 手动 X 不再粘滞，新请求流到达即弹出。
8. **`_clear_rid()`**：最后一个 rid 清掉后立即 `_hide_always_one()`（`not self._rids and not self._all_hidden and not self._always_one_setting() and always_one_visible`），实现「请求均结束后消失」。

### 后端：需求 2 任务栏去占位（`gui.py`）

9. **`_hide_panel_taskbar()`**（新方法）：`_on_panel_loaded` 里调用。
   - `BeginInvoke(MethodInvoker(_remove))` 异步投递到 UI 线程（不阻塞 execute 线程）。
   - 回调内 `SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW)` —— 去任务栏按钮、不重建句柄、无闪烁。
   - 纯 ctypes，不依赖 pythonnet 的窗口属性。

### 行为验收清单（手动测试项）

- [ ] 总览页卡片显示「协议分布」（zh），切换语言后译文正确
- [ ] 侧栏不在任务栏单独占位，任务栏只有主窗一个按钮
- [ ] 「始终开启」ON：启动即显示侧栏，无请求也保留（行为不变）
- [ ] 「始终开启」OFF：启动不显示侧栏，无请求无窗口
- [ ] 「始终开启」OFF + 发请求 → 请求流出现时侧栏弹出
- [ ] 「始终开启」OFF + 请求结束 → 侧栏立即消失（不等 watchdog）
- [ ] 「始终开启」OFF + 手动 X 过侧栏 → 下个请求流照常弹出（不粘滞）
- [ ] 「始终开启」ON + 手动 X 过侧栏 → 仍不弹出（持久面板语义保留）
- [ ] GUI 重启正常不卡死（任务栏修复无死锁）
- [ ] uvicorn 8088 行为不变（本版本未动 proxy 路径）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/app.js` | 改（CARD_DEFS 标题 + 4 i18n 翻译） |
| `src/relay/web/index.html` | 改（静态标题 + cache bump） |
| `src/relay/web/_probe_persistence.html` | 改（静态标题，调试副本） |
| `src/relay/panel_pool.py` | 改（`start` / `assign` / `_clear_rid` 三处，约 +12 行） |
| `src/relay/gui.py` | 改（新增 `_hide_panel_taskbar`，约 +45 行） |
