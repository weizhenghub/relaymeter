# 完全透传模式 (v0.XX)

⚠ **本特性在用户拍板的设计下做了若干"非显而易见"决策**，实施前先读这一篇。

## 1. 目标与边界

**目标**：当用户开启透传模式后，所有 HTTP 请求绕过现有平台路由器（Anthropic / OpenAI 转换 + 协议分发），**原样转发**到客户端鉴权头里指定的 URL。统计写入独立 sqlite。

**不在本特性范围**：
- ❌ 协议转换（Anthropic↔OpenAI↔Gemini↔火山 Responses 互换）—— 完全不做
- ❌ 配额 / 限流拦截 —— 纯透传
- ❌ 客户端 SDK 改造 —— 用户只需要改 base_url + api_key 即可

## 2. 客户端如何调用

```bash
# 客户端打中继（127.0.0.1:8088），路径随便填（被忽略）
curl http://127.0.0.1:8088/anything \
  -H "x-api-key: https://api.openai.com/v1/chat/completions@@sk-xxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [...]}'
```

中继会把这个请求**原样**发到 `https://api.openai.com/v1/chat/completions`：
- URL 部分（含路径）**直接当目标地址**，不再拼客户端打的路径
- `x-api-key` 替换为 `x-api-key: sk-xxx`
- body 不动

```bash
# 也支持 Authorization Bearer 风格（自动剥 Bearer 解析）
curl http://127.0.0.1:8088/anything \
  -H "Authorization: Bearer https://api.anthropic.com/v1/messages@@sk-ant-xxx" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-opus-4-6", "messages": [...]}'
```

## 3. 鉴权头解析规则

按**最后一个** `@@` 切分：

| 鉴权头值 | url | api-key |
|---|---|---|
| `https://x.com@@sk-xxx` | `https://x.com` | `sk-xxx` |
| `https://user:pw@host.com@@sk-xxx` | `https://user:pw@host.com` | `sk-xxx` |
| `Bearer https://x.com@@sk-xxx` | `https://x.com` | `sk-xxx`（先剥 Bearer） |

**为什么按最后一个？** URL 允许含单个 `@`（userinfo），按第一个切会被 userinfo 干扰。按 `@@` 双字符作分隔符避开了这个歧义。

**为什么用 `@@` 而不是 `|` 或 `::`？** 视觉上和单 `@` 区分得开，对打字者友好。代价是 URL 含 `@@` 时无法表达——我们按"用户负责写对 URL，URL 不含 `@@`"约定。

## 4. 关键架构决策

### 4.1 纯 ASGI 中间件

| 选项 | 拒绝原因 |
|---|---|
| `BaseHTTPMiddleware` | 缓冲整个 response body，破坏 SSE 流式 |
| catch-all FastAPI router `/` | 抢走 `/api/*` 等管理路径，用户无法关开关 |
| **纯 ASGI 中间件** | ✅ 挂最外层，可豁免，可条件触发，零开销透传 |

### 4.2 豁免路径白名单

透传开启时仍可访问：

```
/api/          # 管理 API（toggle mode, manage upstreams）
/stats         # 统计聚合
/healthz       # liveness
/models        # OpenCode models 端点
/live          # live streaming panel
/messages      # message search
/requests      # request list
/favicon.ico /openapi.json /docs /redoc
```

**漏一条 = 用户被锁死在透传模式**。

### 4.3 独立 sqlite 文件

`passthrough.db` 与 `relay.db` **物理隔离**：
- schema 两张表：`passthrough_requests`、`passthrough_upstreams`
- 配置项 `RELAY_PASSTHROUGH_DB` 默认 `./passthrough.db`
- 一个坏不影响另一个；可独立备份/迁移

### 4.4 上游指纹

`(raw_url, raw_api_key, model_field_name)` 三元组完全相等 = 同一上游。

**不做任何规范化**：大小写、尾斜杠、query 参数都按字面区分。同一上游不同写法 = 两个 upstream。

**raw_url 是客户端鉴权头里 `@@` 前面的那个 URL**，**不是**拼接后的最终地址。这样同一上游不同客户端 SDK 拼出的 path 后缀（`/v1/messages` vs `/v1/messages/count_tokens`）会被识别为同一 fingerprint。

### 4.4b 客户端路径后缀自动拼接

**⚠ 关键认知：上游信息只在 auth 头里。** 客户端打给中继的路径是
`http://127.0.0.1:8088/anthropic/v1/messages`（中继自己的地址，因为
`ANTHROPIC_BASE_URL` 配的就是中继），**完全不含上游信息**。中继能拿
到的唯一上游地址来源 = 用户手动填写的 auth 头（`url@@key` 里的
`url`）。

客户端 SDK 补的 `/v1/messages` 虽然落在中继路径上，它**同时是
Anthropic 协议的 endpoint 后缀**——不管 SDK 直连 DeepSeek 还是打
中继，它都会拼这一截。所以中继把它从客户端路径里"抄"出来，拼到
用户手填的 auth URL 后面，正好凑成完整上游地址。

**用户手填 URL 的两种情况**（也只有这两种，不可能出现缺 `/anthropic`
的裸 host —— 因为用户照抄官方 base_url）：

| 写法 | auth URL 例 | 中继行为 |
|---|---|---|
| A. 完整带后缀 | `https://api.deepseek.com/anthropic/v1/messages@@sk-...` | Rule 1 命中 → 不重复拼 |
| B. 不带后缀（base） | `https://api.deepseek.com/anthropic@@sk-...` | Rule 3 → 拼上 `/v1/messages` |

> **📌 附加提示 —— 用户的处境**：用户会填什么样的 URL？
> 用户照抄 DeepSeek 官方给的 base_url（`https://api.deepseek.com/anthropic`），
> 官方文档就写明了要带 `/anthropic`。所以用户**永远不会**填出缺
> `/anthropic` 的裸 host——要么完整（带 `/v1/messages`），要么官方
> base（带 `/anthropic`、不含 `/v1/messages`）。中继只需覆盖这两种，
> 不必为"裸 host + 缺协议前缀"的假想情形设计分支。

**背景**：所有 Anthropic 兼容 SDK（包括 Claude Code）在拼请求路径时会**自动在 base URL 后面追加 `/v1/messages`**。用户配置 `ANTHROPIC_BASE_URL=http://127.0.0.1:8088/anthropic` 时，SDK 实际打到 `http://127.0.0.1:8088/anthropic/v1/messages`。OpenAI 兼容 SDK 同理（追加 `/v1/chat/completions` 等）。**DeepSeek 官方文档原文**：

> base_url (Anthropic): `https://api.deepseek.com/anthropic`
> base_url (OpenAI): `https://api.openai.com`

也就是说，用户在 `ANTHROPIC_AUTH_TOKEN` 里写的 `url@@key` 中的 `url` 部分是**base URL**（不带 endpoint 后缀），期望"中继补完后缀转发给上游"。如果不补，请求到了 base根路径就被 DeepSeek 拒为 404。

#### 中继的责任：把客户端路径中的 endpoint 后缀拼到 base URL 后面

客户端打 `http://127.0.0.1:8088/anthropic/v1/messages`，`ANTHROPIC_AUTH_TOKEN` 里写 `https://api.deepseek.com/anthropic@@sk-xxx`：

| 阶段 | URL |
|---|---|
| 客户端 SDK 实际请求 | `http://127.0.0.1:8088/anthropic/v1/messages` |
| ASGI scope.path | `/anthropic/v1/messages` |
| ASGI scope.root_path | `/anthropic` |
| 剥掉平台前缀后 suffix | `/v1/messages` |
| **最终转发到上游** | **`https://api.deepseek.com/anthropic/v1/messages`** ✓ |

`scope["path"]` 是客户端完整路径，`scope["root_path"]` 是 FastAPI `include_router(prefix="/anthropic")` 自动剥掉的平台前缀——ASGI 嵌套 mount 的官方答案。剥前缀 + 拼接逻辑在 `passthrough/middleware.py::_join_client_path()`。

#### 拼接规则（防双拼）

base 是 auth URL 的 path 段（不含 scheme/host），suffix 是剥前缀后的客户端 path。

**Rule 1 — 扫 base_path 所有后缀位置**：找最长的"也是 suffix 前缀"的 base_path 尾。

```python
for i in range(len(base_path), 0, -1):
    tail = base_path[i:]
    if suffix == tail or suffix.startswith(tail):
        return base + suffix[len(tail):]
```

例子：

| base_path | suffix | tail 命中 | 最终 |
|---|---|---|---|
| `/anthropic/v1/messages` | `/v1/messages` | `/v1/messages`（==） | `https://.../anthropic/v1/messages` |
| `/anthropic/v1/messages` | `/v1/messages/count_tokens` | `/v1/messages`（startswith） | `https://.../anthropic/v1/messages/count_tokens` |
| `/v1/messages` | `/v1/messages` | `/v1/messages`（==） | `https://.../v1/messages` |

**Rule 2 — /v1 去重**：`base_path.endswith("/v1")` 且 `suffix.startswith("/v1")` → 剥掉重复 `/v1`（OpenCode `https://opencode.ai/zen/go/v1` 这种情况没有 segment-aligned overlap，Rule 1 漏掉）。

| base_path | suffix | 最终 |
|---|---|---|
| `/opencode/zen/go/v1` | `/v1/messages` | `https://opencode.ai/zen/go/v1/messages` |
| `/opencode/zen/go/v1` | `/v1/chat/completions` | `https://opencode.ai/zen/go/v1/chat/completions` |

**Rule 3 — 直接拼**：

| base_path | suffix | 最终 |
|---|---|---|
| ``（空，upstream 无 path） | `/v1/messages` | `https://host/v1/messages` |
| `/anthropic` | `/v1/messages` | `https://host/anthropic/v1/messages` |

#### 与转换模式对齐

`proxy._anthropic_messages_url()` 做的是同一件事——base URL + `/v1/messages`。但它**只支持 Anthropic 协议**（仅 `/v1/messages` 一种后缀），不处理 OpenAI 的 `/v1/chat/completions` 等。透传模式的 `_join_client_path()` 更通用：suffix 直接来自客户端实际打的路径，**协议无关**。

#### 用户配置习惯对齐

用户在配置 Claude Code 时按 `ANTHROPIC_BASE_URL=http://127.0.0.1:8088/anthropic` 这种 base URL 习惯填写，**auth 头里不需要手打 `/v1/messages` 后缀**——SDK 拼路径、中继拼转发，与现有转换模式行为完全一致。

### 4.5 上游名生成

```
去掉 http:// 或 https://
+ URL 余下部分
+ | + model 字段名
+ | + api-key 末 4 位
```

例：`api.openai.com/v1/chat/completions|model|efgh`

用户可在设置页重命名（设 `display_name`），空字符串清除用户命名回到自动生成。

## 5. 状态机

```
[关闭] -- 启用开关 --> [开启]
  │                      │
  │                      │ -- (客户端发 url@@key 请求) --> 直接转发，写 passthrough.db
  │                      │ -- (客户端发 /api/* 请求) ----> 走管理 API
  │                      │
  └── 关闭开关 <--       │
```

切换：
- 内存：`settings.passthrough_mode = bool(value)`，中间件每次请求读这个值
- 持久化：`.env` 的 `PASSTHROUGH_MODE=true/false`
- 中间件：始终在栈上，OFF 时是零开销透传

## 6. 数据流（请求处理）

```
客户端                      中间件                                上游
  │                            │                                  │
  │ POST /anything             │                                  │
  │ x-api-key: url@@sk-x       │                                  │
  │ {"model":"gpt-4o",...}     │                                  │
  │ ─────────────────────────► │                                  │
  │                            │ 1. read body                     │
  │                            │ 2. parse auth → (url, sk-x)       │
  │                            │ 3. extract model → "gpt-4o",      │
  │                            │    field_name="model"             │
  │                            │ 4. filter headers                 │
  │                            │ 5. rewrite x-api-key → "sk-x"     │
  │                            │ 6. upsert upstream                │
  │                            │ 7. client.stream(url, body)       │
  │                            │ ──────────────────────────────►  │
  │                            │                                  │
  │                            │ ◄────────── response ──────────  │
  │                            │ 8. forward each chunk to client  │
  │                            │ 9. response end → deep_search    │
  │                            │    → record to passthrough.db    │
  │ ◄───────────────────────── │                                  │
```

## 7. 数据库 Schema

```sql
-- 每次调用一行
CREATE TABLE passthrough_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    url TEXT NOT NULL,
    api_key_alias TEXT NOT NULL,
    model_field_name TEXT NOT NULL DEFAULT 'model',
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    status_code INTEGER,
    error TEXT,
    request_method TEXT,
    request_path TEXT
);

-- 自动发现的上游（PK = fingerprint 三元组）
CREATE TABLE passthrough_upstreams (
    url TEXT NOT NULL,
    api_key_alias TEXT NOT NULL,
    model_field_name TEXT NOT NULL DEFAULT 'model',
    display_name TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    request_count INTEGER DEFAULT 0,
    PRIMARY KEY (url, api_key_alias, model_field_name)
);
```

## 8. 客户端字段解析

| 优先级 | 字段名 | 备注 |
|---|---|---|
| 1 | `model` | Anthropic + OpenAI + Responses 默认 |
| 2 | `model_id` | 某些兼容实现 |
| 3 | `engine` | 极少数协议 |

第一次命中即用，**`field_name` 是 fingerprint 的一部分**——同一 URL + key 但模型字段名不同的请求算不同上游。

## 9. 响应 usage 提取

**深度递归搜索**响应 body：
- 顶层 / 嵌套 dict / list 都扫描
- 匹配字段：`input_tokens` / `output_tokens` / `prompt_tokens` /
  `completion_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`
- OpenAI legacy 的 `prompt_tokens` / `completion_tokens` 折叠为 `input_tokens` /
  `output_tokens`
- **取最大值**（处理 OpenAI cumulative output_tokens）
- SSE：每个 `data:` 行单独 parse + 整 buffer 也尝试（兜底非标准 envelope）

## 10. HTTP 路由

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/passthrough/mode` | 读开关状态 |
| PUT | `/api/passthrough/mode` | 切换开关（持久化 .env） |
| GET | `/api/passthrough/upstreams` | 列自动发现的上游（key 已 mask） |
| PUT | `/api/passthrough/upstreams/{url:path}/rename` | 重命名（query 带另两 fingerprint） |
| GET | `/api/passthrough/stats` | 统计聚合（dim × range） |

## 11. 已知 trade-off

| 决策 | 接受的不便 |
|---|---|
| `@@` 分隔符 | URL 里含 `@@` 无法表达；用户需避免 |
| 自动拼接客户端 path 后缀 | 内部按 3 条规则防双拼（见 §4.4b）；用户写 base URL 即可，与 Claude Code SDK 习惯一致 |
| 不规范化上游 URL | 同一上游不同写法 = 两个 upstream；用户需写一致 |
| model 名按字面 | `gpt-4o` / `gpt-4o-2024-08-06` 算两个 model 行 |
| model_field_name 是 fingerprint | 同一 URL+key 但字段名不同 = 两个上游（极少见） |
| 透传 + 转换 不混跑 | 全局开关，没有中间态 |

## 12. 决策演进历史

透传模式设计过程中 URL 处理经历了三次主要变更：

### 12.1 v0.XX 初始设计："URL 不拼路径"

最初的决策（拍板时间 2026-08-20 上午）：客户端打什么路径，auth 头里的 URL 就是目标地址，**中继不做任何拼接**。理由是用户当时明确说"直接往那个 url 发不久好了吗"。

### 12.2 v0.XX+1 决策反转："中继帮拼客户端 path"（方案 B）

**触发场景**：用户贴出真实部署配置 `ANTHROPIC_AUTH_TOKEN=https://api.deepseek.com/anthropic@@sk-...`，发现 base URL 是裸地址不带 `/v1/messages`，按 v0.XX 设计会把请求发到 `https://api.deepseek.com/anthropic` 根路径 → DeepSeek 404。

**根因复盘**：用户之前的认知里"base URL = 请求目标"，但实际所有 Anthropic 兼容 SDK（Claude Code 也在内）都会**自动在 base URL 后面追加 `/v1/messages`**。这条隐性行为用户在 v0.XX 设计前未意识到，所以"URL 不拼路径"的假设在转换模式正常工作是因为 `proxy._anthropic_messages_url()` 在补，透传模式没人补就 404。

**认知修正引用**："我之前一直不清楚实际上所有的 anthropic 标准后面都有 `/v1/messages` 这样的后缀，实际上虽然我的配置文件是裸地址，但是 claude 自动帮我补全了才发出的"——用户

**拍板**：方案 B（用户零配置负担，与 Claude Code SDK 习惯一致）。代码改动只动 1 个文件（`passthrough/middleware.py`），参考 `proxy._anthropic_messages_url()` 实现 3 条拼接规则。

### 12.3 v0.XX+2 边界条件修复："upstream 路径是 suffix 的祖先"双拼

**触发场景**：用户二轮提出"要是人家自带了你还拼后缀上去那不就出错了"。

**复盘 v0.XX+1 的 R1 漏洞**：`base.endswith(suffix)` 只判"完全相等"，漏了"base 是 ancestor"。例如：
- auth URL = `.../anthropic/v1/messages`
- 客户端 SDK 自动追加后路径 = `/anthropic/v1/messages/count_tokens`（Claude Code 调 count_tokens 接口）
- v0.XX+1 行为：R1 不命中 → R3 直接拼 → `.../anthropic/v1/messages/v1/messages/count_tokens` 💥 双拼

**修法**：把 R1 改成"扫 base_path 所有末尾位置找最长的也是 suffix 前缀的尾"。O(N) 一次扫描覆盖 1a（完全相等）和 1c（ancestor）两种情况。

**测试覆盖**：新增 `tests/test_passthrough_path_join.py::test_join_no_double_append` 7 条用例 + 用 `"/v1/messages/v1/" not in actual` 这种硬断言防止回归。最终 `pytest = 17 passed`。

**经验沉淀**：
1. **实现前先问"用户实际 SDK 长什么样"**：本次 v0.XX 设计漏问"SDK 会自动做什么"，导致 base URL 假设错位。下次类似特性，必问"用户期望中继做什么 vs SDK 已经做什么"。
2. **边界条件测试要带"不可能出现"的硬断言**：参数化测"期望值"不够，得加 substring 断言。后续任何重构触碰 `_join_client_path` 都会被这条断言抓住。
3. **`endswith` 判 ancestor 关系是不够的**：要"base 末尾段 is suffix 前缀段"，得扫所有位置。简单的 O(N) 循环比嵌套 if 可靠。

## 13. 未来扩展点（未实现）

- 别名机制：把多个字面 URL 别名到同一个透传上游
- model alias：`gpt-4o-*` 折叠为 `gpt-4o`
- 插件 hook：`passthrough.pre_forward`、`passthrough.post_response`、
  `passthrough.usage_extracted` 已在 plugin.py 占位名预留，可被 plugins 接管