# 总览卡片边缘拖拽（v0.185）开发文档

> 修复总览卡片右下角 / 右沿 / 下沿三个 8–20px 命中区拖拽改尺寸 / 改列跨度功能
> 的三个累积 bug：①起点坐标系错位导致拖动无效；②CSS Grid span 不接小
> 数导致横向只能停在 1 列；③`cursor: inherit !important` 在 WebView2 下
> 把 body 光标变 auto 导致按下后鼠标样式跳回普通。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 查看上次会话内容

> 继续解决拖动无效问题

> 按下后还是跳变，然后拖动无效

> 鼠标移上边缘时，卡片上浮且鼠标样式变化。当按下后，卡片瞬间取消上浮且鼠标样式回归普通样式。

> 现在鼠标样式不会再跳变了。但是拖动没用

> 上下拖动很正确。但是左右完全拖不动，可能是因为旁边有贴着的容器顶住了不让移动。需要处理这种情况

> 拖动行为正确。不过能拖动到的还是只有三个位置，需要修正为20挡位

> 行为正确。写开发文档和更新日志。遵循约束

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **起点坐标系对齐吸附档位**：mousedown 时 `startH` 必须跟 `st.height` 同坐标系，否则 move 里 `newH = startH + dy` 跨过第一档需要的天量位移 | 指令"按下后还是跳变，然后拖动无效" |
| B | **CSS Grid span 不接小数**：横向 `--ov-span` 写小数（1.5/2.8）会被静默丢弃，卡片永远停在 1 列宽 | 指令"左右完全拖不动"（CDP 实测） |
| C | **WebView2 cursor:inherit 退化**：body.{mode} * { cursor: inherit !important } 把 body 自身 cursor 也变 inherit → 解析为 auto → "鼠标样式回归普通" | 指令"按下后...鼠标样式回归普通样式" |
| D | **悬停上浮覆盖命中区**：.glass-card:hover translateY(-3px) 在按下瞬间让命中区跟着上浮 3px → 鼠标脱离命中区 → wireEdgeResize 把 cursor 抢回 grab | 指令"按下后，卡片瞬间取消上浮" |
| E | **wireEdgeResize 抢 cursor / 启动 window resize**：window 级 mousedown capture 监听器跟 card-edge 抢 capture 队列，必须 stopImmediatePropagation 才能阻断 | 根因分析 |
| F | **20 档列跨度**：从 3 档整数恢复为 1.0–3.0 步长 0.1（21 个端点，含 3.0），拖动必须真实落档 | 指令"修正为20挡位" |
| G | **高度 31 档**：跟列跨度一侧对齐手感；起点必须走吸附档位而非实测内容高度 | 设计决定（拍板，符合 feedback_decide_show_effect） |
| H | **min-height 兜底**：图表卡 `min-height: 220px` 会静默夹住 --ov-h 到 220 以下，显式高度一旦生效需强制 min-height:0 | CDP 实测发现 |

### 隐含但需要自行决策的点

- 拖动期间是"逐档写 localStorage"还是"up 时一次性写" —— 选择后者（避免 500ms 拖动写 30 次存储）
- 高度起点用"已存值"还是"实测 body 高度" —— 选择前者（无持久化时 fallback 到 `clamp(measuredH)` 再吸附）
- 总览布局从 CSS Grid 切到 flex 是否影响其他视图 —— 选择局部覆盖 `.view[data-view="overview"] > .grid`，不动 history/upstreams 等共用 `.grid` 的视图
- "重要约束"用 ⚠ 标记放显眼位置（遵守 feedback_doc_weight_pref + feedback_dev_doc_structure）

---

## 3. 分析需求后得出的开发路径

### 第一阶段：纵向高度（cursor + 起点对齐 + 31 档）

**问题链**：
1. 卡片 hover translateY(-3px) → 命中区随之上浮 → 鼠标脱离 → cursor 跳 grab → "鼠标样式回归普通"
2. 命中区在卡外（bottom:-4px）→ `.glass-card { overflow:hidden; border-radius }` 裁掉 → cardResizePointerDown 早返回 → dragstart 接管 → "按下又跳回普通"
3. cursor:inherit !important 在 body.{mode} * 规则下让 body 自身 cursor = auto → "鼠标样式跳变"
4. startH = 实测 body 高度 (27.5px) 但 st.height = 吸附档 (200px) → move 算 newH = 27.5 + dy → dy > 212px 才能跨到 280 → "拖动无效"
5. move 里 `if (target !== st.height)` 看似无 bug，但叠加 #4 就完全锁死

**修复**：
- A：命中区 `bottom:0 / right:0 / width:8px`，完全在卡内，圆角裁两角但其它位置全可命中
- B：`body.card-resizing .glass-card:hover { transform: none !important }`，拖拽中禁上浮
- C：`body.card-resizing-h/-w/-both { cursor: ns-/ew-/nwse-resize }` + `body.card-resizing-h * { cursor: ns-resize !important }`，**直接写具体 cursor 值**，不用 inherit
- D：`e.stopImmediatePropagation()` 阻断 wireEdgeResize capture 队列；`if (!body.card-resizing) wireEdgeResize 写 cursor`
- E：`startH = OVERVIEW_HEIGHTS[initSnap]`，`baseH = s0.height > 0 ? s0.height : clamp(measuredH)`，move 端 `newH = clamp(startH + dy)`，跟 `st.height` 同坐标系
- F：`OVERVIEW_HEIGHTS = [160, 180, ..., 760]` 31 档（步长 20px），持久化只校验 `height > 0` 不查表，换表不会让旧值失效

### 第二阶段：横向宽度（CSS Grid → flex + 21 档）

**根因**：`grid-column: span var(--ov-span, 1)` —— spec `span <integer>` 只接整数，CDP 实测 `--ov-span=1.5/2.5/2.8` 全部被静默丢弃，卡永远 1 列。

**修法**：总览布局切到 **flex**。卡 `width: calc(var(--ov-span) / 3 * (100% - 32px))`，任何小数都生效。`32px = 2 × 16px`（行内 3 卡之间的 gap 总和）。不影响 history/upstreams/opencode 视图（它们自己有 `grid-template-columns: none + display:flex` 的覆盖规则）。

**21 档闭环**：原 20 档循环 `for (i=0; i<20; i++)` 只跑到 i=19 → 最后一项 2.9 而不是 3.0。CDP 实测拖到底 dx=700 也只能吸到 2.9，永远到不了"满 3 列"。改成 `i <= 20`，闭合到 3.0。

### 第三阶段：CDP 实测验证

每阶段都用 Edge CDP 跑真实页面（`file:///...index.html`）测：
- mousedown 是否进 card-resizing 模式（bodyClass 校验）
- mousemove 派发后 `--ov-span` / `--ov-h` 是否真的写到了 inline style
- 卡片 getBoundingClientRect 是否真的换宽度 / 高度
- 跨档阈值是否符合预期（位移 +10px 即能跨过 20px 档中点）
- 边界 clamp（最左 / 最右 / 最上 / 最下）行为正确

---

## 4. 实现中遇到的问题

### 问题 1：WebView2 的 `cursor: inherit !important` 在 `body.x * { }` 选择器下把 body 自身 cursor 也变成 inherit

body 没有父级，inherit 退化成 `auto`。结果 body cursor = auto 而不是预期的 `ns-resize` —— **用户看到"鼠标样式跳回普通"**。

**解法**：不要写 `cursor: inherit !important`。直接写具体值：

```css
body.card-resizing-h, body.card-resizing-h * { cursor: ns-resize !important; }
body.card-resizing-w, body.card-resizing-w * { cursor: ew-resize !important; }
body.card-resizing-both, body.card-resizing-both * { cursor: nwse-resize !important; }
```

三条 mode 类分别给 ns/ew/nwse，覆盖 `.glass-card` 及其全部后代（玻璃卡的 grab 比 `*` 特异性高，必须靠 `*` 跟它同位再用 !important 强行覆盖）。

### 问题 2：`.glass-card:hover { translateY(-3px) }` 把命中区跟着卡上抬

命中区在卡上绝对定位，translate 整个卡 → 命中区物理位置同步上抬 3px → 鼠标没动但已经脱离命中区 → cardResizePointerDown 早返回 → 后续 mouseup 走 wireEdgeResize → "按下后鼠标跳回 grab + 卡片瞬回上浮"。

**解法**：

```css
body.card-resizing .view[data-view="overview"] .glass-card:hover {
  transform: none !important;
}
```

拖拽期间整卡禁上浮，松手后还原。`!important` 是因为 `.glass-card:hover` 的 transform 是直接声明，特异性 (0,2,0)，`.card-resizing:hover` 只有 (0,2,0) 同位，必须靠 !important 抢后声明。

### 问题 3：wireEdgeResize 在 window capture 相位抢 mousedown

wireEdgeResize 跟 cardResizePointerDown 都挂在 `window.addEventListener('mousedown', ..., true)`（capture）。用户点卡边缘命中区时 wireEdgeResize 先走 `detectEdge()`，命中窗口边缘就启动 `api.resizeWindow`，把整窗开始 resize。

**解法**：在 cardResizePointerDown 里 **`stopImmediatePropagation()`**（不是 stopPropagation）。stopPropagation 只挡 bubble，capture 队列里下一个监听器仍会跑；stopImmediate 才会阻断同相位后续监听器。

### 问题 4：起点坐标系错位导致 move 永远不跨档

旧代码：

```js
const curCssH = rect.height / z;          // 27.5 (实测内容高度)
const initSnap = /* ... */ ;              // 0
_cardResize = { startH: curCssH, height: OVERVIEW_HEIGHTS[initSnap] /* = 200 */ };
```

后续 move 里 `newH = startH + dy = 27.5 + dy`。要 newH 跨过 240（200 和 280 的中点）需要 `dy > 212.5px`。CDP 实测 dy=0..180 全程 `--ov-h` 不变。

**解法**：起点跟档位对齐：

```js
const baseH = (s0 && s0.height > 0) ? s0.height : clampH(measuredH);
const initSnap = /* snap baseH */;
startH: OVERVIEW_HEIGHTS[initSnap],   // 跟 st.height 同坐标系
```

move 端：

```js
const newH = clamp(round(st.startH + dy));   // dy>10px 即能跨过 20px 档中点
```

### 问题 5：CSS Grid `span <integer>` 不接小数 —— 横向完全失效

CDP 实测：

```
span=1.5 → actualW=313 (1 列宽，跟 span=1 一样)
span=2.5 → actualW=313
span=2.8 → actualW=313
span=2   → actualW=633 (2 列宽，OK)
span=3   → actualW=952 (3 列宽，OK)
```

CSS spec：`grid-column: span <integer> | <custom-ident>` —— **不接受小数**。Chromium/Firefox/WebView2 都静默丢。

这是 **SPEC 硬限**，不是浏览器 bug。要么只用整数（3 档 1/2/3），要么换布局机制。

**解法**：总览布局切 flex。

```css
.view[data-view="overview"] > .grid {
  display: flex;
  flex-wrap: wrap;
  gap: 16px !important;          /* !important 兜底：CDP 实测发现不加会被某条更深特异性规则压成 6px */
  align-items: flex-start;
}
.view[data-view="overview"] > .grid > .glass-card {
  flex: 0 0 auto;
  width: calc(var(--ov-span, 1) / 3 * (100% - 32px)) !important;
  max-width: none !important;
  min-width: 0;
}
```

公式 `(span/3) * (100% - 32px)`：32px = 2 × 16px gap（行内 3 卡总 gap）。

⚠ **flex 方案特异性陷阱**：写 `.view[data-view="overview"] > .grid { gap: 16px }` 不加 !important 会**被某条更深的规则压成 6px**（CDP 多次复现，根因未完全定位 —— `Page.getResourceContent` 在 file:// origin 下被 CORS 挡，没法 dump 浏览器实际解析的样式表；!important 兜底更稳）。同样 width 也加 !important。**这是本次最隐蔽的坑**，纯靠 CDP 实测才发现。

### 问题 6：20 档循环边界错位（i<20 → 只能到 2.9）

```js
for (let i = 0; i < 20; i++) out.push(Math.round((1 + i*0.1) * 10) / 10);
// [1, 1.1, 1.2, ..., 2.8, 2.9]   ← 最后一项 2.9，不是 3.0
```

`i=19` 时 `1 + 19*0.1 = 2.9`。3.0 永远进不来。CDP 实测 dx=700 → clamp 到 3.0 → nearest = 2.9（因 2.9 在数组里，3.0 不在）→ 永远到不了"满 3 列宽"。

**解法**：`i <= 20`，闭合到 3.0：

```js
for (let i = 0; i <= 20; i++) out.push(Math.round((1 + i*0.1) * 10) / 10);
// [1, 1.1, 1.2, ..., 2.9, 3.0]   ✓
```

### 问题 7：图表卡 `min-height: 220px` 静默夹住 --ov-h

`.stats-chart-host` / `[data-card^="stats-"] .card-body` 都有 `min-height: 220px`。CSS 规范：`min-height` 优先于 `height`，所以即便 `--ov-h: 180px !important`，220 以下的拖动会被静默夹住。

**解法**：在 `.card-body[style*="--ov-h"]` 规则里**强制 min-height: 0**：

```css
.view[data-view="overview"] .grid .glass-card:not(.agent-view-list) .card-body[style*="--ov-h"] {
  height: var(--ov-h) !important;
  flex: none;
  min-height: 0 !important;   /* 新增：解除 220 下限 */
}
```

### 问题 8：CDP file:// origin 下无法直接读 stylesheet.cssRules

`Page.getResourceContent` 在 file:// 下被 CORS 挡，CDP 拿不到浏览器实际解析的 CSS。`CSS.getMatchedStylesForNode` 返回 0 条规则（headless Edge 跟 pywebview WebView2 行为不一致）。定位 #5 的 !important 兜底靠直接观察 `getComputedStyle()` 的 runtime 值，不能靠静态分析。

### 问题 9：HTML/CSS/JS 资源版本号没改 → 浏览器复用旧 CSS

`index.html` 里 `<link rel="stylesheet" href="styles-20260817.css?v=20260825-01" />` 跟 `<script src="app.js?v=20260825-09"></script>`。改完 CSS/JS 后**不会自动重载**，必须手动 bump 版本号（`?v=20260826-01` / `?v=20260826-03`）才能让 headless Edge / pywebview WebView2 拉新版。

⚠ **每次改完前端静态文件必须同步 bump `index.html` 里的版本号**，否则用户看到的是旧行为。调试时容易踩这个坑（"我改了为什么没生效？"——十有八九是没 bump 版本号）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 / 行号 |
|---|---|---|
| #1 cursor:inherit 退化 | 三个 mode 类直接写具体 cursor 值 | styles-20260817.css |
| #2 hover translateY 覆盖命中区 | `body.card-resizing .glass-card:hover { transform: none !important }` | styles-20260817.css |
| #3 wireEdgeResize 抢 mousedown capture | `e.stopImmediatePropagation()` | app.js:cardResizePointerDown |
| #4 起点坐标系错位 | `startH = OVERVIEW_HEIGHTS[initSnap]`，`baseH` 优先用已存值 | app.js:cardResizePointerDown |
| #5 CSS Grid span 不接小数 | 总览切 flex，width: calc() | styles-20260817.css |
| #6 20 档循环边界错位 | `i <= 20` 闭合到 3.0 | app.js:OVERVIEW_SPANS |
| #7 min-height:220 夹住 --ov-h | `[style*="--ov-h"]` 规则加 `min-height: 0 !important` | styles-20260817.css |
| #8 CDP CORS 限制 | 靠 runtime getComputedStyle 验证，绕过 stylesheet 静态分析 | 测试方式 |
| #9 资源版本号必须 bump | `?v=20260826-01` / `?v=20260826-03` | index.html |

---

## 6. 是否完全遵循规划路径开发

**部分偏离**。

### 完全按规划（无偏离）：

- cursor 用具体值不用 inherit
- 命中区拉回卡内
- hover 上浮在 card-resizing 时禁掉
- 起点基准用已存值（无则 fallback）
- 移动端 newH 走 clamp + 起点
- 横向切 flex
- 20 档 fine-grained（实际 21 档闭合到 3.0）

### 偏离之处：

- **(a) 没预测到 "CSS Grid span 不接小数"** —— 一开始以为是"旁边有贴着的容器顶住了不让移动"（用户的合理直觉），实际是 CSS spec 硬限。CDP 实测前完全没往这个方向想，靠 CDP 多个 span 值的实测才发现。
- **(b) 没预测到 flex `gap: 16px` 必须加 !important** —— 写完 flex 规则后 `getComputedStyle(.grid).gap` 一直返回 6px，多次怀疑自己写错了；多次翻 stylesheet 找不到更深特异性规则，最后靠 `!important` 兜底。**真因没定位**（file:// CDP CORS 挡了 stylesheet 静态分析）。
- **(c) 20 档循环边界 i<20 → 2.9** —— 写的时候算了一下"20 档 = i 0..19 闭 2.9"就直接写了，没意识到 3.0 是关键的"满列宽"档位。CDP 实测 dx=700 不能到 3.0 才反应过来。
- **(d) 高度起步用 OVERVIEW_HEIGHTS[initSnap] 而非 measuredH** —— 旧逻辑 `startH = measuredH (27.5)` 配合 `st.height = 200` 形成坐标系错位。修法是把 `startH` 跟 `st.height` 都对齐到档位值。这条偏离规划里"无持久化时用 clamp(measuredH) 再吸附" —— 实际更彻底，直接用吸附值，clamp 留给 move 端做边界保护。

### 重大调整：无。

---

## 7. 最终实现点

### 1. 总览布局切换（CSS）

`styles-20260817.css` 把 `.view[data-view="overview"] > .grid` 从 CSS Grid 切到 flex + wrap：

```css
.view[data-view="overview"] > .grid {
  display: flex;
  flex-wrap: wrap;
  gap: 16px !important;      /* !important 兜底特异性坑 */
  align-items: flex-start;
}
.view[data-view="overview"] > .grid > .glass-card {
  flex: 0 0 auto;
  width: calc(var(--ov-span, 1) / 3 * (100% - 32px)) !important;
  max-width: none !important;
  min-width: 0;
}
```

⚠ **CSS Grid `grid-column: span <integer>` 不接小数** —— 这是本次最深的坑。20 档 fine-grained 在 Grid 下完全失效（CDP 实测 `--ov-span=1.5/2.5/2.8` 全部被静默丢弃为 1 列宽）。要 fine-grained 必须切 flex 或写 JS 设置 `width` 像素值。

⚠ **flex `gap: 16px` 不加 !important 会被压成 6px** —— 真因没定位（file:// CDP CORS 挡了 stylesheet 静态分析）。!important 兜底。

### 2. 列跨度档位（JS）

`app.js`:

```js
const OVERVIEW_SPANS = (() => {
  const out = [];
  for (let i = 0; i <= 20; i++) out.push(Math.round((1 + i * 0.1) * 10) / 10);
  return out;
})();
// → [1, 1.1, 1.2, ..., 2.9, 3.0]  21 个端点闭合到 3.0
```

⚠ **`i <= 20` 不是 `i < 20`** —— 后者最后一项是 2.9，CDP 实测拖到底 dx=700 也只能吸到 2.9，永远到不了"满 3 列宽"。3.0 必须显式闭合。

### 3. 高度档位（JS）

```js
const OVERVIEW_HEIGHTS = (() => {
  const out = [];
  for (let h = 160; h <= 760; h += 20) out.push(h);
  return out;
})();
// → [160, 180, 200, ..., 760]  31 档
```

### 4. 拖动起点对齐（JS）

`cardResizePointerDown`:

```js
const baseH = (s0 && s0.height > 0) ? s0.height : clampH(measuredH);
const initSnap = /* snap baseH to nearest */;
// ...
_cardResize = {
  startH: OVERVIEW_HEIGHTS[initSnap],   // 跟 st.height 同坐标系
  height: OVERVIEW_HEIGHTS[initSnap],
  startSpan: naturalSpan(card),
  span: naturalSpan(card),
  // ...
};
```

⚠ **`startH` 必须跟 `st.height` 同坐标系** —— 旧代码 `startH = measuredH (27.5)` 跟 `st.height = 200` 错位，move 算 `newH = 27.5 + dy`，要 dy > 212px 才能跨到 280 档。修法是两者都从吸附档位取值。

### 5. CSS 边缘光标与 hover（CSS）

```css
/* hover 上浮在拖拽期间禁掉 */
body.card-resizing .view[data-view="overview"] .glass-card:hover {
  transform: none !important;
}

/* 三个 mode 类直接写具体 cursor 值，不用 inherit */
body.card-resizing-h, body.card-resizing-h * { cursor: ns-resize !important; }
body.card-resizing-w, body.card-resizing-w * { cursor: ew-resize !important; }
body.card-resizing-both, body.card-resizing-both * { cursor: nwse-resize !important; }
```

⚠ **`cursor: inherit !important` 在 `body.x * { }` 规则下会把 body 自身 cursor 变 inherit → 解析成 auto**（body 没父级）。这是 WebView2 的隐性 quirk，CDP / 普通 Chromium 复现稳定。**绝对不要用 inherit**，必须直接写具体 cursor 值。

### 6. min-height 兜底（CSS）

```css
.view[data-view="overview"] .grid .glass-card:not(.agent-view-list) .card-body[style*="--ov-h"] {
  height: var(--ov-h) !important;
  flex: none;
  min-height: 0 !important;   /* 解除 220 下限（stats-chart-host / [data-card^="stats-"]） */
}
```

⚠ **`min-height` 优先于 `height`** —— 图表卡 `min-height: 220px` 会静默夹住 `--ov-h: 200px`。显式高度一旦生效必须强制 `min-height: 0`，否则拖到 220 以下"没反应"。

### 7. wireEdgeResize 互斥（JS）

`cardResizePointerDown` 头部：

```js
e.preventDefault();
e.stopImmediatePropagation();   // 而非 stopPropagation
e.stopPropagation();
```

`wireEdgeResize` 的 mousemove 处理器头部：

```js
} else if (!document.body.classList.contains("card-resizing")) {
  // 写 cursor 之前检查：卡尺寸拖拽期间不要覆盖
}
```

### 8. 持久化（既有，无需改）

`getCardSize / setCardSize / applyCardSize` 对 height 只校验 `> 0`，对 span 走 `OVERVIEW_SPANS.find(_spanEq)`。换档位表不会让已存的旧值失效（旧 span=2 仍能 find 到 `[1.1.1.1, ..., 2.8, 2.9, 3.0]` 里的 2.0；旧 height=280 不在新的 [160..760 步长 20] 数组里但 280 > 0 校验过、不强制走档）。

### 行为验收清单（手动测试项）

- [ ] 下沿拖动：每 +15px 跨一档（31 档 20px 步长），全程不掉帧
- [ ] 右沿拖动：每 +30px 跨一档（21 档 0.1 步长），卡片宽度 306 → 920 真实变化
- [ ] 右下角拖动：宽高同时变
- [ ] 拖到最上：body 高度卡到 160px（不会更小）
- [ ] 拖到最右：--ov-span 卡到 3.0（满列宽，不会更大）
- [ ] 拖到最左：--ov-span 卡到 1.0
- [ ] 拖到最下：body 高度卡到 760px
- [ ] 松手后 localStorage 写入 `{span, height}`，刷新后还原
- [ ] 之前存过 span=2.8 / 2.3 的卡重载后宽度仍正确（705px / 705px）
- [ ] hover 边缘命中区：ns-resize / ew-resize / nwse-resize 光标
- [ ] 按下后光标保持（不被 wireEdgeResize 抢回 grab）
- [ ] 拖拽中卡不上浮（translateY 解除）
- [ ] chart-row 卡（自然 span=2）未拖过时宽度 = 2 列宽
- [ ] stats-xxx 卡拖到 220 以下仍能继续变小（min-height 兜底）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/styles-20260817.css` | 改（总览布局 grid → flex；命中区拉卡内；hover 上浮禁掉；cursor 写具体值；min-height 兜底） |
| `src/relay/web/app.js` | 改（OVERVIEW_HEIGHTS 31 档；OVERVIEW_SPANS 21 档；cardResizePointerDown 起点对齐；move 端 clamp + round；stopImmediatePropagation） |
| `src/relay/web/index.html` | 改（CSS/JS 版本号 bump `20260825-01/09` → `20260826-01/03`） |