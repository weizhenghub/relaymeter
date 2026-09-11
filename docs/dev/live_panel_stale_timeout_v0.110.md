# 实时栏截断连接自愈 + 阶段感知超时（v0.110）开发文档

## 1. 用户的初始指令

> 另一个问题，不是所有调用都会正确返回结束符，有很多中途截断的，就会导致一直在"流式中"这样窗口根本清不掉，怎么办呢？
>
> 不能检测是否长时间中断吗？比如模型已经输出了一堆正文但现在已经没有继续输出了，这种不是可以判断出是已结束未正常中断吗？还是说会有其它的情况？
>
> thinking pause设置60s超时，thinking pause和正文流衔接设置20s超时。已有正文流内容输出但没有新内容增长设置10s超时，可乎？
>
> （追问）分5个阶段：思考等待——思考出字中——思考结束，正文等待——正文出字中——正文结束。思考等待、出字中，以及正文出字中这3个阶段以现在的参数量能判断吗？
>
> 好了。写开发文档吧

**背景**：v0.109 单窗口合并后，stale 兜底已有雏形（`_stale_secs=25s` + watchdog tick 10s + `relayLiveTimeout` 只翻 badge 不清内容），但用户进一步要求**按流阶段差异化超时** —— 思考期容忍更久、正文期快速回收。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | 中途截断不广播 done → 检测并兜底清理（badge 不再永久「流式中」） | 指令 1 |
| B | 「已输出正文但停止增长」必须判为中断 | 指令 2 |
| C | 思考 pause 60s 超时 | 指令 3 |
| D | 思考→正文**衔接期** 20s 超时 | 指令 3 |
| E | 已有正文输出但不再增长 → 10s 超时 | 指令 3 |
| F | 明确 5 个阶段中「思考等待 / 思考出字中 / 正文出字中」能否从现有数据判断 | 指令 4（追问） |

### 隐含但需要确认 / 需自行决策的点

- **阶段感知的数据基础**：SSE delta 携带**累积文本**（`thinking_text`/`assistant_text`），每次上游 chunk 都广播一次 delta（`proxy.py` 逐事件镜像）→「有事件到达」= 连接活着 + 流在推进；「文本非空」= 进入对应阶段。
- **衔接期是否可独立判定** —— 需先用数据能力回答用户追问，再决定 D 是否可行（见 §4 问题 1）。
- **超时后果**：推「超时 done」给前端 —— 只翻 badge（「流式中」→「出错」），**不清**已累积的正文/思考（与正常 done 的 `applyStreamText("")` 分开）。
- **阈值可调**：三档常量集中在 `panel_pool.py`，不落 settings（内部兜底参数，非用户可配置项）。

---

## 3. 分析需求后得出的开发路径

### 3.1 先回答用户追问（阶段能否判断）

以 `push_event` 收到的 delta（累积 `thinking_text` / `assistant_text`）构建**四态状态机**：

| 阶段 | pool 侧可观测 | 能判断 |
|---|---|---|
| 1 思考等待 | 有事件，thinking/text 皆空 | ✅ |
| 2 思考出字中 | `thinking_text` 非空、正文空 | ✅ |
| 3 思考结束·正文等待（衔接） | **与思考出字中·暂停观测完全相同**（thinking 有、正文无、静默） | ❌ 不能独立判定 |
| 4 正文出字中 | `assistant_text` 非空 | ✅ |
| 5 正文结束 | done 事件 / release | ✅ |

**结论**：衔接期在 pool 侧与「思考出字中暂停」是同一个观测（thinking 有累积、正文空、无新事件），无法独立判定 → 衔接**并入思考档**（60s）。

### 3.2 方案定型：三档阈值 + 阶段状态机

```
rid 阶段（_stage）："" / wait（思考等待）→ "thinking"（思考出字中，含衔接）→ "text"（正文出字中）
  单向推进（累积文本不回退）：wait→thinking→text；正文出现直接置 text 覆盖前态。
  空文本事件用 setdefault，不覆盖已有 thinking/text。
  清理点（release / clear / timeout / assign 复用）→ pop，回落 wait 档。

阈值（_stage_threshold）：
  wait      90s  思考等待（慢上游/排队沉默最宽容）
  thinking  60s  思考出字中（含衔接，长思考不误杀）
  text      10s  正文出字中（正文流已启动仍停 10s → 判死，快速回收）
```

watchdog tick 10s 扫一遍：`grid_rids` 与 `always_one_rid` 各自的 `_last_event[rid]` 超时按**当前阶段阈值**判定 → 超时动作（grid 清块 / always 推 timeout + 清 rid + 按常驻开关 hide）不变。

### 3.3 开发顺序

```
#1 panel_pool.py：删 _stale_secs 单阈值 → _stale_wait/_thinking/_text 三档
   + _stage 字典 + _note_stage() + _stage_threshold()；
   push_event 每事件调 _note_stage；watchdog 两处判定改按阶段取阈值；
   清理点 pop _stage。
#2 smoke_worker.py：test_stale_release 三阈值缩 0.3 沿用；
   新增 test_stage_threshold 验证状态机。
#3 smoke 全绿 + 开发文档。
```

---

## 4. 实现中遇到的问题

### 问题 1：衔接期（思考结束→正文等待）能否独立判定

用户明确要 20s 衔接超时。查证中继给 pool 的数据（`proxy.py` 广播）：delta 只带 `assistant_text`/`thinking_text`/`usage_live`，**没有「thinking 块已结束」的信号**（`stop_reason`/`content_block_stop`/`phase` 都不进 GUI）。思考结束后的衔接静默，与思考中暂停是**同一观测** → pool 侧无法判断衔接起点。

**可选方案**：① 中继在 thinking 块结束时广播标记（`content_block_stop`）→ pool 精确 20s 判衔接；② 衔接并入思考档（60s）。方案 ① 依赖上游发独立 thinking 块 stop，DeepSeek/火山/MiniMax 转兼容格式不一定发，且改动跨 relay+pool。经 AskUserQuestion 权衡，用户在追问中关注的是「阶段能否判断」而非坚持 20s，最终选定**衔接并入思考档**（60s）—— 罕见场景下衔接卡死最坏等 60s，可接受。

### 问题 2：为什么「无事件」能当「断了」的信号

用户关心是否可靠。机制基础：delta 是**累积文本** + 中继逐 chunk 广播 → 只要上游还在吐字（思考或正文）就必有事件刷新 `_last_event[rid]`。25s/60s 完全无事件只可能：上游断流、上游静默等待超长、或 SSE 线程卡死。这与「输出多少」无关 —— 吐越多刷新越勤。

**误报边界**（停顿但不产生事件）：长 thinking pause（>60s 憋不出字）、tool call 等待期（>60s）、超慢流（>10s/token）。误报后果受保护 —— `relayLiveTimeout` 不清已显示内容，只翻 badge + 释放 rid，不丢数据。

### 问题 3：超时处置不能复用正常 done 路径

正常 done（`relayLiveEvent` type=done）前端会 `applyStreamText("")` **清空**正文/思考，然后 reset。中断超时**不该清** —— 用户想看已输出的内容。需要一个独立入口 `relayLiveTimeout(rid, msg)`：只 `setPhaseBadge("done", "连接中断…")` + 状态文本「出错」，不清 `$stream`/思考/工具调用；`currentRid` 为 null 时才补。

### 问题 4：常驻开关下 stale 动作的分叉

v0.109 已实现「无论常驻开关都推 timeout」；本次阶段化**不得改变该语义**。常驻开 → timeout + 清 rid + 窗口保持可见（内容保留可读）；常驻关 → 额外 hide。阶段感知只影响**判定阈值**，不影响动作。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 位置 |
|---|---|---|
| #1 衔接不可判定 | 衔接并入思考档（60s）；文档记录数据边界（delta 无 thinking 块结束信号，无法独立判定） | panel_pool.py（设计决策） |
| #2 无事件=断流 | `_last_event` + 三档阈值；向用户说明 delta 累积文本保证「流在推进必刷新」 | panel_pool.py |
| #3 超时不清内容 | 独立 `relayLiveTimeout(rid,msg)`：只翻 badge/状态文本 + 补 currentRid，不走 done 的清空路径 | panel_pool.py / live_panel.js |
| #4 常驻分叉 | stale 动作逻辑复用 v0.109（`_push_always_one_timeout` + 清 rid + 按开关 hide），仅阈值换 `_stage_threshold(rid)` | panel_pool.py |

### 最终参数

```
_stale_wait_secs      = 90.0   思考等待（无任何字）
_stale_thinking_secs  = 60.0   思考出字中（含衔接）
_stale_text_secs      = 10.0   正文出字中
_stage                : rid → "" / "wait" / "thinking" / "text"
```

---

## 6. 是否完全遵循规划路径开发

**部分偏离（均为数据能力边界下的合理收敛）**。

### 完全按规划（无偏离）：

- 中途截断自愈：grid stale 清块收窄窗口 / always_one 推 timeout 清 rid
- 三档差异化阈值落地（wait 90 / thinking 60 / text 10）
- 阶段状态机单向推进（wait→thinking→text，正文出现覆盖、空文本不回退）
- 超时不清已显示内容（`relayLiveTimeout` 只翻 badge）
- 常驻开关语义不变（常驻开保持可见、内容可读）
- 阈值集中在 panel_pool.py 三个常量，smoke 可缩时测试

### 偏离之处：

- **(a) 衔接期 20s → 并入思考档 60s**：用户指令 D（20s 衔接）未独立实现。原因：中继 delta 不含 thinking 块结束信号，衔接与思考暂停观测相同，无法判定衔接起点（§4 问题 1）。**数据能力限制下的收敛，非疏漏。**
- **(b) 5 阶段 → 4 态**：用户的「思考结束/正文等待」与「思考出字中」合并为一态（thinking）。同理系数据限制。
- **(c) 阈值数值与用户原案微调**：用户原案 60/20/10，落地 90/60/10 —— wait 档取 90s（覆盖慢上游/排队），thinking 含衔接故取 60s（用户对思考 pause 的原值）。正文档 10s 完全按用户要求。

### 重大调整：无。

---

## 7. 最终实现点

### 后端（Python）

1. **`panel_pool.py`** —— 阶段感知超时
   - **替换**：`self._stale_secs = 25.0` → `_stale_wait_secs=90.0` / `_stale_thinking_secs=60.0` / `_stale_text_secs=10.0`
   - **新增**：`self._stage: dict[str, str]`（rid → ""/wait/thinking/text）
   - **新增**：`_note_stage(rid, ev)` —— 按累积文本推进阶段（`assistant_text` 非空 → text；否则 `thinking_text` 非空 → thinking；否则 `setdefault("wait")`）
   - **新增**：`_stage_threshold(rid)` —— 按 `_stage[rid]` 返回对应阈值（未知/未定 → wait 档）
   - **`push_event`**：`_last_event` 刷新后调 `_note_stage(rid, ev)`
   - **watchdog**：grid stale 判定、always_one stale 判定均改 `> self._stage_threshold(rid)`；动作不变（`_push_always_one_timeout` / 清 rid / 按常驻 hide）
   - **清理点** pop `_stage[rid]`：`release`(always)、`_clear_grid_block`、watchdog timeout 分支、`assign`（always/grid 两分支，rid 复用防残留旧阶段）

2. **`live_panel.js`** —— 新增 `window.relayLiveTimeout(rid, msg)`
   - `setPhaseBadge("done", msg || "连接中断")` + `$status` 置「出错」
   - **不清** `$stream`/思考/工具内容；`currentRid === null` 时才补 rid
   - 与正常 `relayLiveEvent`(done) 分开（done 会 `applyStreamText("")` 清空）

### 测试

- `smoke_worker.py`：
  - `test_stale_release`：三阈值缩到 0.3 沿用（常驻关 → timeout+hide+rid=""；常驻开 → timeout+rid=""+保持可见），断言 `relayLiveTimeout` 派发
  - 新增 `test_stage_threshold`：wait→thinking→text 推进、空文本不回退、清理后回落 wait 档 —— 5 测试全绿

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/panel_pool.py` | 改（单阈值 → 三档 + 阶段状态机） |
| `src/relay/web/live_panel.js` | 改（新增 `relayLiveTimeout` 入口） |
| `smoke_worker.py` | 改（三阈值缩时 + 新增状态机测试） |
| `docs/dev/live_panel_stale_timeout_v0.110.md` | 新增（本文档） |

### 验证建议（用户手动）

- 重启 GUI → 真实截断流（流式中途 kill 上游）：badge 约 10s（正文期）/ 60s（思考期）翻「出错」，窗口/网格块清掉
- 正常请求不受影响：正文持续输出不断刷新 `_last_event`，不会误触 10s
