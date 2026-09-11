# 图标 + 三超时设置 + 衔接独立检测 + UI 收尾（v0.111-0.112）开发文档

> 本批承接 v0.110（`live_panel_stale_timeout_v0.110.md`）之后的三段独立需求，
> 用户明确「做完另外几个修改再统一写开发文档」，故合为一份审计文档。

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 给应用程序的图标换一个高级点的。
>
> 你来做决定，我看效果。（拒绝图标多选方案的追问）
>
> 在设置里新增：思考流超时时间；思考-正文衔接超时时间；正文流超时时间；
>
> 先停下，现在实现到哪一步了？（打断验证，要求状态汇报）
>
> 不用，我们做完另外几个修改再统一写开发文档。1、侧栏侧栏的每一个容器长度设定，高最多是宽的1.5倍。剩下的空间如果没有新请求占用掉，就裸露背景。
> 2、配置页面不要用背景块突出文字，直接让悬浮在底背景上就行。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 应用图标升级（托盘 + 窗口/任务栏），设计我拍板并直接出效果 | 指令 1 + 指令 2 |
| B | 设置页新增「思考流超时时间」（thinking 档，默认 60s） | 指令 3 |
| C | 设置页新增「思考-正文衔接超时时间」（gap 档，默认 20s） | 指令 3 |
| D | 设置页新增「正文流超时时间」（text 档，默认 10s） | 指令 3 |
| E | 实时栏每个容器高度上限 = 宽的 1.5 倍，剩余空间裸露背景 | 指令 5-1 |
| F | 配置页去掉背景块，文字直接悬浮在底背景上 | 指令 5-2 |

### 隐含但需自行决策的点

- **B/C/D 与 v0.110 的承接**：v0.110 的三档阈值（thinking 60 / gap 20 / text 10）是写死在 `panel_pool.py` 的常量，本次把它们变成用户可配置项（settings + bridge + snapshot + 设置页 UI）。
- **C 的拦路石**：v0.110 §4 问题 1 已证实**衔接期无法独立判定**（中继 delta 没有「思考块已结束」信号，衔接静默与思考暂停是同一观测），所以 v0.110 把 gap 并入了 thinking 档。用户现在点名要「思考-正文衔接超时时间」→ 必须先把 **thinking-done 信号**补出来，否则 20s 档形同虚设。
- **E 的范围**：侧栏主栏 + 网格区两块都算「容器」，都要封顶；裸背景 = 块高度按内容自适应，不再均分拉伸填满。
- **F 的范围**：只去**正文说明段**的背景块；代码块 / 可点击上游行保留浅底（它们是工具/交互元素，不是正文）。

---

## 3. 分析需求后得出的开发路径

按依赖顺序拆成四条主线：

```
主线一  图标（指令 A）—— 独立，先行
  确认 pywebview 6.2.1 create_window 是否有 icon 参数（无）
  → 确定方案：托盘用 pystray，窗口/任务栏用 Win32（LoadImageW + WM_SETICON）
  → 新建共享 icon.py（一份 gauge 设计服务托盘 + 窗口两处），tray.py 委托之

主线二  thinking-done 信号（C 的前置）—— 先于设置落地
  parser 层给「思考结束」一个权威信号：
    think_split.py    `</think>` 闭合时置 _thinking_closed
    anthropic.py      思考块 content_block_stop 时置 _thinking_finished
    openai.py         reasoning_content 流无结束标记 → 只有 </think> 信号
  proxy 层沿 SSE 广播：
    _InFlight.thinking_done 字段
    delta 广播时带 thinking_done = parser.thinking_finished
  pool 层消费：
    _note_stage：thinking_done → 阶段置 "gap"（四档：wait/thinking/gap/text）

主线三  三超时设置（B/C/D）—— 依赖主线二
  config.py   三字段（relay_live_panel_*_timeout，.env key）
  panel_pool  构造注入 stale_*_secs，_stage_threshold 补 gap 档
  gui.py      桥 get/set ×3 + _apply_live_panel_timeouts（写回 pool 阈值）
              + snapshot 三字段
  app.js      设置页「实时栏管理」组加 3 个数字输入
              （init 读 snapshot，change → clamp 1-600 → 写回）

主线四  UI 收尾（E/F）—— 纯 CSS，最后做
  live_panel.css      主栏正文流 / 网格块高度上限 600px（= 宽 400 × 1.5）
  styles-20260817.css 配置页 .cfg-guide-block 透明化
  版本号 query 递增（app.js ?v=20260822-04 / styles / live_panel.css）
```

### 开发顺序落地

```
#1 icon.py + tray.py 改造 → 托盘/窗口图标换新
#2 think_split / anthropic / openai parser 思考结束信号
#3 proxy 广播 thinking_done
#4 panel_pool 四档状态机 + gap 档阈值
#5 config + gui 桥 + snapshot 三超时
#6 app.js 设置页 UI + 绑定
#7 live_panel.css + styles CSS 收尾
#8 node --check / 相关测试 / 统一文档
```

---

## 4. 实现中遇到的问题

### 问题 1：衔接期独立检测 —— v0.110 遗留的能力缺口，本次必须补

v0.110 把 gap 并进 thinking 档就是因为它不可判定。用户点名要 20s 衔接超时后，
这个缺口不能再用「并入 thinking」回避。查证中继数据链路（`proxy.py` SSE 广播 +
`panel_pool.push_event`）后发现：delta 携带的只有累积文本，**没有**「思考块已结束」
这一事件；而衔接的起点正是「思考结束」这个时刻。

**信号源盘点**（谁能告诉我思考结束了）：
- Anthropic 协议：思考块有 `content_block_stop`（块级结束）**且**正文出现前必有
  `</think>` 文本标记 —— 两个信号源，都可探。
- OpenAI 协议：`reasoning_content` 流**没有块级结束标记**，只有模型转正文时
  `</think>`（DeepSeek 风格）才闭合 —— 信号源只剩 `</think>`。

### 问题 2：pywebview 6 不支持窗口图标参数

`create_window()` 没有 `icon` 参数（pywebview 6.2.1）。查证可行路径：Win32 原生
`LoadImageW` 加载 `.ico` → 取 HWND → 发 `WM_SETICON`（ICON_SMALL + ICON_BIG）。
任务栏/窗口图标走这条；托盘图标走 pystray + PIL。两处要**同一份设计**，否则视觉割裂。

### 问题 3：图标设计方案被用户拒绝走选择流程

我本想用 AskUserQuestion 让用户从几个方案里挑，被拒绝（「你来做决定，我看效果」）。
后续设计由我拍板：**深色圆角渐变底 + 琥珀色用量弧环（~68% 留缺口）+ 白色粗体 T**，
amber 取 GUI 深色主题 `--platform-anthropic` / `--accent` 品牌色，T = Token。

### 问题 4：`SyntaxError: Identifier 'lastConfigSig' has already been declared`

配置页指南代码复制了既有 settings-config 编辑器的 sig 变量名，在 app.js 里重复声明。
重命名为 `lastCfgGuideSig`（声明 + 2 处引用同步）。

### 问题 5：`test_api_get_status_shape` 失败 —— 既有失效测试，非本次改动

跑验证时 `tests/test_gui_web.py::test_api_get_status_shape` 报断言失败，额外 key 是
`passthrough_mode`。追查：透传模式功能（`project_passthrough`）给 `get_status()` 加了
字段但**没同步更新这个精确 key 集合断言**，是既有死测试。与本次三超时 / 图标改动无关
（我改的是 snapshot 和 PanelPool 构造，没动 get_status）。

### 问题 6：组合 pytest 挂起

整模块跑 pytest 偶发挂起（既有基础设施噪音，见 memory 记录「组合 pytest 挂起/栈溢出，
单独跑全绿就记录不追」）。单测逐个跑全绿后确认非本次改动引入，按记录不追。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 位置 |
|---|---|---|
| #1 衔接不可判定 | **新增 thinking-done 信号链路**：`</think>` 闭合（think_split.`_thinking_closed`）+ Anthropic 思考块 `content_block_stop`（anthropic.`_thinking_finished`）→ parser.`thinking_finished` → proxy `_InFlight.thinking_done` → SSE delta 广播携带 → pool `_note_stage` 置 "gap" 档。OpenAI 无块级信号，`thinking_finished` 只回 `_think_split.thinking_closed`，无信号则回退 thinking 档 | think_split.py / anthropic.py / openai.py / proxy.py / panel_pool.py |
| #2 pywebview 无 icon 参数 | 共享 `icon.py`：`build_app_icon(size)`（4× 超采样合成）+ `build_tray_image()`（64px 托盘）+ `write_ico(path)`（16-256 多尺寸 .ico）。托盘 pystray 委托 `icon.build_tray_image`；窗口/任务栏 Win32 `LoadImageW` + `WM_SETICON` + HICON 缓存 | icon.py（新增）/ tray.py |
| #3 设计拍板 | gauge 设计直接实现并出效果，不走选择流程 | icon.py |
| #4 JS 重名 | sig 变量重命名 `lastConfigSig` → `lastCfgGuideSig` | app.js |
| #5 死测试 | `test_api_get_status_shape` 断言补 `passthrough_mode` + 值断言 | tests/test_gui_web.py |
| #6 pytest 挂起 | 相关测试逐个跑绿即记录，不追基础设施 | — |

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大调整）**。主线一到四均按 §3 路径落地，C 的拦路石（gap 独立
检测）通过新增 thinking-done 信号在计划内解决 —— v0.110 记录在案的能力缺口，
本次真正补齐，而非又一次收敛。

### 细节偏离（均为设计内取舍）

- **(a) gap 信号源按协议分叉**：Anthropic 双信号源（块 stop + `</think>`），OpenAI
  单信号源（仅 `</think>`）。OpenAI 无信号时 gap 档回退 thinking 档 —— 这是协议能力
  差异，非疏漏（§4 问题 1）。
- **(b) wait 档（90s）不在设置内**：用户只要三档（thinking/gap/text），思考等待档
  保持写死 90s 不暴露。
- **(c) 阈值下限 clamp 1s**：`max(1.0, float(x))`，防止设置成 0/负值直接让 watchdog
  误杀。
- **(d) 三超时设置写回的是运行中的 pool 阈值**（`_apply_live_panel_timeouts` 直接改
  `pool._stale_*_secs`），不要求重启 GUI 生效 —— 与「改代码让用户自己点 GUI 重启」
  的惯例一致，这是运行时参数不是代码改动。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/icon.py`（新增）** —— 共享图标模块
   - gauge 设计：深色圆角渐变底（顶 50,50,58 → 底 15,15,21）+ 琥珀 245,158,11
     弧环 ~68% 留缺口 + 白色粗体 T
   - `build_app_icon(size)`（4× 超采样）/ `build_tray_image()`（64px）/
     `write_ico(path)`（16-256 多尺寸 .ico）
   - 托盘（pystray）与窗口/任务栏（Win32）共用一份设计

2. **`src/relay/tray.py`** —— 托盘图标委托 `icon.build_tray_image()`，移除原 PIL 直绘

3. **思考结束信号（gap 独立检测的基础）**
   - `think_split.py`：新增 `_thinking_closed`（`</think>` 闭合时置位）+ `thinking_closed` property
   - `anthropic.py`：新增 `_thinking_block_index` / `_thinking_finished`；
     `content_block_start` 记录思考块 index，`content_block_stop` 匹配时置位；
     `thinking_finished = self._thinking_finished or self._think_split.thinking_closed`
   - `openai.py`：`thinking_finished = self._think_split.thinking_closed`（仅 `</think>` 信号）
   - `proxy.py`：`_InFlight.thinking_done` 字段；delta 广播携带
     `thinking_done = bool(getattr(parser, "thinking_finished", False))`；
     `_broadcast_live_event` 对 delta/done `setdefault("thinking_done", ...)`

4. **`src/relay/panel_pool.py`** —— 四档阶段状态机 + 三超时可注入
   - 构造参数 `stale_wait_secs=90.0 / stale_thinking_secs=60.0 / stale_gap_secs=20.0 /
     stale_text_secs=10.0`，`max(1.0, float(...))` 存储
   - `_stage[rid]` ∈ "" / wait / thinking / gap / text；`_note_stage`：
     `assistant_text`→text，`thinking_done`→gap，`thinking_text`→thinking，
     否则 setdefault wait；单向不回退
   - `_stage_threshold` 补 "gap" 分支返回 `_stale_gap_secs`

5. **`src/relay/config.py`** —— 三超时设置字段（.env key）
   - `relay_live_panel_thinking_timeout: float = 60.0`（`RELAY_LIVE_PANEL_THINKING_TIMEOUT`）
   - `relay_live_panel_gap_timeout: float = 20.0`（`RELAY_LIVE_PANEL_GAP_TIMEOUT`）
   - `relay_live_panel_text_timeout: float = 10.0`（`RELAY_LIVE_PANEL_TEXT_TIMEOUT`）

6. **`src/relay/gui.py`** —— 桥 + snapshot
   - PanelPool 构造注入三超时
   - snapshot 新增 `live_panel_thinking_timeout / gap_timeout / text_timeout` 三字段
   - `_live_panel_timeout(attr, default)` helper、`_apply_live_panel_timeouts()`（实时改
     pool 阈值，不重启生效）、6 个 get/set 桥方法

### 前端（JS + CSS + HTML）

7. **`src/relay/web/app.js`**
   - 6 个 api bridge 方法（`get/setLivePanel{Thinking,Gap,Text}Timeout`）
   - 设置页「实时栏管理」组新增 3 个数字输入（`prefs-live-panel-thinking-input` /
     `gap-input` / `text-input`，中文 hint）
   - `bindTimeoutInput(sel, setter, def)`：change → clamp 1-600 → 写回
   - `initTimeoutInput`：snapshot 字段优先，bridge 兜底
   - 修复 `lastConfigSig` 重复声明 → `lastCfgGuideSig`

8. **`src/relay/web/live_panel.css`**（v0.112）
   - `.live-panel-stream-wrap`：`flex: 0 1 auto; max-height: 600px; min-height: 140px`
     （不再拉伸填满窗口，高度按内容自适应封顶）
   - `.grid-rows`：`overflow-y: auto`（块超高时区内滚动）
   - `.grid-block`：`flex: 0 1 auto; max-height: 600px`（不再均分压缩）

9. **`src/relay/web/styles-20260817.css`**（v0.112 + v0.112b）
   - `.cfg-guide-block`：transparent / 无 border / 无圆角 / padding 0（正文段去背景块）
   - `.cfg-banner`：去 border-left，transparent
   - `.cfg-guide`：gap 16→20px
   - **v0.112b**：新增 `.cfg-mode-bar`（当前模式指示条 + 蓝/琥珀 chip）、`.cfg-mode-title`
     与 `.cfg-mode-tag`（模式块大标题）、`.cfg-steps`（配置步骤编号行）、`.cfg-adv`
     + `.cfg-adv-summary`（高级折叠区，仅虚线分隔、无背景块）

10. **`src/relay/web/app.js`**（v0.112b）—— 配置页重构为「两种模式前置」
    - 顶部：接入地址 banner（两模式共用同一中继地址，仅 api-key 填法不同）
    - 模式条：实时显示当前模式（转换模式 / 完全透传）
    - **转换模式**块：是什么（路由+协议转换）+ 怎么配置（两步）+ Claude Code / Codex 示例
    - **完全透传模式**块：是什么（按 `目标地址@@上游key` 原样直达）+ 怎么配置（两步）+ curl 示例
    - `<details class="cfg-adv">` 高级折叠区收尾：当前 active 上游 / 快捷切换 / 已配置上游
      （复杂内容折叠，默认收起）

11. **版本号**：`index.html` app.js `?v=20260822-05`、styles `?v=20260822-05`（v0.112g）；
    `live_panel.html` live_panel.css `?v=20260822-01`

12. **v0.112f/g 四项 UI 收尾** —— 独立成档，见
    `ui_liquid_glass_wrapup_v0.112c-g.md`（本批含总览卡片间距 + 液态玻璃 +
    #46 spotlight 裸底 / #47 统计按钮高亮 / #48 删死区 / #49 全页玻璃）。

### 测试

- `tests/test_gui_web.py`：`test_api_get_status_shape` 断言补 `passthrough_mode`
  （既有失效测试修复）；status/snapshot 相关测试 4 个全绿
- `smoke_worker.py`：`test_stage_threshold` 补 gap 档测试（thinking_done → gap 20s）
  与无信号回退测试
- `node --check`：app.js / live_panel.js / live_panel_grid.js 全通过

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/icon.py` | 新增（共享图标模块） |
| `src/relay/tray.py` | 改（委托 icon 模块） |
| `src/relay/parsers/think_split.py` | 改（`</think>` 闭合信号） |
| `src/relay/parsers/anthropic.py` | 改（思考块 stop 信号） |
| `src/relay/parsers/openai.py` | 改（仅 `</think>` 信号） |
| `src/relay/proxy.py` | 改（thinking_done 广播） |
| `src/relay/panel_pool.py` | 改（四档状态机 + 三超时注入） |
| `src/relay/config.py` | 改（三超时设置字段） |
| `src/relay/gui.py` | 改（桥 + snapshot + 写回 pool） |
| `src/relay/web/app.js` | 改（bridge + 设置 UI + 绑定） |
| `src/relay/web/live_panel.css` | 改（容器高度上限） |
| `src/relay/web/styles-20260817.css` | 改（配置页去背景块） |
| `src/relay/web/index.html` / `live_panel.html` | 改（版本号） |
| `tests/test_gui_web.py` | 改（status 断言补 passthrough_mode） |
| `smoke_worker.py` | 改（gap 档测试） |
| `docs/dev/live_panel_icon_timeouts_ui_v0.111-112.md` | 新增（本文档） |
| `docs/dev/ui_liquid_glass_wrapup_v0.112c-g.md` | 新增（液态玻璃 + UI 收尾独立文档） |

### 验证建议（用户手动）

- 托盘图标 = 深色底琥珀弧环 T；窗口/任务栏图标同款（重启 GUI 后可见）
- 设置页「实时栏管理」→ 改三超时值 → 保存后**不重启**直接生效（pool 阈值被桥写回）
- 真实请求观察 gap 档：思考结束（`</think>`）后正文迟迟不来 → 约 20s 翻「出错」
  （对比 v0.110 的 60s）
- 侧栏容器超高时内部滚动、不足时裸露背景；配置页正文段无背景块、代码块仍有浅底
