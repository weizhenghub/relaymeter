# 设置页新增：语言 / 存储空间管理 / 存储位置管理（v0.113n）开发文档

> 设置页加三组选项：**语言**（选项 + i18n 钩子，设置页可见翻译）、**存储空间管理**
> （显示占用 + 按天清理消息 + VACUUM + 清空日志）、**存储位置管理**（显示各存储路径
> + 可修改 DB 位置）。

---

## 1. 用户的初始指令

按时间顺序原文（一字未改）：

> 增加"语言"选项，存储空间管理选项，存储位置管理选项

（我追问澄清，用户选择并备注：）

> 语言深度：全量界面翻译 —— 备注：只加
> 存储空间管理：显示占用情况 / 清理消息记录 / 压缩数据库 / 清空日志（四项全要）
> 存储位置管理：显示 + 可修改

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | **语言选项** —— 设置页加语言选择器，存 localStorage；「只加」= 加选项 + i18n 机制钩子，全量翻译留后续会话 | 指令 + 备注「只加」 |
| B | **存储空间管理：显示占用** —— relay.db / passthrough.db / 日志目录 / upstreams.json 的大小、条数、时间范围 | 指令 |
| C | **存储空间管理：清理消息记录** —— 按天数删（保留近 N 天）或清空全部 | 指令 |
| D | **存储空间管理：压缩数据库** —— 对两个 DB 执行 VACUUM 回收空间 | 指令 |
| E | **存储空间管理：清空日志** —— 清空 `.relay-logs/` | 指令 |
| F | **存储位置管理** —— 显示当前路径 + 可修改 relay.db / passthrough.db 位置（写 .env + 搬文件） | 指令 |

### 隐含但需自行决策的点

- **语言「只加」的具体边界**：不做全量翻译（约 7000 行 app.js 全硬编码中文、零 i18n
  基建）。只做：选项 + 一个能**立刻看到效果**的最小可见面 = 设置页全部静态文案（分组/
  条目标题/hint/区块标题/子菜单/主题三档）+ 新存储区块。切换后设置页整页换语言，
  钩子（dict + applyLang）就位，全量翻译 = 扩 dict + 扩大 selector。
- **i18n 实现形态**：用**中文原文做 dict key**、切换时对匹配元素逐 `textContent`
  替换（`data-zh` 缓存原文供切回）。不引入 `data-i18n` 属性、不动模板 —— 侵入最小。
- **读写分离**：relay 进程持 DB 写锁（WAL + asyncio.Lock）。**写操作走 relay HTTP
  端点**；**读（占用信息）GUI 本地算**（relay 停了也能看，只读 `mode=ro` 打开）。
- **⚠ 严禁 kill 8088（记忆规则）**：`move_storage` 遇中继运行中文件被锁 → **回滚 .env、
  不自动重启**，返回可读提示让用户自己停/起。
- **清理消息的级联语义**：`messages` 表 FK `ON DELETE CASCADE`（db.py:64），删
  `requests` 行即级联删消息 —— 按天清理只需一条 DELETE。
- **日志目录无 env 键**：`.relay-logs/` 硬编码 project root（server.py:247），本轮
  「存储位置」只显示、不可改（改要动 server.py / autostart_boot / gui 多处，超范围）。
- **upstreams.json 只读显示**：有 env 键但改位置牵动 config 加载链，本轮只显示。
- **防抖式渲染守卫**：存储信息含文件 I/O + DB 查询，不能每 500ms tick 跑 ——
  `storageRendered` 守卫只渲染一次 + 手动「刷新」按钮。

---

## 3. 分析需求后得出的开发路径

```
后端（写 = relay 进程，读 = GUI 进程）
  db.py            Database 增 delete_requests_before / delete_all_requests / vacuum
  passthrough/db.py PassthroughDatabase 增 delete_before / delete_all / vacuum
  routers/api.py   POST /api/storage/cleanup-messages / vacuum / clear-logs
  gui.py           Api 增 get_storage_info（本地只读算占用）
                       cleanup_messages / vacuum_storage / clear_logs（HTTP→relay）
                       move_storage（写 .env + 搬文件 + 回滚）
前端（app.js / index.html / styles）
  I18N            dicts(中/英) + t() + applyLang() + setSegLang()；外观组加「语言」seg
  renderSettingsStorage  存储管理 section：占用组 + 位置组 + 操作按钮
  index.html      settings-storage section + nav 子项「存储管理」
  styles          .settings-storage-actions / -path / .seg-cleanup / .sc-clear-row
```

### 开发顺序落地

```
#1 后端 db 层：删除 + vacuum 方法（db.py / passthrough/db.py）
#2 后端 relay 端点：/api/storage/{cleanup-messages,vacuum,clear-logs}
#3 后端 gui 桥：get_storage_info（本地算）+ 三个写操作（HTTP）+ move_storage（.env+搬文件）
#4 前端 api 桥 + I18N 基建 + 语言 seg（外观组）+ 设置页字符串钩子
#5 前端 renderSettingsStorage + renderActiveView 调用 + index.html section/nav + styles
#6 py_compile + node --check + 单测（删/真空级联）+ headless stub-bridge 探针
#7 版本 bump + 清理探针 + 写文档
```

---

## 4. 问题

### 4.1 语言：整个界面零 i18n 基建，全量翻译是天量工作量

app.js ~7000 行、live_panel.js、两个 html 全部硬编码中文，`<html lang="zh-CN">`。
没有任何翻译机制。用户注明「只加」—— 本轮不能做全量翻译，但选项必须**可见生效**，
否则像坏掉的开关。

**改法**：最小可见面 = 设置页全部静态文案 + 存储区块。用**中文原文做 key** 的 dict，
`applyLang()` 对 `.settings-group-label / .settings-item-title / .settings-item-hint /
.settings-section-title / .settings-section-subtitle / .nav-sub-item / .seg-theme .seg-btn
/ .settings-storage .btn` 逐元素替换 `textContent`，`data-zh` 缓存原文切回还原。
不侵入模板、不改 HTML 结构。

### 4.2 存储空间管理：谁有写权限？

relay 进程持有 DB（WAL + `_lock`，db.py:147）；GUI 进程只读打开（`mode=ro`，
gui.py:1271 先例）。两个进程同时写 DB 会撞锁 / 损坏。

**改法**：**写走 relay HTTP**（`POST /api/storage/*`，gui.py `urllib` 镜像
`get_relay_settings` 模式，api.py:327 同款）。**读本地算**：GUI 直接 `os.path.getsize`
+ 只读 sqlite 查条数/时间范围 —— relay 停了也能看占用。

### 4.3 清理消息的级联

`messages.request_id` FK `ON DELETE CASCADE`（db.py:64）—— 删 requests 行，
其 messages 自动删。所以 `delete_requests_before(ts)` 一条 DELETE 就够，不用逐条删。

### 4.4 压缩数据库为什么需要 relay 端点

SQLite `VACUUM` 要求无并发事务；GUI 只读连接 + relay 写连接并存时 VACUUM 会拿到
"database is locked"。VACUUM 必须由持锁的 relay 进程在 `_lock` 内执行。

### 4.5 修改存储位置：Windows 文件锁 + 严禁 kill 8088

DB 在 WAL 模式下被 relay 进程长期持有句柄，运行中搬文件必然 `PermissionError`。
**记忆规则：严禁自动 kill 8088 中断用户会话**。所以 move_storage 不能「停→搬→起」。

**改法**：写 .env → 尝试搬文件 → 失败（被锁）→ **回滚 .env**，返回可读提示
「请先在『中继状态』停止中继，再执行迁移」，把停/起的主动权交给用户。

### 4.6 存储信息不能每 500ms tick 重算

`renderSettingsStorage` 若每 tick 跑文件 stat + DB 查询，idle 时也在刷盘。

**改法**：`storageRendered` 守卫只渲染一次；「刷新」按钮 + 每次操作完成后手动重拉
（`refreshStorageInfo`）。数据变化通过操作后的手动刷新体现。

### 4.7 headless 探针的坑：stub 里的 Windows 路径 `\u` 炸掉整个脚本

探针 stub 数据里写了 `"C:\proj\upstreams.json"` —— JS 把 `\u` 当 unicode 转义起始，
`\upstreams` 不是合法转义 → **SyntaxError → 整个 stub 脚本不执行 → 报告全空**。
（`\p`/`\r` 是合法转义所以没炸，唯独 `\u` 炸。）**改法**：stub 路径改用正斜杠
`C:/proj/...`。另外 `_call` 走的是 snake_case（`get_storage_info`），stub Proxy 的 key
也得用 snake_case，否则 `db-hint` 停在「加载中…」。

---

## 5. 解决

### 5.1 db.py：删除 + VACUUM（Database / PassthroughDatabase）

```python
# db.py Database 新增（同款锁模式，串行化写）
async def delete_requests_before(self, ts: float) -> int:
    async with self._lock:
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute("DELETE FROM requests WHERE ts < ?", (ts,))
            await c.commit()
            return cur.rowcount or 0

async def delete_all_requests(self) -> int: ...   # DELETE FROM requests
async def vacuum(self) -> None: ...               # VACUUM（_lock 内）

# passthrough/db.py PassthroughDatabase 同款：delete_before / delete_all / vacuum
```

- `messages` 级联删，无需单独处理。
- 已单测（usage-stats env）：新/旧行各 1 → `delete_requests_before` 只删旧行（deleted=1，
  messages 保留 1 条）；`delete_all` relay/pt 各 1；vacuum 正常。

### 5.2 routers/api.py：存储清理端点

```python
@router.post("/storage/cleanup-messages")   # body {days:int}
    days<=0 → db.delete_all_requests() + pt_db.delete_all()
    days>0  → cutoff = now - days*86400 → delete_requests_before / pt_db.delete_before
    return {ok, days, relay_deleted, passthrough_deleted}

@router.post("/storage/vacuum")             # db.vacuum() + pt_db.vacuum()
@router.post("/storage/clear-logs")         # 遍历 <root>/.relay-logs/ 删常规文件
    # 只删该目录正下方 is_file()；realpath 前缀校验防目录穿越
```

- 均读 `request.app.state.db` / `.pt_db`（main.py:60/65）。
- `time` 是新增 import（api.py 原本没有）。

### 5.3 gui.py：Api 存储桥

- **`get_storage_info()` 本地算**：从 `self._app.settings` 取解析后的 relay_db /
  relay_passthrough_db / relay_upstreams_file + `_project_root()/`.relay-logs`。
  逐项 `os.path.getsize`；DB 用 `sqlite3` 只读 `mode=ro`（gui.py:1271 同款）查
  `SELECT COUNT(*), MIN(ts), MAX(ts) FROM requests` / `COUNT(*) FROM messages` /
  `COUNT(*) FROM passthrough_requests`（表不存在兜底 0）。日志目录数文件 + 总字节。
  返回 `{db, pt_db, logs, upstreams}`。
- **`cleanup_messages(days)` / `vacuum_storage()` / `clear_logs()`**：`_storage_post()`
  `urllib` POST `{base_url}/api/storage/...`（镜像 get_relay_settings gui.py:1173）；
  relay 不可达返回 `{ok:false, error:"中继未运行：..."}`。
- **`move_storage(kind, new_path)`**：
  1. 校验 kind ∈ {relay, passthrough}；`abspath`；父目录 mkdir；与当前相同 → 报错。
  2. `update_env_var("RELAY_DB"|"RELAY_PASSTHROUGH_DB", new)`（pydantic-settings 字段
     `relay_db` ↔ env `RELAY_DB`，config.py:409）。
  3. `shutil.move` 搬文件 + `-wal`/`-shm` 副件（存在才搬）。
  4. `PermissionError`（Windows 文件锁 = 中继运行中）→ **回滚 .env 恢复旧值**，返回
     「中继正在运行，文件被占用。请先在『中继状态』停止中继，再执行迁移」。
  5. 成功 → `reload_settings()` + `self._app.settings = get_settings()`（同步 GUI 副本，
     gui.py:1213 同款）。`shutil` 为新 import。

### 5.4 app.js：I18N 基建 + 语言选项

```js
const I18N_SELECTOR = [
  ".settings-group-label", ".settings-item-title", ".settings-item-hint",
  ".settings-section-title", ".settings-section-subtitle", ".nav-sub-item",
  ".seg-theme .seg-btn", ".settings-storage .btn",
].join(", ");
const I18N = {
  lang: (() => { try { const v = localStorage.getItem("lang"); return v === "en" ? "en" : "zh"; } catch (_) { return "zh"; } })(),
  en: { "外观": "Appearance", "主题": "Theme", /* …设置页全部静态文案… */ },
};
function t(key) { return (I18N.lang === "en" && I18N.en[key]) || key; }
function setSegLang(lang) { /* .seg-lang 按钮 active */ }
function applyLang() {
  document.documentElement.lang = I18N.lang === "en" ? "en" : "zh-CN";
  document.body.classList.toggle("lang-en", I18N.lang === "en");
  setSegLang(I18N.lang);
  document.querySelectorAll(I18N_SELECTOR).forEach((el) => {
    let zh = el.getAttribute("data-zh");
    if (zh == null) { zh = (el.textContent || "").replace(/\s+/g, " ").trim(); if (!zh) return; el.setAttribute("data-zh", zh); }
    const val = t(zh);
    if (el.textContent !== val) el.textContent = val;
  });
}
```

- **外观组加「语言」seg**（renderSettingsPrefs，紧跟「主题」后）：简体中文 / English。
- **wireSettingsPrefs** 绑 seg-lang：点击 → `I18N.lang` + `localStorage.setItem` +
  `applyLang()`（纯前端，无后端桥）。
- **调用点**：DOMContentLoaded 首帧（静态区块/子菜单立即生效）+ renderSettingsPrefs
  末尾（动态渲染后）+ renderSettingsStorage 末尾。
- 切语言即时生效（逐元素替换，不重渲染；prefsRendered 只渲染一次，DOM 跨视图常驻）。

### 5.5 app.js：renderSettingsStorage + 桥

- **api 桥**：`getStorageInfo()` / `cleanupMessages(days)` / `vacuumStorage()` /
  `clearLogs()` / `moveStorage(kind, path)`。
- **renderSettingsStorage(body)**（renderActiveView 设置分支调用，app.js:1819 附近）：
  `storageRendered` 守卫只渲染一次；两组渲染：
  - **存储空间管理**：消息数据库 / 透传数据库 / 日志目录 / 上游配置 四行（占位「加载中…」，
    `refreshStorageInfo` 异步填充 `_fmtBytes` 大小 + 条数 + 时间范围）；操作按钮
    `[清理消息记录] [压缩数据库] [清空日志] [刷新]`。
  - **存储位置管理**：relay.db / passthrough.db 路径 + `[修改]`（`prompt()` 预填当前路径
    → `moveStorage`）；日志目录 / upstreams.json 路径只读显示。
- **onStorageOp**：
  - 清理 → 自绘弹窗 `pickCleanupDays()`（7/30/90/180 天 seg + 清空全部 danger）→
    `cleanupMessages(days)` → `alertModal` 显示删除条数。
  - 压缩 → `confirmModal` 确认 → `vacuumStorage()`。
  - 清空日志 → `confirmModal` danger → `clearLogs()`。
  - 每次操作后 `refreshStorageInfo` 重拉。
- **onMoveStorage**：`prompt()` → `moveStorage(kind, path)` → 成功 `alertModal` 提示
  「若中继运行中，先停止再启动使用新位置」；失败显示 error。
- `pickCleanupDays` 自绘 modal（复用 `.modal-overlay/.modal-card-dialog` 样式，body
  不 escape —— confirmModal 的 body 会被 `escape()` 吃掉，无法放交互按钮，故自绘）。

### 5.6 index.html / styles

- **index.html**：nav 子菜单加 `data-settings-sub="storage"`（存储管理）；settings 视图加
  `<section class="settings-section" data-card="settings-storage">` +
  `#card-settings-storage-body`（渲染走 renderSettingsStorage）。
- **styles-20260817.css**：`.settings-storage-actions`（按钮行 flex wrap）、
  `.settings-storage-path`（长路径换行）、`.seg-cleanup .seg-btn`（小号 seg）、
  `.sc-clear-row`（清空全部按钮间距）。

---

## 6. 是否完全按规划

**按规划落地**，有一个自缩范围 + 两个探针踩坑：

- **语言（A）**：选项 + i18n 钩子 + 设置页可见翻译。用户备注「只加」—— 全量翻译明确
  留后续会话，钩子已就位（扩 dict + 扩大 I18N_SELECTOR）。**自缩范围**：upstreams.json
  位置只读显示、日志目录位置只读显示（无 env 键，改要动 server.py/autostart_boot/gui
  多处，超「只加」范围）。
- **存储空间管理（B–E）**：四项全落地。读本地算 / 写走 relay HTTP 的职责分离是本次
  核心决策。
- **存储位置管理（F）**：relay.db + passthrough.db 可改（写 .env + 搬文件 + 回滚）；
  运行中迁移返回可读提示、**不自动重启**（遵守「严禁 kill 8088」记忆规则）。
- **探针坑**：stub 数据里 Windows 路径 `\u` 导致 stub 脚本 SyntaxError 全不执行 →
  改正斜杠；`_call` 走 snake_case，stub Proxy key 也要 snake_case（否则 db-hint 停在
  「加载中…」）。
- `python -m py_compile` 四后端文件通过；`node --check app.js` 通过；DB 删除/vacuum
  级联单测通过；headless stub-bridge 探针全过（见 5.4/5.5 验证）。
- 版本号 bump：app.js `?v=20260822-18`、styles `?v=20260822-21`（index.html +
  live_panel.html）。

---

## 7. 最终实现点

### 改动文件清单

| 文件 | 改动 |
|---|---|
| `src/relay/web/app.js` | I18N 基建（dicts/t/applyLang/setSegLang/I18N_SELECTOR）+ 外观组「语言」seg + wireSettingsPrefs 绑定 + DOMContentLoaded 调用；api 增 `getStorageInfo/cleanupMessages/vacuumStorage/clearLogs/moveStorage` 五桥；`renderSettingsStorage`（占用+位置两组）+ `refreshStorageInfo/_fillStorage/_fmtBytes/_fmtTs` + `onStorageOp/pickCleanupDays/onMoveStorage`；renderActiveView 调用 |
| `src/relay/web/index.html` | nav 子菜单 `data-settings-sub="storage"`（存储管理）+ `settings-storage` section + `#card-settings-storage-body`；版本号 query bump：styles `?v=20260822-21`、app.js `?v=20260822-18` |
| `src/relay/web/styles-20260817.css` | `.settings-storage-actions` / `.settings-storage-path` / `.seg-cleanup .seg-btn` / `.sc-clear-row` |
| `src/relay/web/live_panel.html` | 版本号 query bump：styles `?v=20260822-21`（共享样式表缓存） |
| `src/relay/db.py` | `Database` 增 `delete_requests_before` / `delete_all_requests` / `vacuum`（`_lock` 内串行化） |
| `src/relay/passthrough/db.py` | `PassthroughDatabase` 增 `delete_before` / `delete_all` / `vacuum` |
| `src/relay/routers/api.py` | 增 `POST /api/storage/cleanup-messages` / `vacuum` / `clear-logs`；import `time` |
| `src/relay/gui.py` | `Api` 增 `get_storage_info`（本地只读算）/ `cleanup_messages` / `vacuum_storage` / `clear_logs`（`_storage_post` HTTP→relay）/ `move_storage`（写 .env + 搬文件 + 回滚，不自动重启）；import `shutil` |

### 状态流

- **语言**：设置页 → 界面与偏好 → 外观 → 语言 切 English → 设置页全部标题/hint 变
  英文、`<html lang>` 变 en、nav「存储管理」→ Storage；切回中文还原；刷新后保持
  （localStorage）。
- **存储空间管理**：存储管理 section 显示四类占用（大小/条数/时间范围/路径）；
  `清理消息记录`（保留 7/30/90/180 天或清空全部）→ 显示删除条数；`压缩数据库` →
  VACUUM；`清空日志` → 删 `.relay-logs/` 常规文件；`刷新` 重拉。
- **存储位置管理**：显示 relay.db / passthrough.db / 日志目录 / upstreams.json 路径；
  `修改` relay.db / passthrough.db → `prompt()` 输入新路径 → 写 .env + 搬文件；
  中继运行中文件被锁 → 提示先停止中继（.env 回滚）。

### 验证

- DB 单测（usage-stats env）：按天删除只删旧行且 messages 级联、delete_all、vacuum
  全通过。
- `python -m py_compile` gui.py / db.py / passthrough/db.py / routers/api.py 通过；
  `node --check app.js` 通过。
- headless stub-bridge 探针：语言 seg 渲染、存储两组渲染（ops=4 / moves=2）、
  `get_storage_info` 填充（`1.2 MB · 350 请求 · 900 条消息 · 时间范围`）、路径显示、
  切 English 后 group-label=Appearance / nav-sub=Storage / html-lang=en /
  lang-en-class=true、切回 zh 还原 —— 全通过；探针文件已清理。
- 刷新 GUI：设置页 → 存储管理：看到占用与路径；操作各项；外观 → 语言切换可见生效。
