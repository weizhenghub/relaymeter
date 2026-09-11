# 实时栏浮动解耦 + 关闭按钮不再挤占容器空间（v0.136）开发文档

## 1. 用户的初始指令

> 1、实时流窗口脱离磁吸后，当出现新容器或延展等动作，会跳回磁吸位置，需要修正此行为，只要脱离磁吸区就彻底解耦
> 2、实时流窗口顶上的关闭"x"按钮不挤占容器空间，实际功能按钮允许移到接近顶部边缘的位置

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 浮动解耦：拖离磁吸后，新容器 / 自动延展 / 主窗移动 / 还原都不拽回磁吸位 | 指令 1 |
| B | 浮动解耦是彻底脱钩 —— 任何几何重算都不再 move | 指令 1（"彻底解耦"） |
| C | 关闭 X 按钮不再挤占容器空间（不再预留 32px 顶栏带） | 指令 2 |
| D | 关闭 X 按钮允许贴近窗口顶部边缘 | 指令 2 |

### 隐含但需要确认的点（设计自决）

- **浮动位置持久化**：浮动后 X 关闭窗口再打开要回到原位，不落到屏外（-32000,-32000）。
- **docked_getter 注入方式**：池通过 callback 读 gui 的 `_panel_docked`，避免面板轮询主窗状态。
- **新容器 / 延展的列扩展**：浮动时 auto_extend 仍要工作（窗口变宽），但只改宽度，不动位置 / 高度。
- **主窗还原**：dock 态还原才贴右；浮动态还原只重显不拽。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位 bug 根因

**PanelPool._apply_geometry**（`panel_pool.py:557-592`）**无条件** move 侧栏到 `(主窗右缘, 主窗顶)`。触发链：

```
新容器/延展 → live_panel_layout(nCols) → pool.request_width(ncols)
  → pool._relayout() → _apply_geometry()
    → window.move(主窗右缘, 主窗顶)   ← 即使浮动也拽回
    → window.resize(target_width, 主窗高度)   ← 同时改高度
```

修前 `_panel_docked` 标志只在 `_dock_panel(force=False)` 入口处跳过 `_dock_panel`（主窗 moved/resized 路径），**但 _apply_geometry 这条路径从未读 `_panel_docked`** —— 真正的 bug。

### 第二阶段：设计解耦方案

按"dock 态 vs 浮动态"两种语义分开 `_apply_geometry`：

```
_apply_geometry:
  if docked:
    window.move(主窗右缘, 主窗顶)
    if (width, height) 变化: window.resize(width, 主窗高度)
  else (浮动态):
    缓存当前位置 → _float_pos
    仅 width 变化时原地 resize(width, 当前高度)   ← 不 move、不跟主窗高
```

`_show_always_one` 在浮动 + 隐藏后重显时优先 move 回 `_float_pos`。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `gui.py` 在 `PanelPool` 构造前初始化 `self._panel_docked = True` | 无 |
| 2 | `gui.py` 给池传 `docked_getter=lambda: self._panel_docked` | #1 |
| 3 | `panel_pool.py` 池接收 `docked_getter` 参数，存 `self._docked_getter` | #2 |
| 4 | `panel_pool.py` `_apply_geometry` 按 docked 分两态 | #3 |
| 5 | `panel_pool.py` `_show_always_one` 在浮动 + `_float_pos` 非空时 move 回浮动位 | #4 |
| 6 | `panel_pool.py` 新增 `set_float_pos(x, y)` 公共方法 | #4 |
| 7 | `gui.py` `_update_panel_snap` 脱开时调 `pool.set_float_pos(x, y)` | #6 |
| 8 | `gui.py` `_on_main_restored` / `_show_from_tray` 尊重浮动态 | #4 |
| 9 | `live_panel.css` `.lp-col` 顶 padding 32px → 4px；新增 `.live-panel-body .window-controls { top:3px; right:6px }` | 无 |
| 10 | 资源版本 bump | #1-#9 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 解耦粒度 | 浮动态 = 不 move + 不跟主窗高，仅原地改宽 | 用户原话"彻底解耦"；保留 auto_extend 延展能力但保留拖到的位置/大小 |
| `_panel_docked` 初始值 | True（启动即贴右） | 不变 v0.97/v0.130 行为；首次启动仍贴右 |
| 浮动位置缓存 | `_float_pos: Optional[(int, int)]` 存最近一次拖到的位置 | 隐藏再显示能恢复（X / 全部隐藏 / 主窗最小化） |
| 浮动 + 窗口大小 | 保留当前高度（不跟主窗 mh） | 用户拖到的尺寸即所愿 |
| docked_getter 注入 | lambda 闭包读 `self._panel_docked` | 池不直接耦合 gui，模块边界干净 |
| 关闭按钮贴近顶 | top: 3px / right: 6px（vs 主窗 top: 6px / right: 8px） | 侧栏窄 400px，按钮更贴顶角；列内容也从顶开始（padding 4px） |
| 列顶 padding | 32px → 4px（var(--spacing-xs)） | 按钮不再预留顶栏带，列内容从顶开始 |

---

## 4. 实现中遇到的问题

### 问题 1：`_panel_docked` 初始化晚于 PanelPool 构造

**症状**：原代码 `self._panel_docked = False` 在 `__init__` 末尾（第 2722 行附近），而 `PanelPool` 构造在第 2671 行 —— 构造时 `docked_getter` 找不到属性。

**解法**：把 `self._panel_docked = True` 提到 `PanelPool` 构造前（第 2677 行）；原位置留注释"已在 Pool 构造前初始化"。

### 问题 2：池 worker 拿到 closure 的旧值

`_apply_geometry` 里 `docked = bool(self._docked_getter())` 在**调用时**读最新值，不是构造时；放进 `_enqueue_op` 的 lambda 也只闭包 `w/cw/ch` 等几何值，**没有闭包 docked 标志**（move/resize 入队即可）。

### 问题 3：浮动态 resize 时主窗高不可用

**症状**：docked 分支用 `mh = self._dock_getter()[3]`；浮动态不该跟主窗高。

**解法**：浮动态从 `w.height` 读**当前**窗口高，不读主窗高。`getattr(w, "height", 0) or self._last_panel_h or self._panel_height` 三层兜底（首次布局前 height 可能 0）。

### 问题 4：`_show_always_one` 在浮动 + 隐藏时会落到 (-32000,-32000)

**症状**：`_hide_always_one` 把窗口 move 到屏外。浮动 + X 关掉后 `_show_always_one` 不补 move → 重显时屏幕看不到。

**解法**：在 `_show_always_one` 末尾判断 `not docked and _float_pos is not None`，从 `_float_pos` 读取并 move 回去。`docked` 分支 `_apply_geometry` 已经贴右，无需额外 move。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 初始化顺序 | `self._panel_docked = True` 提到 PanelPool 构造前 | gui.py |
| #2 closure | 入队 lambda 只闭包几何值，docked 即时读 | panel_pool.py |
| #3 浮动态高度 | 从 `w.height` 实读，不读主窗 | panel_pool.py |
| #4 浮动隐藏 | `_show_always_one` 在浮动 + `_float_pos` 非空时 move 回 | panel_pool.py |

**最终 _apply_geometry 关键改动（panel_pool.py）：**

```python
docked = bool(self._docked_getter()) if self._docked_getter is not None else True
if docked:
    self._enqueue_op(lambda ww=w, xx=mx + mw, yy=my: ww.move(xx, my))
    if width != self._last_panel_w or mh != self._last_panel_h:
        self._last_panel_w = width
        self._last_panel_h = mh
        self._enqueue_op(lambda ww=w, cw=width, ch=mh: ww.resize(cw, ch))
else:
    if self.always_one_visible:
        self._float_pos = (
            int(getattr(w, "x", 0) or 0),
            int(getattr(w, "y", 0) or 0),
        )
    cur_w = int(getattr(w, "width", 0) or self._last_panel_w or self._panel_width)
    cur_h = int(getattr(w, "height", 0) or self._last_panel_h or self._panel_height)
    if width != cur_w:
        self._last_panel_w = width
        self._last_panel_h = cur_h
        self._enqueue_op(lambda ww=w, cw=width, ch=cur_h: ww.resize(cw, ch))
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **docked_getter 注入**：池通过 callback 读 gui 的 `_panel_docked`（不直接 import gui）。
- **docked/浮动态分支**：`_apply_geometry` 按 docked 分两态；浮动 = 不 move + 高度从 w.height 读 + 仅在 width 变时 resize。
- **`_panel_docked = True` 初始值**：提前到 PanelPool 构造前。
- **浮动位置持久化**：`_float_pos` 缓存最近一次位置，`_show_always_one` 恢复用。
- **`_update_panel_snap` 脱开时上报**：调 `pool.set_float_pos(x, y)`。
- **关闭按钮贴顶**：CSS `.live-panel-body .window-controls` top:3px/right:6px。
- **列顶 padding 4px**：列内容从顶开始，不再为按钮预留 32px。

### 偏离之处：

- **(a) 浮动时不读主窗高度**：plan 没明确说，但浮动态语义"彻底脱钩"要求不跟主窗高（主窗变了高度也不会拽侧栏），**这是规划内推论**。
- **(b) 浮动态仍 resize 延展宽度**：plan 没明确，但 auto_extend 是用户早就要求过的能力，浮动时仍要能延展（否则窗口就被锁在 panel_width），**沿用 v0.130 行为**。
- **(c) `[dock_panel_semaphore]` 防递归**：plan 没提，但 _dock_panel → window.move → events.moved → _on_panel_moved → _update_panel_snap 的递归防护（v0.97 已有）保持不变 —— 浮动态不走 _dock_panel 也就不会触发该链，**沿用现状**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/gui.py`**（`App.__init__`）：
   - `PanelPool` 构造前初始化 `self._panel_docked = True`（原位置 `False` 的赋值改为注释"已在 Pool 构造前初始化"）。
   - `PanelPool` 构造传 `docked_getter=lambda: self._panel_docked`。
   - `_update_panel_snap` 脱开 + `_panel_docked=False` 后调 `pool.set_float_pos(x, y)`。
   - `_on_main_restored` / `_show_from_tray` 在 `if self._panel_docked:` 内才调 `_dock_panel(force=True)`。

2. **`src/relay/panel_pool.py`**：
   - `__init__` 新增 `docked_getter: Callable[[], bool] | None = None` 参数，存 `self._docked_getter`。
   - 新增 `self._float_pos: Optional[tuple[int, int]] = None` 浮动位置缓存。
   - 新增 `set_float_pos(x, y)` 公共方法（带 lock）。
   - `_apply_geometry` 重写为 docked/浮动 两态（见第 5 节最终代码）。
   - `_show_always_one` 末尾判断浮动 + `_float_pos` 时 move 回浮动位再 show。
   - `_relayout` docstring 更新（dock vs 浮动态两种语义）。

### 前端

1. **`src/relay/web/live_panel.css`**：
   - `.lp-col` 顶部 padding `32px → var(--spacing-xs)`（4px），列内不再为关闭按钮预留 32px 顶栏带。
   - 新增 `.live-panel-body .window-controls { top: 3px; right: 6px; }`（侧栏内按钮更贴顶角；主窗 `.window-controls` 不受影响）。

### 测试

未新增测试（修复是单点 docked 分支；现有 `panel_pool` 由 PyWebView 集成的 GUI 跑通，jsdom 测不到 native window）。

### 行为验收清单（手动测试项）

- [ ] 启动实时栏 → 拖出主窗右缘磁吸区（纵向超出重叠），放手后停在原位
- [ ] 浮动态时发新请求 → 容器出现 / 自动延展宽度 → 侧栏仍停在原位、不被拽回磁吸位
- [ ] 浮动态时移动主窗 → 侧栏不动（之前会被拽回磁吸位）
- [ ] 浮动态时最小化主窗再还原 → 侧栏在原位重显（不拽回磁吸位）
- [ ] 浮动态时 X 关掉侧栏再点顶栏「实时流」按钮重开 → 侧栏回到原浮动位置（不是屏外或磁吸位）
- [ ] dock 态（贴右）时上述所有动作行为不变（仍贴右 + 高度跟主窗）
- [ ] 关闭 X 按钮贴近窗口右上角（top 3px / right 6px），不再压住容器
- [ ] 列内容从窗口顶部 4px 开始（之前 32px 空带）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/gui.py` | 改（_panel_docked 初始化提前 + docked_getter 注入 + 3 个 dock 函数尊重浮动态） |
| `src/relay/panel_pool.py` | 改（docked_getter 参数 + _float_pos + set_float_pos + _apply_geometry 双分支 + _show_always_one 浮动恢复） |
| `src/relay/web/live_panel.css` | 改（.lp-col padding-top 32→4 + 侧栏 .window-controls 贴顶） |
| `src/relay/web/live_panel.html` | 改（无 — 未 bump 资源版本；CSS/JS 未变） |
