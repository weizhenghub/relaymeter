"""横向模块薄包装 Service（V0.120+）—— 委托现有模块函数。

Phase 4：``error_analyzer`` / ``advanced_switch`` / ``probe`` 三个横向模块
以 Service 形态注册到 ctx，供插件 / 其它横向模块用 ``ctx.svc(...)`` 取到，
**不再** ``from relay.error_analyzer import ...``（私生子 import）。

形态：**薄委托** —— 不重写业务逻辑；只包一层 ctx 注册 + 委托现有模块函数。
无独立状态 / 无 task / 无资源，Disposer 为 no-op。

注册 key：

* ``ctx.svc("error_analyzer")`` —— 错误分类（GUI 进程 / 插件诊断）
* ``ctx.svc("advanced_switch")`` —— 智能切线（proxy 层 pre_upstream 用）
* ``ctx.svc("probe")`` —— 上游探活（路由层 / 新建上游自动探测）
"""

from __future__ import annotations

from typing import Any, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["ErrorAnalyzerService", "AdvancedSwitchService", "ProbeService"]


def _noop_dispose() -> None:
    """薄包装无资源要释放。"""


class ErrorAnalyzerService:
    """错误分类（relay.error_analyzer 的 Service 包装）。"""

    def __init__(self) -> None:
        self._ctx: Optional[RelayContext] = None

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("error_analyzer", self)
        self._ctx = ctx
        return _noop_dispose

    # ---- 委托 ----

    def classify_error(self, settings: Any, context: dict, **kw: Any) -> Any:
        from ..error_analyzer import classify_error

        kw.setdefault("ctx", self._ctx)
        return classify_error(settings, context, **kw)

    def build_context(self, row: dict) -> dict:
        from ..error_analyzer import build_context

        return build_context(row)

    def build_prompt(self, context: dict) -> str:
        from ..error_analyzer import build_prompt

        return build_prompt(context)


class AdvancedSwitchService:
    """智能切线（relay.advanced_switch 的 Service 包装）。"""

    def __init__(self) -> None:
        self._ctx: Optional[RelayContext] = None

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("advanced_switch", self)
        self._ctx = ctx
        return _noop_dispose

    # ---- 委托 ----

    async def decide(self, *args: Any, **kw: Any) -> Any:
        from ..advanced_switch import decide

        kw.setdefault("ctx", self._ctx)
        return await decide(*args, **kw)

    def get_analysis_stats(self) -> dict:
        from ..advanced_switch import get_analysis_stats

        return get_analysis_stats()

    def get_route_stats(self) -> dict:
        from ..advanced_switch import get_route_stats

        return get_route_stats()


class ProbeService:
    """上游探活（relay.probe 的 Service 包装）。"""

    def __init__(self) -> None:
        self._ctx: Optional[RelayContext] = None

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("probe", self)
        self._ctx = ctx
        return _noop_dispose

    # ---- 委托 ----

    async def probe_upstream(
        self,
        url: str,
        api_key: str,
        *,
        timeout: float = 8.0,
        emit=None,
        model: Optional[str] = None,
        prober: Optional[str] = None,
    ) -> dict:
        from ..probe import probe_upstream

        return await probe_upstream(
            url, api_key, timeout=timeout, emit=emit, model=model, prober=prober
        )

    async def connectivity_test(
        self,
        url: str,
        api_key: str,
        *,
        wire: Optional[str] = None,
        auth_style: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 15.0,
    ) -> dict:
        from ..probe import connectivity_test

        return await connectivity_test(
            url, api_key, wire=wire, auth_style=auth_style,
            model=model, timeout=timeout,
        )
