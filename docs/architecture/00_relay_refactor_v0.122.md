# 中继「一切即插件」重构总账（v0.117 → v0.122）

> **本文档是本次中继服务容器化重构的唯一文档**（原 9 份阶段文档合并而成）：
> 哲学红线 + 7 个阶段（Phase 0–6）做了什么 + 终态架构 + 稳定 API 契约 + 测试基线 +
> 逐阶段回滚。改了什么 / 为什么这么改 / 最终长什么样 / 怎么回滚，**一册答全**。
>
> 日期：2026-08-23。测试基线：**770 passed / 0 failed**（`test_gui_web.py` 按约定跳过）。

---

## 1. 用户的初始指令

> 需要采用类似于 deepseek harness 类似的方式，对中继项目进行完全模块化，提高扩展性。

后续阶段指令：`进行phase1` → `开始phase2` → `继续phase3` → `继续phase4` → `继续phase5` →
`继续phase6` → `全部写入开发文档，并单独构建一个文档记录本次大超大改动` →（合并为本文档一份）。

⚠ **「一切即插件」是哲学红线**，不是技术选型：

> 「不存在需要打补丁的特权内核」— `docs/cordis-primer.zh.md`（deepseek-harness 底层 Cordis 框架）

**Pythonic 移植，不字面照搬 TS Cordis**：用 Protocol / dataclass / asyncio / contextvars，
不用 `Reflect.metadata` + `Service.inject`。中继自身 17,000 行 / 50 文件，三大文件
`proxy.py` 3785 / `gui.py` 3993 / `config.py` 1463。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 参考 deepseek-harness「一切即插件」架构，Python 移植 | 指令 |
| B | **不回退 V0.4 插件平台** —— `ctx.register_wire_converter` 等接口必须继续工作；`plugins/volc_agent.py` 不修改即可加载 | 用户显式 |
| C | **热路径零侵入** —— `stream_iter` / `_bump_chunk_activity` / `_broadcast_live_event` / `parser.feed` / `_filter_anthropic_sse` 不许动 | 用户显式 |
| D | 每阶段独立 PR 可回滚，不做大爆炸 | 用户显式 |
| E | 保留未跟踪特性（passthrough / panel_pool / error_analyzer / advanced_switch / probe / quota_client） | 工作树实测 |
| F | 4-mode 事件总线（emit / waterfall / parallel / serial）一次性建齐 | 设计决策 |
| G | 文档 7 节固定结构；⚠ 重要主题放显眼位置 | memory |
| H | 测试基线 `test_gui_web.py` 栈溢出按约定跳过 | memory |
| I | 简单机械任务（grep / 行数统计）走本地 ollama 或 bash，不跑主上游 | 用户显式 |
| J | 阶段 5/6（profile / 插件迁移）从「可选」落地为「必做」 | 用户显式 |

**关键决策总表**（各阶段明细见 §3）：

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| 1 | 热路径红线 | `stream_iter`/`_bump_chunk_activity`/`_broadcast_live_event`/`parser.feed`/`_filter_anthropic_sse` **永不动** | per-byte < 1us；await 即 ms 级 |
| 2 | 4-mode bus | emit / waterfall / parallel / serial | 对齐 cordis；`run_hooks`→waterfall、`arun_overrides`→parallel、`emit_event`→emit |
| 3 | proxy/ 4 个 re-export 模块 | **re-export 即终态**（不物理下沉） | 函数共享 proxy_legacy closure；物理复制制造双份漂移 |
| 4 | alerts/reasoning shim | **始终走 legacy** | test 依赖 `_dispatch_alerts.clear()` / `_REASONING_BY_TOOL_CALL.clear()` 模块全局 |
| 5 | http shim | **转发 test monkey-patch 到 proxy_legacy** | `monkeypatch.setattr(proxy._proxy_alive)` 必须命中 legacy 实现 |
| 6 | 横向模块 | QuotaMonitor 真 Service；3 个薄包装 | 无状态模块不重写，薄委托防双实现漂移 |
| 7 | passthrough / panel_pool / live_panel | **deferred** | ASGI 中间件 + pywebview OS 绑定，Service 化语义不清、风险 > 收益 |
| 8 | Profile | 单入口 + `--profile`/`RELAY_PROFILE` env | GUI autostart spawn 子进程经 env 继承 |
| 9 | 未知名 profile | 回退 web | 向后兼容，拼错不挂 |
| 10 | Service 风格插件 | 模块级 `apply(ctx)` 胶水 + class | loader 签名冻结；零侵入 |

---

## 3. 分析后得出的开发路径

### 3.0 ⚠ 热路径红线 + 私生子基线

**红线（任何阶段绝对不可动）**：

| 名称 | 性质 |
|---|---|
| `stream_iter` | SSE async-generator 主循环；每 SSE chunk 走一遍 |
| `_bump_chunk_activity` | **per-SSE-byte** 状态写入；只 mutex，无 setattr |
| `_broadcast_live_event` | `Queue.put_nowait` 满则丢；**非阻塞**，hot path ONLY |
| `_filter_anthropic_sse` | 字节缓冲 + 行切分；插件**不得**拦截原始字节 |
| `parser.feed` | 纯 Python sync 状态机调用，必须 on event-loop tick |

想观察每个 chunk → 订阅 LiveBus 自己拉，**不能**进钩子链。

**私生子 import 基线**（Phase 0 锁定 8 处 → 终态 6 处，其中 4 处为刻意保留的兼容 fallback）：

| 调用方 | 行 | 终态 |
|---|---|---|
| `autostart.py:53` | `from .config import _project_root` | ✅ 内部 helper，非私生子 |
| `quota_monitor.py:198` | `from .routers.api import _persist_active` | ⚠ ctx 优先，无 ctx 回退（刻意 fallback） |
| `routers/anthropic.py:9` / `openai.py:9` | `from ..proxy import _infer_client_wire_from_path, relay` | ✅ 路由层入口，走 proxy/ re-export（sanctioned） |
| `routers/stats.py:533` | `from ..proxy import _in_flight, _in_flight_done` | ⚠ ctx 优先，无 ctx 回退（刻意 fallback） |
| `services/settings_mutator.py:90` | `from ..routers.api import _persist_active` | ✅ **唯一 legacy 委托源**（Phase 4 设计内） |

`app.state.settings` 直读 36 处（Phase 0 基线）—— Phase 4 只收口了 `_persist_active`，
`app.state.settings` 直读属 Phase 4 扩展范围外，未清零。

### 3.1 Phase 0 —— 冻结公开 API（v0.117）

**目标**：冻结 + 文档化「对插件 / 横向模块永远稳定」的接口。

**新建**：`docs/architecture/00_context_overview_v1.md`（已被本文档取代）+
`STABILITY.md`（已并入 §5.2）+ `src/relay/_api_stable.py`（单行 re-export 全部稳定符号）+
`tests/test_api_stable.py`。
**修改**：`plugin.py` 加 `__all__` + 各 `register_*` docstring 顶部 STABLE 标记；
`config.py` 4 处 mutator 加 `@deprecated` runtime warning；`proxy.py` 顶部加「内部接口 moved」段。
**删除**：无。**测试**：基线 238 全绿，私生子基线 8 处锁定。

### 3.2 Phase 1 —— RelayContext 核心（v0.117）

**目标**：建 Cordis 等价 Python 骨架，**不**改任何现有模块行为，只新增 + 兼容层。

**新建** `src/relay/core/`（7 文件，1423 行）：

```
core/
├─ __init__.py       re-export 全部
├─ context.py        RelayContext：单例 + service registry + 4-mode 委托 + scope + dispose 反序
├─ events.py         EventBus 4-mode（emit / waterfall / parallel / serial）+ 异常隔离 + priority
├─ service.py        RelayService Protocol + Disposer + @inject(deps=...) + compose_disposers + DisposerChain
├─ loader.py         bootstrap_builtin_services + attach_legacy_plugin_platform（兼容层）
├─ interning.py      InterningRegistry 共享单例池
└─ scope.py          RequestScope（contextvars per-request 绑定）
```

**修改**：`main.py` lifespan 加 `ctx = RelayContext(...)` → `bootstrap` → `attach_legacy` →
`app.state.ctx = ctx` → finally `ctx.dispose()`。
**关键设计**：兼容层 monkey-patch `emit_event`/`run_hooks`/`arun_overrides` 转发到 ctx.bus——
**双投递是允许的**（旧插件走旧 `_EVENTS`，新订阅走新 bus）；`dispose()` 先 `bus.clear()` 再反序释放。
**测试**：4 文件 72 个新增 → 647 全绿。volc_agent.py / example_logger.py 不修改仍加载。

### 3.3 Phase 2 —— 隐藏状态提为服务（v0.118）

**目标**：把 proxy.py 的隐藏状态抽成 9 个 Service，注入 RelayContext；hot path 调用点不变。

**新建** `src/relay/services/`（9 核心服务 + 8 测试文件）：

```
services/__init__.py            bootstrap 拓扑序注册 9 服务
services/http_client_pool.py    _client_pool / _get_client / _close_http_clients / _system_proxy_url
services/inflight_store.py      _InFlight / _in_flight / _in_flight_done / sweeper 自启 + dispose cancel
services/live_bus.py            非阻塞 publish()（满则丢）+ subscribe 返 Disposer
services/alert_log.py           环形 + dedup（_dispatch_alerts）
services/reasoning_cache.py     LRU + 跨线绑定（_REASONING_BY_TOOL_CALL 族）
services/url_builder.py         路径拼接 / normalize
services/auth_header.py         鉴权 header + 插件覆盖
services/settings_service.py    Pydantic Settings 只读包装（raw 暴露原对象）
services/settings_mutator.py    save_upstreams_json / update_env_var 写集中化
```

**修改**：`core/context.py` 加 `add_disposer()`；`core/loader.py` bootstrap 真接 +
`attach_legacy_plugin_platform` 检测已注册则跳过；`main.py` 删手写 sweeper / `_close_http_clients`
import，finally 仅 `ctx.dispose()`；`advanced_switch.py` / `error_analyzer.py` / `routers/stats.py`
接受可选 `ctx` 参数（ctx 优先 + 无 ctx fallback）。
**关键坑**：`SettingsService` 与 attacher 同注册 `settings` 抛 `ServiceAlreadyRegistered` → loader
静默跳过；`InflightStore.apply()` 无 running loop 抛 RuntimeError → try/except 包 sweeper 启动。
**测试**：+162（Phase 1+2）→ **737 全绿**。热路径 SSE 回放（test_live_stream + test_proxy_integration
+ test_wire_3way_stream_thinking）不回归。

### 3.4 Phase 3 —— 拆 proxy.py（v0.119）

**目标**：3813 行单文件 → `src/relay/proxy/` 包，`from relay import proxy; proxy._xxx()` 全兼容。
原文件备份 `proxy_legacy.py` 作 git bisect + 永久 fallback。

**新建** `src/relay/proxy/`（12 文件，946 行）+ `proxy_legacy.py`（3813 行备份）：

```
proxy/__init__.py          223 行  re-export 全量 + shim 再导出
proxy/_util.py             132 行  ⭐真拆分（纯函数）
proxy/_auth.py              76 行  ⭐真拆分（鉴权拼装）
proxy/_dispatch.py          51 行  re-export（wire 推断 / path normalize / match）
proxy/_thinking.py          35 行  re-export（thinking 剥离 / rewrite）
proxy/_streaming.py         21 行  re-export（SSE 字节缓冲，hot path）
proxy/_relay.py             21 行  re-export（relay() 主入口）
proxy/_inflight_shim.py    135 行  ctx 优先 + legacy fallback
proxy/_http_shim.py         77 行  ctx 优先 + legacy fallback + patch 转发
proxy/_live_shim.py         71 行  ctx 优先 + legacy fallback
proxy/_alerts_shim.py       49 行  始终 legacy（test 兼容）
proxy/_reasoning_shim.py    55 行  始终 legacy（test 兼容）
```

**删除**：`src/relay/proxy.py`（3813 行）→ 由 proxy/ 包 + proxy_legacy.py 替代。
**关键设计**：`_dispatch/_thinking/_streaming/_relay` **re-export 即终态**（不物理下沉，
函数共享 proxy_legacy closure）；shim / re-export 子模块用 `from .. import proxy_legacy`
（绝对导入，避免 `relay.proxy.proxy_legacy` 循环 import）。
**关键坑**：test 的 legacy monkey-patch（`_dispatch_alerts.clear()` / `_REASONING_BY_TOOL_CALL.clear()`
/ `_proxy_alive`）必须仍命中 legacy 模块全局 → alerts/reasoning shim 始终走 legacy；http shim
转发 patch。
**测试**：0 新增（纯拆分）→ **737 全绿**。

### 3.5 Phase 4 —— 横向模块插件化（v0.120）

**目标**：横向模块以 RelayService 形态出现。**交付 QuotaMonitor 真 Service + 3 薄包装；
passthrough / panel_pool / live_panel deferred**。

**新建**：

| 路径 | 行数 | 类型 |
|---|---|---|
| `services/quota_monitor_service.py` | 64 | `QuotaMonitorService`（apply 注册 + start 幂等 + get_task + disabled） |
| `services/horizontal_services.py` | 150 | `ErrorAnalyzerService` / `AdvancedSwitchService` / `ProbeService` 薄包装（`from ..module import fn`，`kw.setdefault("ctx", self._ctx)`） |
| `tests/test_quota_monitor_service.py` | 100 | 单测 |
| `tests/test_horizontal_service.py` | 130 | 单测 |

**修改**：`settings_mutator.py` +`persist_active()`（**`_persist_active` 唯一委托源**）；
`services/__init__.py` +4 re-export；`core/loader.py` bootstrap **扩到 14 服务**
（9 原 + bus + quota_monitor + 3 横向）；`quota_monitor.py` `_switch` 改 ctx 优先 + 回退；
`main.py` lifespan 走 `ctx.svc("quota_monitor")`，回退旧 `start`。
**为何 deferred**：`passthrough/middleware.py` 是 ASGI 中间件（Starlette `add_middleware` kwargs
注入，Service 化要改 ASGI 挂载时序）；`panel_pool.py` 绑 pywebview OS 窗口 + GUI 分离进程
（pool 永远拿不到 relay ctx）。二者风险 > 收益。
**测试**：+15 → **752 全绿**。

### 3.6 Phase 5 —— Profile/Bundle 组合（v0.121）

**目标**：把「哪些子系统加载」从硬编码提为可配置开关。

**新建**：`src/relay/profile.py`（72 行，`Profile` dataclass + `resolve_profile()` +
`profile_from_name()`）+ `tests/test_profile.py`（140 行）。
**修改**：`src/relay/__main__.py` 加 `--profile=NAME` / `--profile NAME` 解析（serve/gui 通用，
stats 忽略）+ `gui` 子命令在 profile.gui=False 时 no-op 退出。
**三档**：`web`（默认，GUI+HTTP+passthrough）/ `headless`（无 GUI）/ `passthrough-only`（纯透传调试）。
**关键设计**：**单入口 + env**（不用 cordis 多 bundle 文件）——GUI autostart spawn 子进程，
profile 经 `RELAY_PROFILE` env 自然继承；未知名 profile → 回退 web。
**测试**：+11 → **763 全绿**。

### 3.7 Phase 6 —— 插件迁移 + passthrough-only 裁减（v0.122）

**目标**：展示新 Service 风格插件写法；落地 `passthrough-only` profile 路由裁减。

**新建**：

| 路径 | 行数 | 类型 |
|---|---|---|
| `plugins/volc_agent_v2.py` | 100 | Service 风格（`class VolcAgentService` + 模块级 `apply(ctx)` 胶水） |
| `plugins/example_logger_v2.py` | 80 | Service 风格 + `ctx.on` + `ctx.svc('inflight')` demo |
| `tests/test_plugin_v2.py` | 130 | 单测 |

**修改**：`plugin.py` —— `PluginContext` 加 `_relay_ctx` + `svc()`/`register()` 委托
RelayContext（未绑定抛 KeyError）；`load_plugins` 注入 `app.state.ctx` 到插件 ctx；
`main.py` `create_app` 加 `passthrough-only` 分支（只留透传中间件 + `/` + `/healthz`，裁掉
/anthropic /openai /api /stats /models /root 业务路由）。
**兼容矩阵（终态）**：旧 `apply(ctx)` 永久支持；新 `class XService: apply(ctx) -> Disposer` +
模块级胶水本阶段起支持——loader 契约不变（只认模块级 `apply(ctx)`）。
**测试**：+7 → **770 全绿**。

### 3.8 终态架构（v0.122）

**服务容器拓扑**：

```
main.py lifespan
  └─ RelayContext(app, settings, db)           # 进程级单例
       ├─ bootstrap_builtin_services(ctx)      # 注册 14 个服务
       │    ├─ HttpClientPool        ctx.svc("pool")
       │    ├─ AlertLog              ctx.svc("alerts")
       │    ├─ ReasoningCache        ctx.svc("reasoning")
       │    ├─ LiveBus               ctx.svc("bus") / ctx.bus
       │    ├─ UrlBuilder            ctx.svc("url_builder")
       │    ├─ AuthHeader            ctx.svc("auth_header")
       │    ├─ InflightStore         ctx.svc("inflight")  (依赖 LiveBus)
       │    ├─ SettingsService       ctx.svc("settings")
       │    ├─ SettingsMutator       ctx.svc("mutator")
       │    ├─ QuotaMonitorService   ctx.svc("quota_monitor")
       │    ├─ ErrorAnalyzerService  ctx.svc("error_analyzer")
       │    ├─ AdvancedSwitchService ctx.svc("advanced_switch")
       │    └─ ProbeService          ctx.svc("probe")
       ├─ attach_legacy_plugin_platform(ctx)   # 旧 plugin.py 注册表 → ctx.bus 桥接
       ├─ load_plugins(app)                     # plugins/*.py（旧 + 新风格兼容）
       └─ ctx.dispose()  # 反序释放 Disposer
```

**终态目录**：

```
src/relay/
├─ __main__.py     --profile= (Phase 5)
├─ profile.py      Profile 三档 (Phase 5)
├─ core/           7 文件 1423 行 (Phase 1)
├─ services/       12 文件 1573 行 (Phase 2/4)
├─ proxy/          12 文件 946 行 (Phase 3)
├─ proxy_legacy.py 原 proxy.py 备份 3813 行 (Phase 3)
├─ plugin.py       PluginContext + svc/register 委托 (Phase 6)
└─ routers/        anthropic/openai/api/models/stats/root
plugins/
├─ volc_agent.py / example_logger.py        旧风格（永久兼容，未修改）
├─ volc_agent_v2.py / example_logger_v2.py  Service 风格 (Phase 6)
```

### 3.9 子系统职责总表（13 服务 + 7 独立模块）

**服务容器内（可注入 / 可 dispose / 可回滚）**：

| 服务 | key | 功能 |
|---|---|---|
| HttpClientPool | `pool` | 中继所有出站请求的 httpx 客户端池；同一上游 URL 复用连接，自动判系统代理（localhost 跳过） |
| AlertLog | `alerts` | 环形告警缓冲；`emit("alert",...)` 去重 + 限量，GUI/API 拉取最近告警 |
| ReasoningCache | `reasoning` | 跨协议 thinking/推理缓存；anthropic→openai 转换时把 thinking block 按 tool_call id 绑定注入 messages |
| LiveBus | `bus` | 实时事件总线；`publish()` 非阻塞广播（队列满丢不阻塞热路径），GUI 订阅推流；`_broadcast_live_event` 服务化 |
| UrlBuilder | `url_builder` | 上游 URL 拼装；判 cfg.url 是否含 `/v1`、补 `/messages`、normalize 各平台 path |
| AuthHeader | `auth_header` | 鉴权头拼装唯一来源；按 wire+平台生成 Authorization / x-api-key，支持插件 `auth_scheme` 覆盖 |
| InflightStore | `inflight` | 进行中请求登记表 + 计费前置状态；register/update/bump/set_phase/complete + 超时 sweeper；stats 实时数据源 |
| SettingsService | `settings` | 只读配置包装；横向模块拿 `ctx.svc("settings")` 而非 `app.state.settings`，`.raw` 暴露原 Pydantic |
| SettingsMutator | `mutator` | 写配置集中入口；`save_upstreams_json` / `update_env_var` / `persist_active`，斩私生子 import |
| QuotaMonitorService | `quota_monitor` | 后台配额监控；周期算上游利用率 + 5h 窗口余量，触发自动切换 |
| ErrorAnalyzerService | `error_analyzer` | 错误分类；请求失败 classify_error 判因（限流/鉴权/超时/模型名）→ 上下文 + prompt → alert |
| AdvancedSwitchService | `advanced_switch` | 智能切换决策；`quota.low` 事件 decide 是否切/切到哪，调分析模型判断 |
| ProbeService | `probe` | 上游探活；对候选上游发测试请求验 key/连通性，供切换前预检 |

**容器外（刻意保留独立形态，未服务化）**：

| 模块 | 功能 |
|---|---|
| passthrough | 完全透传模式；url@@api-key 鉴权 + 纯 ASGI 中间件拦截所有 HTTP 直转上游 + 独立 passthrough.db，与中继业务隔离 |
| panel_pool / gui | pywebview 桌面面板 + 窗口池；与 relay 分离进程，走 GUI 拉取 inflight/告警 |
| parsers | anthropic/openai 的 SSE usage 解析器（feed/finalize），stable |
| plugin | 外部插件加载平台；`load_plugins(app)` 扫 plugins/*.py，注册 wire 转换器/钩子/事件订阅 |
| proxy/ + proxy_legacy | 中继核心；请求分发（wire 推断）、SSE 字节缓冲流、thinking 剥离重写、auth 检查 |
| routers/ | 5 个 HTTP 路由：/anthropic /openai /api /stats /models + root catchall |
| linguafranca | 外部独立包，跨线请求体转换（已是插件形态） |

---

## 4. 验收清单

### 4.1 终态兼容性（v0.122 验证）

| 承诺 | 状态 |
|---|---|
| 旧 `from relay import proxy; proxy._xxx()` 全兼容 | ✅ 全量 re-export |
| `volc_agent.py` / `example_logger.py` 不修改仍加载 | ✅ `test_context_loader.py` |
| `load_plugins(app)` 签名不变 | ✅ STABILITY §5.2 |
| hot path SSE 回放不回归 | ✅ test_live_stream + test_proxy_integration + test_wire_3way_stream_thinking |
| test 的 legacy 模块全局 monkey-patch 仍生效 | ✅ shim 设计保证 |
| 旧 + 新插件同载不冲突 | ✅ `test_plugin_v2.py::test_old_and_new_volc_load_together` |
| 未设 profile = 现状默认 | ✅ `test_profile.py::test_default_profile_is_web` |
| `app.state.ctx` 保留（横向模块 + gui 依赖） | ✅ main.py lifespan |
| **全量测试** | **770 passed / 0 failed**（test_gui_web.py 按约定跳过） |

### 4.2 改动规模总账

| 维度 | 数字 |
|---|---|
| 阶段数 | 7（Phase 0–6） |
| 新增目录 | `core/`（7 文件）`services/`（12 文件）`proxy/`（12 文件） |
| 新增模块行数 | core 1423 + services 1573 + proxy 946 = **3,942 行** |
| 备份文件 | `proxy_legacy.py` 3813 行（原 proxy.py 完整副本，永久 fallback） |
| 新增插件 | `plugins/volc_agent_v2.py` + `example_logger_v2.py`（290 行） |
| 新增测试文件 | 16 个 |
| 新增测试用例 | 197 个 |
| 测试增量 | 基线 → 737 → 752 → 763 → **770** |
| 删除 | `src/relay/proxy.py` 整文件（3813 行）→ proxy/ 包 + proxy_legacy.py 替代 |

### 4.3 阶段验收状态（全绿）

- [x] Phase 0 冻结 API：`_api_stable.py` + STABILITY + deprecation warning
- [x] Phase 1 RelayContext：core/ 7 文件 + 4-mode bus + 兼容层（+72）
- [x] Phase 2 服务抽离：9 个 Service + bootstrap（+90 → 737）
- [x] Phase 3 拆 proxy.py：proxy/ 包 + 5 shim + proxy_legacy 备份（737 无回归）
- [x] Phase 4 横向模块：QuotaMonitor 真 Service + 3 薄包装 + persist_active（+15 → 752）
- [x] Phase 5 Profile：三档 + `--profile=` CLI（+11 → 763）
- [x] Phase 6 插件迁移：PluginContext 委托 + v2 插件 + passthrough-only 裁减（+7 → **770**）

---

## 5. 文件改动一览

### 5.1 各阶段改动

| 阶段 | 版本 | 新建 | 修改 | 删除 |
|---|---|---|---|---|
| 0 | v0.117 | `_api_stable.py` + test + 2 docs | plugin.py / __init__.py / config.py / proxy.py | 0 |
| 1 | v0.117 | core/ 7 文件 + 4 test | main.py | 0 |
| 2 | v0.118 | services/ 9 核心 + 8 test | context.py / loader.py / main.py / proxy.py / advanced_switch.py / error_analyzer.py / routers/stats.py | 0 |
| 3 | v0.119 | proxy/ 12 文件 + proxy_legacy.py | 0 | **proxy.py（3813 行）** |
| 4 | v0.120 | quota_monitor_service.py + horizontal_services.py + 2 test | settings_mutator.py / services/__init__ / core/loader.py / quota_monitor.py / main.py | 0 |
| 5 | v0.121 | profile.py + test_profile.py | __main__.py | 0 |
| 6 | v0.122 | volc_agent_v2.py + example_logger_v2.py + test_plugin_v2.py | plugin.py / main.py | 0 |

### 5.2 冻结稳定 API 契约（原 STABILITY.md 摘要）

以下接口**签名 / 行为 / 字段语义**冻结，变更必须走 deprecation → 删流程：
- **插件加载器**：`load_plugins(app, directory=None)` / `plugins_dir()` / `emit_event()` /
  `run_hooks()` / `arun_overrides()` / `override_providers()` / `parser_factory_for()` /
  `wire_converter_for()` / `auth_scheme_for()` / `prober_for()` / `billing_unit_for()`
- **PluginContext**：`.settings` / `.db` / `.name` / `.log` / `register_wire` / `register_parser` /
  `register_wire_converter` / `register_auth_scheme` / `register_prober` / `register_billing_unit` /
  `register_override` / `register_hook`（alias `hook`）/ `on` / `emit` / `push_alert`
- **钩子签名**：`pre_dispatch` / `pre_upstream` / `post_response` / `decide_auth` /
  `before_quota_deduct` / `decide_quota_switch` / `after_probe`
- **事件 payload**：`request.started` / `request.done` / `db.recorded` / `quota.autoswitch` /
  `quota.recovered` / `probe.finished` / `config.upstreams_changed` / `auth.failed` / `alert`
- **解析器**：`relay.parsers.anthropic.AnthropicUsageParser` / `openai.OpenAIUsageParser`
- **Re-export 入口**：`relay._api_stable`（V0.117 起）

**钩子 info 字段契约**（`fn(info) -> ...`）：

| 钩子 | 字段 | 可写 |
|---|---|---|
| `pre_dispatch` | `model` / `body` / `cfg`(只读) / `passthrough`(只读) | model / body |
| `pre_upstream` | `headers` / `body`(仅跨协议可写) / `cfg`(只读) / `upstream_url`(只读) | headers |
| `post_response` | `platform` / `cfg` / `model` / `status` / `usage` / `error` / `req_db_id` / `upstream` / `request_id` / `streaming` | 全只读 |
| `decide_auth` | `platform` / `kind` / `key_masked` / `path` / `require_token` | True 放行 / False 拒绝 |
| `before_quota_deduct` | `usage` / `status` / `error` | False 跳过落库 |
| `decide_quota_switch` | `from_upstream` / `to_upstream` / `reason` / `utilization_pct` | False 否决切换 |
| `after_probe` | `wire` / `endpoint` / `auth_style` / `key_valid` | 全部可写改判 |

**事件 payload 契约**：

| 事件 | payload 关键字 |
|---|---|
| `request.started` | `platform` / `upstream` / `model` / `key_masked` / `request_id` |
| `request.done` | 同 `post_response` 钩子（`usage` 是 `UsageAcc` 对象） |
| `db.recorded` | `row_id` / `platform` / `upstream` / `model` / `status` / `request_id` / `usage` |
| `quota.autoswitch` | 成功：`platform`/`upstream_name`/`from_upstream`/`to_upstream`/`utilization_pct`/`ok=True`；失败：`platform`/`upstream_name`/`ok=False`/`error` |
| `quota.recovered` | `platform` / `upstream_name` / `quota_5h` / `remaining_5h` |
| `probe.finished` | `upstream` / `wire` / `auth_style` / `model` / `key_valid` / `ok` / `error` |
| `config.upstreams_changed` | `changed`（上游名列表） |
| `auth.failed` | `platform` / `kind` / `key_masked` / `path` / `reason` |
| `alert` | `message`(str) + `**extra` |

- **不稳定**：任何 `from .proxy import _<name>` 私生子、`from .routers.api import _persist_active`、
  横向模块内 `from .config import save_upstreams_json/update_env_var`、`app.state.settings` 直挂

---

## 6. 测试覆盖

| 阶段 | 新增测试 | 累计 |
|---|---|---|
| Phase 0 | — | 238（基线） |
| Phase 1 | +72 | — |
| Phase 2 | +90 | **737** |
| Phase 3 | 0（纯拆分，无回归） | 737 |
| Phase 4 | +15 | **752** |
| Phase 5 | +11 | **763** |
| Phase 6 | +7 | **770** |

**新增测试文件（16 个）**：test_api_stable / test_context / test_context_events /
test_context_loader / test_context_legacy_emit / test_http_client_pool / test_inflight_store /
test_live_bus / test_alert_log / test_reasoning_cache / test_url_builder / test_auth_header /
test_settings_mutator / test_quota_monitor_service / test_horizontal_service / test_profile /
test_plugin_v2（= 18 个文件，197 用例）。

**关键回归测试**：
- 热路径 SSE：test_live_stream + test_proxy_integration + test_wire_3way_stream_thinking
- 插件兼容：test_context_loader.py::test_volc_agent_compatible_through_compat_layer
- shim：test_dispatch.py（7 失败→通过）+ test_plugin.py::test_auth_failed_events
- 双插件同载：test_plugin_v2.py::test_old_and_new_volc_load_together
- Profile：test_profile.py（默认 web / env override / 未知名回退 / CLI 两形态 / gui headless noop）

**验证命令**：

```bash
pytest tests/ --ignore=tests/test_gui_web.py -q   # 770 全绿（按模块分批跑防栈溢出）
grep -rnE 'from\s+\.+\s*(proxy|routers\.api|config)\s+import\s+_[a-zA-Z]' src/relay/ --include='*.py' \
  | grep -vE '(proxy/|routers/api\.py|config\.py|proxy_legacy\.py)'   # 6 处，4 处刻意 fallback
RELAY_PROFILE=passthrough-only python -c "import relay.main"          # 6 routes，无业务路由
```

---

## 7. 风险与回滚

### 7.1 跨阶段风险矩阵

| 风险 | 检测 | 缓解 |
|---|---|---|
| hot path 被插件拦截 | perf test + 静态 lint | 红线 + 启动检查 bus subscriber 数 |
| 私生子 `_xxx` import 漏改 | grep 监控 | 阶段末基线对比 |
| ctx dispose 反序破坏 | 关闭循环测试 | Disposer 装饰 + `_disposers` 反序 |
| 双层事件投递 | 计数 helper 测试 | attach_legacy 只让一个路径投递（双投递是设计取舍） |
| import path 漂移 | pytest collection | proxy/ re-export 全量 + proxy_legacy 永久 fallback |
| test 的 legacy monkey-patch 失效 | 全量测试 | shim 始终 legacy / 转发 patch |
| `SettingsService` 与 attacher 同注册 settings | ServiceAlreadyRegistered | loader 静默跳过 + attacher `if not in ctx` 判定 |
| passthrough / panel_pool deferred | 验收标注 | 保留旧入口，不强制 Service 化 |

### 7.2 每阶段回滚策略

每阶段独立 commit、独立可 revert：

```bash
# 例：回滚 Phase 3（拆 proxy.py）
git checkout <phase2-commit> -- src/relay/proxy.py
git rm -r src/relay/proxy/ src/relay/proxy_legacy.py
# 770 测试基线不受影响（proxy.py 单文件仍完整）
```

各阶段 revert：Phase 1 = 删 core/ + 4 test + main.py 4 行；Phase 2 = revert loader/main/proxy/
横向模块修改 + 删 services/；Phase 4 = checkout 5 个修改文件 + 删 4 个新文件；Phase 5 = checkout
__main__.py + 删 profile.py/test_profile.py；Phase 6 = checkout plugin.py/main.py + 删 v2 插件/test。
旧 `volc_agent.py` / `example_logger.py` 全程未被改，永不受回滚影响。

### 7.3 红线（终态硬约束，永不可触碰）

1. `stream_iter` 字节缓冲路径
2. `_bump_chunk_activity` 锁策略（只 mutex，不可 setattr）
3. `_filter_anthropic_sse` 实现
4. `db.record()` 直接调用 schema
5. linguafranca 跨线转换（已是独立包）
6. `load_plugins(app)` 签名（除 `app: FastAPI` 外必填位置参数）
7. `volc_agent.py` / `example_logger.py` 删除或改签名
8. `app.state.ctx` 删除

### 7.4 遗留 & 后续建议

| 项 | 状态 | 建议 |
|---|---|---|
| passthrough / panel_pool / live_panel Service 化 | deferred | 按需做，配合 `--profile` 的 GUI 适配 |
| `_dispatch/_thinking/_streaming/_relay` 物理下沉 | re-export 即终态 | 除非未来需要独立扩展，否则保持 |
| `quota_monitor.py:198` / `routers/stats.py:533` 残留 fallback import | 兼容保留 | 横向模块全部接 ctx 后可删 |
| `app.state.settings` 直读 36 处 | 未清零 | 属 Phase 4 扩展范围，非本计划目标 |
