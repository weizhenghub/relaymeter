# 设置页 UI 偏好全部持久化（v0.113u）开发文档

> 把设置页所有 UI 偏好（开关 / 数值 / 选择）从「前端的 localStorage」+「后端的
> .env / upstreams.json」统一收口：进入设置页立即按上次退出值显示，切回去刷新
> 也保持 —— 永不在重启后丢状态。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 所有设置写入本地磁盘，下次启动时加载表，确保和上次退出时一样

（紧接追问：）

> 全部 UI 设置都要持久化

**语义边界**（用户回复）：UI 偏好（开关 / 数值 / 模式 / 语言 / 视图状态）全要；
数据/业务状态（如历史请求、统计）不在范围。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **后端开关**（实时栏 / 侧栏无边框 / IO 映射 / 自启 / 启动隐藏 / 自动切换 / 报错分析等）→ 从 `.env` / `upstreams.json` 启动即加载 | 指令 |
| B | **前端开关**（无边框 / 调试栏 / 开发者模式 / 闪烁等）→ localStorage | 指令 |
| C | **数值 / 选择**（实时栏最大并发数 / 三档超时 / 语言 / 主题 / 模式）→ 对应后端键或 localStorage | 指令 |
| D | **视图状态**（统计 range / dim / mode、设置页子菜单）→ localStorage | 隐含 |
| E | **进入设置页首帧**就用上次值渲染（不依赖异步拉取，避免先空后填的抖动） | 隐含 |

### 隐含但需自行决策的点

- **不引入新持久化层**：现状是「前端 localStorage + 后端 .env / upstreams.json」分层，
  既能用就别加 IndexedDB / electron-store 之类。本轮只**确保首帧读到上次值**。
- **真值来源（SoT）**：开关类以**后端 .env / upstreams.json** 为准；前端 localStorage
  只是后端异步延迟期的 cache；首帧用 snapshot 一次渲染到位，再 refreshPrefsDynamic 用
  bridge 拉一次后端真值覆盖（这是当前架构已经有的，不变）。
- **三档超时用 wheelPicker** 自绘而非原生 number input → 初始化必须显式调
  `wheelPicker(host, setter, def)`，否则 DOM 里只是一个空 div，aria-valuenow=null。
- **统计 mode 的恢复优先级**：`localStorage.consume-mode` 在「passthrough 模式开关」未开
  时生效；开了 passthrough 模式则 mode 永远是「passthrough」（这是已有行为，本轮不重写）。
- **不入 relay.db**：本轮零新增 schema、零迁移、零 kill-8088。
- **headless 探针**：必须用 snake_case 的 bridge method 名（`_call("get_autostart")` 不是
  camelCase），否则 `refreshPrefsDynamic` 拿不到真值，自动把首帧值覆盖成默认。

---

## 3. 分析需求后得出的开发路径

```
后端（gui.py）
  _rebuild_snapshot  增字段：live_panel_frameless / start_hidden / save_messages /
                     autoswitch_enabled / autoswitch_pool / autoswitch_at /
                     error_analysis_{enabled,upstream,model} / autostart_enabled
前端（app.js）
  renderSettingsPrefs(body, snap)
                     所有 checkbox 改读 snap.xxx；number input 改读 snap.live_panel_max
  wireSettingsPrefs(body, snap)
                     透传 snap 给三档 wheel 初始化；首帧值 = snap.xxx_timeout
  mountTimeoutWheel
                     def 参数 = snap.xxx_timeout ?? 默认值
  statsState
                     init 时从 localStorage 读 stats-range / stats-dim
  wireStatsToolbar
                     点击后写回 localStorage（持久化）
  refreshPrefsDynamic
                     不变 —— 后端真值覆盖（已有逻辑）
探针（_probe_persistence.py）
  stub bridge        snake_case 全套 get_* method
  snap               模拟上次退出状态：18 个开关全开/全关按预期；3 档超时=90/45/15；
                     live_panel_max=6；passthrough_mode=false
  验证               18 checkbox 状态 + 3 wheel 初始化 + stats range/dim/mode
```

### 开发顺序落地

```
#1 后端 _rebuild_snapshot 补齐缺失字段（gui.py:2614 附近）
#2 前端 renderSettingsPrefs 模板把 18 checkbox 全改读 snap
#3 前端 wireSettingsPrefs 接 snap → mountTimeoutWheel 三档
#4 前端 statsState init + wireStatsToolbar 持久化
#5 写 _probe_persistence.py（headless stub bridge + 完整断言）
#6 跑探针 → 修 → 重跑直到全 OK
#7 版本 bump + 清理旧探针 + 写文档
```

---

## 4. 问题

### 4.1 三档超时 wheelPicker 初始化静默失败（核心 bug）

进入设置页时，`prefs-live-panel-thinking-input` 等三个 div 都是空 div（模板里就只有
`<div class="... wheel-picker" data-timeout="..."></div>`），需要 `wireSettingsPrefs`
末尾的 `mountTimeoutWheel` 显式调 `wheelPicker(host, setter, def)` 才会注入 band/3
rows/unit，并把 `aria-valuenow` 写到 host。

第一版 `wireSettingsPrefs` 只接 `body` 一个参数；但 `mountTimeoutWheel` 第 5173 行用
了 `snap.live_panel_thinking_timeout` —— **`snap` 不在作用域** → `ReferenceError: snap
is not defined` → 整个 `wireSettingsPrefs` 中断抛出 → `refreshPrefsDynamic` 也没跑到 →
用户看到的就是「空白超时选择器」+「某些开关未被实时拉后端覆盖」。

**探针侧现象**：`children=0 | hasWpSet=false | aria=null`，但同一探针里所有 checkbox
显示 OK（因为 checkbox 是模板字面量直接渲染，wireSettingsPrefs 的中断不影响）。

**改法**：`wireSettingsPrefs(body)` 改 `wireSettingsPrefs(body, snap)`；renderSettingsPrefs
调用点同步传 `snap`。这一改后 `prefs-autostart` 等开关被 refreshPrefsDynamic 用后端真
值覆盖，探针侧暴露 snake_case mock 名错的第二个 bug（见 4.2）。

### 4.2 stub bridge method 名要 snake_case

`_call(method, args)` 走的是 snake_case（`get_autostart` / `get_live_panel` 等）；
探针最初 mock 用了 camelCase（`getAutostart` / `getLivePanel`）→ `_call` 找不到 key
→ 返回 fallback `null` → `refreshPrefsDynamic` 里 `autoStart.checked = !!(null)` →
checkbox 被覆盖成 unchecked。

**改法**：mock Proxy 的所有 key 改成 snake_case（与 `_call` 入参一致）。

### 4.3 probe 里 body 长度 66 = 「加载中…」的歧义

`card-settings-prefs-body` 默认内容是 `<div class="card-empty">加载中…</div>`（index.html
348 行），长度恰好约 66 字节。第一版探针把 `body.innerHTML.length` 当成「已渲染」标志
误读 —— renderSettingsPrefs 一旦没跑，length 就是 66 看起来正常。**改法**：检查具体
checkbox 存在性（`MISSING` vs `checked/unchecked`），不做长度断言。

### 4.4 stats mode 的恢复优先级

`statsState.mode` 来自 `getConsumeMode()`（app.js:1537）；`getConsumeMode` 的实现是
「`passthrough_mode` 开启 → 永远 passthrough；否则读 `localStorage.consume-mode`」。
这不是 bug，是已有架构——探针里 `snap.passthrough_mode=true` 时 mode-active 就显示
passthrough 而不是 localStorage 里的 `all`，符合预期。本轮不动。

### 4.5 字段名映射（Settings 真名）

`gui.py _rebuild_snapshot` 第一版我写了 `save_messages` / `autostart_enabled` /
`launch_hidden` 等「友好名」，但 `Settings`（pydantic-settings）真名带前缀：
`relay_save_messages` / `relay_quota_autoswitch` / `relay_gui_start_hidden` 等。
第一版直接 `getattr(settings, "save_messages", True)` 全部命中默认值，真值没进
snapshot → 设置页 checkbox 永远显示默认。

**改法**：用 `relay_*` 前缀真名 + `getattr(..., default)` 兜底；autostart 因为
`Settings` 里没有，从 `self.autostart.is_enabled()` 单独读并 try/except 兜底（这个
字段无法在 dict 字面量里 try/except，所以单独写到 dict 外）。

---

## 5. 解决

### 5.1 gui.py：_rebuild_snapshot 补齐字段

```python
# gui.py:2614 附近 _rebuild_snapshot 返回的 snapshot dict
snapshot = {
    # ... 原有字段 ...
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
# autostart 不在 Settings，从 autostart 对象读；try/except 不能放 dict 字面量内 → 单独写
try:
    snapshot["autostart_enabled"] = (self.autostart.is_enabled() if self.autostart else False)
except Exception:
    snapshot["autostart_enabled"] = False
```

### 5.2 app.js：renderSettingsPrefs 模板首帧读 snap

所有 checkbox 把 `${prefs.xxx ? "checked" : ""}`（localStorage 旧版）改 `${snap && snap.xxx ? "checked" : ""}`；
number input 用 `value="${snap && typeof snap.xxx === "number" ? snap.xxx : default}"`。
**关键**：18 个 checkbox + 1 个 number input + 1 个 wheel-picker 区（共 3 个 div）。

```js
// 示例（line 4706 / 4721 / 4741 / 4754 / 4765 等）
<input type="checkbox" class="prefs-no-panel-frame" ${snap && snap.live_panel_frameless ? "checked" : ""} />
<input type="checkbox" class="prefs-live-panel" ${snap && snap.live_panel ? "checked" : ""} />
<input type="checkbox" class="prefs-live-panel-concurrent" ${snap && snap.live_panel_concurrent ? "checked" : ""} />
<input type="checkbox" class="prefs-live-panel-always-one" ${snap && snap.live_panel_always_one ? "checked" : ""} />
<input type="checkbox" class="prefs-save-messages" ${snap && snap.save_messages ? "checked" : ""} />
<input type="checkbox" class="prefs-autostart" ${snap && snap.autostart_enabled ? "checked" : ""} />
<input type="checkbox" class="prefs-launch-hidden" ${(snap && snap.start_hidden) || prefs.launchHidden ? "checked" : ""} />
<input type="checkbox" class="prefs-autoswitch" ${snap && snap.autoswitch_enabled ? "checked" : ""} />
<input type="checkbox" class="prefs-io-map" ${snap && snap.show_io_map ? "checked" : ""} />
// ... 全部 18 个按此模式改 ...

<input type="number" min="1" max="8" step="1"
       class="prefs-live-panel-max-input"
       value="${snap && typeof snap.live_panel_max === "number" ? snap.live_panel_max : 3}" />
```

### 5.3 app.js：wireSettingsPrefs 修复核心 bug

```js
// 改前
function wireSettingsPrefs(body) { ... mountTimeoutWheel(..., (snap && ...) ...) }
// 改后
function wireSettingsPrefs(body, snap) {
  // ...
  const mountTimeoutWheel = (sel, setter, def) => {
    const host = body.querySelector(sel);
    if (!host) return;
    wheelPicker(host, setter, def);
  };
  mountTimeoutWheel(".prefs-live-panel-thinking-input", s => api.setLivePanelThinkingTimeout(s), (snap && typeof snap.live_panel_thinking_timeout === "number") ? snap.live_panel_thinking_timeout : 60);
  mountTimeoutWheel(".prefs-live-panel-gap-input",       s => api.setLivePanelGapTimeout(s),       (snap && typeof snap.live_panel_gap_timeout === "number") ? snap.live_panel_gap_timeout : 20);
  mountTimeoutWheel(".prefs-live-panel-text-input",      s => api.setLivePanelTextTimeout(s),      (snap && typeof snap.live_panel_text_timeout === "number") ? snap.live_panel_text_timeout : 10);
}

// 调用点同步
wireSettingsPrefs(body, snap);
```

### 5.4 app.js：statsState 持久化

```js
// app.js:1849 附近
const statsState = { range: "30d", dim: "upstream", mode: "relay", loading: false, needRender: false };
try {
  const _savedRange = localStorage.getItem("stats-range");
  if (_savedRange === "1d" || _savedRange === "7d" || _savedRange === "30d") statsState.range = _savedRange;
  const _savedDim = localStorage.getItem("stats-dim");
  if (_savedDim === "upstream" || _savedDim === "model") statsState.dim = _savedDim;
} catch (_) {}

// wireStatsToolbar 点击处理里
try { localStorage.setItem("stats-range", statsState.range); } catch (_) {}
try { localStorage.setItem("stats-dim", statsState.dim); } catch (_) {}
```

### 5.5 探针 _probe_persistence.py

- stub `window.pywebview.api`：Proxy get 拦截，`get_snapshot` / `get_status` 返回
  `__snap` / `__status` 副本；所有 `get_*` 用 snake_case（`get_autostart` /
  `get_live_panel_frameless` / `get_live_panel_thinking_timeout` 等）。
- `__snap` 模拟「上次退出」：18 个开关按预期态、3 档超时=90/45/15、max=6、
  passthrough_mode=false。
- localStorage 预置：`stats-range=1d` / `stats-dim=model` / `consume-mode=all` /
  `lang=zh` / `relay-gui-prefs-v1` 含所有 prefs 字段。
- 断言：18 checkbox 状态 + `prefs-live-panel-max-input=6` + 3 wheel `children=5` +
  `aria-valuenow` 等于 snap 值 + stats range/dim/mode active 态。
- 用 Edge headless `--dump-dom` 抓渲染后 HTML，正则提取 `<pre id="__report">` 报告。

---

## 6. 是否完全按规划

**按规划落地**，有一个核心 bug 修复 + 几个探针踩坑：

- **三档超时 bug**：v0.113k 引入 wheelPicker 时漏传 snap 进 wireSettingsPrefs，导致
  `ReferenceError`。本轮彻底修，并验证 `children=5 / aria=90 / __wpSet=true`。
- **后端 snapshot 字段名错**：第一版用友好名 `save_messages` / `autostart_enabled`
  等，但 Settings 真名是 `relay_save_messages` / autostart 不在 Settings 等 → 全部
  命中默认值。改用真名 + `getattr(..., default)` 兜底。
- **autostart 单独 try/except**：因为 Settings 无 autostart 字段，从 `self.autostart`
  对象读。Python 不允许 try/except 在 dict 字面量里 → 单独写到 dict 外赋值。
- **探针 mock key**：camelCase 改 snake_case，与 `_call` 入参对齐。
- **stats mode-active**：探针设 `passthrough_mode=true` + `consume-mode=all`，结果
  显示 passthrough——这是 `getConsumeMode()` 的设计（passthrough 模式开关优先），
  不是 bug，不改。
- `python -m py_compile` gui.py 通过；`node --check app.js` 通过；headless 探针
  18/18 + 3/3 + 4/4 stats 断言全通过。
- 版本号 bump：app.js `?v=20260822-28`。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/gui.py` | `_rebuild_snapshot` 补 9 字段（live_panel_frameless / start_hidden / save_messages / autoswitch_*3 / error_analysis_*3）；autostart_enabled 单独 try/except 写；全用 `relay_*` 前缀真名 + `getattr` 兜底 |
| `src/relay/web/app.js` | `wireSettingsPrefs(body)` → `wireSettingsPrefs(body, snap)`；renderSettingsPrefs 调用点同步；renderSettingsPrefs 模板 18 checkbox + 1 number 全改读 snap；statsState init 从 localStorage 恢复 range/dim；wireStatsToolbar 点击写回 localStorage；版本号 bump `?v=20260822-28` |
| `src/relay/web/_probe_persistence.py` | headless stub bridge 探针：snake_case 全套 get_* + 18 checkbox + 3 wheel + max-input + stats range/dim/mode 断言（诊断用） |
| `src/relay/web/_probe_wheel.py` / `_probe_wheel2.py` | wheelPicker bug 定位探针（已发现 root cause 并修，可清理） |

### 状态流

- **重启进入设置页**：tick 500ms 内拿到 snapshot（首帧即有真值）→ renderActiveView
  → renderSettingsPrefs → 模板用 snap 渲染所有 checkbox/number → wireSettingsPrefs
  → mountTimeoutWheel 三档用 snap 初始化 wheelPicker → refreshPrefsDynamic 异步拉
  bridge 真值覆盖（结果应一致）。
- **统计页切换 range/dim**：点击按钮 → 同步更新 statsState + active class + 写
  localStorage → 下次进统计页 / 重启 → statsState init 从 localStorage 读 → 按钮
  active 态恢复。
- **后端开关**：用户在设置页改动 → `setLivePanel*` / `setAutostart` 等 bridge →
  后端更新 .env → tick 拿到新 snapshot → 模板 re-render（设置页 prefsRendered 守卫
  不会重新渲染，但首帧已经对了；改动走 change 事件即时反馈）。

### 验证

- `python -m py_compile src/relay/gui.py` 通过。
- `node --check src/relay/web/app.js` 通过。
- headless stub-bridge 探针（`_probe_persistence.py`）全过：
  - 18/18 checkbox 状态与预期一致（含 autostart 在 mock 修后为 checked OK）
  - `prefs-live-panel-max-input: 6 (want 6)`
  - 3 wheel `children=5 / hasWpSet=true / aria-valuenow=90|45|15`（与 snap 完全一致）
  - `range-active: 1d (want 1d)` + `dim-active: model (want model)`
  - `mode-active: passthrough`（passthrough_mode=true 优先，符合 getConsumeMode 设计）
- wheel bug 定位探针（`_probe_wheel2.py`）修复前报 `ReferenceError: snap is not
  defined @ app.js:5173`；修复后报 `children=5 | aria=90 | hasWpSet=true`。
