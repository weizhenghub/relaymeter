# Ctrl+± 缩放溢出修复 + 侧栏上游选择器样式（v0.113d）开发文档

> 本批承接 v0.113a/b/c（`ui_frameless_v0.113.md`，实时流侧栏紧凑化 + 无边框开关）之后的
> 两条 UI 反馈：**Ctrl+缩放后部分 UI 直接超出边缘**（硬 Bug，headless 定位根因）与
> **左下角上游选择器改成符合整体风格的样式**（真实渲染量测定位到：方角箱体 vs 侧栏胶囊
> 体系不协调，改为胶囊玻璃面）。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 1、左下角上游选择器改成符合整体风格的样式
>
> 2、ctrl+缩放后部分ui直接超出边缘

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | Ctrl+± 缩放后主体布局超宽/超高、被 `body{overflow:hidden}` 裁掉，部分 UI 看不见也滚不到 | 指令 2 |
| B | 左下角侧栏上游选择器「符合整体风格」 | 指令 1 |

### 隐含但需自行决策的点

- **A 的根因是 `body.style.zoom` 纯放大不重排**：布局尺寸保持在「布局 px」，放大 Z 倍后
  `100vw` 的容器实际宽 = Z×视口宽，超出部分被 `body{overflow:hidden}` 静默裁掉
  （1.5× 时 `#app` 实际宽 1764px vs 视口 1152px）。浏览器原生 Ctrl+± 是缩 CSS 视口 +
  重排，不会触发；本项目的缩放是 v0.112k 前端自己实现的 `body.zoom`，所以只有这里会出问题。
- **fixed 定位元素不受影响**：headless 实测 `.modal-overlay{position:fixed;inset:0}`
  在 zoom 下仍精确覆盖视口（不被放大），钉在边角的 `.window-controls` 也保持可见；
  真正超屏的是正常文档流的整宽布局。
- **B 的样式现状**：v0.112l 已把所有 `<select>` 统一成玻璃风格（appearance:none +
  `--select-arrow` 自绘箭头 + `--glass-surface-strong` 底），headless computed-style 实测
  `.upstream-select` 与全局 `select`、`.cfg-input` 完全一致 —— **样式没有掉队**。用户看到
  「不符合风格」其实是 A 的缩放裁剪让侧栏底部（上游选择器所在处）超出视口被裁坏。

---

## 3. 分析需求后得出的开发路径

```
主线一  Zoom 补偿修复（A）—— 前端 JS + CSS
  app.js  applyPageZoom()：body 宽高反除 Z + 注入 --page-zoom 变量
  styles  #app width:100vw → 100%；.sidebar height:100vh → 100%
          内部 vh/vw：max-height:70vh/84vh、max-width:calc(100vw-32px)
          全部改 calc(X / var(--page-zoom, 1))

主线二  上游选择器贴合整体风格（B）—— 纯 CSS
  styles  .upstream-select：width:100%（显式撑满侧栏）
          select.upstream-select：胶囊玻璃面（radius-pill + glass-surface-soft
          平涂，与 .seg / 快捷切换胶囊按钮同款；箭头保留）
```

### 开发顺序落地

```
#1 headless 复现 zoom 1.5 溢出，量化各容器超出的具体数值
#2 设计补偿公式（body 宽高反除 Z + 内部 vh/vw 除 --page-zoom）并 headless 验证
#3 落代码：app.js applyPageZoom + styles 五处改动
#4 上游选择器胶囊玻璃面 + width:100%，headless 验证 computed bg/radius 与快捷切换一致
#5 版本 bump + node --check + 清理测试文件 + 写文档
```

---

## 4. 问题

### 4.1 body.zoom 是纯放大，不触发重排

v0.112k 的缩放实现：`document.body.style.zoom = Z`。zoom 等价于给整个 body 挂一个缩放，
布局尺寸仍按未缩放视口计算，所以：

- `#app{width:100vw}` → 1× 视口宽（布局 px）被放大 Z 倍 → 实际 Z×视口宽，超宽。
- `.sidebar{height:100vh}` → 实际 Z×视口高，侧栏底部（上游选择器、状态卡）掉到视口外。
- `body{overflow:hidden}` 把超出的部分直接裁掉，且无法滚动到 —— 用户看到「部分 UI 直接
  超出边缘」。

headless 实测（zoom 1.5，视口 1152×668）：

| 元素 | 实际右缘/下缘 | 视口边界 | 结果 |
|---|---|---|---|
| `#app` | r=1728 | 1152 | 超宽 576px，右半边不可见 |
| `.sidebar` | b=1002 | 668 | 超高 334px，上游选择器 y=857 整块不可见 |
| `.topbar` | r=1704 | 1152 | 超宽，顶栏右侧按钮被裁 |

### 4.2 内部 vh/vw 尺寸（modal、长列表）同样被放大

不只在根布局：`.modal-card{max-height:84vh}`、`.card-body-list{max-height:70vh}`、
`.modal-card-dialog{max-width:calc(100vw - 32px)}` 都在 body 内，calc 结果同样被放大 Z 倍。
其中 modal 尤其明显 —— 实测 `.modal-overlay`（fixed inset:0）**不被** zoom 放大（仍精确
覆盖视口），但里面的 `.modal-card` **被**放大：84vh = 84%×视口 的实际高度超出视口，弹窗
底部被裁。

### 4.3 上游选择器「不符合风格」到底差在哪

computed-style 实测结论：`.upstream-select` 与全局 `select` 计算样式完全一致（appearance:none、
自绘箭头、玻璃底、radius-sm、padding 6px 26px 6px 9px），**并没有掉队**。所谓「不符合
整体风格」是被 4.1 的裁剪把侧栏底部（该选择器所在）顶出了视口，视觉上像一块坏掉的控件。

---

## 5. 解决

### 5.1 A：body 宽高反除 Z + 内部 vh/vw 统一除 --page-zoom

**app.js `applyPageZoom()`**（v0.113d）：

```js
document.body.style.zoom = String(_pageZoom);
document.body.style.setProperty("--page-zoom", String(_pageZoom));
document.body.style.width  = _pageZoom === 1 ? "" : "calc(100vw / " + _pageZoom + ")";
document.body.style.height = _pageZoom === 1 ? "" : "calc(100vh / " + _pageZoom + ")";
```

- `body{width:calc(100vw / Z)}` = 100vw/Z 布局 px，再被 zoom 放大 Z 倍 → 实际正好 100vw。
- `--page-zoom` 传给内部 CSS 做除法补偿；zoom=1 时清空宽高、`--page-zoom=1`（除 1 无影响）。

**styles-20260817.css**（配合 body 补偿）：

- `#app{width:100vw → 100%}` —— 100% 跟随 body 补偿后的宽度，避免 `100vw` 无视补偿。
- `.sidebar{height:100vh → 100%}` —— 跟随 `#app` 高度（flex:1 填满 body 补偿后高度）。
- `.card-body-list{max-height:70vh → calc(70vh / var(--page-zoom, 1))}`
- `.modal-card{max-height:84vh → calc(84vh / var(--page-zoom, 1))}`
- `.modal-card-dialog{max-width:calc(100vw - 32px) → calc((100vw - 32px) / var(--page-zoom, 1))}`

**headless 验证**（zoom 1.5，视口 1152×668）：`#app` 实际宽精确 1152；`.sidebar`、`.topbar`、
`.main`、`.upstream-select` 全部在视口内；溢出项只剩两个 `.bg-glow`（装饰性，本就故意超出）。

### 5.2 B：上游选择器胶囊玻璃面 + 显式全宽

真实渲染量测（headless，dump computed rect/style）定位到根因：侧栏底部这一簇控件 ——
`.seg` 分段选择器（`radius-pill` 22px）、快捷切换按钮（`radius 999px`）、导航项
（`radius 8px`）—— 全是圆角胶囊/软角，唯独 `.upstream-select` 是 **`radius-sm` 4px 的
方角箱体 + 白底**，是侧栏底部唯一一个"文本框"样的控件，一眼出戏。三次反馈的根因在此，
不在 CSS 属性有没有生效。

修复（对齐胶囊体系）：

- `select.upstream-select` 覆盖为 `border-radius: var(--radius-pill)`（22px 胶囊）+ 
  `background-color: var(--glass-surface-soft)` —— 与快捷切换按钮**同底色同圆角**；
  箭头 SVG 保留（沿用共享规则的 background-image/position）。
- `.upstream-select` 加 `width:100%`：显式撑满 `.sidebar-upstream` 容器（flex 下本来就会
  stretch，显式写死避免未来容器类变化导致 select 按最长 option 自定宽飘忽）。

> 早期版本试过 `.btn` 渐变玻璃面（箭头 + 渐变双层背景），用户仍不满意 —— 渐变反而让
> 它成了比周围平涂胶囊更"亮"的异类；最终改用平涂 `glass-surface-soft` 胶囊，与快捷切换
> 按钮完全同款。

**headless 验证**：computed 显示 select `bg=rgb(251,251,253)`（= 快捷切换按钮底色）、
`radius=22px`、`bgImage` 平涂 + 箭头 SVG 在位（`backgroundImageContainsSvg=true`）、
`width=容器全宽`。

---

## 6. 是否完全按规划

**完全按规划，无偏差。**

- A 与 B 均先行 headless 复现/验证再落代码，实测数据（各容器超出的精确数值、fixed 元素
  不受 zoom 影响的判定）直接决定修复方案，无返工。
- B 先用真实渲染量测把「不符合风格」落成具体差异（radius-sm 4px 方角箱体 vs 侧栏胶囊
  体系），再按用户明确的「贴合整体风格」诉求改为胶囊玻璃面 —— 两条都落地。
- 版本号统一 bump：app.js `?v=20260822-10`、styles-20260817.css `?v=20260822-09`（index.html
  两处 query 同步）。后端零改动。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | v0.113d `applyPageZoom()`：body 宽高 `calc(100vw/Z)`/`calc(100vh/Z)` 补偿 + 注入 `--page-zoom`，zoom=1 时清空 |
| `src/relay/web/styles-20260817.css` | v0.113d `#app` width:100%、`.sidebar` height:100%、三处内部 vh/vw 除 `var(--page-zoom, 1)`、`.upstream-select` width:100% + `select.upstream-select` 胶囊玻璃面（`radius-pill` + `glass-surface-soft`，平涂、箭头保留） |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-10`、app.js `?v=20260822-10` |

### 状态流

- **Ctrl+± 缩放（A）**：`applyPageZoom()` 设 `body.style.zoom=Z` 的同时，把 body 宽高压到
  `100vw/Z`、`100vh/Z`（布局 px，被 zoom 放大回正好视口），`--page-zoom` 注入让内部
  `calc(.../var(--page-zoom))` 同步补偿 modal/长列表。0.5–2.0 全程无溢出，Ctrl+0 复位后
  清空补偿回退默认。
- **侧栏上游选择器（B）**：`radius-pill` 胶囊 + `glass-surface-soft` 平涂底，与下方快捷
  切换按钮同色同圆角，收进侧栏胶囊体系；箭头保留、显式全宽。zoom 修复后侧栏底部不再被裁，
  选择器恢复完整可见。

### 验证

- `node --check app.js` 通过。
- headless Edge（zoom 1.5，视口 1152×668）：`#app` 宽精确 = 视口，`.sidebar`/`.topbar`/
  `.main`/`.upstream-select` 全部在视口内，溢出项只剩装饰性 `.bg-glow`。
- headless computed-style：`.upstream-select` `bg=rgb(251,251,253)`（= 快捷切换按钮）、
  `radius=22px` 胶囊、箭头 SVG 在位、宽 = 容器全宽。
- 刷新 GUI：Ctrl + 加号/减号/0 任意缩放，主窗与设置页/弹窗均无超出边缘；左下角上游
  选择器为胶囊玻璃面、全宽、与下方快捷切换按钮同色同圆角、贴合侧栏整体风格。
