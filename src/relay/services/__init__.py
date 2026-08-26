"""``src/relay/services/`` —— Phase 2 起的中继服务层（V0.118+）。

每个 service = 一个 ``RelayService`` 实例，``apply(ctx) -> Disposer``，
注册到 ``ctx.svc(...)`` 暴露给横向模块 / 插件。

拓扑序（依赖）::

    HttpClientPool        ─┐
    AlertLog              ─┤ 独立
    ReasoningCache        ─┤
    LiveBus               ─┤
    UrlBuilder            ─┤ 独立
    AuthHeader            ─┤
    SettingsService       ─┴─ SettingsMutator（依赖 settings）

    InflightStore         ── 依赖 LiveBus（v0.118；Phase 2 起把 _broadcast 走 bus）

⚠ **STABLE since v0.118**：服务名（``ctx.svc("pool"/"bus"/...)``）+ 公开方法签名
冻结；新增方法可以，删/改名必须先走 deprecation warning。

每个服务的细节见对应 .py 文件。
"""

from .alert_log import AlertLog
from .auth_header import AuthHeader  # AUTO_SENTINEL is module-level const; not re-exported from package __init__
from .horizontal_services import (
    AdvancedSwitchService,
    ErrorAnalyzerService,
    ProbeService,
)
from .http_client_pool import HttpClientPool
from .inflight_store import InflightStore
from .live_bus import LiveBus
from .quota_monitor_service import QuotaMonitorService
from .reasoning_cache import ReasoningCache
from .settings_mutator import SettingsMutator
from .settings_service import SettingsService
from .url_builder import UrlBuilder

__all__ = [
    "AdvancedSwitchService",
    "AlertLog",
    "AuthHeader",
    "ErrorAnalyzerService",
    "HttpClientPool",
    "InflightStore",
    "LiveBus",
    "ProbeService",
    "QuotaMonitorService",
    "ReasoningCache",
    "SettingsMutator",
    "SettingsService",
    "UrlBuilder",
]
