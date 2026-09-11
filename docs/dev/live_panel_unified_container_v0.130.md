# 实时流侧栏大改 —— 统一容器 + 动态多列 + 自动延展（v0.130）开发文档

## 1. 用户的初始指令

> 现在大改侧栏机制：当有请求流时，根据流的类型在侧栏创建容器（识别到思考流 → 建「带思考流容器」；一进来直接正文流 → 建「纯正文容器」）。并发请求各建各的容器，允许自动压缩（不超过原尺寸 ±10%）来在多个并发时刚好填满侧栏高度。设置新增「自动延展侧栏」选项。侧栏主栏也不再限制只允许一个主内容，并发内容纵向叠放拼接在下方。tool 工具的调用：此时有几个 tool 工具就允许拼接几个 tool 容器，tool 容器最多 20% 高度，可压缩来适应窗口宽度。现在开始规划此功能。

> 交互确认（AskUserQuestion 三选）：
> - 自动延展方式 → **宽度延展**：窗口变宽让更多并发显示，复用原 grid 右延展逻辑
> - tool 容器拼接位置 → **有调用就随时在最后一个容器的后面拼接**，不管是哪个请求的调用
> - 端点数据容器 → **保留顶部全局**，显示最新活跃请求 5 字段

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 按流类型动态建容器：`thinking_text` 非空 → thinkstream；仅 `assistant_text` → puretext | 指令 1 |
| B | 并发请求各建各的容器，允许 ±10% 压缩以刚好填满侧栏高度 | 指令 2 |
| C | 设置新增「自动延展侧栏」= **宽度延展** | 用户确认 |
| D | 主栏不再限一个主内容，并发内容**纵向叠放拼接**在下方 | 指令 4 |
| E | tool 容器全局拼接，随时拼在**最后一个容器后面** | 用户确认 |
| F | tool 容器最多 20% 高、可压缩（只收不涨） | 指令 5 |
| G | 端点数据容器保留顶部全局，显示最新活跃 rid 5 字段 | 用户确认 |
| H | 容器 kind 首事件定类，**中途不换类** | 用户 v0.125 历史确认 |

### 隐含但需要确认的点（规划阶段已论证）

- 布局引擎：flex column-wrap vs JS 显式布局 → **JS 显式布局**（列内要精确「刚好填满」+ 守 ±10% 钳制，flex 只能近似且 WebView2 对 column-wrap 百分比 basis 不稳；反正列数必须 JS 算出来上报 Python）
- 生命周期：统一模型下每个 rid 容器 done 后 10s 清除（grid 式生命周期），原「always_one 最后结果常驻」行为消失 —— 计划默认接受 grid 式清除
- 列数上限：`_max_cols = floor(屏幕余宽/panel_width)` 兜底 ≤6 列

---

## 3. 分析需求后得出的开发路径

### 新模型总览

```
body (flex row, 100% 窗口宽)
└─ .live-panel-main (100% 宽、100vh、flex row、gap 3px)
   ├─ .lp-col #1 (400px、flex column、100vh、overflow-y auto)
   │   ├─ .live-panel-endpoint      (静态单例，固定不压缩，永远 col1 顶)
   │   ├─ 动态 rid 容器 #1…         (thinkstream / puretext，JS 建)
   │   └─ (最后一个请求的容器后) tool 容器（全局，到达序追加）
   ├─ .lp-col #2 …                  ← auto_extend ON 时按需增列
   └─ … (max 列数 = 屏幕工作区余宽/panel_width，兜底 ≤6)
```

- 所有 active rid 的容器 + endpoint + 全局 tool 容器，统一作为 JS 显式布局的 items
- 无 grid 区：`.grid-region` / `relayGridEvent` / `body.has-grid` 全部退役
- 事件入口统一为 `relayLiveEvent(ev)`（per-rid 路由）

### 实现顺序（按依赖拆 5 步）

```
#1 config.py / gui.py / app.js 设置项四件套    (可先落地不破坏现有)
#2 panel_pool.py 统一 rid 模型                  (删 grid 分支、_target_width、request_width)
#3 live_panel.js 统一入口 + per-rid 容器 + 布局  (合并 grid.js)
#4 live_panel.html/css 结构替换                 (删 grid 死代码)
#5 端到端验证
```

技术关键决策：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 布局引擎 | JS 显式布局（layout/packColumns/fillColumn） | 精确填满 + ±10% 钳制 + 单一数据源（列数+每列高） |
| 自然高 | thinkstream=0.5×H / puretext=0.333×H / tool=内容高 cap 0.2×H / endpoint=内容高 | 固定值 → layout 不随 delta 跑 |
| 贪心列打包 | `it.nat > avail && it.nat ≤ H` 时开新列（auto_extend ON 且 cols<maxCols） | 放不下才换列 |
| 水填分配 | 上界 min(1.1×nat) / 下界 0.9×nat；tools 只收不涨 | 刚好填满 + 不超 ±10% |
| 宽度上报 | JS `reportWidth(nCols)` → `live_panel_layout(nCols)` → `request_width` → `_apply_geometry` | 非阻塞 op worker + `_last_panel_w` 防抖 |
| 窗口宽度 | `_target_width() = clamp(_js_width_cols,1,_max_cols) × panel_width` | auto_extend OFF → 1×panel_width |
| 统一 rid 模型 | `_rids:set[str]` 替代 `grid_rids/grid_blocks` | 单一来源，per-rid 路由 |
| tools 全局拼接 | 到达序追加，cap 30 条 + 20vh 滚动 | 无论哪个请求的调用 |

---

## 4. 实现中遇到的问题

### 问题 1：`layout()` items 过滤把容器排除 → 内容容器永不显示

**现象**（用户现场复现）：endpoint 正常、tool 卡正常，但 thinkstream/puretext 容器全部不出现。

**根因**：`layout()` 的 items 收集过滤条件 `if (rec && rec.el && rec.el.parentNode)`。`ensureContainer` 首次创建容器用 `createElement("section")`，**从未 append 进 DOM** → `rec.el.parentNode === null` → 容器被排除在 items 之外 → `placeColumns` 挂不到它。后续每个 delta/done 在 `ensureContainer` 走 `if (_ridEls[rid]) return` 早退，不再触发 layout → 容器永远游离在 DOM 外。

**解法**：去掉 `rec.el.parentNode` 条件。`relayLiveClear` 已完整清理 `_ridEls/_ridOrder`，无僵尸条目；`placeColumns` 先清列再重挂，天然防重复。本地 jsdom 复现 FAIL→PASS，21 项断言全绿。

### 问题 2：`_apply_geometry` screen clamp 缺守卫 → 侧栏侧栏（延展列）不弹出

**现象**（用户现场复现）：单列显示正常，多并发时窗口不变宽、延展列不出现。

**根因**：v0.130 新增的屏幕余宽 clamp：

```python
if screen is not None:            # bug：screen=(0,0) 非 None
    sw = screen[0]                # sw=0
    max_w = max(self._panel_width, sw - (mx + mw) - 4)   # = 400
    width = min(width, max_w)     # 延展宽被压回 400
```

`webview.screen` 不可用时 `_screen_size()` 返回 `(0,0)`（非 None），延展宽度被 clamp 回单列 400px。对比 `_on_panel_loaded` 算 max_cols 时有 `if sw > 0` 守卫，作者知道 screen 可能失效，但 `_apply_geometry` 漏了。

**解法**：`if screen is not None and screen[0] > 0:` —— 无效屏幕尺寸时跳过 clamp，走 `_target_width` 原生逻辑（JS 侧 `LP_MAX_COLS` 兜底 ≤6）。本地单测 4/4 PASS（screen 失效 2 列 → 800，修复前 400）。

### 问题 3：删除 grid 分支后 `always_one_rid` 残留引用

`toggle_all_visible` / `hide_panel` 仍引用 `self.always_one_rid`，删除 `_find_slot`/grid 分支后 AttributeError 风险。

**解法**：`__init__` 加 `self.always_one_rid: str = ""` stub（保留属性兼容旧判断，等价于不成立）。

### 问题 4：tools cap 静态 NodeList bug

初始 cap-30 循环在 while 内重新 query `all.length = remaining.length` —— 静态 NodeList 不反映 DOM 变化，删除不可靠。

**解法**：改元素兄弟遍历：捕获 `first`/`nextElementSibling`，只删 `.live-panel-tool` 节点，递减 `overs`。

### 问题 5：placeColumns 挂载 parentNode 用错

`$endpoint.parentNode.appendChild(c)` 错误 —— JS 创建的 `.lp-col` 必须是 `.live-panel-main` 直接子元素，而 endpoint 在 col1 里。

**解法**：缓存 `$main = document.querySelector(".live-panel-main")`，用 `$main.appendChild(c)`。

### 问题 6：puretext 收到 thinking_text 内容被默默丢弃（设计妥协，非崩溃）

**现象**：F agent 报告 puretext rid 后续收到 thinking_text → `refs.thinking` 未定义 → TypeError 高严重度。

**验证**：jsdom 复现 `errors=0` —— `setText`/`isSticky` 都有 null 守卫，**不崩**。但 `refs.thinking` 不存在 → 思考内容被默默丢弃（用户看不到）。kind 锁定是用户确认的设计（首事件定类不换类），故这是**行为偏差**（thinkstream 定类后思考可见；puretext 定类后思考丢弃），非崩溃 bug。

### 问题 7：`set_max_cols` 与 `set_auto_extend` 不对称

B agent 报告：`set_max_cols` 只置 `_max_cols`，不通知前端、不 `_relayout`。

**评估**：`set_max_cols` 仅窗口加载时调一次（gui.py:3513），且紧接着 3516 已推 `setMaxCols` 给 JS。运行时 max_cols 不变，JS/Python 初始同步。属防御性缺失，**非实际运行 bug**，按「不做多余防御」原则未动。

### 问题 8：跨 wire 流 thinking 被剥（G agent 报告，注释明示）

跨 wire 流注释明示 thinking 被剥 → 侧栏永远 puretext 容器。

**评估**：属于 proxy 层既有的字段语义，非本次重构引入，未处理。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 容器永不显示 | 去掉 `layout()` items 的 `rec.el.parentNode` 过滤 | web/live_panel.js |
| #2 延展列不弹出 | `_apply_geometry` clamp 加 `screen[0] > 0` 守卫 | panel_pool.py |
| #3 always_one_rid 残留 | `__init__` 加空字符串 stub | panel_pool.py |
| #4 tools cap 静态 NodeList | 改元素兄弟遍历删除 | web/live_panel.js |
| #5 placeColumns parentNode 错 | 缓存 `$main` 作 append 目标 | web/live_panel.js |
| #6 puretext 收 thinking 丢弃 | 设计妥协（kind 锁定），setText null 守卫防崩 | — |
| #7 set_max_cols 不对称 | 评估非实际 bug，未动 | — |
| #8 跨 wire thinking 被剥 | proxy 既有语义，未处理 | — |

---

## 6. 是否完全遵循规划路径开发

**部分偏离（均为 session 内新发现/细节补充）**。

### 完全按规划

- 5 步实现顺序（#1 设置四件套 → #2 pool 统一模型 → #3 JS 统一入口 → #4 HTML/CSS 结构替换 → #5 验证）
- 放弃 flex column-wrap，改 JS 显式布局（贪心分列 + 水填 ±10%）
- 用户确认的 3 个决策全部实现（宽度延展 / tool 拼最后容器后 / endpoint 顶部全局）
- 容器 kind 首事件定类不换类
- done 后 10s 清除（grid 式生命周期）
- tool cap 30 条 + 20vh 滚动

### 偏离之处（session 内新增）

- **(a) 修复了 2 个 session 中发现的真 bug**：#1 容器永不显示（layout items 过滤）、#2 延展列不弹出（screen clamp 缺守卫）。这两个是计划里没有的，属于端到端验证阶段暴露的运行时 bug，直接修复。
- **(b) `set_max_cols` 不对称**：B agent 报告，评估后确认非实际运行 bug（仅加载时调用一次），**未修改** —— 严格遵循「不做多余防御」。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`config.py`**：新增设置字段
   - `relay_gui_live_panel_auto_extend: bool = True`（.env 键 `RELAY_GUI_LIVE_PANEL_AUTO_EXTEND`）

2. **`gui.py`**：桥方法 + snapshot + 初始化
   - `get_live_panel_auto_extend / set_live_panel_auto_extend`（写 settings + `update_env_var` + `pool.set_auto_extend`）
   - `live_panel_layout(ncols)`：JS→Python 宽度上报入口 → `pool.request_width(int(ncols))`
   - `get_live_panel_layout_hint()`：返回 `{panel_width, max_cols, auto_extend}`（初始化用）
   - snapshot 新增 `"live_panel_auto_extend"` 字段
   - `_screen_getter()` + `PanelPool(..., screen_getter=_screen_getter)`
   - `_on_panel_loaded`：按屏幕工作区余宽算 max_cols + push `setMaxCols(n); setAutoExtend(bool);`

3. **`panel_pool.py`**（统一 rid 模型重构）
   - 删 `grid_rids / grid_blocks / _note_grid_blocks / _grid_block_count / _find_slot / _merged_width`
   - 新增 `_rids:set[str]`、`_auto_extend:bool`、`_js_width_cols:int=1`、`_max_cols:int=6`、`always_one_rid=""` stub
   - `assign`：统一 add 到 `_rids` + 显窗，返回 `"ok"`
   - `push_event`：删 always/grid 分支，一律 `relayLiveEvent(payload)`
   - `release`：统一延迟 `_clear_rid(rid)` → `relayLiveClear(rid)` + 移出 `_rids` + `_relayout`
   - watchdog：iterate `_rids` 做 stale 自愈（`_push_rid_timeout` + `_schedule_clear`）
   - 新增 `set_auto_extend(on)` / `request_width(cols)` / `set_max_cols(n)`
   - `_target_width()`：`(auto_extend ? clamp(_js_width_cols,1,_max_cols) : 1) × panel_width`
   - `_apply_geometry`：宽 clamp 到屏幕余宽，**`screen[0] > 0` 守卫**（本 session 修复）
   - `attach_sse_push_to_pool`：统一 `relayLiveEvent(payload)` 路由

### 前端

4. **`web/live_panel.js`**（合并 live_panel_grid.js，重写）
   - 容器 registry `_ridEls[rid] = {el, kind, refs, rThinking, rStream, ...}` + `_ridOrder[]`（到达序）
   - `ensureContainer(rid, ev)`：首事件定 kind，createElement 建容器，id 带 rid 后缀
   - `applyRid(rid, ev)`：写 thinking/stream/usage/rate（per-rid 元素集）
   - `relayLiveEvent(ev)` 统一入口：snapshot 只更 endpoint；带 rid → ensure + applyRid；endpoint 显示最新活跃 rid
   - `applyTools` 全局拼接（追加 + cap 30 + age ticker）
   - `layout()/packColumns/fillColumn()`：贪心分列 + 水填 ±10% + rAF 节流 + `reportWidth(nCols)`
   - `relayLiveClear(rid)` / `relayLiveTimeout(rid, msg)` / `setAutoExtend(on)` / `setMaxCols(n)`
   - **修复**：`layout()` items 过滤去掉 `rec.el.parentNode`（本 session 主 bug）

5. **`web/live_panel.html`**
   - 删 grid-region 块 + `<script live_panel_grid.js>` 引用
   - 删静态 thinkstream/puretext 主栏容器（改 JS 模板）
   - `.live-panel-main` 内放 `.lp-col` + endpoint + 空态「等待请求…」+ tool 卡

6. **`web/live_panel.css`**
   - `.live-panel-main` 改列行容器（flex row、width 100%、gap 3px）
   - 新增 `.lp-col`（400px、flex column、100vh、padding 32px 留顶栏、overflow-y auto）、`.lp-empty`、`.live-panel-rid-head`/`.lp-rid-index`/`.lp-rid-meta`
   - thinkstream/puretext 保留自然高（`flex:0 0 auto`），JS 内联高覆盖
   - tool 容器 `max-height:20vh; overflow:auto`
   - 删 `.grid-region/.grid-rows/.grid-empty/.grid-block` 与 `body.has-grid`

7. **`web/app.js`**（四处）
   - api wrapper：`getLivePanelAutoExtend / setLivePanelAutoExtend`
   - `renderSettingsPrefs`：#live-panel-mgmt-group 内加「自动延展侧栏」switch
   - `wireSettingsPrefs`：加 change 监听
   - `refreshPrefsDynamic`：加初始值拉取

8. **`web/live_panel_grid.js`**：**删除**（obsolete）

### 资源文件版本

| 文件 | 版本 |
|---|---|
| `live_panel.js` | `?v=20260823-46` |
| `live_panel.css` | `?v=20260823-43` |
| `styles-20260817.css`（侧栏引用） | 对齐主窗 `?v=20260823-33` |

### 行为验收清单（手动测试项，实机已验证）

- [x] 单请求带思考 → thinkstream 容器出现，endpoint 更新
- [x] 两并发均思考 → 两容器；auto_extend ON → 窗口 2 列；OFF → 单列纵排 + 滚动
- [x] 混合：请求 A 首事件仅 assistant_text → puretext；请求 B 带思考 → thinkstream
- [x] 2 次 tool_use → 全局 tool 容器按到达序追加（拼在最后容器后），20vh 内滚动
- [x] 设置页切 auto_extend → 实时变宽/收窄（侧栏侧栏弹出）
- [x] done 后 10s → 容器清除、窗口收窄回 1 列
- [ ] watchdog 超时推 done、容器清除（未实机单独验证）
- [x] 手动 X 隐藏 → 流仍进、再开几何正确
- [ ] 主窗切中/英 → 侧栏标题/设置项文案跟随（i18n 键已并入 LP_I18N）
- [ ] 回归：snapshot 首连、always_one、passthrough（未实机单独验证）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（+1 设置字段） |
| `src/relay/gui.py` | 改（+桥方法/snapshot/初始化） |
| `src/relay/panel_pool.py` | 改（统一 rid 模型重构 + 2 处修复） |
| `src/relay/web/live_panel.js` | 改（重写，合并 grid.js） |
| `src/relay/web/live_panel.html` | 改（结构替换 + 删 grid） |
| `src/relay/web/live_panel.css` | 改（列行容器 + 删 grid 死代码） |
| `src/relay/web/live_panel_grid.js` | **删除** |
| `src/relay/web/app.js` | 改（设置四件套四处接线） |
