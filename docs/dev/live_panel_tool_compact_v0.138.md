# 实时栏 tool 回归紧凑样式 / 关闭按钮裸悬浮 / 「等待」水印删除（v0.138）开发文档

## 1. 用户的初始指令

> 1、tool工具的容器样式还是用之前的，只不过拆分掉。
> 2、左上角的关闭，完全去掉背景，直接裸悬浮
> 3、背景的"等待"两个大字删去

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | tool 容器回归 v0.137 之前的 `.live-panel-tool` 紧凑小块（不是玻璃卡） | 指令 1 |
| B | tool 拆分逻辑保留（每条独立 layout item，见缝插针） | 指令 1（"只不过拆分掉"= 拆分逻辑保留） |
| C | 关闭 X 按钮完全裸悬浮（无任何背景 / 边框 / 阴影 / 圆角 / padding） | 指令 2 |
| D | 「等待」背景水印完全删除（HTML / CSS / JS 三处全清） | 指令 3 |

### 隐含但需要确认的点（设计自决）

- **tool 不再带序号**：v0.137 的玻璃卡带序号共享池，回归紧凑小块后**不带序号**（紧凑小块本就没有 rid 头，序号硬塞进 `.live-panel-tool` 不合适）。
- **关闭按钮 hover**：v0.137 液态玻璃下 hover 给按钮染色 + 容器玻璃透出；裸悬浮后 hover 仅给按钮自身 `.win-ctrl:hover`（沿用现有 `glass-surface-strong`），浮层完全透明。
- **水印删除范围**：`.lp-watermark` CSS 全删、`#lp-watermark` HTML 元素删、`body.watermark-away` toggle 两处都删（snapshot 路径 + done 路径）。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位 v0.137 改动

| v0.137 改动 | 这次回退 |
|---|---|
| `.live-panel-tool-card .lp-tool-name / .live-panel-tool-age` 专属样式 | 删除 |
| `.live-panel-tool-args` max-height: none | 恢复 max-height: 120px |
| `.live-panel-body .window-controls` 液态玻璃（rgba 0.11 + blur + border + 圆角 + 阴影） | 全部清空，仅留 top/right 锚点 |
| `.live-panel-body .window-controls` padding: 2px | 改回 0 |
| HTML `<div class="lp-watermark">` | 删除 |
| CSS `.lp-watermark` 全块 | 删除 |
| JS `body.watermark-away` 两处 toggle | 删除 |
| JS `buildToolCard` 用 `.live-panel-card.live-panel-tool-card` + rid 头 | 改回 `<div class="live-panel-tool">` 紧凑小块 |
| JS `applyTools` 调 `nextFreeCircledIdx` 序号 | 删除 |
| JS `toolNaturalHeight` 估算分支 | 简化为仅实测 |

### 第二阶段：方案设计

**tool 卡片回退**：

```js
function buildToolCard(tool) {
  // v0.138：回归原 tool 小块样式（.live-panel-tool）—— 不是玻璃卡
  const card = document.createElement("div");
  card.className = "live-panel-tool";
  const name = document.createElement("div");
  name.className = "live-panel-tool-name";
  name.textContent = (tool && (tool.name || tool.tool || "tool")) + "";
  const age = document.createElement("span");
  age.className = "live-panel-tool-age";
  age.textContent = tl("刚刚");
  name.appendChild(age);
  const args = document.createElement("pre");
  args.className = "live-panel-tool-args";
  args.textContent = (tool && (tool.input !== undefined
    ? JSON.stringify(tool.input, null, 2) : JSON.stringify(tool, null, 2))) || "";
  card.appendChild(name);
  card.appendChild(args);
  return { card, idx: null, age, args };
}
```

**关闭按钮裸悬浮**：

```css
.live-panel-body .window-controls {
  top: 3px;
  right: 6px;
  background: transparent;
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  padding: 0;
}
```

**水印删除**：HTML 元素 + CSS 块 + JS 两处 toggle 一并清掉。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `live_panel.js` `buildToolCard` 改回 `.live-panel-tool` 紧凑小块（无 rid 头） | 无 |
| 2 | `live_panel.js` `applyTools` 删除 `nextFreeCircledIdx` 调用 | #1 |
| 3 | `live_panel.js` `toolNaturalHeight` 简化（仅实测 + 兜底 80） | #1 |
| 4 | `live_panel.css` 删除 `.live-panel-tool-card` 专属样式 | 无 |
| 5 | `live_panel.css` `.live-panel-tool-args` max-height 恢复 120px | 无 |
| 6 | `live_panel.css` `.live-panel-body .window-controls` 清空所有背景相关属性 | 无 |
| 7 | `live_panel.html` 删除 `#lp-watermark` 元素 | 无 |
| 8 | `live_panel.css` 删除 `.lp-watermark` + `.lp-watermark span` + `body.watermark-away .lp-watermark` | 无 |
| 9 | `live_panel.js` 删除 `body.classList.add/remove("watermark-away")` 两处 | 无 |
| 10 | 资源版本 bump | #1-#9 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| tool 卡片是否带序号 | 不带 | `.live-panel-tool` 紧凑小块本无 rid 头，序号硬塞不自然 |
| tool 卡片样式 | 完全恢复 v0.137 之前的 `.live-panel-tool` 紧凑小块 | 用户原话"还是用之前的" |
| 关闭按钮 hover | 仅 `.win-ctrl:hover`（沿用现有 glass-surface-strong） | 裸悬浮后 hover 给按钮自身染色 |
| 关闭按钮位置 | 保留 v0.137 的 top: 3px / right: 6px（贴近顶角） | 沿用 v0.137 锚点 |
| 水印删除 | HTML/CSS/JS 三处一并清 | 用户原话"删去"，不留死代码 |

---

## 4. 实现中遇到的问题

### 问题 1：tool 卡片 `nextFreeCircledIdx` 删了但 `buildToolCard` 仍返回 idx

**症状**：v0.137 的 `buildToolCard` 返回 `{card, idx, age, args}`，idx 是序号 span 元素；改回紧凑小块后 idx span 不存在 → 后续赋值会抛 TypeError。

**解法**：`buildToolCard` 返回 `{card, idx: null, age, args}`；`applyTools` 不再调 `built.idx.textContent = toCircled(idx)`（直接删除该行），`rec` 不再含 `idx` 字段。

### 问题 2：`live_panel_tool_age` 的 `margin-left: auto` 已删但样式还引用 `.live-panel-tool-card`

**症状**：CSS 里残留 `.live-panel-tool-card .live-panel-tool-age { margin-left: auto; }` —— 现在没有 `.live-panel-tool-card` 元素，这条规则死代码。

**解法**：删除整个 `.live-panel-tool-card .lp-tool-name` 和 `.live-panel-tool-card .live-panel-tool-age` 块。

### 问题 3：水印删除时 JS 还引用 `body.watermark-away`

**症状**：JS 里有两处 `document.body.classList.add/remove("watermark-away")`，删 CSS 后无对应样式但 toggle 仍跑（无害，但冗余）。

**解法**：直接删除两处 toggle，注释说明 v0.138 删除。

### 问题 4：v0.137 的 `.live-panel-tool-args` max-height: none 与卡片独立性绑死

**症状**：回归紧凑小块后 tool 卡内容是 args 文本，独立卡不需要 args 自身 max-height —— 应该回到 v0.137 之前的 120px。

**解法**：`.live-panel-tool-args` max-height 恢复 120px，overflow 恢复 auto。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 idx span | `buildToolCard` 返回 idx:null；applyTools 不调 idx.textContent | live_panel.js |
| #2 死代码 CSS | 删 `.live-panel-tool-card .lp-tool-name / .live-panel-tool-age` | live_panel.css |
| #3 死代码 JS | 删 watermark-away toggle 两处 | live_panel.js |
| #4 max-height | 恢复 120px | live_panel.css |

**最终 buildToolCard（live_panel.js）：**

```js
function buildToolCard(tool) {
  // v0.138：回归原 tool 小块样式（.live-panel-tool）—— 不是玻璃卡。
  // 拆分到独立 layout item 即可，视觉保持原来的紧凑小条（名字 + 时间戳 + args）。
  const card = document.createElement("div");
  card.className = "live-panel-tool";
  const name = document.createElement("div");
  name.className = "live-panel-tool-name";
  name.textContent = (tool && (tool.name || tool.tool || "tool")) + "";
  const age = document.createElement("span");
  age.className = "live-panel-tool-age";
  age.textContent = tl("刚刚");
  name.appendChild(age);
  const args = document.createElement("pre");
  args.className = "live-panel-tool-args";
  args.textContent = (tool && (tool.input !== undefined
    ? JSON.stringify(tool.input, null, 2) : JSON.stringify(tool, null, 2))) || "";
  card.appendChild(name);
  card.appendChild(args);
  return { card, idx: null, age, args };
}
```

**最终关闭按钮样式（live_panel.css）：**

```css
.live-panel-body .window-controls {
  top: 3px;
  right: 6px;
  background: transparent;
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  padding: 0;
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **tool 卡片**：回归 `.live-panel-tool` 紧凑小块；拆分逻辑（每条独立 layout item / 见缝插针）保留。
- **关闭按钮**：完全裸悬浮（透明 + 无 border + 无圆角 + 无阴影 + 无 padding）；top/right 锚点沿用 v0.137。
- **水印**：HTML / CSS / JS 三处全清，不留死代码。
- **资源版本**：live_panel.css ?v=20260823-54 → ?v=20260823-55；live_panel.js ?v=20260823-56 → ?v=20260823-57。

### 偏离之处：

- **(a) tool 卡片不再带序号**：plan 没明确，但 `.live-panel-tool` 紧凑小块没有 rid 头结构，硬塞序号破坏视觉一致性。**结构自决**。
- **(b) `nextFreeCircledIdx` 函数删除**：plan 没明确，但 tool 不再带序号则该函数无调用方，留着也是死代码。**清理自决**。
- **(c) `live_panel_layout_hint` 不动**：v0.137 的 `get_main_geometry` 保留，跨缝连续功能仍工作。**沿用**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

无改动。

### 前端

1. **`src/relay/web/live_panel.js`**：
   - `buildToolCard` 改回 `<div class="live-panel-tool">` 紧凑小块结构（无 rid 头），返回 `{card, idx: null, age, args}`。
   - `applyTools` 删除 `nextFreeCircledIdx()` 调用与 `built.idx.textContent = toCircled(idx)` 两行。
   - `toolNaturalHeight` 简化：去掉 args 行数估算分支，仅实测 `offsetHeight` + 兜底 80。
   - 删除 `body.classList.add/remove("watermark-away")` 两处 toggle。

2. **`src/relay/web/live_panel.css`**：
   - 删除 `.live-panel-tool-card .lp-tool-name` 与 `.live-panel-tool-card .live-panel-tool-age` 两个专属样式。
   - `.live-panel-tool-args` max-height 恢复 `120px`，overflow 恢复 `auto`。
   - `.live-panel-body .window-controls` 清空所有背景相关属性（`background: transparent; backdrop-filter: none; -webkit-backdrop-filter: none; border: 0; border-radius: 0; box-shadow: none; padding: 0`），仅留 `top: 3px; right: 6px`。
   - 删除 `.lp-watermark` / `.lp-watermark span` / `body.watermark-away .lp-watermark` 整个块。

3. **`src/relay/web/live_panel.html`**：
   - 删除 `<div class="lp-watermark" id="lp-watermark" aria-hidden="true">...</div>` 元素。
   - live_panel.css ?v=20260823-54 → ?v=20260823-55。
   - live_panel.js ?v=20260823-56 → ?v=20260823-57。

### 测试

未新增测试（视觉/样式回退 + 死代码清理；现有 layout pipeline 不变）。

### 行为验收清单（手动测试项）

- [ ] 实时流关闭 X 按钮：完全裸悬浮（无任何背景），hover 时只按钮自身 X 染色
- [ ] 「等待」背景大字完全消失（空闲时窗口背景纯色 + 光晕，无水印干扰）
- [ ] 并发 tool 调用：每条仍是独立 layout item 见缝插针（拆分逻辑保留）
- [ ] tool 卡片样式回归紧凑小块（名字 + 时间戳 + args），不再是玻璃卡
- [ ] tool 卡片 args 长内容仍走 max-height 120px 内部滚动
- [ ] 跨缝连续（v0.137）功能仍工作（get_main_geometry 未动）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.js` | 改（buildToolCard 回退 + 序号代码删除 + 水印 toggle 删除） |
| `src/relay/web/live_panel.css` | 改（关闭按钮清空 + tool-card 样式删除 + .live-panel-tool-args 恢复 + .lp-watermark 块删除） |
| `src/relay/web/live_panel.html` | 改（HTML 元素删除 + 资源版本 bump） |
