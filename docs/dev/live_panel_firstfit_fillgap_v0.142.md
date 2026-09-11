# 实时栏 tool 见缝插针修复（v0.142）开发文档

## 1. 用户的初始指令

> 现在的tool排布仍有问题，左边那栏的下方仍有差不多20%空间但tool还是移动到最后一栏区显示了，不符合"见缝插针"的规范

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 左侧列底部空位（~20%）应被 tool 卡回填，而不是一路推到最后一列 | 指令（症状） |
| B | 符合「见缝插针」：每个 item 放第一个能放下的列，所有列都放不下才开新列 | 指令（规范） |
| C | 列数应更紧凑（不能因 tool 无谓多开列） | 推论 |

### 隐含但需要确认的点（设计自决）

- **旧算法缺陷**：`layout()` 列打包用 `colIndex` 单指针，只前进不回头。col0 塞满 80% 后 `colIndex` 推进到新列，之后所有 item（含小 tool）都写进当前列，**永不回头检查 col0 的空位** → 左侧 ~20% 空位被浪费。
- **修复方案**：改成 first-fit（首次适应）—— 每个 item 从最左列开始扫描，放进第一个 `nat <= H - col.used` 的列；都没有才开新列。
- **放不下又不能开新列**（卡超列高 / auto_extend 关）：塞进空位最大的列溢出滚动（原逻辑是留在当前列）。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位缺陷

`live_panel.js` `layout()` 列打包段（v0.137 引入）：

```js
let colIndex = 0;
items.forEach((it) => {
  let col = cols[colIndex];
  const avail = H - col.used;
  if (it.nat > avail && it.nat <= H) {
    if (LP_AUTO_EXTEND && cols.length < LP_MAX_COLS) {
      cols.push({ used: 0, els: [] });
      colIndex = cols.length - 1;   // ← 只前进，永不回头
      col = cols[colIndex];
    }
  }
  col.els.push(it);
  col.used += it.nat;
});
```

问题链：
1. rid1(50%) + endpoint 占 col0 约 65%
2. rid2(50%) 放不进 col0 → 开新列，colIndex=1
3. tool(5%) → colIndex=1 → 放 col1（即使 col0 还有 35% 空位）
4. 用户看到：col0 下方 ~20% 空，tool 却出现在最后一列

### 第二阶段：方案设计

first-fit 重写：

```js
const cols = [{ used: endpointHeight(), els: [] }];
items.forEach((it) => {
  let target = null;
  for (let i = 0; i < cols.length; i += 1) {
    if (it.nat <= H - cols[i].used) { target = cols[i]; break; }
  }
  if (!target) {
    if (it.nat <= H && LP_AUTO_EXTEND && cols.length < LP_MAX_COLS) {
      target = { used: 0, els: [] };
      cols.push(target);
    } else {
      let best = cols[0];
      cols.forEach((c) => { if (H - c.used > H - best.used) best = c; });
      target = best;
    }
  }
  target.els.push(it);
  target.used += it.nat;
});
```

- 保持 items 顺序（rid 按 `_ridOrder`、tool 按 `_toolsOrder` 到达序）—— 同列内相对顺序不变。
- col0 首个空位优先 → 回填底部空位。
- 溢出兜底：空位最大的列（视觉上最不挤）。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `layout()` 列打包循环改 first-fit | 无 |
| 2 | jsdom `firstfit.js` 新用例（空位回填 + 无空位落后列） | #1 |
| 3 | 旧 harness 选择器更新（`#lp-tools` → `.lp-col`） | #1 |
| 4 | 资源版本 bump | #1 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 打包算法 | first-fit（首次适应） | 最贴合「见缝插针」；实现 3 行，行为直观 |
| 扫描顺序 | 左 → 右（col0 优先） | 回填最早列底部空位，视觉自然 |
| 开新列条件 | 所有现有列都放不下 | 不再因小 tool 无谓多开列 |
| 溢出兜底 | 空位最大的列 | 原「当前列」语义在 first-fit 下无意义，改为最不挤的列 |
| 同列内顺序 | 保持 items 到达序 | 不破坏 rid 容器序列 / tool 到达序 |

---

## 4. 实现中遇到的问题

### 问题 1：反向场景（col0 真没空位时 tool 应落后列）测试预期写错

**症状**：首版 `firstfit.js` T8 用 3 个 thinkstream（各 50%）构造「挤满」场景，断言 tool 落后列。跑出来 FAIL —— 因为 col0 实际还有 `736 - (110+368) = 258px` 空位，first-fit 正确地把 tool 放进 col0，是**测试预期错了**。

**解法**：改用 `p1 thinkstream(368) + p2 puretext(245)` 把 col0 用到 723（剩 13 < 80 tool 高），p3 puretext 开 col1。tool 扫 col0(13<80)、col1(491>=80) → 落 col1。T8 修正为 `t3ColIdx === 1`。

### 问题 2：三个旧 harness 用 `#lp-tools .live-panel-tool` 选择器

**症状**：v0.139 起 tool 卡是独立 `<details class="live-panel-tool">` 挂 `.lp-col`，`#lp-tools` 永远为空 → repro.js D/E、upgrade.js F2/F3、repro_extended.js H2 全部 FAIL（got=0）。

**解法**：选择器改为 `.lp-col .live-panel-tool`。这是 harness 过时，非 first-fit 回归（v0.139 已存在）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 测试预期 | 精确构造 col0 空位 13px 场景 | firstfit.js |
| #2 harness 过时 | 3 个 harness 选择器更新 | repro / upgrade / repro_extended |

**最终 layout() 列打包（live_panel.js）：**

```js
// 贪心列打包 —— 见缝插针（first-fit）：每个 item 从最左列开始找
// 第一个放得下的列，所有列都放不下才开新列（auto_extend 时）。
// 这样小 tool 卡会回填到前面列底部的空位，而不是一路推到最后一列
// （旧算法 colIndex 只前进不回头 → 左侧 20% 空位被浪费）。
const cols = [{ used: endpointHeight(), els: [] }];
items.forEach((it) => {
  let target = null;
  for (let i = 0; i < cols.length; i += 1) {
    if (it.nat <= H - cols[i].used) { target = cols[i]; break; }
  }
  if (!target) {
    if (it.nat <= H && LP_AUTO_EXTEND && cols.length < LP_MAX_COLS) {
      target = { used: 0, els: [] };
      cols.push(target);
    } else {
      let best = cols[0];
      cols.forEach((c) => { if (H - c.used > H - best.used) best = c; });
      target = best;
    }
  }
  target.els.push(it);
  target.used += it.nat;
});
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **first-fit 重写**：从最左列扫描第一个放得下的列，所有列放不下才开新列。
- **回填左侧空位**：col0 有 ~20% 空位时小 tool 卡回填，不再推到最后一列。
- **列数紧凑**：不因 tool 无谓多开列。
- **同列内顺序保持**：items 到达序不变。

### 偏离之处：

- **(a) 溢出兜底改为「空位最大的列」**：plan 没明确，原「当前列」语义在 first-fit 下无意义，改为最不挤的列。**结构自决**。
- **(b) 顺手更新 3 个过时 harness**：plan 没提，但选择器 `#lp-tools .live-panel-tool` 在 v0.139 已失效，不修则回归套件一直红。**清理自决**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

无改动。

### 前端

1. **`src/relay/web/live_panel.js`** `layout()`：
   - 列打包循环从 `colIndex` 单指针顺序打包 → first-fit 全列扫描。
   - 开新列条件收紧为「所有现有列都放不下且 auto_extend 且未达 max_cols」。
   - 溢出兜底改为「空位最大的列」。

### 资源版本

- live_panel.js `?v=20260823-60 → ?v=20260823-61`。
- live_panel.css / styles-20260817.css / app.js 不动。

### 测试

- **`firstfit.js`（新，10 项）**：T0 两列建立 / T1 tool 挂载 / T2 tool 在 col0（非最后一列）/ T3 tool 未推到最后一列 / T4 rid1 在 col0 / T5 rid2 在 col1 / T6 列数仍 2 / T7 三并发两列 / T8 col0 空位不足 tool 落后列 / T9 无 JS error —— 全过。
- **回归**：repro / repro_extended / upgrade（20）/ clear_check（10）/ endpoint（25）全过（后三者选择器更新后）。

### 行为验收清单（手动测试项）

- [ ] 两个并发请求开两列后，新 tool 卡回填 col0 底部空位（不再出现在最后一列）
- [ ] col0 空位不足时 tool 才落到后列 / 新列
- [ ] 列数不再因 tool 无谓增加（窗口宽度更紧凑）
- [ ] rid 容器序列顺序不被 first-fit 打乱

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.js` | 改（layout() 列打包改 first-fit） |
| `src/relay/web/live_panel.html` | 改（live_panel.js 资源版本 bump） |
| `docs/dev/live_panel_firstfit_fillgap_v0.142.md` | 新建 |
