"""RelayContext —— Cordis 等价的 Python 服务容器（v0.117+）。

哲学（参考 ``docs/architecture/00_context_overview_v1.md`` §1 + §6）：

> 「不存在需要打补丁的特权内核」

中继的所有能力 = ctx 里能 ``.svc(...)`` 出来的对象。proxy.py 本身
也只是一个普通 service（Phase 3 拆分子模块后注入 ctx）。

API 形态：

* 注册面（lifespan 启动用）：``ctx.register(key, instance) -> Disposer``
* 解析面（运行期用）：``ctx.svc(key) -> instance`` 或 ``ctx[key]``
* 4-mode 事件（业务用）：``ctx.emit / waterfall / parallel / serial / on``
* per-request 绑定：``ctx.scope(**fields)`` context manager；
  ``ctx.current.rid`` 在任意下游位置同步读。
* 关闭：``ctx.dispose()`` —— 反序调所有 Disposer，单个抛错不阻断。

⚠ **STABLE since v0.117**：本文件主要公开符号
（``RelayContext.register / svc / emit / waterfall / parallel / serial /
on / scope / dispose / current``）是中继对插件作者 + 横向模块的稳定契约。
变更签名 / 行为 / 字段语义必须先走 deprecation warning。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from .events import EventBus
from .interning import InterningRegistry
from .scope import RequestScope, current_scope
from .service import DisposerLike, compose_disposers

__all__ = ["RelayContext", "ServiceAlreadyRegistered", "ServiceNotFound"]


log = logging.getLogger("relay.core.context")


class ServiceAlreadyRegistered(Exception):
    """``ctx.register(k, v)`` 同一 key 第二次注册时抛（lifespan 启动期）。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"service {key!r} 已注册；重复 register 是 bug，请改名或显式 replace=True")
        self.key = key


class ServiceNotFound(Exception):
    """``ctx.svc(k)`` 未注册时抛——运行期是 bug，启动期是配置错。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"service {key!r} 未注册；可注册的 key 见 ctx.services()")
        self.key = key


class RelayContext:
    """服务容器 + 4-mode 事件总线 + per-request scope。

    进程内建议只构造**一个**（lifespan 入口）；多实例可并存（测试 / 嵌套
    scope），但 ``register`` 语义是**严格单例**——重复注册抛
    ``ServiceAlreadyRegistered``。

    字段：

    ============= ============================================================
    字段           说明
    ============= ============================================================
    ``app``        关联的 FastAPI 实例（可能 None，单元测试场景）
    ``settings``   Pydantic Settings 实例（来自 ``app.state.settings``）
    ``db``         主 aiosqlite Database 实例（来自 ``app.state.db``）
    ``intern``     共享单例池（Phase 2+ 用）
    ``bus``        进程内 4-mode EventBus
    ``current``    ``RequestScope`` —— 当前请求的 contextvars 绑定
    ``log``        ``logging.getLogger("relay.core")`` 容器专属 logger
    ============= ============================================================

    ``register(key, instance) -> Disposer`` 的语义：

    1. 把 instance 存到 ``self._svc[key]``；
    2. 返回一个 Disposer：调用它 = 反注册（从 ``self._svc`` 里删 key）；
    3. 同时被 ``self._disposers`` 记录，``ctx.dispose()`` 时**反序**调。
    """

    def __init__(
        self,
        *,
        app: Any = None,
        settings: Any = None,
        db: Any = None,
        bus: Optional[EventBus] = None,
        intern: Optional[InterningRegistry] = None,
        scope: Optional[RequestScope] = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.db = db
        self.bus = bus if bus is not None else EventBus()
        self.intern = intern if intern is not None else InterningRegistry()
        self.current = scope if scope is not None else current_scope
        self.log = log

        self._svc: dict[str, Any] = {}
        self._order: list[str] = []  # 注册顺序；dispose 反序遍历
        self._disposers: list[DisposerLike] = []
        self._lock = threading.RLock()

    # ---- 注册 / 解析 ----

    def register(
        self,
        key: str,
        instance: Any,
        *,
        replace: bool = False,
    ) -> DisposerLike:
        """注册一个服务实例。

        - ``replace=False``（默认）：key 已注册时抛 ``ServiceAlreadyRegistered``。
        - ``replace=True``：覆盖旧实例；旧实例的 Disposer 被丢弃（**不**调），
          调用方负责旧实例的释放。

        返回的 Disposer 在 ``ctx.dispose()`` 时被反序调用。
        """
        with self._lock:
            if key in self._svc and not replace:
                raise ServiceAlreadyRegistered(key)

            self._svc[key] = instance
            if key not in self._order:
                # 已存在的 key + replace=True 不动 _order（保持首次注册位置）
                self._order.append(key)

            def _dispose(k: str = key) -> None:
                self._unregister(k)

            self._disposers.append(_dispose)
            self.log.debug(
                "ctx.register(%r) = %r (总 %d 个 service)",
                key, instance, len(self._svc),
            )
            return _dispose

    def _unregister(self, key: str) -> None:
        with self._lock:
            self._svc.pop(key, None)
            # 不动 _order —— 万一被重新注册（replace=True 路径），顺序保留

    def add_disposer(self, disposer: DisposerLike) -> None:
        """追加一个 Disposer 到 ``_disposers`` —— 给 service 用：
        ``apply(ctx)`` 时把自己 cleanup 的 hook 也挂进来，让 ``ctx.dispose()``
        自动级联反序释放 service 内部状态（如清空 dict）。

        不能与 ``register`` 返回的同一个 disposer 同源（否则 dispose 两次）；
        通常用法：``register`` 的 dispose 管 key 注销，``add_disposer`` 管
        service 状态。
        """
        with self._lock:
            self._disposers.append(disposer)

    def svc(self, key: str) -> Any:
        """取已注册服务；未注册抛 ``ServiceNotFound``。"""
        with self._lock:
            if key not in self._svc:
                raise ServiceNotFound(key)
            return self._svc[key]

    def __getitem__(self, key: str) -> Any:
        return self.svc(key)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._svc

    def services(self) -> list[str]:
        """当前已注册 service 的 key 列表（按注册顺序）。"""
        with self._lock:
            return list(self._order)

    # ---- 4-mode 事件（委托给 bus）----

    def on(
        self,
        event: str,
        handler: Optional[Callable[..., Any]] = None,
        *,
        priority: int = 0,
    ) -> Any:
        """订阅事件；支持 ``ctx.on(event, handler)`` 和装饰器 ``@ctx.on(event)``。

        返回 Disposer（调它 = 注销）。
        """
        if handler is None:
            # 装饰器用法
            def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
                self.bus.subscribe(event, fn, priority=priority)
                return fn
            return _deco
        return self.bus.subscribe(event, handler, priority=priority)

    def emit(self, event: str, **payload: Any) -> None:
        """广播观察者；sync handler 同步调，async handler 调度到事件循环。"""
        self.bus.emit(event, **payload)

    async def waterfall(self, event: str, *args: Any, **kwargs: Any) -> Any:
        return await self.bus.waterfall(event, *args, **kwargs)

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        return await self.bus.parallel(event, *args, **kwargs)

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        return await self.bus.serial(event, *args, **kwargs)

    def subscribers(self, event: str) -> list[tuple[int, Any]]:
        return self.bus.subscribers(event)

    # ---- per-request scope ----

    def scope(self, **fields: Any) -> Any:
        """per-request 字段绑定；context manager。

        用法::

            with ctx.scope(rid=uuid4().hex, platform="anthropic") as req:
                await relay(request, ...)

        块内任意位置 ``ctx.current.rid`` / ``ctx.current["rid"]`` 可同步读；
        块退出自动还原。
        """
        return self.current.bind(**fields)

    @property
    def rid(self) -> Optional[str]:
        """``ctx.current.rid`` 的快捷属性。"""
        return self.current.get("rid")

    # ---- shutdown ----

    def dispose(self) -> None:
        """反序调用所有 Disposer；单个抛错不阻断后续。

        幂等：调多次只在第一次有效（第二次起 ``_svc`` 已空，直接返回）。

        关掉之后：``bus`` 的订阅全清空（emit 触发不到任何 handler），
        之后发事件不再有副作用——这是 lifespan 退出场景的期望。
        """
        with self._lock:
            disposers = list(self._disposers)
            self._disposers.clear()
            self._svc.clear()
            self._order.clear()

        # 先清空 bus 订阅（emit 不再触发任何 handler），再反序释放
        # 各个 service。如果反过来，某个 service.apply 留下的 handler
        # 在 dispose 中还会被触发一次。
        try:
            self.bus.clear()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("bus.clear 失败（已隔离）: %s", exc)

        if not disposers:
            return

        run = compose_disposers(*disposers)
        try:
            run()
        except Exception as exc:  # noqa: BLE001
            # compose_disposers 内部已隔离；这里只是双保险
            self.log.warning("ctx.dispose 顶层兜底: %s", exc)

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"RelayContext(services={list(self._svc)}, "
                f"order={list(self._order)})"
            )
