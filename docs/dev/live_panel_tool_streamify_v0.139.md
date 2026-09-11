# 实时栏 tool 容器正文化 + 主题切换动效（v0.139）开发文档

## 1. 用户的初始指令

> 1、tool工具容器框架采用正文容器样式。最多高度20%，见缝插针时可折叠，多余内容向下滚动可查看。
> 2、主题切换时增加一个动效

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | tool 容器框架采用正文容器样式（与 `.live-panel-stream` 同款：panel-alt + hairline + radius-sm） | 指令 1 |
| B | tool 容器最多高度 20% | 指令 1 |
| C | 见缝插针时可折叠（用户手动收起腾出列内空间） | 指令 1 |
| D | 多余内容向下滚动可查看（args 长文本可滚动） | 指令 1 |
| E | 主题切换时增加一个动效（颜色平滑过渡） | 指令 2 |

### 隐含但需要确认的点（设计自决）

- **可折叠用 `<details>`/`<summary>` 原生控件**：原生语义 + 键盘可达 + 零 JS 状态管理；CSS 自定义箭头 `▾` 旋转替代默认三角。
- **默认展开**：与 `.live-panel-stream` 常显同语义；用户主动点 summary 才折叠腾空间。
- **20vh 实现位置**：用 `calc(20vh - 24px)` 扣掉 summary 高（≈24px），给 args 区作为 `max-height` 上限；溢出 `overflow-y: auto` 内部滚动。
- **主题动效用 CSS transition**：html/body/`.glass-card`/`.live-panel-card`/`.live-panel-tool` 挂 `--theme-trans`（共用一条曲线 + 时长），避免各元素自己 transition 参差。
- **不动 hover/click 类 transition**：只在 `data-theme` 切那一帧生效，避免给所有交互元素增加常驻 transition 开销。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位 v0.138 状态

**A. tool 容器样式**：v0.138 回退到 `.live-panel-tool` 紧凑小块 —— `margin-top: 4px` + `font-size: 11px` + 没有独立 padding/border，只在 `buildToolCard` 拼 `<div class="live-panel-tool">` + name + age + args pre。指令 1 要求"采用正文容器样式"= 升级到 `.live-panel-stream` 同款（panel-alt + hairline + radius-sm + 等宽 padding 6 8 + 单色文字）。

**B. 20vh 上限**：v0.137 的玻璃卡版本去掉了 `.live-panel-tool-args` `max-height: 120px`；v0.138 也没有任何高度限制（整卡由 layout 决定）。指令 1 要求"最多 20%" —— args 自身的滚动容器必须有上限，整卡由 layout 决定。

**C. 可折叠**：用 `<details>`/`<summary>` 原生控件，CSS `list-style: none` 隐默认三角 + 自绘 `▾` 旋转箭头。JS 无需管理折叠状态（浏览器原生 toggle）。

**D. 主题动效**：现有 `setTheme(name)` 直接 `setAttribute("data-theme", name)` 切换属性，CSS 选择器瞬时重算颜色，无过渡。指令 2 要求加动效 —— 在根 + 主题感知元素挂 `transition`，让颜色沿 0.35s 曲线插值。

### 第二阶段：方案设计

**A. tool 容器升级为正文容器样式**（CSS）：

```css
.live-panel-tool {
  margin-top: 4px;
  background: var(--panel-alt);
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  font-size: 11px;
  overflow: hidden;
}
```

**B+C+D. `<details>` 重构 + 20vh 上限**（CSS）：

```css
.live-panel-tool > summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  cursor: pointer;
  user-select: none;
  list-style: none;
}
.live-panel-tool > summary::-webkit-details-marker { display: none; }
.live-panel-tool > summary::marker { display: none; content: ""; }
.live-panel-tool[open] > summary {
  border-bottom: 1px dashed var(--hairline);
}
.live-panel-tool > summary::after {
  content: "▾";
  font-size: 9px;
  transition: transform .18s var(--ease-out);
}
.live-panel-tool:not([open]) > summary::after {
  transform: rotate(-90deg);
}
.live-panel-tool-args {
  max-height: calc(20vh - 24px);  /* 扣掉 summary 高 */
  overflow-y: auto;
  scroll-behavior: smooth;
}
```

**E. 主题切换动效**（CSS）：

```css
:root {
  --theme-trans: background-color .35s var(--ease-out),
                color .25s var(--ease-out),
                border-color .35s var(--ease-out),
                box-shadow .35s var(--ease-out),
                fill .35s var(--ease-out),
                stroke .35s var(--ease-out);
}
html, body {
  transition: var(--theme-trans);
}
.glass-card,
.live-panel-card,
.live-panel-tool {
  transition: ...已有... , background-color .35s var(--ease-out);
}
```

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `live_panel.js` `buildToolCard` 改 `<details>` 结构 | 无 |
| 2 | `live_panel.css` `.live-panel-tool` 用 panel-alt + hairline + radius-sm | 无 |
| 3 | `live_panel.css` summary 头 + 自绘箭头 + `[open]` 状态 | #1 |
| 4 | `live_panel.css` `.live-panel-tool-args` max-height `calc(20vh - 24px)` + overflow | 无 |
| 5 | `styles-20260817.css` 新增 `--theme-trans` 变量 | 无 |
| 6 | `styles-20260817.css` `html, body` 挂 `var(--theme-trans)` | #5 |
| 7 | `styles-20260817.css` `.glass-card` transition 加 background-color | #5 |
| 8 | `live_panel.css` `.live-panel-card` / `.live-panel-tool` transition `var(--theme-trans)` | #5 |
| 9 | 资源版本 bump（styles / live_panel.css / live_panel.js） | #1-#8 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 折叠实现 | 原生 `<details>`/`<summary>` | 零 JS 状态、键盘可达、CSS 可定制箭头 |
| 默认折叠/展开 | 默认展开（`open` 属性） | 与 `.live-panel-stream` 常显同语义；用户主动折叠腾空间 |
| 自绘箭头 | `▾`（向下三角） `[open]` 0deg / `[open=false]` -90deg | 视觉紧凑，不依赖字体图标 |
| 20vh 位置 | args 区 max-height（不是整卡） | 整卡高由 layout 决定；args 自身滚动 |
| 主题动效时长 | 0.35s（背景/边框/阴影）+ 0.25s（文字） | 背景过渡稍长看得清，文字稍短避免拖沓 |
| 主题动效作用范围 | html/body + 主题感知容器（card/tool/glass-card） | 根级过渡覆盖所有子元素；显式给玻璃卡/侧栏卡挂避免子元素 transition 参差 |
| 不动的 transition | hover / click / ripple / animation | 避免给所有交互元素加常驻 transition；只管 `data-theme` 切那一帧 |

---

## 4. 实现中遇到的问题

### 问题 1：`<details>` 默认三角箭头很难完全隐藏

**症状**：WebView2 (Chromium) 上 `<summary>` 默认有 `::-webkit-details-marker` + `::marker` 两个伪元素，必须同时 `display: none` 才能干掉三角。

**解法**：CSS 同时写 `.live-panel-tool > summary::-webkit-details-marker { display: none; }` 和 `.live-panel-tool > summary::marker { display: none; content: ""; }`，再用 `summary::after` 自绘 `▾`。

### 问题 2：20vh 上限扣掉 summary 高

**症状**：指令"最多高度 20%"语义模糊 —— 是整卡 20% 还是 args 20%？如果整卡 20%，列内布局会被吞；如果 args 20%，整卡可被 layout 自由扩展。

**解法**：args 区 `max-height: calc(20vh - 24px)`（24px ≈ summary 头高），整卡无上限由 layout 决定。args 溢出向下滚动，整卡可以随列内空间伸缩。

### 问题 3：主题 transition 时长怎么定

**症状**：0.1s 太快看不出插值；1s 太慢用户等不及。背景色和文字色应该不同步（文字改太快会有"一闪而过"感）。

**解法**：背景/边框/阴影/fill/stroke 走 0.35s（视觉感受柔），文字走 0.25s（响应更快）。统一 `--ease-out: cubic-bezier(.22, 1, .36, 1)` 曲线（沿用现有 token）。

### 问题 4：哪些元素需要挂主题 transition

**症状**：如果只在 `html, body` 挂，`.glass-card` / `.live-panel-card` / `.live-panel-tool` 的 background-color 不会插值（继承 ≠ transition），切换瞬间还是硬切。

**解法**：根级 transition + 主题感知容器自身各挂一次 `var(--theme-trans)`。子元素 transition 不挂（避免 transition 嵌套导致合成层抖动）。

### 问题 5：主题切换时 24h 图表不重绘

**症状**：原 `setTheme` 末尾有 `_hourChartTheme = null; if (currentView === "overview" && lastSnap) renderAll(lastSnap, lastStatus);` —— 这是 JS 主动重绘图表，因为 canvas 颜色不在 CSS 变量范围。**这次不动这段**，CSS transition 是给 DOM 元素的；canvas 仍走 JS 重建（瞬时切换是合理的，因为 canvas 像素不是简单颜色插值）。

**解法**：保留原 setTheme 末尾的图表重建逻辑，不引入额外 JS hook。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 三角箭头隐藏 | summary 伪元素 + `list-style: none` 三处全清 | live_panel.css |
| #2 20vh 位置 | args 区 max-height（整卡无上限） | live_panel.css |
| #3 transition 时长 | 背景 0.35s + 文字 0.25s + 共用曲线 | styles-20260817.css |
| #4 元素挂载范围 | html/body + 玻璃卡/侧栏卡/tool 自身 | styles + live_panel.css |
| #5 canvas 图表 | 沿用原 setTheme 末尾 JS 重建逻辑 | app.js |

**最终 buildToolCard（live_panel.js）：**

```js
function buildToolCard(tool) {
  // v0.139：包成 `<details>` 可折叠 —— summary 显示名字+时间戳，
  // body 是 args。默认展开（与 .live-panel-stream 常显同语义）；
  // 用户点 summary 折叠腾空间给新卡。CSS 给整卡 20vh 上限 + 内部滚动。
  const card = document.createElement("details");
  card.className = "live-panel-tool";
  card.open = true;
  const name = document.createElement("span");
  name.className = "live-panel-tool-name";
  name.textContent = (tool && (tool.name || tool.tool || "tool")) + "";
  const age = document.createElement("span");
  age.className = "live-panel-tool-age";
  age.textContent = tl("刚刚");
  const summary = document.createElement("summary");
  summary.appendChild(name);
  summary.appendChild(age);
  card.appendChild(summary);
  const args = document.createElement("pre");
  args.className = "live-panel-tool-args";
  args.textContent = (tool && (tool.input !== undefined
    ? JSON.stringify(tool.input, null, 2) : JSON.stringify(tool, null, 2))) || "";
  card.appendChild(args);
  return { card, idx: null, age, args };
}
```

**最终主题切换动效（styles-20260817.css）：**

```css
:root {
  --theme-trans: background-color .35s var(--ease-out),
                color .25s var(--ease-out),
                border-color .35s var(--ease-out),
                box-shadow .35s var(--ease-out),
                fill .35s var(--ease-out),
                stroke .35s var(--ease-out);
}
html, body {
  transition: var(--theme-trans);
}
.glass-card {
  transition:
    transform .18s var(--ease-out),
    box-shadow .18s var(--ease-out),
    border-color .18s var(--ease-out),
    background-color .35s var(--ease-out);  /* v0.139：主题切换 */
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **tool 容器升级**：`.live-panel-tool` 用 panel-alt + hairline + radius-sm，与 `.live-panel-stream` 同款。
- **20vh 上限**：args 区 `max-height: calc(20vh - 24px); overflow-y: auto`，整卡由 layout 决定。
- **可折叠**：原生 `<details>`/`<summary>` + 自绘 `▾` 箭头，键盘可达 + 零 JS 状态。
- **默认展开**：与正文流常显同语义。
- **主题动效**：根级 + 容器级 `var(--theme-trans)`，背景 0.35s + 文字 0.25s + 共用 `--ease-out` 曲线。

### 偏离之处：

- **(a) `.live-panel-card` 挂的是完整 `--theme-trans`**（不是只 background-color）：计划只说"扩 background-color"，但 `--theme-trans` 已经包含了 border-color / box-shadow 一起插值，更一致。**结构自决**。
- **(b) 主题动效时长**：plan 没明确具体毫秒。0.35s 背景 + 0.25s 文字是权衡（视觉柔 + 响应不拖沓）。**视觉自决**。
- **(c) setTheme 末尾的图表重建逻辑不动**：canvas 颜色不在 CSS 变量范围，JS 主动重建是合理的，强行套 transition 反而会显示错误中间态。**沿用**。
- **(d) 不写新测试**：主题切换是纯 CSS 动效，jsdom 不渲染过渡；tool `<details>` 结构功能由浏览器原生提供。**跳过**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

无改动。

### 前端

1. **`src/relay/web/styles-20260817.css`**：
   - `:root` 新增 `--theme-trans` 变量（background-color .35s + color .25s + border-color .35s + box-shadow .35s + fill .35s + stroke .35s，共用 `--ease-out` 曲线）。
   - `html, body` 挂 `transition: var(--theme-trans)`，做根级过渡。
   - `.glass-card` transition 追加 `background-color .35s var(--ease-out)`（已有 hover transition 保留）。

2. **`src/relay/web/live_panel.css`**：
   - `.live-panel-tool` 升级正文容器样式（panel-alt + hairline + radius-sm + overflow:hidden）；挂 `var(--theme-trans)` 主题过渡。
   - `.live-panel-tool > summary` 整行可点 + `list-style: none` + `user-select: none`；伪元素 marker 全隐 + `summary::after` 自绘 `▾` 箭头 + `[open=false]` 旋转 -90deg + `transform` transition .18s。
   - `.live-panel-tool[open] > summary` 展开时底加分隔线（dashed hairline）。
   - `.live-panel-tool-args` `max-height: calc(20vh - 24px); overflow-y: auto; scroll-behavior: smooth`，等宽字体 + 10px + 行高 1.55 + 内部 padding 6 8。
   - `.live-panel-card` 挂 `transition: var(--theme-trans)` 主题过渡。

3. **`src/relay/web/live_panel.js`**：
   - `buildToolCard` 改 `<details class="live-panel-tool" open>` 结构（summary = name + age；body = args pre）。返回 `{card, idx: null, age, args}`，applyTools 后续赋值不抛错。

4. **`src/relay/web/index.html` / `src/relay/web/live_panel.html`**：
   - styles-20260817.css ?v=20260824-04 → ?v=20260824-05。
   - live_panel.css ?v=20260823-55 → ?v=20260823-58。
   - live_panel.js ?v=20260823-57 → ?v=20260823-58。

### 测试

未新增测试（CSS 动效 + 原生 `<details>` 折叠由浏览器提供；jsdom 不渲染 transition / 不模拟 details）。

### 行为验收清单（手动测试项）

- [ ] tool 卡片视觉：与 `.live-panel-stream` 同款（panel-alt + hairline + 圆角 + 等宽 padding 6 8）
- [ ] tool 卡片 args 最多 20vh - 24px，超出滚动
- [ ] tool 卡片 summary 点击折叠 / 展开，箭头 `▾` 旋转
- [ ] tool 卡片默认展开（与正文流常显同语义）
- [ ] 主题切换：所有颜色（背景 / 文字 / 边框 / 阴影）沿 0.35s / 0.25s 插值，无瞬间硬切
- [ ] 主题切换时 24h 图表瞬时重绘（沿用原 setTheme 行为）
- [ ] tool 卡片列打包 / 见缝插针 / auto_extend 不受影响（仍是独立 layout item）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/styles-20260817.css` | 改（+`--theme-trans` 变量 / `html,body` 过渡 / `.glass-card` 加背景色过渡） |
| `src/relay/web/live_panel.css` | 改（.live-panel-tool 正文化 + `<details>` 折叠样式 + 20vh 上限 + .live-panel-card / .live-panel-tool 主题过渡） |
| `src/relay/web/live_panel.js` | 改（`buildToolCard` 改 `<details>` 结构） |
| `src/relay/web/index.html` | 改（CSS cache 版本 bump） |
| `src/relay/web/live_panel.html` | 改（CSS cache 版本 bump + live_panel.css / live_panel.js 版本 bump） |
