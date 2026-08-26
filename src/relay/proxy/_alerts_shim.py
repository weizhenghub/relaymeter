"""proxy/_alerts_shim.py —— AlertLog 兼容层（V0.119+）。

* ctx 优先：``ctx.svc("alerts")``（AlertLog）
* 回退：``proxy_legacy._dispatch_alerts`` 模块全局
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .. import proxy_legacy


_ctx_singleton: dict[str, Any] = {"ctx": None}


def bind_ctx(ctx: Any) -> None:
    _ctx_singleton["ctx"] = ctx


def _alerts() -> Optional[Any]:
    ctx = _ctx_singleton["ctx"]
    if ctx is None:
        return None
    try:
        return ctx.svc("alerts")
    except Exception:  # noqa: BLE001
        return None


# 兼容旧符号
_ALERT_MAX = proxy_legacy._ALERT_MAX
_ALERT_DEDUPE_WINDOW = proxy_legacy._ALERT_DEDUPE_WINDOW
_dispatch_alerts = proxy_legacy._dispatch_alerts
_alert_dedup = proxy_legacy._alert_dedup
_alert_seq = proxy_legacy._alert_seq


def push_dispatch_alert(
    kind: str, platform: str, raw_key: str, model: Optional[str],
) -> None:
    """V0.119 兼容：始终走 legacy（test 通过 monkey-patch / _dispatch_alerts.clear()
    依赖 proxy_legacy 的模块全局）。Phase 4 起 AlertLog service 接管。"""
    proxy_legacy.push_dispatch_alert(kind, platform, raw_key, model)


def take_dispatch_alerts(since_id: int = 0) -> list[dict]:
    """同上：始终走 legacy（兼容 test 期望）。"""
    return proxy_legacy.take_dispatch_alerts(since_id)