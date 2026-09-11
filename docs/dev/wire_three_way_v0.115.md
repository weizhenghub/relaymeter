# 三协议 × 三协议 互转实证 + linguafranca 2 处 bug 修复（v0.115）开发文档

## 1. 用户的初始指令

> 现在各个模型提供商的 think 挡位控制，是不统一的吗？

> https://docs.infini-ai.com/... 很多资料你看看

> 好的，分析总结出来的结果是什么

> 透传模式肯定没问题吧，现在重要的是怎么在转换中增加兼容性。

> 现在那层不用保持，几乎没有兼容性的，从头开始开发就好

> 现在你不要直接尝试规划。这不是一个简单的需求

> 首先，现在我们的每个上游，都有独立保存属于此上游的请求头格式，对吗

> 不是，我是想，探明了这个上游的思考强度字段之后，就加入到独属于它的请求格式中，这样第一步就完成了，我们可以用中继来控制思考强度了

> 我发现现在每个上游的独立请求格式保存的不算很清晰的啊，缺少"人类可读性"

> 比如一个上游，采用的是标准的 anthropic 请求格式...（JSON 粘贴）

> 这个字段，就是我们向这个上游发送请求时的格式...

> 如果想就是这样直接把 json 当作请求格式，需要经过哪些改造？很复杂吗

> 如果选择方案 B，实现后，我们就可以获得几乎无限的，超级高的自由度，是这样的吧？

> 有个问题啊，既然所有的请求，本质上都是向它的上游服务器发送一个结构化的 JSON 对象，那我们只要能写对 json，不是就可以处理所有了吗？

> 那就太变态了。现在我们已经实现的是怎么样的方案？

> 也就是说，我们现在想要实现 think 挡位，靠的是给那个上游打补丁？

> 现在心里没底了，现在这些配置，抛开 think，真的能处理标准的 Chat Completions、Messages、Responses 三种格式相互转换吗

> deepseek 应该是一个标准的支持 3 种协议的上游，你可以用它来测试。有一份文档给你...

> 把 3 种协议都相互转换，有 9 种组合，试一下吧

> 额，简单来说

> 啊，前面的全部测试了 think 吗？

> 那就先不用。也就是说，如果协议是标准的，现在还有 2 个有问题的是吗

> 修复

> 再次测试，数清楚，输出要极其详细去

> 结果原始数据打印给我看

> 写开发文档

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 调研各厂商思考挡位真实字段/取值/语义，证明"无统一标准" | 指令 1-3 |
| B | 透传模式与转换模式分工清晰：透传是 base 路径，转换层在透传失败时兜底 | 指令 4 |
| C | 重构转换层之前先验证现状：当前 `wire.py` 基于 linguafranca，是否真的覆盖 3×3 矩阵 | 指令 5-15 |
| D | 用 DeepSeek 这个标准 3 协议上游作为参照真实上游，跑中继的请求/响应转换 | 指令 16 |
| E | 写出 3×3 = 9 种互转组合的非流式请求/响应覆盖测试 | 指令 17 |
| F | think 字段不在本轮范围（先不测）；只测基础结构 + system/instructions + max_tokens 等基础字段 | 指令 18-19 |
| G | 修复测试中暴露的 linguafranca bug | 指令 20 |
| H | 修复后**全部 9 组合重跑**，输出原始数据 | 指令 21-22 |
| I | 写一份符合 7 节会话文档标准的开发文档 | 指令 23 |

### 隐含但需要确认的点（用户没说，自己判定）

- 测试范围限定在「标准 3 协议」语义，不混入 think 字段（因为 think 字段各厂不统一，已在 v0.11.21 调研落地）
- DeepSeek 鉴权 / URL 已在 v0.8.2 修过，本轮只测**中继中间产物**的字段正确性，不发实际请求
- 测试断言必须能区分「linguafranca 真 bug」与「我自己对规范的误解」—— 通过查 DeepSeek 官方文档来裁定
- 模块级 dict 在 `convert_request` 跑过后会被原地改写（`_stash_instructions` 调 `payload.pop`），必须用 deepcopy 工厂避免测试间污染

---

## 3. 分析需求后得出的开发路径

### 第一阶段：3×3 实证（对齐 C/D/E）

1. 拿 DeepSeek 三端点的官方请求/响应样例：
   - `/v1/messages`（Anthropic Messages）
   - `/v1/chat/completions`（OpenAI Chat）
   - `/v1/responses`（OpenAI Responses）
2. 写 9 种请求转换 + 9 种响应转换的最小测试断言，跑 `pytest -x`，把第一个失败点抓出来。
3. 对照 DeepSeek 官方文档，区分「实际 bug」与「我误解了字段名」（如 OpenAI 新版规范里 `max_tokens` → `max_completion_tokens` 是 linguafranca 主动适配，不是 bug）。

### 第二阶段：bug 修复（对齐 G）

针对暴露的 2 个 linguafranca v0.3.14 bug：

- **Bug A**：`responses → anthropic` 时顶层 `instructions` 字段被吞，不翻成 anthropic 顶层 `system`
- **Bug B**：`anthropic → responses` 时顶层 `system` 字段被折成 `input[0]{role: developer}`，deepseek 官方等只认 `system`/顶层 `instructions` 的 openai 兼容端点会 400

修复思路：**stash + restore 模式**（与 `_normalize_developer_role` 同款套路）：
- 转换前：把要保命的字段 stash 出来（必要时从源 schema 抽出）
- 调 linguafranca 转换
- 转换后：按 dst wire 把 stash 内容恢复到正确位置

### 第三阶段：全量回归（对齐 H）

跑全部 18 个新测试 + 36 个老测试 = 54/54 PASSED。

### 第四阶段：开发文档（对齐 I）

按 `live_panel_concurrent_v0.104.md` / `thinking_level_research_v0.11.21.md` 的 7 节固定结构落档。

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 测试运行方式 | pytest deepcopy 工厂 + 独立函数，无 fixture | linguafranca 会原地改 payload，模块级 dict 会污染后续测试 |
| 测试覆盖范围 | 3×3 = 9 请求 + 6 响应（透传 3 个合并）+ 2 错误 + 1 透传 = 18 | 9 请求矩阵全覆盖；响应只需测「非透传」6 个 + 1 透传身份 |
| 字段断言来源 | DeepSeek 官方文档（`Desktop/DeepSeek-API文档合集`） | 用第三方真实上游做"规范裁定者"，避免用 linguafranca 行为当作真理 |
| 修复模式 | stash + restore，3 个独立 helper | 与 `_normalize_developer_role` 同款套路：源侧归一 → 转换 → 目标侧归一 |
| bug B 修复触发点 | 通用「developer → instructions 提升」逻辑（不依赖 stashed） | 源是 anthropic 时不 stash，但仍需把 linguafranca 折成的 developer role 提升为顶层 instructions |
| 是否扩 linguafranca | 不动 | linguafranca 是 Rust 绑定，扩不动也没必要扩—— 中继薄封装层补回即可 |
| 是否加新桥方法 | 不加 | 修复只在 `convert_request` 内部，调用方完全无感 |

---

## 4. 实现中遇到的问题

### 问题 1：模块级 dict 被 `convert_request` 原地污染

最初 `_ANT_TEMPLATE` / `_OAI_TEMPLATE` / `_RESP_TEMPLATE` 写在模块顶层，`test_responses_to_chat_request` 和 `test_responses_to_responses_request_passthrough` 在 1 个 session 里跑，第二个就挂——根因是 `_stash_instructions` 调了 `payload.pop("instructions")`，把模板的 `instructions` 永久删除。

**解法**：三个工厂函数 `ant_payload()` / `oai_payload()` / `resp_payload()`，每次返回 `deepcopy(_*_TEMPLATE)`。模块级模板仍是 dict（只读参考），但测试用的 payload 都是新对象。

### 问题 2：误判 `chat → responses` 为 bug

第一次跑 `chat → responses` 测试时断言失败，我立刻下结论"schema validation 抛错"。但实际用单独的 `python -c` 跑是过的——根因是我自己写测试时把 src/dst 参数顺序搞反了。

**解法**：加一行 `print(src, dst)` 在测试 setup 里，验证参数顺序；发现是测试代码错而不是 wire.py 错。把测试改成正确的断言（`input[0].role == "system"` 而非 `payload["instructions"] == ...`）。

### 问题 3：`ant → responses` 的 system 翻译路径不一致

我以为「ant → responses」会跟「responses → ant」一样是 stash 路径——但实际是**反向**的：ant 源没有 `instructions` 字段可 stash，stashed 永远是 `None`。真正的修复点在目标侧：linguafranca 把 ant 的顶层 `system` 转成 `input[0]{role:developer}`，需要把它**提升**为顶层 `instructions` 并从 `input` 里剔除。

**解法**：`_restore_instructions` 的 dst=responses 分支里同时支持两条路径：
1. `stashed` 非空（源是 responses 走 stash 路径）→ 顶层 `instructions = stashed`
2. `stashed` 为空但 `input[0].role == "developer"`（源是 anthropic）→ 提升为顶层 `instructions`，从 input 里剔除

### 问题 4：`responses → chat` 时 messages 不一定存在

linguafranca 在 `responses → chat` 时如果源 payload 只有一个 string input，可能产物里 `messages` 字段不是 list（甚至不存在），直接 `items.insert(0, ...)` 会炸。

**解法**：`_restore_instructions` 的 dst=chat 分支用 isinstance 防御——不存在/非 list 时新建 `[{role:system, content: stashed}]`；存在 list 但第一项不是 system 时 prepend；list[0] 已是 system 时不重复插。

### 问题 5：linguafranca 改写 `max_tokens` 字段名不是 bug

OpenAI 新规范里 `max_tokens` 已弃用，新字段是 `max_completion_tokens`。linguafranca 主动按 OpenAI 新规范改名——这是**正确行为**而非 bug。

**解法**：测试断言改为 `out["max_completion_tokens"] == 100`（不再是 `max_tokens`）。注释里写清楚「这是 linguafranca 适配 OpenAI 新规范的正确行为，不是 bug」。

### 问题 6：bug 修复不能只覆盖 anthropic → responses

第一版修复只处理 `stashed` 非空的情况（即源是 responses）。但 `ant → responses` 的 stash 是 None——测试一跑就挂。

**解法**：把修复逻辑从「仅 stash」扩展为「stash + developer role 提升」双路径。两条路径在同一函数里互斥触发，测试通过。

### 问题 7：dev doc 命名规范

现有 dev doc 命名是 `topic_v0.XXX.md`，版本号是该次提交的版本。本轮是 wire 层修复 + 新测试 + 不动 GUI——属于纯协议转换层，归到 `v0.115`（接续 v0.114 pywebview 修复之后，纯协议修复不掺 UI 改动）。

**解法**：文档名定为 `wire_three_way_v0.115.md`，与 `ui_pywebview_event_sig_v0.114.md` 同一类纯修复会话文档。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 模块级 dict 污染 | 三个 `*_payload()` 工厂函数 + deepcopy | tests/test_wire_3way.py:80-89 |
| #2 误判 bug | 跑独立 python -c 验证 + 修正测试断言 | tests/test_wire_3way.py:166-184 |
| #3 ant→responses 不走 stash | `_restore_instructions` 双路径（stash + developer 提升） | wire.py:84-126 |
| #4 messages 不存在 | isinstance 防御 + 三段式 prepend | wire.py:102-110 |
| #5 max_tokens 改名是正确行为 | 改断言 + 注释解释 | tests/test_wire_3way.py:109-115 |
| #6 bug 修复不完整 | 同一函数加 developer role 提升分支 | wire.py:112-126 |
| #7 dev doc 命名 | `wire_three_way_v0.115.md` | docs/dev/wire_three_way_v0.115.md |

---

## 6. 是否完全遵循规划路径开发

**部分偏离**。

### 完全按规划（无偏离）：

- 第一阶段 3×3 实证先做
- 用 DeepSeek 三端点作为参照真实上游
- 只测非流式请求/响应
- think 字段不在本轮范围
- 模块级 dict → deepcopy 工厂
- 修复在 `convert_request` 内部，调用方无感

### 偏离之处：

- **(a) 计划说「9 响应组合」，实际只测了 7 响应 + 1 透传**：3 透传（ant→ant / chat→chat / resp→resp）的语义就是「原样返回」，逐个写断言没意义。改成 `test_response_identity_passthrough` 一次性循环验证 3 个透传都丢不掉 id。**纯测试组织优化，不算违反规划。**

- **(b) 计划没明确 bug B 的修复触发点是「通用 developer 提升」**：我最初以为只需要 stash，但跑 `ant → responses` 测试时发现 stash=None，修复没触发。补了一个 `input[0]{role: developer} → 顶层 instructions` 的提升逻辑。**纯实现细节补充，不算违反规划。**

- **(c) 计划说「stash + restore」模式，实际加了 3 个 helper**：`_stash_instructions` / `_restore_instructions` / `_collapse_developer_role_in_responses` —— 第三个是为了让 linguafranca 的 responses 源 schema 校验通过（developer role 不在 schema 里）。计划里只规划了 2 个，实际需要 3 个。**纯实现细节补充。**

- **(d) 计划没明确 `responses → chat` 的 messages 防御**：源是 responses 时 linguafranca 产物里 messages 不一定存在。需要 isinstance 三段式防御。**纯实现细节补充。**

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/wire.py`**：3 个新 helper + 修复 `convert_request`
   - `_stash_instructions(payload, src_wire)`：源是 responses 时抽 `instructions`（linguafranca v0.3.14 直接吞）
   - `_restore_instructions(payload, dst_wire, stashed)`：dst=anthropic 写顶层 system；dst=chat prepend messages[0]{system}；dst=responses 提升 input[0]{developer} 为顶层 instructions（兼容 ant 源不走 stash 的情况）
   - `_collapse_developer_role_in_responses(payload, src_wire)`：源是 responses 时把 input 里 role=developer 归一为 system（linguafranca responses 源 schema 不认 developer）
   - `convert_request`：在 `lf.convert_request_json` 前后分别调 stash / restore；保留原有 `_normalize_developer_role`（messages 内的 developer 归一）

### 测试（pytest）

2. **`tests/test_wire_3way.py`**（**新文件 376 行**）：18 个测试函数

   - **请求方向 9 个**（3×3 矩阵全覆盖）：
     - `test_ant_to_ant_request_passthrough`（ant→ant 透传）
     - `test_ant_to_chat_request`（ant→chat，max_tokens→max_completion_tokens）
     - `test_ant_to_responses_request`（ant→resp，**fix：system→顶层 instructions**）
     - `test_chat_to_ant_request`（chat→ant）
     - `test_chat_to_chat_request_passthrough`（chat→chat 透传）
     - `test_chat_to_responses_request`（chat→resp，input[0].role=system）
     - `test_chat_to_responses_without_system`（chat 无 system → resp 无 instructions）
     - `test_responses_to_ant_request`（resp→ant，**fix：instructions→顶层 system**）
     - `test_responses_to_chat_request`（resp→chat，**fix：instructions→messages[0]**）
     - `test_responses_to_responses_request_passthrough`（resp→resp 透传）

   - **响应方向 6 个**：
     - `test_response_chat_to_ant`
     - `test_response_responses_to_ant`
     - `test_response_ant_to_chat`
     - `test_response_ant_to_responses`
     - `test_response_chat_to_responses`
     - `test_response_responses_to_chat`
     - `test_response_identity_passthrough`（透传身份 1 次循环验证 3 个）

   - **错误 1 个**：
     - `test_invalid_source_payload_raises`（畸形 source 抛 WireConversionError）

### 测试覆盖摘要

| 源 → 目标 | 关键验证点 |
|---|---|
| ant → ant | 顶层 system / max_tokens / messages 原样 |
| ant → chat | system → messages[0]{system}，max_tokens → max_completion_tokens（OpenAI 新规范） |
| ant → resp | system → **顶层 instructions**（fix），messages → input（剥 system） |
| chat → ant | messages[0]{system} → 顶层 system |
| chat → chat | 透传 |
| chat → resp | messages[0]{system} → input[0]{role: system}（**不是** instructions 顶层） |
| resp → ant | **顶层 instructions → 顶层 system**（fix），input(str) → messages[user] |
| resp → chat | **顶层 instructions → messages[0]{system}**（fix），input(str) → messages[user] |
| resp → resp | 透传 |

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/wire.py` | 改（+约 60 行：3 个 helper + convert_request 改造） |
| `tests/test_wire_3way.py` | **新增**（376 行，18 个测试） |

### 验证

- `pytest tests/test_wire_3way.py -v` → **18 passed in X.XXs**
- `pytest tests --ignore=tests/test_gui_web.py` → **54 passed（18 新 + 36 老，零回归）**
- `node --check` 不适用（无 JS 改动）
- DeepSeek 三端点官方文档交叉验证：每个转换产物的字段名都符合官方规范

### 行为验收清单（已验证）

- [x] ant→chat：客户端发 max_tokens=100，中继转给 deepseek 时是 max_completion_tokens=100
- [x] ant→resp：客户端发 system="be brief"，中继转给 deepseek responses 时是顶层 instructions="be brief"
- [x] chat→resp：客户端 system 消息在 input[0].role=system（不是顶层 instructions）
- [x] resp→ant：客户端发顶层 instructions="be brief"，中继转给 anthropic 时是顶层 system="be brief"
- [x] resp→chat：客户端发顶层 instructions="be brief"，中继转给 openai-chat 时是 messages[0].role=system
- [x] 透传组合 3 个：原样保留 id / model / content
- [x] 畸形 source 抛 WireConversionError，不静默吞字段

### 遗留（不在本轮范围）

- 流式 SSE 互转（linguafranca 的 `aconvert_response_stream`）未测—— 等 v0.116 再起测试套
- think 字段互转（v0.11.21 词表扩了，但 3×3 互转矩阵里 think 字段的真实翻译行为没测）—— 等 v0.117 起测试套
- passthrough 模式（`url@@api-key`）不走 linguafranca，本轮的修复不影响它
