# 悬浮球联动打磨 + 非并发容器固定骨架（v0.166）开发文档

## 1. 用户的初始指令

> （前序 B）磁吸（dock）与悬浮球互斥：开球即浮动、关球即磁吸，二者不能同时生效；
> 把「始终开启一个」改名为「磁吸状态下始终开启一个」——该选项只在磁吸模式下生效。
> 覆盖 5 个场景：球 S2 隐藏侧栏 / 球 S1 流式中弹侧栏 / 磁吸+always_one ON 常驻一个 /
> 磁吸+always_one OFF 无请求时隐藏 / 顶层「实时流侧栏」OFF 关闭一切。

> （本次 C，逐字）
> 1、拖动悬浮球时，侧栏应当始终跟随悬浮球，而不是停止并松开鼠标后才跳动过去。
> 2、关闭允许并发选项后，只允许且只必须存在 1 个端点数据容器，完整内容容器，tool 容器。
>    允许内容空，不许不存在，不许多。完成后再次扫描设置选项之间是否完备。
> 3、小幽灵图标做宽胖一点，更符合原来的样式。允许边界范围有一点椭圆。
> 4、完成后写开发文档和更新日志，注意约束。
> 5、将上面的 4 个任务也加入到待办列表，然后继续依序执行。

### 场景拆解

- **互斥（B）**：球 = 锚点浮动模式，磁吸 = 贴主窗右缘 dock 模式。两态互斥，由「悬浮球」开关单一驱动：开 = 浮动，关 = 磁吸。
- **改名（B）**：「始终开启一个」语义收窄为「仅磁吸（dock）模式生效」——浮动（球）模式下 always_one 恒不生效。
- **拖动实时跟随（C1）**：拖球过程中侧栏连续跟随，而非松手后一次性跳过去。
- **非并发容器固定（C2）**：关「允许并发」后，前端固定保留 1 端点 + 1 完整内容 + 1 工具容器骨架，内容可为空，容器不可缺/不可多。
- **小幽灵加宽（C3）**：幽灵图标变宽胖（宽>高，允许椭圆边界）。
- **（跟进反馈）球启动即激活**：GUI 进程创建时悬浮球即出现，不等首条请求流。
- **（跟进反馈）启动侧栏误显示**：主开关关或悬浮球模式下，启动时侧栏不该强显（用户「没开实时流侧栏侧栏却在」）。
- **（跟进反馈）拖动卡顿**：拖动球时帧率偏低、明显卡顿感。
- **（跟进反馈）悬浮窗模式侧栏与主窗解耦层级**：只有磁吸住时才强制同一层级。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 磁吸与悬浮球互斥：开球=浮动（脱离磁吸），关球=磁吸（贴主窗右缘） | B |
| B | `_should_keep_idle_open()` 只在「磁吸 + 始终开启一个」成立；浮动模式恒 False | B |
| C | 「始终开启一个」文案改名「磁吸状态下始终开启一个」（4 语言 + 提示） | B |
| D | 拖球过程中侧栏连续跟随（实时 relayout，非松手后跳） | C1 |
| E | 拖动中不写盘（30~50ms 一次太频繁），松手由持久化落盘 | C1 |
| F | 关「允许并发」后前端固定保留 3 容器骨架（端点/内容/工具），内容可空 | C2 |
| G | 后端非并发模式 assign() 只保留 1 个活跃 rid（旧 rid 清掉） | C2 |
| H | 小幽灵几何加宽加胖（body 横向 0.13~0.87s，宽>高，允许椭圆） | C3 |

### 隐含但需要确认的点（用户没说，要追问）

- 拖动实时跟随的节流频率（太密会写盘/relayout 抖动，太疏会跳）—— 取 50ms 节流（~20fps）。
- 拖动中是否持久化位置 —— 否，松手才落盘（避免高频写盘）。
- 非并发模式下「允许内容空，不许不存在」—— 用前端**持久空容器占位**（`__idle__` 完整内容 + 空工具卡），不销毁骨架。
- 小幽灵「允许边界椭圆」—— 身体宽>高即可，眼睛随脸宽拉开间距，不要求严格圆形。

---

## 3. 分析需求后得出的开发路径

### 核心方案

**互斥（B）**：`panel_pool._float_ball_enabled` 作为唯一模式位；`_should_keep_idle_open()` 与 `_ball_should_show()` 互斥派生。`gui.set_float_ball` 设 `_panel_docked = not enabled` 双向联动。

**拖动实时跟随（C1）**：`ball_layer._mouse_tick` 的 DRAGGING 分支在 `_present(reposition)` 后，按 50ms 节流把当前 logical 位置丢给 `on_move` 回调（新线程跑）→ `pool._ball_drag` 更新 `_ball_pos` + `_relayout()`（不写盘）。松手 `on_move_end` → `_persist_ball_pos` 落盘。

**非并发容器固定（C2）**：两层。
- 后端 `assign()`：非并发时清掉旧 rid，保证最多 1 活跃 rid。
- 前端 `setConcurrent(on)`：非并发时建 `__idle__` 空完整内容容器 + 空工具卡占位，布局时始终推进去；`findReusableDone` 把 idle 当可复用槽，内容 rebind 到它身上，骨架不销毁。

### 开发路径（按依赖顺序）

```
#1  panel_pool.py: _should_keep_idle_open/_ball_should_show 互斥        (B)
#2  gui.py:        set_float_ball 设 _panel_docked + _update_panel_snap 双向 (B)
#3  app.js/index.html: 「始终开启一个」改名 4 语言                        (B)
#4  ball_layer.py: DRAGGING 分支 50ms 节流 on_move 回调                 (C1)
#5  panel_pool.py: _ball_drag + _create_ball 传 on_move                (C1)
#6  panel_pool.py: assign() 非并发清旧 rid；set_concurrent/enforce_concurrent_off (C2)
#7  gui.py:        set_live_panel_concurrent 恒推 setConcurrent         (C2)
#8  live_panel.js: setConcurrent + __idle__/工具占位 + findReusableDone (C2)
#9  ball_layer.py: _draw_ball 小幽灵加宽加胖                             (C3)
```

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 互斥载体 | `_float_ball_enabled` 单一模式位 | 一个开关驱动，不引入第三态 |
| always_one 生效域 | `(not _float_ball_enabled) and _always_one_setting()` | 浮动模式 always_one 不生效，球 S1/S2 接管侧栏显隐 |
| 拖动跟随节流 | 50ms（~20fps），新线程跑回调 | 太密 relayout 抖动 + 高频排队；太疏有跳感 |
| 拖动中写盘 | 否，松手才落盘 | 30~50ms 一次写 .env 太频繁 |
| 非并发容器 | 前端持久空容器占位（`__idle__` + 空工具卡） | 「内容可空，容器不可缺」——骨架永不销毁，内容 rebind |
| 非并发 assign | 清旧 rid 保最多 1 活跃 | 后端兜底，前端骨架只剩一个内容槽 |
| 幽灵宽胖 | body 0.13~0.87s（宽74%）× 0.22~0.80s（高58%） | 宽>高，矮胖椭圆感；眼睛中心 0.37/0.63 随脸宽拉开 |

---

## 4. 实现中遇到的问题

### 问题 1：拖动中侧栏「松手才跳」—— 拖动无实时回调

v0.165 的 `BallLayer` 只暴露 `on_move_end`（松手时持久化），拖动过程没有任何回调 → 侧栏只能等松手后一次性 `_persist_ball_pos` + `_relayout`，表现为「停止并松开后才跳过去」。

**解法**：`BallLayer.__init__` 增 `on_move` 参数；`_mouse_tick` 的 DRAGGING 分支在 `_present(reposition)` 后按 50ms 节流把当前 logical 位置丢给 `on_move`（新线程跑）。`pool._ball_drag` 更新 `_ball_pos` + `_relayout()`（不写盘）。松手仍走 `on_move_end` → `_persist_ball_pos` 落盘。

### 问题 2：非并发模式下容器会随请求结束被整个销毁

老 `relayLiveClear` 在并发模式直接移除容器 DOM；非并发模式下若沿用，请求一结束容器就没了，下次请求又新建 → 违反「容器不可缺」的固定骨架要求。

**解法**：`relayLiveClear` 在 `!_concurrent` 分支不删容器，只重置内容 + badge 置「空闲」（idle）；`findReusableDone` 把 idle 容器当可复用槽，下次请求内容 rebind 进去。另加 `ensureSingleSkeleton()` 保证 `_ridOrder` 为空时也有 `__idle__` 完整内容容器 + 空工具卡占位。

### 问题 3：非并发时后端可能还挂着多个活跃 rid

前端即使固定骨架，后端 `_rids` 若累积多个 rid，事件路由与 10s 清理会错乱（多余容器反复建/清）。

**解法**：`assign()` 在 `!_concurrent_setting()` 时把非当前 rid 全 `_clear_rid` 掉，保证活跃 rid 恒 ≤1。

### 问题 4：小幽灵比例偏「瘦长」不符参考图

v0.165e 初版 body 0.20~0.80s × 0.20~0.82s，接近正方形偏高，且眼睛间距窄（0.40/0.60），整体显瘦。

**解法**：几何重设为 body 0.13~0.87s × 0.22~0.80s（宽>高，矮胖椭圆），眼睛中心 0.37/0.63、眼宽 0.060s（间距随脸宽拉开），手臂随身体加宽探出。

### 问题 5：悬浮球要等首条请求流才出现

用户反馈「悬浮球在中继 GUI 进程创建时就要激活，而不是等到第一条请求流来」。根因：侧栏窗口创建为 `hidden=True`（`PanelPool.start`），其 `loaded` 事件要等侧栏**首次显示**才触发；而建球的唯一入口挂在侧栏 `_on_panel_loaded`（`gui._ensure_ball`）。浮动（球）模式下侧栏空闲隐藏 → 侧栏 loaded 与 `webview.start()` 不同步，球默默 defer 到首条请求流才激活。

**解法**：在主窗 `_on_loaded`（`webview.start()` 返回、native 就绪）直接 `pool.refresh_ball_visibility()` 建球。`refresh_ball_visibility` 见 native 就绪则 `_create_ball()`，否则置 `_ball_pending` 兜底；且经 `_ball_should_show` 门控（实时流侧栏 OFF 不弹球）。旧的 `_on_panel_loaded → _ensure_ball`（幂等，`self._ball is not None` 即 return）保留为无害冗余。

### 问题 6：启动时侧栏误显示（主开关关/球模式也强显）

用户「没开实时流侧栏，侧栏却在」。根因：三处启动强显（`PanelPool.start` / `gui._on_loaded` / `gui._on_panel_loaded`）的 gate 是 `live_panel OR always_one`。`always_one` 默认 `True`，于是：
- 主开关（实时流侧栏）关掉，`always_one` 仍 True → 强显；
- 悬浮球模式（`_float_ball_enabled`），`live_panel OR always_one` 仍可能 True → 侧栏被拽出来，违背「球模式空闲隐藏」。

**解法**：新增 `pool._should_show_on_startup()` = `_live_panel_setting() AND _should_keep_idle_open()`（即主开关 ON + 磁吸 + always_one）。三处统一换用它：
- 主开关 OFF → 启动不显示（无请求不占屏）；
- 悬浮球模式 → `_should_keep_idle_open()` 恒 False → 不强显，侧栏由球 S1/S2 控制；
- 磁吸 + always_one ON → 常驻显示；OFF → 首个请求到达再弹。

### 问题 7：拖动悬浮球卡顿

用户「按住小幽灵拖动时帧率有点低，明显卡顿感」。根因：球定位帧率 = `_MOUSE_POLL_MS`（全局轮询 timer，每 tick 一次 `UpdateLayeredWindow` 重定位球位置）。30ms ≈ 33fps，拖动时肉眼可感卡顿。

**解法**：`_MOUSE_POLL_MS` 30 → 15（≈64fps）。WinForms `Timer` 分辨率下限约 15.6ms，再小会被系统按 15.6 取整，无意义。注意 `_FOLLOW_MS=50`（侧栏跟随 relayout 节流）**不改** —— 它只控制「侧栏跟着球 relayout」的频率，不是球的定位帧率；`relayout` 成本（列数重算 + 宽度动画）远高于球的净重定位，30fps 的侧栏 relayout 已平滑。

### 问题 8：悬浮窗模式侧栏与主窗未解耦层级

用户「悬浮窗状态下，侧栏和主窗口解耦层级，只有磁吸住时才强制同一层级」。根因：侧栏是主窗的 `OwnedForm`（`AddOwnedForm`，v0.95），恒与主窗同一层级 —— 主窗被遮挡侧栏退后、主窗回顶侧栏回、主窗最小化侧栏跟着藏。但浮动（球）模式侧栏从球左上角展开，是桌面独立窗（依赖球 `WS_EX_TOPMOST`），不该再跟随主窗。旧 `_adopt_panel_owner` 无条件 `AddOwnedForm`，无解耦路径。

**解法**：改为 `_sync_panel_owner()`（幂等，按 `_panel_docked` 决定）：磁吸 dock 态 `AddOwnedForm` 绑定同一层级，浮动（球）态 `RemoveOwnedForm` 解耦为独立顶层窗。用 `_panel_owner_bound` 记住上次态，只在目标态与实际态不一致时才改（避免每帧重复 Add/Remove）。切换点：`set_float_ball`（开球=解耦/关球=绑定）、`_update_panel_snap`（磁吸=绑定/浮动=解耦）、`_dock_panel`（force dock 必绑定，但在 ball_mode guard 之后——浮动不会进）、`_on_panel_loaded`（初始按 `_panel_docked`）。native 变更走 `BeginInvoke` 异步（防 RecreateHandle 死锁），且 `_on_main_minimized`/`_on_main_restored` 已有 ball_mode guard（主窗最小化不藏侧栏/球，还原不重新 dock），与解耦语义一致。

### 问题 9：点不了悬浮窗（能拖）+ 侧边白屏 —— 并发双击创建球

用户「点不了悬浮窗（能拖）+ 侧边白屏，消息流出现后消失」。实测 log：同一位置前后 8ms 出现 **paired "ball start dispatched"**（两个 `BallLayer` 各带一个 `start dispatch` + `ensure_gdi` + `mouse poller started`），随后所有 click 都打 `relay.gui.panel_pool: ball_clicked: ball=False`。

**根因**：球的 check-then-create（`_ball is None` 判定）**无锁**，且调用方横跨多线程 —— `PanelPool.start`(init)、`gui._on_loaded`(execute，本轮新增的启动即激活)、`gui._on_panel_loaded`(execute)、`set_live_panel`/`set_float_ball`(api worker)。两线程同时读到 `_ball is None` → 各 `new` 一个 `BallLayer` → 屏幕出现**两个球/两个鼠标轮询器**，其中一个不被 `pool._ball` 跟踪（孤儿）。孤儿有自己的 poller 所以能拖动，但 `ball_clicked` 里 `self._ball is None` → 点球无反应。且两个 15ms 动画循环 + 轮询器叠加 `BeginInvoke` 到单一 UI 线程，挤占侧栏 WebView 渲染 → 侧边白屏，直到流状态稳定收敛才消失。

**解法**：`_create_ball` 整体 check-then-create 持 `_lock` 原子；`refresh_ball_visibility` 的 check-then-create 也持 `_lock`。`_lock` 是 `RLock`（可重入），`set_float_ball`/`set_live_panel` 已持锁再调 `_create_ball` 无死锁。加锁后后到者见 `_ball` 已非 None 只 show，不再产生孤儿/双球，也就不会挤占 UI 线程致白屏。

用户反馈「悬浮球在中继 GUI 进程创建时就要激活，而不是等到第一条请求流来」。根因：侧栏窗口创建为 `hidden=True`（`PanelPool.start`），其 `loaded` 事件要等侧栏**首次显示**才触发；而建球的唯一入口挂在侧栏 `_on_panel_loaded`（`gui._ensure_ball`）。浮动（球）模式下侧栏空闲隐藏 → 侧栏 loaded 与 `webview.start()` 不同步，球默默 defer 到首条请求流才激活。

**解法**：在主窗 `_on_loaded`（`webview.start()` 返回、native 就绪）直接 `pool.refresh_ball_visibility()` 建球。`refresh_ball_visibility` 见 native 就绪则 `_create_ball()`，否则置 `_ball_pending` 兜底；且经 `_ball_should_show` 门控（实时流侧栏 OFF 不弹球）。旧的 `_on_panel_loaded → _ensure_ball`（幂等，`self._ball is not None` 即 return）保留为无害冗余。

### 问题 10：S2 折叠后侧栏被弹回（延展/收回时）

用户「点击进入隐藏模式后，有新内容窗口创建还是会弹出侧栏；似乎在侧栏延展或收回的时候」。

**根因**：pywebview WinForms 后端的 `move()`（winforms.py:620）与 `resize()`（winforms.py:600）底层 `SetWindowPos` 都带 `SWP_SHOWWINDOW` —— 对**隐藏窗**调用会把窗口**重新显示**。S2 隐藏模式 `_hide_always_one` 置 `always_one_visible=False` 但**不**置 `always_one_manually_hidden`（后者专指用户手动 X），所以 `_apply_geometry` 旧的 `_all_hidden/手动X` 守卫罩不住隐藏态。流式容器增减 → 前端 `reportWidth` → `live_panel_layout` → `request_width` → `_relayout` → `_apply_geometry`（球分支贴球位）仍 enqueue move/resize → 隐藏中的侧栏被重新弹出。

**解法**（两层，防双路重显）：
1. `_apply_geometry` 增 `force_apply` 参数，默认 False。非 show 路径且 `not always_one_visible` → 直接 `return True`（几何只缓存不刷新，真正定位留给 `_show_always_one`）。`_show_always_one` 改传 `force_apply=True` 强制在 show 前定位。
2. `_hide_always_one` 折叠时 `_width_anim_seq += 1` 失效在飞的宽度补间动画 —— 否则动画线程每 22ms 连发 `resize()`（SWP_SHOWWINDOW）把隐藏窗持续弹回，主守卫罩不住在途 op。

**影响面**：`_apply_geometry` 的所有调用点语义不变（hide 路径原本就不该在隐藏时改几何），仅 `_show_always_one` 这一真正显示入口多传 `force_apply=True`。

### 问题 11：拖球/拖侧栏时都会「贴近主窗右缘就磁吸」+ 中途真吸附

用户「拖动悬浮球时，就算靠近主窗口右边缘也不磁吸，只有按住侧栏拖拽它时才磁吸。且只要没松手，磁吸就不真的发生」。

**根因**：现状的磁吸路径对所有 `panel.moved` 一视同仁——立即计算 `would_dock` 并真改 dock/float/owner。两条触发源都被这条路径捕获：
1. **拖球**：`ball_layer._ball_drag` → `pool._relayout()` → `_apply_geometry` 球分支 `move()` 面板 → pywebview `easy_drag` 把这次 move 视为侧栏被拖 → 派发 `moved` 事件 → `_update_panel_snap` 立即执行 dock/undock。**球拖到主窗右缘 → 面板跟过去 → 立刻磁吸（关球 + dock + AddOwnedForm）**——这是误吸附。
2. **拖侧栏**：用户真按住面板拖，pywebview `easy_drag` 派发 `moved` → `_update_panel_snap` 立即执行。**拖动期间每帧都触发 snap**，用户拖到右缘的途中面板被「贴右」拽回，体验差。

需求拆解：
- 拖球 → 完全不磁吸（屏蔽 snap）
- 拖侧栏 → 进入「预览」模式：缓存意图、不真执行
- 任一拖动 → 松手（moved 静默 ~200ms）后按最后意图一次性落地

**解法**（4 件套，全在 `panel_pool` + `gui` 层，球层零改动）：
1. **池增 4 个状态** + **6 个方法**：
   - 状态：`self._ball_dragging`（球拖动期间 True）、`self._panel_dragging`（侧栏拖动期间 True）、`self._pending_snap`（缓存 `{x, y, dock}`）、`self._drag_settle_timer`（200ms 静默检测松手）。
   - 方法：`preview_snap(x,y,dock)`（仅缓存）、`apply_pending_snap()`（取缓存 + 清标志）、`begin_panel_drag()`（置 `_panel_dragging=True` + cancel 旧 timer）、`arm_drag_settle(after_ms=200)`（重置 timer）、`_drag_settle`（timer 回调，调注册的 cb）、`set_drag_settle_callback(cb)`（gui 注册）、`ball_dragging()` / `panel_dragging()` / `pending_snap()`（查询）。
2. **球拖动 → 直接屏蔽**：`pool._ball_drag` 进 `_ball_dragging=True`；`pool._persist_ball_pos`（球松手）进 `_ball_dragging=False`。`gui._on_panel_moved` 见 `pool.ball_dragging()` True 直接 return，**不进 snap 路径**。球本身不需要回调改动（已有 `on_move_end` 绑 `_persist_ball_pos`）。
3. **侧栏拖动 → 预览模式**：每次 `moved` 都 `begin_panel_drag` + `arm_drag_settle(200ms)`；`_update_panel_snap` 见 `pool.panel_dragging()` True → `preview_snap(x,y,would_dock)` 后立即 return（**不真改 dock/float/owner**）。200ms 内无新 moved → `_drag_settle` 定时器到点 → 调 `gui._on_drag_settle_apply` → 取出缓存 → `_update_panel_snap(x,y)` 重跑（此时 `_panel_dragging=False`，走真执行）。
4. **递归屏蔽**：settle → 真执行 → 调 `set_float_ball`/`_dock_panel` → 派发反向 `moved` → 若不挡会再次触发 settle 自递归。**整体包 `_dock_panel_semaphore=True/finally False`**：同步路径挡掉同一帧内的反向 `_on_panel_moved`；worker 异步派发的反向 moved 落在 finally 之后，视为新事件、走 drag-settle 路径缓存最新意图、自然收敛（不会无限递归，因为再 settle 时位置不变、`set_float_ball(True)` 已 True 早 return、`set_float_ball_pos_from_panel` 幂等、`_ball.move_to` 不派发面板 moved）。

**关键设计点**：球层零改动 —— `_ball_drag`/`_persist_ball_pos` 本就是拖动生命周期的两个钩子（球层 fire-and-forget 调用），只需在池入口翻标志位即可。`_update_panel_snap` body 内 dock/float 两侧的 `_dock_panel_semaphore` 包裹原本只挡 dock 递归（v0.97），v0.167 扩到挡 settle→snap→反向 moved→settle 自递归。

**行为对比**：
- 拖球贴近主窗右缘 → 球到位、面板跟到位，**面板不动、球不动、不吸附**（旧：球到位立刻吸附关球 + 贴右）
- 拖侧栏贴近主窗右缘 → 拖动过程面板**完全不被吸附**（旧：每帧都尝试贴右，会把面板拽回）；松手 ~200ms 后**才吸附/才浮动**（旧：立刻）
- 拖动结束判定：moved 静默 200ms（pywebview WinForms `Move` 事件无单独 release 事件，靠事件间隔兜底）

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 拖动无实时回调 | `BallLayer` 增 `on_move`，DRAGGING 分支 50ms 节流回调 | ball_layer.py / panel_pool.py |
| #2 非并发容器被销毁 | `setConcurrent` + `__idle__`/工具占位 + `relayLiveClear` 非并发只重置内容 | live_panel.js |
| #3 非并发后端多 rid | `assign()` 非并发清旧 rid | panel_pool.py |
| #4 幽灵瘦长 | `_draw_ball` 几何加宽加胖 | ball_layer.py |
| #5 球延迟到首条请求（用户反馈） | 主窗 `_on_loaded` 直接 `refresh_ball_visibility` 建球，不等侧栏 loaded | gui.py |
| #6 启动侧栏误显示（用户反馈） | `_should_show_on_startup`（主开关+磁吸+always_one）替换三处 `live_panel OR always_one` | panel_pool.py / gui.py |
| #7 拖动卡顿（用户反馈） | `_MOUSE_POLL_MS` 30→15（≈64fps），`_FOLLOW_MS` 不变 | ball_layer.py |
| #8 悬浮窗与主窗未解耦层级（用户反馈） | `_adopt_panel_owner` → `_sync_panel_owner`（幂等，按 `_panel_docked` 绑定/解耦） | gui.py |
| #10 S2 折叠后侧栏被弹回（用户反馈） | `_apply_geometry` 隐藏态非 show 跳过原生 move/resize（SWP_SHOWWINDOW 重显）+ `_show_always_one` 传 `force_apply=True`；`_hide_always_one` +1 `_width_anim_seq` 失效在飞补间 | panel_pool.py |
| #11 拖球/拖侧栏中途磁吸（用户反馈） | 池增 `_ball_dragging/_panel_dragging/_pending_snap/_drag_settle_timer` + `preview_snap/apply_pending_snap/begin_panel_drag/arm_drag_settle/_drag_settle/set_drag_settle_callback/ball_dragging/panel_dragging/pending_snap` 9 方法；gui `_on_drag_settle_apply` 落地；snap body 双侧包 `_dock_panel_semaphore` 防自递归 | panel_pool.py / gui.py |
| 互斥（B） | `_should_keep_idle_open`/`_ball_should_show` 互斥 + `_panel_docked` 联动 | panel_pool.py / gui.py |
| 改名（B） | 4 语言 i18n 键 + 提示文案「磁吸状态下始终开启一个」 | app.js |

---

## 6. 是否完全遵循规划路径开发

**基本遵循**，无方案级偏离。

### 完全按规划（无偏离）：

- 互斥用单一模式位 `_float_ball_enabled`，开球=浮动、关球=磁吸。
- always_one 生效域收窄为「磁吸 + 始终开启一个」。
- 拖动实时跟随 = 节流回调 + 拖动中不写盘、松手落盘。
- 非并发容器固定 = 后端清 rid（≤1）+ 前端持久空容器占位（内容可空、容器不缺）。
- 幽灵加宽 = 身体宽>高（允许椭圆）。

### 补充决策（规划未明说）：

- **节流频率取 50ms**：规划只说「连续跟随」，未定频率；实测 50ms 平衡流畅与抖动。
- **非并发「内容可空」用 `__idle__` 哨兵 rid + 空工具卡占位**：规划说「允许内容空、不许不存在」，实现用「持久空容器 + rebind」而非「空时隐藏容器」——保证骨架恒在。
- **`findReusableDone` 把 idle 当可复用槽**：让请求结束后容器停在「空闲」态等 rebind，而非销毁重建。

### 重大调整：无。

---

## 7. 最终实现点

### `src/relay/panel_pool.py`（互斥 + 跟随 + 非并发）

1. **`_should_keep_idle_open()`**：`(not _float_ball_enabled) and _always_one_setting()` —— 浮动模式 always_one 恒不生效。
2. **`_ball_should_show()`**：`_float_ball_enabled and _live_panel_setting() and not _all_hidden` —— 球可见条件。
3. **`_ball_drag(x, y)`**（新增）：拖动中实时更新 `_ball_pos` + `_relayout()`（不写盘），clamp 与 `_persist_ball_pos` 同口径。
4. **`_create_ball()`**：`BallLayer(...)` 增 `on_move=self._ball_drag`。
5. **`assign()`**：`!_concurrent_setting()` 时把非当前 rid 全 `_clear_rid`，保活跃 rid ≤1。
6. **`set_concurrent(enabled)`**（新增）：推 `setConcurrent(...)` 到侧栏前端。
7. **`enforce_concurrent_off()`**：关并发时清全部 rid（已有，复用）。
8. **`set_float_ball(enabled)`**：开球=浮动（不再被 always_one 牵制），关球=磁吸 + 清 S2。

### `src/relay/ball_layer.py`（跟随 + 幽灵）

9. **`__init__`** 增 `on_move=None` 参数 + `_last_follow_ts`。
10. **`_mouse_tick` DRAGGING 分支**：`_present(reposition)` 后 50ms 节流调 `on_move`（`threading.Thread` 跑，`_invoke_move` 包装）。
11. **`_draw_ball` 几何加宽加胖**：body 0.13~0.87s × 0.22~0.80s（宽>高，椭圆）；眼中心 0.37/0.63、眼宽 0.060s；手臂外缘 0.06/0.94。

### `src/relay/gui.py`（互斥联动 + 并发推送 + 球启动即激活）

12. **`set_float_ball(enabled)`**：设 `_panel_docked = not enabled` + `pool.set_float_ball(enabled)`；关球时 `_dock_panel(force=True)`。
13. **`_update_panel_snap(x, y)`**：近右缘 → 关球 + 贴右（磁吸）；离右缘 → 定球位置 + 开球（浮动，球跟随侧栏左上角）。
14. **`set_live_panel_concurrent(enabled)`**：关时 `enforce_concurrent_off()`，无论开关都 `set_concurrent(enabled)` 推前端。
15. **`_on_loaded`（主窗）球启动即激活**：`webview.start()` 后 native 就绪即调 `pool.refresh_ball_visibility()` 建球，不等侧栏 `_on_panel_loaded`（浮动模式空闲隐藏侧栏，侧栏 loaded 要等首条请求才触发 → 球延迟）；幂等 + `_ball_should_show` 门控。
16. **`_on_loaded` / `_on_panel_loaded` 启动强显收窄**：两处启动 dock+show 的 gate 由 `live_panel OR always_one` 改为 `pool._should_show_on_startup()`（主开关 ON + 磁吸 + always_one），修「主开关关/球模式启动侧栏也在」。
17. **`_sync_panel_owner`（替换 `_adopt_panel_owner`）**：按 `_panel_docked` 幂等绑定/解耦侧栏 owned 层级。docked=True（磁吸）→ `AddOwnedForm`；False（浮动）→ `RemoveOwnedForm`。`_panel_owner_bound` 记上次态避免重复。native 变更走 `BeginInvoke` 异步。挂接点：`set_float_ball` / `_update_panel_snap`（磁吸/浮动分支各一）/ `_dock_panel`（force dock 必有，已在 ball_mode guard 后）/ `_on_panel_loaded`（初始同步）。

### `src/relay/web/live_panel.js`（非并发骨架）

15. **`setConcurrent(on)`**：设 `_concurrent`；并发开时移除占位，非并发时 `ensureSingleSkeleton()`。
16. **`ensureSingleSkeleton()`**：`_ridOrder` 空则建 `__idle__` 空完整内容容器 + 空工具卡占位。
17. **`buildToolPlaceholder()` / `removeToolPlaceholder()` / `removeIdlePlaceholder()`**：工具/空闲占位的建删。
18. **`findReusableDone()`**：`badgePhase === "done" || "idle"` 均可复用。
19. **`relayLiveClear(rid)`**：非并发分支只重置内容 + badge「空闲」，不删容器。
20. **`layout()` / `applyTools()` / `removeTool()` / `loadLayoutHint()` / `relayLivePanelInit()`**：接入占位 push/remove，`loadLayoutHint` 拉 `get_live_panel_concurrent` 初始化 `setConcurrent`。

### `src/relay/web/app.js` / `index.html`（改名 + 版本）

21. **改名**：4 语言 i18n 键「磁吸状态下始终开启一个」+ 提示「磁吸模式下无论有无请求都保留一个空闲实时栏」（en/zh-TW/ja/ko）+ HTML 标题/提示 + 注释。
22. **cache**：`index.html` `app.js?v=20260825-02` → `?v=20260825-03`；`live_panel.html` `live_panel.js?v=20260824-03` → `?v=20260825-01`。

### ⚠ 关键约束（底层，勿再踩）

- **互斥**：球与磁吸二选一，`_float_ball_enabled` 是唯一模式位；always_one 只在磁吸生效。
- **拖动跟随**：拖动中不写盘（松手才落盘），回调新线程跑（UI 线程永不碰 `pool._lock`）。
- **非并发容器**：骨架恒在、内容可空、不可缺不可多；后端活跃 rid 恒 ≤1。

### 行为验收清单（手动测试项）

- [ ] 开「悬浮球」→ GUI 启动（主窗 loaded）时球立即出现，不等首条请求流
- [ ] 主开关（实时流侧栏）OFF → 启动时侧栏不显示；开「悬浮球」→ 只有球出现，侧栏仍隐藏
- [ ] 磁吸 + always_one ON → 启动侧栏常驻；磁吸 + always_one OFF → 启动不显示、首个请求到才弹
- [ ] 拖动球 → 顺滑无卡顿（~64fps）
- [ ] 浮动（球）模式：侧栏是独立顶层窗 —— 主窗被遮挡/最小化/回顶时侧栏**不**跟随退后/隐藏/回弹（解耦层级）
- [ ] 磁吸 dock 态：侧栏与主窗同层级 —— 主窗最小化侧栏跟着藏、还原跟着回（绑定层级）
- [ ] 拖侧栏近右缘（磁吸）→ 绑定；拖离右缘（浮动）→ 解耦；来回切换层级正确跟随
- [ ] 开「悬浮球」→ 球出现，侧栏从球位置展开（浮动）；关球 → 侧栏贴主窗右缘（磁吸）
- [ ] 拖侧栏近右缘 → 关球贴右；拖侧栏离右缘 → 开球跟随（互斥双向）
- [ ] 拖动球 → 侧栏连续跟随（松手前就贴住球位置），松手后位置持久化，重启还原
- [ ] 关「允许并发」→ 侧栏固定显示 1 端点 + 1 完整内容 + 1 工具容器，内容空时容器仍在、不消失不重复
- [ ] 非并发下连续发 2 个请求 → 第 2 个顶掉第 1 个，仍只有 1 个内容容器
- [ ] 请求结束 → 容器回「空闲」，不销毁
- [ ] 「磁吸状态下始终开启一个」ON + 磁吸 → 无请求时侧栏常驻；OFF → 无请求时隐藏
- [ ] 浮动（球）模式下 always_one 不生效，侧栏显隐由球 S1/S2 控制
- [ ] 顶层「实时流侧栏」OFF → 球与侧栏全关
- [ ] 小幽灵宽胖（宽>高椭圆），眼睛间距随脸宽拉开
- [ ] `py_compile` ball_layer.py / panel_pool.py / gui.py 通过；`node --check` live_panel.js / app.js 通过

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | 改（互斥 `_should_keep_idle_open`/`_ball_should_show`、`_ball_drag`、`assign` 非并发清 rid、`set_concurrent`、`set_float_ball`） |
| `src/relay/ball_layer.py` | 改（`on_move` 参数 + 拖动节流回调 `_invoke_move` + 幽灵几何加宽） |
| `src/relay/gui.py` | 改（`set_float_ball` 互斥联动、`_update_panel_snap` 双向、`set_live_panel_concurrent` 恒推） |
| `src/relay/web/live_panel.js` | 改（`setConcurrent` + `__idle__`/工具占位 + `relayLiveClear`/`findReusableDone`） |
| `src/relay/web/app.js` | 改（4 语言改名「磁吸状态下始终开启一个」） |
| `src/relay/web/index.html` | 改（JS cache `?v=20260825-02` → `?v=20260825-03`） |
| `src/relay/web/live_panel.html` | 改（JS cache `?v=20260824-03` → `?v=20260825-01`） |
