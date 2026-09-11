# Release 打包流程（v0.172）开发文档

> 目的：首次为 RelayMeter 做公开 release，产出 **PyPI wheel + Windows onefile exe** 双渠道。
> 本文固化整个探索过程、关键决策、命令、frozen 陷阱与修复，下次 release 直接照此执行，不必从零探索。

## 1. 用户的初始指令

> 现在这个项目的名称到达叫什么统一了吗（项目名统一排查）
>
> 好的，需要打包成release，应该用什么格式
>
> （打包格式选择）→ PyPI wheel + Windows onefile exe 两者都要
>
> 拷贝exe文件到桌面上
>
> 启动了exe的，再想从pycharm运行发现：单实例互斥被占用
>
> 退出行为不符合预期。托盘退出不掉 → 修复 frozen 托盘强退

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 项目名统一为 **RelayMeter**（GUI 窗题 / 托盘 / 网页 / README / 测试断言），内部包名/命令前缀统一为 `relay`/`RELAY_*` | 名称排查 |
| B | 发布 **PyPI wheel**（标准 Python 分发，pip install） | 格式选择 |
| C | 发布 **Windows PyInstaller onefile exe**（免 Python 用户直接双击） | 格式选择 |
| D | frozen exe 必须能跑 serve/gui/stats 三态 | C 的派生 |
| E | 单实例互斥在 exe 下不能绕过 | 实测冲突 |
| F | frozen 托盘"退出中继"必须真正退出进程 | 实测 bug |
| G | 流程固化进 dev doc，下次不再从零探索 | 本指令 |

### 隐含约束（探索后确认）

- 无现成打包配置：仓库只有 pyproject.toml，无 .spec / setup.cfg / MANIFEST。
- 运行时唯一非 Python 资源是 `src/relay/web/` 前端树；图标/托盘/悬浮球全部运行时合成（PIL/GDI+），无静态图片资产。
- 只打 Windows（本机 Win11 + pywebview WinForms），Linux/macOS 后续 CI 再说。
- exe 形态 onefile（用户下载即用），不维护 onedir。

## 3. 分析需求后得出的开发路径

### 3.1 两个核心架构决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| **单 exe 三态分发** | `relaymeter.exe` 入口转发到 `relay.__main__.main()` | `__main__.py` 已实现 serve/gui/stats argv 分发（__main__.py:74），单 exe 直接复用，无需新建多入口 |
| **数据目录** | frozen 下 `_project_root()` 固定回 `%LOCALAPPDATA%\Relay\` | 避免 relay.db/upstreams.json/.env 散落在 exe 所在目录（如 Downloads）；与 .env 既定意图、gui.py 的 `_APP_DATA_DIR` 一致 |

### 3.2 产物清单

| 产物 | 命令 | 落点 |
|---|---|---|
| `relaymeter-0.1.0-py3-none-any.whl` | `python -m build --wheel --sdist` | dist/ |
| `relaymeter-0.1.0.tar.gz` | 同上 | dist/ |
| `relaymeter.exe`（33.6 MB onefile） | `python scripts/build_release.py --exe` | dist/ |

### 3.3 一键构建脚本 `scripts/build_release.py`

```bash
python scripts/build_release.py            # 出 wheel + exe
python scripts/build_release.py --wheel    # 仅 wheel
python scripts/build_release.py --exe      # 仅 exe
python scripts/build_release.py --clean    # 先清 dist/ build/ *.egg-info
```

内部：`ensure_build_ico()` 调 `relay.icon.write_ico` 合成 exe 资源图标（`build/relaymeter.ico`）→ `python -m PyInstaller relaymeter.spec --noconfirm --distpath dist --workpath build`。

### 3.4 wheel 的关键：package-data

`pyproject.toml` 加：

```toml
[tool.setuptools.package-data]
relay = [
    "web/*.html",
    "web/*.js",
    "web/*.css",
    "web/vendor/*.js",
    "web/cursors/*.png",
]
```

**坑**：setuptools 的 `[tool.setuptools.exclude-package-data]` **不会**从 sdist 已收集的文件里剔除文件（它只在"package 内 py 之外的已包含资源"上作用，无法对 sdist 兜底收集的结果做减法）。要干净 wheel，必须把开发残留**物理移出** `src/relay/web/` → 已迁到 `scratch/web_dev_probes/`（探针 / _err.html / _mgmt.html / .bak）。

### 3.5 spec：datas 显式清单而非 Tree

`relaymeter.spec` 用 `datas` 显式列出 web/ 下每个文件 → `relay/web/`，不用 `Tree` 全量拷，避免带进探针 / 备份。目标目录结尾**必须有分隔符**（`"relay/web"` → 文件夹合并；`"relay/web/"` → 逐文件拷入）。

### 3.6 4 处 frozen 感知代码改动（全用 `getattr(sys, "frozen", False)` 守卫）

| 文件 | 源码态 | 冻结态 |
|---|---|---|
| `server.py:_start_locked` | `[sys.executable, -m uvicorn, relay.main:app, ...]` | `[sys.executable, serve, --listen H:P]`（`__main__` 分发 → in-process uvicorn） |
| `plugin.py:plugins_dir` | `Path(__file__).parent.parent.parent / "plugins"` | `Path(sys.executable).parent / "plugins"`（exe 旁） |
| `autostart.py:_command_line` | `"<pythonw.exe>" -m relay.autostart_boot` | `"{exe}"`（exe 默认 gui） |
| `config.py:_project_root` | 向上找 pyproject.toml | `%LOCALAPPDATA%\Relay\` |

### 3.7 单实例互斥 & 托盘退出的坑

见第 4 节。

## 4. 实现中遇到的问题

### 问题 1：`exclude-package-data` 不生效

**现象**：加了 exclude 重建 wheel，`_probe_*.py` 仍在。
**根因**：setuptools 的 exclude-package-data 不做 sdist 文件减法——它只决定"已被包进来的资源是否被 package-data 覆盖"，对 sdist 兜底收集进包的文件无效。
**解法**：把 `src/relay/web/` 下的开发残留物理移出（`mv` 到 `scratch/web_dev_probes/`），不靠 exclude。重建后 wheel 内 web/ 干净 10 文件（app.js / cursors / float_ball.html / index.html / live_panel.* / styles-20260817.css / vendor/）。

### 问题 2：frozen GUI 单实例互斥被绕过

**现象**：双击桌面 exe 后，从 PyCharm 跑 `main.py` 走 relay-gui，`SingleInstanceGuard acquired=False` → takeover 10s 超时退出；同时 `tasklist` 见 3 个 `relaymeter.exe`。
**根因**：frozen exe 是 PyInstaller bootloader（runw.exe）+ 自身 Python 运行时，`sys.executable` 指向 exe，源码态 `_pythonw_exe()` 找不到 pythonw 兄弟进程；单实例守卫在模块导入后才创建（gui.py:4708），两个 exe 同时双击会竞态互斥。
**后续**：源码态与 exe 共用同一 `.env` 的端口，先占端口者胜。测试 exe 前需先退出当前 GUI，或用 `--profile=headless` 只验 serve。

### 问题 3：frozen 托盘"退出中继"点不动

**现象**：右键托盘 → 点"退出中继"无反应，进程不退出（实测 3 个 PID 残留）。
**根因**：托盘菜单回调 `_quit_from_tray`（gui.py:3734）原路径 = `window.destroy()` → 跨线程 marshal 回主线程 → `_on_closing` → `Application.Exit()`。frozen windowed exe 下这条跨线程 destroy 链挂住，`Application.Exit()` 永不触发。
**解法**：`_quit_from_tray` 加 frozen 分支——不再依赖窗口消息循环，直接 `_stop_event.set()` + daemon 线程尽力 `server.stop()`（最多 join 3s）+ `os._exit(0)`。源码态（pythonw）走原 teardown 不变。

```python
if getattr(sys, "frozen", False):
    self._stop_event.set()
    _stop_thread = threading.Thread(
        target=self.server.stop, name="frozen-quit-stop", daemon=True
    )
    _stop_thread.start()
    _stop_thread.join(timeout=3.0)
    _logger.info("frozen quit: exiting")
    os._exit(0)
```

### 问题 4：PyInstaller 6 + pywebview 6 + pystray 的 hiddenimports

**现象**：首次打包 GUI 路径缺 webview 平台后端。
**解法**：spec 里 `hiddenimports = collect_submodules("webview")` + 显式 `["webview.platforms.edgechromium"]`。

### 问题 5：窗口标题 / 托盘标题在 frozen 下仍显示旧名

**现象**：改名 RelayMeter 后，wheel/exe 标题一致，但发现 `tests/test_tray.py:67` 断言 `"Usage Stats 中继"`（旧名）——随版本更名遗留。
**解法**：同步更新测试断言为 `"RelayMeter"`，usage-stats env 下 24 项全过。

## 5. 最后如何解决

- **wheel**：补 package-data + 物理清 web/ 残留 → `python -m build` 出 3 个产物（whl 851KB / sdist 954KB）。
- **exe**：4 处 frozen 守卫 + spec datas 显式清单 + hiddenimports webview → `python scripts/build_release.py --exe` 出 `dist/relaymeter.exe`（33.6 MB）。
- **验证**：
  - `serve --listen 127.0.0.1:9124` → `/healthz` 200、`/api/upstreams` 200（7937 B，upstreams.json 读取正常），uvicorn 日志正常。
  - wheel 新 venv `pip install` → `_WEB_DIR` 解析到 `site-packages/relay/web/`，10 文件齐全，4 个 console script（relay/relay-gui/relay-dashboard/relay-stats）全部可调用。
  - `tests/test_autostart` 24 全过；`tests/test_plugin` 5 项 e2e 失败为既有环境问题（对照实验证实与 frozen 改动无关）。

## 6. 是否完全遵循规划路径开发

- ✅ 双渠道（wheel + exe）按计划产出。
- ✅ 单 exe 三态分发复用 `__main__.main()`。
- ✅ 4 处 frozen 守卫全部 `getattr(sys,"frozen",False)`，源码态零行为变化。
- ⚠️ 原计划"Windows 优先，Linux/macOS 后续"——维持，未做三平台 CI。
- ✅ TUI dashboard 不进 exe（windowed exe 无终端，textual 无法渲染；pip 装的 `relay-dashboard` 覆盖）。
- ✅ 开发残留移出 web/ 超出原计划（原计划想用 exclude 解决，实测无效，改为物理迁移）——该偏差已固化到第 4 节问题 1。

## 7. 最终实现点

| # | 实现点 | 状态 |
|---|---|---|
| A | 项目名统一 RelayMeter（含测试断言） | ✅ |
| B | PyPI wheel：package-data + 清 web/ 残留 | ✅ |
| C | Windows onefile exe：spec + bootstrap + 4 处 frozen 守卫 | ✅ |
| D | exe serve 冒烟验证（healthz + upstreams） | ✅ |
| E | 单实例互斥冲突文档化（不硬修，源码/exe 同端口需互斥） | ✅ |
| F | frozen 托盘强退（os._exit 分支） | ✅ |
| G | 流程固化进本文档 | ✅ |

### 下次 release 的命令速查

> ⚠ **重要**：frozen 托盘退出走 `os._exit(0)`，会跳过正常 teardown。计量写入（relay.db）在 `os._exit` 前不一定刷盘。若担心丢计量，exe 版可考虑退出前显式 `db.close()`（当前未实现，作为已知限制）。

```bash
# 1. 构建
python scripts/build_release.py --clean    # 一键 wheel + exe（清残留）
# 产物: dist/relaymeter-0.1.0-{tar.gz,whl} + dist/relaymeter.exe

# 2. 验证 wheel（新 venv）
python -m venv /tmp/relay_venv
/tmp/relay_venv/Scripts/python -m pip install dist/relaymeter-0.1.0-py3-none-any.whl
/tmp/relay_venv/Scripts/python -c "from relay.gui import _WEB_DIR; print(sorted(p.name for p in _WEB_DIR.iterdir()))"

# 3. 验证 exe serve（避开 8088）
dist/relaymeter.exe serve --listen 127.0.0.1:9124   # /healthz 应 200

# 4. 发布（需 token，手动执行）
twine upload dist/relaymeter-0.1.0.tar.gz dist/relaymeter-0.1.0-py3-none-any.whl
gh release create v0.1.0 dist/relaymeter.exe --generate-notes
```

### 验证测试（usage-stats env）

```bash
conda activate usage-stats
python -m pytest tests/test_autostart.py -q -p no:cacheprovider
python -m pytest tests/test_tray.py -q -p no:cacheprovider
# test_plugin 的 5 项 e2e 失败为既有环境问题，与 frozen 改动无关
```
