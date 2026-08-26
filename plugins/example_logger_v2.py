"""V0.122 新风格插件示例 —— 事件日志（Service 风格 + 4-mode + ctx.svc）。

演示 Service 风格插件的三个新能力：

1. ``class ExampleLoggerService`` —— ``apply(ctx) -> DisposerLike``；
2. ``ctx.on("request.done", ...)`` 订阅进程事件（与旧 ``apply(ctx)``
   插件同款，但此处返回 Disposer 注销订阅）；
3. ``ctx.svc("inflight").snapshot()`` —— 从服务容器取服务（Phase 6 起
   PluginContext 委托到 RelayContext）。

与 ``plugins/example_logger.py`` 可同时加载，各自独立打日志。

插件契约：模块必须导出 ``apply(ctx)``。
"""

from __future__ import annotations

import logging

log = logging.getLogger("relay.plugin.example_logger_v2")


class ExampleLoggerService:
    """事件日志（Service 风格）。"""

    name = "example_logger_v2"

    def __init__(self) -> None:
        self._unsub: callable | None = None
        self._ctx = None

    def apply(self, ctx) -> callable:
        self._ctx = ctx

        @ctx.on("request.done")
        def _on_done(**payload):
            platform = payload.get("platform", "?")
            model = payload.get("model", "?")
            status = payload.get("status", "?")
            usage = payload.get("usage")
            tokens = getattr(usage, "total_tokens", None)
            log.info(
                "[v2] request.done platform=%s model=%s status=%s tokens=%s",
                platform, model, status, tokens,
            )

        # 存一个卸载句柄（loader 兼容：旧插件不返回 Disposer 也 OK）
        self._unsub = lambda: log.info("[v2] example_logger unsubscribed")

        # 演示 ctx.svc() —— 取 inflight 服务做只读快照（服务容器能力）。
        try:
            inflight = ctx.svc("inflight")
            n = len(inflight.snapshot()) if hasattr(inflight, "snapshot") else 0
            log.info("[v2] inflight snapshot size=%d (via ctx.svc)", n)
        except KeyError:
            log.info("[v2] ctx.svc('inflight') 不可用（lifespan 未注入）— 跳过快照")

        def _dispose() -> None:
            if self._unsub is not None:
                try:
                    self._unsub()
                except Exception:  # noqa: BLE001
                    log.warning("[v2] unsub failed", exc_info=True)

        return _dispose


# ---- loader 契约胶水 ----

_inst = ExampleLoggerService()


def apply(ctx) -> callable:
    return _inst.apply(ctx)
