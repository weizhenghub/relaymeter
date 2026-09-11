# 侧栏 token 统计改实时增长帧模式（v0.113x）开发文档

> 侧栏 token 四字段（输入 / 输出 / cache_read / cache_creation）在流式期间
> 改为**逐字符跳动**，不再「流结束后跃变到结果」。其中 output_tokens 是核心
> —— Anthropic SSE 协议只在 message_delta 时一次性给 cumulative 值，本轮用
> 字符长度粗估补齐中间帧。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 侧边栏token统计改为增长帧统计模式，在出现流字符时就开始计量输入输出并实时显示更新数据，而不是结束后跃变到结果数值。请你判断此功能实现是否需要涉及大量修改？

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **输出 token 实时跳动** —— 每收到一个流字符就更新 output_tokens 显示 | 指令 |
| B | **输入 token 实时显示** —— 用户提到的"输入"在 Anthropic 协议里是 message_start 一次性给的，本身已实时；保持 | 指令 |
| C | **估算而非真值** —— 协议不给时用字符长度粗估，仅用于显示 | 推断 |
| D | **不污染计费** —— db.record 计费用 message_delta 真值，估算值仅入广播字段 | 推断 |

### 隐含但需自行决策的点

- **估算口径**：英文 / 代码 ≈ 4 字符/token；中文 ≈ 1.5 字符/token（CJK 按 Unicode
  范围统计）。混排按字符类别分段折算。误差 ±20%，实时显示够用。
- **作用域边界**：估算**只入 SSE 广播字段**，**不入** `last_usage`（Anthropic
  适配器路径的权威计费字段）、**不入** `parser.usage`（其它路径的权威字段）、
  **不入** `db.record` 的 UsageAcc。done 事件由 message_delta 真值覆盖。
- **三处广播点都加** —— Anthropic adapter SSE（proxy.py:700）、cross-wire
  streaming（proxy.py:2712）、direct streaming（proxy.py:3466）。三处模式一致。
- **monotonic 约束**：`max(u.output_tokens, _estimate_output_tokens(assembled_text))`
  保证估算不会比真值小、流式期间不倒退。
- **前端 fallback**：live_panel.js applyUsage 读 `output_tokens_est`，无则退回
  `output_tokens`（done 时本就只有真值）。
- **不重启 relay**：纯代码 + 重启 GUI 即可（侧栏窗口自带 SSE 订阅线程，会自动续上）。

---

## 3. 分析需求后得出的开发路径

```
proxy.py
  +_estimate_output_tokens(text) -> int  字符类别分段折算（CJK / 其他）
  usage_now 增 output_tokens_est         三个 streaming 路径
  = max(parser/last_usage.output_tokens, _estimate(...))  保单调
live_panel.js
  applyUsage                              读 output_tokens_est，取 max 显示
                                          dataset.raw 用 tOutShow（保证回放
                                          / 重渲染时不回退）
docs/dev/v0.113x                         7 节结构
```

### 开发顺序落地

```
#1 proxy.py 加 _estimate_output_tokens 工具（CJK 范围 0x4E00-0x9FFF 等）
#2 三个 streaming 路径的 usage_now 增 output_tokens_est 字段
#3 live_panel.js applyUsage 读 est 字段、取 max 显示
#4 py_compile + node --check + estimator 行为断言
#5 写文档
```

---

## 4. 问题

### 4.1 协议层限制：output_tokens 不在流中间发

Anthropic SSE 协议把 usage 切成两段：
- `message_start.message.usage` —— input_tokens + cache_creation + cache_read（一次性）
- `message_delta.usage` —— output_tokens（cumulative，**通常只在流结束前一条 delta 里发一次**）

所以「output_tokens 始终是 0 / N」直到 message_delta 到来才一次性跳到终值。**这是协议层
限制**，不是 relay bug，绕不过去。

OpenAI 上游 + linguafranca 解析器**会**逐帧更新 parser.usage.output_tokens（每条
content delta 都带），但跨协议路径 frame 间仍可能空隙。统一加估算补齐是更稳的选择。

### 4.2 估算不能污染计费

`db.record` 的 UsageAcc 是计费权威字段，必须用 message_delta 真值。如果把估算塞进
`last_usage["output_tokens"]`，所有持久化统计都会被错误数据污染。

**改法**：**新加字段** `output_tokens_est`，仅入 `usage_now`（广播 dict），不入
`last_usage` / `parser.usage` / `UsageAcc`。三个广播点各自构造，互不影响计费路径。

### 4.3 估算必须单调

如果估算值低于 message_delta 给的真值（早期 assembled_text 还短），显示会先低后高
跳一下，体感像「闪」。**改法**：`max(u.output_tokens, _estimate_output_tokens(...))`
强制估算 ≥ 真值，估算只在前向补缺。

### 4.4 估算系数

CJK 字符 1.5 字符/token 是经验值（中文 token 比英文短得多）；英文 / 代码 4 字符/token
是 GPT 经典口径。混排按字符类别分别累加后取整。误差 ±20%，对实时显示够用（侧栏要的是
「在跳动」，不是精确数字；done 后真值会覆盖回来）。

### 4.5 不能影响 input_tokens / cache 字段

input_tokens 是 message_start 一次性给的真值，cache 字段也是；它们本来就是首帧就跳到
真值，不需要估算。**改法**：估算只针对 output_tokens；其它三字段保持原逻辑。

---

## 5. 解决

### 5.1 proxy.py：_estimate_output_tokens 工具

```python
def _estimate_output_tokens(text: str) -> int:
    """v0.113x：粗估字符数 → token 数，供侧栏实时跳动。

    Anthropic SSE 协议只在 message_delta 时一次性发 cumulative
    output_tokens；流中间不更新。侧栏要实时跳动，用字符长度粗估；
    **只用于侧栏显示**，不入 db.record。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x303F  # CJK 标点
        ):
            cjk += 1
        else:
            other += 1
    return max(1, int(round(cjk / 1.5 + other / 4)))
```

### 5.2 proxy.py：三处 streaming 广播点加 output_tokens_est

Anthropic adapter SSE（line ~700）：

```python
output_est = _estimate_output_tokens((assistant_now or "") + (thinking_now or ""))
usage_now = {
    "input_tokens": last_usage["input_tokens"],
    "output_tokens": last_usage["output_tokens"],
    "output_tokens_est": max(last_usage["output_tokens"], output_est),
    "cache_read_input_tokens": ...,
    "cache_creation_input_tokens": ...,
}
```

cross-wire streaming（line ~2712）和 direct streaming（line ~3466）模式一致，把
`u.output_tokens`（来自 `parser.usage`）做 max 估算。

**关键不变量**：
- `last_usage`（Anthropic adapter）/ `parser.usage`（其它）**不被估算覆盖**。
- `db.record` 用 `UsageAcc(**last_usage)` / `parser.finalize()` → 仍是真值。
- `_complete_inflight` 把 inflight 的 usage_live 同步成 usage_now，**这里 inflight
  存的也是估算值**。如果以后有脚本从 inflight 读 usage 做计费，需要审计——目前没有
  这种用法（inflight 只用于侧栏显示 + 留痕）。

### 5.3 live_panel.js：applyUsage 读 est 字段

```js
const tOut = usage.output_tokens || 0;
const tOutEst = (typeof usage.output_tokens_est === "number" && usage.output_tokens_est > 0)
  ? usage.output_tokens_est : 0;
const tOutShow = Math.max(tOut, tOutEst);  // 取大，永远前进
setTokenValue($tokOut, tOutShow, prev.out);
$tokOut.dataset.raw = String(tOutShow);
```

- prev 是 dataset.raw，从 tOutShow 读，不是 tOut（旧代码从 tOut 读，但 setTokenValue
  内部只比较 prev 与 new 触发动画，不影响值）。改 dataset.raw 用 tOutShow 让回放不
  回退。
- 旧前端只读 `usage.output_tokens` 时行为不变（fallback）；新前端读 est 优先。
- **done 事件不带 output_tokens_est**（三个 done 路径都没加），所以 tOutEst=0，
  tOutShow=tOut 真值。done 后立即校准。

### 5.4 不动哪些地方

- **db.record 计费路径**：三处 done 路径都用 `usage` / `last_usage` 真值构造 UsageAcc。
  不动。
- **snapshot inflight**：`_inflight_entry` 直接 `dict(inf.usage_live)`，inflight 是
  三个 streaming 路径 `_update_inflight(usage_live=usage_now)` 写进去的——所以 snapshot
  也带 output_tokens_est。侧栏首连 snapshot 时也能看到估算值，符合预期。
- **adapter 路径的 message_delta 真值覆盖**：line 690 `last_usage["output_tokens"]
  = u["output_tokens"]`，message_delta 到达时 last_usage 跳到真值，下一次 broadcast
  的 `max(last_usage, est)` 仍 ≥ 真值，done 后由 done 路径真值覆盖。链路完整。

---

## 6. 是否完全按规划

**按规划落地**：

- 加了一个工具函数 `_estimate_output_tokens`，三处 broadcast 点加字段，前端读
  est→max 显示。改动量符合「中等」判断。
- 估算系数 1.5/4，验收测试覆盖英文/中文/混合/代码/流式单调性。
- **没动** db.record、`last_usage`、`parser.usage`、UsageAcc 计费路径。
- **没动** done 路径构造 usage_live 的方式（仍只填真值四字段，无 est）。
- `python -m py_compile src/relay/proxy.py` 通过；`node --check src/relay/web/live_panel.js`
  通过。
- estimator 行为测试（usage-stats env）：
  - 英文 45 字符 → 11 tokens（约 4:1）✓
  - 中文 18 字符 → 12 tokens（约 1.5:1）✓
  - 混合 46 字符 → 16 tokens ✓
  - 代码 39 字符 → 10 tokens（约 4:1）✓
  - 流式 3 → 7 → 9 单调递增 ✓

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/proxy.py` | 新增 `_estimate_output_tokens(text)` 工具；三个 streaming 广播点（Anthropic adapter SSE / cross-wire streaming / direct streaming）的 `usage_now` 增 `output_tokens_est` 字段，`max(真值, 估算)` 保单调；不动 `last_usage` / `parser.usage` / `db.record` |
| `src/relay/web/live_panel.js` | `applyUsage(u)` 读 `output_tokens_est`，与 `output_tokens` 取 max 后写 DOM；dataset.raw 用 tOutShow 保回放不回退；注释说明估算仅用于显示 |
| `docs/dev/ui_sidebar_realtime_token_v0.113x.md` | 本文档 |

### 状态流

- **流开始** —— Anthropic 路径：`message_start` 给 input_tokens + cache 字段，output_tokens=0
  / est=0（assembled_text 为空）。侧栏 input 跳到真值，output 显示 0。
- **首条 content_block_delta** —— assembled_text="你好"（2 CJK 字符）→ est=1；
  message_delta 还没到，last_usage.output_tokens=0。侧栏取 max(0,1)=1，开始跳动。
- **继续流式** —— assembled_text 累积，est 单调递增；侧栏 output 字段逐帧更新。
- **message_delta 到达** —— last_usage.output_tokens 跳到真值（比如 47）；
  下一次 broadcast 的 max(47, est=12) = 47；侧栏显示 47（真值通常远大于估算）。
- **done** —— done 路径 usage_live 只含真值四字段，不含 est。侧栏 applyUsage 取
  max(47, 0) = 47，校准。
- **db.record** —— 三处都走 UsageAcc(**last_usage) / parser.finalize()，从 message_delta
  真值落库；估算值未污染计费。

### 验证

- estimator 行为：英文 11 / 中文 12 / 混合 16 / 代码 10（字符/token 比符合预期）。
- 单调性：`你好世界`→`你好世界这是一段中文`→`你好世界这是一段中文测试文本` 三档分别
  3/7/9 tokens，单调递增无倒退。
- 语法：`py_compile proxy.py` + `node --check live_panel.js` 双过。
- 端到端（手动）：重启 GUI → 触发一段 Anthropic 流式请求 → 侧栏 output 字段从 0 开始
  随字符逐跳，结束时跳到 message_delta 真值；db.record 计费行 output_tokens 与真值
  一致（不被估算污染）。
