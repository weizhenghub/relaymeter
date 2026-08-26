"""v0.12 跨协议转换 —— 基于 martian-linguafranca（导入名 linguafranca）。

覆盖三种 wire 的请求/响应/流式双向互转（docs/wire-dispatch-plan.md §4）：
  - anthropic-messages（Anthropic Messages）
  - openai-chat（OpenAI Chat Completions）
  - openai-responses（Open Responses）

linguafranca 是 Rust 核心 + Python 绑定，schema 校验严格（畸形 payload 会抛
SchemaValidationError），转换有损时返回 warnings。本模块只做薄封装：
  * wire 名 → FormatName 映射
  * 错误响应体互转（linguafranca 只认成功 schema，错误体另处理）
  * SSE 序列化（linguafranca 产出事件 dict，这里负责拼成 SSE 字节）
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterable, Optional

import linguafranca as lf

from .config import (
    WIRE_ANTHROPIC_MESSAGES,
    WIRE_OPENAI_CHAT,
    WIRE_OPENAI_RESPONSES,
)


class WireConversionError(Exception):
    """请求/响应体不符合源格式 schema，无法转换。"""


_FORMAT = {
    WIRE_ANTHROPIC_MESSAGES: lf.FormatName.ANTHROPIC_MESSAGES,
    WIRE_OPENAI_CHAT: lf.FormatName.OPENAI_CHAT_COMPLETIONS,
    WIRE_OPENAI_RESPONSES: lf.FormatName.OPEN_RESPONSES,
}

# stop_reason 映射（anthropic 端直接交给 linguafranca，这里只用于错误体拼装，
# 实际响应转换不经过本模块的映射表）。


def _fmt(wire: str) -> lf.FormatName:
    try:
        return _FORMAT[wire]
    except KeyError:
        raise WireConversionError(f"未知 wire: {wire!r}") from None


def _normalize_developer_role(payload: dict) -> dict:
    """把 messages 里的 ``developer`` role 归一为 ``system``（原地改）。

    两层触发：
      1. 源侧 —— 新版 Claude Code 可能发 ``role: developer``（Anthropic 对
         system 的新别名），linguafranca 的 anthropic 源 schema 不认
         ``developer``，会直接 SchemaValidationError。
      2. 目标侧 —— linguafranca 把 anthropic 顶层 ``system`` 字段转成
         openai 的 ``developer`` role message（OpenAI 新版语义），但 deepseek
         官方等 openai 兼容端点只认 ``system``，``developer`` 直接 400。
    ``system`` 三种 wire 全认（Anthropic / OpenAI Chat / Open Responses），
    归一安全。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "developer":
            m["role"] = "system"
    return payload


def _stash_instructions(payload: dict, src_wire: str) -> Optional[str]:
    """源侧 Open Responses 时把顶层 ``instructions`` 抽出来，linguafranca
    v0.3.14 会把它直接吃掉（不映射成 anthropic 顶层 system）。"""
    if src_wire != WIRE_OPENAI_RESPONSES:
        return None
    inst = payload.get("instructions")
    if isinstance(inst, str) and inst:
        payload.pop("instructions", None)
        return inst
    return None


def _restore_instructions(
    payload: dict, dst_wire: str, stashed: Optional[str],
) -> None:
    """目标侧收尾：把 stash 的 instructions 写回；把 linguafranca 把
    anthropic 顶层 system 折成的 ``input[0]{role:developer}`` 提升为顶层
    ``instructions`` 并从 input 里剔除。

    两种触发：
      1. ``stashed`` 非空 —— 源是 openai-responses，dst=anthropic 写顶层
         system；dst=openai-chat 插 messages[0]{role:system}（linguafranca
         不一定保留了空 messages，所以按 list/isinstance 防御）。
      2. dst=openai-responses 且 input[0] 是 developer —— 把它的 content
         提升为顶层 instructions，从 input 里删除（也覆盖 anthropic 源
         不走 stash 的情况）。
    """
    if dst_wire == WIRE_ANTHROPIC_MESSAGES and stashed:
        payload["system"] = stashed
        return
    if dst_wire == WIRE_OPENAI_CHAT and stashed:
        # linguafranca 对 responses→chat 的产物里 messages 可能只有 user
        # 条，前面没 system 占位。这里直接 prepend 一条 system message。
        items = payload.get("messages")
        if isinstance(items, list):
            if not items or items[0].get("role") != "system":
                items.insert(0, {"role": "system", "content": stashed})
        else:
            payload["messages"] = [{"role": "system", "content": stashed}]
        return
    if dst_wire == WIRE_OPENAI_RESPONSES:
        # 优先用 stashed（responses→responses 透传保持不变），否则从 input
        # 里把 developer role item 提升为顶层 instructions。
        items = payload.get("input")
        if isinstance(items, list) and items:
            head = items[0]
            if isinstance(head, dict) and head.get("role") == "developer":
                content = head.get("content")
                if isinstance(content, str) and content:
                    payload["instructions"] = content
                payload["input"] = items[1:]
            elif stashed and "instructions" not in payload:
                payload["instructions"] = stashed
        elif stashed and "instructions" not in payload:
            payload["instructions"] = stashed


def _collapse_developer_role_in_responses(payload: dict, src_wire: str) -> None:
    """源侧 Open Responses 时把 input 里 role=developer 归一为 system。

    linguafranca v0.3.14 的 responses 源 schema 不认 developer role。
    DeepSeek Responses 视 developer 等同 system，这里归一即可。
    """
    if src_wire != WIRE_OPENAI_RESPONSES:
        return
    items = payload.get("input")
    if not isinstance(items, list):
        return
    for i in items:
        if isinstance(i, dict) and i.get("role") == "developer":
            i["role"] = "system"


def convert_request(payload: dict, src_wire: str, dst_wire: str) -> dict:
    """请求体转换。schema 校验失败抛 WireConversionError。

    V0.3 扩展层：优先分派给插件注册的 (src, dst) 转换器；插件转换器
    返回 ``None`` 或未注册时回退内置 linguafranca。
    """
    from .plugin import wire_converter_for
    plugin_converter = wire_converter_for(src_wire, dst_wire)
    if plugin_converter is not None:
        converted = plugin_converter(payload, src_wire, dst_wire)
        if converted is not None:
            return converted
    # 源侧归一：Claude Code 的 developer role 消息先转 system，否则 linguafranca
    # 源 schema 校验就挂。
    _normalize_developer_role(payload)
    # 源侧 Open Responses 时把 input 里的 developer role 归一为 system
    # （linguafranca v0.3.14 的 responses 源 schema 不认 developer）。
    _collapse_developer_role_in_responses(payload, src_wire)
    # 源侧 Open Responses 时抽出 instructions（linguafranca v0.3.14 会
    # 直接吃掉它，不翻成 anthropic 顶层 system / 也不翻成 responses 顶层
    # instructions，需要手动 stash + restore）。
    stashed = _stash_instructions(payload, src_wire)
    try:
        r = lf.convert_request_json(payload, source_format=_fmt(src_wire),
                                    target_format=_fmt(dst_wire))
    except lf.SchemaValidationError as exc:
        raise WireConversionError(str(exc)) from exc
    for w in r.warnings:
        # 有损转换提示（如 frequency_penalty 被丢弃），当前静默容忍。
        pass
    # 目标侧归一：linguafranca 转出的 developer message（来自顶层 system 字段）
    # 改回 system，兼容 deepseek 官方等只认 system 的 openai 端点。
    _normalize_developer_role(r.value)
    # 还原 instructions —— dst=anthropic → 顶层 system；dst=responses 且
    # 源是 anthropic 时把 developer 折成的 system item 提升为顶层 instructions。
    _restore_instructions(r.value, dst_wire, stashed)
    return r.value


def convert_response(payload: dict, src_wire: str, dst_wire: str) -> dict:
    """非流式响应转换。schema 校验失败抛 WireConversionError。"""
    try:
        r = lf.convert_response_json(payload, source_format=_fmt(src_wire),
                                     target_format=_fmt(dst_wire))
    except lf.SchemaValidationError as exc:
        raise WireConversionError(str(exc)) from exc
    return r.value


def convert_stream(
    events: AsyncIterator[dict], src_wire: str, dst_wire: str,
) -> AsyncIterator[dict]:
    """流式事件转换。输入是上游 SSE 解析出的 JSON 事件 async 迭代器，
    输出是目标格式的事件 dict async 迭代器。

    v0.116：linguafranca v0.3.14 不支持 3 个流式透传（ant→ant / chat→chat /
    resp→resp），会抛 ``UnsupportedConversionError``。本模块做兜底 —— src==dst
    时直接 yield 输入事件，与 ``convert_request`` / ``convert_response`` 的
    passthrough 语义保持一致。
    """
    if src_wire == dst_wire:
        return events  # type: ignore[return-value]
    return lf.aconvert_response_stream(events, source_format=_fmt(src_wire),
                                       target_format=_fmt(dst_wire))


def convert_stream_sync(
    events: Iterable[dict], src_wire: str, dst_wire: str,
) -> Iterable[dict]:
    """同步版，测试用。src==dst 时直接透传（见 ``convert_stream`` 注释）。"""
    if src_wire == dst_wire:
        return events
    return lf.convert_response_stream_json(events, source_format=_fmt(src_wire),
                                           target_format=_fmt(dst_wire))


def serialize_sse_event(event: dict, target_wire: str) -> bytes:
    """把转换后的单个事件 dict 拼成 SSE 字节。

    openai-chat：`data: {...}\\n\\n`（无 event 名）。
    anthropic-messages / openai-responses：`event: <type>\\ndata: {...}\\n\\n`。
    """
    data = json.dumps(event, ensure_ascii=False).encode("utf-8")
    if target_wire == WIRE_OPENAI_CHAT:
        return b"data: " + data + b"\n\n"
    etype = event.get("type", "message")
    return f"event: {etype}\r\ndata: ".encode() + data + b"\r\n\r\n"


def error_to_client(err: dict, dst_wire: str) -> dict:
    """把上游错误体统一转成客户端平台格式。

    openai-chat / openai-responses 错误体是 ``{"error": {...}}``；anthropic 是
    ``{"type":"error","error":{...}}``。目标格式决定输出形状。
    """
    detail = err.get("error") if isinstance(err, dict) else None
    if not isinstance(detail, dict):
        detail = {"type": "api_error", "message": str(err)}
    if dst_wire == WIRE_ANTHROPIC_MESSAGES:
        return {
            "type": "error",
            "error": {
                "type": detail.get("type") or "api_error",
                "message": detail.get("message") or str(detail),
            },
        }
    return {"error": detail}


def is_stream(payload: dict) -> bool:
    """判断请求是否流式（跨格式判断用，兼容三种格式的字段）。"""
    if isinstance(payload.get("stream"), bool):
        return payload["stream"]
    return False
