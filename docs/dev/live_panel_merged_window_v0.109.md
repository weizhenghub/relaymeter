# 实时栏单窗口合并（v0.109）开发文档

## 1. 用户的初始指令

> 能否将主栏和侧栏合并为实际一个窗口？现在侧栏和主栏还是有一点间隔，他们实际上是两个窗口
>
> 当出现并发时，将侧栏宽度向右侧延长（重设窗口宽度）。这是能够做到的吗？
>
> 可以些。现在的侧栏主栏和侧栏侧栏的样式全部不变，只是在并发时侧栏侧栏的弹出方式改成延长原侧栏主栏的宽度到可以同时包括侧栏主栏和侧栏侧栏。请分析此需求
>
> 实现

**背景**（本次指令之前已确认的方案）：v0.108 起并发实时栏改为「always_one 主面板 + 1 个独立精简版网格窗口」，右侧延展。功能正确，但主栏与侧栏是**两个 OS 窗口**，视觉上有间隔。本次指令要求合并为**一个窗口**：空闲/单请求时窗口只有主栏宽；并发时窗口宽度向右侧延长，右侧露出网格区；两侧样式完全不变。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 主栏（live_panel 全量内容）与网格区（并发流块）合并为**同一个 OS 窗口** | 指令 1 |
| B | 空闲 / 单请求 → 窗口宽度 = 主栏宽（400px），只显示主栏区 | 指令 2 |
| C | 并发 ≥2 → 窗口宽度向右侧**延长**到包含主栏 + 网格区（2×panel_width） | 指令 2 |
| D | 主栏区样式、网格区样式**全部不变**（视觉零改动） | 指令 3 |
| E | done → 块保留约 10s 后清除，网格区块清空 → 窗口收窄回主栏宽 | 指令 2（隐含延续 v0.108 语义） |
| F | 无独立「网格窗口 X」按钮 —— 全窗口只有一个右上角 X（关整窗） | 指令 3（合并的自然推论） |

### 隐含但需要确认 / 需自行决策的点

- 网格区显隐机制：CSS 类切换（`body.has-grid`）vs 每帧 JS 判断 —— 选前者（无块时 CSS `display:none`，收窄后天然隐藏）
- 窗口宽度变化时机：只有「有块 / 无块」两态，宽度只在变化时 resize（避免抖动）
- 网格区宽度 = panel_width（400），与主栏同宽 —— 保持"两块各占一段"的视觉，符合用户"侧栏侧栏"原尺寸
- 主题：合并后同 document，`setTheme` 一次整窗生效，删掉网格窗口单独的 `relayGridInit` 重试链
- 废弃文件：`live_panel_grid.html` 失去引用，成为死文件

---

## 3. 分析需求后得出的开发路径

### 3.1 方案定型

**永远只有 1 个 OS 窗口**（v0.109）。窗口内横向排两区：

```
空闲 / 单请求：      ┌──主窗──┐ ┌──实时栏窗口 400px──┐
                    └────────┘ └主栏区（全量）───────┘

并发 ≥2：            ┌──主窗──┐ ┌──实时栏窗口 800px──────────────┐
                    └────────┘ └主栏区（请求①全量）──网格区（②③流块）──┘
```

- **位置**：窗口贴主窗右缘 `(mx+mw, my)`，高度跟随主窗 `mh`（底部对齐，同 v0.108）
- **宽度** = `_merged_width()`：网格块数 > 0 → `2×panel_width`；否则 `panel_width`
- **网格区显隐**：前端按「有无块」切 `body.has-grid`；无块时 `.grid-region` `display:none`
- **数据流**：`push_event` 网格分支把 `relayGridEvent(ev)` 推到同一个 `always_one_window`；`_clear_grid_block` 把 `relayGridClear(rid)` 也推到同一窗口

### 3.2 开发顺序

```
#1 panel_pool.py：删 grid_window/网格窗口管理，路由改到 always_one_window
   （_merged_width / _apply_geometry / _relayout 重写）
#2 gui.py：删 grid_url 参数 / hide_grid_panel bridge / _push_theme_to_panels 简化
   / _quit_from_tray 网格销毁
#3 live_panel.html：body 改横向 flex（.live-panel-main + .grid-region），载入两个 JS
#4 live_panel.css：.live-panel-main / .grid-region / body.has-grid 显隐
#5 live_panel_grid.js：删独立 setTheme/relayGridInit/X，emptyVisible 切 has-grid
#6 smoke_worker.py：适配单窗口模型
#7 并发验证 + 清理废弃代码 + 开发文档
```

---

## 4. 实现中遇到的问题

### 问题 1：两个 OS 窗口 → 一个窗口的宽度动态化

v0.108 的 `_relayout` 分别 move/resize 两个窗口（always_one 固定 400 宽，grid 独立 400 宽在右侧）。合并后只有一个窗口，宽度要两态（400 / 800）动态切。

**风险**：每次 SSE 事件都调 `_relayout`，如果每次都 `resize()` 会触发大量无谓 native resize 抖动（WinForms 高频 Resize 事件）。

### 问题 2：网格区显隐靠什么

窗口加宽后右侧露出网格区，但「无块」时右侧必须不可见（收窄前的一瞬不能闪白板）。纯靠 Python resize 宽度不够 —— 加宽后 CSS 必须同步决定右侧是否渲染。

### 问题 3：合并后路由目标变了

`push_event` 网格分支原先 `relayGridEvent` 到 `self.grid_window`；删除网格窗口后要改为推给 `self.always_one_window`。`_clear_grid_block` 同理。漏改任何一处都会丢并发事件。

### 问题 4：主题初始化链路冗余

v0.108 网格窗口是独立 WebView2，需要独立的 `_on_grid_loaded` → `relayGridInit`（含 pywebview loaded 0 参数陷阱 + 4 次重试）。合并后网格区与主栏**同 document**，主题由 `live_panel.js` 的 `setTheme` 一次性覆盖整窗 —— 整条网格主题初始化链成为死代码。

### 问题 5：并发验证时"主栏空白"（排查类问题）

重启 GUI 后首跑并发，用户报「侧栏主栏没显示，直接到侧栏侧栏去了」。读 `.panel-pool-trace.log` 发现**所有请求 `slot='grid'`，从没有一次 `always`** —— 主栏永远空闲占位。

**根因**：不是 v0.109 代码 bug。那次跑的 GUI 进程加载的是旧版 panel_pool（v0.108 双窗口语义），重启加载新代码后 assign 恢复正常（trace 铁证：`POOL_START` 后第一个请求 `slot='always'`）。**这是「旧进程跑旧代码」的经典陷阱，代码本身正确。**

### 问题 6：清理时发现的隐藏死代码

清理废弃代码时发现：
- `_trace()` —— 临时诊断函数（写项目根 `.panel-pool-trace.log`），注释自明"排查完删除"，但调用点还在 release / gridclear / SSE 路由
- `_max_setting()` / `_clamp_max()` —— 无任何调用方（v0.104 的 max 上限语义在 v0.108 网格模型下已 no-op）
- `hide_panel_for_window()` —— 无调用方（v0.108 由 `hide_grid_panel` bridge 调用，该 bridge 随合并删除）
- `live_panel_grid.html` —— 孤儿文件，无任何代码引用

---

## 5. 最后如何解决

| 问题 | 解决方案 | 位置 |
|---|---|---|
| #1 宽度动态化 | 新增 `_merged_width()`（块>0 → 2×panel_width）；`_apply_geometry()` 只在**宽/高真正变化**时 `resize`，move 每次派发；`_last_panel_w/h` 缓存 | panel_pool.py |
| #2 网格区显隐 | CSS：`.grid-region` 默认 `display:none`，`body.has-grid .grid-region` 才 `display:flex`；`emptyVisible()` 按 `#grid` 是否有 `.grid-block` 切 `body.has-grid`。窗口收窄时网格区天然隐藏，无闪白 | live_panel.css / live_panel_grid.js |
| #3 路由改目标 | `push_event` 网格分支、`_clear_grid_block` 的 evaluate_js 目标从 `grid_window` 改为 `always_one_window`；`assign` 不再懒建网格窗口（无上限） | panel_pool.py |
| #4 主题链清理 | 删 `_ensure_grid_window`/`_on_grid_loaded`/`_grid_init_retry`/`relayGridInit`/网格 `setTheme`；主题统一走 `live_panel.js` 的 `setTheme`（gui `_push_theme_to_panels` 只推 panel_window） | panel_pool.py / gui.py / live_panel_grid.js |
| #5 主栏空白 | 定位为旧进程跑旧代码，重启后 assign 正常（trace 证据）；加了 `ASSIGN_CHECK` 诊断 trace 佐证后移除 | 诊断 trace（已清理） |
| #6 死代码 | 删 `_trace` 函数 + 调用点（路由日志转 `_logger.info`，进 `relay-gui.log`，跨重启保留）；删 `_max_setting`/`_clamp_max`/`hide_panel_for_window`；删 `live_panel_grid.html` | panel_pool.py / web |

---

## 6. 是否完全遵循规划路径开发

**部分偏离（偏离均为合理收敛，无重大返工）**。

### 完全按规划（无偏离）：

- 永远 1 个 OS 窗口，宽度两态（400 / 800）动态切
- 主栏区 / 网格区样式完全复用既有 CSS，视觉零改动
- 并发路由：第 1 个请求进主栏（always），第 2+ 进网格区（grid）
- done → 块保留 10s 后清除，清空后窗口收窄回主栏宽
- 网格区无独立 X，全窗口一个右上角 X（关整窗）
- 主窗拖动 → 窗口跟右缘移动（refresh_geometry 不变）

### 偏离之处：

- **(a) 网格方向在 v0.108 内曾有调整**：早期方案是"块向右延展、窗口宽度随块数增长"，用户看后纠正为"侧延展栏内**纵向堆叠** 2-3 个"。v0.108 落地为固定宽窗口内纵向 flex。本次 v0.109 合并时继承纵向堆叠，宽度只在「有无块」两态切换（不再随块数变宽）。**合并方案按用户 v0.109 指令实现，方向已在 v0.108 定稿。**
- **(b) 网格区宽度定为 panel_width（400）**：用户只说"延长到可以同时包括侧栏主栏和侧栏侧栏"。侧栏区原 400 宽，合并后右侧网格区固定 400，窗口总宽 800。**符合"样式全部不变"。**
- **(c) `_max_setting`/`_clamp_max` 无调用方**：v0.104 的"最多显示数量"在网格模型下失去意义（无上限），`enforce_max` 保留为 no-op 兼容设置页 API，但读设置的 `_max_setting` 成了死代码，清理删除。**清理动作超出"合并"范围，用户明确要求清理废弃代码。**
- **(d) 诊断 trace 临时加删**：为定位"主栏空白"加了 `ASSIGN_CHECK`/`POOL_START`/`ALWAYS_ONE_CLOSING` trace，验证后连同原有 `_trace` 一起清理。**纯排查手段，非功能。**
- **(e) `live_panel_grid.html` 删除**：失去引用的孤儿文件。**用户要求清理废弃代码。**

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`panel_pool.py`** —— 单窗口合并模型
   - **删除**：`grid_window`/`grid_visible`/`grid_manually_hidden` 属性、`_ensure_grid_window`/`_show_grid`/`_hide_grid`/`_on_grid_closing`/`_on_grid_loaded`/`_grid_init_retry`/`_relayout_grid` 方法、`grid_url` 参数、`_trace` 函数、`_max_setting`/`_clamp_max`、`hide_panel_for_window`
   - **保留**：`grid_rids`/`grid_blocks`/`_note_grid_blocks`/`_grid_block_count`（宽度计算用）、worker 队列、watchdog（grid stale 自愈 / worker 卡死替换）、always_one 常驻语义
   - **路由**：`push_event` 网格分支 → `relayGridEvent` 到 `always_one_window`；`_clear_grid_block` → `relayGridClear` 到 `always_one_window`
   - **`assign`**：always 空闲优先（`!always_one_rid && !manually_hidden`）；否则并发开 → grid（无上限）；并发关 → 拒绝
   - **`_merged_width()`**：`_grid_block_count() > 0` → `2×panel_width`，否则 `panel_width`
   - **`_apply_geometry()`**：贴主窗右缘 + 高度跟随主窗；resize 只在宽/高变化时派发（`_last_panel_w/h` 缓存）
   - **`_relayout()`**：收敛为 `_apply_geometry()`
   - **`enforce_concurrent_off`**：清空全部网格块（窗口收窄回主栏宽）
   - **路由日志**：`_trace`（`.panel-pool-trace.log`）→ `_logger.info`（`relay-gui.log`，跨重启持久）

2. **`gui.py`**
   - 删 `grid_url=(_WEB_DIR / "live_panel_grid.html").as_uri()` 参数
   - 删 `hide_grid_panel` bridge
   - `_push_theme_to_panels`：只推 panel_window（一次 setTheme 整窗生效）
   - `_quit_from_tray`：移除网格窗口 destroy（panel_window 即唯一侧栏窗口）

### 前端

3. **`live_panel.html`**：body 改横向 flex
   - `.live-panel-main`（左，400px，原主栏全部内容原样包入）
   - `.grid-region`（右，`#grid` 块区 + `#grid-empty` 占位）
   - 加载 `live_panel.js` + `live_panel_grid.js` 两个脚本

4. **`live_panel.css`**：
   - `.live-panel-body`：`flex-direction:row; height:100vh; overflow:hidden`
   - `.live-panel-main`：`width:400px; height:100vh; padding:32px …; overflow-y:auto`（原 body 样式）
   - `.grid-region`：默认 `display:none`；`body.has-grid .grid-region` 才 `display:flex`；`width:400px; height:100vh`
   - `.grid-rows` / `.grid-block` / `.grid-empty` 保留（区域级样式不变）

5. **`live_panel_grid.js`**：
   - `emptyVisible()`：按块数切 `document.body.classList.toggle("has-grid", has)`
   - 删 `#grid-btn-close` X handler、`setTheme`、`relayGridInit`（同页由 live_panel.js 统一处理）

6. **`live_panel_grid.html`**：**删除**（孤儿文件，无任何引用）

### 清理废弃代码汇总

| 废弃代码 | 类型 | 处理 |
|---|---|---|
| `live_panel_grid.html` | 孤儿文件 | 删除 |
| `_trace()` + 调用点 | 临时诊断（注释自明"排查完删除"） | 删除；路由日志转 `_logger.info` |
| `_max_setting()` / `_clamp_max()` | 无调用方 | 删除 |
| `hide_panel_for_window()` | 无调用方 | 删除 |

### 测试与验证

- `smoke_worker.py`：适配单窗口模型（删 `grid_window` mock，断言 `relayGridEvent` 派发到 `always_one_window`，`_merged_width()` 两态）—— 4 测试全绿
- 3 并发实测（`test_concurrent_panel.py 3 20 e1701fa6`），`.panel-pool-trace.log` 证据：
  - 第 1 请求 `slot='always'`（主栏）
  - 第 2、3 请求 `slot='grid'`（网格区），窗口加宽 800px
  - done → 块 10s 延迟清除，窗口收窄回 400px，主栏常驻

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | 改（单窗口合并 + 死代码清理） |
| `src/relay/gui.py` | 改（删 grid_url / hide_grid_panel / 主题推送简化 / tray 销毁） |
| `src/relay/web/live_panel.html` | 改（body 横向 flex：主栏区 + 网格区） |
| `src/relay/web/live_panel.css` | 改（.live-panel-main / .grid-region / body.has-grid） |
| `src/relay/web/live_panel_grid.js` | 改（has-grid 切换，删主题/X） |
| `src/relay/web/live_panel_grid.html` | **删除**（孤儿） |
| `smoke_worker.py` | 改（适配单窗口模型） |
