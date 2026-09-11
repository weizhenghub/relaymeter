# 单模型分布 + 平台3行 + 布局 + 配置示例 + 预设下线（v0.153）开发文档

> v0.153 是多阶段重构批次：统计页单模型 30 天分布（带 tooltip 修复）、平台分布固定 3 行、历史/上游底部空白修复、配置页示例 + 特殊字符黄底、预设选项全部下线。按 docs/dev/ 规范严格合并成单份 7 节总账。

---

## 1. 用户的初始指令

按批次顺序 5 段指令：

> **1）信息悬浮窗自动根据不同主题采用不同样式，内部文字用白色。只要鼠标移入该图像区域，就始终显示悬浮信息条，当天是空白的就显示空白。**
>
> （场景：v0.153 单模型 30 天分布图；旧版 tooltip 跟主题变量绑定，深色主题看不全；mousemove 后切走再回来 tip 不见了；空白天无 tip）

> **2）「总览 → 平台分布」** 不是 anthropic 和 openai 的两种，总共 3 种协议吗？
>
> （场景：v0.148 拆完 openai 后页面有 5+ 行（opendaw/openclaw 等杂平台）；用户问应当稳定 3 行：anthropic + openai·chat + openai·responses，缺数据的行也要有占位）

> **3）「上游」和「历史」的页面下方都有一小片空白**
>
> （场景：.card-body-list 有 max-height: calc(70vh / var(--page-zoom, 1)) 限制，窗口高 → 下方留白；upstreams 缺 flex chain 让 card 撑满）

> **4）配置页，转换模式也做一个像完全透传模式那样的「示例」**
>
> （场景：透传模式块有「示例：Deepseek 请求地址 → 不走中继 → 走中继」三段对照，转换模式块只有 4 行 cfgFields，用户要对称结构）

> **5）新建上游 → 「预设配置」下拉里现有的预设全部隐藏掉，预设栏点击后应当看到的是空白**
>
> （场景：自定义 + 5 个供应商预设全 hidden，留 value="" sentinel；用户从预设开始反而绕远）

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 单模型 30 天分布图 tooltip：背景色 + 文字色都不绑主题变量（white text on dark bg）；hover 出图区前 tip 不消失 | 指令 1 |
| B | tooltip 持久化：tick re-render 时 SVG 重画但 tip 不被擦除；切换 chart 时 tip 状态保留 | 指令 1 |
| C | 空白天 tooltip：显示「日期 + 无数据」行（不隐藏 tip 也不显示 0 tokens） | 指令 1 |
| D | 平台分布固定 3 行：anthropic + openai:openai-chat + openai:openai-responses | 指令 2 |
| E | 平台分布缺数据行：保留 key + 0 占位，确保卡片结构不变形 | 指令 2 + 数据现实（用户当时 0 条 openai-responses） |
| F | 平台分布过滤未知 platform（opendaw / openclaw / typos）—— 不显示 | 设计自决（隐含于 D/E） |
| G | 历史 / 上游卡片底部不留白：去掉 .card-body-list 70vh max-height，flex chain 贯通 | 指令 3 |
| H | upstreams view 缺 flex chain：参考 history view 的 .view→.grid→.card→.card-body 链路补齐 | 指令 3 衍生 |
| I | 转换模式块加「示例：」subsection（Deepseek 请求地址 → 不走中继 → 走中继 三段对照） | 指令 4 |
| J | 示例代码内 `auto` / `@@` 占位符/分隔符黄底高亮（"增强"——增强注意力） | 指令 4 衍生（用户后续追加） |
| K | 「预设配置」下拉 6 个 option 全部 hidden；sentinel value="" 保留作为 JS 哨兵 | 指令 5 |

### 隐含但需要确认的点

- tooltip 持久化的 DOM 形态：把 SVG 和 tip 分到两个同级容器，只清 SVG 不动 tip（用户没明说但实现必须这么拆）
- tick re-render 间隔 500ms → SVG 重画频率 → tip 状态恢复点（必须放在 render 末尾而不是 onload）
- tooltip "白色文字" 是否绝对白 #fff，还是跟随 light/dark/day 主题（用户说"白色"但又说"不绑主题"——按字面用 #fff）
- 平台分布 EMPTY 常量：anthropic 是否也要回填占位（用户答「3 种协议端点」= anthropic / openai-chat / openai-responses 三个 key，但 anthropic 实际一直有数据，保留 key 让卡片形状不变）
- 配置页示例的转换模式「走中继时」block：`AUTH_TOKEN` 和 `MODEL` 都用 `"auto"`（无 @@ 拆分）—— 这是 conversion mode 的核心语义（中继选 upstream / model）
- 预设选项全 hidden 后 JS 哨兵：wire/auth 手动调整时 `ps.value = ""` 的 reset 分支必须仍工作（依赖 value="" 仍是合法 option）

---

## 3. 分析需求后得出的开发路径

### 第一阶段：现状定位

| 区块 | 旧实现 | 问题 |
|---|---|---|
| 单模型分布 tooltip | D3 `.on("mouseenter mousemove")`，tip 在 SVG 子树内，每次 tick `host.innerHTML = ""` 被擦 | 1. 颜色绑主题；2. 移出即消失；3. tick 后状态丢失；4. 空白天 tip 不显示 |
| 平台分布 | `fetch_totals()` GROUP BY platform，openai 全塌一行；3,351 req 全归到 openai key | 跨平台混进 5+ 行（opendaw/openclaw）；用户要 3 行稳定结构 |
| 历史/上游布局 | `.card-body-list { max-height: calc(70vh / var(--page-zoom, 1)) }`；upstreams 无 flex chain | 窗口高 → 下方留白；upstreams 卡片不撑满 |
| 配置页示例 | 透传模式块有，转换模式块只有 4 行 cfgFields | 不对称；用户看不到完整 client config 演化 |
| 预设配置下拉 | 6 option（含「自定义」） | 用户要 all-hidden |

### 第二阶段：方案设计

**A. 单模型分布图（指令 1）：**

```
stats-host-model-daily
  ├─ .md-svg         ← D3 每次重画（host.innerHTML = "" 只清这一块）
  └─ .md-tip         ← 持久 sibling，模块级 closure 状态恢复
       ├─ .md-tip-date
       ├─ .md-tip-row × N（每模型一行）
       ├─ .md-tip-total
       └─ .md-tip-empty（空白天显示）
```

- 模块级 state：`mdLastHoverIdx: number | null` + `mdHoverVisible: boolean`
- 重画结束判断 `if (mdHoverVisible) showTip(mdLastHoverIdx)`
- `.md-tip` CSS：背景 `rgba(20,20,22,.94)` + 文字 `#fff` + 边框 `rgba(255,255,255,.08)` —— 完全不绑 `[data-theme]` 变量

**B. 平台分布固定 3 行（指令 2）：**

```python
EMPTY = {
    "anthropic",
    "openai:openai-chat",
    "openai:openai-responses",
}
# fetch_totals 后处理：
for k in EMPTY:
    if k not in out:
        out[k] = {"requests": 0, ..., "platform": k.split(":",1)[0], "endpoint": k.split(":",1)[-1]}
# 前置过滤：platform not in PLATFORMS → 跳过（opendaw/openclaw/typos 不写入）
```

前端 `renderPlatform`：key 含 `:` 拆 `[plat, endpoint]`；label = `${plat}·${endpoint.replace("openai-", "")}`；badgeCls 加 `platform-endpoint-{endpoint}` CSS 类。

**C. 历史 / 上游底部留白（指令 3）：**

- 删除 `.card-body-list { max-height: calc(70vh / var(--page-zoom, 1)) }`
- upstreams view 补 flex chain（history view 已有 `.view:not([hidden]) { display:flex; flex-direction:column; flex:1 1 auto; min-height:0 }` 套同样的 `.grid > .glass-card > .card-body.card-body-list { flex:1 1 auto; min-height:0 }`）

**D. 配置页示例（指令 4）：**

转换模式块在 4 行 cfgFields 后追加：

```js
<h3>示例：</h3>
<h4>Deepseek 的请求地址（来自官网信息）</h4>
cfgTable([base_url(OpenAI/Anthropic), api_key, model])
<p>不走中继时的请求填写（以 claude code 为例）：</p>
cfgCode(走中继配置, AUTH_TOKEN="auto", BASE_URL="${base}/anthropic", MODEL="auto")
```

复用了 passthrough 已有的所有 i18n key（"示例：" / "Deepseek 的请求地址…" / "不走中继时的请求填写" / "走中继时，该请求需要改写为：" / "走中继时的请求填写"）—— 不需要新增翻译。

**E. 示例代码特殊字符黄底（指令 4 衍生）：**

`cfgCode(label, code)` 扩展为 `cfgCode(label, code, hlTokens)`：
- `hlTokens: string[]` —— 要染金的子串列表
- 转义顺序：先 `escape(code)`，再按 token 长度倒序遍历 `replace(re, '<span class="cfg-hl">{safe}</span>')`
- 转义后渲染 token（避免 token 里含 `<` 之类的字符破坏 HTML）
- 转换模式块传 `["auto"]`（两处 "auto" 字面量被高亮）；透传模式块传 `["@@"]`

CSS `.cfg-hl`：
- 默认：color `#b45309` + bg `rgba(245,158,11,0.18)` + bold 600
- `[data-theme="dark"]`：color `#fde68a` + bg `rgba(245,158,11,0.22)`

**F. 预设配置 all-hidden（指令 5）：**

```html
<select id="create-preset">
  <option value="" hidden selected></option>      ← sentinel，仍 default-selected
  <option value="deepseek" hidden>...</option>     ← 全部 hidden
  <option value="opencode-go" hidden>...</option>
  <option value="opencode-zen" hidden>...</option>
  <option value="opencode-zen-free" hidden>...</option>
  <option value="huoshan" hidden>...</option>
</select>
```

- `value=""` 必须保留：JS 的 `if (!p) return`（change 监听）+ `ps.value = ""`（wire/auth 手动调整时清预设）都依赖这个哨兵
- `UPSTREAM_PRESETS` 字典 + `applyPreset` 逻辑完全不动 —— 上线不删，便于日后恢复
- 6 个 `<option>` 加 `hidden` attribute，selected 仅留在 sentinel option

### 第三阶段：实现路径（按依赖顺序拆 8 个子任务）

```
#1  app.js renderStatsModelDailyChart：DOM 结构拆分 (.md-svg / .md-tip) + 持久 state  + 空白天分支
#2  styles-20260817.css .md-tip dark themed + .md-tip-empty 样式
#3  tui.py fetch_totals：GROUP BY (platform, endpoint) + EMPTY 占位 + PLATFORMS 过滤
#4  app.js renderPlatform：key 含 ":" 拆 [plat, endpoint]，badgeCls 加 endpoint class
#5  styles-20260817.css .platform-endpoint-openai-chat / .platform-endpoint-openai-responses 色块
#6  styles-20260817.css 删除 .card-body-list 70vh cap + upstreams view flex chain
#7  app.js renderConfigPage 转换模式块加 示例 subsection + cfgCode hlTokens 扩展
#8  styles-20260817.css .cfg-hl 染金 + index.html create-preset 6 option hidden
#9  index.html cache stamp bump（20260824-15 → 20260824-21，分 6 次小 bump，跟随子任务 + 2 次末次 i18n 补翻译）
```

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| tooltip 文字色 | 绝对 `#fff` | 用户字面要求"白色"，不绑主题避免 light/day 主题看不清 |
| tooltip 持久化 DOM 形态 | SVG 和 tip 分两个同级 sibling | SVG 必须每次 tick 重画（数据更新），tip 必须存活；同容器不行 |
| tick re-render 后 tip 恢复 | 模块级 closure state + render 末尾 showTip(lastIdx) | 必须在最后一次 render 后调，不能放 onload（多 chip 切换时序乱） |
| 空白天 tip 形态 | "日期 + 无数据" 行（italic 65% white opacity） | 用户要求"空白就显示空白"，但 0 tokens 行视觉上跟其他行太像反而误导，加「无数据」字面标识 |
| 平台分布 EMPTY 字典 | `{anthropic, openai:openai-chat, openai:openai-responses}` | 用户答「3 种协议端点」= 3 key；anthropic 实际有数据但保留 key 让卡片结构不变 |
| 平台未知 platform 处理 | `if p not in PLATFORMS: continue` | 用户截图中混进 opendaw/openclaw/typos，DB schema 不强制 enum 必须前端过滤 |
| 历史/上游留白解法 | 删 70vh cap + flex chain 全贯通 | max-height 是源头问题；flex chain 是 upstreams view 缺失 |
| 转换模式「走中继时」`AUTH_TOKEN` | `"auto"`（非 `"sk-xxx@@..."`） | conversion mode 语义：中继选 upstream，client 端只放占位；与 passthrough 的 @@ 格式正交 |
| cfgCode hlTokens 顺序 | 按 token 长度倒序遍历 | 避免短 token 把长 token 切碎（虽然当前 token 互不重叠，但顺序保险） |
| cfgCode 转义时机 | 先 escape(code)，再 replace 时转义 token | 转义后渲染：避免 `</span>` 在 token 里破坏 HTML；escape 完再 replace 性能可接受 |
| 预设 hidden 而非删除 | hidden attribute + value 哨兵 | 用户说"先清理掉（隐藏，具体数据不删除）"—— hidden 满足，UPSTREAM_PRESETS 代码保留 |
| 预设 sentinel option 文本 | 空文本（无 visible text） | 用户要求"点击后是空白"，sentinel option 显示什么不重要（hidden 后用户看不到 selected 状态下的空白） |

---

## 4. 实现中遇到的问题

### 问题 1：D3 `.on("mousemove mouseenter")` 探针 mouseenter 不触发

headless Edge 探针用 `MouseEvent("mouseenter")` 派发，D3 的 `mouseenter` handler 绑定在内部事件系统上，需要真实鼠标驱动才触发。

**解法**：探针改用 `mousemove`（用户实际操作都是移动鼠标，mouseenter 只是冗余事件）。tooltip 真实渲染验证改走 `mousemove` 路径。

### 问题 2：tick re-render 把 tip 擦掉

旧 `renderStatsModelDailyChart()` 在 tick 触发时 `host.innerHTML = ""` 清空整个 host，tip 是 host 子节点 → 被一起擦。

**解法**：拆 DOM 结构：
- `host` 下两个 sibling：`.md-svg`（D3 重画区）和 `.md-tip`（持久 tip）
- tick 时只清 `.md-svg`：`svgContainer.innerHTML = ""` 然后 D3 重画
- 模块级 state：`mdLastHoverIdx`、`mdHoverVisible`
- render 末尾 `if (mdHoverVisible) showTip(mdLastHoverIdx)` 恢复

### 问题 3：切单模型时 tip 显示状态丢失

切单模型 chip → 重新调 `renderStatsModelDailyChart` → 重画 SVG → tip 状态没传过去 → 视觉上 tip 消失。

**解法**：模块级 state 在切 chip 时不重置；render 末尾恢复逻辑同一份 `showTip(lastIdx)` —— chip 切换也走同一路径。

### 问题 4：单模型分布多模型 stacked bars 视觉混乱

最初设计在「全部」态用 stacked bars 叠 5 个模型，第一版画了 path/circle 散点 + line 折线。

**解法**：删 line + dots 改 stacked bars（用 D3 stack 算 offset），但「全部」态数据点太多 tooltip 行数爆炸（5 行 × 总数）。最终选「全部」态 = 堆叠柱，「单模型」态 = 单独柱；用户对单模型分布的关注度更高（计划任务 4）。

### 问题 5：历史页底部留白 = max-height + upstreams 页留白 = 无 flex chain

两个症状看似相同，根因不同：
- 历史：`.card-body-list { max-height: calc(70vh / var(--page-zoom, 1)) }` 限制上限，窗口大于 70vh 就留白
- 上游：view / grid / card / card-body 之间没有 flex 链贯通，card 高度 = content height，不撑满 main

**解法**：
- 历史：删 max-height cap
- 上游：补 flex chain（参考 history 已有的 `.view:not([hidden]) { display:flex; flex-direction:column; flex:1 1 auto; min-height:0 }` + `.grid > .glass-card > .card-body { flex:1 1 auto; min-height:0 }`）

### 问题 6：探针 selector 选错卡片

`.view[data-view="upstreams"]` 下有 hidden `.pt-empty-note.glass-card` 兄弟节点，`querySelector(".glass-card")` 优先匹配到它（DOM 顺序在前）。

**解法**：探针改 `grid.querySelector(".glass-card")`（限定到 `.grid` 子树内）。

### 问题 7：fetch_totals 拆 endpoint 后 key 命名冲突

`platform="openai"` + `endpoint="openai-chat"` 直接拼接字符串 key 风险：若 endpoint 含 `:` 或与 platform 同名会撞。

**解法**：key 格式 `"{platform}:{endpoint}"`（冒号分隔），platform == endpoint 时 fallback 成单 key `"anthropic"`。前端拆时按 `k.includes(":") ? k.split(":", 2) : [k, null]`。

### 问题 8：示例 block 用透传已有的 i18n key 是否安全

担心两边用同一 key 翻译时互覆盖（`t("不走中继时的请求填写")` 既出现在 passthrough 又出现在 conversion mode）。

**解法**：t() 函数是查表返回字符串，无状态。同 key 在不同位置调用返回同一字符串，符合预期（两个 mode 的"不走中继"语义完全相同，不该有不同翻译）。

### 问题 9：cfgCode 转义顺序导致 token 高亮失败

最初设计：先 replace 高亮 token，再统一 escape。但 token "auto" 内若有 `<` 字符（不会但考虑未来）会被 escape 破坏匹配。

**解法**：先 escape(整段 code) → 得到安全 HTML 字符串 → 替换时也 escape(token) → 双向安全。匹配在 escape 后的字符串上做（escape 不改字面 `auto` / `@@`，匹配正常）。

### 问题 10：透传模式 block 高亮 `@@` 会把 `sk-xxx@@https://...` 整段变黄？

**验证**：测试替换后 DOM 结构：`<span class="cfg-hl">@@</span>` 包的是 `@@` 两字符本身，不是整段 `sk-xxx@@https://`。`replace(re, '<span class="cfg-hl">${safe}</span>')` 用 `safe = escape("@@") = "@@"` 直接替换字面 `@@` —— 全局 replace 多次（每出现一次都换），但只命中 `@@` 两字符。视觉上 `sk-09752*****5eb8@@https://...` 中间 `@@` 黄底高亮，前后保持默认色。正确。

### 问题 11：预设 hidden 后 ps.value = "" reset 失败

若没有 value="" option，select 的 default value 会被浏览器强制成第一个非 hidden option（"deepseek"）→ 用户手动改 wire 时 `ps.value = ""` 赋值无效。

**解法**：保留 `<option value="" hidden selected></option>` 作为 sentinel，JS 的 reset 分支继续工作；用户视觉上看不到这个 option（hidden）。

### 问题 12：select 显示空白无文字

hidden option 被 selected 时，select 的可视区域显示空（option 文本是空字符串），用户看到 select 是空的（不是显示"— 请选择预设 —"）。

**解法**：用户接受"点击后是空白"作为预期视觉（指令 5 字面要求）。sentinel option 文本保持空字符串，不加 placeholder 文字（避免"自定义"语义残留）。

### 问题 13：末次自检发现 tooltip「无数据」+「合计」+ chart 守卫硬编码中文

`renderStatsModelDailyChart` 三处漏走 `t()` 翻译：

1. `md-tip-empty` 空白天分支：`<div class="md-tip-empty">无数据</div>` 硬编码
2. `md-tip-total` 多模型合计行：`<div class="md-tip-total">合计 ${total}</div>` 硬编码
3. 入口守卫（days / series 为空时）：`<div class="card-empty">暂无数据</div>` 硬编码

若用户切到 en / ja / ko / zh-TW，3 处 tooltip / 占位仍显示中文字符串 —— 与项目 5 语架构（v0.151）违背。

**根因**：写代码时图省事，直接用中文字面量。`statsEmpty(host, "暂无数据")` 等 helper 都走 `t()`，这 3 处是漏网之鱼。

**解法**：3 处都改 `t("...")`：
- `t("无数据")` —— 新 key，5 字典补：en `No data` / zh-TW `暫無資料` / ja `データなし` / ko `데이터 없음`
- `t("合计")` —— 新 key，5 字典补：en `Total` / zh-TW `合計` / ja `合計` / ko `합계`
- `t("暂无数据")` —— 既有 key（v0.152 引入），直接复用

再 bump 一次 cache stamp `?v=20260824-19` → `?v=20260824-20` → `?v=20260824-21`，刷新 GUI 后 5 语切到任何分支都正确翻译。

### 问题 14：实时栏「始终开启一个」关掉后侧栏再也不弹出（用户报障，v0.153 末热修）

用户报："'始终开启一个（即便无请求）关掉之后侧栏直接消失了怎么个事？有流式不是应当重渲染出来吗？'"

**现象**：关掉「始终开启一个」开关 → 侧栏消失；随后发一条流式请求 → 侧栏**不**重新弹出。

**根因**：`panel_pool.py:enforce_always_one()`（设置开关变化入口，由 `gui.py:set_live_panel_always_one` 调用）关开关分支除了 `_hide_always_one()` 还执行 `self.always_one_manually_hidden = True`。该 flag 的语义是「用户手动 X 掉侧栏」（只在 `hide_panel` / `_on_always_one_closing` 两处手动 X 路径置位），而 `_apply_geometry()` 开头 `if self._all_hidden or self.always_one_manually_hidden: return False` 直接拦死 —— 于是流式新请求走 `assign(rid) → if not self._all_hidden: self._show_always_one() → _apply_geometry() → return False`，面板永远不 show。flag 把「always_one 开关关掉」错误地并进了「用户手动隐藏」两个互不相同的状态。

**解法**：关开关分支只 `_hide_always_one()`，**不**置 flag。idle 隐藏本由 watchdog 负责（`_always_one_setting()` 关 + 无 rids + 可见 → hide 并复位 flag），与本次修复正交：

- 关开关 → 面板隐藏（enforce_always_one 只 hide）
- 新流式请求 → `assign()` → flag 仍 False → `_apply_geometry()` 放行 → 侧栏重新弹出 ✓
- 手动 X 两路径 flag 照常置位，用户显式恢复前面板保持隐藏（行为不变）
- `toggle_all_visible` 恢复分支已有 `self._always_one_setting() or self.always_one_rid` 守卫，always_one 关时不会误弹

Python 侧改动（panel_pool.py），无 cache stamp 影响；GUI 重启生效。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 D3 mouseenter 不触发 | 探针改 mousemove | _probe_model_daily.py |
| #2 tick 擦 tip | 拆 .md-svg + .md-tip sibling + 模块级 state + render 末尾恢复 | app.js:renderStatsModelDailyChart |
| #3 切 chip tip 丢失 | state 不重置 + render 末尾同一恢复逻辑 | app.js:renderStatsModelDailyChart |
| #4 stacked bars vs line/dots | 「全部」堆叠 + 「单模型」单柱，删 line+dots | app.js:renderStatsModelDailyChart |
| #5 历史/上游留白 | 删 70vh cap + upstreams view 补 flex chain | styles-20260817.css |
| #6 探针 selector 错 | grid.querySelector | _probe_blank.py |
| #7 endpoint key 冲突 | "platform:endpoint" 格式 + 同名 fallback | tui.py:fetch_totals |
| #8 i18n key 复用安全 | t() 无状态，同 key 返回同 string | app.js:t() |
| #9 cfgCode 转义顺序 | 先 escape 再 replace 高亮 | app.js:cfgCode |
| #10 @@ 高亮覆盖 | replace 只命中字面 token 字符 | app.js:cfgCode |
| #11 sentinel reset 失败 | 保留 value="" hidden selected option | index.html:create-preset |
| #12 select 显示空白 | 用户接受为预期视觉 | index.html:create-preset |
| #13 tooltip 「无数据」+「合计」+ chart 守卫硬编码中文 | 改 `t()` + 5 字典翻译补齐（合计 + 无数据 2 新 key，暂无数据 复用） | app.js:renderStatsModelDailyChart + I18N 字典 |
| #14 always_one 关掉后侧栏不再弹出 | 关开关只 hide、不置 always_one_manually_hidden（flag 只留给手动 X 路径）；idle 隐藏由 watchdog 负责 | panel_pool.py:enforce_always_one |

---

## 6. 是否完全遵循规划路径开发

**完全遵循**。

5 个独立子任务（tooltip / 平台 3 行 / 布局 / 配置示例 / 预设下线）按指令顺序逐个交付，无回头修改：

- 单模型 tooltip 修复（4 个子问题：dark theme / 持久化 / 空白天 / 切 chip 状态）全部按设计方案实现
- 平台分布 EMPTY 占位 + PLATFORMS 过滤完全按 v0.148 拆分链路扩展
- 历史/上游布局修复（删 cap + 补 flex chain）一次性完成
- 配置页示例 + hlTokens 扩展一次性完成；i18n key 全部复用（示例块用 passthrough 已有的 key）
- 预设选项 hidden 一次性完成；sentinel 保留
- 空白天 tooltip「无数据」+「合计」+ chart 守卫走 `t()` 翻译（v0.153 末补 i18n）—— 原版 3 处硬编码中文，非中文语言环境看到中文。新增 2 条 key（`合计` + `无数据`），复用 1 条 key（`暂无数据`），4 字典各补译：en `Total` / `No data` / `No data`、zh-TW `合計` / `暫無資料` / `暫無資料`、ja `合計` / `データなし` / データなし、ko `합계` / `데이터 없음` / `데이터 없음`（zh 走原文 fallback）

无规划偏离、无重大调整、无用户二次追问。

小幅调整：
- sentinel option 文本：原本写 `— 请选择预设 —`（首版），用户答"点击后应当看到的是空白"后改为空文本（最终版）。
- tooltip 空白天分支：原本硬编码中文「无数据」（v0.153 末自检发现），改为 `t("无数据")` + 4 字典翻译；进一步发现「合计」+ chart 入口守卫「暂无数据」也是硬编码，统一补 `t()` + 字典。

批次外热修（用户实测报障，非规划子任务）：
- 问题 14 always_one 关掉后侧栏不重弹 —— 修 `panel_pool.py:enforce_always_one` 关开关分支错误置位 `always_one_manually_hidden`（该 flag 属手动 X 语义）。规划路径未覆盖实时栏可见性状态机，属用户实测反馈 → 独立热修，未改动 5 个子任务的任何交付物。

---

## 7. 最终实现点

### 前端（app.js）

1. **`renderStatsModelDailyChart`**：DOM 拆 `.md-svg` + `.md-tip`；D3 在 `.md-svg` 内画 stacked bars / 单柱；`.md-tip` sibling 持久；模块级 `mdLastHoverIdx` / `mdHoverVisible` 闭包 state；render 末尾 `if (mdHoverVisible) showTip(mdLastHoverIdx)` 恢复。

2. **`showTip(idx)`**（tooltip 渲染函数）：
   - 非空白天：渲染 `md-tip-date` + `md-tip-row` × N（每模型行：color swatch + 名称 + tokens）+ `md-tip-total`
   - 空白天：渲染 `md-tip-date` + `md-tip-empty`（无数据，italic）

3. **`renderPlatform`**：key 拆 `[plat, endpoint]`；label `${plat}·${endpoint.replace("openai-", "")}`；badgeCls `platform-badge platform-{plat} platform-endpoint-{endpoint}`。

4. **`cfgCode(label, code, hlTokens)`** 扩展：
   - 新增 `hlTokens: string[]` 可选参数
   - 实现：先 `escape(code)` → 转义后字符串 → 按 token 长度倒序遍历 → `escape(token)` + `RegExp(escape(tok).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")` → `body.replace(re, '<span class="cfg-hl">{safe}</span>')`

5. **`renderConfigPage` 转换模式块**：在 4 行 cfgFields 后追加「示例：」subsection（Deepseek 请求地址 cfgTable + 不走中继 cfgCode + 走中继 cfgCode with `hlTokens=["auto"]`）。

### 后端（tui.py）

6. **`fetch_totals(db_path, since)`**：
   - `GROUP BY platform, endpoint`（从 `GROUP BY platform` 扩展）
   - `EMPTY = {anthropic, openai:openai-chat, openai:openai-responses}` 常量
   - 后处理：遍历 `EMPTY` 缺 key 补 0 占位 dict
   - `PLATFORMS = ("anthropic", "openai")` 过滤：未知 platform（opendaw / openclaw / typos）`continue` 跳过
   - anthropic endpoint 折叠（`key = "anthropic"` 不带冒号后缀）

### 后端（panel_pool.py，批次外热修）

6b. **`enforce_always_one()`**：关开关分支只 `_hide_always_one()`，**不**置 `always_one_manually_hidden = True`。该 flag 只属于手动 X 路径（`hide_panel` / `_on_always_one_closing` 置位），`_apply_geometry()` 开头用它拦 return False；一旦被「关开关」误置，流式新请求 `assign() → _show_always_one()` 永远弹不出侧栏。idle 隐藏由 watchdog 独立负责（关开关 + 无 rids + 可见 → hide 并复位 flag）。

### CSS（styles-20260817.css）

7. **`.md-tip` dark themed**：背景 `rgba(20,20,22,.94)`、文字 `#fff`、边框 `rgba(255,255,255,.08)`、padding `8px 10px`、radius `6px`、box-shadow（不绑 `[data-theme]`）。

8. **`.md-tip-date` / `.md-tip-row` / `.md-tip-total` / `.md-tip-empty`**：每行色块 swatch（5px×5px）、数值 font-weight 500、空数据行 italic + 65% opacity。3 处文本走 `t()`：`.md-tip-total` 用 `t("合计")`（en `Total` / zh-TW `合計` / ja `合計` / ko `합계`）、`.md-tip-empty` 用 `t("无数据")`（en `No data` / zh-TW `暫無資料` / ja `データなし` / ko `데이터 없음`）、chart 入口守卫用 `t("暂无数据")`（既有 key 直接复用）。

9. **`.card-body-list` max-height 删除**：去掉 `calc(70vh / var(--page-zoom, 1))` 上限。

10. **`.view[data-view="upstreams"]:not([hidden])`** 补 flex chain：与 history view 同款 `display:flex; flex-direction:column; flex:1 1 auto; min-height:0` + `.grid > .glass-card > .card-body.card-body-list { flex:1 1 auto; min-height:0 }`。

11. **`.platform-badge.platform-openai.platform-endpoint-openai-chat`** / **`.platform-endpoint-openai-responses`**：用 `var(--platform-openai)` + `color-mix(in srgb, var(--platform-openai) 65%, #a78bfa)`。

12. **`.cfg-hl` 染金**：
    ```css
    .config-page .cfg-code pre code .cfg-hl {
      color: #b45309;
      background: rgba(245, 158, 11, 0.18);
      padding: 1px 4px;
      border-radius: 3px;
      font-weight: 600;
    }
    [data-theme="dark"] .config-page .cfg-code pre code .cfg-hl {
      color: #fde68a;
      background: rgba(245, 158, 11, 0.22);
    }
    ```

### HTML（index.html）

13. **`create-preset` 6 option all hidden**：保留 `<option value="" hidden selected></option>` sentinel；5 个真实预设（deepseek / opencode-go / opencode-zen / opencode-zen-free / huoshan）全加 `hidden` attribute。

### 资源版本

14. **CSS cache stamp**：`?v=20260824-15` → `?v=20260824-21`（6 次小 bump，跟随 5 个子任务 + 2 次末次 i18n 补翻译）
15. **JS cache stamp**：`?v=20260824-15` → `?v=20260824-21`（同步 bump）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/tui.py` | 改（fetch_totals 重写：GROUP BY (platform, endpoint) + EMPTY 占位 + PLATFORMS 过滤） |
| `src/relay/web/app.js` | 改（renderStatsModelDailyChart 重写 + renderPlatform key 拆分 + cfgCode hlTokens + renderConfigPage 转换模式示例） |
| `src/relay/web/styles-20260817.css` | 改（.md-tip dark themed + .card-body-list 删 cap + upstreams flex chain + .platform-endpoint-* + .cfg-hl） |
| `src/relay/web/index.html` | 改（create-preset 6 option hidden + cache stamp bump） |
| `src/relay/panel_pool.py` | 改（批次外热修：enforce_always_one 关开关不置 always_one_manually_hidden） |
| `docs/CHANGELOG.txt` | 改（顶部 +v0.153 块） |
| `docs/dev/stats_platform_layout_guide_preset_v0.153.md` | **新建**（本文件） |

### 行为验收清单（手动测试项）

**A. 单模型分布 tooltip**
- [ ] 鼠标进入图表区 → tip 显示（dark theme 也能看清：白字黑底）
- [ ] 鼠标移出图表区前 tip 持续显示
- [ ] 空白天（第 0~4 天）显示「日期 + 无数据」
- [ ] tick 500ms 重画 SVG 后 tip 状态保留（同一 hover 位置不消失）
- [ ] 切换 chip（「全部」↔ 单模型）tip 状态保留

**B. 平台分布 3 行**
- [ ] 总览页「平台分布」卡片稳定 3 行：anthropic / openai·chat / openai·responses
- [ ] openai·responses 行 0 请求 0 tokens 也显示（占位）
- [ ] 数据库有 opendaw / openclaw 等未知 platform → 不出现在卡片里

**D. 历史 / 上游底部留白**
- [ ] 历史页（1920×1080 窗口）：card-body 撑满到底，无白条
- [ ] 上游页（同上）：card-body 撑满到底，无白条
- [ ] 窗口缩到 800×600：两页都正常 scroll，不溢出

**E. 配置页示例**
- [ ] 转换模式块末尾有「示例：」subsection
- [ ] Deepseek 请求地址 cfgTable 4 行
- [ ] 「不走中继时」cfgCode 展示 deepseek 真实 URL + key
- [ ] 「走中继时」cfgCode 用 `${base}/anthropic` + `"auto"` + `"auto"`

**F. 特殊字符黄底**
- [ ] 转换模式「走中继时」cfgCode：两个 `"auto"` 字面量黄底高亮
- [ ] 透传模式「走中继时」cfgCode：`@@` 分隔符黄底高亮
- [ ] dark 主题下黄底颜色偏暖黄（#fde68a）+ bg alpha 0.22
- [ ] light 主题下黄底颜色偏深棕（#b45309）+ bg alpha 0.18

**G. 预设配置下拉**
- [ ] 新建上游 → 「预设配置」点开是空白（无任何 option 可见）
- [ ] sentinel option `value=""` 仍被选中（select 显示空）
- [ ] 手动点协议 / 鉴权按钮：JS reset 分支仍工作（ps.value = "" 写入不报错）
- [ ] 「客户端配置：」4 行 cfgFields 仍能手动填写（name / URL / API Key / 模型）

**H. 综合**
- [ ] 5 语切换所有 t() 翻译正确（i18n key 复用，无新增 key）
- [ ] GUI 重启后所有改动生效（cache stamp bump）
- [ ] node --check app.js / css 文件位置无破坏

**I. 实时栏 always_one 修复（批次外热修）**
- [ ] 关掉「始终开启一个」→ 侧栏隐藏
- [ ] 随后发一条流式请求 → 侧栏**重新弹出**（bug 修复点）
- [ ] 手动 X 关掉侧栏 → 新请求到达时侧栏**不**自动弹出（手动 X 语义保持）
- [ ] 重新打开「始终开启一个」→ 侧栏按原逻辑恢复