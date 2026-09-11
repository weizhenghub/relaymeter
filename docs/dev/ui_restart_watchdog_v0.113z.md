# 重启链路改造（v0.113z）开发文档

## Context

顶栏「重启」按钮（`#btn-restart`，index.html:118）当前走 `Api.restart_server`
→ `ServerProcess.restart`（gui.py:870 / server.py:201），本质是 GUI 进程内
`stop()+start()` 自己 spawn 的 child uvicorn。本次改造按用户原话「设置守
护进程，然后手动杀死自己，接着守护进程重启中继，中继启动后清理守护进程」
重写为外部 watchdog 链路。

链路：`GUI 点击「重启」→ 写 marker + spawn watchdog（detached）→ GUI
sys.exit(0) → watchdog 等旧中继退出 → 起新中继 → 中继 lifespan touch
.relay-ready → watchdog 看到 marker → 起新 GUI（带 --restarting）→
watchdog exit`。

## 1. 用户的所有指令（按时间顺序）

> 重写顶部调试栏中"重启"的逻辑：点击后设置守护进程，然后手动杀死自己，
> 接着守护进程重启中继，中继启动后清理守护进程。

澄清三轮问答：
- 守护形态 → 外部脚本（PS + bash 镜像）
- 守护退出信号 → 中继启动后写「ready」文件
- 重启范围 → 中继 + GUI 一起重启

## 2. 实现点提炼

| # | 实现点 | 来源 | 文件 |
|---|---|---|---|
| A | 新增 watchdog.ps1：等旧 ready 清 → 起 start_relay.ps1（Hidden） → 轮询 ready 30s → 起 start_gui.ps1 --restarting → exit 0/1 | 用户原话 + 守护选择 | scripts/restart_watchdog.ps1 |
| B | watchdog.sh 镜像（nohup setsid + disown） | 跨平台对称 | scripts/restart_watchdog.sh |
| C | 中继 lifespan yield 前 touch `.relay-ready`；finally 开头 touch `.relay-shutdown` + 末尾 unlink ready | 中继 ready 信号选择 | src/relay/main.py |
| D | `Api.restart_server` 重写：stop 自己 child + spawn watchdog（detached） + sys.exit(0) | 用户原话「设置守护...杀死自己」 | src/relay/gui.py |
| E | `--restarting` CLI 标志 → App.restarting 字段 → mutex 等待分支跳过接管 | watchdog 拉新 GUI 路径 | src/relay/gui.py |

### 隐含但需自行决策的点

- **不动 `ServerProcess`**：保留 `start/stop/restart` 给其他场景（托盘菜单 / 自动启停等），
  仅顶栏「重启」走新链路。`restart_server` 改成新实现是单点替换，不动 API 协议。
- **「不杀端口」**：watchdog 自己 `start_relay.ps1` 启新中继，若端口还有残留
  孤儿 uvicorn，新 uvicorn 起不来——这是合理失败模式，用户从「中继启动失败」
  报错定位根因。比「先杀端口再起新」少一次竞态。
- **失败兜底**：watchdog 在「中继 30s 内未 ready」时 exit 1，不拉 GUI。GUI
  已经死透，不会出现「GUI 没起来、守护卡住」的孤儿态。
- **state 隔离**：`.relay-ready` / `.relay-shutdown` 写在 `_project_root()`（项目根），
  跟 `.env` / `relay.db` 同级，便于用户自查「中继到底启没启」。
- **GUI 退出协议**：`sys.exit(0)` 让 Python 走完清理（tray.stop / _clear_gui_pid），
  webview.start() 抛 SystemExit 返回，App.run 自然结束。
- **marker unlink 关键**：finally 末尾必须 unlink `.relay-ready`，否则下次重启
  watchdog 会误判「旧中继还在」直接跳过 wait 阶段 → start_relay 时端口被旧中继
  占着 → 新中继失败。
- **--restarting 标志**：旧 GUI sys.exit 后 mutex 自动释放，新 GUI 通常 `guard.acquired=True`
  直接走主路径；标志是兜底——若时序卡了（极小概率），新 GUI 不会去「接管」
  旧 GUI 触发的 force-kill 兜底逻辑，而是单纯轮询等到 mutex 可用。

## 3. 需求分析 → 开发路径

```
会话 #1 — watchdog 脚本骨架
  scripts/restart_watchdog.ps1（PowerShell 主路径）
  scripts/restart_watchdog.sh（POSIX 镜像）

会话 #2 — 中继 ready marker
  src/relay/main.py：lifespan yield 前 touch ready
                       finally 开头 touch shutdown + 末尾 unlink ready

会话 #3 — GUI 重启逻辑改造
  src/relay/gui.py：Api.restart_server 重写为 spawn watchdog + sys.exit
  src/relay/gui.py：run() 加 --restarting 标志 + App 字段 + mutex 等待分支
```

### 开发顺序落地

```
#1 watchdog.ps1（Win 主路径）
#2 watchdog.sh（POSIX 镜像）
#3 main.py lifespan touch ready / shutdown / unlink
#4 gui.py App.__init__ + restarting 字段
#5 gui.py run() argparse +restarting + mutex 等待分支
#6 gui.py Api.restart_server 重写
#7 验证（py_compile / bash -n）
```

## 4. 问题

### 4.1 Anthropic 不发中间 output_tokens（与本任务无关但已存在）

延续 v0.113x 思路，估算字段不入计费路径——本次只动重启链路，与实时
token 无关。

### 4.2 中继 lifespan 失败如何区分「成功 vs 失败」？

`yield` 之前的代码若抛异常，lifespan 直接走 finally → 中继进程退出，但
`.relay-ready` 没写过。watchdog 30s 轮询 timeout → exit 1，不拉 GUI。
**success marker 必须放在 yield 之前**——这是「中继已就绪」的唯一信号。

### 4.3 `.relay-ready` 必须 unlink（v0.113z 关键约束）

如果中继正常退出（finally 走完）不 unlink，下次重启时 watchdog 第 1 步
「等旧 marker 清掉」会等 6s 才发现 marker 仍在 → 浪费 6s 后才进 start_relay。
更糟：如果 watchdog 已经在起新中继而旧 marker 还在，watchdog 会同时
「等旧 marker 清」 + 「轮询新 ready」逻辑顺序错乱 → 整个状态机被搞崩。
`finally` 末尾 `unlink(missing_ok=True)` 是兜底保险。

### 4.4 watchdog 自身失败（脚本不存在 / 中继起不来）

watchdog 在每个 Start-Process 前 `Test-Path` 检查；中继 ready 30s 超时
`exit 1`。**GUI 已经在 spawn watchdog 后 sys.exit(0)**，用户从「双击
start_gui.ps1」或「计划任务」角度手动恢复——不会出现「GUI 没起来、守护
还卡住」的孤儿态。

### 4.5 GUI mutex 等待 vs 接管语义冲突（v0.113z 设计点）

新 GUI 由 watchdog spawn 时，旧 GUI 正在 sys.exit 过程中——mutex 还在。
如果新 GUI 走「接管」分支，会去 pipe `quit` 命令通知旧 GUI 退出（多此一
举）+ 等待 10s + force-replace 兜底（旧 GUI 已死但 PID 文件可能残留）。
**`--restarting` 标志让新 GUI 跳过接管逻辑**，仅 spin 等 mutex 可用
（通常 < 1s）。

### 4.6 watchdog 与 start_gui 透传参数

`start_gui.ps1` 末行 `& $python -m relay.gui @args` 已支持 `@args` 透传；
`start_gui.sh` 末行 `exec "$PYTHON" -m relay.gui "$@"` 也支持。watchdog 直接
传 `--restarting` 进 start_gui 即可，**start_gui 脚本本身不动**。

### 4.7 GUI `sys.exit(0)` 与 webview 清理顺序

`sys.exit(0)` 抛 SystemExit → `webview.start()` 抛 SystemExit 返回 →
`App.run` 走 `_tray.stop() + _clear_gui_pid()` → Python 解释器退出。
单实例锁依赖 OS 进程退出自动释放（single_instance.py:8-9 注释「靠内核
自动释放」），不需要显式释放代码。

## 5. 解决

### 5.1 scripts/restart_watchdog.ps1

```powershell
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $PSCommandPath
$root = (Join-Path $here "..")
$mark = Join-Path $root ".relay-ready"

# 1. 等旧 marker 清掉
for ($i = 0; $i -lt 30; $i++) {
    if (-not (Test-Path $mark)) { break }
    Start-Sleep -Milliseconds 200
}

# 2. 起新中继（detached, Hidden）
Start-Process -FilePath "powershell" -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "$startRelay"
) -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput (...watchdog-start-relay.out) `
  -RedirectStandardError  (...watchdog-start-relay.err) | Out-Null

# 3. 等 .relay-ready（最多 30s）
$ready = $false
for ($i = 0; $i -lt 150; $i++) {
    if (Test-Path $mark) { $ready = $true; break }
    Start-Sleep -Milliseconds 200
}
if (-not $ready) { Write-Error "relay did not become ready within 30s"; exit 1 }

# 4. 拉新 GUI（detached, --restarting）
Start-Process -FilePath "powershell" -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "$startGui", "--restarting"
) -WorkingDirectory $root -WindowStyle Hidden ...
exit 0
```

### 5.2 scripts/restart_watchdog.sh（POSIX 镜像）

nohup + setsid + disown 三件套保证守护退出后子进程不会被 SIGHUP 牵连
死掉。结构与 .ps1 完全镜像（4 步 + exit 0/1）。

### 5.3 src/relay/main.py lifespan

```python
# v0.113z：写 .relay-ready marker 给外部 watchdog 用。
_ready_marker = _project_root() / ".relay-ready"
try:
    _ready_marker.touch()
except OSError as exc:
    log.warning("failed to touch %s: %s", _ready_marker, exc)
try:
    yield
finally:
    # shutdown marker（诊断用） + 立刻 unlink ready
    _shutdown_marker = _project_root() / ".relay-shutdown"
    try:
        _shutdown_marker.touch()
    except OSError:
        pass
    try:
        _ready_marker.unlink(missing_ok=True)
    except OSError:
        pass
    sweep_stop.set()
    ...
```

### 5.4 src/relay/gui.py Api.restart_server

```python
def restart_server(self) -> dict:
    """v0.113z：外部守护链路。"""
    # 1. stop 自己 child（让 watchdog 更快接管）
    try:
        self._app.server._stop_locked()
    except Exception as exc:
        log.warning("restart_server: _stop_locked failed: %s", exc)

    # 2. spawn watchdog（detached, CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW）
    root = _project_root()
    watchdog = (root / "scripts" /
                ("restart_watchdog.ps1" if sys.platform == "win32"
                 else "restart_watchdog.sh"))
    if not watchdog.exists():
        return {**self.get_status(), "error": f"watchdog_missing: {watchdog}"}
    try:
        subprocess.Popen(
            [...],
            cwd=str(root), stdout=DEVNULL, stderr=DEVNULL, stdin=DEVNULL,
            creationflags=..., close_fds=...,
        )
    except Exception as exc:
        return {**self.get_status(), "error": f"watchdog_spawn_failed: {exc}"}

    # 3. GUI 自己退出
    sys.exit(0)
```

### 5.5 src/relay/gui.py run() --restarting 分支

```python
parser.add_argument("--restarting", action="store_true",
    help="v0.113z：标记本进程是 restart_watchdog spawn 的接替者...")
...
if not guard.acquired:
    if args.restarting:
        # 旧 GUI 已 sys.exit，mutex 即将自动释放。spin 等即可。
        deadline = time.time() + 10.0
        while time.time() < deadline:
            guard = SingleInstanceGuard("relay-gui")
            if guard.acquired:
                break
            time.sleep(0.1)
        if not guard.acquired:
            return  # abort
    else:
        # 既有接管逻辑（v0.93）
        ...
```

## 6. 是否完全按规划

**按规划落地**，无遗漏：

- watchdog 脚本两个平台都已实现（PS + bash 镜像）
- 中继 lifespan marker 协议按 yield 前 / finally 头 / finally 末三段位写
- Api.restart_server 重写为 spawn + sys.exit
- --restarting 标志 + mutex 等待分支
- 验证 `python -m py_compile` + `bash -n` 通过
- PowerShell 静态检查不可用（环境无 pwsh），手工对照 cmdlet 调用语法

**端到端验证需要用户在 GUI 里点一次**：
- 正常运行 → 点「重启」→ 窗口消失 → 2-3s 内新窗口起来
- `.env` 故意写错端口 → 点「重启」→ 窗口消失 → 30s 后无新窗口 → watchdog exit 1
- 任务管理器看 watchdog 进程短暂出现后立刻消失

**回归测试**：托盘「退出中继」路径、API 调用 `restart_server`、外部接管路径
均不变。`_takeover_external_listener` 保留供其他场景使用。

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `scripts/restart_watchdog.ps1` | **新**：PowerShell 守护脚本（Win 主路径） |
| `scripts/restart_watchdog.sh` | **新**：POSIX 守护脚本 |
| `src/relay/main.py` | `from pathlib import Path` 提升到顶层；`lifespan` yield 前 touch `.relay-ready`，finally 开头 touch `.relay-shutdown` + 末尾 unlink ready |
| `src/relay/gui.py` | `App.__init__` 加 `restarting: bool = False`；`Api.restart_server` 完整重写为 spawn watchdog + sys.exit；`run()` argparse 加 `--restarting`；mutex 等待分支按 restarting 跳过接管逻辑；两处 `App(settings, diag=args.diag)` 同步加 restarting 透传 |
| `docs/dev/ui_restart_watchdog_v0.113z.md` | 本开发文档 |

### 状态流（用户视角）

- **正常重启** —— GUI 顶栏「重启」按钮 → 窗口消失 → 2-3s 后新 GUI 窗口
  起来（中继 lifespan 写 `.relay-ready` → watchdog 看到 → 拉新 GUI）→
  watchdog 进程消失。
- **中继启动失败** —— 重启按钮 → 窗口消失 → 30s 后仍无新窗口（watchdog
  30s 超时 exit 1）→ 用户从「双击 start_gui.ps1」或「计划任务」恢复。
- **历史 GUI 接管仍可用** —— 用户从开始菜单 / 命令行直接 `relay-gui`
  启动第二个实例，仍走 v0.93 接管逻辑（pipe quit + 等待 + force-replace）。

### 验证

- `python -m py_compile src/relay/gui.py src/relay/main.py` 通过。
- `bash -n scripts/restart_watchdog.sh` 通过。
- `pwsh` 不在环境，手工对照 .ps1 cmdlet 调用语法（Test-Path / Start-Process /
  For / Write-Error / exit 都是标准 cmdlet）。
- 端到端：等用户在 GUI 实测一次「重启」按钮。
- 回归：托盘「退出中继」路径、API restart_server 调用（无 spawn watchdog）、
  外部接管（`_takeover_external_listener`）保持不变。

### 跨任务共性（与 v0.113 系列的衔接）

- **不动计费 / 进程边界**：watchdog 启 start_relay.sh 是阻塞前台命令，
  start_relay 自己 exec uvicorn，watchdog 只 Start-Process 后立刻
  detach。uvicorn 仍是 uvicorn 进程（CREATE_NEW_PROCESS_GROUP），与
  ServerProcess 同款。
- **失败兜底对称**：v0.113u「设置持久化」是 localStorage 兜底 + 后端
  权威；v0.113z 是 watchdog 30s 超时 exit 1 + GUI sys.exit 兜底。都是
  「后端权威 + 异步轮询兜底」。
- **持久化优先级**：watchdog 写 `.relay-ready` 是进程间 marker，不是
  业务持久化（finally unlink）。与 v0.113u 写 `.env / upstreams.json` 的
  业务持久化分层清晰。
- **严禁 kill 8088**：watchdog 不 kill 端口——端口冲突让 start_relay 自
  己暴露成「中继启动失败」，用户从错误日志定位。比「先杀端口再起新」
  少一次竞态，符合记忆规则的精神（不主动杀 8088 监听者）。

---

## ⚠ 已知问题（用户实测报告，2026-08-22 留档，待后续会话修复）

**症状**：点击顶栏「重启」后，UI 显示「中继未运行」+ 按钮恢复可用，但
**窗口没有消失、没有进入「重启中…」终态**，watchdog / 新 GUI 都没有起来。

**根因（已定位）**：

Python 端 `Api.restart_server` (gui.py) 在 spawn watchdog 后立刻
`sys.exit(0)`——这本身是对的。问题是 JS 端 `restartRelay()` (app.js:422)
有这段：

```js
const res = await api.restart();   // :439
// ... await 在此挂起
```

`sys.exit(0)` 把 Python 解释器杀掉，pywebview bridge 断——`await`
**永远不会拿到返回值**。但 pywebview 内部把这种「连接突然断」转成
**静默 reject**，被 `catch (e)` 兜底，最后 `finally` 块（:451-484）：

- :469 主动 `api.status()` → 此时旧中继刚死、新中继还在 30s 等待中，
  → 渲染「中继未运行」（dot-stopped）+ sidebar 状态「已停止」
- :482-483 按钮恢复 enabled
- :485 `return { ok: true }`

用户看到「重启」→「中继未运行」、按钮恢复——但 watchdog **确实**已经
起来了，正在 30s 内等 `.relay-ready`。新中继 ready 后会拉新 GUI，但
**用户当前已经「完成」了「重启」操作**，感觉是「点了重启但啥也没发生」。

**两条修复方向**（用户决定走哪条前不动）：

1. **JS 端 fire-and-forget（推荐）**：
   `await api.restart()` → `api.restart()` 不 await，立即进入「重启中…
   窗口即将消失」终态（dot-restarting + 灰按钮 + status-label = "重启中…
   等待新窗口"），让 Python 进程自然退出。**不要在 finally 里拉
   api.status() / 恢复按钮**——反正进程马上死。
   改动小：app.js restartRelay() 改 ~15 行。

2. **Python 端改造**：
   `Api.restart_server` 不在 Python 里 `sys.exit(0)`，而是返回
   `{restarting: True, watchdog_pid: ...}` 让 JS 显示「窗口即将消失」
   toast，**JS 主动调 `api.shutdown_gui()`** 走 `_quit_from_tray` 路径
   优雅退出（tray.stop + _clear_gui_pid + mutex 释放）。更对称但改动
   更大（gui.py + app.js 两端）。

**状态**：本次会话**不动**，等用户决定走方向 1 还是方向 2 后另开会话
修复。watchdog 脚本 / 中继 lifespan marker / `--restarting` 标志这三
块本身是对的，无需回退。

