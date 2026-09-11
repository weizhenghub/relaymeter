# 总览卡片间距 + 液态玻璃质感 + UI 收尾四项（v0.112c-0.112g）开发文档

> 本批承接 v0.111-0.112（`live_panel_icon_timeouts_ui_v0.111-112.md`，图标+三超时）之后的
> 一段连续 UI 需求：先调总览卡片间距，再上「液态玻璃」质感并修缺陷，最后一次收尾四项。
> 其中液态玻璃是研究型任务（用户只给了一个保存的博客网页做参考），故本批以「反推机制 →
> 迭代验证」为主线，与常规功能开发不同。

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 总览页卡片边距调小
>
> 卡片可否改成"液态玻璃"质感？这里有个网页你看看：'C:\Users\weizheng\Desktop\使用 CSS 和 SVG 实现苹果液态玻璃效果 _ 刘念的个人博客.html'
>
> 效果是对了但是玻璃中间有大块的圆圈咋回事
>
> 1、"用量最大的模型"不必做背景，直接悬空在裸底上。2、统计页面模式按钮点击没反应（内容变化但是按钮本体位置没动）。3、设置中依然有"数据显示 加载中..."，删掉。4、所有页面全部卡片换成液态玻璃。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 总览页卡片边距调小（grid gap 10→6px，卡 padding 12→10px） | 指令 1 |
| B | 卡片改「液态玻璃」质感（Apple Liquid Glass 方案，参考保存网页） | 指令 2 |
| C | 修「玻璃中间大圆圈」缺陷 | 指令 3 |
| D | 「用量最大的模型」去卡片背景，悬空裸底 | 指令 4-1 |
| E | 统计页三档模式按钮 active 高亮不动（内容变、按钮本体不动） | 指令 4-2 |
| F | 删设置页「数据显示 加载中…」死区 | 指令 4-3 |
| G | 全部页面卡片换成液态玻璃 | 指令 4-4 |

### 隐含但需自行决策的点

- **B 是研究型任务**：用户只给了保存的博客网页（无技术说明文字），液态玻璃的
  实现机制要从 HTML 反推出来，且是「你来做决定，我看效果」的设计拍板（沿用
  `feedback_decide_show_effect` 惯例，不走 AskUserQuestion）。
- **G 撞上 v0.41 性能教训**：v0.41 因 `backdrop-filter:blur` 拖慢 WebView2 而把
  全部玻璃下线。本次 SVG filter 比 blur 更重，扩展到全页面必须留降级路径
  （浏览器不支持 `backdrop-filter:url(#)` 时自动落回半透明渐变，仍是玻璃观感）。
- **D 与 G 的取舍冲突**：spotlight 自身带 `.glass-card` 类，既要「全卡玻璃」又要
  「这张裸底」→ 用 `:not(.top-model-spotlight)` 排除 + 独立清零规则覆盖，而不是
  在 HTML 里去掉 glass-card 类（布局/内边距仍靠它）。
- **C 的定位**：不是 CSS 层问题，是**资源源图缺陷** —— 反推出来的 PNG 位移图 /
  高光层自带圆形/亮斑，位移后变成可见鼓包 + 圆环。

---

## 3. 分析需求后得出的开发路径

按依赖顺序拆成三条主线：

```
主线一  卡片间距（指令 A）—— 纯 CSS，先行
  .view[data-view="overview"] .grid  gap 10px → 6px
  .view[data-view="overview"] .grid .glass-card  padding 12px → 10px

主线二  液态玻璃机制（B + C）—— 研究型，中间是迭代验证
  反推保存网页：backdrop-filter:url(#filter-id) 引用内联 SVG filter，
    SVG = feImage(噪声位移图) + feDisplacementMap(折射) + specular 高光层
  提取 PNG 资源（warp.png 位移图 + spec.png 高光层等 9 张去重）
  初版落地：index.html 顶部注入 SVG filter，总览 7 卡接 backdrop-filter
  缺陷暴露：位移图/高光层自带大圆斑 + 左上亮块 → 位移后成鼓包+圆环
  换方案：弃 PNG，用 feTurbulence 程序化低频噪声（无外部资源、无图案）

主线三  UI 收尾四项（D / E / F / G）—— 最后做
  E：stats 三档按钮 click handler 漏 syncStatsModeButtons()（JS bug）
  F：index.html 删 settings-passthrough 死 section（无渲染函数填充）
  G：液态玻璃选择器从总览扩到全局 .glass-card
  D：.top-model-spotlight 从玻璃选择器排除 + 清零规则（裸底）
```

### 开发顺序落地

```
#1 总览页卡片间距（A）
#2 液态玻璃机制反推 + 初版（总览 7 卡，B）
#3 圆斑缺陷修复 → feTurbulence（C）
#4 E：stats 按钮补 syncStatsModeButtons()
#5 F：删设置页死 section
#6 D + G：spotlight 裸底 + 全页面玻璃
#7 node --check / headless 截图验证 / 统一开发文档
```

---

## 4. 实现中遇到的问题

### 问题 1：液态玻璃机制未知 —— 只有一份保存的博客网页

用户没给技术说明，只有 `使用 CSS 和 SVG 实现苹果液态玻璃效果 _ 刘念的个人博客.html`
（272KB）。用 python 抽取而非 grep（Windows 读大文件 / grep 不可用场景）：
- **feTurbulence 不在 HTML 里** —— 技术是 `backdrop-filter: url(#filter-id)` + base64
  PNG `feImage` 噪声位移图 + specular 高光层。
- 提取出 9 张去重 PNG（warp / spec / 噪声平铺图等）。

### 问题 2：Read 工具读截图报 "Unsupported Image"

headless Edge 截图（PNG / JPEG 都试过）Read 均无法显示。改用 **PIL 像素分析**做
视觉验证：unique-colors 对比（svg 3484 vs blur 2910 vs none 1812）、radial-ring
亮度、horizontal-gradient 测量 —— 成为本批反复使用的工作流。

### 问题 3：玻璃中间有大块的圆圈（用户反馈）

初版总览卡玻璃中间出现大圆斑。定位到**源图 PNG 自带缺陷**：
- `warp.png` 左上角有白色亮块 → `feDisplacementMap` 位移后成鼓包；
- `spec.png` 中心有圆环 → specular 亮点放大成圈。

### 问题 4：headless Edge --screenshot 输出路径被重置

相对路径截图报「拒绝访问」。改用绝对路径（C:\...\_glass_shot.png）后正常。

### 问题 5：Python `%` 格式化字符串与 CSS 的 `%` 冲突

构造 base64 注入字符串时 `%`（CSS 的百分比/格式）与 Python 格式化互撞。
改用 `.replace('__TOKEN__', value)` 占位注入。

### 问题 6：`test_api_get_status_shape` 失败 —— 既有失效测试

跑验证时断言失败，多余 key 是 `passthrough_mode`。追查：透传模式功能给
`get_status()` 加了字段但没同步更新这个精确 key 集合断言，是既有死测试，与本次
UI 改动无关。

### 问题 7：E 的根因 —— click handler 漏了高亮同步

统计页三档按钮 click handler 里 `renderStats()` 正常换了内容，但**漏调
`syncStatsModeButtons()`**，active 类不跟着切，按钮本体视觉不动。

### 问题 8：玻璃下背景光晕「分块渐变」（用户反馈）

用户反馈「背景的光晕在玻璃下的部分晕染的不够均匀导致有很强的分块渐变感」。
根因在 **feTurbulence 噪声尺度与位移量的搭配**：
- `baseFrequency 0.014` → 波长 ~71px，比卡片尺度（~300px）只有几个周期，
  比光晕渐变过渡带（blur 80px）还粗 → 位移把平滑渐变**整体折叠**成几大块错位；
- `scale 16` 位移幅度大，块与块之间错位明显；
- `saturate 1.5` 把错位造成的色差再放大 → 视觉上就是清晰的分块渐变。
- 附带：headless Edge 对 SVG filter（普通 filter 与 backdrop-filter 的
  `url(#svg)`）在 SwiftShader 下**均不渲染**（feTurbulence/feDisplacementMap
  不生效），验证工作流从「headless 截图 + PIL」失效，只能靠原理推导 + 用户实测。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 位置 |
|---|---|---|
| #1 机制未知 | 反推确定机制；用 `feImage` base64 初版验证后，**弃 PNG 全部资源**，改 `feTurbulence` 程序化低频噪声 + `feColorMatrix` 高饱和（无外部依赖，file:// 下可加载，无图案） | index.html |
| #2 截图不可读 | PIL 像素分析替代 Read（unique-colors / radial-ring / gradient） | 验证工作流 |
| #3 大圆圈 | 换 `feTurbulence`（baseFrequency 0.014 0.014 / numOctaves 2 / seed 7）→ `feDisplacementMap` scale 16 → `feColorMatrix` saturate 1.5；filter 区域扩到 `x=-15% y=-15% w=130% h=130%` 防边缘切边；删除 `web/liquid/` 临时资源目录 | index.html / styles-20260817.css |
| #4 截图路径 | 输出路径用绝对路径 | 验证工作流 |
| #5 % 冲突 | `.replace('__TOKEN__', ...)` 占位注入 | 验证脚本 |
| #6 死测试 | `test_api_get_status_shape` 断言补 `passthrough_mode` + 值断言 | tests/test_gui_web.py |
| #7 按钮高亮 | click handler 在 `renderStats()` 前补 `syncStatsModeButtons()` | app.js |
| #8 光晕分块 | filter 参数重配：baseFrequency 0.014→0.04（波长 ~71px→~25px，宏观均匀）、scale 16→8（位移减半）、saturate 1.5→1.1（不再放大色差）。headless 无法复现 SVG filter，靠真实 WebView2 用户实测 | index.html |

---

## 6. 是否完全遵循规划路径开发

**部分偏离**。三条主线中：
- **A / D / E / F / G 完全按 §3 路径落地**（纯 CSS 或单点 JS/HTML 修改，无偏差）。
- **B / C 属研究型，无预先规划可遵循** —— 机制靠反推、方案经过多轮迭代
  （feImage base64 → file relative href → pattern 平铺 → feTurbulence），最终方案
  是「弃全部外部资源、纯程序化」，与初版设想（复用源 PNG）**重大调整**。

### 细节偏离（均为设计内取舍）

- **(a) 液态玻璃只作用于 `.glass-card:not(.top-model-spotlight)`**，spotlight 单独
  裸底 —— 用 `:not()` 排除而非改 HTML 类，保布局/内边距。（D + G 的交点）
- **(b) 全页面玻璃保留降级路径**：不支持 `backdrop-filter:url(#)` 时落回半透明
  渐变，视觉仍是玻璃，不破版。（G 撞 v0.41 教训的缓解）
- **(c) E/F 是用户点出来的计划外缺陷**，属于需求追加，不算偏离。
- **(d) SVG filter 区域扩到 130%** 防 feDisplacementMap 位移到 filter 边界时切边。

---

## 7. 最终实现点

### 前端（HTML + CSS + JS）

1. **`src/relay/web/index.html`**
   - body 顶部（bg-glow 之后）注入内联 SVG filter `#lg-glass`：
     `feTurbulence`（fractalNoise / baseFrequency 0.04 0.04 / numOctaves 2 / seed 7）
     → `feDisplacementMap`（scale 8，R/G 通道）→ `feColorMatrix`（saturate 1.1）；
     filter 区域 `x="-15%" y="-15%" width="130%" height="130%"`
     - **v0.112e**：初版 baseFrequency 0.014（波长 ~71px）/ scale 16 / saturate 1.5
     - **v0.112h**：改 0.04 / 8 / 1.1 —— 波长降到 ~25px 让位移宏观均匀细腻，
       位移减半、saturate 不再放大色差，消除「玻璃下光晕分块渐变」（见问题 8）
   - **删除**「数据显示」死 section：`settings-passthrough` 整节（
     `card-settings-passthrough-body` 无渲染函数填充，永远停在「加载中…」）
   - styles 引用 bump：`styles-20260817.css?v=20260822-06`

2. **`src/relay/web/styles-20260817.css`**
   - **v0.112c 卡片间距**：`.view[data-view="overview"] .grid` gap 10→6px；
     `.view[data-view="overview"] .grid .glass-card` padding 12→10px
   - **v0.112d/e→g 液态玻璃**：选择器从总览 7 卡扩为全局
     `.glass-card:not(.top-model-spotlight)` —— 覆盖总览/统计/live/history/上游/
     设置全部卡片：白渐变背景（160deg, 0.20/0.05/0.13）+ 白描边 0.28 +
     `backdrop-filter:url(#lg-glass)` + 外阴影 + inset 顶部高光
   - **v0.112g spotlight 裸底**：`.top-model-spotlight` 清 background/border/
     backdrop-filter/box-shadow，hover 不上浮，`::before` 内高光隐藏 —— 文字直接
     浮在页面底色上
   - **v0.112i 四页卡片去背景**：`.view[data-view="live|upstreams|history|stats"]
     .glass-card` 清 background/border/backdrop-filter/box-shadow + hover 不上浮 +
     `::before` 隐藏 —— 实时/上游/历史/统计四页悬空裸底（同 spotlight）。
     液态玻璃最终只保留**总览 + 设置**两页（#49 部分回退）

3. **`src/relay/web/app.js`**
   - `stats-mode-group` click handler 补 `syncStatsModeButtons()`（v0.112f）：
     切 mode 后先同步本页三档按钮 active 高亮，再 `renderStats()` 换内容
   - `syncStatsModeButtons()`（2114 行）：按 `statsState.mode` toggle 每个按钮的
     `.active`

4. **`src/relay/web/live_panel.html`** —— styles 引用 bump `?v=20260822-06`

### 测试

- `tests/test_gui_web.py`：`test_api_get_status_shape` 断言补 `passthrough_mode`
  （既有失效测试修复，非本次改动引入）
- `node --check app.js` 通过；CSS 花括号平衡校验（664/664）
- **headless Edge 验证**（getComputedStyle dump）：spotlight 计算样式 =
  `background=transparent / border=transparent / box-shadow=none /
  backdrop-filter=none / ::before display=none`（裸底确认）；
  普通卡片 = `background-image=linear-gradient(...) / backdrop-filter=url(#lg-glass)`
  / 白描边 + inset 高光（非 overview 视图也生效，全页玻璃确认）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/index.html` | 改（注入 #lg-glass filter + 删死 section + 版本号） |
| `src/relay/web/styles-20260817.css` | 改（卡片间距 + 全页液态玻璃 + spotlight 裸底 + 四页去背景） |
| `src/relay/web/app.js` | 改（stats 三档按钮补高亮同步） |
| `src/relay/web/live_panel.html` | 改（版本号） |
| `tests/test_gui_web.py` | 改（status 断言补 passthrough_mode） |
| `docs/dev/live_panel_icon_timeouts_ui_v0.111-112.md` | 改（版本号记录同步到 v0.112g） |
| `docs/dev/ui_liquid_glass_wrapup_v0.112c-g.md` | 新增（本文档） |

### 验证建议（用户手动）

- 总览页：6 卡间距明显收紧（gap 6px / padding 10px）
- 总览 + 设置页卡片呈液态玻璃（柔和折射 + 顶部高光）；实时/上游/历史/统计四页
  卡片悬空裸底（同 spotlight，无框无影）—— v0.112i 起
- 「用量最大的模型」整条直接浮在背景上，无卡片框、无阴影、无边框
- 统计页点「仅透传 / 全部 / 仅转换」→ 内容**和**按钮高亮一起切
- 设置页不再有「数据显示 加载中…」残留
- ⚠ 若实测 WebView2 掉帧（全页玻璃面积比 v0.41 下线时更大），说一声，把玻璃范围
  收缩回总览/统计两页
- v0.112h：玻璃下光晕应均匀晕染、无分块渐变感；若仍分块 → 继续降 scale / 提
  baseFrequency，若玻璃变得太没存在感 → scale 回调到 10-12

---

## 附：v0.112j 设置页小字说明精简（独立小批）

用户过了一遍设置页全部小字说明（约 38 处），对其中 10 处给出改写/删除决定，逐条落地：

| # | 项目 | 决定 | 落地 |
|---|---|---|---|
| 1 | auto 兜底模型 | 问「在哪」—— 未定改写 | 不改（见下方说明） |
| 2 | 思考流超时时间 | 改写 | `思考流已有内容但无新增，判定为中断的间隔时间` |
| 3 | 思考-正文衔接超时 | 改写 | `收到思考流停止信号，等待正文的超时时间` |
| 4 | 正文流超时时间 | 改写 | `正文流已有内容但无新增，判定为中断的间隔时间` |
| 5 | 实时流侧栏（外观） | 改写（用户微调） | `开启后右侧出现独立小窗，实时显示当前请求的思考过程和正文，方便调试` |
| 6 | 实时流侧栏（实时栏管理副本） | 删按钮 + 组按开关显隐 | 删 `prefs-live-panel-mgr` 副本开关；`#live-panel-mgmt-group` 默认 hidden，由 `syncLivePanelMgmtGroup()` 按「外观」组开关控制（初始化 / change / 顶栏 toggle 三处同步） |
| 7 | 最多显示的数量 | 改写 | `最多同时显示的并行数量` |
| 8 | 模型倍率 | 删说明 | 移除 cfg-hint |
| 9 | 5 小时额度 | 删说明 | 移除 cfg-hint |
| 10 | 快捷切换（subtitle） | 改写 | `选中的模型可在左下角快速切换为预设`（index.html） |
| 11 | 中继模式 | 保持不说明 | 不动 |

**改动文件**：`app.js`（6 处 hint + 删副本开关及 wiring + `syncLivePanelMgmtGroup` 辅助，`?v=20260822-06`）、`index.html`（quick-switch subtitle + 版本号）。

**#1 auto 兜底模型位置**：在「设置 → 上游配置」—— 点击某个上游名称那一行展开 `<details>`，里面第 3 个字段即「auto 兜底模型」。若当前是**透传模式**，整个「上游配置」区块被 `body.passthrough-mode` 规则隐藏（styles:3877），需切回转换模式才能看到。

**验证建议**：设置页关闭「实时流侧栏」→ 实时栏管理组整体消失；开启 → 出现；顶栏「实时流」按钮切换同样联动。三超时/并发等控件在组隐藏时不可见。

---

## 附：v0.112k 页面缩放 —— Ctrl + 加号/减号/0（纯前端）

用户要求「Ctrl+滚轮调页面缩放」。调研 + 实测的结论与最终方案：

- **根因**：pywebview 6.2.1（edgechromium.py:287）生产运行时把
  `AreBrowserAcceleratorKeysEnabled` 设为 `False`（debug 默认关）→ WebView2
  内置的 Ctrl+滚轮 / Ctrl+加号/减号 / Ctrl+0 缩放全部失效。
- **第一版尝试（后端）**：在 `_on_loaded` / `_on_panel_loaded` 里通过
  `win.native.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = True`
  重开 —— 实测无效。可能原因：pywebview 事件回调非 UI 线程，WinForms 控件
  跨线程访问被吞，或运行时设置不生效。**已回滚**（gui.py 恢复原样，无残留）。
- **最终方案（纯前端 app.js，v0.112k）**：WebView2 该开关**只禁内置动作、
  不拦按键事件**（事件仍到达页面），所以前端自己处理：
  - `Ctrl + +`（含 `=`，无 shift 的加号键）/ `Ctrl + -`（含 `_`）→ 步进 0.1
    调 `document.body.style.zoom`，范围 clamp 0.5–2.0；`Ctrl + 0` 复位 1.0。
  - 缩放值存 localStorage `page-zoom`，重启 GUI 保持。
  - `initPageZoom()` 在 DOMContentLoaded 里调用。
- **改动文件**：`app.js`（`?v=20260822-07`），`gui.py` 无改动。

**验证建议**：重启后主窗按 `Ctrl +` 放大、`Ctrl -` 缩小、`Ctrl 0` 复位；
缩放是整页 body.zoom，浮动背景光晕/窗口按钮随页面一起缩放（等同浏览器整页
缩放观感）。实时栏窗口未接（用户只要求主窗）。

---

## 附：v0.112l 统一所有 `<select>` 选择器风格

用户要求「把所有的选择器都改成符合整体风格的样式」。全部 7 个 select 分布在：
`#create-preset` / `#create-preset-model` / `#create-billing-unit`（新建上游 modal）、
`.cfg-billing-unit`（上游配置计费模式）、`.adv-*`（高级切换模型）、
`#qs-edit-target`（快捷切换编辑）、`.upstream-select`（sidebar 上游切换）。

**改动（styles-20260817.css）**：
- 新增 `--select-arrow` 主题变量（data-URI SVG chevron，颜色取各主题
  `--text-muted`，light/day/dark 各自覆盖，随主题切换变色）。
- 全局 `select` 规则：`appearance:none` 去掉原生外观/箭头 + 自绘箭头
  （background right 8px / size 12px / 玻璃底色 `--glass-surface-strong`）+ 统一
  边框/圆角/字体/`padding:6px 26px 6px 9px`（右 26px 给箭头留位）+ hover/focus。
- `select.cfg-input, select.upstream-select` 高 specificity 覆盖 —— 这两类的
  `background`/`padding` shorthand 会重置 background-image 吃箭头、把
  padding-right 压回 8-9px。

**验证**：headless Edge computed-style dump + 截图像素扫描，4 个代表 select
（cfg-input / cfg-billing-unit / upstream-select / 裸 select）全部 `appearance:none`
+ 箭头 data-URI + `#85858b` chevron 像素精确命中（133,133,139），背景统一 #fff。
选项列表下拉弹出层是 OS 渲染，无法定制，只统了框体本身。

**改动文件**：`styles-20260817.css`（`?v=20260822-07`，index.html + live_panel.html 同步）。

---

## 附：v0.113a 实时流侧栏卡片极窄边框 + 极窄边距

用户：「侧边实时流栏，所有卡片用极窄边框 + 极窄边距」。

**改动（live_panel.css，`?v=20260822-03`）**：
- `.live-panel-card`：`border: 1px → 0.5px solid var(--card-border)`；圆角
  `--radius-lg(16px) → --radius-md(8px)`；内边距 `8px/12px → 4px 8px`；卡内
  gap `4px → 3px`。
- `.live-panel-header`：同上（0.5px / radius-md / 4px 8px）。
- `.live-panel-main` 卡片间距 `4px → 3px`；`.live-panel-stream-wrap` 卡内
  gap `4px → 3px`（与 .live-panel-card 一致）。
- 网格区同步：`.grid-region`/`.grid-rows` 间距 `4px → 3px`。
- 内层内容框（pre/stream/tool/key）仍 1px hairline，不随卡片缩 —— 它们是
  卡片内容而非卡片本体。
- ⚠ 迭代：首版内边距 `2px 6px` 太紧（文字贴边框），用户反馈「文字距离边框太近
  了又显得很丑」，改为 `4px 8px`；用户又给精确约束「至少保持距离左边缘 1 个文字
  大小，上下 0.8 个文字大小」，最终用 em 相对单位：`padding: 0.8em 1em`
  （13px 字号 = 上下 10.4px / 左右 13px），跟随字号缩放不写死 px。

---

## 附：v0.113b 设置「无边框」开关（总览页裸底）

用户：「设置增加"无边框"，开启后总览页所有卡片去掉玻璃容器，裸悬浮在底背景上」。

**改动（app.js + styles-20260817.css，`?v=20260822-08`）**：
- `prefs` 新增 `noFrame: false`（localStorage 前端偏好，不落后端）。
- `applyPrefsClass()` 按 `prefs.noFrame` 切换 `body.overview-no-frame` class。
- 设置页「界面与偏好 → 外观」组新增「无边框」开关（`.prefs-no-frame`），
  `wireSettingsPrefs` 注册 change handler（仅 `prefs.noFrame = checked; savePrefs()`）。
- CSS：`body.overview-no-frame .view[data-view="overview"] .glass-card` 清掉
  背景/边框/阴影/backdrop-filter/hover 上浮/内高光（与 v0.112i 四页裸底同款）。
  只作用总览 6 卡；`.top-model-spotlight` 本就是裸底，无需再覆盖。

**验证**：`node --check app.js` 通过。刷新 GUI 后：设置页开「无边框」→ 总览页
6 卡立即裸底悬浮；关闭恢复液态玻璃。侧栏卡片肉眼确认 0.5px 细边框 + 紧凑间距。

---

## 附：v0.113c 设置「侧边栏无边框」开关（实时流侧栏裸底）

用户：「设置的无边框中添加'侧边栏无边框'」。语境承接 v0.113a/b —— 「侧边栏」
指右侧实时流侧栏（live panel）窗口；左侧导航栏 v0.43 起本就无框，不是它。

**关键架构点**：实时流侧栏是**独立 webview 窗口**（live_panel.html），不共享
主窗 `body.overview-no-frame` class 机制；prefs localStorage 跨窗口不可靠。
所以状态走**后端持久化 + 桥广播**（与主题广播同款通道 `_push_theme_to_panels`）：

- **config.py**：新增 `relay_gui_panel_frameless: bool = False`（.env 键
  `RELAY_GUI_PANEL_FRAMELESS`）。
- **gui.py**：新增 `get/set_live_panel_frameless()` 桥方法（持久化 + update_env_var
  + `_push_frameless_to_panels(enabled)`）；`_push_frameless_to_panels` =
  `panel_window.evaluate_js("setPanelFrameless(true/false)")`；`_on_panel_loaded`
  加载时同步一次当前值（侧栏每次打开都正确）。
- **app.js**：bridge 加 `get/setLivePanelFrameless`；「界面与偏好 → 外观」组
  「无边框」开关下方新增「侧边栏无边框」开关；`refreshPrefsDynamic` 拉后端初值。
- **live_panel.js**：暴露 `window.setPanelFrameless(on)` 切 `body.panel-no-frame`。
- **live_panel.css**：`body.panel-no-frame .live-panel-card/.live-panel-header`
  清背景/边框/阴影/内高光（裸底）；内层内容框（pre/stream/tool/key）保留
  hairline —— 它们是卡片内容，无边框下仍需可读分隔。

**验证**：`node --check` 两 JS + `ast.parse` 两 py 全过。刷新 GUI：设置页开
「侧边栏无边框」→ 实时流侧栏所有卡片立即裸底；侧栏重开仍保持（后端持久化）。
