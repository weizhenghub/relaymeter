"""proxy/_relay.py —— 主 ``relay()`` + ``_reject_dispatch`` + ``_relay_cross_wire``。

策略：函数本体在 :mod:`relay.proxy_legacy`；这里仅 re-export。

⚠ **STABLE since v0.117**：``relay()`` 是路由层入口；签名冻结。
"""

from __future__ import annotations

from .. import proxy_legacy


relay = proxy_legacy.relay
_reject_dispatch = proxy_legacy._reject_dispatch
_relay_cross_wire = proxy_legacy._relay_cross_wire


__all__ = [
    "relay",
    "_reject_dispatch",
    "_relay_cross_wire",
]