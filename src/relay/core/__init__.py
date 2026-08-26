"""中继服务容器核心（v0.117+）—— Cordis 风格的 Python 重写。

入口::

    from relay.core import (
        RelayContext,        # 服务容器
        RelayService,        # 插件/横向模块契约
        Disposer, DisposerLike,  # 反序资源释放协议
        DisposerChain,       # FIFO/LIFO 容器
        inject,              # 声明依赖
        compose_disposers,   # 多 Disposer 合成
        EventBus,            # 4-mode 事件总线（直接构造）
        RequestScope,        # per-request contextvars
        InterningRegistry,   # 共享单例池
        bootstrap_builtin_services,    # 内置服务 loader
        attach_legacy_plugin_platform, # 兼容旧 plugins/*.py
    )

⚠ **STABLE since v0.117** —— 本包对外符号是稳定 API；变更签名 / 行为 /
字段语义必须先走 deprecation warning（详见 ``docs/architecture/STABILITY.md``）。
"""

from __future__ import annotations

from .context import RelayContext, ServiceAlreadyRegistered, ServiceNotFound
from .events import EventBus, EventHandler, WaterfallNext
from .interning import InterningRegistry
from .loader import (
    attach_legacy_plugin_platform,
    bootstrap_builtin_services,
    bootstrap_services,
    dispose_async_tasks,
)
from .scope import RequestScope, current_scope
from .service import (
    Disposer,
    DisposerChain,
    DisposerLike,
    RelayService,
    compose_disposers,
    inject,
    is_async_callable,
    maybe_await,
    read_deps,
)

__all__ = [
    # 主入口
    "RelayContext",
    "RelayService",
    # 协议 / 类型
    "Disposer", "DisposerLike", "EventHandler", "WaterfallNext",
    # 服务契约工具
    "inject", "compose_disposers", "DisposerChain",
    "is_async_callable", "maybe_await", "read_deps",
    # 4-mode 事件
    "EventBus",
    # per-request scope
    "RequestScope", "current_scope",
    # 共享池
    "InterningRegistry",
    # loader
    "bootstrap_builtin_services", "bootstrap_services",
    "attach_legacy_plugin_platform",
    "dispose_async_tasks",
    # 异常
    "ServiceAlreadyRegistered", "ServiceNotFound",
]
