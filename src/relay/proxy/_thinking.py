"""proxy/_thinking.py —— thinking block 剥离 / rewrite（V0.119 拆）。

策略：函数本体在 :mod:`relay.proxy_legacy`；这里仅 re-export。
"""

from __future__ import annotations

from .. import proxy_legacy


_extract_adaptive_effort = proxy_legacy._extract_adaptive_effort
_budget_to_effort = proxy_legacy._budget_to_effort
_strip_thinking_except = proxy_legacy._strip_thinking_except
_peek_thinking = proxy_legacy._peek_thinking
_client_thinking = proxy_legacy._client_thinking
_rewrite_thinking_for_upstream = proxy_legacy._rewrite_thinking_for_upstream
_strip_cache_control_scope = proxy_legacy._strip_cache_control_scope
_strip_thinking_blocks = proxy_legacy._strip_thinking_blocks
_strip_disallowed_content = proxy_legacy._strip_disallowed_content
_extract_last_user_message = proxy_legacy._extract_last_user_message
_preview_user_text = proxy_legacy._preview_user_text


__all__ = [
    "_extract_adaptive_effort",
    "_budget_to_effort",
    "_strip_thinking_except",
    "_peek_thinking",
    "_client_thinking",
    "_rewrite_thinking_for_upstream",
    "_strip_cache_control_scope",
    "_strip_thinking_blocks",
    "_strip_disallowed_content",
    "_extract_last_user_message",
    "_preview_user_text",
]