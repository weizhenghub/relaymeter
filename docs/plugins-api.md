# 插件平台接口参考（plugins-api）

插件平台 V0.0.1~V0.4 的**程序细节**：插件怎么加载、ctx 有哪些方法、
每个钩子 / 事件 / 注册表的确切契约。开发过程与踩坑见
`docs/plugins-dev-log.md`。

---

## 1. 插件是什么

插件 = `plugins/` 目录下一个 `.py` 文件（目录可用环境变量
`RELAY_PLUGINS_DIR` 覆盖，默认 `项目根/plugins`）。

- 文件名以下划线开头的跳过（`_xxx.py` 不算插件）。
- 每个插件模块**必须导出 `apply(ctx)`**：启动时被调用一次，在 apply 里
  注册 wire / parser / 转换器 / 钩子 / 事件订阅 / 覆盖处理器。
- 加载顺序 = 文件名排序；单个插件加载失败只跳过它（记日志），
  不影响中继启动与其它插件。
- 插件在 **server 进程** lifespan 里加载（`main.py` 调 `load_plugins(app)`）；
  GUI 进程不加载。

```python
# plugins/my_plugin.py
def apply(ctx) -> None:
    @ctx.register_hook("pre_upstream")
    def pre_upstream(info):
        info["headers"]["x-my-header"] = "1"
```

改完插件文件 → 重启中继（server 进程）生效。

## 2. PluginContext 方法一览

`ctx` 是插件唯一入口。跨插件共享同一个 ctx 实例 —— **不要给 ctx 挂
插件私有状态**（用模块级变量）。

| 方法 | 版本 | 作用 |
|---|---|---|
| `register_wire(name, *, endpoint, auth_style="bearer")` | V0.0.1 | 注册新 wire 的默认端点/认证风格 |
| `register_parser(wire, factory)` | V0.0.1 | 注册按 wire 选择的 usage parser |
| `register_hook(name, fn=None)`（别名 `hook`） | V0.0.1~V0.2 | 注册请求生命周期 / 决策钩子（装饰器/直传两用） |
| `on(event, fn=None)` | V0.0.1 | 订阅进程内事件（装饰器/直传两用） |
| `emit(event, **payload)` | V0.0.1 | 发事件（广播给所有订阅者） |
| `push_alert(message, **extra)` | V0.0.1 | 推一条告警（即 `emit("alert", message=..., **extra)`） |
| `register_wire_converter(name, fn=None)` | V0.3 | 注册协议转换器（按 (src_wire, dst_wire) 分派） |
| `register_auth_scheme(name, fn=None)` | V0.3 | 注册上游认证方案（`auth_style: <插件名>` 时生效） |
| `register_prober(name, fn=None)` | V0.3 | 注册探活器（`prober: <插件名>` 时生效） |
| `register_billing_unit(name, fn=None)` | V0.3 | 注册计费器（`billing_unit: <插件名>` 时生效） |
| `register_override(subsystem, priority=0, fn=None)` | V0.4 | 注册子系统覆盖处理器（替换层） |
| `db` / `settings` | V0.0.1 | 服务句柄（SQLite 数据库 / Settings） |
| `log` | V0.0.1 | 插件专属 logger（`relay.plugin.<插件名>`） |

## 3. 钩子（register_hook）

普通钩子签名 `fn(info: dict) -> None`，同步异步都行（异步会被 await）。
`info` 是**可变 dict**，异常全部隔离（记日志，不阻断转发）。

决策钩子（V0.2）签名 `fn(info: dict) -> Any`，由 `run_hooks` 取**第一个
非 None 返回值**：某个插件返回非 None 即生效，后面的插件不再调用；
全部返回 None 则走内置默认。异常同样隔离。

可用钩子名（`_HOOK_NAMES`，按调用顺序）：

| 钩子 | 版本 | 时机 | 返回非 None 的语义 |
|---|---|---|---|
| `pre_dispatch` | V0.0.1 | 分发判定后、DISPATCH 日志前 | 无特殊语义（忽略返回值） |
| `pre_upstream` | V0.0.1 / V0.0.2 | 发送前最后一改（adapter 路径 V0.0.2 补上） | 忽略返回值 |
| `post_response` | V0.0.1 / V0.0.2 | 落库完成后（adapter 路径 V0.0.2 补上） | 忽略返回值 |
| `decide_auth` | V0.2 | 认证判定时 | `True` = 放行；`False` = 拒绝（401） |
| `before_quota_deduct` | V0.2 | 配额扣减落库前 | `False` = 跳过本次落库（不扣配额）；也可原地改 `info["usage"]` |
| `decide_quota_switch` | V0.2 | 自动切换决策时 | `False` = 否决本次自动切换 |
| `after_probe` | V0.2 | 探活出结果后 | 原地改 info 字段即改判探测结论 |

注册顺序 = 调用顺序；同钩子多插件按加载顺序执行。

### pre_dispatch —— 分发判定后、DISPATCH 日志前

| 字段 | 类型 | 可写 | 说明 |
|---|---|---|---|
| `platform` | str | 只读 | `"anthropic"` / `"openai"` |
| `cfg` | PlatformConfig | **只读** | 上游配置（V0.0.1 不支持换上游） |
| `model` | str\|None | **可写** | 改后 relay 用 `_rewrite_model_in_body` 同步重写请求体 |
| `body` | bytes | **可写** | 整体替换；relay 会重新提取 model 同步 inflight |
| `client_model` | str\|None | 只读 | 客户端原始模型 |
| `passthrough` | bool | 只读 | 是否透传（key=真值） |

### pre_upstream —— 发送前最后一改

| 字段 | 类型 | 可写 | 说明 |
|---|---|---|---|
| `platform` | str | 只读 | |
| `cfg` | PlatformConfig | 只读 | |
| `model` | str\|None | 只读 | |
| `body` | bytes | 只读（主路径）；**可写（跨协议路径）** | 跨协议路径 body 是转换后的上游 payload，整体替换直接发出 |
| `headers` | dict | **可写** | 增删请求头（原地改或整体替换） |
| `upstream_url` | str | 只读 | 目标 URL |

### post_response —— 落库完成后（只读快照）

| 字段 | 说明 |
|---|---|
| `platform` | `"anthropic"` / `"openai"` |
| `cfg` | 上游配置 |
| `model` | 实际转发的模型 |
| `status` | 上游 HTTP 状态码 |
| `usage` | `UsageAcc` 对象（字段见 `src/relay/models.py`） |
| `error` | `None` 或 `"upstream_429"` / `"wire_convert: ..."` 等 |
| `req_db_id` | 请求记录 id（跨协议路径为 None） |
| `upstream` | 上游名 |
| `request_id` | 上游 request-id（可能 None） |
| `streaming` | 是否流式请求 |

非流式在 `_complete_inflight` 后触发；流式在流结束 finally（`done`
广播后）触发。adapter 路径（V0.0.2）在 `_anthropic_adapter_relay(_sse)`
的成功/失败分支触发。

### decide_auth —— 认证判定（V0.2）

`info` 字段：

| 字段 | 类型 | 可写 | 说明 |
|---|---|---|---|
| `platform` | str | 只读 | `"anthropic"` / `"openai"` |
| `kind` | str | 只读 | `"require_token"`（当前唯一取值） |
| `key_masked` | str | 只读 | 客户端 Authorization 脱敏值（如 `"sk-***"` / `"Bearer ***"`） |
| `path` | str | 只读 | 请求路径（如 `/openai/v1/chat/completions`） |
| `require_token` | bool | 只读 | 是否启用了 `require_auth_token` |

返回 `True` = 放行（跳过 require_auth_token 校验）；`False` = 拒绝
（401）。全部 None → 内置 require_auth_token 校验。

> 优先级说明：**V0.4 的 `auth` 覆盖处理器先于本钩子执行**（覆盖返回非
> None 则本钩子与内置校验都不会跑）。

### before_quota_deduct —— 配额扣减前（V0.2）

`info` 字段：

| 字段 | 类型 | 可写 | 说明 |
|---|---|---|---|
| `platform` | str | 只读 | |
| `cfg` | PlatformConfig | 只读 | |
| `model` | str | 只读 | |
| `usage` | UsageAcc | **可写** | 原地改 input/output/cache 计数字段即调整本次落库用量 |
| `status` | int | 只读 | 上游 HTTP 状态码 |
| `error` | str\|None | 只读 | 错误标识 |

返回 `False` = 跳过本次落库（`db.record` 返回 0，不扣配额）；也可原地改
`info["usage"]` 调整计数字段。全部 None → 正常扣减。

### decide_quota_switch —— 自动切换决策（V0.2）

`info` 字段：

| 字段 | 类型 | 可写 | 说明 |
|---|---|---|---|
| `platform` | str | 只读 | |
| `upstream_name` | str | 只读 | 触发判定的上游 |
| `from_upstream` | str | 只读 | 当前活跃上游 |
| `to_upstream` | str | 只读 | 拟切换到的上游 |
| `reason` | str | 只读 | `"quota_exhausted"` / `"error_rate"` 等 |
| `utilization_pct` | float | 只读 | 当前配额利用率（0-100+） |

返回 `False` = 否决本次自动切换（配额监控器跳过本次切换）。全部 None →
放行。

### after_probe —— 探活结果处理（V0.2）

`info` 字段：

| 字段 | 类型 | 可写 | 说明 |
|---|---|---|---|
| `wire` | str\|None | **可写** | 识别出的协议；改判此处即改判探测结论 |
| `endpoint` | str\|None | **可写** | 识别出的端点路径 |
| `auth_style` | str\|None | **可写** | 识别出的认证风格 |
| `key_valid` | bool | **可写** | 是否通过连通性验证 |
| `ok` | bool | 无效 | 是否有识别结果；改动只反映到 `probe.finished` 事件，不影响判定 |
| `models` | list | 只读 | 探测发现的可选模型 |
| `url` | str | 只读 | 探测目标 URL |
| `evidence` | list | 只读 | 探测步骤记录 |

原地改 `info["wire"]` / `info["endpoint"]` / `info["auth_style"]` /
`info["key_valid"]` 即改判探测结论（内部探测结果结构随后被覆盖）。

## 4. 事件（on / emit）

`on(event, fn)` 订阅；订阅者同步异步都行（异步订阅者会被调度到事件循环，
仅在事件循环运行时有效）。payload 按关键字参数传给 fn；异常隔离。

内置事件：

| 事件 | 版本 | 触发时机 | payload |
|---|---|---|---|
| `request.started` | V0.1 | 请求进入 relay 主路径后 | `platform` / `upstream` / `model` / `key_masked` / `request_id`（可能 None） |
| `request.done` | V0.0.1 | 每次请求落库完成后 | 与 post_response 钩子 info 同字段（usage 是 UsageAcc 对象） |
| `db.recorded` | V0.1 | 每次配额落库后 | `row_id` / `platform` / `upstream` / `model` / `status` / `request_id` / `usage`（UsageAcc） |
| `quota.autoswitch` | V0.1 | 自动切换判定后 | 成功：`platform` / `upstream_name` / `from_upstream` / `to_upstream` / `utilization_pct` / `ok=True`；失败：`platform` / `upstream_name` / `ok=False` / `error` |
| `quota.recovered` | V0.1 | 上游配额恢复时 | `platform` / `upstream_name` / `quota_5h` / `remaining_5h` |
| `probe.finished` | V0.1 | 探活完成时 | `upstream`(url) / `wire` / `auth_style` / `model` / `key_valid` / `ok` / `error` |
| `config.upstreams_changed` | V0.1 | 上游配置保存成功后 | `changed`（上游名列表） |
| `auth.failed` | V0.1 | 认证失败时 | `platform` / `kind` / `key_masked` / `path` / `reason` |
| `alert` | V0.0.1 | `ctx.push_alert` 推送时 | `message`（str）+ `**extra`（任意附加字段） |

各事件的 payload 细节：

- `request.started`：`platform`（`"anthropic"`/`"openai"`）、`upstream`
  （上游名）、`model`（可能 None）、`key_masked`（脱敏后的客户端 key）、
  `request_id`（uuid，主路径恒有；adapter/跨协议路径可能 None）。
- `request.done`：与 post_response 钩子 info **同字段同语义**（`status` /
  `usage` / `error` / `req_db_id` / `streaming` 等，见 §3）。
- `db.recorded`：`row_id`（新插入行 id，可用于对账）、`usage` 为
  UsageAcc 对象（非 dict）。
- `quota.autoswitch`：失败分支（如当前无活跃上游）只有 `platform` /
  `upstream_name` / `ok=False` / `error`；成功分支含 from/to 与
  `utilization_pct`。
- `quota.recovered`：`quota_5h` 为配置值，`remaining_5h` 为恢复后剩余。
- `probe.finished`：`upstream` 键是**探测 URL**（不是上游名）；`ok` 为
  是否有识别结果；`error` 仅失败时有值。
- `config.upstreams_changed`：GUI 或 API 保存 upstreams.json 成功后触发。
- `auth.failed`：`kind` 恒为 `"require_token"`；`key_masked` 与
  `reason` 已脱敏（不会含完整 key）。

自定义事件：任意插件 `ctx.emit("my.event", ...)`，任意插件 `ctx.on("my.event")`
——事件总线是进程内全插件共享的。

## 5. 扩展注册表（V0.3）

### 背景：upstreams.json 的 `adapter` 字段

上游除了 `wire` 之外可配 `adapter: <插件名>`（V0.3 新增，可选）。
`adapter` 声明的**转换器优先于 wire 推断**：`convert_request(payload,
src_wire, dst_wire)` 按 `(src_wire, dst_wire)` 查插件注册表，命中就用
插件转换器（不检查 adapter 名字是否匹配 —— 注册表本身就是按转换对
分派的）；未命中才回退 linguafranca 内置转换。因此插件的典型做法是
"注册转换器 + 让上游在配置里声明"：

```json
{
  "name": "volc-code-plan",
  "wire": "openai-chat",
  "adapter": "volcagent"
}
```

- `adapter` 不参与 wire 判定（wire 仍是配置的主协议字段）；
- 未知 adapter 名不报错（回退 linguafranca），配置/序列化原样保留；
- 转换器按 `(src_wire, dst_wire)` 对注册，与 adapter 名解耦 —— 同名
  转换对后注册的覆盖先注册的（按加载顺序）。

### register_wire_converter —— 协议转换器

```python
@ctx.register_wire_converter("my-conv")
def conv(payload: dict, src_wire: str, dst_wire: str) -> dict | None:
    ...
```

契约：返回转换后的 dict；返回 `None` = 放弃（回退 linguafranca 内置转换）。
`convert_request(payload, src_wire, dst_wire)` 按 `(src_wire, dst_wire)` 查
插件转换器，命中优先，未命中回退 linguafranca。查询：
`wire_converter_for(src_wire, dst_wire)`。

### register_auth_scheme —— 上游认证方案

```python
@ctx.register_auth_scheme("my-scheme")
def scheme(cfg, platform) -> tuple[str, str]:
    return ("x-api-key", "secret")
```

契约：`fn(cfg, platform) -> (header_name, header_value)`。上游配置
`auth_style: <插件名>` 时，中继发上游请求用该插件方案注入认证头，
替代内置 bearer / x-api-key / none。查询：`auth_scheme_for(name)`。

### register_prober —— 探活器

```python
@ctx.register_prober("my-prober")
def prober(url, api_key, *, timeout, model) -> dict:
    ...
```

契约：返回标准探测结构
`{wire, endpoint, auth_style, models, key_valid, ok, evidence, quirks}`。
`probe_upstream(..., prober="my-prober")` 命中插件时整段探测委托给它，
事件 `probe.finished` 照发。查询：`prober_for(name)`。

### register_billing_unit —— 计费器

```python
@ctx.register_billing_unit("my-bill")
def cost(model, *, raw_count, input_tokens, output_tokens,
         cache_read_input_tokens, cache_creation_input_tokens) -> float:
    ...
```

契约：返回该次请求的加权消耗基数（再乘 model_multipliers）。上游配置
`billing_unit: <插件名>` 时替代内置 count/token 计算（tui 成本统计生效）。
查询：`billing_unit_for(name)`。

## 6. 替换层：子系统覆盖机制（V0.4）

```python
@ctx.register_override("auth", priority=10)
async def my_auth(request, platform):
    ...
    return response          # 非 None = 整体接管认证子系统
```

覆盖语义：`arun_overrides(subsystem, *args)` 按 priority **降序**依次调用
已注册处理器，**第一个返回非 None 的生效**（整体接管该子系统），调用方
跳过默认实现；全部返回 None → 调用方走内置实现。

- **优先级**：priority 越高越先尝试（默认 0）。同 priority 按注册顺序。
- **防递归**：同一子系统重入（处理器内部再触发同子系统覆盖）直接放弃，
  返回 `(None, None)`，杜绝无限循环。
- **失败隔离**：单个处理器抛异常只记日志，继续下一个处理器。

已接线子系统：

| 子系统 | 时机 | 处理器契约 | 非 None 语义 |
|---|---|---|---|
| `auth` | 每个请求认证判定前 | `fn(request, platform) -> Response \| None` | 直接返回该 Response（整体接管认证） |

模块级：`register_override(subsystem, fn, *, priority=0, owner="plugin")`、
`arun_overrides(subsystem, *args, **kwargs) -> (result, provider)`、
`override_providers(subsystem)`。

## 7. 模块级函数（一般不需要直接用）

- `plugins_dir() -> Path`：插件目录（`RELAY_PLUGINS_DIR` 优先）。
- `load_plugins(app=None, directory=None) -> list[str]`：加载并返回已加载
  插件名（`main.py` 内部调用）。
- `parser_factory_for(wire) -> Callable | None`：按 wire 查 parser 工厂。
- `wire_converter_for(src_wire, dst_wire) -> Callable | None`：查转换器。
- `auth_scheme_for(name)` / `prober_for(name)` / `billing_unit_for(name)`：
  查扩展注册表。
- `run_hooks(name, info)`：钩子链执行函数（V0.2 起返回首个非 None）。
- `emit_event(event, **payload)`：事件广播执行函数。
- `register_override(...)` / `arun_overrides(...)`：覆盖机制（V0.4）。

以上执行函数均**全部异常隔离**（proxy.py 内部调用）。

## 8. 示例：完整插件骨架

```python
"""示例：决策钩子 + 事件订阅 + 扩展注册 + 覆盖。"""
from __future__ import annotations


def apply(ctx) -> None:
    ctx.log.info("插件已加载")

    @ctx.register_hook("decide_auth")
    async def decide_auth(info):
        if info["path"].startswith("/internal/"):
            return True          # 内部路径免鉴权

    @ctx.register_hook("before_quota_deduct")
    def before_quota_deduct(info):
        if info["status"] == 429:
            return False         # 429 不扣配额

    @ctx.register_billing_unit("my-bill")
    def cost(model, **k):
        return 1.0

    @ctx.register_override("auth", priority=10)
    async def my_auth(request, platform):
        token = request.headers.get("x-custom-token")
        if token and token.startswith("plg-"):
            return None          # 继续内置认证
        from starlette.responses import Response
        return Response(b'{"error":"plugin-deny"}', status_code=403)

    @ctx.on("quota.autoswitch")
    def on_switch(**payload):
        if payload.get("ok"):
            ctx.push_alert("自动切换", **payload)
```

## 9. 真实插件参考：volc_agent（火山 Responses 接入）

`plugins/volc_agent.py` 是第一个真实插件（V0.3），完整源码可作模板
（关键形态，非逐行复刻）：

```python
from __future__ import annotations

import os
import sys
from pathlib import Path

_LIB_ROOT = Path(os.environ.get(
    "ANTHROPIC_TO_RESPONSES_PATH",
    r"C:\Claude-Code\anthropic_to_responses",   # 火山转换库（包目录）
))
# 包目录的父目录加入 sys.path 才能 `import anthropic_to_responses`
if str(_LIB_ROOT.parent) not in sys.path and _LIB_ROOT.is_dir():
    sys.path.insert(0, str(_LIB_ROOT.parent))

try:
    from anthropic_to_responses import request as _a2r  # noqa: F401
    _LIB_OK = True
except Exception:  # 库不可用则整体回退 linguafranca
    _a2r = None
    _LIB_OK = False


def _anthropic_to_responses(payload, src_wire, dst_wire):
    return _a2r.anthropic_to_responses(payload)


def _openai_chat_to_responses(payload, src_wire, dst_wire):
    from relay.wire import convert_request  # 先经 linguafranca 转 anthropic
    anthropic = convert_request(payload, "openai-chat", "anthropic-messages")
    return _a2r.anthropic_to_responses(anthropic)


def apply(ctx) -> None:
    if not _LIB_OK:
        ctx.log.warning("volcagent: 转换库不可用，跳过注册")
        return
    ctx.register_wire_converter(
        "anthropic-messages", "openai-responses", _anthropic_to_responses,
    )
    ctx.register_wire_converter(
        "openai-chat", "openai-responses", _openai_chat_to_responses,
    )
```

要点（踩坑汇总）：

- **sys.path 加父目录**：库本身是包目录 `anthropic_to_responses/`，要
  `import anthropic_to_responses` 必须把它的**父目录** `C:\Claude-Code`
  加进 sys.path。
- **库缺失优雅跳过**：import 失败只记 warning 并 return，不注册转换器
  （测试覆盖：`test_volc_agent_converter_fails_gracefully_when_lib_missing`）。
- **转换器名只是标识**：`register_wire_converter(src, dst, fn)` 按
  (src_wire, dst_wire) 分派，注册名不参与匹配。
- **回退语义**：转换器返回 None 由调用方回退 linguafranca；`_openai_chat_to_responses`
  内部用 `relay.wire.convert_request` 借内置转换做中转。
- **对应上游配置**：volc-code-plan 配 `"wire": "openai-chat"` +
  `"adapter": "volcagent"`；请求经 `convert_request` 插件命中
  `(openai-chat, openai-responses)` 转换后直发火山端点。
- **验证命令**（真实链路）：向中继发 `/anthropic/v1/messages`，
  `Authorization: Bearer ark-<火山 key>`，预期 200 且模型名
  `minimax-m3`（火山默认模型）。

## 10. 边界与限制（V0.4）

- 钩子**不能**：换上游（cfg 只读）、拦截/阻断请求（普通钩子只可改不能
  拒；决策钩子与覆盖机制是拦截能力）。
- **adapter 路径**（`requires_anthropic_adapter` 上游）V0.0.2 起有完整
  pre_upstream / post_response / request.done。
- **转换器可注册**（V0.3）；同 wire 直通 + parser 始终可用。
- 覆盖机制只有 `auth` 一个子系统已接线（V0.4）；wire_convert / probe /
  billing 走 V0.3 的按名注册表。
- 插件失败（加载/钩子/事件处理/覆盖处理器抛错）只记日志，绝不影响
  请求转发。
- 测试命令：`& "C:\Users\weizheng\miniconda3\envs\usage-stats\python.exe"
  -m pytest tests\test_plugin.py -q`（默认 python 3.9 无 pytest）。
