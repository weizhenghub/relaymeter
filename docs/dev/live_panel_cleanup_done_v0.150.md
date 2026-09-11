# 新容器出现清理 done 容器（v0.150）开发文档

## 1. 用户的初始指令

> 只要出现了新容器，除了保持原处理逻辑，再额外增加“清理”机制，此时所有“完成”“出错”状态的容器都清理掉。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 新容器出现（新 rid 到达）时触发一次清理 | 指令（触发时机） |
| B | 清理对象 = 所有「完成」和「出错」状态的容器 | 指令（清理范围） |
| C | 保持原处理逻辑不变（v0.144 的跨 rid 复用 / v0.141 的同 rid 复用 / 新建） | 指令「除了保持原处理逻辑」 |
| D | 清理后容器立即移除（不等后端 10s 销毁计时器） | 指令「此时…都清理掉」（即时语义） |

### 隐含但需要确认的点（设计自决）

- **「完成」与「出错」的判定**：`setRidBadge` 里两者都写 `badgePhase = "done"`，区别仅在 `badgeError` 是否非空（`完成` = done + 无 error，`出错` = done + 有 error）。所以清理判定用 `badgePhase === "done"` 即可同时覆盖两者。
- **触发点**：`ensureContainer(rid, ev)` 里 `_ridEls[rid]` 不存在（新 rid 第一次到达）即为「新容器出现」；同 rid 后续事件（`_ridEls[rid]` 已存在）不算，不触发清理。
- **复用 vs 清理的边界**：`findReusableDone()` 会复用**第一个** done 容器（含「出错」，因为出错也是 badgePhase="done"）。复用后该容器被 rebind 成 streaming（`setRidBadge(rec, "streaming", null)`），不再在清理范围内；**剩余**的 done 容器才是清理对象。
- **清理动作**：复用现成的 `window.relayLiveClear(rid)`（完整清理 DOM + `_ridEls`/`_ridOrder`/`_ridIndex`/`_epRids` + renderEpTop/List + scheduleLayout），DRY 不重复实现。后端到点的销毁计时器再调 `relayLiveClear(旧rid)` 时因 rid 已删 → no-op（与 v0.144 的旧 rid no-op 同一机制）。
- **streaming 容器不受影响**：清理只针对 `badgePhase === "done"`，进行中的容器（streaming/calling/uploading）不被误清。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位容器管理链路

```
relayLiveEvent(ev) → applyRid(rid, ev) → ensureContainer(rid, ev)
  ├─ _ridEls[rid] 已存在 → return（同 rid 后续事件，不触发清理）
  ├─ findReusableDone() 命中 → rebindContainer(reuse, rid, ev)（复用 done 容器）
  └─ 否则 → buildContainer 新建
```

容器状态（`setRidBadge`）：

| badgePhase | badgeError | 徽标 attr | 用户看到 |
|---|---|---|---|
| "streaming" | "" | streaming | 流式中 |
| "done" | "" | done | 完成 |
| "done" | 非空 | error | 出错 |

### 第二阶段：方案设计

在 `ensureContainer` 里，复用 / 新建完成后统一调用 `cleanupDoneContainers(rid)`：

```js
const reuse = findReusableDone();
let rec;
if (reuse) {
  rec = rebindContainer(reuse, rid, ev);   // 复用的 done 容器已 rebind 成 streaming
} else {
  // 新建容器（badgePhase 初始 "streaming"）
  ...
}
cleanupDoneContainers(rid);   // 清理剩余 done（复用后残留 / 新建后残留）
return rec;
```

`cleanupDoneContainers(excludeRid)`：遍历 `_ridOrder`，收集 `badgePhase === "done"` 且 `rid !== excludeRid` 的 rid，逐个 `window.relayLiveClear(r)`。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `ensureContainer` 重构：提前 return 改为统一末尾清理 | 无 |
| 2 | 新增 `cleanupDoneContainers(excludeRid)` | #1 |
| 3 | `live_panel.html` bump cache stamp | #1 |
| 4 | jsdom harness `cleanup_done.js`（4 场景 14 断言） | #2 |
| 5 | 更新 `concurrent5.js` 场景 2 断言（行为变化） | #2 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 触发位置 | `ensureContainer` 末尾（复用 / 新建统一路径） | 两种「新容器出现」都覆盖；且此时复用容器已被 rebind 成 streaming，不会被误清 |
| 清理判定 | `badgePhase === "done"` | 「完成」「出错」都是 badgePhase="done"，一个条件覆盖两者 |
| 排除当前 rid | `cleanupDoneContainers(excludeRid)` 参数 | 刚 rebind / 新建的容器是 streaming，本就不会被命中；排除参数是防御性 + 语义清晰 |
| 清理动作 | 复用 `window.relayLiveClear` | DRY；它已处理全部清理细节，且后端计时器到点后 no-op 安全 |
| 后端计时器 | 不动（仍 10s 调度） | 前端提前清理后，后端到点 `relayLiveClear(旧rid)` 因 rid 已删 → no-op，无需改后端 |
| 收集后批量清 | 先 `doneRids.push` 再逐个 clear | 避免边遍历 `_ridOrder` 边 splice 导致索引错乱 |

---

## 4. 实现中遇到的问题

### 问题 1：`ensureContainer` 复用分支提前 `return`，无法在末尾统一清理

原代码 `if (reuse) return rebindContainer(reuse, rid, ev);` 直接返回，新建分支末尾才 `return rec`。要在两种路径都加清理，要么在两个分支各加一句（重复），要么重构。

**解法**：把提前 `return` 改成 `let rec; if (reuse) { rec = rebindContainer(...); } else { ...新建... }`，末尾统一 `cleanupDoneContainers(rid); return rec;`。复用分支的注释（v0.144）原样保留，不丢语义。

### 问题 2：`relayLiveClear` 在 IIFE + 严格模式下，裸标识符不可见

文件是 `(function () { "use strict"; ... })()` 包裹，`window.relayLiveClear = function relayLiveClear(rid) {...}` 是**命名函数表达式**——函数名 `relayLiveClear` 只在其自身作用域内可见，不会泄漏到 IIFE 外层。所以 `cleanupDoneContainers` 里裸调 `relayLiveClear(r)` 会 ReferenceError。

**解法**：用 `window.relayLiveClear(r)` 调用（它明确挂在 window 上，且 Python 也是经 `window.relayLiveClear(...)` evaluate_js 调用，语义一致）。

### 问题 3：`concurrent5.js` 场景 2 旧断言与新清理语义冲突

旧断言「第 6 个复用 done → 仍 5 容器」（期望 5 全 done 后第 6 个复用 1 个、其余 4 个继续挂），正是 v0.150 要消灭的行为。跑出 2 个 FAIL：

```
FAIL  第 6 个复用 done → 仍 5  [got=1]
FAIL  容器 = r2-r6（r1 被 r6 复用）  [rids=r6]
```

**解法**：这是**预期行为变化**（5 全 done → 第 6 个复用 r1 + 清理 r2-r5 → 只剩 r6），不是 bug。更新 `concurrent5.js` 场景 2 断言为「→ 1 容器」「容器 = r6」。场景 3（5 并发中仅 1 done，其余 4 streaming）仍 PASS，证明 streaming 不被误清。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 复用分支提前 return | 重构为 `let rec` + if/else，末尾统一清理 | live_panel.js |
| #2 relayLiveClear 不可裸调 | 用 `window.relayLiveClear(r)` | live_panel.js |
| #3 concurrent5 旧断言冲突 | 更新场景 2 断言匹配新清理语义 | concurrent5.js（Temp harness） |

**核心代码（live_panel.js，v0.150）：**

```js
// ensureContainer 末尾
cleanupDoneContainers(rid);
return rec;

// 新增函数
function cleanupDoneContainers(excludeRid) {
  const doneRids = [];
  for (let i = 0; i < _ridOrder.length; i += 1) {
    const r = _ridOrder[i];
    const rec = _ridEls[r];
    if (rec && r !== excludeRid && rec.badgePhase === "done") doneRids.push(r);
  }
  doneRids.forEach((r) => window.relayLiveClear(r));
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **触发时机**：新 rid 到达（`_ridEls[rid]` 不存在）→ 复用/新建后统一清理。
- **清理范围**：`badgePhase === "done"`（「完成」「出错」全覆盖）。
- **保持原逻辑**：v0.144 复用 / 新建 / streaming 容器判断全部不动，只在末尾追加清理。
- **复用现成 relayLiveClear**：DRY，后端计时器 no-op 安全。

### 偏离之处：

- **(a) `concurrent5.js` 断言更新**：规划没提要改既有 harness，但新语义必然改变「5 全 done 后第 6 个复用」的容器数（5 → 1）。这是**行为变化导致的断言更新**，不是规划偏离。

### 重大调整：无。

---

## 7. 最终实现点

### 前端

1. **`src/relay/web/live_panel.js`**：
   - `ensureContainer(rid, ev)` 重构：提前 `return` 改为 `let rec` + if/else 结构，末尾统一 `cleanupDoneContainers(rid); return rec;`（复用分支 v0.144 注释原样保留）。
   - 新增 `cleanupDoneContainers(excludeRid)`：遍历 `_ridOrder`，收集 `badgePhase === "done"` 且非当前 rid 的容器，逐个 `window.relayLiveClear(r)`。

2. **`src/relay/web/live_panel.html`**：cache stamp `live_panel.js?v=20260824-01 → 20260824-02`。

### 测试（jsdom harness，`C:\Users\weizheng\AppData\Local\Temp\lp_harness\`）

3. **`cleanup_done.js`**（新建，14 项断言全过）：
   - S1：3 个 done 堆积 → 新容器复用第 1 个 + 清剩余 2 个 → 只剩 1 容器（data-rid=D）
   - S2：出错 + 完成混合堆积 → 新容器出现全清理 → 只剩 1（徽标复位 streaming）
   - S3：A streaming + B done → C 复用 B，A 不被误清 → 2 容器（A,C）
   - S4：单 done 复用（v0.144 既有）→ 仍 1 容器，不破坏

4. **`concurrent5.js`**（更新场景 2 断言）：「第 6 个复用 done → 仍 5」改为「→ 1 容器」；「容器 = r2-r6」改为「容器 = r6」。场景 3（streaming 不误清）不动仍 PASS。

5. **回归**：`reuse_crossrid` / `concurrent5` / `sticky_remount` / `upgrade`(20) / `empty_check`(21) / `firstfit`(10) / `tools_clear`(8) / `endpoint`(25) / `clear_check`(10) / `repro` / `repro_extended` 全过。

### 行为验收清单（手动测试项）

- [ ] 多个并发请求全部 done（完成/出错混合）后，新请求到达 → 复用 1 个 + 立即清掉其余 done 容器（不再等 10s）
- [ ] 「出错」容器（如连接中断）同样被清理
- [ ] 进行中的 streaming 容器不被误清
- [ ] 单 done 复用（v0.144）行为不变：顺序请求仍只 1 容器
- [ ] 后端 10s 销毁计时器到点后无 JS error（relayLiveClear 对已删 rid 是 no-op）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.js` | 改（ensureContainer 重构 + cleanupDoneContainers 新增） |
| `src/relay/web/live_panel.html` | 改（cache stamp bump） |
| `C:\Users\weizheng\AppData\Local\Temp\lp_harness\cleanup_done.js` | 新建（jsdom harness，14 断言） |
| `C:\Users\weizheng\AppData\Local\Temp\lp_harness\concurrent5.js` | 改（场景 2 断言更新） |
| `PROGRESS.md` | 改（顶部 +v0.150 记录） |
| `docs/CHANGELOG.txt` | 改（顶部 +v0.150 块） |
| `docs/dev/live_panel_cleanup_done_v0.150.md` | 新建（本文件） |
