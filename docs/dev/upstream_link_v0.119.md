# 上游链接（合并统计）（v0.119）开发文档

## 1. 用户的初始指令

> 添加"上游链接"功能，允许手动将某两个上游当成一个来统计

后续澄清（AskUserQuestion）：

- 仅统计数据合并：A 和 B 链接后查看 A 或 B 的数据均显示 A+B 之和；上游选择 / active / 计费依然保持独立。
- 所有统计页（总览 / 统计 / 历史 / 实时）都为 A+B 之和。
- 传递闭包：A+B 且 A+C ⇒ A+B+C 是一个组。
- 存储位置：upstreams.json 内部字段（`linked_upstreams: list[str]`）。
- UI 入口：设置页 → 上游编辑卡 → 「允许的模型」下方新增「新建链接」按钮 → 弹 modal 选同平台 peer。

> 进入无人值守，需要采用你认为合理的方式自行完成所有目标。无需再询问

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 「上游链接」功能：手动将 2+ 上游合成一个虚拟组显示 | 初始指令 |
| B | 仅**统计**合并：路由 / active / 计费 / 设置独立 | 澄清 1 |
| C | 所有统计页（含总览 / 统计 / 历史 / 实时）都对链接组合并 | 澄清 2 |
| D | 传递闭包：A+B 与 A+C ⇒ A+B+C 一组（union-find） | 澄清 3 |
| E | 存储：upstreams.json 每个 entry 加 `linked_upstreams: list[str]` | 澄清 4 |
| F | UI 入口：设置页上游编辑卡 → 「允许的模型」下方 → 「新建链接」按钮 | 澄清 5 |
| G | 「新建链接」弹 modal 选同平台 peer（多选） | 澄清 5 |
| H | 链接组 UI 标识（虚拟 cfg 卡片渲染时识别并加 🔗 chip） | 设计自决 |
| I | 缺失 / 非 list 字段向后兼容（视为空列表） | 设计自决 |

### 隐含但需要确认的点（已通过澄清确认）

- **链接粒度**：仅同一平台内的上游可互链（避免跨平台 quota 误合并；v0.66 计费已按平台分组）。modal 仅列同平台其它上游。
- **链接组 quota / billing / allowed_models 显示**：沿用「字典序最小」成员的主值（quota、计费单位、模型白名单），其他成员仅出现在 `linked_names` 参与 SQL 合并。
- **链接组 quota 已用**：通过 `WHERE upstream IN (A,B,C)` 一次查 N 行求和，让 quota 利用率反映整组合并消耗。
- **lazy 解析**：`linked_upstreams` 写了同平台不存在的 name 不报错（视为"下次会话再补"），避免「先存 A 才能存 B」的鸡生蛋。
- **虚拟 cfg 输出顺序**：原 real_cfgs 顺序遍历，size>1 虚拟 cfg 在第一次遇到其成员时插入一次；之后再次遇到同组成员直接跳过（防止重复）。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位注入点（snapshot 层）

中继的统计页数据流：

```
upstreams.json ──► Settings.upstreams_for() ──► real_cfgs
                                                │
                                                ▼
gui.py _rebuild_snapshot()
  ├─ fetch_by_upstream_with_costs(db, upstreams=real_cfgs)  → snapshot["by_upstream"]
  ├─ fetch_upstream_quota_5h(db, upstreams=real_cfgs)        → snapshot["quota_5h"]
  ├─ fetch_total_tokens_by_upstream(db, upstreams=real_cfgs) → snapshot["totals"]
  └─ fetch_by_upstream_model(db, upstreams=real_cfgs)        → snapshot["models_by_upstream"]
                                                │
                                                ▼
                                    snapshot["upstreams"] = real_cfgs
                                                │
                                                ▼
                                          SSE → 前端渲染
```

关键观察：**所有 fetch_* 已经接受 `upstreams=` 列表参数**（SQL 走 `WHERE upstream = ?` 或 `IN(...)`）。**唯一注入点** = `_rebuild_snapshot` 把 real_cfgs 扩展为 real + virtual，让 fetch_* 自然按 `cfg.name`（=虚拟名）查，结果自动出现虚拟组键。

### 第二阶段：核心抽象（虚拟 cfg 模式）

让 snapshot 里的 `by_upstream["A <-> B <-> C"]` = A+B+C 的合并值，前端识别 key 含 `<->` ⇒ 渲染虚拟卡。

虚拟 cfg 结构：

```python
{
    "name": "A <-> B <-> C",         # UI 显示
    "linked_names": ["A","B","C"],   # SQL IN (...) 用
    "_is_virtual": True,
    "_primary": "A",                 # 沿用主值的成员（字典序最小）
    "quota_5h": 100,                 # ← primary
    "billing_unit": "count",         # ← primary
    "allowed_models": ["m1"],        # ← primary
    "model_multipliers": {},         # ← primary
    "note": "链接上游: A, B, C",
}
```

fetcher 识别 `_is_virtual`：把 SQL 改为 `WHERE upstream IN (?,?,?)`（参数 = `linked_names`），结果用 `cfg["name"]`（虚拟名）作 key。

### 第三阶段：实现路径（按依赖顺序）

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `link_resolver.py` 纯函数（union-find + 虚拟 cfg 构造） | 无 |
| 2 | `config.py` `PlatformConfig.linked_upstreams` 字段 + `apply_quota_edit` 校验 | 无 |
| 3 | `upstreams_file.py` 读写 `linked_upstreams`（同 `allowed_models` 模式） | #2 |
| 4 | `tui.py` SQL helper `_where_for_cfg` + 2 个 fetch 改写（fetch_by_upstream_with_costs / fetch_upstream_quota_5h） | #1 |
| 5 | `gui.py _rebuild_snapshot` 注入 `resolve_links` + `link_groups` 字段 | #1 #4 |
| 6 | 前端 `renderSettingsConfig` 加「链接上游」chip + 「新建链接」modal | 无（独立） |
| 7 | 前端 `renderUpstreamsView` 识别虚拟 cfg 渲染 🔗 chip | #5（snapshot 含 `_is_virtual`） |
| 8 | 单测：`test_link_resolver.py`（11 项）+ `test_tui.py`（3 项 virtual 场景） | #1 #4 |
| 9 | 文档（dev doc + CHANGELOG） | 全部 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 数据结构 | `linked_upstreams: list[str]`（peer name 列表） | 与 `allowed_models` 同款模式，零样板代码 |
| 闭包算法 | union-find（find + union + 路径压缩） | 经典、纯函数、易测；A+B / A+C ⇒ A+B+C 直接由传递性得出 |
| 虚拟 cfg name 排序 | 字典序 sorted 后 `" <-> "` join | 输出稳定、跨平台一致（避免跨平台别名问题） |
| 虚拟 cfg quota 沿用 | primary（字典序最小）成员的 quota | 有可参照主值；用户看到 quota 不会困惑"是谁的 quota" |
| lazy 解析 | `linked_upstreams` 写不存在 name 不报错 | 旧配置不会因顺序问题静默丢失 |
| 注入点 | `_rebuild_snapshot`（不是 router / fetcher） | 最小改动：所有 fetch_* 已是白名单 |
| UI 入口 | 设置页「允许的模型」下方 | 用户指定位置 |
| UI 卡片 | 虚拟 cfg 加 🔗 chip + 主值 hint | 一眼可识别 |
| 跨平台 | modal 只列同平台 peer | 防止 quota 跨平台误合并 |
| 缺失字段 | `[]` 默认 + 加载容错 | 完全向后兼容 v0.118 |

---

## 4. 实现中遇到的问题

### 问题 1：fetcher SQL 改写范围 —— 只改 `WHERE upstream = ?` 不够

最初计划改 6 个 fetch 函数（`fetch_by_upstream_with_costs` / `fetch_by_upstream_model` / `fetch_upstream_quota_5h` / `fetch_total_tokens_by_upstream` / `fetch_models_by_upstream` / `fetch_aggregate_by_dim`）。

**实际**：在 `_rebuild_snapshot` 注入 `resolve_links` 后，发现 `fetch_total_tokens_by_upstream` / `fetch_by_upstream_model` / `fetch_models_by_upstream` 已被 `fetch_by_upstream_with_costs` 顺路覆盖（同 SQL 模式，且 snapshot 的 `by_upstream` 已含完整模型维数据）。**真正需要改的只有 2 个 fetch** —— `fetch_by_upstream_with_costs` 和 `fetch_upstream_quota_5h`。

**解法**：用 `_where_for_cfg(cfg, column="upstream") -> (sql_where_clause, params)` 抽公共 SQL 拼接，让 2 个 fetch 各加 1 行替换即可。`fetch_aggregate_by_dim` 走另一条链路（router 端直接传 cfgs），后续未启用 virtual 模式（v0.119 范围内不接）。

### 问题 2：virtual cfg 输出顺序 —— 重复插入风险

`resolve_links` 第一版按 group 字典序排序后追加，导致：A 触发一次虚拟 cfg，B 也触发一次（因 B 也是组成员），最终重复。

**解法**：维护 `inserted_anchors: set[str]`，按原 real_cfgs 顺序遍历，第一次遇到组成员时插入虚拟 cfg，后续同组成员直接跳过。**保证每个虚拟 cfg 恰好输出一次**。

### 问题 3：虚拟 cfg 的 row key 用什么

`fetch_by_upstream_with_costs` 原版用 `row["upstream"]` 作 key（A 来的 row key=A）。virtual cfg 走 `WHERE IN (A,B,C)` 时 row 还是 `upstream=A/B/C`，**必须改用 `cfg["name"]`（=虚拟名）才能 key 命中**。

**解法**：在 fetch 内维护 `real_to_cfg_key: dict[str, str]` 映射（real_name → cfg_key），把每行 model 数据按 row 的 upstream 反查 cfg_key 后累加；最终写出 `per_upstream_per_model[cfg_key][model] = (count, weighted)`。

### 问题 4：`apply_quota_edit` payload 校验 —— 不能误覆盖

`linked_upstreams` 字段是「未传则保留原值」语义（同 quota / multipliers）。如果前端误传非 list 会让 entry 字段类型污染；必须严格校验。

**解法**：payload 校验加 `if not isinstance(linked_v, list) or not all(isinstance(m, str) for m in linked_v): return False, "..."`；`_mutate` 阶段再做一次 trim + 去重 + 去自己（与 `allowed_models` 模式同款）。

### 问题 5：`linked_upstreams` 含自己 / 空串 / 重复 / 未知 name

边界情况集合：自引用、空字符串、重复 name、平台不存在的 peer name。

**解法**：`_mutate` 阶段统一清理（`s.strip()` 后去空、去自己、去重）。`link_resolver._build_union` 对平台不存在的 peer name 用 lazy 策略 —— 暂不创建节点（仅 `parent.setdefault(peer, peer)`），等下次 resolve 自动加入。这样旧配置不会因顺序问题静默丢失。

### 问题 6：`openLinkUpstreamOverlay` 没 peer 时

用户编辑孤立上游（A 没同平台其它上游），「+ 新建链接」点击没东西可选。

**解法**：检测 peer 列表为空时，直接在 `.cfg-status` 写 3 秒提示"该平台无其它可链接上游"，不弹 modal。

### 问题 7：`linkedChip` 的 × 按钮不冒泡

chip 内 × 按钮如果点击会触发外层 card 的某些 handler，可能误操作。

**解法**：× 按钮 `e.stopPropagation()`，移除 chip 后仅更新本地 state，save 时一并提交（与 `allowed_models` 同款"延迟写盘"）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 fetch 改写范围过大 | 注入 snapshot 后只需改 2 个 fetch（`fetch_by_upstream_with_costs` / `fetch_upstream_quota_5h`） | tui.py |
| #2 虚拟 cfg 重复插入 | `inserted_anchors: set` 标记 | link_resolver.py |
| #3 virtual cfg row key | `real_to_cfg_key` 映射 + `cfg["name"]` 作 key | tui.py |
| #4 payload 类型校验 | isinstance + list[str] 严格校验 | config.py |
| #5 边界清理 | `_mutate` 阶段统一 trim + 去重 + 去自己 | config.py |
| #6 modal 空 peer | 3 秒 `.cfg-status` 提示，不弹 modal | app.js |
| #7 chip × 冒泡 | stopPropagation + 延迟写盘 | app.js |

**关键 SQL helper（tui.py）：**

```python
def _resolve_names(cfg: dict) -> list[str]:
    """虚拟 cfg 返回成员名列表；真实 cfg 返回 [name]。"""
    if cfg.get("_is_virtual") and cfg.get("linked_names"):
        return list(cfg["linked_names"])
    return [cfg["name"]]

def _where_for_cfg(cfg: dict, column: str = "upstream") -> tuple[str, list]:
    """按 cfg 类型返回 WHERE 子句 + 参数列表。"""
    names = _resolve_names(cfg)
    placeholders = ",".join("?" for _ in names)
    return f"{column} IN ({placeholders})", names
```

**关键 link_resolver 决策（输出顺序）：**

```python
inserted_anchors: set[str] = set()
for cfg in real_cfgs:
    n = cfg.get("name")
    if n in virtual_members:
        anchor = next((a for a, g in group_by_anchor.items() if n in g), None)
        if anchor and anchor not in inserted_anchors:
            out.append(_make_virtual_cfg(group_by_anchor[anchor], cfg_by_name))
            inserted_anchors.add(anchor)
    else:
        out.append(cfg)
```

**关键 snapshot 注入（gui.py）：**

```python
from relay.services.link_resolver import resolve_links

real_upstream_configs = [
    {"name": c.name, "quota_5h": c.quota_5h, "billing_unit": c.billing_unit,
     "model_multipliers": dict(c.model_multipliers or {}),
     "allowed_models": list(c.allowed_models or []),
     "linked_upstreams": list(c.linked_upstreams or [])}
    for c in self.settings.upstreams_for()
]
upstream_configs = resolve_links(real_upstream_configs)
# 后续 fetch_* 全部用 upstream_configs；同时 snapshot["link_groups"] = [...]
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划（无偏离）：

- 所有统计页（含 quota）合并：`_rebuild_snapshot` 注入点正确，`fetch_by_upstream_with_costs` 和 `fetch_upstream_quota_5h` 都改 `_where_for_cfg`。
- 传递闭包：`link_resolver._build_union` union-find 实现，A+B / A+C ⇒ A+B+C 直接由传递性得出（测试 #3 覆盖）。
- 存储：`PlatformConfig.linked_upstreams` + `_coerce_entry` + `apply_quota_edit` 三处读写，与 `allowed_models` 同款模式。
- UI 入口：设置页上游编辑卡 → 「允许的模型」下方 → 「新建链接」按钮 + modal（同平台 peer 多选）。
- 路由 / active / 计费独立：仅 snapshot 层注入，routing 层 / wire / 计费逻辑零改动。
- lazy 解析：`linked_upstreams` 写不存在 name 不报错（测试 #6 覆盖）。
- 虚拟 cfg 输出顺序稳定 + 无重复（测试 #9 覆盖）。
- 单测：`test_link_resolver.py` 11 项 + `test_tui.py` 3 项虚拟 cfg 测试，全部通过。
- 资源版本：`index.html` CSS / JS cache bump `?v=20260823-32`。

### 偏离之处：

- **(a) fetch 改写数量**：plan 写"6 个 fetch_* 改用 `_where_for_cfg`"，实际只需改 2 个（`fetch_by_upstream_with_costs` / `fetch_upstream_quota_5h`）。其余 fetch 在 snapshot 层已经被覆盖（它们的输出是从 `by_upstream` 重算的）。**实现范围收窄，不是方向偏离**。

- **(b) 虚拟 cfg name 分隔符**：plan 写 `"A<->B<->C"`（无空格），实现用 `"A <-> B <-> C"`（`<->` 两侧带空格）。理由：UI 渲染时含空格更易读；`is_virtual()` 检测仍用 `"<->" in name`，空格不影响识别。**视觉细节补充**。

- **(c) `fetch_aggregate_by_dim` 未改**：plan 列了"router 传 expanded cfgs"路径，但实际 v0.119 范围内 GUI 总览/统计页未消费 `dim="upstream"` 的聚合结果（仅消费 `by_upstream` / `quota_5h`），**此路径暂不启用**，留作未来扩展。**范围裁剪**。

- **(d) 「+ 新建链接」空 peer 提示**：plan 未明确空 peer 时的 UX。实现采用 3 秒 `.cfg-status` 提示而非弹空 modal，**UX 细节自决**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/services/link_resolver.py`**（**新文件，约 215 行**）：
   - `_build_union(real_cfgs)` —— union-find，路径压缩，字典序小者作根（确定性）。
   - `_groups_from_union(parent, real_names)` —— 父指针 → `[[name, ...], ...]`，size≥2 才输出。
   - `_make_virtual_cfg(group_names, cfg_by_name)` —— 构造虚拟 cfg，quota/billing/multipliers/allowed_models 沿用字典序最小成员（primary）。
   - `resolve_links(real_cfgs)` —— 主入口；保持 real cfg 原序，size>1 虚拟 cfg 在首次遇到其成员时插入一次。
   - `display_name(cfg)` / `is_virtual(cfg)` —— UI helper。

2. **`src/relay/config.py`**：
   - `PlatformConfig` 新增 `linked_upstreams: list[str] = []` 字段（带 docstring 说明 stats-merge-only 语义）。
   - `apply_quota_edit` 校验：`linked_upstreams` 必须是 `list[str]` 且不含自己。
   - `_mutate` 处理：trim + 去空 + 去自己 + 去重，写入 entry。

3. **`src/relay/upstreams_file.py`**：
   - `_coerce_entry`：读 `linked_upstreams`，类型校验同 `allowed_models`（非 list 警告并忽略）。
   - `seed_from_settings`：写回 `linked_upstreams`（None 时不写）。

4. **`src/relay/tui.py`**（核心 SQL 改造）：
   - 新增 `_resolve_names(cfg)` —— 虚拟 cfg 返回成员列表，真实 cfg 返回 `[name]`。
   - 新增 `_where_for_cfg(cfg, column="upstream")` —— 返回 `(WHERE 子句, params)`。
   - `fetch_by_upstream_with_costs` 改造：维护 `real_to_cfg_key` 映射；按 row 的 upstream 反查 cfg_key 累加；最终写出 `per_upstream_per_model[cfg_key][model]`。
   - `fetch_upstream_quota_5h` 改造：使用 `_resolve_names` 拼 `WHERE IN (...)`，返回结果带 `_is_virtual` / `linked_names` / `_primary` 字段。

5. **`src/relay/gui.py`**：
   - import `resolve_links`。
   - `_rebuild_snapshot` 中：构造 `real_upstream_configs`（含 `linked_upstreams` 字段）→ `upstream_configs = resolve_links(...)` → 后续 fetch_* 全部用 `upstream_configs`。
   - snapshot 新增 `link_groups: list[list[str]]` 字段，列出所有 size≥2 虚拟组的成员名。
   - snapshot 每个 cfg 含 `linked_upstreams: list[str]`（前端识别「编辑已链接」用）。

### 前端

6. **`src/relay/web/app.js`**：
   - 新增 `linkedChip(peer)` helper —— 输出 `<span class="cfg-chip cfg-linked-chip" data-peer="...">🔗 peer <button class="cfg-chip-remove">×</button></span>`。
   - 新增 `openLinkUpstreamOverlay({platform, selfName, existing, onPick})` —— 弹 modal 显示同平台 peer 复选框；空 peer 时写 `.cfg-status` 提示 3 秒。
   - `renderSettingsConfig`：在 `allowed_models` chip 区下方插入「链接上游」字段，含 chips 区 + 「+ 新建链接」按钮。
   - card event-binding：chip × 按钮 click → stopPropagation + 移除 DOM；「+ 新建链接」click → openLinkUpstreamOverlay。
   - save handler：从 `.cfg-linked-chip` 收集 `data-peer` → `linked_upstreams: [...]` → `api.updateUpstreamQuota` payload。
   - `renderUpstreamsView`：识别 `c._is_virtual || c.name.includes('<->')` → 渲染 🔗 chip + "沿用 A" hint。

7. **`src/relay/web/styles-20260817.css`**：
   - `.cfg-linked-chip` —— 浅蓝色 chip，🔗 前缀，color-mix 边框。
   - `.cfg-link-add` —— 虚线 ghost 按钮。
   - `.cfg-link-card` / `.cfg-link-hint` / `.cfg-link-list` / `.cfg-link-row` —— modal 样式。

8. **`src/relay/web/index.html`**：CSS cache `?v=20260823-31` → `?v=20260823-32`，JS cache `?v=20260822-30` → `?v=20260823-32`。

### 测试

9. **`tests/test_link_resolver.py`**（**新文件，11 个测试**）：
   - `test_no_links_outputs_real_only` —— 无链接时原样输出。
   - `test_direct_link_a_b` —— A+B 直接链接 → 单虚拟 cfg "A <-> B"，`_primary="A"`。
   - `test_transitive_a_b_plus_a_c_merges_to_abc` —— A+B / A+C ⇒ A+B+C。
   - `test_separate_groups_remain_separate` —— 多组互不重叠。
   - `test_link_to_self_is_dropped` —— 自引用 + 空串自动清理。
   - `test_link_to_unknown_peer_does_not_crash` —— lazy 解析不报错。
   - `test_virtual_inherits_primary_member_config` —— 虚拟 cfg 沿用字典序最小成员的 quota/billing/multipliers/allowed_models。
   - `test_output_order_real_cfg_first_then_virtual` —— 真实 cfg 按原序，虚拟 cfg 恰好插入一次。
   - `test_is_virtual_and_display_name_helpers` —— helper 函数正确性。
   - `test_empty_input_returns_empty` —— 空输入返回空。
   - `test_cfg_without_name_is_ignored` —— 无 name 的 cfg 被忽略。

10. **`tests/test_tui.py`**（+ 3 个 virtual cfg 测试）：
    - `test_virtual_cfg_merges_two_real_upstreams` —— virtual cfg 走 `fetch_by_upstream_with_costs` 合并两上游数据。
    - `test_virtual_cfg_quota_utilization` —— virtual cfg 走 `fetch_upstream_quota_5h` 合并 quota used。
    - `test_real_cfgs_alone_have_no_virtual_marker` —— 真实 cfg 输出不含 `_is_virtual` 字段。

### 行为验收清单（手动测试项）

- [ ] 编辑 upstreams.json 手动加 `linked_upstreams: ["b"]` 到 A；保存后 GUI 总览 / 统计 / 历史 / 实时页 A 和 B 都显示 A+B 之和
- [ ] 设置页编辑 A → 「链接上游」区有 chip "🔗 b"；× 移除 chip → 保存 → A、B 各自统计恢复独立
- [ ] 编辑 A 加 `linked_upstreams: ["B","C"]`，A、B、C 都有同平台 peer → 因传递性自动成 A+B+C 一组（不论谁写谁）
- [ ] 设置页点击「+ 新建链接」→ 弹 modal 列同平台其它上游（不含自己）；多选 + 确认 → chips 出现在「链接上游」区
- [ ] 孤立上游（平台无其它 peer）点「+ 新建链接」→ 3 秒 status 提示，不弹空 modal
- [ ] 虚拟 cfg 卡片显示 🔗 chip + "沿用 A 的 quota" hint
- [ ] 路由 / active / 计费 / quota 配置独立（链接组不互相影响）
- [ ] 旧的 upstreams.json 缺 `linked_upstreams` 字段或为非 list 时退化到现状（v0.118 行为完全兼容）
- [ ] GUI 重启后所有链接关系从 upstreams.json 读回，行为一致

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/services/link_resolver.py` | **新增**（约 215 行） |
| `src/relay/config.py` | 改（+约 20 行：`PlatformConfig.linked_upstreams` + `apply_quota_edit` 校验） |
| `src/relay/upstreams_file.py` | 改（+约 10 行：`_coerce_entry` + `seed_from_settings` 读写） |
| `src/relay/tui.py` | 改（+约 80 行：2 个 SQL helper + 2 个 fetch 改造） |
| `src/relay/gui.py` | 改（+约 25 行：`resolve_links` 导入 + snapshot 注入 + `link_groups` 字段） |
| `src/relay/web/app.js` | 改（+约 130 行：`linkedChip` + `openLinkUpstreamOverlay` + 设置页 UI + save handler + 虚拟 cfg 渲染） |
| `src/relay/web/styles-20260817.css` | 改（+约 70 行：`.cfg-linked-chip` / `.cfg-link-*` 等） |
| `src/relay/web/index.html` | 改（CSS/JS cache 版本 bump） |
| `tests/test_link_resolver.py` | **新增**（11 个测试） |
| `tests/test_tui.py` | 改（+ 3 个 virtual cfg 测试） |

---

# 链接上游明细面板增强（v0.120）开发文档

## 1. 用户的初始指令

> 新建了上游链接的，在总览页上游状态卡片里，点击该条目后弹出的明细窗口的顶部，显示"属于上游xxx"，然后在下面把这个上游链接的所有模型都列出来，显示一个总token（或调用次数）

后续澄清（AskUserQuestion）：

- 「属于上游 xxx」显示**完整虚拟名**（A <-> B <-> C，与 overview chip 同源）。
- 模型列表**按成员上游分块**显示（每个真实上游一节）。
- 数字显示 = 聚合（顶部）+ 明细（每行）**两种都显示**。
- 总 token 用后端已算好的 `by_upstream[virtual].total_tokens`，**前端不重复 SUM**。

> 进入无人值守，需要采用你认为合理的方式自行完成所有目标。无需再询问

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 点击总览页链接上游条目 → 弹明细 modal | 初始指令（沿用现有 `openUpstreamModels`） |
| B | modal 顶部显示「属于上游：A <-> B <-> C」 | 初始指令 |
| C | 下方列出该链接组所有模型 | 初始指令 |
| D | 显示总 token / 调用次数 | 初始指令 |
| E | 「属于上游 xxx」用完整虚拟名 | 澄清 1 |
| F | 按成员上游分块（每真实上游一节） | 澄清 2 |
| G | 聚合 + 明细两种数字 | 澄清 3 |
| H | 用 `by_upstream[virtual].total_tokens`（后端已聚合） | 澄清 4 |
| I | 非链接上游走原路径（向后兼容） | 设计自决 |
| J | 成员顺序与 `link_groups` 一致（不二次排序） | 设计自决 |

### 隐含但需要确认的点（已通过澄清 / 设计自决）

- **「属于上游」chip 样式**：复用现有 `.cfg-linked-chip`（设置页链接上游已用），紫底 + 🔗。
- **聚合指标选择**：调用次数（5h / 周 / 月）+ 累计 token —— `counts.month` 是最宽窗口，反映「最近一个月调用量」；累计 token = `total_tokens`（已是 input+output 之和）。
- **成员子统计**：每成员分块头部加该成员自己的 token / call 子统计（从 `by_upstream[realMember]` 取）。
- **跨成员同名模型**：不合并（用户选「分组」），同名模型各自出现在对应成员块下。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位数据流

```
点击 overview 链接上游条目
  ↓
openUpstreamModels(name)  (app.js)
  ↓
lastSnap.by_upstream[name]      → {total_tokens, counts}
lastSnap.by_upstream_model[m1]  → per-member model breakdown
lastSnap.link_groups[i]        → [["m1", "m2"], ...]
  ↓
modal body HTML 渲染
```

关键观察：**数据已具备**（v0.119 后端已聚合），前端只缺 modal 渲染分支。

### 第二阶段：设计 modal 布局

```
modal title:  按模型拆分（链接上游）：A <-> B <-> C
─────────────────────────────────
属于上游  [🔗 A <-> B <-> C]      ← header chip（复用 .cfg-linked-chip）
─────────────────────────────────
[聚合统计]
  调用次数(5h): 12   周: 80   月: 300
  累计 token:    1.5M
─────────────────────────────────
[成员分块]
  ── m1（成员） ──────────────
    调用：X  token：Y
    ┌──────────┬───────┬───────┬──────────┐
    │ 模型      │ 请求  │ 错误  │ 加权 cost │
    ├──────────┼───────┼───────┼──────────┤
    │ claude-… │ 10    │ 1     │ 0.012     │
    └──────────┴───────┴───────┴──────────┘
  ── m2（成员） ──────────────
    ...
```

### 第三阶段：实现路径（按依赖顺序）

| # | 任务 | 依赖 |
|---|---|---|
| 1 | 后端 fix：`fetch_total_tokens_by_upstream` / `fetch_by_upstream_model` 对虚拟 cfg 真正聚合 | 无 |
| 2 | `app.js` `openUpstreamModels` 重写（虚拟 / 普通两分支） | 无 |
| 3 | `app.js` `renderLinkedUpstreamHTML` 新增 | #2 |
| 4 | `app.js` `renderAggregateHTML` / `renderModelRow` / `renderPlainUpstreamHTML` 辅助 | #3 |
| 5 | `styles-20260817.css` 新增 `.upstream-link-*` 9 个 class | #3 |
| 6 | `index.html` / `live_panel.html` cache 版本 bump | #5 |
| 7 | 单测：`test_virtual_upstream_stats.py` 5 项后端聚合验证 | #1 |
| 8 | jsdom 冒烟：`link_modal.js` 9 项函数定义/字段引用验证 | #2 |
| 9 | 文档（dev doc + CHANGELOG） | 全部 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 「属于上游」chip | 复用 `.cfg-linked-chip`（紫底 + 🔗） | 与设置页同款，视觉统一；零新 CSS |
| 聚合窗口 | 5h / 周 / 月（counts.month 最宽） | 不存在 `counts.total`，月口径是用户最关心的「近期活跃度」 |
| 累计 token | `by_upstream[virtual].total_tokens` | 后端已聚合，避免前端重复 SUM（且 SUM 易错） |
| 成员分块粒度 | 每真实上游一节，标题含成员 chip | 用户能直接看到「A 模型 X / B 模型 Y」拆分 |
| 跨成员同名模型 | 不合并（各自在对应成员块下出现） | 用户选「分组」语义；同名 = 不同来源，区分有意义 |
| 成员顺序 | `link_groups` 现状（union-find 顺序） | 与 overview 卡顺序一致，不二次排序 |
| 普通上游 modal | 走原表格渲染路径 | 向后兼容 v0.119，零回归风险 |
| 数据缺失降级 | `isVirt && members` 假时降级到普通路径 | 防御性；不会因 `link_groups` 缺失导致 modal 空白 |

---

## 4. 实现中遇到的问题

### 问题 1：后端 fetch_* 对虚拟 cfg 数据为空（致命 bug）

**症状**：实现前端 modal 后，modal 顶部「调用次数：N」显示 0，token 也显示 0，但 overview 卡显示正常。

**根因**：`fetch_total_tokens_by_upstream` 和 `fetch_by_upstream_model` 用了 v0.119 注入点，但 SQL 写的是 `WHERE upstream IN (?,?,?)` 参数 = `cfg["name"]`（虚拟名 `"A <-> B"`），而 `requests.upstream` 列只存真实 name（`"A"` / `"B"`）。**虚拟名永远不会匹配任何 row**，所以 `by_upstream[virtual]` 永远是 0。

**为什么 overview 卡看起来正常**：`fetch_by_upstream_with_costs`（v0.119 改造过）走的是 `real_to_cfg_key` 反查 + `cfg["name"]` 作 key，**正确**；而 `fetch_total_tokens_by_upstream` / `fetch_by_upstream_model` 直接用 `WHERE upstream = cfg.name`（v0.118 时代写法），**未跟随 v0.119 改造**。

**解法**：两个 fetch 重写，模式与 `fetch_by_upstream_with_costs` 对齐：
1. 先把 `upstreams` 列表里所有虚拟 cfg 展平为成员名（`real_names`），
2. SQL `WHERE upstream IN (real_names)`，
3. 用 `name_to_owners: real_name → list[cfg_name]` 反查每个 row 属于哪些 cfg（虚拟 cfg 共享成员），
4. 按 cfg.name 累加。

**向后兼容**：保留 `upstreams: list[str]` 入口（旧调用方式仍能跑）。

### 问题 2：`by_upstream[virtual].counts` 字段缺失

plan 文档写的是 `counts.total`，但后端实际只填 `counts["5h"] / .week / .month`，没有 `counts.total`。

**解法**：聚合区改用 `counts.month`（最宽窗口），同时显示 5h / 周 / 月三个口径，让用户一眼看到近期活跃度。

### 问题 3：jsdom 测 `openUpstreamModels` 不便

`openUpstreamModels` 是模块内私有函数，`lastSnap` 也是私有变量。直接 eval 整个 app.js 后无法注入 snapshot。

**解法**：冒烟测试改为**静态分析**：验证函数定义唯一、不再使用 `counts.total`、`cfg-linked-chip` 复用、reverse parse 用 `sort().join(" <-> ")`。9 项静态断言足够防止回归。

### 问题 4：Windows `Path.unlink` PermissionError

`_make_db` 创建的 sqlite 文件还开着，pytest 末尾 `Path(db).unlink(missing_ok=True)` 报 `WinError 32`。

**解法**：用 `os.unlink` 包 `_safe_unlink(path)` helper，try/except OSError 吞掉错误（DB 已关闭连接但偶尔 Windows 文件句柄延迟释放）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 虚拟 cfg fetch 失效 | `fetch_total_tokens_by_upstream` / `fetch_by_upstream_model` 重写：展平成员 + `name_to_owners` 反查 + 按 cfg.name 累加 | tui.py |
| #2 `counts.total` 不存在 | 聚合区改用 `counts["5h"] / .week / .month` 三档 + `total_tokens` | app.js |
| #3 jsdom 私有函数不可达 | 静态分析（fnCount / 字段引用 / 反向 parse 正则） | link_modal.js |
| #4 Windows 文件句柄未释放 | `_safe_unlink(path)` 包裹 `os.unlink` + try/except | test_virtual_upstream_stats.py |

**关键 fetch 重写（tui.py）：**

```python
def fetch_total_tokens_by_upstream(db_path, *, upstreams):
    # 向后兼容：list[str] 直接当 real cfg 处理
    if upstreams and isinstance(upstreams[0], str):
        names = list(upstreams)
        virtual_pairs = {}
    else:
        real_names = []
        virtual_pairs = []  # (cfg_name, [real_name, ...])
        for cfg in upstreams:
            if cfg.get("_is_virtual") and cfg.get("linked_names"):
                virtual_pairs.append((cfg["name"], list(cfg["linked_names"])))
                real_names.extend(cfg["linked_names"])
            else:
                real_names.append(cfg["name"])
        names = list(dict.fromkeys(real_names))  # 去重保序

    if not names:
        return {}

    ph = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT upstream, COALESCE(input_tokens,0)+COALESCE(output_tokens,0) "
        f"FROM requests WHERE upstream IN ({ph})", names).fetchall()

    bucket = {n: 0 for n in names}
    for up, t in rows:
        bucket[up] += int(t or 0)

    # 虚拟 cfg 输出 = 成员之和
    out = {}
    for n in names:
        out[n] = bucket[n]
    for cfg_name, members in virtual_pairs:
        out[cfg_name] = sum(bucket.get(m, 0) for m in members)
    return out
```

`fetch_by_upstream_model` 同样模式：展平成员 → `name_to_owners` 反查 → 每行按所有 owner cfg 累加。

**关键 modal 渲染分支（app.js）：**

```js
function openUpstreamModels(name) {
  const snap = lastSnap;
  const groups = (snap && snap.link_groups) || [];
  const isVirt = name.indexOf("<->") >= 0;
  let members = null;
  if (isVirt) {
    for (const g of groups) {
      if (Array.isArray(g) && g.length >= 2 &&
          g.slice().sort().join(" <-> ") === name) {
        members = g; break;
      }
    }
  }
  title.textContent = isVirt
    ? "按模型拆分（链接上游）：" + name
    : "按模型拆分：" + name;
  body.innerHTML = (isVirt && members)
    ? renderLinkedUpstreamHTML(name, members, snap)
    : renderPlainUpstreamHTML(name, snap);
  overlay.classList.add("open");
}
```

**关键 CSS 增量（styles-20260817.css）：**

9 个 class，全部用现有 `--panel-alt` / `--muted` / `--border` 变量：
- `.upstream-link-header` —— flex header（label + chip）
- `.upstream-link-label` —— 12px muted
- `.upstream-link-aggregate` —— 4-stat 行（panel-alt 底）
- `.upstream-link-stat b` —— 加粗 + tabular-nums
- `.upstream-link-members` —— 成员分块容器
- `.upstream-link-member` —— 边框 + padding
- `.upstream-link-member-head` —— flex space-between（成员名 + 子统计）
- `.upstream-link-member-stats` —— 11px muted tabular-nums
- `.upstream-link-member-table` —— 12px 模型表

---

## 6. 是否完全遵循规划路径开发

**基本按规划（核心功能完全遵循；一处关键 bug 发现后扩范围修复）。**

### 完全按规划：

- modal 顶部「属于上游：A <-> B <-> C」chip（紫底 🔗，复用 `.cfg-linked-chip`）—— 沿用现有 CSS，零新 chip 样式。
- 聚合统计（调用次数 5h/周/月 + 累计 token）—— 字段引用从 `by_upstream[virtual].{total_tokens, counts}` 取。
- 按成员分块（每真实上游一节，含成员自己的 token / call 子统计）—— 用 `link_groups` + `by_upstream_model[realMember]`。
- 模型表沿用原列结构（模型 / 请求 / 错误 / tokens / 加权 cost）—— `renderModelRow` helper 抽出复用。
- 普通上游走原路径（无「属于上游」、无聚合行）—— `renderPlainUpstreamHTML` 沿用 v0.119 表格逻辑。
- HTML 不动（占位由 JS append 到 `#upstream-models-body` 顶部）。
- 资源版本：`index.html` / `live_panel.html` CSS cache `?v=20260823-34`、JS cache `?v=20260823-54`。

### 偏离 / 扩范围之处：

- **(a) 后端 fetch_* 重写（扩范围）**：plan 写「不改后端任何逻辑（数据已具备）」，但实际实现 modal 后发现**数据并不具备**——`fetch_total_tokens_by_upstream` / `fetch_by_upstream_model` 在虚拟 cfg 下输出 0（致命 bug）。Agent 验证时发现，**扩范围修复**：两个 fetch 重写，模式与 v0.119 的 `fetch_by_upstream_with_costs` 对齐（展平成员 + 反查 + 累加）。**实现与 v0.119 同方向，只是两个 fetch 漏改**。

- **(b) `counts.total` 不存在（plan 错误）**：plan 文档假设 `by_upstream[virtual].counts.total` 存在，实际后端只填 `5h / week / month`。修正：聚合区显示三档（5h / 周 / 月）+ 累计 token，与用户「总 token 或调用次数」需求兼容（实际给了更详细版本）。

- **(c) 「属于上游」位置**：plan 写「modal 顶部新增一行」，实际作为「属于上游」chip + 「聚合统计」面板一同放在 modal 标题下方、模型表格上方，符合用户「顶部显示属于上游，下面列出模型」语义。

- **(d) 测试**：plan 写「不写新测试」，实际后端 bug 修复需要回归测试，加上 jsdom 静态冒烟（9 项）保证前端无回归。新增 `tests/test_virtual_upstream_stats.py`（5 项）和 `AppData\Local\Temp\lp_harness\link_modal.js`（9 项）。

### 重大调整：无（方向完全正确，只是 plan 漏估了后端 fetch_* 的 bug 范围）。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/tui.py`**（核心 fix）：
   - `fetch_total_tokens_by_upstream(db_path, *, upstreams)` 重写：支持 `list[str] | list[dict]`；虚拟 cfg 走「展平成员 → SQL IN → 按 cfg.name 累加」；`output_tokens + input_tokens` 求和；返回 `{cfg_name: total_tokens}`。
   - `fetch_by_upstream_model(db_path, *, upstreams)` 重写：同样展平成员模式；`name_to_owners` 反查；每行按所有 owner cfg 累加 model row；`cost` + `weighted_cost` 也累加；返回 `{cfg_name: {model: row}}`。

2. **`src/relay/gui.py`**（caller 适配）：
   - `_rebuild_snapshot` 中 `fetch_total_tokens_by_upstream` 的 caller 改为传 `upstream_cfgs_for_total`（含 `_is_virtual` + `linked_names` 字段）。
   - `fetch_by_upstream_model` caller 无需改（已传 cfg dicts）。

### 前端

3. **`src/relay/web/app.js`**（modal 重写）：
   - `openUpstreamModels(name)` 重写：识别 `isVirt = name.includes("<->")`；从 `lastSnap.link_groups` 反向解析 members；标题区分「按模型拆分（链接上游）」vs「按模型拆分」；body 走 `renderLinkedUpstreamHTML` 或 `renderPlainUpstreamHTML`。
   - `renderLinkedUpstreamHTML(name, members, snap)` 新增：拼 header + aggregate + members 三段 HTML。
   - `renderAggregateHTML(upData, counts)` 新增：4 个 stat（5h / 周 / 月 调用 + 累计 token）。
   - `renderPlainUpstreamHTML(name, snap)` 新增：原表格渲染（向后兼容）。
   - `renderModelRow(model, d)` helper 新增：两个分支共用模型行渲染。

4. **`src/relay/web/styles-20260817.css`**（9 个新 class）：
   - `.upstream-link-header` / `.upstream-link-label`
   - `.upstream-link-aggregate` / `.upstream-link-stat b`
   - `.upstream-link-members` / `.upstream-link-member`
   - `.upstream-link-member-head` / `.upstream-link-member-stats`
   - `.upstream-link-member-table`
   - 全部用现有 `--panel-alt` / `--muted` / `--border` 变量，零新色板。

5. **`src/relay/web/index.html`**：
   - CSS cache `?v=20260823-33` → `?v=20260823-34`
   - JS cache `?v=20260823-53` → `?v=20260823-54`

6. **`src/relay/web/live_panel.html`**：
   - CSS cache `?v=20260823-33` → `?v=20260823-34`

### 测试

7. **`tests/test_virtual_upstream_stats.py`**（**新文件，5 个测试**）：
   - `test_total_tokens_virtual_aggregates_members` —— 虚拟 cfg = 成员之和（修复后端 bug 的核心测试）。
   - `test_total_tokens_real_unchanged` —— 真实 cfg 行为不变。
   - `test_by_upstream_model_virtual_aggregates_members` —— 虚拟 cfg 按 cfg.name 聚合 model breakdown（含 cost）。
   - `test_by_upstream_model_real_unchanged` —— 真实 cfg 行为不变。
   - `test_total_tokens_with_string_list_still_works` —— 向后兼容 `list[str]`。

8. **`AppData\Local\Temp\lp_harness\link_modal.js`**（**新文件，9 项 jsdom 静态断言**）：
   - L1-L5：`openUpstreamModels` / `renderLinkedUpstreamHTML` / `renderPlainUpstreamHTML` / `renderAggregateHTML` / `renderModelRow` 函数定义唯一。
   - L6：不再使用 `counts.total`（改用 5h/week/month）。
   - L7：`renderLinkedUpstreamHTML` 用 `.cfg-linked-chip`。
   - L8：反向解析用 `.slice().sort().join(" <-> ")`。
   - L9：app.js 可被 eval（无 JS error）。

### 行为验收清单（手动测试项）

- [ ] 总览页找到链接上游条目（name 含 `<->`）→ 点击 → modal 顶部出现「属于上游：A <-> B <-> C」chip（紫底 🔗）
- [ ] 紧下方出现聚合统计：5h / 周 / 月调用次数 + 累计 token 数字与 overview 卡一致
- [ ] 模型表格按成员分块：每个真实上游一节，每节小标题显示成员 chip + 该成员自己的 token / call 子统计
- [ ] 跨成员同名模型各自出现在对应成员块下（不合并）
- [ ] 非链接上游（普通）走原路径：仅模型表格，无「属于上游」、无聚合行（向后兼容 v0.119）
- [ ] overview 卡与 modal 数字一致（同一 `by_upstream[name].{counts, total_tokens}` 数据源）
- [ ] 路由 / active / 计费 / quota 配置独立（链接组不互相影响，沿用 v0.119）
- [ ] 旧的调用方式 `fetch_total_tokens_by_upstream(db, upstreams=["alpha"])` 仍能跑

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/tui.py` | 改（+约 50 行：`fetch_total_tokens_by_upstream` / `fetch_by_upstream_model` 重写） |
| `src/relay/gui.py` | 改（+约 5 行：caller 适配传 cfg dicts） |
| `src/relay/web/app.js` | 改（+约 110 行：`openUpstreamModels` 重写 + 5 个 helper 新增） |
| `src/relay/web/styles-20260817.css` | 改（+约 60 行：9 个 `.upstream-link-*` class） |
| `src/relay/web/index.html` | 改（CSS/JS cache 版本 bump） |
| `src/relay/web/live_panel.html` | 改（CSS cache 版本 bump） |
| `tests/test_virtual_upstream_stats.py` | **新增**（5 个测试） |
| `AppData\Local\Temp\lp_harness\link_modal.js` | **新增**（9 项 jsdom 静态断言） |

---

# 链接上游支持跨平台同名 cfg（v0.120 续篇）开发文档

## 1. 用户的初始指令

> 链接了 e170 和 e170-openai，好像没什么变化啊

补充：用户在设置页尝试链接 `anthropic/e1701fa6` 和 `openai/e1701fa6`（同一名字，跨平台），总览页没有任何变化。

> 进入无人值守，需要采用你认为合理的方式自行完成所有目标。无需再询问

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 跨平台同名 cfg 应能链接 | 用户报告（隐含） |
| B | `openLinkUpstreamOverlay` modal 应跨平台搜索 | 根因定位 |
| C | 链接列表显示平台标签（如 `[openai]`）区分同名 cfg | 设计自决 |
| D | modal hint 文案去掉「同平台」字眼 | 设计自决 |
| E | 后端 `resolve_links` 已支持跨平台（`upstreams_for()` 不带参数返回全部），无需改 | 验证后端 |
| F | 数据契约 `cfg.name` 不带平台前缀，跨平台同名 cfg 在 `resolve_links` 里自然 union | 现有实现 |

### 隐含但需要确认的点（已通过验证）

- **后端是否需要改**：`resolve_links` 在 `gui.py:2901` 调 `self.settings.upstreams_for()`（无 platform 参数），而 `upstreams_for()`（`config.py:734`）无参时返回 `anthropic + openai` 合并列表 → **后端早已支持跨平台**。问题只在 modal 没暴露跨平台 peer。
- **跨平台 cfg 是否实际存在**：v0.12.1 起 upstreams.json 是单池（`upstreams: [{...}, ...]`），每条 cfg 只有一个 name，平台由 `wire` 字段决定。所以「同 name 跨 platform」是合法状态（e.g. `e1701fa6` 在 anthropic 和 openai 各有一条）。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位问题

```
upstreams.json
├─ anthropic.upstreams: [{name: "e1701fa6", ...}, {name: "f3af39d7", ...}, ...]
└─ openai.upstreams: [{name: "e1701fa6", ...}, {name: "default", ...}, ...]
```

用户想链接 `anthropic/e1701fa6` 和 `openai/e1701fa6`。点击「+ 新建链接」时：

```js
// v0.119 原版 openLinkUpstreamOverlay
const ups = (snap.upstreams && snap.upstreams[plat]) || [];  // plat=anthropic
for (const u of ups) {
  if (!u || !u.name || u.name === selfName) continue;
  peers.push(u);  // 只看 anthropic 平台
}
// 结果：peers 只含 anthropic 的其它 cfg（不含 openai/e1701fa6）
// 同平台无其它可链接 → 直接提示「同平台下没有其它上游可链接」
// 用户根本看不到 openai 的 e1701fa6
```

### 第二阶段：设计修复

跨平台搜索 —— 遍历**所有平台**的 `snap.upstreams`：

```js
for (const [p, ups] of Object.entries(snap.upstreams || {})) {
  for (const u of (ups || [])) {
    if (!u || !u.name) continue;
    if (u.name === selfName && p === plat) continue;  // 排除自己
    peers.push({ ...u, _platform: p });
  }
}
```

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `openLinkUpstreamOverlay` 改跨平台搜索 | 无 |
| 2 | 链接 row 显示 `[platform]` 标签（仅跨平台时） | #1 |
| 3 | modal hint 文案去掉「同平台」 | #1 |
| 4 | `.cfg-link-row-platform` CSS class 新增 | #2 |
| 5 | 资源版本 bump | #1-#4 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 跨平台 vs 同平台 | 全部跨平台（单一搜索路径） | 用户需求是「能链接跨平台」，单一路径实现简单；同平台自然被包含 |
| 平台标签 | 仅跨平台时显示 `[openai]` 等 | 同平台不显示（冗余），跨平台显示区分同名 cfg |
| `_platform` 数据来源 | 从 `Object.entries(snap.upstreams)` 的 key 取 | snapshot 已经按 platform 分组 |
| 后端是否改 | 不改 | 验证后端 `resolve_links` 早已跨平台（`upstreams_for()` 无参合并 anthropic + openai） |
| 测试 | 不加 | 修复是单点 modal 搜索逻辑，jsdom 测 1 个函数即可 |

---

## 4. 实现中遇到的问题

### 问题 1：用户报告前没意识到跨平台是设计遗漏

v0.119 文档里 `openLinkUpstreamOverlay` 明确写「只列同平台其它上游」，**故意限制**。当时设计理由是「防止 quota 跨平台误合并」。

但 v0.12.1 起 upstreams.json 是单池（不分平台），cfg.name 不带平台前缀 —— 同名 cfg 跨平台是合法且常见状态（很多用户在 anthropic / openai 都配同名 minnimax 镜像）。v0.119 的「同平台限制」实际是过度保护。

**解法**：去掉同平台限制；跨平台同名的 cfg 应能链接（与同平台链接行为一致：union-find 闭包、SQL `WHERE upstream IN (...)` 合并、不影响路由/计费/active）。

### 问题 2：跨平台同名 cfg 的 `_primary` 取谁

`_make_virtual_cfg`（`link_resolver.py:138`）按字典序取 `primary_name`，quota / billing / multipliers 沿用 primary。

跨平台同名 cfg 的 `_primary` 仍是按字典序选（不区分平台）。如果两个 cfg name 完全相同（`e1701fa6` / `e1701fa6`），字典序无法区分 —— 用 `Object.entries` 的迭代序或 `cfg_by_name` 第一个出现的为准。

**当前实现**：virtual cfg.name = `"e1701fa6 <-> e1701fa6"`（同名重复），quota 沿用第一个成员的。

**潜在问题**：UI 显示「`e1701fa6 <-> e1701fa6`」有点丑（重复名）。但用户既然这么配就是他的意图，不主动改。

### 问题 3：modal 列表里的 `_platform` 怎么传到 save

**验证链路**：
1. modal `cfg-link-cb` 加 `data-platform` 属性（仅记录，不影响 save）
2. onPick 回调只传 `cb.value`（= cfg.name）
3. save 时 `card.querySelectorAll(".cfg-linked-chip")` 收集 `data-peer` → `linked_upstreams: [...]`
4. **save 完全不关心 platform**，因为 `linked_upstreams` 是 cfg.name 列表（不带平台）；后端 `resolve_links` 自动按 cfg.name union

**结论**：modal 不需要把 platform 传到 save，只在 UI 显示时区分同名 cfg 即可。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 跨平台限制过度 | modal 改为跨平台搜索 | app.js |
| #2 虚拟名重复 | 不解决（用户意图） | — |
| #3 platform 不传 save | modal 只在 UI 用，不影响 save 链路 | app.js |

**关键 modal 修复（app.js）：**

```js
function openLinkUpstreamOverlay({ platform, selfName, existing, onPick }) {
  const snap = lastSnap || {};
  const peers = [];
  const plat = platform || "";
  // 跨平台搜索 —— 遍历所有平台
  const allPlats = snap.upstreams || {};
  for (const [p, ups] of Object.entries(allPlats)) {
    for (const u of (ups || [])) {
      if (!u || !u.name) continue;
      if (u.name === selfName && p === plat) continue;  // 排除自己（同平台）
      peers.push({ ...u, _platform: p });
    }
  }
  // ... 渲染时 u._platform !== plat 时显示 [platform] 标签
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无偏离）。**

### 完全按规划：

- **跨平台搜索**：modal 遍历 `Object.entries(snap.upstreams)`，所有平台的 cfg 都进入 peer 列表。
- **平台标签**：仅 `_platform` 与当前 card.platform 不同时显示 `[platform]`，同平台不显示冗余。
- **后端不改**：验证 `resolve_links`（`gui.py:2901`）已跨平台（`upstreams_for()` 无参合并），无需改后端。
- **数据契约不变**：`linked_upstreams` 仍是 cfg.name 列表（不带平台前缀）；union-find 自动按 cfg.name union。
- **资源版本**：app.js ?v=20260823-54 → ?v=20260823-55。

### 偏离之处：

- **(a) 验证后端**：plan 没明确「先验证后端是否已支持」，实际先 grep `resolve_links` + `upstreams_for()` 确认后端 OK 才改前端。**验证流程，非方向偏离**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

无改动（验证后端 `resolve_links` 早已跨平台）。

### 前端

1. **`src/relay/web/app.js`**（`openLinkUpstreamOverlay` 改写）：
   - peer 搜索改为 `Object.entries(snap.upstreams)` 跨平台遍历。
   - 排除自己用 `(u.name === selfName && p === plat)` 双条件（同名 cfg 跨平台不算自己）。
   - 每个 peer 加 `_platform` 字段（用于 UI 标签）。
   - modal row 渲染：跨平台时显示 `<span class="cfg-link-row-platform">[${platform}]</span>`。
   - modal hint 文案改为「选择要链接到 **X** 的其它上游」（去掉「同平台」字眼）。

2. **`src/relay/web/styles-20260817.css`**（新增 1 个 class）：
   - `.cfg-link-row-platform` —— 10px muted, 4px margin-left, opacity 0.7。

3. **`src/relay/web/index.html`**：
   - app.js ?v=20260823-54 → ?v=20260823-55。

### 测试

未新增测试（修复是单点 modal 搜索逻辑；现有 `test_link_resolver.py` 已覆盖后端跨平台 union-find 行为）。

### 行为验收清单（手动测试项）

- [ ] 设置页 → anthropic/e1701fa6 → 链接上游区点「+ 新建链接」→ modal 列表能看到 openai/e1701fa6（标注 `[openai]`）
- [ ] modal 列表同时显示同平台其它 cfg（如 anthropic/f3af39d7，不带平台标签）
- [ ] 勾选 openai/e1701fa6 → 确定 → chip 显示「🔗 e1701fa6」（不带平台标签，因为 chip 只存 peer name）
- [ ] 点保存 → upstreams.json 中 anthropic/e1701fa6 的 `linked_upstreams: ["e1701fa6"]`
- [ ] 总览页刷新后出现虚拟卡 `e1701fa6 <-> e1701fa6`（紫色 🔗 chip）
- [ ] 点击虚拟卡 → modal 顶部「属于上游：e1701fa6 <-> e1701fa6」+ 按成员分块（两节，名字相同但成员标签区分）
- [ ] 老的同平台链接（A+B）行为完全不变（向后兼容 v0.119）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/app.js` | 改（+约 15 行：`openLinkUpstreamOverlay` 跨平台搜索） |
| `src/relay/web/styles-20260817.css` | 改（+5 行：`.cfg-link-row-platform` 新 class） |
| `src/relay/web/index.html` | 改（app.js cache 版本 bump） |
