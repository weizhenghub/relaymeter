# 流式路径 finally 收尾 shield 修复：客户端断开时 DB 记录不再漏记（v0.199）开发文档

## 1. 用户的初始指令

> 我看到 codex 的请求路径是 openai-response → anthropic，我们是否正确做了这个协议的统计？

随后用户发现 codex 多轮对话后 relay.db 里一条记录都没有：

> 啥都没问题那问题就大了去了，现在我已经用 codex 对话好几轮了。请求都没记录上啊。

范围确认：**只改中继（relay），不动 codex 客户端源码**。

---

## 2. 从初始指令中提炼出的实现点

| # | 实现点 | 来源 |
|---|---|---|
| A | codex（openai-responses 入 → anthropic-messages 出）请求必须写进 relay.db | 指令 |
| B | 现状：codex 请求零记录，需定位断点在统计链路的哪一环 | 现象 |
| C | 修复只落中继侧，客户端（codex）零改动 | 范围确认 |
| D | 不能碰生产 8088：改完由用户重启 GUI 生效 | 环境约束 |

### 隐含但需要确认的点

- "没记录"= 请求行缺失，还是行在但 usage 空？→ 实测为**整行缺失**（requests 表无该请求 id）。
- 是否上游没回完整流 / linguafranca 转换挂起？→ 逐一排除（见 §4）。
- 其它客户端（完整读完流、不主动断开）为何能记录？→ 行为差异正是根因线索。

---

## 3. 分析需求后得出的开发路径

### 统计链路全景

codex 请求实际走 relay 的 **cross-wire 路径**：`openai-responses` 入 → linguafranca 转 `anthropic-messages` 出。relay 内共 3 条**流式**路径，DB 记录都写在各自的 `gen()` / `stream_iter()` 的 `finally` 块里：

| 路径 | 入口 | 函数 | record 位置 |
|---|---|---|---|
| adapter SSE | `/v1/...`（Anthropic 原生代理适配） | `gen()` | `_adapter_finalize`（proxy_legacy.py:744） |
| **cross-wire**（codex 走这条） | `/openai/v1/responses` 等 | `gen()` | `_cross_wire_finalize`（proxy_legacy.py:3153） |
| direct 流式透传 | wire 相同直连 | `stream_iter()` | `_direct_finalize`（proxy_legacy.py:3880） |

### 根因（决定性）

relay 用的 starlette 是旧路径（uvicorn spec_version **2.3**，`httptools_impl.py`）：

```
StreamingResponse.__call__ (spec < 2.4)
  → listen_for_disconnect 起并协程
  → 客户端断开 → disconnect 触发 → cancel_scope.cancel()
  → gen() 任务被取消
```

codex 的行为：**收到 `response.completed` 后立即断开连接**。于是：

1. 服务端 `listen_for_disconnect` 收到断开 → 取消 `gen()` 任务；
2. 任务进入 `finally`；
3. **`finally` 内第一个 `await` 会立即再次抛出 `CancelledError`**（asyncio 规范：取消状态下的任务，finally 里的 await 若没被 shield 保护，会立刻被同一次取消打断）—— 打断点是 `await upstream_resp.aclose()` / `await db.record(...)`；
4. `db.record` 永不执行 → **请求整行不入库**。

完整读完流（读到 EOF、不主动断开）的客户端不触发取消 → finally 的 await 全部执行 → 记录成功。这就是"别的客户端正常、codex 全漏"的原因。

### 修复方案：`asyncio.shield` 包裹 finally 收尾

把每条流式路径 finally 里的收尾逻辑抽成独立 async 闭包，finally 里用 `asyncio.shield(...)` 调用并吞掉取消：

```python
finally:
    try:
        await asyncio.shield(_cross_wire_finalize())
    except asyncio.CancelledError:
        log.info("%s cross-wire finalize cancelled during teardown", platform)
    except Exception:
        log.exception("%s cross-wire finalize failed during teardown", platform)
```

`shield` 让内部的 `db.record` 等 await 在任务已被取消的状态下**照常跑完**；外层 `except asyncio.CancelledError` 把取消信号留在收尾完成后再结算。

### 技术关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 修复点 | 3 条流式路径的 finally 全部 shield | 三条路径 record 结构相同、取消机制相同，一次修全 |
| 收尾抽闭包 | `_adapter_finalize` / `_cross_wire_finalize` / `_direct_finalize` | finally 里直接内联太长且难读；闭包内每步已有独立 try/except |
| 局部变量规避 | 闭包内用重命名局部（`nonlocal_usage`/`record_id`/`fin_usage`/`req_db_id`） | 闭包不能改写外层同名局部变量，避免 `nonlocal` 声明污染 |
| 非流式路径 | **不 shield** | 非流式/连接错误/拒绝行全在 handler 顶层 `await`，客户端断开不取消 handler（响应完整写回后才返回） |
| passthrough | **不 shield** | 纯 ASGI 中间件 `await send(...)` 直转（middleware.py:230），不经 starlette StreamingResponse 取消链路；`pt_db.record` 另有独立 try/except（middleware.py:274） |
| 版本号注释 | v0.166（与文档版本解耦） | 代码内注释版本号沿用既有线序；文档版本走 v0.199 |

---

## 4. 实现中遇到的问题

### 问题 1：「断开 → finally 不执行」假设被推翻

初判以为客户端断开会跳过 finally。用隔离实验（lab1/lab2，临时端口 8231/8232 + 临时 DB）证明：**断开时 finally 照样执行**，但 finally 内的 await 被打断。据此转向"取消如何打断 finally"。

### 问题 2：「上游流不完整」假设被推翻

怀疑火山（URL=`https://ark.cn-beijing.volces.com/api/coding`，wire=anthropic-messages，model=deepseek-v4-flash）没回完。用真实 codex body 抓上游响应（`cw_fetch_upstream.py`）：**完整 18 个事件、含 `message_stop`**。上游没问题。

### 问题 3：「linguafranca 转换挂起」假设被推翻

怀疑转换层挂住导致 gen() 出不来。把真实上游数据喂 relay 的 `convert_stream`（`cw_repro_real_upstream.py`）：**干净结束，9 个事件**。转换层没问题。

### 问题 4：隔离验证要区分 abort 与 full-read 两种客户端

- **abort**（codex 行为）：收到 `response.completed` 即断开 → 触发取消 → 旧代码不记录、新代码记录。
- **full-read**（正常客户端）：读到 EOF → 不取消 → 新旧都记录。

**解法**：隔离 relay（端口 8235，`RELAY_DB`→临时 DB，复用现有 upstreams.json），分别跑 abort 与 full-read 双测试确认。

### 问题 5：替换 direct finally 时 Edit 精确匹配失败

三处改造中，direct 路径 finally 因插入行偏移导致 `old_string` 匹配失败。**解法**：改用 Python 脚本按行号替换（仅这一处，其余两处 Edit 成功）。

---

## 5. 最后如何解决

| 问题 | 解决方案 | 文件 |
|---|---|---|
| #1 finally 不执行 | 推翻假设，转向"取消打断 finally 内 await" | — |
| #2 上游流不完整 | 抓包坐实完整 18 事件 + message_stop | — |
| #3 linguafranca 挂起 | 真实数据过 convert_stream 干净结束（9 事件） | — |
| #4 双客户端验证 | 隔离 relay 上 abort + full-read 双测 | 临时脚本 |
| #5 Edit 匹配失败 | Python 脚本按行号替换 direct finally | proxy_legacy.py |

---

## 6. 是否完全遵循规划路径开发

**完全按规划**：目标（codex 请求入库）、路径（cross-wire finally 收尾）、范围（只改 relay、3 条流式路径）全程无偏离。

### 偏离之处

- 无功能偏离。诊断早期有三个假设被推翻（§4 #1-#3），属于排查过程而非路径偏离；最终修复就是最初锁定的 shield 方案。

---

## 7. 最终实现点

### `src/relay/proxy_legacy.py`

1. **adapter SSE `gen()`**（:738）：
   - 新增 `_adapter_finalize()` 闭包（:744）：`db.record` + `db.record_messages` + `_complete_inflight`。
   - `finally`（:949）→ `await asyncio.shield(_adapter_finalize())` + 捕获 CancelledError/Exception。

2. **cross-wire `gen()`**（:3146，codex 走这条）：
   - 新增 `_cross_wire_finalize()` 闭包（:3153）：`aclose` + `parser.finalize` + `db.record` + `record_messages` + `_complete_inflight` + `_broadcast_live_event(done)` + `post_response` hooks。
   - `finally`（:3293）→ `await asyncio.shield(_cross_wire_finalize())` + 捕获。

3. **direct `stream_iter()`**（:3872）：
   - 新增 `_direct_finalize()` 闭包（:3880）：`aclose` + STREAM END 日志 + `parser.finalize` + `db.record` + `record_messages` + `_complete_inflight` + `_broadcast_live_event` + hooks。
   - `finally`（:4075）→ `await asyncio.shield(_direct_finalize())` + 捕获。

4. **不改**：非流式 JSON 路径（:3789 finally 仅 aclose，无 DB await）、连接错误/拒绝行（handler 顶层，非取消链路）。

### 验证（隔离端口 8235 + 临时 DB，8088 生产全程未碰）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| TEST 1：abort（收到 completed 即断，codex 行为） | **不记录** | ✅ 入库 id=1，1254 in / 12 out，200 |
| TEST 2：full-read（正常客户端，回归） | 记录 | ✅ 入库 id=2，1254 in / 2 out，200 |

DB `requests` 表 2 行全在，`messages` 表 user/assistant 完整落盘。

### 行为验收清单（手动测试项）

- [ ] 用 codex 对话几轮 → 重启 GUI 后 relay.db 出现对应请求行（usage 正确）
- [ ] 其它客户端（完整读流）请求仍正常入库（回归无损）
- [ ] abort 请求行 error 列为空（status=200）且 usage 非空
- [ ] 侧栏实时面板不出现"幽灵 STREAMING 行"（`_complete_inflight` 照常执行）

### 修改的文件清单

| 文件 | 类型 |
|---|---|
| `src/relay/proxy_legacy.py` | 改（3 条流式路径 finally 收尾 shield：adapter :952 / cross-wire :3301 / direct :4080） |
