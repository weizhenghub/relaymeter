

# 新建上游后切换 404（v0.197）开发文档

> ⚠ **重要**：本 bug 是「新建/删除上游后，中继与上游文件不同步」导致的切换 404。
> 修复把"刷新上游列表"的能力从 **GUI 桥的偶尔补救** 提升到 **select 端点自身必做**，
> 彻底消灭"必须重启 GUI 才能切换新上游"的现象。任何后续改动 select 链路/新建上游
> 的代码，都必须保持"切换前从磁盘 overlay 一次"这一前提，否则 bug 会复发。

## 1. 用户的初始指令

> 中继每次新建一个什么上游，只要在这个 GUI 生命周期中切换就一定会莫名其妙弹窗错误。
> 一定要直接重启 GUI 才能成功切换。是不是新建上游的时候没保存或者没立刻加载？

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 新建上游后、同一 GUI 生命周期内切换 → 弹窗报错 | 指令 |
| B | 重启 GUI 后能切 → 与进程重启强相关 | 指令 |
| C | 怀疑"没保存 / 没立刻加载" | 指令 |
| D | 弹窗文案 = 404 / 其它 HTTP 错（用户补答） | 追问 |

**隐含调研问题**（影响根因定位）：

- 新建上游这条链到底写了哪个文件、让哪个进程知道？
- 中继进程 vs GUI 进程的 settings 各自从哪来、何时刷新？
- 404 的确切来源（谁抛、抛什么）？

---

## 3. 分析需求后得出的开发路径（根因定位）

### 3.1 两条进程、三份 settings

中继 = **GUI 进程**（pywebview 主窗，`gui.py`）+ 它 `Popen` 拉起的 **uvicorn 子进程**
（`server.py:116`，命令 `-m uvicorn relay.main:app`）。同一个"上游列表"在这两个进程里
各有一份独立快照：

| 进程 | settings 来源 | 何时刷新 |
|---|---|---|
| GUI 进程 | `Api.create_upstream` 里 `reload_settings(); self._app.settings = get_settings()` | 写盘后立刻（GUI 侧） |
| uvicorn 子进程 | `lifespan` 里 `app.state.settings = get_settings()`（`main.py:61`） | **启动时一次**；之后只有被 `/api/upstreams/refresh` 通知才刷新 |

**关键**：`create_upstream` / `remove_upstream` 走的 **GUI 桥**（`config.add_upstream` 写
`upstreams.json` + reload **GUI 进程** 的 settings）。**uvicorn 子进程**的 `app.state.settings`
是启动快照，**不知道**磁盘新加的条目。

### 3.2 复活链：refresh 补救为何靠不住

`create_upstream` 写盘后，会 POST `/api/upstreams/refresh` 通知 **uvicorn 子进程** reload
（`gui.py:2667-2679`）。但这趟请求被 `except: pass` **静默吞掉**，可能失败的原因：

- 子进程此刻正在 restart（用户"新建后立刻切"时，上一轮 `restartRelay()` 可能还没就绪）→ 2s 超时；
- GUI 进程 `base_url` 与子进程实际监听地址不一致；
- 子进程 uvicorn 尚未启动完成，连接被拒。

### 3.3 404 的确切来源（真机复现）

拿到运行中的真实 uvicorn（PID 命中）后，用"**写盘但不 refresh → 直接 select**"复现：

```
HTTP 404 {"detail":"\"no upstream named 'diag_xxx'; known: ['f3af39d7', ...]"}
```

对照 `routers/api.py` `select_active`：`settings.set_active(platform, body.name)` 抛
`KeyError` → `raise HTTPException(404, str(exc))`（api.py:176-177）。**因为子进程
`app.state.settings` 没有这个新条目，`set_active` 找不到 → 404 → GUI 桥透传给前端 alert。**

**重启 GUI 为何能切**：重启会杀了旧 uvicorn、Popen 新进程 → 新进程 `lifespan` 里
`get_settings()` **重新读盘** → 拿到含新条目的配置 → `set_active` 能找到。

### 3.4 为什么初期排查"配置路径错了"被证伪

- 直接 `curl /api/upstreams` → `active=dp官方`、18 条（含新建的一条）——**子进程确实读 AppData 那份**；
- 模拟子进程 cwd=任意 + `os.environ` 无 `RELAY_UPSTREAMS_FILE` → `Settings()` 仍解析到
  `AppData\...\upstreams.json`（因 `.env` 里写的是**绝对路径**，`_resolve_config_path` 对绝对路径原样返回）；
- 结论：**配置加载路径本身没错，问题只在"进程启动后要不要重读"**。

---

## 4. 实现中遇到的问题（根因收敛过程）

### 问题 1：`reload_settings()` 会不会把配置清空？

**不会**。`reload_settings()` 重建的是模块级单例 `_settings`，且 `Settings.load()` 能靠
绝对路径稳定指向 AppData 那份。**否决**"refresh 会清空上游列表"的早期假设。

### 问题 2：`refresh` 端点本身是好的，为什么用户还碰到？

手动 `POST /api/upstreams/refresh`（子进程已跑起来）→ `{"ok":true}`、`/api/upstreams` 计数正确 = 18。
**端点没问题，问题在 `create_upstream` 里那趟补救请求不可靠**（见 §3.2），被静默吞掉。

### 问题 3：能否用 `reload_settings()` 整个替换 settings？

**不行**。`reload_settings()` 会重建整个 Settings，可能把运行时子进程的内存状态一起重置
（`set_active` 临时写的 `active`、`passthrough_mode` 等）。选择**就地 overlay**：用
`apply_to_settings(settings, settings.relay_upstreams_file)` 只覆盖 `upstreams` + `active` 指针，
保留其它字段。

### 问题 4：overlay 失败怎么办？

`apply_to_settings` 只在文件存在时生效，`load()` 缺文件返回 `None` 且不抛。若文件被临时占用
（极端情况），overlay 抛错后 **不能硬拒切换**（会退化成更差的体验）。选择 `except: log.warning`
软降级，让切换继续走原快照。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| 子进程 settings 不知道新上游 | select 端点**自身**在 set_active 前 `apply_to_settings(settings, relay_upstreams_file)`，把磁盘上游 overlay 进当前 settings | routers/api.py |
| 不能整体替换 Settings | 用就地 overlay，保留运行时状态 | routers/api.py |
| refresh 补救不可靠 | 不再依赖那趟 POST，select 端点自己兜底 | routers/api.py |
| overlay 失败不能硬拒 | `except: log.warning` 软降级 | routers/api.py |

### 改动（`src/relay/routers/api.py` `select_active`）

在 `_ensure_known(platform)` 之后、`settings.set_active(platform, body.name)` 之前，插入：

```python
try:
    from ..upstreams_file import apply_to_settings
    apply_to_settings(settings, settings.relay_upstreams_file)
except Exception as exc:
    log.warning("select: apply_to_settings failed: %s", exc)
```

**为何放前面且用当前 `settings`**：

- 每次切换都用磁盘最新上游列表 → 新建/删除后无需 restart、不赌 refresh 是否送达；
- 用 `request.app.state.settings`（当前对象）而不是 `reload_settings()`（新建对象）→ 保留运行时状态；
- `apply_to_settings` 就地改 `settings.upstreams` 与 `settings.active`，正符合"切换动作"的语义。

---

## 6. 是否完全遵循规划路径开发

**无（用户直接定了修法，无预先规划路径）。**

这次是**排查型**任务：用户报 bug → 我按"复现 → 定位 → 修正"走，而非"需求分析 →
多轮追问 → 规划"。修法由用户在三选一里选定：**"select 恒先 reload"**（用户选），
未走另外两个方向（"select 时自适应重试" / "先看完整方案再定"）。选它的理由：
把刷新集中到唯一的切换入口，一步到位，不需要在 GUI 桥侧再加可靠性逻辑。

### 偏离 / 补充说明

- **未改 GUI 桥的 refresh**：`create_upstream` 里那趟 POST 保留（触发刷新没有坏处，
  能少一次 select 端的重读），但不再依赖它。`except: pass` 保持现状。
- **未动 `_persist_active`**：select 成功后的持久化路径不变。

---

## 7. 最终实现点

### 后端（Python）

1. **`src/relay/routers/api.py`** `select_active`：`_ensure_known` 后 加一段
   `apply_to_settings(settings, settings.relay_upstreams_file)` 的兜底 overlay（带
   log.warning 软降级）。

### 行为验收清单（手动测试项）

- [ ] 新建一个上游 → **不重启 GUI** → 顶栏/上游页切到它 → 不再弹「切换失败 / HTTP404」
- [ ] 删除一个上游 → 不重启 → 切换其它上游正常
- [ ] 切换后 quick_switch / 侧栏高频刷新不抖动
- [ ] `passthrough_mode` 开关状态在切换后保持（不被 apply overlay 重置）
- [ ] 正常（未新建）时的切换：行为不回归，仍是即时生效

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/routers/api.py` | 改（`select_active` 加 apply_to_settings 兜底，约 +14 行） |

### 测试

- `tests/test_control_api.py` — 19 passed（含 select 相关用例，无回归）
- 单元模拟：构造不含新条目的旧快照 settings → `apply_to_settings` 后新条目被注入 →
  `set_active` 成功（不再抛 KeyError）
- 真机复现："写盘不 refresh → 直接 select" 在**未打补丁**时命中 404；补丁生效后需重启
  uvicorn 子进程加载新代码（用户验证）
