# 插件平台开发记录（dev log）

面向继续开发插件平台的人。接口用法看 `docs/plugins-api.md`，本文件只记
**每次开发的目标、过程、踩坑与验证**。按版本追加，新条目写在最前面。

> 定位：中继作为平台 —— 暴露几乎所有子系统给插件（观察 / 拦截 / 扩展 /
> 替换），agentplan 私有协议是第一个用例。V0.0.1 只是骨架。

---

## V0.0.1 插件平台骨架（2026-08-19 完成）

### 目标

1. `src/relay/plugin.py`：插件加载器 + ctx 契约
   （`register_wire` / `register_parser` / `register_hook` / `on` / `emit` /
   `push_alert` / `db` / `settings` / `log`）。
2. wire 注册表化：`config.py` 增加 `_EXTRA_WIRE_DEFAULTS` +
   `register_extra_wire`，`is_known_wire` / `all_wire_defaults` 全链路生效
   （upstreams 校验、effective_endpoint / effective_auth_style、probe 默认值）。
3. parser 选择分支改查注册表（`parser_factory_for`），插件可注册新 wire 的
   usage parser。
4. `relay()` 插入三个钩子：`pre_dispatch`（分发判定后）、`pre_upstream`
   （发送前）、`post_response`（落库后），全部异常隔离。
5. 示例插件 `plugins/example_logger.py`（能看到真实效果）。
6. `main.py` lifespan 装配 `load_plugins(app)`。
7. 测试 + 文档 + PROGRESS.md。

### 过程

1. **plugin.py 契约设计**（一次成型）：模块级注册表
   `_HOOKS` / `_EVENTS` / `_PARSERS` / `_LOADED` + `PluginContext`。
   内置 3 个 wire 的 parser 工厂在 import 时 `_register_default_parsers()`。
   钩子调用是**直接函数调用**（不走事件派发），热路径零额外开销。
2. **config.py 注册表**：`register_extra_wire(name, *, endpoint, auth_style)`，
   三个查询函数统一合并 `KNOWN_WIRES + _EXTRA_WIRE_DEFAULTS`。
3. **upstreams_file.py / probe.py 联动**：wire 校验、allowed 列表、
   `WIRE_DEFAULTS` 查询点全部改走新函数。
4. **proxy.py 三钩子**：
   - `pre_dispatch` 在「分发判定 + 模型链 + advanced-switch」之后、DISPATCH
     日志之前；插件改 `info["model"]` 时 relay 用 `_rewrite_model_in_body`
     同步重写请求体，改 `info["body"]` 时 relay 重新提取 model 同步 inflight。
   - `pre_upstream` 在 `phase=calling` 之后、上游请求之前；`cfg` 只读。
   - `post_response` 非流式在 `_complete_inflight` 后、流式在流结束 finally
     `done` 广播后；只读快照。
   - `request.done` 事件与 post_response 同点 emit。
5. **main.py**：lifespan 里 `app.state.db/settings` 设置完后
   `load_plugins(app)`（GUI 进程不加载，只有 server 进程加载）。
6. **示例插件 + 测试**：`tests/test_plugin.py` 10 条（含 ASGI 端到端：
   pre_dispatch 改 model 上游真实收到改写 body、pre_upstream 注入头到达）。
7. **回归 + 冒烟**：286 passed（276 既有 + 10 新增，排除 test_gui_web.py）；
   冒烟 `load_plugins` → `loaded: ['example_logger']`。

### 踩坑记录

#### 坑 1：删 import 时连带删了还在用的名字（NameError）

改 upstreams_file.py 时把 `KNOWN_WIRES` 的 import 一起删了，但 L317
（`_opt_known` 的 allowed 列表）还在用 → 测试直接 NameError。
教训：改引用前先 grep 全部使用点，再决定 import 去留。

#### 坑 2：批量替换漏了变体写法（NameError）

probe.py 的默认值引用 `WIRE_DEFAULTS[...]` 用 PowerShell 批量替换成
`all_wire_defaults()[...]`，但只替换了带下标的形式，先 grep 到 3 处没换
（L303 / L319 / L326 / L333 里漏了 3 处不带下标的引用点）。grep 确认
覆盖率要含全部形态（有没有 `[` 下标都算）。

#### 坑 3：验证期 —— PowerShell 里 curl 的 JSON 被转义弄坏（不是中继 bug）

`curl.exe -d "{\"model\":...}"` 在 PowerShell 里传给 curl 的 body 变成
`{\`（2 字节）——中继拿到非法 JSON，`_extract_model` 返回 None，坏体被
原样透传，上游 500。一度误判是中继 bug。**教训**：在 PowerShell 里发 JSON
请求用 Python/httpx 脚本文件（UTF-8 写入），不要用 curl 内联转义。

#### 坑 4：验证期 —— httpx header 不能含非 ASCII

想用 `Authorization: Bearer deepseek官方-openai`（中文名上游）指定上游，
httpx 直接 `UnicodeEncodeError`——HTTP header 按 RFC 只允许 token 字符，
**中文 key 发不出去**（这不是中继问题）。换成 ASCII 名的上游
（f3af39d7-openai）验证。

#### 坑 5：验证期 —— 透传 key 匹配的是 api_key 值，不是上游 name

发 `Bearer f3af39d7-openai`（上游 name）→ 502 `relay_unknown_key`。
`_match_upstream_by_key` 匹配的是归一化后的 `uc.api_key`（去 Bearer 前缀），
不是 name。要用真实 api_key（`gw-f3af39d7-...`）才能命中透传。
注意同名 api_key 的两个上游（f3af39d7 / f3af39d7-openai）命中第一个。

#### 坑 6：跨协议路径缺钩子（真实 bug，验证时发现）

验证 200 成功链路时发现：请求成功返回 200、pre_dispatch 有日志，但
**pre_upstream / post_response 没触发**。定位：`_relay_cross_wire` 的
分叉（proxy.py 原 L2772）在 pre_upstream（原 L2853）**之前** return，
跨协议路径完全绕过了这两个钩子；adapter 路径同理。
修复：cross-wire 内补三处 —— pre_upstream（headers 构造后、build_request
前，body 已是转换后 payload，插件可整体替换 body/headers）、post_response
非流式（广播 done 后）、post_response 流式（finally 广播 done 后）。
**adapter 路径（opencode-go）仍缺，列入 V0.0.2。**
教训：钩子插入点要按「每条真实转发路径」审计，不能只在主路径插一次就
认为全覆盖——三条路径（直通 / cross-wire / adapter）分别走。

#### 坑 7：测试环境

- 默认 `python` 是 3.9 无 pytest，必须用
  `C:\Users\weizheng\miniconda3\envs\usage-stats\python.exe`。
- 全量测试排除 `tests/test_gui_web.py`（既有组合跑挂起问题）。
- bash 工具 workdir 参数不可靠，命令一律用绝对路径。

### 验证（真实链路）

- 重启中继后日志：`plugin loaded: example_logger` + 3 钩子 + 事件订阅全注册。
- key=auto 走活跃上游（free 模型）→ 上游 429 `FreeUsageLimitError`
  （OpenCode Zen free 额度限制，与插件无关）。
- key=api_key 透传 f3af39d7-openai → minnimax 跨协议（openai-chat→
  anthropic-messages）→ **200 OK**，日志四钩子全触发：
  `pre_dispatch` → CROSS-WIRE → `pre_upstream` → 200 → `post_response
  status=200 in=36 out=8 cache_read=128 error=None` → `request.done`。
- trace 日志确认上游真实收到插件注入的头 `x-relay-example: plugin-v0.0.1`。
- 回归 286 passed。

### 交付物

- 新增：`src/relay/plugin.py`、`plugins/example_logger.py`、
  `tests/test_plugin.py`（10 条）。
- 修改：`config.py`（wire 注册表）、`upstreams_file.py`（校验认新 wire）、
  `probe.py`（默认值走 all_wire_defaults）、`proxy.py`（三钩子 + 跨协议
  路径钩子补齐）、`main.py`（lifespan 装配）、`PROGRESS.md`（v0.98.0）、
  `.env.example`（RELAY_PLUGINS_DIR 说明）。
- 文档：`docs/plugins-api.md`（接口参考，本文件姊妹篇）。

---

## V0.0.2~V0.4 插件平台四层（2026-08-19 完成）

### 目标

按规划把平台从骨架推进到完整四层：V0.0.2 补 adapter 路径钩子（每一条
真实转发路径都能被观察/干预）；V0.1 事件总线铺开（观察层）；V0.2 决策
点钩子（拦截层）；V0.3 扩展层（转换器 + auth/探活/计费器注册表 + 第一个
真实插件 volc_agent 接入火山 Responses）；V0.4 替换层（子系统覆盖机制）。

### 过程

1. **V0.0.2 adapter 路径钩子**：`_anthropic_adapter_relay` 成功分支
   `post_response`（`_complete_inflight` 后）+ `request.done`；失败分支
   同点补 post_response + done；`_anthropic_adapter_relay_sse` 在
   `gen()` 里 stream 段前 pre_upstream、finally 里 post_response + done。
   测试 11 passed（plugin 3 + adapter 路径 8）。
2. **V0.1 事件总线**：
   - `db.recorded`（db.py `record` 内，含 row_id）；
   - `quota.autoswitch`（成功含 from/to/utilization_pct/ok，失败含
     ok=False + error）+ `quota.recovered`（quota_monitor）；
   - `probe.finished`（probe_upstream + connectivity_test 成功/失败）；
   - `config.upstreams_changed`（config.py `save_upstreams_json` 成功路径，
     **函数内 import** 避免循环依赖）；
   - `auth.failed`（`_check_auth` require_token 分支，payload 脱敏 key；
     push_dispatch_alert 顺带把告警内容脱敏）；
   - `request.started`（`relay()` 主路径 `_register_inflight` 后）。
   测试 17 passed plugin（含 db.recorded/request.started 端到端、
   auth.failed 单元+e2e、probe、quota 切换、config 变更）。
3. **V0.2 决策点钩子**：`run_hooks` 返回**第一个非 None**（注册顺序）；
   `_HOOK_NAMES` 扩到 7 个。`decide_auth`（`_check_auth` async 化，返回
   True 放行）、`before_quota_deduct`（db.record 返回 False 跳过落库返回
   0、可原地改 usage）、`decide_quota_switch`（False 否决切换）、
   `after_probe`（原地改 info 字段改判结论）。测试 24 passed plugin；
   相关回归 65 passed；test_plugin + upstreams_file + wire = 90 passed。
4. **V0.3 转换器注册表**：plugin.py `_WIRE_CONVERTERS` +
   `register_wire_converter`（ctx 方法 + 模块级，name 参数）+ 
   `wire_converter_for`；wire.py `convert_request` 先查插件后回退
   linguafranca。PlatformConfig.adapter 字段 + upstreams_file 加载/序列化
   roundtrip。修复了重复定义函数导致的覆盖问题。
5. **V0.3 火山接入（第一个真实插件）**：`plugins/volc_agent.py` ——
   sys.path 加 `C:\Claude-Code`（父目录）后 `import anthropic_to_responses`，
   注册 `anthropic-messages → openai-responses` 与
   `openai-chat → openai-responses` 两个转换器；库缺失优雅跳过。真实
   upstreams.json 的 volc-code-plan 加 `"adapter": "volcagent"`。测试
   30 passed plugin。重启中继验证 volc_agent 加载；真实请求
   `/anthropic/v1/messages` key=ark-a77e2e46-… 到 volc-code-plan → **200**
   （全链路 anthropic → 火山转换 → minimax-m3 响应）。
6. **V0.3 扩展注册表（auth/prober/billing）**：plugin.py
   `_AUTH_SCHEMES/_PROBERS/_BILLING_UNITS` + 注册/查询函数 + ctx 方法。
   接线：tui 两处成本计算接 `billing_unit_for`（未知回退 count）；
   proxy `_apply_auth_override` + cross-wire 的 auth_style 分派接
   `auth_scheme_for`；config.py `effective_auth_style` 放行任意非空
   auth_style（插件方案名）、billing_unit 改 `str="count"`；
   config.add_upstream / apply_quota_edit / upstreams_file 校验放行插件名。
   测试 36 passed plugin（含 auth 方案 e2e、billing 计费 e2e、
   prober 分派、配置流穿插件名）。
7. **V0.4 替换层**：plugin.py `_OVERRIDES` 注册表 +
   `register_override`（priority 降序）+ `arun_overrides`（async 版，
   返回 (result, provider)）。防递归：`_OVERRIDE_ACTIVE` 同子系统重入
   直接放弃；失败隔离：处理器异常记日志继续下一个。接线 `auth` 子系统：
   proxy `_check_auth` 开头调 `arun_overrides("auth", request, platform)`，
   返回非 None 即整体接管认证。测试 41 passed plugin。

### 踩坑记录

- **V0.2 测试**：`_HOOKS` 是普通 dict，新钩子名必须先加进 `_HOOK_NAMES`，
  否则 KeyError；decide_auth 端到端测试客户端发 Bearer 时会被内置流程
  自动当合法透传 key 处理，断言要按语义写。
- **V0.3 库路径**：火山库 `C:\Claude-Code\anthropic_to_responses` 是包
  目录，sys.path 必须加**父目录** `C:\Claude-Code` 才能 import。
- **V0.3 计费器**：`billing_unit` 从 Literal("count","token") 放宽为 str
  后，config.add_upstream / upstreams_file 的严格校验要同步放宽，否则
  插件计费器名保存不了。
- **V0.4 覆盖执行器**：处理器的返回判断是"非 None 即接管"，async 处理器
  需 await 之后才能判断，所以覆盖执行器必须 async 化（`arun_overrides`）。

### 测试清单（tests/test_plugin.py，41 条）

按版本分组的完整用例清单，改代码后至少跑 `test_plugin.py` 全量：

- **V0.0.1 骨架（10 条）**：register_wire_via_ctx / default_parsers_registered /
  register_parser_roundtrip_and_no_overwrite / register_hook_decorator_and_unknown /
  run_hooks_order_and_isolation / run_hooks_can_mutate_info / event_bus_emit_and_isolation /
  load_plugins_from_directory / load_plugins_missing_dir / pre_dispatch_hook_rewrites_model_end_to_end。
- **V0.0.2 adapter 钩子（1 条 e2e）**：adapter_path_hooks_end_to_end
  （`_anthropic_adapter_relay` 成功路径 pre_upstream → post_response →
  request.done 全触发 + 注入头到达）。
- **V0.1 事件总线（6 条）**：request_started_and_db_recorded_events（e2e）/
  auth_failed_events（单元）/ auth_failed_require_token_end_to_end（e2e）/
  probe_finished_event / quota_autoswitch_event / config_upstreams_changed_event。
- **V0.2 决策钩子（7 条）**：run_hooks_returns_first_non_none /
  run_hooks_isolates_boom_and_keeps_later_value / before_quota_deduct_veto（False
  不落库）/ before_quota_deduct_modifies_usage（原地改 usage 落库值变）/
  decide_auth_allow_end_to_end / decide_quota_switch_veto / after_probe_rewrite。
- **V0.3 转换器注册表 + adapter 字段（5 条）**：register_wire_converter_decorator /
  wire_convert_request_dispatches_to_plugin / wire_convert_request_falls_back_to_linguafranca /
  upstream_adapter_field_roundtrip / volc_agent_plugin_loads_and_converts /
  volc_agent_converter_fails_gracefully_when_lib_missing。
- **V0.3 扩展注册表（5 条）**：register_auth_scheme_decorator /
  register_prober_and_billing_unit / auth_scheme_applied_end_to_end（插件
  方案的头真实到达上游）/ billing_unit_applied_to_quota（插件计费值进
  tui 成本统计）/ prober_dispatch（probe_upstream 委托插件 + 事件照发）/
  extension_names_flow_through_config（add_upstream / apply_quota_edit /
  upstreams_file 全链路保留插件名）。
- **V0.4 替换层（5 条）**：override_priority_and_first_non_none /
  override_isolation_on_error / override_reentry_guard（处理器内重入同
  子系统 → (None, None) 防死循环）/ auth_override_intercepts_end_to_end
  （插件 403 整体接管）/ auth_override_abstain_keeps_default（弃权 →
  默认 401）。

### 关键接线位置速查（改内核代码时的出发点）

| 能力 | 源码位置 |
|---|---|
| 钩子链 / 事件广播 / 注册表 | `src/relay/plugin.py`（run_hooks / emit_event / *_for） |
| `decide_auth` + `auth` 覆盖 | `src/relay/proxy.py` `_check_auth`（覆盖 → 钩子 → 内置校验） |
| `request.started` / `pre_dispatch` / `pre_upstream` / `post_response` | `src/relay/proxy.py` `relay()` + `_relay_cross_wire` + `_anthropic_adapter_relay(_sse)` |
| `db.recorded` + `before_quota_deduct` | `src/relay/db.py` `record()` |
| `quota.autoswitch` / `quota.recovered` + `decide_quota_switch` | `src/relay/quota_monitor.py` `_check_once()` |
| `probe.finished` + `after_probe` + prober 分派 | `src/relay/probe.py` `probe_upstream()` |
| `config.upstreams_changed` | `src/relay/config.py` `save_upstreams_json()` |
| 转换器分派 | `src/relay/wire.py` `convert_request()` |
| auth 方案分派（主路径 / cross-wire） | `src/relay/proxy.py` `_apply_auth_override()` + `_relay_cross_wire` |
| billing 接线 | `src/relay/tui.py` `fetch_by_upstream_with_costs()` / `fetch_by_upstream_model()` |
| adapter 字段 | `src/relay/config.py` PlatformConfig + `src/relay/upstreams_file.py` |

### 回归与验证

- 全量（排除 test_gui_web.py）：**317 passed**。
- 顺带修复一个既存 flaky 测试：test_poc_relay.py 的 mock_post 里
  `orig_post(self, ...)` 用了**原 client（真实 transport）**而 mock
  client 建了没用，导致一直打真实 opencode.ai（多数 200 偶发 503 挂）。
  改为 `orig_post(client, ...)` 后确定性 16 passed in 0.65s（原先 42s）。
- 重启中继验证：volc_agent 两转换器注册 + example_logger 钩子正常；
  真实请求 volc-code-plan → 200（adapter 字段 + 转换器真实生效）。

### 交付物

- 新增：`plugins/volc_agent.py`（火山接入，真实插件）。
- 修改：`plugin.py`（事件/决策钩子/四注册表/覆盖执行器）、`proxy.py`
  （事件 + 决策钩子 + auth 覆盖 + auth_style 分派）、`db.py`、`probe.py`
  （事件 + after_probe + prober 分派）、`quota_monitor.py`、`config.py`
  （adapter 字段 + 校验放宽 + config.upstreams_changed）、`wire.py`
  （转换器分派）、`tui.py`（billing 接线）、`upstreams_file.py`、
  `tests/test_plugin.py`（41 条）、`tests/test_poc_relay.py`（mock 修复）。
- 文档：`docs/plugins-api.md`（更新到 V0.4）、`PROGRESS.md`（v0.98.0）。

---

## 规划（未实施，按顺序推进）

- **V0.5（可选）**：覆盖机制扩展 —— wire_convert / probe / billing 也走
  覆盖执行器（目前按名注册表），支持 async 覆盖处理器。
- **V1.0（候选）**：agentplan 私有协议插件化作为端到端用例验证平台
  完整闭环。
