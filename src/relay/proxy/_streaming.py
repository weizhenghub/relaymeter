"""proxy/_streaming.py —— SSE 字节缓冲 + adapter 入口（hot path）。

策略：函数本体在 :mod:`relay.proxy_legacy`；这里仅 re-export。

⚠ **STABLE since v0.119**：hot path 字节缓冲路径不变（红线 §1）。
"""

from __future__ import annotations

from .. import proxy_legacy


_filter_anthropic_sse = proxy_legacy._filter_anthropic_sse
_anthropic_adapter_relay = proxy_legacy._anthropic_adapter_relay
_anthropic_adapter_relay_sse = proxy_legacy._anthropic_adapter_relay_sse


__all__ = [
    "_filter_anthropic_sse",
    "_anthropic_adapter_relay",
    "_anthropic_adapter_relay_sse",
]