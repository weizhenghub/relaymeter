# 开发文档

面向在本仓库上继续开发的人（开发者视角）。用户视角看 `README.md`，变更历史看
`PROGRESS.md`，专题设计看 `docs/*`。

## 快速上手

```bash
cd C:/Users/weizheng/PycharmProjects/Usage_stats

# 一次性：装成 editable 包（console scripts 才能用）
python -m pip install -e .

# 开发常用三件套
python main.py                    # 启动 GUI（会自动拉起 8088 中继）
python main.py serve              # 只起 HTTP 中继，不起 GUI
python -m pytest tests/ --ignore tests/test_gui_web.py   # 跑测试（见下）
```

环境是 conda：`C:\Users\weizheng\miniconda3\envs\usage-stats`。项目配置了
`pyproject.toml` 的 `pythonpath = ["."]`，所以 `pytest` 直接能 import `src/` 与
根目录的 PoC 模块。

## 架构总览

```
CLI / Claude Code / Codex / OpenClaw ──► 127.0.0.1:8088
                                          │  uvicorn relay.main:app
                                          │  ├─ routers/*        (HTTP 入口)
                                          │  ├─ proxy.py         (转发 + 用量解析 + 轨迹日志)
                                          │  ├─ wire.py          (跨协议转换)
                                          │  └─ db.py (SQLite WAL)
                                          ▼
                          GUI ── pywebview 壳 (gui.py) ── src/relay/web/
                                (index.html + app.js + styles-20260817.css)
```

两个进程，两套数据通道：

- **中继（8088，FastAPI）**：真实转发流量、解析 usage、写 `relay.db`。无前端。
- **GUI（pywebview 壳）**：宿主 `src/relay/web/` 前端，通过 `pywebview.api.*`
  桥调后端；后端 `gui.py` 里一个后台线程每 0.5s 重建一次 snapshot
  （从 SQLite + 中继 `/live` 聚合），`window.pywebview.api.get_status()` 把它交给
  前端。

GUI 启动时会自动把中继作为**独立子进程**拉起；若发现 8088 已被别的进程占用则
进入只读监控模式（启动/停止按钮禁用，重启按钮会强杀对方进程接管，见 README
v0.62）。

## 目录地图

```
src/relay/
├── main.py               FastAPI app 工厂 + lifespan
├── config.py             Settings + PlatformConfig + 项目根锚定
├── upstreams_file.py     upstreams.json 读写/播种/容错
├── db.py                 aiosqlite + WAL + 迁移
├── proxy.py              流式转发 + 路径归一化 + in-flight 追踪 + _tlog
├── wire.py               协议常量 + cross-wire 转换        (v0.12)
├── advanced_switch.py    实验性 weak/strong 模型路由       (v0.11.18)
├── probe.py              上游协议自动探测 + 连通性测试  (v0.12 / v0.84)
├── quota_monitor.py      5h 配额监控 + 自动切换             (v0.20)
├── quota_client.py       配额 API 客户端（上游侧）         (v0.20)
├── server.py             uvicorn 生命周期 + 端口接管       (v0.9)
├── single_instance.py    单 GUI 实例锁                     (v0.11.5)
├── tray.py               Windows 系统托盘                  (v0.5 / v0.9)
├── parsers/              SSE 用量解析（anthropic/openai）
├── routers/              各协议与 API 路由
├── headers.py            hop-by-hop 头过滤
├── models.py             Pydantic 用量模型
├── cli.py / tui.py       relay-stats / relay-dashboard
├── gui.py                relay-gui（pywebview 壳 + 桥）
└── autostart*.py         Windows HKCU\Run + tray-only 引导
src/relay/web/            GUI 前端（无构建步骤，纯静态三件套）
tests/                    测试套件
docs/                     设计文档（本文件 + wire-dispatch-plan 等）
```

## 前端开发（src/relay/web）

### 无构建步骤

纯静态：`index.html` + `app.js` + `styles-20260817.css`（CSS 文件名带日期，
见下方缓存陷阱）。改完保存即可，GUI 里点
**刷新GUI** 按钮（`btn-reload-gui`，调 `api.reloadGui()`）重新加载页面。

### ⚠️ file:// 缓存陷阱（重要，2026-08-17 实战总结）

GUI 用 pywebview 以 `file://` URI 加载前端，两个坑：

1. **`?v=` query 缓存破坏不可靠**。Chromium 对 `file://` 子资源（CSS/JS）的缓存
   策略不遵循 HTTP 语义，加 `?v=` 未必能强制刷新。**改文件名最可靠**
   （如 `styles.css` → `styles-20260817.css`），但更常见的做法是改完 CSS 直接点
   刷新按钮 + 重启 GUI。
2. **pywebview 每次启动用一个全新的临时 user-data-dir**（`%TEMP%\tmpXXXX\`），
   所以正常启动**不会**复用旧缓存——反过来说，没有持久缓存可清。重启 GUI 必然
   加载磁盘上的最新前端文件。

### 改动前端的三条验证路径

1. **GUI 内点「刷新GUI」** —— 最快，前端改完即生效（后端代码改动不适用）。
2. **重启 GUI**（`python main.py`）—— 前端 + 后端桥代码都生效。
3. **浏览器直接打开** `src/relay/web/index.html`（`file://`）—— 无桥（`pywebview`
   为 null），`window.pywebview.api.*` 调用全部静默跳过，适合纯 CSS/布局调试。

### 排查"改了 CSS 但界面没变"的定位流程

用 WebView2 的 DevTools Protocol 直读**浏览器实际计算的样式**，别猜：

```bash
# 1. 以带调试端口的方式启动 GUI
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9222"
python main.py
# 2. 列出页面拿到 webSocketDebuggerUrl
curl http://127.0.0.1:9222/json/list
# 3. 用 python websockets 连上，Runtime.evaluate 执行 getComputedStyle(...) 检查
```

（临时脚本见 `C:\Users\weizheng\AppData\Local\Temp\opencode\`，用完即删。）

### 真实案例：上游状态行"又窄又扁"

症状：15 个上游全挤在 ~17px 的行里、统计数字看不见、名字截断。排查路径：
改布局方向 → 改 max-height → 换文件名 → 换 user-data-dir → 换调试端口——全无效。

真相：`#card-upstream-body` 是 **flex column** 容器且 `max-height: 280px`，里面的
`.upstream-row` 是 flex item，默认 `flex-shrink: 1`，15 行总高远超 280px 时被
**整体压缩**到每行 17px。修复是一行 CSS：`.upstream-row { flex: none; }`。

教训：flex 容器里固定高度 + 子项可收缩时，行会被压扁。遇到"改 CSS 不生效"，
先用 DevTools Protocol 验证 `getComputedStyle` 到底应用了什么，再判断是缓存还是
逻辑问题。

### 前端排序规则（v0.92 修正）

`renderUpstream`（app.js）里 `entries` 在渲染前排序：

1. 当前激活（`active_per_platform`，目前只认 anthropic 平台）置顶；
2. 其余全部按累计 tokens `total_tokens` 降序（消耗量大在前）。

v0.80 曾加过「最近调用单席」（`last_ts` 最大者独立占位、active 不占此位）——但用户
明确要的是「active → 按消耗量降序」两级排序，单席会把 token 用量大的上游压下去
（教训：先问清排序意图再动手，别自作主张加第三级）。行重排复用 v0.77 的
`applyUpstreamRowOrder`（短路判断，只在顺序变化时重排，不每 tick detach/append）。

### 切换跟随问题（v0.92）

症状：左下角切换器切换模型后，只有切换器自身变了，用量条目 /「上游状态」置顶绿点 /
「上游」页 active / 实际模型都不跟随，且不重启 8088 中继。

- **根因**：`index.html` 里 `app.js?v=20260816-105` 缓存戳陈旧——磁盘 app.js 已迭代到
  v0.91（`renderSidebar` 的 change handler → `applyUpstreamDirect` → `restartRelay` 链路
  早已就位），版本号没 bump，WebView2 一直加载早期缓存 JS（没有切换/重启逻辑）。
  教训：改 `app.js` 后必须同步推进 `index.html` 的 `app.js?v=` 缓存戳（`styles-*.css`
  同理），规则见 PROGRESS.md。
- **验证**：`apply_upstream`（POST `/api/upstreams/{platform}/select`）+ `restart_server`
  （`_takeover_external_listener` 杀外部监听者 → `ServerProcess.start()` Popen 新 child）
  链路 E2E 实测通过：切换后 active 正确落盘、重启后中继健康、`get_status` 回显一致。
  注意 `ServerProcess` child 依赖 editable 安装（`pip install -e .`）让 `relay` 可导入，
  否则 `python -m uvicorn relay.main:app` 会因找不到模块直接退。

### 切换跟随硬化（v0.93）

v0.92 修了缓存戳陈旧，但**修了之后用户报告"切了还是只切换器变了，PID 没变"**——
排查发现加固做得不够。

#### 三个隐性缺陷

1. **`applyUpstreamDirect` 成功判定用 `res.ok === false`**：relay 端 router 返回
   body 没 `"ok"` 字段（`{"platform": ..., "selected": _public(cfg)}`），`undefined
   === false` 是 `false` 走不到失败分支——这是误打误撞能跑通，但任何**未来
   字段命名调整**（例如改成 `"success": true`）都会让它静默走不到
   `restartRelay()`。修：改成 `res.error` 存在 → 视为失败。

2. **`restartRelay()` 之后只清 `lastStatusSig`、靠 500ms tick 顺带渲染**：
   tick 偶尔被节流/跳过 → 用户看到「切换器变了 / sidebar 没变 / 状态条没动」
   ——实际是重启成功了，但渲染没及时走。修：restart 后**主动** `Promise.all`
   拉 snapshot + status 并全量 `renderAll` + `renderSidebar` +
   `renderUpstreamsView` + `renderActiveView`，并清掉 `lastSidebarSig` /
   `lastUpstreamsSig` / `lastActivePerPlatform` / `autoswitchToasts` /
   `_quotaBarsCache`。

3. **`gui.py:restart_server` 三个 `try/except: pass` silent swallow**：restart
   失败时上层拿到 `{"ok": ...}` 但其实是 fallback —— 没有任何错误日志，
   用户看到「PID 没变」却完全不可追溯。修：改成 `log.exception(...)` +
   `takeover_error = f"start_failed: {exc}"` 让 JS 端能从 `status["error"]`
   拿到原因并 `alert`。

#### 即时反馈

v0.93 在 `<select>` change handler 里**立刻** `setText("status-label",
"切换中…")` —— 之前的版本完全靠 `restartRelay` 内部的 setText，但若
`isRestarting` 被卡在 `true`（早期某次 restart 异常 finally 没跑），或
`apply_upstream` 链路早退（router 不存在、404），用户**完全看不到动静**。
提前 setText 让任何静默失败都至少有个视觉锚点。

#### 经验沉淀

- **bridge 调用前的即时反馈**比事后 UI 更重要：HTTP 调用 50ms~几秒期间用户
  看不到动静会以为"卡了"。给所有 mutating 桥调用加前置"切换中…" / "写入中…"。
- **silent `except: pass` 是技术债**：调试时找不到任何线索。`log.exception` +
  错误传播给上层至少能 `alert`，比静默好 100 倍。
- **sig 缓存的失效边界**：清 sig 要按视图边界清全，不能假设"清一个 tick 会
  把其它视图也带过去"。显式列清单（sidebar / upstreams / active / autoswitch
  toast / quota bars）。

### 上游切换 HTTP 400 修复（v0.94）

v0.93 修了切换跟随硬化，用户**重启后**立刻又报「切换失败:http400:bad request」——
alert 里只能看到 urllib 默认的 `Bad Request`，看不到 FastAPI 的 `{"detail": ...}`
真实原因。两条独立的 bug 缠在一起：

#### A. `_mutate` 没兼容 v0.12 扁平格式

`config.py:set_upstream_model._mutate`（行 1045 起）和 `set_upstream_default_model._mutate`
（行 989 起）**只写了旧 per-platform 格式**：

```python
def _mutate(data: dict) -> None:
    body = data.setdefault(platform, {})         # ← 旧格式 {"anthropic": {...}}
    body.setdefault("upstreams", [])
    for entry in body["upstreams"]:
        ...
```

但用户的 `upstreams.json` 在 v0.12 迁移后已经是**扁平格式**：
`{"active": "opencode-go", "upstreams": [...]}`. 命中扁平时 `data["anthropic"]`
不存在 → `setdefault` 创建一个**空的** `{"anthropic": {"upstreams": []}}` 子树 →
循环空 → `raise KeyError(...)` → `save_upstreams_json` 的 `except Exception` 包成
"未预期错误" → HTTP 400。

参考 `apply_quota_edit`（行 822）、`add_upstream`（行 1195）、`remove_upstream`（行 1282）
已经在 `_mutate` 开头有这段 v0.12 兼容块：

```python
if not isinstance(data.get("upstreams"), list):
    merged: list = []
    for plat in PLATFORMS:
        section = data.get(plat)
        if isinstance(section, dict) and isinstance(section.get("upstreams"), list):
            merged.extend(section["upstreams"])
            data.pop(plat, None)
    data["upstreams"] = merged
    data.setdefault("active", "")
```

v0.12 迁时**漏掉了 set_upstream_model / set_upstream_default_model 两个**，导致
整 v0.8x~v0.9x 期间，凡是扁平格式文件 + 走这两个 API → 必然 400。

修：两个 `_mutate` 加上同款 5 行兼容前缀，并把 KeyError 文案去掉平台前缀
（`f"找不到名为 {{name!r}} 的上游"`，扁平后无 platform 维度）。

#### B. `urllib.HTTPError` 不读 body

`gui.py:apply_upstream` 行 679 的旧 except 块 `except urllib.error.HTTPError as exc:`
只用 `exc.reason`（恒为 `Bad Request`），**根本不读** FastAPI 返回的
`{"detail": "..."}` 响应体。改成：

```python
except urllib.error.HTTPError as exc:
    body = ""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        pass
    return {"platform": platform, "name": name, "ok": False,
            "error": f"HTTP{exc.code}: {exc.reason or 'request failed'} | {body}".strip()}
```

后续任何 HTTP 4xx 都能在 alert 里看到真实原因。

#### 经验沉淀

- **格式迁移必须枚举所有 mutator**：`save_upstreams_json` 的**读路径**（验证循环）
  支持两种格式，**写路径**（`_mutate` 闭包）各自维护——加新 mutator 时一定要按
  `apply_quota_edit` / `add_upstream` 的模板加前缀兼容块，不能只复制主体逻辑。
- **HTTP 错误代理要把响应体透传**：`urllib.HTTPError.strerror` 只给 reason phrase，
  对 FastAPI 之类的 JSON-错误响应是信息黑洞。任何走 `urlopen` 的 HTTP 调用都要
  `exc.read()` 一下，body 比 reason 有用 100 倍。
- **回归测试覆盖两个格式**：`tests/test_upstreams_file.py` 末尾新增 4 条 v0.12 扁平
  fixture —— `set_upstream_model_writes_model_on_flat_file` /
  `set_upstream_model_clear_on_flat_file` / `set_upstream_default_model_writes_on_flat_file`
  / `mutator_migrates_legacy_to_flat_on_write`，覆盖"写 model / 清 model / 写 default_model /
  legacy→flat 迁移"四个分支，下次再加 mutator 时按这四条模板抄。

### nav「实时」波纹对比度（v0.92）

`.nav-item[data-view="live"].has-stream::after` 的亮带原来复用 `var(--button-primary)`：
暗夜黑主题里该 token 被压成 `#3a3a3c`（深灰玻璃面），扫过 `#2c2c2e` 面板几乎不可见
（≈1.2:1）。新增专用 token `--nav-ripple`：light/day 保持原色，dark 用 `#f5f5f7`
（≈11:1）。教训：主题 token 是「语义化」的，动画高亮不要顺手复用会被主题压暗的色板。

### 上游名称旁 token 数字（v0.82）

按次数计费（`billing_unit !== "token"`）的上游，名称旁显示累计 token 消耗的**纯数字**
（无任何文字标签）。布局：`.upstream-name` 改 flex 行，`.upstream-name-text` `flex:1`
可截断省略，`.upstream-name-tokens` `flex:none` 不收缩——名称过长时优先保住数字。
按 token 计费的上游不受影响（它们第二行本就有「累计 tokens」）。

### sidebar 选择器与卡片不匹配（v0.83）

症状：重启 GUI/8088 后，左下角「上游」选择器回显的上游和「上游状态」卡片置顶的
active 对不上（选择器回退到下拉第一项）。根因：`renderSidebar` 的 `buildOpts` 给
option 打 `selected` 的判定是 `c.name === activeName && primaryModel === m`；当 active
上游 `model` 为 null（未指定，用第一个 allowed）且有多个 `allowed_models` 时，该判定
永远 false → 无 option 被选中 → 浏览器回退选第一项。修复：active 且 model 未指定时
默认选中第一个 `allowed_models` 对应的 option。

## 统计页（v0.99）

左侧菜单新增的「统计」视图，把既有 `snapshot.by_*` 切片放大 + 加两段直查中继 DB
的聚合（时段聚合 / 每日趋势）。

### 双入口：HTTP 路由 + GUI 桥

为了对调试 / 外部脚本同样友好，两条入口都做了，**语义、口径、错误结构严格对齐**：

| 入口 | 端点 / 方法 | 用途 |
|---|---|---|
| HTTP | `GET /api/stats/aggregate?range=&dim=&top=` | 外部脚本、命令行调试 |
| HTTP | `GET /api/stats/daily?days=` | 同上 |
| GUI 桥 | `api.statsAggregate(dim, range, top)` | 前端按钮 / 视图 |
| GUI 桥 | `api.statsDaily(days)` | 同上 |

pywebview 用 `file://` 加载 `index.html`，`fetch()` 拿不到中继，所以前端必须走
桥。桥直接调 `tui.fetch_aggregate_by_dim` / `tui.fetch_daily`，路径短、不需要 cors
配置，与既有 `fetch_requests` / `fetch_conversation` 模式一致。

两条路径都遵守：HTTP 仍 200，错误用 `{error, valid: [...]}` 结构体返回。前端
统一按 `body.error` 字段分支处理，**不引入 4xx/5xx 渲染分支**。

### 「total 不被 top 截断」的小坑

早期实现让 `tui.fetch_aggregate_by_dim` 直接按 top 截前 N 行，路由算 total 时
只对截过的 dict 求和，得到「top=2 总请求数 = 2」这种错位语义。修法：

```
tui.fetch_aggregate_by_dim → 永远全量返回 + total_tokens DESC 排序
route / bridge → 拿全量算 total（窗口内全部维度合计）
            → 截前 top 行（只控制表格显示，total 不受影响）
```

写测试时也容易踩：`test_aggregate_top_n_caps_rows` 验证 `len(rows) == top` 但
`total.requests == 所有维度合计`，二者解耦。

### UTC 日桶

`fetch_daily` 用 `(CAST(ts AS INT) / 86400) * 86400` 做桶对齐（UTC 边界），
显式选了简化方案：用户深夜跨天的请求按 UTC 切分（"我的 0:30 算昨天"），但避免了
tzinfo 处理复杂化与夏令时 23/25 小时歧义。如果以后接本机时区偏好，桶对齐改成
`localtime(ts)` 一行的事。

### 重绘策略：进入视图主动拉 + 空闲 tick 不重绘

参考历史页（`resetHistory` → `loadHistoryPage`）的设计：进入视图时主动从桥拉一次，
数据落进 `statsState` 后保持；500ms 轮询不刷新统计页（统计类卡片一秒一刷没意义），
只通过 `renderStatsQuota` / `renderStatsCross` 直接消费 `snap.by_upstream` /
`snap.by_upstream_model`（这些已经在 500ms tick 链路上了）。这样切回主窗 1 秒内
配额条 / 对照表的更新是即时的，但聚合 / 每日不参与无意义重建。

### 颜色与进度条全部走 CSS 变量

`stats-quota-bar-fill[data-warn=warn|critical|exhausted]` 三档色阶用线性渐变
（`#eab308→#ca8a04` / `#ef4444→#b91c1c`），与主窗已有 token-bar / 释放波纹色板
一致；三主题自动跟随 `--accent` / `--hairline` 等现有变量，无新增主题分支。

## 后端开发

### ⚠ 思考挡位无统一标准，须逐上游声明 thinking_options（重要 · 2026-08-22 调研总结）

各厂商「思考挡位（thinking level）」**没有标准**——字段名、取值集合、语义三层都不同，
连「OpenAI 兼容」内部都不统一。这是所有思考相关改动的底层约束，必须遵守：

- **字段名不同**：Anthropic 用 `thinking`(budget)；OpenAI 用 `reasoning_effort` /
  `reasoning.effort`；Gemini 用 `thinkingConfig`；Kimi K2 用 `thinking`(开/关)、K3 用
  `reasoning_effort`；GLM 用 `thinking`(enabled/disabled)。
- **取值集合不同**：OpenAI 全集 `none/minimal/low/medium/high/xhigh/max`；DeepSeek 只
  `low/high/max`（且把 `medium`/`xhigh` 静默并成 `high`）；Qwen 只
  `off/low/medium/xhigh`（**没有 high**，xhigh 是默认）；GLM 只有开/关无分级。
- **语义不同**：Anthropic 是数字 token 预算；Qwen(sglang) 是往 system 注入「思考契约」
  文本，不是预算；GLM 是开关节点。

**后果（中继必须怎么做）**：

1. **不能靠 URL 推断**某上游支持哪些挡位——第三方网关方言差异连 DeepSeek 官方 harness
   社区都靠「挡位自动测定 + 方言自动修复」插件（dsh-gateway-presets #564）解决。
2. **必须逐上游声明 `thinking_options`**（`upstreams.json` 手工填），中继只做透传/翻译，
   **不替上游做语义判断、不做「并档」**（否则 xhigh/minimal 等真实挡位会被悄悄丢成 high/low）。
3. **词表要够宽**：`_KNOWN_THINKING` 与 `_reasoning_options` 必须收下
   `off/none/minimal/low/medium/high/xhigh/max/enabled`，否则声明的挡位被静默丢弃、
   opencode 也看不到。

> 逐家调研证据见 `docs/thinking-level.md` §8「各厂商思考挡位真实调研」。

### snapshot 数据流（GUI 前端的数据来源）

`gui.py:App._rebuild_snapshot()` 每 0.5s 重建 `snapshot` 字典，字段约定见
`app.js` 顶部注释。关键字段：

- `by_upstream`: `{name: {counts: {5h/week/month}, 5h_release_text, last_ts,
  total_tokens, billing_unit, ...}}` —— 上游状态卡片 + 自动排序的依据。
- `by_upstream_model`: 按 (upstream, model) 拆分的调用详情。
- `active_per_platform`: `{platform: name}`。
- `recent` / `live`: 最近请求 + 进行中请求。

新增展示字段的链路：`tui.py` 查询函数 → `gui.py` snapshot 组装 → `app.js` 消费。
注意 `by_upstream` 的形状由 `fetch_by_upstream` / `fetch_by_upstream_with_costs`
定义，测试依赖它的字段契约，新增字段要保持向后兼容（测试里有对
`fetch_by_upstream` 形状的断言）。

### 排序用字段（v0.77）

- `last_ts`：该上游最后一次请求的 unix 时间戳，`fetch_by_upstream` 的 SQL 里
  `MAX(ts) AS last_ts` 得到，零行上游为 `None`。
- `total_tokens`：全时段累计 token 总和（input+output+cache），
  `fetch_total_tokens_by_upstream` 对所有上游返回（v0.77 起不再只查 token 计费
  上游）。

### 上游连通性测试（v0.84–v0.86）

「新建上游」表单里的「连通性测试」按钮，按**当前表单配置**（wire / 鉴权头 / 模型）
构造一条 `ping` 消息真实发到上游，验证配置能否真正通。与「测试」按钮（自动探测，
试两种鉴权头判 wire）互补：探测负责猜协议，连通性测试负责按用户填的配置原样发。

三层分工：

- **`probe.py:connectivity_test()`**：按给定 wire 决定端点 + 请求体
  （anthropic→`/v1/messages`，openai-chat→`/v1/chat/completions`，
  openai-responses→`/v1/responses`），用给定 auth_style + key 原样发，不猜。
- **`probe.py:_extract_reply()`**：判定成功与否**不看 HTTP 2xx**——必须从响应体
  解析出模型的文本回复才算通（v0.85）。按三种协议分别解析：Anthropic
  `content[0].text`、OpenAI Chat `choices[0].message.content`（兼容多模态数组）、
  Responses `output[0].content[0].text` / `output_text`。2xx 但 body 是错误对象
  或空文本，判失败并打印错误 message。非 2xx 附状态码语义提示
  （401 鉴权 / 402 余额 / 404 端点 / 429 限流等）。
- **`gui.py:connectivity_test()`** 桥方法：后台线程跑（复用 `test_upstream` 的
  `evaluate_js` 推送模式），日志经 `window.relayProbeLog` 实时推，结束推
  `window.relayProbeDone`。**注意**：连通性测试在 GUI 进程内直连上游，**不经过
  8088 中继**。

前端（`app.js`）：按钮收集表单 `create-wire`/`create-auth-style`/第一个 chip 的
model，调 `api.connectivityTest`；`relayProbeDone` 用模块级 `_activeProbeBtn` 区分
两个测试按钮各自的完成恢复。v0.86 起日志末尾附「──── 原始请求 ────」块
（方法/URL/请求头/JSON body），evidence 最后一条由后端拼接。

两个坑（教训）：

1. **URL 拼接要跟真实转发一致**。`join_endpoint` 只处理 base 以 `/v1` 结尾的情况；
   若上游 URL 已是完整端点（如 `.../v1/chat/completions`），会拼成双重路径。真实
   转发的 URL 归一化在 `proxy.py:_normalize_api_path`（v0.12.2），连通性测试还没
   复用，填完整端点 URL 会 404——见 PROGRESS.md 待查项。**v0.95 已把 `join_endpoint`
   通用化**（见下「探测鲁棒性三连」第 1 条）。
2. **别用 2xx 当「连通」**。有些上游 2xx 但 body 是错误对象（例如 anthropic 的
   `{"type":"error","error":{...}}`），必须解析回复文本才算数（v0.85 修正）。
3. **⚠ 思考型上游：正文 text 块不一定在 content[0]**（v0.96.1，DeepSeek V4 官方
   Anthropic 端点实测）。DeepSeek V4 响应开头是 `{"type":"thinking",...}` 块，
   正文 text 块排在后；`_extract_reply` 原来只读 `content[0]`，2xx 也判「没解析到
   回复」。修成**遍历所有 content 块取第一个带 text 的**（自然跳过 thinking 块）。
   同类上游（任何默认开思考、响应先 thinking 后 text 的）都吃这个坑。
4. **⚠ 探测 max_tokens 别用 1**（同上实测）。思考模型把 1 个 token 全烧在思考上，
   正文 text 块根本不出现（max_tokens=1/16/64 实测都只有 thinking，256 才有
   "pong"）。连通性测试三个 wire 统一 `_CONNECT_MAX_TOKENS=512`。

### ⚠ 客户端路径鲁棒性：base_url 后缀与协议分发（2026-08-21 实战总结）

> **这是路由层的核心防线** —— 客户端拼出来的路径千奇百怪，中继必须
> 在不加任何限制的前提下识别协议。漏一个常见拼法 = 整个上游类客户端
> 不可用。所有改动集中在 `src/relay/proxy.py` 的 `_ANTHROPIC_PATH_SUFFIXES`
> / `_OPENAI_CHAT_PATH_SUFFIXES` / `_OPENAI_RESPONSES_PATH_SUFFIXES` /
> `_infer_client_wire_from_path` / `_sniff_client_wire` 五处。

**为什么需要这套机制**

客户端 SDK 在拼 `base_url` 后的后缀时五花八门（实测过的）：

- Anthropic 官方 SDK：`/v1/messages`（带 `/v1`）
- OpenAI 官方 SDK：`/v1/chat/completions` 或 `/v1/responses`（带 `/v1`）
- OpenCode 桌面版的 `@ai-sdk/openai-compatible` 4.0.23：**裸** `/chat/completions`（不带 `/v1`）
- OpenAI Responses 简写：`{"input": "hi"}`（字符串，不是数组）
- Anthropic / OpenAI 在路径上加各种前缀：`/anthropic/v1/messages`、`/openai/v1/chat/completions`
- 客户端 base URL 误配双斜杠：`http://...:8088//v1/messages` → 实际 path `//v1/messages`
- 客户端 SDK 不补 `/v1`：`/messages`、`/count_tokens`、`/completions`、`/embeddings`、`/responses`
- 客户端错配 prefix：`/anthropic/v1/chat/completions`（Anthropic SDK 实际发 OpenAI 协议）

**三层兜底设计**（顺序敏感）

1. **路由 catchall**（`routers/{anthropic,openai,root}.py`）—— `/anthropic/{path:path}`、`/openai/{path:path}`、`/{path:path}` 三个 catchall 路由，确保**任何非空路径都不会 404**。prefix 路由先注册，根 catchall 最后注册，按 FastAPI 路由匹配顺序兜底。
2. **路径末段推断**（`proxy.py:_infer_client_wire_from_path`）—— 提取最后一段路径与已知端点 suffix 列表匹配。按 `platform_hint`（prefix 推断）决定 bucket 检查顺序：anthropic hint 先查 `_ANTHROPIC_PATH_SUFFIXES`，否则按"重名端点（`/v1/files`、`/v1/models`）按 hint 消歧"原则。
3. **body 嗅探**（`proxy.py:_sniff_client_wire`）—— 路径推断不出来时读 body top-level 字段：
   - Anthropic：`system` + `messages` 列表
   - OpenAI Responses：`input` 是字符串或数组
   - OpenAI Chat：`messages` 数组首项含 `role` 字段

3 层独立运行，任何一层命中即返回，**互不阻塞**。根 catchall 默认 `platform_hint="anthropic"`（与客户端默认行为一致），按 wire→platform 映射决定走哪个 bucket。

**4 轮鲁棒性审计与修复（2026-08-21）**

每轮都是「实测发现 bug → 修 → pytest 回归 → 端到端验收」闭环：

| 轮次 | 触发场景 | 修复 | 文件:行 |
|---|---|---|---|
| 1 | `POST /responses` + `{"input":"hi"}` → 400 无法识别 | `_sniff_client_wire` 接受 `(str, list)`；`_OPENAI_RESPONSES_PATH_SUFFIXES` 加裸 `/responses` | `proxy.py:1742`、`proxy.py:1630-1637` |
| 2 | `POST //v1/messages` 双/三斜杠 → 错认 openai | `_infer_client_wire_from_path` 开头折叠连续斜杠（`"/".join(filter)`） | `proxy.py:1672-1679` |
| 3 | `POST /messages`、`/count_tokens`、`/messages/batches` 裸 → 错认 openai | `_ANTHROPIC_PATH_SUFFIXES` 加 3 条裸路径 | `proxy.py:1629-1637` |
| 4 | `POST /chat/completions`、`/completions`、`/embeddings`、`/models` 等裸 → 走嗅探兜底 | `_OPENAI_CHAT_PATH_SUFFIXES` 加 14 条裸路径 | `proxy.py:1657-1674` |

**当前路径 suffix 完整清单**

`_ANTHROPIC_PATH_SUFFIXES`（9 条）：`/v1/messages`、`/v1/messages/batches`、`/v1/messages/count_tokens`、`/v1/files`、`/v1/organizations`、`/complete`、`/messages`（裸）、`/count_tokens`（裸）、`/messages/batches`（裸）。

`_OPENAI_RESPONSES_PATH_SUFFIXES`（3 条）：`/v1/responses`、`/v1/responses/input_items`、`/responses`（裸）。

`_OPENAI_CHAT_PATH_SUFFIXES`（26 条）：`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/models`、`/v1/batches`、`/v1/files`、`/v1/moderations`、3×`/v1/audio/*`、3×`/v1/images/*`、`/v1/fine_tuning/jobs`、`/v1/assistants`、`/v1/threads`，以及对应的 14 条裸版本。

**关键约束（⚠）**

- **bucket 检查顺序保证不冲突**：`_ANTHROPIC_PATH_SUFFIXES` 加裸 `/messages` 时，`_OPENAI_CHAT_PATH_SUFFIXES` 列表里**只有** `/v1/messages` 没有裸 `/messages`，所以 anthropic bucket 先查先赢，不存在两边都命中时的歧义。
- **`/v1/files` 是已知重名端点**：Anthropic 和 OpenAI 都有 `/v1/files` API，注释明确写"按 `platform_hint` 消歧"——anthropic hint 归 anthropic-messages，openai hint 归 openai-chat。
- **裸 `/files` 始终归 openai**：因为 `_ANTHROPIC_PATH_SUFFIXES` 只有 `/v1/files`，没有对应的裸版本。这是一个**遗留不一致**（历史设计），非本次改动引入。如要彻底对称需要再加一条 `/files` 到 `_ANTHROPIC_PATH_SUFFIXES`，但当前实测无客户端受此影响。
- **多斜杠折叠只动开头/中间连续斜杠，不动尾部**：`///v1/messages/` → `/v1/messages/`（末尾 `/` 由 `.rstrip("/")` 处理）；`///v1//messages` → `/v1/messages`（中间折叠）。split + filter + join 保持合法路径语义。
- **OpenAI Responses `input` 接受 string 或 list**：API 规范允许 `input: "hi"` 字符串简写（等效于 `input: [{"role":"user","content":"hi"}]`）。嗅探必须两种都识别。

**调试经验沉淀**

1. **永远用 trace log 验证 platform 分发**：curl 测试时打开 `relay_trace.log`，搜 `REQ ENTER` 看 `platform=` 字段。HTTP 502 的 `relay_unknown_key` 是 dispatcher 找不到匹配 key**已经进入 relay 层**，**不是路径识别问题**。
2. **真 key 不一定能找到**：本地测试用 active 上游的 key 拼到对路径才能完整跑通，但 trace 验证用任何非空 body 都够（嗅探会识别协议）。
3. **OpenAPI schema 不渲染 `/{path:path}`**：Starlette 的 catchall 路径不会出现在 `/openapi.json` 里。不要被这个误导以为"根 catchall 没注册"——实际跑一个 `GET /` 看是否返回 400 而不是 404 就知道是否生效。
4. **重启才能验证新代码**：中继是 uvicorn 进程，没热重载。改完代码必须点 GUI「重启」按钮（或 `python main.py serve`），否则进程跑的还是旧版。**改文件时间和进程启动时间对比** 是验证重启是否生效的最快方式。
5. **pytest 累计 50/50 全绿**：5 条新增回归测试（`test_sniff_responses_input_string_form`、`test_infer_client_wire_responses_bare_path`、`test_infer_client_wire_collapses_consecutive_slashes`、`test_infer_client_wire_anthropic_bare_paths`、`test_infer_client_wire_openai_chat_bare_paths`）锁住每个修复点，防止未来误改回归。
6. **passthrough 模式完全隔离**：`grep passthrough` 在三个 router 文件和 `proxy.py` 里**零命中**。passthrough 是纯 ASGI 中间件（`passthrough/middleware.py`）+ 独立 `passthrough.db`，与转换模式物理隔离。本次所有修复都在转换模式范围内，**不涉及 passthrough**。

### 上游探测鲁棒性三连（v0.95 · join_endpoint 通用化 / P2 真实模型名 / 协议纠错）

三个独立改进，合起来让「新建上游」对**协议变体多的中转站**（DeepSeek V4、
各种 Anthropic/OpenAI 兼容网关）一次配通。触发场景：用户创建 DeepSeek 官方
上游，URL `https://api.deepseek.com/anthropic`（官方 Anthropic base_url）+
协议 anthropic-messages + 模型 `deepseek-v4-flash`，但连通性测试一直
「HTTP 400 没解析到模型回复」。三个根因各被一条修掉。

#### 1. `join_endpoint` 通用化：版本段感知拼接（config.py:115）

原实现只识别 base 以 `/v1` 结尾（剥 endpoint 自带 `/v1`），其它 base 形态一律
原样拼。通用规则改为：

- **base 末段是版本段**（`v1` / `v1beta` / `v2`…，可带 `api/` 前缀，如
  `opencode.ai/zen/go/v1`、`generativelanguage.googleapis.com/v1beta`、
  `openrouter.ai/api/v1`）→ 版本信息已由 base 给出，endpoint 自带的 `/vN` 前缀
  剥掉（`/v1/messages` → `/messages`）。
- **base 末段是命名空间段**（如 DeepSeek Anthropic 的 `/anthropic`、OpenAI-family
  的 `/openai`）→ 命名空间不是版本段，endpoint 的 `/vN` **保留**，结果
  `.../anthropic/v1/messages` —— 与 Anthropic SDK 的 `base_url + "/v1/messages"`
  约定一致。
- **base 是根地址** → 原样拼 `/v1/messages`。

实现：两条正则 `_BASE_VERSION_SEG`（`^v\d+[a-z]*$`，大小写不敏感，判断 base 末
段）+ `_ENDPOINT_VERSION_PREFIX`（`^/(v\d+[a-z]*)/`，匹配 endpoint 开头版本段）。
覆盖 `probe.py` 全部 6 处调用 + `proxy.py:1946` 真实转发路径，一处改全生效。

**注意**：DeepSeek 的 `/anthropic` 拼接改前改后**都正确**（`/anthropic/v1/messages`
正是 SDK 约定）——它真正的坑在第 2 条。

#### 2. P2：探测用真实模型名（probe.py + gui.py + routers/api.py + app.js）

DeepSeek V4 官方**严格校验模型名**：占位名 `relay-probe` / `relay-connect-test`
一律 400。而 `probe_upstream` 在 `GET /v1/models` 失败（DeepSeek 的 `/anthropic`
命名空间下没有 models 端点 → 404 → models 空）时，`model` 落到占位名 → 400 →
`key_valid=False` → 连通性误判「key 无效」。

修复：`probe_upstream` 新增 `model: Optional[str] = None` 参数，最小请求优先用
调用方给的真实模型名，其次 `models[0]`，最后才占位名：

```python
model = model or (models[0] if models else "relay-probe")
```

透传链路（三层全通，UI「测试」按钮 + 中继 `/api/probe_upstream` 都带上）：

| 层 | 位置 | 改动 |
|---|---|---|
| 前端 | `app.js:testUpstream(url, key, model)` + 测试按钮点击处取 chips 模型 | 与「连通性测试」同款取法 |
| GUI 桥 | `gui.py:test_upstream` / `gui.py:probe_upstream` | 加 model 参数，透传 |
| 中继端点 | `routers/api.py:/api/probe_upstream` | `_Body.model` 透传 |

#### 3. 协议纠错：连通性测试失败后反向探测（probe.py:_suggest_after_fail）

用户协议选错（anthropic ↔ openai 颠倒）时，`connectivity_test` 按错协议发 →
失败（400/404/405/422 或 2xx-空体）→ **自动追加一次 `probe_upstream` 反向探测**
（用真实模型名），对比探测出的 wire / auth_style 与用户配置，输出明确纠错建议：

- 协议不同 → `⚠ 反向探测发现该端点的实际协议是 {pw}（你配置的是 {cw}）。建议把
  协议改成 {pw}{（可选）鉴权方式改成 {pa}} 后重试。`
- 协议同、鉴权不同 → `⚠ 协议匹配，但鉴权方式不对：实际是 {pa}（你配置的是 {cs}）。`
- 协议同、鉴权同、key 有效 → `✓ 反向探测用真实模型名验证通过 —— 协议与 key 均
  正确；若连通性仍失败，检查模型名是否正确。`（定位到模型名这一层）
- 协议同、key 无效 → `⚠ 协议匹配，但 key 未通过验证 —— 请检查 API key。`

**evidence 顺序约束**：原始请求文本（`───── 原始请求 ─────` 块）必须保持 evidence
**最后一条** —— 前端 `relayProbeDone` 取 `ev[ev.length-1]` 判断 `includes("原始
请求")` 才打印。反向探测日志（经 `emit=log` 实时进 evidence）在它之前。重构为
`fail_return()` 辅助函数统一「先反向探测、再 append 原始请求、再 return」。

best-effort：反向探测失败（网络 / 异常）不抛、不改返回结果，只追加一条日志。
连通性测试总耗时 = 原测试（≤15s）+ 反向探测（probe_upstream ≤8s），后台线程跑、
日志实时推，不阻塞 UI。

### 流式结束：message_stop 后主动断流（v0.87）

**现象**：请求流已经结束（客户端已收完回复），但实时面板还显示它在 STREAMING，
挂着不动（assistant_text 空、无字节推进），直到 sweeper `STREAMING_STALE_AFTER`
（90s）强清才变 done。

**根因**：SSE（`text/event-stream`）是**长连接**，服务端发完事件流后**没有义务
关闭连接**——"回复结束"由业务事件声明（Anthropic 的 `message_stop`、OpenAI 的
`[DONE]`），不是由传输层的连接关闭声明。opencode.ai/zen/go 这类网关在
`message_stop` 后保持连接打开（keep-alive 复用 / 等超时），而中继的流式 generator
只 `async for` 读到连接关闭为止，于是傻等。

一句话类比：中继以为"车门关 = 到站"，但司机不关门；到站的真信号是"广播报站"
（`message_stop`）。

**修复**：`parsers/anthropic.py` 的 `AnthropicUsageParser` 加 `message_stop_seen`
属性（收到 `message_stop` 事件置 True）；proxy 三条流式路径（adapter / cross-wire /
直通）在 parser 状态到位后主动 break/return，走 finally 的 db.record +
`_complete_inflight`。三条路径的结束判定：

| 路径 | 判定 |
|---|---|
| adapter SSE | `parsed.type == "message_stop"` → break |
| 直通 stream_iter | `platform=="anthropic"` 且 `parser.message_stop_seen` → break |
| cross-wire sse_events | `upstream_wire==anthropic` 且**当前行解析出的** `d.get("type") == "message_stop"` → return |

> ⚠️ cross-wire 必须用**当前行 d.type**，不能用 `parser.message_stop_seen`：
> `parser.feed(chunk)` 一次处理整个 chunk 的所有帧，会提前把 message_stop_seen 置
> True，导致 buf 逐行解析还没 yield 完前面事件（如 content_block_delta "Hi"）就提前
> return、丢内容（v0.88 修复 test_cross_wire_openai_to_anthropic_end_to_end 回归时发现）。

**三种"截断"场景的行为**（已用 MockTransport 集成验证，均无 streaming 残留）：

| 场景 | 行为 |
|---|---|
| 上游流中途截断（无 message_stop，连接直接结束） | 已收内容照常转发；流自然退出 → finally 入库（usage 为部分值）+ complete |
| 客户端断开（aclose / 中途停止） | `except GeneratorExit` 捕获 → finally 的 db.record + complete 照常执行 |
| 完整流 | message_stop 一到即结束（v0.87 修复） |

**教训**：
1. 判断流结束要靠 parser 的**逐帧状态**（SSE 事件可能跨 chunk 边界），不能直接搜
   chunk 字节里有没有 `message_stop`。
2. 改 streaming 路径后必须发一次真实流式请求验证「message_stop → 立即 done」，
   而不是等 sweeper 兜底。
3. 排查"幽灵 streaming"：`curl http://127.0.0.1:8088/live`，看有没有
   `phase=streaming` 且 age_sec 一直增长、assistant_text 空的条目。

### prompt cache 统计与跨协议转换（v0.88）

`cache_read_input_tokens` / `cache_creation_input_tokens` 是 **Anthropic 协议**的用量
字段。OpenAI 协议（chat.completions / responses）缓存字段叫 `cached_tokens` /
`cache_write_tokens`，嵌套在 `prompt_tokens_details` 或 `input_tokens_details` 里。

**四种缓存字段命名**（openai 各家不统一，宽匹配）：

| 协议 | 读缓存 | 写缓存 |
|---|---|---|
| Anthropic Messages | `cache_read_input_tokens` | `cache_creation_input_tokens` |
| OpenAI Chat Completions | `prompt_tokens_details.cached_tokens` | `prompt_tokens_details.cache_write_tokens` |
| OpenAI Responses | `input_tokens_details.cached_tokens` | `input_tokens_details.cache_write_tokens` |

**三层处理**：

1. **Anthropic 上游**：`parsers/anthropic.py` 从 `message_start`/`message_delta`
   吸收 `cache_read_input_tokens` —— 一直正常（命中率 90%+）。
2. **OpenAI Chat / Responses 上游**：`parsers/openai.py` 新增
   `_extract_openai_cached_tokens()` 宽匹配 chat 的 `prompt_tokens_details.cached_tokens`
   / `prompt_cache_hit_tokens` 与 responses 的 `input_tokens_details.cached_tokens`
   等多种命名，流式 Path1/Path2 + 非流式 `extract_from_json` 都解析 → 记入
   `cache_read_input_tokens`。v0.88 修复（此前 openai 上游 cache_read 恒 0，DB 不记账）。
3. **cross-wire 透传**：linguafranca 把 openai→anthropic 转换时**本身就会映射**
   `cached_tokens` → `cache_read_input_tokens`（实测 message_delta 里已带 640 等值）。
   v0.88 额外在 `platform_wire==anthropic` 时给 message_start/message_delta 的 usage 用
   parser 统计值 max() 合并，作为 linguafranca 版本差异的保险，不覆盖已有值。

**完整端到端验证**（2026-08-18，三段协议经中继真实请求）：

| 上游 → 客户端 | 协议转换 | 客户端可见 cache | DB 统计 |
|---|---|---|---|
| minnimax anthropic (f3af39d7) → openai | anthropic→openai | `cached_tokens` 142→750 ✓ | cache_read 142→756 ✓ |
| minnimax openai (f3af39d7-openai) → anthropic | openai→anthropic | `cache_read` 142→756 ✓ | cache_read 142→756 ✓ |
| deepseek Responses → anthropic | responses→anthropic | `cache_read` 640 ✓ | cache_read 640 ✓ |

**结论**：三段协议（Anthropic Messages / OpenAI Chat Completions / OpenAI Responses）
两两之间，prompt cache 字段完全相互转换；DB 正确统计；客户端可见。

**排查"Cache hit rate 0%"**：
- anthropic 上游 0% 是真没命中（查 DB cache_read 确认）
- openai 上游 DB 恒 0 是 v0.88 之前 parser 没解析（已修）
- Claude Code 显示 0% 也可能是上游没返回缓存字段（`cached_tokens`）或请求没带
  `stream_options.include_usage`（openai 需显式开启）

**跨协议 cache 转换是 linguafranca 的工作**——三种协议两两之间的读写缓存都正确映射，
包括 anthropic `cache_creation` ↔ openai `cache_write_tokens`。v0.88 的 proxy.py
补写是冗余保险（max() 不覆盖 linguafranca 已生成的值）。

### ⚠ 跨线转换拆块 + 思考型上游 reasoning_content 回传（v0.97.1–v0.97.2）

**这是 openai 端点（cross-wire：anthropic 入口 → openai 上游）上最严重的一类
问题**——同时暴露三个现象，且让 openai 端点「完全不可用」，只能切回 anthropic
端点保命。三条问题**同根**：都落在跨线转换这条路径上，同 wire 透传永远碰不到。
本节把根因、修复、以及「为什么对其它上游也安全」写透。

#### 现象（用户报告的三件事，全部只在 openai 端点出现）

1. **思考栏无显示** —— 侧栏「思考（N 字）」恒为空。
2. **工具调用栏无显示** —— 侧栏「工具调用」栏不出 assistant tool_use JSON。
3. **400 反复中断** —— 多轮工具调用对话中，上游持续返回
   `The reasoning_content in the thinking mode must be passed back to the API.`，
   输出被拦腰截断，工具调用永远执行不完。切到 anthropic 端点（同 wire 字节
   透传）立即正常，所以这不是模型/会话问题，而是 **openai 跨线转换路径特有**。

   **精确时序（为什么「第一轮正常、第二轮才报错」）**：第一轮请求带 `tools`，
   模型正常返回一条 `assistant(tool_calls)` 消息（此时不校验）；客户端执行完
   工具后发**第二轮**，第二轮的 `messages` 历史里带着上一轮那条
   `assistant(tool_calls)` 消息——DeepSeek 在**这一轮**才校验「回传的
   assistant(tool_calls) 消息必须带 reasoning_content」。所以 400 永远出现在
   **工具调用之后的下一轮**，表现为「工具能发出去、但永远等不到第二轮结果」，
   工具调用流程卡死。这也是为什么用户形容「完全不可用」——不是单次报错，而是
   每次工具调用都注定断在第二跳。

#### 根因一：OpenAI parser 不采集思考与工具调用（现象 1、2）

侧栏「思考」「工具调用」两栏的数据源是 `OpenAIUsageParser` 的两个组装方法。
v0.97.1 之前它们都是死 stub：

```python
def assembled_thinking(self) -> Optional[str]:
    return None            # 旧实现 —— 思考栏永远空

def assembled_tool_use_json(self) -> Optional[str]:
    return None            # 旧实现 —— 工具调用栏永远空
```

而 openai 上游把这两类内容放在**非标准 / 分片**字段里，旧 parser 根本没采集：

- **思考**：DeepSeek 系思考型上游把推理放在 `delta.reasoning_content`（与
  `delta.content` 平级，非 OpenAI 标准字段），不是 anthropic 那种
  `thinking_delta` 块。旧 parser 的 Path 3 只 append `delta.content`，对
  `reasoning_content` 视而不见。
- **工具调用**：OpenAI Chat 流式把一次工具调用按 `delta.tool_calls[].index`
  **分片推送**——首片带 `id` / `function.name`，后续片只带 `function.arguments`
  碎片（如 `'{"cmd"'` → `':"ls"}'`）。旧 parser 完全没有按 index 归并这些碎片。

**修复**（`parsers/openai.py`）：

- `__init__` 加 `self._thinking_chunks: list[str]`（openai.py:68）与
  `self._tool_call_blocks: dict[int, dict]`（openai.py:73）。
- Path 3 流式分支（openai.py:143-173）：`delta.reasoning_content` → append 进
  `_thinking_chunks`；`delta.tool_calls` 按 `index` 归并进 `_tool_call_blocks[idx]`
  （`{"id", "name", "partial_json"}`，arguments 碎片 `+=` 拼接）。
- 非流式 `extract_from_json`（openai.py:269-287）：`message.reasoning_content`、
  `message.tool_calls` 同样采集。
- `assembled_thinking()`（openai.py:219）改为 `return "".join(self._thinking_chunks) or None`。
- `assembled_tool_use_json()`（openai.py:189）把归并的 tool_call 碎片组装成
  **anthropic 格式**的 assistant tool_use JSON（与 `AnthropicUsageParser` 同格式，
  侧栏「工具调用」栏共用一套渲染）；arguments 碎片拼出来若是畸形 JSON，`input`
  回退为 `{"_raw_partial": ...}`，不抛异常。

#### 根因二：linguafranca 把 anthropic assistant 逐块拆成多条 openai 消息（现象 3 的根源）

`_relay_cross_wire` 用 linguafranca 把 anthropic 请求转成 openai 请求时，
anthropic assistant 消息的 `content` **数组**（可同时含 text / tool_use / thinking
块）被**逐块**转成**多条连续的 openai assistant 消息**：

| anthropic assistant `content[]` 块 | linguafranca 转出的 openai 消息 |
|---|---|
| `{"type":"text","text":"..."}` | `{"role":"assistant","content":"..."}` |
| `{"type":"tool_use","id":...,"name":...,"input":...}` | `{"role":"assistant","tool_calls":[...]}` |
| `{"type":"thinking","thinking":"..."}` | `{"role":"assistant","reasoning_content":"..."}` |

实测（两段 text + 一个 tool_use 的 anthropic 消息转 openai）：

```jsonc
// linguafranca 原始输出 —— 一条 anthropic assistant 被拆成两条 openai assistant
{"content": [{"text":"one","type":"text"},{"text":" two","type":"text"}], "role": "assistant"},
{"role": "assistant", "tool_calls": [{"function": {"arguments": "{}", "name": "read"}, "id": "t1", "type": "function"}]}
```

注意两点：**多条 text 块保留在同一条 content 消息里**（不会丢文本，也不拆成
多条 content 消息）；真正的拆分发生在「content 一条 / tool_calls 一条 /
reasoning_content 一条」这种**异类块之间**。

**为什么拆开是致命的**：DeepSeek 等思考型 openai 上游对「带工具调用的 assistant
消息回传」有硬校验——`tool_calls` 与 `reasoning_content` 必须在**同一条** assistant
消息里（官方样例 append 的是一条同时带 content / reasoning_content / tool_calls
的消息）。拆开后，带 `tool_calls` 的那条消息**缺 reasoning_content** → 400
「must be passed back」。这是现象 3 的根因。

#### 根因三：DeepSeek 思考模式的严格回传校验（现象 3 的触发条件）

DeepSeek 的 API 规则（`proxy.py:845-849` 注释原文）：

> 请求带 `tools` 时，后续请求必须把上一轮 assistant 输出的 `reasoning_content`
> **原样**回传，且必须与 `tool_calls` 在同一条 assistant 消息里；缺了直接 400。

两层含义：
1. **字段必须存在** —— 官方样例：模型未输出推理时客户端同样 append
   `reasoning_content: ""`，空串可过，**缺字段不可过**。
2. **内容必须原样** —— 空串只是兜底；真实推理内容必须精确回传（否则 DeepSeek
   认为上下文被篡改，推理链断裂）。

这就是为什么「先加个空串兜底」**没能消除 400**：空串兜底解决的是「字段存在」，
但 linguafranca 拆块让 `reasoning_content` 和 `tool_calls` 落在两条消息上——
兜底的空串被写到了**错误的那条消息**上，带 tool_calls 的那条依然裸奔。

#### 修复：合并 + 注入 + 响应侧留存，三处联动

**① 响应侧留存（`proxy.py:854` `_bind_reasoning_to_tool_calls`）**

流式响应剥离 reasoning_content 时（proxy.py:2262-2278），把本响应累积的
`reasoning_content` 按 `tool_call id` 存进有界 dict `_REASONING_BY_TOOL_CALL`
（proxy.py:850，key = openai tool_call id，上限 512 条插入序淘汰）。思考在
tool_calls 出现**之前**就已流完，绑定时取「此刻累积值」。

**为什么要先剥离**：`reasoning_content` 是 DeepSeek 的非标准字段，linguafranca
的严格 schema 校验不认识它（会抛 `SchemaValidationError`：未知字段 / missing
field）。所以不能让它原样流进转换层——必须先剥下来单独存，下游再按需回灌。
剥离只删字段、不删思考内容（内容先 `append` 进 `_current_reasoning` 再 `pop`）。

**机制前提（为什么「按 tool_call id 回传」可靠）**：linguafranca 做 anthropic
`tool_use` ↔ openai `tool_calls` 转换时，`id` 字段**原样保留**（wire-dispatch-plan.md
§4.1：`id→tool_call_id` 是同名映射，不重生成）。所以响应侧按 openai tool_call id
存的 reasoning，在请求侧能靠**同一个 id** 查回来——往返两个方向 id 都不变，这是
整个留存→注入闭环成立的地基。若 id 在转换中变了，这条链就断了。

**② 请求侧合并（`proxy.py:902` `_merge_split_assistant_messages`）**

把 linguafranca 拆开的**连续 assistant 消息合并回一条**：content / tool_calls /
reasoning_content 各取第一个非空值。实测合并后：

```jsonc
{"role": "assistant",
 "content": [{"text":"one","type":"text"},{"text":" two","type":"text"}],
 "tool_calls": [{"function": {"arguments": "{}", "name": "read"}, "id": "t1", "type": "function"}]}
```

与原始 anthropic 消息形态一致，tool_call id（`t1`）原样保留。

**③ 请求侧注入（`proxy.py:870` `_inject_reasoning_to_messages`）**

合并后，对每条带 tool_calls 的 assistant 消息按 tool_call id 从
`_REASONING_BY_TOOL_CALL` 查回真实 reasoning_content 写回。查不到（历史来自
anthropic 端点 / relay 重启内存清空 / 旧对话）兜底注入空串（字段存在即可过）。
**关键顺序**：先合并再注入（proxy.py:2063-2067），且注入时若消息已带非空
reasoning_content（合并时从 thinking 块拿到的真实内容）**不覆盖**（proxy.py:892）。

#### 完整数据流

```
响应侧（上游 → 客户端）
  openai SSE delta.reasoning_content ──剥离──> _current_reasoning 累积
  openai SSE delta.tool_calls ──绑定──> _REASONING_BY_TOOL_CALL[tool_call_id] = reasoning
  （剥离后 reasoning_content 不再进 linguafranca，避免 schema 校验挂）

请求侧（客户端 → 上游）
  anthropic assistant content[] ──linguafranca──> 多条连续 openai assistant
  _merge_split_assistant_messages ──合并──> 单条（content+tool_calls+reasoning 归一）
  _inject_reasoning_to_messages ──注入──> 带 tool_calls 的消息补齐 reasoning_content
  发往 DeepSeek ──校验通过，不再 400
```

#### 通用性：这是 DeepSeek 特有吗？改了对其它上游安全吗？

分三层看，**结论：不是 DeepSeek 的 hack，是通用正确性修复**。

| 层 | 归属 | 说明 |
|---|---|---|
| linguafranca 拆块 | **通用**（与模型无关） | 任何 anthropic→openai 跨线，只要同回合产出 thinking+tool_use+text 就会拆。MiniMax、Qwen、OpenAI 官方走这条转换路径都会遇到 |
| 400 硬失败 | **DeepSeek 特有** | 「reasoning_content 必须回传且同消息，否则 400」是 DeepSeek 思考模式的 API 校验规则，其它端点没有这条 400 |
| 拆块带来的协议风险 | **通用** | OpenAI 兼容协议要求 assistant 的 tool_calls 作为完整单条消息回传（下一轮 tool_result 要配对）。拆散在宽松端点能过校验，但遇到严格校验或需引用完整 tool_calls 状态的场景就会错乱。合并后形态**更接近标准** |

**为什么不误伤正常历史（安全性论证）**：

1. **只跑跨线 openai 上游**：`_merge_split_assistant_messages` / `_inject_reasoning_to_messages`
   只在 `_relay_cross_wire`（proxy.py:2063）调用，且注入有 `upstream_wire == _OAI`
   门（proxy.py:2066）。anthropic 上游 / 同 wire 字节透传**完全不经过**。
2. **连续 assistant 只会来自拆块**：正常对话轮次之间必有 user / tool 消息分隔，
   merge 遇到 `role != "assistant"` 就断（proxy.py:937）。所以「连续 assistant
   消息」**唯一**来源就是 linguafranca 拆块，不会把正常历史里的独立 assistant
   消息误合并。
3. **文本不丢**：linguafranca 把多条 text 块保留在**同一条** content 消息里
   （实测确认），merge 的「取首个非空 content」不会丢掉任何文本块——它真正合并的
   只有「content / tool_calls / reasoning」三条之间的拆分。

**唯一理论边角**：若客户端自己发了畸形的「连续 assistant 消息」历史（不合规），
会被合并成一条（有损）。但那种历史对任何严格端点本来就是坏的，不属于正常场景。

#### 验证结果

- **单测**（`pytest tests/test_parsers.py tests/test_wire.py` = **43 passed**）：
  - `tests/test_wire.py` **更新**既有 `test_reasoning_content_captured_and_injected_for_tool_calls`
    （step 4 改为断言空串兜底、step 5 断言真值不被覆盖），**新增** 2 条：
    `test_merge_split_assistant_reunites_tool_calls_and_reasoning`、
    `test_merge_split_assistant_keeps_reasoning_over_inject_fallback`。
  - `tests/test_parsers.py` 新增 6 条（openai parser 思考/工具调用采集）：
    `test_openai_assembled_thinking_captures_reasoning_content`、
    `test_openai_assembled_thinking_returns_none_when_absent`、
    `test_openai_assembled_thinking_non_streaming`、
    `test_openai_assembled_tool_use_json_streaming`、
    `test_openai_assembled_tool_use_json_parallel_calls`、
    `test_openai_assembled_tool_use_json_non_streaming`。
- **端到端**：openai 端点（deepseek官方-openai，inbound=anthropic / outbound=openai-chat）
  实测——多轮工具调用对话**无 400 中断**（`phase='done'`），`/live` 快照
  `thinking_text` 有值、`cache_read_input_tokens=20864` 正常，侧栏思考/正文流/
  工具调用三栏全部显示。

#### 经验沉淀

- **空串兜底 ≠ 字段定位正确**：第一次只加 `reasoning_content: ""` 没消除 400，
  因为兜底被写到了 linguafranca 拆出的**错误消息**上。遇到「字段必须存在」类
  校验，先确认字段落在**哪条消息**，再谈兜底。
- **转换层的拆块是上游校验失败的常见来源**：跨协议转换不是无损的，linguafranca
  对 anthropic content 数组的「逐块展开」语义，在 anthropic 侧是合法（多块），
  在 openai 侧却破坏了「单条 assistant 消息完整性」的隐含约定。任何 strict 上游
  都可能因此失败——**协议转换后要做结构归一**，不是只做字段级校验。
- **同 wire 透传永远碰不到这类问题**：用户「只能切回 anthropic 端点保命」正是
  因为同 wire 走字节透传、不经过转换。排查时先确认走了哪条路径（`/live` 的
  `inbound_wire`/`outbound_wire`，或日志 `CROSS-WIRE ... wire=anthropic->openai-chat`），
  避免在错误的路径上浪费时间。

#### v0.97.3 收尾：注入门按「能力/厂商」收窄，不按「协议」一刀切

**v0.97.2 埋的雷**：`_inject_reasoning_to_messages` 的调用点是
`upstream_wire == _OAI`（proxy.py 原 2066 行）——只要上游是 openai 协议就注入
`reasoning_content`。但 `reasoning_content` 是 **DeepSeek 思考模式的私有字段**，不是
openai 协议的标准字段。MiniMax（minnimax.chat 中转站）是**非思考** openai 上游，它
不产出、也不认识这个字段。于是 MiniMax 收到带 `reasoning_content=""` 的
assistant(tool_calls) 消息时，整条请求被 400。**同一个字段，对 DeepSeek 是「必须
有」，对 MiniMax 是「不能有」，方向完全相反。**

**现象（用户报告 + 日志证据）**：用户在 minnimax.chat 中转站挂了两个上游——
- `f3af39d7`（wire=anthropic-messages，同 wire 字节透传）：工具调用**正常**。日志
  02:08:28 可见 minimax 返回 `tool_use Glob {"pattern":"C:/Users/weizheng/Desktop/*"}`，
  `stop_reason="tool_use"`（relay_trace.log）。
- `f3af39d7-openai`（wire=openai-chat，anthropic 入口 → openai 上游的跨线转换路径）：
  **工具结果传不上去**。日志里 `CROSSWIRE ENTER ... f3af39d7-openai` 之后**没有**
  对应的 `STREAM END` / 正常 `UPSTREAM RESP`，请求在中转站/上游被 400 拦下。

用户原话「minnimax 中转站的命令还是传不上去，且问题只出现在 openai 端点转译，直接
用 anthropic 的是可以的」。**关键点**：同 wire（anthropic）字节透传不经过
`_inject_reasoning_to_messages`，所以 anthropic 端点完全正常——这再次印证「同 wire
透传碰不到这类问题」。

**为什么响应侧不用改**：`_REASONING_BY_TOOL_CALL` 只有在「上游响应真的吐了
`reasoning_content`」时才会被 `_bind_reasoning_to_tool_calls` 填值。MiniMax 是
非思考模型，从不吐 reasoning_content，所以这个 dict 对 MiniMax 永远是空的，响应侧
无副作用。问题纯粹在**请求侧的空串兜底**被无条件应用到了非 DeepSeek 上游。

**修复（一处判定 + 一处调用点，全在 `proxy.py`）**：
1. 新增 `_upstream_needs_reasoning(cfg, model)`（proxy.py:870）：按上游
   `cfg.url` / `cfg.model` / 解析出的 `model` 三个字符串是否含 `"deepseek"` 判断
   是不是 DeepSeek 系思考型。命中 DeepSeek 官方（`api.deepseek.com`）、OpenCode Zen
   转发的 deepseek 模型（`deepseek-v4-flash-free` 等，URL 无 deepseek 但模型名有）
   都算；MiniMax（`minnimax.chat` + `MiniMax-M3`）三个都不含 deepseek → 不算。
2. 调用点（proxy.py:2091）从 `upstream_wire == _OAI` 改成
   `upstream_wire == _OAI and _upstream_needs_reasoning(cfg, model)`；
   `_inject_reasoning_to_messages` 的参数从 `is_openai_upstream` 更名为
   `needs_reasoning` 并重写 docstring。

**验证（代码侧已过，端到端待 GUI 重启确认）**：
- 单测：`tests/test_wire.py` 新增 `test_upstream_needs_reasoning_only_deepseek_thinking`
  + `test_inject_reasoning_skipped_for_non_deepseek_openai`。`pytest tests/test_wire.py`
  = **19 passed**（含 v0.97.1/0.97.2 既有 3 条 reasoning 测试仍全绿）。
- 直连复现脚本：MiniMax 的 assistant 消息转换后只剩 `['role','tool_calls']` 两个键、
  无 `reasoning_content`。
- ⚠️ **未验证**：8088 端口的中继进程不能杀（会中断用户当前会话），改完代码后须由
  用户自己点 GUI 重启拉新代码；本次未做「GUI 重启 → f3af39d7-openai 跑工具调用」
  的真机端到端验证，留待下次（详见交接文档 docs/handoff-minimax-openai-tool-result.md）。

**经验（通用，写进协议容错哲学）**：**「给上游补字段」的门，要开在能力/厂商维度，
不能开在协议维度。** 「协议」和「这个字段是不是该上游认的」是两码事——同一协议下
不同 provider 对非标准字段的态度可能完全相反。补任何非标准字段（`reasoning_content`
只是其中一个例子）前，先问「这是谁的私有扩展」，用 url/model 名等厂商信号收窄，
而不是用 `wire == openai` 这种协议信号一刀切。

#### v0.97.4：linguafranca 流式转换丢「首片完整 tool_calls 参数」（跨线路径独有）

**现象（用户报告）**：v0.97.3 之后（代码侧已全绿），用户早起实测 `f3af39d7-openai`
（MiniMax openai 端点，跨线路径）**依然**没法正确调用工具——模型正确思考
「Let me use Bash to list the desktop directory」，但 Claude Code 连续两次报
**「Invalid tool parameters」**。区别于 v0.97.3（400 断在第二跳，工具结果回传
不上），这次是模型**已经返回了 tool_use**，但客户端收到的是**空参数**的工具调用。

**根因：linguafranca 流式转换丢参数（`_relay_cross_wire` 的 sse_events 路径）**。

直连 linguafranca 复现（openai-chat → anthropic-messages 流式转换）：

| 输入 | 输出 |
|---|---|
| **单片**首片即带完整参数 `arguments:'{"command":"ls"}'`（MiniMax / DeepSeek 这类一上来把完整参数塞进首片的上游） | `content_block_start input:{}` → **直接** `content_block_stop`，**无** `input_json_delta` —— **参数被静默丢弃** → 客户端 `tool_use input={}` → 「Invalid tool parameters」 |
| 首片空参数 `arguments:""` + 独立全量参数片（OpenAI 官方分片发送） | `content_block_start input:{}` → `content_block_delta input_json_delta '{"command":"ls"}'` → 正常 |

也就是说：**「流式 tool_calls 参数要分片、首片留空」是 OpenAI 官方格式隐含的约定，
linguafranca 依赖这个约定；MiniMax / DeepSeek 等上游不遵守（首片即塞满），
linguafranca 就丢。** 非流式转换无此问题（arguments 整体保留）。

**为什么同 wire 透传 / anthropic 端点不受影响**：`f3af39d7`（wire=anthropic-messages）
走字节透传，根本不经过 linguafranca 转换；openai 端点的请求则要过
`_relay_cross_wire` → `sse_events` → `convert_stream`。再次印证「同 wire 透传碰不到
这类问题」。受影响范围：**所有 openai-chat wire 上游**的跨线流式响应（MiniMax openai
端点、opc-deepseek、opc-hy3 等「这堆模型」），但实际只有「首片即带完整参数」的上游
（MiniMax / DeepSeek）会触发，OpenAI 官方分片流不受影响。

**为什么 v0.97.3 没拦住**：v0.97.3 修的是**请求侧**「回传时补 reasoning_content」的
门太宽；v0.97.4 是**响应侧**「流式转换丢参数」的 linguafranca 缺陷，两者是同一现象
两段链条上的不同环节。v0.97.3 修完，请求侧不再 400，模型能出 tool_use 了——于是
暴露出了响应侧这个更深一层的问题（工具调用本身带不出参数）。

**修复（proxy.py，两处）**：

1. 新增 `_split_openai_tool_call_arguments(d, opened)`（proxy.py:980，紧挨
   `_merge_split_assistant_messages`）：把「未开片的非空 arguments」拆成
   **空参数首片（保留 id/name）+ 独立全量参数片**。`opened` set 记录已见 id/name 的
   tool_call index（本响应流内维护），已开片的增量参数原样透传——OpenAI 官方分片流
   不受影响。判断规则：`id` 片 / 空参数片 → 记入 opened；非空参数且未开片 → 拆。
2. `sse_events()`（proxy.py:2361-2391）：openai 分支在 `yield d` 前调
   `_split_openai_tool_call_arguments(d, opened_tool_call_indexes)`，返回非空则依次
   yield 拆出的 chunk。`opened_tool_call_indexes` 在 sse_events 内、逐响应初始化
   （每次请求/响应一个流，tool_call index 从 0 重新计数，无需跨响应保留）。

**验证（代码侧已过，端到端待 GUI 重启确认）**：
- `tests/test_wire.py` 新增 4 条：`test_split_full_args_first_chunk_recovers_arguments`
  （拆片后 linguafranca 产出 input_json_delta，直接断言参数不丢）、
  `test_split_leaves_incremental_stream_unchanged`（OpenAI 分片流三段原样透传、
  参数拼回完整）、`test_split_parallel_tool_calls_all_first_chunk_full`（MiniMax/
  DeepSeek 平行多工具，每个 index 首片都带完整参数，转换后参数都保留）、
  `test_split_multiple_tool_calls_only_unopened`（同 chunk 多 tool_call 只拆未开片）。
  `pytest tests/test_wire.py` = **23 passed**；
  `pytest tests/test_proxy_integration.py` = **22 passed**。
- ⚠️ **未验证**：8088 端口的中继进程不能杀（会中断用户当前会话），改完代码后须由
  用户自己点 GUI 重启拉新代码；本次未做「GUI 重启 → f3af39d7-openai 跑真实工具调用」
  的真机端到端验证，留待下次（详见交接文档 docs/handoff-minimax-openai-tool-result.md）。

**经验（补充到协议容错哲学）**：**跨协议转换库（linguafranca）对「符合官方分片
约定」的流有隐含依赖——首片留空参数、后续片增量拼装。** 上游不合约（首片塞满）
时，转换库会**静默丢内容**（不报错、无 warning），这是比「报错」更难查的一类问题：
错误信息永远不会告诉你「参数被丢了」，只能靠复现对照才能确认。排查流式转换丢内容，
第一件事就是拿上游原始 chunk 直接喂转换库对照「首片空 vs 首片满」两种形态。

### ⚠ 协议容错哲学（重要 · 所有解析器/转换器必须遵守）

**背景（2026-08-18 讲解沉淀）**：中继处在客户端与上游之间，两边都可能不守规范。
"标准协议的字段是固定的，但很多站点不完全遵守"—— 顺序错 / 缺字段 / 名字不对 /
自建字段，四类不规范**全都真实遇到过**。下面是 relay 的容错范式，改解析器 /
转换器 / 加新上游前必读。

#### 三层容错网

```
写死规范名        +        宽匹配已知别名        +        未知名宽松兜底
(必须的,否则认不出)   (为已验证的上游加分)       (unknown 不报错,宁可忽略)
```

| 层 | 做法 | 对应代码 |
|---|---|---|
| ① 写死规范名 | Anthropic/OpenAI 官方协议规定的名字，必须精确匹配 | `message_start` / `content_block_delta` / `message_stop`（anthropic.py:104-163）；`tool_use` / `thinking` / `tool_result` / `text` 块 type |
| ② 宽匹配已知别名 | 每验证一家新上游，把它的不同命名**硬编码**进识别清单 | `_extract_openai_cached_tokens` 认 4 种缓存命名（openai.py:31）；`_THINKING_CLIENT_FIELDS` 认 3 种思考字段拼写（proxy.py:1365）|
| ③ 宽松兜底 | 未知名不报错、不崩溃，宁可忽略/试一把 | `else: t = delta.get("text")`（未知 delta.type 当正文，anthropic.py:160）；JSON 解析失败 `log.debug` 跳过 |

#### 四类不规范 → 各自动用哪层

| 不规范 | 真实案例 | relay 应对 |
|---|---|---|
| **顺序错误** | `minnimax.chat` 把 input+cache 字段全塞在 `message_delta`（规范应在 `message_start`）| `_absorb_usage` 两个事件都调、`max()` 逐字段取大 —— 早到晚到都收得住（anthropic.py:168）|
| **缺少字段** | 有的上游 `message_start` 只发 `input_tokens`、不发 cache | `u.get(key, 0)` 缺字段当 0，不报错；后到字段再 `max()` 覆盖。**注意**：缺字段是"上游没发"，relay 不能凭空造 —— 侧栏显示 0/`—` 属于合法结果（v0.95「done 后 cache 0000」排查结论）|
| **名字不正确** | OpenAI 生态缓存字段各家不统一 | ② 层 `_extract_openai_cached_tokens` 逐种试（openai.py:31）|
| **自建字段** | `prompt_cache_hit_tokens` 是 **opencode.ai/zen 自己发明的**，官方没有 | ② 层硬编码认它 —— 为"某家上游加分支"是正确姿势，**不是配置化** |

#### 为什么不做成配置

这些名字是**外部协议契约** —— 换不换由 Anthropic / OpenAI / 各家上游说了算，不是
relay 能改的。做成配置反而引入一类新 bug：拼错一个名字，整个解析静默失效，还不好查。
正确取舍 = 写死规范名 + 对已知别名硬编码容错。

**新增一家上游时的正确动作**：把它家特有的字段命名按 ② 层加一个分支（照
`_extract_openai_cached_tokens` 的模板），**不要**把协议名做成配置，**不要**依赖
"上游会完全遵守规范"。

#### 排查起点（新站点解析出 0 / 空内容时）

1. 先怀疑**字段命名 / 位置又不一样** —— 按顺序错 / 缺字段 / 名字不对三类排查
2. 大概率定位到 `_absorb_usage`（anthropic）或 `_extract_openai_cached_tokens`
   （openai）该加哪个别名
3. 用 `scripts/debug_dump_sse.py` 抓上游原始 SSE 字节确认实际字段（见「调试工具」）

### 数据库

SQLite（默认 `./relay.db`，WAL 模式）。两张表：`requests`（每请求一行 + usage
字段 + upstream/api_key_alias）和 `messages`（对话内容，可选，`RELAY_SAVE_MESSAGES`）。
索引见 README「Database」节。旧库通过 `ALTER TABLE` 自动补列。

## 测试

```bash
python -m pytest tests/ --ignore tests/test_gui_web.py -v
```

**不要**直接跑全量 `python -m pytest tests/`：`test_gui_web.py` 在 Python 3.13 +
`unittest.mock` 下会栈溢出（`Windows fatal exception: stack overflow`）拖垮整个
运行，必须 `--ignore` 掉（README 有记录）。其中个别用例（如
`test_apply_upstream_updates_settings_on_success`）单独跑也会失败，属于既有问题，
与本仓库日常改动无关。

**组合跑挂起（v0.95 收尾观测，未追）**：把 `test_gui_web.py` 与
`test_live_stream.py` / `test_probe.py` 等拼进**同一个 pytest 进程**时，
`test_gui_web.py` 的 toggle 用例处挂起（单独跑该文件 22/22 全绿）。判为测试
基建的跨文件状态泄漏（某文件留下的后台线程 / asyncio loop 污染），非运行时 bug。
建议按模块分批跑（如 `pytest tests/test_parsers.py tests/test_wire.py ... ` 且
`test_gui_web.py` 单独一批），不要一把梭全量。

集成测试用 httpx `MockTransport`，不需要真实网络/API key。改 `tui.py` 等查询逻辑
后，跑 `-k "fetch_by_upstream or total_tokens"` 一类定向用例即可。

## 调试工具

| 工具 | 用途 |
|---|---|
| `relay_trace.log` | 极详细请求/SSE 轨迹（v0.12.2）。`UPSTREAM RESP` 若 `content-type: text/html` 说明上游路径配错 |
| `relay-gui --diag` | GUI 桥/轮询诊断日志（`relay-gui.log`），冻结桥的定位入口 |
| `_start_debug_http`（127.0.0.1:8089） | GUI `--diag` 下提供 `GET /<view>` 切视图，用于无头验证 |
| DevTools Protocol (9222) | 见上文"排查流程"，读浏览器真实 computed style |
| `scripts/debug_dump_sse.py` | 打印上游原始 SSE 字节，排"直播面板空"首选 |
| `tools/probe_headers.py` | 独立上游探测工具：`python tools/probe_headers.py <url> <key> <model>`，逐请求 dump 完整响应头（含 rate-limit 头、错误体形状），末尾附带跑一遍中继自动判定给结论。排 401/429/错误体形状问题时首选 |
| 本地假上游（`http.server` + `probe.connectivity_test` 直调） | 验证连通性测试/`_extract_reply` 不必依赖真实可用 key——起 `HTTPServer` 返回固定 JSON（200+回复 / 200+错误对象 / 401），`asyncio.run(connectivity_test(...))` 断言 ok 与 reply。真实上游大多欠费/限流，边界用例用假上游最稳 |

## 实时流侧栏窗口（v0.89）

v0.89 在主 GUI 窗口之外新增一个**第二 pywebview 窗口**（`panel_window`），
专门展示「最近一次模型调用」的全貌：上游、模型、出向 key **明文**、入向/出向
wire、逐 chunk 流式内容（正文 + extended thinking + tool_use + 用户提示词预览）、
实时跳动的 token 四字段与缓存命中率。跟主窗**完全双向锚接**。

### 数据流总览

```
中继进程 (8088)
  proxy.py
  ├─ _InFlight（模块级字典，进程内）
  │   ├─ api_key（明文）/ inbound_wire / outbound_wire
  │   ├─ usage_live / thinking_text / tool_use_json
  │   └─ 已有 phase / bytes_received / user_text_preview / assistant_text
  ├─ _broadcast_live_event(rid, kind, **payload)
  │   └─ 写入 _subscribers: set[asyncio.Queue]
  │
  routers/stats.py:live_stream   (SSE)
  │   └─ 每个 HTTP 连接 = 一个 queue，subscribe/unsubscribe 包在生成器里
  │      首连先推一条 snapshot 事件，之后 15s 无事件发 `: keepalive`
  ▼
GUI 进程 (main.py)
  gui.py:_start_live_panel_stream
  └─ daemon 线程 urllib 连 127.0.0.1:8088/live/stream
     ├─ 行解析：split('\n\n')，data: 抽取，: 注释跳过
     ├─ 指数退避重连（1s→10s 上限）
     └─ panel_window.evaluate_js("relayLiveEvent(ev)")  跨线程安全
        （WinForms Control.Invoke，gui.py:736-737 既有注释背书）
        ▼
        src/relay/web/live_panel.html + live_panel.js
        单一入口 window.relayLiveEvent(ev)，按 type 分支处理
        snapshot / delta / done —— 侧栏只展示「最近一次调用」
```

三个事件类型（来自 `proxy._broadcast_live_event` / live_stream snapshot）：

| type | 触发 | 主要字段 |
|---|---|---|
| `snapshot` | 首连时（`stats.py:226-232`）；从 `get_inflight_snapshot()` 取最近一条（含 v0.95 起也含已完成） | 完整 `_InFlight` 字段（除 `api_key` 已 `_mask_key` 掩码） |
| `delta` | 流式期间每 chunk（adapter / cross-wire / 直通 三处插桩） | `assistant_text` / `thinking_text`（**累积**而非单次增量）、`usage_live`，**v0.95 起自动补齐**上游/平台/模型/wire/api-key 6 字段 |
| `done` | 流结束 / 非流式响应一次性 | 全文、`tool_use_json`、`usage_live` 终值、`error` |

### 广播插桩点

**7 处**广播分布在 **3 个转发函数**、**5 条响应路径**上：

| 行号 | 函数 | 路径 | kind |
|---|---|---|---|
| 647 | `_anthropic_adapter_relay_sse` | adapter SSE 流式（字节循环内） | `delta` |
| 688 | `_anthropic_adapter_relay_sse` | adapter SSE finally | `done` |
| 2020 | `_relay_cross_wire` | cross-wire 非流式（解析完整响应后） | `done` |
| 2081 | `_relay_cross_wire` | cross-wire 流式（`gen()` 字节循环） | `delta` |
| 2212 | `_relay_cross_wire` | cross-wire 流式 finally | `done` |
| 2698 | `relay()` | 直通流式（`stream_iter` 字节循环） | `delta` |
| 2809 | `relay()` | 直通 finally | `done` |

**`relay()` 6a 非流式分支（line 2564 `if not is_sse`）不广播**——非流式响应一次性入库 + 完结 inflight，无中间态可展示。仅 cross-wire 的非流式路径（`parse_response`）广播 done，因为其输出最终由 `convert_stream` 包装成 SSE 转回客户端时仍走 streaming response，从客户端视角是流式、需侧栏显示。

每处 `delta` 广播前必先 `_update_inflight(assistant_text=..., thinking_text=..., usage_live=...)`——保证 inflight 字典与广播事件一致（`proxy.py:1055` 文档化）。

### `_InFlight` 字段注入来源

`api_key` / `inbound_wire` / `outbound_wire` 三字段在 `relay()` 主路径集中填入
（`proxy.py:2380-2397`，v0.92 修正位置），其它函数复用同一 inflight entry
（`inflight_id` 透传）：

```python
await _update_inflight(inf.request_id, upstream=cfg.name)
inbound_wire = WIRE_OPENAI_CHAT if platform == "openai" else WIRE_ANTHROPIC_MESSAGES
effective_api_key = cfg.api_key or client_key  # v0.92: passthrough 兜底
await _update_inflight(
    inf.request_id,
    inbound_wire=inbound_wire,
    outbound_wire=cfg.effective_wire(platform),  # 出向 wire
    api_key=effective_api_key,                    # 中继实际发给上游的 key
)
```

- `inbound_wire`：客户端协议，按 `platform` 推断。
- `outbound_wire`：上游协议，按 `cfg.effective_wire(platform)` 推断（取决于上游是否声明某 wire）。
- `api_key`：`cfg.api_key` 优先（中继转发场景，override 后发的是 cfg 真 key）；
  `cfg.api_key` 为空时（passthrough 直传场景）兜底用 `client_key` —— 客户端发
  的真 key 中继原样转发给上游，正是侧栏该展示的。明文落 inflight，HTTP/SSE
  出口经 `_mask_key` 掩码。

侧栏展示的 `→` 由此而来：inbound_wire → outbound_wire，相等即直通、不等即 cross-wire 协议转换。

### wire / api_key 字段注入（v0.92 修正）

**症状**：跨协议路径（openai→anthropic）+ dual-protocol 上游（OpenCode Go 类
`requires_anthropic_adapter`）的请求到 SSE snapshot 时，wire 显 `—`、api_key
显 `（明文待加载）` 或 `（无）`；passthrough 直传场景即使 wire 正确，api_key
也始终显示「（无）」。

**根因**：`_register_inflight` 在请求体解析前用空字符串注册三字段（`proxy.py:2229`），
后续靠 `_update_inflight` 补——但**只在直通路径**（`_apply_auth_override` 之后
line 2463 旧位置）补一次。`_relay_cross_wire`（line 1832，跨协议路径）和
`_anthropic_adapter_relay`（line 331，dual-protocol 路径）走的是 `return` 早
退，**根本没补**；passthrough 模式下 `cfg.api_key` 为空，`api_key=cfg.api_key or ""`
永远是空串。

**修复**：把 `_update_inflight(wire/api_key)` 移到**分发判定 + advanced-switch
之后、路径分叉之前**的统一位置（`proxy.py:2380-2397`）—— 所有路径都共用一处
写入，cross_wire / adapter 路径不再各自补。`effective_api_key = cfg.api_key
or client_key` 兜底透传场景。

**教训**：`return` 早退的路径分叉里，要把需要在所有路径上都生效的 inflight 字
段提到分叉**之前**集中写入。散落在分叉内部会让路径 B/C 漏写，单测又不一定
覆盖每条路径；线上一踩一个准。

### SSE 订阅线程内部结构

`gui.py` 内的 SSE 订阅线程由 5 个方法组成（除了 `_start_live_panel_stream`）：

| 方法 | 职责 |
|---|---|
| `_start_live_panel_stream` | 创建 daemon 线程 + `_sse_stop_event`；幂等，已跑则早退 |
| `_sse_loop` | 顶循环：`_sse_connect_once()` → 失败指数退避（1s→10s 上限）→ 收到 `_sse_stop_event` 退出 |
| `_sse_connect_once` | `urllib.request.urlopen` 连 `/live/stream`，循环读行、调 `_sse_handle_frame` |
| `_sse_handle_frame` | 解析一行 SSE 帧：split `\n\n` 拿 frame，跳过 `: keepalive` / 空行，提取 `data:` 行后 `json.loads` |
| `_sse_push_to_panel` | 包装 `panel_window.evaluate_js("relayLiveEvent(ev)")`，try/except 吞 `OSError`（窗口关闭 / 页面重载） |

`_sse_stop_event` 是 `threading.Event`，`set_live_panel(False)` 把它置位 + `join()` 线程，下次开窗再启新一条。中继侧的 `_subscribers` 在 SSE 生成器 `finally` 里调 `_unsubscribe_live_stream(queue)`，**无队列泄漏**。

### 关键文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/parsers/anthropic.py` | +`_thinking_chunks` 列表 + `assembled_thinking()` 方法；`content_block_delta` 的 `thinking_delta` 分支从 `pass` 改为 append；`signature_delta` 仍跳过（签名不是可读内容） |
| `src/relay/parsers/openai.py` | +`assembled_thinking()` stub 返回 None（保留扩展点） |
| `src/relay/proxy.py` | `_InFlight` 增 6 字段（`proxy.py:817-834`）；`get_inflight_snapshot` 掩码 api_key；`_broadcast_live_event` + `_subscribers` pub/sub；**7 处广播点**（3 个函数 × 5 条响应路径，详见下方「广播插桩点」） |
| `src/relay/routers/stats.py` | +`GET /live/stream`（`stats.py:209-258`）：SSE 生成器 + 订阅管理 + 15s keepalive |
| `src/relay/db.py` | `record_messages` 加 `thinking_text` kwarg（仅文本，无 thinking_json——thinking 块没有 tool_use 那种结构化 payload，`db.py:168,187-190`）；`messages.role='thinking'` 不需 schema 迁移（列是自由 TEXT） |
| `src/relay/config.py` | +`relay_gui_live_panel: bool = False`（`config.py:453`） |
| `src/relay/gui.py` | 第二窗口创建 / 双向锚接 / 反向联动 / 生命周期 / 主题同步 / SSE 订阅线程；新增 `get_live_panel` / `set_live_panel` / `get_live_panel_api_key` 三个 Api 桥方法 |
| `src/relay/web/live_panel.html` | **新建**——侧栏结构（header / meta / token / details / stream / tools） |
| `src/relay/web/live_panel.js` | **新建**——单一入口 `window.relayLiveEvent(ev)` |
| `src/relay/web/live_panel.css` | **新建**——主题令牌复用主窗 `styles-20260817.css` 的 CSS 自定义属性 |
| `src/relay/web/index.html` | +`<button id="btn-live-panel">实时流</button>`（在 `btn-io-map` 与 `btn-theme` 之间） |
| `src/relay/web/app.js` | +`api.getLivePanel()` / `setLivePanel()` stub（`app.js:86-87`）；设置项 `prefs-live-panel`（`app.js:2549`）；change handler（`app.js:2773`）；`refreshPrefsDynamic` 初值（`app.js:3181`）；顶栏按钮 click handler + **`renderLivePanelBtn(snap)`**（`app.js:4031-4038`，**sig-based dedup**：模块级 `_livePanelBtnSig` 短路同状态，避免每 tick 重写 `aria-pressed` / 文本）；`window.syncSidePanelToggle(enabled)` 全局函数（`app.js:4348`）供侧栏窗口回叫；thinking 渲染分支（`app.js:1482`：`role === "thinking"` → `modal-msg-thinking` 类） |
| `tests/test_parsers.py` | thinking 累积单测：`test_thinking_delta_accumulates_into_assembled`、`test_signature_delta_does_not_pollute_text`、`test_non_stream_thinking_block_extracted_from_content_array` 等 |
| `tests/test_proxy_integration.py` | 广播事件序列端到端：模拟 SSE 响应，断言逐 chunk delta + 终结 done |
| `tests/test_live_stream.py` | SSE 帧解析 + GUI 推送（**7 个新用例**）：`test_sse_handle_frame_extracts_data_line`、`test_sse_handle_frame_skips_keepalive_and_blank_lines`、`test_sse_handle_frame_malformed_json_does_not_push`、`test_sse_handle_frame_concatenates_multi_data_lines`、`test_sse_push_to_panel_evaluates_relayLiveEvent`、`test_sse_push_to_panel_swallows_evaluate_js_exceptions`、`test_sse_loop_backoff_then_exit_on_stop` |
| `tests/test_gui_web.py` | 开关持久化 + cleartext key 桥（**3 个新用例**）：`test_set_live_panel_persists_and_calls_show_hide`、`test_get_live_panel_api_key_returns_cleartext`、`test_get_live_panel_api_key_missing_returns_error` |

### 关键技术决策（已验证）

- **pywebview 6.2.1**（conda env `usage-stats` 实装）原生 `events.shown/moved/
  resized/minimized/restored`（`webview/window.py:168-173`），WinForms 后端已做
  DPI 换算（`platforms/winforms.py:417-447`）。**锚接是事件驱动的，不需要轮询线程**。
- `create_window(hidden=True, x=..., y=..., frameless=True)` 是 6.2.1 签名参数；
  我们用 **frameless + easy_drag**（v0.89 后期从原生标题栏改来）——无系统
  标题栏，HTML 顶部复用 `styles-20260817.css` 的 `.window-controls / .win-ctrl
  / .win-ctrl-close` 自绘右上角 X 按钮；拖动交给 easy_drag，反向联动走
  `events.moved`。
- `window.native` 就是 WinForms Form；现有代码已在 `gui.py` 用 ctypes 操作
  `win.native.Handle` 做最大化/还原。**反向联动靠 `panel_window.events.moved`
  + 重入标志 `_dock_panel_semaphore`** 防双向 move 递归（WinForms `Move`
  在拖拽中高频触发）。
- uvicorn 单 worker（`server.py:89-105` 无 `--workers`；`main.py:139-145` 无
  `workers=`）→ 进程内 `_subscribers` pub/sub 可行，无需跨进程队列。
- `webview.start()` 只在**所有**窗口销毁后返回（WinForms `Application.Exit`
  仅当 `len(BrowserView.instances) == 0`）→ 托盘「退出中继」必须**先**
  `panel_window.destroy()` **再** `window.destroy()`，否则进程挂住。
- parser 的 `usage`（`UsageAcc`）**已经**逐帧更新（`anthropic.py:_absorb_usage`
  168-181，`output_tokens` 取 max 累积）→ 实时 token 不需要新增解析逻辑，只需
  镜像到 `_InFlight.usage_live` 并广播。
- `/live` 与 `/live/stream` 都无 middleware / auth（`create_app` 无
  `add_middleware`，`_check_auth` 只在 `relay()` 内部对 `/anthropic/*`、`/openai/*`
  生效）→ 端点同安全姿态（仅本机）。
- `evaluate_js` 跨线程安全：内部走 `Control.Invoke`（`gui.py:736-737` 既有注释）。

### api-key 明文通道

**原则：明文只在进程内/loopback 通道内流转；对外只返掩码。**

> v0.95 更新：第一版「明文只走 pywebview 桥」是错的——GUI 与 Relay 是**两个独立
> Python 进程**，桥内部 `from relay import proxy` 拿到的是 GUI 进程的模块副本，
> `_in_flight` 永远空，桥永远返回 ""。v0.95 改为 SSE payload 直接带
> `api_key_cleartext`（loopback，不扩大攻击面，详见下方「api-key 明文通道（v0.95
> 架构修正）」）。本节是 v0.89 原始记录，桥 fallback 保留但仅过渡期有效。

- `proxy.get_inflight_snapshot()` 序列化时调 `_mask_key()`（`proxy.py:1721-1725`）
  对 `api_key` 掩码，`/live` 与 `/live/stream` 两条端点的消费者看到的都是
  `sk-1****abcd` 形式。
- 明文仅在 `_InFlight.api_key` 字段（`proxy.py:823`）保留；GUI 通过
  `Api.get_live_panel_api_key(request_id)`（`gui.py:1120`）按 rid 取出。

> v0.95 之前前端只在有 pywebview 桥时调 `applyCleartextKey`，浏览器直开
> `live_panel.html` 会回退到「（无）」。v0.95 起明文改走 SSE payload
> `api_key_cleartext`（见「api-key 明文通道（v0.95 架构修正）」），桥仅在
> cleartext 缺省时兜底。

### 双向锚接

主窗事件 → 侧栏跟随：

```python
self.window.events.moved     += self._dock_panel
self.window.events.resized   += self._dock_panel
self.window.events.minimized += lambda: self.panel_window.hide()
self.window.events.restored  += self._on_main_restored   # show + 重新 dock
```

`_dock_panel()` 读主窗 `x/y/width/height`，把侧栏摆到
`panel.move(x + width, y)` + `panel.resize(panel_width, height)`。

侧栏事件 → 主窗跟随：订阅 `panel_window.events.moved`，算位移 delta 后
`self.window.move(...)`。**同一个 `_dock_panel_semaphore` 布尔标志护住双向**——
任何一方 move 触发 dock 时置 True，dock 内再触发的 move handler 看到标志就早退，
避免无限递归。

实测 WinForms 下抖动明显时退化方案：改用 `panel.native.ResizeEnd` /
`MouseCaptureChanged` .NET 事件做「拖拽结束时一次性对齐」。v0.89 首发按
`events.moved` 实现，已手测通过。

### 侧栏紧凑化 + owned-window 一体性（v0.95 收尾）

**① 侧栏紧凑化（`live_panel.html/css/js`，缓存戳 `?v=20260818-02`）**

用户要求「尽可能少的空间装尽可能多的内容，缓存区显示窗口任何情况下不被压缩」：

- 移除「用户提示词预览」`<details id="lp-prompt-wrap">`（与正文流/思考重复度高，
  是空间主要占用者）。**连带**：`live_panel.js` 里所有 `$promptWrap` / `$prompt`
  引用必须同步删（DOM 缓存行、`reset()` 的 `setText($prompt,"（暂无）")`、snapshot
  分支的 `setText($prompt, ev.user_text_preview) + $promptWrap.open = false`）——
  容器移除后这些引用是 null，不删就是运行时抛错。
- 「思考」默认展开：`<details id="lp-thinking-wrap" open>`。
- **防压缩的规则**：`.live-panel-card` 统一 `flex: 0 0 auto`（除正文流卡片）；
  `.live-panel-stream-wrap` 是唯一 `flex: 1` 的可伸缩卡片（`min-height: 140px`）。
  窗口高度变化全部由正文流/页面滚动吸收，缓存区/头部/token 永远完整。
- token 区 `grid-template-columns: repeat(4, 1fr)` 一行排开 4 个 token，标签中文：
  输入 / 输出 / 缓存读取 / 缓存创建；命中率独立一行（紧凑）。

**② owned-window 一体性（`gui.py:_adopt_panel_owner`）**

用户要求「主窗被其它应用遮挡时侧栏也退到后面，主窗回顶时侧栏跟着回」——
这是 **WinForms owned form** 语义，但 pywebview 的 `create_window` **没有 owner
参数**（6.2.1 签名里只有 `on_top`，那是"永远置顶"不是"随 owner"）。解法：

```python
# 两窗 native 都是 BrowserForm（继承 WinForms.Form）
self.window.native.AddOwnedForm(self.panel_window.native)
```

调用点在 `_on_panel_loaded`（页面加载时两窗 native 都已建好；`create_window`
阶段 native 还是 None）。仅 winforms 后端有 `.native`，其它后端 `getattr` 拿到
None 直接跳过。

**owned 语义的三个连带影响**：

1. 主窗最小化 → owned 自动隐藏（现有 `_on_main_minimized` 的手动 hide 变冗余
   但无害）；还原 → `_on_main_restored` 按开关重显，逻辑不变。
2. 主窗 hide→托盘 → owned 随 owner 被 WinForms 隐藏 —— **`_show_from_tray` 必须
   补一段**「按 `relay_gui_live_panel` 开关重显侧栏 + 重新 dock」，否则侧栏永远
   消失。与 `_on_main_restored` 同款逻辑，两处都要。
3. 退出顺序仍必须先 `panel_window.destroy()` 再 `window.destroy()`（owned 关系
   不影响 `BrowserView.instances` 清空判据）。

**坑**：这个行为 Python 单测验证不了（要真窗口 + WinForms 消息循环）。只能
GUI 重启手测：主窗被别的窗口盖住再点回来、最小化/还原、关到托盘再托盘恢复。

### 正文流/思考传全文 + 背景光晕 + thinking 滚底（v0.95 收尾 续）

**① 去掉 2000 字符截尾（`proxy.py`，后端）**

用户发现思考字数到 2000 就卡住、但思考窗口还在跳 —— 不是上游限 2000 字、
不是识别问题，是 relay 广播侧的**截尾策略**：

```
上游流式输出思考全文（假设 3000 字）
  └─ _update_inflight(thinking_text=thinking_now)         ← 存全文，无截断
     └─ _broadcast_live_event(thinking_text=thinking_now[-2000:])  ← 广播截尾
        └─ live_panel.js applyThinking 用 text.length 显示字数 → 恒 2000
```

所有广播点（`proxy.py` 649/650、691/692、2041、2101/2102、2233/2234、
2718/2719、2830）和 snapshot 序列化（`proxy.py:1246`、`1253`）都做
`assistant_text` / `thinking_text` 的 `[-2000:]`。正文流 ≤2000 字时无感；
超长时每次 delta 只推最后 2000 字符，前端 `text.length` 恒等于 2000，字数卡
死；但内容每 chunk 都在更新 → 像「滚动窗口」钉在最新 2000 字。

**修**：删掉全部 9 处切片，广播与 snapshot 直接传全文。前端**无需改** ——
字数标签用 `text.length` 自动显示真实字数；`_InFlight` 与 DB 落盘本来就是
全文，`parser.assembled_thinking()` 不受影响。`_broadcast_live_event` 只自动
补齐头部字段、从不改文本（`proxy.py:1054`），无隐藏截断。**回归**：
`tests/test_proxy_integration.py tests/test_live_stream.py tests/test_parsers.py`
53/53 过，无测试断言截断行为。

**经验**：`[-2000:]` 这类「给侧栏减负」的截尾，把**字数**和**内容**一起截了
—— 用户看到「字数卡在 2000」会当成「上游限 2000 字」。减负要考虑是否只影响
展示不影响语义：全文经 SSE 推给本机 loopback 开销可忽略，截尾省的那点字节
不值得用「字数造假」换。

**② 背景光晕（`live_panel.html/css`，缓存戳 `?v=20260818-03`）**

主窗光晕机制（`styles-20260817.css` v0.42）：`html` 撑 `--root-bg`、`body`
`background: transparent`、两块 `.bg-glow`（fixed + `z-index:-1` +
`filter: blur` 静态模糊圆块，首帧 paint 后缓存、零 per-frame 开销）浮在 html
之上 body 内容之下。live_panel 之前 `body { background: var(--root-bg) }`
把光晕全盖住。**修**：`live_panel.html` body 里加两个
`<div class="bg-glow bg-glow-1/2">`（与主窗 index.html 同款）、
`live_panel.css` body 改 `background: transparent`。三主题自适应（`--glow-1/2`
各主题有定义，不用新写 CSS）。

**③ thinking 自动滚底（`live_panel.js`，缓存戳 `?v=20260818-03`）**

用户要求「思考容器始终显示新增内容（始终向下滚动到最低）」。原 `isSticky()`
写死 `$stream`，只有正文流自动滚底。**修**：泛化为 `isSticky(el)` ——
`_stickyState[el.id]` 给每个容器独立粘底状态（`{ last, at }`），正文流 / 思考
互不干扰；`applyThinking` 写入前判粘底、写入后 `scrollTop = scrollHeight`，
与 `applyStreamText` 同款。行为一致：只有用户主动上滚一段距离（>50px 一次性）
才暂停跟随，轻触 / 短暂停顿（≤500ms 记忆）不误停。`.live-panel-pre` 本就有
`max-height: 160px; overflow: auto`，可滚动。

**④ 自定义细滚动条（`live_panel.css`，缓存戳 `?v=20260818-04`）**

用户觉得三个可滚动容器（正文流 / 思考 / tool args）的**原生滚动条太丑**
（Windows WebView2 默认粗白/灰矩形）。**修**：与主窗 `styles-20260817.css`
同款细滚动条 —— `scrollbar-width: thin` + `scrollbar-color: var(--hairline)
transparent`（Firefox），WebKit 侧 `width: 6px`、track 透明、
thumb `var(--hairline)` + 3px 圆角、hover 加深到 `--text-secondary`。三主题
自适应（`--hairline` / `--text-secondary` 各主题都有定义）。滚动功能不变
（`max-height + overflow: auto` 仍在），只是替换滚动条外观。

**经验**：WebView2 是 Chromium，`::-webkit-scrollbar` 规则完全生效；用
`var(--hairline)` 这种极淡 token 做 thumb，视觉上比原生滚动条轻得多、和
`--panel-alt` 背景融为一体。别顺手 `display: none` 整个隐藏 —— v0.12.4
教训（上游卡）证明「藏滚动条 = 用户以为没滚动」。

### 开关与持久化

照 `show_io_map` 的端到端模板做：

- `config.Settings.relay_gui_live_panel: bool = False`（`config.py:453`）；
  走 `update_env_var("RELAY_GUI_LIVE_PANEL", "1"/"0")` 写项目根 `.env`。
- 桥 `api.get_live_panel() / set_live_panel(enabled)`：`set_live_panel(True)`
  内部除了持久化、show + dock 侧栏，**还**会调 `_start_live_panel_stream()`
  启 SSE 订阅线程；`False` 停线程 + hide 侧栏。
- 前端三处：`api.getLivePanel()` / `setLivePanel(enabled)` stub（`app.js`
  顶部）；`refreshPrefsDynamic` 初值拉取；change handler 同步到
  `syncSidePanelToggle(enabled)` 顶栏按钮的 `aria-pressed`。
- 顶栏图标：`index.html` 的 `btn-io-map` 旁加 `btn-live-panel`，与设置页开关
  共享同一后端开关、状态互相同步。
- 启动时若开关为开：`_on_loaded` 里 show + dock + 启 SSE 线程。

### thinking 落盘

`messages` 表的 `role` 列是自由 TEXT（无 CHECK 约束），新增 `role='thinking'`
行**不需要** schema 迁移：

- `db.record_messages`（`db.py:160-190`）加 `thinking_text` 一个 kwarg
  （thinking 块没有结构化 payload，不加 `thinking_json`），沿用「空值跳过不建行」
  的既有语义。
- 三条流式路径的 `finally` 把 `parser.assembled_thinking()` 传进去。
- 消费侧：`fetch_conversation`（`tui.py:714-743`）与前端对话详情弹窗按 role
  渲染；新 role 要么显示为可折叠块、要么被忽略。新增 role **必须**检查现有渲染
  是否会因未知 role 报错——v0.89 加了 thinking 渲染分支。
- tool_use 已经在存（`assistant_json` ← `assembled_tool_use_json()`），不用改。

### 常见坑

1. **`assembled_thinking()` 必须在 `assembled_text()` 之后才正确**：thinking
   块的解析在 `content_block_delta` 的 `thinking_delta` 分支；正文提取的宽松
   fallback（`else: t = delta.get("text")`）**必须**在 thinking 分支之后，
   否则 thinking 会污染正文。`parsers/anthropic.py:147-156` 的位置不是随便放
   的。

2. **cross-wire 不能用 `parser.message_stop_seen` 判收尾**：`parser.feed(chunk)`
   一次处理整个 chunk 的所有帧，会提前把 `message_stop_seen` 置 True，导致
   buf 逐行解析还没 yield 完前面事件（如 content_block_delta "Hi"）就提前
   return、丢内容。cross-wire 必须用**当前行 d.type**。v0.88 修过这个回归，
   v0.89 切记别再用错。

3. **每 chunk 广播必须绝不阻塞转发主路**：`_broadcast` 内锁只锁住 `set` 拷贝，
   锁外 `put_nowait`，`QueueFull` 丢帧并计数（宁可掉帧也不能背压拖慢转发）。
   侧栏没开时 GUI 不订阅、订阅者集合为空时 `_broadcast` 早退。

4. **`evaluate_js` 跨线程抛异常要吞**：窗口关了 / 页面重载时 `evaluate_js` 会
   抛 `OSError` 之类的，`_sse_push_to_panel` 的 try/except 静默吞异常，**不能**
   让 SSE 线程崩——崩了要等 1s 退避重连才恢复，体验差。

5. **退出顺序**：`webview.start()` 只在所有窗口销毁后返回。托盘「退出中继」
   必须先 `panel_window.destroy()` 再 `window.destroy()`。侧栏单独挂 closing
   handler 返回 True + hide + 把开关置关——点侧栏 X 等于关开关，符合直觉。

6. **侧栏隐藏时暂停 SSE 订阅省开销**：`set_live_panel(False)` 走
   `_stop_live_panel_stream` 把 `_sse_stop_event` 置位、join 线程。
   `set_live_panel(True)` 再启一条新的。中继侧 `_subscribers` 会在 SSE
   生成器 finally 里 `_unsubscribe_live_stream(queue)`，无泄漏。

### 调试

| 症状 | 排查 |
|---|---|
| 侧栏一片空白 | 看 `relay-gui.log` SSE 线程是否连上 8088；`curl -N http://127.0.0.1:8088/live/stream` 直查订阅链路 |
| api-key 只显示掩码 | 检查 `live_panel.js:applyCleartextKey` 的 `window.pywebview.api.get_live_panel_api_key` 是否被调用——浏览器直开（无桥）这条永远不进；要看 GUI 是否用 pywebview 壳起的 |
| token 不跳动 | 看 SSE delta 事件有没有 `usage_live` 字段；中继端 parser 是否在流式路径插桩 |
| 双向联动抖动 / 递归 | `_dock_panel_semaphore` 是否在两处 handler 都加上了；不行退化到 `native.ResizeEnd` 拖拽结束对齐 |
| 拖侧栏标题栏主窗不动 | 看 `panel_window.events.moved` 是否成功订阅；WinForms 后端事件名是否拼对（`moved` 不是 `move`） |
| 切主题侧栏不变 | `_on_toggle_theme` / `Api.set_theme` 是否对**两个**窗口都 `evaluate_js("setTheme(...)")` |
| 顶栏按钮状态不同步 | 看 `syncSidePanelToggle(enabled)` 是否被设置页 change handler 调到；按钮 `aria-pressed` 与设置页 checkbox 都靠它 |

### 已知限制 / 未来工作

- OpenAI 兼容上游的 `reasoning_content` / `reasoning_text` 字段在
  `proxy.py:2083-2087` 已被剥离（v0.66 之前的行为），侧栏 OpenAI 路径暂不展示
  thinking 内容。`parsers/openai.py:assembled_thinking()` 留了 stub 扩展点。
- 侧栏只展示「最近一次」调用；多条并发请求时只显示最后结束的那一条（SSE 的
  `currentRid` 比较逻辑）。
- 反向联动抖动兜底方案（`native.ResizeEnd`）未实装，**待 WinForms 实测抖动
  明显时启用**。

### 侧栏头部字段在 done 后仍空白（v0.95 修复）

v0.94 修了切换 400 之后，用户开「实时流」侧栏**单测 OK**，但真跑了一次
非流式响应，**侧栏立刻从有数据变成「入向→出向 —」、api-key 「（明文待加载）」**。
两条独立 bug 缠在一起，构成双重故障。

#### A. snapshot 路径跳过 done 条目

`routers/stats.py:live_stream` 首连时循环 `get_inflight_snapshot()`，原代码是：

```python
for snap in get_inflight_snapshot():
    if snap.get("phase") == "done":
        continue    # ← 跳过已完成条目
    ...
    break
```

设计意图：「侧栏只展示最近一次调用」+「已完成就别再来烦我了」—— 但**侧栏也
需要展示最近一次**已完成**调用**（用户开侧栏往往就是冲着已经结束的那条去
看 wire/api-key/全文）。**v0.94 实施时这个 skip 写过头了**，把整个 done 类
排除在首连 snapshot 之外。

修：去掉 `if phase == "done": continue`，首连直接拿最近一条（含 done）广播
snapshot 事件。注释里也讲清楚 v0.94 改了什么。

#### B. done 事件不补齐侧栏头部字段

`proxy._broadcast_live_event` 在 `kind="done"` 时只透传调用方给的 payload。原
始调用方（`_relay_cross_wire` / `_anthropic_adapter_relay_sse` / `relay()`
finally）**绝大多数**只传 `assistant_text / thinking_text / tool_use_json /
usage_live / error` —— **上游 / 平台 / 模型 / wire / api-key 一个都没传**。
live_panel.js 的 `done` handler 之前也只更新 stream / thinking / usage / tools，
上游/平台/模型/wire/api-key 五项根本不写。

后果：流式响应侧栏一直在动 → 切到非流式，done 事件一来 → 头部五项瞬间空白，
直到下一次流式请求才恢复。

修：
- `live_panel.js` 的 `done` 分支补齐 `setText($upstream, ev.upstream)` /
  `setText($platform, ev.platform)` / 模型行 / `applyWire(ev.inbound_wire,
  ev.outbound_wire)` / `applyCleartextKey(ev.request_id)` 五处写入，与
  `snapshot` 分支对齐（snapshot 写啥 done 也写啥，双保险）。
- `proxy.py:_broadcast_live_event` 在 `kind == "done"` 时**自动**从 inflight
  字典（`_in_flight` 或 `_in_flight_done`）补齐 6 个字段
  （`upstream / platform / model / client_model / inbound_wire / outbound_wire
  / api_key`），payload 里已有的优先（`setdefault`）—— 给上游类显式覆盖留
  口子。新增 helper `_find_done(rid)`（`proxy.py:1085-1090`）查 `_in_flight_done`
  列表（不弹出，仅引用）。

`_find_done` 的存在使得 done 事件到达时即便 `rid` 已从 `_in_flight` 弹出
（finally 后调 `promote_done`），仍能从最近 32 条已完成 inflight 里找回
头部字段。`MAX_DONE_VISIBLE`（`proxy.py:840`）= 32，确保历史够查。

#### 三层防护

| 层 | 文件 | 职责 |
|---|---|---|
| snapshot | `stats.py:226-232` | 首连 / 重连时取最近一条 inflight，**含** done —— 给晚开侧栏的用户一份「最近一次」的完整快照 |
| broadcast | `proxy.py:1069-1092` | **delta + done** 事件自动从 inflight 字典补齐 6 个头部字段 —— 不依赖调用方记得传，**也不**依赖前端 reset 后记得从 payload 写回 |
| UI | `live_panel.js:278-318` | delta + done handler 都写入 upstream/platform/model/wire/api-key —— 不依赖事件里有这些字段（双保险）|

任意一层失效，另两层都还能让侧栏显示完整。

#### 经验沉淀

- **「最近一次」是双向的**：包含 done 比只包含 in-flight 更对 —— 用户
  开侧栏常常是为了看**刚完成**的请求的 wire/api-key/全文，不是为了盯
  当前流。「跳过已结束」是典型的"想当然简化"陷阱。
- **广播事件字段补齐要在 broadcaster，不在每个 caller**：
  `_broadcast_live_event` 是单一漏斗，所有路径的 done 都过它；让 broadcaster
  统一补齐头部元数据，**单一改动覆盖 7 处插桩点**（详见上方「广播插桩点」
  表）。如果把补齐散落在各 caller 的 finally，必然漏改。
- **snapshot + done 双保险不是冗余**：snapshot 是「**过去**某刻的完整状态」，
  done 是「**刚才发生**的事件」—— 时序错位（GUI 重连 / 切到新页面）下，
  snapshot 给兜底；正常路径下 done 给最实时的。两条都要写对，缺一就露馅。

#### C. delta 期间头部 5 项被清空后不再补回（v0.95 续）

v0.95 第一刀修了 done 后空白，但**截图（流式过程中、status=流式中、正文已累积
+ 头部空白）证明**侧栏还有第二处空白 —— 用户开侧栏时正好有**旧条目**已 done，
新请求 streaming 中，触发时序：

1. **GUI 开侧栏** → SSE 首连 → `get_inflight_snapshot()` 返回最近一条（旧条目，
   已 done，字段齐全） → snapshot 事件推过去 → 前端 `reset()` 清空旧数据 →
   写入头部 5 项 → 显示**旧请求**的元数据。
2. **新请求开始** → 第一条 delta 事件过来 → 前端 `currentRid !== ev.request_id`
   → `reset(ev.request_id)` **再次**清空头部 5 项 → 只设 `（流式）`占位 +
   `setPhaseBadge("streaming")` → **头部 5 项瞬间空白**。
3. **后续 delta 一直推** → 但 delta payload **不带**上游/平台/模型/wire/api-key
   5 字段（原始 7 处 delta 插桩只传 `assistant_text / thinking_text / usage_live`）→
   **头部一直空白到 done**。
4. **done 事件** → v0.95 第一刀的自动补齐生效 → 头部 5 项终于填上。

**截图正是 step 3 的中间态** —— 状态徽章 `流式中`、正文累积中、上游/平台/模型/
wire/api-key 全空白。

#### 修复（v0.95 续）

- **后端**：`proxy._broadcast_live_event`（`proxy.py:1069`）的 `if kind == "done":`
  改为 `if kind in ("delta", "done"):` —— delta 事件也走 inflight 字典补齐 6
  字段。`_find_done(rid)` 复用 —— delta 期间 rid 在 `_in_flight`，done 后才进
  `_in_flight_done`，但 `_in_flight.get(rid) or _find_done(rid)` 表达式两边都
  覆盖。文档字符串顶部注释新增「v0.95：delta 同样自动补齐」一段。
- **前端**：`live_panel.js:278-318` 的 delta 分支 `currentRid !== ev.request_id`
  走 reset 后，**用 payload 里的字段写头部**（upstream/platform/model/wire/
  applyCleartextKey），`upstream` 缺位时回退 `（流式）`占位以兼容老的 7 处
  delta 插桩（理论上不会再触发，但兜底不要省）。这样 delta 自己也是「snapshot
  + done 双保险」的第三角。

#### 经验沉淀（v0.95 续）

- **reset() 是清空 + 重置，不是无脑清空**：`reset()` 把所有字段回默认占位
  是对的（避免上一条的尾巴留在这条开头），但**写头部字段的责任必须随 reset
  一起来** —— 任何 reset 后必须紧跟一次「从 payload 或 inflight 写头部」的
  调用。delta 分支之前漏了，done 分支 v0.95 第一刀补了，v0.95 续把 delta
  分支补齐。
- **「事件不带字段」和「前端不写字段」必须同步设计**：广播层不带 + 前端不写
  = 双重空白，且**两端都觉得自己做了对的事**。把补齐逻辑收敛在 broadcaster
  一处，前端就只负责「**有就写、无就空**」的镜像 —— 不会出现「该带的没带、
  该写的没写」的对称缺失。
- **截图是真相的唯一来源**：v0.95 第一刀 commit 后我说「done 后空白修复完成」，
  但截图里 status=流式中 + 正文累积 + 头部空白 —— 说明**还有未修复路径**。
  单元测试只能覆盖 done 路径（test_proxy_integration 的 `assert_last_event` 只
  断言最终态），流式过程中的中间空白要靠**真实运行截图**才能暴露。今后
  类似 SSE 增量 UI 的修复必须等用户「跑一次流式 + 截图」才算闭环。

#### 用户验证（v0.95 全量）

- 触发一次流式调用 → 第一条 delta 一来就应直接显示上游/平台/模型/wire/api-key
  （不再空白等待）；后续 delta 持续累积正文，头部 5 项保持显示 → 流式结束时
  done 事件再次写定（无视觉跳变）。
- 触发一次非流式调用（`curl -X POST .../messages` 不带 `stream=true`，或
  客户端发非流式请求） → 头部字段**仍然完整**显示，不会变 `—` / `（明文待加载）`。
- 33/33 proxy_integration + live_stream 测试通过（v0.95 第一刀 100/100 不变）。

#### 关键文件

| 文件 | 改动 |
|---|---|
| `src/relay/routers/stats.py` | `live_stream` snapshot 循环去掉 `if phase == "done": continue` |
| `src/relay/proxy.py` | `_broadcast_live_event` 在 `kind in ("delta", "done")` 时自动从 inflight 字典补齐 6 字段；新增 `_find_done(rid)` helper |
| `src/relay/web/live_panel.js` | delta + done handler 都写入 upstream/platform/model/wire/api-key —— 不依赖事件里有这些字段（双保险）|

### 侧栏正文流不自动滚到底（v0.95 第二轮 · Bug 1）

v0.95 第一刀 + 续把头部字段补齐后，用户实测流式响应：状态徽章 `流式中`、
头部 5 项齐全、正文累积——但**正文停在屏幕中间，新字符在下面被截掉**，必须
手动滚到底才能看新内容。

#### 根因

`live_panel.js:isSticky()` 的 slack = 4px 太严格：

```js
function isSticky() {
  const slack = 4;          // ← 太严
  return $stream.scrollTop + $stream.clientHeight >=
         $stream.scrollHeight - slack;
}
```

正文累积 → `scrollHeight` 增长 → 用户鼠标哪怕轻触一下滚轮 / 触摸板
（5–15px）→ 距离底部就超过 4px → `isSticky()` 立刻返回 false → `applyStreamText`
不再自动滚底 → 正文定在用户上次位置。

测试机（无鼠标）感受不到：开发期是键鼠自动重载脚本，根本没滚轮事件干扰。
用户机器（触摸板 / 鼠标）一旦碰一下滚轮就触发。这是**典型的"开发环境 vs
真实环境"不一致**导致的漏网 bug。

#### 修复（两层叠加）

1. **slack 4 → 24px**：覆盖触摸板轻触 / 滚轮咔哒声的滚动量。`clientHeight`
   通常 300–500px，24px 占比 < 10%，不影响视觉（滚到底 vs 差 24px 几乎看不出）。
2. **粘底状态记忆**：模块作用域加 `let _lastSticky = true; _lastStickyAt = 0;`
   —— 上一次 `isSticky()` 返回 true 时，**接下来 500ms 内即便当前不粘底也
   仍粘底**：

   ```js
   if (_lastSticky && (now - _lastStickyAt) < 500) {
     return true;
   }
   _lastSticky = false;
   _lastStickyAt = now;
   return false;
   ```

设计意图：只有用户**主动向上滚一段距离（>50px 一次性）**才算放弃粘底——
反映真实翻页意图。短暂不粘底（滚轮咔哒、touchpad 误触）保留粘底。

#### 经验沉淀

- **slack 不是"无限小就精确"**：4px 是理想化的"我就在底"判定，但**没有任
  何鼠标设备能精确停在 4px 内**。合理 slack 应当 ≥ 一次最小滚动事件位移
  （典型 5–15px），再 ×2~3 余量。**24px 是经验值**。
- **粘底判定应该是"惯性"而非"瞬时"**：用户视野焦点从底部挪到正文里看一段
  旧内容，再回来到底部——这是合法 UX。但正文累积时短暂脱离底部（滚轮轻触）
  不该让用户每次手动滚回去。500ms 记忆窗口是「一次最小阅读节拍」的近似。
- **真机触摸板/鼠标测试不可省**：触摸板和鼠标的滚动事件粒度（每 tick 多少
  px）vs 自动化测试的 synthetic 事件差异巨大。**带滚轮的设备亲手测一次**是
  这类 UX 修正的最低门槛。
- **不要相信"测试通过"**：单元测试覆盖的是 `_InFlight` 状态机和 parser 逻辑，
  前端 DOM 交互（滚动 / 点击 / 触摸）行为**完全不在测试覆盖范围**。回归靠
  截图，自动化靠 Selenium / Playwright（本期未引入）。

### api-key 明文通道：SSE 取代 pywebview 桥（v0.95 第二轮 · Bug 2）

v0.95 第一刀 + 续把头部 5 项（含 api-key 掩码）都填上了，但**明文 api-key
字段一直显示「（无）」**。截图：

```
api-key: （无）
```

#### 根因（两进程架构的必然）

GUI 与 Relay 是**两个独立 Python 进程**：

- **Relay（uvicorn 子进程）**：收到 HTTP 请求、解析 usage、写 `relay.db`，
  在 `proxy.py` 模块级维护 `_in_flight` / `_in_flight_done` 字典——**所有 HTTP
  请求都走它**。
- **GUI（pywebview 壳）**：宿主前端；通过 `window.pywebview.api.*` 调
  `gui.py:Api` 的方法；GUI 自己 `import relay.proxy` 拿到的是**GUI 进程内**
  的 `proxy` 模块副本，**`_in_flight` 永远是空的**（HTTP 请求从来不到 GUI）。

`gui.py:get_live_panel_api_key(rid)` 内部：

```python
from relay import proxy as _proxy     # ← GUI 进程的 proxy 模块
clear = _proxy._in_flight.get(rid)   # ← 永远是 None
```

桥永远返回空串 → 前端 `applyCleartextKey` 拿到空 → 显示「（无）」。

第一版（v0.95 之前）的注释里**根本没意识到这一点**，以为「桥能拿到 inflight」。
直到 v0.95 续截图打脸。

#### 修复（架构层面：SSE 携带明文）

让 SSE payload 直接带 `api_key_cleartext`，前端 `applyCleartextKey` 优先用
payload 里的明文，**不再调 pywebview 桥**。

**后端**（`routers/stats.py:live_stream` snapshot 循环）：

```python
for snap in get_inflight_snapshot():
    rid = snap.get("request_id")
    cleartext = ""
    if rid and rid in _in_flight:
        cleartext = getattr(_in_flight[rid], "api_key", "") or ""
    elif rid:
        for inf in _in_flight_done:
            if inf.request_id == rid:
                cleartext = getattr(inf, "api_key", "") or ""
                break
    if cleartext:
        snap = {**snap, "api_key_cleartext": cleartext}
    payload = json.dumps({"type": "snapshot", **snap}, ensure_ascii=False)
    yield f"data: {payload}\n\n".encode("utf-8")
```

snapshot / delta / done 三个事件都有 `api_key_cleartext` 字段（`proxy._broadcast_live_event`
已经在 done 时设了，snapshot 由 `stats.py:live_stream` 自己拼，delta 走
`_broadcast_live_event` 的 `kind in ("delta", "done")` 自动补齐）。

**前端**（`live_panel.js:applyCleartextKey`）：

```js
function applyCleartextKey(rid, cleartext) {
  // 优先 SSE payload（v0.95 第二轮）
  if (typeof cleartext === "string" && cleartext.length) {
    setText($key, cleartext);
    return;
  }
  // 旧桥 fallback（仅过渡期有效，GUI 重启后永远拿到 ""）
  if (window.pywebview && window.pywebview.api.get_live_panel_api_key) {
    window.pywebview.api.get_live_panel_api_key(rid || null)
      .then(r => setText($key, (r && r.api_key) || "（无）"))
      .catch(() => setText($key, "（读取失败）"));
  } else {
    setText($key, "（无）");
  }
}
```

#### 攻击面评估

SSE 监听 `127.0.0.1:8088/live/stream`（loopback），**任何能访问 8088 的客户端
已经能调 `/v1/messages` 转发任意请求了**——明文 key 走 SSE 不扩大攻击面。
完整推论：

- 明文 key 本来就在 `_InFlight.api_key` 里（存内存对象，不是 db）。
- 已有路径能暴露它：bridge（已存在）、`/api/upstreams` 列出 key 列表（已存在）。
- SSE 走 loopback 是这 3 条里**最局部**的（仅 `127.0.0.1:8088`）。

攻击场景：恶意本地程序能调 `http://127.0.0.1:8088/v1/messages` 发请求 →
relay 用上游 key 转发 + 计费；同一恶意程序连 SSE 也能拿到 key。
**等同攻击面**。

#### 经验沉淀

- **多进程架构的模块级状态是反模式**：`from relay import proxy as _proxy`
  在两个进程里拿到的是**两个模块副本**，state 永远不同步。任何"看着像
  同进程调用、其实是跨进程 IPC"的代码都是雷。**架构层面承认两进程 = 所
  有共享状态必须经 IPC（HTTP / 文件 / SSE / 命名管道）。**
- **「复用现有桥」前先验证桥能拿到数据**：v0.95 之前的注释想当然"bridge 调
  `get_live_panel_api_key` 拿 inflight"，没考虑 GUI 进程的 inflight 是空的。
  **写跨进程 IPC 前先 `print` 一下对端能不能看到状态，10 秒验证胜过 10 分钟
  注释。**
- **明文 key 不需要再藏**：本来就是「用户跑的中继代理 + 用户自己的 key」，
  GUI 进程要拿明文去显示给用户看——**用户已经知道自己的 key**，多绕一道
  IPC 只是延迟和出错机会，不是安全收益。SSE 直传 = 最简最快。
- **删 dead code**：桥的 `get_live_panel_api_key` 现在**永远返回 ""**——
  下一轮可以删。fallback 块留 1 个版本过渡，verify 后清。

### 关键文件清单（v0.95 第二轮）

| 文件 | 改动 |
|---|---|
| `src/relay/routers/stats.py` | `live_stream` snapshot 循环里从 `_in_flight` / `_in_flight_done` 取明文 api-key，写入 `api_key_cleartext` 字段 |
| `src/relay/web/live_panel.js` | `isSticky()` slack 4→24 + 500ms 粘底记忆；`applyCleartextKey` 优先用 SSE payload 的 `cleartext` 字段，桥 fallback 仅过渡期保留 |

不动：`proxy._broadcast_live_event`（v0.95 续已给 done 事件带 cleartext，
delta 走 `kind in ("delta", "done")` 自动补齐），`gui.py:get_live_panel_api_key`
（保留作 fallback，verify 后可删）。

#### 已知上游行为：流式中间窗口 usage 全 0（v0.95 排查，**非 bug**）

v0.95 头部字段修复闭环后，用户实测反馈「**侧栏显示流式中、正文有字符，但
token 四字段全 0、命中率 `—`**」。截图三态：

1. **全 0 + 正文有**：message_start 已过、message_delta 还没到
2. **完整 + 正文有**：message_delta 已到，正常
3. **完整 + 正文空**：message_delta 已到、当前 content_block 已结束 / 新 block 未开始

**根因（直接抓上游原始 SSE 验证）**：

上游 `minnimax.chat`（配置里 e1701fa6）发的 `message_start`：

```json
"usage":{"input_tokens":0,"output_tokens":0,"service_tier":"standard"}
```

而 `message_delta` 才是：

```json
"usage":{"input_tokens":41,"output_tokens":6,"cache_read_input_tokens":128,"service_tier":"standard"}
```

**对比 Anthropic 官方**：`message_start.usage` 应含 `input_tokens /
cache_creation_input_tokens / cache_read_input_tokens` 三字段（output_tokens
在 message_delta 才给）。`minnimax.chat` **不发 cache 字段在 message_start 里**
—— 这是**上游协议不完全合规**的表现，不是 relay bug。

**侧栏时序（一次请求内的 usage 演变）**：

| 阶段 | 上游 event | parser 吸收 | 侧栏显示 |
|---|---|---|---|
| 起始 | `message_start` | input=0, output=0（service_tier 字段忽略） | 全 0 / 命中率 `—` |
| 文本累积 | `content_block_delta`（多次） | 不动 usage；累积 `assembled_text()` | **全 0 / 命中率 `—` + 正文累积中** ← **截图里的中间态** |
| 文本结束 | `content_block_stop` | 不动 usage | 全 0 + 正文定格 |
| 收尾 | `message_delta` | input=41, output=6, cache_read=128 | 跳到完整值 |
| 完结 | `message_stop` | 不动 usage | 维持完整值 |

**判定**：是上游 `message_start → message_delta` 之间**自然的消息时序**，**不是
bug**。**正文先于完整 usage 出现是 Anthropic 协议设计决定的**（输入侧先告诉你
prompt 多大、输出侧最后告诉你生成了多少）。

#### 「done 后 cache 仍 0000」（Bug 3 结论）

用户在 v0.95 第二轮修复后实测：**截图七**（done 事件后）四字段仍为 0。一开始
怀疑是 parser bug，最终定位是**截图时机 + 上游 message_start 不带 cache 字段**
的复合：

1. 用户截图时机——done 事件已发，但 `message_delta` 的 usage 数据**还没到侧栏**。
   实际 message 协议顺序是：
   ```
   message_start → content_block_delta* → message_delta → message_stop
   ```
   - `done` 事件在 `proxy.py:relay()` 的 `finally` 发出，**不等 `message_delta`**
     （因为 message_delta 走 SSE parser 路径，done 走 HTTP 响应结束路径，两
     条独立路径；用户在 message_delta 之前的瞬间截图就会拍到全 0）。
   - `minnimax.chat` 的 `message_start` 不带 cache 字段，message_delta 才是
     真值；如果 done 已经发而 message_delta 还没到，cache=0 是**中间态**而非
     终态。

2. **截图触发的时间窗**——done 触发后到 message_delta 触发之间存在 race
   window（约几十 ms）。自动 reload 期间未发新请求，旧条目 hit 这个窗口的概率
   比正常流式还高（因为旧 inflight 在 `_in_flight_done` 里被新 done 立刻覆盖）。

**验证**：直接抓上游原始 SSE（`curl -N`），看到 `message_delta` 里
`cache_read_input_tokens: 128` —— 字段**有**，只是**来在 done 之后**。relay
parser 用 `max()` 累积而不是覆盖，**任何后续到达的 usage 都会刷新侧栏**——
所以这不是漏解析，是 race。

**判定**：**非 bug**，是上游协议时序 + 截图时机叠加的"看起来像 bug"。侧栏
UX 上仍然有改进空间（见下方「未来改进」）。

#### 经验沉淀（Bug 3）

- **「done 之后还 0」可能是 race window 而非丢失**：上游 message_delta 的
  usage 数据在 done 事件之后到达是合法协议时序，截图卡在这个窗口=看起来
  像「done 后还是 0」**。**直接 `curl -N` 看上游原始 event 顺序是最快的
  鉴定手段。
- **`max()` 累积 = 防御性 parse**：`UsageAcc` 走 `max()` 而不是覆盖，意味着
  **晚到的 usage 仍能刷新显示**——只要侧栏还在监听 SSE，最终值**一定**对。
  但「最终值多久到」是上游决定的，relay 无法控制。
- **多请求并发时的 inflight 复用**：`_in_flight_done` 是 LRU 32 条，done 之后
  rid 移到 `_in_flight_done`；如果另一条新请求开始 broadcast 同一个 rid（理论
  不可能，rid 是 uuid）会复用，但 UUID 撞库概率≈0，**实际无需担心**。

**未来改进（可选，未实装）**：给 `UsageAcc` 加 `populated: bool` 字段，任何
`_absorb_usage` 调用后置 True。`live_panel.js:applyUsage` 在
`!usage_live.populated` 时把 input/output/cache 显示为 `—`（含义「未知」）而非
`0`（含义「确实是 0」）。这样截图里的中间态会更准确表达「尚未到达」。

#### 完整抓帧核验（2026-08-18 实测，`minnimax.chat` 真实上游）

用 `scripts/debug_dump_sse.py`（有效 key f3af39d7，MiniMax-M3，max_tokens=64）
抓完整原始流，逐帧对照官方 Anthropic 协议。**结论：事件骨架全部标准，差异
只有两处，都集中在 usage 展示**。

流式 8 帧原始序列：

```
message_start → ping → content_block_start → content_block_delta×2 → content_block_stop → message_delta → message_stop
```

| 帧 | 标准核对 | 差异 |
|---|---|---|
| HTTP | `text/event-stream` ✓ | — |
| `message_start` | 结构标准（id/model/role/type/content[]） | **usage 全 0 占位**：`{"input_tokens":0,"output_tokens":0,"service_tier":"standard"}`，无 cache 字段，input 也是 0 非真值 |
| `ping` | 官方 keepalive 事件 ✓ | 时序略早（message_start 后立即发），合法 |
| `content_block_start` | `{"type":"text","text":""}` ✓ | 标准 |
| `content_block_delta`×2 | `delta:{"type":"text_delta","text":"…"}` ✓ | 标准 |
| `content_block_stop` | ✓ | 标准 |
| `message_delta` | `delta:{stop_reason}` + usage ✓ | **真值在这**：`input_tokens:43, output_tokens:6, cache_read_input_tokens:128` —— input+cache 位置错（规范应在 message_start） |
| `message_stop` | ✓ | 标准 |

非流式（`stream=false`，HTTP `application/json`）：顶层多**自建信封字段**
`"base_resp":{"status_code":0,"status_msg":""}`（MiniMax 自家 API 风格，官方
没有 —— 四类不规范之「自建字段」）；缺 `stop_sequence`（官方 null 也发）；
但 usage **一次给齐**（input/output/cache_read/cache_creation 全有）—— 非流式
反而比流式标准。

**精确化原「除了 cache 其它都标准」的说法**：不成立 —— 差异是「**usage 整体
后置**」（message_start 占位 0 → 真值全塞 message_delta，不止 cache，input 也
是占位 0）+ 非流式自建信封。**relay `max()` 累积 + 宽容解析正好吸收这两处，
最终值正确，判定非 bug**。抓帧方法沉淀：用有效 key 直接 POST 上游
`/v1/messages` 逐帧看，比读代码/猜时序快 —— 本次从「怀疑」到「铁证」一次
调用（2 次调用，各 64/16 token）。

#### 经验沉淀

- **协议不标准上游是常见现象**：minnimax.chat / 各类「Anthropic 兼容」网关
  实现程度参差不齐（缺字段、字段错位、event 名拼写差异都见过）。parser 走
  `max()` 累积而非覆盖、遇到未知 event 直接 `pass` 是关键 —— **保证哪怕上游
  不完全合规也不崩**。但**完全合规的上游消息时序**用户不一定熟悉，看到
  「0」会以为是 bug 报告 —— 这是侧栏 UX 改进空间。
- **debug 时直接 curl 上游拿原始 SSE 比读代码快 10 倍**：绕开 relay 看上游
  真的发了什么 event / 哪些字段在哪个 event 里 —— 30 秒定位（本次从「现象」
  到「根因」耗时），不用猜 parser 逻辑。`grep "event:\|data:"` 就能看清整个
  协议时序。
- **截图三态分析套路**：用户描述 bug 时如果只说「数字不对」，让 ta 截**多张
  不同时刻**的图（流式刚起 / 中段 / 收尾）。本次三张截图一次定位到「message_delta
  之前正常为 0」结论，单图反而会误导。

#### ⚠ 上游计数波纹：峰位置语义 vs 可见窗口（v0.96，重要）

`background-size:200% 100%` 下，**可见窗口 = 背景图的一半（50%）**。背景
`background-position` 动画 -100%→100% 时，窗口扫过背景图的 **[50%,100%] → [0%,50%]**
（峰在背景图里的位置 w 要同时满足「进入窗口」才可见）。

**坑**：原来 `--flash-width`（默认 88%）= **峰在背景图里的位置**。动画全程窗口
只扫 [50,100] 和 [0,50] 两半，峰在 88% 处几乎不进入窗口 —— 窗口只显示峰的
左翼渐深段，视觉上就是「**左浅右深**」怪相。补右翼对称渐隐也没用（第一轮修复
失败，用户复验"动画没变"），因为峰根本不在窗口内。

**修**：`--flash-width` 语义改为**波总宽**，峰固定在背景图中心 50%，半宽 =
`flash-width/4`（默认 88% → 波宽 44% < 窗口 50%，对称波完整可见）。渐变：
`transparent 0% → color-mix(46%) calc(50%-w/4) → color-mix(88%) calc(50%-w/8)
→ 纯色 50% → color-mix(88%) calc(50%+w/8) → color-mix(46%) calc(50%+w/4)
→ transparent 100%`。keyframes / background-size / 滑动方向不动。改动点：
`.upstream-row::after`（bump，蓝）与 `.upstream-row-decreased::after`（decrease，
绿，**保持 `background-image` 属性名** —— `background` 简写重置 background-size，
v0.57 踩过）两处。

**通用教训**：`background-size:200%` + `background-position` 动画的设计，先算
清楚「峰/亮带在背景图里的坐标 + 窗口可见范围 = 背景图的一半」，再定峰的落点。
峰落在窗口扫不到的位置，再对称的渐变也是白搭。`body.body-flash::before`
（styles:2208）本来就是对称形态，可作对照。

## 上游页自动切换预览（v0.90–v0.91）

设置页勾选「允许自动切换 API」之后，上游页 active 上游行额外展示三段信息：

1. `当前使用：xxx` —— 总是显示
2. `耗尽后将切换到 xxx` / `额度即将耗尽，将切换到 xxx` —— 仅当开关打开且有候选
3. 池内无可用目标：`⚠ 池内无可切换目标，耗尽后保留`
4. 实际切换后：旧 active 行顶贴绿色 `已切换到 xxx` toast，3 秒淡出

后端 `quota_monitor.py:pick_replacement` + `_check_once` + `_switch` 早在 v0.20 就
实现了耗尽自动切换，但**没有任何 UI 提示**让用户预先知道。v0.90 加这一层。

### 数据契约（v0.90 新增）

GUI 上游页拿数据的链路是 500ms 轮询 `/api/snapshot` + `/api/status`。v0.90 把
autoswitch 三个字段从 `/api/settings` 搬到 `/api/status`：

```python
# gui.py:355-363
"autoswitch_enabled": bool(getattr(s, "relay_quota_autoswitch", False)),
"autoswitch_pool":   list(getattr(s, "relay_autoswitch_pool", None) or []),
"autoswitch_at":     float(getattr(s, "relay_quota_switch_at", 0.9)),
```

前端 0 成本：已经在 500ms 轮询链路里，不需要再发请求。

候选选择放在**前端独立算**（`pickHintTarget`），理由：

- 实时性：sig 命中重渲染时算，不用等 500ms tick
- 与后端真切换解耦：后端 `pick_replacement` 仍按阈值过滤避免切过去立即又触发
  切换，前端 hint 是「显示」不是「切换」，候选即便 utilization 已 ≥ 阈值也照
  显示，让用户看到确切去向
- 后端逻辑单测覆盖到位，前端只是镜像渲染逻辑

### 前后端 candidate 选择口径差异（重要）

| 维度 | 后端 `pick_replacement` | 前端 `pickHintTarget` |
|---|---|---|
| 阈值过滤 | `utilization_5h < threshold` 才参与 | **不过滤**，按 utilization 升序取最低 |
| 池 | `pool` 空 = 全部兄弟；非空 = 仅池内 | 同上 |
| 排除 | 排除 active 自身 | 同上 |
| 返回 | 最低 util 兄弟；空池 = 全部 ≥ 阈值 → None | 最低 util 兄弟；全部都不存在 → None |

设计意图：前端 hint 是「告诉用户会发生什么」（哪怕结果是切到一个也已快耗尽
的兄弟），后端是真执行（必须留余量避免雪崩）。两者**口径不一致是有意为之**。

### 文案双态（v0.90.5 调优）

```js
const AUTOSWITCH_HINT_AT = 0.7;   // 与 config.py:relay_quota_switch_at 同步
const label = util >= AUTOSWITCH_HINT_AT
  ? "额度即将耗尽，将切换到"
  : "耗尽后将切换到";
```

设置页**不暴露改阈值的 UI**（硬编码）。后端真切换的阈值在前端只是文案紧急度
的依据，不是用户能调的参数。

### hint 颜色跟随 quota 进度条（v0.90.7）

```html
<div class="autoswitch-hint" data-level="${warning_level}">...</div>
```

`data-level` 直接来自 `by_upstream[name].warning_level`（`tui.py:_classify_warning`，
ok/warn/critical/exhausted）。CSS 用同样的 `--quota-ok/warn/critical/exhausted`
变量，hint 颜色跟 quota 进度条完全一致。**不要在前端重新算阈值**——level 由
后端统一给，前端只挑 token。

### 切换后 toast（v0.90）

模块作用域维护 `lastActivePerPlatform` 缓存。`renderUpstreamsView` 检测
`prev !== current` → 给旧 active 贴 `<div class="autoswitch-toast">已切换到
<b>xxx</b></div>`，3 秒后淡出（`@keyframes autoswitch-toast-fadeout`）。

**零 quota 推送**——复用现有 status 轮询，无新增推送链路。

### ⚠ GUI / Relay 配置双副本陷阱（v0.91 修复）

**症状**：勾选 autoswitch checkbox 后，上游页 hint **不出现**；关闭 checkbox
hint 立即消失（说明前端 sig 缓存 OK，问题在「开关 off 那一面」没触发）。

**根因**：GUI 进程 `self._app.settings` 与 Relay 进程 `app.state.settings` 是
**两份独立的内存对象**。

```python
# gui.py:355  (Api.get_status)
"autoswitch_enabled": bool(getattr(self._app.settings, "relay_quota_autoswitch", False)),
```

`Api.update_relay_settings` 走 HTTP PUT 到 `/api/settings/autoswitch`，更新的是
relay 进程的 `app.state.settings`，并写 `.env`。**GUI 这边的 `self._app.settings`
完全没动**。下一次 `api.status()` 500ms tick 拿到 `autoswitch_enabled: false`，
`renderUpstreamsView` 的 sig 不变（`switchEnabled` 字段没变），跳过重渲染，
hint 不出现。

关掉时为什么正常？JS 端 `lastUpstreamsSig = null` 主动清缓存（`app.js:2745`），
且 `switchEnabled` 从 true → false **也是 sig 变化**——双重保险。打开时
`switchEnabled` 从 false → true 也算 sig 变化，但因为 `self._app.settings` 是
旧值 false，sig 计算用的是 false，结果还是「switchEnabled: false」，**前端
看不出任何变化**。

**修复**（`gui.py:936-942`）：成功后 `reload_settings()` + rebind，模式与其它
7 个写 `.env` 的 handler 完全一致：

| Handler | reload 行号 | 写什么 |
|---|---|---|
| `update_upstream_quota` | 846 | 配额数值 |
| `save_quick_switch` | 879 | quick_switch 模板 |
| `update_advanced_switch` | 1036 | 高级切换配置 |
| `set_upstream_model` | 1317 | 上游 default model |
| `set_upstream_default_model` | 1345 | 平台默认 model |
| `create_upstream` | 1370 | 新建上游 |
| `remove_upstream` | 1409 | 删除上游 |

```python
if isinstance(result, dict) and not result.get("error"):
    try:
        reload_settings()
        self._app.settings = get_settings()
    except Exception:
        pass
return result
```

**这条规律适用于所有走「HTTP PUT 到 relay → relay 写 .env」路径的 handler**。
写新功能时记住：GUI 副本不会自动同步，必须显式 `reload_settings()`。
**审 PR 时可作为 checklist**：`grep -n 'def \w.*\(.*\).*-> dict:$\|reload_settings()'` 看看
新加的 handler 是否走了 reload 模式。

### 关键文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/gui.py` | `Api.get_status()` 加 autoswitch 三字段；`Api.update_relay_settings` 加 reload（v0.91） |
| `src/relay/web/app.js` | 新增 `pickHintTarget`；扩展 `lastUpstreamsSig`；active autoswitch 提示块；`lastActivePerPlatform` + toast 注入；change handler `lastUpstreamsSig = null` |
| `src/relay/web/styles-20260817.css` | 新增 `.autoswitch-current` / `.autoswitch-hint` / `.autoswitch-hint-warn` / `.autoswitch-toast` + `@keyframes`；`[data-level="warn|critical|exhausted"]` 配色 |

不动：`quota_monitor.py`（前端镜像其逻辑）、`/api/settings`（autoswitch 三字
段保留供设置页用）、`routers/api.py`、`live_panel.html/css/js`。

### 调试

| 症状 | 排查 |
|---|---|
| 开启 checkbox hint 不出现 | DevTools 看 `await api.status().autoswitch_enabled` 是否为 true；若是 false → `update_relay_settings` 没 reload，参考 v0.91 修复 |
| 关闭 checkbox hint 不消失 | JS 端 `lastUpstreamsSig = null` 是否在 change handler 里调用（`app.js:2745`） |
| hint 颜色与 quota 进度条不一致 | `by_upstream[name].warning_level` 字段是否传到前端；`tui.py:_classify_warning` 阈值是否漏配 |
| 切换后 toast 没贴 | `lastActivePerPlatform` 模块作用域是否被某次重构改成本地；`active_per_platform` 字段是否还在 `/api/status` 返回 |
| toast 重复触发 / 抖动 | 500ms tick 期间 active 短暂抖动（设置页 PUT autoswitch 期间）会误判切换。可加 200ms 防抖或退化为只信稳定 1 tick 以上的变化 |

### 已知限制 / 未来工作

- **`no_quota` 配色没显式定义**：`warning_level` 实际有 5 个值
  `ok|warn|critical|exhausted|no_quota`（`tui.py:_classify_warning`，level
  = `None` 时返回 `no_quota`）。CSS 只为 `warn|critical|exhausted` 配了规则，
  `no_quota` 上游的 hint 会落到默认的 `--quota-ok`（绿色）—— quota-bar 本体
  这时根本不画（无利用率），hint 却显示绿色，**视觉略不一致**。修法：在
  `styles-20260817.css` 加一条 `[data-level="no_quota"] { color: var(--text-secondary); }`。
- 客户端 model 映射本期未实现（无路由层）—— 只显示上游名。proxy 里现有的
  `fallback_model`（`proxy.py:2310/2327`）是透传路径的「client 请求的模型不在
  该上游 allowed 列表时」兜底改写，不影响 autoswitch UI 的展示口径，等真要做
  model-level 路由层时再扩展 markup。
- 切换 toast 只靠 `lastActivePerPlatform` 单 tick 比对，500ms 抖动窗口内可能误
  报。可加 200ms 防抖或比对 `prev_prev`。

## 发布 / 版本规范

- 版本号递增记录在 `PROGRESS.md`（v0.x.x 条目），改动同步更新 README 的
  对应小节。
- 后端代码、前端代码、数据契约（snapshot 字段）相互耦合，改动跨层时三者要一起
  改、一起测。
- `upstreams.json` / `.env` / `relay.db` 是运行时产物，不入库；改配置逻辑先看
  `upstreams.example.json` 和 `.env.example`。
- **CSS 缓存戳是易忘点**：WebView2 对 file:// 会缓存 CSS。改
  `styles-20260817.css` 后必须 bump `index.html` 里 `<link>` 的 `?v=`
  （`?v=20260821-NN`），否则旧 CSS 一直生效。前端 JS 有版本号参数
  （`app.js?v=...`）同款约定。

---

## 开发文档编写规范（v0.113z 起）

本仓库有两类「开发文档」，写法不同，先分清：

- **主文档 `docs/development.md`**（本文件）：沉淀*跨会话通用*的开发约束、架构
  底层原理、易再犯的坑。改主仓代码前先翻对应章节。
- **会话文档 `docs/dev/*.md`**：一次需求的完整开发记录，是**可追溯的审计文档**。
  命名 `主题_v0.XXX.md`（如 `ui_sidebar_realtime_token_v0.113x.md`），需求完成后
  写，不是每条 git commit 都写。

会话文档严格按下面 7 节固定结构撰写，**顺序固定，不增不漏**：

1. **用户的初始指令**：用户最初说的话**原样贴上**（一字不改，作为锚点）。若
   有澄清问答，按时间顺序逐条列原始提问与回答。
2. **从初始指令中提炼出的实现点**：把原始指令逐条拆成可执行的子目标（表格：
   `# | 实现点 | 来源 | 文件`），并标注「隐含但需自行决策的点」。
3. **分析需求后得出的开发路径**：写怎么拆解、用什么方案、按什么顺序实施。
4. **实现中遇到的问题**：列开发过程中踩到的坑、与原计划不符的发现。
5. **最后如何解决**：针对每个问题写实际采用的解法。
6. **是否完全遵循规划路径开发**：诚实写「完全按规划 / 部分偏离 / 重大调整」，
   并说明理由。
7. **最终实现点**：列出最终真正落地实现的功能点（不是原计划，是事实）。

### 追加规则：末尾按版本号顺序追加，中间可改但保留标签

一个会话文档需要记录多次会话的演进时（例如 `ui_sidebar_realtime_token_v0.113x.md`
存了 v0.113x，后续又做 v0.113y 增强）：

- **新增内容追加在文件末尾**，用版本号（`## v0.113y 追加（YYYY-MM-DD）`）作
  分隔锚点，**禁止插入中间打散历史段**。历史段位置不动，便于按时间线扫读。
- **纠错 / 补充事实可以改旧段**，但**必须保留段落顶部的版本号标签**作为溯源
  锚点。改动较大（重构逻辑、推翻旧结论）时，在段首加一行
  `> v0.113y 修订：……` 注明改动时间与原因。
- **严禁**：把新内容插入中间打散旧段；把旧段搬走重排版破坏锚点。

### ⚠ 写之前先读格式参考（重要）

上方 7 节是骨架，但**很多具体格式细节**（实现点表格化、`### 问题 N` +
加粗**解法**、§6 三档分列、§7 按模块分小节 + 行为验收 `- [ ]` 清单 +
修改文件清单表格、版本号标题等）骨架里没写全。

**撰写前必须先 Read 参考文档掌握具体风格，不要只满足于骨架：**

`docs/dev/live_panel_concurrent_v0.104.md`

把这篇作为格式模板，然后再开始写新的会话文档。

### ⚠ 重要底层约束要提高权重（重要）

协议、架构等「所有改动都必须遵守的底层约束」内容，写进 `docs/development.md`
时用 **`### ⚠ …（重要）`** 标题，放在它管辖的代码区域章节的头部（如「后端
开发」下紧跟协议章节），**不要埋在版本修复流水账里**。

---

## v0.102 大改动开发指南

v0.102 起主仓有三块新东西：完全透传模式 UI 隔离 + 三极消耗口径开关 + 总览
卡片管理。本章把这三个新模块的开发约定一次写清，避免后人重复踩坑。

### 完全透传模式（passthrough mode）

`body.passthrough-mode` 类由 `renderAll` 按 `snap.passthrough_mode` 切换
（`app.js:1897`），是整套 UI 隔离的**单一驱动开关**。开启后所有非透传相关的
section / 侧栏控件都被 `styles-20260817.css` 的 passthrough-mode 规则组
统一隐藏（`styles-20260817.css:3760` 附近，规则 1~5）。

**白名单（透传模式下保留）：**
- `settings-passthrough`（透传模式开关）
- `settings-passthrough-upstreams`（透传上游自动发现）
- `settings-relay-mode`（中继模式双极按钮，v0.102.2 顶部新 section）

**加新 section 时**：若该 section 在透传模式下应隐藏，无需额外 CSS（已被规则 1
通配排除）；若应保留，把它加到 `:not([data-card="..."])` 白名单。**别**在
JS 里手动 toggle hidden —— body 类是统一驱动，再叠 JS 控制会出现状态不同步。

**设置页结构（v0.102.2 后，顶到底）：**
1. `settings-relay-mode` —— 加粗"中继模式"标题 + 双极 pt-mode-switch
   （renderSettingsPassthrough 渲染到此 body），切中继模式的核心开关
2. `settings-passthrough` —— "数据显示"section 标题 + 标题旁三极 consume-
   switch-inline（仅透传/全部/仅转换）
3. `settings-passthrough-upstreams` —— 透传上游自动发现列表
4. `settings-relay`、`settings-prefs`、`settings-config`、`settings-quickswitch`
   —— 透传模式下被规则 1 整体隐藏

### 三极消耗口径开关（consume switch）

胶囊分段控件，部署在：
- 总览页顶部 `consume-toolbar`（标签"数据显示"）
- `settings-passthrough` section 标题旁（`consume-switch-inline`）
- 统计页工具栏是**另一套**模式按钮组（`stats-mode-group`，不共用）

**全局状态**：localStorage `consume-mode` 持久化（`passthrough`/`all`/`relay`），
模块变量 `window._consumeMode` 缓存。切换档位的流程：

```
用户点按钮 → setConsumeMode(m)
  ├── window._consumeMode = m
  ├── localStorage["consume-mode"] = m
  ├── 同步所有 .consume-switch 的 active 类
  └── silent=false:
       ├── currentView==="overview":
       │     lastOverviewMode=null（强制 renderAll 判档位变化）
       │     ptOverviewCache=null（清透传缓存）
       │     lastCardsSig=null（强制重渲卡片）
       │     renderAll(lastSnap, lastStatus)
       │         → mode!=="relay" 时 fetchPtOverview(true)
       │         → then 回调 renderAll 用 ptOverviewToSnap/mergeSnap 渲染
       └── currentView==="stats":
              statsState.mode=m; syncStatsModeButtons(); renderStats()
```

**档位数据契约**：
- `relay` —— 直接用 snapshot（relay.db）
- `passthrough` —— 用 passthrough.db 的 `/api/passthrough/overview`
  （`passthrough_overview` 桥代理 `/api/passthrough/overview?range=1d`，
  totals / by_upstream / by_model / by_platform(host 分组) / by_hour / recent）
- `all` —— 前端 `mergeSnap(relay, pt)` 合并：model 按 key 相加，
  upstream 拼接且透传行加 `[透传]` 前缀，hour 桶对齐相加，recent 按 ts 合并

**数据契约层**（`src/relay/passthrough/db.py`）：
- `aggregate_by_dim(dim, since)` —— 行键 `key`、input/output/cache_read/
  cache_creation/total_tokens/errors；v0.102 修了 column offset bug（之前的
  `int(r[1])` 会把 'model' 列当数字崩 `invalid literal`）
- `fetch_by_hour(since)` —— 新增，结构对齐 `tui.fetch_by_hour`（hour, requests,
  in_tokens, out_tokens, tokens），让前端能合并 relay + pt 小时桶
- `fetch_recent(limit)` —— 已存在，最近 N 行

**路由层**（`src/relay/routers/api.py:539`）：
- `GET /api/passthrough/overview?range=1d|7d|30d` —— 一次返回 overview
  全部字段；非法 range 返 `{"error": ...}`（与 `passthrough/stats` 同口径）

**桥层**（`src/relay/gui.py:582`）：
- `Api.passthrough_overview(range="30d")` —— pywebview 桥，urllib 代理 HTTP，
  timeout 3.0。失败返 `{error: ...}` 或抛异常（被前端 `_call` 的 try/catch
  捕获，返 `{error: "bridge_unavailable"}`）

**前端缩略映射**（`app.js` `ptOverviewToSnap`）：
- by_upstream key 用 `ptUpstreamLabel` 转成 `host|field`（短键名）
- by_model 行经 `ptModelRow` 把 cache_read+cache_creation 拆出来作
  `cache_tokens`，input 减去 cache，保持 spotlight 三段条 input+output+cache
  口径正确

**关键坑**：
- `fetchPtOverview` 的 promise 用 `ptOverviewPromise` 做并发短路；失败用
  `ptFetchFailed` 标记 + 5s 重试节流（`PT_RETRY_AFTER_FAIL`），避免每 tick
  反复打后端
- renderAll 在 ptLoading 时**保留旧卡片内容不闪空**，数据到后用 `cardFadeIn`
  淡入（`styles-20260817.css` 关键帧），sig 用 `lastRenderedMode` 判断
  切档才淡入，避免同档 tick 重播动画
- chart.js 复用 `_hourChart` 单例——ptLoading 占位覆盖 canvas 前必须
  `_destroyHourChart()`，且复用分支检查 `_hourChart.canvas.isConnected`，
  否则 chart 更新到脱离 DOM 的 canvas 上"消失"

### 总览卡片管理

localStorage `overview-cards-v1` 存卡片显隐+顺序数组，`CARD_DEFS` 定义
6 张候选卡（hourly / upstream / today / platform / models / recent）。

**渲染函数多实例支持**：所有 6 卡都用 `forEachCardBody(type, fn)`（按
`.glass-card[data-card="<type>"] .card-body` 选择器遍历 body）替代原
`$("card-xxx-body")` 单实例——用户可创建多个同类型卡（如多个"最近活动"）。

**关键 DOM 模板**（`createCardEl`）：
- 标题行带 `<button class="card-close-btn">` 删除按钮（hover 卡时浮现）
- body class：hourly 卡用 `card-body card-body-chart`（高度依赖），其他
  用普通 `card-body`
- draggable=true 保留拖拽排序（顺序存 `overview-card-order-v1`）

**顺序不互相覆盖**：`ensureOverviewCards` 只在"配置缺失 / 显隐不匹配"时
增减卡 DOM，**不重排已有卡**（避免破坏拖拽顺序）。拖拽顺序由
`overview-card-order-v1` 维护，与 `overview-cards-v1`（显隐）独立。

**FAB 右下角**：`.cards-manage-fab` fixed 定位，点击打开 `#cards-manage-overlay`
弹层（checkbox 列表）。所有事件用 document 级委托（关闭按钮 / 弹层遮罩 / ESC），
动态创建的卡同样生效。

### 关键动效约定

- **卡片 hover 上浮**（`.glass-card`）：`transform: translateY(-3px)` + 阴影
  增强，0.18s 过渡。v0.41 注释曾因 WebView2 transform 触发合成层重算
  而禁用——v0.102.2 重启回归确认无明显性能问题
- **切档淡入**（`.card-fade-in`）：`cardFadeIn` keyframes（opacity 0→1 +
  translateY 6px→0），0.22s ease。**只在档位变化时触发**（`lastRenderedMode`）
- **统计图表淡入**（`.stats-chart-fade`）：同 keyframes 复用。**只在参数
  变化时触发**（`lastStatsRenderKey` = `mode|range|dim`），避免 poll tick
  每 500ms 触发导致"一直闪"
- **上游状态 bump/decrease**：v0.102.2 改用 `live-ripple` 1.8s cubic-bezier
  infinite（直接复制左侧"实时"导航动画），decrease 用 `live-ripple
  infinite reverse`。JS 摘类时间 2s→1.8s（对齐动画周期），避免波纹被
  中途截断
- **STREAMING 跑马灯**（`.live-phase-streaming::after`）：白色光带
  background-position 动画 1.6s 循环扫过，respect prefers-reduced-motion
- **pt-mode-switch 玻璃主题**（`.pt-mode-switch`）：玻璃渐变面板 + 主色渐变
  滑块，hover 用 box-shadow 增强 + 边框加深（不用 transform scale —— 会让
  flex 子元素文字在某些渲染顺序下消失），文字颜色 hover 不变仅 font-weight
  600→700

### 双向同步陷阱

很多 UI 状态需要双向同步，常见三处：

1. **透传模式 toggle ↔ 档位默认**：`renderSettingsPassthrough` toggle
   handler 成功后调 `setConsumeMode(enabled ? "passthrough" : "relay")`，
   **不要**让两个状态各走各路
2. **三极开关 ↔ 统计页 mode 组**：统计页工具栏 mGroup handler 直接
   `window._consumeMode = statsState.mode` + `mountConsumeSwitches()` +
   `renderStats()`；反过来 `setConsumeMode` 切到 stats 视图时也写
   `statsState.mode` + `syncStatsModeButtons()`
3. **设置页 nav-sub 子菜单 ↔ 章节显隐**：透传模式隐藏所有非透传 section，
   但 nav-sub 的"上游配置""快捷切换"子项默认仍显示——它们点击滚动到的是
   已隐藏的 section。v0.102.2 在 CSS 规则 5 把这两个子项一并隐藏
   （`body.passthrough-mode .nav-sub-item[data-settings-sub="config|quickswitch"]`）

**通用原则**：所有双向联动都走单一全局状态 + 单点 mutation 函数（如
`setConsumeMode`），不要在多个 handler 里直接改 DOM / state 后再各自补同步。

### 实测坑（v0.102 修复沉淀）

- **CSS 版本号缓存**：详见上文"发布/版本规范"。改 CSS 忘 bump 是最常踩的
- **stats_chart 一直闪**：`renderStats` 在 poll tick 也跑（currentView=stats
  时走 renderActiveView），不是只在用户切档时跑。`lastStatsRenderKey`
  控制只在参数变化时淡入
- **spotlight 数字/条不对但中文对**：入场动画 `animateIntroValue` 的 rAF
  循环**没有失效机制**。切档重渲时旧动画继续覆盖 DOM。`spotlightAnimGen`
  代际计数器 + `gen !== spotlightAnimGen` 帧检查解决
- **图表面板 offsetWidth=0**：新建 hourly 卡缺 `card-body-chart` 类（高度
  依赖），图表高度 0 看不见。`createCardEl` 给 hourly 补该类
- **暗背景下灰色文字消失**：pt-mode-opt hover 时**不要**用
  `var(--button-primary)` 做文字色（dark 主题 #3a3a3c 与深色玻璃背景撞色），
  保持 `var(--text-primary)` 或加 font-weight 700 即可

### 验证 checklist（PR 评审参考）

- [ ] CSS 改了？bump `styles-20260817.css?v=` 参数
- [ ] JS 改了？bump `app.js?v=` 参数
- [ ] 改了 `body.passthrough-mode` 显示/隐藏行为？同步检查 5 条 CSS 规则
- [ ] 加新 section/视图？确认 `setView` 的 early-return / `renderActiveView`
      覆盖
- [ ] 改三极开关逻辑？同步 `renderAll` overview 分支的 sig 计算（含 mode/
      ptLoading/ptFetchFailed）+ `setConsumeMode` 的 silent 分支
- [ ] 改卡片管理？确认 `forEachCardBody` 选择器覆盖新 type
- [ ] 改关键动效（fade/淡入）？确认不是每 tick 触发（用代际 / 参数 key
      控制）

