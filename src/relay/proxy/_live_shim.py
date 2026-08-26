"""proxy/_live_shim.py —— LiveBus 兼容层（V0.119+）。

* ctx 优先：``ctx.svc("inflight")`` 暴露 ``subscribe()`` / ``unsubscribe()``
  / ``publish()``（V0.119 起，InflightStore 内部走 LiveBus）。
* 回退：test / 早期 lifespan 走 :mod:`relay.proxy_legacy` 模块全局
  ``_SUBSCRIBERS`` + ``_broadcast``。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from .. import proxy_legacy


_ctx_singleton: dict[str, Any] = {"ctx": None}


def bind_ctx(ctx: Any) -> None:
    _ctx_singleton["ctx"] = ctx


def _bus() -> Optional[Any]:
    ctx = _ctx_singleton["ctx"]
    if ctx is None:
        return None
    try:
        return ctx.svc("livebus")
    except Exception:  # noqa: BLE001
        return None


# 兼容旧访问
_SUBSCRIBERS = proxy_legacy._SUBSCRIBERS
_BROADCAST_QUEUE_MAX = proxy_legacy._BROADCAST_QUEUE_MAX
_BROADCAST_DROP_COUNT = proxy_legacy._BROADCAST_DROP_COUNT


def _subscribe_live_stream() -> "asyncio.Queue":
    b = _bus()
    if b is not None:
        return b.subscribe()
    return proxy_legacy._subscribe_live_stream()


async def _unsubscribe_live_stream(q: "asyncio.Queue") -> None:
    b = _bus()
    if b is not None:
        await b.unsubscribe(q)
        return
    await proxy_legacy._unsubscribe_live_stream(q)


async def _broadcast(event: dict) -> None:
    """V0.119 起 broadcast 走 LiveBus.publish()（非阻塞）；ctx 缺则回退 legacy。"""
    b = _bus()
    if b is not None:
        # LiveBus.publish 是 async；put_nowait 内部队列，async 包装 awaitable
        await b.publish(event)
        return
    await proxy_legacy._broadcast(event)


async def _broadcast_live_event(rid: str, kind: str, **payload: Any) -> None:
    """包装 ``{type: kind, request_id: rid, **payload}`` 广播。"""
    ev = {"type": kind, "request_id": rid, **payload}
    await _broadcast(ev)


def _get_subs_lock() -> asyncio.Lock:
    return proxy_legacy._get_subs_lock()