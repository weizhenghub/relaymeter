# 上游选择器展开动效（v0.113g）开发文档

> 承接 v0.113e/f（`ui_upstream_picker_v0.113e.md` 自绘下拉 + `ui_scrollbar_v0.113f.md` 滚动条）的
> 后续反馈：自绘上游选择器的菜单现在是 **hidden 瞬间显隐**，没有任何过渡，要求加**展开动效**。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 展开动效

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 上游选择器菜单展开 / 收起时带动效（不再瞬间显隐） | 指令 1 |

### 隐含但需自行决策的点

- **动效形态**：展开列表是**向上弹出**（`bottom:calc(100%+6px)`），配**淡入 + 轻微上滑 +
  微缩放**（`translateY(6px) scale(.98) → 0/1`）最贴合"从底缘滑入"的方向感；收起反向。
  时长 150ms（ease-out），不拖沓。
- **hidden 不能直接做过渡**：`hidden` 是 `display:none`（UA），没有过渡机会。要用两段式：
  `.open` class 驱动 CSS transition，`hidden` 只做**最终**显隐开关（打开先除 hidden、收起
  等过渡完再设 hidden）。
- **快速连点要安全**：打开过程中立刻又关、关闭中立刻又开，不能卡死或残留。收起有 180ms
  timer（过渡结束后才设 hidden），打开时要清掉未完成的 timer。
- **事件委托仍只绑一次**：v0.113e 的 document 级委托继续用，只把开关逻辑换成带动效的
  `_upMenuOpen` / `_upMenuClose`。

---

## 3. 分析需求后得出的开发路径

```
主线  上游菜单展开/收起动效（A）—— JS + CSS
  app.js    委托块加 _upMenuOpen / _upMenuClose / _upMenuCloseAll
            （两段式：.open class 过渡 + hidden 最终显隐 + 180ms 收起 timer）
  styles    .upstream-menu 加初始态（opacity:0 / translateY(6px) scale(.98) /
            pointer-events:none）+ transition；.upstream-menu.open 打开态
```

### 开发顺序落地

```
#1 确认 hidden 无过渡机会 → 设计 .open class 两段式
#2 app.js 委托块重写：三个辅助函数 + 所有开关/关闭路径改用它
#3 styles .upstream-menu 加初始态 / .open / transition
#4 headless 验证：初始/打开 computed、收起 180ms 后 hidden、快速连点重开
#5 版本 bump + node --check + 清理测试文件 + 写文档
```

---

## 4. 问题

### 4.1 hidden 属性没有过渡机会

`hidden` 由 UA 样式 `[hidden]{display:none}` 实现，属性切换是**瞬间**显隐，CSS `transition`
对 `display` 无效。要动效必须引入一个可过渡的状态 class，让 `opacity` / `transform` 变化。

### 4.2 两段式时序：打开要 reflow，收起要等过渡

- **打开**：先 `menu.hidden = false`（元素可布局），但此时元素仍是初始态（`opacity:0` +
  下移），必须 `void menu.offsetWidth` **强制同步 reflow** 让初始态落地到帧，再加 `.open`
  —— 否则浏览器把"去掉 hidden + 加 .open"合并到同一帧，from 态从未渲染，transition 不触发。
- **收起**：去掉 `.open` 后过渡启动，但 `hidden` 若立即设回 true 会 `display:none` 瞬间消失
  —— 要等过渡结束（180ms）再设 hidden。用 timer 而非 `transitionend`（简单、幂等）。

### 4.3 快速连点

若收起 timer 未清，用户快速再点开：`_upMenuOpen` 检查 `menu.hidden` 仍是 false（timer 没到）
会直接 return —— 菜单卡在关闭中。修正：打开时先 `clearTimeout` 未完成的收起 timer。

---

## 5. 解决

### 5.1 app.js：带动效的开/关辅助函数

```js
function _upMenuOpen(menu) {
  if (!menu) return;
  if (menu._upCloseTimer) { clearTimeout(menu._upCloseTimer); menu._upCloseTimer = null; }
  menu.hidden = false;
  void menu.offsetWidth;            // 强制 reflow，让初始态（透明/下移）落地
  menu.classList.add("open");
}
function _upMenuClose(menu) {
  if (!menu || menu.hidden) return;
  if (menu._upCloseTimer) return;   // 已在收起中，幂等
  menu.classList.remove("open");
  menu._upCloseTimer = setTimeout(() => {
    menu._upCloseTimer = null;
    menu.hidden = true;
  }, 180);
}
const _upMenuCloseAll = (except) => {
  document.querySelectorAll("[data-upstream-menu].open").forEach(m => {
    if (!except || !m.closest("[data-upstream-picker]").contains(except)) _upMenuClose(m);
  });
};
```

委托块各路径统一改用它：点外部 → `_upMenuCloseAll`；点触发按钮 → 按 `.open` 判定 toggle；
选选项 / Esc → `_upMenuClose`。打开/关闭判定改用 `.open` class（收起中 hidden 仍是 false，
用 hidden 判断会误判）。

### 5.2 styles：初始态 + .open + transition

```css
.upstream-menu {
  /* ...原有面板样式... */
  opacity: 0;
  transform: translateY(6px) scale(.98);
  pointer-events: none;             /* 过渡期间不可点 */
  transition: opacity .15s var(--ease-out), transform .15s var(--ease-out);
}
.upstream-menu.open {
  opacity: 1;
  transform: translateY(0) scale(1);
  pointer-events: auto;
}
```

向上展开（`bottom:calc(100%+6px)`）配向下位移，视觉上菜单"从底缘滑入"。

### 5.3 headless 验证

- 初始：`opacity=0`、`transform=matrix(0.98,0,0,0.98,0,6)`、`transition=0.15s`。
- 打开：`hidden=false`、`.open` 加上了；收起 220ms 后 `hidden=true`；关闭中重开
  `hidden=false` + `.open`（timer 被清）。
- ⚠ headless 虚拟时间不推进 CSS transition（Chromium 行为），`OPEN-MID opacity=0` 是环境
  限制非代码问题；transition 由浏览器原生在 class 切换时执行，初始态/打开态 computed 与
  transition 声明已确证正确。

---

## 6. 是否完全按规划

**完全按规划，无偏差。**

- 动效选淡入 + 上滑 + 微缩放，与菜单向上展开方向一致；150ms ease-out 不拖沓。
- 两段式（`.open` + hidden）正确处理了 hidden 无过渡、打开需 reflow、收起需等过渡三个坑。
- 快速连点安全（打开清收起 timer）；`_upMenuCloseAll` 复用，点外部/Esc/选中全走动效关闭。
- 版本号统一 bump：app.js `?v=20260822-12`、styles-20260817.css `?v=20260822-13`
  （index.html / live_panel.html styles 引用同步）。后端零改动。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | v0.113g 委托块加 `_upMenuOpen` / `_upMenuClose` / `_upMenuCloseAll`；触发/选项/外部/Esc 全部改带动效开关；判定改用 `.open` class |
| `src/relay/web/styles-20260817.css` | v0.113g `.upstream-menu` 初始态（opacity:0 / translateY(6px) scale(.98) / pointer-events:none）+ transition 150ms ease-out；`.upstream-menu.open` 打开态 |
| `src/relay/web/index.html` | 版本号 query bump：styles `?v=20260822-13`、app.js `?v=20260822-12` |
| `src/relay/web/live_panel.html` | styles 引用同步 `?v=20260822-13` |

### 状态流

- **展开动效（A）**：点触发按钮 → `_upMenuOpen`（去 hidden → reflow → `.open`）→ 菜单
  淡入 + 从底缘滑入；再点 / 选选项 / 点外部 / Esc → `_upMenuClose`（去 `.open` → 淡出滑落
  → 180ms 后 hidden）。快速连点不卡死。

### 验证

- `node --check app.js` 通过；headless computed：初始透明下移、`.open` 后隐藏开合时序
  正确、快速连点重开正常（transition 本身由浏览器原生执行）。
- 刷新 GUI：点左上角上游选择器 → 菜单淡入 + 轻微上滑展开；关闭时淡出滑落，不瞬间消失。
