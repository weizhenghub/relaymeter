# 侧栏实时显示专题汇总（v0.113k + l + m + x）开发文档

> 把侧栏「实时跳动」相关的工作集中回顾：滚轮选择器（v0.113k 自绘 / v0.113l
> 折叠 / v0.113m 悬停展开）+ 侧栏 token 四字段实时更新（v0.113x output 估算）。
> 各自独立 dev doc 见 `ui_timeout_wheel_picker_v0.113k.md` /
> `ui_wheel_picker_collapse_v0.113lm.md` / `ui_sidebar_realtime_token_v0.113x.md`；
> 本文件讲三者协同 + 用户感知链路。

---

## 1. 用户的初始指令汇总

按时间顺序原文（一字未改）：

> 三档超时改为自绘滚轮选择器（wheelPicker），替代原生 number input

> 滚轮选择器不要一直展开，单格更紧凑；鼠标悬停再展开

> 侧边栏token统计改为增长帧统计模式，在出现流字符时就开始计量输入输出并实时显示更新数据，而不是结束后跃变到结果数值

三轮指令对应三件事：v0.113k（自绘）/ v0.113l+m（折叠展开）/ v0.113x（实时
token）。本汇总把它们放到「侧栏实时」这一主题下审视共性。

---

## 2. 跨版本共性提炼

| # | 共性点 | 三个版本的体现 |
|---|---|---|
| A | **自绘控件** —— 不依赖原生 number input / DOM widget | wheelPicker 自绘三条值 + band；token 字段也跳过中间件直接 setText |
| B | **逐事件驱动** —— 不节流、不合并，UI 跟着 SSE 帧跳 | wheelPicker mount 一次后续被 SSE 实时更新；token 字段每条 delta 一次 applyUsage |
| C | **持久化与显示分离** —— UI 是视图，权威值在后端 | wheel 改值 350ms 防抖写 setLivePanel*Timeout；token est 不入 db.record |
| D | **首帧对齐** —— 进入设置页立即看到上次退出态，不等异步 | wireSettingsPrefs 用 snap 三档超时初值；applyUsage 读 snap 真值 |

### 隐含但需自行决策的点

- **不引入新持久化层**：不引入 IndexedDB / sessionStorage / electron-store。
  现状是「前端 localStorage（仅 UI 偏好）+ 后端 .env / upstreams.json（开关 /
  数值 / 选择）」分层，已能 cover。v0.113k wheelPicker 三档值走 .env
  `relay_live_panel_{thinking,gap,text}_timeout`；v0.113x token est 不持久化
  （done 后被真值覆盖）。
- **不污染计费**：v0.113x 严格只用新增字段 `output_tokens_est`，不动
  `last_usage` / `parser.usage` / `db.record` / `UsageAcc`。done 路径不带 est
  字段，前端 max 真值时拿到真值校准。
- **不引入节流**：estimate 输出节奏 = SSE 事件节奏。browser 渲染帧率（~60fps）
  自然节流；不再额外 setTimeout/requestAnimationFrame，否则会丢更新或闪烁。
- **共用 SSE 链路**：三个特性的数据来源都是 `_broadcast_live_event` →
  `live/stream` SSE → GUI `_sse_thread` → `panel_window.evaluate_js` →
  `window.relayLiveEvent(ev)` → 各自 handler。链路一致，无分叉。

---

## 3. 端到端「侧栏实时跳动」链路

```
中继进程 (proxy.py)
  ├── Anthropic adapter SSE / cross-wire / direct 三条流式路径
  │   每条解析出 parser.assembled_text() / assembled_thinking() / parser.usage
  │   构造 usage_now = {input, output, output_tokens_est=max(真值, 估算), cr, cc}
  │   _update_inflight(usage_live=usage_now)
  │   _broadcast_live_event("delta", usage_live=usage_now)
  │
  └── done 路径（同三处）
      parser.finalize() / last_usage 真值
      _broadcast_live_event("done", usage_live=真值四字段，无 est)

GUI 进程 (gui.py)
  _sse_thread (daemon, 连 8088/live/stream)
  每帧 JSON.loads → panel_window.evaluate_js("relayLiveEvent(payload)")

侧栏 JS (live_panel.js)
  relayLiveEvent(ev) 单入口
  ├── type="delta" → applyStreamText + applyThinking + applyUsage
  ├── type="done" → applyUsage(真值) + applyTools
  └── applyUsage(u)
      读 output_tokens / output_tokens_est → max → 写 $tokOut

设置页 wheelPicker (app.js)
  mountTimeoutWheel(sel, setter, snap.xxx_timeout ?? 默认)
  ├── wheelPicker(host, setter, def) mount（构造 band/rows/unit）
  ├── render() 写 aria-valuenow + dataset.raw
  ├── hover enter → open() 加 .wp-open（CSS 32→76px 展开）
  ├── hover leave 150ms → closeAll() 收缩
  ├── click 上半/下半 / wheel / 拖动 → set(±1) → 350ms 防抖写后端
  └── setLivePanel*Timeout → api → bridge → cfg 字段更新 → tick 拿到新 snap → 下次 mount 用真值
```

---

## 4. 三者协同的边界与冲突

### 4.1 wheelPicker 与 token 实时显示互不干扰

wheelPicker 在**设置页**（settings view → 实时栏管理组），token 在**侧栏
live panel**（独立窗口）。两者 DOM 完全分离、JS 模块分离（`app.js` vs
`live_panel.js`）、数据源分离（`api.snapshot()` 后端 pull vs `relayLiveEvent`
SSE push）。

唯一共同点：都消费 `snapshot.live_panel_*` 字段。wheel 用 `live_panel_thinking_timeout`
等三档；token 用 `live_panel_frameless` / `live_panel` 等开关。**没有共享状态
变量**，互不阻塞。

### 4.2 wheelPicker 折叠态与 token 跳动的渲染帧率

wheelPicker 折叠态高度 32px（CSS transition .15s），展开 76px；hover enter/leave
触发 CSS class 切换 + JS open()/closeAll()，无重渲染开销。

token 字段每条 SSE delta 都 setText（DOM textContent 赋值），现代浏览器对此
优化良好，~60fps 下肉眼流畅。

二者都用 CSS / DOM 级别操作，无 layout thrashing；不触发 reflow 重排。

### 4.3 「持久化链路」分工

- **wheelPicker 三档值**：用户改值 → 防抖 350ms → `setLivePanel*Timeout` →
  bridge → `cfg.relay_live_panel_*_timeout` → `update_env_var` 写 .env → 重启
  GUI 后 `apply_to_settings` 读回 → snapshot 真值 → 下次 mount 时显隐正确。
- **token 四字段**：协议给的真值（message_delta / OpenAI parser.usage） +
  v0.113x 估算字段。done 时真值覆盖估算；db.record 落库仅真值；侧栏显示用
  max。**无显式持久化逻辑** —— 估算字段不入 db，不写 .env，不写 upstreams.json。

### 4.4 「首帧对齐」协同

`tick()` 500ms 一次拉 snapshot → `renderActiveView` 设置分支 → `renderSettingsPrefs`
模板 + `wireSettingsPrefs` 挂载 wheelPicker（三档用 snap 初值）→ `refreshPrefsDynamic`
异步拉 bridge 真值覆盖 → wheelPicker 的 `initTimeoutWheel` 优先用 snap，回退
bridge。

侧栏 live panel 独立：SSE `snapshot` 首连推一条 → `relayLiveEvent(snapshot)` →
`applyUsage` 读 `usage_live`（含 output_tokens_est） → 首帧就有估算值显示。

**两者都用 snap 作 source of truth，bridge 异步校准**。这是 v0.113u 的统一模式。

---

## 5. 用户感知

| 场景 | 用户看到 | 背后 |
|---|---|---|
| 切到设置页 | 三档超时显示上次数值（90 / 45 / 15） | v0.113u snap 真值 + v0.113k wheelPicker 初始化 |
| hover 思考流超时 | 32px 单格 → 150ms 内展开 76px 三行 | v0.113l 折叠态 + v0.113m 悬停展开 |
| 滚轮 / 点击 / 拖动 | 数字 ±1 实时跳动 | v0.113k 交互 |
| 停止操作 350ms | 值写后端；下次 mount 用真值 | 防抖 → bridge → .env |
| 触发一段 Anthropic 流式请求 | 侧栏 input 立即跳到真值；output 从 0 随字符逐跳 | v0.113x output_tokens_est + max 显示 |
| 流结束 (message_delta) | output 跳到真值（通常比估算大） | max(真值, est) = 真值 |
| 流结束 (done) | output 不再变 | done 路径不带 est 字段，前端取 max(真值, 0) = 真值 |
| 重启 GUI | 三档 + 侧栏 last inflight 全部恢复 | snapshot 持久化 + inflight 内存 |

---

## 6. 是否完全按规划

**按规划落地**，三个版本相互独立、无冲突：

- v0.113k 自绘 wheelPicker 单文件改动（app.js + styles）。
- v0.113l/m 折叠展开纯 CSS + 三个监听，零 DOM 结构变化。
- v0.113x output_tokens 估算三处 broadcast 点加字段 + 前端 max 显示。
- **三处均不动计费路径**（last_usage / parser.usage / db.record / UsageAcc）。
- 三处均不动 snapshot 现有 schema（`inflight.usage_live` 多一个 est 字段，dict 透传）。
- 端到端测试：触发 Anthropic 流式 + 切设置页改超时 + 重启 GUI 三个动作串行跑通。

---

## 7. 最终实现点（汇总）

### 改动文件清单

| 文件 | v0.113k | v0.113l+m | v0.113x |
|---|---|---|---|
| `src/relay/web/app.js` | wheelPicker 自绘 + mountTimeoutWheel | + mouseenter/leave 监听 + isOpen() return guards | — |
| `src/relay/web/styles-20260817.css` | .wheel-picker 基础样式 | + 32↔76px transition + 显隐控制 | — |
| `src/relay/web/live_panel.js` | — | — | applyUsage 读 output_tokens_est max |
| `src/relay/proxy.py` | — | — | _estimate_output_tokens + 三处 broadcast 加 est 字段 |
| 文档 | ui_timeout_wheel_picker_v0.113k.md | ui_wheel_picker_collapse_v0.113lm.md | ui_sidebar_realtime_token_v0.113x.md + 本汇总 |

### 验证汇总

- `python -m py_compile` gui.py / proxy.py 通过。
- `node --check` app.js / live_panel.js 通过。
- estimator 行为测试：英文 11 / 中文 12 / 混合 16 / 代码 10（usage-stats env）。
- wheel picker 折叠展开：32px ↔ 76px 平滑过渡，hover 来回不闪（150ms delay）。
- 端到端：设置页改三档 → 重启 GUI → 保持；侧栏触流 → output 实时跳 → 结束真值；切 wheelPicker hover 改值 → 350ms 后写后端 → tick 同步。
