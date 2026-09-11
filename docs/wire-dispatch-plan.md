# v0.12 分发与协议层重构 — 开发文档

> 状态：已落地（P0–P4 + 收尾）。本文档是设计蓝图；**实施与设计有若干演进**，
> 以 PROGRESS.md 的"v0.12 收尾"段为准。关键演进见下。

## 0. 实施演进说明（重要，比下面旧设计更准）

1. **跨协议转换改用 `martian-linguafranca`**（Rust 核心 + Python 绑定），
   不再手写转换器。覆盖 **anthropic-messages / openai-chat / openai-responses**
   三种格式请求+响应+流式双向（§4 原只规划前两种）。
2. **单池单 active 重构**：删掉"平台/客户端入口"分组。`upstreams.json` 改扁平
   `{"active", "upstreams"}`；key 匹配搜整个池（§2 的"不跨平台"规则废除）。
   platform 只作"入站 URL 路径 + DB 记账"的派生事实，不再是配置分组。
3. **分发改 key 唯一信号**：Claude Code 会把 `ANTHROPIC_MODEL=auto` 解析成
   具体模型名再发，故 model 不可靠；`key=auto`=中继转发、真 key=透传，
   "混合态"报错已移除（§2 已同步改）。
4. **探测按钮从 GUI 移除**（用户要简化表单），后端 `/api/probe_upstream` +
   `probe.py` 保留以备复用；协议选择改为三按钮手动选。

---

## 0. 背景与目标

中继现状是"傻转发收发室"：上游说什么协议全靠人肉选对 URL，body 结构差异
完全不处理，分发只认平台 active 上游。本次重构把它升级为"懂行的收发室"：

1. **分发按钥匙（api key）认人**：auto/auto=中继定；真 key+真模型=透传到
   key 对应上游；配置错误（auto 单独出现）报错弹窗。
2. **每个上游显式登记协议（wire）**：端点、认证方式、请求体结构不再靠猜。
3. **新建上游自动探测**：填名字/地址/key 三样，中继发探测请求自动得出
   wire/端点/认证/模型列表/key 有效性，GUI 确认后保存。
4. **跨协议翻译**：Anthropic ↔ OpenAI Chat 请求/响应/流式/工具/usage/错误
   双向转换，客户端无感。

## 1. 术语

| 术语 | 含义 |
|---|---|
| 平台 (platform) | 客户端入口协议族：`anthropic`（/anthropic/*，Claude Code/OpenClaw 等）、`openai`（/openai/*，opencode 等） |
| wire | 上游线协议：`anthropic-messages`、`openai-chat`（首发两种）、`openai-responses`（P4 只声明） |
| 中继转发 | 客户端 key=auto 且 model=auto → 用 GUI 选的 active 上游 |
| 透传 | 客户端 key=真值 → 按 key 匹配上游，模型按客户端（或兜底） |
| 混合态 | key/model 恰好一个是 auto → 配置错误，400 + 弹窗 |
| 内容核 | 概念上的中间表示；实现不落地为独立数据结构，走"解码器→编码器"配对 |

## 2. 分发设计（P1）

### 2.1 判定表（不跨平台，key 匹配只在同 platform 段内）

客户端 key 取法：anthropic 入口读 `x-api-key`（退 `authorization`）；
openai 入口读 `authorization`（退 `x-api-key`）。读出后归一化：strip
`Bearer `/空白，`""`/缺失视为 auto。

**关键：key 是唯一可靠信号。** model 字段不可靠——Claude Code 会把
`ANTHROPIC_MODEL=auto` 解析成具体模型名（如 claude-haiku-4-5-20251001）
再发出，所以不能用 model 是否 auto 做判定。

| key（归一化后） | 判定 | 行为 |
|---|---|---|
| auto（含空/缺失） | 中继转发 | active 上游；模型由中继定（强映射→default→allowed[0]→原样），忽略客户端模型 |
| 真值 | 透传 | 同段内按归一化 key 匹配上游 → 2.2 三分支 |
| 真值未命中 | 透传失败 | 502 `relay_unknown_key` + 告警 |

> v0.12 早期曾设计"混合态"（key/model 恰好一个 auto → 400），实测被
> Claude Code 的 auto 模型解析行为误伤，已移除（见 §2.4）。

### 2.2 透传三分支

1. **key 命中 + 模型在该上游 allowed_models** → 用命中上游的
   url/auth/wire，模型照客户端，账记命中上游。
2. **key 命中 + 模型不在列表** → 兜底 `cfg.model`（强映射）或
   `allowed_models[0]`，照发照记（记 DB 时 error=None，模型用兜底值；
   inflight 备注模型被改写）。
3. **key 未命中任何上游** → 不转发，502 `relay_unknown_key` +
   告警弹窗。DB 记一条 error 行（upstream=`未知key`）。

同 key 多上游（历史上出现过同 url 同 key 拆三个的情况，已合并）：
匹配到多个时取**第一个**命中的（配置顺序），日志提示多义。

### 2.3 告警通道（中继 → GUI）

- proxy 内 `_alerts` 环形列表（带 `(platform, kind, key尾4位, model)`
  去重键，5 分钟节流）。
- 新增 `GET /api/alerts?since=<id>`，GUI 轮询（复用现有 poll 节奏）。
- 混合态/未知 key 两类告警；GUI 收到后 toast + 常驻横幅（可关闭）。
- key 展示脱敏：`gw-f3af…3dba`（前 6 后 4）。

### 2.4 与旧逻辑的关系

- 旧 `AUTH_AUTO_SENTINEL` 503 分支：保留（key=auto 中继转发、active 上游
  无 key 时仍走 503 哨兵）。
- v0.12 早期"混合态"（key/model 恰好一个 auto → 400 relay_mixed_auto）：
  **已移除**。实测 Claude Code 会把 `ANTHROPIC_MODEL=auto` 解析成具体模型名
  再发送，导致 key=auto + 具体模型被误判成混合态。改为 key 唯一信号：
  key=auto → 中继转发（忽略客户端模型），key=真 → 透传。
- v0.11.22 `_resolve_display_upstream` / "平台透传（模型名）"虚拟上游：
  废弃删除，标签=实际转发上游名。
- `_apply_auth_override`：中继转发路径保留；透传路径 key 本来就是
  命中上游自己的，仍走同一函数（cfg.api_key 必有值）。

## 3. 协议声明（P0，行为零变化）

### 3.1 PlatformConfig 新字段

```python
wire: Optional[str] = None          # anthropic-messages | openai-chat | openai-responses
endpoint: Optional[str] = None      # 消息端点路径；None=按 wire 默认
auth_style: Optional[str] = None    # bearer | x-api-key | none；None=由旧 auth_header 推导
```

默认推导（加载时填充，不改用户文件直到用户保存）：

| wire | endpoint 默认 | auth_style 默认 | 必带请求头 |
|---|---|---|---|
| anthropic-messages | /v1/messages | x-api-key | anthropic-version: 2023-06-01 |
| openai-chat | /v1/chat/completions | bearer | — |
| openai-responses | /v1/responses | bearer | — |

### 3.2 存量迁移（读时推断，不回写）

- `wire` 缺失：按所在平台段推断（anthropic 段→anthropic-messages，
  openai 段→openai-chat）。
- `auth_style` 缺失：`auth_header=authorization`→bearer；
  `x-api-key`→x-api-key；其余→按 wire 默认。
- `requires_anthropic_adapter=true`：wire 推断为 anthropic-messages 且
  adapter 行为（换 x-api-key、模型小写、剥 thinking）继续生效——P2/P3
  重构翻译层时再吸收，本阶段不动。

### 3.3 直通 vs 转换路径

- 平台 wire == 上游 wire → **直通**（现状字节转发 + auth/model/thinking
  手术，客户端 URL 路径保留拼接，/models /count_tokens 等附属端点继续工作）。
- 平台 wire != 上游 wire → **转换**（P2/P3）：URL 恒为 `url+endpoint`，
  客户端路径忽略。

## 4. 翻译层（P2 anthropic→openai-chat；P3 反向）

### 4.1 请求向映射（anthropic → openai-chat）

| Anthropic | OpenAI Chat | 备注 |
|---|---|---|
| 顶层 `system`（str 或 blocks） | messages[0] {role:system} | blocks 拼文本 |
| messages[].content blocks | content 字符串 / 多 part 数组 | text 拼接；image base64→image_url data URI |
| assistant `tool_use` block | assistant 消息 `tool_calls` | id→tool_call_id |
| user `tool_result` block | 独立 {role:tool, tool_call_id} 消息 | 拆出 |
| tools[].{name,description,input_schema} | tools[].{type:function,function:{name,description,parameters}} | 结构改写 |
| thinking.budget_tokens | reasoning_effort | 复用现有挡位翻译；不支持则剥离 |
| max_tokens / stop_sequences / temperature / top_p | max_tokens / stop / temperature / top_p | 改名 |
| cache_control | **丢弃** | 无对应；日志 + GUI 标注 |
| stream | stream + stream_options:{include_usage:true} | 必须补 |

### 4.2 响应向

- **非流式**：openai choices[0].message → anthropic message 结构；
  finish_reason→stop_reason 映射（tool_calls→tool_use, stop→end_turn,
  length→max_tokens）；usage 字段名映射（prompt_tokens→input_tokens,
  completion_tokens→output_tokens）。
- **流式状态机**：首 chunk→message_start+content_block_start(text)；
  文本 delta→content_block_delta(text_delta)；tool_calls 按 index 攒参
  数（分片 JSON），攒齐发 content_block_start(tool_use)+完整 input JSON；
  finish_reason→message_delta(stop_reason+usage)；末发 message_stop。
  usage 从末 chunk（include_usage）提取。
- **错误翻译**：openai `{"error":{...}}` → anthropic
  `{"type":"error","error":{"type":...,"message":...}}`，状态码保留。
- **usage parser 跟上游 wire 走**（转换路径下 parser_factory 换成
  上游 wire 的 parser），顺手修掉混合 wire 计量问题。

### 4.3 P3 反向（openai-chat 入 → anthropic-messages 上游）

请求：role:system→顶层 system；tool 消息→tool_result block；
tool_calls→tool_use；reasoning_effort→thinking budget（按挡位表）。
响应：anthropic SSE→openai delta 流（message_start→role delta；
text_delta→content delta；tool_use→tool_calls 分片；message_delta→
finish_reason+usage chunk）。

## 5. 自动探测（P4）

### 5.1 探测流程（新建上游向导"检测"按钮）

```
输入：url + api_key
① 目录命中：models.opencode.ai 缓存按域名查 → 直接给出 wire+端点+怪癖备注
② GET  {url}/v1/models   Bearer 与 x-api-key 各一发 → 认证方式 + 协议线索 + 模型列表
③ POST {url}/v1/chat/completions（胜出认证，最小请求 stream:true, max_tokens:1）
④ POST {url}/v1/messages        （同上）
判读：③④ 谁通谁是 wire；流式事件形状二次验证；错误体形状兜底判据
     （openai: {"error":{...}} / anthropic: {"type":"error",...}）
```

### 5.2 探测端点

`POST /api/probe_upstream {url, api_key}` →
`{wire, endpoint, auth_style, models[], key_valid, evidence[], quirks[]}`。
全部 httpx 异步、8s 超时、best-effort（任何失败都不阻塞，返回已判明部分）。

### 5.3 GUI

新建上游表单从"全手填"改为：名字/地址/key 三项 → [检测] → 自动补全
（wire/端点/认证/模型列表只读展示，允许改）→ [保存]。探测失败退化为
手动二选一（大白话：Anthropic 格式（Claude 系）/ OpenAI 格式（多数服务商））。

## 6. 阶段验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| P0 | wire 字段 + 推断迁移 | 现有 5 上游全部加载出正确 wire；pytest 全绿；转发行为逐字节不变 |
| P1 | key 分发 + 告警 | 四类请求（auto-auto/真-真命中/真-真未命中/混合）单测+实测全对；GUI 弹窗可见 |
| P2 | anthropic→openai 转换 | Claude 协议请求经中继打 minnimax openai 端点：非流式/流式/工具调用/usage 计量全通 |
| P3 | 反向转换 | openai 协议请求打 opencode-go（anthropic wire）全通 |
| P4 | 探测+向导 | 对 minnimax/opencode/deepseek 三家探测结论正确；新建上游 30 秒流程走通 |

## 7. 风险与对策

- tool_calls 分片攒包：独立单测覆盖乱序/半截 JSON。
- cache_control 跨 wire 丢失：日志 + GUI 上游详情标注"无缓存"。
- 转换引入延迟：同 wire 恒直通不受影响。
- 旧哨兵逻辑误伤：P1 判定替换 `AUTH_AUTO_SENTINEL` 分支时回归
  test_proxy_integration.py 对应用例。
