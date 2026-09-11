# pywebview moved/resized 事件签名崩溃修复（v0.114）开发文档

## Context

PyCharm 跑 `main.py` 启动 GUI，日志刷屏两行错误：

```
[pywebview] attach_main_moved_to_pool.<locals>.<lambda>() takes 1 positional argument but 2 were given
TypeError: ... takes 1 positional argument but 2 were given   （重复几十次）
watchdog replacing wedged window-op worker (progress 66s ago)  （重复几十次）
```

## 1. 用户的初始指令

> pycharm出现「……[pywebview] attach_main_moved_to_pool.<locals>.<lambda>() takes 1 positional argument but 2 were given …… watchdog replacing wedged window-op worker ……」，请你分析原因

> 修改

> 写入文档

## 2. 实现点提炼

| # | 实现点 | 来源 | 文件 |
|---|---|---|---|
| A | 定位 TypeError 抛出点：确认不是 pywebview 库内 bug，而是项目代码 | 初始指令 | src/relay/panel_pool.py |
| B | 查明 pywebview 6.2.1 事件分派签名规则（`Event.set` 用 inspect.signature 决定参数） | 排查 | webview/event.py |
| C | 查清 moved/resized 事件实际触发参数（WinForms 后端 set(x,y)/set(w,h) 各 2 参） | 排查 | webview/platforms/winforms.py |
| D | 修复 1 参 lambda → 吞参 | 修改 | src/relay/panel_pool.py |
| E | 盘点全项目 events 注册，确认只有此一处崩 | 排查 | gui.py / panel_pool.py |
| F | 用真实 pywebview Event 类复现验证：修前抛 / 修后不抛 | 验证 | （测试脚本） |
| G | 按 7 节规范写入会话文档 | 指令 | docs/dev/ |

### 隐含但需自行决策的点

- **为什么只有这一处崩**：全项目 events 注册盘点后，moved/resized 只有 3 处：
  - `panel_pool.py` 的 1 参 lambda（崩）
  - `gui.py` 的 `_dock_panel(*args)`（2 参兼容，安全）
  - `gui.py` 的 `_on_panel_moved(x, y)`（恰好固定 2 参，安全）
  loaded/closing/minimized/restored 都是 0 参触发，pywebview 走 `func()` 分支，不传参。
- **watchdog 风暴是次生症状**：每次移动窗口抛异常 → 事件 worker 无进展 → watchdog
  误判卡死 → 换血刷屏。修根因后风暴自动消失，不动 watchdog 逻辑。
- **pywebview 版本**：6.2.1。旧版 moved/resized 可能不传参，1 参 lambda 恰好成立；
  升级后签名变化导致参数暴漏。

## 3. 分析需求后得出的开发路径

1. 先 grep 确认 `attach_main_moved_to_pool` 在项目源码而非 pywebview 包内 → 排除库 bug。
2. 读 `webview/event.py` 的 `Event.set()`，掌握 6.2.1 的分派规则（0 参 / 带 window / 其余原样透传）。
3. 读 `webview/platforms/winforms.py`，确认 moved/resized 触发时带 2 个位置参数。
4. 修 `panel_pool.py` 的 lambda。
5. 全项目 grep `events.` 盘点所有注册，逐一核对签名。
6. 用真实 pywebview `Event` 类写最小复现脚本，验证修前抛 / 修后不抛 / 0 参回调安全。
7. `py_compile` 确认语法。

## 4. 实现中遇到的问题

- **问题 1**：`attach_main_moved_to_pool` 在 pywebview 包里 grep 不到，一度怀疑库 bug。
  **解决**：全盘 grep 后确认它在项目自己的 `src/relay/panel_pool.py:746`，是项目注册的
  1 参 lambda 与 pywebview 6.2.1 事件分派不匹配。
- **问题 2**：不能确认其它 events 注册是否也崩。
  **解决**：盘点全部 12 处注册，逐一对照签名；仅此一处。
- **问题 3**：验证脚本需要真实 pywebview 分派逻辑。
  **解决**：直接用 `webview.event.Event` 类 + `set(100, 200)` 复现，得到与用户日志
  完全一致的 TypeError；改为 `*args` 后通过。

## 5. 最后如何解决

- **根因**：`Event.set()`（`webview/event.py:36-45`）用 `inspect.signature` 分派：
  回调既非 0 参也非带 `window` 参数 → `func(*args)` 原样透传。WinForms 后端
  （`winforms.py:437/444`）触发 `moved.set(x, y)` / `resized.set(w, h)` 各 2 参，
  1 参 lambda 收到 2 参 → TypeError，每次移动/缩放窗口都抛。
- **修复**：lambda 改吞参：
  ```python
  app.window.events.moved += lambda *args, **kwargs: pool.refresh_geometry()
  app.window.events.resized += lambda *args, **kwargs: pool.refresh_geometry()
  ```
- **验证**：真实 pywebview `Event` 类复现 —— 旧写法抛同款 TypeError、回调不执行；
  新写法正常收到参数并执行；0 参回调走 `func()` 分支安全。`py_compile` 通过。

## 6. 是否完全遵循规划路径开发

**完全按规划**。排查顺序（源码定位 → 分派规则 → 触发参数 → 修复 → 全量盘点 →
真实复现验证）与第 3 节规划的 7 步一致，未偏离。

## 7. 最终实现点

- `src/relay/panel_pool.py:749-750`：moved/resized 的 1 参 lambda 改为
  `lambda *args, **kwargs`，消除 `takes 1 positional argument but 2 were given`。
- 全项目 events 注册盘点确认：仅此一处与 pywebview 6.2.1 分派规则不兼容，
  gui.py 其余 11 处注册（`_dock_panel(*args)` / `_on_panel_moved(x,y)` /
  0 参 loaded/closing/minimized/restored）均安全。
- watchdog 换血风暴（次生症状）随根因修复自然消失，未改 watchdog 逻辑。
- 修复不触发 8088 中继重启；生效需用户自行点 GUI 重启。
