# 新容器出现后粘底失效修复（v0.147）开发文档

## 1. 用户的初始指令

> 有新容器出现时，原有还在跑的内容容器就不会自动滚动到底部，修复此问题

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 已有流式容器保持自动滚底（粘底） | 指令（现象：新容器出现后失效） |
| B | 新容器出现 / 列结构变化时不得打断原有容器的粘底状态 | 指令（触发条件 = 新容器） |
| C | 修复后：单容器流式粘底、多容器共存、跨列重挂三种场景都要正确 | 指令 + 现有行为保持 |

### 隐含但需要确认的点（设计自决）

- **粘底机制已存在**：`isSticky(el)`（live_panel.js:143）在 `scrollTop+clientHeight >= scrollHeight-24` 时判定为「用户贴底」，流式写入后把 `scrollTop` 推到 `scrollHeight`（:648 / :656）。所以问题不在流式写入逻辑，而在「新容器出现」这个动作**把已有容器的滚动状态破坏了**。
- **触发点是 `placeColumns`**：每次 layout（新 rid 建容器、rid 清除、列数变化）都会走 `placeColumns` 把列内元素 detach 再 re-append —— 这个重挂正是「新容器出现」时一定会执行的路径。
- **jsdom 无布局**：`scrollHeight/clientHeight` 恒 0，测试必须 mock 布局尺寸才能让粘底判定有意义。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位触发链路

```
新容器 delta → applyRid → buildContainer → scheduleLayout → layout()
  → placeColumns(cols)          ← 每次布局必走
    → while (colEl.firstChild) colEl.removeChild(...)   ← 无条件清空重挂
      → 流式容器 detach → 重挂时滚动区重建 scrollTop 归 0
        → isSticky(el) = scrollTop(0) + clientHeight >= scrollHeight - 24  → false
          → 后续流式写入不再滚底（粘底失效）
```

关键点：`placeColumns` 的 detach/reattach 是滚动位置丢失的唯一来源。**不重挂的列根本不该动**；**必须重挂的列要在重挂后把滚动位置还给原容器**。

### 第二阶段：方案设计

三条互补策略：

1. **同序列跳过重挂（主修复）**：若列内现有子节点集合 + 相对顺序与目标完全一致（排除静态 `$endpoint`），直接跳过该列的 remount。流式容器不被移出 DOM → scrollTop 自然保留 → 粘底不断。这是「新容器出现」场景（往已有列末尾追加，已有容器顺序不变）的主路径。
2. **滚动快照恢复（兜底）**：对确实需要重挂的列，先对所有可滚动容器快照 scrollTop（`.live-panel-stream` / `.live-panel-pre` / `.live-panel-tool-args` / `.lp-ep-list`），重挂后仅对仍连接在文档的节点恢复。
3. **仅恢复 connected 节点**：被移走的容器（换 rid / 跨列重排）不写回，避免误恢复僵尸节点的位置。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | 定位 `placeColumns` 无条件重挂（live_panel.js:1006） | 无 |
| 2 | 加 scrollSnap 快照/恢复 | #1 |
| 3 | 加 sameOrder 跳过重挂分支 | #2 |
| 4 | jsdom harness 验证（sticky_remount.js） | #3 |
| 5 | 全回归 | #4 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 主修复 | 同序列跳过重挂 | 不 detach = scrollTop 天然保留；「新容器追加」绝大多数情况命中 |
| 兜底 | 快照 + 恢复 scrollTop | 覆盖真正重挂（清除/跨列移动）场景，保证位置不丢 |
| 恢复条件 | `sc.isConnected && top > 0` | 防僵尸节点写回；top=0 不必写（本就该是 0） |
| 快照范围 | stream/pre/tool-args/ep-list | 与 `applyRid` 里 `isSticky` 作用到的滚动容器一致（:1023-1027） |
| 判定粒度 | 按列比对 `el ===` 引用相等 | 容器对象未变（复用容器时 rebind 改内容不换对象），引用相等即未重挂 |
| 测试 | mockScroll defineProperty | jsdom 无布局，scrollHeight=1000/clientHeight=200 让粘底判定有意义 |

---

## 4. 实现中遇到的问题

### 问题 1：`$endpoint` 在 col-1 顶部的干扰

**症状**：跳过重挂判定若把 `$endpoint` 也算进 `kids`，则「列内有无 endpoint」变化会让 sameOrder 误判（endpoint 是静态的，位置不该参与容器顺序判定）。

**解法**：比对时用 `filter(c => c !== $endpoint)` 排除 endpoint；col-1 时单独用 `insertBefore($endpoint, colEl.firstChild)` 保证其始终在最前（:1033-1039）。

### 问题 2：jsdom 无布局 → 测试首两版全红

**症状**：jsdom 的 `scrollHeight/clientHeight` 恒 0。首版 sticky_remount.js 里 `applyRid` 的粘底分支 `scrollTop = scrollHeight(=0)` 把 mock 的 800/1000 全部覆盖成 0，断言全挂；第二版只 mock scrollHeight 仍 0。

**解法**：第三版用 `Object.defineProperty` 同时 mock `scrollHeight=1000`、`clientHeight=200`（`configurable: true`），让 `isSticky` 判定（`scrollTop+200 >= 1000-24`）和滚底写入（`scrollTop=1000`）都有真实值；且 mock 必须在「已滚到接近底部」之后（`stream.scrollTop=800` 手动预置），保证 `applyRid` 写入走粘底分支而非覆盖归零。

### 问题 3：S2 断言「继续流式仍滚底」的时序

**症状**：新容器 r2 出现后，原容器 r1 的 delta 要继续写流式，但 r1 此时若未被跳过重挂（或快照没恢复），`isSticky` 会因 scrollTop 归 0 判 false。

**解法**：这正是回归的核心断言 —— r2 加入后 r1 的 scrollTop 必须仍是 1000，再发 r1 delta 验证写入仍走粘底分支。三个 S2 断言（容器数=2、scrollTop 保留 1000、续流式仍 1000）全绿即证明跳过重挂生效。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 endpoint 干扰判定 | 比对时排除 `$endpoint` + 单独保证其置顶 | live_panel.js `placeColumns` |
| #2 jsdom 无布局 | mockScroll defineProperty（scrollHeight=1000 / clientHeight=200） | sticky_remount.js |
| #3 续流式粘底时序 | S2 三连断言（容器数 / 保留 1000 / 续写仍 1000） | sticky_remount.js |

**最终 `placeColumns` 关键分支（live_panel.js:1006）：**

```js
// v0.147：重挂前快照所有滚动容器的 scrollTop —— removeChild+appendChild
// 会把可滚动元素的位置重置（detach 后滚动区重建归零），导致正在粘底
// 流式的容器停止自动滚底。快照后重挂结束时恢复。
const scrollSnap = [];
document.querySelectorAll(
  ".live-panel-stream, .live-panel-pre, .live-panel-tool-args, .lp-ep-list"
).forEach((sc) => { scrollSnap.push([sc, sc.scrollTop]); });

colEls.forEach((colEl, ci) => {
  const want = cols[ci].els;
  // v0.147：列内容集合 + 相对顺序都没变 → 跳过重挂（不 detach 任何容器）。
  const kids = Array.prototype.filter.call(colEl.children, (c) => c !== $endpoint);
  const sameOrder = kids.length === want.length && want.every((it, i) => kids[i] === it.el);
  if (sameOrder) {
    if (ci === 0 && $endpoint && colEl.firstChild !== $endpoint) {
      colEl.insertBefore($endpoint, colEl.firstChild);
    }
    return;  // 不重挂，流式容器滚动位置/粘底状态自然保留
  }
  while (colEl.firstChild) colEl.removeChild(colEl.firstChild);
  if (ci === 0 && $endpoint) colEl.appendChild($endpoint);
  want.forEach((it) => { colEl.appendChild(it.el); });
});
// 重挂后恢复滚动位置（仍连在文档的节点才恢复）。
scrollSnap.forEach(([sc, top]) => {
  if (sc.isConnected && top > 0) sc.scrollTop = top;
});
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **同序列跳过重挂**：不 detach 流式容器 → 粘底天然保留（新容器出现主路径）。
- **快照 + 恢复**：真正重挂（清除 / 跨列移动）后滚动位置还给原容器。
- **jsdom mock 布局**：`mockScroll` 让粘底判定和滚底写入有意义，S1/S2/S4 全部真实验证。

### 偏离之处：

- **(a) 快照范围比最初设想宽**：从只覆盖 `.live-panel-stream` 扩到 stream/pre/tool-args/ep-list 四个滚动容器。原因：`placeColumns` 可能重挂的不只是流式容器，pre（纯文本）/ tool-args（工具参数）/ ep-list（端点列表）任何带滚动状态的容器都该保留位置 —— **视觉/行为自决**，更完整。

### 重大调整：无。

---

## 7. 最终实现点

### 前端

1. **`src/relay/web/live_panel.js`** `placeColumns`（:1006）：
   - 重挂前快照全部滚动容器（`.live-panel-stream` / `.live-panel-pre` / `.live-panel-tool-args` / `.lp-ep-list`）的 scrollTop。
   - 每列比对（排除 `$endpoint`）子节点集合 + 相对顺序：未变 → 跳过重挂；变化 → 清空重挂。
   - 重挂后对仍 connected 且 `top > 0` 的节点恢复 scrollTop。

### 资源版本

- `live_panel.js ?v=20260823-62 → ?v=20260823-63`；`live_panel.html` 引用同步（:62）。

### 测试

- 新 jsdom harness `sticky_remount.js`（8 检查全过）：
  - S1 单容器流式粘底滚动到底（scrollTop → 1000）
  - S2 新容器 r2 出现后原容器仍粘底（scrollTop 保留 1000）+ 新容器后继续流式仍滚底
  - S3 全程无 JS error
  - S4 强制跨列重挂后快照恢复 r3 的 800 + 无 error
- 全回归 11 个 harness 全过（repro / repro_extended / upgrade / clear_check / endpoint / firstfit / tools_clear / empty_check / reuse_crossrid / concurrent5 / sticky_remount）。

### 行为验收清单（手动测试项）

- [ ] 单个请求流式输出 → 内容自动滚底
- [ ] 第二个请求（新容器）出现 → 第一个请求容器仍自动滚底，滚动位置不丢
- [ ] 中途清除某个请求 → 其它请求容器位置保留、继续滚底
- [ ] 并发多请求加列 / 减列 → 各容器粘底状态正常
- [ ] 全程无滚动位置回跳 / 无 JS 控制台报错

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.js` | 改（`placeColumns` 跳过重挂 + 快照恢复） |
| `src/relay/web/live_panel.html` | 改（live_panel.js 版本号） |
| `docs/dev/live_panel_sticky_remount_v0.147.md` | 新建 |
