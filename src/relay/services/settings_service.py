"""``SettingsService`` —— 配置只读包装（V0.118+）。

Phase 2.8 起，横向模块 / 插件作者通过 ``ctx.svc("settings")`` 取
``SettingsService``，调 ``service.raw`` 拿原 ``Settings`` 实例。

设计要点（与原 proxy.py / config.py 模块级 getter 等价）：

* **薄包装** —— 不复制 ``Settings`` 字段，只 hold 一份引用；
* **load()** —— 首次访问 lazy load（兼容 ``config.get_settings()`` 缓存行为）；
* **reload()** —— 调 ``config.reload_settings()``；
* **不引入 deprecation** —— 因为旧 ``from relay.config import get_settings``
  跨模块 import 不动（Phase 2.8 是「wrap 但不强制」过渡期）。

⚠ **STABLE since v0.118**：method signature 冻结。
"""

from __future__ import annotations

from typing import Any, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["SettingsService"]


class SettingsService:
    """Settings 只读包装。

    用法::

        svc = SettingsService()
        svc.apply(ctx)               # 注册 ctx.svc("settings")
        s = svc.raw                  # pydantic Settings 实例
        s = svc.reload()             # 重新 load + 返回
    """

    def __init__(self, settings: Any = None) -> None:
        self._settings: Optional[Any] = settings

    def apply(self, ctx: RelayContext) -> DisposerLike:
        """注册 SettingsService 实例到 ``ctx.svc("settings")``。

        语义：
          - **裸调用**（直接 ``SettingsService(...).apply(ctx)``）：wrapper 占据
            ``settings`` key。``ctx.svc("settings").raw`` 拿原 Pydantic Settings。
          - **lifespan 链中调用**（``bootstrap_services`` 在前）：``settings`` 已被
            旧插件注册时，使用 ``replace=False`` 抛 ``ServiceAlreadyRegistered`` 由
            loader 捕获，日志后跳过（保留旧插件语义不变）。

        返回 disposer 卸载 wrapper（无外部资源）。
        """
        if self._settings is None:
            self._settings = ctx.settings
        ctx.register("settings", self)
        return lambda: None  # 无状态清理

    @property
    def raw(self) -> Any:
        """原 Pydantic Settings 实例。"""
        return self._settings

    def reload(self) -> Any:
        """重新 load settings 并刷新 self._settings。"""
        # 懒导入：避免 services 包与 config.py 形成硬循环
        from .. import config as _config

        fresh = _config.reload_settings()
        self._settings = fresh
        return fresh
