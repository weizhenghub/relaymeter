"""中继代理核心 —— proxy 包的入口（V0.119+）。

从 V0.119 起，原 3813 行的 ``src/relay/proxy.py`` 单文件拆为 6 个 ≤600 行的子模块：

* :mod:`relay.proxy._util`     —— 字节/字符串/header mask、preview、估算 token
* :mod:`relay.proxy._auth`     —— auth header 拼装、key normalize、mask
* :mod:`relay.proxy._dispatch` —— wire 推断 / path normalize / upstream 匹配
* :mod:`relay.proxy._thinking` —— thinking block 剥离 / rewrite
* :mod:`relay.proxy._streaming`—— SSE 字节缓冲 + adapter 入口（hot path）
* :mod:`relay.proxy._relay`    —— 主 ``relay()`` 函数 + ``_reject_dispatch`` / ``_relay_cross_wire``

⚠ **STABLE since v0.119**：包对外导出（``relay()`` / ``stream_iter`` /
``_get_client`` / ``_close_http_clients`` / ``_register_inflight`` / 等）
保持原 ``proxy.py`` 的可见性，让 ``from relay import proxy; proxy._xxx()``
仍工作（**兼容**旧私生子调用，但 ``STABILITY.md`` §2 标记为 deprecated，
Phase 4 起全部转 ``ctx.svc(...)``）。

迁移期兼容：

* ``relay.proxy_legacy`` —— V0.118 旧的单文件 ``proxy.py`` 副本（保留以备
  git bisect；不 import，文件级备份）。
* ``src/relay/proxy/_util.py`` 等子模块 —— 拆出的实现，每个 import path
  仍能被 ``proxy.<name>`` 形式从 ``__init__.py`` re-export 拿到。

⚠ hot path 红线（V0.119+ 起维持）：

* ``stream_iter`` 字节缓冲路径**不**变；
* ``_bump_chunk_activity`` / ``_broadcast_live_event`` / ``parser.feed`` /
  ``_filter_anthropic_sse` 不进事件钩子链（仅接受 §7.4 同模块直接调用）。

参见：:doc:`/docs/architecture/30_split_proxy_v0.119`
"""

from __future__ import annotations

# 模块级 re-export —— 让 `from relay import proxy; proxy._xxx()` 仍工作
from ._util import (  # noqa: F401
    _mask,
    _redact_headers,
    _trunc,
    _body_preview,
    _estimate_output_tokens,
    _sse_preview,
    _strip_thinking_content,
)
from ._auth import (  # noqa: F401
    _normalize_key,
    _auth_header_value,
    _client_auth_key,
    _mask_key,
)
from ._dispatch import (  # noqa: F401
    _infer_client_wire_from_path,
    _resolve_platform_for_wire,
    _sniff_client_wire,
    _normalize_api_path,
    _join_upstream_url,
    _match_upstream_by_key,
    _anthropic_messages_url,
)
from ._thinking import (  # noqa: F401
    _extract_adaptive_effort,
    _budget_to_effort,
    _strip_thinking_except,
    _peek_thinking,
    _client_thinking,
    _rewrite_thinking_for_upstream,
    _strip_cache_control_scope,
    _strip_thinking_blocks,
    _strip_disallowed_content,
    _extract_last_user_message,
    _preview_user_text,
)
from ._streaming import (  # noqa: F401
    _filter_anthropic_sse,
    _anthropic_adapter_relay,
    _anthropic_adapter_relay_sse,
)
from ._relay import (  # noqa: F401
    relay,
    _reject_dispatch,
    _relay_cross_wire,
)

# inflight 注册管理（保留旧路径） —— 实际状态在 InflightStore 里，proxy
# 模块级 fn 是兼容 shim（lifespan bootstrap_services 之后 ctx 已注入）
from . import _inflight_shim as _inflight  # noqa: F401

# 系统代理 / client pool（保留旧路径） —— 实际状态在 HttpClientPool 里
from . import _http_shim as _http  # noqa: F401

# live stream broadcast（保留旧路径） —— 实际状态在 LiveBus / InflightStore 里
from . import _live_shim as _live  # noqa: F401

# alert log（保留旧路径） —— 实际状态在 AlertLog 服务里
from . import _alerts_shim as _alerts  # noqa: F401

# reasoning cache（保留旧路径） —— 实际状态在 ReasoningCache 服务里
from . import _reasoning_shim as _reasoning  # noqa: F401


# ---- shim 公开属性再导出 ----
# 兼容旧路径访问 ``proxy._REASONING_BY_TOOL_CALL`` / ``proxy._SUBSCRIBERS`` /
# ``proxy._client_pool`` / ``proxy._dispatch_alerts`` / ``proxy._in_flight`` /
# ``proxy._HTTP_TIMEOUT`` / ``proxy._anthropic_messages_url`` / 等等。
_in_flight = _inflight._in_flight
_in_flight_done = _inflight._in_flight_done
_in_flight_lock = _inflight._in_flight_lock
MAX_DONE_VISIBLE = _inflight.MAX_DONE_VISIBLE
BODY_LARGE_THRESHOLD = _inflight.BODY_LARGE_THRESHOLD
_SUBSCRIBERS = _live._SUBSCRIBERS
_BROADCAST_QUEUE_MAX = _live._BROADCAST_QUEUE_MAX
_BROADCAST_DROP_COUNT = _live._BROADCAST_DROP_COUNT
_client_pool = _http._client_pool
_HTTP_TIMEOUT = _http._HTTP_TIMEOUT
_dispatch_alerts = _alerts._dispatch_alerts
_alert_dedup = _alerts._alert_dedup if hasattr(_alerts, "_alert_dedup") else _alerts._dispatch_alerts
_alert_seq = _alerts._alert_seq if hasattr(_alerts, "_alert_seq") else 0
_ALERT_MAX = _alerts._ALERT_MAX
_ALERT_DEDUPE_WINDOW = _alerts._ALERT_DEDUPE_WINDOW
_REASONING_BY_TOOL_CALL = _reasoning._REASONING_BY_TOOL_CALL
_MAX_REASONING_ENTRIES = _reasoning._MAX_REASONING_ENTRIES


# ---- shim 函数再导出 ----
# 让 ``from relay.proxy import _broadcast`` / ``_register_inflight`` /
# ``_get_client`` / ``_close_http_clients`` 等继续工作（兼容 test / 旧 plugin）。
_subscribe_live_stream = _live._subscribe_live_stream
_unsubscribe_live_stream = _live._unsubscribe_live_stream
_broadcast = _live._broadcast
_broadcast_live_event = _live._broadcast_live_event
_get_subs_lock = _live._get_subs_lock
_register_inflight = _inflight._register_inflight
_update_inflight = _inflight._update_inflight
_bump_chunk_activity = _inflight._bump_chunk_activity
_set_inflight_phase = _inflight._set_inflight_phase
_complete_inflight = _inflight._complete_inflight
_find_done = _inflight._find_done
get_inflight_snapshot = _inflight.get_inflight_snapshot
_get_inflight_lock = _inflight._get_inflight_lock
_get_client = _http._get_client
_close_http_clients = _http._close_http_clients
_system_proxy_url = _http._system_proxy_url
push_dispatch_alert = _alerts.push_dispatch_alert
take_dispatch_alerts = _alerts.take_dispatch_alerts
_bind_reasoning_to_tool_calls = _reasoning._bind_reasoning_to_tool_calls
_upstream_needs_reasoning = _reasoning._upstream_needs_reasoning
_inject_reasoning_to_messages = _reasoning._inject_reasoning_to_messages
_merge_split_assistant_messages = _reasoning._merge_split_assistant_messages
_split_openai_tool_call_arguments = _reasoning._split_openai_tool_call_arguments


# ---- AUTH_AUTO_SENTINEL（兼容旧 import） ----
# 见 proxy_legacy.py: AUTH_AUTO_SENTINEL = "auto"。test_proxy_integration 仍
# import 此常量。
from ..proxy_legacy import AUTH_AUTO_SENTINEL  # noqa: F401


# ---- proxy_legacy 其他公开符号全量 re-export ----
# 让 ``from relay.proxy import _sanitize_anthropic_payload`` /
# ``_normalize_upstream_error`` / ``_error_body`` / ``_proxy_alive`` /
# ``_sweep_loop`` / ``_monitor_quiet_streaming`` / ``_sweep_inflight_once`` /
# ``_ctx_from`` / ``_broadcast_live_event`` 等所有符号继续可用。
from ..proxy_legacy import (  # noqa: F401
    httpx,  # 让 ``proxy.httpx.AsyncClient`` 仍可用（test monkey-patch 用）
    _ctx_from,
    _apply_auth_override,
    _proxy_alive,
    _proxy_alive_cache,
    _PROXY_PROBE_TTL,
    _ANTHROPIC_ERROR_TYPES,
    _error_body,
    _normalize_upstream_error,
    _sanitize_anthropic_payload,
    _OPENAI_API_ENDPOINTS,
    _ANTHROPIC_API_ENDPOINTS,
    _API_VERSION_RE,
    _ANTHROPIC_PATH_SUFFIXES,
    _OPENAI_RESPONSES_PATH_SUFFIXES,
    _OPENAI_CHAT_PATH_SUFFIXES,
    _extract_model,
    _rewrite_model_in_body,
    _THINKING_BUDGETS,
    _THINKING_CLIENT_FIELDS,
    _THINKING_OFF_VALUES,
    _strip_cache_control_scope_in_place,
    _strip_thinking_blocks_in_place,
    _OPENAI_CREATED_ZERO_RE,
    STREAMING_STALE_AFTER,
    MAX_INFLIGHT_AGE,
    SWEEP_INTERVAL,
    STREAM_QUIET_AFTER,
    QUIET_MONITOR_INTERVAL,
    UPLOAD_QUIET_AFTER,
    _sweep_inflight_once,
    _sweep_loop,
    _monitor_quiet_streaming,
)


__all__ = [
    # ---- 公开稳定 API（V0.117+ STABLE）----
    "relay",
    # ---- Phase 3 内部实现（deprecated for new code；Phase 4 转 ctx.svc）----
    # _util
    "_mask", "_redact_headers", "_trunc", "_body_preview",
    "_estimate_output_tokens", "_sse_preview", "_strip_thinking_content",
    # _auth
    "_normalize_key", "_auth_header_value", "_client_auth_key", "_mask_key",
    # _dispatch
    "_infer_client_wire_from_path", "_resolve_platform_for_wire",
    "_sniff_client_wire", "_normalize_api_path", "_join_upstream_url",
    "_match_upstream_by_key", "_anthropic_messages_url",
    # _thinking
    "_extract_adaptive_effort", "_budget_to_effort", "_strip_thinking_except",
    "_peek_thinking", "_client_thinking", "_rewrite_thinking_for_upstream",
    "_strip_cache_control_scope", "_strip_thinking_blocks",
    "_strip_disallowed_content", "_extract_last_user_message", "_preview_user_text",
    # _streaming
    "_filter_anthropic_sse", "_anthropic_adapter_relay",
    "_anthropic_adapter_relay_sse",
    # _relay
    "_reject_dispatch", "_relay_cross_wire",
]