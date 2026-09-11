# 实时栏关闭按钮液态玻璃 / 等待水印细体 / 主窗侧栏光晕跨缝连续 / tool 卡片独立成项（v0.137）开发文档

## 1. 用户的初始指令

> 1、实时流窗口"关闭"按钮背景采用类似于总览页的卡片液态玻璃，等待两个字细一点
> 2、我知道为什么实时流栏显得不和谐了，它和主窗口的背景没有做到平滑的过度，到边缘时会有一种色层分裂感
> 3、并发的多个工具调用不要并在一个窗口，tool同样拆分到多个容器。位置见缝插针

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 实时流关闭按钮背景改用总览页同款液态玻璃（`.glass-card`） | 指令 1 |
| B | 「等待」两字细一点 | 指令 1 |
| C | 主窗与实时流栏背景跨缝平滑（消除色层分裂） | 指令 2 |
| D | 并发 tool 拆分到独立容器（不并在一个窗口/卡） | 指令 3 |
| E | tool 位置见缝插针（跟随列打包） | 指令 3 |
| F | tool 仍作为 layout 引擎的独立 item（与 per-rid 容器同级） | 推论 |

### 隐含但需要确认的点（设计自决）

- **液态玻璃参数**：`backdrop-filter: blur(23px) saturate(112%)` + 半透明白 + 透明 border + 11px 圆角（沿用 `.glass-card`）。
- **「细一点」**：字重 800 → 200。
- **跨缝连续原理**：两个 OS 窗口各自画 `.bg-glow`（相对自己视口），缝处亮度不连续；让侧栏在 dock 时**把光晕圆心对齐到主窗屏幕坐标**，缝两侧本来就是同一个模糊圈。
- **tool 卡片视觉**：第一版做玻璃卡（与 rid 容器同款），后被用户否决（v0.138 改回原 `.live-panel-tool` 紧凑小块）—— 本 dev doc 记录 v0.137 的初版玻璃卡方案。
- **tool 序号**：与 rid 共享序号池（见缝插针时用户能看到"是同一批的调用"）。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位样式与数据流

**A. 关闭按钮**：v0.120a 给主窗 `.window-controls` 加了 135° 渐变 + 磨砂玻璃背景；侧栏 `.live-panel-body .window-controls` 继承同一组样式规则。指令 1 要求改"卡片液态玻璃"——指 `.glass-card` 那种（纯半透明白 + blur，不是 135° 渐变）。

**B. 水印**：v0.120a 引入 `.lp-watermark` + `body.watermark-away` toggle。指令 1 要求字重 800 → 200（细一点）。

**C. 跨缝连续**：两个独立 OS 窗口。主窗 `.bg-glow-1` 圆心在主窗视口 (90,90)、`.bg-glow-2` 在主窗视口 (mainW-150, mainH-110)。侧栏 dock 时左缘 = 主窗右缘，但侧栏的 `.bg-glow` 是相对自己视口画的 → 缝处 90px 与 (mainW-150)px 两套坐标不连贯。

**D. tool 卡片**：v0.130 把 tool 容器当一个整体（`.live-panel-card` 包所有 tool）；layout 把整卡作为一个 item。指令 3 要求拆分 + 见缝插针 → tool 每条独立成 item。

### 第二阶段：方案设计

**A. 关闭按钮液态玻璃**（CSS）：

```css
.live-panel-body .window-controls {
  background: rgba(255, 255, 255, 0.11);
  backdrop-filter: blur(23px) saturate(112%);
  -webkit-backdrop-filter: blur(23px) saturate(112%);
  border: 1px solid rgba(255, 255, 255, 0);
  border-radius: 11px;
  box-shadow: var(--card-shadow);
  padding: 2px;
}
```

**B. 水印字重**：

```css
.lp-watermark span {
  font-weight: 200;  /* 原 800 */
}
```

**C. 跨缝连续**：JS 轮询 `get_main_geometry()` 拿主窗宽高 + dock 状态 → docked 时 body 加 `.glow-docked` + 把 `--glow-mw / --glow-mh` 写到 CSS 变量 → CSS 据此调整侧栏 `.bg-glow-1/2` 位置：

```css
.live-panel-body.glow-docked .bg-glow-1 {
  top: -260px;
  left: calc(-260px - var(--glow-mw, 0px));  /* 主窗 (90,90) → 侧栏 (-mainW+90, 90) - 350 */
}
.live-panel-body.glow-docked .bg-glow-2 {
  left: -600px;
  top: calc(var(--glow-mh, 0px) - 560px);   /* 主窗 (mainW-150, mainH-110) → 侧栏 (-150, mainH-110) - 450 */
  bottom: auto; right: auto;
}
```

`bridge.get_main_geometry()` 新 API 返回 `{x, y, w, h, docked}`。`1s setInterval` 轮询（覆盖主窗移动 / 侧栏拖离磁吸等所有 dock 状态变化）。

**D. tool 卡片独立**：每条 tool 一个 `.live-panel-card live-panel-tool-card`，加进 `_toolEls / _toolsOrder`。`layout()` 把 tools 当独立 item，贪心列打包算法见缝插针（与 per-rid 容器同等待遇）。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `live_panel.css` `.live-panel-body .window-controls` 改液态玻璃 | 无 |
| 2 | `live_panel.css` `.lp-watermark span` 字重 800 → 200 | 无 |
| 3 | `gui.py` 新增 `Api.get_main_geometry` | 无 |
| 4 | `live_panel.js` `syncGlow()` + 1s 轮询 + resize 时触发 | #3 |
| 5 | `live_panel.css` `.glow-docked` 偏移规则 + CSS 变量 `--glow-mw / --glow-mh` | #4 |
| 6 | `live_panel.js` 工具调用管理重写（每条独立 .live-panel-tool-card） | 无 |
| 7 | `live_panel.js` `layout()` 把 tools 当独立 item | #6 |
| 8 | `live_panel.js` `clearTools` / `relayLivePanelInit` 完整清理 tool 卡片 | #6 |
| 9 | 资源版本 bump（live_panel.css + live_panel.js） | #1-#8 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 关闭按钮液态玻璃参数 | `blur(23px) saturate(112%)` + `0.11` 白底 + 透明 border + 11px 圆角 | 沿用 `.glass-card` 参数，视觉一致 |
| 水印字重 | 800 → 200（细体） | 用户原话"细一点" |
| 跨缝轮询频率 | 1s 一次 | 成本可忽略；覆盖所有 dock 状态变化（含主窗移动、侧栏拖离磁吸） |
| 跨缝 CSS 变量名 | `--glow-mw / --glow-mh` | 简短，只用于 `.bg-glow-*` 重定位 |
| tool 卡片视觉 | 玻璃卡（与 rid 同款）+ 共享序号池 | 用户首版要求"独立成卡" |
| tool 卡片序号分配 | `nextFreeCircledIdx` 从 rid 已用序号池里取下一个最小空位 | 让用户看到「这些是同一批的调用」 |
| tool 卡片列打包 | 与 rid 容器共用 `layout()` 贪心算法；tool 算自己 nat（H 限 + 实测 offsetHeight） | 见缝插针语义 |
| tool 自动清除 | 仍是 `_toolsClearSec` 后 `_toolsOrder` 整组清 | 沿用 v0.134 行为 |
| tool cap | `_TOOLS_CAP` 默认 30，超出从最旧移除 | 沿用 v0.134 行为 |

---

## 4. 实现中遇到的问题

### 问题 1：`get_main_geometry` 需要暴露 `_panel_docked`

**症状**：gui.Api 是独立类，`self._app._panel_docked` 是 App 实例的属性。

**解法**：Api 的 `get_main_geometry` 通过 `self._app._panel_docked` 直接读（与其它 Api 方法同款模式）。

### 问题 2：`get_main_geometry` 在 webview.start() 之前调用会阻塞

**症状**：`_dock_getter` 已用 `events.shown.wait(15)` 短路 0 尺寸，但 `get_main_geometry` 不在 dock_getter 路径上 —— 启动早期直接读 `window.x/y/w/h` 会阻塞。

**解法**：try/except 包裹，捕到异常返回 `{"ok": False, "error": ...}`；JS 端 `.then(r => if (!r.ok) return)` 静默跳过。

### 问题 3：tool 卡片用 `.live-panel-card` 类后样式对不齐

**症状**：`.live-panel-card` 有 padding `0.8em 1em`，但 rid 卡用 `.live-panel-rid-head`（无 padding），tool 卡片想用 rid 头部样式却套上 .live-panel-card padding 后头被压扁。

**解法**：tool 卡片用 `.live-panel-card.live-panel-tool-card` 复用玻璃卡样式，单独写 `.live-panel-tool-card .lp-tool-name` / `.live-panel-tool-card .live-panel-tool-age` 覆盖头部 padding / age 推右等细节。

### 问题 4：tool 卡片高度如何算

**症状**：tool 卡片是动态高（args 多则高、寡则低），`_apply_geometry` 不再批量算。

**解法**：`toolNaturalHeight(rec, H)` 临时去掉 inline 高 → 实测 `offsetHeight`；若 DOM 刚挂未稳定（offsetHeight 0），用估算 `28 + lines × 14 + 16`。

### 问题 5：tool 卡片的 grow/shrink 水填

**症状**：原 layout 给 tool 整卡 `+10% 水填`（给的是"整块"），现在每张 tool 独立，水填基数变 → 多 tool 时可能出现水填不足。

**解法**：去掉原"tools 优先吃空位"特殊路径，所有 item 一视同仁走 `nat × 0.1` 水填 + 共享空位池；tool 卡片内容少，水填也少，自然小巧。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 `_panel_docked` 暴露 | `Api.get_main_geometry` 通过 `self._app._panel_docked` 读 | gui.py |
| #2 webview.start 阻塞 | try/except 包裹，错误返回 `{ok:false}` | gui.py |
| #3 tool 卡样式对不齐 | `.live-panel-tool-card` 专属样式覆盖 | live_panel.css |
| #4 tool 高实测 | `toolNaturalHeight` 实测 + 估算兜底 | live_panel.js |
| #5 水填基数 | 去特殊路径，所有 item 走同款 `nat × 0.1` | live_panel.js |

**最终 get_main_geometry（gui.py）：**

```python
def get_main_geometry(self) -> dict:
    """v0.137：返回主窗几何 + 侧栏 dock 状态 —— 侧栏用它把背景光晕
    对齐到主窗的屏幕坐标，使 dock 贴右时两个窗口的模糊光晕跨缝连续，
    不再有「色层分裂」。"""
    try:
        app = self._app
        if getattr(app, "window", None) is None:
            return {"ok": False, "error": "no main window"}
        return {
            "ok": True,
            "x": app.window.x, "y": app.window.y,
            "w": app.window.width, "h": app.window.height,
            "docked": bool(getattr(app, "_panel_docked", False)),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
```

**最终 syncGlow（live_panel.js）：**

```js
function syncGlow() {
  if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.get_main_geometry) return;
  window.pywebview.api.get_main_geometry()
    .then(function (r) {
      if (!r || !r.ok) return;
      if (r.docked && r.w > 0 && r.h > 0) {
        document.documentElement.style.setProperty("--glow-mw", r.w + "px");
        document.documentElement.style.setProperty("--glow-mh", r.h + "px");
        document.body.classList.add("glow-docked");
      } else {
        document.body.classList.remove("glow-docked");
      }
    })
    .catch(function () {});
}
// DOMContentLoaded + setInterval(syncGlow, 1000)
```

**最终 layout() items 合并（live_panel.js 关键改动）：**

```js
const items = [];
_ridOrder.forEach((rid) => {
  const rec = _ridEls[rid];
  if (rec && rec.el) items.push(rec);
});
_toolsOrder.forEach((tid) => {           // v0.137：tool 卡片独立 item
  const rec = _toolEls[tid];
  if (rec && rec.el) items.push(rec);
});
// ... 贪心列打包不变（items 多了 tool 也走同款 avail/col push 逻辑）
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **液态玻璃**：参数与 `.glass-card` 完全一致（blur 23px / saturate 112% / 半透明白 0.11 / 透明 border / 11px 圆角）。
- **水印字重**：800 → 200。
- **跨缝连续**：JS 轮询主窗几何 + dock 状态 → body `.glow-docked` + CSS 变量 → CSS 把 `.bg-glow` 圆心映射到主窗屏幕坐标。
- **tool 独立**：每条 `.live-panel-card.live-panel-tool-card`，与 per-rid 容器同级进 layout，共享序号池。
- **见缝插针**：layout() 的贪心列打包不变，tool 与 rid 容器同等看待，列满了开新列。

### 偏离之处：

- **(a) 1s 轮询开销**：plan 没明确频率。1s 一次 + resize 触发；bridge 调用 trivial（一个 dict），jsdom 测试不影响。**权衡 + 视觉自决**。
- **(b) CSS 变量用 --glow-mw / --glow-mh**：plan 没明确命名，短的就行。**命名自决**。
- **(c) tool 卡片首版用玻璃卡**：这是用户 v0.137 时点的指令（"独立成卡"）。v0.138 改回紧凑小块（见 live_panel_unified_container_v0.130.md 续篇 / 即将追加的 v0.138 文档）。**v0.137 内不偏离**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/gui.py`**（`Api` 类）：
   - 新增 `get_main_geometry()` 返回 `{ok, x, y, w, h, docked}`；异常路径返回 `{ok: False, error}`。

### 前端

1. **`src/relay/web/live_panel.css`**：
   - `.live-panel-body .window-controls` 改总览页同款液态玻璃（rgba 0.11 + blur 23 + saturate 112 + 透明 border + 11px 圆角 + 卡片阴影）。
   - `.lp-watermark span` font-weight 800 → 200。
   - 新增 `.live-panel-body.glow-docked .bg-glow-1 / .bg-glow-2` 偏移规则（CSS 变量 `--glow-mw / --glow-mh` 驱动）。
   - 新增 `.live-panel-tool-card .lp-tool-name`（等宽字体）/ `.live-panel-tool-card .live-panel-tool-age`（margin-left: auto 推右）。
   - `.live-panel-tool-args` max-height 120px 去掉（独立成卡由 layout 决定整卡高）。

2. **`src/relay/web/live_panel.js`**：
   - 新增 `syncGlow()`：调 `get_main_geometry` → dock 时 `body.glow-docked` + 写 CSS 变量。
   - `setInterval(syncGlow, 1000)` + `resize` 时同步触发。
   - DOMContentLoaded 阶段调一次 `syncGlow()`。
   - 工具调用管理重写：
     - `applyTools` 每条建独立 `.live-panel-card.live-panel-tool-card` 进 `_toolEls / _toolsOrder`。
     - `nextFreeCircledIdx` 从 rid 序号池里取最小空位。
     - `clearTools` / `relayLivePanelInit` 完整遍历 `_toolEls` 摘下 DOM。
   - `layout()` items 合并：rid 容器 + tools 容器合并为同一 items 列表；`toolNaturalHeight` 实测每张卡 offsetHeight。

### 测试

未新增测试（修复是单点 docked 分支；现有 `panel_pool` 由 PyWebView 集成的 GUI 跑通，jsdom 测不到 native window）。

### 行为验收清单（手动测试项）

- [ ] 实时流关闭 X 按钮：背景是液态玻璃（半透明白 + blur），三主题都看得清
- [ ] 「等待」两字变细（200 轻体），比之前的 800 重体更轻盈
- [ ] dock 贴右时：主窗右侧的模糊光晕平滑延伸到实时流栏，缝处无色层分裂
- [ ] 浮动时：侧栏光晕回落到窗口相对（自包含），不再受主窗几何影响
- [ ] 并发工具调用：每条独立成卡（玻璃卡样式 + 序号 ① ② ③…），按到达序见缝插针到不同列
- [ ] tool 卡多到列满：auto_extend 自动开新列延展窗口宽度
- [ ] tool 自动清除（`_toolsClearSec` 后）：所有 tool 卡同时清掉

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/gui.py` | 改（+约 15 行：`Api.get_main_geometry`） |
| `src/relay/web/live_panel.css` | 改（+约 30 行：液态玻璃 / 字重 / .glow-docked / tool-card 样式） |
| `src/relay/web/live_panel.js` | 改（+约 80 行：syncGlow + 工具管理重写 + layout 合并 tools） |
| `src/relay/web/live_panel.html` | 改（CSS cache 版本 bump + JS cache 版本 bump） |
