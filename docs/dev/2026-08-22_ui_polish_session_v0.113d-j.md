# 2026-08-22 当日 UI 打磨会话总览（v0.113d → v0.113j）

> 当日连续 7 个版本（d/e/f/g/h/i/j），一条主线：**外观与缩放打磨**。每个版本都有独立
> 的 7 节开发文档（链接见 §2），本文是当日总入口 —— 一次会话改了什么、版本怎么演进、
> 最终动了哪些文件。

---

## 1. 当日用户指令（按时间顺序，一字未改）

| # | 版本 | 指令 | 对应文档 |
|---|---|---|---|
| 1 | v0.113d | 1、左下角上游选择器改成符合整体风格的样式；2、ctrl+缩放后部分ui直接超出边缘 | `ui_zoom_fix_v0.113d.md` |
| 2 | v0.113d·3 | 左下角上游选择器改成符合整体风格的样式。这个已经重复第3遍了 | `ui_zoom_fix_v0.113d.md` |
| 3 | v0.113e | 包括展开后的表格也要符合样式啊 | `ui_upstream_picker_v0.113e.md` |
| 4 | v0.113f | 滚动进度条改样式 | `ui_scrollbar_v0.113f.md` |
| 5 | v0.113g | 展开动效 | `ui_picker_animation_v0.113g.md` |
| 6 | v0.113h | 给所有页面切换都采用合适的动效 | `ui_view_animation_v0.113h.md` |
| 7 | v0.113i | 缩放到一程度都，统计中的饼图会超出自己的容器然后被裁切 | `ui_stats_zoom_fix_v0.113i.md` |
| 8 | v0.113i·2 | 那个容器不跟着缩放的话，不管怎么改饼图都是会超的 | `ui_stats_zoom_fix_v0.113i.md` |
| 9 | v0.113i·3 | 饼图本体被缩小很多 | `ui_stats_zoom_fix_v0.113i.md` |
| 10 | v0.113j | 所有内容框左侧的细彩色条删掉。以后不再需要此类设计 | `ui_remove_accent_bars_v0.113j.md` |

---

## 2. 各版本详情（7 篇独立文档）

| 版本 | 主题 | 详情文档 | 一句话 |
|---|---|---|---|
| v0.113d | 上游选择器胶囊化 + zoom 补偿 | [ui_zoom_fix_v0.113d.md](ui_zoom_fix_v0.113d.md) | 选择器第 3 版改成 `glass-surface-soft` 胶囊（对齐 quick-switch）；body 宽高反除 zoom + `--page-zoom` 变量补偿溢出 |
| v0.113e | 自绘展开下拉 | [ui_upstream_picker_v0.113e.md](ui_upstream_picker_v0.113e.md) | 原生 `<select>` 展开是 OS 渲染改不了样式 → 触发器按钮 + 自绘菜单面板，文档级事件委托一次绑定 |
| v0.113f | 滚动条样式 | [ui_scrollbar_v0.113f.md](ui_scrollbar_v0.113f.md) | 全局 thin + hairline 滚动条（`scrollbar-width` / `::-webkit-scrollbar` 双轨），主窗 + live_panel 独立窗 |
| v0.113g | 菜单展开动效 | [ui_picker_animation_v0.113g.md](ui_picker_animation_v0.113g.md) | `.open` 类驱动 opacity/transform 过渡；`hidden` 无过渡 → 两阶段（开要 reflow、关等 180ms） |
| v0.113h | 页面切换动效 | [ui_view_animation_v0.113h.md](ui_view_animation_v0.113h.md) | `display:none→block` 会触发 CSS 动画自播 → `.view:not([hidden])` 挂 `view-enter` 入场动画，fill-mode none |
| v0.113i | 统计图表 zoom 溢出 | [ui_stats_zoom_fix_v0.113i.md](ui_stats_zoom_fix_v0.113i.md) | 三层：`_statsHostSize` 除 zoom → SVG 改 viewBox+CSS 100% 跟随容器 → 饼图列扩 span 2（容器做宽） |
| v0.113j | 移除内容框彩色左条 | [ui_remove_accent_bars_v0.113j.md](ui_remove_accent_bars_v0.113j.md) | 删 modal 消息角色色条 + 设置页 active 绿条；保留 role 文本 / 淡绿底可辨性 |

---

## 3. 演进主线

一条关于「**容器与缩放**」的认知链，是当日最有价值的沉淀：

```
v0.113d  body 宽高反除 zoom 补偿 → 布局不溢出
v0.113i  ① getBoundingClientRect() 在 zoom 下返回物理像素 →
         SVG 固定 px 尺寸被双重放大 → 除以 --page-zoom 换算回 CSS 像素
v0.113i  ② 但容器本身在补偿下被压缩（饼图列视觉 137px）→
         图表只要由 JS 定死就永远对不上 → SVG 改 viewBox + CSS 100% 完全跟随容器
v0.113i  ③ 跟随容器 = 跟随窄柱子 → 饼图列扩 span 2（把容器做宽），
         饼图半径随缩放放大（zoom=1 93px / zoom=1.5 126px）
```

结论（写入各文档的经验）：**zoom 下所有"设尺寸"都要回到 CSS 像素 / 让元素跟随容器；
「定位/交互」类 getBoundingClientRect 用物理像素天然一致，不能一并除 zoom。**

另一条并行线是**选择器与动画**（v0.113d→e→g→h）：从"改原生控件样式"（失败 3 次）到
"自绘控件 + 两阶段过渡 + 视图入场动画"，沉淀了「原生控件不能定制就自绘」与
「hidden 无过渡」两个关键机制。

---

## 4. 改动文件与最终版本状态

| 文件 | 改动内容 | 版本 query（当前） |
|---|---|---|
| `src/relay/web/app.js` | `_statsHostSize`、`renderTreemap`/`renderPie` viewBox 化、上游选择器自绘（v0.113e 起） | `?v=20260822-14` |
| `src/relay/web/styles-20260817.css` | 胶囊选择器、自绘菜单面板、全局滚动条、open/view-enter 动画、stats-chart-host flex、饼图 span 2、删彩色左条 | `?v=20260822-17` |
| `src/relay/web/index.html` | 版本号 query 随 styles/app.js 递增 | `?v=20260822-17`（styles）/ `?v=20260822-14`（app.js） |
| `src/relay/web/live_panel.css` | v0.113f 全局滚动条（独立窗共享规则） | `?v=20260822-06` |
| `src/relay/web/live_panel.html` | styles query 递增 | `?v=20260822-17`（styles） |

> 注：`live_panel.js`（?v=20260822-02）与 `live_panel_grid.js`（?v=20260822-01）当天上午
> 有 v0.109–v0.112 改动，属 live 面板独立窗口工作，已有 `live_panel_*_v0.109~v0.112.md`
> 各文档覆盖，不属本会话（v0.113 外观系列）。

---

## 5. 验证手段沉淀

- **headless Edge 复刻真实渲染**：独立页复制 app.js 的 `_statsHostSize` / `renderPie` /
  `renderTreemap` 算法 + 真实 CSS，量 `getBoundingClientRect` 差值（svgRight−hostRight）
  与 arc bbox，zoom=1 / 1.5 双档量化溢出与饼图半径。
- **全页 DOM 扫描**：stub bridge（Proxy 假 `api.*`）+ 真实 app.js 渲染各视图 + 打开 modal，
  遍历计算样式找「彩色左边框 / 左侧细竖条背景」，量化而非猜测。

---

## 6. 是否完全按规划

**当日 10 条指令全部落地，无遗留。**

- 7 篇独立文档各 7 节结构完整；本文为当日总入口。
- 版本号最终态：styles `?v=20260822-17`、app.js `?v=20260822-14`、live_panel.css
  `?v=20260822-06`。
- `node --check app.js` 全程通过；headless 量化验证（溢出 diff / 饼图半径 / 色条残留）
  均记录在各版本文档 §5。
