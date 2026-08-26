"""``LiveBus`` —— 实时事件广播总线（V0.118+）。

从 ``proxy.py:1259-1322`` ``_SUBSCRIBERS`` / ``_SUBS_LOCK`` / ``_broadcast`` /
``_subscribe_live_stream`` / ``_unsubscribe_live_stream`` + 常量
``_BROADCAST_QUEUE_MAX`` / ``_BROADCAST_DROP_COUNT`` 抽出。

设计要点（与原 proxy.py 完全一致，不允许行为漂移）：

* **每 chunk 都广播一次** —— 无订阅者时早退，零开销；
* **绝不阻塞主路** —— 锁内只快照订阅者，锁外 ``put_nowait``；撞
  ``QueueFull`` 丢最新事件，绝不 await 队列空间；
* **独立锁** —— 不复用 ``_in_flight_lock``，后者每 chunk 都要拿；
* **每个订阅者一个 Queue** —— 异步安全的 FIFO；
* **publish 非阻塞** —— 同 ``_broadcast``，但作为 ``LiveBus.publish()``
  方法；
* **drop count 暴露** —— ``drop_count`` property，方便监控。

⚠ **STABLE since v0.118**：method signature 冻结。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["LiveBus"]

log = logging.getLogger("relay.services.live_bus")

_BROADCAST_QUEUE_MAX = 200       # 单订阅者队列容量上限


class LiveBus:
    """进程内实时事件广播总线（live panel / SSE 数据源）。

    用法::

        bus = LiveBus()
        bus.apply(ctx)               # 注册 ctx.svc("livebus")
        q = bus.subscribe()
        try:
            await bus.publish({"type": "delta", ...})
            while True:
                ev = await q.get()
                ...
        finally:
            await bus.unsubscribe(q)
    """

    _BROADCAST_QUEUE_MAX = _BROADCAST_QUEUE_MAX

    def __init__(self) -> None:
        self._subs: set = set()                   # set[asyncio.Queue[dict]]
        self._lock: Optional[asyncio.Lock] = None # 懒构造（无 loop 时不爆）
        self._drop_count: int = 0

    # ---- service 注册 ----

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("livebus", self)

        def _dispose() -> None:
            self._subs.clear()

        ctx.add_disposer(_dispose)
        return _dispose

    # ---- 公开 API ----

    def _get_lock(self) -> asyncio.Lock:
        """懒构造 asyncio.Lock —— import 期无事件循环可能。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def subscribe(self) -> asyncio.Queue:
        """新订阅一份；返回 asyncio.Queue(maxsize=200) 给调用方 await q.get()。"""
        lock = self._get_lock()
        # lazy: 不需要 async lock；set.add 是同步操作
        q: asyncio.Queue = asyncio.Queue(maxsize=self._BROADCAST_QUEUE_MAX)
        self._subs.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """取消订阅 + 清空残留（避免下次订阅错位消费）。"""
        async with self._get_lock():
            self._subs.discard(q)
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def publish(self, event: dict) -> None:
        """非阻塞广播。lock 内只快照订阅者；lock 外 put_nowait（满则丢）。"""
        lock = self._get_lock()
        async with lock:
            if not self._subs:
                return
            targets = list(self._subs)
        for q in targets:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 侧栏卡死了；宁掉帧不反压主路
                self._drop_count += 1

    @property
    def drop_count(self) -> int:
        """累计丢帧数（调试用）。"""
        return self._drop_count

    def subscriber_count(self) -> int:
        """当前订阅者数。"""
        return len(self._subs)
