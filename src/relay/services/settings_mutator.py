"""``SettingsMutator`` —— 配置写入集中化（V0.118+）。

集中化：
- ``save_upstreams_json``（旧 proxy.py:780 等多处散落用）
- ``update_env_var``（旧 config.py:744）

Phase 2.11 起，插件 / 横向模块改用 ``ctx.svc("mutator").save_upstreams(...)`` /
``.update_env_var(...)``，**不再** 从 ``from relay.config import ...`` import。

⚠ **STABLE since v0.118**：method signature 冻结。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["SettingsMutator"]

log = logging.getLogger("relay.services.settings_mutator")


class SettingsMutator:
    """集中化 mutator 入口。

    用法::

        mut = SettingsMutator()
        mut.apply(ctx)              # 注册 ctx.svc("mutator")
        ok, msg = mut.save_upstreams_json(data)
        ok = mut.update_env_var("KEY", "value")
    """

    def __init__(self) -> None:
        self._pending: list[dict] = []

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("mutator", self)

        def _dispose() -> None:
            self._pending.clear()

        ctx.add_disposer(_dispose)
        return _dispose

    # ---- 公开 API ----

    def save_upstreams_json(
        self,
        data: dict,
        *,
        path: Optional[str] = None,
    ) -> tuple[bool, str]:
        """原子写 upstreams.json；校验失败不落盘。

        内部委托 ``relay.config.save_upstreams_json``（已存在的实现）；
        本方法只是去掉「settings + mutator split」的两参旧签名。
        """
        from .. import config as _config
        from ..config import Settings  # noqa: F401  用于 isinstance 检查

        # 取一个 stub Settings 实例（如果 mut 拿不到，就构造最小 stub）
        settings = getattr(_config, "get_settings")()
        return _config.save_upstreams_json(
            settings,
            mutator=lambda d: d.clear() or d.update(data),
            path=path,
        )

    def update_env_var(self, key: str, value: str) -> bool:
        """写一行到项目根 .env；成功 True，文件被锁 / 无权限 False。"""
        from .. import config as _config

        return _config.update_env_var(key, value)

    def reload_settings(self) -> Any:
        """重新 load settings（写完后调用让后续 ctx.svc("settings").raw 见到新值）。"""
        from .. import config as _config

        return _config.reload_settings()

    def persist_active(self, settings: Any, platform: str, name: str) -> None:
        """记录平台当前激活上游到配置持久层（upstreams.json 优先，回退 .env）。

        Phase 4：quota_monitor._switch / routers.api 走这里，不再直接
        ``from .routers.api import _persist_active``。
        """
        from ..routers.api import _persist_active

        _persist_active(settings, platform, name)

    # ---- 调试 ----

    def pending_count(self) -> int:
        return len(self._pending)
