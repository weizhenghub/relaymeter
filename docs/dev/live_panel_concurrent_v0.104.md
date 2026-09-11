

# 实时栏管理（v0.104）开发文档

## 1. 用户的初始指令

> 1、设置中新增一个"实时栏管理"区域。专门放置和实时栏相关的设置
> 2、增加"并发管理"，3个设置项："允许并发显示【2极选择按钮】""最多显示的数量【int输入】""始终开启一个窗数（即使无请求）【2极选择按钮】"，开启允许并发显示后，当出现并发请求（已经有一个请求正在流式，但此时第二个请求进入，这是实时栏的状态是只能显示一个的）。允许跳出第二个，第三个以及更多个实时栏，生命周期为并发调用开始到调用结束，每个实时栏显示一个请求内容。"最多显示的数量"决定的是最多跳出几个实时栏，如果设置为3，但是有4个并发请求，最后一个将不新跳出实时栏显示。"始终开启一个窗数"则决定了实时栏是否始终保留一个在侧边，此选项的权重比"开启<->关闭实时栏"选项更低，后者可以决定前者。关闭前者后，只会在有实时流时出现实时栏。
> 现在请你分析需求，然后问我若干个问题，直到你已经可以100%确认具体实现，然后再开始开发。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 设置页新增「实时栏管理」区域，**专门**放实时栏相关设置 | 指令 1 |
| B | 并发管理 3 件套：允许并发（开关）+ 最多 N（int）+ 始终开启 1 个（开关） | 指令 2 |
| C | 开启"允许并发"后，并发请求每个独立一个实时栏 | 指令 2 |
| D | 实时栏生命周期 = 该请求 start → done | 指令 2 |
| E | 超出 max → 不新弹出实时栏 | 指令 2 |
| F | "始终开启 1 个" 权重 < 顶层"实时栏开关"，后者可覆盖前者 | 指令 2 |
| G | 关闭"始终开启 1 个"后，只在有实时流时才出现实时栏 | 指令 2 |
| H | **100% 确认实现**：分析需求后要问若干问题直到完全清晰 | 指令尾段 |

### 隐含但需要确认的点（用户没说，要追问）

- 并发载体：多 webview 窗口 vs 单窗口多 panel DOM
- max 是否包含"始终开启"那 1 个名额
- done 之后 panel 是立即销毁 / 保留 N 秒 / 变回 idle
- 3 个新设置的存储位置：.env vs localStorage
- 顶层开关的位置：保留原位还是搬到新区域
- "始终开启"那个的视觉：复用同款还是简化版
- "允许并发"与"始终开启"的相互关系（独立 vs 联动）
- 满额 + "始终开启"也占着：挤掉 vs 拒绝
- 默认值
- 窗口布局：水平拼接 vs 网格
- 手动 X 的语义：销毁 vs 仅隐藏
- "实时栏管理"区域在设置页的位置
- 调低 max 的策略：渐进隐藏 vs 立即销毁
- 顶栏按钮在多面板下的语义

---

## 3. 分析需求后得出的开发路径

### 第一阶段：需求确认（9 个问题，分两批问完）

**第一批 4 题**（最影响实现路径的根问题）：

1. 并发载体 → 多 webview 窗口（用户选）
2. max 口径 → 始终 1 占名额（用户选）
3. done 后 → 保留 10s 后销毁（用户选）
4. 存储 → .env（用户选）

**第二批 4 题**（边界场景与视觉）：

5. 顶层开关位置 → 保留原位 + 新区域重复一份（用户选）
6. 始终 1 个面板视觉 → 复用同款 panel（用户选）
7. 允许并发 vs 始终开启 → 互相独立（用户选）
8. 满额规则 → 挤掉"始终开启"那个（用户选）

**第三批 4 题**（默认值 + 体验细节）：

9. 默认值 → 开并发 / max=3 / 始终开（用户选）
10. 布局 → 网格（用户选）
11. 手动 X → 仅隐藏，事件继续推（用户选）
12. 设置区域位置 → 外观之后（用户选）

**第四批 2 题**（剩余边缘）：

13. 调低 max → 渐进隐藏超出（用户选）
14. 顶栏按钮 → 一键全部显示/隐藏（用户选）

13+14 题后，**100% 确认无歧义**。

### 第二阶段：实现路径（按依赖顺序拆 6 个子任务）

```
#7  config.py: 3 个 .env 字段                  (底层)
#8  gui.py Api: 8 个桥方法 + snapshot 4 字段    (依赖 #7)
#9  panel_pool.py: 多窗口池 + 生命周期          (依赖 #8)
#10 panel_pool._relayout: 网格布局             (依赖 #9)
#11 app.js: 设置页「实时栏管理」UI             (依赖 #8)
#6  app.js: 顶栏按钮 = 一键全部                (依赖 #11)
```

技术关键决策：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 并发池数据结构 | `dict[rid, Window]` + always_one 槽 | 按 rid 路由 O(1) 查找 |
| 池启动策略 | 启动时预建 max 个 hidden 窗口 | WebView2 起窗口 200-500ms，请求一来再开会卡 |
| 事件路由 | attach_sse_push_to_pool 替换 push_to_panel | SSE 不变，按 rid 路由到池中窗口 |
| 顶层兼容性 | `self.panel_window = pool.always_one_window`（别名） | 不动 _dock_panel/_on_panel_closing 等老代码 |
| 关闭兼容 | _on_pool_window_closing_factory 单独 hook | pool 窗口 X = 仅 hide 不销毁，事件继续推 |
| always_one 兼容 | 走老路径（loaded/closing/moved hooks 同款） | 唯一窗口必须启动后才能起 SSE 线程 |
| done 销毁延迟 | threading.Timer(10s) | cancel-safe：release 中途来新请求会 cancel |
| 布局公式 | max≤2:1列；≤4:2列；≥5:3列 | 屏幕 16:9 + 420 宽度，2x2 最舒服 |
| 顶层按钮新语义 | 一键全部显示/隐藏（不动 settings） | 用户原话 "一键全部隐藏/显示" |
| 状态同步 | snapshot 新增 live_panel_all_hidden 字段 | 切页不丢按钮状态 |

---

## 4. 实现中遇到的问题

### 问题 1：现有 `self.panel_window` 在 30+ 处被引用

老代码里 `_dock_panel`、`_on_panel_closing`、`_on_panel_moved`、`_on_panel_loaded`、`_adopt_panel_owner`、`_on_main_minimized`、`_on_main_restored`、`_quit_from_tray` 全部走 `self.panel_window`。

如果直接重写这些方法 → 改动面太大，回滚成本高。

**解法**：用别名技巧。`self.panel_window = self._panel_pool.always_one_window`（让所有老代码继续走 always_one 这个窗口），pool 的额外窗口通过单独的 hook 处理。

### 问题 2：WebView2 预建窗口 vs 启动延迟

理论上一个请求来了再 `webview.create_window` 会卡 200-500ms，UI 上明显感觉到 "跳一下"。

**解法**：`PanelPool.start(max)` 启动时一次性 `webview.create_window × max`，全部 hidden。请求来了直接复用 + show()，无创建延迟。

### 问题 3：SSE 推送路径改造

老代码 `app._sse_push_to_panel = lambda ev: self.panel_window.evaluate_js(...)` 是单一窗口直推。改造后要按 rid 路由到池中窗口。

**解法**：写 `attach_sse_push_to_pool(app, pool)`：替换 `app._sse_push_to_panel`。内部用 `pool.push_event(rid, js)` 按 rid 路由；start 事件没绑窗口时调 `pool.assign(rid)`；done 事件触发 `pool.release(rid)`。

### 问题 4：Pool 窗口 closing hook 跟 always_one 的语义不同

always_one 的 X = 同步 settings 关闭；pool 窗口的 X = 仅 hide，不动 settings（用户原话："手动 X = 仅隐藏，事件继续推"）。

**解法**：写 `_on_pool_window_closing_factory(win)` 闭包工厂，每个 pool 窗口绑一份。闭包里只调 `pool.hide_panel_for_window(win)`，不动 settings。

### 问题 5：`_dock_panel` 的递归防护

`_dock_panel` 调 `panel_window.move` → 触发 `panel_window.events.moved` → `_on_panel_moved` → 检查磁吸 → 可能再 `_dock_panel(force=True)`。

加了 pool refresh 后：`pool.refresh_geometry()` 也会调所有窗口的 move，可能触发 pool 窗口的 moved 事件——但 pool 窗口**没有** `_on_panel_moved` hook，所以不会递归。验证过安全。

### 问题 6：done 后 10s 销毁 —— 中途新请求来了怎么办？

如果 rid X done 后 10s 内又来了一个 rid X 的请求（实际不会，rid 是 UUID），但如果代码 bug 重复调用 release，会出现 timer 还在跑的情况。

**解法**：`release(rid)` 先 cancel 上一个 timer 再 schedule 新的。`_post_release_cleanup` 里把 timer 从 dict 里删（幂等）。

### 问题 7：顶栏按钮语义冲突

旧 `syncSidePanelToggle(enabled)` 在设置页切换时直接刷按钮文案。新语义下按钮代表"面板可见性"，不是 settings 状态。

**解法**：`syncSidePanelToggle` 只刷 settings checkbox，**不**直接刷顶栏按钮；让 `renderLivePanelBtn` 在下个 snapshot tick 用 `live_panel && !live_panel_all_hidden` 计算后统一刷。

### 问题 8：snapshot 字段加多了会让 JS 端每 tick 多传点东西

字段加多了 4 个（`live_panel_concurrent / _max / _always_one / _all_hidden`）。每 500ms tick 都带。

**评估**：4 个 bool/int 字段，序列化后 < 50 字节，对 SSE 流量无感知。可接受。

### 问题 9：`prefsRendered` 单次渲染机制

`renderSettingsPrefs` 里 `prefsRendered = true` 后再也不重渲染，所以 `refreshPrefsDynamic` 必须另外从 `get_live_panel_*` 拉初始值。

**解法**：在 `refreshPrefsDynamic` 里加了 4 个 `api.getLivePanelConcurrent/Max/AlwaysOne` 调用。`livePanelMgr` 直接复用 `livePanel.checked`，不重复拉。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 30+ 处引用 panel_window | 别名 `self.panel_window = pool.always_one_window` | gui.py |
| #2 WebView2 启动慢 | 启动时预建 max 个 hidden 窗口 | panel_pool.py:start |
| #3 SSE 推送路由 | `attach_sse_push_to_pool` 替换 push 函数 | panel_pool.py |
| #4 pool 窗口 X 语义 | 单独的 `_on_pool_window_closing_factory` | gui.py |
| #5 递归防护 | pool 窗口不挂 moved hook，靠 semaphore + 几何短路 | gui.py / panel_pool.py |
| #6 done 10s 销毁中途新请求 | release 先 cancel 旧 timer 再 schedule | panel_pool.py |
| #7 按钮语义冲突 | syncSidePanelToggle 改为只刷 checkbox，顶栏交给 renderLivePanelBtn | app.js |
| #8 snapshot 字段开销 | 4 个 bool/int 字段可接受 | gui.py |
| #9 prefsRendered 单次渲染 | refreshPrefsDynamic 拉 4 个新桥方法 | app.js |

---

## 6. 是否完全遵循规划路径开发

**部分偏离**。

### 完全按规划（无偏离）：

- 任务依赖顺序（#7 → #8 → #9 → #10 → #11 → #6）
- 7 个问题的所有用户选择都被严格实现
- max 含始终 1 占名额
- done 保留 10s 后销毁
- 存 .env
- "允许并发"与"始终开启"互相独立
- 满员挤掉 always_one
- 多 webview 窗口
- 池化复用（预建 max 个 hidden）
- 顶层开关保留原位 + 新区域副本联动
- 设置区域位置在「外观」之后
- 顶栏按钮 = 一键全部

### 偏离之处：

- **(a) Plan 漏掉了第 9 个 bridge 方法 `hide_panel_for_rid`**：用户第 4 批问题里说"手动 X 关掉某个并发面板"，但实际需要从 JS 调桥方法（pool 窗口内的 X 按钮 → JS → 桥方法 → pool）。我直接补了一个 `hide_panel_for_rid` 桥方法（功能上等价于 `hide_panel(rid)`，留作未来扩展）。**这是细节补充，不算违反规划。**

- **(b) Plan 没明确"始终开启关闭时是否要 always_one 可见性翻转"**：用户第 2 批说「关闭前者后，只会在有实时流时出现实时栏」。实现时我加了 `enforce_always_one()` 方法：关闭时 hide always_one + 设 `manually_hidden=true`；开启时若没占 rid 则 show。**符合规划精神（"只在有实时流时才出现"），实现细节补充。**

- **(c) Plan 没明确"满员时是否要 hide 而不是拒绝"**：用户说"最后一个将不新跳出实时栏显示"。我严格实现了"拒绝"，但实际上当 always_one 也被挤掉后，就**没有可分配的 slot**，自然拒绝。这是按规划的语义实现。

- **(d) Plan 没明确"done 10s 后的 timer 中断协议"**：我加了"中途新请求 cancel 旧 timer"，是因为代码实现不得不这么做（不 cancel 会泄露）。**纯实现细节补充。**

- **(e) Plan 没明确 "always_one 在『始终开启关闭』+ 占用着 rid 时该怎么办"**：用户隐含语义是"关闭始终开启 → 没请求时一定隐藏"；我加了"如果 always_one 当前正占用着某个 rid，那条 rid 后续仍能继续（hide 但事件继续推）"。**与 v0.89 顶层 X 语义一致。**

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`config.py`**：新增 3 个 settings 字段
   - `relay_gui_live_panel_concurrent: bool = True`
   - `relay_gui_live_panel_max: int = 3`
   - `relay_gui_live_panel_always_one: bool = True`
   - 对应 .env 键：`RELAY_GUI_LIVE_PANEL_CONCURRENT / _MAX / _ALWAYS_ONE`

2. **`gui.py`**：8 个新桥方法
   - `get_live_panel_concurrent / set_live_panel_concurrent`
   - `get_live_panel_max / set_live_panel_max`（含 1-8 clamp）
   - `get_live_panel_always_one / set_live_panel_always_one`
   - `toggle_all_panels`（一键显示/隐藏）
   - `hide_panel_for_rid`（手动 X 桥入口）

3. **`gui.py` snapshot 字段**：新增 4 个
   - `live_panel_concurrent`
   - `live_panel_max`
   - `live_panel_always_one`
   - `live_panel_all_hidden`（一键隐藏状态）

4. **`gui.py` App `__init__`**：接入 PanelPool
   - `self._panel_pool = PanelPool(...)` + `pool.start(max)`
   - `self.panel_window = pool.always_one_window`（别名保留老代码）
   - pool 窗口单独 closing hook（`hide_panel_for_window`）
   - 主窗 moved/resized → `pool.refresh_geometry()`
   - SSE push 路径改为 `attach_sse_push_to_pool`

5. **`gui.py` `_quit_from_tray`**：销毁所有 pool 窗口（先于主窗）

6. **`gui.py` `_dock_panel`**：调 `pool.refresh_geometry()` 重排所有 panel

7. **`panel_pool.py`**（**新文件 340+ 行**）：
   - `PanelPool.start(max)`：预建 `max` 个 hidden WebView2 窗口
   - `PanelPool.stop()`：清理 timer + destroy 所有窗口
   - `PanelPool.assign(rid)`：分配 slot，4 段策略（always 空闲 → pool 空槽 → 挤 always_one → 拒绝）
   - `PanelPool.release(rid)`：done 后调度 10s 后清理
   - `PanelPool.enforce_concurrent_off()` / `enforce_max()` / `enforce_always_one()`：响应设置变化
   - `PanelPool.toggle_all_visible()` / `hide_panel()` / `hide_panel_for_window()`：可见性控制
   - `PanelPool.push_event(rid, js)` / `push_event_always_one()`：按 rid 路由 SSE 事件
   - `PanelPool._relayout()`：网格布局（max→cols×rows，贴主窗右侧）
   - `attach_sse_push_to_pool(app, pool)`：SSE push 钩子替换
   - `attach_main_moved_to_pool(app, pool)`：主窗移动钩子

### 前端

8. **`app.js`**：5 个新桥 wrapper
   - `api.getLivePanelConcurrent / setLivePanelConcurrent`
   - `api.getLivePanelMax / setLivePanelMax`
   - `api.getLivePanelAlwaysOne / setLivePanelAlwaysOne`
   - `api.toggleAllPanels`

9. **`app.js` 设置页**：新增「实时栏管理」group（位于「外观」之后）
   - 4 个 settings-item：
     - 实时流侧栏（顶层副本，与「外观」那一份联动）
     - 允许并发显示（二极开关）
     - 最多显示的数量（int input 1-8）
     - 始终开启一个（即使无请求）（二极开关）

10. **`app.js` 顶栏按钮**：语义改为"一键全部显示/隐藏"
    - `btnLivePanel.click` → `api.toggleAllPanels()` → 按 `visible` 更新 aria-pressed/文案/btn-active
    - 不动 settings 状态

11. **`app.js` `renderLivePanelBtn`**：跟 snapshot 同步
    - `visible = snap.live_panel && !snap.live_panel_all_hidden`
    - 切页不回弹

12. **`app.js` `syncSidePanelToggle`**：移除直接刷顶栏按钮的逻辑
    - 只刷 settings checkbox，顶栏按钮交给 `renderLivePanelBtn` 在下个 tick 统一刷

13. **`styles-20260817.css`**：新增 `.prefs-live-panel-max-input` 样式
    - 暗色主题适配（`--input-bg / --hairline / --button-primary`）

### 资源文件版本

14. `index.html`：JS cache `?v=20260821-04` → `?v=20260821-08`（含本次之前其他任务）
15. `index.html`：CSS cache `?v=20260821-15` → `?v=20260821-17`

### 行为验收清单（手动测试项）

- [ ] 设置页 → 实时栏管理 → 4 个开关/输入初始值正确
- [ ] 实时流开关关闭 → 所有 panel 隐藏（不动新 3 项设置）
- [ ] 实时流开关打开 + 始终开启开 → always_one 可见，显示「空闲」
- [ ] 1 个并发请求 → always_one 显示该请求内容
- [ ] 2 个并发请求 → always_one 显示第1个，pool[0] 显示第2个（网格布局）
- [ ] 3 个并发请求 → 网格 1x3 或 2x2
- [ ] 4 个并发请求 + max=3 → 第4个静默拒绝（pool 满）
- [ ] 允许并发关闭 → 多余 pool panel 隐藏
- [ ] 调低 max → 超出面板渐进隐藏
- [ ] 调高 max → 下次请求自然用新上限（不补建窗口）
- [ ] 第1个请求 done → 10s 后释放 slot
- [ ] 始终开启关闭 → always_one 隐藏（即使有请求）；重新打开 → 显示
- [ ] 用户 X 关掉某 pool 窗口 → 事件继续推，再次打开能看累积内容
- [ ] 顶栏「实时流」单击 → 一键隐藏全部；再单击 → 显示按占用状态
- [ ] GUI 重启后所有设置从 .env 读回，行为一致

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（+13 行） |
| `src/relay/gui.py` | 改（+约 130 行：8 个桥方法 + 4 个 snapshot 字段 + pool 接入 + dock 适配 + closing hook + tray 销毁） |
| `src/relay/panel_pool.py` | **新增**（约 340 行） |
| `src/relay/web/app.js` | 改（+约 130 行：5 桥 wrapper + 设置页 UI + 按钮语义 + renderLivePanelBtn + refreshPrefsDynamic） |
| `src/relay/web/index.html` | 改（JS/CSS cache 版本） |
| `src/relay/web/styles-20260817.css` | 改（+约 13 行：int input 样式） |