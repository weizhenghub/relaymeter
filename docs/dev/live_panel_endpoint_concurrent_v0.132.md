# 实时流侧栏端点容器改并发列表 + 容器架构清单（v0.132）开发文档

## 1. 用户的初始指令

> 首先把上面这个表格完整的写入文档。然后修改端点数据容器，改为并发请求全部显示在其中。

「上面这个表格」= 实时流侧栏当前容器架构清单（见第 2 节）。

---

## 2. 侧栏容器架构清单（v0.130+ 动态模型）

侧栏（实时流子窗口）的容器由 `live_panel.html` 静态骨架 + `live_panel.js` 动态创建/布局构成。

| 容器 | 类型 | 何时显示 |
|---|---|---|
| **端点数据容器** `.live-panel-endpoint` | 静态单例 | 永远 col1 顶。v0.132 起：顶部全局（最新活跃 rid 上游 + phase 徽标）+ 中间**并发请求列表**（每个请求一行：平台·模型 + phase + 入出向，超高 30vh 滚动）+ 底部 api-key（全局）。v0.130-131 是单请求 5 字段 |
| **带思考流容器** `.live-panel-thinkstream` | JS 动态创建 | 请求有 think 流（`thinking_text` 非空）时建，含统计+思考区+正文区三区，自然高 50vh |
| **纯正文容器** `.live-panel-puretext` | JS 动态创建 | 首事件仅正文流（无 think）时建，含统计+正文区，自然高 33vh |
| **工具调用容器** `#lp-tools-wrap` | 静态单例（内容 JS 追加） | 有 tool 调用时拼在最后一个容器后面；无 tool 时整卡隐藏 |

已删除：`#lp-empty` 空态占位（「等待请求」，v0.131）。

要点：
- thinkstream/puretext 是并发请求**各建各的**；列数由 JS 布局（`layout()`）按内容 + `auto_extend` 决定，最多 nCols × 400px。
- puretext 后续收到 thinking_text 会**升级**为 thinkstream（`upgradeToThinkstream`，v0.131）。
- done/error 后 10s 自动清除对应 rid 容器（Python `release → _schedule_clear → _clear_rid`，`destroy_after_done_sec=10`）。
- 容器间距：`.live-panel-main`（列间）与 `.lp-col`（列内）gap 均 1px（v0.131 由 3px 收紧）。

---

## 3. 分析需求后得出的开发路径

### endpoint 容器模型（v0.130 单请求 → v0.132 并发列表）

```
.live-panel-endpoint（静态单例，col1 顶，flex:0 0 auto）
├─ .live-panel-endpoint-row           ← 顶部全局：最新活跃 rid 上游名 + phase 徽标
├─ .lp-ep-list（#lp-ep-list）          ← 并发请求列表，超高 30vh 内滚动
│   └─ .lp-ep-item ×N（每个并发请求一行，按到达序）
│       ├─ .lp-ep-item-top    platform · model      [phase 徽标]
│       └─ .lp-ep-item-sub    入向 → 出向          （inbound → outbound 字节）
└─ .live-panel-row-key（api-key）     ← 底部全局（同一端点所有请求共用）
```

- **数据**：`_epRids: Map<rid, {upstream, platform, client_model, model, phase, error, inbound, outbound}>`，到达序由 Map 迭代序保证（`set` 追加、不重排）。
- **顶部"最新活跃"**：`_lastEpRid` = 最后到达事件的 rid（v0.130 语义保留）。`relayLiveClear` 删的是最新 rid 时回退到剩余 Map 最后一项，全空回 `_snapTop`（首连快照兜底）或 `—`/idle。
- **snapshot 事件**（无 rid）只作 `_snapTop` 顶部兜底，不建列表行。
- **生命周期**：与 per-rid 容器同源同灭 —— `applyEndpoint(ev)` 建/更行，`relayLiveClear(rid)` 删行，`relayLivePanelInit` 全清。
- **phase**：抽公共 `phaseLabel(phase, error)` → `{label, attr}`，端点顶部徽标 / 列表行 / per-rid 容器徽标三处共用（原 `setPhaseBadge` 逻辑抽出）。
- **api-key**：`applyCleartextKey(rid, cleartext)` 用最新 rid 拉，全局单值（不变）。
- **高度**：`.lp-ep-list` `max-height:30vh; overflow-y:auto`，并发多时列表内部滚动，不无限拉长挤压下方容器。

### 实现顺序

```
#1 HTML：endpoint 卡结构替换（删 sub/wire 静态行，加 #lp-ep-list）
#2 CSS：.lp-ep-list / .lp-ep-item / 子项样式
#3 JS：_epRids + phaseLabel + applyEndpoint 重写 + renderEpTop/renderEpList
#4 JS：relayLiveClear / relayLivePanelInit 接列表清理
#5 版本 bump + jsdom 验证
```

---

## 4. 实现中遇到的问题

### 问题 1：新版 applyEndpoint 插入位置残留旧版，同名覆盖

`applyEndpoint` 的 old_string 只覆盖了 applyWire 区块 + applyCleartextKey 开头，旧 `applyEndpoint`（modelLabel 之后、per-rid 区之前）未被覆盖 → 产生**两个同名函数声明**，JS 后者覆盖前者 → 生效的是旧版（引用已删除的 `$platform/$model/$wire` → 运行时 TypeError 风险）。

**解法**：grep 定位残留（`$platform/$model/$wire` 命中 3 行），删除旧 `applyEndpoint` 整块，确认 `grep -c "function applyEndpoint"` = 1。

### 问题 2（测试断言）：顶部"最新活跃"语义被误判

初版 endpoint.js 断言 B3「done r1 后顶部仍 r2」、E3「全清后顶部 —」失败 —— 实现语义是：
- "最新活跃" = **最后到达事件**的 rid（done 也是事件），所以 r1 done/error 后顶部显示 r1 error。
- 全清后顶部回 **snapshot 兜底**（`_snapTop`），不是 `—`。

**解法**：修正测试断言贴合设计语义（B3 = r1 error；E3 = snap-up），代码零改动。

### 问题 3（工具环境）：Grep 工具损坏 / Write 与 bash 的 /tmp 不一致

同 v0.131（`rg.exe` ENOENT 用 Bash grep 兜底；Write 落 `C:\tmp` 需 cp 到 jsdom 目录跑）。本次 Write 直接写了 `C:\Users\weizheng\AppData\Local\Temp\lp_harness\`，绕开不一致。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 同名 applyEndpoint 覆盖 | 删除残留旧版，确认唯一 | web/live_panel.js |
| #2 测试断言与语义不符 | 修正断言（顶部=最后到达事件 / 全清回 snapshot 兜底） | /tmp/lp_harness/endpoint.js |
| #3 工具环境 | Bash grep 兜底 + 直接写 jsdom 目录 | — |

---

## 6. 是否完全遵循规划路径开发

**无规划文件**（用户口头指令），按「先文档（容器清单表格）→ 后实现」顺序执行，全部遵循：

- 容器架构清单表格完整写入文档第 2 节（用户明确"首先把表格写入文档"）。
- endpoint 并发请求全部显示：顶部全局 + 列表 + api-key 三层结构，生命周期与 per-rid 容器同源。
- 抽 `phaseLabel` 共用，删除 endpoint 单请求 5 字段静态逻辑（sub/wire 行）。

### 重大调整：无。

---

## 7. 最终实现点

### 前端（全部在前端，后端零改动）

1. **`web/live_panel.html`**
   - endpoint 卡结构替换：删 `.live-panel-endpoint-sub`（平台·模型静态行）与 wire 静态行，加 `<div class="lp-ep-list" id="lp-ep-list"></div>`（并发列表，位于顶部行与 api-key 行之间）

2. **`web/live_panel.js`**
   - 删 `$platform/$model/$wire` 引用，加 `$epList`
   - 抽 `phaseLabel(phase, error)` → `{label, attr}`（`setPhaseBadge` 改用它）
   - 新增 `_epRids: Map`、`_lastEpRid`、`_snapTop`、`epRecord(ev)`、`renderEpList()`、`renderEpTop()`
   - `applyEndpoint(ev)` 重写：有 rid → 写 `_epRids` + 置 `_lastEpRid` + 拉 api-key + `renderEpList()`；snapshot → `_snapTop`；总 `renderEpTop()`
   - `relayLiveClear(rid)`：`_epRids.delete(rid)` + 最新 rid 回退 + `renderEpTop/renderEpList`
   - `relayLivePanelInit`：`_epRids.clear()` + 重置顶部/api-key + `renderEpList`
   - `applyLivePanelLang` SEL：删 `#lp-platform/#lp-model`，加 `.lp-ep-meta/.lp-ep-wire`

3. **`web/live_panel.css`**
   - 新增 `.lp-ep-list`（flex column、gap 2px、max-height 30vh、overflow-y auto、顶部分隔虚线）、`.lp-ep-item`（panel-alt 底、hairline 边、radius-sm）、`.lp-ep-item-top/.lp-ep-meta/.lp-ep-badge/.lp-ep-item-sub/.lp-ep-wire`

### 资源文件版本

| 文件 | 版本 |
|---|---|
| `live_panel.js` | `?v=20260823-48` → `?v=20260823-49` |
| `live_panel.css` | `?v=20260823-45` → `?v=20260823-46` |

### 验证

- **jsdom（`/tmp/lp_harness/endpoint.js`）23 项断言全过**：snapshot 兜底 / 两并发 2 行字段正确 / done+error 徽标 / 顶部=最后到达事件 / clear 回退到剩余最后到达 / 全清回 snapshot 兜底 / init 清空 / 全程无 JS error。
- **回归**：upgrade.js 20 项 + clear_check.js 10 项全过（v0.131 升级 / tools 显隐 / 清理生命周期无破坏）。

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.html` | 改（endpoint 结构 + 版本 bump） |
| `src/relay/web/live_panel.js` | 改（并发列表 + phaseLabel 抽取） |
| `src/relay/web/live_panel.css` | 改（.lp-ep-* 样式） |
| `docs/dev/live_panel_endpoint_concurrent_v0.132.md` | **新建** |

================================================================================

## v0.133 追加（2026-08-23）

> 延续 v0.132「端点数据容器改并发列表」的同一轮迭代，本轮三处口头反馈：
> 1. 「看到了。上下是窄了，但是左右还是宽，缩窄」
> 2. 「端点数据容器中并行的最新的放在最上面」
> 3. 「如果底下还有空位，优先将tool等移动进去，不浪费空间」

---

### 1. 用户的初始指令（本轮三句，原样锚定）

- 看到了。上下是窄了，但是左右还是宽，缩窄
- 端点数据容器中并行的最新的放在最上面
- 如果底下还有空位，优先将tool等移动进去，不浪费空间

### 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 列内左右留白收窄：`.lp-col` 水平 padding 12px → 4px（上下保留） | 指令 1 |
| B | 端点并发列表「最新活动排最上」：最新到达/更新的请求置顶，较旧的沉底 | 指令 2 |
| C | 工具容器优先填底部空位：有空位时撑到完整内容高（突破旧 20vh 封顶），无空位时收回内容实测高仍可滚动 | 指令 3 |

### 3. 分析需求后得出的开发路径

```
#1 CSS：.lp-col padding 水平 var(--spacing-md) → var(--spacing-xs)（12→4px）
#2 JS：applyEndpoint 已知 rid 先 delete 再 set（Map 插入序=最近活动序）
#3 JS：renderEpList 反向遍历 _epRids → 最新在最上
#4 CSS：去掉 #lp-tools-wrap 的 max-height:20vh 封顶
#5 JS：toolsNaturalHeight 上限 0.2×H → H
#6 JS：layout() 列内水填优先把空位给 tools 直到完整内容高，剩余再给内容容器 +10%
```

要点：
- 列表排序：保持 Map 单一数据源不变，**插入序即最近活动序** —— 旧实现是"先到先建 → 旧在顶"，需翻转。
- 更新语义：一个已存在的 rid 再来事件时，必须把它"移到末尾"才正确反映"最新活动"，否则 Map 不重排，旧 rid 永远沉底。
- 工具撑满：旧实现 `toolsNaturalHeight` 与 CSS `max-height:20vh` **双重封顶**在 0.2×H，必须同时去掉才能让 JS 把空位喂给工具。

### 4. 实现中遇到的问题

**问题 1：列表插入序 = 到达序，最新沉底**
旧 `renderEpList` 正向 `forEach`，先到的 r1 在前（顶部），后到的 r2 在后（底部）。用户要"最新在最上" → 需反向渲染。

**问题 2：已存在 rid 再来事件不重排**
`applyEndpoint` 直接 `_epRids.set(rid, ...)` 对已存在的 key 只覆盖值、**不改插入位置**，所以"更新 r1"不会让 r1 回到列表顶部。需显式 `delete` 再 `set`。

**问题 3：工具被 0.2×H 双重封顶**
`toolsNaturalHeight` 用 `Math.min(h, H * 0.2)` 锁死 20vh，CSS `#lp-tools-wrap { max-height: 20vh }` 又封一层。水填再怎么给空位也撑不过 20vh。两处都要去。

**问题 4（测试）：$toolsWrap 启动期被摘出 DOM**
`layout()` 对空工具容器 `display:none` 且不放进任何列 → 启动初始 layout 后 `#lp-tools-wrap` 从 DOM 摘除，`document.querySelector` 取不到，无法 mock `offsetHeight`。需在元素挂载后再测，或 mock 原型。

### 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 列表最新沉底 | `renderEpList`：`Array.from(_epRids.entries()).reverse()` 反向渲染 | live_panel.js |
| #2 更新不重排 | `applyEndpoint`：`if (_epRids.has(rid)) _epRids.delete(rid); _epRids.set(...)` | live_panel.js |
| #3 工具双重封顶 | CSS 去 `max-height:20vh`；`toolsNaturalHeight` 上限 `0.2×H → H` | live_panel.css / live_panel.js |
| #4 测试取不到 wrap | 在 `HTMLDetailsElement.prototype` 上 define `offsetHeight`，挂载态无关都回报内容高 | tools_fill.js |

**layout() 水填优先 tools 逻辑**（§3 #6）：
```
const extra = avail - sum;              // 列内总空位
let toolsFull = $toolsWrap.offsetHeight; // 此时 style.height="" → 完整内容高
let giveTools = min(toolsFull - toolsNat, extra);  // 优先给工具，封顶到完整内容高
const remaining = extra - giveTools;    // 剩余空位
// 内容容器（非 tools）按 +10% 比例分 remaining
```

### 6. 是否完全遵循规划路径开发

**无规划文件**（三轮均为口头指令），按"先改 CSS padding → 再改列表排序 → 再改工具填位"顺序执行，**全部遵循**：

- 左右留白：`.lp-col` 水平 padding `12px → 4px`，列间 1px 不变，上下（顶栏 32 / 底 12）保留。
- 列表最新在最上：Map 插入序=最近活动序 + 反向渲染，顶部"最新活跃"全局语义不变。
- 工具填位：JS 水填优先给工具撑到完整内容高，CSS/JS 双重 20vh 封顶均去除。

### 重大调整：无。

### 7. 最终实现点

**前端（全部在前端，后端零改动）**

1. **`web/live_panel.js`**
   - `applyEndpoint`：已知 rid 先 `delete` 再 `set`（插入序=最近活动序）
   - `renderEpList`：`Array.from(_epRids.entries()).reverse()` 反向遍历 → 最新活动在最上
   - `toolsNaturalHeight(H)`：上限 `Math.min(h, H * 0.2)` → `Math.min(h, H)`
   - `layout()` 列内水填：先给 tools 分配空位直到完整内容高，剩余再给内容容器做 +10%

2. **`web/live_panel.css`**
   - `.lp-col`：`padding: 32px var(--spacing-md) var(--spacing-md)` → `padding: 32px var(--spacing-xs) var(--spacing-md)`（水平 12→4px）
   - `#lp-tools-wrap`：删除 `max-height: 20vh`（只留 `overflow: auto`），解除 CSS 封顶

**资源文件版本**

| 文件 | 版本 |
|---|---|
| `live_panel.js` | `?v=20260823-49` → `?v=20260823-51` |
| `live_panel.css` | `?v=20260823-46` → `?v=20260823-48` |

**验证**

- **jsdom 全过**：endpoint.js 25 项（含 A2/A5 最新在最上、A7/A8 更新较早项移顶且字段刷新）/ upgrade.js 20 项 / clear_check.js 10 项 / 新增 tools_fill.js 8 项（工具撑满内容高突破 20vh、主容器仍水填、空位不足不超内容高、无 JS error）。
- 5 agent 并行验证全 PASS：jsdom 三件套 + tools_fill、主窗 nav 波纹、CSS gap/padding、端点列表 JS 逻辑、thinkstream 升级+tools 显隐。

**修改的文件清单**

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.html` | 改（版本 bump：js -51 / css -48） |
| `src/relay/web/live_panel.js` | 改（最新置顶 + 工具填位 + 上限调整） |
| `src/relay/web/live_panel.css` | 改（.lp-col 水平 padding + 去 #lp-tools-wrap 封顶） |
| `docs/CHANGELOG.txt` | 改（新增 v0.133 块） |
| `docs/dev/live_panel_endpoint_concurrent_v0.132.md` | 改（末尾追加 v0.133 节） |

================================================================================

## v0.134 追加（2026-08-23）

> 延续 v0.132「端点并发列表」轮次。本轮两条指令：
> 1. 「工具调用同样设置超时20s自动清除」
> 2. 「然后在设置中增加几个可调设置」（经澄清：工具清除超时 20s / 工具调用上限 30 / 完成清除超时 10s / 端点列表高 30vh 四个全加）。

---

### 1. 用户的初始指令（本轮两句，原样锚定）

- 工具调用同样设置超时20s自动清除
- 然后在设置中增加几个可调设置

### 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 工具容器最后更新后自动清除，默认 20s（对齐 per-rid done 10s 清除语义） | 指令 1 |
| B | 设置页新增「工具清除超时（20s）」可调 | 指令 2 |
| C | 设置页新增「工具调用上限（30 条）」可调 | 指令 2 |
| D | 设置页新增「完成清除超时（10s）」可调（后端 panel_pool 原硬编码 10 → 配置项） | 指令 2 |
| E | 设置页新增「端点列表高度（30vh）」可调 | 指令 2 |

### 3. 分析需求后得出的开发路径

```
#1 前端：applyTools 末尾 scheduleToolsClear()（setTimeout 默认 20s）→ clearTools 清空
#2 前端：setToolsClearSec / setToolsCap / setEpListVh 三个 setter（改写 _toolsClearSec / _TOOLS_CAP / .lp-ep-list max-height）
#3 前端：relayLivePanelInit 重置时取消待清除定时器
#4 后端：config 新增 relay_live_panel_done_clear_timeout（默认 10）+ relay_gui_live_panel_tools_clear_timeout / tools_cap / ep_list_vh
#5 后端：gui.py 桥 get/set 四组 + snapshot 字段；panel_pool set_done_clear_timeout
#6 前端：app.js 设置页 UI（滚轮×2 + 数字×2）+ 桥 wrapper + 监听 + refreshPrefsDynamic 初始值
#7 资源版本 bump + jsdom 验证
```

要点：
- 工具清除与 per-rid 清除**两套独立定时器**：per-rid 由 Python `release→_schedule_clear→_clear_rid`（后端驱动）；工具清除由**前端** `applyTools→scheduleToolsClear`（前端驱动），语义一致但载体不同。
- 「完成清除超时」原 `panel_pool.destroy_after_done_sec` 硬编码 10，要提到设置页 → 必须改 config + gui 桥 + panel_pool 构造读取 + `set_done_clear_timeout`。
- 三个前端项走「写 .env + 调侧栏 setter」模式（仿 auto_extend），用 `_set_live_panel_float_setting` 通用助手 + `_live_panel_js_fn` 映射类名。

### 4. 实现中遇到的问题

**问题 1：工具清除定时器不触发（测试初版）**
harness 把 `setToolsClearSec(0.05)` 但 `scheduleToolsClear` 有 `Math.max(1, ...)` 下限（防 0/负数），被钳到 1s；而测试只等 200ms → 永不到点。
**解法**：测试改为 `setToolsClearSec(1)` + 等 1.3s 验证；生产默认值 20s 不受影响。下限保留（避免误设 0 导致每次注入即清空）。

**问题 2：gui.py 编辑产生重复函数**
初次插入 `set_live_panel_done_clear_timeout` 时 old_string 未覆盖到函数体末尾，留下一个残缺副本 + 完整副本 → `IndentationError`。
**解法**：删除残缺副本，确认 `grep -c "def set_live_panel_done_clear_timeout"` = 1，`py_compile` 通过。

**问题 3：_settings() 在 __init__ 早期调用是否安全**
`PanelPool.__init__` 第 72 行 `_settings()` 读取 `self._api._app.settings`，此时 `self._api` 已赋值（line 65）；`_settings` 是类方法、定义位置在 671 不影响调用。验证安全。

### 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 定时器钳制下限 | 测试用 1s + 等 1.3s；保留 Math.max(1) 下限 | tools_clear.js / live_panel.js |
| #2 gui.py 重复函数 | 删除残缺副本，确认唯一 + py_compile | gui.py |
| #3 _settings 早期调用 | 确认 self._api 已赋值、方法可调用 | panel_pool.py |

### 6. 是否完全遵循规划路径开发

**无规划文件**（两轮口头指令），按「先工具清除 → 再四个可调设置（前端 3 + 后端 1）」顺序执行，**全部遵循**：
- 工具 20s 自动清除：前端 setTimeout + clearTools，对齐 per-rid 语义。
- 四设置：工具清除超时 / 工具上限 / 完成清除超时 / 端点列表高度，均落地可调 + 写回 .env + 即时生效。
- 后端完成清除超时从硬编码 10 改为配置项并接设置页。

### 重大调整：无。

### 7. 最终实现点

**前端（live_panel.js / app.js / live_panel.html）**

1. `live_panel.js`
   - `scheduleToolsClear()`：applyTools 末尾调用，setTimeout(clearTools, _toolsClearSec×1000)，下限 1s。
   - `clearTools()`：清空 #lp-tools + 隐藏容器 + 取消 age ticker + scheduleLayout。
   - `setToolsClearSec(sec)` / `setToolsCap(n)` / `setEpListVh(vh)`：三个设置 setter。
   - `_TOOLS_CAP` 由 const 改 let（可被 setter 改）；`relayLivePanelInit` 重置时 `clearTimeout(_toolsClearTimer)`。
2. `app.js`
   - 桥 wrapper：`get/setLivePanelDoneClearTimeout`、`get/setLivePanelToolsClearTimeout`、`get/setLivePanelToolsCap`、`get/setLivePanelEpListVh`。
   - 设置页 `live-panel-mgmt-group` 新增 4 项：完成清除/工具清除（滚轮 wheel-picker）、工具上限/端点列表高（数字 input）。
   - `wireSettingsPrefs`：自动延展块后挂工具上限 / 端点列表高 change 监听；`mountTimeoutWheel` 加 done-clear / tools-clear。
   - `refreshPrefsDynamic`：两个滚轮 initTimeoutWheel + 两个数字 input 初始值。

**后端（config.py / gui.py / panel_pool.py）**

3. `config.py`：新增 `relay_live_panel_done_clear_timeout=10.0`、`relay_gui_live_panel_tools_clear_timeout=20.0`、`relay_gui_live_panel_tools_cap=30`、`relay_gui_live_panel_ep_list_vh=30.0`。
4. `gui.py`：四组 get/set 桥（done-clear/tools-clear 返 `{seconds}`，cap/vh 返 `{value}`）；通用 `_set_live_panel_float_setting` + `_live_panel_js_fn` 映射；snapshot 加 4 字段。
5. `panel_pool.py`：`__init__` 读 `relay_live_panel_done_clear_timeout` 覆盖默认；新增 `set_done_clear_timeout(sec)`；`PanelPool(...)` 构造传 `destroy_after_done_sec=int(settings...)`。

**资源文件版本**

| 文件 | 版本 |
|---|---|
| `live_panel.js` | `?v=20260823-51` → `?v=20260823-52` |
| `app.js`（index.html 引用） | `?v=20260823-47` → `?v=20260823-52` |

**验证**

- **jsdom 全过**：endpoint.js 25 / upgrade.js 20 / clear_check.js 10 / tools_fill.js 8 / tools_clear.js 8（setToolsCap 生效、超时自动清空+隐藏、setEpListVh 改写、无 JS error）= 71 项。
- Python `py_compile` 通过（config/gui/panel_pool）；JS `node --check` 通过（live_panel.js/app.js）。

**修改的文件清单**

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（+4 配置项） |
| `src/relay/gui.py` | 改（+4 组桥 + snapshot 字段 + 通用助手） |
| `src/relay/panel_pool.py` | 改（读配置 + set_done_clear_timeout） |
| `src/relay/web/live_panel.js` | 改（工具清除 + 3 setter + init 取消） |
| `src/relay/web/live_panel.html` | 改（js 版本 bump -52） |
| `src/relay/web/app.js` | 改（4 设置 UI + 桥 + 监听 + 初始值） |
| `src/relay/web/index.html` | 改（app.js 引用，同批） |
| `docs/CHANGELOG.txt` | 改（新增 v0.134 块） |
| `docs/dev/live_panel_endpoint_concurrent_v0.132.md` | 改（末尾追加 v0.134 节） |

================================================================================

## v0.135 追加（2026-08-23）

> 延续 v0.134「侧栏可调设置」轮次。本轮一句话指令：
> 「继续增加，并新增『样式高级设置』『等待时间高级设置』开关，收纳部分内容」
>
> 经 AskUserQuestion 二次确认：
> - 收纳策略：超时归「等待时间」分组，布局/外观归「样式高级设置」分组。
> - 三个新设置：工具常驻开关 / 列表字号档位 / 最小列数。

---

### 1. 用户的初始指令（本轮一句，原样锚定）

- 继续增加，并新增「样式高级设置」「等待时间高级设置」开关，收纳部分内容

### 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 「等待时间高级设置」分组开关：收纳 5 个超时项（思考/衔接/正文/完成清除/工具清除），默认 OFF | 指令 + Q1 答案 |
| B | 「样式高级设置」分组开关：收纳工具上限 / 端点列表高 / 侧边栏无边框 / 工具常驻 / 列表字号 / 最小列数 | 指令 + Q1 + Q2 |
| C | 工具常驻开关：开启后无工具调用也保留工具容器占位（不再整卡隐藏） | Q2 答案 |
| D | 列表字号档位（small/medium/large）：body.data-list-font → CSS 改写 .lp-ep-list 字号（11/12/13） | Q2 答案 |
| E | 最小列数（auto_extend 模式下也至少 N 列）：layout() 末尾用 _minCols floor 补列 | Q2 答案 |
| F | 折叠状态本地偏好（prefs.livePanelTimeoutAdvanced / livePanelStyleAdvanced）：切设置页回来保留 | 指令"收纳"语义 |

### 3. 分析需求后得出的开发路径

```
#1 HTML：app.js 设置页 live-panel-mgmt-group 内重排：5 超时项挪入"等待时间高级设置"嵌套面板；工具上限/端点列表高/无边框挪入"样式高级设置"嵌套面板，并新增工具常驻开关、列表字号档位（seg 三档）、最小列数（数字）。
#2 CSS：复用现有 .settings-item-nested + .adv-switch-panel 折叠样式（hairline 左边 + nested 间距）。
#3 JS prefs 默认值：livePanelTimeoutAdvanced=false、livePanelStyleAdvanced=false（持久化于 prefs 对象）。
#4 JS wireSettingsPrefs：两个高级开关 change → nested 面板显隐 + 写 prefs + savePrefs()；新增 setLivePanelToolsAlways / setLivePanelMinCols / setLivePanelListFont 三个监听。
#5 JS refreshPrefsDynamic：两个开关初始态从 prefs 读 + 面板显隐；3 个新设置初始值用桥拉取。
#6 后端 config.py：3 个新字段（tools_always / min_cols / list_font）。
#7 后端 gui.py：6 个桥（get/set × 3），3 个即时下发用 evaluate_js，snapshot 加 3 字段。
#8 前端 live_panel.js：3 个 setter（setToolsAlways / setMinCols / setListFont）；layout() 工具可见性判断增加 _toolsAlways OR toolCount > 0；layout() 末尾用 _minCols 补列；loadLayoutHint 拉取 3 个新设置。
#9 前端 live_panel.css：.live-panel-body[data-list-font="small|medium|large"] 三档字号钩子。
#10 资源版本 bump + jsdom 验证（advanced_groups.js 11 项 + 5 个 v0.134 回归）
```

要点：
- 收纳策略是**纯 UI 重排**：原 5 超时 + 4 设置项散在「实时栏管理」组顶层，现在分别裹进两个 nested 面板；面板隐藏状态下子项与 JS 监听均不工作（无 DOM 引用），切回显示再激活。
- 折叠状态**与设置值解耦**：开关本身的状态（.prefs-live-panel-timeout-advanced 等）是「用户是否展开」这个 UI 状态，独立于其收纳项的当前值。prefs 持久化这个 UI 状态。
- 工具常驻开关切换的语义边界：仅控制**显隐**（layout 决定 wrap 是否进 items），不绕过 _TOOLS_CAP 也不重置 timer —— 仍走 scheduleToolsClear 旧逻辑。

### 4. 实现中遇到的问题

**问题 1：嵌套面板 hidden 时如何防止内嵌的 wheel-picker / number input 监听重复绑定**
旧实现 mountTimeoutWheel / 数字 input 的 change 监听都通过 `body.querySelector` 拿到 host 再 addEventListener；嵌套面板 hidden 时 host 仍存在（hidden 属性不影响 DOM 树）所以监听照常绑，没问题。切换显示后也不会重复绑（无重新渲染）。
**解法**：无需特殊处理，确认 mountTimeoutWheel 在首次进入设置页时跑一次就够。**实际验证**：所有 v0.111 wheel-picker 在嵌套面板内仍正常 initTimeoutWheel 拉初始值。

**问题 2：tools-always 关时 layout 会把 #lp-tools-wrap 从 DOM 摘出**
v0.130 的 layout() 实现：toolCount=0 时 `wrap.style.display="none"`，但 placeColumns() 只把 items 里的元素挂到列里 —— tools 不在 items 就不挂，wrap 被留在原 .lp-col-1 中但 display:none；**但实际 wrap.parentNode 可能仍指向原 .lp-col-1**（jsdom 测试表现为 querySelector 返回 null —— layout 在 placeColumns 时把它从原列 removeChild）。所以 harness 需要**先注入一条工具让 wrap 回到 DOM**才能查 .style.display。
**解法**：harness 先 `relayLiveEvent({type:"done", tool_use_json:[...]})` 让 wrap 挂回 DOM 再做断言；纯视觉/状态断言都通过。

**问题 3：seg-btn 默认 active 状态**
setLivePanelListFont 桥返回 `{value: "medium"}`，harness 应据此给 medium 按钮加 active 类。CSS 已有 `.seg-btn.active` 样式（见 styles-20260817.css `.seg-lang` / `.seg-theme`）。app.js 中 seg-btn click 监听 toggle active 类，写回后端。**无新问题，沿用现有 seg 模式。**

### 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 嵌套面板监听重复 | mountTimeoutWheel 一次性绑，hidden 不影响 | live_panel.js / app.js |
| #2 wrap 在 DOM 外不可查 | harness 先注入工具再观察 | advanced_groups.js |
| #3 seg 模式 | 复用既有 .seg-btn / data-list-font 属性 + click toggle active | app.js / live_panel.css |

### 6. 是否完全遵循规划路径开发

**无规划文件**（单轮口头 + AskUserQuestion 二次确认），按「先 UI 重排（HTML/CSS/JS）→ 后端 config → 后端 gui.py 桥 → 前端 setter/CSS → 验证」顺序执行，**全部遵循**：
- 收纳策略与 Q1 答案一致：超时 5 项归「等待时间」，布局/外观 5 项归「样式」。
- 新增 3 项与 Q2 答案一致：工具常驻 / 列表字号 / 最小列数。
- 默认两个开关 OFF、prefs 持久化折叠状态、nesting CSS 复用既有 .adv-switch-panel 模式。

### 重大调整：无。

### 7. 最终实现点

**前端**

1. `app.js`（设置页 live-panel-mgmt-group 内重排）
   - 「实时栏管理」组：保留前 5 项（允许并发/最多显示/始终开启/自动延展/等待时间高级开关）。
   - 「等待时间高级设置」开关（.prefs-live-panel-timeout-advanced）+ 嵌套 #prefs-live-panel-timeout-advanced-panel：5 超时 wheel-picker。
   - 「样式高级设置」开关（.prefs-live-panel-style-advanced）+ 嵌套 #prefs-live-panel-style-advanced-panel：工具上限/端点列表高/侧边栏无边框/列表字号 seg/工具常驻开关/最小列数。
   - 「外观」组删除「侧边栏无边框」项（挪到样式嵌套面板）。
   - wireSettingsPrefs：两开关 change 监听 + 3 新设置监听（number / number / seg）；refreshPrefsDynamic：prefs 同步初始面板显隐 + 桥拉初始值。

2. `live_panel.js`
   - `setToolsAlways(on)`：写 `_toolsAlways` + `scheduleLayout()`。
   - `setMinCols(n)`：clamp [1,6] + 写 `_minCols` + `scheduleLayout()`。
   - `setListFont(tier)`：clamp 到 small/medium/large → `body.dataset.listFont`。
   - `layout()`：工具可见性条件改为 `toolCount > 0 || _toolsAlways`；末尾加 `_minCols` floor 补列（while 循环，限 LP_AUTO_EXTEND && cols.length < LP_MAX_COLS）。
   - `loadLayoutHint()`：拉取 get_live_panel_tools_always / get_live_panel_min_cols / get_live_panel_list_font 三个初始值并生效。

3. `live_panel.css`
   - `.lp-ep-list` 默认 `font-size: 12px`。
   - `.live-panel-body[data-list-font="small|medium|large"]` 三档 `.lp-ep-list` / `.lp-ep-meta` 字号钩子（11/12/13）。

**后端**

4. `config.py`
   - `relay_gui_live_panel_tools_always: bool = False`。
   - `relay_gui_live_panel_min_cols: int = 1`。
   - `relay_gui_live_panel_list_font: str = "medium"`。

5. `gui.py`
   - `get/set_live_panel_tools_always`：写 settings + 写 .env + 下发 `setToolsAlways(bool)`。
   - `get/set_live_panel_min_cols`：clamp 1-6 + 写 .env + 下发 `setMinCols(int)`。
   - `get/set_live_panel_list_font`：白名单 small/medium/large + 写 .env + 下发 `setListFont(str)`。
   - `get_snapshot` 加 3 字段：`live_panel_tools_always` / `live_panel_min_cols` / `live_panel_list_font`。

**资源文件版本**

| 文件 | 版本 |
|---|---|
| `live_panel.js` | `?v=20260823-52` → `?v=20260823-53` |
| `live_panel.css` | `?v=20260823-48` → `?v=20260823-49` |
| `app.js`（index.html 引用） | `?v=20260823-52` → `?v=20260823-53` |

**验证**

- **jsdom 全过**：endpoint.js 25 / upgrade.js 20 / clear_check.js 10 / tools_fill.js 8 / tools_clear.js 8 / 新增 advanced_groups.js 11（setToolsAlways 开/关显隐、setMinCols 至少 3 列、setListFont 4 档切换 + 兜底 medium、全程无 JS error）= **82 项**。
- **Python `py_compile` 通过**（config.py / gui.py）。
- **JS `node --check` 通过**（live_panel.js / app.js）。
- **函数唯一性**：`grep -c` 确认 live_panel.js 12 个函数声明各 1 处（v0.135 新增 3 个 setter 无重复）；gui.py 6 个新桥各 1 处。

**修改的文件清单**

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（+3 配置项） |
| `src/relay/gui.py` | 改（+6 桥 + snapshot +3 字段） |
| `src/relay/web/live_panel.js` | 改（+3 setter + layout 集成 + loadLayoutHint 拉取） |
| `src/relay/web/live_panel.html` | 改（js 版本 -53 / css 版本 -49） |
| `src/relay/web/live_panel.css` | 改（+列表字号三档钩子） |
| `src/relay/web/app.js` | 改（设置页重排 + prefs 默认 + 监听 + 初始值） |
| `src/relay/web/index.html` | 改（app.js 版本 -53） |
| `docs/CHANGELOG.txt` | 改（新增 v0.135 块） |
| `docs/dev/live_panel_endpoint_concurrent_v0.132.md` | 改（末尾追加 v0.135 节） |
| `C:\Users\weizheng\AppData\Local\Temp\lp_harness\advanced_groups.js` | **新建**（v0.135 jsdom 11 项 harness） |
