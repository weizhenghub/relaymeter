"""proxy/_dispatch.py —— wire 推断 / path normalize / upstream 匹配。

V0.119 拆自 :mod:`relay.proxy_legacy`：

* ``_infer_client_wire_from_path`` / ``_resolve_platform_for_wire``
* ``_sniff_client_wire``
* ``_normalize_api_path``
* ``_join_upstream_url``
* ``_match_upstream_by_key``
* ``_anthropic_messages_url``

策略（V0.119+）：本模块函数本体**完全在 proxy_legacy**；这里仅 re-export
+ 文档化。Phase 4 起考虑把这些函数下沉到独立 dataclass / 策略对象（不
再 import proxy_legacy）。

⚠ deprecated for new code —— Phase 4 起由 :class:`relay.services.UrlBuilder`
接管 URL 拼接；wire 推断按 cfg rule 走 :class:`relay.services.dispatch_rule`。
"""

from __future__ import annotations

from .. import proxy_legacy


# ---- URL 拼接 / normalize ----

_anthropic_messages_url = proxy_legacy._anthropic_messages_url
_join_upstream_url = proxy_legacy._join_upstream_url
_normalize_api_path = proxy_legacy._normalize_api_path


# ---- Wire 推断 / 嗅探 ----

_infer_client_wire_from_path = proxy_legacy._infer_client_wire_from_path
_resolve_platform_for_wire = proxy_legacy._resolve_platform_for_wire
_sniff_client_wire = proxy_legacy._sniff_client_wire


# ---- 上游匹配 ----

_match_upstream_by_key = proxy_legacy._match_upstream_by_key


__all__ = [
    "_anthropic_messages_url",
    "_join_upstream_url",
    "_normalize_api_path",
    "_infer_client_wire_from_path",
    "_resolve_platform_for_wire",
    "_sniff_client_wire",
    "_match_upstream_by_key",
]