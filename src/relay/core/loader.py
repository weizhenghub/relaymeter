"""服务 loader + 兼容旧 plugins/*.py 平台（v0.117+）。

两层职责：

1. **内置服务 loader（Phase 2 真接）** —— ``bootstrap_services(ctx)``
   按拓扑序注册 9 个服务（HttpClientPool / AlertLog / ReasoningCache /
   LiveBus / UrlBuilder / AuthHeader / InflightStore / SettingsService /
   SettingsMutator）；Phase 1 的 ``bootstrap_builtin_services`` 保留为
   占位 alias，不再扩展。

2. **兼容旧 plugin 平台（关键）** —— ``attach_legacy_plugin_platform(ctx)``
   把现有 ``relay.plugin`` 模块里那堆模块级注册表（``_HOOKS`` / ``_EVENTS`` /
   ``_PARSERS`` / ``_WIRE_CONVERTERS`` / ``_AUTH_SCHEMES`` / ``_PROBERS`` /
   ``_BILLING_UNITS`` / ``_OVERRIDES``）**转发**到 ctx.bus。这样：

   - 旧 ``plugins/volc_agent.py`` / ``example_logger.py`` 不修改即可加载；
   - ``emit_event(...)`` 仍走旧路径，但内部 ``bus.emit(...)`` 同步触发
     ctx.on(...) 订阅的 handler；
   - ``run_hooks(...)`` 仍走旧路径，但 ctx.bus 也能订阅同事件（双投递
     是允许的——emit 异常隔离，同一 handler 被多个 bus 投递不会重复执行
     因为我们只让**一个**走新路径，另一个走旧路径，由 emit_event 选）。

⚠ **STABLE since v0.117**：「兼容转发」语义本身是稳定 API（plugin 作者的
``load_plugins(app)`` 签名不变）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Optional

from .context import RelayContext, ServiceAlreadyRegistered

__all__ = ["bootstrap_services", "bootstrap_builtin_services", "attach_legacy_plugin_platform"]


log = logging.getLogger("relay.core.loader")


# ---- 内置服务 loader ----


def bootstrap_services(ctx: RelayContext) -> int:
    """按拓扑序注册 9 个 services/* 服务（Phase 2）。

    返回注册成功的 service 数。

    拓扑序（无依赖先行）::

        1. HttpClientPool        （无依赖）
        2. AlertLog              （独立）
        3. ReasoningCache        （独立）
        4. LiveBus               （独立）
        5. UrlBuilder            （独立）
        6. AuthHeader            （独立）
        7. SettingsService       （依赖 ctx.settings）
        8. SettingsMutator       （依赖 ctx.settings / settings service）
        9. InflightStore         （独立；Phase 2.10+ 起内部走 LiveBus）
    """
    # 懒导入：避免 services 包与 core 包形成硬循环
    from ..services import (
        AdvancedSwitchService,
        AlertLog,
        AuthHeader,
        ErrorAnalyzerService,
        HttpClientPool,
        InflightStore,
        LiveBus,
        ProbeService,
        QuotaMonitorService,
        ReasoningCache,
        SettingsMutator,
        SettingsService,
        UrlBuilder,
    )

    count = 0
    # 顶层 ctx.bus 也注册（Phase 1 兼容期保留）
    try:
        ctx.register("bus", ctx.bus)
        count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("bus 已存在: %s", exc)

    services = [
        HttpClientPool(),
        AlertLog(),
        ReasoningCache(),
        LiveBus(),
        UrlBuilder(),
        AuthHeader(),
        InflightStore(),
        SettingsService(ctx.settings),
        SettingsMutator(),
        QuotaMonitorService(),
        ErrorAnalyzerService(),
        AdvancedSwitchService(),
        ProbeService(),
    ]
    for svc in services:
        try:
            svc.apply(ctx)
            count += 1
        except ServiceAlreadyRegistered as exc:
            # Phase 1 兼容期：旧插件可能已经注册过相同 key（如 settings）；
            # 静默跳过即可，service 不阻塞 lifespan
            log.debug("service %s 重复注册跳过: %s", type(svc).__name__, exc.key)
        except Exception as exc:  # noqa: BLE001
            log.warning("service %s apply 失败: %s", type(svc).__name__, exc)
    log.info("bootstrap_services 注册 %d 个 services/* 实例", count)
    return count


def bootstrap_builtin_services(ctx: RelayContext) -> int:
    """Phase 1 占位 alias —— 调用 ``bootstrap_services``。

    保留为了不破坏 Phase 1 测试兼容（``test_context_legacy_emit``）。
    """
    return bootstrap_services(ctx)


# ---- 兼容旧 plugin 平台 ----


def attach_legacy_plugin_platform(ctx: RelayContext) -> int:
    """把 ``relay.plugin`` 的模块级注册表转发到 ctx.bus。

    关键设计：
      * 旧 ``emit_event(event, **payload)`` 仍由 plugin.py 实现，但
        我们 hook 一个对 ``ctx.bus.emit`` 的转发 —— 旧订阅者会走旧路径
        （``_EVENTS`` 字典），新订阅者（``ctx.on(...)``）走新 bus；
      * 这样 ``plugins/volc_agent.py`` 里的 ``ctx.on('request.done', ... )``
        （标准 plugin API）会被 plugin.py 存到 ``_EVENTS``，**不**走到
        ctx.bus —— 这是设计意图（不重复投递）。

    返回注册的「桥接服务」数。
    """
    from .. import plugin as legacy_plugin

    count = 0

    # ---- 1) 桥接 emit_event → ctx.bus.emit ----
    # 旧 emit_event 先跑旧订阅者，然后调 ctx.bus.emit 把事件也广播到
    # 直接订阅 ctx.bus 的 handler。这样:
    #   - 老 plugins 代码（写 _EVENTS）继续工作；
    #   - 新模块（ctx.on("request.done", ...)）也能订阅。
    # 实现: monkey-patch 旧 emit_event，**保留**原行为 + 追加 bus.emit。
    _wrap_emit_event(legacy_plugin, ctx)

    # ---- 2) 桥接 run_hooks → ctx.bus.waterfall ----
    # 同样：先跑旧钩子链，然后跑 ctx.bus.waterfall（链式短路）。
    # 这样 pre_dispatch 等钩子既能被旧 plugin 注册，也能被新风格
    # （@ctx.on 注册到 bus）订阅到。
    _wrap_run_hooks(legacy_plugin, ctx)

    # ---- 3) 桥接 arun_overrides → ctx.bus.parallel ----
    # 覆盖层（V0.4 替换层）：先跑旧 override，再跑 ctx.bus.parallel。
    # 优先级：旧 override（已按 priority 排好序）优先；ctx.bus.parallel
    # 仅用于 ctx.on("override:<subsystem>", ...) 这类**新风格**订阅。
    _wrap_arun_overrides(legacy_plugin, ctx)

    # ---- 4) 把 settings/db/app 暴露为 ctx svc（兼容期旧模块仍读 app.state）----
    # Phase 2 起：services 可能已注册过同名 key（如 SettingsService 占 settings），
    # 此时 ctx.svc("settings").raw 仍能拿到原始 Pydantic Settings；attacher
    # 不应覆盖。逻辑：
    #   * 已注册 → 跳过（Phase 2 service owner 优先）
    #   * 未注册 → 注册原对象（Phase 1 旧行为）
    if ctx.settings is not None and "settings" not in ctx:
        ctx.register("settings", ctx.settings)
        count += 1
    if ctx.db is not None and "db" not in ctx:
        ctx.register("db", ctx.db)
        count += 1
    if ctx.app is not None and "app" not in ctx:
        ctx.register("app", ctx.app)
        count += 1

    # ---- 5) 兼容层结束 ----
    log.info(
        "attach_legacy_plugin_platform: %d 个桥接服务已注册（emit/hooks/overrides 三桥接）",
        count,
    )
    return count


# ---- 转发实现 ----


def _wrap_emit_event(legacy_plugin: Any, ctx: RelayContext) -> None:
    """monkey-patch ``emit_event`` —— 原行为 + 转发到 ctx.bus.emit。"""
    original = legacy_plugin.emit_event

    def _wrapped(event: str, **payload: Any) -> None:
        # 1) 原路径：旧 _EVENTS 订阅者
        try:
            original(event, **payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("legacy emit_event %r 失败: %s", event, exc)
        # 2) 新路径：ctx.bus 订阅者
        try:
            ctx.bus.emit(event, **payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("ctx.bus.emit %r 失败: %s", event, exc)

    # 保留原函数名 / docstring（外部 isinstance / docs 友好）
    _wrapped.__name__ = getattr(original, "__name__", "emit_event")
    _wrapped.__doc__ = getattr(original, "__doc__", None)
    legacy_plugin.emit_event = _wrapped  # type: ignore[assignment]


def _wrap_run_hooks(legacy_plugin: Any, ctx: RelayContext) -> None:
    """monkey-patch ``run_hooks`` —— 原行为 + 转发到 ctx.bus.waterfall。

    关键：bus.waterfall 是 async；旧 run_hooks 是 async，所以 await 链路
    兼容。在 async 上下文里 ``ctx.bus.waterfall(name, info)`` 会跑新订阅者。
    """
    original = legacy_plugin.run_hooks

    async def _wrapped(name: str, info: dict) -> Any:
        # 1) 原路径：旧 _HOOKS 链
        legacy_result = await original(name, info)
        # 2) 新路径：ctx.bus.waterfall（链式短路）
        try:
            bus_result = await ctx.bus.waterfall(name, info)
        except Exception as exc:  # noqa: BLE001
            log.warning("ctx.bus.waterfall(%r) 失败: %s", name, exc)
            bus_result = None
        # 取第一个非 None（兼容原 run_hooks「返回第一个钩子的非 None」语义）
        if legacy_result is not None:
            return legacy_result
        return bus_result

    _wrapped.__name__ = getattr(original, "__name__", "run_hooks")
    _wrapped.__doc__ = getattr(original, "__doc__", None)
    legacy_plugin.run_hooks = _wrapped  # type: ignore[assignment]


def _wrap_arun_overrides(legacy_plugin: Any, ctx: RelayContext) -> None:
    """monkey-patch ``arun_overrides`` —— 原行为 + 转发到 ctx.bus.parallel。"""
    original = legacy_plugin.arun_overrides

    async def _wrapped(subsystem: str, *args: Any, **kwargs: Any) -> tuple[Any, Optional[str]]:
        # 1) 原路径
        try:
            result, provider = await original(subsystem, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("legacy arun_overrides(%r) 失败: %s", subsystem, exc)
            result, provider = None, None
        if result is not None:
            return result, provider
        # 2) 新路径：ctx.bus.parallel(...)
        try:
            results = await ctx.bus.parallel(f"override:{subsystem}", *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("ctx.bus.parallel(override:%r) 失败: %s", subsystem, exc)
            return result, provider
        for r in results:
            if r is not None:
                return r, "ctx.bus"
        return result, provider

    _wrapped.__name__ = getattr(original, "__name__", "arun_overrides")
    _wrapped.__doc__ = getattr(original, "__doc__", None)
    legacy_plugin.arun_overrides = _wrapped  # type: ignore[assignment]


# ---- 反初始化辅助 ----


async def dispose_async_tasks(tasks: list[asyncio.Task]) -> None:
    """lifespan finally 收尾：cancel 一组 task + await，错误隔离。

    暴露给 main.py 用，避免 main.py 自己维护 cancel/await 重复模板。
    """
    for t in tasks:
        if t is None or t.done():
            continue
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("task %s 收尾异常（已隔离）: %s", t.get_name(), exc)
