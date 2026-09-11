# 设置页新增：报错分析 —— 允许小模型分析报错信息（v0.113o）开发文档

> 设置页加一个选项：**允许小模型分析报错信息** + 可设置一个分析模型。当出现报错时
> （余额耗尽 / 网络错误 / 达到次数限制等），把报错信息发给这个小模型判断错误类型，
> 给用户可读的中文提示，以右上角 toast 弹出。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 新建一个允许采用小模型分析报错信息的选项，允许设置一个模型，当出现报错信息时，将信息发送到这个小模型，判断错误类型（例如余额耗尽，网络错误，达到次数限制）给用户提示

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **设置开关** —— 设置页加「允许小模型分析报错信息」选项（开/关） | 指令 |
| B | **设置分析模型** —— 可选择一个模型（上游 + 模型二选），建议轻量小模型 | 指令 |
| C | **触发时机** —— 出现报错时把错误信息发给小模型 | 指令 |
| D | **分类 + 提示** —— 小模型判断错误类型（余额耗尽 / 网络错误 / 达到次数限制等），返回用户可读中文提示 | 指令 |

### 隐含但需自行决策的点

- **⚠ 分类在 GUI 进程做，不动 relay 的 proxy 错误路径**：报错最终都落在 relay.db
  `requests` 表（`error` / `status_code` 列）；GUI 已有只读访问先例（`mode=ro`）。
  GUI 轮询线程已有「增量游标拉告警」先例（`/api/alerts?since=`）—— 错误提示完全
  镜像这个模式，**零新增传输层、零改 relay/proxy**，遵守「严禁 kill 8088」记忆规则。
- **外呼模式复用 probe 先例**：GUI 进程做外呼已有 `probe_upstream` 先例（后台线程 +
  `asyncio.run` + httpx POST 最小 messages payload），报错分类复制同款 HTTP 段。
- **模型清单来源**：`get_advanced_switch` 的模型去重清单（`{upstream, model, label}`）
  是现成的 —— 报错分析的下拉直接用同一来源（抽成公共辅助函数）。
- **分析哪些错误**：`client_disconnect`（用户主动取消）不算需要提示的错误，排除。
- **GUI 重启不重放历史**：游标 init 时 = 当前 `MAX(id)`，只分析此后新出现的错误。
- **防 429 风暴**：同一 (upstream, 状态码或错误前缀) 60s 内去重，不重复弹 toast。
- **fire-and-forget**：分类失败绝不阻塞轮询，游标照常推进（不重试）。
- **前端 option 分隔符坑**：`upstream\u0000model`（NUL）分隔 —— `innerHTML` 里的
  `\u0000` 会被浏览器替换成 U+FFFD（实测 charCode 65533），必须用 DOM API
  `createElement("option")` + `opt.value` 属性赋值（实测保留 charCode 0）。
- **「测试」按钮**：用一条示例 429 报错真实跑一次分类（外呼所选上游），`alertModal`
  显示分类结果 —— 验证模型与提示词。

---

## 3. 分析需求后得出的开发路径

```
后端（全部 GUI 进程；relay 进程零改动）
  error_analyzer.py  ERROR_SYSTEM_PROMPT（分类 prompt）+ build_context + build_prompt
                     + parse_verdict（JSON-tolerant）+ resolve_target（上游定位）
                     + async classify_error（httpx POST，镜像 advanced_switch._call_analysis_model）
  config.py          Settings 增 error_analysis_enabled / _upstream / _model 三字段
                     + save_error_analysis（镜像 save_advanced_switch 的 _mutate 模式）
  upstreams_file.py  apply_to_settings 读顶层 error_analysis 对象（fail-open）
  gui.py             _upstream_model_catalog 辅助（get_advanced_switch 抽出共用）
                     Api 增 get_error_analysis / set_error_analysis / test_error_analysis
                     App 增错误提示轮询：游标 + 挂起队列 + debounce + 后台分类线程
前端（app.js / index.html / styles）
  3 桥（getErrorAnalysis / setErrorAnalysis / testErrorAnalysis）
  renderSettingsError（开关 + 模型下拉 + 测试/保存按钮，DOM API 建 option）
  renderErrorHints（右上角 toast，id 去重 + 8s 淡出 + × 关闭）
  index.html          nav 子项「报错分析」+ settings-error section
  styles              .error-hint toast 系列 + 每类型边框色
```

### 开发顺序落地

```
#1 error_analyzer.py 新模块（prompt + 纯函数 + classify_error）
#2 config.py 三字段 + save_error_analysis；upstreams_file.py 读入
#3 gui.py _upstream_model_catalog 抽出 + 三个 Api 方法
#4 gui.py App 轮询（游标/挂起/debounce/后台线程）
#5 前端：3 桥 + renderSettingsError + renderErrorHints + index.html section/nav + styles
#6 py_compile + node --check + 纯函数单测 + headless stub-bridge 探针
#7 版本 bump + 清理探针 + 写文档
```

---

## 4. 问题

### 4.1 分类放在哪一侧？（核心架构决策）

relay 的 proxy 错误路径是每次请求都要走的 **关键路径**（upstream_429 /
upstream_disconnect / relay_unknown_key…）。若在此做同步外呼分类，会拖慢出错请求的
响应，且外呼失败可能污染错误处理逻辑。而 relay 进程「严禁 kill」、改动要重启。

**决策**：**分类全部在 GUI 进程**。报错已经落在 relay.db `requests` 表；GUI 轮询线程
增量扫新错误，后台线程外呼分析模型，结果经 `snapshot["error_hints"]` 推前端。relay
进程零改动、零重启；GUI 重启顶多从 `MAX(id)` 继续，不重放历史。

### 4.2 分析模型怎么解析成「上游 + 模型」？

用户选的是「上游 / 模型」二元的组合（下拉单值 `upstream\u0000model`）。但配置持久化
在 `error_analysis_upstream` / `error_analysis_model` 两个字段，且用户可能只配上游
（用其兜底模型）或不配（全部留空）。

**改法**：`resolve_target(settings, context)` 三级解析 ——
`error_analysis_upstream` 精确匹配优先；否则找提供所选 `error_analysis_model` 的上游；
再找不到用第一个 anthropic 上游的兜底模型。返回 `(cfg, model)`；无模型可解析 → 返回
`{"ok": False, "error": "未配置可用的分析模型"}`（不抛异常，调用方 fire-and-forget）。

### 4.3 GUI 进程如何只读访问 relay.db？

relay 进程持 DB 写锁（WAL），GUI 直接打开会撞锁。

**改法**：只读 URI 连接 `sqlite3.connect("file:{path}?mode=ro", uri=True)` —— 与
`get_storage_info` 的 `mode=ro` 先例（gui.py:1458）同款。扫描后立即 `close()`。

### 4.4 `innerHTML` 里的 `\u0000` 被浏览器替换成 U+FFFD

前端 option 值用 NUL 分隔（`upstream\u0000model`）。第一版用 innerHTML 拼
`<option value="deepseek\u0000MiniMax-M3">` —— 探针显示 `model-selected=false`：
浏览器把属性里的 `\u0000` 替换成 U+FFFD（charCode 65533），值变成
`deepseek\uFFFD…`，与保存的 `deepseek\u0000…` 对不上。

**改法**：用 DOM API `document.createElement("option")` + `opt.value` 属性赋值
（实测保留 charCode 0），代码注释说明原因。**遗留**：`advSelect`（app.js:4960）存在
同样的 innerHTML `\u0000` 潜在 bug —— 超范围未动，记入文档。

### 4.5 429 风暴会刷爆分类调用与 toast

一次限流可能让上游连续几分钟持续 429，每次都分类 + 弹 toast 是灾难。

**改法**：debounce —— 按 `(upstream, status_code 或 error 前缀)` 做 key，60s 内同
key 直接跳过分类。`_hint_debounce` dict + `_hint_lock` 保护。

### 4.6 分类失败会不会卡住轮询？

外呼可能超时 / 上游不可达 / 无模型。若失败重试会阻塞 `_rebuild_snapshot`。

**改法**：fire-and-forget —— 后台线程 `asyncio.run(classify_error(...))` 内部捕获
一切异常返回 `{ok: False}`；`_schedule_error_hint_classifications` 无论成败都推进
`_last_error_hint_cursor`，不重试、不卡轮询。

### 4.7 headless 探针的时序坑：设置渲染依赖首次 poll tick

第一版探针在解析期同步 `nav.click()` —— app 的 nav 事件在 DOMContentLoaded 才绑定，
解析期 click 是空操作；且设置渲染依赖首次 500ms poll tick 把 `lastSnap` 填上后
`renderActiveView` 才跑。

**改法**：点击放 `setTimeout(...,600)`，再用 `waitFor(cond, cb, 40)`（300ms × 40）
轮询等 `!!sel && sel.options.length > 0` 再断言 —— 全过。

---

## 5. 解决

### 5.1 error_analyzer.py（新模块）

```python
ERROR_SYSTEM_PROMPT  # 分类 prompt：type 只取 balance/rate_limit/auth/network/
                     #   server/config/other；只输出 JSON {"type","hint"}，hint ≤30字中文
CATEGORY_LABELS = { "balance": "余额不足", "rate_limit": "达到限制", "auth": "鉴权失败",
                    "network": "网络错误", "server": "服务端错误", "config": "配置问题",
                    "other": "其他" }
SAMPLE_ERROR_CONTEXT  # 429 示例，供设置页「测试」按钮
build_context(row)    # requests 一行 → {platform, model, upstream, status_code, error, message}
build_prompt(context) # 平台/模型/上游/状态码/内部错误码/错误消息 → user prompt
parse_verdict(text)   # JSON-tolerant，镜像 advanced_switch._parse_verdict
resolve_target(settings, context)  # 上游定位（见 4.2）
async classify_error(settings, context)  # httpx POST（见下）
```

`classify_error` 的 HTTP 段镜像 `advanced_switch._call_analysis_model`
（advanced_switch.py:183-226）：

```python
client = proxy._get_client(cfg.url)
url = proxy._anthropic_messages_url(cfg.url)   # cfg.url 已含 /v1 时只补 /messages
payload = {"model": model, "max_tokens": 150,
           "messages": [{"role": "user",
                         "content": ERROR_SYSTEM_PROMPT + "\n\n" + build_prompt(context)}]}
style = cfg.effective_auth_style("anthropic")  # bearer → authorization / x-api-key
resp = await client.send(req)
# 非 200 → {ok:False, error:"分析上游 {status}: {text[:200]}"}
# 成功 → 拼 content 里 type=="text" 的块 → parse_verdict
# 返回 {ok:True, type, hint, category} 或 {ok:False, error}；一切异常捕获不抛
```

### 5.2 config.py：Settings 三字段 + save_error_analysis

```python
# v0.113o 报错分析 —— 顶层 error_analysis 配置，存 upstreams.json（config.py:464）
error_analysis_enabled: bool = False
error_analysis_upstream: Optional[str] = None
error_analysis_model: Optional[str] = None

def save_error_analysis(settings, payload) -> (ok, msg):   # config.py:1078
    # _s/_b 帮手提取 clean = {"enabled": _b("enabled"), "upstream": _s("upstream"),
    #                          "model": _s("model")}
    # _mutate 里 data["error_analysis"] = clean
    # 走 save_upstreams_json(settings, _mutate)（镜像 save_advanced_switch config.py:1021）
```

### 5.3 upstreams_file.py：apply_to_settings 读入

```python
ea_raw = raw.get("error_analysis")            # upstreams_file.py:461
if ea_raw is not None:
    if not isinstance(ea_raw, dict):          # 非对象 → 警告忽略（fail-open）
        log.warning(...)
    else:
        settings.error_analysis_enabled = isinstance(v, bool) and v
        settings.error_analysis_upstream = _ea_str("upstream")   # strip 后非空才取
        settings.error_analysis_model = _ea_str("model")
```

### 5.4 gui.py：Api 三个方法 + App 轮询

- **`_upstream_model_catalog(settings)`**（gui.py:309）：从 `get_advanced_switch`
  （gui.py:1418-1437）抽出的模型清单构建 —— 遍历 `PLATFORMS` × `upstreams_for`，
  对 `model` + `allowed_models` 去重成 `{upstream, model, label}`。`get_advanced_switch`
  与 `get_error_analysis` 共用。
- **`get_error_analysis()`**（gui.py:1512）→ `{config: {enabled, upstream, model},
  models: _upstream_model_catalog(s)}`。
- **`set_error_analysis(payload)`**（gui.py:1526）→ `save_error_analysis` +
  `reload_settings()` + `self._app.settings = get_settings()`（镜像 gui.py:1490-1495）。
- **`test_error_analysis()`**（gui.py:1540）→ 后台线程
  `asyncio.run(classify_error(self._app.settings, SAMPLE_ERROR_CONTEXT))`，结果经
  `win.evaluate_js("window.relayErrorAnalysisDone && ...(" + json.dumps(_json_safe(result)) + ")")`
  推前端（镜像 test_upstream）；立即返回 `{"started": True}`。
- **App `__init__`**（gui.py:2392 后）：`_last_error_hint_cursor: int = 0` /
  `_pending_error_hints: list[dict] = []` / `_hint_lock` / `_hint_debounce: dict[tuple, float]`。
- **`_rebuild_snapshot`**（gui.py:2777-2780，alerts 块之后）：`enabled` 才调
  `_schedule_error_hint_classifications()`（异常静默）；随后
  `snapshot["error_hints"] = self._drain_error_hints()`。
- **`_schedule_error_hint_classifications()`**（gui.py:2786）：
  1. 只读查 `SELECT id, ts, platform, model, upstream, status_code, error FROM requests
     WHERE error != '' AND error != 'client_disconnect' AND id > ? ORDER BY id LIMIT 20`。
  2. 游标为 0 → 置为 `MAX(id)` 并 return（**GUI 重启不重放历史**）。
  3. 每条：`(upstream, status or err.split(":")[0])` debounce 60s；未过 → 跳过。
  4. 过 → 记 debounce 时间 + 起后台线程 `_classify_error_hint(ctx)`。
  5. 无条件推进 `_last_error_hint_cursor = max(id)`。
- **`_classify_error_hint(ctx)`**（gui.py:2853）：`asyncio.run(classify_error(...))`；
  成功 → 锁内 append `{id, type, category, hint, platform, upstream, model, ts}` 到
  `_pending_error_hints`；失败 → 丢弃。
- **`_drain_error_hints()`**（gui.py:2877）：锁内取走清空挂起队列。

### 5.5 app.js：桥 + renderSettingsError + renderErrorHints

```js
getErrorAnalysis()    { return this._call("get_error_analysis"); },   // app.js:117
setErrorAnalysis(p)   { return this._call("set_error_analysis", [p]); },
testErrorAnalysis()   { return this._call("test_error_analysis"); },
```

- **`renderActiveView`** 设置分支（app.js:1829）：`renderSettingsError($("card-settings-error-body"))`。
- **`renderSettingsError(body)`**（app.js:5439）：`errorAnalysisRendered` 守卫只渲染一次；
  三行：开关（`#error-analysis-toggle`，`.switch`）+ 分析模型下拉（`#error-analysis-model`）
  + 测试/保存按钮。**模型下拉用 `document.createElement("option")` 建**（NUL 分隔，见 4.4）。
  保存 → `setErrorAnalysis({enabled, upstream, model})` → `alertModal`；测试 → 注册
  `window.relayErrorAnalysisDone` 回调 → `api.testErrorAnalysis()` → `alertModal`
  显示分类结果。
- **`renderErrorHints(snap)`**（app.js:5542）：`seenErrorHintIds` Set 去重；建
  `#error-hints` 容器，`.error-hint.error-hint-{type}` 卡片（标题「类别 · 上游」+
  hint 详情 + × 关闭）；自动 8s 淡出 + `.error-hint-leave` 渐变；镜像
  `renderDispatchAlerts`（app.js:6669-6706）结构，错误色边框。
- **`tick()`**（app.js:6870）：`renderDispatchAlerts(lastSnap)` 后加
  `renderErrorHints(lastSnap)`。

### 5.6 index.html / styles

- **index.html**：nav 子菜单加 `<div class="nav-sub-item" data-settings-sub="error">报错分析</div>`
  （存储管理后、配置前）；settings 视图加
  `<section class="settings-section" data-card="settings-error">` +
  `#card-settings-error-body`（初始「加载中…」）。
- **styles-20260817.css**：`#error-analysis-model { max-width: 280px; }`；
  `#error-hints`（position fixed top:48px right:12px z-index:9000，镜像
  `#dispatch-alerts`）/ `.error-hint` / `.error-hint-title` / `.error-hint-detail` /
  `.error-hint-close` / `.error-hint-leave`；每类型边框色
  （balance/rate_limit 琥珀、auth/server 红、network 蓝、config 紫）。
- 版本 bump：app.js `?v=20260822-19`、styles `?v=20260822-22`（index.html +
  live_panel.html）。

---

## 6. 是否完全按规划

**完全按规划落地**，无自缩范围，有两个探针踩坑 + 一个遗留提醒：

- **A 开关 / B 模型**：设置页「报错分析」区块，开关 + 模型下拉 + 测试/保存。下拉用
  与高级切换**同一模型清单**（抽出 `_upstream_model_catalog` 共用）。
- **C 触发**：GUI 轮询增量扫 relay.db 新错误（`error != ''` 且排除
  `client_disconnect`），后台线程外呼。relay/proxy 路径零改动、零重启 —— 严格遵守
  「严禁 kill 8088」。
- **D 分类 + 提示**：`classify_error` 真实外呼所选上游，`parse_verdict` JSON-tolerant，
  结果以右上角 toast 弹出（类别标签 + 中文 hint）。
- **探针坑 1**：`innerHTML` 里 `\u0000` 被浏览器替换成 U+FFFD → 改用 DOM API 建
  option（代码注释说明）→ 探针 `model-selected=true` 全过。
- **探针坑 2**：设置渲染依赖首次 poll tick + nav 事件 DOMContentLoaded 才绑定 → 点击
  放 setTimeout + waitFor 轮询。
- **遗留**：`advSelect`（app.js:4960）有同样的 innerHTML `\u0000` 潜在 bug —— 本轮
  超范围未改，已记。若该下拉出现「选不中已保存项」，应同样改 DOM API。
- `python -m py_compile` 四后端文件（config / upstreams_file / gui / error_analyzer）
  通过；`node --check app.js` 通过；纯函数单测（build_context / build_prompt 含
  `状态码：429` / parse_verdict 干净/内嵌/无 JSON / CATEGORY_LABELS）通过；headless
  stub-bridge 探针全过（section/toggle/model-options=2/model-selected=true/
  model-label/test-save/toast 全家）；探针文件已清理。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/error_analyzer.py` | **新**：ERROR_SYSTEM_PROMPT + CATEGORY_LABELS + SAMPLE_ERROR_CONTEXT + build_context + build_prompt + parse_verdict + resolve_target + async classify_error |
| `src/relay/config.py` | Settings 增 `error_analysis_enabled` / `error_analysis_upstream` / `error_analysis_model`（config.py:469-471）；`save_error_analysis`（config.py:1078，镜像 save_advanced_switch 的 _mutate 模式） |
| `src/relay/upstreams_file.py` | `apply_to_settings` 读顶层 `error_analysis` 对象 → 三字段（upstreams_file.py:461-476，fail-open） |
| `src/relay/gui.py` | `_upstream_model_catalog`（gui.py:309，get_advanced_switch 抽出共用）+ Api 三方法（get/set/test_error_analysis，gui.py:1512-1568）+ App 轮询（游标/挂起队列/debounce/后台分类线程，gui.py:2392 / 2777-2882） |
| `src/relay/web/app.js` | 3 桥（app.js:117-119）+ renderActiveView 调用（1829）+ `renderSettingsError`（5439，DOM API 建 option）+ `onSaveErrorAnalysis`/`onTestErrorAnalysis`（5506/5528）+ `renderErrorHints`（5542）+ tick 接线（6870）；版本 `?v=20260822-19` |
| `src/relay/web/index.html` | nav 子项 `data-settings-sub="error"`（报错分析）+ `settings-error` section + `#card-settings-error-body`；版本 query bump：styles `?v=20260822-22`、app.js `?v=20260822-19` |
| `src/relay/web/styles-20260817.css` | `#error-analysis-model` 宽度 + `#error-hints` / `.error-hint` / `.error-hint-leave` / 每类型边框色 / `.error-hint-title` / `.error-hint-detail` / `.error-hint-close` |
| `src/relay/web/live_panel.html` | 版本 query bump：styles `?v=20260822-22`（共享样式表缓存） |

### 状态流

- **配置**：设置页 → 高级 → 报错分析：开关「允许小模型分析报错信息」+ 模型下拉
  （`上游 / 模型`，选项与高级切换同源）+ 「测试」/「保存」。保存 → upstreams.json
  顶层 `error_analysis: {enabled, upstream, model}`；GUI 重启后保持（apply_to_settings
  fail-open 读入）。
- **测试**：选好模型点「测试」→ 后台真实外呼（示例 429）→ `alertModal` 显示
  `类别：达到限制 / 提示：请求过于频繁，触发了限流，请稍后再试` 之类。
- **端到端**：启用 + 选模型后，向无 key 上游 / 不存在模型发请求 → 报错落 relay.db →
  约 1s 内 GUI 轮询扫到 → 后台分类 → 主窗右上角出现错误提示 toast（类别标签 +
  中文 hint + × 关闭，8s 自动淡出）；同错误 60s 内去重不重复弹。
- **重启语义**：GUI 重启 → 游标 init = 当前 `MAX(id)` → 历史错误不重放，只分析重启后
  新错误。

### 验证

- `python -m py_compile` config.py / upstreams_file.py / gui.py / error_analyzer.py 通过；
  `node --check app.js` 通过。
- 纯函数单测（usage-stats env）：build_context / build_prompt（含「状态码：429」）/
  parse_verdict（干净 JSON / 内嵌 Markdown / 无 JSON → None）/ CATEGORY_LABELS 全通过。
- headless stub-bridge 探针：`section-exists=true, toggle-checked=true,
  model-options=2, model-selected=true, model-label=true, test-save=true,
  toast-box=true, toast-card=true, toast-title=true, toast-detail=true,
  toast-close=true` —— 全过；探针文件已清理。
- 刷新 GUI：设置页 → 高级 → 报错分析：看到开关 + 模型下拉；开启 + 选模型 + 保存；
  点「测试」看分类结果；故意发错误请求看右上角 toast。
