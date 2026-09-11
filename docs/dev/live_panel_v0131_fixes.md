# 实时流侧栏 v0.130 修复轮 + 主窗 nav 波纹一致性（v0.131）开发文档

## 1. 用户的初始指令

> 实时，主窗左侧导航「实时」扫光波纹在无请求时还在跑（用户原话「没有正在进行中的请求，但是动画还是在跑」，确认指主窗左侧菜单「实时」的波纹）。
>
> 侧栏「等待请求」四个字删掉，「工具调用」容器没有时也不占位，隐藏掉。
>
> 发现问题，有 think 流但没有创建完整容器而是用了仅实时容器（即 puretext 容器）。
>
> （上一轮 v0.130 验证后由用户口头提出，本轮为修复轮。）

本轮共 4 项，无规划文件，直接修复 + 验证。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 主窗 nav「实时」波纹在无请求时熄灭（与实时卡空态同一语义） | 指令 1 |
| B | 侧栏删除「等待请求」空态占位（#lp-empty） | 指令 2 |
| C | 工具调用容器无内容时不占位、整卡隐藏 | 指令 2 |
| D | 有 think 流的请求必须是完整 thinkstream 容器（puretext 首定类后升级） | 指令 3 |

### 隐含但需要确认的点（调查后确认）

- **nav 波纹归属**：主窗 `styles-20260817.css` 的 `.nav-item[data-view="live"].has-stream::after`（live-ripple 扫光），由 `app.js` 的 `updateLiveNavRipple()` 控制 —— 不是侧栏动画。
- **A 的根因方向**：`renderLive`（实时卡）与 `updateLiveNavRipple`（波纹）过滤不一致 → 需先读后端 `age_sec` 语义。
- **D 的根因方向**：`ensureContainer` 首事件定 kind → 首事件未带 thinking_text 时定成 puretext 且中途不换类。

---

## 3. 分析需求后得出的开发路径

### A：nav 波纹 vs 实时卡语义对齐

后端 `proxy_legacy.py` 的 `age_sec = max(0, time.time() - inf.last_update)` —— 是**最后活跃后的空闲秒数**，不是总时长。

`renderLive` 对 `phase === "streaming"` 有孤儿逃生口：`age_sec >= STREAMING_STALE_SEC(120)` 的条目不显示（后端 sweeper 90s 强清做主线，UI 层只是兜底）。`updateLiveNavRipple` 之前只按 `calling/streaming` 一律点亮 → 存在 streaming 孤儿时：实时卡空态、波纹仍扫。

**解法**：`STREAMING_STALE_SEC` 从 renderLive 内部提为模块级共享常量，`updateLiveNavRipple` 加同款过滤：

```js
if (phase === "calling") { hasStream = true; break; }
if (phase === "streaming" && Number(r.age_sec || 0) < STREAMING_STALE_SEC) { hasStream = true; break; }
```

保证「实时卡空态 ⇔ 波纹熄灭」同一语义。版本 bump：app.js `?v=20260823-33 → -47`。

### B/C：侧栏空态删除 + tools 整卡显隐

- **B**：删 `live_panel.html` 的 `#lp-empty` div；`live_panel.js` 删 `$emptyState` / `updateEmptyState` 全部引用（5 处：selector 收集、SEL i18n 列表、ensureContainer、relayLiveClear、relayLivePanelInit）；`live_panel.css` 删 `.lp-empty` 规则。
- **C**：`layout()` 构建 items 前算 `toolCount = $tools.querySelectorAll(".live-panel-tool").length`，`$toolsWrap.style.display = toolCount > 0 ? "" : "none"`，仅 toolCount>0 才 push `{kind:"tools"}`。顺序保证：**先设 display 再量高**（toolsNaturalHeight 在 display:none 下 offsetHeight=0，但入 items 前 display 已非 none，无影响）。

### D：puretext → thinkstream 升级

**根因**：`ensureContainer(rid, ev)` 首事件定 kind = `ev.thinking_text ? "thinkstream" : "puretext"`。首事件若是 calling/uploading 或正文先到（thinking_text 未带/空串），容器建 puretext；后续 delta 带非空 thinking_text 时 `_ridEls[rid]` 已存在 → ensureContainer 直接返回，kind 永不换 → thinking_text 无思考区被静默丢弃（`applyRid` 里 `typeof ev.thinking_text === "string"` 分支写 `refs.thinking`，puretext 无此 ref）。

**解法**：新增 `upgradeToThinkstream(rec)`（buildContainer 之后）：

```js
function upgradeToThinkstream(rec) {
  if (!rec || rec.kind === "thinkstream" || !rec.el) return;
  const stats = rec.el.querySelector(".live-panel-stats");
  const think = buildThink(rec.refs);   // 挂 refs.thinking / thinkingSpeed / thinkingCount
  if (stats && stats.nextElementSibling) stats.insertAdjacentElement("afterend", think);
  else rec.el.appendChild(think);
  rec.el.classList.remove("live-panel-puretext");
  rec.el.classList.add("live-panel-thinkstream");
  rec.kind = "thinkstream";
  scheduleLayout();
}
```

`applyRid` 开头加 `if (rec.kind === "puretext" && ev.thinking_text) upgradeToThinkstream(rec);`（在写 refs.thinking 之前，确保思考区已插入、refs.thinking 已赋值）。不重建整卡，已累积正文/统计原样保留。

版本 bump：live_panel.js `?v=20260823-46 → -48`，live_panel.css `?v=20260823-43 → -44`。

---

## 4. 实现中遇到的问题

### 问题 1（A）：波纹与实时卡过滤不一致

根因见 §3-A。`updateLiveNavRipple` 只按 phase 判亮，缺 renderLive 的 stale 过滤。

### 问题 2（D）：首事件未带 thinking_text → puretext 且永不换类

根因见 §3-D。这是「kind 首事件定类」设计的边界漏洞：首事件 ≠ 思考流首事件。calling 事件不带 thinking_text 字段，正文先到也不带。

### 问题 3（jsdom 测试时序）：tools 隐藏断言 getElementById 返回 null

初版 upgrade.js 在 DOMContentLoaded 触发、layout() 首轮把 `#lp-tools-wrap` 从静态位置移入列/隐藏之后才 `getElementById` → 元素已 move，`document.getElementById` 返回 null → TypeError。

**解法**：在 makeDom 后、layout 跑之前捕获元素引用（元素是移动不是删除，引用仍有效）。已修正测试脚本。

### 问题 4（工具环境）：Grep 工具损坏

`rg.exe` ENOENT（工作目录 reset 后 vendor 路径失效），全程用 Bash grep + Read 兜底。

### 问题 5（写文件路径）：Write 的 /tmp 与 bash 的 /tmp 不同

Write 落 `C:\tmp\lp_harness`，jsdom 依赖在 `C:\Users\weizheng\AppData\Local\Temp\lp_harness`。**解法**：cp 到 AppData 目录跑，跑完清理误落副本。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 波纹 vs 实时卡不一致 | `STREAMING_STALE_SEC` 提为共享常量 + `updateLiveNavRipple` 加同款 stale 过滤 | web/app.js |
| #2 puretext 永不换类 | `upgradeToThinkstream` 检测非空 thinking_text 补插思考区 + 换类 + rec.kind 更新 | web/live_panel.js |
| #3 jsdom 测试时序 | layout 前捕获 toolsWrap 引用 | /tmp/lp_harness/upgrade.js |
| #4 Grep 工具坏 | Bash grep / Read 兜底 | — |
| #5 /tmp 路径不一致 | cp 到 jsdom 目录跑 + 清理 | — |

---

## 6. 是否完全遵循规划路径开发

**无规划文件**（修复轮），按用户 3 条口头指令逐项实现。全部遵循：

- A 波纹：与 renderLive 共享同一 stale 常量与过滤，语义严格一致（node 6 场景验证）。
- B 空态删除彻底（html/js/css 三处），无残留引用（jsdom 验证 G 场景）。
- C tools 显隐在 layout 内统一控制，先显隐再量高顺序正确（jsdom F 场景）。
- D 升级不重建整卡、已累积内容保留、思考区不重复插入（jsdom A/B/C/D/E 场景）。

### 重大调整：无。

---

## 7. 最终实现点

### 前端（本 4 项全部在前端）

1. **`web/app.js`**（nav 波纹修复）
   - `STREAMING_STALE_SEC = 120` 从 renderLive 内部提为模块级共享常量（renderLive 定义前，带注释说明两处共用）
   - `updateLiveNavRipple` 循环加 stale 过滤：calling 亮；streaming 且 `age_sec < STREAMING_STALE_SEC` 才亮

2. **`web/live_panel.js`**（侧栏三改动）
   - 删 `$emptyState` 变量、`updateEmptyState` 函数定义与 5 处调用、LP_I18N 里「等待请求…」键、SEL 选择器里的 `#lp-empty`
   - `layout()`：tools 显隐（toolCount>0 才入 items + display 切换）
   - 新增 `upgradeToThinkstream(rec)`；`applyRid` 开头调用
   - `relayLivePanelInit`：空态引用清除

3. **`web/live_panel.html`**
   - 删 `#lp-empty` div
   - 顶部注释同步（静态元素说明去掉 #lp-empty）

4. **`web/live_panel.css`**
   - 删 `.lp-empty` 规则

### 资源文件版本

| 文件 | 版本 |
|---|---|
| `app.js`（主窗） | `?v=20260823-33` → `?v=20260823-47` |
| `live_panel.js` | `?v=20260823-46` → `?v=20260823-48` |
| `live_panel.css` | `?v=20260823-43` → `?v=20260823-44` |

### 验证

- **jsdom（`/tmp/lp_harness/upgrade.js`）20 项断言全过**：
  - A1-A5 首事件无 think → puretext → think 到 → 升级 thinkstream / 不再 puretext / 思考区插入 / 无错误
  - B 升级后正文保留；E 升级后思考内容写入
  - C1-C4 首事件带 think 直接 thinkstream；D1-D3 全程无 think 保持 puretext
  - F1-F4 tools 无内容隐藏 / 有内容显示 / 卡在列内 / 无错误
  - G1-G2 `#lp-empty` 删除、建容器后无错误
- **nav 波纹一致性 node 逻辑验证 6 场景全一致**：stale streaming(150s) / 正常 streaming(30s) / calling / done / 空 / mixed stale+normal → 「实时卡空态 ⇔ 波纹熄灭」全部成立

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/app.js` | 改（nav 波纹 stale 过滤） |
| `src/relay/web/live_panel.js` | 改（空态删 + tools 显隐 + 升级） |
| `src/relay/web/live_panel.html` | 改（删 #lp-empty + 版本 bump） |
| `src/relay/web/live_panel.css` | 改（删 .lp-empty） |
| `src/relay/web/index.html` | 改（app.js 版本 bump） |
