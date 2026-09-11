# 悬浮球 S2 收起「先放大到侧栏大小再收缩」修复（v0.198.1）开发文档

## 1. 用户的初始指令

> 现在悬浮球再有流（侧栏展开）状态下，点击切换到 S2，会将整个悬浮球球放大到侧栏的大小然后才开始收缩，而不是侧栏收缩到悬浮球大小。

(背景)v0.198 已声称修复过此问题（「修复悬浮球转换为 S2 时异常放大的问题」），但用户实测**未解决**。本次是真正的根因定位 + 修复。

### 场景拆解

- **期望行为**：有流展开态（窗口 = 球帽 + 侧栏）点球帽切 S2 → 面板从当前大小**平滑收缩**回球帽（56×56）。
- **实际行为**：点球帽瞬间整个**球帽被放大到侧栏大小**（整窗被一个巨球占满），然后才开始收缩。
- **关键边界**：v0.184.2 已有「延迟球帽翻转」（收起动画结束后再 ghostSetState(done)），说明串行动画顺序的意图早就存在，但漏了一条路径。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 收起动画期间渲染层不能提前切「收起布局」 | 现象 |
| B | 面板隐藏 + 球帽翻转都必须延迟到收起动画结束 | 现象 + v0.184.2 已有意图 |
| C | 展开路径保持同帧出现（不能误伤展开） | 隐含 |

### 隐含但需要确认的点

- 收起动画的"起点"在 `_anim_resize` 里读窗口**当前实际尺寸**（Electron `getBounds()`），所以窗口收缩本身是对的 —— 问题只出在渲染层提前切了 collapsed。
- 磁吸（dock）模式不经过 `_apply_expanded_state` 的收起分支，不受影响。

---

## 3. 分析需求后得出的开发路径

### 核心方案：收起渲染延迟到动画 on_done（渲染层零改动）

```
S1 → S2（有流展开态）
  ball_clicked
    → _sync_geometry_to_mode：_expanded=False，球帽翻转记入 _deferred_ball_state
    → _apply_expanded_state：〔原〕立即 ghostSetExpanded(false) → 球帽被拉满窗 ← 根因
                            〔新〕只记 _deferred_expanded_render=False，不动渲染层
    → _relayout → _apply_geometry → _anim_resize(56,56, on_done=flush)
    → 动画期间 body 保持展开布局：球帽恒 56px 左上，面板被窗口收缩自然压没
    → on_done：_flush_deferred_render = ghostSetExpanded(false) + set_expanded(false)
              + ghostSetState(done)
```

### 根因（决定性）

`panel_pool._apply_expanded_state` 第一行就 `ghostSetExpanded(false)` → body 加 `collapsed` → `ghost_panel.html` 的

```css
body.collapsed #ghost-cap { flex: 0 0 100%; width: 100%; height: 100%; }
```

把 56px 的球帽 SVG **拉满整个侧栏窗口**（476×900），然后才等 `_anim_resize` 把整窗缩回 56×56 —— 所以用户看到"球先放大到侧栏大小再收缩"。

v0.184.2 的 `_deferred_ball_state` 只延迟了球帽翻转（ghostSetState done），**没延迟 `ghostSetExpanded(false)`** —— 这就是 v0.198 声称修复却未解决的原因（只修了一半）。

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 收起渲染时机 | 延迟到 `_anim_resize` 的 `on_done` | 动画期间必须保持展开布局，球帽才恒 56px |
| 展开渲染时机 | **保持同帧立即下发** | 展开 = 面板要立刻出现，与窗口扩窗同帧；不能被延迟逻辑误伤 |
| 面板隐藏与球帽翻转 | 合并到一个 `_flush_deferred_render` | 两件事都是"收起动画结束后的收尾"，同一 on_done 落地、顺序固定（先藏面板再翻帽） |
| 渲染层（ghost_panel.html / ball_main.js） | 零改动 | 根因在 Python 侧的时序，不在前端 |

---

## 4. 实现中遇到的问题

### 问题 1：v0.198 声称修复但未解决 —— 只延迟了球帽翻转

v0.184.2 引入 `_deferred_ball_state`（球帽翻转延迟到收起动画 on_done），v0.198 沿用它修"S2 放大"，以为延迟翻转就够了。但放大来自 **`ghostSetExpanded(false)` 提前执行**触发的 CSS 拉伸，跟球帽翻转（ghostSetState done）是两条独立的下发路径 —— 只延迟后一条，球帽照样被拉满窗。

**解法**：把"面板隐藏"也纳入延迟，与球帽翻转合并成 `_flush_deferred_render`。

### 问题 2：展开路径不能被延迟逻辑误伤

若把"面板隐藏延迟"简单套用到所有 `_apply_expanded_state` 调用，展开（True）也会被延迟 —— 面板迟迟不出来。

**解法**：`_apply_expanded_state` 内显式分支 —— `expanded=True` 时清掉 deferred、**立即** `ghostSetExpanded(true)` + `set_expanded(true)`；只有 `expanded=False` 才记 `_deferred_expanded_render=False` 走延迟。

### 问题 3：被打断 / 首次几何 / 无动画路径的兜底

`_anim_resize` 有早退路径（起始即已到目标尺寸 / 被新一轮动画接管 / 隐藏打断），这些路径 `on_done` 不会被调用 —— 若只看 on_done，deferred 会永远悬着，球帽一直不翻、面板一直不藏。

**解法**：`_apply_geometry` 里原有 3 处 `_flush_deferred_ball_state` 调用点（首次几何、动画 on_done、无动画时的兜底）全部同步换成 `_flush_deferred_render`，保证任何路径都收敛。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 球帽被拉满窗 | 面板隐藏（`ghostSetExpanded(false)` + `set_expanded(false)`）延迟到收起动画 on_done | panel_pool.py |
| #2 展开被误伤 | `_apply_expanded_state` 展开立即渲染 / 收起延迟渲染分支 | panel_pool.py |
| #3 打断路径悬置 | 3 个 flush 调用点统一换 `_flush_deferred_render` | panel_pool.py |

---

## 6. 是否完全遵循规划路径开发

**完全按规划**：核心方案（收起渲染延迟到动画 on_done、渲染层零改动）从定位到实现没有走偏，也没有额外偏离。

### 偏离之处

- 无。唯一前期误判是"先查历史修改"，被用户叫停（历史修改并未解决问题）；随后转向复现 + 逐行时序分析直接命中根因。

---

## 7. 最终实现点

### `src/relay/panel_pool.py`

1. **新字段 `_deferred_expanded_render: Optional[bool] = None`**（构造，`_deferred_ball_state` 旁）：
   - 语义 = 「收起动画结束后要落地的渲染层目标」。`False` = 收起（隐藏面板 + 停 hover）；`None` = 无待办；`True` 预留（展开不需要延迟，实际不写 True）。

2. **`_apply_expanded_state()`（行 420）** 改时序：
   - `expanded=True`（展开）：`_deferred_expanded_render=None`；立即 `ghostSetExpanded(true)` + `set_expanded(true)`（与扩窗动画同帧）。
   - `expanded=False`（收起）：只记 `_deferred_expanded_render=False`，**不动渲染层**；`_relayout` 让 `_anim_resize` 先把窗口缩成球帽。
   - 收起动画期间 body 保持展开布局 —— 球帽恒 56px 左上，面板被窗口收缩自然压没，全程无"球帽被拉满窗"。

3. **`_flush_deferred_ball_state()` 升级为 `_flush_deferred_render()`（行 654）**：
   - 先落地面板隐藏：`ghostSetExpanded(false)` + `set_expanded(false)`（恢复 hover 判定）。
   - 再落地球帽翻转：`ghostSetState(done)`（沿用 `_set_ball_state`）。
   - 任一为空则跳过对应项（幂等）。

4. **`_apply_geometry()` 3 处调用点**（行 1343 / 1348 / 1354）：
   - 首次几何 → `_flush_deferred_render()`
   - 收起动画 `on_done` → `_flush_deferred_render`
   - 尺寸未变 + 动画不在飞（打断/恢复兜底）→ `_flush_deferred_render()`

### 渲染层（零改动）

- `src/relay/web/ghost_panel.html`：`body.collapsed #ghost-cap { flex: 0 0 100% }` 未动 —— 现在只有窗口已缩成球帽后才会收到 `ghostSetExpanded(false)`，此时球帽满窗 = 正常的球，不再放大。
- `src/relay/electron_app/ball_main.js`：`resize`/`set_expanded` 未动。

### 验证（独立 ElectronBallWindow 实测，不起 GUI）

| 场景 | 球帽宽度 | 结论 |
|---|---|---|
| 展开态 476×900（非 collapsed） | 56px | 展开布局球帽恒 56 ✓ |
| 旧行为：`ghostSetExpanded(false)` 后 | 476px | 根因坐实（球帽被拉满窗） |
| 修复后收起动画中间帧（窗口 400→56） | 56px 全程 | 球帽不被拉大，面板被窗口压没 ✓ |
| 修复后 collapsed@56×56 | 56px | 缩成球后隐藏面板，球帽满窗 = 正常 ✓ |

### 行为验收清单（手动测试项）

- [ ] 悬浮球 → 有流（侧栏展开）→ 点球帽切 S2 → 面板**平滑收缩**成球帽，不再先放大到侧栏大小
- [ ] 收起动画期间球帽恒 56px（不闪大）
- [ ] S2 → 再点回 S1 → 面板正常展开、球帽形态恢复 flow
- [ ] 无流状态下 S1↔S2 切换不出现任何放大
- [ ] 收起动画过程中来新流 / 打断 → 收敛正常（无悬置的球帽翻转）
- [ ] 磁吸模式（球关）不受影响

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | 改（`_deferred_expanded_render` 字段 + `_apply_expanded_state` 展开/收起分时序 + `_flush_deferred_render` + 3 个调用点） |
| `src/relay/web/ghost_panel.html` | 不改（CSS 保持，时序修复后不再触发放大） |
| `src/relay/electron_app/ball_main.js` | 不改 |
