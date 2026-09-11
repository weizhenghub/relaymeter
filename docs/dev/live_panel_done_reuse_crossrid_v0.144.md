# done 容器跨 rid 复用补完（v0.144）开发文档

## 1. 用户的初始指令

> 同一个会话却出现了并发才有的多容器现象，没有并发理应不应该出现多容器

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 单会话（无并发）不应出现多个容器 | 指令（症状） |
| B | 「多容器」只在真并发时出现 | 指令（规范） |

### 隐含但需要确认的点（设计自决）

- **根因定位**：v0.141「done 容器立刻复用」只实现了**同 rid** 复用（后端 `assign(rid)` 同 rid 重来会 `_cancel_destroy_timer` 取消 10s 销毁 + 前端 `resetRidContent` 清内容）。但中继每个上游请求 `request_id = uuid4().hex` 全新 —— 一个会话的两段顺序请求是两个**不同** rid。新 rid 的 `ensureContainer` 在 `_ridEls` 里查不到 → **新建容器**；旧 done 容器还要挂满 `destroy_after_done_sec`（10s）。于是「无并发却看到两个容器」。
- **v0.141 文档的偏差**：v0.141 文档第 4 节问题 3 / 第 6 节偏离 (a) 宣称「跨 rid 也复用：done 容器清完后下次任何 rid 事件 setText 覆盖」—— 实际代码**没有做 rebind**，只是把 done 容器留成独立 layout item。跨 rid 时旧容器仍占位，新 rid 另起新卡。文档说法是**未兑现的设想**，v0.144 才真正补完。
- **修复语义**：新 rid 到达且存在 done 容器时，**直接 rebind 该容器到新 rid**（改 data-rid / 键 / 序号过户 / meta / 徽标复位），而不是新建 + 让旧卡挂 10s。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：复现

jsdom 场景：A delta → A done → B delta（顺序，无并发）。断言 `.lp-rid` 数量。

**现状**：A done 后 1 个；B delta 后 **2 个**（A 挂满 10s + B 新建）→ FAIL。

### 第二阶段：方案设计

`ensureContainer(rid, ev)` 在 `_ridEls[rid]` 未命中时，先找 `findReusableDone()`：

```js
function findReusableDone() {
  for (let i = 0; i < _ridOrder.length; i += 1) {
    const rec = _ridEls[_ridOrder[i]];
    if (rec && rec.badgePhase === "done") return rec;
  }
  return null;
}
```

找到就 `rebindContainer(reuse, rid, ev)`，否则走原新建逻辑。

`rebindContainer` 要点：
1. 容器 DOM 节点**不换**，只改身份：`rec.rid = rid`、`rec.el.dataset.rid = rid`、`_ridEls[oldRid]` → `_ridEls[rid]`。
2. `_ridOrder` 里 oldRid 原地替换为 rid（到达序 / 同列内顺序稳定）。
3. `_ridIndex` 序号**过户**：新 rid 沿用旧 rid 的序号（可见 ① ② ③ 不变）。
4. `resetRidContent` 清残留 + `rThinking/rStream.reset()` 复位速率 + `updateRidMeta` + `setRidBadge("streaming")`。
5. endpoint 并发列表 `_epRids.delete(oldRid)` + `renderEpList()`（新 rid 已由 applyEndpoint 加入，旧 done 请求不再占行）。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `findReusableDone` / `rebindContainer` helper + `ensureContainer` 接入 | 无 |
| 2 | `resetRidContent` 顺带清 `dataset.raw`（复用后 token bump 从头记） | #1 |
| 3 | jsdom `reuse_crossrid.js`（顺序复用 + 并发不误并 + rebind 后身份/徽标/endpoint） | #1 |
| 4 | 资源版本 bump | #1 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 复用目标 | 到达序第一个 done 容器 | 最旧 done 优先，序号序列紧凑 |
| rebind vs 新建 | rebind（换身份不换 DOM） | 直接兑现 v0.141「立刻复用」；序号/位置稳定 |
| 并发语义 | 只复用 done，绝不碰 active | 真并发仍各建容器，不误并 |
| 序号 | 过户（沿用旧 rid 序号） | 容器没换，可见序号不变 |
| endpoint 列表 | 移除旧 done 行 | 容器已 rebind，列表同步不再显示旧请求 |
| 后端 | 不动 | 10s 销毁 timer 照常；rebind 后 `relayLiveClear(oldRid)` 在 `_ridEls` 找不到旧 rid → no-op 安全 |
| kind 换类 | 沿用首 owner 的 kind，不降级 | 与「中途不换类」一致；puretext 复用 thinkstream 容器仅显示「思考（暂无）」 |

---

## 4. 实现中遇到的问题

### 问题 1：regression 套件 tools_clear.js 红（v0.137 起的过时 harness）

**症状**：`tools_clear.js` `doc.querySelector("#lp-tools-wrap")` 返回 null → `wrap.querySelectorAll` 抛 TypeError。该 harness 还是 v0.134 时代写法：在 `#lp-tools-wrap` 里找 `.live-panel-tool`。

**解法**：v0.137 起 tool 卡是独立 layout item，`placeColumns` 把 `#lp-tools-wrap` 整个从 DOM 移除。更新为 `.lp-col .live-panel-tool`；T6「容器隐藏」改为「全文档无 tool 卡」。**harness 过时，非本次改动回归**（与上批修 repro/upgrade/repro_extended 同类）。

### 问题 2：`dataset.raw` 残留导致复用后 token bump 失灵

**症状**：rebind 后 `applyUsageTo` 读 `refs.tok[k].dataset.raw` 当 prev —— 残留旧值，新 token 值 < 旧 raw 时 `val > prev` 不成立，数字变化不闪。

**解法**：`resetRidContent` 在把 tok 值写 "0" 的同时 `delete refs.tok[k].dataset.raw`，复用后从 0 起记 bump。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| 跨 rid 多容器 | `ensureContainer` 未命中时先 rebind done 容器（`findReusableDone` + `rebindContainer`） | live_panel.js |
| token bump 残留 | `resetRidContent` 清 `dataset.raw` | live_panel.js |
| 过时 harness | tools_clear.js 选择器改 `.lp-col .live-panel-tool` | harness |

**最终 ensureContainer（live_panel.js）：**

```js
function ensureContainer(rid, ev) {
  if (_ridEls[rid]) return _ridEls[rid];
  // v0.144：跨 rid 复用 —— 新请求（新 rid）到达时若已有 done 容器，直接
  // 复用它（rebind 到新 rid），不再新建。v0.141 只做了「同 rid 复用」
  // （靠后端 _cancel_destroy_timer）；跨 rid 时旧 done 容器要挂满
  // destroy_after_done_sec，单会话顺序请求也会看到两个容器（无并发却
  // 多容器）。rebind 后旧 rid 的后端清除定时器到点 relayLiveClear(oldRid)
  // 在 _ridEls 里找不到旧 rid → no-op，安全。
  const reuse = findReusableDone();
  if (reuse) return rebindContainer(reuse, rid, ev);
  const kind = ev.thinking_text ? "thinkstream" : "puretext";
  // ... 原新建逻辑不变
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **rebind 优先**：新 rid 未命中且存在 done 容器 → 复用该容器（换身份不换 DOM）。
- **只复用 done**：并发时 active 容器不受影响，真并发仍各建容器。
- **序号过户**：可见序号（① ② ③）保持，到达序原地替换。
- **endpoint 列表同步**：旧 done 请求移除，只留新请求一行。
- **后端不动**：10s 销毁 timer 语义保留，rebind 后旧 rid clear 变 no-op。

### 偏离之处：

- **(a) 顺带修 tools_clear.js 过时选择器**：plan 没提，但不修则回归套件一直红（v0.137 起已失效）。**清理自决**。
- **(b) `resetRidContent` 清 `dataset.raw`**：plan 只说 rebind 流程，但不清 raw 会让复用后 token bump 失效（新值 < 旧 raw 不闪）。**细节自决**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

无改动（panel_pool release 仍挂 10s 销毁 timer；rebind 后旧 rid 的 `relayLiveClear` 在前端为 no-op）。

### 前端

1. **`src/relay/web/live_panel.js`**：
   - `ensureContainer(rid, ev)`：`_ridEls[rid]` 未命中 → 先 `findReusableDone()`，命中则 `rebindContainer(reuse, rid, ev)`。
   - 新增 `findReusableDone()`：按 `_ridOrder` 找第一个 `badgePhase === "done"` 的容器。
   - 新增 `rebindContainer(rec, rid, ev)`：data-rid / `_ridEls` 键 / `_ridOrder` 位 / `_ridIndex` 序号过户 / `resetRidContent` + 速率复位 / `updateRidMeta` / `setRidBadge("streaming")` / `_epRids.delete(oldRid)` + `renderEpList()`。
   - `resetRidContent`：tok 复位 "0" 时 `delete dataset.raw`。

### 资源版本

- live_panel.js `?v=20260823-61 → ?v=20260823-62`。
- live_panel.css / styles-20260817.css / app.js 不动。

### 测试

- **`reuse_crossrid.js`（新，11 项）**：S1 顺序两段请求始终 1 容器 / rebind 后 data-rid=B / 徽标复位 streaming / endpoint 列表剩新行 / 连续三轮顺序复用 / S2 真并发 2 容器 + done 后复用仍 2（Y,Z）—— 全过。
- **回归**：repro / repro_extended / upgrade（20）/ clear_check（10）/ endpoint（25）/ firstfit（10）/ empty_check（21）全过。tools_clear（8）选择器更新后全过。

### 行为验收清单（手动测试项）

- [ ] 一个会话跑完（done），紧接着发下一个请求（无并发）—— 侧栏始终 1 个容器，不再叠出 2 个
- [ ] 真并发两个请求 —— 2 个容器各显各的
- [ ] done 容器被复用后，徽标从「完成」立刻切回「流式中」，正文/思考清空并写入新内容
- [ ] 复用后 token 计数从 0 起记，数字变化正常闪烁
- [ ] 10s 内无新请求：done 容器保持「完成」到点销毁（原语义不变）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.js` | 改（ensureContainer 跨 rid 复用 + findReusableDone/rebindContainer + resetRidContent 清 raw） |
| `src/relay/web/live_panel.html` | 改（live_panel.js 资源版本 bump） |
| `docs/dev/live_panel_done_reuse_crossrid_v0.144.md` | 新建 |
