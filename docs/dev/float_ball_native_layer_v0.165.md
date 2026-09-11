# 悬浮球原生 GDI+ 分层窗（v0.165）开发文档

## 1. 用户的初始指令

> 新增「悬浮球」功能:设置页开关开启后,桌面出现一个可拖动的球。球是侧栏的锚点 —— 侧栏从球的左上角展开。球有两态:
> - **S1(状态1)**:绿球,正常模式,侧栏按请求流显隐
> - **S2(状态2)**:红球,隐藏模式,侧栏折叠,球显示"有流"
>
> (演进)点球行为:**点击球始终切换 S1↔S2**(无论是否有流),不是只在有流时切。修正"无流时点击无效"的错误说法。
>
> (事故现场)点击球颜色没变;修复过程中又发现 GUI 无日志可查(无 --diag 时 ball 日志全丢)。

### 场景拆解

- **需求 1**:悬浮球 = 桌面锚点,侧栏从球位置展开;可拖动,拖完持久化位置。
- **需求 2**:两态可视化。S1 绿(正常)、S2 红(隐藏模式 + 有流徽标),差异明显。
- **需求 3**:点击球恒切换 S1↔S2。S1→S2 进隐藏模式;S2→S1 回正常,若仍有流立即弹侧栏。
- **历史背景(为什么是原生)**:v0.165 前用 **WebView2 窗口**做球,透明两次实测失败(见 §4 问题 1/2)。用户拍板改用**原生 GDI+ 分层窗**,绕开 WebView 合成表面。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 原生 GDI+ 分层窗替代 WebView2(透明真·圆形) | 背景 |
| B | 球 = 侧栏锚点:侧栏从球位置展开;拖球→侧栏跟随;拖侧栏→球跟随 | 需求 1 |
| C | 球可拖动,位置持久化到 settings | 需求 1 |
| D | 两态可视化:S1 绿 / S2 红(差异明显) | 需求 2 |
| E | 点击球恒切换 S1↔S2(无论是否有流) | 需求 3 |
| F | S1→S2 顺带收起展开的侧栏;S2→S1 若有流立即弹侧栏 | 需求 3 |
| G | 始终开启 ON 时球隐藏(常驻侧栏用不上锚点),侧栏仍从球记忆位置展开 | 隐性 |
| H | 任何原生失败静默降级为「无球」,绝不阻塞侧栏主流程 | 隐性 |

### 隐含但需要确认的点(用户没说,要追问)

- 透明方案:WebView2 已两次失败 → 直接原生,无需再问(用户已拍板)。
- 点击语义:用户先误说"只在有流时切",后主动修正为"恒切"。以修正为准。
- 拖动与点击的区分:拖动结束不触发点击(≥阈值算拖,<阈值算点)。
- S2 期间新请求:侧栏不弹,但球动画持续(S2 有流徽标)。

---

## 3. 分析需求后得出的开发路径

### 核心方案:原生 WinForms 分层窗 + GDI+ 自绘(自包含模块 `ball_layer.py`)

```
BallLayer(thread-safe API)                WinForms/GDI+ 全在 pywebview STA UI 线程
  start/destroy/show/hide/move_to/set_state ──BeginInvoke──► _create_and_show/_present/_draw_ball
```

- **窗口**:WinForms `Form`(Borderless、`ShowInTaskbar=False`、`TopMost=True`)。
- **透明**:`WS_EX_LAYERED` + `UpdateLayeredWindow` 逐像素 alpha(32bpp premultiplied DIB)。球外像素 alpha=0 直接透桌面,真·圆形。
- **绘制**:GDI+ 画 `Bitmap(Format32bppPArgb)` → `LockBits` → 逐行 `memmove` 进 `DIBSection` → `UpdateLayeredWindow`。
- **线程**:全部窗体/GDI+ 对象活在 pywebview 单一 STA UI 线程;跨线程调用一律 `BeginInvoke` 异步派发。

### 开发路径(本次会话,按依赖顺序)

```
#1 config.py:  3 个 settings 字段(开关 + 位置 x/y)              (底层)
#2 ball_layer.py: 原生分层窗 + GDI+ 绘制 + 三态渲染             (核心)
#3 ball_layer.py: 输入方案 —— 全局轮询状态机(参考 AHK Floatyball)  (依赖 #2)
#4 panel_pool.py: WebView2 球 → BallLayer 换内壳,保签名           (依赖 #2,#3)
#5 panel_pool.py: 修「异步 start() 竞态 → 引用被丢」              (依赖 #4, 事故)
#6 gui.py: 桥方法 + ball_mode guard + snapshot + _ensure_ball   (依赖 #4,#5)
#7 app.js: 设置页开关 + getFloatBall/setFloatBall 桥 + i18n     (依赖 #6)
#8 ball_layer.py: _draw_ball 玻璃球样式重写                      (依赖 #2)
```

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 透明实现 | `UpdateLayeredWindow` + 32bpp premultiplied DIB | 逐像素 alpha,唯一能透桌面的途径(键控/WebView 均已失败) |
| ⚠ 输入方案 | **全局轮询状态机**(GetAsyncKeyState + WindowFromPoint),30ms timer | `WS_EX_NOACTIVATE` + 分层窗在本机收不到 WM 鼠标消息(实测"点击没变化");AHK Floatyball 用同款轮询,验证可靠 |
| ⚠ 跨线程 | `BeginInvoke` 异步派发,绝不 `Invoke()` 阻塞 | 历史 RecreateHandle 跨线程 SendMessage 无人泵 → GUI 假死(py-spy 实测) |
| 点击/拖动回调 | 新起 Python `threading.Thread` 跑 | UI 线程永不碰 `pool._lock`,防双向死锁 |
| ⚠ 坐标口径 | pool↔ball 全程 **logical 像素**,仅 `UpdateLayeredWindow` 边界乘 `scale` | 与侧栏几何、`_screen_size` 同口径;DPI 因子复用 `main_native._scale` 单一来源 |
| ⚠ 异步建窗竞态 | `start()` fire-and-forget;pool **保留引用**,不同步查 `available` | `available` 在 BeginInvoke 返回时恒 False,同步检查必然误删引用(§4 问题 5) |
| GDI 句柄 | 所有 handle-returning ctypes 显式 `restype` | 64 位下默认 c_int 截断句柄 → CreateDIBSection 判空失败 |
| pythonnet IntPtr | `_handle_int(h)` = `h.ToInt64()` | pythonnet 3.x `int(f.Handle)` 抛 TypeError |
| 降级 | 任何异常置 `available=False` → 球静默无 | 宁可无球,不要错球(绝不回退 Solid/方窗/TransparencyKey) |
| 诊断 | ball_layer/panel_pool 自挂文件 handler → `.relay-logs/ball-debug.log` | GUI 无 --diag 时 root 无 handler,INFO 全丢(§4 问题 4) |

---

## 4. 实现中遇到的问题

### 问题 1:WebView2 `LWA_COLORKEY` → 黑底

`WS_EX_LAYERED` + `SetLayeredWindowAttributes(LWA_COLORKEY=#010203)` → 绿球被**黑色圆角矩形**包围。WebView2 的 D3D 硬件合成表面不参与颜色键控,key 色渲染成不透明近黑。

**解法**:放弃键控。走 `UpdateLayeredWindow` 逐像素 alpha。

### 问题 2:WebView2 `TransparencyKey` → 白底椭圆

pywebview `transparent=True` + 窗体 `BackColor`/`TransparencyKey` → **白底椭圆**。`TransparencyKey` 同样键控不了 WebView 子 HWND 的合成表面。

**解法**:彻底弃用 WebView2 做球,改原生 GDI+ 分层窗。`float_ball.html` 不再引用(文件保留不删)。

### 问题 3:点击球没反应 —— 分层窗收不到 WM 鼠标消息

原生层起步后,点击用 `Form.MouseDown/Move/Up`(即 WM 鼠标消息),但 `WS_EX_NOACTIVATE` + 逐像素 alpha 分层窗**收不到** WM_LBUTTONDOWN 之类 → "点击没变化"。同时计划里推荐的 `SetWindowRgn`(椭圆 hit-test region)+ layered 组合在本机也冲突,鼠标事件到不了窗体。

**解法(参考 AHK Floatyball)**:彻底不依赖窗口收鼠标消息。常驻 30ms timer 全局轮询:
- `GetAsyncKeyState(VK_LBUTTON)` 读左键物理态(= AHK `GetKeyState("LButton","P")`)
- `GetCursorPos` + `WindowFromPoint == ball hwnd` 判光标是否在球上(= AHK `MouseIsHwnd(hBall)`)
- 状态机 `IDLE → PRESSED → DRAGGING`:按下且在球内→记起点;松开未移动→点击;移动≥4px→拖拽;拖拽中→跟随光标;拖完→持久化。

### 问题 4:诊断日志不可见

GUI 进程默认(无 `--diag`)不配 root handler,`relay.ball_layer`/`panel_pool` 的 INFO 全被丢弃 → 修 bug 无据可查,只能盲猜。

**解法**:两个模块**自挂文件 handler**,写 `.relay-logs/ball-debug.log`,幂等,不依赖 `basicConfig`/`--diag`。后续所有球行为可查。

### 问题 5:球显示但点击无效 —— 异步 start() 竞态(决定性 bug)

日志实锤:
```
03:06:05,459 ball unavailable (create_and_show failed)   ← 竞态误判
03:06:05,474 ensure_gdi ok size=56x56 ...                ← 其实建成功了
03:06:05,477 present ulw ok=True                         ← 球正常显示
03:06:09,574 ball_clicked: ball=False ...                ← pool 引用没了
```

**根因**:`panel_pool._create_ball` 里 `self._ball.start()` 是 **fire-and-forget 异步派发**(BeginInvoke),返回时 `_create_and_show` 还没跑,`available` 恒为 False → 同步检查 `if not self._ball.available: self._ball = None` 把引用扔了。球照常异步建起来显示,但 pool 永远失去引用 → 点击/状态推送全部 no-op(即使轮询检测到了点击)。

**解法**:删掉同步检查,pool 保留引用。真正失败由 `_create_and_show` 内部置 `available=False` 兜底(无害 no-op)。`available` 只剩 `_present` 内部防护使用。

### 问题 6:样式脏 —— 底部阴影糊掉高光

旧版 `_draw_ball` 画完径向渐变后**整球再盖一层 30alpha 黑** → 把高光糊脏,球显得灰平。

**解法**:改 `SetClip(path)` 只压**底部 45%** 的暗弧,上左高光完整保留;补内缘描边 + 上左白色 glossy 高光。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1/#2 WebView2 透明失败 | 弃用 WebView2,原生 `UpdateLayeredWindow` 逐像素 alpha | ball_layer.py |
| #3 点击没反应 | 全局轮询状态机(GetAsyncKeyState + WindowFromPoint),不依赖 WM 消息 | ball_layer.py |
| #4 日志不可见 | 模块自挂文件 handler → `.relay-logs/ball-debug.log` | ball_layer.py / panel_pool.py |
| #5 异步 start() 竞态 | 删同步 `available` 检查,pool 保留引用 | panel_pool.py |
| #6 样式脏 | `_draw_ball` 重写:底部暗弧(非整球黑)+ 高光 + 描边 + flow 游动光斑 + s2 徽标 | ball_layer.py |
| 建球时机 | `_on_panel_loaded`(post-`webview.start()`)补建,`window.native` 就绪 | gui.py |

---

## 6. 是否完全遵循规划路径开发

**部分偏离**,集中在输入方案。

### 完全按规划(无偏离):

- 原生 GDI+ 分层窗替代 WebView2(plan: WebView2 在 layered 窗偶发黑屏是头号风险 → 命中,换原生)。
- `UpdateLayeredWindow` 逐像素 alpha + 32bpp premultiplied DIB + `BLENDFUNCTION(AC_SRC_OVER, AC_SRC_ALPHA)`。
- WS_EX 组合:`LAYERED | TOPMOST | TOOLWINDOW | NOACTIVATE`,清 `APPWINDOW`。
- 坐标口径 logical,仅 `UpdateLayeredWindow` 边界乘 scale。
- `BeginInvoke` 异步派发、绝不 `Invoke` 阻塞;回调新起线程跑。
- 三态可视化 idle/flow/s2(plan 的 GDI 重绘方案)。
- 降级:失败静默无球,绝不回退方窗/TransparencyKey。

### 偏离之处:

- **(a) 输入方案**:plan 推荐「`SetWindowRgn` 椭圆 region 做角落穿透,冲突则退化为 MouseDown 半径判定」。两者都依赖窗口收到 WM 鼠标消息,而本机分层窗收不到(实测)。**改用全局轮询状态机**(参考 AHK Floatyball,用户提供的工作参考),不依赖窗口消息。属方案级偏离,是"点击没变化"的正解。
- **(b) `_create_ball` 竞态**:plan 没提「`start()` 异步返回时不能同步查 available」。这是实现事故,修复后属防御性补充。
- **(c) 诊断日志自包含**:plan 假设 GUI 有 --diag 能看到日志,实际无。给球模块加自挂文件 handler,属排障基础设施补充。

### 重大调整:无。

---

## 7. 最终实现点

### `src/relay/config.py`(settings 字段)

1. **`relay_gui_float_ball: bool = False`**:悬浮球总开关。.env 键 `RELAY_GUI_FLOAT_BALL`。
2. **`relay_gui_float_ball_x: int = -32000` / `relay_gui_float_ball_y: int = -32000`**:球最后位置,**逻辑像素**(与侧栏几何同口径)。-32000 表示从未定过(未显示/未拖过)。.env 键 `RELAY_GUI_FLOAT_BALL_X/_Y`。
   - ⚠ 坐标口径:config 注释原写「物理像素」是错的,已改「逻辑像素」—— pool↔ball 全程 logical,仅 `UpdateLayeredWindow` 边界乘 scale。

### `src/relay/ball_layer.py`(**新文件**,自包含核心)

3. **BallLayer 类,线程安全公开 API**(任意线程可调):
   - `start(x, y)` / `destroy()` / `show()` / `hide()` / `move_to(x, y)` / `set_state(mode)`
   - `available` 属性、`position` 属性
4. **窗口**:WinForms `Form`(Borderless/Manual/`ShowInTaskbar=False`/`TopMost=True`) + WS_EX 样式(LAYERED/TOPMOST/TOOLWINDOW/NOACTIVATE,清 APPWINDOW)。先建 Handle 再设样式(无 RecreateHandle)。
5. **GDI+ 绘制管线**:
   - `_ensure_gdi`: `CreateDIBSection`(top-down 32bpp)+ `CreateCompatibleDC` + `SelectObject`,句柄缓存
   - `_redraw_into_dib`: `Bitmap(Format32bppPArgb)` → GDI+ 画球 → `LockBits` → 逐行 `memmove` 进 DIB
   - `_present`: 唯一调 `UpdateLayeredWindow` 处,redraw/reposition 二选一,`ptDst=(int(x*scale),int(y*scale))`
6. **三态渲染 `_draw_ball`(v0.165e 改为 Claude 小幽灵)**:
   - **视觉重设计**:从「玻璃球」改为 **Claude 像素风小幽灵**(terracotta 陶土橘主体 + 深褐竖条眼睛 + 波浪圆摆下摆,参考 Claude 官方吉祥物图)。
   - idle: 幽灵正常站立,静态(停 timer 省 CPU)
   - flow: 呼吸光晕 + 轻微上下浮动(bob,`sin(frame*0.22)`)
   - s2: 幽灵绕中心 180° 倒立(脚朝天)+ 脉冲外环 + 白底红心「有流」徽标
   - 实现要点:几何按 `min(w,h)` 归一化;`fill_rr` 拼 `GraphicsPath` 画圆角身体(无内置 FillRoundedRectangle);s2 用 `Translate+Rotate(180)+Translate` 再 `ResetTransform`;所有 brush/path 均 `Dispose()`。
   - ⚠ **踩坑**:`fill_rr(g, x, y, rw, rh, rad, brush)` 首个形参是 `g`,调用时漏传 `g` 会让首参错位(身体根本画不出来)——py_compile 查不出,只能实机/离屏渲染预览才能暴露。
7. **输入(全局轮询状态机)**:常驻 30ms timer;`GetAsyncKeyState(VK_LBUTTON)` + `GetCursorPos`/`WindowFromPoint`;`IDLE→PRESSED→DRAGGING`;点击/拖拽回调新起 Python 线程。
8. **诊断**:模块自挂文件 handler → `.relay-logs/ball-debug.log`(幂等)。

### `src/relay/panel_pool.py`(换内壳,保签名)

9. **构造**:`_float_ball_enabled`(从 settings 读)、`_ball: BallLayer | None`、`_ball_pos: (logical, None=未定)`、`_ball_s2: bool`、`_ball_pending: bool`(native 未就绪延迟建标记)。`_ball_pos` 从 `relay_gui_float_ball_x/y` 读回(>-1000 才采用)。
10. **`_create_ball()`**: `BallLayer(main_native=app.window.native, ball_size, scale_getter, theme_bg, on_click=ball_clicked, on_move_end=_persist_ball_pos)`;**保留引用,不同步查 available**(§4 问题 5)。
11. **`_ensure_ball()`**: `_on_panel_loaded`(post-start,native 就绪)补建被 deferred 的球。
12. **`ball_clicked()`**: 点击恒切换 S1↔S2。S2→S1: `set_state("idle")` + 有流立即弹侧栏;S1→S2: `set_state("s2")` + 侧栏展开且有流时收起。
13. **`_set_ball_state(mode)`** 替换 `_ball_push(js)`;**`_persist_ball_pos(x, y)`** 替换 `_on_ball_moved`(logical + clamp + settings 持久化 + relayout)。
14. **`set_float_ball(enabled)`**(设置页开关回调): 开 → 建球 + show(始终开启 ON 时隐藏)+ relayout;关 → 清 S2 + hide + destroy。
15. **`set_float_ball_pos_from_panel(x, y)`**(logical): 侧栏被拖走 → 球跟随,保持「球=锚点」不变式。
16. **`assign()`/`_clear_rid()`** 尊重 `_ball_s2`(S2 期间新请求不弹侧栏、球动画持续);`_clear_rid` 后空且 S1 → 回 idle。`_apply_geometry` 球分支读 `_ball_pos` 作锚点。

### `src/relay/gui.py`(桥 + guard + snapshot)

17. **桥方法**:
    - `get_float_ball()`: 读 `settings.relay_gui_float_ball` 返回 `{"enabled": bool}`。
    - `set_float_ball(enabled)`: 写 settings + 调 `pool.set_float_ball(enabled)`。
    - `ball_clicked()`: 转发到 `pool.ball_clicked()`(原 float_ball.html WebView 桥入口,保留兼容)。
18. **snapshot** 新增 `float_ball` 字段(app.js 设置页开关初始化用)。
19. **`float_ball_url`** 参数保留(签名兼容)但不再建 WebView2;`ballSetState`/WebView 桥管线消失,由 `set_state` 取代。
20. **ball_mode guard**: `_dock_panel` / `_on_main_minimized` / `_on_main_restored` 在 `pool.ball_mode` 时跳过 dock(侧栏由球锚点控制,不贴主窗右缘)。
21. **`_update_panel_snap`**: ball_mode 分支调 `pool.set_float_ball_pos_from_panel(x, y)`(拖侧栏 → 球跟随)。
22. **`_on_panel_loaded`**: `pool._ensure_ball()`(native 就绪的正确时机)。

### `src/relay/web/app.js`(设置页 UI)

23. **桥 wrapper**: `getFloatBall()` / `setFloatBall(enabled)`。
24. **设置页「实时栏管理」**新增 `悬浮球` 开关(`.prefs-live-panel-float-ball` checkbox,初始值读 snapshot `float_ball`);change 事件 → `setFloatBall` → 回读刷新勾选。
25. **4 个 i18n 字典**(en / zh-TW / ja / ko)补「悬浮球」标题 + 提示文案。

### `src/relay/web/float_ball.html`

26. **保留文件不删**,但运行时不再引用(WebView2 水波 CSS 版)。`ballSetState`/`window.pywebview.api.ballClicked` 桥整体消失,由 `BallLayer.set_state` / `_on_click` 取代。

### ⚠ 关键约束(底层,勿再踩)

- **坐标口径**:pool↔ball 全 **logical**,仅 `UpdateLayeredWindow` 边界乘 scale。
- **跨线程**:一切窗体操作走 `BeginInvoke`(异步),绝不 `Invoke()`/`WaitOne()` 阻塞。
- **锁纪律**:绝不在持有 `pool._lock` 时调 `_dispatch`;回调跑新线程,UI 线程永不碰 `pool._lock`。
- **输入不依赖 WM 消息**:分层窗收不到鼠标消息,必须全局轮询。
- **异步建窗竞态**:`start()` 返回时 `available` 恒 False,pool 必须保留引用。

### 行为验收清单(手动测试项)

- [ ] 开关 ON → 桌面真·圆球,球外区域透明可点(透桌面,非黑/白/灰块);on_top、无任务栏位、点球不抢主窗焦点、可拖动
- [ ] 开关 OFF → 球消失;重启球回上次位置
- [ ] 拖动球 → 侧栏从球位置展开(左上角对齐);拖侧栏 → 球跟随(锚点不变式)
- [ ] 点击球 → S1 绿 ↔ S2 红(无论是否有流);S2 期间新请求侧栏不弹但球动画持续;再点 → 回 S1 且侧栏立即弹出
- [ ] 始终开启 ON → 球隐藏,侧栏仍从球记忆位置展开
- [ ] 屏幕右下角放球 → 侧栏向左上展开不越界
- [ ] 一键隐藏按钮连带藏球
- [ ] 降级:临时改 `ball_layer` 抛错 → 球静默消失,侧栏不受影响
- [ ] `.relay-logs/ball-debug.log` 有完整行为链(create/ensure_gdi/present/press/click/set_state)

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改(+3 settings 字段 + 坐标口径注释修正「物理→逻辑」) |
| `src/relay/ball_layer.py` | **新增**(原生分层窗 + GDI+ 三态渲染 + 全局轮询输入) |
| `src/relay/panel_pool.py` | 改(WebView2 球 → BallLayer,`ball_clicked`/`_set_ball_state`/`_persist_ball_pos`/`set_float_ball`/`set_float_ball_pos_from_panel`,竞态修复) |
| `src/relay/gui.py` | 改(3 桥方法 + snapshot `float_ball` + ball_mode guard×3 + `_update_panel_snap` 分支 + `_ensure_ball`) |
| `src/relay/web/app.js` | 改(2 桥 wrapper + 设置页悬浮球开关 + 4 i18n) |
| `src/relay/web/float_ball.html` | 不改(文件保留,运行时不再引用;v0.165b WebView2 版) |
| `src/relay/web/index.html` | 改(JS cache `?v=20260825-01` → `?v=20260825-02`,CSS 沿用 v0.164 的 `-01`) |
