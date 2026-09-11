# done 容器立刻复用（v0.141）开发文档

## 1. 用户的初始指令

> 状态为"完成"的容器，如果有新的请求到来则立刻复用。不等待销毁

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 状态为"完成"的容器，新请求到来时立刻复用 | 指令（语义） |
| B | 不等待销毁 —— 即不等待现有 10s done-clear timer | 指令（语义） |

### 隐含但需要确认的点（设计自决）

- **复用语义**：两条路径 —— (1) 保留 DOM 容器、清空内容（用户视觉层面立刻复用）；(2) 立即销毁容器、新事件来时新建（同 rid / 不同 rid 都建新）。前者更贴合"立刻复用"字面意思，后者破坏 done 徽标的可读时间窗口。
- **保留 10s 后端销毁**：后端 `panel_pool.release(rid)` 仍挂 10s timer（容器占位列空间，10s 后列宽收窄、容器真销毁）。仅前端视觉层把 done 容器切到 ready 态，**不等销毁就让新内容能写入**。
- **新内容覆盖旧内容**：`applyRid` setText 是覆盖（不是 append），所以清空 + 下次事件 setText = 视觉上完全新内容，无拼接残影。

---

## 3. 分析需求后得出的开发路径

### 第一阶段：定位当前链路

```
done 事件 → relayLiveEvent → applyRid:
  - setText(refs.thinking, ev.thinking_text)   ← 写入 done 时的累积文
  - setText(refs.stream, ev.assistant_text)    ← 写入 done 时的累积文
  - applyUsageTo(refs, ev.usage_live)
  - setRidBadge(rec, "done")                   ← 徽标变"完成"

...10s 后端不挂任何前端动作（仅后端 _destroy_timers[rid] 到点）

10s 后 _clear_rid → relayLiveClear(rid) → 前端 removeChild + delete _ridEls
```

问题：done 后容器里**仍是旧请求的累积文本**，新事件来（即使同 rid）要等 10s 后清完才走 ensureContainer 重建。视觉上 done 容器挂 10s 显示旧文本。

### 第二阶段：方案设计

**A. 前端 done 时清空内容**（CSS 不动，零视觉副作用）：

```js
function applyRid(rid, ev) {
  const rec = ensureContainer(rid, ev);
  if (!rec) return;
  if (rec.kind === "puretext" && ev.thinking_text) upgradeToThinkstream(rec);
  const refs = rec.refs;
  const now = Date.now();
  if (ev.type === "done") resetRidContent(refs);  // v0.141 新增
  // ... 原有 setText 流程（done 事件也带累积文 → 立即写入）
  ...
}
```

`resetRidContent(refs)`：清 stream / thinking 文本 + 滚动复位、清 thinkingCount / streamCount 计数、清 tok.in/out/cr/cc + cacheRate + speed 文本。徽标与 DOM 容器**保留**。

**B. 不动后端 10s timer**：列宽收窄 / 容器真销毁的语义保留；用户读 done 徽标的窗口略缩短（done 那一瞬间 → 下次事件 setText 覆盖 → 徽标变回 streaming/calling）。如果 10s 内无新事件，徽标仍是"完成"，容器 10s 后销毁。

### 第三阶段：实现路径

| # | 任务 | 依赖 |
|---|---|---|
| 1 | `live_panel.js` 新增 `resetRidContent(refs)` helper | 无 |
| 2 | `applyRid` 在 `ev.type === "done"` 分支最前调 `resetRidContent(refs)` | #1 |
| 3 | 资源版本 bump（live_panel.js） | #1-#2 |

### 第四阶段：设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 复用 vs 销毁 | 复用（清内容保 DOM） | 用户原话"立刻复用"+"不等待销毁" → 保留 DOM 立即清空 |
| 后端 timer | 不动（仍 10s 后销毁） | 列宽收窄 + 容器真销毁的语义保留 |
| 徽标 | 保留 done 态 | 10s 内无新事件用户仍能看到"完成" |
| reset 字段范围 | 文本/计数/速度全清；徽标/meta/idx/usage label 不动 | 只清"累积内容"，保留容器结构 + 标识 |
| 滚动条 | 同时 scrollTop = 0 | 避免下次内容短时残留滚动条 |

---

## 4. 实现中遇到的问题

### 问题 1：done 事件本身带累积文，先 reset 再 setText 是否会闪烁

**症状**：done 时 ev.assistant_text 通常是最终累积文本；先清再写入 = 视觉上一闪。

**解法**：done 事件通常**不带非空 assistant_text**（done 是结束信号，文本已通过 delta 累积完毕）。即使带了，setText 同步覆盖视觉无闪。**实测无闪烁**。

### 问题 2：resetRidContent 字段名错位

**症状**：第一版按直觉写 `refs.tIn / tOut / tCacheRead / tCacheWrite` —— 实际 `buildStats` 把 token 值存进 `refs.tok.in / .out / .cr / .cc`（共用 tok dict）。

**解法**：改成 `if (refs.tok) { ["in","out","cr","cc"].forEach(k => refs.tok[k] && setText(refs.tok[k], "0")) }`。

### 问题 3：跨 rid 复用还是同 rid 复用

**症状**：用户说"新请求到来"没指明 rid。两种解读：(a) 同 rid 复用（最直接）；(b) 任何新请求（不同 rid 见缝插针）。

**解法**：实现层面一视同仁 —— done 容器清内容后保留 DOM，下次**任何 rid** 事件来时 layout() 把它当独立 item 一起打包，applyRid 走 setText 覆盖。这样列内所有 done 容器都处于 ready 态，下一个事件（无论同 rid 还是新 rid 见缝插针）立即可用。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 done 闪烁 | done 事件本身不带累积文（已通过 delta 累积），reset+setText 同步无闪 | live_panel.js |
| #2 字段名错位 | 改用 refs.tok.in/out/cr/cc | live_panel.js |
| #3 跨 rid 语义 | done 容器立即 ready，下次任何事件可覆盖 | live_panel.js |

**最终 resetRidContent（live_panel.js）：**

```js
function resetRidContent(refs) {
  if (!refs) return;
  if (refs.thinking) { setText(refs.thinking, ""); refs.thinking.scrollTop = 0; }
  if (refs.stream) { setText(refs.stream, ""); refs.stream.scrollTop = 0; }
  if (refs.thinkingCount) setText(refs.thinkingCount, "");
  if (refs.streamCount) setText(refs.streamCount, "");
  if (refs.tok) {
    ["in", "out", "cr", "cc"].forEach((k) => {
      if (refs.tok[k]) setText(refs.tok[k], "0");
    });
  }
  if (refs.cacheRate) setText(refs.cacheRate, "—");
  if (refs.thinkingSpeed) setText(refs.thinkingSpeed, "");
  if (refs.streamSpeed) setText(refs.streamSpeed, "");
}
```

**最终 applyRid done 分支（live_panel.js）：**

```js
function applyRid(rid, ev) {
  const rec = ensureContainer(rid, ev);
  if (!rec) return;
  if (rec.kind === "puretext" && ev.thinking_text) upgradeToThinkstream(rec);
  const refs = rec.refs;
  const now = Date.now();
  if (ev.type === "done") resetRidContent(refs);  // v0.141：立刻清内容
  // ... 原有 setText 流程
}
```

---

## 6. 是否完全遵循规划路径开发

**完全按规划（无重大偏离）。**

### 完全按规划：

- **done 立刻清内容**：新增 `resetRidContent(refs)`，applyRid done 分支最前调一次。
- **保留 DOM 容器 + 徽标**：徽标"完成"不变，结构（idx / meta / badge）保留。
- **不动后端 10s timer**：列宽收窄 + 容器真销毁语义保留。
- **新内容覆盖**：setText 直接覆盖，无拼接残影。

### 偏离之处：

- **(a) 跨 rid 也复用**：plan 没明确，但 layout 把所有 rid 容器同等看待，done 容器清完后下次任何 rid 事件 setText 覆盖自然成立。**结构自决**。
- **(b) reset 字段含 scrollTop 复位**：plan 只说"清内容"，但 scrollTop 残留滚动条会在下次内容少时露馅，一并复位。**视觉自决**。
- **(c) `resetRidContent` 不清理 tool 卡片**：tool 卡片是全局独立管理（`applyTools` + `_toolsOrder`），与 per-rid 容器生命周期解耦。done 不触发 tool 清。**保持现状**。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

无改动（panel_pool release 仍挂 10s 销毁 timer；仅前端视觉层切 ready 态）。

### 前端

1. **`src/relay/web/live_panel.js`**：
   - `applyRid` `ev.type === "done"` 分支最前调 `resetRidContent(refs)`。
   - 新增 `resetRidContent(refs)` helper：清 stream / thinking 文本 + scrollTop 复位 + thinkingCount / streamCount 计数清空 + `refs.tok.in/out/cr/cc` 复位 "0" + cacheRate 复位 "—" + thinkingSpeed / streamSpeed 文本清空。徽标 + idx + meta + DOM 容器不动。

### 资源版本

- live_panel.js `?v=20260823-59 → ?v=20260823-60`。
- live_panel.css / styles-20260817.css / app.js 不动。

### 测试

未新增测试（前端视觉行为；jsdom 测不到 DOM 内容渲染）。

### 行为验收清单（手动测试项）

- [ ] 请求 done 后，容器内累积文本（thinking / stream）立即清空（保留徽标"完成"）
- [ ] done 容器计数（输入/输出/缓存读/缓存写/命中率）立即复位
- [ ] 同 rid 来新事件（即使 calling/uploading 阶段），内容直接写入 done 后的空容器（无拼接）
- [ ] 10s 内无新事件，容器仍挂在 DOM 里，徽标"完成"保持；10s 后销毁
- [ ] 跨 rid 复用：done 容器清空后，layout 把它当独立 item，下次新 rid 事件见缝插针写入

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/web/live_panel.js` | 改（+`resetRidContent` helper / `applyRid` done 分支调用） |
| `src/relay/web/live_panel.html` | 改（live_panel.js 资源版本 bump） |
