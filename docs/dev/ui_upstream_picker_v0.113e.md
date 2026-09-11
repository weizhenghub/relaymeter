# 侧栏上游选择器自绘下拉（展开列表贴合风格）（v0.113e）开发文档

> 承接 v0.113d（`ui_zoom_fix_v0.113d.md`）的后续反馈：把左下角上游选择器做成胶囊玻璃面后，
> 用户进一步指出 **「包括展开后的表格也要符合样式」** —— 原生 `<select>` 的下拉列表是
> OS 渲染（Windows 白底系统列表框），CSS 完全无法定制，唯一出路是把原生 select 换成
> **自绘下拉组件**（触发按钮 + 自定义菜单面板），展开后的列表才能贴合整体风格。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 1、左下角上游选择器改成符合整体风格的样式
>
> 2、ctrl+缩放后部分ui直接超出边缘
>
> 包括展开后的表格也要符合样式啊

（前两条在 v0.113d 处理；本条是追加的第三条 —— 展开后的下拉列表也要符合样式。）

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 上游选择器**展开后的选项列表**也要符合整体风格 | 指令 3 |

### 隐含但需自行决策的点

- **原生 `<select>` 的展开列表无法定制**：`.upstream-select` 框体可以用 CSS 玻璃化
  （v0.112l 已做），但点击后弹出的 option 列表是 **OS 渲染**的 —— Windows 上是一个白底
  系统列表框，跟玻璃 UI 完全脱节，CSS 改不动。要满足「展开后也符合样式」，只能把原生
  select 替换成**自绘下拉组件**。
- **自绘必须保持行为等价**：旧 select 的 option `value = "plat|name|model"`，change 后
  走 `applyUpstreamDirect(plat, name, model)`。自绘组件要 1:1 保留这套语义、分组结构
  （anthropic / openai 两组）、active 上游回显、选中后「切换中…」过渡态与切换链路。
- **事件绑定不能每 tick 重绑**：`renderSidebar` 每次 sig 变化都重建 `#sidebar-upstream-list`
  的 innerHTML，若在渲染函数里绑监听会无限堆叠 —— 自绘下拉的开关/选中/关闭必须用
  **document 级事件委托**只绑一次。

---

## 3. 分析需求后得出的开发路径

```
主线  上游选择器原生 select → 自绘下拉组件（A）—— JS + CSS
  app.js   renderSidebar：buildOpts(option) → buildRows(对象数组)，
           select+optgroup → 触发按钮 + 菜单面板（分组标签 + 选项按钮）
           新增 document 级 click/keydown 委托（开/关/选/点外部关闭/Esc）
  styles   .upstream-select：<select> → <button>（flex + SVG 箭头）
           新增 .upstream-picker / .upstream-menu / .upstream-menu-item 等
           select.upstream-select 覆盖规则删除（不再有 select 用它）
```

### 开发顺序落地

```
#1 确认原生 option 下拉无法定制（Windows OS 渲染），决定自绘
#2 app.js 重写 renderSidebar：buildRows + 触发按钮 + 菜单面板 + 全局委托
#3 styles 改 .upstream-select 为按钮、新增菜单面板样式、清死规则
#4 headless 验证：胶囊触发、菜单向上展开不被侧栏 overflow 裁剪、active 高亮
#5 版本 bump + node --check + 清理测试文件 + 写文档
```

---

## 4. 问题

### 4.1 原生 select 的展开列表是 OS 渲染

v0.112l / v0.113d 都只能玻璃化 select **框体**；点击展开的 option 列表是 Windows 系统
列表框（白底、系统字体、无边框圆角），与玻璃卡片 UI 完全脱节。CSS 的 `appearance`、
`::part` 都无法触达这个 OS 弹层 —— 想让它「符合样式」只能绕开原生 select。

### 4.2 自绘组件要与旧 select 行为严格等价

旧 select 承载了：按模型拆多 option（`0/1/N 个 allowed_models`）、按平台 optgroup 分组、
active 上游默认选中（v0.83 的 model 为 null 时选第一个模型的回显修正）、选中后走
`applyUpstreamDirect` + 四行「切换中…」setText。任何一个语义丢失都会破坏现有切换链路。

### 4.3 事件绑定与每 tick 重建冲突

`renderSidebar` 在 snapshot 变化时整块重写 `#sidebar-upstream-list`（有 sig 短路，但切换
上游后必然重建）。如果在渲染函数里给按钮绑 `addEventListener`，每次重建都叠加一份监听。
必须用 document 级委托 + `closest()`，只绑一次，靠 DOM 结构定位。

### 4.4 菜单面板会被侧栏 overflow 裁剪

`.sidebar { overflow-y: auto }`，若菜单向下展开到侧栏内容区之外会被裁掉/只能滚动。
侧栏上游选择器在侧栏**底部**（`margin-top:auto`），天然适合**向上展开** —— 下方是
quick-switch / status-card，上方是 logo / nav 大片空白。

---

## 5. 解决

### 5.1 自绘下拉组件（app.js）

**`renderSidebar` 内重写**：

- `buildOpts`（拼 `<option>`）→ `buildRows`（返回 `{value, label, selected}` 对象数组）。
  `value` 仍是 `plat|name|model`，selected 判定沿用 v0.83 逻辑（active + model 未指定时选
  第一个 allowed）。
- HTML 结构：`<select data-upstream-select>` → `.upstream-picker` 内放触发按钮
  `.upstream-select[data-upstream-trigger]`（当前 active 回显 + SVG 箭头）+ `.upstream-menu`
  菜单面板（`upstream-menu-group-label` 分组标签 + `upstream-menu-item[data-upstream-item]`
  选项按钮，active 项加 `.active`）。
- 触发按钮回显：取 selected 项的 label；无选中时回退第一条。

**全局事件委托（只绑一次，放在 renderSidebar 定义之后）**：

- `document` click 委托：
  1. 先关闭所有其它已打开的下拉（排除本次点击所在的下拉）；
  2. 点 `.upstream-trigger` → toggle 本下拉；
  3. 点 `.upstream-item` → 关下拉、解析 `data-value` 成 `plat|name|model`、四行
     「切换中…」setText、`applyUpstreamDirect(...).catch(...)`（与旧 change 同链路）。
- `document` keydown 委托：`Escape` 关闭所有下拉。

### 5.2 样式（styles-20260817.css）

- `.upstream-select` 从 `<select>` 规则改为 `<button>` 规则：`display:flex` +
  `justify-between` + `.upstream-select-current`（ellipsis）+ `.upstream-select-arrow`
  （真实 SVG 箭头，不再用 background-image）。胶囊 radius-pill + `glass-surface-soft`
  底（v0.113d 定的）保留。
- 新增 `.upstream-picker { position: relative }`、`.upstream-menu`（绝对定位、
  `bottom: calc(100% + 6px)` 向上展开、白底面板 + hairline + radius-md + 阴影、
  max-height 240px 内部滚动）、`.upstream-menu-group-label`、`.upstream-menu-item`
  （hover 玻璃底、`.active` 主色加粗）。
- 删除 `select.cfg-input, select.upstream-select` 共享规则里的 upstream-select 分支和
  `select.upstream-select` 胶囊覆盖（侧栏不再有 select.upstream-select），`select.cfg-input`
  箭头规则保留。

### 5.3 headless 验证

- 触发按钮：胶囊（radius 22px）、`bg=rgb(251,251,253)`（= 快捷切换按钮）、flex + SVG 箭头。
- 菜单：向上展开（触发按钮 y=465，菜单 y=315，间隔 6px），`MENU-CLIPPED=false`（不被侧栏
  overflow 裁剪），白底 + hairline + radius 8px。
- active 项高亮：light 主题 `--button-primary=#0a0a0a` → `rgb(10,10,10)` 加粗。
- `node --check app.js` 通过；旧 `data-upstream-select` / `select.upstream-select` 零残留。

---

## 6. 是否完全按规划

**完全按规划，无偏差。**

- 判断「原生 option 下拉无法定制」后直接走自绘，没有在 CSS 上徒劳挣扎。
- 自绘组件 1:1 保留旧 select 语义（value 三元组、分组、v0.83 回显修正、applyUpstreamDirect
  链路），且用 document 级委托规避了每 tick 重绑问题。
- headless 实测菜单向上展开不被侧栏 overflow 裁剪（4.4 的预判成真，向上展开是对的）。
- 版本号统一 bump：app.js `?v=20260822-11`、styles-20260817.css `?v=20260822-11`。后端零改动。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | v0.113e `renderSidebar`：buildOpts → buildRows、select/optgroup → 触发按钮 + `.upstream-menu` 菜单面板；新增 document 级 click/keydown 事件委托（开/关/选/点外部/Esc 关闭） |
| `src/relay/web/styles-20260817.css` | v0.113e `.upstream-select` 改按钮（flex + SVG 箭头）、新增 `.upstream-picker`/`.upstream-menu`/`.upstream-menu-group-label`/`.upstream-menu-item`、删除 `select.upstream-select` 死规则 |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-11`、app.js `?v=20260822-11` |

### 状态流

- **自绘下拉（A）**：触发按钮显示当前 active 上游 → 点击向上弹出玻璃菜单面板（分组标签 +
  选项，active 高亮主色）→ 点选项 → 关菜单 + `applyUpstreamDirect(plat, name, model)` +
  「切换中…」过渡态；点外部 / Esc 关闭。snapshot 变化 → `renderSidebar` 重渲染 → 触发回显
  与 active 高亮自动刷新。

### 验证

- `node --check app.js` 通过。
- headless：触发按钮胶囊玻璃面 + SVG 箭头；菜单向上展开、不被侧栏 overflow 裁剪；
  active 项主色加粗。
- 刷新 GUI：点左下角上游选择器 → 展开的列表是玻璃面板（分组 + 选项 + 高亮 active），
  与整体风格一致；选一项正常切换并重启；点外部 / Esc 关闭。
