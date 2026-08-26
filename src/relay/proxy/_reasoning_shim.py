"""proxy/_reasoning_shim.py —— ReasoningCache 兼容层（V0.119+）。

* ctx 优先：``ctx.svc("reasoning")``
* 回退：``proxy_legacy._REASONING_BY_TOOL_CALL`` 模块全局
"""

from __future__ import annotations

from typing import Any, Optional

from .. import proxy_legacy


_ctx_singleton: dict[str, Any] = {"ctx": None}


def bind_ctx(ctx: Any) -> None:
    _ctx_singleton["ctx"] = ctx


def _reasoning() -> Optional[Any]:
    ctx = _ctx_singleton["ctx"]
    if ctx is None:
        return None
    try:
        return ctx.svc("reasoning")
    except Exception:  # noqa: BLE001
        return None


# 兼容旧访问
_REASONING_BY_TOOL_CALL = proxy_legacy._REASONING_BY_TOOL_CALL
_MAX_REASONING_ENTRIES = proxy_legacy._MAX_REASONING_ENTRIES


def _bind_reasoning_to_tool_calls(reasoning: str, tool_calls) -> None:
    """V0.119 兼容：test 通过 ``proxy._REASONING_BY_TOOL_CALL.clear()`` 后
    调本函数验证 dict 内容。ctx 路径走 ReasoningCache 会让 legacy dict
    保持空。强制 legacy：Phase 4 起两个 store 合并。"""
    proxy_legacy._bind_reasoning_to_tool_calls(reasoning, tool_calls)


def _upstream_needs_reasoning(cfg, model: Optional[str]) -> bool:
    return proxy_legacy._upstream_needs_reasoning(cfg, model)


def _inject_reasoning_to_messages(messages, needs_reasoning: bool) -> None:
    proxy_legacy._inject_reasoning_to_messages(messages, needs_reasoning)


def _merge_split_assistant_messages(messages) -> None:
    proxy_legacy._merge_split_assistant_messages(messages)


def _split_openai_tool_call_arguments(d: dict, opened: set) -> list:
    return proxy_legacy._split_openai_tool_call_arguments(d, opened)