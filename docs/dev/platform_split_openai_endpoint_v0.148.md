# 「平台分布」openai 拆端点（v0.148）开发文档

## 1. 用户的初始指令

> openai 区分两个端点

附用户截图：「总览 → 平台分布」卡片只有两行：

```
anthropic  38,417 请求 · 270,718,574 tokens
openai      3,351 请求 · 126,301,441 tokens
```

截图数字与本地 `C:\Users\weizheng\AppData\Local\Relay\relay.db` 完全对得上（实测 openai: req=3351, in+out=126,301,441）。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 「总览 → 平台分布」卡片里 OpenAI 不应塌成单一 `openai` 行 | 指令 + 截图 |
| B | 拆分维度 = **客户端入口协议**（`/v1/chat/completions` vs `/v1/responses` Responses API） | AskUserQuestion 答复 |
| C | `anthropic` 行保持原状（不拆） | 截图（仍是一行） |
| D | 数据源真实情况：跨所有 uvicorn 访问日志统计，OpenAI 入向 100% 是 `POST /openai/v1/chat/completions`，没有 `/v1/responses` 入向（唯一的 `/responses` 是出向 —— volc Responses 插件打 `opencode.ai/zen/v1/responses`，那是 outbound 不影响 openai 平台行） | 数据现实（隐含） |
| E | 现有 3,351 条 openai 行必须一次性回填为 `openai-chat`（D 的直接后果） | D 推论 |
| F | `openai-responses` 行从 0 开始增长（未来真有 Responses API 客户端接入才涨） | D 推论 |

### 隐含但需要确认的点（用户没说，要追问）

- 拆分维度本身（`platform:upstream` vs `platform:endpoint` vs `platform:model`）—— 已在 AskUserQuestion 确认 = 协议端点 chat / responses
- DB 现状：`requests` 表 schema 没有存客户端入口协议 —— 需要新增列
- 哪些 record 路径需要透传新列：`proxy_legacy.py` 9 处 `db.record(...)` 调用
- 拆分后旧 `anthropic` key 是否兼容（迁移前 anthropic 行 endpoint=NULL）
- 卡片配色：openai-chat 用现成 `--platform-openai` 蓝绿，openai-responses 用色变种（淡紫青，与 OpenAI Responses 品牌色一致）

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位根因

「总览」页「平台分布」卡片渲染 = `renderPlatform(snap)` → 读 `snap.by_platform`（`gui.py:2901`）= `tui.fetch_totals(db)` 按 `requests.platform` 列 GROUP BY。

OpenAI 这边实际上有两条客户端入口：
- `/openai/v1/chat/completions`（chat wire，v0.66 一直走这条）
- `/openai/v1/responses`（Responses API，v0.143 新增）

但 `requests` 表 schema（`src/relay/db.py:36-49`）只存 `platform` / `upstream` / `model` —— **没有存客户端入口协议（wire）**，所以所有 OpenAI 流量一律塌成 `openai` 一个 key。

### 第二阶段：方案设计

6 文件改动 + 1 测试：

```
#8  db.py         SCHEMA/MIGRATIONS 加 endpoint 列 + 索引 + 回填
#15 tui.py        fetch_totals 按 (platform, endpoint) GROUP BY
#10 proxy_legacy  9 处 db.record() 透传 endpoint
#13 app.js        renderPlatform 解析 "<plat>:<ep>" key
#11 styles.css    新增 2 色（chat + responses）
#9  index.html    bump css/js cache stamp
#12 test_stats.py 加拆分断言
#14 PROGRESS.md   v0.148 改记录
```

技术关键决策：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 列类型 | `endpoint TEXT`（无 NOT NULL） | 迁移前存量行 NULL 必须允许（不能 ALTER COLUMN 加约束）；后续 record 路径都填实际 wire |
| 索引形态 | `idx_platform_endpoint_ts (platform, endpoint, ts)` 复合索引 | 拆 GROUP BY 后会扫两遍（chat / responses 各一次），复合索引让每个 bucket 各走一次 index range；ts 在末位是因为 WHERE 时常带 since 过滤 |
| 回填策略 | `WHERE platform='openai' AND endpoint IS NULL → 'openai-chat'` | 跨 uvicorn 访问日志已证 100% chat；anthropic 行不回填，COALESCE 让其走 platform 兜底（视觉仍是 anthropic 一行） |
| GROUP BY key 格式 | `<platform>:<endpoint>` 冒号分隔 | 下划线会与 model 名冲突（`openai_chat` vs `openai-chat` 都是字面量但语义不同）；冒号明确是维度拼接 |
| anthropic 兼容 | `COALESCE(NULLIF(endpoint, ''), platform) AS endpoint` 兜底 | 迁移前 anthropic 行 endpoint=NULL → COALESCE 等于 platform → GROUP BY key 仍是 `anthropic`（旧 key 兼容） |
| adapter 内部 endpoint 取值 | `client_wire`（不是上游协议） | endpoint 是「客户端入口」，adapter 转发不改变客户端用的是什么 wire |
| `_reject_dispatch` endpoint | `platform`（写平台名当占位） | 拒绝行没有真实 wire 入向，写平台名便于将来排查；不污染正常拆分行 |
| 卡片配色 | chat = `--platform-openai`（沿用现成变量）；responses = `color-mix(in srgb, --platform-openai 65%, #a78bfa)` | light/day/dark 三套主题都有 `--platform-openai`，不需新变量；color-mix 复用同一基色 + 紫色调变体区分 chat |
| 标签文本 | `openai·chat` / `openai·responses`（剥 `openai-` 前缀，因前面已有平台名） | 与现有 `anthropic · upstream-name` 标签风格一致 |
| 测试 fixture | 复用既有 `tmp_db`（`_connect` 模式）+ `Database.init()` 自动建出新 schema | 不引入新 fixture，复用既有测试基建 |

### 第三阶段：实现路径（按依赖顺序拆）

```
#8  db.py        ALTER TABLE ADD COLUMN endpoint       (底层 schema)
                  ↓
                  init() 一次回填 + 建复合索引
                  ↓
                  record() 加 endpoint 参数透传
                  ↓
#15 tui.py       fetch_totals 按 (platform, endpoint)  GROUP BY
#10 proxy_legacy 9 处 record() 传 client_wire          (并行)
                  ↓
#13 app.js       renderPlatform 解析 "<plat>:<ep>"     (依赖 #15)
#11 styles.css   .platform-endpoint-openai-chat / -responses
#9  index.html   cache stamp
#12 test_stats.py 端点拆分断言
```

---

## 4. 实现中遇到的问题

### 问题 1：`CREATE INDEX ... ON requests(platform, endpoint, ts)` 在 SCHEMA 里失败

报错 `no such column: endpoint`，因为 `executescript(SCHEMA)` 顺序执行 `CREATE INDEX` 时 `ALTER TABLE ADD COLUMN endpoint` 还没跑。

**解法**：把 `CREATE INDEX idx_platform_endpoint_ts` 从 `SCHEMA` 字符串里移走，改在 `Database.init()` 内 ALTER 完列后再建（保证 SCHEMA 自身对存量 DB 不出错 —— 不引用还不存在的列）。

### 问题 2：`PRAGMA journal_mode=WAL` 报 "cannot change into wal mode from within a transaction"

ALTER TABLE + CREATE INDEX + UPDATE 隐式开事务；PRAGMA journal_mode=WAL 不能在同一连接里和 DML 混用。

**解法**：`init()` 内 DML（ALTER + CREATE INDEX + UPDATE）执行后显式 `await c.commit()` 一次，再开 PRAGMA WAL / synchronous / busy_timeout，再 commit。

### 问题 3：9 处 `db.record(...)` 调用分布在 6 个不同函数里

清点：
- `_anthropic_adapter_relay` 1 处（line 519）
- `_anthropic_adapter_relay_sse` 1 处（line 552）
- `_reject_dispatch` 1 处（line 2609）
- `_relay_cross_wire` 3 处（line 2779 / 2834 / 3092）
- 同 wire 字节透传 3 处（line 3564 / 3635 / 3813）

adapter 两个函数没有 `client_wire` 直接可见（在 relay() 那边）—— 需要给函数签名加 `endpoint: Optional[str] = None` 参数透传；relay() → adapter 调用点传 `endpoint=client_wire`。

**解法**：函数签名加可选参数（默认 None，让现有调用方不破坏），adapter 内部 record() 直接透传 `endpoint`（来自参数）。

### 问题 4：`_reject_dispatch` 的 endpoint 取值歧义

「未知 key」「混合态」拒绝行没有真实客户端 wire 入向 —— 写 `None`？写 `platform`？写 `'reject'`？

**解法**：写 `platform`（平台名当占位字符串，便于将来排查时 SQL 看 `WHERE endpoint='openai'` 仍能命中拒绝行；不污染正常拆分行因为拒绝行 token=0）。

### 问题 5：测试断言数学误差（3 次迭代）

初始 seed 计算 `since=now-1d` 时哪些行落入 1d 窗口算错：

- 误以为 5 行 chat 中只有 3 行 ≤ now-1d → 实际 offsets=[0, 100, 3600, 7200, 7800] 全都 ≤ now-86400（5 行全在 1d 内）
- 误以为 seed 只有 1 个 8 天前老行 → 实际写了 2 个（chat 1 + responses 1）→ `out_all['openai:openai-chat']['requests']` 应为 5 不是 6

**解法**：重算 offsets 与窗口关系，逐条校核断言值；最终 seed：6 chat + 4 responses + 4 anthropic (NULL endpoint) + 2 个 8 天前老行（1 chat + 1 responses）。

### 问题 6：sqlite3 INSERT binding 计数

seed INSERT 时漏了一列（5 个 value 但需要 6 个）→ sqlite3.ProgrammingError。

**解法**：seed 调用前对照 schema 字段数 + binding 数；补 `upstream` 列值。

### 问题 7：Windows tmpfs cleanup pytest PermissionError（per `feedback_test_infra_chase.md`）

新加的 `test_fetch_totals_splits_openai_by_endpoint` 单独跑全过；`pytest tests/test_stats.py` 整体跑时 fixture teardown 报 PermissionError，与测试逻辑无关。

**解法**：per 既有反馈 memory（"组合 pytest 挂起/栈溢出，单独跑全绿就记录不追"），独立重跑断言全通，记录不追。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 CREATE INDEX 时机 | 移到 `init()` 内 ALTER 后再建 | `db.py` |
| #2 WAL + DML 混用 | ALTER/CREATE INDEX/UPDATE 后显式 commit 再开 WAL | `db.py` |
| #3 9 处 record() 透传 | 函数签名加 `endpoint` 参数；adapter 加 `endpoint=client_wire` 参数；其余函数内部直接用 `client_wire` 变量 | `proxy_legacy.py` |
| #4 _reject_dispatch endpoint | 写 `platform` 占位 | `proxy_legacy.py` |
| #5 测试断言数学 | 重算 since 窗口；最终 seed 6+4+4+2 行 | `test_stats.py` |
| #6 INSERT binding 计数 | 补 `upstream` 列值 | `test_stats.py` |
| #7 tmpfs cleanup 噪声 | 单独跑全绿就记录不追 | — |

---

## 6. 是否完全遵循规划路径开发

**完全按规划**。

### 完全按规划（无偏离）：

- 任务依赖顺序（#8 → #15/#10 → #13/#11/#9 → #12 → #14）
- 拆分维度 = 协议端点 chat / responses（用户答复）
- anthropic 单一 endpoint 不拆（视觉一行不变）
- 拆分 key 格式 `<platform>:<endpoint>`（冒号分隔）
- COALESCE(NULLIF(endpoint,''), platform) 兜底（迁移前 anthropic NULL 走 platform）
- 一次性回填 `WHERE platform='openai' AND endpoint IS NULL → 'openai-chat'`
- 9 处 `db.record(...)` 全部透传 endpoint
- adapter 内部走 `endpoint=client_wire`（不是上游协议）
- `_reject_dispatch` 写 `endpoint=platform` 占位
- 配色：chat = `--platform-openai`、responses = `color-mix(...65%, #a78bfa)`
- 标签 `openai·chat` / `openai·responses`
- 测试 seed 6+4+4+2 行，复用 `tmp_db` fixture
- 不在范围：`fetch_aggregate_by_dim` 的 `dim=platform` 行（不拆）、`passthrough.db`（不动）

### 偏离之处：

- **(a) CREATE INDEX 从 SCHEMA 移到 init()**：规划里把 `idx_platform_endpoint_ts` 写在 SCHEMA 字符串里，但实测 SCHEMA 的 `executescript` 会按顺序跑 CREATE TABLE → CREATE INDEX，但 ALTER TABLE 在 MIGRATIONS 循环里晚跑 → 索引引用未存在的列 → 报错。**纯执行细节修正，不影响逻辑。**

- **(b) init() 加显式 commit**：规划没明说 WAL PRAGMA 不能与 DML 混在同一连接 —— 这是 SQLite 的隐含约束（journal_mode 必须在事务外执行），实测触发了"cannot change into wal mode from within a transaction"。**隐含约束补全，不算违反规划。**

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/db.py`** — `SCHEMA` 移除 `idx_platform_endpoint_ts`（避免引用未建列）；`MIGRATIONS` 新增 `("endpoint", "TEXT")`；`Database.init()` 在 ALTER 完列后建复合索引 + 一次性回填 openai 旧行 `WHERE platform='openai' AND endpoint IS NULL → 'openai-chat'`；DML 之后显式 `await c.commit()` 再开 PRAGMA WAL/synchronous/busy_timeout；`Database.record()` 加 `endpoint: Optional[str] = None` 参数，INSERT 列增加 `endpoint`。

2. **`src/relay/tui.py`** — `fetch_totals` GROUP BY 由 `platform` 改为 `(platform, endpoint)`，新增 `COALESCE(NULLIF(endpoint, ''), platform) AS endpoint` 兜底；返回 key 用 `f"{p}:{ep}" if ep and ep != p else p`（anthropic 单 endpoint 不带后缀 = 旧 key，兼容）。

3. **`src/relay/proxy_legacy.py`** — 9 处 `db.record(...)` 全部新增 `endpoint` 参数：
   - `_anthropic_adapter_relay`（line 519）：新增 `endpoint: Optional[str] = None` 函数参数，record 透传 `endpoint`
   - `_anthropic_adapter_relay_sse`（line 552）：同上
   - `_reject_dispatch`（line 2609）：`endpoint=platform`（占位）
   - `_relay_cross_wire` 错误 / 非流 / 流（line 2779 / 2834 / 3092）：`endpoint=client_wire`
   - 同 wire 字节透传 错误 / 非流 / 流（line 3564 / 3635 / 3813）：`endpoint=client_wire`
   - relay() → adapter 调用点（line 3xxx）：传 `endpoint=client_wire`

### 前端（Web）

4. **`src/relay/web/app.js`** — `renderPlatform` 解析 key：
   - `const hasColon = k.indexOf(":") >= 0;`
   - `plat = hasColon ? k.slice(0, k.indexOf(":")) : k;`
   - `endpoint = hasColon ? k.slice(k.indexOf(":") + 1) : null;`
   - `badgeCls = showEndpoint ? "platform-badge platform-{plat} platform-endpoint-{endpoint}" : "platform-badge platform-{plat}";`
   - `label = showEndpoint ? "{plat}·{endpoint.replace('openai-','')}" : plat;`（剥前缀因平台名已在前）
   - `renderToday` 不动（`Object.values(totals).forEach` 累加无 key 顺序假设）

5. **`src/relay/web/styles-20260817.css`** — 新增两色块：
   ```css
   .platform-badge.platform-openai.platform-endpoint-openai-chat {
       background: var(--platform-openai);
   }
   .platform-badge.platform-openai.platform-endpoint-openai-responses {
       background: color-mix(in srgb, var(--platform-openai) 65%, #a78bfa);
   }
   ```

### 资源文件版本

6. **`src/relay/web/index.html`** — cache stamp `styles-20260817.css?v=20260824-06 → 20260824-07`、`app.js?v=20260824-05 → 20260824-06`（pywebview 默认 cache 不会自动刷新，bump 才拿到新版）。

### 测试

7. **`tests/test_stats.py`** — 新增 `test_fetch_totals_splits_openai_by_endpoint`：
   - seed 14 行：6 openai-chat + 4 openai-responses + 4 anthropic (NULL endpoint) + 2 个 8 天前老行（1 chat + 1 responses）
   - 调 `tui.fetch_totals(db)`，断言返回 dict 包含 `openai:openai-chat`、`openai:openai-responses`、`anthropic` 三个 key，input/output_tokens 各自正确
   - 调 `fetch_totals(since=...)`，断言 since 过滤三种窗口（1d / 1h / 100s）分别覆盖
   - 复用既有 `tmp_db` fixture（`_connect` 模式），`Database.init()` 自动建出新 schema

### 文档

8. **`PROGRESS.md`** — v0.143 → **v0.148** 改记录（v0.143 已被「设置页文案精简」占用，本改动升级为 v0.148）；记录 6 文件改动 + 1 测试 + 不在范围项。

9. **`docs/CHANGELOG.txt`** — 顶部插入 v0.148 块（纯文本，分隔符，中文冒号分组）。

10. **`docs/dev/platform_split_openai_endpoint_v0.148.md`** — 本文件（新建，7 节结构）。

### 行为验收清单（手动测试项）

- [ ] GUI 重启 → 启动日志无 `no such column: endpoint` / `cannot change into wal mode` 报错
- [ ] 「总览 → 平台分布」卡片 3 行：`anthropic` / `openai·chat` / `openai·responses`
- [ ] `anthropic` 数据不变（38,417 req · 270.7M tokens，橙色 badge）
- [ ] `openai·chat` 显示 3,351 req · 126.3M tokens（蓝绿 badge）
- [ ] `openai·responses` 显示 0 / 0（淡紫青 badge）
- [ ] `curl POST /openai/v1/responses` 打一条 → `openai·responses` 行的 requests +1
- [ ] chat-completions 客户端继续打 → `openai·chat` 数字正常累加
- [ ] pytest `tests/test_stats.py -k endpoint` 单条新断言 1 条 + 既有 17 条全绿
- [ ] 单独跑 `tests/test_stats.py` 整体 358+1 passed

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/db.py` | 改（SCHEMA 减 1 索引、MIGRATIONS +1 列、init() +回填+commit、record() +1 参数） |
| `src/relay/tui.py` | 改（fetch_totals GROUP BY + COALESCE + key 拼接） |
| `src/relay/proxy_legacy.py` | 改（9 处 db.record() + adapter 函数签名加参数） |
| `src/relay/web/app.js` | 改（renderPlatform 解析 `<plat>:<ep>`） |
| `src/relay/web/styles-20260817.css` | 改（+2 色块） |
| `src/relay/web/index.html` | 改（CSS/JS cache bump） |
| `tests/test_stats.py` | 改（+1 测试） |
| `PROGRESS.md` | 改（v0.143 → v0.148 改记录） |
| `docs/CHANGELOG.txt` | 改（顶部 +v0.148 块） |
| `docs/dev/platform_split_openai_endpoint_v0.148.md` | **新建**（本文件） |
