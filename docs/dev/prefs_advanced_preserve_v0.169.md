# 设置页高级折叠组保留用户值（v0.169）开发文档

## 1. 用户的初始指令

> 那些高级设置（关闭后会折叠掉部分设置的），关掉之后，如果被他折叠的设置有更改，不要给他重置回默认了哦

### 场景拆解

- **目标**：「等待时间高级设置」「样式高级设置」两个开关折叠的 wheel-picker / number input / checkbox / seg 三档。
- **诉求**：用户改过的嵌套值不能被「重置回默认」。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 在「离开设置 → 重新进入」时，wheel-picker / 嵌套 input / checkbox / seg 不应回到 snap 默认值 | 用户反馈 |
| B | 仅看高级折叠组内元素；其它顶层 input 每次渲染都从 snap/bridge 实时拉最新 | 设计取舍 |

### 隐含但需要确认的点

- 触发场景：用户改值后立即离开设置页（防抖 350ms + 500ms 轮询窗口内），下次进入时 `snap` 仍是旧值，wheel-picker 用 snap 初始化 → 显示「默认」。
- 修复策略选择：snapshot 旧 DOM 兜底 vs flush 防抖 vs 强制 snap 刷新 —— 选 snapshot 最稳，不引入额外网络/时序假设。

---

## 3. 分析需求后得出的开发路径

### 核心方案

**Snapshot 旧 DOM 兜底**：`renderSettingsPrefs` 进入时（`body.innerHTML = ...` 之前）抓旧 DOM 上 wheel-picker 中行（`is-cur` 行 textContent = 当前 `_v`）、嵌套 number input、嵌套 checkbox、list-font seg 三档 active 值 → 模块级 `_prefsDirtySnapshot`。`mountTimeoutWheel` / `maxInput` 初始值用 `wpDef` 工具函数：快照 > snap > 默认常量。`initTimeoutWheel` / `refreshPrefsDynamic` 里嵌套 input / checkbox / seg 同样先查快照再决定是否覆盖。

### 开发路径

```
#1  app.js: _prefsDirtySnapshot 模块级变量                          (A)
#2  app.js: _snapshotPrefsValues(body) 抓 wheel/input/checkbox/seg  (A)
#3  app.js: renderSettingsPrefs 进入即 snapshot                    (A)
#4  app.js: mountTimeoutWheel + maxInput 用 wpDef                  (A)
#5  app.js: initTimeoutWheel 加 dataKey 参数，跳过覆盖             (A)
#6  app.js: refreshPrefsDynamic 内嵌套 input/checkbox/seg 同样处理 (A)
#7  index.html: cache ?v=20260825-03 → ?v=20260825-04              (cache)
```

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 兜底时机 | 重渲染 snapshot 旧 DOM | 不引入 flush / 强制刷新副作用，简单可靠 |
| 兜底对象 | wheel-picker 中行 textContent / nested input .value / checkbox .checked / seg .active | 三种典型 UI 元素全覆盖 |
| 优先级 | 快照 > snap > 默认 | snap 仍是权威（用户改动落库后下次 snap 自动跟上），快照只兜 snap 滞后 |
| 跳过覆盖 | initTimeoutWheel 检测 dataKey 有快照值则 return | 避免 mountTimeoutWheel 已用快照、又被 `__wpSet(snap, true)` 刷回 |
| 范围限定 | 仅高级折叠组 + wheel-picker | 顶层 input 每次渲染实时拉，无需覆盖 |

---

## 4. 实现中遇到的问题

### 问题 1：snapshot key 不稳定

初版用 `el.className` 作 key，HTML 重渲染后 className 相同但语义不同（嵌套在 advanced panel 内的 `.prefs-live-panel-tools-cap-input` vs 顶层同名）→ 顶层会被错误覆盖。

**解法**：wheel-picker 用 `data-timeout` / `data-max`（项目本就为组件设计的稳定属性）；嵌套 input/checkbox 用 `#prefs-live-panel-style-advanced-panel` 限定查询范围，类名仅在该 scope 内唯一即可。

### 问题 2：snapshot 被 initTimeoutWheel 反向覆盖

`mountTimeoutWheel` 用快照创建 wheel-picker → 紧接着 `initTimeoutWheel` 又用 `__wpSet(fromSnap, true)` 静默回写 → 即便 `fromSnap` 与 `_v` 相同因 silent set 内部 `if (v === _v) return` 跳过渲染，但若有差异会反向刷回。

**解法**：`initTimeoutWheel` 加 `dataKey` 参数，若 `_prefsDirtySnapshot["wp:" + dataKey]` 是数字则 `return`（跳过整段回写路径）。`maxInput.__wpSet` 同样加 `if (dirty) return` 守卫。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| 嵌套值被重置 | snapshot 旧 DOM → wpDef / input 直读 / checkbox 直读 / seg 直读优先级高于 snap | app.js |
| 快照被反向覆盖 | initTimeoutWheel / maxInput 加 dirty guard | app.js |

---

## 6. 是否完全遵循规划路径开发

**完全遵循**，无方案级偏离。

- 实施策略 = snapshot 旧 DOM，与规划一致。
- 兜底对象限定 = wheel-picker + 高级折叠组嵌套 input/checkbox/seg，与规划一致。
- 优先级 = 快照 > snap > 默认，与规划一致。

### 补充决策（规划未明说）

- **snapshot 用稳定 key（`data-timeout` / `data-max`）**：规划只说"抓旧 DOM"，未指定 key；实测用 className 不稳（同名元素跨 scope），改用 data-attribute + scope 限定。
- **initTimeoutWheel 加 dirty guard 而非改 mountTimeoutWheel 顺序**：规划未明说；但 mountTimeoutWheel 先跑、initTimeoutWheel 后跑，若 initTimeoutWheel 不挡会反向刷回 → 加 guard 比改顺序更小侵入。

### 重大调整：无。

---

## 7. 最终实现点

### `src/relay/web/app.js`

1. **`_prefsDirtySnapshot`**：模块级缓存，跨渲染保留。重渲染后被消费即失效（下一轮重渲会重新 snapshot）。
2. **`_snapshotPrefsValues(body)`**：
   - 抓所有 `.wheel-picker` 中行 textContent → `snap["wp:" + data-timeout/max]` = 整数。
   - 抓 `#prefs-live-panel-style-advanced-panel` 内 number input → `snap["num:" + className]` = float。
   - 抓 `#prefs-live-panel-style-advanced-panel` 内 checkbox → `snap["chk:" + className]` = bool。
   - 抓 `#prefs-live-panel-style-advanced-panel .seg-list-font .seg-btn.active` → `snap["seg:list-font"]` = 档位字符串。
3. **`renderSettingsPrefs`**：进入即 `_prefsDirtySnapshot = _snapshotPrefsValues(body)` 再写 `body.innerHTML`。
4. **`wpDef(dataKey, snapKey, fallback)`**：快照 > snap > 默认。`mountTimeoutWheel` 5 处超时 + maxInput 全部用它。
5. **`initTimeoutWheel(sel, getter, fallback, def, dataKey)`**：新增 `dataKey`，若有快照值则跳过整段回写。
6. **`refreshPrefsDynamic`**：`tools-cap-input` / `ep-list-vh-input` / `min-cols-input` / `tools-always` / `no-panel-frame` / `list-font` seg 在回写前先查快照。

### `src/relay/web/index.html`

7. **cache**：`app.js?v=20260825-03` → `?v=20260825-04`。

### ⚠ 关键约束（底层，勿再踩）

- **snapshot 必须在 `body.innerHTML = ...` 之前抓**，否则旧 DOM 已被销毁。
- **scope 限定**：嵌套 input/checkbox 必须用 `#prefs-live-panel-style-advanced-panel` 限定，否则同名顶层元素会被误覆盖。
- **wheel-picker key 必须用 `data-timeout` / `data-max`**（稳定属性），不能用 `className`（重渲染后跨 scope 不稳定）。
- **优先级**：snap 仍是权威。snap 有最新值时仍用 snap（用户改动落库后自动跟上）；快照仅在 snap 滞后于用户改动时兜底。

### 行为验收清单（手动测试项）

- [ ] 开高级「等待时间」组 → 改 wheel-picker（不动 350ms）→ 切到「实时」tab → 立即切回设置 → wheel 值仍是用户改的值
- [ ] 同上操作，但等 1s 再切 tab → wheel 值仍是用户改的值（snap 已跟上）
- [ ] 同上「样式」组：改 `tools-cap` / `tools-always` / `min-cols` / `no-panel-frame` / `list-font` → 切 tab → 切回 → 值仍是用户改的
- [ ] 重启 GUI → 高级组开关状态保留（prefs.livePanelTimeoutAdvanced / prefs.livePanelStyleAdvanced localStorage）
- [ ] 首次进入设置 → 嵌套值显示 snap/bridge 默认（无旧 DOM 快照，正常）
- [ ] `node --check` app.js 通过

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/app.js` | 改（_prefsDirtySnapshot / _snapshotPrefsValues / renderSettingsPrefs 入口 snapshot / wpDef / initTimeoutWheel + maxInput dirty guard / refreshPrefsDynamic 内嵌套 input/checkbox/seg 优先快照） |
| `src/relay/web/index.html` | 改（JS cache `?v=20260825-03` → `?v=20260825-04`） |
