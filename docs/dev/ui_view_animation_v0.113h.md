# 视图切换入场动效（v0.113h）开发文档

> 承接 v0.113g（`ui_picker_animation_v0.113g.md`，上游菜单展开动效）的后续反馈：
> 主窗**侧栏导航视图切换**（总览 / 实时 / 历史 / 上游 / 统计 / 设置 / 配置）目前是
> `hidden` 属性瞬间显隐，没有任何过渡，要求**所有页面切换都采用合适的动效**。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 给所有页面切换都采用合适的动效

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 所有视图切换（侧栏导航各视图之间）采用合适的动效 | 指令 1 |

### 隐含但需自行决策的点

- **视图切换机制**：`.view[hidden]{display:none}`（styles:1364），`setView()`（app.js:1484）
  靠翻转 `hidden` 瞬间显隐。要动效必须利用这个机制本身。
- **动效形态**：与 v0.113g 上游菜单动效同语言 —— **淡入 + 轻微上移**（`translateY(8px) → 0`
  + `opacity 0→1`），200ms ease-out。克制、不拖沓，符合玻璃 UI。
- **入场 vs 交叉过渡**：切走瞬间消失 + 切入淡入上移（入场动效）。**不做**旧视图淡出 +
  新视图淡入的交叉 —— 需要 JS 延迟 hidden 切换（每切慢 150ms、快速连点易出错），对"合适的
  动效"而言入场动效已足够且更稳。
- **纯 CSS 方案**：`.view` 从 `display:none` → `block` 时 CSS `animation` **自动播放一次**，
  零 JS 改动，所有视图（含 v0.110 config 页）自动获得，后续新增视图也自动继承。

---

## 3. 分析需求后得出的开发路径

```
主线  视图切换入场动效（A）—— 纯 CSS
  styles  @keyframes view-enter（淡入 + 上移 8px）+ .view:not([hidden]) 挂动画
```

### 开发顺序落地

```
#1 确认 setView 靠 hidden 翻转显隐；v0.100 的 intro 是卡内数字滚动、非视图切换
#2 设计纯 CSS 入场动画（display:none→block 时 animation 自动播）
#3 落代码：styles 一处
#4 headless 验证：显示视图有 animation、切换后新视图播动画 / 旧视图消失、fill 无残留
#5 版本 bump + 清理测试文件 + 写文档
```

---

## 4. 问题

### 4.1 hidden 翻转是瞬间显隐，无过渡机会

与 v0.113g 菜单动效同源问题：`hidden` 是 `display:none`，属性切换瞬间消失/出现，`transition`
对 `display` 无效。菜单用 `.open` class 两段式解决；视图切换有更简的解法 —— 见 4.2。

### 4.2 利用"display:none → block 时 animation 自动播放"

CSS `animation` 在元素**匹配 selector 且被渲染**时播放一次。`.view` 从 `display:none`
（hidden）变 `display:block` 的瞬间，元素重新渲染，`animation` 自动从头播 —— 无需 JS 干预、
无需 `.open` class、无需 reflow hack（animation 天然有 from 帧）。这是视图切换动效的零成本
实现路径。

### 4.3 同一视图内重渲染不能重播动画

`renderActiveView` / `renderAll`（app.js:1799）每 500ms tick 改的是 `.view` **内部**容器
（`#card-xxx-body`）的 innerHTML，`.view` 元素本身 display 状态不变 → animation 不重播。
只有 `setView` 翻转 hidden 才播。避免"每 tick 闪一下"。

### 4.4 动画结束不能残留位移/透明

`animation-fill-mode` 默认 `none`：动画结束回**基态**（无 transform、opacity 默认 1）。
基态 `.view` 没有设 transform/opacity → 结束后元素干净归位，无残留。

---

## 5. 解决

### 5.1 styles-20260817.css：view-enter 动画

```css
@keyframes view-enter {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
.view:not([hidden]) {
  animation: view-enter .2s var(--ease-out);
}
```

- 挂在 `.view:not([hidden])`（即当前显示的视图）上；每次 hidden 移除都播一次。
- transform 只在合成层，不触发 reflow；`.main` 的 overflow-y:auto 下 8px 位移瞬时、
  无布局抖动。
- 与 v0.113g 菜单动效（150ms）错开节奏：视图内容更大，用 200ms，稍从容。

### 5.2 headless 验证

- 显示视图 computed `animation=view-enter`、`animationDuration=0.2s`、`animationFillMode=none`。
- 切换后：新视图 `animation=view-enter`、旧视图 `display=none`（瞬间消失）。
- ⚠ headless 虚拟时间不推进 CSS 动画（与 v0.113g 同限），`transform` 停在 from 态是环境
  限制非代码问题；animation 由浏览器原生执行，fill 无残留由 `fill-mode:none` + 基态保证。

---

## 6. 是否完全按规划

**完全按规划，无偏差。**

- 用 display:none→block 时 animation 自动播放的特性实现，**纯 CSS、零 JS 改动**，规避了
  视图切换动效的所有复杂度（无需 .open class / reflow / transitionend）。
- 动效形态与 v0.113g 菜单动效同语言（淡入 + 轻微上移），全 UI 动效统一。
- 不做交叉过渡（克制 + 稳）；同视图 tick 重渲染不重播（验证过 renderActiveView 改内部
  容器）。
- 版本号 bump：styles-20260817.css `?v=20260822-14`（index.html / live_panel.html 同步）。
  app.js 零改动。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/styles-20260817.css` | v0.113h `@keyframes view-enter`（opacity 0→1 + translateY 8px→0，0.2s ease-out）+ `.view:not([hidden])` 挂动画 |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-14` |
| `src/relay/web/live_panel.html` | styles 引用同步 `?v=20260822-14` |

### 状态流

- **视图切换（A）**：点侧栏导航 → `setView` 翻转 hidden → 旧视图瞬间消失 → 新视图
  `display:none→block` 触发 `view-enter` 动画（淡入 + 上移 200ms）。总览/实时/历史/上游/
  统计/设置/配置全部生效；同视图 tick 重渲染不重播。

### 验证

- headless：显示视图 `animation=view-enter`、0.2s、fill=none；切换后新视图播动画、旧视图
  display:none。
- 刷新 GUI：点侧栏各视图，切换时新页面淡入 + 轻微上移，不再"啪"地硬切。
