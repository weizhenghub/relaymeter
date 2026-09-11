# 平台识别器 + 平台流量展示（v0.155）开发文档

> v0.155 是「平台识别器」特性：新增 `agent` 维度，从每个请求的 User-Agent 识别它来自哪个客户端工具（Claude Code / OpenCode / Codex / OpenClaw …），认不出的存「未知」；总览新增「平台流量」卡片、统计页新增「30天按平台流量」块；识别结果可被用户重命名。按 docs/dev/ 规范写 7 节总账。

---

## 1. 用户的初始指令

按对话顺序 4 段指令 + 1 次范围确认：

> **1）有个问题啊，要是真的需要区分不同数据包来自哪个平台，是能够做到的吗？难度高吗**
>
> （场景：想知道哪些流量来自哪个 AI 工具，而不是只分协议入口）

> **2）是区分来到中继的数据包是 claude code 的，还是 codex，还是 openclaw这种**

> **3）你的意思是，只能通过不同平台走不同协议来计量，实际上请求体本身并不会说它来自哪个平台，如果两个平台走一个协议（比如openai chat），就没法区分**
>
> （场景：用户在确认「请求体不含身份」这个前提，理解为何需要额外的识别机制）

> **4）可以在中继中加一个平台识别器，识别不同的请求来自哪个平台**

> **5）【AskUserQuestion 范围确认】** 在总览页新建一个卡片「平台流量」，在统计中新增一个「30天按平台流量」。其它原有的统计和计量逻辑保留。认不出的存「未知」，允许用户重命名。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 平台识别器：从请求识别客户端工具名（claude-code / opencode / codex / openclaw …） | 指令 1/2/4 |
| B | **`agent` 是独立于 `platform` 的新维度** —— `platform` = 入口路径（anthropic/openai），`agent` = 客户端工具名，两字段都落库互不影响 | 指令 3（用户点破：同协议不同工具无法靠路径区分）+ 设计决策 |
| C | 总览页新增「平台流量」卡片 | 指令 5 |
| D | 统计页新增「30天按平台流量」块 | 指令 5 |
| E | 认不出的统一存字面量「未知」，可整体重命名 | 指令 5 |
| F | 识别结果可被用户逐项重命名（「未知」也能改） | 指令 5 |
| G | 其它原有统计和计量逻辑保留（不动现有 `platform` 字段语义） | 指令 5 |

### 隐含但需要确认的点（分析阶段确认）

- **识别信号选型**：请求体 vs User-Agent。请求体只声明协议（model / messages），**不声明工具身份**；User-Agent 头是每个 SDK 都会自报的身份。→ 选 UA。⚠ 这是本特性立论的根基：UA 是唯一协议无关的身份信号。
- **别名/重命名的存储归属**：GUI 与中继子进程各持一份 Settings（双副本陷阱）。若别名由中继进程消费，改名后两进程不一致会互相覆盖。→ 纯展示层：原始 agent 名落库，`agent_aliases` 映射只在前端渲染时应用，后端聚合永远返回原始名。⚠ 别名是「展示层映射」，不是「数据改写」。
- 「未知」桶的形态：独立常量（`AGENT_UNKNOWN = "未知"`）而非空串，空串/NULL 聚合时 COALESCE 归入。
- 版本归一：同工具不同版本（claude-cli 2.8.4 vs 999.0.0-restored）归同一 agent（`claude-code`）。

---

## 3. 分析需求后得出的开发路径

### 阶段一：识别信号实测验证

从 `relay_trace.log` 抽真实 UA 分布（52,319 条）：

| 实际 UA | 频率 | 归为 |
|---|---|---|
| `claude-cli/999.0.0-restored (undefined, cli)` | 29,895 | claude-code |
| `Bun/1.3.14` | 13,794 | **未知**（运行时默认 UA） |
| `python-httpx/0.28.1` | 4,541 | 未知 |
| `opencode/1.18.18 ai-sdk/provider-utils/4.0.23 …` | 2,988 | opencode |
| `claude-cli/2.8.4 (undefined, cli)` | 167 | claude-code |
| `Python-urllib/3.9` | 133 | 未知 |
| `curl/8.21.0` | 89 | 未知 |
| `ai-sdk/anthropic/4.0.39 ai-sdk/provider-utils/5.0.27 …` | 1 | ai-sdk |

识别率 ≈ 64%（claude-code 58.2% + opencode 5.8% + ai-sdk ~0%），36% 未知，其中最大盲区是 `Bun/1.3.14`（26.7%）—— Bun 运行时 fetch 的默认 UA，凡 Bun 写的工具不显式设 UA 就长这样，中继拿不到工具名。

### 阶段二：技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 识别算法 | 小写子串匹配（`_RULES` 有序表，首个命中生效） | 简单、零依赖、可穷举测试；UA 结构稳定无需正则 |
| 规则优先级 | `claude-code` 前的 `opencode` 必须先于 `ai-sdk/` | OpenCode 的 UA 同时含 `ai-sdk` 字样，顺序错会误归 |
| 落库 | `requests.agent` 列（`MIGRATIONS` 追加 `("agent","TEXT")`） | 旧库自动 `ALTER TABLE ADD COLUMN`，旧行 NULL |
| 未知兜底 | `AGENT_UNKNOWN = "未知"` 字面量 + 聚合 `COALESCE(NULLIF(agent,''), '未知')` | NULL（迁移前）/ 空串 / 未识别统一一个桶，可整体改名 |
| 别名持久化 | `upstreams.json` 顶层 `agent_aliases` dict | 仿 `quick_switch` 三套顶层配置模式，`apply_to_settings` 解析 |
| 别名应用点 | 仅前端渲染（`agentDisplayName`），后端聚合返原始名 | 避开 GUI/中继双 Settings 副本陷阱；改名后旧数据自动按新名显示 |
| 统计块数据 | 固定 `statsAggregate("agent","30d",0,"relay")` + 30s TTL | 与 dim/range 选择器解耦（agent 只存在于转换库），TTL 防 500ms tick 反复打桥 |

#### agent 字段的完整语义（三态）

`requests.agent` 列有三种取值来源，**聚合前必须统一口径**：

| 场景 | 存储值 | 聚合结果 |
|---|---|---|
| 迁移前的旧行（无 agent 列） | NULL | `COALESCE` → 「未知」 |
| 迁移后 UA 未识别（Bun / curl / python-httpx …） | 「未知」字面量 | 「未知」 |
| 识别成功 | 规范名（claude-code / opencode / codex …） | 原名（可被别名映射成显示名） |
| 异常空串 | `''` | `NULLIF('')`→NULL → `COALESCE` → 「未知」 |

**设计意图**：NULL 与「未知」在聚合层**故意合并成一个桶**——旧行和未识别流量对用户没有区别（都是"不知道是哪个工具"），分开反而制造两个无法区分的杂桶。桶的显示名可整体改（设置页把「未知」改成「自研客户端」即整体改名，不丢归类）。

### 阶段三：任务依赖顺序（后端 → 前端 → 测试）

```
#14 agent.py: 识别器 sniff_agent              (无依赖，纯函数)
#10 db.py: agent 列 + record() 参数            (依赖 #14 语义，无代码依赖)
#11 proxy_legacy.py: 入口识别 + 线程穿透         (依赖 #10)
#12 upstreams_file.py + config.py: 别名持久化
#15 tui.py + stats.py + gui.py: 聚合 + 快照 + 桥方法
#13 app.js + index.html + css: 卡片/统计块/设置别名
#16 tests: test_agent_sniff.py + 回归
```

---

## 4. 实现中遇到的问题

### 问题 1：`proxy_legacy.py` 的 `_InFlight` 注释记错导致 Edit 落空

给 `_InFlight` dataclass 加 `agent` 字段时，old_string 里把注释「衔接卡死」记成「衔接卡停」，Edit 两次失败。

**解法**：Read 实际行 1050-1053 取原文，精确匹配后一次成功。

### 问题 2：`gui.py` 的 `return {"ok": True, "path": msg}` 六处重名

`save_agent_aliases` 桥方法插入点定位时，锚字符串匹配 6 处，Edit 报 ambiguous。

**解法**：把插入锚点扩到含 `get_relay_settings` def 的上下文，唯一定位到 `save_quick_switch` 之后。

### 问题 3：`proxy_legacy.py` 两个相同的上游失败 record 块

上游错误路径两处 record 代码块字面相同（`upstream_error: {exc}`），Edit 无法区分。

**解法**：用 `error=f"upstream_error: {exc}"` vs `error=None if status < 400` 区分两块。

### 问题 4：Windows pytest 的 tmp_path 清理 PermissionError

每次 pytest 会话收尾，`cleanup_dead_symlinks` 报 `WinError 5` 拒绝访问 `pytest-current`，exit 被污染（测试本身全过）。

**解法**：按 memory「测试基建问题不要过度追查」—— 不追，单跑/分批跑用 `--basetemp` 绕过，只 grep 进度行的 PASSED/FAILED 读结果。

### 问题 5：特性让既有测试 `test_aggregate_invalid_dim` 失效

我把 `agent` 加进 dim 白名单后，该测试断言 `valid == {platform, upstream, model}` 不再成立。

**解法**：断言集合补 `agent`（特性使然，非回归）。

### 问题 6：`test_fetch_totals_splits_openai_by_endpoint` 既有 `NameError`

第 328 行 `assert rows["(无)"]["requests"] == 2`，`rows` 未定义（复制粘贴残留）。**与本次无关**，属存量坏测试。

**解法**：不动它，向用户明示存在；用户确认后再清理。

### 问题 7：真实流量 36% 未知 + 历史不可回溯

实测 Bun/1.3.14 占 26.7% 识别不出；且 DB 只存归类结果不存原始 UA，规则升级后存量行无法重分类。

**解法**：本次按用户范围交付（认不出归「未知」可整体改名）；原始 UA 落库列为后续可选项，已在文档明示。

### 问题 8：为什么 agent 必须独立成列，不并入 platform？

v0.148 刚把 platform 拆成 `(platform, endpoint)` 并让「平台分布」卡固定 3 行。若把 agent 塞进 platform 维度：①「平台分布」卡会再炸回 5+ 行（每个工具一行，飘忽）；② `fetch_aggregate_by_dim(dim=platform)` 的 top-N 随 agent 数抖动；③ 语义混淆——platform 是"走哪个入口协议"，agent 是"谁发的请求"，两个正交问题。

**解法**：独立 `agent` 列 + 独立聚合函数，两维度各自展示。既有 platform 逻辑一行不动。

### 问题 9：为什么别名是展示层映射，不直接改写 agent 列？

备选方案是把 agent 列的值直接替换成用户显示名。否决理由三条：
① **双 Settings 副本陷阱**：GUI 与中继子进程各持一份 Settings，若别名由中继进程消费，改名后两进程不一致会互相覆盖（v0.11.18 踩过同类坑）。
② **不可逆数据改写**：agent 列是原始归类证据，改名写回后原始值丢失，用户反悔无法恢复。
③ **旧数据跟不上**：展示层映射天然做到"改名后历史数据自动按新名显示"；若写回，改名前的行仍是旧名、改名后的行是新名，聚合被撕成两半。

**解法**：原始名落库，`agent_aliases`（原始名→显示名）只在前端渲染时应用。后端聚合永远返回原始名。

### 问题 10：为什么统计块固定 relay 库 + 30s TTL？

agent 维度只存在于转换库（relay.db）——透传库（passthrough.db）按 `url@@key` 指纹分桶，没有 agent 概念。若统计块跟 dim/range/模式选择器联动：① 切到"仅透传"档时无数据可拉，块空闪；② 500ms poll tick 每次进统计页都拉一次桥，无谓打后端。

**解法**：固定 `dim=agent, range=30d, mode=relay` + 30s TTL 缓存。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 _InFlight 注释错 | Read 原文后精确匹配 | proxy_legacy.py |
| #2 桥方法插入 ambiguous | 扩锚点上下文唯一化 | gui.py |
| #3 record 块重名 | 用 error 值区分两块 | proxy_legacy.py |
| #4 pytest tmp 清理炸 | --basetemp 绕过 + 只看进度行 | 测试运行姿势 |
| #5 invalid_dim 断言 | valid 集合补 agent | tests/test_stats.py |
| #6 既有 NameError | 不动，明示用户 | tests/test_stats.py |
| #7 36% 未知 + 不可回溯 | 交付范围内能力；UA 落库列为后续 | agent.py / db.py |
| #8 agent 并入 platform 会炸 3 行结构 | 独立 agent 列 + 独立聚合函数 | db.py / tui.py |
| #9 别名写回 agent 列（双副本 + 不可逆 + 撕历史） | 纯展示层映射，原始名落库 | config.py / upstreams_file.py / app.js |
| #10 统计块跟档位联动会空闪 + 反复打桥 | 固定 relay 库 + 30s TTL | app.js |

---

## 6. 是否完全遵循规划路径开发

**基本遵循，两处偏离（均为细节，非规划违背）。**

### 完全按规划：

- agent 独立于 platform 的新维度，两字段都落库
- 别名纯展示层（原始名落库，前端渲染时映射），避开双 Settings 副本陷阱
- 认不出的统一存 `AGENT_UNKNOWN = "未知"`，可整体重命名
- 总览「平台流量」卡 + 统计「30天按平台流量」块
- 其它统计/计量逻辑不动（`platform` 字段语义、quota 口径全保留）
- 规则顺序 = 优先级（opencode 先于 ai-sdk）
- 测试覆盖 sniff 纯函数 + 落库 + 两种聚合

### 偏离之处：

- **(a) 计划写「版本戳 ?v=20260824-23 → 24 (app.js + styles)」，实际 `?v=` 只存在于 index.html 一处**：app.js 与 styles 的加载 URL 都由 index.html 引用，bump 一次 index.html 即全覆盖（v0.148 也是「index.html 同步 bump」的同一约定）。**文档表述修正，行为无差异。**
- **(b) 计划没写「统计块固定 relay 库 + 30s TTL」**：实现时为避免 500ms poll tick 反复打桥、并让 agent 块不被 dim/range 选择器干扰，加了两条实现约束。**纯实现细节补充。**

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/agent.py`**（**新文件 44 行**）
   - `AGENT_UNKNOWN = "未知"`（认不出的统一字面量）
   - `_RULES: list[tuple[str, tuple[str, ...]]]` 有序子串表：
     `claude-code ← ("claude-cli","claude code")` / `opencode ← ("opencode",)` / `codex ← ("codex",)` / `openclaw ← ("openclaw",)` / `ai-sdk ← ("ai-sdk/",)`
   - `sniff_agent(user_agent) -> str`：空 UA → 未知；lower 后按表顺序首个命中生效；全不命中 → 未知
   - 核心代码：
     ```python
     AGENT_UNKNOWN = "未知"

     _RULES = [
         ("claude-code", ("claude-cli", "claude code")),
         ("opencode",    ("opencode",)),
         ("codex",       ("codex",)),
         ("openclaw",    ("openclaw",)),
         ("ai-sdk",      ("ai-sdk/",)),
     ]

     def sniff_agent(user_agent):
         if not user_agent:
             return AGENT_UNKNOWN
         ua = user_agent.lower()
         for key, needles in _RULES:
             if any(n in ua for n in needles):
                 return key
         return AGENT_UNKNOWN
     ```
   - 规则是「canonical key + 小写子串集合」，**顺序即优先级**（首个命中生效）；新增工具 = 在 `_RULES` 插一条
   - ⚠ 改规则后**存量数据不自动重分类**（DB 只存归类结果，见盲区说明）

2. **`src/relay/db.py`**
   - `SCHEMA` requests 建表加 `agent TEXT`（放 `endpoint` 之后）
   - `MIGRATIONS` 追加 `("agent", "TEXT")`（旧库自动 `ALTER TABLE ADD COLUMN`，旧行 NULL 预期）
   - `record()` 加 `agent: Optional[str] = None` 关键字参数；INSERT 列清单 + VALUES 占位加 `agent`；`info` dict + `emit_event` 带上 `agent`

3. **`src/relay/proxy_legacy.py`**（入口识别 + 全链透传）
   - `from .agent import sniff_agent`
   - `relay()` 入口：`agent = sniff_agent(request.headers.get("user-agent"))`
   - `_InFlight` dataclass 加 `agent: str = ""` 字段；`_register_inflight()` 加 `agent` 参数
   - 透传参数：`_anthropic_adapter_relay()` / `_reject_dispatch()` / `_relay_cross_wire()` 各加 `agent: str = ""` 参数，内部所有 `db.record(...)` 补 `agent=agent`
   - **10 处 record 调用点全部带 `agent=agent`**（主路径 3 + 分支路径 7）

4. **`src/relay/tui.py`**
   - 新增 `fetch_agent_totals(db_path, since=None)` —— GROUP BY 口径与 `fetch_totals` 的 platform 分支对称：
     ```sql
     SELECT COALESCE(NULLIF(agent, ''), ?) AS agent,
            COUNT(*) AS requests,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
            COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
            SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS errors
     FROM requests {where}
     GROUP BY COALESCE(NULLIF(agent, ''), ?)
     ORDER BY (input_tokens + output_tokens + cache_read_input_tokens + cache_creation_input_tokens) DESC
     ```
     返回 `{agent: {requests, input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, errors}}`，按总 token 降序
   - `fetch_aggregate_by_dim`：`valid_dims` 加 `agent`；`dim == "agent"` 分支 `key_expr = "COALESCE(NULLIF(agent, ''), '未知')"`

5. **`src/relay/routers/stats.py`**
   - `stats_aggregate` dim 白名单加 `"agent"`（描述同步）

6. **`src/relay/gui.py`**
   - `_build_snapshot` 加 `by_agent = tui.fetch_agent_totals(db)`（try/except 兜底空 dict）
   - 桥方法 `get_agent_aliases()` + `save_agent_aliases(aliases)`（仿 `save_quick_switch`：mutator → reload → `{ok, path}`）
   - `stats_aggregate` 桥 dim 白名单加 `"agent"`

7. **`src/relay/config.py` + `src/relay/upstreams_file.py`**（别名持久化）
   - `Settings` 加 `agent_aliases: Optional[dict[str, str]] = None`
   - `config.py` 加 `save_agent_aliases(settings, aliases)`：strip 过滤非空 k/v → `save_upstreams_json` mutator 原子写顶层 `agent_aliases`
   - `upstreams_file.apply_to_settings()`：解析顶层 `agent_aliases`（fail-open：非 dict 忽略），过滤非空字符串 k/v

### 前端

8. **`src/relay/web/app.js`**
   - `CARD_DEFS` 加 `{ key: "agent", title: "平台流量", cls: "" }`（platform 与 models 之间）；`renderAll` 加 `forEachCardBody("agent", b => renderAgent(b, ov))`
   - `agentDisplayName(raw)`：`agent_aliases[raw] || raw`（别名映射，纯展示）
   - `renderAgent(body, snap)`：读 `snap.by_agent`，行结构复用 `.platform-row` + 新 `.agent-badge`，total = 4 类 token 相加，显示名走别名。输出结构：
     ```html
     <div class="platform-row">
       <span class="agent-badge agent-claude-code" data-i18n-keep>claude-code</span>
       <span class="platform-meta" data-i18n-keep>3,421 请求 · 1.2M tokens</span>
     </div>
     ```
   - 别名数据流：`snapshot.by_agent`（原始名）→ `agentDisplayName(raw) = agent_aliases[raw] || raw` → DOM。`agent_aliases` 启动时经桥 `get_agent_aliases` 读一次缓存，保存后本地刷新 + 重渲染
   - `renderStatsAgent()`：固定 `api.statsAggregate("agent","30d",0,"relay")` + 30s TTL，rows 的 key 经 `agentDisplayName` 映射后复用 `renderPie(data, "stats-host-agent")`
   - 设置页 `renderSettingsAgentAlias(body, snap)`：列出 `by_agent` 出现过的 agent + 已有别名 + 「未知」，每行可编辑显示名，保存走 `window.__agentAliasSave` → `api.saveAgentAliases` → 刷新 `agentAliases` + 重渲染
   - `api` 加 `getAgentAliases()` / `saveAgentAliases(aliases)` 两个桥 wrapper
   - 启动 `loadAgentAliases()`：`startPolling` 时读一次别名缓存

9. **`src/relay/web/index.html`**
   - 总览 grid 加静态卡 `data-card="agent"`「平台流量」（platform 与 models 之间）
   - 统计页 stats-grid 加 `glass-card[data-card="stats-agent"]` + host `#stats-host-agent`，标题「30天按平台流量」
   - 设置页加 `settings-section[data-card="settings-agentalias"]`「平台别名」区 + `#card-settings-agentalias-body`
   - 版本戳 `?v=20260824-23 → 24`（app.js + styles 均由 index.html 引用，bump 一次全覆盖）

10. **`src/relay/web/styles-20260817.css`**
    - `.agent-badge` 徽标：默认灰（未知）+ `agent-claude-code`（橙 `#d97706`）/ `agent-opencode`（绿 `#10a37f`）/ `agent-codex`（蓝 `#2563eb`）/ `agent-openclaw`（紫 `#c026d3`）/ `agent-ai-sdk`（紫 `#8b5cf6`）
    - `.agent-alias-input` 设置页别名输入框样式
    - `.stats-grid .glass-card[data-card="stats-agent"] { grid-column: span 2 }`（与 pie 同宽，补满 2×2 网格）

### 测试

11. **`tests/test_agent_sniff.py`**（**新建，23 用例全绿**）
    - `sniff_agent` 已知 UA 参数化（claude-cli ×2 版本 / opencode ×1 / codex / openclaw / ai-sdk / "Claude Code" 全名）
    - 未知 UA 参数化（None / 空串 / Bun / node-fetch / Python-urllib / curl / python-requests / testclient / 全空格）
    - 大小写不敏感 + 优先级（opencode 含 ai-sdk 字样仍归 opencode）
    - `record(agent=...)` 落库 + 不传 agent → NULL
    - `fetch_agent_totals` 分组 + NULL/空串/显式「未知」三行并桶 + errors 计数
    - `fetch_aggregate_by_dim(dim="agent")` total_tokens 口径 + 非法 dim 抛 ValueError

12. **`tests/test_stats.py`**
    - `test_aggregate_invalid_dim` valid 集合补 `agent`（特性使然）

### 行为验收清单（手动测试项）

- [ ] 真实 Claude Code 请求 → 总览「平台流量」卡出现 `claude-code` 行（橙徽标）
- [ ] Bun/curl 脚本请求 → 出现在「未知」行（灰徽标）
- [ ] 统计页 → 30 天按平台流量饼图出现（独立于 dim/range 选择器）
- [ ] 设置页 → 平台别名 → 把「未知」改成「自研客户端」→ 保存 → 总览卡 + 统计饼图立刻显示新名
- [ ] 别名只影响显示，`requests.agent` 落库值不变（重启 GUI 后别名仍生效，来自 upstreams.json）
- [ ] 切英文/日文语言，agent 名不被翻译（`data-i18n-keep`）
- [ ] 旧库（迁移前无 agent 列）启动正常，旧行聚合进「未知」
- [ ] `curl "http://127.0.0.1:8088/api/stats/aggregate?dim=agent&range=30d&top=0"` 返回 rows，key 含 claude-code / 未知

### 不在本次范围

- 不改现有 `platform` 字段语义、不动计量/quota 逻辑
- 不识别「同一工具的不同版本」（claude-cli 2.8.4 vs 999.0.0-restored 都归 `claude-code`）
- 别名映射不做 GUI/中继跨进程实时同步（纯展示层，前端渲染时读一次）
- live 面板、实时流侧栏不带 agent 字段
- ⚠ **已知盲区**：Bun/1.3.14 等运行时默认 UA 无法识别（中继拿不到工具名）；DB 只存归类结果不存原始 UA，规则升级后存量行不可重分类（如需可后续加 `raw_ua` 列）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/agent.py` | **新增**（44 行） |
| `src/relay/db.py` | 改（SCHEMA + MIGRATIONS + record） |
| `src/relay/proxy_legacy.py` | 改（入口 sniff + _InFlight + 10 处 record 透传 + 3 个分支函数参数） |
| `src/relay/tui.py` | 改（fetch_agent_totals + fetch_aggregate_by_dim agent） |
| `src/relay/routers/stats.py` | 改（dim 白名单） |
| `src/relay/gui.py` | 改（by_agent 快照 + 2 个桥方法 + dim 白名单） |
| `src/relay/config.py` | 改（agent_aliases 字段 + save_agent_aliases） |
| `src/relay/upstreams_file.py` | 改（apply_to_settings 解析） |
| `src/relay/web/app.js` | 改（CARD_DEFS + renderAgent + renderStatsAgent + 设置别名 + 2 桥 wrapper） |
| `src/relay/web/index.html` | 改（总览卡 + 统计块 + 设置区 + 版本戳） |
| `src/relay/web/styles-20260817.css` | 改（.agent-badge + .agent-alias-input + stats-agent span 2） |
| `tests/test_agent_sniff.py` | **新增**（23 用例） |
| `tests/test_stats.py` | 改（invalid_dim valid 集合） |

---

## v0.156 变更：兜底行为升级 + 桶名改名「其它」

> v0.155 发布后按用户反馈做的行为升级。本变更节追加在同一份特征总账末尾（按版本号顺序追加）。

### 用户指令

> **「后面的所有请求，都不要再走那个未知分类了，有什么头字段就新建什么新平台，只有完全没有头的才归入其它」**

（背景：v0.155 上线后真实流量 36% 落「未知」，最大盲区 Bun/1.3.14 占 26.7%。用户不接受这么大一块流量进兜底桶——Bun 请求**是有 UA 的**（`Bun/1.3.14`），中继只是不认得它，就该按它的 UA 单独建一个平台。）

### 行为变化（前后对比）

| 请求的 User-Agent | v0.155（旧） | v0.156（新） |
|---|---|---|
| `claude-cli/2.8.4 …` | claude-code | claude-code（白名单命中，不变） |
| `opencode/1.18.18 ai-sdk/…` | opencode | opencode（白名单命中，不变） |
| `Mozilla/5.0 … clawx/0.5.2 …` | openclaw（v0.155 后补 clawx 子串） | openclaw（不变） |
| `Bun/1.3.14` | **未知** | **bun**（动态抽 token） |
| `python-httpx/0.28.1` | **未知** | **python-httpx**（动态抽 token） |
| `undici` | **未知** | **undici**（动态抽 token） |
| `curl/8.5.0` | **未知** | **curl**（动态抽 token） |
| （完全无 UA 头） | 未知 | **其它**（兜底桶，改名） |

### 技术决策

1. **三档判定（`sniff_agent`）**：
   1. 白名单命中 → 规范名（原 `_RULES` 顺序表，clawx 修复保留）。
   2. 有 UA 但未命中 → `_extract_ua_token()` 抽 token 动态建平台。
   3. 完全无 UA（None / 空串 / 纯空白）→ `AGENT_UNKNOWN`。
2. **动态抽 token 规则（`_extract_ua_token`）**：UA 是空格分隔的 `product/version` token + 括号注释。取第一个有意义的 product name：
   - 跳过带括号的 token（注释 = OS/引擎说明，如 `(Windows NT 10.0; zh-CN)`）——否则 PowerShell 的 UA 会抽出 `zh-cn` 而不是 `Windowspowershell`；
   - 跳过基础设施标记（`_INFRA` 集合：Mozilla / AppleWebKit / KHTML / Gecko / Chrome / Chromium / Electron / Safari / Windows / NT / Win64 / x64 / Macintosh / Linux / Android …）；
   - 跳过纯数字 token（如 `10.0`）；
   - 结果取 `/` 或 `:` 之前的小写段（`Bun/1.3.14` → `bun`）。
3. **兜底桶改名「未知」→「其它」**：`AGENT_UNKNOWN = "其它"`（前端 `agentDisplayName` fallback、设置页别名区文案同步）。**存量回填**：`init()` 加一次性 `UPDATE requests SET agent='其它' WHERE agent='未知'`（仿 v0.143 endpoint backfill，幂等，只对 agent 列已存在的库执行）。
4. **不回写 / 不回溯**：DB 只存归类结果，不存原始 UA → v0.155 期间已归「未知」的存量行回填成「其它」，但**不**回溯成动态平台（拿不到原始 UA）。这是可接受的信息损失，已写进 CHANGELOG「不在本次范围」。

### 实测验证

对 `relay_trace.log` 52,271 条 UA 跑新 `sniff_agent`：

```
claude-code 30,264 · bun 13,805 · python-httpx 4,541 · opencode 3,361 ·
python-urllib 170 · curl 89 · node 22 · windowspowershell 9 · undici 4 ·
openclaw 3 · testclient 2 · ai-sdk 1 · 其它 0
```

原 36% 盲区全部转化为独立平台，兜底桶归零。

### 文件改动（v0.156 增量）

| 文件 | 改动 |
|---|---|
| `src/relay/agent.py` | `AGENT_UNKNOWN` 改「其它」；`sniff_agent` 三档；新增 `_INFRA` + `_extract_ua_token` |
| `src/relay/db.py` | init() 加 agent 回填；MIGRATIONS / record 注释更新 |
| `src/relay/web/app.js` | agentDisplayName fallback「未知」→「其它」；设置页别名区兜底 key 同步 |
| `src/relay/web/index.html` | 设置页文案「未知」→「其它」；版本戳 24 → 25 |
| `tests/test_agent_sniff.py` | 重构 32 用例（动态 token / 基础设施跳过 / init 回填） |

### v0.156 验收

- [ ] 真实 Bun 脚本请求 → 总览「平台流量」出现 `bun` 行
- [ ] python-httpx / curl / undici 请求各得独立平台
- [ ] 完全无 UA 的请求（如部分 CLI 裸请求）→ 「其它」行
- [ ] 重启 relay 后旧「未知」行自动并进「其它」（init 回填）
- [ ] 设置页「平台别名」仍能改「其它」显示名

---

## v0.157 变更：UA 归类用户规则 + raw_ua 列

> v0.156 完成后用户提的进阶需求。原始指令：「并添加允许用户给某一个 UA 重命名/归类的设置，日后同一种头 UA，都算是某一个平台的」。先 AskUserQuestion 锁定两个设计点：① 匹配方式 = **整串精确匹配**（大小写敏感、最可控）；② 归类目标 = **两者都支持**（可新建平台名，或并入已有平台名）。

### 行为变化（前后对比）

| 场景 | v0.156 | v0.157 |
|---|---|---|
| 设置页配 `{"Bun/1.3.14": "脚本"}` → 收到 `Bun/1.3.14` UA 的请求 | `bun`（动态抽 token） | **`脚本`**（用户规则赢） |
| 设置页配 `{"clawx/0.5.2": "桌面客户端"}` → 收到 clawx UA | `openclaw`（白名单） | **`桌面客户端`**（用户规则赢） |
| 设置页配 `{"Bun/1.3.14": "脚本"}` → 收到 `Bun/1.4.0` UA | 不存在规则 | `bun`（动态抽 token，未命中精确规则） |
| 设置页配 `{"Bun/1.3.14": "脚本"}` → 收到 `BUN/1.3.14` UA | 不存在规则 | `bun`（大小写敏感未命中） |
| 收到未在最近列表里的老规则对应的 UA | 会被动态抽 token 归错 | 直接命中老规则，落用户指定的平台 |

### 三档 → 四档判定顺序

```
   入口 resolve_agent(raw_ua, ua_rules)
   ├─ ① ua_rules[raw_ua.strip()] 整串命中（去空白、大小写敏感、value 非空）
   │     → 返回用户指定的平台名                    ← 优先级最高
   ├─ ② _RULES 子串命中（白名单）
   │     → 规范名（claude-code / opencode / …）
   ├─ ③ _extract_ua_token 抽第一个 product token
   │     → 动态平台名（bun / python-httpx / undici …）
   └─ ④ 完全无 UA / 抽不到
         → AGENT_UNKNOWN（"其它"）
```

### 关键技术决策

1. **DB 新增 `raw_ua TEXT` 列**：v0.155 遗留盲区 —— DB 只存归类结果不存原始 UA，导致 GUI 无法列出「真实出现过的 UA」供用户归类。v0.157 补 `raw_ua` 列，proxy 入口从 User-Agent 头取原文 → 全链透传 15 处 record。聚合完全不读 `raw_ua`（不被 COALESCE 归入任何桶），仅供设置页 `fetch_recent_uas` 数据源。
2. **整串精确匹配 + 大小写敏感**：用户已勾选。理由：UA 头里 `"Bun/1.3.14"` 和 `"Bun/1.4.0"` 是不同工具的不同版本，不应被同一规则一刀切；不区分大小写会误伤 `"Bun/..."` 和 `"bun/..."` 之类同名异源的伪命中。
3. **设置页 datalist 自动补全**：每行右侧输入框用 `<datalist>` 列出已有平台名（含 `by_agent` 出现过的 + 当前 ua_rules 的 value + "其它"），用户可输入新平台名或从列表选已有的。留空 = 删除该规则（保存时 input 空字符串不出现在 dict 里）。
4. **中继进程消费链路**：与 v0.155 的 `agent_aliases` **不一样** —— 后者是纯展示层（GUI 缓存），前者是中继进程在请求入口 `resolve_agent` 直接读 `settings.ua_rules`。所以 GUI 桥 `save_ua_rules` 写盘后必须 POST `/api/upstreams/refresh` 通知中继子进程 reload（仿 `add_upstream` 的 `_ur.Request` 2s 超时静默兜底写法）。
5. **`fetch_recent_uas` 30s TTL**：设置页每行都要渲染一个 UA，但 500ms poll tick 每 tick 都打桥会无谓损耗。仿 `renderStatsAgent` 的 30s TTL 模式（只在切到设置页 / TTL 过期 / 拉到 UA 后重新渲染时才打一次）。
6. **v0.156 fallback 行为完全保留**：用户规则是新增的优先级最高层，原「白名单 → 动态 token → 兜底桶」三档顺序不动。规则命中只决定落库 agent 值，platform / endpoint / 所有聚合口径完全不变。

### 文件改动（v0.157 增量）

| 文件 | 改动 |
|---|---|
| `src/relay/agent.py` | 新增 `resolve_agent(ua, ua_rules)` |
| `src/relay/db.py` | MIGRATIONS 加 `raw_ua TEXT`；record() 加 raw_ua 参数进 INSERT + info dict |
| `src/relay/config.py` | Settings 加 `ua_rules` 字段；`save_ua_rules` helper（mutator 模式） |
| `src/relay/upstreams_file.py` | `apply_to_settings` 解析顶层 ua_rules（fail-open） |
| `src/relay/proxy_legacy.py` | 入口 raw_ua + resolve_agent；_InFlight / _register_inflight / 3 个分支函数加 raw_ua 参数；15 处 record 补 raw_ua=raw_ua |
| `src/relay/tui.py` | 新增 `fetch_recent_uas(db, limit=100)` |
| `src/relay/gui.py` | 桥方法 `get_ua_rules` / `save_ua_rules`（含 POST refresh 通知）/ `get_recent_uas` |
| `src/relay/web/app.js` | api wrappers + `renderSettingsUaRules` + `__uaRuleSave` + loadUaRules + 30s TTL |
| `src/relay/web/index.html` | 新增 `settings-section[data-card="settings-uarules"]`；版本戳 25 → 26 |
| `src/relay/web/styles-20260817.css` | 新增 `.ua-rule-ua`（等宽省略）+ `.ua-rule-input` |
| `tests/test_agent_sniff.py` | 新增 9 用例（resolve_agent + record(raw_ua) + fetch_recent_uas） |
| `tests/test_upstreams_file.py` | 新增 3 用例（ua_rules 解析 / fail-open / save_ua_rules 往返） |

### v0.157 验收

- [ ] 设置页 → UA 归类区出现最近真实 UA（30s TTL 内自动列出）
- [ ] 把 `Bun/1.3.14` 归到「脚本」→ 保存 → 下个 `Bun/1.3.14` 请求的总览/统计归到「脚本」而不是「bun」
- [ ] 把已有平台名（如「opencode」）填进某行 → 保存 → 该 UA 后续请求归到「opencode」
- [ ] 留空某行 → 保存 → 该规则被删除
- [ ] 命中老规则（不在最近列表）的 UA → 仍然命中，落用户指定的平台（不会因为"列表里看不到"就丢规则）
- [ ] 完全无 UA 头的请求 → 仍然归「其它」兜底桶
- [ ] 重启 relay 后所有规则生效（落盘 upstreams.json）

---

## v0.158 变更：总览「平台流量」就地重命名

> 用户反馈：「平台流量卡片中点击可以直接重命名」。v0.155/v0.156 已经支持设置页「平台别名」改显示名，但每次改名都要切到设置页才能编辑 —— 把 alias 编辑入口挪到总览卡片本身，点徽标直接进入就地编辑。

### 行为前后

| 场景 | v0.157 | v0.158 |
|---|---|---|
| 改 `bun` 的显示名 | 切设置页 → 平台别名区 → 找到行 → 编辑 → 保存 → 切回总览 | 总览页直接点 `bun` 徽标 → 输入框 → 保存 |
| 改完后的别名落点 | upstreams.json 顶层 `agent_aliases` | 同上（共用缓存） |
| 编辑期间 500ms poll tick | 输入框被刷掉（设置页有 `lastAgentAliasSig` 守卫但兜底用的是 keys + agentAliases；编辑中是 input 值） | `__agentRowEditing` 守卫在 `renderAgent` 顶部跳过整卡重建 |
| 按 ESC / Enter | 设置页 input 行为 | 直接取消 / 提交 |

### 设计决策

1. **共用 agentAliases 缓存 + 单源保存**：总览就地编辑和设置页「平台别名」共用同一份 `agentAliases`（前端内存），保存都走 `api.saveAgentAliases(next)` 全量覆盖。**不存在两份 cache 互相覆盖的陷阱** —— 任何入口写盘 + reload 后 `loadAgentAliases()` 拉到的就是最新值。设置页 `lastAgentAliasSig = null` 也由 v0.158 显式置零，让下次设置页渲染反映新值。
2. **统计保持不动（不替换整行）**：选 A —— 只换徽标。理由：① 改的是"显示名"，不影响该 agent 的请求数 / token 数；② flex 布局让输入框自然撑宽，统计不动避免布局抖动。
3. **事件委托，不逐徽标绑定**：500ms tick 重渲染会反复 `body.innerHTML = ...`，逐徽标 `addEventListener` 会在每次重建时被 GC（健壮性），但每 tick 重建 N 个绑定也是浪费。统一在 `document` 上做一次事件委托，按 `data-agent-row` 找行 + 按 class 区分"徽标/保存/取消"三个动作。
4. **500ms tick 跳过整卡**：编辑中 poll tick 进来，`__agentRowIsEditing(body)` 返回 true → `renderAgent` 提前 return。代价：编辑期间该卡的统计数字不更新（500ms 一次的节奏，用户编辑完看到的是进入编辑那一刻的值，可接受）。

### 文件改动（v0.158 增量）

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | renderAgent 加 `data-agent-row`；新增 `__agentRowEditing` / `__startAgentEdit` / `__commitAgentEdit` / `__cancelAgentEdit` + click/keydown 委托；renderAgent 顶部加编辑守卫 |
| `src/relay/web/styles-20260817.css` | 新增 `.agent-row-edit` / `.agent-row-input`（focus 蓝色描边） |
| `src/relay/web/index.html` | 版本戳 26 → 27 |

### v0.158 验收

- [ ] 总览 → 平台流量 → 点 `claude-code` 徽标 → 该行变输入框 + 保存/取消，右侧统计保持
- [ ] 输入新名 → 保存 → 该行立刻显示新名（不需刷新）
- [ ] 设置页「平台别名」区也同步显示新名（下次渲染时）
- [ ] 编辑中按 ESC → 还原徽标，输入被丢弃
- [ ] 编辑中按 Enter → 等价于点保存
- [ ] 编辑期间 500ms poll tick 不刷该卡（输入框不被冲掉）
- [ ] 留空保存 → 等价于删除该 agent 的别名
- [ ] 设置页和总览两套入口互不冲突（都是同一份 agentAliases）

---

## v0.159 变更：总览「平台流量」改名 + 选色 modal

> 用户原话：「修改点击某个条目的行为为，弹出编辑窗口，可以输入名字，选择颜色」。v0.158 inline-edit 是「同行变 input」，交互紧但样式不可定制；v0.159 把编辑入口挪到独立 modal，加「完整色环」让徽标底色由用户掌控。

### 行为前后

| 场景 | v0.158 | v0.159 |
|---|---|---|
| 点 `claude-code` 徽标 | 同行变 input + 保存/取消 | 弹出 modal，含 name input + 完整色环（H/S/L slider + HEX + 实时预览） |
| 改显示名 | inline input | modal name input（最长 32 字符） |
| 改颜色 | 不支持 | 三个滑块 + HEX 直输 + 实时预览 + 重置颜色 |
| 删除别名 | 不支持 | modal「删除该别名」按钮 = 落回原名 + css 默认配色 |
| `agent_aliases` 存储格式 | `{raw: "<displayName>"}` | `{raw: {name, color}}` |
| 旧 string value 文件 | 正常 | load 时自动升级为 `{name: <v>, color: None}`，下次写盘落新格式 |
| 重启后颜色 | 不支持 | 落 upstreams.json 顶层 `agent_aliases[raw].color`，重启仍生效 |

### 关键设计决策

1. **`agent_aliases` 升级为对象字典**（用户已勾选）：`{raw: {name: str, color: str|None}}`。color 是 CSS color 字符串（hex / hsl / named 都行），None 时渲染层不写 inline style → css 默认配色生效。**load 侧自动迁移** v0.155 旧 string value（写盘后新格式）—— 老配置文件无需手动改。
2. **完整色环（H/S/L 三个 slider + HEX 直输）**（用户已勾选）：相比预设调色板，最灵活可表达任意色相；HEX 直输对习惯 css 的用户友好；饱和度 slider 默认下限 20%（防接近灰色的脏色）。
3. **共用 `agentAliases` 缓存**：总览 modal 和设置页「平台别名」区都改写同一份内存对象 → 同一条 `api.saveAgentAliases({raw: {name, color}})` 链路 → 同份 upstreams.json。**避免双 Settings 副本陷阱** —— 任何入口写盘后另一入口下次渲染就拿到新值。设置页 `__agentAliasSave` 写时保留既有 color（用户在总览改过颜色 → 设置页写回时**不丢**）。
4. **inline style 优先于 css 类**：渲染层给徽标加 `style="background:<userColor>"`，覆盖 `.agent-claude-code { background: #d97706; }` 等 css 默认；color 为 None 时不写 inline style → css 默认配色生效。这是最轻量的实现，无需动态创建样式表。
5. **三个动作而非两个**：除了「保存」「取消」外加「重置颜色」（抹掉 color，落回 css 默认，name 保留）和「删除该别名」（整条删除，落回原名）。这两个动作让 modal 也能做"撤销"类操作，不需要单独再去设置页清。
6. **HEX 直输同步滑块**：HEX 失焦时拆回 H/S/L 三个滑块值（颜色空间转换在内存里走 hsl，无 CSS 依赖），三个滑块联动实时预览——所见即所得。

### 颜色空间转换（JS 端）

```js
function __hslToHex(h, s, l) {  // h:0-360, s:0-100, l:0-100 → #RRGGBB
  s /= 100; l /= 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => {
    const c = l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return Math.round(255 * c).toString(16).padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}
function __hexToHsl(hex) {
  // 拆解 hex → RGB → HSL（Hue 0-360, Sat/Light 0-100）
  // 标准公式，~15 行 JS。
}
```

### 文件改动（v0.159 增量）

| 文件 | 改动 |
|---|---|
| `src/relay/config.py` | `Settings.agent_aliases` 类型改 `dict[str, dict[str, Optional[str]]]`；`save_agent_aliases` 改签名 `dict[str, dict]`，校验 name 非空 + color strip |
| `src/relay/upstreams_file.py` | `apply_to_settings` 解析 agent_aliases：旧 string value 自动升级成 `{name, color: None}`，新对象校验 name / color |
| `src/relay/web/app.js` | 删除 v0.158 inline-edit 整套（`__agentRowEditing` / 事件委托 / keydown）；新增 `agentBadgeColor(raw)` + `openAgentAliasModal(rawKey)` + `__hslToHex` / `__hexToHsl` + 完整事件处理；renderAgent 加 inline style；`__agentAliasSave` 写时保留既有 color |
| `src/relay/web/styles-20260817.css` | 删 `.agent-row-edit` / `.agent-row-input` v0.158 样式；新增 `.alias-edit-*` 完整色环样式（含 .alias-edit-hue 完整色相 + .alias-edit-light 同步 hue 的亮度条） |
| `src/relay/web/index.html` | 版本戳 27 → 28 |
| `tests/test_upstreams_file.py` | 新增 3 用例（dict schema roundtrip / 旧 string 迁移 / fail-open 非法值丢弃） |

### v0.159 验收

- [ ] 总览 → 平台流量 → 点 `claude-code` 徽标 → 弹出 modal，含 name + 完整色环
- [ ] 拖动色相 slider → 预览徽标实时变色，HEX 同步更新
- [ ] HEX 直输 → 三个 slider 自动同步到对应位置
- [ ] 保存 → 该行立刻显示新名 + 新色（不需刷新）
- [ ] 重置颜色 → color 字段从 agent_aliases 中抹掉，徽标落回 css 默认（橘色）
- [ ] 删除该别名 → 整条条目从 agent_aliases 中删掉，徽标落回原名 + 默认色
- [ ] 取消 → 任何修改丢弃，缓存不变
- [ ] 重启 relay 后颜色仍生效（落 upstreams.json）
- [ ] 设置页「平台别名」区保存 name 时保留用户在总览改过的 color
- [ ] 老配置文件（旧 string value）自动迁移，下次写盘落新格式

## v0.160 变更：色环改 Material 12×8 离散色板

### 用户指令（v0.160）

> 「（贴一张 Material Design 风格 12 列 × 8 行色板截图）」

要求：把 v0.159 的 HSL/饱和度/亮度三 slider + HEX 直输改成离散色板点击直选。

### 行为前后对比

| 行为 | v0.159（滑块） | v0.160（色板） |
|---|---|---|
| 选色方式 | 拖三个 slider（hue / sat / light）+ HEX 直输 | 点 12×8 网格中任一色块 |
| 颜色空间 | HSL ↔ HEX 双向换算（30 行 JS） | 无换算，直接 hex 字符串 |
| 操作步数 | 选色平均 3-5 次拖动 | 1 次点击 |
| 精度 | 任意 HEX | 96 个预设色 |
| 自定义 | HEX 直输可写任意颜色 | 不支持（必须从预设选） |
| 视觉表现 | 完整 360° hue 渐变 + 同步 lightness 条 | Material Design 风格离散色板 |
| 代码量 | ~80 行（slider 处理 + 数学） | ~25 行（色板渲染 + click 委托） |

### 关键设计决策

1. **色板来源**：Material Design 500 系族调色板（Tailwind 同样 12 色相 × 8 lightness）—— 与用户给的截图视觉一致。每个色相从 Tailwind 取 8 个 lightness 阶（50/100/200/300/400/500/600/700/800/900 中的 8 个），共 96 个 hex。
2. **12 列色相**：红/橙/琥珀/黄/青柠/绿/青/天蓝/蓝/紫/品红/粉 —— 覆盖主流徽标配色，跳过 brown / gray / blue-gray（与黑白中性色无辨识度）。
3. **8 行色阶**：每色相从浅到深排。第 1 行（最浅）用作背景强调色（如「统计」类无强对比的徽标），第 4-5 行（中等亮度）作为默认推荐（白字可读性最好），第 8 行（最深）用作头部 / 强调。
4. **状态简化**：删 `__hslToHex` / `__hexToHsl`（30 行数学），删 6 个 slider 监听器。`stateColor` 直接存 `"#rrggbb"` 字符串，保存时序列化器照旧。
5. **代码风格**：色板常量化 `__MATERIAL_SWATCHES`（96 项 flat array）。不动态生成（无好处，硬编码 96 个 hex 反而可读）。
6. **保留语义**：「重置颜色」按钮照旧抹掉 `stateColor` → null → css 默认 `.agent-{raw}` 配色生效。

### 色板 JS 数据结构

```js
const __MATERIAL_SWATCHES = [
  // 红 (8 阶)
  "#fecaca", "#fca5a5", "#f87171", "#ef4444", "#dc2626", "#b91c1c", "#7f1d1d", "#450a0a",
  // 橙 (8 阶)
  "#fed7aa", "#fdba74", "#fb923c", "#f97316", "#ea580c", "#c2410c", "#7c2d12", "#431407",
  // ... 共 12 列 × 8 行 = 96 个
];
```

### 文件改动 v0.160 增量

| 文件 | 改动 |
|---|---|
| `src/relay/web/styles-20260817.css` | 删 `.alias-edit-picker` / `.alias-edit-row` / `.alias-edit-row label` / `.alias-edit-row input[type=range|text]` / `.alias-edit-hue` / `.alias-edit-light` 整套 v0.159 slider 样式；新增 `.alias-swatch-grid`（12 列 grid + 4px gap）+ `.alias-swatch`（aspect-ratio:1 + hover scale 1.12）+ `.alias-swatch.is-selected`（外环） |
| `src/relay/web/app.js` | 删 `__hslToHex` / `__hexToHsl`（30 行数学）；新增 `__MATERIAL_SWATCHES` 常量；`openAgentAliasModal` body 模板把 4 行 `.alias-edit-row` slider 替换为单个 `<div class="alias-swatch-grid">` 渲染 96 个 `.alias-swatch`；删 6 个 slider input/change 监听器，新增 1 个 `.alias-swatch-grid` 事件委托 click 处理 |
| `src/relay/web/index.html` | 版本戳 28 → 29（CSS + JS 各一处） |
| 其它 | 后端 / bridge / 测试 / 配置解析均零改动 |

### v0.160 验收

- [ ] 总览 → 平台流量 → 点 `claude-code` 徽标 → 弹出 modal，含 name + 12×8 色板网格
- [ ] 色板网格 12 列 × 8 行 = 96 个色块，hover 放大到 1.12、过渡 80ms
- [ ] 点击任一色块 → 预览徽标实时变色 + 该色块加蓝色外环（`is-selected`）
- [ ] 重选不同色块 → 选中态平滑迁移（新色块加外环，旧色块移除）
- [ ] 保存 → 该行立刻显示新名 + 新色（不需刷新）
- [ ] 重置颜色 → color 字段从 agent_aliases 中抹掉，徽标落回 css 默认（橘色）
- [ ] 删除该别名 → 整条条目从 agent_aliases 中删掉，徽标落回原名 + 默认色
- [ ] 取消 → 任何修改丢弃，缓存不变
- [ ] 重启 relay 后颜色仍生效（落 upstreams.json）
- [ ] 极端路径：用户存的旧 hex（如 `#abc` 简写 / `red` named）落回 css 默认（`is-selected` 不命中），不报错

## v0.161 变更：总览「平台流量」加显示模式三档开关

### 用户指令（v0.161）

> 「"显示次数""显示token""均显示"三极按钮」

要求：在「平台流量」卡片顶部加三档切换按钮，控制右侧 meta 文案显示请求数 / token / 都显示。

### 行为前后对比

| 行为 | v0.160 前 | v0.161 后 |
|---|---|---|
| meta 文案 | 固定 `${fmtNum(requests)} 请求 · ${fmtTokens(total)} tokens` | 三档切换：仅次数 / 仅 token / 都显示 |
| 切换触发 | 无（写死） | 点击三档按钮，localStorage 持久化 |
| 重渲染范围 | — | 仅 `card-agent-body` 行（不抢焦点、不抖其他卡） |
| 视觉 | 无控件 | 复用 `.consume-switch` 胶囊分段控件（与透传模式开关同语言） |

### 关键设计决策

1. **复用 `.consume-switch` 样式**：与 v0.102 的透传模式三档按钮视觉完全一致，不引入新 CSS 类（除一个 `.card-title-row` 行容器布局）。
2. **持久化**：localStorage 键 `agent-view-mode`，值 `"both" | "requests" | "tokens"`，默认 `"both"`（保持向后兼容既有用户）。
3. **细粒度重渲染**：点击切换只调 `forEachCardBody("agent", b => renderAgent(b, lastSnap))` 重画 agent 卡片行，**不走全 renderAll**。原因：全 renderAll 会重新刷整个 overview 卡片树，对其他卡（实时流、模型分布等）造成不必要的 DOM 重排；用户切换显示模式是局部关切。
4. **i18n 标签硬编码**：档位名称「显示次数 / 显示token / 均显示」不带 `data-i18n` 属性（与透传模式开关一致）—— 模式名称是 UI 控制语义，不参与 i18n 翻译（类似「保存」「取消」等动作按钮）。
5. **不影响点击编辑入口**：徽标点击仍打开 modal，与显示模式正交。

### 文件改动 v0.161 增量

| 文件 | 改动 |
|---|---|
| `src/relay/web/index.html` | agent 卡片标题下新增 `<div class="card-title-row"><div class="consume-switch" data-agent-view-switch></div></div>` |
| `src/relay/web/styles-20260817.css` | 新增 `.card-title-row` flex 行容器（靠左对齐 + 8px gap + spacing-sm margin-bottom） |
| `src/relay/web/app.js` | `renderAgent` 读 `getAgentViewMode()` 决定 meta 文案；新增 `AGENT_VIEW_MODES` / `AGENT_VIEW_LABELS` / `getAgentViewMode` / `setAgentViewMode` / `renderAgentViewSwitch` / `mountAgentViewSwitch`；在主 mount 流程里调 `mountAgentViewSwitch()` |

### v0.161 验收

- [ ] 总览 → 平台流量 → 卡片顶部出现三档胶囊按钮：均显示（默认 active）/ 显示次数 / 显示token
- [ ] 默认状态 meta 文案 = `12 请求 · 1.2k tokens`（既有行为）
- [ ] 切到「显示次数」→ meta 文案 = `12 请求`（无 token 段）
- [ ] 切到「显示token」→ meta 文案 = `1.2k tokens`（无请求段）
- [ ] 点击徽标 → 仍弹出编辑 modal，与显示模式正交
- [ ] 切档后徽标颜色 / 名称 / 点击行为全部保留
- [ ] 刷新页面 → 显示模式从 localStorage 恢复
- [ ] 切档仅刷新 agent 卡片行，**不抖其他卡**（实时流 / 模型分布 / 上游卡片继续平滑轮询）
- [ ] 切档不会触发新数据请求（lastSnap 复用，仅重渲染）

## v0.162 变更：撤回 v0.161 全局开关，改 per-row 显示模式

### 用户指令（v0.162）

> 「是在点击某一个平台的设置页中单独设置其如何显示而不是统一设置」

明确反对 v0.161 的全局开关，要求每行单独设置。位置：经澄清确认 = 总览点击弹出的编辑 modal（不是设置页列表）。

### 撤回 v0.161 的代码清单

| 类型 | 位置 | 处理 |
|---|---|---|
| HTML | agent 卡片标题下 `<div class="card-title-row">` 容器 | 删除 |
| CSS | `.card-title-row` flex 行容器 | 删除 |
| JS | `AGENT_VIEW_MODES` / `AGENT_VIEW_LABELS` / `getAgentViewMode` / `setAgentViewMode` / `renderAgentViewSwitch` / `mountAgentViewSwitch` | 整段删除 |
| JS | `renderAgent` 顶部读全局 mode 决定 meta 文案 | 改读 `agentRowView(raw)`（per-row） |
| JS | mount 流程里的 `mountAgentViewSwitch()` 调用 | 删除 |
| localStorage 键 | `agent-view-mode` | 撤回（不主动清 —— 用户即使有旧值也无害，前端不再读） |

### v0.162 新增：per-row 显示模式

1. **schema 升级**：`{name, color}` → `{name, color, view}`，view ∈ `{"both", "requests", "tokens"}`，非法值静默丢弃（默认 both）。
2. **Settings 类型不变**：`Optional[dict[str, dict[str, Optional[str]]]]` 已覆盖 view 字段（`view: str | None`），无需改类型注解。
3. **新函数 `agentRowView(raw)`**：读 `agent_aliases[raw].view`，缺省 / 非法 → `"both"`。
4. **modal 加三档**：色板上方加 `<div class="consume-switch alias-edit-view">` 三档胶囊（复用透传模式开关的 `.consume-switch-btn` 样式），点击实时更新 `stateView`，保存时一并写入。
5. **设置页 `__agentAliasSave`**：写时保留既有 view（用户在 modal 改过显示模式 → 设置页只改名字写回来时不丢）。
6. **后端白名单校验**：save 与 load 双侧对称；非法值（既不是三个允许值之一也不是 None）静默丢弃。

### 行为前后对比

| 行为 | v0.161 全局 | v0.162 per-row |
|---|---|---|
| 控件位置 | 总览卡片顶部（全局） | 总览点击某平台 → modal（每行独立） |
| 影响范围 | 一键切换所有平台显示模式 | 只改被点的那一行 |
| 持久化 | localStorage `agent-view-mode` | `agent_aliases[raw].view`（与 name/color 同 schema） |
| 撤回难度 | 易（前端 key 全删即可） | 难（schema 涉及后端 + config + 持久化） |
| 数据模型 | 全局 UI 状态（无 agent 维度） | 每个 agent 独立属性（可导出 / 备份 / API 化） |

### 关键设计决策

1. **撤回 v0.161 是「退一步进两步」**：全局开关是「UI 控制」范畴，per-row 是「数据模型」范畴。后者更通用，可导出 / API 化 / 多端同步（如未来加 CLI / 移动端），前者只服务 WebView2 单点。
2. **view 用白名单 + 静默丢弃**：不抛错（破坏配置文件 = 不可接受）；非法 view 落默认 both（最安全 fallback）；保留写盘策略与 color 一致 ——「不写 None、不在 keys 里出现」。
3. **modal 与设置页共用一份 `agentAliases` 缓存**：用户在 modal 改 view → 设置页下次 poll 时读新缓存（已生效）；用户在设置页只改 name → 写回时保留 view（不丢）—— 双入口不冲突。
4. **撤销「撤回 v0.161」的成本考量**：~120 行代码 + 1 个 CSS 类 + 1 个 localStorage 键。看似大，但避免了「全局 vs per-row」语义混乱的长期技术债 —— 用户表达明确，按用户意图走。
5. **modal 里 view 与 color 共存**：两者都是「这个平台长什么样」的展示属性，放同一 modal 是合理的。如果未来再加 sort / hide 等「行为」属性，应考虑拆 tab 或单独 modal。

### 文件改动 v0.162 增量

| 文件 | 改动 |
|---|---|
| `src/relay/web/index.html` | 撤回 `<div class="card-title-row">` 三档开关容器 |
| `src/relay/web/styles-20260817.css` | 撤回 `.card-title-row` flex 容器样式 |
| `src/relay/web/app.js` | 撤回 `AGENT_VIEW_MODES` 等 6 个全局函数；新增 `agentRowView(raw)` 读 per-row view；`renderAgent` 改读 `agentRowView`；modal 加 `<div class="consume-switch alias-edit-view">` 三档 + click handler；modal save payload 加 `view` 字段；`__agentAliasSave` 写时保留既有 view |
| `src/relay/config.py` | `save_agent_aliases` 加 view 白名单校验（合法值写入 entry；非法值静默丢弃，不写 view 键） |
| `src/relay/upstreams_file.py` | `apply_to_settings` 解析 agent_aliases 时加 view 白名单校验（对称） |
| `tests/test_upstreams_file.py` | 新增 2 用例：v0.162 view roundtrip（白名单 / 非法 / None / 缺失 全部正确）；load 侧 fail-open（空字符串 / 数字 / 数组 view 静默丢弃） |

### v0.162 验收

- [ ] 总览 → 平台流量 → 点 `claude-code` 徽标 → 弹出 modal，含 name + 显示模式三档 + 12×8 色板
- [ ] modal 默认 active 档 = 该平台既有 view（`both` / `requests` / `tokens` 之一）
- [ ] 切档仅更新 modal 内按钮高亮（不影响卡片行 —— modal 关闭前不立即生效）
- [ ] 保存 → 该行立刻按新 view 显示 meta（`requests` → `12 请求`；`tokens` → `1.2k tokens`；`both` → `12 请求 · 1.2k tokens`）
- [ ] 同一卡片内 `claude-code` 切到 `tokens`，`opencode` 切到 `requests` → 两行分别按各自 view 显示（**互不影响** —— 关键验收点）
- [ ] 重启 relay 后 view 仍生效（落 upstreams.json）
- [ ] 设置页「平台别名」保存 name 时不丢用户在 modal 改过的 view
- [ ] 设置页「平台别名」保存 name 时不丢用户在 modal 改过的 color（v0.159 既有行为保留）
- [ ] 极端路径：旧 `{raw: string}` value（v0.155 格式）load 后无 view 字段 → 默认 both（不影响）
- [ ] 极端路径：手改 upstreams.json 把 view 写成 `"garbage"` → load 后该 raw 无 view 键 → 默认 both（不抛错）
- [ ] 极端路径：撤回的 `agent-view-mode` localStorage 旧值残留 → 前端不再读取，无副作用
