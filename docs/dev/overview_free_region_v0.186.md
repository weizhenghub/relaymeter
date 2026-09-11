# 总览卡片「区域自由」级（v0.186）开发文档

> 给总览卡片新增一个独立的「自由模式」：每张卡可以脱离 flex 流，
> 用 X,Y 像素绝对定位，可以叠放到其它卡之上。点 resize 面板里的「📌 自
> 由」按钮切换。位置存 localStorage，刷新后还原。之前那个 flex 流的
> 模式正式命名为「默认模式」。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 查看上次会话内容

> 继续解决拖动无效问题

> 按下后还是跳变，然后拖动无效

> 鼠标移上边缘时，卡片上浮且鼠标样式变化。当按下后，卡片瞬间取消上浮且鼠标样式回归普通样式。

> 现在鼠标样式不会再跳变了。但是拖动没用

> 上下拖动很正确。但是左右完全拖不动，可能是因为旁边有贴着的容器顶住了不让移动。需要处理这种情况

> 拖动行为正确。不过能拖动到的还是只有三个位置，需要修正为20挡位

> 行为正确。写开发文档和更新日志。遵循约束

> 好的。鼠标移入区域的时候，边缘的变色可拖动指示条可以删去或透明化了

> 制作"区域自由"级。允许任何一个卡片，在空白位置悬浮，无需和其它卡片挨着，也允许叠放到其它卡片之上

> 保留当前样式。增加一个切换功能，可切换到刚才说的"自由模式"

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **保留所有现有样式不动**：v0.185 的 31 档高度、21 档宽度、命中区透明、CSS Grid→flex 切到布局 —— 全部不重写，只加自由模式增量 | 指令"保留当前样式" |
| B | **双模式独立可切换**：网格（默认）+ 自由两种模式共存，每张卡独立持有状态 | 指令"任何一个卡片...可切换到自由模式" |
| C | **复用现有 resize 面板 UI**：在原面板里加按钮，不开新面板，不引入新入口 | 指令"增加一个切换功能" |
| D | **X,Y 像素绝对定位**：脱离 flex 流，`position: absolute` + `top/left` inline style | 设计决定（拍板，符合 feedback_decide_show_effect） |
| E | **可叠放**：自由模式卡可以盖在其它卡之上，最近一次拖动的卡置顶 | 指令"允许叠放到其它卡片之上" |
| F | **位置持久化**：存 localStorage（沿用 `overview-card-sizes-v1`），刷新后还原 | 设计决定（沿用 v0.185 持久化体系） |
| G | **整卡可拖**：自由模式下，鼠标按住卡标题/卡体非交互区即可拖动整张卡 | 设计决定（拍板） |
| H | **边缘 resize 不受影响**：自由模式下仍然能拖边缘命中区改 span/height | 设计决定（不破坏现有功能） |
| I | **backward-compatible 持久化**：v0.185 及之前存的 map 无 `free` 字段，读时回退默认模式 | 已有用户必须不崩 |

### 隐含但需要自行决策的点

- 拖动期间是"逐次写 localStorage"还是"up 时一次性写" —— 选后者（v0.185 同套路，500ms 拖动只写 1 次存储）
- 拖动起点基准用"已存位置"还是"实测 grid 偏移" —— 选前者（无持久化时 fallback 到当前 grid 相对偏移）
- 自由卡叠放时 z-index 怎么管理 —— 用 module-scope 自增计数器 `_freeDragZCounter`，mouseup 时 ++ 写到 `card.style.zIndex`
- "重要约束"用 ⚠ 标记放显眼位置（遵守 feedback_doc_weight_pref + feedback_dev_doc_structure）

---

## 3. 分析需求后得出的开发路径

### 第一阶段：状态层（持久化 schema 扩展）

**需求**：在 `overview-card-sizes-v1` 这个 localStorage map 上加一个可选 `free: {x, y}` 字段，且不破坏旧用户。

**改动**：
- `getCardSize(key)` 返回值加 `free` 字段（缺省/null → 默认模式）
- `setCardSize(key, span, height, free)` 加第 4 参数 `free`，存盘时规范化 `{x, y}`（Number.isFinite 兜底）
- `applyCardSize(card)` 末尾追加 `applyCardFreePosition(card, s.free)` 调用

### 第二阶段：应用层（DOM 切换）

**`applyCardFreePosition(card, free)`**：
- `free={x, y}`：加 `.card-free-positioned` 类，写 inline `top/left`
- `free=null`：清 `.card-free-positioned` 类、清 inline `top/left`（不清 z-index —— 保留用户叠放层级）

### 第三阶段：交互层（按钮 + 拖动）

**面板按钮**：
- index.html 在 `#card-resize-panel` 里加 `<button class="card-free-toggle" data-card-free-toggle="1">📌 自由</button>`
- `syncResizePanel(card)` 渲染按钮 + 同步 active 类 + **写 `freeBtn.dataset.card = key`**（click 委托要拿这个 dataset）
- document 级 click 委托：响应 `data-card-free-toggle` 按钮，stopPropagation + 切换 + applyCardSize + syncResizePanel（刷新位置和按钮态）

**自由拖动**：
- 跟 v0.185 resize 同一套 window 级 capture listener（`mousedown`/`mousemove`/`mouseup` + `mouseleave` + `blur` 兜底）
- mousedown 三段过滤：(a) 卡必须是 `.card-free-positioned`；(b) 不在边缘命中区（让位 resize）；(c) 不在交互控件
- 临时 `card.draggable = false`（mouseup 还原）—— 跟 v0.185 同套路
- 拖动期间 `body.card-dragging-free`，CSS 给 cursor:move !important
- mouseup 写 localStorage + `card.style.zIndex = ++_freeDragZCounter`

### 第四阶段：CSS 增量

只追加新规则，**不改任何现有规则**：
- `.view[data-view="overview"] > .grid > .glass-card.card-free-positioned` —— position:absolute + margin:0 !important
- `body.card-dragging-free .glass-card.card-free-positioned` —— 禁过渡、禁 hover 上浮
- `body.card-dragging-free, ... * { cursor: move !important }` —— 拖动期间全卡 move 光标（不用 inherit，避开 v0.185 WebView2 陷阱）
- `.card-free-toggle` / `.card-free-toggle.active` —— 按钮样式（跟 `.card-span-btn` 同款）

### 第五阶段：CDP 实测验证

复用 v0.185 的 `verify_clean.bun.ts` 模式（WebSocket → Runtime.evaluate 派发 MouseEvent）。新建 `verify_free_region.bun.ts` 跑 6 个 Step 全过：
- 面板按钮存在
- 点击 toggle → 卡加类 + inline top/left + localStorage + active 态
- 拖动 left+200 / top+150 精确增量 + cursor: move + body 类
- 第二张自由卡叠放 z-index 递增
- 切回网格：清类 + 清 inline + 清 free + 按钮 inactive
- 回归：自由模式下边缘 resize 仍生效

---

## 4. 实现中遇到的问题

### 问题 1：index.html 按钮缺 `data-card` 属性 → click 委托拿不到 key

按钮静态 HTML 只写了 `data-card-free-toggle="1"`，没有 `data-card`。click 委托里 `btn.dataset.card` 是 `undefined` → `if (!key) return` 早返回。

**解法**：`syncResizePanel(card)` 里渲染按钮后追加 `freeBtn.dataset.card = key`（跟 v0.185 列跨度按钮的 dataset 处理一致）。

### 问题 2：CDP dispatchEvent 里 panel 弹不出来（v0.185 既有 bug）

document 级 click 委托同时挂了"点卡体弹面板"和"点外部关面板"两个 handler。点卡体时两个委托**同步按注册顺序**跑：

1. 弹面板委托 → `syncResizePanel(card)` 设 `panel.hidden = false`
2. 关闭面板委托 → 检查 `panel.hidden === false`（刚被步骤 1 设的）→ `panel.hidden = true`

CDP 里 `Runtime.evaluate` 跑 `dispatchEvent` 时所有委托同步跑，立刻互斥关掉。真实 WebView2 mouse 行为下两次 click 间有 microtask 间隔，所以用户没遇到。

**不影响生产**（真实用户用 WebView2 mouse 没问题），但 CDP 测试需要绕开：直接手动设 `btn.dataset.card = key` 后 click，绕过"先弹面板再关面板"的互斥链。

### 问题 3：`Runtime.evaluate` 不支持 `await new Promise`

测试表达式里 `await new Promise(r => setTimeout(r, 50))` 直接抛 `SyntaxError: Unexpected identifier "undefined"`（实际是 CDP 异步表达式不识别）。改成同步代码。

### 问题 4：WebView2 `cursor: inherit` 陷阱（继承自 v0.185 教训）

`body.x * { cursor: inherit !important }` 让 body 自身 cursor 变 inherit → 退化成 auto。自由拖动用 `cursor: move !important` 直接写具体值，避开。

### 问题 5：边缘 resize 跟自由拖动 mousedown 优先级

两者都挂 `window.addEventListener('mousedown', ..., true)`（capture 相位）。自由拖动 mousedown 必须**早返回**让位给 resize 命中区：

```js
if (t.closest("[data-card-edge-h], [data-card-edge-w], [data-card-edge-br]")) return;
```

否则同一 mousedown 既启动 resize 又启动 free-drag，_cardResize 状态错乱。CDP 验证 Step 6 确认：自由模式下点边缘命中区只挂 `card-resizing-w`，不挂 `card-dragging-free`，resize 走通。

### 问题 6：`z-index` 局部隔离

只给单张自由卡 `style.zIndex` 写自增计数器，**不能**给整个 `.grid` 加 z-index —— 否则 hover / 弹窗机制被打乱。Step 4 验证：先后拖两张卡，第二张的 z-index > 第一张（2 vs 3）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 / 行号 |
|---|---|---|
| #1 按钮缺 data-card | `syncResizePanel` 写 `freeBtn.dataset.card = key` | app.js:syncResizePanel |
| #2 CDP panel 互斥 | 测试绕过：手动设 dataset.card + click | verify_free_region.bun.ts |
| #3 await 不支持 | 表达式改纯同步 | verify_free_region.bun.ts |
| #4 cursor:inherit | `body.card-dragging-free * { cursor: move !important }` | styles-20260817.css |
| #5 mousedown 优先级 | 自由拖动 mousedown 早返回让位边缘命中区 | app.js:cardFreeDragPointerDown |
| #6 z-index 隔离 | module 计数器 + 单卡 inline style.zIndex | app.js:cardFreeDragPointerUp |

---

## 6. 是否完全遵循规划路径开发

**完全遵循**。

| 规划项 | 实际 |
|---|---|
| 保留所有现有样式不动 | ✅ 没改任何 v0.185 的 CSS / JS，只追加新规则 |
| 复用 resize 面板加按钮 | ✅ index.html 加一个按钮，syncResizePanel 渲染 + 状态同步 |
| X,Y 像素定位 + 自由切换 | ✅ `position: absolute !important` + `top/left` inline |
| 可叠放 + z-index 管理 | ✅ module 计数器 + 单卡 zIndex |
| localStorage 持久化 | ✅ `overview-card-sizes-v1` map 加 `free` 字段，backward-compatible |
| 整卡可拖 + 边缘 resize 不受影响 | ✅ 自由模式专属拖动 + resize 早返回让位 |
| Cursor 直接写具体值不用 inherit | ✅ `move !important` |

**没预测到的实际调整**：

- **(a) 自由拖动光标在 `*` 选择器下要 !important**：`.glass-card` 有 `cursor: grab`（特异性 0,3,0），`*` 选择器只有 (0,2,0)，必须靠 !important 抢后声明。规划里说"WebView2 inherit 陷阱"但没说 !important 必要性。
- **(b) `body.card-dragging-free` 必须挂在 body 上**：规划里写了但实现时想挂在卡上更精细，后来发现 drag 期间鼠标可能滑到卡外（.grid 空白），body cursor 兜底更稳。
- **(c) 第二次切回网格时 z-index 不清**：规划说"切回网格清掉 top/left + localStorage 项"，实际**不清 z-index** —— 用户叠放过的层级应当保留，避免切回网格时层级信息丢失。如果用户想要"完全重置"，可以刷新页面。这是体验上的小优化。

---

## 7. 最终实现点

### 1. localStorage schema 扩展（JS）

```json
{
  "hourly": { "span": 1, "height": 200, "free": { "x": 200, "y": 150 } },
  "today":  { "span": 1, "height": 0,   "free": null }
}
```

- `free: {x, y}` → 自由模式（Number 必须 finite）
- `free: null` 或字段缺省 → 默认模式
- 旧 v0.185 数据无 `free` 字段 → 读时回退默认模式（不崩）

### 2. DOM 切换（CSS）

```css
.view[data-view="overview"] > .grid > .glass-card.card-free-positioned {
  position: absolute !important;
  margin: 0 !important;
}
```

⚠ **`!important` 兜底特异性**：跟 v0.185 同坑 —— `.view[data-view="overview"] > .grid > .glass-card { flex: 0 0 auto }` 同特异性 (0,2,0) vs (0,2,0)，必须靠 !important 抢。

### 3. 自由拖动（JS）

`cardFreeDragPointerDown` → `cardFreeDragPointerMove` → `cardFreeDragPointerUp` 三件套，挂 window 级 capture listener（跟 `_cardResize` 平行）：

- mousedown 过滤：必须是 `.card-free-positioned` + 非边缘命中区 + 非交互控件
- 临时 `card.draggable = false`（mouseup 还原）
- 实时 `card.style.left/top = 起点 + dx/dy`（grid-relative 偏移）
- mouseup 写 localStorage + `card.style.zIndex = ++_freeDragZCounter`

### 4. 按钮 click 委托（JS）

document 级 click 委托响应 `data-card-free-toggle`：

- 网格 → 自由：setCardSize(key, s.span, s.height, {x, y})，y = 当前 rect.top - gridRect.top
- 自由 → 网格：setCardSize(key, s.span, s.height, null)
- stopPropagation + applyCardSize + syncResizePanel（刷新 active + 位置）

### 5. 光标（CSS）

⚠ **WebView2 cursor:inherit 陷阱**：直接写具体值，不用 inherit。

```css
body.card-dragging-free,
body.card-dragging-free .view[data-view="overview"] .glass-card.card-free-positioned,
body.card-dragging-free .view[data-view="overview"] .glass-card.card-free-positioned * {
  cursor: move !important;
}
```

`!important` 是因为 `.glass-card` 自身的 `cursor: grab` (0,3,0) 比 `*` (0,2,0) 更具体。

### 6. 面板按钮（HTML + CSS）

`index.html` 在 `#card-resize-panel` 加按钮：

```html
<button type="button" class="card-free-toggle"
        data-card-free-toggle="1"
        title="切换自由模式（脱离默认的 flex 流、可叠放）">📌 自由</button>
```

`syncResizePanel` 渲染时同步 `freeBtn.dataset.card = key`（click 委托靠这个 dataset 识别卡）。

### 行为验收清单（手动 + CDP）

- [x] 点 hourly 卡体弹面板，「📌 自由」按钮可见未 active
- [x] 点按钮 → 卡加 `.card-free-positioned` 类，inline top/left 已写
- [x] `localStorage["overview-card-sizes-v1"]` 包含 `free: {x, y}`
- [x] 在自由卡标题区 mousedown → 拖 +200,+150 → mouseup，left+200 / top+150 精确
- [x] 拖动期间 body.card-dragging-free 挂上 + cursor: move（卡 + body）
- [x] 第二张自由卡叠放 z-index 自增（2 vs 3）
- [x] 再点按钮 → 切回网格：卡退出类、inline 清空、localStorage.free = null
- [x] 自由模式下边缘 resize 仍生效（回归通过，`--ov-span` 正常变化）
- [x] v0.185 旧数据无 `free` 字段仍能正常加载（回退默认模式）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/styles-20260817.css` | 改（追加 `.card-free-positioned` / `body.card-dragging-free` / `.card-free-toggle` 规则，未改任何 v0.185 规则） |
| `src/relay/web/app.js` | 改（`getCardSize`/`setCardSize` 加 free 字段；`applyCardFreePosition` 新增；`applyCardSize` 末尾调一次；`syncResizePanel` 渲染按钮 + 写 dataset.card；document 级 click 委托响应 toggle；`cardFreeDragPointerDown/Move/Up` 新增三件套 + 挂 window 级 capture listener；自由模式下点卡体不弹 resize 面板） |
| `src/relay/web/index.html` | 改（resize 面板加 `<button class="card-free-toggle">`；CSS/JS 版本号 bump `20260826-02/03` → `20260826-03/04`） |
| `docs/dev/overview_free_region_v0.186.md` | 新增（7 节开发文档） |
| `docs/CHANGELOG.txt` | 新增 v0.186 条目 |
| `%TEMP%/h3/verify_free_region.bun.ts` | 新增（CDP 端到端验证脚本，26/26 全过） |

---

## v0.186.1 增量 — 自由按钮迁到「管理卡片」

### 用户反馈
> 将切换开关放到"卡片管理"选项中

### 改动要点
- `index.html`：resize 面板删掉按钮
- `app.js`：
  - `syncResizePanel` 删除按钮渲染逻辑
  - `openCardsManage` 改写：每行用 `<span class="cards-manage-title">` + `<button class="card-free-toggle" data-card="${key}"${on?"":" disabled"}>📌 自由</button>` 横排
  - 自由按钮 click 委托保留，但 toggle 后同步**弹层内**按钮的 active 态（不再调 syncResizePanel）
- `styles-20260817.css`：`.cards-manage-title { flex: 1 1 auto }` 让标题占满剩余宽度；`.card-free-toggle:disabled { opacity: 0.4; cursor: not-allowed }` 让未勾选卡灰显
- 版本号 bump `20260826-03/04` → `20260826-04/05`

### 验证
CDP `verify_free_region_v2.bun.ts`（**29 / 29 全过**）：弹层打开 + 11 行 + hourly 按钮启用 + 未勾选卡按钮 disabled + toggle + 拖动 + 切回 + 边缘 resize 回归。

---

## v0.187 增量 — 全局总开关 + 双槽独立记忆

### 用户反馈
> 一个按钮统一切换自由模式和默认模式，且两套机制用两个位置记忆配置

两点诉求，第二点是真正的痛点：

1. **逐卡开关 → 全局总开关**。v0.186 每张卡一个 📌 按钮，11 张卡要逐个点。用户想的是「整个总览页换一种布局玩法」，不是「这张卡自由、那张卡不自由」。
2. **两套配置互相踩踏**。v0.186 把 `free:{x,y}` 塞进 `overview-card-sizes-v1` 的同一条记录，两套模式**共用 span/height**。在自由模式把卡拉大拉小，切回默认模式后默认模式的尺寸也被改掉了，回不去原样。

### 存储：双槽（保留旧 key 当默认槽 → 老用户零丢失）

| key | 归属 | 形状 |
|---|---|---|
| `overview-card-sizes-v1`（沿用） | 默认模式 | `{key: {span, height}}` |
| `overview-card-order-v1`（沿用） | 默认模式 | `[key, ...]` |
| `overview-free-layout-v1`（新增） | 自由模式 | `{key: {span, height, x, y}}` |
| `overview-layout-mode-v1`（新增） | 全局标量 | `"default"` \| `"free"` |

⚠ 自由坐标改成**扁平存**（`x`/`y` 直接挂记录上），不再套一层 `free:{}`。默认槽从此**永不含**坐标字段。

### 关键设计：读写路由层（改动面收窄的诀窍）

不去逐个改 30+ 个调用点，而是在 `savedSize` / `getCardSize` / `setCardSize` 底下插一层 `_slotKey()` 路由：

```
_slotKey()  →  isFreeMode() ? FREE_LAYOUT_KEY : CARD_SIZE_KEY
```

于是 `applyCardSize` / 边缘 resize 收尾 / 列跨度按钮 / 自由拖动 mouseup **一行都不用改**，就自动读写「当前模式那一槽」。

### 顺手修掉 v0.186 一个潜伏 bug

`setCardSize(key, span, height)` 被两处调用时**只传 3 个参数**（`app.js` 列跨度按钮、边缘 resize 收尾）。v0.186 把缺省的 `free` 一律归一化成 `null` 写回 —— 等于**顺手把坐标擦了**，后果是自由模式下拖一下边缘改尺寸、或点一下列跨度，卡就**静默掉回默认模式**。

v0.187 把第 4 参改成三态语义：

| 传入 | 语义 |
|---|---|
| `undefined`（不传） | **保留**槽里已有的 x/y，只更新 span/height |
| `null` | 显式清掉坐标 |
| `{x, y}` | 写入新坐标 |

### 模式切换：`switchLayoutMode(next)`

首次进自由模式给自由槽 **seed** —— 以卡当前实测位置（相对 `.grid` 的偏移）为初始 x/y，span/height 从默认槽复制。用户切过去看到「布局原样没动，只是现在能拖了」，而不是所有卡塌到 (0,0) 叠成一坨。

⚠ **三步顺序不能乱**：seed 必须在 `_layoutMode` 改成 `"free"` **之前**量位置（此时卡还在 flex 流里，`getBoundingClientRect` 拿到的才是默认布局下的位置）；但必须在改模式**之后**写盘（`setCardSize` 按当前模式路由槽）。所以是 **先量 → 再切 → 后写**。

切换收尾：全卡重跑 `applyCardSize` → 默认模式额外 `applyCardOrder()`（自由模式下 absolute 定位，DOM 顺序不影响视觉，跳过）→ `lastCardsSig = null` + `renderAll`（卡宽变了，D3 图必须按新尺寸重算）。

### 实现中遇到的问题

**问题 1：`free: null` 的旧记录漏剥，破坏幂等性。** 迁移函数最初用 `legacy[k].free` 真值判断筛选待迁移记录。但 v0.186 对「默认模式的卡」也会显式写 `free: null` —— 这类记录的 `free` key 是假值，被真值判断漏掉，`free: null` 就永久留在默认槽里。后果有两个：破坏「默认槽永不含 free」的不变式；且下次迁移仍把它算作「有旧数据」，**幂等性也丢了**。改用 `"free" in legacy[k]`（判 key 存在而非真值），坐标只从真值里搬。这个 bug 是单测第 5 组抓出来的（37/38 那一次）。

**问题 2：CDP 端口不可用，改用源码抽取式单测。** 本轮 GUI 实例不是带 `--remote-debugging-port=9222` 启动的，9222 没监听；重启 GUI 会打断用户会话，不能做。改成 `test_dual_slot_v0187.js`：用 `fs.readFileSync` 读 `app.js`，按注释锚点**原样抽出**被测函数的源码文本，注入 mock `localStorage` 后 `new Function` 执行。好处是测的是**产品代码本体**，不是照抄的副本（照抄版本会掩盖真实 bug）。DOM 相关部分（seed 实测位置、`applyCardSize`）留给 CDP 脚本。

### 验证

`test_dual_slot_v0187.js`（**38 / 38 全过**，纯 Node，无需 CDP / 浏览器）：

| 组 | 覆盖 |
|---|---|
| 1–2 | 默认模式起点、写入只落默认槽、默认槽不含 x/y |
| **3** | **★核心★ 双槽隔离**：默认 span 1.3 → 自由改 2.5+(300,200) → 切回默认仍 1.3 → 再切自由 2.5+(300,200) 原样还原 |
| 4 | `free` 参数三态语义（undefined 保留 / null 清 / NaN 拒写）—— 即上面那个潜伏 bug |
| 5–7 | v0.186 旧数据迁移、幂等、不覆盖用户已有自由槽记录 |
| 8 | 模式持久化 + reload 还原 |
| 9 | 脏数据兜底（JSON 损坏 / `"null"` 不抛异常） |

CDP 脚本 `verify_layout_mode_v3.bun.ts` 已写好（9 组，含全局生效 / seed / 往返隔离 / 迁移 / resize 回归），待 GUI 以调试端口启动后可跑。

静态核对：`node --check app.js` 通过；CSS 花括号 881/881 平衡；`.card-free-positioned` / `body.card-dragging-free` 等 v0.186 自由模式规则 **git diff 显示 0 行改动**（未回归）。

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/app.js` | 改（新增 `FREE_LAYOUT_KEY`/`LAYOUT_MODE_KEY`/`_layoutMode`/`isFreeMode`/`saveLayoutMode`/`_slotKey`/`_readSlot`/`_writeSlot`/`_migrateFreeFieldOnce`/`switchLayoutMode`；`savedSize`/`getCardSize`/`setCardSize` 改走路由 + 三态 free 语义；`applyCardSize` 末尾按模式传记录；`openCardsManage` 改渲染总开关行；逐卡 toggle 委托 → `[data-layout-mode]` 委托；两处漏参调用补注释） |
| `src/relay/web/styles-20260817.css` | 改（`.card-free-toggle` 全套 → `.layout-mode-row`/`-label`/`-group`/`-btn` segmented control；v0.186 自由模式规则未动） |
| `src/relay/web/index.html` | 改（CSS/JS 版本号 bump `20260826-05/06` → `20260826-06/07`） |
| `docs/CHANGELOG.txt` | 新增 v0.187 条目 |
| `%TEMP%/h3/test_dual_slot_v0187.js` | 新增（源码抽取式单测，38/38） |
| `%TEMP%/h3/verify_layout_mode_v3.bun.ts` | 新增（CDP 端到端，待调试端口） |

---

## v0.187.1 增量 — 自由拖动边界钳制

### 用户反馈

> 自由也太自由了，咋还移动到左边的菜单栏上面去了？

### 现象

自由模式下把卡片往左甩，鼠标可以拖到 `.grid` 左边界外（甚至 `.sidebar` 上方）。`.sidebar` 在 v0.118 提到 `z-index: 500`（"给 sidebar 也升到 500，让整个侧栏层浮到 .main 之上"），掉进去的卡片被压住、既点不到也拖不到。**不可恢复**——唯一手段是手动清 localStorage。生产里等于一次拖动废掉一张卡。

### 根因

`cardFreeDragPointerMove`（app.js:12041）只算 `st.startLeft + dx`，无任何边界检查。`startLeft`/`startTop` 用的是「卡当前 inline style」的 `parseInt`（已可能 < 0，比如从旧 v0.186 数据迁移过来的、或拖过 0 轴后回的），所以 `+ dx` 后随便就是负的。

### 修复：`_clampFreePos(card, x, y)`

app.js:2159 起新增 **纯函数**（不依赖任何模块状态，注入 grid.clientWidth/clientHeight 即可 —— 适合源码抽取式单测）：

```js
const FREE_EDGE_KEEP = 48;
function _clampFreePos(card, x, y) {
  const grid = card && card.parentElement;
  if (!grid) return { x, y };
  const gw = grid.clientWidth;
  const gh = grid.clientHeight;
  let nx = x, ny = y;
  if (nx < 0) nx = 0;
  if (ny < 0) ny = 0;
  const maxX = Math.max(0, gw - FREE_EDGE_KEEP);
  const maxY = Math.max(0, gh - FREE_EDGE_KEEP);
  if (gw > 0 && nx > maxX) nx = maxX;
  if (gh > 0 && ny > maxY) ny = maxY;
  return { x: Math.round(nx), y: Math.round(ny) };
}
```

设计点：

- **左/上硬钳到 0**（不让卡片滑出 `.grid` —— 侧栏方向是问题的核心，必须硬约束，不能 "允许一点点负数"）
- **右/下钳到 `容器 - 48px`**：`FREE_EDGE_KEEP = 48` 保留一条可抓区，**避免卡完全贴到右/下边后鼠标无处落脚**（贴边后整张卡被 edge 命中区或自己挡住抓不住）
- **`Math.max(0, gw - FREE_EDGE_KEEP)` 兜底**：容器比保留量还小（极端窄窗口 / 未布局）时不出负数
- **`Math.round` 化**：避免 inline `style.left = "123.456789px"` 这种长小数污染
- **无 `parentElement` 时降级**（罕见，比如 detached card）：原值返回，不抛

### Wiring：3 处

| # | 位置 | 作用 |
|---|---|---|
| 1 | `cardFreeDragPointerMove`（app.js:12041） | **拖动实时钳制**——鼠标不管拖到哪，inline style 永远在 `.grid` 内 |
| 2 | `applyCardFreePosition` 加类后（app.js:2143） | **救援 v0.187 期间已经卡死在侧栏底下的旧卡**——reload 后直接拉回 `.grid` 内；顺带处理**窗口缩窄**导致原坐标越界 |
| 3 | 函数自身定义 | — |

⚠ **mouseup 写盘读的是 `card.style.left/top`**（已经被钳过的），所以 localStorage 里存的坐标天然合规 —— 下次 reload 不会再越界。不需要额外的"写盘前再钳一次"。

### 实现中遇到的问题

**问题 1：第一版 `maxX` 写得像凑数。** 我最初写 `Math.max(0, gw - Math.min(cw,gw) + Math.max(0, Math.min(cw,gw) - FREE_EDGE_KEEP))`（引入 card 的 offsetWidth/Height 做相对钳制），写完自己看懵了 —— 既不显然、也很可能是错的。简化成 `Math.max(0, gw - FREE_EDGE_KEEP)`（绝对钳到容器-保留量），删掉未用的 `cw`/`ch`。**教训**：边界条件代码要先问"读这段代码的人能一眼看出意图吗"，否则就是 bug 温床。

**问题 2：DOM 层验收缺位。** 本轮 GUI 不是带 `--remote-debugging-port=9222` 启动的（用户没重启，重启会打断 8088 中继对话），CDP 9222 无监听 → 不能跑 `verify_layout_mode_v3.bun.ts` 风格的端到端。源码抽取式单测能覆盖钳制算法本身，但**真实拖到侧栏底下卡不住 / reload 后旧卡被救援**这两条是 DOM 行为，留给用户在 GUI 里手动验。

### 验证

`test_clamp_v01871.js`（**12 / 12 全过**，纯 Node）：

| 组 | 覆盖 |
|---|---|
| 1 | 正常范围 (300,200) 透传不动 |
| **2** | **★核心★ 负坐标钳到 0**：`x=-250→0`（侧栏方向）、`y=-80→0`、极端 `(-9999,-9999)→(0,0)` |
| 3 | 右/下钳到 `容器-48`：(951,751) 刚好在界内不动；远超 → `952`/`752` |
| 4 | 极端窄容器：30x20、0x0（未布局）不出负数 |
| 5 | 小数 `123.456 → 123` round |
| 6 | `parentElement === null` 降级不抛 |

### 修改的文件清单

| 文件 | 类型 |
|---|---|---|
| `src/relay/web/app.js` | 改（新增 `FREE_EDGE_KEEP` + `_clampFreePos`；3 处 wiring；无新依赖、无 CSS 改动） |
| `src/relay/web/index.html` | 改（CSS/JS 版本号 bump `20260826-06/07` → `20260826-07/08`） |
| `docs/CHANGELOG.txt` | 新增 v0.187.1 条目 |
| `docs/dev/overview_free_region_v0.186.md` | 追加本段 |
| `%TEMP%/h3/test_clamp_v01871.js` | 新增（源码抽取式单测，12/12） |

---

## v0.187.2 增量 — SAFE_MARGIN 视觉安全边距 + seed overlap 防御

### 用户反馈（两个一起报）

1. > 自由也太自由了，咋还移动到左边的菜单栏上面去了？
2. > 首先初始的时候必须让每一个卡片都不重叠，后面怎么移动或重叠是用户的事情。其次现在还是会蔓延到左侧菜单栏

**用户对「蔓延」的语义变了**。v0.187.1 解决的是「卡滑进 `.sidebar` 底下、`.sidebar` z-index:500 盖住卡再也点不到」的**功能层 bug**（代码层已修：负坐标钳回 0，12/12 单测过）。但用户复测报「还是蔓延」 —— 真实痛点其实是**视觉层**：

> 「卡左边紧贴 `.grid` 左边缘 ≈ 紧贴 `.sidebar` 右边缘，看着像蔓延」

`.grid` 紧贴 `.main` 的 padding 内边距（`var(--spacing-xl)`），所以 `.grid` 左边缘离 `.sidebar` 右边缘只有**约 32px**。当卡 `x=0` 时视觉上紧贴侧栏。

### 修复：`FREE_SAFE_MARGIN = 16`

不是改 `.grid` padding（会缩窄默认模式卡片宽），**改钳制边界**：左/上各留 16px 起步，右/下保留 `SAFE_MARGIN + FREE_EDGE_KEEP = 64px` 的抓取区。

```js
const FREE_EDGE_KEEP = 48;
const FREE_SAFE_MARGIN = 16;   // ← 新增
function _clampFreePos(card, x, y) {
  // ...
  if (nx < FREE_SAFE_MARGIN) nx = FREE_SAFE_MARGIN;
  if (ny < FREE_SAFE_MARGIN) ny = FREE_SAFE_MARGIN;
  const maxX = Math.max(FREE_SAFE_MARGIN, gw - FREE_SAFE_MARGIN - FREE_EDGE_KEEP);
  const maxY = Math.max(FREE_SAFE_MARGIN, gh - FREE_SAFE_MARGIN - FREE_EDGE_KEEP);
  if (gw > 0 && nx > maxX) nx = maxX;
  if (gh > 0 && ny > maxY) ny = maxY;
  return { x: Math.round(nx), y: Math.round(ny) };
}
```

⚠ **16 不是随便定的**：太小（<8）看不出区别，太大（>32）会明显压缩可布局区。16 ≈「一眼能看出来有间距但又不挤」。

### 修复：seed 后 overlap 防御（满足「初始不重叠」）

`switchLayoutMode` seed 阶段加了步骤 3.5：seed 写盘后、applyCardSize 之前跑 O(n²) overlap nudge：

```js
// --- 3.5 seed 后做一次 overlap 防御 ---
if (seed && grid) {
  const cards = [];
  grid.querySelectorAll(".glass-card[data-card]").forEach(el => {
    const key = el.dataset.card;
    if (!key || !(key in _readSlot(FREE_LAYOUT_KEY))) return;
    const rec = _readSlot(FREE_LAYOUT_KEY)[key];
    cards.push({ key, el, x: rec.x, y: rec.y, w: el.offsetWidth, h: el.offsetHeight });
  });
  let moved = true;
  for (let pass = 0; pass < 16 && moved; pass++) {
    moved = false;
    for (let i = 0; i < cards.length; i++) {
      for (let j = i + 1; j < cards.length; j++) {
        const a = cards[i], b = cards[j];
        if (a.x < b.x + b.w - 1 && a.x + a.w - 1 > b.x &&
            a.y < b.y + b.h - 1 && a.y + a.h - 1 > b.y) {
          b.y = a.y + a.h + 12;
          const p = _clampFreePos(b.el, b.x, b.y);
          b.x = p.x; b.y = p.y;
          moved = true;
        }
      }
    }
  }
  if (moved || cards.some(c => c.y !== seed.find(s => s.key === c.key).y)) {
    const m2 = _readSlot(FREE_LAYOUT_KEY);
    for (const c of cards) {
      if (m2[c.key]) { m2[c.key].x = c.x; m2[c.key].y = c.y; }
    }
    _writeSlot(m2, FREE_LAYOUT_KEY);
  }
}
```

设计点：

- **理论上不该触发**：默认模式 flex 流天然不重叠；钳制又把越界卡往左推，反而可能挤到另一张。
- **触发场景**：(a) 窗口极窄时 flex 把卡挤到 grid 边界外，钳回 SAFE_MARGIN 后若干卡挤一堆；(b) 有用户改过 span > 1 的卡，跟其它卡高度差大时 flex 行高 + 换行偶发重叠。
- **只挪后者**：保持 flex 流的相对顺序不变（前者位置不动），只把后挤到的卡往下挪一行（`y += 前者 h + 12`）。
- **最多 16 轮**：扫完无变化就 break。极端情况（比如 11 张全叠在一起）可能还要多轮，但 16 轮对真实场景绰绰有余。
- **结果写回槽**：nudge 后更新 `overview-free-layout-v1`，下次 reload 直接从这套坐标起步。
- **保证「切到自由模式那一瞬视觉不重叠」** —— 之后用户怎么拖是用户的事。

### 实现中遇到的问题

**问题 1：测试本身笔误。** 第一版 `test_clamp_v01872.js` 写了两个会失败但不是 bug 的断言：(a) 容器 `0x0` 时期望钳到 SAFE_MARGIN，实际 `gw=0` 时代码路径 `if (gw > 0 && ...)` 不钳（保留原值 —— 容器未布局不应强行约束）；(b) 「maxX 上限 = 936」断言写成 `p === undefined`，但 `p` 早被前一行赋值了。修测试用例（不改产品代码），18/18 全过。

**问题 2：DOM 层验收缺位仍存。** 跟 v0.187.1 一样：用户没起调试端口、CDP 9222 无监听 → seed 后第一帧视觉是否真的 SAFE_MARGIN / overlap nudge 是否真生效，得用户在 GUI 里手动验（Ctrl+R 重载看效果）。

### 验证

`test_clamp_v01872.js`（**18 / 18 全过**，纯 Node）：

| 组 | 覆盖 |
|---|---|
| 1 | 正常范围 (300,200) / 边界 (16,16) 透传不动 |
| **2** | **★核心★ 负坐标钳到 SAFE_MARGIN**：`x=-250→16`、`y=-80→16`、极端 `(-9999,-9999)→(16,16)`、`(-1,-1)→(16,16)`、`(0,0)→(16,16)` |
| 3 | 右/下钳到 `容器-SAFE_MARGIN-EDGE_KEEP`：(935,735) 刚好界内；远超 → 936/736 |
| 4 | 极端窄容器 (30x20) 钳到 SAFE_MARGIN；0x0 不强行约束 |
| 5 | 小数 round |
| 6 | `parentElement === null` 降级不抛 |
| 7 | 边界相等性（x=950→936、x=16 不动） |

`test_dual_slot_v0187.js` 仍 38/38 全过（双槽逻辑未回归）。

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/app.js` | 改（`_clampFreePos` 加 `FREE_SAFE_MARGIN = 16`；`switchLayoutMode` 步骤 3.5 加 overlap nudge） |
| `src/relay/web/index.html` | 改（CSS/JS 版本号 bump `20260826-07/08` → `20260826-08/09`） |
| `docs/CHANGELOG.txt` | 新增 v0.187.2 条目 |
| `docs/dev/overview_free_region_v0.186.md` | 追加本段 |
| `%TEMP%/h3/test_clamp_v01872.js` | 新增（源码抽取式单测，18/18） |


## v0.187.3 增量 — 定位基准根因修复（`position:relative` on `.grid`）

### 用户反馈

> 行为并没有正确，上一轮的还是会溢出到菜单栏

v0.187.1/187.2 已经把 `_clampFreePos` 钳制逻辑做到单测 18/18 全过（硬边界 + SAFE_MARGIN 视觉边距 + EDGE_KEEP 抓取区），**行为依旧错**。这不是钳制不够，而是更底层的**定位基准 bug** —— 方向治错了，SAFE_MARGIN 调再大也是徒劳。

### 根因：`offsetParent` 落到了 `body`，JS 坐标语义与 CSS 解释基准不一致

**JS 全链路把卡 `left/top` 当「相对 `.grid` 的偏移」**：
- seed 阶段：`x: r.left - gr.left`（viewport 相减 → grid-relative）
- 拖动：`startLeft: parseFloat(card.style.left)` 直接当 grid-relative；new = `startLeft + dx`
- 存槽：`// 这是 grid-relative 偏移，applyCardFreePosition 读出来时直接当 left/top 用`
- 钳制：`_clampFreePos(grid.clientWidth...)` 以 `.grid` 尺寸为基准

**但 CSS 从没给 `.grid`（或其任何祖先）设 `position`**。DOM 链：
`#app(flex) > main.main(flex,scroll) > section.view[data-view=overview](block) > div.grid(flex) > div.glass-card(position:relative)`

越往上找，`#app` / `.main` / `.view` 全链无一处 `position`，所以卡 `position:absolute !important` 后 `offsetParent` 只得落到 **`body`**（满视口 flex 布局）。

**后果**：`.grid` 相对 `body` 的偏移 = `(sidebar 宽, topbar 高)`。卡 `left/top` 本应相对 grid、却被解释成相对 body，导致**所有自由模式卡系统性右下偏移一个常量**。侧栏在 grid 左侧，所以 `left=SAFE_MARGIN(16)`（相对 body）落在 grid 左边 = **侧栏区域** → 视觉「蔓延到侧栏」。

> 一句话：`_clampFreePos` 钳的是 grid-relative 坐标，浏览器却把它当 relative-to-body，坐标基准错位，钳 16px 还是进侧栏。

### 修复：给 overview `.grid` 加 `position: relative`

```css
.view[data-view="overview"] > .grid {
  display: flex;
  flex-wrap: wrap;
  position: relative;   /* ← 新增：建立定位上下文，offsetParent=.grid */
  gap: 16px !important;
  align-items: flex-start;
}
```

**为什么一处就够**：`.grid` 成为定位祖先后，卡 `position:absolute` 的 `offsetParent = .grid`，`left/top` 相对 grid → 与 JS 的 seed（`r.left-gr.left`）、拖动（`startLeft+dx`）、存取槽（grid-relative 偏移）**全部对齐**。钳制 `_clampFreePos` 的 `clientWidth` 基准也正好匹配。**JS 一行未改。**

### 边界情况核对

| 场景 | 影响 |
|---|---|
| `.main` 是 scroll 容器 | grid 非 overflow 容器，加 relative 不影响滚动 |
| zoom | `_clampFreePos` 用 `clientWidth`（不含 transform scale），不冲突 |
| `.glass-card:hover` transform 上浮 | transform 不改变 offsetParent，无影响 |
| z-index 叠放 | 卡 absolute 后仍能设 zIndex；grid 无 z-index 不改 stacking |
| 5301 `.view[data-view="overview"] .grid` | 只写 `gap:6px`，无 position，特异性 (0,2,0) 后出现但不覆盖 |

### 实现中遇到的问题

**问题 1：为什么前两轮没发现？** 单测、SAFE_MARGIN、钳制全做对了，但测试是**源码抽取 + mock 注入**，mock 的 `grid.clientWidth/clientHeight` 绕过了真实 layout，暴露不出「offsetParent 落到 body」这种只在实际渲染才出现的基准错位。要抓这类 bug 得看真实 DOM/渲染，CDP 或实机才能验。

**问题 2：为什么不改 JS 去读 offsetLeft？** `card.offsetLeft` 相对 offsetParent，但 offsetParent 是 body —— 要改成相对 grid 得 `offsetLeft - grid.offsetLeft`，或者统一按 body 算。那是在「接受错误基准」上打补丁，属于修错方向。正确做法是让 CSS 基准对齐 JS 语义（加 relative），一处根治。

### 验证

- `%TEMP%/h3/test_clamp_v01872.js` 仍 **18 / 18** 全过（未改钳制函数本体，纯 CSS 修复）。
- CSS 花括号平衡 881/881。
- **实机**（用户 GUI 操作，未主动重启 8088 进程）：
  - 切自由模式，确认卡初始布局与默认模式一致（不再整体右偏下偏）。
  - 拖一张卡向左，确认停在 `.grid` 左边缘留出 SAFE_MARGIN 空隙，**不进侧栏**。
  - 拖到最顶/最底/最右，确认都被钳在容器内。
  - 切回默认再切回自由，位置记忆正确。

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/styles-20260817.css` | 改（overview `.grid` 加 `position: relative` + 根因注释） |
| `src/relay/web/index.html` | 改（CSS/JS 版本号 bump `20260826-09/10` → `20260826-10/11`） |
| `docs/CHANGELOG.txt` | 新增 v0.187.3 条目 |
| `docs/dev/overview_free_region_v0.186.md` | 追加本段 |


