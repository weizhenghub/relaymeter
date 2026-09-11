# 2026-08-22 会话合并开发文档：UI 微调 + 设置持久化 + 侧栏实时

> 本次会话做了四件事，按用户提的时间顺序：
> 1. **EN 版统计工具栏按钮紧凑化** ——「Last 24h → 24h」「by upstream → Upstream」挤到一行
> 2. **统计页按钮强制单行** —— 三档 / 两档按钮组必须同一行，内部文字不换行
> 3. **所有设置写入本地磁盘** —— UI 偏好持久化到 .env / upstreams.json / localStorage，刷新 / 重启 GUI 保持
> 4. **侧栏 token 改实时增长帧模式** —— Anthropic SSE 协议只在 message_delta 给 output_tokens，本轮用字符长度粗估补齐
>
> 各自独立 doc 见 `ui_sidebar_realtime_token_v0.113x.md` / `ui_settings_persistence_v0.113u.md` /
> `ui_wheel_picker_collapse_v0.113lm.md` / `ui_sidebar_realtime_token_v0.113x_summary.md`；
> 本文件讲会话脉络、跨任务共性、回归风险。

---

## 1. 用户的所有指令（按时间顺序）

> en版本的信息切换按钮不用Last 24h，直接24h就好，也不用by upstreaming，去掉by。这样能尽量挤在一行。

> 依然有按钮由于文字太长变成了两行：Passthrough mode，7day 24day，等。按钮无论是3极还是2极必须三个选项在同一行，内部文字必须在同一行。允许修改按钮本体长宽高

> 所有设置写入本地磁盘，下次启动时加载表，确保和上次退出时一样

> 全部 UI 设置都要持久化

> 侧边栏token统计改为增长帧统计模式，在出现流字符时就开始计量输入输出并实时显示更新数据，而不是结束后跃变到结果数值。请你判断此功能实现是否需要涉及大量修改？

> 检查吧

> 继续做吧

> 写开发文档

（最后一条「写开发文档」即为本文件 + 三份独立 dev doc + 一份汇总 doc。）

---

## 2. 四件事的提炼与共性

| # | 实现点 | 来源 | 文件 |
|---|---|---|---|
| A | EN i18n 字典加 24h / Upstream / Model 短词 | 指令 1 | app.js I18N.en |
| B | renderStats hint 文案简化 | 指令 1 | app.js renderStats |
| C | .stats-toolbar / .stats-* 按钮 CSS 加 flex-wrap:nowrap + white-space:nowrap | 指令 2 | styles-20260817.css |
| D | gui.py `_rebuild_snapshot` 补 9 字段（live_panel_frameless / start_hidden / save_messages / autoswitch_*3 / error_analysis_*3 / autostart_enabled） | 指令 3+4 | gui.py |
| E | app.js `renderSettingsPrefs` 模板 18 checkbox + 1 number 改读 snap | 指令 3+4 | app.js |
| F | app.js `wireSettingsPrefs(body)` → `(body, snap)`；mountTimeoutWheel 用 snap 初值 | 指令 3+4 | app.js |
| G | app.js `statsState` init 从 localStorage 读 range/dim；wireStatsToolbar 写回 | 指令 3+4 | app.js |
| H | proxy.py 加 `_estimate_output_tokens` + 三处 streaming 广播加 `output_tokens_est` 字段 | 指令 5 | proxy.py |
| I | live_panel.js `applyUsage` 读 est 字段 max 显示 | 指令 5 | live_panel.js |

### 隐含但需自行决策的点

- **不动计费 / 进程边界**：H 严格只用新字段 `output_tokens_est`，不动 `last_usage` /
  `parser.usage` / `db.record` / `UsageAcc`；D 不杀 8088（遵守「严禁 kill 8088」记忆规则）。
- **持久化优先级**：后端开关以 .env / upstreams.json 为准，前端 localStorage 是 cache；
  snap 是桥，refreshPrefsDynamic 是异步校准——这套分工不变。
- **EN 翻译「只加」**：与 v0.113n「语言只加选项 + i18n 钩子」一致——本次会话
  只改最常用的几个 toolbar 词，不全量翻译。
- **按钮尺寸**：用户明确允许改长宽高，所以 CSS 改 padding / gap 是被授权的。
- **monotonic 约束**：估算字段必须 `max(真值, 估算)`，流式期间不倒退。
- **跨任务无耦合**：A/B/C 是纯 UI 文案 + CSS；D-G 是设置持久化；H-I 是侧栏
  token。三者改的文件 / 函数基本不重叠，无顺序依赖。

---

## 3. 分析需求后得出的开发路径

```
会话 #1 — EN 按钮紧凑化
  app.js I18N.en 加短词
  app.js renderStats hint 简化
  styles-20260817.css .stats-toolbar 系列改 padding + nowrap

会话 #2 — 按钮强制单行
  styles-20260817.css .stats-toolbar-range / -dim-group / -mode-group
                   + flex-wrap:nowrap + white-space:nowrap

会话 #3 — 所有设置持久化
  gui.py  _rebuild_snapshot 补字段（Settings relay_* 前缀真名 + getattr）
  app.js  renderSettingsPrefs 模板 18 checkbox + 1 number 改读 snap
  app.js  wireSettingsPrefs(body, snap) — 修 snap undefined ReferenceError
  app.js  statsState init + wireStatsToolbar 写 localStorage

会话 #4 — 侧栏实时 token
  proxy.py  + _estimate_output_tokens（CJK 1.5 / 其他 4 字符/token）
  proxy.py  三处 broadcast usage_now 增 output_tokens_est 字段
  live_panel.js applyUsage 读 est 字段 max 显示

验证
  py_compile / node --check
  headless stub-bridge 探针 _probe_persistence.py（18 checkbox + 3 wheel + max + stats）
  estimator 行为断言（usage-stats env）
```

### 开发顺序落地

```
#1 EN 按钮 i18n（会话 #1）
#2 CSS 按钮单行（会话 #2）
#3 gui.py snapshot 字段补齐（会话 #3）
#4 app.js renderSettingsPrefs 模板首帧读 snap（会话 #3）
#5 app.js wireSettingsPrefs 接 snap → 修 ReferenceError（会话 #3 探针 bug）
#6 app.js statsState localStorage（会话 #3）
#7 proxy.py _estimate_output_tokens + 三处 broadcast（会话 #4）
#8 live_panel.js applyUsage 读 est max（会话 #4）
#9 探针 + estimator 单测
#10 文档（独立 doc + 本汇总 doc）
```

---

## 4. 问题（含跨任务踩坑）

### 4.1 EN i18n 缺短词（会话 #1）

用户原话「Last 24h → 24h」「by upstream → Upstream」—— I18N.en dict 里没有短
映射，hint 文案直接拼 `rangeZh · 按dimZh`，英文环境就显示「Last 24h · by Upstream」，
与短词需求不符。

**改法**：I18N.en 加映射 `"近 24h": "24h" / "按上游": "Upstream" / "按模型": "Model"`；
renderStats hint 按 lang 分支用 en dict 替换：`${I18N.en[rangeZh] || rangeZh}
${I18N.en[dimZh] || dimZh}${t(modeLabel)}`。

### 4.2 按钮换行（会话 #2）

`.stats-toolbar` 父容器 `flex-wrap: wrap` 默认；按钮文字「Passthrough mode」「7day」
偏长，窄屏 / zh-CN 字体宽度下换行。用户允许改长宽高，所以降 padding + 加 `nowrap`。

**改法**：`.stats-toolbar` gap 12→8px；`.stats-range-btn / .stats-dim-btn /
.stats-mode-btn` padding 4px 12px→4px 10px + `white-space: nowrap`；`.stats-toolbar-hint`
也加 nowrap 防 hint 自身换行。父容器三个 group 也加 `flex-wrap: nowrap`。

### 4.3 gui.py snapshot 字段缺失（会话 #3）

`_rebuild_snapshot` 历史上只塞了 live_panel / live_panel_max 等几个字段，
后端 Settings 有 18+ 字段但没全进 snapshot。前端 `renderSettingsPrefs` 模板读
`prefs.xxx ? "checked" : ""`（localStorage），导致跨设备 / 首次启动值不对。

**改法**：snapshot 补 9 字段，全用 `getattr(self.settings, "relay_*前缀真名", default)`
兜底；autostart 不在 Settings → 从 `self.autostart.is_enabled()` 读 + try/except
单独写（dict literal 不能包 try/except）。

### 4.4 wireSettingsPrefs 缺 snap 参数 → ReferenceError（会话 #3 探针踩坑）

`mountTimeoutWheel` 第 5173 行用 `snap.live_panel_thinking_timeout` —— 但
`wireSettingsPrefs` 只接 `body`，snap 不在作用域。**ReferenceError: snap is not
defined @ app.js:5173**，整个 wireSettingsPrefs 中断抛出，refreshPrefsDynamic
也没跑到。

**改法**：`wireSettingsPrefs(body, snap)`，renderSettingsPrefs 调用点同步传
`snap`。这一改连带暴露第二个探针 bug（4.5）。

### 4.5 stub bridge method 名 snake_case 不匹配（会话 #3 探针踩坑）

`_call(method, args)` 走 snake_case（`get_autostart` / `get_live_panel` 等）；
探针 mock 用了 camelCase → `_call` 找不到 key → fallback null → refreshPrefsDynamic
里 `autoStart.checked = !!(null)` → checkbox 错误覆盖成 unchecked。

**改法**：mock Proxy 所有 key 改 snake_case，对齐 `_call` 入参。

### 4.6 statsState 持久化缺失（会话 #3）

mode 已有 `getConsumeMode()` 从 localStorage 读；range/dim 没有持久化 → 切页回来
重置为默认「30d / upstream」。

**改法**：statsState init 加 `localStorage.getItem("stats-range")` / `"stats-dim"` 三选一
白名单校验；wireStatsToolbar 点击 handler 加 `localStorage.setItem`。

### 4.7 Anthropic SSE 协议不发中间 output_tokens（会话 #4 协议限制）

Anthropic SSE 把 usage 拆两段：`message_start.message.usage` 一次性给 input +
cache；`message_delta.usage` cumulative output_tokens，**只在流结束前一条发一次**。
所以 output_tokens 始终是 0 / 终值，没有中间帧。OpenAI 上游 + linguafranca 倒是
逐帧更新 parser.usage.output_tokens，但跨协议路径 frame 间仍有空隙。

**改法**：加 `_estimate_output_tokens(text)` 工具（CJK 1.5 字符/token + 其他 4 字符/token），
三处 streaming 广播 `usage_now` 增 `output_tokens_est = max(真值, 估算)` 字段，前端
applyUsage 取 max 显示。**只入广播字段，不动 last_usage / parser.usage / db.record**。

### 4.8 估算不能污染计费（会话 #4 严格约束）

`db.record` 的 UsageAcc 是计费权威字段，必须用 message_delta 真值。

**改法**：**新加字段** `output_tokens_est`，仅入 `usage_now`（广播 dict），不入
`last_usage` / `parser.usage` / `UsageAcc`。三个 done 路径的 usage_live 构造
**不带 est** —— 前端 max(真值, 0) = 真值，自动校准。

### 4.9 estimator 单调性（会话 #4）

如果估算值低于 message_delta 真值，显示会「先低后高跳一下」，体感像闪。

**改法**：`max(u.output_tokens, _estimate_output_tokens(...))` 强制估算 ≥ 真值，
估算只在前向补缺；message_delta 真值到达后 max 真值优先（通常真值远大于估算）。

---

## 5. 解决

### 5.1 app.js：I18N.en 加短词（会话 #1）

```js
const I18N = {
  en: {
    "近 24h": "24h", "近 7 天": "7 days", "近 30 天": "30 days",
    "按上游": "Upstream", "按模型": "Model",
    // ... 其它既有
  },
};
```

### 5.2 styles-20260817.css：按钮单行（会话 #2）

```css
.stats-toolbar { gap: 8px; }
.stats-toolbar-range, .stats-dim-group, .stats-mode-group { flex-wrap: nowrap; }
.stats-range-btn, .stats-dim-btn, .stats-mode-btn {
  padding: 4px 10px;
  white-space: nowrap;
}
.stats-toolbar-hint { white-space: nowrap; }
```

### 5.3 gui.py：snapshot 补字段（会话 #3）

```python
snapshot = {
    # ... 原有 ...
    "live_panel_frameless": bool(getattr(self.settings, "relay_gui_panel_frameless", False)),
    "start_hidden": bool(getattr(self.settings, "relay_gui_start_hidden", False)),
    "save_messages": bool(getattr(self.settings, "relay_save_messages", True)),
    "autoswitch_enabled": bool(getattr(self.settings, "relay_quota_autoswitch", False)),
    "autoswitch_pool": list(getattr(self.settings, "relay_autoswitch_pool", None) or []),
    "autoswitch_at": float(getattr(self.settings, "relay_quota_switch_at", 0.9)),
    "error_analysis_enabled": bool(getattr(self.settings, "error_analysis_enabled", False)),
    "error_analysis_upstream": getattr(self.settings, "error_analysis_upstream", None),
    "error_analysis_model": getattr(self.settings, "error_analysis_model", None),
}
try:
    snapshot["autostart_enabled"] = (self.autostart.is_enabled() if self.autostart else False)
except Exception:
    snapshot["autostart_enabled"] = False
```

### 5.4 app.js：renderSettingsPrefs 模板 + wireSettingsPrefs 修 bug（会话 #3）

模板：所有 `${prefs.xxx ? "checked" : ""}` → `${snap && snap.xxx ? "checked" : ""}`，
number input 用 `value="${snap && typeof snap.xxx === "number" ? snap.xxx : default}"`。

wireSettingsPrefs：
```js
function wireSettingsPrefs(body, snap) {
  // ... 既有 ...
  const mountTimeoutWheel = (sel, setter, def) => {
    const host = body.querySelector(sel);
    if (!host) return;
    wheelPicker(host, setter, def);
  };
  mountTimeoutWheel(".prefs-live-panel-thinking-input", s => api.setLivePanelThinkingTimeout(s), (snap && typeof snap.live_panel_thinking_timeout === "number") ? snap.live_panel_thinking_timeout : 60);
  // ...
}
// 调用点：
wireSettingsPrefs(body, snap);
```

### 5.5 app.js：statsState 持久化（会话 #3）

```js
const statsState = { range: "30d", dim: "upstream", mode: "relay", loading: false, needRender: false };
try {
  const r = localStorage.getItem("stats-range");
  if (r === "1d" || r === "7d" || r === "30d") statsState.range = r;
  const d = localStorage.getItem("stats-dim");
  if (d === "upstream" || d === "model") statsState.dim = d;
} catch (_) {}
// wireStatsToolbar 点击处理：
try { localStorage.setItem("stats-range", statsState.range); } catch (_) {}
try { localStorage.setItem("stats-dim", statsState.dim); } catch (_) {}
```

### 5.6 proxy.py：估算工具 + 三处广播（会话 #4）

```python
def _estimate_output_tokens(text: str) -> int:
    if not text: return 0
    cjk = other = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                or 0x3000 <= cp <= 0x303F):
            cjk += 1
        else:
            other += 1
    return max(1, int(round(cjk / 1.5 + other / 4)))

# 三处 broadcast（Anthropic adapter SSE / cross-wire / direct）：
output_est = _estimate_output_tokens((assistant_now or "") + (thinking_now or ""))
usage_now = {
    "input_tokens": ...,
    "output_tokens": ...,  # 不动，仍是真值
    "output_tokens_est": max(..., output_est),  # 新字段，仅广播
    "cache_read_input_tokens": ...,
    "cache_creation_input_tokens": ...,
}
```

### 5.7 live_panel.js：applyUsage 读 est（会话 #4）

```js
const tOut = usage.output_tokens || 0;
const tOutEst = (typeof usage.output_tokens_est === "number" && usage.output_tokens_est > 0)
  ? usage.output_tokens_est : 0;
const tOutShow = Math.max(tOut, tOutEst);
setTokenValue($tokOut, tOutShow, prev.out);
$tokOut.dataset.raw = String(tOutShow);  // dataset.raw 也用 show 值
```

---

## 6. 是否完全按规划

**四件事按规划落地**，四个独立踩坑：

- **会话 #1 EN i18n**：按用户给的短词映射；不全量翻译（与 v0.113n 决策一致）。
- **会话 #2 按钮单行**：用户允许改按钮尺寸，所以 padding 4px 10px + nowrap 是被
  授权的；与 v0.113l+m 的折叠态 wheel picker 风格一致。
- **会话 #3 设置持久化**：核心 bug 是 `wireSettingsPrefs` 缺 snap 参数导致
  `ReferenceError`，连带暴露探针 mock snake_case 错。修后 18/18 checkbox +
  3/3 wheel + max-input + stats range/dim 全 OK。
- **会话 #4 侧栏实时 token**：估算系数 1.5/4，验收覆盖英文/中文/混合/代码/流式
  单调性。三处 broadcast 加字段，前端 max 显示，**绝对不动**计费路径。

`py_compile gui.py / proxy.py` 通过；`node --check app.js / live_panel.js` 通过；
estimator 行为单测（usage-stats env）：英文 11 / 中文 12 / 混合 16 / 代码 10
/ 流式单调 3→7→9 全过；探针 `_probe_persistence.py` 18/18 + 3/3 + 4/4 stats 全过。

---

## 7. 最终实现点

### 改动文件清单（合并四会话）

| 文件 | 改动 |
|---|---|
| `src/relay/gui.py` | `_rebuild_snapshot` 补 9 字段（live_panel_frameless / start_hidden / save_messages / autoswitch_*3 / error_analysis_*3 / autostart_enabled）；全用 Settings 真名 `relay_*` 前缀 + `getattr` 兜底；autostart_enabled 单独 try/except 写 dict 外 |
| `src/relay/proxy.py` | 新增 `_estimate_output_tokens(text)` 工具；三个 streaming 广播点（Anthropic adapter SSE / cross-wire streaming / direct streaming）的 `usage_now` 增 `output_tokens_est` 字段，`max(真值, 估算)` 保单调；不动 `last_usage` / `parser.usage` / `db.record` |
| `src/relay/web/app.js` | I18N.en 加 24h / Upstream / Model 短词；renderStats hint 按 lang 分支用 en 替换；`renderSettingsPrefs` 模板 18 checkbox + 1 number 全读 snap；`wireSettingsPrefs(body)` → `(body, snap)`（修 snap undefined ReferenceError）；statsState init 从 localStorage 读 range/dim；wireStatsToolbar 点击写回；stats 工具栏按钮 CSS 类组合配合 styles；版本号 bump |
| `src/relay/web/live_panel.js` | `applyUsage(u)` 读 `output_tokens_est`，与 `output_tokens` 取 max 后写 DOM；dataset.raw 用 tOutShow 保回放不回退；注释说明估算仅用于显示 |
| `src/relay/web/styles-20260817.css` | `.stats-toolbar` gap 12→8px；按钮组加 `flex-wrap: nowrap`；按钮 padding 12→10px + `white-space: nowrap`；`.stats-toolbar-hint` 加 nowrap |
| `docs/dev/2026-08-22_session_polish_persistence_v0.113.md` | 本汇总 |
| `docs/dev/ui_sidebar_realtime_token_v0.113x.md` | 侧栏实时 token 独立 doc（已存在） |
| `docs/dev/ui_sidebar_realtime_token_v0.113x_summary.md` | v0.113k+l+m+x 协同汇总 doc |
| `docs/dev/ui_wheel_picker_collapse_v0.113lm.md` | wheel picker 折叠展开独立 doc |
| `docs/dev/ui_error_analysis_toggle_v0.113q.md` | 报错分析折叠独立 doc |
| `docs/dev/ui_settings_persistence_v0.113u.md` | 设置持久化独立 doc（已存在） |

### 状态流（用户视角）

- **EN 用户进统计页** —— toolbar 显示「24h / Upstream / Passthrough」（短词），
  三档单行不换行。
- **用户改设置** —— 切换 toggle / 输入数值 / 选语言 → 立刻写后端 .env 或
  upstreams.json → 切页 / 重启 GUI 保持。
- **用户进设置页** —— 首帧按上次退出态渲染所有开关 / 数值（wheel picker 折叠态
  32px 单格显示当前值）；hover 展开 → 滚轮 / 点击 / 拖动切值 → 防抖 350ms 写后端。
- **用户触发流式请求** —— 侧栏 input 立即跳真值；output 从 0 随字符逐跳（估算）→
  流结束跳到 message_delta 真值；done 后稳定。
- **GUI 重启** —— snapshot 持久化（live_panel_thinking_timeout / error_analysis_*3 /
  autostart_enabled 等）+ localStorage 持久化（stats-range/dim）+ relay.db 持久化
  （requests 表记录 + 历史 inflight 状态）→ 用户感知「刷新后还在原状态」。

### 验证

- `python -m py_compile src/relay/gui.py src/relay/proxy.py` 通过。
- `node --check src/relay/web/app.js src/relay/web/live_panel.js` 通过。
- 探针 `_probe_persistence.py` headless stub-bridge：18/18 checkbox + 3/3 wheel
  picker（aria=90/45/15 + children=5）+ max-input=6 + stats range=1d/dim=model
  全 OK；mode-active=passthrough（passthrough_mode=true 优先，符合 getConsumeMode
  设计）。
- estimator 行为单测（usage-stats env）：英文 11 / 中文 12 / 混合 16 / 代码 10；
  流式 3→7→9 单调递增无倒退。
- 端到端：设置页改三档 → 重启 GUI → 保持；侧栏触流 → output 实时跳 → 结束真值；
  EN toolbar 三档单行不换行。
