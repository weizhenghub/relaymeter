"""proxy/_util.py —— 字符串/字节 mask、preview、token 估算。

V0.119+ 从 ``proxy.py`` 拆出。无状态模块；只依赖标准库 + ``re``。

公开稳定（re-export via ``relay.proxy.__init__``）：

* ``_mask(v, keep=4)``
* ``_redact_headers(headers)``
* ``_trunc(s, n=4000)``
* ``_body_preview(body, n=4000)``
* ``_estimate_output_tokens(text)``
* ``_sse_preview(chunk, n=240)``
* ``_strip_thinking_content(content)``

⚠ deprecated for new code —— Phase 4 起由 ``relay.services`` 接管（或按需
复制到 ``relay.proxy._util`` 内）。
"""

from __future__ import annotations

import re

__all__ = [
    "_SENSITIVE_HDRS",
    "_OPENAI_CREATED_ZERO_RE",
    "_mask", "_redact_headers", "_trunc", "_body_preview",
    "_estimate_output_tokens", "_sse_preview", "_strip_thinking_content",
]


# ---- 静态常量 ----

_SENSITIVE_HDRS = {"authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"}


# v0.12.1：openai 直通流 created 归一化 —— 匹配 `"created":0` / `"created": 0`。
# minnimax 等上游把时间戳发成 0，部分 openai 兼容客户端显示异常。
_OPENAI_CREATED_ZERO_RE = re.compile(rb'"created"\s*:\s*0')


# ---- 公开函数 ----


def _mask(v: str, keep: int = 4) -> str:
    v = "" if v is None else str(v)
    if not v:
        return "(empty)"
    if len(v) <= keep * 2 + 3:
        return "***"
    return v[:keep] + "..." + v[-keep:]


def _redact_headers(headers) -> dict:
    out: dict = {}
    if not headers:
        return out
    for k, v in headers.items():
        if str(k).lower() in _SENSITIVE_HDRS:
            out[k] = _mask(v)
        else:
            out[k] = v
    return out


def _trunc(s, n: int = 4000) -> str:
    s = s if isinstance(s, str) else str(s)
    if len(s) <= n:
        return s
    return s[:n] + f"... [truncated {len(s) - n} chars]"


def _body_preview(body: bytes, n: int = 4000) -> str:
    try:
        txt = body.decode("utf-8")
    except Exception:
        return f"<{len(body)} bytes binary>"
    return _trunc(txt, n)


def _estimate_output_tokens(text: str) -> int:
    """v0.113x：粗估字符数 → token 数，供侧栏实时跳动。

    Anthropic SSE 协议只在 message_delta 时一次性发 cumulative
    output_tokens；流中间不更新（OpenAI 上游 linguafranca 解析器倒是逐帧
    更新 parser.usage.output_tokens，但跨协议路径走 assembled_text 推时仍
    可能有帧间空隙）。侧栏要实时跳动，用字符长度粗估；**只用于侧栏显示**，
    不入 db.record（计费仍以 message_delta 真实值为准）。

    估算口径：CJK 字符 1.5 字符/token，中文标点算 CJK；其它 4 字符/token
    （英文 / 代码 / 数字 / 空格）。混排按字符类别分段折算。实测误差
    典型 ±20%，对实时显示够用。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x303F  # CJK 标点
        ):
            cjk += 1
        else:
            other += 1
    # 至少 1 token，避免 0 让侧栏看起来"卡住"。
    return max(1, int(round(cjk / 1.5 + other / 4)))


def _sse_preview(chunk: bytes, n: int = 240) -> str:
    """Decode one SSE chunk and show the `data:` payloads (truncated)."""
    try:
        txt = chunk.decode("utf-8", "replace")
    except Exception:
        return f"<{len(chunk)} bytes binary>"
    lines = []
    for ln in txt.split("\n"):
        ln = ln.rstrip("\r")
        if ln.startswith("data:"):
            payload = ln[5:].strip()
            if payload == "[DONE]":
                lines.append("[DONE]")
            else:
                lines.append(_trunc(payload, n))
    if not lines:
        return _trunc(txt, n)
    return " | ".join(lines)


def _strip_thinking_content(content: list) -> list:
    """Drop ``type=="thinking"`` blocks from a response content array."""
    return [b for b in content if isinstance(b, dict) and b.get("type") != "thinking"]