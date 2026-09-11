


# 悬浮球 + 实时侧栏合并成单窗口（v0.180）开发文档

## 1. 用户的初始指令

> （v0.179 幽灵球替换 GDI 完成后）"可以了，然后，将侧边栏和悬浮球直接合并成一个窗口，需要展开时，以悬浮球为现成webview容，向两边延展长度，绘制窗口。描述有些抽象，你能明白我的意思吗？"

前置背景：v0.179 用纯 pywebview 幽灵球（`BallLayer2`，96px 透明幽灵）替换 GDI+ 后，悬浮球（`relay/ball_layer2.py`）与实时侧栏（`panel_pool._always_one_window`，400px live_panel 面板）仍是**两个独立原生窗口**。用户希望把它们合并成**一个窗口**：收起态 = 球形态（透明幽灵球、桌面锚点）；要展开时**复用球那个 webview 窗口本身**，以球为现成容器，向**右**延展外画侧栏内容；收起时同一窗口缩回球的尺寸。

用户锁定的生产行为契约（问答确认）：
- **透明度**：球区真透明（幽灵漂浮感），面板区为**实心不透明卡片**。放弃面板半透明光晕（键控下任何透明像素都会成孔洞）。
- **球的角色**：球固定留**左端当锚点帽**，点击它收起/展开面板。收起时窗口缩回纯球尺寸，s2 倒立红心态仍可见。
- **展开方向**：球在**左**，只往**右**延展。
- **展开后几何**：球方左上 + 面板随球高起步（球帽方留在左上，面板在其右侧，高度从球高长到整面板高）。

用户已确认的路线（问答）：单窗合并落地（球窗即面板容器）+ 面板实底 + 球留左锚点 + 只往右延展。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 单渲染层 `ghost_panel.html` 同时承载「球帽 + 面板」两种形态（同一 DOM，非两个窗口） | 指令 |
| B | 收起态 = 纯球（窗口 = ball_size 方，面板 `display:none`，球区真透桌面）；展开态 = 球方留左 + 右面板 | 指令 + 用户问卷 |
| C | 球留**左端**当锚点帽，点击它收起/展开面板 | 用户问卷 |
| D | 展开方向只往**右**，球方留在左上、位置恒不变 | 用户问卷 |
| E | 透明：球区真透 + 面板实底（同一键控窗口内共存） | 用户问卷 + v0.179 键控原理 |
| F | 点击球帽 → `set_expanded` 翻转展开态；球帽视觉 S1<->S2（绿=展开带动画 / 红倒立=收起）保留 | 生产契约 |
| G | `always_one_window`（磁吸用）保留：关球回磁吸模式恢复 dock 贴右 | 架构 |
| H | 消除跨窗 Z 序之争（单窗无「球 > 侧栏」压序问题） | 合并收益 |

### 隐含但需要确认的点（用户已确认）

- 面板区实底 `--root-bg`（非键控色 #010203）→ 键控无孔洞。愿意放弃面板半透明光晕。✅
- 球作为锚点帽固定不动（球帽方恒在窗口左上），面板向右延展。✅
- 展开时窗口从球方拉宽到 `ball_size + panel_width`，高度 = 面板高度。✅

---

## 3. 分析需求后得出的开发路径

### 核心结论：键控透明作用于整窗表面，球区透明与面板实底可共存

v0.179 已证：键控窗体 `BackColor=TransparencyKey=#010203` 把所有 `=(1,2,3)` 的像素键空。这一键控**作用于整窗表面**，不是分区域。因此在**同一个键控窗口**里：

- **球帽区**（透明 HTML，无底色矩形）露出窗体 `BackColor`=#010203 → 被键空 → **真透桌面**。
- **面板区**（实心 `--root-bg`/`--panel-bg` 底 ≠ 1,2,3）→ **不透明**，键控不碰它。

**只要面板 DOM 像素级不透明即可共存**。v0.180 先做可行性 demo（`scratch/webview_ball_demo/ghost_panel_demo.py` + `cap_screen_hwnd.py`）**逐像素抓包验证**了这一点：展开态 596x900 → LEFT 球区透桌面（幽灵身体 `(210,124,99)` + 眼睛，无面板底色漏入）+ RIGHT 面板区实底白 `(255,255,255)` **0 键控孔洞**；收起态 96x96 正常。这是整个方案最大风险点的验证。

### 为什么面板必须实底

WebView2 不能做真逐像素 alpha（v0.165 已证），键控是唯一透明途径。键控会把任何等于 #010203 的像素键空 → 面板区若带半透明/透明像素就会成桌面孔洞。故面板必须**实心不透明**。

### 为什么合并能消除一类 bug

侧栏与主窗同 TopMost，其 resize/show 用 `SetWindowPos(SWP_SHOWWINDOW→HWND_TOP)` 会压过球；球的 top-keep 又把球压回最顶 → 两机制交替胜出 → 宽度动画期间球「悬浮在侧栏上面 ↔ 被侧栏遮挡」反复闪烁。合并成**单窗**后，`always_one` 不再参与球模式布局，`_resize_safe`/`_anim_resize` 的 Z 序竞争在球模式**一并消失**。

### 实现路径（按依赖顺序 5 个子任务）

```
#35 src/relay/web/ghost_panel.html     (渲染层：单窗球帽+面板布局，后端桥)
#32 src/relay/ball_layer2.py           (BallLayer2 改单窗容器 + set_expanded + resize)
#36 panel_pool.py                      (球交互与几何改走球窗路由)
#33 gui.py                             (float_ball_url → ghost_panel.html + owned 调整)
#34 验证：py_compile + demo 级冒烟 + 确认 web 资产进包
```

技术关键决策：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 渲染层 | 新 `ghost_panel.html`（复用 live_panel 结构 + 幽灵 SVG） | 单窗承载两形态；live_panel.js 原样加载 |
| 透明度 | 球区透明（键空 #010203）+ 面板实底 `--root-bg` | 键控整窗表面，面板必须实底防孔洞 |
| 球锚点 | `#ghost-cap` 恒 ball_size 方，`#ghost-panel-surface` 右侧实底 | 球留左锚点，面板向右延展 |
| 展开/收起 | `set_expanded(bool)` → `ghostSetExpanded` + MoveWindow 扩/缩窗宽 | MoveWindow 保留左上角（球帽位置不变） |
| 面板尺寸 | `set_panel_size_fn(getter)` 实时口径（target_width clamp） | 面板随列数 auto-extend 动态变宽 |
| 命中判定 | `_over_ball_inner` 只对左端球帽方区内切圆 | 展开态窗变宽，不再假设整窗是圆 |
| Z 序 | 去掉跨窗侧栏压序，仅保留置顶保活 | 单窗无 Z 之争 |
| 磁吸模式 | `always_one_window` 保留，关球即恢复 dock | 两模式互斥，dock 侧栏仍贴右 |

---

## 4. 实现中遇到的问题

### 问题 1：`create_window` 需要 `js_api` 才能让 live_panel.js 的 bridge 工作

球窗口加载 `ghost_panel.html`（内含 live_panel DOM + live_panel.js），live_panel.js 里 `window.pywebview.api.live_panel_layout / panel_close / get_live_panel_api_key / get_main_geometry / ballClicked` 等桥必须可用。原 `BallLayer2.create_window` 没传 `js_api`。

**解法**：`_create_ball` 给 `BallLayer2` 传 `js_api=self._api`（gui.Api 实例），`BallLayer2._create_and_show` 透传给 `webview.create_window(js_api=...)`。方法按名分派，球窗与 always_one 复用同一 Api。

### 问题 2：球帽 CSS 变量 `--ball-size` 默认值 ≠ 生产球尺寸

`ghost_panel.html` 里 `#ghost-cap` 用 `var(--ball-size, 56px)`，但生产球尺寸是 `ball_size`（可配置）。若不让页面知道实际 `--ball-size`，球帽方区会按 56px 渲染，与 Python 侧 MoveWindow 的球方尺寸不一致。

**解法**：`_on_loaded` 里（设 TransparencyKey 之前）`evaluate_js("window.ghostSetBallSize && window.ghostSetBallSize(<ball_size>);")`，页面据此设 `--ball-size`，让 SVG 球帽方区与原生球方严格对齐。

### 问题 3：展开态窗口变宽，`_over_ball_inner` 圆命中失效（原按整窗圆算）

v0.179 的 `_over_ball_inner` 用「光标距窗中心 ≤ 0.48 边长」当命中。展开态窗口 = 球方 + 面板，整窗中心早不在球上，判定失效。

**解法**：`_over_ball_inner` 只对**左端球帽方区**（`ball_size × ball_size`，恒在窗口左上）的**内切圆**判定：`cap = ball_size * scale`，圆心 `(r.left + cap/2, r.top + cap/2)`，命中 `hypot(dx,dy) <= cap*0.48`。收起态窗 = 球方，行为不变。

### 问题 4：面板「收起」语义在不同入口不一致（X 按钮 vs 球点击）

面板 X 按钮（`panel_close` → `_hide_panel_and_sync_off`）在球模式下若走回「hide always_one」路径会关掉实时流开关并藏错窗口（always_one 在球模式未参与布局）。而球点击（`_hide_always_one` 球分支）应收起球窗 + S2 球帽视觉。

**解法**：`_hide_panel_and_sync_off` 加球模式分支 → `pool._hide_always_one()`（球模式分支已实现收起球窗 + S2 + `always_one_visible=False`），并**不**关开关、不藏 always_one。两条入口语义统一。

### 问题 5：`_apply_geometry` 需在球模式早退，避免误操作 always_one

`_apply_geometry` 原来的球模式分支仍对 `always_one_window` 做 move/resize。合并后球模式几何应由**球窗自身**（`set_expanded` + `refresh_geometry`）驱动，不该碰 always_one。

**解法**：`_apply_geometry` 开头加球模式早退：`if self._float_ball_enabled and self._ball is not None: self._relayout_ball_panel(); return True`。磁吸（dock）模式不受影响（关球后 `_float_ball_enabled=False`，走原 dock 分支）。

### 问题 6：`set_expanded` 后需同步 `always_one_visible`

球模式下 `_clear_rid` 的空闲自动收起判定用 `always_one_visible` 判断「面板显示中」。若 `set_expanded(True)` 不置位它，最后一个 rid 清掉后 watchdog/`_clear_rid` 不会自发收起面板（把 `_rids` 清空时残留展开态）。

**解法**：`_set_ball_expanded`（pool 层）同步 `self.always_one_visible = on`。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 js_api 透传 | `BallLayer2` 构造接收 `js_api`，`create_window(js_api=...)` | ball_layer2.py / panel_pool.py |
| #2 `--ball-size` 不匹配 | `_on_loaded` 设键控前 `ghostSetBallSize` 推球帽边长 | ball_layer2.py |
| #3 展开态圆命中失效 | `_over_ball_inner` 只对左端球帽方区内切圆判定 | ball_layer2.py |
| #4 面板收起语义不一 | `_hide_panel_and_sync_off` 球分支走 `pool._hide_always_one()` | gui.py |
| #5 `_apply_geometry` 误操 always_one | 球模式早退 → `_relayout_ball_panel` | panel_pool.py |
| #6 `always_one_visible` 不同步 | `_set_ball_expanded` 同步标志 | panel_pool.py |
| #7 面板 JS 路由到球窗 | `_panel_route_window()` 球模式返回 `ball.window` | panel_pool.py |
| #8 面板尺寸实时口径 | `_ball_panel_size()` getter（target_width clamp）注入 `set_panel_size_fn` | panel_pool.py |

---

## 6. 是否完全遵循规划路径开发

**基本完全按规划。** 5 个子任务（渲染层 → BallLayer2 容器 → panel_pool 路由 → gui URL → 验证）全部按依赖顺序落地，未出现规划外的大改动。

### 完全按规划：

- 新渲染层 `ghost_panel.html` = 球帽 SVG + live_panel 结构，复用 `live_panel.css` / `live_panel.js`。
- 透明策略落地：球区透明（键空 #010203）+ 面板实底 `--root-bg`（非键控色），可行性 demo 逐像素验证。
- 球留左端锚点帽，点击它收起/展开（`set_expanded`），只往右延展。
- 展开 = MoveWindow 从球方拉宽到 `ball_size + panel_width`，收起 = 缩回球方，左上角（球帽位置）恒不变。
- `always_one_window` 保留（磁吸 dock），关球开关恢复贴主窗右缘。
- 消除跨窗 Z 序之争（单窗无「球 > 侧栏」压序），Z 序保活仅保留置顶。

### 偏离之处：

- 无规划外重大调整。开发中补充了几处接线细节（js_api 透传、`--ball-size` 推送、`_apply_geometry` 球模式早退、`always_one_visible` 同步），均属实现本文档 §3 决策点的必要落地，非方向性变更。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/ball_layer2.py`**（改，单窗球+面板容器）：
   - 新增构造参数：`panel_url / js_api / panel_width / panel_height`（`panel_url` 默认 `ghost_panel.html`）。
   - 新增方法：`set_expanded(bool)`、`set_panel_size_fn(fn)`、`set_panel_size(w,h)`、`refresh_geometry()`、`@property expanded / window`。
   - `_create_and_show()`：`url=self._panel_url`、`js_api=self._js_api`。
   - `_on_loaded()`：设键控前 `ghostSetBallSize(ball_size)`。
   - `_apply_panel_geometry()`：按展开/收起态 MoveWindow 到 `(球方+面板)` 或 `(球方)`，保留左上角 `_pos`。
   - `_over_ball_inner()`：只对左端球帽方区内切圆判定。
   - `_topkeep_tick()`：去掉跨窗侧栏压序，仅置顶保活。
   - 公开 API 向后兼容（新增方法仅扩展）。

2. **`src/relay/panel_pool.py`**（改）：
   - `_create_ball`：给 `BallLayer2` 传 `panel_url=self._float_ball_url` + `js_api=self._api`；建后 `set_panel_size_fn(self._ball_panel_size)` + `_relayout_ball_panel()`。
   - 新增 `_panel_route_window()`（球模式返 `ball.window` 否则 `always_one_window`）、`_ball_panel_size()`（target_width clamp 屏余宽/屏高）、`_relayout_ball_panel()`（调 `ball.refresh_geometry()`）、`_set_ball_expanded(on)`（同步 `always_one_visible`）。
   - `ball_clicked`：翻转展开态（S1→展开 + idle；S2→收起 + s2）。
   - `_apply_geometry`：球模式早退 → `_relayout_ball_panel()`。
   - `_show_always_one`/`_hide_always_one`：球模式分支 → 展开/收起球窗。
   - JS 路由（`set_concurrent`/`push_event`/`push_event_always_one`/`_push_rid_timeout`/`_clear_rid`/`set_auto_extend`）：改用 `_panel_route_window()`。
   - 磁吸（dock）路径不变：关球后 `always_one_window` 恢复贴右。

3. **`src/relay/gui.py`**（改）：
   - `float_ball_url = (_WEB_DIR / "ghost_panel.html").as_uri() + "#theme=" + theme_name`。
   - `_hide_panel_and_sync_off`：球模式分支 → `pool._hide_always_one()`（收起球窗，不关开关、不藏 always_one）。
   - `_dock_panel` 球模式（已如此）走 `pool.refresh_geometry()`；`_sync_panel_owner`/`_sync_panel_topmost` 因 `_dock_panel` 球模式早退不会把 always_one 绑定为 owned / 置顶。

### 渲染层（HTML）

4. **`src/relay/web/ghost_panel.html`**（**新增**）：
   - `#ghost-root` flex.row（`.live-panel-body` 结构，加载 `styles-20260817.css` + `live_panel.css` + `live_panel.js`）。
   - 左 `#ghost-cap`：恒 `ball_size` 方，透明，内 SVG 幽灵（`viewBox 0 0 100 100`，`preserveAspectRatio` 不随窗拉伸）；状态 class `idle/flow/s2 → ghost-idle/ghost-flow/ghost-done`，halo/ring/heart 动画保留。
   - 右 `#ghost-panel-surface`：实底 `background: var(--root-bg)`（防键控孔洞），内含 `.bg-glow-*`（装饰，实底表面非键控色）、`.window-controls`（`#panel-btn-close` X 按钮）、`.live-panel-main` → `.lp-col#lp-col-1`（端点/工具卡，复用 live_panel 结构）。
   - `html { background: transparent !important }`；收起态 `body.collapsed #ghost-panel-surface { display:none }`；展开态球方留左上。
   - 后端桥：`ghostSetState(state)` / `ghostSetExpanded(bool)` / `ghostSetBallSize(px)` / `ghostClicked()`（→ `pywebview.api.ballClicked`），并保留 `ballSetState`/`ballClicked` 兼容别名。

### 不改的东西

- `src/relay/web/ghost_ball.html`（v0.179 单球）**保留不删**（回滚参考）。
- `src/relay/web/float_ball.html`（v0.165 水波球）+ `src/relay/ball_layer.py`（GDI）**保留不删**。
- 磁吸（dock）模式行为不变：关球开关恢复贴主窗右缘的 always_one 侧栏。
- `pyproject.toml` **无需改**：`web/*.html` 通配自动含 `ghost_panel.html`。

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/ghost_panel.html` | **新增** |
| `src/relay/ball_layer2.py` | 改（单窗球+面板容器） |
| `src/relay/panel_pool.py` | 改（球模式路由 / 几何） |
| `src/relay/gui.py` | 改（URL + owned 调整） |
| `src/relay/web/ghost_ball.html` | **不动**（保留回滚） |
| `src/relay/web/live_panel.html`/`.css`/`.js` | **不动**（被 ghost_panel.html 复用） |
| `pyproject.toml` | **无需改**（`web/*.html` 通配） |

### 关键陷阱（务必注意）

1. **面板区必须像素级不透明**：`.ghost-panel-surface` 给实心 `--root-bg`（非 #010203），否则键控下任何透明像素都成桌面孔洞。这是「球区透明、面板实底」落地关键。
2. **球帽命中只对球区判定**：`_over_ball_inner` 用 `ball_size×scale` 方区内切圆，不用整窗宽。
3. **`MoveWindow` 而非 `SetWindowPos` 做扩/缩**：SetWindowPos 在这类窗体返回 TRUE 却不动。
4. **`_win_w`/`_win_h` 仍 `global`**（`_mouse_tick` 借用）；展开态拖动用**按下时捕获的实际窗宽**（`_win_w`），拖动保留面板宽度。
5. **透明键控时机**：`loaded` + `sleep(2)`，不能改回 `shown`。
6. **`always_one_window` 在球模式只作磁吸后备**：`_apply_geometry` 球模式早退，绝不碰它（否则 dock/球布局互相污染）。
7. **`_set_ball_expanded` 同步 `always_one_visible`**：否则空闲自动收起失效。

### 行为验收清单（手动测试项）

- [ ] GUI 重启后球显示为幽灵；点球展开面板（单窗从球方拉宽），再点收起（缩回球方）；球帽位置（左上角）恒不变
- [ ] 收起态：球区透桌面（真透明），幽灵身体/眼睛清晰
- [ ] 展开态：右侧面板实底卡片无键控孔洞，球帽区仍透桌面
- [ ] 流式时球帽 flow 动画；有流 s2 倒立红心；无请求 idle
- [ ] 面板 X 按钮在球模式收起球窗（不关实时流开关）
- [ ] 关球开关 → 恢复磁吸 dock 侧栏（贴主窗右缘）
- [ ] 任何原生失败 → 静默无球（不做成错看方窗/白底）

### 冒烟验证记录

- `py_compile` ball_layer2.py / panel_pool.py / gui.py 全绿。
- 独立冒烟（`scratch/webview_ball_demo/smoke_ghost_panel.py` + `cap_hwnd.py`，生产 `ghost_panel.html` + `BallLayer2` + `set_panel_size_fn(500,900)`）：
  - 展开/收起四件套：收起 96x96 → 展开 596x900 → 收起 96x96 → 展开 596x900，左上角恒 (200,120)，`native.Width/Height` 实测对照 ✓。
  - 屏幕 DC 抓包展开态：整窗 0 键控像素；LEFT 球区透桌面（幽灵 `(210,124,99)`=#D27C63 + 眼睛/光晕，无面板底色漏入）+ RIGHT 面板区实底 `--root-bg` `(245,245,247)`=#f5f5f7 0 键控孔洞 → 「球透明 + 面板实底」共存落地 ✓。
- `web/*.html` 通配已在 pyproject.toml，`ghost_panel.html` 自动进包 ✓。
