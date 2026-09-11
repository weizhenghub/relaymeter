# 滚动条统一样式（v0.113f）开发文档

> 承接 v0.113e（`ui_upstream_picker_v0.113e.md`，上游选择器自绘下拉）的后续反馈：
> 侧栏 / 自绘上游菜单 / modal 内容区等滚动容器露出**系统默认滚动条**（Windows 宽条、
> 方角、灰色），与玻璃 UI 不搭，要求改成符合整体风格的样式。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 滚动进度条改样式

（"滚动进度条"即 scrollbar —— 用户看到的展开菜单 / 侧栏里那条系统默认滚动条。）

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 所有滚动容器的滚动条统一为符合整体风格的细条样式 | 指令 1 |

### 隐含但需自行决策的点

- **现状碎片化**：主窗只有 `.card-body-list`（6px hairline 圆角 3px）、`#card-upstream-body`
  （8px hairline 圆角 4px）、live_panel 三个容器（6px hairline 圆角 3px）有细条样式；
  侧栏 `.sidebar`、自绘上游菜单 `.upstream-menu`（v0.113e 新增，max-height:240px +
  overflow-y:auto）、modal 内容区等**无任何滚动条样式** → 露系统默认宽条。
- **`.main` 的滚动条是刻意隐藏的**（v0.50：`scrollbar-width:none` +
  `.main::-webkit-scrollbar{display:none}`），统一样式时不能把它"救回来"。
- **live_panel 是独立 webview 窗口**，需要同款全局规则（它已 link styles-20260817.css
  拿 CSS 变量，但滚动条规则得在它自己的 live_panel.css 里也放一份才最稳）。
- **不能逐个容器补**：滚动容器会越来越多（v0.113e 的菜单、未来新增），逐个体检补样式
  治标不治本 —— 应加**全局兜底**（`*` 级规则），新滚动容器自动继承；已有特殊容器靠
  specificity 覆盖保持原样。

---

## 3. 分析需求后得出的开发路径

```
主线  全局滚动条兜底（A）—— 纯 CSS
  styles-20260817.css   * scrollbar-width/color + *::-webkit-scrollbar 系列
                        （6px 细条、透明 track、hairline 圆角 thumb、hover 变深）
  live_panel.css        同款全局规则（独立窗口）
```

### 开发顺序落地

```
#1 盘点现有滚动条样式：哪些容器有、哪些没有；.main 隐藏逻辑确认
#2 设计全局兜底规则（* 级），确认与既有局部规则 specificity 无冲突
#3 落代码：styles + live_panel.css 各一段
#4 headless 验证：侧栏 6px、.main 仍隐藏、#card-upstream-body 仍 8px
#5 版本 bump + 清理测试文件 + 写文档
```

---

## 4. 问题

### 4.1 侧栏 / 自绘菜单 / modal 内容区露系统默认滚动条

`.sidebar`（overflow-y:auto）、v0.113e 新增的 `.upstream-menu`（max-height:240px +
overflow-y:auto）、modal 内容区都没有滚动条样式，Windows 下显示系统默认宽条（约 17px、
方角、白 track + 灰 thumb），在玻璃 UI 里非常突兀 —— 这正是用户反馈的来源。

### 4.2 逐个补样式 vs 全局兜底

滚动容器数量会持续增长（v0.113e 的菜单、未来的视图/弹窗）。逐个加专门规则 = 每次新增
容器都要记得补，遗漏即复发。全局 `*` 兜底一次解决所有现在和将来的容器，是正确做法。

### 4.3 全局规则不能破坏既有刻意设计

`.main` 的滚动条是 v0.50 **刻意隐藏**的（隐藏滚动位置条，靠滚轮/触屏滚动）；`
#card-upstream-body` 是 v0.12.5 特意加到 **8px**（比默认 6px 略宽，因为它承载上游列表、
hover 变化明显）。全局 `*` 规则 specificity（0,0,1）低于这些局部规则（0,2,1 / 1,2,1），
天然不覆盖 —— 但要验证确认。

---

## 5. 解决

### 5.1 全局滚动条兜底（styles-20260817.css）

```css
* { scrollbar-width: thin; scrollbar-color: var(--hairline) transparent; }
*::-webkit-scrollbar { width: 6px; height: 6px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb { background: var(--hairline); border-radius: 3px; }
*::-webkit-scrollbar-thumb:hover { background: var(--text-muted, var(--text-secondary)); }
*::-webkit-scrollbar-corner { background: transparent; }
```

- 6px 细条、透明 track、`--hairline`（极淡边框色）圆角 thumb、hover 变 `--text-muted`。
- specificity 检查：`.main::-webkit-scrollbar{display:none}`（0,2,1）与
  `.main{scrollbar-width:none}`（0,1,0）都高于 `*`（0,0,1）→ `.main` 仍隐藏；
  `.card-body-list`（0,2,1）与全局一致；`#card-upstream-body`（1,1,1）> 全局 → 仍 8px。

### 5.2 live_panel.css 同款

独立 webview 窗口放一份完全相同的全局规则，覆盖该窗口里没有专门样式的滚动容器。

### 5.3 headless 验证

- 侧栏：webkit scrollbar 6px、thumb `rgb(229,229,234)`（= hairline）圆角 3px、
  Firefox `scrollbar-width:thin` + `scrollbar-color: hairline transparent` —— 全局兜底生效。
- `.main`：Firefox `scrollbar-width:none` 仍隐藏（webkit `display:none` 同前）。
- `#card-upstream-body`：8px + 圆角 4px 保持不变。

---

## 6. 是否完全按规划

**完全按规划，无偏差。**

- 用全局 `*` 兜底而非逐容器补，新滚动容器（含 v0.113e 的自绘菜单）自动继承细条样式。
- `.main` 刻意隐藏、`#card-upstream-body` 8px、`.card-body-list` 6px 等既有局部设计
  全部保持（specificity 覆盖验证通过）。
- 主窗 + live_panel 两个窗口都加了同款规则。
- 版本号统一 bump：styles-20260817.css `?v=20260822-12`、live_panel.css `?v=20260822-06`
  （live_panel.html 里 styles 引用也从 07 同步到 12）。后端零改动。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/styles-20260817.css` | v0.113f 全局滚动条兜底：`*` scrollbar-width/color + `*::-webkit-scrollbar` 系列（6px / 透明 track / hairline 圆角 thumb / hover 变深 / corner 透明） |
| `src/relay/web/live_panel.css` | v0.113f 同款全局滚动条规则（独立窗口） |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-12` |
| `src/relay/web/live_panel.html` | 版本号 query bump：styles `?v=20260822-12`、live_panel.css `?v=20260822-06` |

### 状态流

- **滚动条统一（A）**：任何滚动容器（侧栏、自绘上游菜单、modal 内容区、card-body-list、
  live_panel 三个内容容器等）都显示 6px 细条 + hairline 圆角 thumb，与玻璃 UI 同风格；
  鼠标悬停变 `--text-muted`。刻意隐藏的 `.main` 与特宽的 `#card-upstream-body` 行为不变。

### 验证

- headless computed-style：侧栏 6px hairline 圆角 thumb + Firefox thin；`.main`
  scrollbar-width:none 仍隐藏；`#card-upstream-body` 仍 8px 圆角 4px。
- 刷新 GUI：侧栏、上游选择器展开菜单、设置页列表等滚动条的细条样式一致；主内容区仍
  无滚动条（滚轮可滚）。
