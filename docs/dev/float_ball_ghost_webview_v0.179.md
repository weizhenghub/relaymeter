

# 纯 pywebview 幽灵球替换 GDI 悬浮球（v0.179）开发文档

## 1. 用户的初始指令

> （v0.178 demo 验证后）"很好，用这个替换掉GDI吧"

前置背景：v0.165 用 GDI+ 自绘（`ball_layer.py` 的 `BallLayer`，`UpdateLayeredWindow` 逐像素 alpha）做悬浮球，因为当时 WebView2 透明两次实测失败。v0.178 实验（`scratch/webview_ball_demo/mini_ghost_ball_keyed.py`）找到真透明配方后，用户要求在 demo 基础上确认的透明幽灵球替换生产 GDI。

用户锁定的生产行为契约（问答确认）：
- 状态由中继活动驱动：idle=空闲 / flow=流式 / s2=有流倒立。
- 点击=切 S1<->S2（侧栏收起/展开），**不做** demo 的形态循环 / 自动定时器。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 新渲染层 = demo 的幽灵 SVG（陶土橙 #D27C63 + 竖眼 + 波浪下摆 + s2 倒立红心） | 指令 |
| B | 状态驱动走 CSS class（idle/flow/s2），**去掉** demo 的 `setInterval` 自动定时器 + `ballCycle()` 形态循环 | 指令 + 用户"去掉自动跳状态" |
| C | 点击不再由 JS 切形态，改走 `window.pywebview.api.ballClicked()` → 生产桥（`gui.Api.ball_clicked` → `pool.ball_clicked` 切 S1<->S2） | 生产契约 |
| D | 透明配方：`transparent=True` + `events.loaded`(非 shown) + `sleep(2)` 后设窗体 `BackColor=TransparencyKey=#010203` | demo 实测 |
| E | 拖动用 `MoveWindow`（`SetWindowPos(SWP_NOZORDER)` 返回 TRUE 却静默不动） | demo 实测 |
| F | `_over_ball` 用 GetWindowRect 矩形判定（WebView2 挂 Chrome_WidgetWin_0 子窗，`WindowFromPoint==hwnd` 恒 False） | demo 实测 |
| G | Z 序保活：每 300ms SetWindowPos 压回最顶 / 压侧栏到球下方 | v0.170 保留 |
| H | **panel_pool 是唯一接口面**：GUI 从不直接碰球视觉，只通过 BallLayer API 交互 → 换 import 即可换实现 | 架构 |

### 隐含但需要确认的点（用户已确认）

- 实现路线：纯 pywebview 幽灵球（新 `ball_layer2.py`，BallLayer 兼容 API）✅
- 生产契约：按生产契约（状态后端驱动、点击切 S1<->S2、无循环）✅
- 球仍作"桌面锚点"，侧栏从球位置展开（`_relayout` 球模式分支不变）✅

---

## 3. 分析需求后得出的开发路径

### 关键结论：接口面全在 `PanelPool`

`panel_pool.py` 是唯一与球交互的地方 —— 它只通过 `BallLayer` 的 `start/destroy/show/hide/move_to/set_state/set_topmost` + `on_click/on_move_end/on_move` 回调组合交互，GUI 从不直接碰球视觉。因此方案是：新写 `relay/ball_layer2.py`（`BallLayer2`，同名 API 包一个 pywebview 幽灵球），`panel_pool._create_ball` 换 import，其余接线零改动。

### 为什么不能只改 float_ball.html

现有 `src/relay/web/float_ball.html`（v0.165b）是**水波液态球**（白底 #3c9 绿波浪），**不是幽灵**。幽灵渲染只存在于 demo 的 `mini_ghost_ball.html`（SVG 幽灵）。所以必须把 demo 的幽灵 HTML 作为新渲染层迁进生产。

### 实现路径（按依赖顺序 5 个子任务）

```
#29 src/relay/web/ghost_ball.html     (渲染层，去掉自动定时器/循环，加后端桥)
#27 src/relay/ball_layer2.py          (BallLayer2，BallLayer 兼容 API)
#31 panel_pool._create_ball 换 import (零改动其余调用点)
#30 gui.py float_ball_url → ghost_ball_url
#28 验证：py_compile + demo 级冒烟 + 确认 web 资产进包
```

技术关键决策：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 渲染层来源 | demo 的 `mini_ghost_ball.html` SVG 原样 | 保持 GDI 同款视觉，改造成本最小 |
| 状态驱动 | `window.ballSetState(state)` → CSS class `ghost-idle/ghost-flow/ghost-done` | 后端推 idle/flow/s2，无 JS 帧循环；动画用 CSS 常驻 |
| 点击桥 | `window.ballClicked()` → `gui.Api.ball_clicked()` | 复用现有桥，切 S1<->S2 |
| 透明键控 | `transparent=True` + loaded + sleep(2) 设 `TransparencyKey=#010203` | demo 实测的唯一有效配方 |
| 拖动 | `MoveWindow`（绝对坐标） | SetWindowPos NOZORDER 在这类窗体静默不动 |
| `_over_ball` | GetWindowRect 矩形判定 + 球内圆命中（≤0.48 边长） | WebView2 子窗导致 WindowFromPoint 恒 False |
| 线程模型 | 窗口经 pywebview 全局事件循环；输入轮询独立 daemon 线程 | 与 always_one_window 建窗同机制 |
| 失败兜底 | 任何原生失败 → `available=False`，球静默降级为「无」 | 宁可无球，不要错球（方窗/白底） |

---

## 4. 实现中遇到的问题

### 问题 1：透明键控时机 —— `shown` vs `loaded`

在 `shown` 里设窗体 `TransparencyKey` 会触发 `RecreateHandle` 把窗口隐藏掉；在 `loaded` 里设但太早（WebView2 未完成合成）键控不生效。

**解法**：在 `events.loaded` 回调里起一个 daemon 线程，`time.sleep(2)` 等合成首帧稳定后再设 `BackColor=TransparencyKey=#010203`。用独立线程避免阻塞 UI。这是 demo 逐像素截图验证过的唯一有效时点。

### 问题 2：`SetWindowPos(SWP_NOZORDER)` 静默不动窗口

拖动时先用 `SetWindowPos(hwnd, ..., SWP_NOZORDER)` 移动，返回 TRUE 但窗口位置纹丝不动。

**解法**：改用 `MoveWindow(hwnd, x, y, w, h, True)`（绝对坐标）。demo 实测拖动跟随光标正确。

### 问题 3：`WindowFromPoint(pt) == hwnd` 恒 False

WebView2 窗体下整个 `Chrome_WidgetWin_0` 子窗覆盖窗体，`WindowFromPoint` 返回子窗而非窗体 HWND，导致光标判定恒在球外。

**解法**：`_over_ball` 改用 `GetWindowRect` 矩形判定 + 球内圆命中（光标距窗中心 ≤ 0.48 边长）。

### 问题 4：`_user32` 名字与加载器函数同名冲突（验证时发现的 bug）

模块级 `_user32 = None` 全局变量与 `def _user32():` 加载器函数**同名**。`_user32 is not None` 恒真（因为它指向函数对象本身），于是加载器永远提前返回函数而非 WinDLL，`_load_user32().GetCursorPos` 变成 `None().GetCursorPos` → 每次被 `except` 吞掉 → 拖动/点击**静默失效**（轮询线程看似 alive，实际全 False）。

**解法**：把加载器函数改名为 `_load_user32()`（与验证过的 `ball_layer.py` 一致），`_user32` 全局只存放 WinDLL。全部 9 个调用点同步更新。

### 问题 5：`_win_w/_win_h` 漏 `global` 声明

在 `_mouse_tick` 的 PRESSED 分支给 `_win_w/_win_h` 赋值，DRAGGING 分支读取。漏 `global` 声明会 `UnboundLocalError` 崩掉轮询线程。

**解法**：`_mouse_tick` 顶部加 `global _win_w, _win_h`。

### 问题 6：Z 序被侧栏/主窗压过

生产里侧栏/主窗同 TopMost，其 resize/show 用 `SetWindowPos(SWP_SHOWWINDOW→HWND_TOP)` 会压过球。

**解法**：`_topkeep_tick` 每 300ms 一次 —— 置顶时 `SetWindowPos(h, HWND_TOPMOST, ...)` 把球压回最顶；非置顶时把侧栏压到球下方。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 透明键控时机 | `events.loaded` + daemon 线程 `sleep(2)` 后设 TransparencyKey | ball_layer2.py `_on_loaded` |
| #2 SetWindowPos 不动 | 改 `MoveWindow`（绝对坐标） | ball_layer2.py `_mouse_tick` |
| #3 WindowFromPoint 恒 False | `_over_ball` 改 GetWindowRect 矩形 + 球内圆命中 | ball_layer2.py `_over_ball*` |
| #4 `_user32` 同名冲突 | 加载器改名 `_load_user32()` | ball_layer2.py |
| #5 `_win_w/_win_h` 漏 global | `_mouse_tick` 顶部加 `global` | ball_layer2.py |
| #6 Z 序被压过 | `_topkeep_tick` 每 300ms SetWindowPos 保活 | ball_layer2.py |
| #7 接口面最小化 | 只改 `panel_pool._create_ball` 一处 import | panel_pool.py |

---

## 6. 是否完全遵循规划路径开发

**基本完全按规划，一处修补（验证时发现的 bug）。**

### 完全按规划：

- 新渲染层 = demo 幽灵 SVG 原样，仅去掉自动定时器 / 形态循环。
- 状态走 `ballSetState` → CSS class，动画 CSS 常驻。
- 点击走 `ballClicked` → 生产桥切 S1<->S2（不做形态循环）。
- 新写 `ball_layer2.py`（BallLayer 兼容 API），`panel_pool` 换 import，其余零改动。
- `gui.py` `float_ball_url` → `ghost_ball_url`。
- 透明配方 / MoveWindow / GetWindowRect / Z 序保活全部照 demo 验证结果。
- 失败兜底 `available=False`（宁可无球，不要错球）。

### 偏离之处：

- **(a) 修复了一个规划外 bug**：`ball_layer2.py` 的 `_user32` 全局与加载函数同名冲突（见 §4 问题 4），导致拖动/点击静默失效。这是**冒烟验证时发现并修复**的真实缺陷，不是规划遗漏。修复方式（改名 `_load_user32`）与已验证的 `ball_layer.py` 保持一致。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/ball_layer2.py`**（**新增，BallLayer2**，BallLayer 兼容 API）：
   - 构造参数：`main_native / ball_size / scale_getter / theme_bg / on_click / on_move_end / on_move / topmost / sidebar_handle_getter`。
   - 公开方法：`start(x,y) / destroy() / show() / hide() / move_to(x,y) / set_state(mode) / set_topmost(bool)` + `@property available / position`。
   - 内部 `_create_and_show()`：`webview.create_window(title="悬浮球", url=ghost_ball_url, width/height=ball_size, min_size=(size,size), resizable=False, frameless=True, easy_drag=False, on_top=topmost, transparent=True, background_color=theme_bg)`。
   - `_on_loaded()`：起 daemon 线程 `sleep(2)` → `clr` 设窗体 `BackColor=TransparencyKey=#010203`。
   - `_mouse_tick()`：全局轮询状态机（IDLE→PRESSED→DRAGGING），`GetAsyncKeyState(VK_LBUTTON)` + `GetWindowRect` 矩形 + 球内圆命中；拖动用 `MoveWindow`，点击/拖动结束/拖动中回调均用独立线程派发。
   - `_topkeep_loop/_topkeep_tick`：每 300ms SetWindowPos 保活 Z 序。
   - `set_state(mode)` → `window.evaluate_js(f"window.ballSetState && window.ballSetState('{mode}');")`。
   - `set_topmost` → `window.on_top = topmost`。
   - 失败 → `available=False`（宁可无球，不要错球）。

2. **`src/relay/panel_pool.py`**（改 `_create_ball`，约 402 行）：
   - `from relay.ball_layer2 import BallLayer2` 替换 `from relay.ball_layer import BallLayer`。
   - `BallLayer` 引用改为 `BallLayer2`（仅 `_create_ball` 内）。
   - 其余所有 `self._ball.xxx()` 调用点因 API 同名**零改动**。

3. **`src/relay/gui.py`**（改 1 行）：
   - `float_ball_url = (_WEB_DIR / "ghost_ball.html").as_uri()`（约 2926 行），注释标 v0.179。
   - 传入 `PanelPool` 的 `float_ball_url` 现在被 `BallLayer2` 真正使用（原 GDI 时代是死参数）。

4. **`src/relay/web/ghost_ball.html`**（**新增**，生产渲染层）：
   - 幽灵 SVG（body #D27C63、eyes #6C402E、4 个下凸半圆波浪下摆、halo 光晕、s2 脉冲外环 + 红心徽标）。
   - **去掉** demo 的 `setInterval` 自动定时器 + `ballCycle()` 形态循环。
   - 后端桥：
     ```js
     window.ballSetState = function (state) { ball.setAttribute("class", CLS[state] || CLS.idle); };
     window.ballClicked = function () { if (window.pywebview?.api?.ballClicked) window.pywebview.api.ballClicked(); };
     ```
   - CSS 状态类：`#ball.ghost-idle / ghost-flow / ghost-done`；动画 bob（flow）、halo-pulse（flow/s2）、ring-pulse + heart-beat + 180° 倒立（s2）。

### 不改的东西

- `src/relay/ball_layer.py`（GDI）**保留不删**（历史对照 / 回滚），只不再被 `panel_pool` 引用。
- `src/relay/web/float_ball.html` **保留**（同 v0.165「文件保留不删」惯例）。
- `pyproject.toml` **无需改**：`relay = ["web/*.html", ...]` 通配自动含 `ghost_ball.html`。
- `panel_pool` 的 `ball_clicked / _set_ball_state / set_float_ball* / ball_mode / refresh_ball_visibility / _ball_should_show / _default_ball_pos / set_float_ball_pos_from_panel` 全部不动 —— 球仍是「桌面锚点」，侧栏从球位置展开（`_relayout` 球模式分支不变）。

### 关键陷阱（从 demo 踩坑提炼）

1. **透明必须在 `loaded` 设，且 `sleep(2)`**：`shown` 会 RecreateHandle 隐藏窗口；太早设键控不生效。
2. **拖动必须 `MoveWindow`**：`SetWindowPos(SWP_NOZORDER)` 在这类窗体返回 TRUE 却不动。
3. **`_over_ball` 用 GetWindowRect 矩形**，不能用 `WindowFromPoint == hwnd`（WebView2 挂子窗恒 False）。
4. **`_win_w/_win_h` 必须 `global`**：漏 global 会 UnboundLocalError 崩轮询线程。
5. **`_user32` 不能与加载函数同名**：加载器应叫 `_load_user32()`（同 ball_layer）。
6. **Z 序保活**：侧栏/主窗同 TopMost，球需每 300ms SetWindowPos 压回最顶（或非置顶时压侧栏）。

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/ghost_ball.html` | **新增** |
| `src/relay/ball_layer2.py` | **新增** |
| `src/relay/panel_pool.py` | 改（`_create_ball` 换 import） |
| `src/relay/gui.py` | 改（`float_ball_url` → `ghost_ball_url`，1 行） |
| `src/relay/ball_layer.py`（GDI） | **不动**（保留回滚） |
| `src/relay/web/float_ball.html` | **不动**（保留回滚） |
| `pyproject.toml` | **无需改**（`web/*.html` 通配） |

### 行为验收清单（手动测试项）

- [ ] GUI 重启后悬浮球显示为陶土橙幽灵（非方窗/白底/黑底）
- [ ] 球外真透桌面（无 #010203 色块）
- [ ] 球可拖动（跟手，松手后位置持久化）
- [ ] 点球切 S1<->S2（侧栏收起/展开）
- [ ] 流式时球为 flow 态（上下浮动 + 呼吸光晕）；有流 s2 倒立 + 红心；无请求 idle
- [ ] 置顶时球始终在侧栏之上；关闭置顶后侧栏压到球下方
- [ ] 任何原生失败 → 静默无球（不做成错看的方窗/白底）
