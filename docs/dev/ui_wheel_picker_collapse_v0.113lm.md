# Wheel Picker 折叠态 + 悬停展开（v0.113l + v0.113m）开发文档

> 设置页「思考流超时 / 思考-正文衔接超时 / 正文流超时」三档的自绘轮盘选择器
> 默认折叠成 32px 单格（只显当前值），鼠标悬停展开 76px 三条值 + 中间高亮带，
> 移出 150ms 延迟收缩。滚轮 / 点击上下半区 / 拖动切值。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 滚轮选择器不要一直展开，单格更紧凑；鼠标悬停再展开

（本轮接续 v0.113k wheelPicker 自绘实现的迭代；用户看了实际渲染后觉得
一直展开占太多垂直空间，希望默认折叠、需要时再展开。）

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **默认折叠** —— 不悬停时只显当前值一格（32px），不显上/下行与高亮带 | 指令 |
| B | **悬停展开** —— 鼠标进入 → 展开 76px 三条值 + 中间高亮 | 指令 |
| C | **移出折叠** —— 鼠标离开 → 折叠 | 指令（隐含） |
| D | **滚轮 / 点击 / 拖动在展开态生效** —— 折叠态不接受输入 | 指令（隐含） |

### 隐含但需自行决策的点

- **展开态高度 76px**：3 × ~24px（ROW=24）+ border + padding，整 76px 容纳
  三行 + 中间高亮带。折叠 32px：只 1 行 + padding。CSS `transition: height .15s`。
- **展开 / 折叠延迟**：移出立刻缩 → 用户从展开态往相邻控件移时容易「划过」
  触发收缩闪一下。加 150ms 延迟收缩（v0.113m）。悬停**不延迟**展开（用户意图明确）。
- **global click / Esc 收所有展开** —— 既有的 `_wpGlobalBound` 全局 click +
  Escape 监听依然工作（折叠态也点外面不会触发 `closest(".wheel-picker")` 短路）。
- **不持久化展开态**：每次 mount 都是折叠，临时 UI 状态不入 .env / upstreams.json。
- **统一行为**：设置页三个 wheel-picker（thinking / gap / text）共用同一套 CSS +
  同一段 JS hover/leave 逻辑，无 per-host 差异。

---

## 3. 分析需求后得出的开发路径

```
app.js (wheelPicker 函数，v0.113k 已有)
  + mouseenter 监听：open()
  + mouseleave 监听：150ms setTimeout(closeAll)
  既有 isOpen() / open() / closeAll() 内部 helper 直接复用
  click handler: !isOpen() return; —— 折叠态点空白不切值（点击展开后才接值）
styles-20260817.css
  .wheel-picker
                  height: 32px; transition: height .15s
                  .wheel-picker-band / .wheel-picker-row:not(.is-cur) display:none
  .wheel-picker.wp-open
                  height: 76px;
                  .wheel-picker-band / .wheel-picker-row:not(.is-cur) display:flex
  .wheel-picker:hover
                  border-color: var(--glass-border)  （弱提示）
```

### 开发顺序落地

```
#1 CSS：折叠态 32px + 隐藏 band + 隐藏非当前行；展开态 76px + 显示；transition
#2 app.js wheelPicker mouseenter/mouseleave 监听 + 150ms 延迟收缩
#3 折叠态 click/wheel/pointerdown 不响应（!isOpen() return）
#4 py_compile / node --check
#5 文档
```

---

## 4. 问题

### 4.1 折叠态只显当前值，但 band / 非当前行还在 DOM

v0.113k 实现里 wheelPicker 函数 `appendChild` band + 3 rows + unit 四件齐挂到 host。
折叠态如果不藏，32px 高度里塞下 76px 三行 + 高亮带 → 视觉错位（中间高亮带切
到当前值的水平位置，与单格观感冲突）。

**改法**：CSS 控制显隐。折叠态 `.wheel-picker-band` + `.wheel-picker-row:not(.is-cur)`
display:none；展开态恢复 display:flex。**不动 DOM 结构**，靠 CSS 切，事件监听
仍由 isOpen() 判断。

### 4.2 鼠标移出立即收缩会闪

用户从展开态 hover 到相邻「测试」按钮 / 说明 hint 时，鼠标轨迹短暂穿越外边距
或别的元素。如果 leave 立即 close，再 enter 时再 open —— 来回闪。

**改法**：leave 加 150ms `setTimeout(closeAll)`。enter 时 clear timeout。
只要 150ms 内重新进入就不收缩。

### 4.3 折叠态 click 是否响应切值

不该响应。折叠态用户看到的就是「当前值 = X」，点空白处无视觉反馈切值 → 用户
困惑（看起来按了没反应）。**改法**：`click` handler 顶行 `if (!isOpen()) return;`。
同理 `wheel` 和 `pointerdown`。

### 4.4 滚动条 / 全局 click 折叠冲突

`_wpGlobalBound` 全局 click 监听：点 wheel-picker 外部 → `closeAll()`。折叠态
点外面也会触发 closeAll —— 但本来就是折叠态，no-op。点击折叠态自身 → handler
return，不展开也不切值。**统一行为**没有副作用。

### 4.5 折叠态打开后第一次 hover 没反应

CSS `.wheel-picker:hover` 加了 `border-color` 变亮 —— 微弱的「可交互」提示。
但不展开。要展开必须由 JS mouseenter 监听触发。CSS hover 与 JS mouseenter
都监听同一事件，CSS 即时触发、JS 触发 open()；二者协同，没有竞态。

---

## 5. 解决

### 5.1 app.js：wheelPicker 加 hover/leave 监听

```js
// v0.113m：悬停展开 / 移出自动收缩（150ms 延迟防划过闪）。
let _leaveTimer = null;
el.addEventListener("mouseenter", () => {
  if (_leaveTimer) { clearTimeout(_leaveTimer); _leaveTimer = null; }
  open();
});
el.addEventListener("mouseleave", () => {
  if (_leaveTimer) clearTimeout(_leaveTimer);
  _leaveTimer = setTimeout(() => {
    _leaveTimer = null;
    closeAll();
  }, 150);
});

// v0.113l：折叠态（!isOpen）下 click/wheel/pointerdown 全部 return
el.addEventListener("click", (e) => {
  if (el._wpSuppressClick) { el._wpSuppressClick = false; return; }
  if (!isOpen()) return;  // 折叠态不切值
  const r = el.getBoundingClientRect();
  set(_v + (e.clientY - r.top < r.height / 2 ? -1 : 1));
});
el.addEventListener("wheel", (e) => {
  if (!isOpen()) return;
  e.preventDefault();
  set(_v + (e.deltaY > 0 ? 1 : -1));
}, { passive: false });
// pointerdown 同款
```

### 5.2 styles-20260817.css：折叠 32px / 展开 76px + 显隐控制

```css
/* v0.113k/l：滚轮选择器（自绘轮盘，替代原生 number input）。
   v0.113l：折叠 = 单格（32px，只显示当前值），鼠标点击才弹出轮盘（76px）；
   展开态三条值 + 中间高亮带，滚轮 / 点击上下半区 / 拖动切值。 */
.wheel-picker {
  position: relative;
  width: 64px;
  height: 32px;  /* v0.113l：折叠态 */
  display: flex;
  flex-direction: column;
  border-radius: var(--radius-md);
  background: var(--glass-surface-soft);
  border: 1px solid var(--hairline);
  overflow: hidden;
  cursor: default;
  user-select: none;
  -webkit-user-select: none;
  font-variant-numeric: tabular-nums;
  transition: height .15s var(--ease-out);
}
.wheel-picker.wp-open {
  height: 76px;  /* v0.113l：展开态 */
  cursor: ns-resize;
}
/* v0.113m：悬停展开 —— hover 时给一点"可交互"提示 */
.wheel-picker:hover {
  border-color: var(--glass-border);
}
/* 折叠态隐藏高亮带与上/下行，只留当前值一格 */
.wheel-picker .wheel-picker-band,
.wheel-picker .wheel-picker-row:not(.is-cur) {
  display: none;
}
.wheel-picker.wp-open .wheel-picker-band,
.wheel-picker.wp-open .wheel-picker-row:not(.is-cur) {
  display: flex;
}
```

### 5.3 切换动画

`transition: height .15s var(--ease-out)`：32 ↔ 76px 高度切换 150ms 缓动。用户
视线感知到「一格 ↔ 三格」的展开反馈，但不喧宾夺主。展开 / 折叠触发频率低
（hover enter/leave），不构成性能负担。

### 5.4 全局 Escape 关闭（既有逻辑）

`_wpGlobalBound` 全局 keydown 监听 `Escape` → `closeAll()` —— 按 Esc 收起所有
展开的 wheel picker（不论 focus 在哪）。**保留既有行为**，折叠态按 Esc 无效果
（已经折叠）。

---

## 6. 是否完全按规划

**按规划落地**，有一个 CSS 选择器小调：

- **host 选中用 `closest(".wheel-picker")`** —— 既有的 `_wpGlobalBound` 全局 click
  短路逻辑用 `e.target.closest(".wheel-picker")` 判断「点的是不是轮盘内部」。
  wheel-picker div 自身就是 `.wheel-picker`，命中即短路。无需修改。
- **mountTimeoutWheel 多次调 wheelPicker 的健壮性**：三个 wheel（thinking / gap / text）
  各调一次，每次都绑自己 hover/leave。`_wpGlobalBound` 单例 flag 保证全局监听只绑一次。
- **CSS 显隐 vs DOM 控制**：选 CSS display 切而不是 add/remove children。理由：
  CSS 切换更轻（无 DOM 抖动）、transition 顺滑、wheelPicker 函数无变更。
- `python -m py_compile`（gui.py 无改动）+ `node --check src/relay/web/app.js` 通过。
- 手动验证：设置页 → 实时栏管理 → 三个超时行 → 折叠态显 32px 单格当前值；
  hover 进入 → 150ms 内展开 76px 三行；移出 → 150ms 后收缩；展开时滚轮 + 鼠标
  上下半区 + 拖动切值生效；折叠时不响应；按 Esc 收起所有展开。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | `wheelPicker` 函数加 `mouseenter` / `mouseleave` 监听（150ms 延迟收缩）；`click` / `wheel` / `pointerdown` handler 顶部加 `if (!isOpen()) return`（v0.113l 折叠态不响应） |
| `src/relay/web/styles-20260817.css` | `.wheel-picker` 默认 32px 折叠 + `.wp-open` 76px 展开 + `transition: height .15s`；折叠态 `.wheel-picker-band` / `.wheel-picker-row:not(.is-cur)` `display:none`，展开态 `display:flex`；`.wheel-picker:hover` 改 border-color 提示 |
| `docs/dev/ui_wheel_picker_collapse_v0.113lm.md` | 本文档 |

### 状态流

- **mount 时** —— 三个 wheel-picker 由 `mountTimeoutWheel` 调 `wheelPicker(host,
  setter, def)` 各初始化一次，初始 `wp-open` class 不存在 → 折叠态 32px 单格。
- **hover 进入** —— mouseenter → `open()` 加 `wp-open` class → CSS transition
  32 → 76px，150ms 缓动展开三行 + 高亮带。
- **hover 移出** —— mouseleave → 150ms setTimeout → `closeAll()` 移除 `wp-open`
  → CSS 76 → 32px 收缩。若 150ms 内重新进入 → clearTimeout → 保持展开。
- **展开态操作** —— 滚轮 / 点击上半 / 点击下半 / 拖动 → set() ±1 → 防抖 350ms
  写后端 `setLivePanel*Timeout(s)`；dataset.raw / aria-valuenow 同步。
- **折叠态操作** —— click/wheel/pointerdown handler `!isOpen() return` 全部静默。
- **全局 Escape** —— `_wpGlobalBound` keydown Escape → closeAll → 收起所有展开。

### 验证

- `node --check src/relay/web/app.js` 通过。
- 手动端到端：
  1. 设置页 → 实时栏管理 → 三档超时各自折叠 32px，仅当前值显示；
  2. hover 任一 → 150ms 内展开 76px 三行 + 高亮带，鼠标移出 150ms 后收缩；
  3. 展开时滚轮 + 点击上下半 + 拖动切值生效；折叠时同操作不响应；
  4. 多档 hover 来回切换（如从 thinking 移到 gap）不闪（leave delay + enter clear）；
  5. 按 Esc 收起所有展开。
- 端到端：切到 90/45/15 → 刷新 GUI → 回到设置页三档仍 90/45/15（v0.113u 持久化
  + v0.113k wheelPicker 初始化配合）。
