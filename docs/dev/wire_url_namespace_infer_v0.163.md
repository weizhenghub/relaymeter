# 跨协议 wire 推断补 URL 命名空间 + 流式路径 settings/agent 泄漏修复（v0.163）开发文档

## 1. 用户的初始指令

> 简单定位中继项目 → 我在 opencode 发送一条消息，它反复回复 `[Image #1][Image #2][Image #3]` → 再发变成 `Not Found` →
> 好的，用人话简单解释 → 我们原先的机制不是无论进来啥都要强制转换的吗？ → 没有很看明白，请再说直白一点。
>
> （中间穿插）当前的处理逻辑是不是有问题？ → 这次直接回复 Not Found。

### 场景拆解

- **现象 1（反复重试）**：opencode 里发"你好"，它像复读机一样反复回 `[Image #1][Image #2][Image #3]` 然后报错重试，每次间隔约 7 秒共 5 次。
- **现象 2（直接 Not Found）**：重启中继后再发，秒回 `Not Found`（opencode 侧 `AI_APICallError: Not Found`），不再重试。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | opencode openai 请求 → relay → 上游，链路要通 | 用户反复报错 |
| B | openai 入口的请求应走 cross-wire 强制转换（用户原话"原先机制不是无论进来啥都要强制转换吗"） | 现象 2 + 用户质疑 |
| C | `dp官方`（DeepSeek，`url=https://api.deepseek.com/anthropic`）必须被认成 anthropic-messages 上游 | 现象 2 根因 |
| D | 上游 wire 判定要看地址命名空间，不能只看进门平台段 | 现象 2 根因 |
| E | cross-wire 流式路径 `settings` 未定义 → NameError，整条流掐断 → opencode 反复重试 | 现象 1 根因 |
| F | adapter 路径（`_anthropic_adapter_relay_sse`）`settings`/`agent`/`raw_ua` 未定义 → NameError | 测试暴露的同类回归 |

### 隐含但需要确认的点（用户没说，要追问）

- 无新增提问：本轮是**修 bug 不是新增功能**，用户全程要我"直接说人话 / 直接改"，未要求先问需求。

---

## 3. 分析需求后得出的开发路径

### 定位过程（分两层，先现象后根因）

**第一层：现象 1（反复重试）→ cross-wire 流式路径 NameError**

- `chat.json`（本会话 log）里 23:47 的 opencode 请求无记录——因为 relay 和 Claude Code 的 log 是两套。
- `relay_trace.log`（`_tlog`，relay.trace）：三个 openai 请求停在 `CROSSWIRE ENTER`，**无 STREAM END**。
- 事故时活跃进程 uvicorn 日志：opencode 请求全部 `POST https://minnimax.chat/v1/messages "200 OK"`，但**无 post_response**。
- ASGI traceback 定位：
  ```
  proxy_legacy.py line 3160, in gen
      if req_db_id and settings.relay_save_messages:
  NameError: name 'settings' is not defined
  ```

**第二层：现象 2（Not Found）→ active 上游切换 + wire 误判**

- 重启中继后 active 上游从 `f3af39d7`(MiniMax) 切到 `dp官方`(DeepSeek)。
- `dp官方` 配置：`url=https://api.deepseek.com/anthropic`、`wire=None`。
- opencode 走 openai 入口，`_normalize_api_path` + `_join_upstream_url` 拼出：
  ```
  https://api.deepseek.com/anthropic/v1/chat/completions   → DeepSeek 没有该端点 → 404
  ```
- 根因：`cfg.effective_wire(platform)` 对 `wire=None` 的 `dp官方` 返回 `openai-chat`（`infer_wire_for_platform` 只认进门平台段，**从不看 base URL 里的 `/anthropic`**），恰好等于 opencode 的 client_wire → **判定同 wire → 走直通而非 cross-wire 转换** → 硬拼错误 URL。

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| wire=None 时如何认语言 | 新增 `infer_wire_from_url`，先按地址命名空间推 | 平台看 URL 前缀（`/anthropic`、`/openai`），与 config.py:106 设计说明一致 |
| 命名空间判定 | 子串匹配 `/anthropic` → anthropic-messages；`/openai` → openai-chat | 简单、覆盖 DeepSeek/OpenAI family |
| 认不出时 | 回退 `infer_wire_for_platform(platform)` | 存量配置 zero 变化（`minnimax.chat` 无命名空间仍按平台段推） |
| 影响面控制 | 只影响 `wire=None` 上游 | 显式声明 wire 的上游一律不被 `infer_wire_from_url` 覆盖（`is_known_wire` 优先） |
| 泄漏修复范围 | cross-wire + adapter 两条路径的 `settings`/`agent`/`raw_ua` 一并补 | 三者同源：v0.155/v0.157 加 `agent`/`raw_ua`、v0.120 加 `relay_save_messages` 时漏了部分函数 |

---

## 4. 实现中遇到的问题

### 问题 1：`infer_wire_for_platform` 与设计注释自相矛盾

config.py:100/106 说"平台看 URL 前缀（/anthropic、/openai），wire 看上游线协议长相"，但 `infer_wire_for_platform`（config.py:163）只按 `platform` 段推，**从不看 URL**。`dp官方` 这种 `url=/anthropic` 的上游被错判成 openai-chat。

**解法**：不动 `infer_wire_for_platform`（它仍作为"无命名空间"兜底），新增 `infer_wire_from_url` 接入 `effective_wire`。

### 问题 2：`settings` 未定义共有 4 处，分属两条路径

grep 出 `settings.relay_save_messages` 在 proxy_legacy.py 有 7 处，其中 4 处所在的函数**没有 `settings` 在作用域内**：
- `_relay_cross_wire`（2675）：2899（非流式）/3160（流式）两处
- `_anthropic_adapter_relay`（399）：594 一处
- `_anthropic_adapter_relay_sse`（676）：881 一处

**解法**：
- `_relay_cross_wire`：入口 `settings = app_state.settings`（它有 `app_state`）。
- `_anthropic_adapter_relay`：`settings = request.app.state.settings`（它有 `request`）。
- `_anthropic_adapter_relay_sse`：新增 `settings` 参数，由调用方透传（它没有 `request`/`app_state`）。

### 问题 3：adapter SSE 路径 `agent`/`raw_ua` 未定义（测试暴露）

`_anthropic_adapter_relay_sse` 的 `gen()` finally 里 `db.record(agent=agent, raw_ua=raw_ua)` 引用这两个变量，但该函数没有这两个参数。这是 v0.155/v0.157 加 `agent`/`raw_ua` 时的**同源回归**——只给部分函数补了参数，漏了 `_anthropic_adapter_relay_sse`。跑 `test_proxy_integration.py` 时暴露。

**解法**：`_anthropic_adapter_relay_sse` 加 `agent`/`raw_ua` 参数（默认 `""`），调用方（`_anthropic_adapter_relay`）透传。

### 问题 4：pytest 组合跑挂起 / temp 目录被锁

`pytest-of-weizheng/pytest-current` 目录被占用，跑测试时 `PermissionError: [WinError 5]`。这是**测试基建问题**（Windows temp 目录锁），不是测试失败。

**解法**：按项目既定约定（`feedback_test_infra_chase`：组合 pytest 挂起/栈溢出单独跑全绿就记录不追），用 `--basetemp` 指定独立临时目录绕开，逐文件/逐组单独跑，全部通过后记录不深追。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 上游 wire 不看 URL | 新增 `infer_wire_from_url`，`effective_wire` 先按地址命名空间推 | config.py |
| #2 `settings` 4 处泄漏 | cross-wire 入口 `app_state.settings`；adapter 入口 `request.app.state.settings`；sse 加参数透传 | proxy_legacy.py |
| #3 adapter sse `agent`/`raw_ua` | sse 函数加参数 + 调用方透传 | proxy_legacy.py |
| #4 pytest temp 锁 | `--basetemp` 独立目录，逐组单独跑 | 测试执行方式 |

### 端到端验证（修复前后对照）

对 `dp官方`（`url=https://api.deepseek.com/anthropic`, `wire=None`），opencode 走 openai 入口：

| 项目 | 修复前 | 修复后 |
|---|---|---|
| `effective_wire(openai)` | `openai-chat`（只看平台段） | `anthropic-messages`（看 URL） |
| client_wire vs upstream | 相等 → **直通** | 不等 → **触发 cross-wire 转换** |
| 拼出 URL | `.../anthropic/v1/chat/completions` → **404** | `.../anthropic/v1/messages` → **正常** |

---

## 6. 是否完全遵循规划路径开发

**部分偏离**，但都是防御性修复，无新增功能。

### 完全按规划（无偏离）：

- 现象 1 根因锁定 cross-wire 流式 `settings` NameError。
- 现象 2 根因锁定 `dp官方` 上游 wire 误判 + URL 硬拼。
- 修复方式 = 补 `settings` 绑定 + 加 URL 命名空间推断，都是把该有的逻辑补上。

### 偏离之处：

- **(a) 修了 `settings` 之外，测试暴露了 adapter SSE 的 `agent`/`raw_ua` 泄漏**：这不是用户报的场景（adapter 路径当前没走到），但属**同源回归**（v0.155/v0.157 加参数漏函数），且就在同一文件同一次改动里，一并修掉。**属防御性补充，不算偏离规划。**
- **(b) 用 `--basetemp` 绕 pytest temp 目录锁**：纯测试执行方式调整，不影响产品代码。

### 重大调整：无。

---

## 7. 最终实现点

### 核心修复：`config.py`

1. **新增 `_URL_NAMESPACE_WIRE`**（config.py:179）：命名空间 → wire 映射。
   ```python
   _URL_NAMESPACE_WIRE = (
       (WIRE_ANTHROPIC_MESSAGES, ("/anthropic",)),
       (WIRE_OPENAI_CHAT, ("/openai",)),
   )
   ```

2. **新增 `infer_wire_from_url(url)`**（config.py:185）：按 base URL 末段命名空间推断上游 wire；认不出返回 `None`。
   - `https://api.deepseek.com/anthropic` → `anthropic-messages`
   - `https://api.deepseek.com/openai` → `openai-chat`
   - `https://minnimax.chat`（无命名空间）→ `None`（交给平台段推断兜底）

3. **`effective_wire`**（config.py:353）：`wire=None` 时先按 URL 命名空间认语言，认不出才退回平台段推断。
   ```python
   def effective_wire(self, platform: str) -> str:
       if is_known_wire(self.wire):
           return self.wire
       url_wire = infer_wire_from_url(self.url)
       if url_wire is not None:
           return url_wire
       return infer_wire_for_platform(platform)
   ```

### 泄漏修复：`proxy_legacy.py`

4. **`_relay_cross_wire`（2675）**：入口加 `settings = app_state.settings`，修复 2899（非流式）/3160（流式）两处 `settings.relay_save_messages` NameError。

5. **`_anthropic_adapter_relay`（399）**：`settings = request.app.state.settings`，修复 594 处。

6. **`_anthropic_adapter_relay_sse`（676）**：新增 `settings`/`agent`/`raw_ua` 参数（默认 `""`），修复 881/875/876 处；调用方（`_anthropic_adapter_relay`）透传。

### 行为验收清单（手动测试项）

- [ ] opencode 走 openai 入口发消息 → 不再 `[Image #N]` 重试、不再 `Not Found`
- [ ] `dp官方`（DeepSeek anthropic 上游）被 openai 请求命中 → 走 cross-wire 转换，拼出 `api.deepseek.com/anthropic/v1/messages`
- [ ] 上游从 MiniMax 切到 DeepSeek 后 opencode 仍能正常回话
- [ ] Claude Code 本体（anthropic 入口）行为不变
- [ ] 显式声明 wire 的上游（如 `f3af39d7`=anthropic-messages、`f3af39d7-openai`=openai-chat）行为不变
- [ ] `minnimax.chat` 这类无命名空间 + wire=None 的上游仍按平台段推断（存量行为）
- [ ] adapter 路径（`requires_anthropic_adapter` 上游）不做 `settings`/`agent`/`raw_ua` NameError

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（+ 约 30 行：`_URL_NAMESPACE_WIRE` + `infer_wire_from_url` + `effective_wire` 接入） |
| `src/relay/proxy_legacy.py` | 改（+ 约 12 行：`_relay_cross_wire`/`_anthropic_adapter_relay` 补 settings 绑定 + sse 补 3 参数透传） |
