# 设置页存储管理重排：逐存储卡片（v0.113p）开发文档

> 设置里「存储管理」原本拆成「存储空间管理」（占用+操作按钮横排）与「存储位置管理」
> （路径+修改）两组，每个存储（relay.db / passthrough.db / 日志目录 / upstreams.json）
> 被劈成两半对读，操作按钮和对象脱节。v0.113p 重排成**每个存储一张卡**：名称 /
> 路径 / 占用 / 就地操作按钮全部合一；清理/压缩按 `target` 只作用于当前卡对应的库。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 优化设置里存储库相关的选项。现在比较乱。现在进入无人值守

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **存储项合并** —— 每个存储的占用信息与位置路径不再拆两组，合并到同一行/卡 | 「比较乱」 |
| B | **操作按钮就地** —— 清理/压缩/清空日志按钮从横排悬浮改为跟随其作用的存储 | 「比较乱」 |
| C | **按库精准操作** —— 清理/压缩只作用于当前库（relay.db / passthrough.db 分别处理） | B 的自然结果 |
| D | **去噪音** —— upstreams.json 是配置文件非存储，退化为只读展示，不占操作区 | 「比较乱」 |
| E | **无人值守** —— 设计决定我拍板（遵守 feedback_decide_show_effect），不追问直接做完展示效果 | 指令 |

### 隐含但需自行决策的点

- **既有数据不动**：`get_storage_info`（gui.py:1256）已返回全部所需
  `{db, pt_db, logs, upstreams}`（size/requests/messages/ts 范围/path）—— 零后端读改动。
- **⚠ 写操作仍走 relay HTTP**：清理/压缩是破坏性操作，relay 进程持 DB 锁
  （WAL），GUI 不写 DB。`cleanup-messages` / `vacuum` 端点加 `target` 参数
  （`"relay" | "passthrough" | "both"`，默认 both）实现按库精准操作。
- **⚠ 严禁 kill 8088**：`move_storage` 语义不变 —— 写 .env + 搬文件 + 失败回滚，
  中继运行中文件被锁 → 提示先停止中继，不自动重启。
- **前端 option 不再需要**：此区块无下拉，不涉及 v0.113o 的 NUL 分隔坑。
- **i18n 不扩大**：存储卡按钮原不在 I18N_SELECTOR 覆盖范围（`.settings-storage .btn`
  从未命中），保持现状，本轮不引入翻译（超范围）。

---

## 3. 分析需求后得出的开发路径

```
后端（破坏性操作仍走 relay；读零改动）
  routers/api.py  CleanupMessagesBody 加 target；cleanup-messages 按 target 只删对应库
                  VacuumBody 加 target；vacuum 按 target 只压对应库
  gui.py          cleanup_messages(days, target) / vacuum_storage(target) 透传
前端（app.js / styles / index.html）
  renderSettingsStorage 重排为 4 张 .storage-card + 底部刷新 + 说明 hint
  _fillStorage / refreshStorageInfo 复用（data-storage-detail / -loc 选择器不变）
  onStorageOp 按 data-target 精准操作；onMoveStorage dataset.move 改 "relay"/"passthrough"
  styles        .storage-card 系列（面板底 + hairline 描边 + 就地按钮行）
  index.html    注释更新 + 版本 query bump
```

### 开发顺序落地

```
#1 routers/api.py：cleanup/vacuum 加 target（body 模型 + 分支）
#2 gui.py：cleanup_messages / vacuum_storage 透传 target
#3 app.js：桥 cleanupMessages(days,target)/vacuumStorage(target) + renderSettingsStorage 重排
#4 app.js：onStorageOp / onMoveStorage / pickCleanupDays 按库联动
#5 styles：.storage-card 系列；index.html 注释 + 版本 bump（app.js 20 / styles 23）
#6 py_compile + node --check + 端点 target 单测 + headless stub-bridge 探针
#7 清理探针 + 写文档
```

---

## 4. 问题

### 4.1 原布局为什么「乱」

两组对读式布局，每个存储被劈两半：

```
存储空间管理                        存储位置管理
  消息数据库 (relay.db)              消息数据库位置  [修改]
    1.2 MB · 350 请求 · 900 条消息     D:\...\relay.db
  透传数据库 (passthrough.db)        透传数据库位置  [修改]
  日志目录 / 上游配置 ...            日志目录位置 / 上游配置位置 ...
  [清理消息记录][压缩数据库][清空日志][刷新]   ← 悬浮，不知道各自作用在哪个存储
```

问题：① 看一个库的完整信息要跨两组对读；② 操作按钮横排悬浮，且「清理消息记录」实际
同时删两个库、「压缩数据库」压两个库，与行文不符；③ upstreams.json 是配置文件非存储，
大小无意义、路径不可改，纯噪音占两行。

**改法**：单组「存储管理」，每个存储一张卡，路径 + 占用 + 就地按钮合一；
upstreams.json 退化为只读展示卡；清理/压缩按卡上的 `target` 精准作用于当前库。

### 4.2 按库精准操作需要后端支持

「清理消息记录」删两个库与卡片语义冲突 —— 消息库卡上的清理按钮应该只清消息库。

**改法**：`cleanup-messages` / `vacuum` 端点 body 加 `target`
（`"relay" | "passthrough" | "both"`，默认 both 兼容旧调用）：

```python
class CleanupMessagesBody(BaseModel):
    days: int
    target: str = "both"   # "relay" | "passthrough" | "both"

relay_deleted = await db.delete_all_requests() if body.target in ("relay", "both") else 0
pt_deleted = await pt_db.delete_all() if body.target in ("passthrough", "both") else 0
# 天数版同构：delete_requests_before / pt_db.delete_before 按 target 分支
```

### 4.3 桥的参数形态：`_call` 是位置展开

`_call(method, args)` 内部 `b[method](...args)` 位置展开（app.js:48）。一度把 payload
包成对象 `[{days, target}]` 会导致 gui 端 `cleanup_messages(self, days)` 收到 dict ——
改成位置传参 `[days, target]` 才对上 gui 的 `(self, days, target)`。

### 4.4 headless 探针的坑：chk/fail 不能关进 stub IIFE

第一版探针把 `chk`/`fail`/`contains` 定义在 stub 的 `(function () {...})()` 内，而断言
回调在 IIFE 之外 —— 首次 `chk` 直接 ReferenceError，且抛在 `waitFor` 的递归
`setTimeout` 里，外层的 try/catch 兜不住 → 报告停在「卡片已渲染」但断言一行没出。
**改法**：`chk`/`fail`/`contains` 提到顶层作用域。

---

## 5. 解决

### 5.1 routers/api.py：cleanup / vacuum 加 target

```python
class CleanupMessagesBody(BaseModel):
    """days>0 保留近 N 天；days<=0 清空全部。target 指定清理哪个库。"""
    days: int
    target: str = "both"  # "relay" | "passthrough" | "both"

@router.post("/storage/cleanup-messages")
async def cleanup_messages(request, body):
    if body.days <= 0:
        relay_deleted = await db.delete_all_requests() if body.target in ("relay", "both") else 0
        pt_deleted = await pt_db.delete_all() if body.target in ("passthrough", "both") else 0
    else:
        cutoff = time.time() - body.days * 86400
        relay_deleted = await db.delete_requests_before(cutoff) if body.target in ("relay", "both") else 0
        pt_deleted = await pt_db.delete_before(cutoff) if body.target in ("passthrough", "both") else 0
    # 响应保留 relay_deleted / passthrough_deleted（未命中的库为 0）

class VacuumBody(BaseModel):
    target: str = "both"

@router.post("/storage/vacuum")
async def vacuum_storage(request, body):
    if body.target in ("relay", "both"): await db.vacuum()
    if body.target in ("passthrough", "both"): await pt_db.vacuum()
    return {"ok": True}
```

### 5.2 gui.py：透传 target

```python
def cleanup_messages(self, days: int, target: str = "both") -> dict:
    return self._storage_post("cleanup-messages", {"days": int(days or 0), "target": target or "both"})

def vacuum_storage(self, target: str = "both") -> dict:
    return self._storage_post("vacuum", {"target": target or "both"})
```

### 5.3 app.js：桥 + renderSettingsStorage 重排

```js
cleanupMessages(days, target){ return this._call("cleanup_messages", [days, target || "both"]); },
vacuumStorage(target)      { return this._call("vacuum_storage", [target || "both"]); },
```

新渲染结构（`_fillStorage` / `refreshStorageInfo` 的 `data-storage-detail` / `-loc`
选择器不变，直接复用）：

```html
<div class="storage-card">
  <div class="storage-card-head">
    <span class="storage-card-name">消息数据库</span>
    <code class="storage-card-file">relay.db</code>
  </div>
  <div class="storage-card-path" data-storage-loc="db">—</div>
  <div class="storage-card-stats" data-storage-detail="db">加载中…</div>
  <div class="storage-card-actions">
    <button class="btn btn-ghost btn-sm" data-storage-op="cleanup" data-target="relay">清理消息记录</button>
    <button class="btn btn-ghost btn-sm" data-storage-op="vacuum" data-target="relay">压缩</button>
    <button class="btn btn-ghost btn-sm" data-move="relay">修改位置</button>
  </div>
</div>
<!-- 透传数据库（data-target="passthrough" / data-move="passthrough"）+ 日志目录
     （仅「清空日志」）+ 上游配置（只读卡，无按钮） -->
<div class="storage-hint">「清理」「压缩」需中继运行中执行。修改数据库位置会写 .env 并搬移文件；中继运行中文件被占用时，需先停止中继再迁移。</div>
<div class="storage-card-actions storage-card-actions-footer">
  <button class="btn btn-ghost btn-sm" data-storage-op="refresh">刷新</button>
</div>
```

### 5.4 app.js：onStorageOp / pickCleanupDays / onMoveStorage

- **onStorageOp**：`op` + `target`（`e.currentTarget.dataset.target`）双键。
  - `cleanup` → `pickCleanupDays(targetName)`（标题随卡联动「清理记录 · 消息数据库
    (relay.db)」）→ `api.cleanupMessages(days, target)`；结果拼接只显示非 0 的
    `relay_deleted` / `passthrough_deleted`。
  - `vacuum` → confirm 文案带目标库名 → `api.vacuumStorage(target)`。
  - `logs` → confirm danger → `api.clearLogs()`；`refresh` → 重拉。
- **onMoveStorage**：`dataset.move` 改为 `"relay" / "passthrough"`，直接
  `api.moveStorage(kind, next)`（gui 端 `move_storage` 本就用这两个字符串，去掉旧映射）。
- **pickCleanupDays(targetName)**：可选参，标题 `清理记录 · ${targetName}`。

### 5.5 styles / index.html / 版本

- **styles-20260817.css**：`.storage-card`（hairline 描边 + 8px 圆角 + `--panel-alt`
  底 + 列向 gap）、`.storage-card-head`（名称 + `code` 文件徽标）、`.storage-card-path`
  （等宽字体 + break-all）、`.storage-card-stats`、`.storage-card-actions`
  （flex wrap 就地按钮行）、`.storage-card-actions-footer`（右对齐）、
  `.storage-card-actions .btn-sm`（11px 小按钮）、`.storage-hint`。删除已无引用的
  `.settings-storage-path`；保留 `.settings-storage-actions`（报错分析设置区块仍用）。
- **index.html**：section 注释更新；版本 bump styles `?v=20260822-23`、app.js
  `?v=20260822-20`。
- **live_panel.html**：共享样式版本 bump `?v=20260822-23`。

---

## 6. 是否完全按规划

**完全按规划落地**，无自缩范围，有一个探针坑：

- **A 存储项合并**：4 张卡，占用 + 路径 + 操作合一，`data-storage-detail` / `-loc`
  选择器不变，`_fillStorage` / `refreshStorageInfo` 零改动复用。
- **B 操作就地**：每张卡下方自带按钮行，不再横排悬浮。
- **C 按库精准**：cleanup/vacuum 按 `data-target` 精准作用于当前库；后端 body 加
  `target`（默认 both 兼容旧调用）。
- **D 去噪音**：upstreams.json 只读卡，无按钮。
- **探针坑**：`chk`/`fail`/`contains` 误关进 stub IIFE → 断言回调 ReferenceError 且
  在 waitFor 递归 setTimeout 里抛、外层 catch 兜不住 → 提到顶层作用域修复。
- `python -m py_compile` routers/api.py / gui.py 通过；`node --check app.js` 通过；
  端点 target 单测（relay-only / passthrough-only / both / 默认 both，cleanup 0 天与
  30 天、vacuum 两态）全过；headless stub-bridge 探针 19 项全过（4 卡、三库占用文案、
  路径、3/3/1/0 按钮数、footer 刷新、data-move / data-target 配对、桥透传 target）；
  探针文件已清理。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/routers/api.py` | `CleanupMessagesBody` 加 `target`（cleanup-messages 按 target 只删对应库，未命中库返回 0）；新增 `VacuumBody` + vacuum 按 target 只压对应库 |
| `src/relay/gui.py` | `cleanup_messages(days, target="both")` / `vacuum_storage(target="both")` 透传 target |
| `src/relay/web/app.js` | 桥 `cleanupMessages(days,target)` / `vacuumStorage(target)`；`renderSettingsStorage` 重排为 4 张 `.storage-card` + hint + 刷新行；`onStorageOp` 按 `data-target` 精准操作；`onMoveStorage` 用 `dataset.move="relay"/"passthrough"`；`pickCleanupDays(targetName)` 标题联动；版本 `?v=20260822-20` |
| `src/relay/web/styles-20260817.css` | `.storage-card` 系列（head/file/path/stats/actions/footer/btn-sm/hint）；删 `.settings-storage-path`；版本 `?v=20260822-23` |
| `src/relay/web/index.html` | section 注释更新 + 版本 query bump |
| `src/relay/web/live_panel.html` | 共享样式版本 query bump |

### 状态流

- **布局**：设置页 → 存储管理：4 张卡片从上到下 = 消息数据库 (relay.db) / 透传数据库
  (passthrough.db) / 日志目录 (.relay-logs) / 上游配置 (upstreams.json，只读)。
  每卡显示路径（等宽）+ 占用明细（大小 · 条数 · 消息数 · 时间范围 / 文件数）。
- **操作**：消息库卡 `[清理消息记录][压缩][修改位置]`（只动 relay.db）；透传库卡
  `[清理透传记录][压缩][修改位置]`（只动 passthrough.db）；日志卡 `[清空日志]`；
  底部 `[刷新]` + 说明 hint。
- **清理**：点清理 → 弹窗标题「清理记录 · 消息数据库 (relay.db)」→ 选保留天数或清空
  全部 → 只删该库。**压缩**：confirm 文案带库名 → 只压该库。**修改位置**：prompt
  预填当前路径 → 写 .env + 搬文件；中继运行中被锁 → 提示先停止中继（不自动重启）。
- **relay 停转时**：占用信息仍可看（本地只读）；破坏性操作返回「中继未运行」。

### 验证

- `python -m py_compile` routers/api.py / gui.py 通过；`node --check app.js` 通过。
- 端点 target 单测（usage-stats env，fake db 记调用）：cleanup target=relay 只调
  relay、target=passthrough 只调 pt、both 与缺省都调两者（0 天与 30 天两分支）；
  vacuum target=relay 只压 relay、both 与缺省都压 —— 全过。
- headless stub-bridge 探针（19 项全过）：`cards=4`；db/pt/logs/upstreams 占用文案
  （1.2 MB · 350 请求 · 900 条消息 / 2.0 MB · 120 条透传请求 / 3.5 MB · 42 个文件 /
  8.0 KB）；三库路径；relay/pt/logs/upstream 按钮数 3/3/1/0；footer 刷新 1；`data-move`
  与 `data-target` 各配对 1 处；桥透传 target（`cleanup_messages(30,"relay")` /
  `vacuum_storage("passthrough")` 收到正确参数）。探针文件已清理。
- 刷新 GUI：设置页 → 存储管理：看到 4 张卡片与就地按钮；分别清理/压缩各库验证只作用于
  当前库；修改位置流程不变。
