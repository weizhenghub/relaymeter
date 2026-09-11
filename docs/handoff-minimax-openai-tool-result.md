# 交接文档：MiniMax 中转站 openai 端点工具调用「Invalid tool parameters」

> **性质**：下次修复的工作底稿（handoff）。本次已定位根因并改代码 + 通过单测，
> 但**未做真机端到端验证**（8088 端口中继不能杀，须用户自己重启 GUI 拉新代码）。
> 下次接手先跑「验证步骤」，过了就收尾；没过按「备选排查方向」继续。
>
> **日期**：2026-08-19
> **版本**：v0.97.4（代码已改，待验证）
> **关联文档**：`docs/development.md`（「跨线转换拆块 + 思考型上游 reasoning_content
> 回传」章节的 v0.97.4 收尾小节）、`PROGRESS.md`（v0.97.4 条目）

---

## 一、一句话结论

v0.97.3 修完请求侧 400 后，用户实测 `f3af39d7-openai`（MiniMax openai 端点，跨线
路径）**依然**调不了工具 —— 模型正确返回了 tool_use，但 Claude Code 报
**「Invalid tool parameters」**。根因是 **linguafranca 流式转换 openai tool_calls →
anthropic 时，若首个 chunk 就携带非空 `arguments`（MiniMax/DeepSeek 一上来把完整
参数塞进首片），会静默丢参数** → 客户端收到 `tool_use input={}`。修法是拆片绕行，
单测全绿，待真机验证。

---

## 二、两轮 bug 的脉络（同一个上游，两段链条）

| | v0.97.3（上一轮，已修） | v0.97.4（本轮，本次改） |
|---|---|---|
| 现象 | 工具结果**回传不上**，断在第二跳 | 模型**已出 tool_use**，但客户端报「Invalid tool parameters」 |
| 链条位置 | **请求侧**（回传 assistant(tool_calls) 时补 reasoning_content 门太宽） | **响应侧**（上游 tool_calls 流转 anthropic 时参数被 linguafranca 丢） |
| 根因 | `reasoning_content=""` 被注入进所有 openai 上游 → MiniMax 400 | linguafranca 依赖「首片留空参数」的 OpenAI 分片约定，MiniMax/DeepSeek 首片塞满 → 参数被静默丢弃 |
| 修复 | 注入门收窄到 DeepSeek 系思考型（`_upstream_needs_reasoning`） | `sse_events` 里拆片绕行（`_split_openai_tool_call_arguments`） |

**为什么 v0.97.3 修完才暴露 v0.97.4**：v0.97.3 之前，请求侧 400 让模型**根本到不了**
返回工具调用的阶段（第一轮就断）；修完 400 没了，模型能出 tool_use 了，才暴露出
响应侧这一层 —— 工具调用本身带不出参数。所以这两轮是「同一现象两段链条上的不同
环节」，不是返工。

---

## 三、现象与证据

### 用户报告

- 截图：模型正确思考「Let me use Bash to list the desktop directory」，但工具调用被
  拒，**「Invalid tool parameters」连续出现两次**。
- 出问题的上游：`f3af39d7-openai`（MiniMax openai 端点，wire=openai-chat，跨线路径）。
- `f3af39d7`（anthropic 端点，同 wire 字节透传）**一直正常** —— 再次印证「同 wire
  透传碰不到这类问题」。

### 复现证据（直连 linguafranca，usage-stats env）

```python
# openai-chat → anthropic-messages 流式转换
# A. 单片首片即带完整参数（MiniMax/DeepSeek 行为）→ 参数被丢
[..., {"content_block": {"id":"call_abc","input":{},"name":"Bash","type":"tool_use"}, "type":"content_block_start"},
      {"index":0,"type":"content_block_stop"}, ...]   # 无 input_json_delta！
# B. 首片空参数 + 独立全量参数片 → 参数保留
[..., {"delta":{"partial_json":"{\"command\":\"ls\"}","type":"input_json_delta"}, "type":"content_block_delta"}, ...]
```

注意：**丢参数不报错、无 warning** —— linguafranca 静默丢。这是比「报错」更难查的
一类问题，只能靠「首片空 vs 首片满」对照复现才能确认。

### 日志佐证

DB 里 f3af39d7-openai 所有请求 status=200（上游本身没问题）；跨线路径不记
UPSTREAM REQ/RESP，但 DB 证明请求都正常到达并返回，问题在下游转换层。

---

## 四、根因分析（完整）

### linguafranca 对分片流的隐含依赖

OpenAI Chat 流式 tool_calls 的官方格式：一次工具调用按 `index` 分片推送 —— 首片带
`id` / `function.name` 且 `function.arguments=""`，后续片只带 `function.arguments`
**碎片**（`'{"co'` → `'mmand":"ls"}'` 这样拼）。linguafranca 的 openai 流式转换器
**依赖这个约定**：它认为「带 id 的那片是空参数」，建 `content_block_start input:{}`，
之后每个带 arguments 的片都当增量 delta 追加。

MiniMax / DeepSeek 这类上游**不遵守**：它们把**完整 JSON 参数直接塞进首片**
（`arguments:'{"command":"ls"}'`）。linguafranca 在建 content_block 时读了 id/name，
却**忽略了同一片里已带的 arguments**，随后又因为没有「后续参数片」而不发任何
`input_json_delta` → 客户端收到的 tool_use 是 `input={}` → Claude Code 报
「Invalid tool parameters」。

### 为什么 non-stream 不受影响

非流式（`convert_response`）转换时 arguments 是**整体**保留的（`input` 字段直接转），
没有「分片累积」这一步，所以参数不丢。只有**流式**路径中招。

### 为什么 anthropic 端点 / 同 wire 透传不受影响

`f3af39d7`（wire=anthropic-messages）走字节透传，不经过 linguafranca 转换；
openai 端点才走 `_relay_cross_wire` → `sse_events` → `convert_stream`。

### 受影响范围

所有 **openai-chat wire 上游**的跨线**流式**响应理论上都走这条路，但只有「首片即带
完整参数」的上游（MiniMax openai 端点、opc-deepseek、opc-hy3 等「这堆模型」）实际
触发；OpenAI 官方分片流不受影响（首片本来空参数）。

---

## 五、当前代码状态（已改了什么）

### 改动文件 1：`src/relay/proxy.py`

1. **新增** `_split_openai_tool_call_arguments(d, opened)`（proxy.py:980，紧挨
   `_merge_split_assistant_messages`）：
   - 扫描 chunk 的 `choices[].delta.tool_calls[]`；对「未开片且首片即带非空
     arguments」的 tool_call，拆成两片：
     - **空参数首片**：原 chunk 深拷贝，该 tool_call 的 `function` 置为
       `{"name": <原名>, "arguments": ""}`（id/name 保留 → linguafranca 建
       content_block）；
     - **独立全量参数片**：`{index, function:{arguments: <完整参数>}}`（linguafranca
       当增量 delta → 产出 `input_json_delta`）。
   - `opened` set 记录已见 id/name 的 index：`id` 片 / 空参数片 → 记入；已开片的
     增量参数 → 原样透传（OpenAI 分片流不受影响）。返回空列表 = 无需拆。
2. **改** `sse_events()`（proxy.py:2361-2391）：openai 分支在 `yield d` 前调
   `_split_openai_tool_call_arguments(d, opened_tool_call_indexes)`，返回非空则依次
   yield 拆出的 chunk；`opened_tool_call_indexes` 在 `sse_events` 内初始化、逐响应
   一个（每次请求/响应一个流，tool_call index 从 0 重新计数，无需跨响应保留）。

（v0.97.3 的 `_upstream_needs_reasoning` 注入收窄**保持不动** —— 那是请求侧的修复，
与本次响应侧问题独立，且已确认在代码里。）

### 改动文件 2：`tests/test_wire.py`

新增 4 条：
- `test_split_full_args_first_chunk_recovers_arguments`：拆片后 linguafranca 产出
  `input_json_delta`，`json.loads(partial_json) == {"command":"ls"}`（参数不丢）。
- `test_split_leaves_incremental_stream_unchanged`：OpenAI 分片流三段 `== []`
  （不拆），原样透传后参数拼回完整。
- `test_split_parallel_tool_calls_all_first_chunk_full`：MiniMax/DeepSeek 平行多工具
  （每个 index 首片都带完整参数），转换后两个 tool_use 参数都保留。
- `test_split_multiple_tool_calls_only_unopened`：同 chunk 多 tool_call 只拆未开片，
  已开片增量参数保留原位。

### 测试结果

```
pytest tests/test_wire.py   →  23 passed（含 v0.97.1/0.97.2/0.97.3 既有 reasoning 测试仍全绿）
pytest tests/test_proxy_integration.py  →  22 passed
```

### 尚未做

- ❌ **真机端到端验证**：没有「GUI 重启 → f3af39d7-openai 跑真实工具调用」的验证。
- ❌ 没有在真实日志里确认「重启后 f3af39d7-openai 的响应能正常出 tool_use + 参数」。

---

## 六、下次验证步骤（Step by Step）

> 前置：代码已改完，但 8088 端口的中继进程还在跑**旧代码**（配置无热重载，proxy.py
> 改了必须重启进程才生效）。**严禁 kill 8088**（会中断用户当前 chat），须由用户在
> GUI 里点重启。

1. **重启中继**：用户点 GUI 的重启（或关闭 GUI 再启动），拉新 proxy.py。
2. **切上游**：在 GUI 里把 active 上游切到 `f3af39d7-openai`（MiniMax openai 端点）。
3. **发一个需要工具调用的请求**：让 Claude Code 跑一个命令，例如
   「帮我看一下桌面上有啥文件」（会触发 Glob/Bash 工具调用）。
4. **观察是否成功**：
   - 模型应返回 tool_use（Glob / Bash）；
   - 工具**参数应完整**（不再是 `input={}`），工具结果能回传、第二轮能正常继续；
   - 侧栏「工具调用」栏应有带参数的 assistant tool_use JSON；
   - **不再出现「Invalid tool parameters」**。
5. **核对日志**（`relay_trace.log`）：
   - 搜 `f3af39d7-openai`，确认 `CROSSWIRE ENTER` 之后有 `STREAM END ... status=200`；
   - 若想确认拆片生效，可在代理日志里 grep 空参数首片 / 独立参数片的产出（或直接
     看客户端收到的 tool_use input 是否非空）。
6. **回归确认（防误伤）**：
   - 切回 `deepseek官方-openai`（DeepSeek 思考型 openai 端点），跑一个多轮工具调用，
     确认 v0.97.2/0.97.3 修的「400 中断」仍然修好；
   - 切回 `f3af39d7`（anthropic 端点），确认同 wire 透传不受影响；
   - 用一个 OpenAI 官方分片流上游（如 claude 上游转 openai 端点，若有）确认增量流
     不因拆片重复/丢片。

---

## 七、如果验证失败的备选排查方向

按可能性从高到低：

1. **仍报「Invalid tool parameters」**：说明拆片没生效或没覆盖到。检查：
   - `_split_openai_tool_call_arguments` 是否真的在 yield 前被调用（grep 日志
     / 断点）；
   - MiniMax 的 chunk 形态是否与假设一致 —— 用 `scripts/debug_dump_sse.py` 抓
     f3af39d7-openai 原始 SSE，看首片 tool_call 是否真的带非空 arguments、是否
     分片。**可能上游本来就是分片发（首片空参数），那根本不是这条路的问题。**
   - 若 chunk 的 `id` 字段缺、或 `function` 结构不同，拆片条件可能不命中，需适配
     （比如有的上游首片无 `function`，arguments 在后续片带 `id` —— 但这种情况
     linguafranca 反而不会丢）。
2. **报的不是 Invalid tool parameters，是别的**：grep 客户端错误信息原文，按新
   错误搜。可能是参数 JSON 本身畸形（如带注释/单引号），或 `input` 类型不对。
3. **多 tool_call 并行时某个参数丢**：可能是 `opened` set 逻辑在多 index 交错时
   判断错。用 `test_split_multiple_tool_calls_only_unopened` 场景扩展复现。
4. **确认走的确实是跨线路径**：`/live` 快照或日志里 `inbound_wire`/`outbound_wire`
   应为 `anthropic-messages`/`openai-chat`。若 wire 判断有变，那根本不是这条路径。

---

## 八、关键文件 / 代码位置索引

| 位置 | 内容 |
|---|---|
| `src/relay/proxy.py:850` | `_REASONING_BY_TOOL_CALL` 有界 dict（响应侧留存，v0.97.1） |
| `src/relay/proxy.py:854` | `_bind_reasoning_to_tool_calls`（响应侧按 id 绑定） |
| `src/relay/proxy.py:870` | `_upstream_needs_reasoning`（v0.97.3 DeepSeek 系判定，**保持不动**） |
| `src/relay/proxy.py:887` | `_inject_reasoning_to_messages`（v0.97.3 注入） |
| `src/relay/proxy.py:924` | `_merge_split_assistant_messages`（v0.97.2 拆块合并） |
| `src/relay/proxy.py:980` | **新增** `_split_openai_tool_call_arguments`（v0.97.4 拆片） |
| `src/relay/proxy.py:2363-2391` | **改动** `sse_events()` openai 分支（yield 前拆片，拆片调用在 2387-2388） |
| `src/relay/proxy.py:2087` | `_relay_cross_wire`（跨线转换入口） |
| `src/relay/proxy.py:2740` | `relay()` 里跨线分叉（`platform_wire != upstream_wire`） |
| `src/relay/wire.py` | linguafranca 薄封装（`convert_stream` 在 :101） |
| `tests/test_wire.py` | 新增 3 条 v0.97.4 测试 + 既有 reasoning 测试 |
| `C:\Users\weizheng\AppData\Local\Relay\upstreams.json` | **真实配置**（f3af39d7-openai 的定义在这里，不在项目里） |
| `C:\Users\weizheng\AppData\Local\Relay\relay.db` | **真实 DB**（不在项目里） |
| `relay_trace.log` | 跨线转换 trace 日志（`CROSSWIRE ENTER` / `STREAM END` / `UPSTREAM RESP`） |
| `scripts/debug_dump_sse.py` | 抓上游原始 SSE 的工具（用 base miniconda env，有 httpx） |

---

## 九、与既有文档的关系

- **`docs/development.md`**：v0.97.4 收尾小节（在「跨线转换拆块 + 思考型上游
  reasoning_content 回传」章节 v0.97.3 之后）已同步写入现象/根因/修复/验证/经验。
- **`PROGRESS.md`**：顶部已有 v0.97.4 条目，记录了本次修复的一句话结论 + 细节。
- **`docs/wire-dispatch-plan.md`**：跨线转换路径的权威规划（§4.1 `id→tool_call_id`
  同名映射是「按 id 回传」成立的地基）。
- **`docs/opencode-zen-upstreams.md`**：OpenCode Zen 转发的 deepseek 模型配置说明
  （`_upstream_needs_reasoning` 通过「模型名含 deepseek」识别这类上游的原因）。

---

## 十、一句话给下次接手

v0.97.3 修的是请求侧 reasoning 注入；**v0.97.4 修的是响应侧 linguafranca 丢
tool_calls 参数**（MiniMax/DeepSeek 首片塞满参数 → 拆成 空参首片 + 独立全量参数片），
单测 22+22 全绿；下次唯一要做的是**重启 GUI 后对 f3af39d7-openai 跑一次真机工具调用
验证**（确认 tool_use 带参数、不再报 Invalid tool parameters），并回归确认
deepseek官方-openai（400 修复）与 f3af39d7（anthropic 透传）没被误伤。
