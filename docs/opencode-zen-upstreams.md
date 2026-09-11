# OpenCode Zen 官方上游端点参考

> 来源：`Desktop/opencode-dev/packages/console/app/src/routes/zen/*` 路由清单 +
> `function/src/log-processor.ts` 的端点白名单（2026-08-17 确认）。
> 用途：给中继 `upstreams.json` 配置 opencode 上游时查端点，以及判断欠费/不可用的原因。
> 更新：2026-08-19 —— 免费模型经中继实测 200 可用；补排查实录（Claude Code 报余额不足根因 = 中继内存残留已删上游）。

## 1. 一句话总结

opencode 官方上游 = **一个域名 `https://opencode.ai` + 两个版本段（按量 `/zen/v1`、
Go 订阅 `/zen/go/v1`）+ 每段三种协议**。同一把 `sk-` key 在两端段的余额/套餐**独立**。

```
https://opencode.ai/zen/v1/...      按量付费（Zen 账户余额，先充值后扣费）
https://opencode.ai/zen/go/v1/...   Go 订阅（月费套餐，按周/月限额）
```

## 2. 端点清单（源码权威）

两段各支持以下路径（`log-processor.ts:13-20` 白名单 + `routes/zen/*/v1/*` 路由文件）：

| 协议 | 按量 | Go 订阅 | 请求体风格 |
|---|---|---|---|
| Anthropic | `/zen/v1/messages` | `/zen/go/v1/messages` | anthropic-version 头 + x-api-key |
| OpenAI Chat | `/zen/v1/chat/completions` | `/zen/go/v1/chat/completions` | Authorization: Bearer |
| OpenAI Responses | `/zen/v1/responses` | `/zen/go/v1/responses` | Authorization: Bearer |
| 模型列表 | `/zen/v1/models` `/zen/v1/models/{id}` | `/zen/go/v1/models` | options/模型元数据 |

注意：
- **`/zen/v1/chat/completions` 才是 OpenAI 协议**；不要拼 `/zen/go/v1/chat/completions`
  加 `/messages` 之类的组合（各端点路由是分开的文件）。
- Go 段也有 `chat/completions`（`routes/zen/go/v1/chat/completions.ts`），所以
  深水 DeepSeek-Pro 那条配 `/zen/go/v1/chat/completions` 是合法的 OpenAI 协议端点。
- 鉴权头统一 `Authorization: Bearer sk-...`（OpenAI 协议）或 `x-api-key: sk-...`
  （Anthropic 协议）。

## 3. 计费语义（为什么同一把 key 有的 401 有的 200）

来源 `routes/zen/util/handler.ts`：

- **按量 `/zen/v1`**：扣 **Zen 账户余额**（`billing.balance <= 0` → `CreditsError
  "Insufficient balance"`，401）。无支付方式 → `noPaymentMethod`。
- **Go 订阅 `/zen/go/v1`**：走 **Go 套餐**，不看账户余额，看套餐**周/月/滚动限额**
  （`goSubscriptionWeekly/Monthly/RollingLimitExceeded`）。套餐内模型不另扣费。
- 所以 `sk-PKFo...`（Go 套餐）在 `/zen/go/v1` 可用、在 `/zen/v1` 却 401 欠费——
  **因为按量余额是 0**，不是 key 失效。
- 地区限制：`zen.api.error.regionNotAllowed`（handler.ts:146）——与余额无关的
  独立错误，注意区分。

## 3.1 免费模型（`-free` 后缀）（2026-08-17 初测 / 2026-08-19 补实）

按量段 `/zen/v1` 有一批 `-free` 后缀的免费模型（实测 6 个：
`opc-deepseek-v4-flash-free` / `opc-hy3-free` / `opc-laguna-s-2.1-free` /
`opc-mimo-v2.5-free` / `opc-nemotron-3-ultra-free` / `opc-nemotron-3.5-lightning-free`；
源码 full 列表里还有 kimi-k2.5-free / glm-4.7-free / minimax-m3-free 等共 31 个）。

- **不需要余额**、不扣费（handler.ts:837 `validateBilling`：`allowAnonymous` → 直接 return
  `"free"`，短路跳过余额检查）。请求正确转发到 `https://opencode.ai/zen/v1/chat/completions`，
  超限时返回 **429 `FreeUsageLimitError`**。
- 429 与 401 含义不同：**401 CreditsError = 按量余额不足**（充值解决）；
  **429 FreeUsageLimitError = 免费档额度用尽**（等窗口滚动重置，充值不解决）。
- **关键判定（源码 handler.ts:967-1008 `validateBilling`）**：只有 `-free / allowAnonymous`
  模型才绕过余额检查；`model` 字段写**不带 -free** 的模型名（如 `deepseek-v4-flash`、
  或透传进来的 `claude-sonnet-5`）会被当**付费模型**验余额 → `balance <= 0` → **401 余额不足**。
  这是"给 Claude Code 用一键改错模型名就报余额不足"的直接原因。
- **IP vs key 限流（2026-08-19 实测）**：免费模型一律按 **IP** 限流（handler.ts:126-128
  `allowAnonymous ? createIpRateLimiter : createKeyRateLimiter`，与是否带 key 无关）。
  裸连（不经过中继）时本机出口 IP 已触发 429；**经中继转发 200 可用**——中继透传
  请求头后 Zen 走的限流路径不同（key 已识别 → key 限流每分钟 1000 次），故带 key 走
  中继"用不完"。排查时裸连会误伤（IP 429），应带 key 经中继验证。

## 4. 当前配置对照（upstreams.json，2026-08-19）

| 上游名 | url | 走套餐 | 实测 | 结论 |
|---|---|---|---|---|
| `claude` | `https://opencode.ai/zen`（+`/v1/messages`） | 按量 | — | 保留；按量欠费会稳定 401 |
| `opc-*-free`（6 条） | `https://opencode.ai/zen/v1`（+`/chat/completions`） | 免费档 | **200 出字** | 经中继可用（免费模型，见 3.1） |
| `opencode-go` | `https://opencode.ai/zen/go/v1` | Go 订阅 | — | **已删除（2026-08-19）**：用户无 Go 订阅，同 key 抢走免费请求导致余额不足 |
| `deepseek官方` / `deepseek官方-openai` | `api.deepseek.com` | 官方 | 200 | 对照组 |

## 5. 配置建议

- 想用 opencode 免费/订阅额度：**只配 Go 段**（`/zen/go/v1`）两个协议各一条。
- 按量段（`/zen/v1`）要等 Zen 账户充值时才能用；充值前会稳定 401。
- 免费模型**只在这条路上能用**：`url=/zen/v1` + 模型名带 `-free` + 允许列表也放
  `-free` 模型。模型名漏 `-free` → 按付费计费 → 401 余额不足。
- 新增模型时先查本表判断该走哪段；`/zen/v1` 401 先查余额，别改 key。
- **GUI 快捷方式（v0.98.4 更新版）**：「新建上游」弹窗顶部"预设配置"下拉：
  - `OpenCode Zen 免费模型` → 自动填 url/协议/鉴权/计费 + **模型下拉**（选 `*-free`
    模型后自动填名称 `opc-<模型名>` + 允许列表），用户只剩 API Key 要填。
  - `OpenCode Go 订阅` → 标注**付费 $5/月**，未订阅会报"余额不足"。
  - `OpenCode Zen 按量付费` → 需 Zen 账户余额。
  - 手动改协议或鉴权会自动清除预设回到自定义。
- **多上游共用同一把 key 的坑（2026-08-19 教训）**：dispatch 按 key 命中**多个**上游时
  取**列表第一个**（WARNING `matches N upstreams; using the first`）。免费上游与其它
  同 key 上游并存时，**顺序决定命中谁**——建议免费上游用独立 key，或保证
  `opc-*-free` 排在列表最前。GUI 删除上游只改配置，**不 reload 运行中的中继**，
  改完配置需重启中继（见 7）。

## 6. 中继侧配合

- opencode 桌面版 OpenAI 入口发 `/openai/chat/completions`（**不带 /v1**），
  中继 `_normalize_api_path` 会自动补 `/v1`（见 PROGRESS v0.12.2）。
- Go 段 Anthropic 上游应开 `requires_anthropic_adapter`（opencode-go 已开）：
  adapter 负责 x-api-key 头 + 模型名小写化 + 剥 thinking。

## 7. 排查实录：Claude Code 用 Zen 免费模型报"余额不足"（2026-08-19）

**现象**：opencode 里配 sk key 用 `deepseek-v4-flash` 免费额度"永远用不完"（当前窗口
就是免费模型）；但中继里"opencode zen 免费模型"预设建的上游给 Claude Code 用，
一发就 **401 余额不足**。

**根因**：不是模型名错、不是 key 错，是**运行中继的内存里残留了已删除的 `opencode-go`
上游**（`zen/go/v1` Go 订阅端点）。时间线：
1. 17:52 中继启动，加载的 `upstreams.json` 还含 `opencode-go`；
2. 17:53 GUI 里删掉 `opencode-go`，磁盘文件已正确；
3. **但中继是独立进程，GUI 删除只写文件 + 重载 GUI 自己的 settings，不通知中继** →
   中继内存仍把 `opencode-go` 放在 `upstreams` 列表第一位；
4. 用户的 sk key 同时命中 `opencode-go` + 6 条 `opc-*` → dispatch 取**第一个**
   `opencode-go` → 请求发到 `zen/go/v1/messages`（Go 订阅端点、付费模型）→
   用户没有 Go 订阅 → `CreditsError 401 余额不足`。

**证据**：中继日志
`dispatch: key sk-PKF… matches 7 upstreams (opencode-go, opc-*…); using the first`
+ `UPSTREAM REQ POST https://opencode.ai/zen/go/v1/messages`。裸连 zen 对照：
`-free` 模型 → 429（免费限流）；`deepseek-v4-flash` / `claude-sonnet-5` → 401（余额不足）。

**修复与验证**：
1. 重启中继（PID 27348）让内存配置对齐磁盘（opencode-go 已不在列表）；
2. 真实请求 `/anthropic` 走 `opc-deepseek-v4-flash-free`，`deepseek-v4-flash-free` /
   `deepseek-v4-flash` / `claude-sonnet-5` 三种模型名**全部 200**（中继强映射到
   `-free` 模型重写后发给免费端点），不再 401；
3. GUI 预设升级（v0.98.4）防止再次踩坑：`opencode-zen-free` 预设带模型下拉自动填
   `-free` 模型，`opencode-go` 预设标注付费。

**通用教训**：中继 GUI 改配置后若"行为没变"，先怀疑**中继进程没 reload 配置**——
重启中继是最快的验证手段。
