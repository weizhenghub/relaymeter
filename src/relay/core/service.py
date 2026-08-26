"""服务契约层（v0.117+）—— RelayService / Disposer / @inject / compose。

设计目标（参考 ``docs/architecture/00_context_overview_v1.md`` §3.5 + §6）：

1. ``RelayService`` = 一个 Protocol，插件/横向模块只要实现 ``apply(ctx) -> Disposer``
   就被接纳。名字 ``name`` 是注册到 ctx 的 key。
2. ``Disposer`` = 反序资源释放协议（callable）。可以 ``ctx.dispose()`` 集中
   反序调；单个抛错不影响后续。
3. ``@inject(deps=...)`` = 装饰器声明依赖。Phase 1 loader 不强制读它（每个
   service 自己解析），Phase 2+ 引入真正的依赖图时 ``ctx.svc(...)`` 内部
   会用这张表做循环检测。
4. ``compose_disposers`` = 多 Disposer 反序合成一个；一个抛错不阻断后续。

⚠ **STABLE since v0.117**：本文件符号（``RelayService`` /
``Disposer`` / ``DisposerLike`` / ``inject`` / ``compose_disposers``）是中继对
插件作者 + 横向模块开发者的稳定契约（详见 ``docs/architecture/STABILITY.md``）。
变更签名 / 行为 / 字段语义必须先走 deprecation warning。
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable, Optional, Protocol, runtime_checkable

__all__ = [
    "RelayService",
    "Disposer",
    "DisposerLike",
    "inject",
    "compose_disposers",
    "DisposerChain",
]

log = logging.getLogger("relay.core.service")


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------


@runtime_checkable
class RelayService(Protocol):
    """所有插件 / 横向模块 / 内置服务统一实现的契约。

    ``apply(ctx)`` 在 lifespan 启动时被调用一次；返回的 ``Disposer`` 在
    lifespan finally 时被反序调用。**不需要** ``name`` 字段（运行时由
    loader / ctx.register() 决定 key）；这里给出只是文档意图。
    """

    def apply(self, ctx: Any) -> "DisposerLike": ...


@runtime_checkable
class Disposer(Protocol):
    """反序资源释放协议。一个 callable，零参调用即可。"""

    def __call__(self) -> None: ...


# DisposerLike = Disposer | Callable[[], None]
DisposerLike = Callable[[], None]


# ---------------------------------------------------------------------------
# inject 装饰器
# ---------------------------------------------------------------------------


_INJECT_ATTR = "__inject_deps__"


def inject(
    deps: Optional[tuple[str, ...]] = None,
    *extra: str,
) -> Callable[[Any], Any]:
    """声明服务依赖（Phase 1：标签语义；Phase 2+：loader 据此构造顺序）。

    两种用法（等价）::

        @inject(deps=("inflight", "bus"))         # keyword form (推荐)
        class MyService: ...

        @inject("inflight", "bus")                # positional form
        class MyService: ...

    ``deps`` 是 ``ctx.svc(...)`` 的 key 列表。Phase 1 不强制按它解析，
    但写下来有三重作用：

    1. 文档意图：service 自己声明「我需要哪些同伴」；
    2. Phase 2+ 真图拓扑排序时 loader 会读它；
    3. ``compose_disposers`` 反序执行，依赖图倒过来就是销毁顺序。

    **不能依赖未声明的 ctx key** —— 不抛错，但审计 / 重构期会标红。
    """
    if deps is None:
        all_deps: tuple[str, ...] = tuple(extra)
    else:
        all_deps = tuple(deps) + extra

    def deco(cls_or_fn: Any) -> Any:
        # 保留原签名（functools.wraps 风格）
        try:
            existing = getattr(cls_or_fn, _INJECT_ATTR, None)
            if existing is not None and tuple(existing) != all_deps:
                log.debug(
                    "service %r 已有 deps=%s，再次 inject 覆盖为 %s",
                    getattr(cls_or_fn, "__name__", cls_or_fn),
                    existing, all_deps,
                )
        except Exception:  # noqa: BLE001
            pass
        setattr(cls_or_fn, _INJECT_ATTR, all_deps)
        return cls_or_fn

    return deco


def read_deps(cls_or_fn: Any) -> tuple[str, ...]:
    """读出 ``@inject(deps=...)`` 注入的依赖列表；未声明 = 空 tuple。

    兼容装饰器未生效的情况（老插件直接 ``apply(ctx)``，没 ``@inject``）——
    返回空 tuple 表示「我不声明依赖，请按 ctx.svc() 实际调用顺序推断」。
    """
    return tuple(getattr(cls_or_fn, _INJECT_ATTR, ()) or ())


# ---------------------------------------------------------------------------
# Disposer 合成
# ---------------------------------------------------------------------------


def compose_disposers(*disposers: Optional[DisposerLike]) -> DisposerLike:
    """把多个 Disposer 反序合成一个；单个抛错不阻断后续。

    用法::

        return compose_disposers(
            sub_service_a.dispose,
            lambda: stop_event.set(),
            sub_service_b.dispose,
        )

    反序语义：先注册的后释放（LIFO）。这是 lifespan 标准约定——late-init
    services tend to depend on early-init ones, so release order is reversed.

    任一 Disposer 抛错只记 warning + 继续下一个；最后如果有错误累计，
    在日志里汇总「dispose 有 N 个 error」但**不抛**——shutdown 必须完成。
    """
    # 过滤 None 与不可调用对象（防御编程）
    chain: list[DisposerLike] = []
    for d in disposers:
        if d is None:
            continue
        if not callable(d):
            log.warning("compose_disposers 跳过非可调用对象: %r", d)
            continue
        chain.append(d)

    if not chain:
        return lambda: None

    if len(chain) == 1:
        return chain[0]

    def _run() -> None:
        errors: list[tuple[DisposerLike, BaseException]] = []
        # 反序
        for d in reversed(chain):
            try:
                d()
            except BaseException as exc:  # noqa: BLE001
                errors.append((d, exc))
                log.warning("dispose %r 失败（已隔离）: %s", d, exc)
        if errors:
            log.warning(
                "compose_disposers 反序执行累计 %d 个错误（shutdown 仍继续）",
                len(errors),
            )

    return _run


# ---------------------------------------------------------------------------
# DisposerChain —— 更结构化的「按顺序压入」容器
# ---------------------------------------------------------------------------


class DisposerChain:
    """FIFO 压入 / LIFO 弹出的 Disposer 容器。

    适用场景：service 在 ``apply(ctx)`` 里**按时间顺序**分配资源，希望
    ``ctx.dispose()`` 自动按**反序**回收。

    用法::

        class MyService:
            def apply(self, ctx):
                chain = DisposerChain()
                chain.add(lambda: close_db())
                chain.add(lambda: stop_thread())
                # 顺序：close_db 在 stop_thread 之后才执行
                return chain.as_disposer()
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: list[DisposerLike] = []

    def add(self, disposer: Optional[DisposerLike]) -> "DisposerChain":
        """压入一个 Disposer；返回 self 以便链式调用。"""
        if disposer is None:
            return self
        if not callable(disposer):
            raise TypeError(f"DisposerChain.add 需要 callable，收到 {disposer!r}")
        self._items.append(disposer)
        return self

    def __len__(self) -> int:
        return len(self._items)

    def as_disposer(self) -> DisposerLike:
        """返回一个 ``() -> None`` 的 Disposer：调用它 = 反序清空本链。

        语义：
          * 链内 items 不动（仍可读 ``len(chain)``）；
          * Disposer 一次有效 —— 多次调用只跑一遍（用 ``_fired`` 标志）；
          * 单个 handler 抛错被隔离；累计错误仅记日志，不抛。
        """
        items = list(self._items)
        state = {"fired": False}

        def _run() -> None:
            if state["fired"]:
                return
            state["fired"] = True
            errors: list[tuple[DisposerLike, BaseException]] = []
            for d in reversed(items):
                try:
                    d()
                except BaseException as exc:  # noqa: BLE001
                    errors.append((d, exc))
                    log.warning("DisposerChain 释放 %r 失败（已隔离）: %s", d, exc)
            if errors:
                log.warning("DisposerChain 累计 %d 个错误", len(errors))

        return _run


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def is_async_callable(obj: Any) -> bool:
    """判断一个对象是否为 async callable（async def 函数 / __call__ 返回 awaitable）。

    Phase 1 loader 用它判断 service.apply 是否需要 await。
    """
    if inspect.iscoroutinefunction(obj):
        return True
    call = getattr(obj, "__call__", None)
    if call is None:
        return False
    return inspect.iscoroutinefunction(call)


def maybe_await(result: Any) -> Any:
    """await or 透传 —— 给 ``apply()`` 可能返回 coroutine 的场景用。"""
    if inspect.isawaitable(result):
        # 调用方负责在 async 上下文里 await
        return result
    return result


# 让 ``functools.wraps`` 在 ``DisposerChain.as_disposer`` 返回的闭包上也能用
functools  # noqa: B018  # 仅占位 import（防止 IDE 误删）
