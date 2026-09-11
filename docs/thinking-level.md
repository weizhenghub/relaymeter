# 思考挡位机制开发文档（v0.11.21）

> 本文件记录"思考挡位（thinking level）"从客户端到真实模型的完整链路。
> 核心设计：**对内（嗅探）/ 对外（重写）两阶段分离**。

---

## 1. 概述

思考挡位用于控制模型在推理前的"思考强度"。整个机制分两段：

| 阶段 | 方向 | 作用 | 位置 |
|---|---|---|---|
| **对内（嗅探）** | opencode ← 中继 | 告诉 opencode 每个模型支持哪些挡位 | `routers/models.py` |
| **对外（重写）** | opencode → 中继 → 真实模型 | 把客户端选的挡位翻译成上游认识的 thinking 参数 | `proxy.py` |

两阶段互相独立：

- **对内**只影响 opencode 的 UI 展示（有哪些挡位可选）；
- **对外**只影响请求转发（真正发什么给上游）。
- 中继在中间翻译：**对内广告能力，对外转换实现**。

```
┌─────────────┐   对内(嗅探)    ┌──────────────┐   对外(重写)    ┌──────────────┐
│   opencode  │ ←────────────── │     中继     │ ──────────────→ │  实际模型     │
│   (客户端)   │  /models/       │ (Relay 8088) │  /v1/messages   │ (go/zen/      │
│             │   api.json      │              │                 │  minimax…)    │
└─────────────┘                 └──────────────┘                 └──────────────┘
```

---

## 2. 对内：嗅探端点 `GET /models/api.json`

### 2.1 为什么需要

opencode 通过 `OPENCODE_MODELS_URL` 环境变量把"模型元数据源"替换成中继的 URL，然后拉取 `{source}/api.json`。中继在这里充当一个 **models.dev 兼容源**。

### 2.2 实现（`src/relay/routers/models.py`）

```
逻辑：
1. 先拉官方源 https://models.opencode.ai/api.json（全量、带 1 小时进程内缓存）
2. 遍历中继配置里的 anthropic 上游，把声明了 thinking_options 的翻译成 opencode Provider 结构
3. 注入一个 id="relay" 的 provider（匹配 opencode config 里的 provider id）
4. 合并返回
```

- **官方源**：`https://models.opencode.ai/api.json`（注意不是 `models.dev`，那个在国内被墙）。
  - 带 `User-Agent` 请求头（否则 403）。
  - 失败回退到上次缓存，缓存 1 小时。
  - **代理而非替换**官方数据 → 官方 185 个 provider + relay 共存，不会丢官方模型。
- **relay provider 结构**（与 models.dev 兼容）：

```json
{
  "relay": {
    "id": "relay",
    "api": "anthropic",
    "npm": "@ai-sdk/anthropic",
    "name": "中继 (Relay)",
    "env": [],
    "models": {
      "deepseek-v4-flash": {
        "id": "deepseek-v4-flash",
        "reasoning": true,
        "reasoning_options": [{"type": "effort", "values": ["low","medium","high","max"]}],
        "limit": {"context": 200000, "input": 200000, "output": 65536},
        "modalities": {"input": ["text","pdf"], "output": ["text"]}
      }
    }
  }
}
```

- **provider id 必须固定 `relay`**：opencode 用 provider id 匹配 config 里的 `provider.relay` 连接信息（baseURL/apiKey），元数据才会挂到真实连接上。
- **哪些模型会出现在 relay provider 里**：只暴露 `thinking_options` 非空（即支持思考）的上游 —— 与"只有 go/zen 需要思考"的原则一致，不支持的模型不会出现，opencode 里也看不到挡位。
- **挡位翻译**（`thinking_options` → opencode `reasoning_options`）：

| upstreams.json 的 thinking_options | opencode 看到的挡位 |
|---|---|
| `[off, low, medium, high, max]` | effort 挡（低/中/高/极速） |
| `[off, minimal, low, medium, high, xhigh, max]` | effort 全量 6 挡（含 OpenAI 的 minimal/xhigh） |
| `[off, enabled]` | 开/关 |
| `[off]` 或未声明 | 无挡位 UI（模型不出现） |

> **v0.11.21 修订（2026-08-22）**：词表已放宽——原 §2.3 翻译表只列 4 挡（low/medium/high/max），会静默丢弃 minimal/xhigh/none，本次补全覆盖并修正广告层（详见 §8 调研）。
> **词表已放宽（v0.11.19+ 引入，2026-08-22 修正）**：`thinking_options` 可填 `off / none / minimal / low / medium / high / xhigh / max / enabled`，不再丢弃 `minimal`/`xhigh`/`none`。`_reasoning_options` 会把其中任意 effort 挡位原样翻给 opencode（顺序按 `minimal→low→medium→high→xhigh→max`）。`none` 与 `off` 语义等价（OpenAI/Gemini 用 `none` 表示关闭），但对外广告时 `off` 不入 effort 列表（关是单独状态）。具体取值由 operators 按真实上游如实声明，中继只原样透传，不替上游做"并档"。

### 2.3 opencode 侧配置

```bash
# 用户级环境变量（GUI 启动会继承）
OPENCODE_MODELS_URL=http://127.0.0.1:8088/models
```

设置后重启 opencode 即可。opencode 的加载优先级（从 asar 反编译确认）：

```
source = OPENCODE_MODELS_URL || "https://models.opencode.ai"
```

⚠️ **`OPENCODE_MODELS_URL` 是替换整个源**：若中继 api.json 只返回 relay，官方模型元数据会全部消失（这是之前踩过的坑）。中继现在代理官方源 + 注入 relay，两者共存。

---

## 3. 对外：思考重写 `proxy.py`

### 3.1 位置

转发管线中，**advanced-switch 决策之后、adapter 判断之前**（`proxy.py:1425-1433`），两条转发路径（adapter / byte-forward）都用重写后的 body。

**关键**：重写按的是**最终目标上游**（`cfg`）——如果 advanced-switch 把请求切到了强上游，重写会按强上游的能力处理。

### 3.2 解析客户端信号 `_client_thinking(body)`

从客户端请求体里找思考信号，三个来源任一：

| 字段 | 含义 | 示例 |
|---|---|---|
| `thinking` | Anthropic 原生 | `{type: enabled, budget_tokens: 20000}` |
| `reasoning_effort` | OpenAI 风格 | `"high"` |
| `reasoningEffort` | opencode 风格 | `"max"` |

归一化成 `{type: enabled/disabled, budget, effort}`。`None` = 客户端没发思考。

### 3.3 按上游能力重写 `_rewrite_thinking_for_upstream(body, cfg)`

```
客户端没发思考          → 原样返回（不干预）
上游支持               → 保留 thinking，effort 映射 budget_tokens
                         low=4096 / medium=16384 / high=32000 / max=64000
上游只支持开/关         → 保留 enabled/disabled，不设 budget
上游不支持(空/纯 off)   → 剥离所有思考字段（thinking/reasoning_effort/reasoningEffort）
```

- 统一输出 Anthropic 协议：`thinking: {type: enabled, budget_tokens: N}`，并删掉多余的开源 effort 字段。
- **绝不把上游不认的标签发过去**（防 400）。

---

## 4. 完整链路示例

```
1. opencode 启动 → 拉 http://127.0.0.1:8088/models/api.json
   → 得到官方 185 provider + relay(deepseek-v4-flash 带 4 挡)
2. 用户在 opencode 选 "relay/deepseek-v4-flash" + 挡位 high
3. opencode 发请求 → 中继 /anthropic/v1/messages
   body 带 reasoningEffort="high"（或 thinking.budget_tokens）
4. 中继 proxy：
   a. advanced-switch 决策（可能切目标上游）
   b. _rewrite_thinking_for_upstream(cfg)：
      - 目标上游支持 → thinking={type:enabled, budget_tokens:32000}
      - 目标上游不支持 → 剥离 thinking
5. 真实模型收到转换后的 thinking，按挡位思考
6. 日志：proxy 打印 "thinking-level rewrite: upstream=X -> {...}"
```

---

## 5. 相关文件

| 文件 | 作用 |
|---|---|
| `src/relay/routers/models.py` | 嗅探端点：代理官方源 + 注入 relay provider |
| `src/relay/proxy.py` | 对外重写：`_client_thinking` / `_rewrite_thinking_for_upstream`（v0.11.20） |
| `src/relay/config.py` | `PlatformConfig.thinking_options` 上游能力声明 |
| `src/relay/advanced_switch.py` | 高级切换（弱→强），先于重写执行 |

---

## 6. 验证清单

```bash
# 1. 嗅探端点
curl http://127.0.0.1:8088/models/api.json   # 应有 anthropic/openai/relay 等 provider

# 2. 重写行为（本地单测）
#    客户端发 reasoningEffort=high → 目标支持: thinking budget 32000
#    客户端发 thinking(budget 20000) → 目标支持: 保留 20000
#    客户端发 thinking → 目标不支持: 剥离
#    客户端没发 → 原样

# 3. 集成
#    重启 8088 后，opencode 模型列表应出现官方模型 + relay(带挡位)
```

---

## 7. 踩过的坑

| 坑 | 原因 | 解决 |
|---|---|---|
| 官方模型全消失 | `OPENCODE_MODELS_URL` 替换整个源，中继只返回 relay | 中继代理官方源 + 注入 relay |
| models.dev 超时 | 国内直连被墙 | 换用 opencode 官方源 `models.opencode.ai` |
| urllib 403 | 默认 UA 被拒 | 加 `User-Agent` 请求头 |
| 旧的 relay-only 缓存 | opencode 磁盘缓存 TTL 5 分钟 | 删除 `~/.cache/opencode/models-*.json`

---

## 8. 各厂商思考挡位真实调研（2026-08-22）

> 本节能证「思考挡位无统一标准」的结论，来自对 5 份厂商资料 + 用户在 opencode 的实测。
> 结论是底层约束（见 `docs/development.md` 后端开发章 `### ⚠ 思考挡位无统一标准`）：
> 中继**必须逐上游声明 `thinking_options`、只透传不并档**。

### 8.1 资料来源

| 来源 | 内容 |
|---|---|
| Infini-AI《DeepSeek 思考参数文档》 | DeepSeek V4 的 thinking/reasoning_effort 取值 |
| LCZ 论坛《Qwen3.8-27B 思考深度档位》 | Qwen sglang 的 reasoning_effort 挡位（该帖已删，结论以 sglang 侧常识 + 用户描述为准） |
| DeepSeek Harness 讨论 #564 | 「思考挡位自动测定 + 方言自动修复」—— 印证无法靠 URL 推断、需实测/探测 |
| Kimi / Moonshot 模型总览 | K3 用 reasoning_effort、K2 用 thinking，同厂不同代方言不同 |
| 智谱 BigModel GLM 思考模式文档 | GLM 用 thinking(enabled/disabled)，无分级挡位，5.3 强制思考 |
| 用户实测（opencode） | hy3-free=4 挡 / Muse Spark=6 挡(含 minimal,xhigh) / MiMo V2.5=none |

### 8.2 逐家真实情况

| 厂商/模型 | 字段 | 真实取值 | 反直觉点 |
|---|---|---|---|
| DeepSeek V4（Infini） | `thinking.type`+`reasoning_effort` | low/high/max（默认 high） | `medium`/`xhigh` 都收但静默并成 high |
| Qwen3.8-27B（sglang） | `reasoning_effort` | off/low/medium/xhigh（xhigh 默认） | **没有 high**；取值是往 system 注入「思考契约」文本，非 token 预算 |
| Kimi K3 | `reasoning_effort` | low/high/max（默认 max） | 一直思考，不能关 |
| Kimi K2.6/K2.7 | `thinking`(enabled/disabled/keep) | 开/关 | **不支持 reasoning_effort** |
| GLM 智谱 | `thinking`(enabled/disabled) | 开/关 | **无分级**；5.3 强制思考关不掉 |
| OpenAI（对照） | `reasoning_effort`/`reasoning.effort` | none/minimal/low/medium/high/xhigh/max | 全集 7 档 |
| Anthropic（对照） | `thinking`(budget)/adaptive | 数字预算/adaptive | 连续值，非离散档 |
| Gemini（对照） | `thinkingConfig` | minimal/low/medium/high 或 数字 | — |
| hy3-free（实测） | — | default/low/medium/high | 4 挡 |
| Muse Spark 1.2 Free（实测） | — | default/low/medium/high/minimal/xhigh | 比 hy3 多 minimal/xhigh |
| MiMo V2.5 Free（实测） | — | none | 直接关思维 |

### 8.3 核心结论

1. **没有标准**，且连「OpenAI 兼容」内部都不统一：字段名、取值集合、语义三层都不同。
2. **最反直觉三点**：Qwen 没有 high、xhigh 才是默认；DeepSeek 把 medium/xhigh 都并成
   high（发 medium 等于发 high，白发）；Kimi 同厂不同代——K3 用 reasoning_effort，K2
   用 thinking。
3. **不能靠 URL 推断**：#564 社区被迫做「挡位自动测定 + 方言自动修复」插件，反向印证
   逐上游声明 `thinking_options` 的路线正确，也说明「自动探测」是真实需求（feature B）。
4. **中继词表曾过窄**：只认 off/low/medium/high/max/enabled，会丢 minimal/xhigh/none；
   且 `_reasoning_options` 只对外广告 4 挡，opencode 看不到 xhigh/minimal——**已在
   v0.11.21 补全覆盖**（见 §2.3 修订注 + `development.md` ⚠ 约束）。

### 8.4 对中继的启示

- A. 扩词表 + 修广告层（**已完成 v0.11.21**）：`_KNOWN_THINKING` 收全 9 值；
  `_reasoning_options` 原样翻 minimal/xhigh/max；operators 按真实上游如实填 thinking_options。
- B. （可选大活）自动探测：建上游时实探它接受哪些挡位，自动填 thinking_options（即 #564 思路）。
- C. 挡位含义由 operators 按真实上游声明，中继只透传/翻译，不替上游做语义判断、不做并档。 |
