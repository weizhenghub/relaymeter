# 所有请求路径保存消息原文与回复（v0.120a）开发文档

## 1. 用户的初始指令

> 所有的请求默认开启保存消息原文与回复，并在设置中添加此选项的开关。

补充澄清：
- 后端 `relay_save_messages` 默认 `True`（v0.12 起），设置页「数据」分组已有开关。
- 实际发现：只有 2 个主路径保存消息原文，其他 4 个路径只写 `db.record`（请求元数据）不写 `db.record_messages`（消息原文），导致请求详情 modal 显示「未保存对话内容」。
- 修复目标：所有有效请求路径都保存消息原文 + 回复。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 后端 `relay_save_messages` 默认 `True` | 初始指令（沿用 v0.12 默认） |
| B | 设置页开关「保存消息原文与回复」 | 初始指令（已有 v0.12 + v0.113） |
| C | **所有**请求路径都保存消息原文 | 隐含但被遗漏（v0.119 之前的 PR 只覆盖 2 路径） |
| D | upstream_error 路径也保存（仅用户请求，无回复） | 设计自决：错误现场有价值 |
| E | dispatch reject 路径不保存（无效请求，无 body） | 设计自决：拒绝路径无意义 |
| F | 保持现有 `record_messages` 调用风格（user_text / user_json / assistant_text / assistant_json） | 不偏离 |
| G | 错误隔离：`record_messages` 失败不阻断请求完成 | 沿用主路径风格（try/except + log.exception） |

### 隐含但需要确认的点（已通过澄清 / 设计自决）

- **路径清单**：v0.12 时代 2 条主路径（`forward_request` 内的非流式 + SSE 分支）；v0.119 / v0.120 改造时新增 adapter 路径（anthropic 走 OpenCode 风格的协议适配）和 cross-wire 路径（anthropic↔openai 协议转换），共 4 条新路径未补 `record_messages`。
- **SSE 路径 assistant_json**：parser 没有 `raw_response` 字段（v0.11.x 旧 SSE parser 设计），所以 `assistant_json=None`；assistant_text 仍可从 `parser.assembled_text()` 取。
- **upstream_error 路径 assistant_text**：上游连接失败，**没有 assistant 回复**，只保存 user_text（用户原始请求）；assistant_text=None。
- **adapter SSE 路径 user_text 来源**：`inbound` 是 dict（不是 bytes），用 `json.dumps(inbound).encode("utf-8")` 临时转回 bytes 喂给 `_extract_last_user_message`。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位覆盖盲区

`proxy_legacy.py` 内所有调用 `db.record` 的位置（grep `db.record` 共 11 处）：

```
grep -n "await db.record" src/relay/proxy_legacy.py
519: adapter 非流式错误分支（HTTPError）
551: adapter 非流式成功分支              ← 没 record_messages
821: adapter SSE finally                 ← 没 record_messages
2581: dispatch reject（无效请求，无 body）  ← 不需要
2751: cross-wire connect error
2806: cross-wire 非流式                  ← 没 record_messages
3049: cross-wire SSE finally             ← 没 record_messages
3507: upstream_error（连接失败）         ← 没 record_messages
3565: 主路径非流式                        ← 已有 record_messages（v0.12）
3743: 主路径 SSE                          ← 已有 record_messages（v0.12）
```

**关键观察**：主路径 2 处有 `record_messages`；其他 5 个 `db.record` 调用都没有补。**未覆盖盲区 = 5 条路径**（adapter ×2、cross-wire ×2、upstream_error ×1）。

### 第二阶段：设计每条路径的修复模式

通用模式（与主路径对齐）：

```python
req_db_id = 0
try:
    req_db_id = await db.record(...)
except Exception:
    req_db_id = 0
    log.exception("db.record failed in X path")

if req_db_id and settings.relay_save_messages:
    try:
        user_text, user_json = _extract_last_user_message(body_bytes)
        await db.record_messages(
            req_db_id,
            user_text=user_text,
            user_json=user_json,
            assistant_text=parser.assembled_text() or None,
            assistant_json=...,  # 见下表
        )
    except Exception as exc:
        log.exception("db.record_messages failed in X path: %s", exc)
```

### 各路径差异表

| 路径 | `record()` 返回 | body 来源 | assistant_text | assistant_json |
|---|---|---|---|---|
| adapter 非流式 | `req_db_id` | `adapter_pinfo["body"]` | `parser.assembled_text()` | `resp.text` |
| adapter SSE | `req_db_id` | `json.dumps(inbound).encode()` | `parser.assembled_text()` | `None`（SSE 无 raw） |
| cross-wire 非流式 | `req_db_id` | `out_body_bytes` | `parser.assembled_text()` | `content.decode()` |
| cross-wire SSE | `req_db_id` | `out_body_bytes` | `parser.assembled_text()` | `None` |
| upstream_error | `req_db_id` | `body`（原始请求） | `None`（无回复） | `None` |

### 第三阶段：实现路径（按依赖顺序）

| # | 任务 | 依赖 |
|---|---|---|
| 1 | adapter 非流式路径补 record_messages | 无 |
| 2 | adapter SSE 路径补 record_messages | 无 |
| 3 | cross-wire 非流式路径补 record_messages | 无 |
| 4 | cross-wire SSE 路径补 record_messages | 无 |
| 5 | upstream_error 路径补 record_messages（仅 user） | 无 |
| 6 | 资源版本 bump（磨砂玻璃相关 + 路径修复） | 无 |
| 7 | 文档（dev doc + CHANGELOG） | 全部 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 路径全量补齐 vs 只补出现 bug 的路径 | 全量补齐 5 条 | 用户报一条 = 全部有问题，避免下一个用户继续报 |
| upstream_error 是否保存 | 保存（仅 user） | 用户原始请求有保留价值；assistant 没有 |
| dispatch reject 是否保存 | 不保存 | 拒绝路径无 body 可提取（API key 不匹配 / 模型不在 allowed_models），保存无意义 |
| 失败隔离 | `try/except + log.exception` | 主路径既有风格，不阻断 `_complete_inflight` |
| body 来源差异 | adapter: `adapter_pinfo["body"]` / SSE: `json.dumps(inbound)` / cross-wire: `out_body_bytes` | 每条路径 body 变量名不同，沿用现有变量 |
| SSE assistant_json | `None` | parser 没 `raw_response`；不破坏 `db.record_messages` 协议（Optional） |

---

## 4. 实现中遇到的问题

### 问题 1：adapter SSE 路径没有原始 body 变量

**症状**：`_anthropic_adapter_relay_sse` 函数签名收 `inbound: dict`（不是 bytes），无法直接喂 `_extract_last_user_message`。

**解法**：就地构造 `json.dumps(inbound).encode("utf-8")` 喂进去。开销可忽略（每次请求 1 次 JSON 序列化）。

### 问题 2：cross-wire 路径的 `out_body_bytes` 已被 `pre_upstream` 插件钩子修改

`pinfo["body"]` 是插件可能修改后的最终 body（更准确反映发出去的内容），但用户原始请求也可能想保留。

**决策**：用 `out_body_bytes`（实际发给上游的 body）。原因：用户最关心「我发了什么给上游」，而不是客户端发到中继的原始 bytes（可能已经过中继 adapter 转换）。

### 问题 3：upstream_error 的 assistant_text 是 None 时的数据库存储

**症状**：`record_messages` 收到 `assistant_text=None` 是否报错？

**验证**：看 `db.py:record_messages` 实现 —— None 允许（语义：assistant 没回复）。

**解法**：照传 `assistant_text=None, assistant_json=None`，数据库该 row 的 `assistant_text`/`assistant_json` 列为 NULL，前端「请求详情」modal 显示「未收到回复」。

### 问题 4：路径行号稳定性

每次修改 `proxy_legacy.py` 后行号会变，CHANGELOG 写「v0.120a: proxy_legacy.py:586-598」这种精确行号可能快速失效。

**解法**：dev doc 用「函数名 + 区块描述」定位（`adapter 非流式成功分支`），CHANGELOG 写近似行号（带「约」「区块」提示），不写死精确行号。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 adapter SSE 无 bytes body | 临时 `json.dumps(inbound).encode("utf-8")` | proxy_legacy.py |
| #2 cross-wire body 来源 | 用 `out_body_bytes`（plugin 修改后） | proxy_legacy.py |
| #3 upstream_error assistant None | 照传 None，db 层允许 | db.py 不改 |
| #4 行号稳定性 | 用函数名 + 区块描述 | docs/ |

**关键代码模板（5 条路径共用）：**

```python
req_db_id = 0
try:
    req_db_id = await db.record(...)  # 原有调用
except Exception:
    req_db_id = 0
    log.exception("db.record failed in X path")
# v0.120a：补 record_messages
if req_db_id and settings.relay_save_messages:
    try:
        user_text, user_json = _extract_last_user_message(body_bytes)
        await db.record_messages(
            req_db_id,
            user_text=user_text,
            user_json=user_json,
            assistant_text=parser.assembled_text() or None,
            assistant_json=...,  # 见差异表
        )
    except Exception as exc:
        log.exception("db.record_messages failed in X path: %s", exc)
```

**5 条路径修改位置：**

| 路径 | 行号（约） | 函数 / 区块 |
|---|---|---|
| adapter 非流式 | 551-598 | `_anthropic_adapter_relay` 非流式成功分支 |
| adapter SSE | 821-868 | `_anthropic_adapter_relay_sse` finally |
| cross-wire 非流式 | 2806-2872 | `_cross_wire_relay` 非流式响应分支 |
| cross-wire SSE | 3049-3127 | `_cross_wire_relay_sse` finally |
| upstream_error | 3507-3599 | `forward_request` 上游连接失败分支 |

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无偏离）。**

### 完全按规划：

- **5 条路径全量补齐**（adapter ×2 + cross-wire ×2 + upstream_error ×1），与原 2 条主路径对齐。
- **失败隔离风格**：所有 `record_messages` 调用包 `try/except + log.exception`，不阻断后续 `_complete_inflight`。
- **保持现有 `db.record` 调用签名**：不变，只多一步 `req_db_id = await db.record(...)` 拿到返回值。
- **`assistant_text` / `assistant_json` 语义**：成功路径填、错误路径 None；不强制必须填。
- **dispatch reject 不补**：设计自决，无 body 可提取。

### 偏离之处：

- **(a) 行号注释**：CHANGELOG 写「`proxy_legacy.py:586-598`」近似行号，实际后续修改会漂移。dev doc 用「函数名 + 区块」定位（更稳定）。**格式自决**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/proxy_legacy.py`**（5 处补 `record_messages`）：
   - `_anthropic_adapter_relay` 非流式成功分支：补 record_messages，body = `adapter_pinfo["body"]`，assistant_json = `resp.text`。
   - `_anthropic_adapter_relay_sse` finally：补 record_messages，body = `json.dumps(inbound).encode()`，assistant_json = None。
   - `_cross_wire_relay` 非流式响应分支：补 record_messages，body = `out_body_bytes`，assistant_json = `content.decode()`。
   - `_cross_wire_relay_sse` finally：补 record_messages，body = `out_body_bytes`，assistant_json = None。
   - `forward_request` upstream_error 分支：补 record_messages（仅 user），body = `body`，assistant_text/assistant_json = None。

2. **`src/relay/db.py`**：**未改**。`record_messages` 已有 Optional assistant_text/json 支持。

### 前端

无改动（仅资源版本 bump）。

### 资源版本

3. **`src/relay/web/styles-20260817.css`**：
   - `.window-controls` 加磨砂玻璃背景（`backdrop-filter: blur(23px) saturate(112%)` + 边缘渐变淡出）。
   - `?v=20260823-36` → `?v=20260824-01`（index.html + live_panel.html 同步 bump）。

### 测试

未新增测试（无新逻辑，纯加 `record_messages` 调用；失败处理已包 try/except；现有 `test_tui.py` 不覆盖此层）。

### 行为验收清单（手动测试项）

- [ ] 设置页 → 数据 → 「保存消息原文与回复」开关打开（默认就是 True）
- [ ] 走 adapter 路径（anthropic 客户端 → OpenCode 风格上游）发请求 → 历史页 → 请求详情显示原文 + 回复
- [ ] 走 cross-wire 路径（anthropic 客户端 → OpenAI 上游）发请求 → 历史页 → 请求详情显示原文 + 回复
- [ ] 走 SSE adapter 路径 → 历史页 → 请求详情显示原文 + 回复（assistant_json 列 NULL）
- [ ] 走 upstream_error 路径（上游连接失败）→ 历史页 → 请求详情显示用户原文，「未收到回复」提示
- [ ] 关闭开关 → 重新发请求 → 历史页 → 请求详情显示「未保存对话内容」（开关生效）
- [ ] 任何新上游（无论哪个平台、哪个 wire）都自动走上述 6 条路径之一，全部保存消息

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/proxy_legacy.py` | 改（+约 70 行：5 处 record_messages 调用） |
| `src/relay/web/styles-20260817.css` | 改（磨砂玻璃背景 + 资源版本 bump） |
| `src/relay/web/index.html` | 改（CSS ?v bump） |
| `src/relay/web/live_panel.html` | 改（CSS ?v bump） |
| `docs/CHANGELOG.txt` | 改（新增 v0.120a 块） |
| `docs/dev/save_messages_path_coverage_v0.120a.md` | **新增**（7 节结构） |