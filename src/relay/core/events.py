"""4-mode EventBus —— Cordis 风格事件分发（v0.117+）。

4 个模式（``00_context_overview_v1.md`` §6 原则 4）：

============== ============================= =====================
模式            语义                           适用场景
============== ============================= =====================
``emit``        广播观察者；async 订阅者后台调度  request.done / alert
``waterfall``   链式短路，第一个 ``await next`` 钩子链 pre_dispatch / pre_upstream
                之前的 handler 决定走向；       / post_response
                返回 None = 不阻拦
``parallel``    ``asyncio.gather`` 全员 await    一个事件触发 N 个独立
                                                后台任务（quota refresh +
                                                alert + log）
``serial``      ``for await`` 顺序执行           N 个 handler 有共享可变
                                                状态，必须按顺序
============== ============================= =====================

设计红线：

* 订阅者异常隔离 —— handler 抛错只记日志，绝不阻断事件派发；
* ``emit`` 的 async handler 用 ``asyncio.ensure_future`` 调度（只在
  event-loop 运行时有效；sync handler 不受影响）；
* ``subscribe`` 返回的 Disposer 删除自己（精确注销）；
* ``waterfall`` 的 ``next(...)`` 调用是「表态」信号——handler 想「接管」
  时 ``return await next(...)``；想「放行」时直接 ``return None``。
  handler 抛错同样视为「不阻拦」（异常隔离）。

⚠ **STABLE since v0.117**：本文件 4 个方法（``emit`` / ``waterfall`` /
``parallel`` / ``serial``）+ ``subscribe`` 是稳定 API。
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
from typing import Any, Awaitable, Callable, Optional

from .service import DisposerLike

__all__ = ["EventBus", "WaterfallNext", "EventHandler"]


log = logging.getLogger("relay.core.events")

# 订阅者签名：
# sync:   def h(**payload) -> None
# async:  async def h(**payload) -> None
EventHandler = Callable[..., Any]

# bucket 元素类型：(neg_priority, seq, handler)
# - neg_priority: -priority 让「高 priority 先」=「小值在前」
# - seq: 单调递增序列号，同 priority 时按订阅先后顺序（不直接比 function 对象）
# - handler: callable


class WaterfallNext:
    """``waterfall`` 模式下传给每个 handler 的 next 句柄。

    用法::

        async def my_handler(info, next_):
            if info["block"]:
                return None            # 短路：后面的 handler 不跑
            result = await next_(info)  # 跑下一个 handler

    内部实现：闭包变量递推。
    """

    __slots__ = ("_iterator", "_exhausted")

    def __init__(self, iterator) -> None:  # noqa: ANN001
        self._iterator = iterator
        self._exhausted = False

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[Any]:
        if self._exhausted:
            async def _noop(*a: Any, **kw: Any) -> Any:
                return None
            return _noop(*args, **kwargs)
        return _WaterfallStep(self._iterator, args, kwargs, self)


class _WaterfallStep:
    """``next_(...)`` 返回的 awaitable；await 它 = 跑下一个 handler。"""

    def __init__(self, iterator, args: tuple, kwargs: dict, parent: WaterfallNext) -> None:
        self._iterator = iterator
        self._args = args
        self._kwargs = kwargs
        self._parent = parent

    def __await__(self):
        return self._run().__await__()

    async def _run(self) -> Any:
        try:
            handler = next(self._iterator)
        except StopIteration:
            # 链尾：默认返回 None
            self._parent._exhausted = True
            return None
        try:
            # handler 签名：(args..., next_) —— 把 next_ 作为最后一个位置参数
            r = handler(*self._args, self._parent, **self._kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("waterfall handler %r 抛错（已隔离）: %s", handler, exc)
            return None
        if inspect.isawaitable(r):
            try:
                r = await r
            except Exception as exc:  # noqa: BLE001
                log.warning("waterfall handler %r await 抛错（已隔离）: %s", handler, exc)
                return None
        return r


class EventBus:
    """进程内 4-mode 事件总线。

    设计：
      * ``self._subs[event] = [(neg_prio, seq, handler), ...]``，priority
        高 + seq 小 → 在前；
      * ``subscribe`` 返回 ``DisposerLike`` —— 调它就把自己从订阅表删除；
      * ``emit`` / ``waterfall`` / ``parallel`` / ``serial`` 都**异常隔离**
        —— handler 抛错只记日志，不阻断其他 handler；
      * 内部对相同 (event, handler) 不做去重 —— 由调用方自己决定重复订阅
        是否合法。
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[tuple[int, int, EventHandler]]] = {}
        self._lock_count = 0
        # emit 进行中时新加的订阅延迟生效，避免「迭代时修改」ConcurrentModification
        self._pending_subs: list[tuple[str, int, int, EventHandler]] = []
        # 单调递增序列号 —— 同 priority 时按订阅先后排序
        self._seq = itertools.count(1)

    # ---- 订阅 ----

    def subscribe(
        self,
        event: str,
        handler: EventHandler,
        *,
        priority: int = 0,
    ) -> DisposerLike:
        """订阅事件，返回 Disposer（调它 = 注销自己）。

        priority 高先调（默认 0）。同 priority 按订阅先后顺序。
        """
        if not callable(handler):
            raise TypeError(f"subscribe 需要 callable handler，收到 {handler!r}")

        seq = next(self._seq)
        neg_prio = -priority

        # emit 期间推迟注册
        if self._lock_count > 0:
            self._pending_subs.append((event, neg_prio, seq, handler))
            return self._make_deferred_disposer(event, handler)

        bucket = self._subs.setdefault(event, [])
        bucket.append((neg_prio, seq, handler))
        bucket.sort()  # 按 (neg_prio, seq) 升序
        log.debug(
            "event %r 订阅 handler=%r priority=%d (总 %d)",
            event, handler, priority, len(bucket),
        )
        return self._make_disposer(event, handler)

    def unsubscribe(self, event: str, handler: EventHandler) -> bool:
        """精度注销：返回是否成功（可能不存在）。"""
        bucket = self._subs.get(event)
        if not bucket:
            return False
        for i, (_, _, h) in enumerate(list(bucket)):
            if h is handler:
                bucket.pop(i)
                return True
        return False

    def subscribers(self, event: str) -> list[tuple[int, EventHandler]]:
        """列出当前 event 的 (priority, handler) 列表（只读快照，按调用顺序）。"""
        return [(-neg_prio, h) for neg_prio, _, h in self._subs.get(event, [])]

    # ---- 4 mode ----

    def emit(self, event: str, **payload: Any) -> None:
        """广播模式（V0.0.1 兼容）：同步 handler 立即调；async handler 调度到事件循环。

        任一 handler 抛错只记日志，不阻断其他 handler。async handler 的
        coroutine 用 ``asyncio.ensure_future`` 启动；返回值不被收集（emit
        是 fire-and-forget 语义）。
        """
        # 锁定订阅表 → 复制一份
        self._lock_count += 1
        try:
            snapshot = [(p, h) for p, _, h in self._subs.get(event, ())]
        finally:
            self._lock_count -= 1
        if self._lock_count == 0:
            self._flush_pending()

        for _, handler in snapshot:
            try:
                r = handler(**payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("event %r handler %r 失败（已隔离）: %s",
                            event, handler, exc)
                continue
            if inspect.isawaitable(r):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    log.debug(
                        "event %r handler %r 返回 awaitable 但无事件循环，"
                        "丢弃结果", event, handler,
                    )
                    continue
                else:
                    asyncio.ensure_future(self._safe_await(r, event, handler))

    async def waterfall(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """链式短路模式：handler 通过 ``next_(...)`` 表态「放行 / 接管」。

        handler 签名约定::

            async def my_handler(info, next_):
                if info.get("block"):
                    return None            # 短路：后面的 handler 不跑
                result = await next_()     # 跑下一个 handler（自动传 args/kwargs）
                return result

        第一个 ``await next_()`` 之前 return 的 handler = 放行（继续走）；
        调了 ``next_()`` 但返回其他值 = 接管（值不再被后面的 handler 覆盖）；
        没调 ``next_()`` 直接 return = 短路（链尾）。

        ``next_`` 是 waterfall 注入的最后一个位置参数；调用方 ``waterfall(event, *args, **kwargs)``
        的 ``args / kwargs`` 自动透传给每个 handler。
        """
        snapshot = self._snapshot(event)
        if not snapshot:
            return None
        handlers = [h for _, _, h in snapshot]
        iterator = iter(handlers)
        next_ = WaterfallNext(iterator)
        try:
            # 把 next_ 作为最后一个位置参数透传；handler 看到 (info, next_)
            return await _WaterfallStep(iterator, args, kwargs, next_)._run()
        finally:
            next_._exhausted = True

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """并行模式：所有 handler ``asyncio.gather`` 同时跑；任一抛错被隔离。"""
        snapshot = self._snapshot(event)
        if not snapshot:
            return []

        async def _safe(p_h: tuple[int, int, EventHandler]) -> Any:
            _, _, h = p_h
            try:
                r = h(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                log.warning("parallel event %r handler %r 失败（已隔离）: %s",
                            event, h, exc)
                return None
            if inspect.isawaitable(r):
                try:
                    return await r
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "parallel event %r handler %r await 失败（已隔离）: %s",
                        event, h, exc,
                    )
                    return None
            return r

        return await asyncio.gather(*(_safe(ph) for ph in snapshot))

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """串行模式：handler 顺序 await；共享状态安全。"""
        snapshot = self._snapshot(event)
        results: list[Any] = []
        for _, _, h in snapshot:
            try:
                r = h(*args, **kwargs)
                if inspect.isawaitable(r):
                    r = await r
            except Exception as exc:  # noqa: BLE001
                log.warning("serial event %r handler %r 失败（已隔离）: %s",
                            event, h, exc)
                r = None
            results.append(r)
        return results

    # ---- 内部 ----

    def _snapshot(self, event: str) -> list[tuple[int, int, EventHandler]]:
        """取快照；同时触发 pending flush。"""
        return list(self._subs.get(event, ()))

    def _make_disposer(self, event: str, handler: EventHandler) -> DisposerLike:
        def _dispose() -> None:
            self.unsubscribe(event, handler)
        return _dispose

    def _make_deferred_disposer(self, event: str, handler: EventHandler) -> DisposerLike:
        """emit 期间订阅：返回的 disposer 也能正确注销（即使 pending 还没 flush）。"""
        def _dispose() -> None:
            self.unsubscribe(event, handler)
            # 也从 pending 列表里清掉
            self._pending_subs = [
                t for t in self._pending_subs
                if not (t[0] == event and t[3] is handler)
            ]
        return _dispose

    def _flush_pending(self) -> None:
        """emit 期间累积的订阅一次性注册。"""
        pending = self._pending_subs
        self._pending_subs = []
        for event, neg_prio, seq, handler in pending:
            bucket = self._subs.setdefault(event, [])
            bucket.append((neg_prio, seq, handler))
            bucket.sort()

    async def _safe_await(self, awaitable: Any, event: str, handler: EventHandler) -> Any:
        try:
            return await awaitable
        except Exception as exc:  # noqa: BLE001
            log.warning("event %r handler %r await 失败（已隔离）: %s",
                        event, handler, exc)
            return None

    # ---- shutdown ----

    def clear(self) -> None:
        """清空所有订阅；emit/waterfall 之后只会跑 noop。

        由 ``RelayContext.dispose()`` 调用，让 ctx 关闭后发事件不会
        触发已经「语境失效」的 handler。
        """
        self._subs.clear()
        self._pending_subs.clear()
        self._lock_count = 0
