"""共享单例池（interning）—— Phase 2+ 服务共享同一实例用。

为什么需要：

Phase 2 起的 5 大服务里，部分应该**全进程单例**（HttpClientPool：共享
httpx 连接池；LiveBus：共享订阅者队列）。但其它一些服务可能希望「每个
ctx 一份」（隔离测试）。

``InterningRegistry`` 是一个显式的「我要共享」表：service 在 ``apply(ctx)``
里调 ``intern.register("inflight", instance)``；后续 ``ctx.svc("inflight")``
看到已注册就直接返回共享实例，**不再走``apply()``**。

Phase 1 不主推——只是给 Phase 2 留位置，loader 看到 ``intern.has("x")`` 时
跳过该 service 的实例化。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

__all__ = ["InterningRegistry"]


log = logging.getLogger("relay.core.interning")


class InterningRegistry:
    """「我要共享」表——service key → instance。

    用法::

        # Phase 2+ 启动期：第一个 ctx 注册共享实例
        intern = InterningRegistry()
        intern.register("inflight", HttpClientPool())

        # 后续任何 ctx.svc("inflight") 直接返回共享实例
        # 不会重新构造（loader 跳过该 service 的 apply()）
    """

    __slots__ = ("_shared",)

    def __init__(self) -> None:
        # 用 dict 而非 WeakValueDictionary：httpx client / asyncio.Lock
        # 不便做 weak ref（可能被内部状态引用），全进程一份不强求回收
        self._shared: dict[str, Any] = {}

    def register(self, key: str, instance: Any) -> None:
        """注册共享实例；同 key 重复注册覆盖（旧实例丢弃，不 dispose——调用方负责）。"""
        self._shared[key] = instance
        log.debug("intern: %r → %r", key, instance)

    def unregister(self, key: str) -> bool:
        """注销共享实例；返回是否成功。"""
        return self._shared.pop(key, None) is not None

    def get(self, key: str) -> Optional[Any]:
        """取共享实例；不存在返回 None（不抛错）。"""
        return self._shared.get(key)

    def has(self, key: str) -> bool:
        return key in self._shared

    def keys(self) -> list[str]:
        """当前已注册 key 列表（snapshot）。"""
        return list(self._shared.keys())

    def clear(self) -> None:
        """清空——只用于测试或彻底重启。生产 lifespan 不要调。"""
        self._shared.clear()
