# 超时时间滚轮选择器（v0.113k）开发文档

> 设置页「实时栏」三档超时时间（思考流 / 思考-正文衔接 / 正文流）由原生 number input
> 改成**自绘滚轮选择器**（iOS 轮盘观感：三条值 + 中间高亮带）。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 超时时间做一个滚轮选择器

（v0.113k 落地后用户复测）

> 鼠标点击才弹出滚轮选择器，不点就是一格

（v0.113l 落地后用户复测）

> 好的，再改为鼠标移入展开，移除自动收缩

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 超时时间输入改「滚轮选择器」 | 指令 1 |
| B | 三档统一（思考流 / 衔接 / 正文流）—— 同为超时输入，风格一致 | 指令 1 + 一致性 |
| C | **点击才弹出轮盘** —— 折叠态 = 单格（只显示当前值），鼠标点击展开，不点就是一格 | 指令 2 |
| D | **鼠标移入展开、移出自动收缩** —— 交互从「点击切换」改为「悬停」 | 指令 3 |

### 隐含但需自行决策的点

- **「滚轮选择器」指什么**：iOS 式轮盘（上下可见相邻值、中间高亮当前值、滚轮滚动切值）。
  既有 v0.113e 自绘选择器的先例（原生 `<select>` 改自绘菜单成功），此处同样自绘，不用
  第三方库。
- **范围与步进**：沿用原 `type=number` 的语义 —— 1–600 秒、步进 1。
- **持久化口径**：沿用 `set_live_panel_*_timeout` 桥，返回 `{seconds}`；防抖 350ms 写回
  （避免 600 档滚轮连打时刷爆桥）。
- **原生 number input 的上下箭头**：砍掉 —— 滚轮选择器自带交互，不再需要 spin 箭头。
- **设计决定**（视觉类自己拍板，不反问用户）：不做惯性滚动（600 档、步进 1，无惯性反而
  可控）；值变化即防抖持久化；拖动手感 1px ≈ 1 值（24px 一行）。
- **v0.113l 折叠/展开**：折叠 = 单格（32px，只显示当前值 + s），鼠标点击才展开成轮盘
  （76px）。互斥一次只开一个；点外部 / Esc 收起。折叠态不响应滚轮（用户要的就是
  「不点就是一格」）。
- **v0.113m 悬停展开/收缩**：把「点击切换」换成「`mouseenter` 展开 / `mouseleave` 收缩」。
  移出带 **150ms 延迟**收缩 —— 鼠标划过三条值行不闪（给一个从 32px 悬停区移进 76px
  展开区的缓冲）。点击只负责切值，不再展开/收起；折叠态 cursor 从 pointer 改 default。

---

## 3. 分析需求后得出的开发路径

```
主线  自绘 wheelPicker 组件 + 三档接入（替换 number input）
  app.js   wheelPicker(el, setter, initial)：自绘 DOM + wheel/click/drag + 防抖持久化
           v0.113l：折叠/展开两态（wp-open 类）+ 全局互斥/外部点击/Esc 收起
           v0.113m：mouseenter 展开 / mouseleave 150ms 延迟收缩
           renderSettingsPrefs  三档 markup：input → .wheel-picker 容器
           wireSettingsPrefs    bindTimeoutInput → mountTimeoutWheel（挂载 + 默认值）
           refreshPrefsDynamic  initTimeoutInput → initTimeoutWheel（snap/桥取真实值）
  styles   .wheel-picker 折叠 32px / .wp-open 展开 76px（transition）；隐藏 band 与非当前行
```

### 开发顺序落地

```
#1 定位超时三档 input（renderSettingsPrefs markup / wireSettingsPrefs / refreshPrefsDynamic）
#2 设计 wheelPicker：三条值 + 高亮带；wheel / 点击上下半区 / 拖动切值；防抖持久化
#3 markup 三档 input → .wheel-picker 容器；JS 挂载 + 取值替换
#4 CSS：.wheel-picker / .wheel-picker-band / .wheel-picker-row / .wheel-picker-unit
#5 headless 验证：真实渲染 + stub bridge 后 wheel/click/clamp/初始值全过
#6 v0.113l：折叠/展开两态 + 全局互斥 + headless 验证（互斥/Esc/外部点击/折叠 wheel 惰性）
#7 版本 bump + node --check + 清理探针 + 写文档
```

---

## 4. 问题

### 4.1 原生 number input 没有轮盘感，且样式不一致

v0.113d–e 已经确立「原生控件不能定制就自绘」的路子（上游选择器就是例证）。number input
的上下箭头是 OS 渲染，无法跟自绘 UI 统一。超时三档挤在设置页，原生箭头突兀。

### 4.2 600 档范围：持久化不能每次滚轮都打后端

滚轮连打一格格走，1–600 全走完会打几百次 `set_live_panel_*_timeout`。必须**防抖**：显示
即时更新，写回合并到停顿 350ms 后最后一次。

### 4.3 取值时序：挂载用默认值，真实值后到

`renderSettingsPrefs`（markup）→ `wireSettingsPrefs`（挂载 + 默认值）→
`refreshPrefsDynamic`（取值）。挂载先于取值，所以组件要暴露 `__wpSet(v, silent)`：
- 有 snapshot 实时值（`snap.live_panel_*_timeout`）→ silent 更新显示；
- 无则走桥 `get_live_panel_*_timeout` → silent 更新。
- **silent = 只改显示，不触发持久化**（值本就是后端真值，回写是多余的）。

### 4.4 silent set 与挂起防抖的交互

isolated 测试发现：`set(v, silent)` 不会取消已挂起的防抖 timer —— 该 timer 到时会把
silent 后的值持久化。**这不是 bug**：silent 只出现在（a）初始化、（b）持久化回调内服务端
回写（此时 `_timer` 已置 null），两者都不会与用户挂起的防抖重叠。保留现状。

### 4.5 v0.113l：闭包 `el` 与 `close.call(p)` 的坑

第一版折叠/展开用 `close.call(p)` 想收掉其它轮盘，但 `close()` 闭包里操作的是**创建时的
`el`**，不是 `this` —— 互斥 / Esc / 外部点击全部失效（headless 实测 a.open=true 未收起）。
**改法**：通用 `closeAll()` 直接 `querySelectorAll(".wheel-picker.wp-open")` 逐个去类，不
依赖闭包/this。`closeAll` 是 generic 的（不绑定具体 el），首轮盘绑定全局 handler 时捕获
的首个 `closeAll` 对全部轮盘同样生效。

---

## 5. 解决

### 5.1 app.js：wheelPicker 组件（v0.113k + v0.113l + v0.113m）

```js
function wheelPicker(el, setter, initial) {
  const MIN = 1, MAX = 600, ROW = 24;
  el.innerHTML = "";
  // band（中间高亮带）+ 3 条值行 + 单位 s
  // ...DOM 构建... el 初始 aria-expanded="false"
  let _v = clamp(initial); let _timer = null;
  function closeAll() {
    document.querySelectorAll(".wheel-picker.wp-open").forEach((p) => {
      p.classList.remove("wp-open");
      p.setAttribute("aria-expanded", "false");
    });
  }
  function open() { closeAll(); el.classList.add("wp-open"); el.setAttribute("aria-expanded", "true"); }
  function set(v, silent) {
    v = clamp(v); if (v === _v) return;
    _v = v; render();
    if (silent) return;
    // 防抖 350ms 写回；服务端纠正则 silent 回写
  }
  // v0.113m：mouseenter 展开 / mouseleave 150ms 延迟收缩（划过三行不闪）；
  //           点击只在展开态切值（上下半区 ±1），不再负责展开/收起
  // 展开态：wheel 防默认 ±1；pointer drag 1px≈1 值；折叠态滚轮/拖动惰性
  // 全局（_wpGlobalBound 只绑一次）：点轮盘外部 / Esc → closeAll()
  el.__wpSet = set;
}
```

- 交互齐全：滚轮 / 点击上下半区 / 拖动，都走同一 `set()`，天然统一 clamp + 防抖。
- **折叠 = 单格**：`.wheel-picker` 高 32px，隐藏 band 与非当前行（CSS），只显示当前值 + s；
  不悬停就是一格。
- **v0.113m 悬停展开/收缩**：`mouseenter` → `open()`（加 `wp-open`，高 76px，三条值 + 高亮带）；
  `mouseleave` → 150ms 延迟 `closeAll()`（鼠标划过三条值行不闪）。
- **展开后收起**：移出（150ms 后）/ 点外部 / Esc / 开另一个轮盘（互斥）。
- 折叠态 cursor 为 `default`（不再 pointer），点击只切值不展开；hover 给 `border-color` 提示可交互。
- 拖动结束后 click 会被 `_wpSuppressClick` 吞掉，不会多跳 1 格。
- `role="spinbutton"` + `aria-valuemin/max/now` + `aria-expanded`，可访问性到位。

### 5.2 markup / 挂载 / 取值

- **markup**：三档 `input[type=number]` → `<div class="prefs-live-panel-*-input wheel-picker">`。
- **wireSettingsPrefs**：`bindTimeoutInput` → `mountTimeoutWheel(sel, setter, def)`（调用
  `wheelPicker`，def 为默认值 60/20/10）。
- **refreshPrefsDynamic**：`initTimeoutInput` → `initTimeoutWheel(sel, getter, fallback, def)`
  （snap 有值 silent set；无则桥取 silent set）。

### 5.3 styles：.wheel-picker 轮盘（折叠 / 展开两态）

```css
.wheel-picker {
  width: 64px; height: 32px;         /* v0.113l：折叠 = 单格 */
  display: flex; flex-direction: column;
  border-radius: var(--radius-md); background: var(--glass-surface-soft);
  border: 1px solid var(--hairline); overflow: hidden;
  cursor: default; user-select: none; font-variant-numeric: tabular-nums;
  transition: height .15s var(--ease-out);
}
.wheel-picker.wp-open { height: 76px; cursor: ns-resize; }
.wheel-picker:hover { border-color: var(--glass-border); }  /* v0.113m：悬停可交互提示 */
/* 折叠态隐藏高亮带与上/下行，只留当前值一格 */
.wheel-picker .wheel-picker-band,
.wheel-picker .wheel-picker-row:not(.is-cur) { display: none; }
.wheel-picker.wp-open .wheel-picker-band,
.wheel-picker.wp-open .wheel-picker-row:not(.is-cur) { display: flex; }
.wheel-picker-band {  /* 中间高亮带，高 = 1/3 */
  position: absolute; left:4px; right:4px; top:33.333%; height:33.334%;
  border-radius:6px; background: var(--glass-surface-strong);
  box-shadow: inset 0 0 0 1px var(--hairline);
}
.wheel-picker-row { flex:1; display:flex; align-items:center; justify-content:center; font-size:11px; color: var(--text-muted); }
.wheel-picker-row.is-cur { font-size:14px; font-weight:700; color: var(--text-primary); }
.wheel-picker-unit { position:absolute; right:8px; top:50%; transform:translateY(-50%); font-size:10px; color: var(--text-muted); }
```

### 5.4 headless 验证（真实渲染 + stub bridge）

v0.113k（展开态一直可见）：
- 三档挂载，初始值 60/20/10 正确（snapshot 实时值），上/下行显示相邻值。
- 滚轮 +1 → 61；滚轮 -1 → 60。点击上半区 → 59。
- `__wpSet(1,true)` 后滚轮 -1 停在 1（clamp）；再 +1 → 2。
- isolated 防抖测试：61→62→silent 只 persist 一次；timer 确实 350ms 后触发。
  （全量探针里 setter 未记录是 headless 虚拟时间下嵌套 timer 未推进的环境产物，逻辑由
  isolated 测试证实。）

v0.113l（点击才弹出）：
- 初始折叠：h=32、open=false、band 隐藏。
- 点击 → 展开：open=true、三条值 59/60/61、band 可见（`transition:none` 覆写下 h=76；
  默认探针 h=32 是虚拟时间不推进 transition 的已知产物）。
- 展开态点击上半区 → 59；滚轮 +1 → 60。
- **互斥**：开第二个轮盘，第一个自动收起（a.open=false, b.open=true）。
- Esc → 全部收起；点外部 → 收起。
- 折叠态滚轮 → 值不变（惰性，符合「不点就是一格」）。

v0.113m（悬停展开/收缩，headless `_probe_wheel3`）：
- 初始折叠：open=false；mouseenter → open=true、三条值 + band 可见。
- 展开态点击上半区 → 59；滚轮 +1 → 60（点击不再展开/收起，只切值）。
- 移出后立即（<150ms）→ 仍展开（延迟收缩生效，划过三行不闪）；移出 250ms → 收起。
- 互斥：悬停第二个轮盘，第一个自动收起。
- 折叠态滚轮 → 值不变（惰性，不悬停就是一格）。
- 折叠态 cursor=default（headless getComputedStyle 验证）。

---

## 6. 是否完全按规划

**v0.113k 按规划；v0.113l、v0.113m 因用户反馈调整交互形态** —— 正是文档要记录的偏差：

- v0.113k：三档超时统一换成自绘滚轮选择器，范围 / 步进 / 持久化口径与原生 input 完全一致。
- v0.113l：用户复测「鼠标点击才弹出，不点就是一格」—— 轮盘默认可见太占位。改为折叠/
  展开两态：折叠 32px 单格（只显示当前值），点击才弹出 76px 轮盘，不点就是一格。
- v0.113m：用户复测「鼠标移入展开、移除自动收缩」—— 把「点击切换」换成「悬停」。
  `mouseenter` 展开、`mouseleave` 150ms 延迟收缩（划过三行不闪）；点击只负责切值，
  不再展开/收起；折叠态 cursor `pointer`→`default`，hover 加边框提示。
- 设计决定（视觉 + 交互细节）自己拍板：无惯性滚动、防抖 350ms、1px≈1 值、拖动后 suppress
  误触 click、折叠态惰性（不响应滚轮）。
- 复用 v0.113e 确立的「原生控件不能定制就自绘」模式，未引入第三方依赖。
- headless 实测抓出闭包/this 坑（`close.call(p)` 失效）并修正为通用 `closeAll()`。
- zoom=1 无回归（新组件不涉 zoom 尺寸逻辑）；`node --check` 通过。
- 版本号 bump：app.js `?v=20260822-17`、styles `?v=20260822-20`（index.html + live_panel.html）。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | 新增 `wheelPicker()` 自绘组件（wheel/click/drag + 防抖持久化 + `__wpSet` 外部取值 + v0.113l 折叠/展开两态 + v0.113m 悬停展开/150ms 延迟收缩 + 全局互斥/Esc/外部点击收起）；三档超时 markup `input` → `.wheel-picker`；`wireSettingsPrefs` 挂载替换 `bindTimeoutInput`；`refreshPrefsDynamic` 取值替换 `initTimeoutInput` |
| `src/relay/web/styles-20260817.css` | `.wheel-picker`（折叠 32px）/ `.wp-open`（展开 76px + transition）/ `.wheel-picker-band` / `.wheel-picker-row(.is-cur)` / `.wheel-picker-unit`；折叠态隐藏 band 与非当前行；v0.113m 折叠态 cursor `default` + `:hover` 边框提示 |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-20`、app.js `?v=20260822-17` |
| `src/relay/web/live_panel.html` | 版本号 query bump：styles `?v=20260822-20`（共享样式表缓存） |

### 状态流

- **滚轮选择器（A）**：三档超时折叠 = 单格（当前值 + s），鼠标移入展开轮盘（上/中/下三条
  值 + 高亮带），滚轮 / 点击上下半区 / 拖动切值，防抖 350ms 写回后端。
- **折叠/展开（C/D）**：不悬停就是一格；`mouseenter` 展开、`mouseleave` 150ms 延迟收缩；
  点外部 / Esc / 开另一个轮盘收起（互斥）。
- **三档统一（B）**：思考流 / 衔接 / 正文流三档同款组件，初始值由 snapshot / 桥取值。

### 验证

- headless 真实渲染 + stub bridge：三档挂载、初始值 60/20/10、滚轮 ±1、点击上半区、
  clamp 到 1、1→2 全通过；isolated 防抖测试证实 350ms 合并持久化与 silent 语义。
- v0.113l：初始折叠 h=32、点击展开（`transition:none` 覆写下 h=76）、互斥 / Esc / 外部
  点击收起、折叠态滚轮惰性 —— 全通过。
- v0.113m：mouseenter 展开、移出立即仍展开（延迟生效）/ 移出 250ms 收起、点击只切值、
  互斥、折叠态滚轮惰性、cursor default —— 全通过。
- `node --check app.js` 通过；探针文件已清理。
- 刷新 GUI：设置页 → 实时栏 → 三档超时显示为单格；鼠标移入弹出轮盘，滚轮 / 点击 / 拖动
  切值，停顿后写回，重启后值保持。
