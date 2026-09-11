# 磁吸动效 / 实时流全上游空白 / 模型菜单两栏 / 卡片悬浮层级（v0.118）开发文档

## 1. 用户的初始指令

> 给按钮和开关新增此动效：`.magnetic { transition: transform 0.3s cubic-bezier(0.2, 0.8, 0.2, 1); }`

> 设置中的开关，系数调大到0.5。特殊样式按钮也要做磁吸。

> 特殊按钮（三级，文字）动效依然没有

> 刚用的 hy3-free 这个上游，流式的数据没有显示在实时流窗口怎么回事？……不对，不只是这个，deepseek 官方的都没有流式了，应该是上次会话修改了什么代码出问题了

> 左下角模型选择菜单栏：1、展开后左右两栏间距过宽，修复   2、允许左右两栏滚动独立

> 卡片的悬浮层级也不够高，现在会被主窗口右边的其它卡片盖到下面。请你添加 todo，解决完刚才的问题后再解决这个。

> 我说了先创建任务列表，按顺序解决，这是后续任务

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 给按钮 + 开关加磁吸动效（`.magnetic` transition） | 指令 1 |
| B | 设置中开关的磁吸系数 → 0.5 | 指令 2 |
| C | 特殊样式按钮也要磁吸 | 指令 2 |
| D | 三级文字按钮（设置子菜单 / 侧栏主导航 / prefs 池 chip）也要磁吸 | 指令 3 + AskUserQuestion 澄清 |
| E | 修复实时流窗口对所有上游都不显示流式数据的问题（上次重构遗留） | 指令 4 |
| F | 模型菜单展开后左右两栏间距过宽 → 收窄 | 指令 5 |
| G | 模型菜单左右两栏允许独立滚动 | 指令 5 |
| H | 卡片悬浮层级不够高 → 提升，不被右侧卡盖住 | 指令 6 |
| I | 先建任务列表，按顺序解（菜单 → 卡片层级），其余为后续任务 | 指令 7 |

### 隐含但需要确认的点（已通过澄清确认）

- **「三级文字按钮」指哪三级**：经 AskUserQuestion 确认 = 设置子菜单 `.nav-sub-item` + 侧栏导航 `.nav-item` + prefs 池 chips `.prefs-pool-chip`。
- **实时流空白范围**：不是单上游，而是全上游（hy3-free、deepseek 官方都坏）→ 指向公共订阅/广播链路，而非某个 wire 分支。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：磁吸动效（CSS + JS 双端）

磁吸的实现范式（前序会话已定，本会话沿用并扩展选择器）：

- **CSS 端**：`@property` 注册 `--mag-x` / `--mag-y` 两个 `<length>` 变量；`.magnetic` 基类挂 `transition: transform 0.3s cubic-bezier(0.2,0.8,0.2,1)`；各具体控件在 `:hover` / `:active` 时 `transform: translate(var(--mag-x), var(--mag-y))`。
- **JS 端**：`initMagnetic()` 用 `document` 级 `mousemove` 委托（**必须 `bubbles:true`**，否则 headless 合成事件不触发），命中 `SEL`（带 `:not(.magnetic)` 去重）时把指针相对元素中心的偏移写入 `--mag-x` / `--mag-y`。

按控件分组落地：

| 控件 | 系数 | 落地方式 |
|---|---|---|
| 普通 `.btn` | 1（默认强度） | `.btn.magnetic:hover` translate |
| 设置开关 `.switch` | **0.5** | `--mag-strength: 0.5` + transform（指令 B） |
| 特殊按钮 `.wire-btn / .quick-switch-btn / .consume-switch-btn / .cards-manage-fab` | 1 | 同 .btn 处理（指令 C） |
| 三级文字 `.nav-item / .nav-sub-item` | 1 | `.nav-*.magnetic:hover` translate（指令 D） |
| prefs 池 `.prefs-pool-chip` | 1 | `.prefs-pool-chip.magnetic:hover` translate（指令 D） |

### 第二阶段：实时流全上游空白（后端 bug）

链路：`gui.py _sse_loop` → GET `/live/stream` → `InflightStore.subscribe()` → 队列收 `_broadcast` → `evaluate_js('relayLiveEvent(...)')` → `live_panel.js`。

上次重构把 `proxy.py` 拆成 `proxy_legacy.py` + `proxy/` 子模块，并引入 `LiveBus` 与 `InflightStore`。`routers/stats.py:472` 的 `/live/stream` 走 `inflight.subscribe()`，但 `InflightStore` 缺 `subscribe/unsubscribe/publish` → `AttributeError` → 每次连接 500 → 实时面板对所有上游全空白。

**解法**：在 `InflightStore` 补三个方法，委托回 legacy 广播（`proxy_legacy._subscribe_live_stream / _unsubscribe_live_stream / _broadcast`）。理由：`relay()` 仍在向 legacy `_SUBSCRIBERS` 广播，订阅侧必须接同一队列，不能另起一套。补测试驱动 ctx 分支验证 subscribe→legacy 广播链路。

### 第三阶段：模型菜单两栏（CSS）

`.upstream-menu` 当前是 `position:fixed` 浮层（left/bottom 由 JS 按 picker 视口坐标设），`display:grid`。

- **间距过宽（F）**：根因 `grid-template-columns: repeat(auto-fit, minmax(0, 1fr))` → 等宽 `1fr` 列被 `min-width:400px`（后降到 280px）撑开成两列各 ~328px、短文字左对齐 → 视觉上两栏被推得太开。改为 `grid-template-columns: max-content max-content`，列宽按内容收缩，anthropic / openai 两栏贴近。
- **独立滚动（G）**：把 `max-height + overflow-y:auto` 从整菜单下放到每个 `.upstream-menu-group`，两栏各自滚动，互不挤压。

### 第四阶段：卡片悬浮层级（CSS）

`.glass-card:hover` 用 `translateY(-3px)` 上浮，但无 `z-index`，DOM 靠后的右侧相邻卡默认叠在上层 → 抬起的那张被压在下面。**解法**：`.glass-card:hover { z-index: 5 }`。

### 任务顺序

按指令 I 建 todo：`#3 模型菜单两栏` → `#1 卡片悬浮层级` → `#4 写文档` → `#5 更新日志`。

---

## 4. 实现中遇到的问题

### 问题 1：磁吸对「三级文字按钮」不生效

前序会话只给 `.btn` / `.switch` / 特殊按钮加了 `initMagnetic` 选择器，`.nav-item` / `.nav-sub-item` / `.prefs-pool-chip` 不在 `SEL` 里 → 指令 D 要求后仍无动效。

**解法**：`SEL` 追加 `.nav-item:not(.magnetic)` / `.nav-sub-item:not(.magnetic)` / `.prefs-pool-chip:not(.magnetic)`；并补对应 CSS 规则（hover translate + transition 含 magnetic 曲线）。

### 问题 2：实时流对所有上游 500（AttributeError）

`/live/stream` 调 `InflightStore.subscribe()`，但重构后该类没有此法 → 500。不是 wire 分支问题（否则只坏某上游），而是公共订阅入口坏 → 全上游流式空白。

**解法**：`InflightStore` 增 `subscribe/unsubscribe/publish`，委托 `proxy_legacy` 的 legacy 广播（详见 §5）。

### 问题 3：headless 合成 mousemove 不触发委托

验证磁吸时，用 `new MouseEvent("mousemove",{clientX,clientY})` 默认 `bubbles:false`，`document` 级 listener 收不到 → 计数 0，误判动效没生效。

**解法**：合成事件必须 `bubbles: true`；修正后验证通过（CSS 版 bump 至 `?v=20260823-29` 阶段）。

### 问题 4：菜单列宽改了 `1fr` 仍宽（首轮漏改）

首轮只把 `min-width:400→280`、移除整菜单 `max-height/overflow`，但 `grid-template-columns` 仍是 `repeat(auto-fit, minmax(0,1fr))` → headless 复测每列仍 328px，间距问题未解。

**解法**：改 `grid-template-columns: max-content max-content`；复测 g1=114px / g2=102px / gap=6px，问题解决。

### 问题 5：复测 `menuWidth:676` 假阳性

headless 用 `position:relative` 的 overlay 验证页测菜单宽度报 676px，而内容只到 269px。

**解法**：判定为 overlay 里 `position:relative`（block，全宽拉伸）的假象；真实 app 用 `position:fixed` 会 shrink-wrap 到紧凑列。检查 `app.js` 无强制宽度代码，确认安全。

### 问题 6：卡片层级 `z-index` 缺失

`.glass-card` 无 `z-index`，hover 上浮后被右侧同层（DOM 靠后）卡覆盖。

**解法**：`.glass-card:hover { z-index: 5 }`（详见 §5）。

### 问题 7：菜单仍被总览/统计卡片盖住（首轮修错对象）

首轮以为菜单被盖是卡片 `z-index` 问题，给 `.glass-card:hover` 加 `z-index:5` 后**问题依旧**。根因：`.sidebar` 是 `position:sticky` → 自成 stacking context，把内部 `.upstream-menu` 的 `z-index:500` 关在侧栏层里；而 `.main` 在 DOM 里排在 sidebar 后面、卡片 hover 又有 `z-index:5`，于是菜单整体被主区卡片压住，菜单 z-index 调到再高也无济于事（被 sidebar 层封顶）。

**解法**：给 `.sidebar` 也设 `z-index:500`，让整个侧栏层浮到 `.main` 之上（详见 §5）。

### 问题 8：菜单独立滚动失效、条目被压扁挤成一坨

`.upstream-menu-group` 是 `display:flex; flex-direction:column`，其子项 `.upstream-menu-item` 默认 `flex-shrink:1`，在 `max-height:240px` 约束下被压扁挤成一坨，而非保持自然高度溢出触发滚动条。

**解法**：`.upstream-menu-item` / `.upstream-menu-group-label` 加 `flex-shrink:0`，禁止收缩，条目按自然高度溢出 → 触发 `overflow-y:auto` 滚动（详见 §5）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 三级文字按钮无磁吸 | `SEL` 追加三类选择器 + 补 CSS hover/transition 规则 | app.js / styles-20260817.css |
| #2 全上游实时流 500 | `InflightStore` 增 `subscribe/unsubscribe/publish` 委托 `proxy_legacy` 广播 | services/inflight_store.py |
| #3 合成事件不触发 | `MouseEvent` 加 `bubbles:true` | 验证脚本 |
| #4 列宽仍宽 | `grid-template-columns: max-content max-content` | styles-20260817.css |
| #5 宽度假阳性 | 判定 overlay 伪影，查 JS 无强制宽度，确认安全 | 验证 |
| #6 卡片被盖 | `.glass-card:hover { z-index:5 }` | styles-20260817.css |
| #7 菜单被卡片盖住 | `.sidebar { z-index:500 }`（sticky 自成层，需抬升侧栏层而非菜单） | styles-20260817.css |
| #8 条目被压扁无滚动 | `.upstream-menu-item` / `.upstream-menu-group-label` 加 `flex-shrink:0` | styles-20260817.css |

**关键代码片段（inflight_store.py）：**
```python
def subscribe(self) -> "asyncio.Queue":
    from ..proxy_legacy import _subscribe_live_stream
    return _subscribe_live_stream()
async def unsubscribe(self, q: "asyncio.Queue") -> None:
    from ..proxy_legacy import _unsubscribe_live_stream
    await _unsubscribe_live_stream(q)
async def publish(self, event: dict) -> None:
    from ..proxy_legacy import _broadcast
    await _broadcast(event)
```

**关键 CSS（upstream-menu 两栏 + 层级 + 滚动）：**
```css
.sidebar {
  position: sticky;
  z-index: 500;                                   /* 抬升侧栏层，菜单才能盖住卡片 */
}
.upstream-menu {
  min-width: 280px;
  display: grid;
  grid-template-columns: max-content max-content; /* 间距过宽修复 */
  gap: 6px;
  align-items: start;
}
.upstream-menu-group {
  min-width: 0;
  display: flex; flex-direction: column;
  max-height: 240px; overflow-y: auto;            /* 独立滚动 */
}
.upstream-menu-item,
.upstream-menu-group-label { flex-shrink: 0; }     /* 禁止压扁，溢出才滚动 */
.glass-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--card-shadow-hover), 0 10px 24px rgba(0,0,0,.08);
  z-index: 5;                                     /* 卡片 vs 卡片悬浮层级 */
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划（无偏离）：

- 磁吸动效覆盖全部要求控件：按钮 / 开关（系数 0.5）/ 特殊按钮 / 三级文字按钮（指令 A–D 全部实现）。
- 实时流全上游空白修复：定位到 `InflightStore` 缺 subscribe，委托 legacy 广播，未另起订阅体系（符合「复用同一广播队列」约束）。
- 模型菜单两栏：间距收窄（`max-content`）+ 独立滚动（`group` 级 `overflow`）均落地（指令 F、G）。
- 卡片悬浮层级：`z-index:5` 提升，不被右侧卡覆盖（指令 H）。
- 任务顺序：建 todo 后严格按「菜单 → 卡片层级 → 文档 → 日志」推进（指令 I）。

### 偏离之处：

- **(a) 菜单首轮漏改 `grid-template-columns`**：先只动 `min-width` 和整菜单 `overflow`，复测发现列仍 328px 才补 `max-content`。属实现次序补刀，非方向偏离。
- **(b) AskUserQuestion 澄清「三级按钮」语义**：指令 D 原文模糊，主动追问确认 = 设置子菜单 / 侧栏导航 / prefs 池 chip 三级。符合「有歧义先确认」的协作约定。
- **(c) 菜单被盖首轮修错对象**：先误判为卡片 `z-index` 问题（给 `.glass-card:hover` 加 `z-index:5`），复测依旧被盖，才定位到 `.sidebar` sticky 自成 stacking context 封顶菜单 z-index，改为抬升 sidebar 层。**首轮方向偏差，二轮修正。**
- **(d) 独立滚动首轮压扁条目**：首轮只加 `max-height + overflow-y:auto`，未考虑 flex 子项 `flex-shrink:1` 会压扁而非溢出，复测条目挤成一坨才补 `flex-shrink:0`。**实现细节遗漏，二轮补齐。**

### 重大调整：无。

---

## 7. 最终实现点

### 前端（CSS / JS）

1. **`styles-20260817.css` 磁吸体系**（沿用 + 扩展）：
   - `@property --mag-x / --mag-y` 注册。
   - 基类 `.magnetic { transition: transform 0.3s cubic-bezier(0.2,0.8,0.2,1); }`。
   - 覆盖：`.btn` / `.switch`（`--mag-strength:0.5`）/ `.wire-btn` / `.quick-switch-btn` / `.consume-switch-btn` / `.cards-manage-fab` / `.nav-item` / `.nav-sub-item` / `.prefs-pool-chip`。

2. **`app.js` `initMagnetic()`**：
   - `SEL` 含全部上述选择器（带 `:not(.magnetic)` 去重），`EXCLUDE = ".win-ctrl, .modal-close"`。
   - `document` 级 `mousemove` 委托写 `--mag-x/--mag-y`；`bubbles:true` 保证合成事件可达。

3. **`styles-20260817.css` 模型菜单**：
   - `.upstream-menu` `grid-template-columns: max-content max-content` + `min-width:280px` + `align-items:start`。
   - `.upstream-menu-group` `max-height:240px; overflow-y:auto`（独立滚动）。
   - `.upstream-menu-item` / `.upstream-menu-group-label` `flex-shrink:0`（禁止压扁，溢出才滚动）。

4. **`styles-20260817.css` 层级**：
   - `.sidebar { z-index:500 }`：sticky 自成 stacking context，抬升侧栏层使菜单能盖住 `.main` 卡片（菜单被盖的根因修复）。
   - `.glass-card:hover { z-index:5 }`：卡片 vs 卡片悬浮层级。

5. **`index.html`**：CSS cache `?v=20260823-29` → `?v=20260823-31`；JS 版本随会话推进。

### 后端（Python）

6. **`services/inflight_store.py`**：
   - 补 `subscribe() / unsubscribe() / publish()` 三法，委托 `proxy_legacy` 的 legacy 广播（`_subscribe_live_stream` / `_unsubscribe_live_stream` / `_broadcast`）。
   - 修复 `/live/stream` 对全部上游 500，实时面板恢复显示流式。

7. **`tests/test_live_stream.py`**：
   - 新增 `test_live_stream_ctx_branch_registers_subscriber`：驱动 ctx 分支验证 subscribe→legacy 广播链路（能订阅、能收广播、关闭后无泄漏）。
   - 全部 12 个 live stream 测试通过。

### 行为验收清单（手动测试项）

- [ ] 普通按钮 / 开关 hover 有磁吸跟随
- [ ] 设置页开关磁吸幅度明显大于普通按钮（系数 0.5 生效）
- [ ] 特殊按钮（wire / quick-switch / consume-switch / cards-manage-fab）hover 磁吸
- [ ] 设置子菜单项 / 侧栏主导航 / prefs 池 chip hover 磁吸
- [ ] 任意上游（hy3-free / deepseek 官方 / openai 等）流式数据实时面板正常显示
- [ ] 模型菜单展开：anthropic 与 openai 两栏贴近（间距已收窄）
- [ ] 每栏条目超长时各自独立滚动，互不影响（条目不被压扁）
- [ ] 菜单展开后盖在总览卡片 / 统计页 UI 之上（不被压到下面）
- [ ] 卡片 hover 上浮且盖在右侧相邻卡之上（不被覆盖）
- [ ] GUI 重启后设置/行为一致

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/styles-20260817.css` | 改（磁吸扩展 + 菜单两栏/滚动 + sidebar z-index + 卡片 z-index） |
| `src/relay/web/app.js` | 改（`initMagnetic` 选择器扩展至三级文字按钮） |
| `src/relay/web/index.html` | 改（JS/CSS cache 版本 bump） |
| `src/relay/services/inflight_store.py` | 改（补 subscribe/unsubscribe/publish 委托 legacy 广播） |
| `tests/test_live_stream.py` | 改（+ ctx 分支回归测试） |
