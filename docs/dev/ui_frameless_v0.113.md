# 实时流侧栏紧凑化 + 无边框开关（v0.113a/b/c）开发文档

> 本批承接 v0.112c-g（`ui_liquid_glass_wrapup_v0.112c-g.md`，液态玻璃 + UI 收尾）之后的
> 连续 UI 需求：先把实时流侧栏卡片改极窄边框 + 极窄边距，再给「设置」加两枚
> 「无边框」开关（主窗总览页 / 实时流侧栏）。核心难点在第二枚开关 —— 实时流
> 侧栏是**独立 webview 窗口**，跨窗口状态不能走主窗的 localStorage，改走
> 「后端持久化 + 桥广播」链路。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 1、侧边实时流栏，所有卡片用极窄边框 + 极窄边距
>
> 2、设置增加"无边框"，开启后总览页所有卡片去掉玻璃容器，裸悬浮在底背景上
>
> 文字距离边框太近了又显得很丑，改一下或者撤销
>
> 至少保持距离左边缘1个文字大小，上下0.8个文字大小的距离
>
> 设置的无边框中添加"侧边栏无边框"

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 实时流侧栏所有卡片：极窄边框 + 极窄边距 | 指令 1 |
| B | 设置「无边框」开关：开启后总览页所有卡片去玻璃容器、裸底悬浮 | 指令 2 |
| A' | 边距精确约束：距离左/右边缘 1 个文字大小、上下 0.8 个文字大小 | 指令 4 |
| C | 设置「侧边栏无边框」开关：实时流侧栏卡片裸底 | 指令 5 |

### 隐含但需自行决策的点

- **A 的迭代**：首版边距压到 `2px 6px` 触发「文字贴边框太丑」的返工（指令 3），
  最终按指令 4 用 **em 相对单位**（`0.8em 1em`）跟随字号缩放，不写死 px。
- **C 的「侧边栏」指实时流侧栏**：左侧导航栏 v0.43 起本就无框（`.sidebar` 无
  background/border-right），语境承接 v0.113a/b 的「卡片裸底」，必指右侧实时流
  侧栏（live panel）窗口。
- **C 是跨窗口状态**：实时流侧栏是独立 webview 窗口（live_panel.html），不共享
  主窗的 `prefs` localStorage（跨窗口 localStorage 不可靠，且侧栏窗口有自己的
  bridge）；必须走后端持久化 + 桥广播，与主题同步（`_push_theme_to_panels`）
  同款通道。

---

## 3. 分析需求后得出的开发路径

```
主线一  实时流侧栏卡片紧凑化（A + A'）—— 纯 CSS，先行
  live_panel.css
    .live-panel-card / .live-panel-header：border 1px → 0.5px
      圆角 --radius-lg(16px) → --radius-md(8px)
      内边距：首版 2px 6px → 返工后 0.8em 1em（em 相对字号）
      卡内 gap 4px → 3px
    .live-panel-main / .grid-region / .grid-rows：卡间距 4px → 3px
    内层内容框（pre/stream/tool/key）保留 1px hairline —— 是内容不是卡片

主线二  设置「无边框」开关 → 总览页裸底（B）—— 主窗前端口径
  app.js
    prefs.noFrame 字段（localStorage）
    applyPrefsClass() 切 body.overview-no-frame
    设置页「外观」组新增开关 .prefs-no-frame + change handler
  styles-20260817.css
    body.overview-no-frame .view[data-view="overview"] .glass-card
      清背景/边框/阴影/backdrop-filter/hover 上浮/内高光
    （.top-model-spotlight 本就裸底，无需覆盖）

主线三  设置「侧边栏无边框」开关 → 实时流侧栏裸底（C）—— 后端持久化 + 桥广播
  config.py       relay_gui_panel_frameless 字段（.env 键）
  gui.py          get/set_live_panel_frameless() 桥 + _push_frameless_to_panels()
                  _on_panel_loaded 加载时同步一次当前值
  app.js          bridge 方法 + 设置页开关 + refreshPrefsDynamic 拉初值
  live_panel.js   window.setPanelFrameless(on) 切 body.panel-no-frame
  live_panel.css  body.panel-no-frame 裸底规则（内层内容框保留 hairline）
```

### 开发顺序落地

```
#1 实时流侧栏卡片紧凑化（A）：0.5px 边框 + 边距收窄 + 圆角缩小
#2 返工（A'）：边距 2px 6px → 0.8em 1em（用户两次反馈）
#3 设置「无边框」开关（B）：主窗前端口径
#4 设置「侧边栏无边框」开关（C）：后端字段 + 桥 + 广播 + 侧栏 JS/CSS
```

---

## 4. 问题

### 4.1 A 首版边距过紧（指令 3）

首版把卡片内边距压到 `2px 6px`（极窄），用户反馈「文字距离边框太近了又显得
很丑，改一下或者撤销」。

### 4.2 边距该用什么单位（指令 4）

用户给出精确约束「至少保持距离左边缘 1 个文字大小，上下 0.8 个文字大小」。
若写死 px（如 `13px 10.4px`），侧栏字体或页面缩放（v0.112k Ctrl+±）时会失配。

### 4.3 C 的跨窗口状态同步

实时流侧栏是**独立 webview 窗口**（live_panel.html + live_panel.js），与主窗
（index.html + app.js）不在同一 document：
- 主窗的 `prefs` 对象存 localStorage —— 侧栏窗口读不到主窗的 localStorage
  （不同窗口的 localStorage 是同源的，但实时栏是独立 pywebview window，且
  pywebview 的 storage 分区不可靠）。
- 侧栏的 body class 只能由侧栏自己 document 内的 JS 切换 —— 主窗 `applyPrefsClass()`
  无法操作侧栏 DOM。

若用「主窗 localStorage + 侧栏加载时读」方案，侧栏已打开的情况下切换开关不会
即时生效（必须关掉重开）。用户预期是像总览页那样即时变。

### 4.4 侧栏窗口 DOM 未就绪时的广播

侧栏窗口可能尚未加载（`evaluate_js` 对未就绪 DOM 会抛异常），广播必须静默吞
异常；且侧栏每次重新打开都要能恢复到正确状态（持久化初值）。

---

## 5. 解决

### 5.1 A / A'：live_panel.css 紧凑化

- `.live-panel-card` / `.live-panel-header`：`border: 1px → 0.5px solid
  var(--card-border)`；圆角 `--radius-lg(16px) → --radius-md(8px)`；内边距
  `padding: 0.8em 1em`（左右 1 个文字大小 / 上下 0.8 个文字大小，13px 字号 =
  13px / 10.4px，跟随字号缩放）；卡内 gap `4px → 3px`。
- `.live-panel-main` / `.grid-region` / `.grid-rows`：卡片间距 `4px → 3px`。
- 内层内容框（`.live-panel-pre` / `.live-panel-stream` / `.live-panel-tool` /
  `.live-panel-key`）保持 1px hairline —— 它们是卡片内容而非卡片本体，无边框
  议题下仍需可读分隔。

### 5.2 B：主窗「无边框」开关（纯前端）

- `prefs.noFrame`（localStorage，前端偏好，不落后端）。
- `applyPrefsClass()` 按 `prefs.noFrame` 切 `body.overview-no-frame`。
- 设置页「界面与偏好 → 外观」组新增「无边框」开关（`.prefs-no-frame`），change
  仅 `prefs.noFrame = checked; savePrefs()`。
- CSS：`body.overview-no-frame .view[data-view="overview"] .glass-card` 清
  背景/边框/阴影/backdrop-filter/hover 上浮/内高光（与 v0.112i 四页裸底同款）。

### 5.3 C：侧边栏无边框（后端持久化 + 桥广播）

与主题广播同款通道，状态落后端：

- **config.py**：`relay_gui_panel_frameless: bool = False`（.env 键
  `RELAY_GUI_PANEL_FRAMELESS`）。
- **gui.py**：
  - `get_live_panel_frameless()` / `set_live_panel_frameless(enabled)` 桥方法
    （持久化 + `update_env_var` + 广播）。
  - `_push_frameless_to_panels(enabled)` = `panel_window.evaluate_js(
    "setPanelFrameless(true/false)")`，静默吞异常（DOM 未就绪时）。
  - `_on_panel_loaded` 加载时同步一次当前值 —— 侧栏每次重新打开都正确。
- **app.js**：bridge 加 `get/setLivePanelFrameless`；「无边框」开关下方新增
  「侧边栏无边框」开关；`refreshPrefsDynamic` 拉后端初值。
- **live_panel.js**：`window.setPanelFrameless(on)` 切 `body.panel-no-frame`。
- **live_panel.css**：`body.panel-no-frame .live-panel-card/.live-panel-header`
  清背景/边框/阴影/内高光；内层内容框保留 hairline。

---

## 6. 是否完全按规划

**完全按规划，无偏差。**

- A 有两次返工（`2px 6px → 0.8em 1em`），属于按用户反馈迭代，最终口径与指令 4
  一致；最终文件是 live_panel.css `?v=20260822-05`（含 C 的裸底规则）。
- B 纯前端，切换即时生效（savePrefs → applyPrefsClass → body class）。
- C 采用后端持久化 + 桥广播，规避了跨窗口 localStorage 的不可靠；侧栏已打开时
  切开关即时广播生效，侧栏重开/重启后由 `_on_panel_loaded` 恢复初值。
- 版本号统一 bump：app.js `?v=20260822-09`、live_panel.css `?v=20260822-05`、
  live_panel.js `?v=20260822-02`、styles-20260817.css 保持 `?v=20260822-08`
  （B 的裸底规则在上一批已 bump）。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/live_panel.css` | v0.113a 卡片紧凑化（0.5px 边框 / 0.8em 1em 边距 / radius-md）+ v0.113c `body.panel-no-frame` 裸底规则 |
| `src/relay/web/app.js` | v0.113b `prefs.noFrame` + `applyPrefsClass` + 设置开关；v0.113c bridge 方法 + 「侧边栏无边框」开关 + `refreshPrefsDynamic` 初值 |
| `src/relay/web/styles-20260817.css` | v0.113b `body.overview-no-frame` 裸底规则 |
| `src/relay/web/live_panel.js` | v0.113c `window.setPanelFrameless(on)` |
| `src/relay/web/index.html` / `live_panel.html` | 版本号 query bump |
| `src/relay/gui.py` | v0.113c `get/set_live_panel_frameless()` + `_push_frameless_to_panels()` + `_on_panel_loaded` 同步 |
| `src/relay/config.py` | v0.113c `relay_gui_panel_frameless` 字段 |

### 状态流

- **主窗总览页无边框（B）**：`prefs.noFrame`（localStorage）→ `applyPrefsClass()`
  → `body.overview-no-frame` → CSS 清掉总览 6 卡玻璃容器。
- **实时流侧栏无边框（C）**：设置开关 → 桥 `set_live_panel_frameless` → 写
  `.env` + `_push_frameless_to_panels` → `evaluate_js("setPanelFrameless(…)")`
  → `body.panel-no-frame` → CSS 清掉侧栏卡片玻璃容器。侧栏重新打开由
  `_on_panel_loaded` 推当前值。

### 验证

- `node --check` app.js / live_panel.js 通过；`ast.parse` gui.py / config.py 通过。
- 刷新 GUI：设置页开「无边框」→ 总览 6 卡立即裸底；开「侧边栏无边框」→ 实时
  流侧栏卡片立即裸底；侧栏重开 / 重启后状态保持（后端持久化）。
- 实时流侧栏卡片肉眼确认：0.5px 细边框、边距左/右 1 文字、上/下 0.8 文字。
