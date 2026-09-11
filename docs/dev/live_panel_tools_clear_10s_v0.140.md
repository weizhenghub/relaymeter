# 工具容器销毁时间收紧到 10s（v0.140）开发文档

## 1. 用户的初始指令

> tool容器销毁时间设置为10s

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 工具容器最后更新后自动清除默认 20s → 10s | 指令（直接给值） |
| B | 已存在的 .env / 用户手动改过的值不受影响（仅默认值变化） | 推论（向后兼容） |
| C | 前端 + 后端默认同步收紧，避免 snapshot 兜底 / JS 兜底还是旧 20 | 推论（一致性） |

### 隐含但需要确认的点（设计自决）

- **7 处默认值要同步**：`config.py` 字段声明、`gui.py` getter/setter 兜底、`gui.py` snapshot 兜底、前端 `_toolsClearSec` 初始值、前端 `scheduleToolsClear` 兜底、前端 `setToolsClearSec` 兜底、主窗 `mountTimeoutWheel` / `initTimeoutWheel` 兜底。任何一处漏改都会有「启动默认值 / 用户滚轮默认值 / 写入 .env 默认值」三者不一的隐患。
- **不动历史 `.env` / 数据库**：已经设过 20 的用户保留 20，要 10s 必须自己在设置页滚轮调到 10。默认值变化只在「首次启动 / 没碰过该设置」的机器生效。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位 20s 默认的所有出现点

按链路从「持久化」到「运行时兜底」扫一遍：

| 层 | 文件:行 | 默认值 |
|---|---|---|
| 配置字段 | `config.py:612` | `20.0` |
| API getter 兜底 | `gui.py:1977` | `20.0` |
| API setter 兜底 | `gui.py:1981` | `20.0` |
| Snapshot 字段 | `gui.py:2849` | `20.0` |
| 前端模块初始值 | `live_panel.js:621` | `20` |
| 前端 schedule 兜底 | `live_panel.js:742` | `20` |
| 前端 setToolsClearSec 兜底 | `live_panel.js:766` | `20` |
| 主窗 mountTimeoutWheel 兜底 | `app.js:5674` | `20` |
| 主窗 initTimeoutWheel 兜底 | `app.js:6127` | `20` |

### 第二阶段：方案设计

所有 9 处都从 `20` / `20.0` 改成 `10` / `10.0`。不引入新字段、不引入新设置项、不改 .env 键名（保持向后兼容）。

不写新测试：默认值变化是 trivial 数字修改，jsdom 测不到 4 层默认值链路；设置页滚轮交互已通过 v0.134 测试覆盖。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `config.py:612` 默认 `20.0 → 10.0` | 无 |
| 2 | `gui.py:1977, 1981, 2849` 兜底 `20.0 → 10.0` | #1 |
| 3 | `live_panel.js:621, 742, 766` 默认 / 兜底 `20 → 10` | 无 |
| 4 | `app.js:5674, 6127` 滚轮兜底 `20 → 10` | 无 |
| 5 | 资源版本 bump（live_panel.js + app.js） | #1-#4 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 是否动 .env 键名 | 不动 | 改键名会让现有用户的 `.env` 静默失效；改默认值让新用户自动 10、现有用户保留原值 |
| 是否给 4 处兜底加注释 | 加（v0.140 标识） | 后续人 grep 时能直接定位这次改动 |
| 是否 bump CSS 版本 | 不 bump | CSS 这次完全没动 |

---

## 4. 实现中遇到的问题

### 问题 1：grep 容易漏兜底

**症状**：第一轮 grep 只找到 5 处；细分「mountTimeoutWheel / initTimeoutWheel / scheduleToolsClear / setToolsClearSec / 模块初始值」后才发现主窗 `app.js` 还有 2 处滚轮兜底（`mountTimeoutWheel` 调设置页滚轮写回 .env，`initTimeoutWheel` 调 init 阶段渲染初始值）。

**解法**：分 4 层（持久化 / API / snapshot / 前端兜底）各 grep 一次，确保 9 处全改。

### 问题 2：兜底值与字段默认值是否要分两个常量

**症状**：如果 `_toolsClearSec` 初始值 10 而 `scheduleToolsClear` 兜底还是 20，调 `_toolsClearSec = NaN` 时仍按 20 跑 → 默认值改了一半。

**解法**：全部统一改成 10（不抽常量）。改动少、扩散面有限；将来若再变 7s 仍然 grep 9 处一起改。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 grep 漏兜底 | 4 层各 grep 一次（字段 / API / snapshot / 前端） | 全栈 |
| #2 兜底不一致 | 9 处全部 20 → 10 | 全栈 |

**最终 9 处默认值（全部 10 / 10.0）：**

- `config.py:612` `relay_gui_live_panel_tools_clear_timeout: float = 10.0`
- `gui.py:1977` `..., 10.0) or 10.0)`
- `gui.py:1981` `..., seconds, 10.0)`
- `gui.py:2849` `..., 10.0) or 10.0)`
- `live_panel.js:621` `let _toolsClearSec = 10;`
- `live_panel.js:742` `Number(_toolsClearSec) || 10`
- `live_panel.js:766` `Number(sec) || 10`
- `app.js:5674` `... : 10)`
- `app.js:6127` `..., 10);`

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **9 处默认值同步**：4 层（config 字段 / API / snapshot / 前端）全改 20 → 10。
- **不动 .env 键名**：现有用户 .env 不失效。
- **不写新测试**：trivial 数字修改。
- **资源版本 bump**：`live_panel.js` 和 `app.js` 同步 bump；CSS 不动。

### 偏离之处：

- **(a) 主窗 `app.js` 滚轮兜底也改了**：plan 没说一定要改这 2 处，但不改会让主窗设置页滚轮初始值仍是 20（前端 _toolsClearSec 实际是 10，但用户看 UI 是 20 会困惑）。**结构自决**。
- **(b) `_set_live_panel_float_setting` 内部的 `default` 参数也跟着改**：plan 没明确，但若不改，setter 写无效值时 fallback 是 20 而非 10，与 getter 行为不一致。**一致性自决**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/config.py`**：字段 `relay_gui_live_panel_tools_clear_timeout` 默认 `20.0 → 10.0`，注释加 v0.140 标识。
2. **`src/relay/gui.py`**：
   - `get_live_panel_tools_clear_timeout` 兜底 `20.0 → 10.0`。
   - `set_live_panel_tools_clear_timeout` 传给 `_set_live_panel_float_setting` 的 `default` 参数 `20.0 → 10.0`。
   - snapshot 字段 `live_panel_tools_clear_timeout` 兜底 `20.0 → 10.0`。

### 前端

1. **`src/relay/web/live_panel.js`**：
   - 模块初始 `_toolsClearSec = 20 → 10`。
   - `scheduleToolsClear` `Number(_toolsClearSec) || 20 → 10`。
   - `setToolsClearSec` `Number(sec) || 20 → 10`。
   - 三处注释加 v0.140 标识。
2. **`src/relay/web/app.js`**：
   - `mountTimeoutWheel(".prefs-live-panel-tools-clear-input", ...)` 兜底 `20 → 10`。
   - `initTimeoutWheel(".prefs-live-panel-tools-clear-input", ..., 20 → 10)`。

### 资源版本

- live_panel.js `?v=20260823-58 → ?v=20260823-59`。
- app.js `?v=20260824-02 → ?v=20260824-03`。
- styles-20260817.css / live_panel.css 不动。

### 测试

未新增测试（trivial 数字修改）。

### 行为验收清单（手动测试项）

- [ ] 清空 .env 中 `RELAY_GUI_LIVE_PANEL_TOOLS_CLEAR_TIMEOUT` 后重启，工具最后更新后 10s 自动清空（原 20s）
- [ ] 设置页「工具清除超时」滚轮默认值显示 10
- [ ] 已设过 20 的 .env 仍按 20 跑（向后兼容）
- [ ] Python 后端 `get_live_panel_tools_clear_timeout` 返回 10（无 .env 时）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/config.py` | 改（字段默认值 20.0 → 10.0） |
| `src/relay/gui.py` | 改（3 处兜底 20.0 → 10.0） |
| `src/relay/web/live_panel.js` | 改（3 处默认 / 兜底 20 → 10） |
| `src/relay/web/app.js` | 改（2 处滚轮兜底 20 → 10） |
| `src/relay/web/live_panel.html` | 改（live_panel.js 资源版本 bump） |
| `src/relay/web/index.html` | 改（app.js 资源版本 bump） |
