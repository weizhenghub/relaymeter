# 侧栏 resize Z 序中性 + 消除「悬浮球在侧栏上面↔被侧栏遮挡」闪烁（v0.171）开发文档

## 1. 用户的初始指令

> 侧边栏延展或收缩时，悬浮窗会在「悬浮在侧栏上面」和「被侧栏遮挡」两个状态间反复闪烁几次。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 侧栏延展/收缩时，悬浮球不再被闪烁遮挡 | 指令整段 |
| B | 闪烁发生在宽度变化期间（不是高度变化） | 隐含：用户用「延展或收缩」明示是宽度 |

### 隐含但需要确认的点

- 闪烁发生的精确窗口：是延展（1→2 列）+ 收缩（2→1 列）**都**有，还是只一个？宽度动画 ~700ms 内通常 3–5 次闪烁 → 与动画 32 帧 × 22ms 节奏吻合 → 大概率全程参与。
- 闪烁是「球在侧栏上 ↔ 球被侧栏挡」双向交替，不是单方向（单方向就只是遮挡问题）→ 双机制各占一阵。
- 用户没要求关闭 Z 序保活（v0.170 的 `ball_layer._topkeep_tick`，300ms 一次把球 SetWindowPos HWND_TOPMOST），也没要求改球的置顶行为 —— 不动 ball_layer.py。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位闪烁来源

1. 先用 `feedback_live_panel_zhujie.md` 锁定术语：「侧栏」= panel_pool 唯一 webview 窗口；「悬浮窗」= ball_layer 球（不是独立 OS 窗口）。
2. 读 panel_pool.py 找「延展/收缩」动作 = `_anim_resize`（宽度补间动画）+ `_apply_geometry`（列数变化触发动画）。
3. 锁定候选原因：宽度动画期间，谁在反复改 Z 序？

### 第二阶段：验证候选

1. 读 pywebview winforms.py `Form.resize`（line 600–618）→ `SetWindowPos(handle, None, x, y, w, h, 64)`。flag `64 = SWP_SHOWWINDOW` 单个，**没有 `SWP_NOZORDER`**。
2. 读 `Form.move`（line 620–643）→ 传 `SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW`（含 `SWP_NOZORDER`）。**只有 resize 是 Z 序肇事者。**
3. 读 ball_layer.py `_topkeep_tick`（line 920–930，v0.170 新增）→ 每 300ms 把球 `SetWindowPos(HWND_TOPMOST)` 压回 band 最顶。

### 第三阶段：确定机制

- 宽度动画 32 帧 × 22ms ≈ 700ms。每帧调一次 `panel_window.resize(...)`，因 `winforms.resize` flag 没有 `SWP_NOZORDER`、hWndInsertAfter 是 NULL(=HWND_TOP) → 侧栏每帧被抬到 Z 序顶 → 临时压在球上面。
- 球的 top-keep 300ms 一次把球压回去。两个机制交替胜出 → 球在 700ms 窗口内闪 2–3 次「在顶 ↔ 被侧栏挡」。
- 修一个机制即可：让侧栏 resize 不动 Z 序（`SWP_NOZORDER`）。

### 第四阶段：设计修法

| 决策点 | 选择 | 理由 |
|---|---|---|
| 在 panel_pool 侧修 | ✅ 选中 | 不碰 ball_layer（保活机制有价值）；不动 pywebview（第三方库） |
| 自己调 SetWindowPos | ✅ 选中 | pywebview `resize()` 的 bug 在 winforms.py 里，无法靠设属性绕过 |
| 用 `_resize_safe` helper | ✅ 选中 | 5 处调用点集中替换，未来 pywebview 修了再切回 `resize()` 容易 |
| 私有 user32 实例 | ✅ 选中 | 沿用 ball_layer v0.170 的踩坑结论：共享 `ctypes.windll.user32` 设 argtypes 会污染 pywebview 的 move() |
| 私有实例用私有 argtypes | ✅ 选中 | 同上 |
| 句柄 `ToInt64()` | ✅ 选中 | 沿用 ball_layer 教训：pythonnet 3.x IntPtr 不能直接 `int()` |
| logical→physical 乘 `native._scale` | ✅ 选中 | 与 pywebview 的 `Form.resize` 一致口径 |
| 位置读 `native.Left/Top`（不动位置） | ✅ 选中 | fix_point=NORTH|WEST 与 pywebview 默认一致 |
| flag = SWP_NOMOVE\|SWP_NOZORDER\|SWP_NOACTIVATE = 0x16 | ✅ 选中 | 不搬位置、不改 Z 序、不抢焦点，正合需求 |
| 失败回退 `w.resize()` | ✅ 选中 | 宁可回到闪烁也要尺寸正确（用户痛点是闪烁，但前提是侧栏能用） |

### 第五阶段：实施

```
#1 panel_pool.py: 新增 _resize_safe()             (无依赖)
#2 panel_pool.py: 替换 5 处 ww.resize() → helper   (依赖 #1)
```

---

## 4. 实现中遇到的问题

### 问题 1：winforms.py `resize` 的 Z 序副作用

pywebview 6.x 的 `Form.resize`（winforms.py:616）`SetWindowPos(handle, None, x, y, w, h, 64)`：

- `hWndInsertAfter = None` → `HWND_TOP`
- flag `64` 只有 `SWP_SHOWWINDOW`，**没有 `SWP_NOZORDER`**

→ 每次 resize 都被当作「抬到 Z 序顶 + 显示」的隐式效果。pywebview 的 `move()` 反倒有 `SWP_NOZORDER`（line 642），不是 move 的问题。

**解法**：完全绕开 pywebview 的 `resize()`，自己调 `SetWindowPos(SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE)`，三合一 flag = `0x16`。

### 问题 2：共享 user32 实例会毒害 pywebview

ball_layer v0.170 踩过这个坑：`ctypes.windll.user32` 是进程级共享对象；在它上面设 `SetWindowPos.argtypes`（cx/cy 声明 `c_int`）会立刻让 pywebview 的 `move()` 抛 `ArgumentError`（pywebview 对 cx/cy 传 `None`，依赖 ctypes 未设 argtypes 时的宽松转换）→ 侧栏所有 move 失败、卡固定位置；主窗也拖不动。

**解法**：用模块级私有 `ctypes.WinDLL("user32", use_last_error=True)` 实例（每个 `_resize_safe` 调用重新 `WinDLL` 一次其实也可以，但 `_load_user32` 风格的模块级单例更省 + 与 ball_layer 对齐）。在本模块里设的 argtypes 只作用于本次 `SetWindowPos` 调用。

### 问题 3：句柄类型

pythonnet 3.x 里 `int(panel_native.Handle)` 会抛 TypeError（`System.IntPtr` 不支持 `int()` 直转）。

**解法**：`h.ToInt64()`（与 `ball_layer._handle_int` 同一惯例）。fallback `int(h)` 兜底，理论不会被触发。

### 问题 4：辅助 UI 线程访问

worker 线程（`_enqueue_op` 派发）调 `_resize_safe`，内部读 `native._scale`、`native.Left/Top`、`native.Handle`。pywebview 的 `BrowserView.instances` 是 UI 线程的对象，跨线程读这些属性会不会出错？

**评估**：原 `pywebview.Form.resize`（winforms.py:600）本身就被设计成 worker 线程调（它也只是读 `_scale`、`self.Location`、`self.Width`），实测在 worker 线程跑得很稳。WinForms 属性读在非 UI 线程下要么返回缓存值、要么跨线程 marshal，不会爆（比 `Invoke` 阻塞友好）。我的实现读这些比 `pywebview.Form.resize` 还少（不读 `self.Width`，因为我们手里有 `to_w`），更安全。

**未做特殊保护**：若真有跨线程读取异常，外层 `except Exception` 兜底 + `w.resize()` 回退，闪烁问题会回来但侧栏尺寸正确。

### 问题 5：失败回退的选择

若私有 `user32` 调 `SetWindowPos` 失败（罕见，但 GDI/句柄异常时可能），是回退 `w.resize()` 还是直接 raise ？

**解法**：回退 `w.resize()`。闪烁是次要问题，侧栏尺寸不能出错（用户场景是「侧栏在用」，尺寸错位会挡主窗）。`raise` 会让 worker 线程抛异常，可能影响其它 op（worker 是单线程串行消费队列）。

### 问题 6：UI 测试 / 视觉验证

侧栏 Z 序闪烁是肉眼可见的视觉问题，没有自动化测试能可靠验证（需要真窗口 + 真鼠标 + 录屏对比）。本任务只做「不闪烁」的负面验证 —— 用户手动触发延展/收窄即可，肉眼看不到闪烁就算修好。

**评估**：可接受。这是 UI 行为类修改的标准验收路径（PROGRESS.md 里 v0.165 球闪烁、v0.170 Z 序回归都是用户手动报 + 修，没自动化）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 winforms.resize Z 序副作用 | 新 `_resize_safe` 自己 SetWindowPos(SWP_NOZORDER) | panel_pool.py:1109 |
| #2 共享 user32 毒害 | 私有 WinDLL 实例 + 模块级缓存 | panel_pool.py:1140 |
| #3 IntPtr 不能 int() | `ToInt64()` + `int(h)` 兜底 | panel_pool.py:1150 |
| #4 跨线程读 UI 线程属性 | 不加保护，靠 `except` + 回退兜底（pywebview 自己也这么干） | panel_pool.py:1133 |
| #5 失败处理 | 回退 `w.resize()`，不 raise | panel_pool.py:1161 |
| #6 无自动化验证 | 走肉眼验收（与 v0.165/v0.170 同口径） | — |

---

## 6. 是否完全遵循规划路径开发

**完全按规划**。

- 第一阶段定位闪烁来源：先读 memory（`feedback_live_panel_zhujie.md`）锁定术语，再读 panel_pool.py 找动画入口 → 按规划走。
- 第二阶段验证候选：读 winforms.py `resize` vs `move` 的 flag 差异 → 准确锁定肇事者。
- 第三阶段确定机制：动画 32 帧 × 22ms ≈ 700ms vs top-keep 300ms 的频率 → 双机制交替胜出的假说与用户报的「反复闪烁几次」吻合。
- 第四阶段设计修法：表格 8 个决策都按规划选 → 私有 user32 + 私有 argtypes + `SWP_NOZORDER` + fallback `w.resize()` 全部落地。
- 第五阶段实施：依赖顺序正确（先 helper 后调用点替换）。
- 无偏离、无重大调整。

### 修订：与规划一致 → 不修改文档本体，按计划交付。

---

## 7. 最终实现点

### 后端（Python）

1. **`panel_pool.py`**：新增 `_resize_safe(w, to_w, to_h)` helper
   - 私有 `ctypes.WinDLL("user32", use_last_error=True)` 实例 + 显式 argtypes（避开共享 user32 污染 pywebview 的坑）
   - `native._scale` 做 logical → physical 换算
   - 句柄 `h.ToInt64()`（pythonnet 3.x IntPtr 不能直接 `int()`）
   - 位置读 `native.Left/Top`（fix_point=NORTH|WEST）
   - flag = `SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE = 0x16`
   - 任何失败回退 `w.resize()`（尺寸优先于闪烁）

2. **`panel_pool.py`**：5 处调用点改走 `_resize_safe`
   - `_anim_resize` 内 3 处（动画帧 1、动画帧 i、收尾精调）
   - `_apply_geometry` 内 2 处（球模式高度变化、磁吸模式仅主窗高度变化）
   - `move()` 不动（本身已 z-order-safe，`SWP_NOZORDER`）

3. **`panel_pool.py` 顶部文档**：`__all_hidden` / 隐藏态守卫注释不动（v0.166 已修过 SWP_SHOWWINDOW 重显侧栏的 bug，本任务无关）

### 前端

无改动。问题在原生窗口层级，纯后端。

### 资源文件版本

无改动（无 JS/CSS cache 变动）。

### 行为验收清单（手动测试项）

- [ ] 侧栏从 1 列延展到 2 列（并发请求到达）→ 球全程稳定悬在侧栏上方
- [ ] 侧栏从 2 列收缩到 1 列（最后一个 rid 释放）→ 球全程稳定悬在侧栏上方
- [ ] 延展期间点击球 → 球状态切换正常（S1/S2），侧栏延展不受影响
- [ ] 延展期间拖动主窗 → 侧栏跟随主窗（moved 事件走的是 pywebview move()，未变）
- [ ] 拖动球 → 侧栏跟随球移动，不影响宽度动画
- [ ] 「悬浮球置顶」开关关闭 → 侧栏降级到普通层级（不盖其它窗口），延展时仍不闪烁
- [ ] 仅侧栏高度变化（不延展）→ 不再升 Z 序（之前每次都抬到顶）
- [ ] 关掉悬浮球 → 走磁吸模式 → 延展时侧栏贴主窗右缘，宽度变化不影响 Z 序（v0.170 后磁吸模式本身已与球层级解耦）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | 改（+约 55 行：1 个 helper + 5 处调用替换 + 注释） |

### 重要参考

- ⚠ **pywebview `winforms.resize` 的 Z 序副作用**（核心约束）：`SetWindowPos(handle, None, x, y, w, h, SWP_SHOWWINDOW=64)` 不带 `SWP_NOZORDER`，hWndInsertAfter=NULL=**HWND_TOP**。任何走 `webview.Window.resize()` 的代码都会被这个行为顺带抬到 Z 序顶 —— 与本项目同样模式的代码都要小心。Move 没事（带 `SWP_NOZORDER`）。本项目侧栏专用 `_resize_safe` 已经把这条封掉；任何**新增**对侧栏/球的 resize 路径都必须走 `_resize_safe`，不能直接调 `w.resize()`。
- ⚠ **共享 `ctypes.windll.user32` 设 argtypes 会污染 pywebview**（v0.170 + 本次再次踩到的同一条坑）：必须用**私有** `ctypes.WinDLL("user32")` 实例。详见 ball_layer._load_user32 注释。