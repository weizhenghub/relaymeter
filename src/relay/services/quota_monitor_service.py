"""``QuotaMonitorService`` —— 5h 配额监控的 Service 形态（V0.120+）。

Phase 4：把 ``relay.quota_monitor`` 的模块级 ``start(app)`` 收进 Service：

* ``apply(ctx)`` 注册 ``ctx.svc("quota_monitor")``；
* 用 ``ctx.svc("settings").raw`` 读配置（不再从 ``app.state.settings``）；
* 监控 task 由 Service 自启，Disposer cancel；
* 旧 ``quota_monitor.start(app)`` 保留为兼容入口（test / 旧 lifespan）。

⚠ **STABLE since v0.120**：``apply(ctx)`` 注册 key ``quota_monitor`` + 公开
方法 ``get_task()`` / ``disabled()``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["QuotaMonitorService"]

log = logging.getLogger("relay.services.quota_monitor")


class QuotaMonitorService:
    """5h 配额后台监控（relay.quota_monitor 的 Service 包装）。"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._ctx: Optional[RelayContext] = None

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("quota_monitor", self)
        self._ctx = ctx

        def _dispose() -> None:
            task = self._task
            self._task = None
            if task is not None and not task.done():
                task.cancel()

        ctx.add_disposer(_dispose)
        return _dispose

    def start(self) -> bool:
        """启动监控循环（幂等）。成功 True；interval<=0 或已在跑 False。"""
        if self._task is not None and not self._task.done():
            return False
        ctx = self._ctx
        if ctx is None or ctx.settings is None:
            log.warning("QuotaMonitorService.start 缺 ctx/settings，跳过")
            return False
        interval = int(getattr(ctx.settings, "relay_quota_check_interval", 0))
        if interval <= 0:
            log.info("quota monitor disabled (relay_quota_check_interval <= 0)")
            return False
        from .. import quota_monitor as _qm

        self._task = asyncio.create_task(_qm.run_monitor(ctx.app), name="quota-monitor")
        log.info("QuotaMonitorService started (interval=%ds)", interval)
        return True

    def get_task(self) -> Optional[asyncio.Task]:
        return self._task

    def disabled(self) -> bool:
        ctx = self._ctx
        if ctx is None or ctx.settings is None:
            return True
        return int(getattr(ctx.settings, "relay_quota_check_interval", 0)) <= 0
