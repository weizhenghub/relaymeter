# 统计页图表缩放溢出修复（v0.113i）开发文档

> 承接 v0.113d（`ui_zoom_fix_v0.113d.md`，zoom 补偿）的后续反馈：Ctrl+± 缩放到一定
> 程度后，**统计页的饼图 / treemap 会超出自己的容器然后被裁切**。v0.113d 修了主体布局
> 与 vh/vw 尺寸，漏掉了 d3 图表 —— 它们用 `getBoundingClientRect()` 取容器尺寸去设 SVG
> 尺寸，在 zoom 下被双重放大。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 缩放到一程度都，统计中的饼图会超出自己的容器然后被裁切

（第一轮修复 `_statsHostSize` 后用户复测）

> 那个容器不跟着缩放的话，不管怎么改饼图都是会超的

（第二轮修复 viewBox + 高度共享预算后用户复测）

> 饼图本体被缩小很多

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 缩放后统计页饼图 / treemap 超出容器被裁 | 指令 1 |
| B | **容器本身被 zoom 压缩，只改饼图尺寸是治标** —— 图表必须跟随容器 | 指令 2 |
| C | **饼图本体被缩小很多** —— 溢出修好了，但饼图太小，需在跟随容器的前提下把饼图做大 | 指令 3 |

### 隐含但需自行决策的点

- **根因（第一层）**：`renderPie` / `renderTreemap`（app.js）用 `host.getBoundingClientRect()`
  取容器尺寸，再设 SVG 的 `.attr("width", w).style("width", w+"px")`。但 **`getBoundingClientRect()`
  在 zoom 下返回物理像素**（已乘 `--page-zoom`）：
  - 容器 CSS 宽 400px、zoom=1.5 → `rect.width = 600`（物理）。
  - SVG 设 `width:600px`（CSS 像素）→ 被 body zoom 再放大 1.5× → 视觉 900px。
  - 容器视觉宽 = 400×1.5 = 600px → SVG 超宽 300px，被容器/外层裁掉。
- **根因（第二层，用户指出的）**：`_statsHostSize` 把 rect 除回 CSS 像素后，SVG 尺寸 =
  容器 **CSS** 尺寸，但容器**视觉**尺寸 = CSS × zoom。v0.113d 的 body 补偿把 `body` 宽度设成
  `calc(100vw / zoom)`，导致 grid 列在 zoom 下**更窄**：饼图列视觉宽被压到 137px，而 SVG
  `Math.max(180, ...)` 下限给 180px **布局**像素（视觉 270px）→ 仍超宽被裁。**容器不跟随，
  图表尺寸永远对不上** —— 所以正解是让图表**完全跟随容器**。
- **其余 getBoundingClientRect 不必修**：全 app 共 5 处，另 3 处是定位/交互（tooltip 视口
  坐标、canvas pointer 归一化、卡片拖拽命中）—— 它们拿物理像素去算的也都是物理坐标 /
  比例，天然一致，除 zoom 反而错。
- **d3 用 SVG，无法用 CSS 百分比流式宽**：SVG 尺寸是 JS 算的（含图例换行、treemap 布局），
  必须在 JS 里换算回 CSS 像素。

---

## 3. 分析需求后得出的开发路径

```
主线  第一层：rect / --page-zoom 换算回 CSS 像素（A）
      第二层：SVG 改 viewBox + CSS 100%，跟随容器；高度预算 = 容器高（B）
  app.js  _statsHostSize(host)：rect / --page-zoom，返回 CSS 像素
          renderTreemap / renderPie 改用 _statsHostSize
          renderTreemap / renderPie：SVG 不设固定 px，改 viewBox + preserveAspectRatio，
            高度预算 = 容器高（图表区与图例共享）
  styles  .stats-chart-host 加 flex 居中，svg 高 auto 时垂直居中
```

### 开发顺序落地

```
#1 定位所有 getBoundingClientRect 用法，区分"设尺寸" vs "定位/交互"
#2 设计 _statsHostSize：rect 除以 --page-zoom（v0.113d 注入 body 的变量）
#3 改 renderTreemap / renderPie（第一层：换算 CSS 像素）
#4 headless 验证第一层：zoom=1.5 下 treemap 已吻合，但饼图仍溢出 133px（容器被压）
#5 定位第二层根因：容器列被压缩 + SVG Math.max 下限 → 改 viewBox + 高度共享预算
#6 headless 验证第二层：zoom=1 与 1.5 双图 0 溢出、精确贴合、图例完整可见
#7 用户复测"饼图本体被缩小很多"：定位第三层 = 饼图列 1/4 太窄 → 扩到 span 2
#8 headless 验证第三层：饼图视觉半径 zoom=1 93px / zoom=1.5 126px，随缩放放大
#9 版本 bump + node --check + 清理测试文件 + 写文档
```

---

## 4. 问题

### 4.1 物理像素被当 CSS 像素用（第一层）

`getBoundingClientRect()` 返回的是视口坐标尺寸 = CSS 尺寸 × zoom。d3 图表把这当 CSS 像素
设回 SVG，等于在 body zoom 之上**再乘了一次 zoom**：SVG 视觉宽 = 容器 CSS 宽 × zoom²，
而容器视觉宽 = 容器 CSS 宽 × zoom → 超宽 zoom 倍，被裁。

### 4.2 哪些 getBoundingClientRect 要修、哪些不要

| 行 | 用途 | 是否需除 zoom |
|---|---|---|
| 1898 `renderTreemap` | rect 宽高 → SVG 尺寸 | **要** |
| 2014 `renderPie` | rect 宽高 → SVG 尺寸 | **要** |
| 4474 disabled-tip | tooltip 定位（`position:fixed`，left/top 视口坐标） | 否（fixed 不被 zoom 放大，用物理像素正确） |
| 4558 canvas pointer | 事件坐标归一化（clientX/clientWidth 同是物理） | 否（比例一致） |
| 6728 卡片拖拽 | 命中检测（clientX/Y 物理 vs rect 物理） | 否（一致） |

### 4.3 用 clientWidth/clientHeight 还是 rect/zoom？

`clientWidth` 是 CSS 布局尺寸，理论上也可用。但现有代码大量用 `rect`，且 `clientWidth`
取不到 border 外宽（rect 含 border），两者口径不一致会引入回归 —— 统一用
`rect / --page-zoom`，与 v0.113d 的补偿口径一致。

### 4.4 容器本身被压缩（第二层 —— 用户指出的真根因）

第一层 `_statsHostSize` 修完后 headless 复测：

- zoom=1.5，treemap svg r=1109 = host r=1109 → **吻合**（treemap 列够宽）。
- zoom=1.5，**饼图 svg r=673 vs host r=540 → 仍溢出 133px**。

为什么饼图修了还超？v0.113d 的 body 补偿把 `body` 宽度压成 `calc(100vw / 1.5)`，grid
饼图列**视觉宽被压到 137px**，但 `renderPie` 的 `Math.max(180, ...)` 下限强制 SVG ≥180px
布局（视觉 270px）→ 视觉宽 270 > 容器 137，被 `.stats-chart-host { overflow:hidden }`
裁掉。**图表用的是固定下限像素，而容器在 zoom 下是动态被压的 —— 两者根本不对齐。**

结论：只要 SVG 尺寸由 JS 定死（无论是否除 zoom），容器一变就超。**正解 = 让 SVG 完全跟随
容器**：viewBox + CSS `width/height:100%`，SVG 盒子即容器盒子，永不超出。

### 4.5 饼图列只有 1/4 宽，跟随容器 = 跟随一个很窄的容器（第三层）

viewBox 修好后 headless 复测：zoom=1.5 饼图 svgRight−hostRight=0（不再溢出），但**视觉
半径只有 ~60px**（zoom=1 时 ~84px）—— 用户反馈"饼图本体被缩小很多"。

为什么？`.stats-grid` 是 4 列，`treemap` 是 `glass-card-wide`（`grid-column:1/-1` 占整行），
饼图落在**第二行只占 1/4 列**。zoom=1.5 时 body 补偿把主区压窄，饼图列视觉只剩 137px，
饼图半径被钳在 min(宽, 高)/2 ≈ 60px。**viewBox 只是让饼图跟随容器 —— 但容器本身就是个
很窄的柱子，饼图自然小。** 要饼图不小，必须先把容器做宽。

---

## 5. 解决

### 5.1 app.js：_statsHostSize 辅助

```js
function _statsHostSize(host) {
  const r = host.getBoundingClientRect();
  const z = parseFloat(getComputedStyle(document.body).getPropertyValue("--page-zoom")) || 1;
  return { w: r.width / z, h: r.height / z };
}
```

- `--page-zoom` 是 v0.113d `applyPageZoom()` 注入 body 的变量（zoom=1 时也是 1），无需
  新增状态。
- zoom=1 时 `rect/1 = rect`，行为不变（无回归）。

### 5.2 renderTreemap / renderPie 改用 viewBox + 高度共享预算

**第一层**：尺寸改用 `_statsHostSize`（CSS 像素）。

**第二层**：SVG 不再 `.attr("width", w).attr("height", totalH).style("width", w+"px")…`
设固定 px，改为：

```js
// 不设固定 px（会双倍放大 / 超出容器），改 viewBox + preserveAspectRatio，
// 尺寸由 CSS 的 .stats-chart-host svg { width:100%; height:100% } 跟随容器。
const svg = d3.select(host).append("svg")
  .attr("viewBox", "0 0 " + w + " " + totalH)
  .attr("preserveAspectRatio", "xMidYMid meet");
```

**高度预算 = 容器高度**（treemap / pie 区与图例共享，让 viewBox 宽高比 == 容器，精确贴合、
图例也完整可见 —— 顺带修掉 zoom=1 时图例被 `overflow:hidden` 裁掉 16~22px 的旧问题）：

```js
// treemap：总高 = 容器 CSS 高，treemap 区 = 容器高 - 图例
let treeH = baseH - legendH;
if (treeH < 60) treeH = 60;              // 图太小保底
const totalH = treeH + legendH;          // 一般 == size.h

// pie：饼图区 = 容器 CSS 高 - 图例
const pieH = Math.max(60, (size.h || 220) - legendH);
const totalH = pieH + legendH;           // == size.h
```

### 5.3 styles：.stats-chart-host flex 居中

当 viewBox 宽高比（受 `Math.max(180,…)` 宽度下限影响）比容器更窄时，svg 盒子小于容器，
flex 让它垂直居中而非贴顶留空：

```css
.stats-chart-host {
  display: flex;
  align-items: center;
  justify-content: center;
}
```

### 5.4 headless 验证（复刻 app.js 算法的独立页）

容器 CSS 尺寸取 `_statsHostSize`，SVG 设 `viewBox` + `preserveAspectRatio`，复测 zoom=1 / 1.5：

| 场景 | 图表 | svgRight−hostRight | svgBottom−hostBottom |
|---|---|---|---|
| zoom=1 | treemap / pie | 0 / 0 | 0 / 0（精确贴合） |
| zoom=1.5 | treemap | 0 | 0（viewBox 比 == 容器，精确贴合） |
| zoom=1.5 | pie | 0 | −81（svg 在容器内垂直居中，不超出） |

对照第一轮（固定 px）：zoom=1.5 饼图 svg r−host r = **+133**（溢出被裁）→ 本轮全部 ≤0，
溢出消除。图例在 zoom=1 也完整可见（不再被 `overflow:hidden` 裁底）。

### 5.5 饼图列扩到 2/4（styles，第三层：把容器做宽）

```css
.stats-grid .glass-card[data-card="stats-pie"] {
  grid-column: span 2;
}
```

- 只针对统计页饼图卡（`data-card="stats-pie"`），treemap 整行不变。
- `glass-card-wide`（`1/-1`）是整行，不适合饼图（饼图高度受限，整行只加空两侧）；span 2
  半行最合适。

headless 实测（真实 CSS + 复刻 renderPie 的 d3 渲染，量 arc bbox）：

| 场景 | 饼图视觉半径 | 溢出 |
|---|---|---|
| zoom=1 | **93px**（span 1 时 84px） | svgRight−hostRight=0 |
| zoom=1.5 | **126px**（span 1 时 ~60px） | svgRight−hostRight=0 |

饼图半径随缩放**放大**（126 > 93），正是"容器不跟着缩放"想要的语义：容器变宽，饼图变大。
zoom=1 时图例折叠成 1 行（卡宽了放得下），`pieH = 容器高 − 图例` 预算让 viewBox 比 ==
容器比（409:220），0 留白。

---

## 6. 是否完全按规划

**前两轮按规划；第三轮因用户反馈调整了方案** —— 这正是文档要记录的偏差：

- 第一轮只做 `_statsHostSize`（rect/zoom）就收尾，**headless 实测发现饼图仍溢出 133px**，
  不能发版。
- 用户点破："容器不跟着缩放，改饼图没用"。据此把方案从"换算尺寸"升级为"**让图表跟随
  容器**"（viewBox + CSS 100% + 高度共享预算 + flex 居中）。
- 用户复测："饼图本体被缩小很多"。viewBox 让饼图跟随容器没错，**但饼图列只有 1/4 宽，
  跟随的是个窄柱子** —— 再做一步"**把容器做宽**"：`data-card="stats-pie"` 扩到 `span 2`。
- 改动范围仍收敛在统计图表：只动 `renderTreemap` / `renderPie`（app.js）+ `.stats-chart-host`
  / `.stats-grid span`（styles），其余定位/交互 rect 未动（tooltip / pointer / 拖拽不受影响）。
- zoom=1 无回归：headless 实测 0 溢出、图例完整；且顺带修复图例被裁的旧问题。
- 版本号 bump：app.js `?v=20260822-14`、styles `?v=20260822-16`（index.html + live_panel.html）。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | v0.113i `_statsHostSize(host)`（rect 物理尺寸 ÷ `--page-zoom` 得 CSS 像素）；`renderTreemap` / `renderPie` 改用；SVG 去掉固定 px，改 `viewBox` + `preserveAspectRatio`，高度预算 = 容器高（treemap/pie 区与图例共享） |
| `src/relay/web/styles-20260817.css` | `.stats-chart-host` 加 `display:flex; align-items:center; justify-content:center`（svg 高 auto 时垂直居中）；`.stats-grid .glass-card[data-card="stats-pie"]` 加 `grid-column: span 2`（饼图列 1/4 → 2/4） |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-16`、app.js `?v=20260822-14` |
| `src/relay/web/live_panel.html` | 版本号 query bump：styles `?v=20260822-16`（共享样式表缓存） |

### 状态流

- **图表缩放（A）**：`renderTreemap` / `renderPie` 渲染时用 `_statsHostSize` 取 CSS 像素
  尺寸作为 viewBox 坐标 → SVG 盒子 = 容器盒子（CSS 100%），任意 zoom 下视觉尺寸 = 容器
  视觉尺寸，不超出被裁。
- **容器跟随（B）**：viewBox 让 SVG 完全跟随容器，容器被 zoom 压窄时图表等比缩小并
  垂直居中，永不溢出。缩放后已渲染图与容器等比，视觉协调，无需重绘。
- **饼图大小（C）**：饼图列扩到 2/4，容器本身变宽 → 饼图视觉半径 zoom=1 ≈93px、
  zoom=1.5 ≈126px，**随缩放放大**，不再"被缩小很多"。treemap 整行不受影响。

### 验证

- `node --check app.js` 通过。
- headless（复刻 app.js 算法独立页）：zoom=1 / 1.5 双图 svgRight−hostRight = 0、
  svgBottom−hostBottom ≤ 0（treemap =0 精确贴合，饼图 ≤0 居中不超）；对照固定 px 路径
  zoom=1.5 饼图 +133 溢出；span 2 后饼图视觉半径 zoom=1 93px / zoom=1.5 126px（arc bbox）。
- 刷新 GUI：先 Ctrl+± 缩放到某档，再进统计页 —— 饼图 / treemap 完整落在卡片容器内，
  不超出被裁、饼图随缩放变大；缩放回 1 正常，图例完整可见。
