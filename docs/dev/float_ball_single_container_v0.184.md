# 悬浮球单容器重构 + S1↔S2 状态机（v0.183 → v0.184.3）开发文档

## 1. 用户的初始指令

会话内的多轮反馈（按时间顺序，原话摘录）：

1. v0.183（前置背景，本期前置动作）：
   > 「悬浮球的点击样式转变还是保留一下」
   > 「保留完整 S1<->S2 双态切换」

2. v0.184（核心合并）：
   > （隐含：把 v0.180 ghost_panel.html 单窗渲染层与 v0.183 Electron 主进程合并，对外暴露单容器窗口；同时把原本 S1↔S2 几何动作的语义改成「点击球帽收起/展开」）

3. v0.184.2（整球翻转）：
   > 「转换到 s2 后将悬浮球翻转 180 度」
   > 「整体 180 度翻转」

4. v0.184.3（S2 持久化，bug fix）：
   > 「在无实时流的状态下，处在 S2，悬浮球也不会变成翻转的样式，只有有流才会变。请修正它。」

5. 收尾：
   > 「非常好。将本会话的所有变动都写入开发文档吧」

前置背景：v0.180 已做出「球帽 + 侧栏合并渲染层 `ghost_panel.html`」的可行性 demo（同一键控窗口内球区透明 + 面板区实底共存，逐像素抓包验证），v0.183 把主进程从 GDI/PyWebView 全部迁到 Electron（`electron_app/ball_main.js` + `electron_ball_window.py`）。本次会话是 v0.183 的尾巴 + v0.184 系列三条 bug fix 链。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 单容器窗口 = `ElectronBallWindow` + `ghost_panel.html` + `ball_main.js` + `ball_preload.js` | 指令 2（v0.184） |
| B | 球模式：收起 = (ball_size, ball_size) 只露球帽（真透桌面 / hover 穿透）；展开 = (ball_size+侧栏宽, panel_height) 「球帽 + 侧栏」同窗（停 hover + 全窗口吞点击） | 指令 2 |
| C | 磁吸模式（悬浮球开关 OFF）：容器强制展开成侧栏、dock 贴主窗右缘、球帽隐藏、高度跟随主窗 | 指令 2 |
| D | 点击球帽（drag-end 无位移）→ 切收起/展开；拖动（收起态拖球帽 / 展开态拖顶栏）→ 移动容器 + 落盘 settings | 指令 2 |
| E | 球帽形态（idle/flow/s2）由请求流驱动，与收起/展开解耦 | 指令 2 |
| F | 还原 S1↔S2 双态语义：S1 = ghost-flow（启动侧边栏显示）/ S2 = ghost-done（始终隐藏侧边栏） | 指令 1（v0.184.1） |
| G | S1 + 有流 → flow + 展开；S1 + 无流 → idle + 收回；S2 + 有流 → done + 不展；S2 + 无流 → done + 不展 | 指令 1（v0.184.1） |
| H | 切到 S2 后整球帽（halo + body + ring + heart 一起）180° 翻转；S1↔S2 切换 400ms 平滑过渡 | 指令 3（v0.184.2） |
| I | S2 是用户显式切换的「偏好」，不应因流 done 就回 idle —— S2 必须持续翻转态，视觉提示保持 | 指令 4（v0.184.3） |

### 隐含但需要确认的点（用户已确认）

- 球模式 vs 磁吸模式切换：单容器内 `body.no-ball` class 控制球帽隐藏。✅
- 拖动源区分："cap"（球帽，无位移=切 S1↔S2）vs "panel"（顶栏，无位移=无操作）。✅
- `_show_sidebar_on_stream: bool` 默认 True（S1），作为 S1↔S2 偏好 flag 持久化在 pool 实例（不写盘，重启回到默认 S1）。✅
- 整球翻转基点 50% 50%（viewBox 中心）+ 父元素 transform 嵌套到子元素 keyframe transform 上（halo/ring/heart 各自走自己的 scale keyframe，仍在已翻转坐标系内）。✅
- S2 持久化范围：仅「球帽形态」持久（始终 done），不持久「_expanded 几何状态」（S2 下用户也不会主动点球帽切 S1）。✅

---

## 3. 分析需求后得出的开发路径

### 核心结论：合并 + 状态机的实现栈

v0.180 已证「同一键控窗口内球区透明 + 面板实底共存」可行；v0.183 已证「Electron 主进程能透明 + 反复 resize + 跨窗通信」。本期工作把两者组装成单容器，并把 v0.180 demo 里没接线的「球帽拖拽 / 顶栏拖拽 / 收起按钮 / 磁吸模式」全部接入实际生产流。

### 实现路径（按依赖顺序 5 个子任务）

```
#1  web/ghost_panel.html       (补：球帽拖拽接线 + 顶栏拖拽区 + 收起按钮 + ghostSetBallVisible)
#2  electron_app/ball_main.js  (补：set_expanded 真透穿透切换 + resizable:true 关键修复)
#3  electron_ball_window.py    (补：set_expanded 桥方法 + events 命名空间)
#4  panel_pool.py              (核心：单容器控制流 + S1↔S2 状态机)
#5  gui.py                     (适配：float_ball_url → ghost_panel.html + 容器事件 hook)
```

随后三轮 bug fix 在 #4 之上迭代：
- v0.184.1：补 `_show_sidebar_on_stream` + `_sync_geometry_to_mode` 单一来源真理
- v0.184.2：CSS 整球帽 `transform: rotate(180deg)` 替换 body 单层旋转
- v0.184.3：`_sync_geometry_to_mode` 拆 S1/S2 分支，S2 强制 done 不论流

技术关键决策：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 单容器承载方 | `ElectronBallWindow`（复用 v0.183）+ `ghost_panel.html`（复用 v0.180）+ `ball_main.js`（复用 v0.183） | 三方都已存在；合并只需接线 |
| 球 vs 磁吸模式 | `body.no-ball` class + `ghostSetBallVisible` 桥 + `_float_ball_enabled` 状态 | CSS 控制渲染，Python 控制几何，两端解耦 |
| 收起/展开触发 | 球帽 mousedown 球内圆命中 → drag-start；mouseup → drag-end 无位移 → TCP `clicked` → `pool.ball_clicked` | 复用 v0.183 drag 状态机，零新增协议 |
| 球帽形态驱动 | 由 `_sync_geometry_to_mode()` 单一来源真理下推（S1/S2 flag + rid 状态共同决定） | 消除 v0.184「点击 vs 来流」两路分别推形态的不一致 |
| S1↔S2 flag 持久化 | 仅进程内 `self._show_sidebar_on_stream: bool`，不写盘 | flag 是「这次会话的偏好」，重启回到默认 S1 反而符合预期 |
| 整球翻转基点 | `transform-origin: 50% 50%`（SVG viewBox 中心） + 父元素 transform + 子元素 keyframe transform 嵌套 | 子元素 keyframe 仍在已翻转坐标系内继续走 scale，视觉正常 |
| S2 持久化范围 | 球帽形态 = 始终 done；不涉及几何（容器仍只在 assign 时展开，S2 不主动展） | 保持 v0.184.1 的「S2 + rid = 不展开」契约，只修正「无 rid 时回 idle」视觉丢失 |

---

## 4. 实现中遇到的问题

### 问题 1：transparent Electron 窗口 `resizable: false` 二次 `setSize` 静默失败

容器需要反复「(56,56) 收起 ↔ (456,900) 展开」。验证冒烟：首次 `setSize(456,900)` 生效，后续 `setSize(56,56)` 无报错但 `getSize()` 仍 456x900。

**根因**：`resizable: false` 在 Windows + transparent 下，首个大幅 resize 生效后系统层把窗口锁死成「不可 resize 状态」，后续 `setSize` 调用直接被忽略。

**解法**：`ball_main.js` `new BrowserWindow({ resizable: true })`（从 `false` 改成 `true`）。代价是用户能拖动窗边缘，但容器仅 ball_size 方 / 全屏展开两种形态，边缘拖动不产生有意义的几何变化，可接受。

### 问题 2：`ghostSetState('done')` 落到 `ghost-idle` class

还原 S1↔S2 后调用 `ghostSetState('done')` 期望翻转到 `ghost-done` class，实际仍 idle。逐字查 `ghost_panel.html` 顶部 `GP_CLS`：

```js
const GP_CLS = { idle: "ghost-idle", flow: "ghost-flow", s2: "ghost-done", done: "ghost-done" };
```

等一下 —— 'done' 已经映射到 'ghost-done'。**这不是本期问题，是误记。** 实际问题是 v0.184 把映射写成 `{idle:"ghost-idle", flow:"ghost-flow", s2:"ghost-done"}`，缺 'done' 键；v0.184.1 还原时补上 'done'。**根因**：v0.184 改 GPU 桥时漏写 done 键；调用方写 `'done'`（按 Python `_set_ball_state` 字面量）→ `GP_CLS.done === undefined` → 兜底成 `'ghost-idle'`。

**解法**：`GP_CLS` 加 `'done': 'ghost-done'` 键。

### 问题 3：v0.184 `ball_clicked` 改为「收起/展开」后，S1↔S2 双态语义丢失

v0.183 主线里 `ball_clicked` 是 S1↔S2 切换（S1 = ghost-flow = 启动侧栏 / S2 = ghost-done = 始终隐藏）；v0.184 把它简化成「收起 ↔ 展开」几何动作。用户反馈：「点击样式转变还是保留一下」「保留完整 S1↔S2 双态切换」。

**根因**：v0.184 抽象成「单一 toggle 几何」时，丢弃了「切换流处理偏好」的语义层。

**解法**：v0.184.1 补 `_show_sidebar_on_stream: bool = True`（S1 默认）+ `_sync_geometry_to_mode()` 单一来源真理方法。`ball_clicked` 重写为：

```python
def ball_clicked(self, *args) -> None:
    with self._lock:
        if not self._float_ball_enabled: return
        if self.always_one_window is None or not self.always_one_visible: return
        if self._all_hidden: return
        self._show_sidebar_on_stream = not self._show_sidebar_on_stream
        new_state = "flow" if self._show_sidebar_on_stream else "done"
        self._set_ball_state(new_state)
        self._sync_geometry_to_mode()
```

几何动作从 `_sync_geometry_to_mode` 内部统一计算（S1+流→展，S2+流→不展）。

### 问题 4：S2 翻转只翻 `#ghost-body`，halo/ring/heart 不翻

v0.184.1 还原 S2 后，CSS 是 `#ghost-body { transform: rotate(180deg) }`。视觉上小幽灵倒立了，但外圈 halo、定位 ring、心跳 heart 仍正向 → 「半翻不翻」别扭。

**根因**：v0.180 的 S2 ghost-done CSS 时代较早（当时 S2 没被还原），只旋转主体，未考虑整球视觉一致。

**解法**：v0.184.2 把 transform 上移到父元素 `#cap-ball.ghost-done { transform: rotate(180deg); }`：

```css
#cap-ball {
  transform-origin: 50% 50%;
  transition: transform 0.4s cubic-bezier(.4, .0, .2, 1);
}
#cap-ball.ghost-done { transform: rotate(180deg); }
```

父元素 transform 会嵌套到子元素 keyframe transform 上（halo/ring/heart 的 `scale(...)` 仍在已翻转坐标系内继续走），观感正常。400ms cubic-bezier 过渡让翻转过程平滑。

### 问题 5：S2 切回无流时球帽回 idle（v0.184.3 关键 bug）

用户反馈：「在无实时流的状态下，处在 S2，悬浮球也不会变成翻转的样式，只有有流才会变。」

**根因**：v0.184.2 的 `_sync_geometry_to_mode` 把「无 rid」统一兜底成 `idle`，没区分 S1/S2 flag：

```python
# 错误版本
if not has_stream:
    target_expanded = False; ball_state = "idle"   # S2 也被兜底成 idle
```

S2 是用户显式切换的「偏好」（原话：「始终隐藏侧边栏」），不应因 rid done 就回 idle 失去视觉提示。否则用户切到 S2 后等几秒就忘了自己在 S2。

**解法**：v0.184.3 拆 S1/S2 分支独立判定：

```python
if self._show_sidebar_on_stream:
    if has_stream:
        target_expanded = True; ball_state = "flow"
    else:
        target_expanded = False; ball_state = "idle"
else:
    target_expanded = False; ball_state = "done"   # S2 无脑 done，不论流
```

顺手修 `_all_hidden=True` 分支 `target_expanded` 未初始化导致的 `UnboundLocalError`（初始化成 `False`、`ball_state = None`，整藏时不主动下发 `ghostSetState`，省一次 IPC）。

### 问题 6：磁吸 dock 球模式下 move 事件回环

容器被拖（`moved` 事件）→ Python `_ball_drag` 落盘 → `_update_panel_snap` 触发可能再 setPosition → ball_main.js `move` handler 又回播 `moved` 事件？验证：ball_main.js `moved` 只由用户拖动时 `tickDrag` 显式 emit，程序化 `setPosition`（初始离屏 / refresh_geometry）不回播。保持此约定即可。

### 问题 7：electron 进程孤儿残留

每轮冒烟都会起 `electron.exe` 容器。`pool.stop()` 没生效或窗口抛异常时残留 electron 进程 → 占着 `webview_api` / GPU 资源。

**解法**：发现残留时**只杀具体 PID**（先 `netstat -ano` 找 LISTEN 端口对应 PID，再 `taskkill //PID xxx //F`），**严禁** `taskkill //IM electron.exe //F`（按镜像名会误伤用户的其他 electron 应用）。**严禁**杀 8088 端口的 python.exe（用户在用 chat）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 transparent 二次 setSize 失效 | `resizable: true` | ball_main.js |
| #2 ghostSetState('done') → idle | `GP_CLS` 补 `'done': 'ghost-done'` | ghost_panel.html |
| #3 ball_clicked 丢 S1↔S2 语义 | 补 `_show_sidebar_on_stream` + `_sync_geometry_to_mode` 单一来源真理 | panel_pool.py |
| #4 S2 翻转不完整 | transform 上移到 `#cap-ball`，父元素嵌套到子 keyframe | ghost_panel.html |
| #5 S2 回 idle | `_sync_geometry_to_mode` 拆 S1/S2 分支独立判定 | panel_pool.py |
| #6 moved 事件回环 | ball_main.js 程序化 setPosition 不回播（已遵守） | ball_main.js |
| #7 electron 孤儿 | 仅杀 PID，先 netstat 确认；不按镜像名 | ops |
| 顺手：`_all_hidden` UnboundLocalError | 初始化 `target_expanded = False` + `ball_state: Optional[str] = None` | panel_pool.py |

---

## 6. 是否完全遵循规划路径开发

**基本完全按规划。** 5 个子任务 + 3 轮 bug fix 都按依赖顺序落地，未出现规划外的大改动。

### 完全按规划（v0.184 base）：

- 单容器承载方 = 复用 v0.180 ghost_panel.html + v0.183 ball_main.js + ball_preload.js。
- 球模式：收起 (ball_size, ball_size) / 展开 (ball_size+侧栏宽, panel_height)；磁吸：dock 贴右、球帽隐藏、随主窗。
- 球帽形态（idle/flow/s2）由请求流驱动，与收起/展开解耦。
- 删除独立球逻辑（BallLayer3 不再被池引用）；ball_layer2.py 死代码不删。

### 完全按用户反馈迭代：

- v0.184.1：还原 S1↔S2 双态语义 + `_sync_geometry_to_mode` 单一来源真理（修复规划偏差）。
- v0.184.2：整球帽 180° 翻转 + 400ms 平滑过渡（用户反馈）。
- v0.184.3：S2 持续翻转，bug 修复（用户反馈）。

### 偏离之处：

- **S1↔S2 flag 不写盘**：原 v0.183 实现也是仅进程内，重启回到 S1。本期保持约定。**实现细节补充，非方向性变更。**
- **S2 几何不持久化**：S2 下用户点球帽切 S1 才会改 flag；S2 期间球帽始终 done，容器根据 `_rids` 自然展开 / 收回。**符合 v0.184.1 契约，只修正视觉态持久性。**
- **`resizable: true` 是 v0.183 隐藏 bug 的修复**：原 v0.183 没测反复 resize 场景，v0.184 才暴露。**算 v0.183 漏验，本期补修。**

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/panel_pool.py`**（核心 — 5 轮迭代累积）：
   - v0.184：`start()` 改建容器窗口（`ElectronBallWindow` + ghost_panel.html），绑 `events.clicked→ball_clicked`、`moved→_ball_drag`、`dragend→_persist_ball_pos`；删 `_create_ball`/`_ensure_ball`/`_show_ball`/`_hide_ball` 的 BallLayer3 逻辑；`refresh_ball_visibility` 改容器显隐。
   - v0.184：`ball_clicked()` 简化为收起/展开切换；`_set_ball_state(mode)` 改 `evaluate_js("ghostSetState('..')")`；`_apply_geometry` 两分支（球模式 / 磁吸）；`_anim_resize` 升级宽高同变补间（ease-out x^4，32 步×22ms）。
   - v0.184：`_ball_drag` / `_persist_ball_pos` 容器被拖 → 更新位置 + 落盘 `settings.relay_gui_float_ball_x/y`；`set_float_ball(True)` → 球模式（`ghostSetBallVisible(true)`），`False` → 磁吸（`ghostSetBallVisible(false)` + dock）。
   - v0.184.1：新增 `_show_sidebar_on_stream: bool = True`（S1 默认）+ `_sync_geometry_to_mode()` 单一来源真理。`ball_clicked` 切 S1↔S2 flag + 同步球帽形态 + 调 `_sync_geometry_to_mode`。`assign`/`_clear_rid`/`set_float_ball`/`toggle_all_visible` 全部改走 `_sync_geometry_to_mode`，消除各处直推 `_expanded = ...` 与 `_set_ball_state` 的不一致。
   - v0.184.3：`_sync_geometry_to_mode` 拆 S1/S2 分支：
     - S1+有流 → `target_expanded=True`, `ball_state="flow"`
     - S1+无流 → `target_expanded=False`, `ball_state="idle"`
     - S2+有流 → `target_expanded=False`, `ball_state="done"`
     - S2+无流 → `target_expanded=False`, `ball_state="done"`（球帽持续倒立）
     - `_all_hidden=True` → `target_expanded=False`, `ball_state=None`（不下发 ghostSetState，省 IPC）

2. **`src/relay/electron_ball_window.py`**（v0.184）：
   - 新增 `set_expanded(bool)` 桥方法 → TCP `set_expanded`。
   - `events = SimpleNamespace(loaded, closing, moved, resized, dragend, clicked)`（`moved`/`dragend`/`clicked` 沿用 v0.183，扩展 3 个事件用于容器拖动 / 收起-展开触发）。

3. **`src/relay/gui.py`**（v0.184）：
   - L2926 `float_ball_url = (_WEB_DIR / "ghost_panel.html").as_uri()`（从 `ghost_ball.html` 改）。
   - `_on_panel_loaded`：evaluate_js 目标改为容器窗口（`setTheme` 等 live_panel.js 函数沿用，无函数名变更）；dock + `refresh_ball_visibility` 适配容器。
   - `_dock_panel`：主窗移动 → 磁吸态容器贴右；球模式不跟。
   - `_update_panel_snap` / `_on_panel_moved`：容器拖动磁吸判定（move 目标 = 容器窗口）。
   - `_ensure_ball`（已删）/ `refresh_ball_visibility` 调用点（`L3556/1845` 等）→ 容器显隐。

### 主进程（Electron）

4. **`src/relay/electron_app/ball_main.js`**（v0.184）：
   - 新增 `case "set_expanded"`：true → `stopHoverLoop()` + `setIgnore(false)`（展开/磁吸：全窗口吞点击）；false → `startHoverLoop()`（收起：球帽 hover 判定）。
   - **`resizable: true`**（关键修复）：transparent + `resizable:false` 的窗口二次 `setSize` 静默失败（首次大改后不再变），容器无法反复收展。
   - `drag-start` / `drag-end` 沿用 v0.183：drag-end 无位移 → TCP `clicked`（仅 cap 源）；有位移 → TCP `dragend`。
   - 拖动中 `emitEvent("moved", [x,y])` 用于 Python `_ball_drag` 跟手；程序化 `setPosition`（初始离屏 / refresh_geometry）不回播 moved（避免 `_pos` 被初始 -32000 覆盖）。

5. **`src/relay/electron_app/ball_preload.js`**（v0.184，沿用 v0.183）：
   - 暴露 `window.pywebview.api`（9 个侧栏方法 + `ballClicked`）+ `ballDrag` 桥 → 给 ghost_panel.html 用。

### 渲染层（HTML / CSS）

6. **`src/relay/web/ghost_panel.html`**（v0.184 + v0.184.1 + v0.184.2）：
   - v0.184：`#ghost-root`（flex row）= 左 `#ghost-cap`（球帽 56×56 透明 SVG）+ 右 `#ghost-panel-surface`（侧栏实底）；`body.collapsed` 收起（surface display:none）/ 展开。
   - v0.184：球帽拖拽接线：`#cap-ball` mousedown（球内圆命中 `dx²+dy² ≤ (50*0.48)²`，viewBox 100 口径）→ `window.ballDrag.start("cap")`；document mouseup/mouseleave → `end()`。
   - v0.184：展开态顶栏拖拽区：`#panel-drag-region`（absolute top:0 left:0 right:0 height:32px，cursor:grab），mousedown 排除 `#panel-btn-close` → `ballDrag.start("panel")`。
   - v0.184：收起按钮 `#panel-btn-close`（title="收起"）→ click → `window.pywebview.api.panel_close()`。
   - v0.184：`window.ghostSetBallVisible(on)` → 磁吸模式隐藏球帽（`body.no-ball` + `#ghost-cap { display:none }`，surface 占满）；球模式恢复。
   - v0.184.1：`GP_CLS` 补 `'done': 'ghost-done'` 键（v0.184 漏写，导致 'done' 兜底成 idle）。
   - v0.184.2：CSS 整球帽翻转：
     ```css
     #cap-ball {
       transform-origin: 50% 50%;
       transition: transform 0.4s cubic-bezier(.4, .0, .2, 1);
     }
     #cap-ball.ghost-done { transform: rotate(180deg); }
     ```
     父元素 transform 嵌套到子元素 keyframe transform 上（halo/ring/heart 各自 scale 仍在已翻转坐标系内继续走），观感正常。删除 v0.184.1 `#cap-ball.ghost-done #ghost-body { transform: rotate(180deg) }` 单层旋转。

### 废弃 / 删除

7. **`src/relay/ball_layer3.py`**：v0.184 起不再被池引用（文件保留，标注废弃）。

### 不改的东西

- `src/relay/web/ghost_ball.html`（v0.179 单球）保留不删（回滚参考）。
- `src/relay/web/live_panel.html` / `.css` / `.js` 不动（被 ghost_panel.html 复用）。
- 磁吸（dock）模式行为不变（贴右、随主窗移动/缩放、dock 时球帽隐藏）。
- `pyproject.toml` 无需改（`web/*.html` 通配自动含 `ghost_panel.html`）。

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | **核心改**：单容器控制 + S1↔S2 状态机（v0.184 + v0.184.1 + v0.184.3） |
| `src/relay/electron_ball_window.py` | 改：set_expanded 桥 + events 命名空间 |
| `src/relay/electron_app/ball_main.js` | 改：set_expanded + resizable:true 修复 |
| `src/relay/web/ghost_panel.html` | 改：拖拽/收起/磁吸接线 + GP_CLS 补 done 键 + 整球翻转 CSS |
| `src/relay/gui.py` | 改：float_ball_url + 容器事件 hook |
| `src/relay/ball_layer3.py` | **不再被引用**（文件保留） |

### 关键陷阱（务必注意）

1. **`resizable: true` 是 Electron transparent 窗口反复 resize 的硬性前提**。`resizable: false` 下首次大幅 setSize 生效后系统层锁死窗口，后续 setSize 静默失败。
2. **`GP_CLS` 必须含 'done' 键**。Python 侧 `_set_ball_state` 字面量是 `'done'`（不是 `'s2'`），v0.184 漏写兜底成 idle。
3. **父元素 transform 嵌套到子元素 keyframe transform 上**。整球 180° 翻转放在 `#cap-ball`（父），子元素 halo/ring/heart 仍可走各自 scale keyframe（在已翻转坐标系内继续），观感正常。
4. **`moved` 事件必须仅用户拖动 emit**。程序化 `setPosition`（初始离屏 / refresh_geometry）必须**不回播** `moved`，否则 `_pos` 被初始 -32000 覆盖，容器定位错乱。
5. **S2 视觉态必须持久**。`_sync_geometry_to_mode` S2 分支无脑 `ball_state="done"`，不论 `_rids` 是否为空。否则用户切 S2 后等几秒就忘了自己在 S2，失去「始终隐藏」的视觉提示。
6. **`_all_hidden=True` 时不下发 `ghostSetState`**。球帽已 hide，形态无意义；恢复时由 `refresh_ball_visibility` 决定。
7. **electron 进程只杀具体 PID，不按镜像名**。`taskkill //IM electron.exe //F` 会误伤用户其他 electron 应用。杀前先 `netstat -ano` 找 LISTEN 端口对应 PID。**严禁**杀 8088 端口的 python.exe（用户在用 chat）。

### 行为验收清单（手动测试项）

- [ ] GUI 重启后球显示为幽灵（56×56 真透桌面）；点球 → 向右下展开「球 + 侧栏」；再点球帽收起
- [ ] 收起态：球区透桌面（真透明），幽灵身体/眼睛清晰
- [ ] 展开态：右侧面板实底卡片无键控孔洞，球帽区仍透桌面
- [ ] 默认 S1：来流时球帽 flow + 容器自动展开；流 done 后收回 idle
- [ ] 点球帽切 S2：球帽 180° 整球翻转 + 心跳动画持续；新流不展开侧栏；球帽持续倒立（不因流 done 回 idle）
- [ ] 再点球帽切 S1：球帽 180° 转回 flow 形态 + 有流时容器展开
- [ ] 拖动：收起态拖球帽移动容器；展开态拖顶栏移动；位置落盘（重启保持）
- [ ] 磁吸模式（关球）：容器 dock 贴主窗右缘、球帽隐藏、随主窗移动/缩放
- [ ] 点击透传回归：球下方放记事本，点球 → 球响应且记事本不收到点击；展开态侧栏点击不穿透
- [ ] 退出 `pool.stop()` → 无残留 electron 进程

### 冒烟验证记录

- **v0.184 容器 smoke diag**（`ElectronBallWindow` + `ghost_panel.html` + `ball_main.js`）：17/17 通过 —— 透明（html/body/cap rgba(0,0,0,0)、surface 实底）、`ghostSetExpanded`/`ghostSetBallVisible` 切 class、反复 `resize(456,900)/(56,56)` 回读（resizable:true 修复验证）、`set_expanded` 吞/穿切换、合成球帽点击 → `clicked` 事件回 Python、`move`/`set_topmost`/`capture`。
- **v0.184.1 S1↔S2 状态机 smoke**（Mock pool）：14/14 通过 —— 默认 S1 / assign S1 展开+flow / 点球帽→S2 收回+done / 切回 S1 重展 / S2 来新 rid 不展开+done / 全清→idle。
- **v0.184.2 整球翻转验证**：容器 smoke 截图对比（idle / done）：done 态 body 中心 y 从 64.5 → 55.5，`cap-ball` computed `transform = matrix(-1, 0, 0, -1, 0, 0)`（180° 旋转矩阵）✓，halo/ring/heart 跟随翻转 ✓。
- **v0.184.3 S2 持久化 smoke**（Mock pool）：13/13 通过 —— 默认 S1+无流→idle / S1+流→flow+展开 / 点球帽→S2 / S2+rid→done+不展 / **S2+清流→done（持久翻转）** ← 关键 / S2+新流→done+不展 / S1+无流→idle / 一键全藏不下发 ghostSetState。
- `py_compile` / `ast.parse` panel_pool.py / gui.py / electron_ball_window.py / ghost_panel.html + `import` 全绿。