"""Streaming HTTP relay core.

This is the only place in the codebase that talks to upstream model APIs. The
contract is: given an incoming FastAPI `Request`, a `platform` name, and a
parser factory, return a `StreamingResponse` that:

1. Looks up the platform's currently-active `PlatformConfig` from
   `app.state.settings`, so the user can switch upstreams at runtime.
2. Forwards request headers + body verbatim, after hop-by-hop filtering and
   after optionally replacing the configured auth header (when the active
   config has an `api_key`).
3. Streams the response back to the client chunk-by-chunk.
4. Simultaneously feeds those chunks into the SSE usage parser.
5. On stream end (or client disconnect or upstream error), records ONE row
   in SQLite with the captured usage, the final status code, the chosen
   upstream's name, and the key alias (if any) for later cost attribution.

The teeing adds zero network latency because parser.feed() runs on the same
event-loop tick as the `yield` — no extra awaits, no extra IO.

⚠ 内部接口稳定性（v0.117+ 起冻结）：

本模块导出的**下划线开头**符号（``_client_pool`` / ``_get_client`` /
``_close_http_clients`` / ``_system_proxy_url`` / ``_proxy_alive_cache`` /
``_InFlight`` / ``_in_flight`` / ``_in_flight_done`` / ``_SUBSCRIBERS`` /
``_broadcast`` / ``_broadcast_live_event`` / ``_REASONING_BY_TOOL_CALL`` 等）
是**实现细节**，不在稳定 API 之内。

Phase 0（v0.117）起视为「私生子调用」—— 不推荐新代码再依赖它们；Phase 2
起会逐步迁到 ``src/relay/services/`` 下的独立服务（``HttpClientPool`` /
``InflightStore`` / ``LiveBus`` / ``AlertLog`` / ``ReasoningCache``），
通过 ``ctx.svc(...)`` 注入。

迁移清单与时间表：``docs/architecture/STABILITY.md`` §2 + 整体节奏见
``docs/architecture/00_context_overview_v1.md`` §3.5。

唯一**对外稳定**符号：``relay()`` 函数本体（路由入口调用的那个）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time
import uuid
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .config import (
    PlatformConfig,
    WIRE_ANTHROPIC_MESSAGES,
    WIRE_OPENAI_CHAT,
    WIRE_OPENAI_RESPONSES,
    infer_wire_for_platform,
    join_endpoint,
)
from .db import Database
from .headers import filter_request_headers, filter_response_headers
from .agent import resolve_agent, sniff_agent
from .models import UsageAcc
from .plugin import (
    arun_overrides,
    auth_scheme_for,
    emit_event,
    parser_factory_for,
    run_hooks,
)


log = logging.getLogger("relay.proxy")


# ---- V0.117+ RelayContext 桥（Phase 2.10）----
# ``relay()`` 入口拿到的 request.app.state 上挂着 ctx（lifespan 注入）。
# 早期内部 fn（``_get_client`` / ``_register_inflight`` 等）仍按模块全局走；
# 新代码 / 横向模块应优先 ``app_state.ctx.svc(...)`` 解析服务。
def _ctx_from(app_state: Any) -> "Any | None":
    """从 ``request.app.state`` 取 ``RelayContext``；可能为 None（早期 test）。"""
    return getattr(app_state, "ctx", None)


# ===== 极端详尽追踪日志（无人值守调试用）=====
import os as _os

_PROJECT_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_TRACE_PATH = _os.path.join(_PROJECT_ROOT, "relay_trace.log")

_tlog = logging.getLogger("relay.trace")
if not _tlog.handlers:
    try:
        _fh = logging.FileHandler(_TRACE_PATH, encoding="utf-8")
        _fh.setLevel(logging.DEBUG)
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _tlog.addHandler(_fh)
        _tlog.setLevel(logging.DEBUG)
        _tlog.propagate = False
    except Exception:
        pass

_SENSITIVE_HDRS = {"authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"}


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


# v0.12.1：openai 直通流 created 归一化 —— 匹配 `"created":0` / `"created": 0`。
# minnimax 等上游把时间戳发成 0，部分 openai 兼容客户端显示异常。
_OPENAI_CREATED_ZERO_RE = re.compile(rb'"created"\s*:\s*0')


# ---------------------------------------------------------------------------
# Anthropic-protocol adapter for OpenCode Go and similar dual-protocol providers
# ---------------------------------------------------------------------------

def _strip_thinking_content(content: list) -> list:
    """Drop ``type=="thinking"`` blocks from a response content array."""
    return [b for b in content if isinstance(b, dict) and b.get("type") != "thinking"]


def _proxy_alive(proxy_url: str) -> bool:
    """True if a TCP connect to the proxy host:port succeeds.

    A stale proxy entry (dead Clash/V2Ray, old PyCharm-injected env var,
    leftover WinINET setting) makes every httpx attempt die with
    ``All connection attempts failed`` while direct connections work —
    the probe tells the two apart.
    """
    try:
        parts = urlsplit(proxy_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except Exception:
        return False


_proxy_alive_cache: tuple[float, str | None, bool] | None = None
_PROXY_PROBE_TTL = 60.0


_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)


# Shared httpx clients keyed by proxy mode (None = direct). Reusing a
# client keeps the TLS session + connection pool warm across requests —
# a fresh AsyncClient per request pays a full handshake (~100-300 ms to
# a remote gateway) on every call. Each entry is tagged with the
# httpx.AsyncClient reference it was built with: tests monkey-patch that
# class, so an identity mismatch makes us rebuild instead of handing back
# a client whose transport belongs to another test.
_client_pool: dict[Optional[str], tuple[type, httpx.AsyncClient]] = {}


def _get_client(upstream_url: str) -> httpx.AsyncClient:
    """Return the shared client for an upstream URL (direct vs proxy)."""
    key = _system_proxy_url(upstream_url)
    entry = _client_pool.get(key)
    if entry is not None and entry[0] is httpx.AsyncClient:
        return entry[1]
    client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False, proxy=key)
    _client_pool[key] = (httpx.AsyncClient, client)
    return client


async def _close_http_clients() -> None:
    """Close every pooled client. Called at server shutdown."""
    for _, client in _client_pool.values():
        try:
            await client.aclose()
        except Exception:
            pass
    _client_pool.clear()


def _system_proxy_url(url: str | None = None) -> str | None:
    """Return the system proxy for an upstream URL (v0.8.5).

    httpx only honours ``HTTP_PROXY``/``HTTPS_PROXY`` env vars, NOT the
    WinINET registry proxy that Windows apps (browsers, ``urllib``) use.
    Read both (env first, registry fallback, exactly like ``urllib``) and
    route through the result — but only if the proxy is actually alive:
    a stale entry pointing at a dead local port fails every attempt with
    ``All connection attempts failed``. Localhost targets always bypass
    the proxy. The liveness verdict is cached for 60 s.
    """
    if url:
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = ""
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
            return None
    proxies = urllib.request.getproxies()
    candidate = proxies.get("https") or proxies.get("http") or None
    if not candidate:
        return None
    global _proxy_alive_cache
    now = time.monotonic()
    if (
        _proxy_alive_cache is not None
        and now - _proxy_alive_cache[0] < _PROXY_PROBE_TTL
        and _proxy_alive_cache[1] == candidate
    ):
        return candidate if _proxy_alive_cache[2] else None
    alive = _proxy_alive(candidate)
    _proxy_alive_cache = (now, candidate, alive)
    return candidate if alive else None


# Valid Anthropic error ``type`` discriminators (the SDK Zod schemas
# reject anything else). An upstream like OpenCode Go answers errors with
# ``{"error": {"type": "upstream error", ...}}`` which is NOT in this set
# — opencode's client then fails to parse the response and the user sees
# "invalid union / no matching discriminator" instead of the real message.
_ANTHROPIC_ERROR_TYPES = frozenset({
    "invalid_request_error", "authentication_error", "permission_error",
    "not_found_error", "request_too_large", "rate_limit_error",
    "api_error", "overloaded_error",
})


def _error_body(platform: str, message: str, etype: str = "api_error") -> dict:
    """Build an error body in the platform's wire format (v0.9).

    Anthropic: ``{"error": {"type", "message"}}``; OpenAI: ``{"error":
    {"message", "type"}}``. ``etype`` must be a valid Anthropic
    discriminator — callers that relay an upstream's own error should run
    it through :func:`_normalize_upstream_error` instead.
    """
    if platform == "openai":
        return {"error": {"message": message, "type": etype}}
    return {"error": {"type": etype, "message": message}}


def _normalize_upstream_error(body: dict) -> dict:
    """Coerce a possibly foreign upstream error body into the Anthropic
    error shape with a valid ``type`` (v0.9).

    Keeps the upstream's ``message`` and optional ``code``, swaps any
    non-whitelisted ``type`` for ``api_error`` so Anthropic clients can
    actually parse it.
    """
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        msg = (
            err.get("message") if isinstance(err, dict)
            else (body.get("message") if isinstance(body, dict) else None)
        )
        return {"error": {"type": "api_error", "message": msg or "upstream error"}}
    msg = err.get("message")
    t = err.get("type")
    if not isinstance(t, str) or t not in _ANTHROPIC_ERROR_TYPES:
        t = "api_error"
    out = {"type": t, "message": msg or "upstream error"}
    if isinstance(err.get("code"), (str, int)):
        out["code"] = err["code"]
    return {"error": out}


def _anthropic_messages_url(base_url: str) -> str:
    """Compose the upstream ``/v1/messages`` URL from a configured base URL.

    The base URL may already carry part of the path depending on how the
    upstream was configured:
      - ``.../v1/messages`` → use as-is (already complete)
      - ``.../v1``          → append ``/messages`` only (e.g.
        ``https://opencode.ai/zen/go/v1``). Appending ``/v1/messages`` here
        would produce ``/v1/v1/messages`` → upstream 404.
      - anything else       → append ``/v1/messages``
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _sanitize_anthropic_payload(payload: dict) -> dict:
    """Normalise payload quirks the OpenCode Go gateway rejects (v0.8.3).

    The gateway answers HTTP 400 with a bare ``{"model": ...}`` body for:
      1. ``tool_choice`` in string form (``"auto"`` / ``"none"`` / ``"any"``)
         — only the object form ``{"type": ...}`` is accepted;
      2. ``max_tokens`` that is missing, 0, or negative;
      3. message ``content`` blocks that are empty lists.

    Each is rewritten to the accepted shape here, in place, so clients that
    send the stricter forms (OpenCode sends a string ``tool_choice``) do not
    get a hard 400 from the gateway.
    """
    tc = payload.get("tool_choice")
    if isinstance(tc, str):
        payload["tool_choice"] = {"type": tc}
    mt = payload.get("max_tokens")
    if not isinstance(mt, int) or mt <= 0:
        payload["max_tokens"] = 1024
    for msg in payload.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, list) and not content:
            msg["content"] = [{"type": "text", "text": ""}]
    return payload


async def _anthropic_adapter_relay(
    *,
    request: Request,
    cfg: PlatformConfig,
    inflight_id: str,
    model: Optional[str],
    body: bytes,
    platform: str,
    db: Database,
    parser_factory: Callable[[], _HasFeed],
    api_key_alias: Optional[str],
    # v0.143：客户端入口 wire（chat / responses / messages）。
    # 透传到内部 record() 写入 endpoint 列，让「平台分布」按
    # (platform, endpoint) 拆分。None → 写 NULL（init() 回填兜底）。
    endpoint: Optional[str] = None,
    # v0.155：客户端工具名，透传到内部 record() 写 agent 列。
    agent: str = "",
    # v0.157：原始 User-Agent 头原文，透传到内部 record() 写 raw_ua 列。
    raw_ua: str = "",
) -> Response:
    """Anthropic-protocol adapter (stream + non-stream).

    Used when ``cfg.requires_anthropic_adapter`` is True — the upstream
    accepts Anthropic protocol but requires protocol-level fixes:
      1. Replace ``Authorization: Bearer`` with ``x-api-key: <cfg.api_key>``
      2. Normalise model to lowercase (``"MiniMax-M3"`` → ``"minimax-m3"``)
      3. Strip ``type=="thinking"`` content blocks (and ``ping`` keepalive)
         from the SSE stream so the client never sees them.

    Two paths fork on the inbound ``stream`` flag:
      - ``stream=true``  → SSE event-level filtering, streamed back via
        ``StreamingResponse`` so the client gets Anthropic-format events
        (``message_start`` → ``content_block_start`` → ``content_block_delta``
        → ``message_delta`` → ``message_stop``) with thinking stripped.
      - ``stream=false`` → buffer + reshape, return a single JSON message.

    Upstream must answer ``/v1/messages`` in Anthropic protocol format.
    """
    # Parse inbound body.
    try:
        inbound: dict = json.loads(body.decode("utf-8"))
    except Exception:
        return Response(
            content=json.dumps({"error": "invalid_request_error", "message": "bad json body"}).encode(),
            status_code=400, media_type="application/json",
        )

    is_stream = bool(inbound.get("stream"))
    # v0.163：adapter 路径同样缺 settings —— 流式/非流式 finally 里
    # ``settings.relay_save_messages`` 会 NameError，与 cross-wire 同源回归。
    settings = request.app.state.settings

    # Normalise model to lowercase (OpenCode anthropic endpoint rejects "MiniMax-M3").
    raw_model = inbound.get("model") or ""
    norm_model = raw_model.lower()

    # Headers: x-api-key replaces whatever auth the client sent.
    upstream_headers = {
        "x-api-key": cfg.api_key or "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        # Honor what the client asked for; SSE clients send
        # ``text/event-stream``, JSON clients send ``application/json``.
        "accept": request.headers.get("accept")
            or ("text/event-stream" if is_stream else "application/json"),
    }
    # OpenCode anthropic endpoint is /v1/messages regardless of what the
    # client requested, but cfg.url may already include part of the path
    # (PoC convention — see poc_relay_9999_sse_v2.py). _anthropic_messages_url
    # handles the /v1/v1/messages double-append case.
    upstream_url = _anthropic_messages_url(cfg.url)

    _tlog.info(
        "ADAPTER ENTER platform=%s upstream=%s url=%s model=%s->%s stream=%s auth=%s body=%s",
        platform, cfg.name, upstream_url, raw_model, norm_model, is_stream,
        _mask(cfg.api_key or ""), _body_preview(body, 2000),
    )

    await _set_inflight_phase(inflight_id, "calling")

    if is_stream:
        return await _anthropic_adapter_relay_sse(
            inbound=inbound,
            upstream_url=upstream_url,
            upstream_headers=upstream_headers,
            cfg=cfg,
            inflight_id=inflight_id,
            model=norm_model,
            platform=platform,
            db=db,
            parser_factory=parser_factory,
            api_key_alias=api_key_alias,
            # v0.143：客户端入口 wire 透传到 record 站点。
            endpoint=endpoint,
            # v0.163：透传 settings，sse finally 里 record_messages 要用。
            settings=settings,
            # v0.163：透传 agent/raw_ua —— sse finally 里 record() 写这两列，
            # 此前漏传直接 NameError（与 settings/agent 同源回归）。
            agent=agent,
            raw_ua=raw_ua,
        )

    # ---- Non-streaming branch (original behaviour) -------------------------

    # Rebuild payload with minimal fields OpenCode accepts.
    payload: dict = {
        "model": norm_model,
        "messages": inbound.get("messages") or [],
        "max_tokens": inbound.get("max_tokens") or 1024,
    }
    if "system" in inbound:
        payload["system"] = inbound["system"]
    # v0.11.19+：透传思考参数 —— 重建 payload 时不能丢任何思考挡位字段，
    # 否则非流式路径下 OpenAI reasoning / Gemini thinkingConfig 等形状会被拦截。
    # 只拷贝思考相关键（不整段转发，避免把客户端私有/中继内部字段透给上游）。
    for _tk in ("thinking", "reasoning", "reasoning_effort", "reasoningEffort", "thinkingConfig", "output_config"):
        if _tk in inbound:
            payload[_tk] = inbound[_tk]
    _sanitize_anthropic_payload(payload)

    # v0.98.1 插件钩子：pre_upstream（adapter 非流式路径）—— 发送前最后
    # 一改。body 是重建后的 payload；插件可整体替换 body / headers。
    adapter_pinfo: dict[str, Any] = {
        "platform": platform,
        "cfg": cfg,
        "model": norm_model,
        "body": json.dumps(payload).encode("utf-8"),
        "headers": upstream_headers,
        "upstream_url": upstream_url,
    }
    await run_hooks("pre_upstream", adapter_pinfo)
    upstream_headers = adapter_pinfo["headers"]

    try:
        resp = await _get_client(upstream_url).post(
            upstream_url, content=adapter_pinfo["body"], headers=upstream_headers
        )
    except httpx.HTTPError as exc:
        log.warning("%s adapter upstream error: %s", platform, exc)
        try:
            await db.record(
                platform=platform, model=norm_model, request_id=None,
                usage=UsageAcc(), status_code=0,
                error=f"upstream_error: {exc}", upstream=cfg.name,
                api_key_alias=api_key_alias,
                # v0.143：客户端入口 wire（拆平台分布用）。
                endpoint=endpoint,
                agent=agent,
                raw_ua=raw_ua,
            )
        except Exception:
            pass
        try:
            await _complete_inflight(inflight_id)
        except Exception:
            pass
        return Response(
            content=json.dumps(_error_body(platform, str(exc))).encode(),
            status_code=502, media_type="application/json",
        )

    status = resp.status_code
    try:
        upstream_json = resp.json()
    except Exception:
        upstream_json = {}

    usage = upstream_json.get("usage") or {}
    parser = parser_factory()
    try:
        parser.feed(resp.text.encode())
    except Exception:
        pass

    # Record usage in DB.
    req_db_id = 0
    try:
        req_db_id = await db.record(
            platform=platform,
            model=norm_model,
            request_id=None,
            usage=UsageAcc(
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens") or 0,
                cache_read_input_tokens=usage.get("cache_read_input_tokens") or 0,
            ),
            status_code=status,
            error=None if 200 <= status < 300 else f"upstream {status}",
            upstream=cfg.name,
            api_key_alias=api_key_alias,
            # v0.143：见 adapter connect-error 注释。
            endpoint=endpoint,
            agent=agent,
            raw_ua=raw_ua,
        )
    except Exception:
        log.exception("db.record failed in adapter path")
    # v0.120：adapter 非流式路径也保存消息原文
    if req_db_id and settings.relay_save_messages:
        try:
            user_text, user_json = _extract_last_user_message(adapter_pinfo["body"])
            await db.record_messages(
                req_db_id,
                user_text=user_text,
                user_json=user_json,
                assistant_text=parser.assembled_text() or None,
                assistant_json=resp.text if resp.text else None,
                thinking_text=parser.assembled_thinking() or None,
            )
        except Exception as exc:
            log.exception("db.record_messages failed in adapter path: %s", exc)
    try:
        await _complete_inflight(inflight_id)
    except Exception:
        pass

    if status != 200:
        # v0.9：错误体归一化成 Anthropic/OpenAI 平台格式 —— 网关返回的
        # {"type": "upstream error"} 会被 opencode 客户端 Zod 拒绝。
        if isinstance(upstream_json, dict):
            err_body = _normalize_upstream_error(upstream_json)
        else:
            err_body = _error_body(platform, f"upstream {status}")
        # v0.98.1 插件钩子：post_response（adapter 非流式错误分支）。
        adapter_rinfo: dict[str, Any] = {
            "platform": platform,
            "cfg": cfg,
            "model": norm_model,
            "status": status,
            "usage": UsageAcc(**usage) if isinstance(usage, dict) else UsageAcc(),
            "error": f"upstream_{status}",
            "req_db_id": None,
            "upstream": cfg.name,
            "request_id": None,
            "streaming": False,
        }
        await run_hooks("post_response", adapter_rinfo)
        emit_event("request.done", **adapter_rinfo)
        return Response(
            content=json.dumps(err_body).encode(),
            status_code=status, media_type="application/json",
        )

    # Shape response: strip thinking blocks, keep text.
    content = upstream_json.get("content") or []
    if isinstance(content, list):
        content = _strip_thinking_content(content)

    shaped = {
        "id": upstream_json.get("id") or "poc-0",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": norm_model,
        "stop_reason": upstream_json.get("stop_reason") or "end_turn",
        "stop_sequence": upstream_json.get("stop_sequence"),
        "usage": {
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens") or 0,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
        },
    }

    await _set_inflight_phase(inflight_id, "done")
    # v0.98.1 插件钩子：post_response（adapter 非流式成功分支）。
    adapter_rinfo = {
        "platform": platform,
        "cfg": cfg,
        "model": norm_model,
        "status": 200,
        "usage": UsageAcc(**usage) if isinstance(usage, dict) else UsageAcc(),
        "error": None,
        "req_db_id": None,
        "upstream": cfg.name,
        "request_id": None,
        "streaming": False,
    }
    await run_hooks("post_response", adapter_rinfo)
    emit_event("request.done", **adapter_rinfo)
    return Response(
        content=json.dumps(shaped).encode(),
        status_code=200, media_type="application/json",
    )


async def _anthropic_adapter_relay_sse(
    *,
    inbound: dict,
    upstream_url: str,
    upstream_headers: dict,
    cfg: PlatformConfig,
    inflight_id: str,
    model: str,
    platform: str,
    db: Database,
    parser_factory: Callable[[], _HasFeed],
    api_key_alias: Optional[str],
    # v0.143：见 _anthropic_adapter_relay 注释。
    endpoint: Optional[str] = None,
    # v0.163：finally 里 record_messages 要读 relay_save_messages，由调用方传入。
    settings: Any = None,
    # v0.163：透传到 finally 的 record() 写 agent/raw_ua 列。
    agent: str = "",
    raw_ua: str = "",
) -> Response:
    """SSE branch of the adapter.

    Opens the upstream with ``httpx.AsyncClient.stream(...)``, reads the
    raw SSE bytes, and re-emits them with thinking/keepalive events
    dropped. The client sees a clean Anthropic-format SSE stream and
    nothing about the ``x-api-key`` rewrite leaks out.

    Usage accounting happens in the generator's ``finally``: we re-parse
    the message_delta's usage block when it arrives, then record the row
    on close. If the stream dies early we still record what we have so
    the live panel doesn't show a ghost row.
    """
    payload: dict = dict(inbound)
    payload["model"] = model
    payload.setdefault("stream", True)
    _sanitize_anthropic_payload(payload)

    parser = parser_factory()

    # Usage is split across two events in the Anthropic SSE protocol:
    # ``message_start.message.usage`` carries input + cache tokens, while
    # ``message_delta.usage`` carries the cumulative output_tokens. We
    # must not let the latter overwrite the former (delta often omits the
    # input/cache fields, and any future zero-value would clobber).
    last_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    error_msg: Optional[str] = None
    final_status: int = 200

    async def gen() -> AsyncIterator[bytes]:
        nonlocal last_usage, error_msg, final_status
        # v0.166：与 cross-wire / direct 同款 shield 修复 —— 客户端断开时
        # starlette 会 cancel 本任务，finally 内第一个 await（db.record 等）
        # 会被再次 CancelledError 打断导致漏记。收尾逻辑抽成闭包，finally
        # 用 asyncio.shield 保护，让记录在取消信号到达后仍能跑完。
        async def _adapter_finalize() -> None:
            req_db_id = 0
            try:
                req_db_id = await db.record(
                    platform=platform,
                    model=model,
                    request_id=None,
                    usage=UsageAcc(**last_usage),
                    status_code=final_status if error_msg is None else 0,
                    error=error_msg,
                    upstream=cfg.name,
                    api_key_alias=api_key_alias,
                    # v0.143：客户端入口 wire 透传 → 写 endpoint。
                    endpoint=endpoint,
                    agent=agent,
                    raw_ua=raw_ua,
                )
            except Exception:
                log.exception("db.record failed in adapter SSE path")
            # v0.120：adapter SSE 路径也保存消息原文
            if req_db_id and settings.relay_save_messages:
                try:
                    user_text, user_json = _extract_last_user_message(json.dumps(inbound).encode("utf-8"))
                    await db.record_messages(
                        req_db_id,
                        user_text=user_text,
                        user_json=user_json,
                        assistant_text=parser.assembled_text() or None,
                        assistant_json=None,  # SSE 没有 raw_response
                        thinking_text=parser.assembled_thinking() or None,
                    )
                except Exception as exc:
                    log.exception("db.record_messages failed in adapter SSE path: %s", exc)
            try:
                await _complete_inflight(inflight_id)
            except Exception:
                pass
            # v0.89：广播 done —— 侧栏定稿。thinking/tool_use 此时已经累完，
            # 一并推过去（payload 不大，前端按需消费）。
            try:
                tool_json = parser.assembled_tool_use_json() or ""
                thinking_final = parser.assembled_thinking() or ""
                await _broadcast_live_event(
                    inflight_id, "done",
                    phase="done",
                    assistant_text=parser.assembled_text(),
                    thinking_text=thinking_final,
                    tool_use_json=tool_json,
                    usage_live={
                        "input_tokens": last_usage["input_tokens"],
                        "output_tokens": last_usage["output_tokens"],
                        "cache_read_input_tokens": last_usage["cache_read_input_tokens"],
                        "cache_creation_input_tokens": last_usage["cache_creation_input_tokens"],
                    },
                    error=error_msg,
                )
            except Exception:
                log.exception("broadcast done event failed in adapter SSE path")
            # v0.98.1 插件钩子：post_response（adapter SSE finally 内）+
            # request.done 事件。插件错误已由 run_hooks / emit_event 隔离。
            try:
                rinfo = {
                    "platform": platform,
                    "cfg": cfg,
                    "model": model,
                    "status": final_status if error_msg is None else 0,
                    "usage": UsageAcc(**last_usage),
                    "error": error_msg,
                    "req_db_id": None,
                    "upstream": cfg.name,
                    "request_id": None,
                    "streaming": True,
                }
                await run_hooks("post_response", rinfo)
                emit_event("request.done", **rinfo)
            except Exception:
                log.exception("plugin post_response hooks failed in adapter SSE")

        client = _get_client(upstream_url)
        # v0.98.1 插件钩子：pre_upstream（adapter SSE 路径）—— 发送前最后
        # 一改。body 是重建后的 payload；插件可整体替换 body / headers。
        pinfo = {
            "platform": platform,
            "cfg": cfg,
            "model": model,
            "body": json.dumps(payload).encode("utf-8"),
            "headers": upstream_headers,
            "upstream_url": upstream_url,
        }
        await run_hooks("pre_upstream", pinfo)
        headers = pinfo["headers"]
        out_body_bytes = pinfo["body"]
        try:
            async with client.stream(
                "POST", upstream_url, content=out_body_bytes, headers=headers,
            ) as upstream_resp:
                final_status = upstream_resp.status_code
                if upstream_resp.status_code != 200:
                    err_bytes = await upstream_resp.aread()
                    try:
                        err_payload = json.loads(err_bytes.decode("utf-8", "replace"))
                    except Exception:
                        err_payload = {"error": {"message": err_bytes[:300].decode("utf-8", "replace")}}
                    # v0.9：网关非 200 的错误体先归一化成 Anthropic 形状，
                    # 否则 {"type": "upstream error"} 直接让客户端 Zod 崩。
                    if isinstance(err_payload, dict):
                        err_payload = _normalize_upstream_error(err_payload)
                    else:
                        err_payload = {"error": {"type": "api_error", "message": str(err_payload)[:300]}}
                    yield (
                        f"event: error\ndata: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
                    ).encode("utf-8")
                    error_msg = f"upstream {final_status}"
                    return

                async for out_bytes, parsed in _filter_anthropic_sse(
                    upstream_resp.aiter_raw(), parser,
                ):
                    # v0.12：每收到一个上游事件都刷新流活跃时间戳，即使该
                    # 事件是 thinking/keepalive 被剥离（out_bytes 为空）——
                    # 否则思考模型在 thinking 段会被 quiet monitor 误判成死流
                    # 强杀（STREAM_QUIET_AFTER 已放宽到 120s，双保险）。
                    await _bump_chunk_activity(inflight_id)
                    if parsed is not None:
                        ptype = parsed.get("type")
                        # v0.11.21：上游在 200 流内嵌 error 事件（如思考被拒 /
                        # 限流）时，把它当终止信号。否则 gen() 不退出、
                        # _complete_inflight 不跑，inflight 卡在 streaming，
                        # 左侧「实时」导航波纹会一直空转（必须置为 done）。
                        if ptype == "error" or isinstance(parsed.get("error"), (dict, str)):
                            err = parsed.get("error")
                            if isinstance(err, dict):
                                error_msg = err.get("message") or err.get("type") or "upstream_error"
                            else:
                                error_msg = str(err)
                            # 仍把错误事件透传给客户端（下方 out_bytes yield），
                            # 随后 break 触发 finally 收尾（phase→done + 广播 done+error）。
                        if ptype == "message_start":
                            u = (parsed.get("message") or {}).get("usage") or {}
                        elif ptype == "message_delta":
                            u = parsed.get("usage") or {}
                        else:
                            u = None
                        if isinstance(u, dict):
                            for k in ("input_tokens", "cache_creation_input_tokens",
                                      "cache_read_input_tokens"):
                                if k in u and isinstance(u[k], int):
                                    last_usage[k] = u[k]
                            if "output_tokens" in u and isinstance(u["output_tokens"], int):
                                last_usage["output_tokens"] = u["output_tokens"]
                        # v0.89：每个被过滤出来的可解析事件（含 thinking 块）都
                        # 镜像到 inflight 并广播 —— 侧栏要看实时跳动的内容流、
                        # token 四字段、thinking。不考虑事件类型，逐事件广播是
                        # 正确的：thinking_delta 在 adapter 路径里被剥离，
                        # 但 parser.assembled_thinking() 仍会累积，模型写入
                        # 内容就能被侧栏看到。
                        assistant_now = parser.assembled_text()
                        thinking_now = parser.assembled_thinking() or ""
                        # v0.113x：output_tokens 估算（字符长度粗估），仅供侧
                        # 栏实时跳动显示。message_delta 给的 last_usage 真值仍
                        # 是 db.record 计费的权威值，不被估算覆盖。
                        output_est = _estimate_output_tokens(
                            (assistant_now or "") + (thinking_now or "")
                        )
                        usage_now = {
                            "input_tokens": last_usage["input_tokens"],
                            "output_tokens": last_usage["output_tokens"],
                            "output_tokens_est": max(
                                last_usage["output_tokens"], output_est
                            ),
                            "cache_read_input_tokens": last_usage["cache_read_input_tokens"],
                            "cache_creation_input_tokens": last_usage["cache_creation_input_tokens"],
                        }
                        # v0.111：思考完成信号（gap 判定用）—— 思考块
                        # content_block_stop / </think> 出现后置 True。
                        thinking_done = bool(getattr(parser, "thinking_finished", False))
                        await _update_inflight(
                            inflight_id,
                            assistant_text=assistant_now,
                            thinking_text=thinking_now,
                            thinking_done=thinking_done,
                            usage_live=usage_now,
                        )
                        await _broadcast_live_event(
                            inflight_id, "delta",
                            assistant_text=assistant_now,
                            thinking_text=thinking_now,
                            thinking_done=thinking_done,
                            usage_live=usage_now,
                        )
                    if out_bytes:
                        yield out_bytes
                    # v0.87：message_stop 后主动结束，不等上游关连接（部分
                    # 上游 message_stop 后连接不关闭，挂到 sweeper 90s 强清）。
                    # v0.11.21：上游内嵌 error 事件（error_msg 已置位）同样主动
                    # 结束，避免 inflight 卡在 streaming 让导航波纹空转。
                    if parsed is not None and (
                        parsed.get("type") == "message_stop" or error_msg is not None
                    ):
                        break
        except httpx.HTTPError as exc:
            log.warning("%s adapter SSE upstream error: %s", platform, exc)
            error_msg = f"upstream_error: {exc}"
            yield (
                f"event: error\ndata: {json.dumps(_error_body(platform, str(exc)))}\n\n"
            ).encode("utf-8")
        finally:
            # v0.166：shield 保护收尾，客户端断开也能入库（见 _adapter_finalize 注释）。
            try:
                await asyncio.shield(_adapter_finalize())
            except asyncio.CancelledError:
                log.info("%s adapter-SSE finalize cancelled during teardown", platform)
            except Exception:
                log.exception("%s adapter-SSE finalize failed during teardown", platform)

    # v0.199.1：修复未定义 `response_headers` 的 NameError（此前 adapter
    # SSE 请求必炸 500）。上游在 gen() 内懒打开，headers 无法在构造期拿到；
    # 与 cross-wire SSE 同款（见 :3408）—— 只给 media_type，SSE content-type
    # 由 media_type 提供。上游的 hop-by-hop / 鉴权头本就不该透给客户端。
    return StreamingResponse(gen(), media_type="text/event-stream")


async def _filter_anthropic_sse(
    upstream: AsyncIterator[bytes],
    parser: _HasFeed,
) -> AsyncIterator[tuple[bytes, Optional[dict]]]:
    """Yield ``(bytes, parsed_data_or_None)`` for each SSE event from upstream.

    Drops events that are ``ping`` keepalives or that belong to a thinking
    content block (so the client never sees Qwen/DeepSeek thinking text
    cross the wire). Everything else is forwarded verbatim.

    Uses a byte buffer to handle half-arriving events across chunk
    boundaries — SSE parsing must not split mid-event, otherwise the
    client's SSE parser will fail on ``event:`` / ``data:`` lines arriving
    in separate chunks.
    """
    buf = b""
    async for chunk in upstream:
        if not chunk:
            continue
        buf += chunk
        try:
            parser.feed(chunk)
        except Exception:
            pass
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            ev_name: Optional[str] = None
            data_lines: list[bytes] = []
            for line in block.split(b"\n"):
                if line.startswith(b"event:"):
                    ev_name = line[len(b"event:"):].strip().decode("ascii", "replace")
                elif line.startswith(b"data:"):
                    data_lines.append(line[len(b"data:"):].lstrip())
            data_payload = b"\n".join(data_lines)
            parsed: Optional[dict] = None
            if data_payload:
                try:
                    parsed = json.loads(data_payload.decode("utf-8", "replace"))
                except Exception:
                    parsed = None

            if ev_name == "ping":
                continue
            if ev_name == "content_block_start" and parsed is not None:
                cb = parsed.get("content_block") or {}
                if isinstance(cb, dict) and cb.get("type") == "thinking":
                    continue
            if ev_name == "content_block_delta" and parsed is not None:
                delta = parsed.get("delta") or {}
                if isinstance(delta, dict):
                    dt = delta.get("type")
                    if dt in ("thinking_delta", "signature_delta"):
                        continue

            yield block + b"\n\n", parsed


# Sentinel value the client can send as its auth header to mean
# "I don't have a key — use whatever the relay has configured". When the
# relay's upstream also has no `api_key`, this triggers a clear 503 instead
# of passing the literal sentinel upstream and getting an opaque 401 back.
AUTH_AUTO_SENTINEL = "auto"


# ---------------------------------------------------------------------------
# In-flight request tracking (for the GUI / TUI live panels)
# ---------------------------------------------------------------------------
#
# When a request enters relay(), we register a `_InFlight` entry in this
# module-level dict and update it as the body uploads and the response
# streams back. On completion, we move the entry to the "done" list, which
# is capped at MAX_DONE_VISIBLE so old completed entries get displaced by
# new in-flight requests (matches the user's "keep until next message"
# requirement).
#
# The state is purely in-process: lost on server restart. The GUI polls
# every 0.5s so a snapshot endpoint is enough; we never write to SQLite
# per chunk (too much IO during streaming).
#
# Reads (`get_inflight_snapshot`) are synchronous and return a copy, so
# callers don't need to hold the lock during expensive serialization.


@dataclass
class _InFlight:
    request_id: str                         # uuid4 hex
    started_at: float
    platform: str
    model: Optional[str]
    phase: str = "uploading"                # uploading | calling | streaming | done
    bytes_received: int = 0
    content_length: Optional[int] = None
    user_text_preview: str = ""
    assistant_text: str = ""
    last_update: float = field(default_factory=time.time)
    # v0.X: set on every byte arrival inside stream_iter(); used by
    # _monitor_quiet_streaming to detect stuck SSE streams where the
    # upstream stopped sending chunks but kept the TCP connection open.
    last_chunk_at: float = field(default_factory=time.time)
    # v0.11.21: 内外转换显示 —— 客户端发来的模型（对内）与中继实际
    # 转发给上游的模型/上游名（对外）。默认同 model，重写后 patch。
    client_model: Optional[str] = None
    upstream: str = ""
    # v0.89 实时流面板（live panel）专用字段。这些只服务于 GUI 侧栏的
    # 「最近一次调用全貌」展示，不参与计费、不落 requests 表。
    #
    # ⚠️ api_key 是**明文**。它只经 pywebview 桥推给本机 GUI 侧栏窗口；
    # `get_inflight_snapshot()`（HTTP /live 的数据源）会用 _mask_key 掩码，
    # 保持 routers/api.py「不通过 HTTP 返明文 key」的既有约定。
    api_key: str = ""
    # 入向 / 出向协议名（如 anthropic-messages → openai-chat）。相等即直通，
    # 不等即发生了 cross-wire 协议转换。
    inbound_wire: str = ""
    outbound_wire: str = ""
    # 流式期间实时跳动的 token 四字段。parser 的 UsageAcc 本来就逐帧更新，
    # 这里只是把它镜像出来供广播；请求结束时的权威值仍以 db.record 为准。
    usage_live: dict = field(default_factory=dict)
    # extended thinking 正文与 tool_use 块（已序列化的 JSON 字符串，避免
    # 跨条目共享可变对象）。
    thinking_text: str = ""
    tool_use_json: str = ""
    # v0.111：思考内容是否已结束（思考块 content_block_stop / </think>
    # 出现过）。panel_pool 据此把「思考→正文衔接期」从思考档里拆出来独立
    # 判定 —— 思考一旦结束，之后的静默就是衔接卡死而非思考暂停。
    thinking_done: bool = False
    # v0.155：客户端工具名（claude-code / opencode / codex / …），入口
    # 从 User-Agent 识别。只用于展示归组，不参与计费 / 路由。
    agent: str = ""
    # v0.157：原始 User-Agent 头原文，落库 raw_ua 列（设置页列出真实 UA）。
    raw_ua: str = ""


# Mutable module state — read by get_inflight_snapshot(), written by the
# helpers below under `_in_flight_lock`.
_in_flight: dict[str, _InFlight] = {}
_in_flight_done: list[_InFlight] = []
_in_flight_lock = asyncio.Lock()
MAX_DONE_VISIBLE = 5
BODY_LARGE_THRESHOLD = 64 * 1024          # bytes; below this, no chunked reading

# v0.97.1：DeepSeek 等思考型 openai 上游要求「带工具调用的 assistant 消息
# 回传时必须原样携带当时输出的 reasoning_content」，否则 400（The
# reasoning_content in the thinking mode must be passed back to the API）。
# 响应侧剥除 reasoning_content 时会按 tool_call id 存一份，请求侧回传时
# 再注入。key = openai tool_call id（往返转换 id 保持不变）。有界防泄漏。
_REASONING_BY_TOOL_CALL: dict[str, str] = {}
_MAX_REASONING_ENTRIES = 512


def _bind_reasoning_to_tool_calls(reasoning: str, tool_calls) -> None:
    """把已累积的 reasoning_content 绑定到本响应出现的 tool_call id。

    只处理带 id 的 tool_call（deepseek 首个 delta 带 id）。dict 有界：
    超过 _MAX_REASONING_ENTRIES 时按插入序淘汰最老的。
    """
    if not reasoning:
        return
    for tc in tool_calls or []:
        if isinstance(tc, dict) and tc.get("id"):
            _REASONING_BY_TOOL_CALL[tc["id"]] = reasoning
    if len(_REASONING_BY_TOOL_CALL) > _MAX_REASONING_ENTRIES:
        for old in list(_REASONING_BY_TOOL_CALL)[: len(_REASONING_BY_TOOL_CALL) - _MAX_REASONING_ENTRIES]:
            _REASONING_BY_TOOL_CALL.pop(old, None)


def _upstream_needs_reasoning(cfg, model: Optional[str]) -> bool:
    """判断该 openai 上游是否 DeepSeek 系思考型（需要回传 reasoning_content）。

    v0.97.3：v0.97.2 的注入兜底把 ``reasoning_content=""`` 写进了**所有**
    openai 上游的 assistant(tool_calls) 消息 —— 但该字段是 DeepSeek 思考型
    端点专用。MiniMax 等非思考 openai 上游不认这个字段，注入后整条请求被
    上游 400（minnimax.chat 中转站现象：模型想跑命令，但工具结果回传不上
    去）。这里按上游 URL / 配置模型 / 解析出的模型名判断是否 DeepSeek 系，
    只对这类上游注入 reasoning_content。
    """
    needle = "deepseek"
    for s in (cfg.url or "", cfg.model or "", model or ""):
        if needle in s.lower():
            return True
    return False


def _inject_reasoning_to_messages(messages, needs_reasoning: bool) -> None:
    """请求侧回传注入：DeepSeek 系思考型 openai 上游的 assistant 消息带
    tool_calls 时，按 id 查回存好的 reasoning_content 写进该消息。原地改
    messages。

    ``needs_reasoning`` 由调用方用 ``_upstream_needs_reasoning`` 判定（openai
    且 DeepSeek 系思考型），不是简单的「是否 openai 上游」—— 非思考 openai
    上游（MiniMax 等）注入空串反而会被 400。

    v0.97.2：查不到（历史来自 anthropic 端点 / relay 重启后内存清空 / 旧
    对话）也兜底注入空串 —— DeepSeek 思考型 openai 端点对带工具调用的
    assistant 消息只校验 ``reasoning_content`` 字段**存在**，不校验内容
    （官方样例：模型未输出推理时客户端同样 append ``reasoning_content=""``），
    空串可过。不注入则 400「must be passed back」。

    v0.97.2 收尾：若消息已带非空 ``reasoning_content``（由
    ``_merge_split_assistant_messages`` 从 thinking 块合并而来），不覆盖 ——
    那是真实推理内容，优先于兜底。
    """
    if not needs_reasoning:
        return
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        tool_calls = m.get("tool_calls")
        if not tool_calls:
            continue
        if m.get("reasoning_content"):
            continue
        m["reasoning_content"] = ""
        for tc in tool_calls:
            rid = tc.get("id") if isinstance(tc, dict) else None
            if rid and rid in _REASONING_BY_TOOL_CALL:
                m["reasoning_content"] = _REASONING_BY_TOOL_CALL[rid]
                break


def _merge_split_assistant_messages(messages) -> None:
    """把 linguafranca 拆开的连续 assistant 消息合并回一条（原地改）。

    背景：linguafranca 把 anthropic assistant 的 ``content`` 数组（text /
    tool_use / thinking 块）**逐块**转成多个连续的 openai assistant 消息 ——
    text 块 → ``{"role":"assistant","content":...}``、tool_use 块 →
    ``{"role":"assistant","tool_calls":[...]}``、thinking 块 →
    ``{"role":"assistant","reasoning_content":...}``。

    但 DeepSeek 等思考型 openai 上游要求 ``tool_calls`` 与 ``reasoning_content``
    在**同一条** assistant 消息里（官方样例 append 的是一条同时带 content /
    reasoning_content / tool_calls 的消息）。拆开会导致带 tool_calls 的消息
    缺 reasoning_content → 400「reasoning_content must be passed back」。

    这里把连续的 assistant 消息合并成一条：content / tool_calls /
    reasoning_content 各取第一个非空值。正常对话轮次之间必有 user/tool 消息
    分隔，所以「连续 assistant」只会出现在 linguafranca 拆块场景，合并安全。
    """
    if not messages:
        return
    merged: list = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            merged.append(m)
            i += 1
            continue
        acc_content = None
        acc_tool_calls = None
        acc_reasoning = None
        j = i
        while j < n:
            mj = messages[j]
            if not (isinstance(mj, dict) and mj.get("role") == "assistant"):
                break
            if acc_content is None and mj.get("content"):
                acc_content = mj["content"]
            if acc_tool_calls is None and mj.get("tool_calls"):
                acc_tool_calls = mj["tool_calls"]
            if acc_reasoning is None and mj.get("reasoning_content"):
                acc_reasoning = mj["reasoning_content"]
            j += 1
        combined: dict = {"role": "assistant"}
        if acc_content:
            combined["content"] = acc_content
        if acc_tool_calls is not None:
            combined["tool_calls"] = acc_tool_calls
        if acc_reasoning is not None:
            combined["reasoning_content"] = acc_reasoning
        merged.append(combined)
        i = j
    messages[:] = merged


def _split_openai_tool_call_arguments(d: dict, opened: set) -> list:
    """linguafranca 流式转换丢 tool_calls 参数的绕行（v0.97.4）。

    现象：f3af39d7-openai（MiniMax openai 端点）等上游能正常出 tool_use，但
    Claude Code 报「Invalid tool parameters」。根因：linguafranca 把 openai
    tool_calls 流转 anthropic 时，若**首个** chunk 就携带非空 ``arguments``
    （MiniMax / DeepSeek 这类一上来把完整参数塞进第一片的上游），会静默丢参数
    —— 客户端收到 ``tool_use input={}`` 且没有任何 ``input_json_delta``。
    拆成「空参数首片（带 id/name）+ 独立全量参数片」后，linguafranca 正常产出
    ``input_json_delta``（已在直连测试验证）。

    只对「未开片的非空参数」拆；``opened`` 记录已见过 id/name 的 tool_call
    index，已开片的增量参数原样透传，增量流（OpenAI 官方分片发送）不受影响。

    返回需替换当前 chunk 的列表；空列表表示无需拆、调用方原样透传 d。
    """
    to_split: list[tuple] = []  # (index, name, full_arguments)
    for ch in d.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta")
        if not isinstance(delta, dict):
            continue
        tcs = delta.get("tool_calls")
        if not isinstance(tcs, list):
            continue
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index")
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments")
            is_str = isinstance(args, str)
            if is_str and args != "" and idx is not None and idx not in opened:
                # 危险情形：未开片且首片即带非空参数 → 拆成两片。
                opened.add(idx)
                to_split.append((idx, fn.get("name"), args))
                continue
            # 其余：身份片（带 id）/ 空参数片 / 已开片增量 → 原样透传。
            # 身份片（linguafranca 在此建 content_block）记入 opened，后续
            # 增量参数就不再误判为「首片完整参数」。
            if tc.get("id") or (is_str and args == ""):
                if idx is not None:
                    opened.add(idx)
    if not to_split:
        return []
    # 空参数首片：原 chunk 深拷贝，仅把待拆 tool_call 的参数置空（id/name 保留）。
    empty = json.loads(json.dumps(d, ensure_ascii=False))
    split_ids = {t[0] for t in to_split}
    args_chunks: list[dict] = []
    for ch in empty.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta")
        if not isinstance(delta, dict):
            continue
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict) or tc.get("index") not in split_ids:
                continue
            idx = tc["index"]
            name = (tc.get("function") or {}).get("name")
            full = next(a for i, n, a in to_split if i == idx)
            tc["function"] = {"name": name, "arguments": ""}
            args_chunks.append({
                "id": d.get("id"),
                "object": "chat.completion.chunk",
                "created": d.get("created"),
                "model": d.get("model"),
                "choices": [{
                    "index": ch.get("index"),
                    "delta": {"tool_calls": [
                        {"index": idx, "function": {"arguments": full}},
                    ]},
                }],
            })
    return [empty] + args_chunks


# Inflight self-heal: when a request gets stuck (e.g. a finally-block
# exception swallowed _complete_inflight, or starlette's StreamingResponse
# never closed the generator), the entry sits in _in_flight forever with
# phase=streaming and the GUI "实时" panel shows a ghost row. The sweeper
# below catches these — any inflight entry whose `last_update` is older
# than STREAMING_STALE_AFTER for streaming (or MAX_INFLIGHT_AGE for any
# phase) gets force-completed.
STREAMING_STALE_AFTER = 90.0   # seconds — phase=streaming + no update
MAX_INFLIGHT_AGE = 600.0       # seconds — any-phase hard ceiling
SWEEP_INTERVAL = 30.0          # seconds — how often the sweeper runs
# v0.X: streaming quiet-detector threshold. SSE streams are expected to
# emit at least one byte every few seconds for any non-trivial response;
# if no byte has arrived for STREAM_QUIET_AFTER, the entry is considered
# stuck (upstream stalled, half-closed TCP, etc.) and force-completed by
# _monitor_quiet_streaming so the GUI "实时" panel stops showing it.
# v0.12：5s 太激进 —— 思考模型（deepseek-v4-flash/glm 等）的 thinking 段
# 会被 adapter 剥离、客户端暂时看不到任何东西，但上游其实一直在发。误判
# 会把正常流强杀。放宽到 120s，并让各流式路径（adapter/直通/cross-wire）
# 在每收到上游字节时都 _bump_chunk_activity，从根上避免误伤。
STREAM_QUIET_AFTER = 120.0     # seconds — phase=streaming + no chunk
QUIET_MONITOR_INTERVAL = 1.0  # seconds — how often to scan for stuck
# v0.11.21: phase=uploading 的静默清理。上传阶段每收到一个 body chunk
# 都会 _update_inflight（刷新 last_update），所以"静默"= 客户端连接
# 半开/断开后没有任何字节进来 —— 此时 model 永远读不出来（body 不完整），
# 条目会带着空 client_model 霸占 live 面板和"内外转换"映射条。
# 60s 无字节进展即视为卡死强制完成（正常大 body 上传在 64KB 阈值下
# 也是秒级完成，60s 足够宽裕）。
UPLOAD_QUIET_AFTER = 60.0     # seconds — phase=uploading + no byte


# ────────────────────────────────────────────────────────────────────
# v0.89：实时流广播（in-process pub/sub）。
#
# 中继是 uvicorn 单 worker（server.py / main.py 都没有 --workers），
# 因此进程内 pub/sub 就够了，不需要走 Redis / 队列。
#
# 设计要点：
#   * **每 chunk 都广播一次**，订阅者集合为空时早退，零开销；
#   * **绝不能阻塞转发主路** —— 锁内只快照订阅者集合，锁外 put_nowait；
#   * **绝不背压** —— 满了（QueueFull）丢最新事件，绝不 await 队列空间，
#     宁可侧栏掉帧也不能拖慢中继转发；
#   * **独立锁 _subs_lock**，不复用 _in_flight_lock —— 后者每 chunk 都要
#     拿，高并发下会跟广播争锁；
#   * **每个订阅者一个 asyncio.Queue** —— 异步安全的 FIFO，订阅/取消
#     都在 SSE 生成器的 finally 里做，对称且无泄漏。
# ────────────────────────────────────────────────────────────────────

_SUBSCRIBERS: set = set()       # set[asyncio.Queue[dict]] —— 严格说队列元素是 dict
_SUBS_LOCK = None              # asyncio.Lock，运行时懒构造（无事件循环时 import 不会爆）
_BROADCAST_QUEUE_MAX = 200     # 满了丢帧；一个流式调用最多 ~几十帧，200 远超实际
_BROADCAST_DROP_COUNT = 0      # 调试：累计丢帧数


def _get_subs_lock() -> asyncio.Lock:
    """懒构造 asyncio.Lock —— 模块级 import 时还没有事件循环。"""
    global _SUBS_LOCK
    if _SUBS_LOCK is None:
        _SUBS_LOCK = asyncio.Lock()
    return _SUBS_LOCK


async def _broadcast(event: dict) -> None:
    """v0.89：把一条事件推给所有订阅者。无订阅者时立刻返回，零成本。

    事件形状（建议字段）：
        type:        "start" | "delta" | "done" | "snapshot" | "drop"
        request_id:  uuid4 hex（"snapshot" 事件可省略）
        其它字段按 type 而异（见各路径填充）。
    """
    global _BROADCAST_DROP_COUNT
    lock = _get_subs_lock()
    async with lock:
        if not _SUBSCRIBERS:
            return
        # 锁内只拷贝订阅者列表，锁外 put_nowait —— put_nowait 本身很快，
        # 但一旦撞 QueueFull 抛 QueueFull，锁内抛会让其它订阅者白等。
        targets = list(_SUBSCRIBERS)
    for q in targets:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 侧栏订阅者卡死了（中继推送快于 GUI 处理）。宁可掉帧也不
            # 要反压中继主路 —— 累计计数仅供调试观察。
            _BROADCAST_DROP_COUNT += 1


def _subscribe_live_stream() -> "asyncio.Queue":
    """v0.89：SSE 端点（/live/stream）调用的订阅入口。

    返回一个新的 asyncio.Queue —— 调用方负责在 finally 里把它从
    `_SUBSCRIBERS` 移除并清空。未连接的语义：
      * Queue 满 200 条后新的 put_nowait 会抛 QueueFull，由 _broadcast
        捕获并计数，不向上抛；
      * 调用方按 await q.get() 一条条读，直到 SSE 客户端断开。
    """
    q: "asyncio.Queue" = asyncio.Queue(maxsize=_BROADCAST_QUEUE_MAX)
    _SUBSCRIBERS.add(q)
    return q


async def _unsubscribe_live_stream(q: "asyncio.Queue") -> None:
    """v0.89：取消订阅并清空残留事件（避免队列引用泄漏）。"""
    lock = _get_subs_lock()
    async with lock:
        _SUBSCRIBERS.discard(q)
    # 清空残留事件，避免下一次订阅错位消费。
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break


def _preview_user_text(body: bytes) -> str:
    """Best-effort extract of the last user message text from a (possibly
    partial) JSON body. Falls back to a UTF-8 tail slice if the JSON is
    truncated mid-string.
    """
    text, _json = _extract_last_user_message(body)
    if text:
        return text[-512:]
    # Partial body — can't parse JSON; show the readable tail.
    try:
        return body[-512:].decode("utf-8", "replace")
    except Exception:
        return ""


async def _register_inflight(
    *,
    platform: str,
    model: Optional[str],
    inbound_wire: str = "",
    outbound_wire: str = "",
    api_key: str = "",
    agent: str = "",
    raw_ua: str = "",
) -> _InFlight:
    inf = _InFlight(
        request_id=uuid.uuid4().hex,
        started_at=time.time(),
        platform=platform,
        model=model,
        client_model=model,
        inbound_wire=inbound_wire,
        outbound_wire=outbound_wire,
        api_key=api_key,
        agent=agent,
        raw_ua=raw_ua,
    )
    async with _in_flight_lock:
        # Cap the total visible set (in_flight + done) at MAX_DONE_VISIBLE
        # by evicting the oldest "done" entry. Never evict in-flight —
        # they're still happening. Single-user semantics: when a new
        # request arrives, the oldest completed one is pushed out.
        while len(_in_flight) + len(_in_flight_done) >= MAX_DONE_VISIBLE:
            if _in_flight_done:
                _in_flight_done.pop()
            else:
                break
        _in_flight[inf.request_id] = inf
    return inf


async def _update_inflight(rid: str, **fields: object) -> None:
    """Patch fields on a live entry. No-op if the entry was already completed."""
    async with _in_flight_lock:
        inf = _in_flight.get(rid)
        if inf is None:
            return
        for k, v in fields.items():
            setattr(inf, k, v)
        inf.last_update = time.time()


async def _bump_chunk_activity(rid: str) -> None:
    """Mark a streaming chunk has just arrived. Lighter than
    _update_inflight — only touches last_chunk_at + last_update, no setattr
    loop, so it can be called once per SSE byte without locking pressure.
    No-op if the entry has already been completed."""
    async with _in_flight_lock:
        inf = _in_flight.get(rid)
        if inf is None:
            return
        now = time.time()
        inf.last_chunk_at = now
        inf.last_update = now


async def _set_inflight_phase(rid: str, phase: str) -> None:
    """Bump phase + push assistant_text snapshot. Used by the streaming path."""
    async with _in_flight_lock:
        inf = _in_flight.get(rid)
        if inf is None:
            return
        inf.phase = phase
        inf.last_update = time.time()


async def _complete_inflight(rid: str) -> None:
    """Move a live entry to the done list. No-op if already gone."""
    async with _in_flight_lock:
        inf = _in_flight.pop(rid, None)
        if inf is None:
            return
        inf.phase = "done"
        inf.last_update = time.time()
        # Don't displace if the only existing done entries are newer — newest-first.
        _in_flight_done.insert(0, inf)
        while len(_in_flight_done) > MAX_DONE_VISIBLE:
            _in_flight_done.pop()


async def _broadcast_live_event(rid: str, kind: str, **payload: object) -> None:
    """v0.89：广播一条 live panel 事件。**只推**不更新 inflight —— 调用方负责先 _update_inflight。

    kind ∈ {"start", "delta", "done", "drop", "snapshot"}。
    字段：
      request_id      —— 事件隶属的请求（snapshot 事件可缺省）
      payload 中其余字段透传给前端（如 assistant_delta, usage_live,
      thinking_delta, phase, ...）。前端按 kind 分支处理。

    v0.94：``done`` 事件自动从 inflight 字典补齐 upstream/platform/model/
    client_model/inbound_wire/outbound_wire/api_key(掩码) 六个「侧栏头部」
    字段 —— 调用方不必再各显式传，且非流式 / 时序错位下，前端也能拿到完整
    元数据，不再依赖首连 snapshot（snapshot 已经不跳过 done 条目，但前端
    live_panel.js 的 done 路径会先 reset 再用这些字段重渲染，双保险）。

    v0.95：``delta`` 事件同样自动补齐侧栏头部字段。``delta`` 期间前端
    live_panel.js 的 delta 分支在 ``currentRid !== ev.request_id`` 时会
    ``reset()``（清空上游/平台/模型/wire/api-key 五项），但 delta payload
    里**没有**这些字段 —— 旧条目（如前一请求的完成态）被清空后，新请求的
    头部字段只能等 ``done`` 才补回来。流式过程中（截图里 status="流式中"
    + 正文累积但头部空白）用户体验非常差。自动补齐让 delta 也带齐头部，
    前端 delta 分支 ``reset`` 后跟着 payload 里的字段写回，时序错位也兜底。
    """
    if kind in ("delta", "done"):
        # v0.94/v0.95：自动补齐侧栏头部字段。payload 里**已经存在**的字段
        # 优先（调用方传了就用调用方的）—— 给上游/平台类显式覆盖留口子。
        inf = _in_flight.get(rid) or _find_done(rid)
        if inf is not None:
            payload.setdefault("upstream", inf.upstream)
            payload.setdefault("platform", inf.platform)
            payload.setdefault("model", inf.model)
            payload.setdefault("client_model", inf.client_model or inf.model)
            payload.setdefault("inbound_wire", inf.inbound_wire)
            payload.setdefault("outbound_wire", inf.outbound_wire)
            # v0.95：SSE done 事件带**明文** api_key，不再走
            # ``get_live_panel_api_key`` pywebview 桥。GUI 进程与中继进
            # 程是**两个独立 Python 进程**，`from relay import proxy`
            # 拿到的是 GUI 自己加载的模块副本（`_in_flight` 永远是空），
            # 桥调用 `target.api_key` 永远返回 ""，侧栏一直显示「（无）」。
            # SSE 监听 127.0.0.1:8088 只在本机 loopback 可达（外部网络
            # 碰不到），明文走 SSE 不会扩大攻击面。``api_key_cleartext``
            # 字段独立于 ``api_key``（保留掩码字段以兼容旧前端 + 调试）。
            payload.setdefault("api_key", _mask_key(inf.api_key))
            # v0.111：思考完成信号兜底 —— 调用方没显式传时（其它广播路径 /
            # 快照重建）用 inflight 上存的最终值。
            payload.setdefault("thinking_done", bool(inf.thinking_done))
            if kind == "done":
                payload.setdefault("api_key_cleartext", inf.api_key)
    ev = {"type": kind, "request_id": rid, **payload}
    await _broadcast(ev)


def _find_done(rid: str) -> Optional["_InFlight"]:
    """从 ``_in_flight_done`` 查已完成的 inflight 引用（不弹出）。"""
    for inf in _in_flight_done:
        if inf.request_id == rid:
            return inf
    return None


async def _sweep_inflight_once() -> int:
    """Force-complete inflight entries that are clearly stuck.

    Two thresholds:
      - phase="streaming" with last_update older than STREAMING_STALE_AFTER
        — bytes have stopped flowing for >90s; almost certainly done.
      - any inflight entry with last_update older than MAX_INFLIGHT_AGE
        — 10-minute hard ceiling regardless of phase (uploading/calling/
        streaming all share this).

    Returns the count of entries forced-done in this sweep tick.
    """
    now = time.time()
    stuck: list[str] = []
    async with _in_flight_lock:
        for rid, inf in list(_in_flight.items()):
            age = now - inf.last_update
            if age < 0:
                continue
            if inf.phase == "streaming" and age >= STREAMING_STALE_AFTER:
                stuck.append(rid)
            elif age >= MAX_INFLIGHT_AGE:
                stuck.append(rid)
    for rid in stuck:
        # _complete_inflight is itself idempotent (pops with default None),
        # so we just call it; if the entry legitimately completed between
        # our scan and now, it's a no-op.
        await _complete_inflight(rid)
    if stuck:
        log.warning(
            "inflight sweeper force-completed %d stuck entr%s: %s",
            len(stuck),
            "y" if len(stuck) == 1 else "ies",
            ",".join(stuck[:5]) + ("…" if len(stuck) > 5 else ""),
        )
    return len(stuck)


async def _sweep_loop(stop_event: asyncio.Event) -> None:
    """Run _sweep_inflight_once every SWEEP_INTERVAL seconds until cancelled.

    `stop_event` lets lifespan() signal the loop to exit without waiting for
    the next interval tick — keeps shutdown snappy. Designed to be wrapped
    in asyncio.create_task(name="inflight-sweeper").
    """
    log.info("inflight sweeper started (interval=%ss)", SWEEP_INTERVAL)
    while not stop_event.is_set():
        try:
            await _sweep_inflight_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - sweeper must self-recover
            log.exception("inflight sweep tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SWEEP_INTERVAL)
        except asyncio.TimeoutError:
            pass
    log.info("inflight sweeper stopped")


async def _monitor_quiet_streaming(stop_event: asyncio.Event) -> None:
    """Force-complete streaming entries whose upstream stopped emitting
    bytes for STREAM_QUIET_AFTER. Cheaper and stricter than _sweep_loop:
    scans every QUIET_MONITOR_INTERVAL, only matches phase=streaming, and
    uses last_chunk_at (set on every byte arrival) instead of last_update
    (which would also move on phase/model transitions that don't actually
    count as 'stream is alive').

    v0.11.21: also force-completes phase=uploading entries with no byte
    progress for UPLOAD_QUIET_AFTER — a half-open client connection would
    otherwise sit in uploading forever with an empty client_model, leaving
    a ghost row in the live panel and the topbar io-map strip.
    """
    log.info(
        "inflight quiet monitor started (interval=%ss threshold=%ss upload=%ss)",
        QUIET_MONITOR_INTERVAL, STREAM_QUIET_AFTER, UPLOAD_QUIET_AFTER,
    )
    while not stop_event.is_set():
        try:
            now = time.time()
            stuck: list[tuple[str, float, str]] = []
            async with _in_flight_lock:
                for rid, inf in list(_in_flight.items()):
                    if inf.phase == "streaming":
                        silent_for = now - inf.last_chunk_at
                        if silent_for >= STREAM_QUIET_AFTER:
                            stuck.append((rid, silent_for, inf.phase))
                    elif inf.phase == "uploading":
                        silent_for = now - inf.last_update
                        if silent_for >= UPLOAD_QUIET_AFTER:
                            stuck.append((rid, silent_for, inf.phase))
            for rid, silent_for, phase in stuck:
                # _complete_inflight is idempotent and moves the entry to
                # _in_flight_done; if the stream genuinely completed in the
                # same instant, this is a no-op (pop returns None).
                log.warning(
                    "quiet monitor: force-completing %s after %.1fs of "
                    "silence (phase=%s)", rid, silent_for, phase,
                )
                await _complete_inflight(rid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - monitor must self-recover
            log.exception("inflight quiet monitor tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=QUIET_MONITOR_INTERVAL)
        except asyncio.TimeoutError:
            pass
    log.info("inflight quiet monitor stopped")


def get_inflight_snapshot() -> list[dict]:
    """Synchronous snapshot of all visible live + recently-completed requests.

    No lock needed because we read from local references and return plain
    dicts — any concurrent mutation would just produce a slightly stale
    snapshot, which is fine for a 0.5s polling consumer.

    v0.89：新增 live-panel 字段。但 **api_key 走掩码**（_mask_key）——
    这个函数同时给 HTTP `/live` 用，明文 key 绝不能进 HTTP 响应。明文
    只经 pywebview 桥推给本机 GUI 侧栏窗口（见 _broadcast）。
    """
    def _entry(inf: _InFlight) -> dict:
        return {
            "request_id": inf.request_id,
            "started_at": inf.started_at,
            "platform": inf.platform,
            "model": inf.model,
            # v0.11.21：内外转换显示 —— 对内客户端模型 + 对外上游名。
            "client_model": inf.client_model or inf.model,
            "upstream": inf.upstream,
            "phase": inf.phase,
            "bytes_received": inf.bytes_received,
            "content_length": inf.content_length,
            "user_text_preview": inf.user_text_preview,
            "assistant_text": inf.assistant_text,
            "age_sec": max(0.0, time.time() - inf.last_update),
            # v0.89 live-panel 字段
            "api_key": _mask_key(inf.api_key),     # HTTP 用掩码
            "inbound_wire": inf.inbound_wire,
            "outbound_wire": inf.outbound_wire,
            "usage_live": dict(inf.usage_live),
            "thinking_text": inf.thinking_text,   # v0.95：不截尾，传全文
        }
    out: list[dict] = []
    for inf in list(_in_flight_done):
        out.append(_entry(inf))
    for inf in list(_in_flight.values()):
        out.append(_entry(inf))
    return out


class _HasFeed:
    """Protocol duck-type: feed(bytes) -> None; finalize() -> UsageAcc."""

    usage: UsageAcc

    def feed(self, chunk: bytes) -> None: ...
    def finalize(self) -> UsageAcc: ...


def _join_upstream_url(upstream_base: str, request_path: str, query: str) -> str:
    """Compose the upstream URL: <upstream_base><request_path>?<query>.

    `request_path` is the original path WITH the platform prefix still on it
    (e.g. /anthropic/v1/messages). The caller is expected to have stripped
    the prefix before calling this — we just concatenate.
    """
    parts = urlsplit(upstream_base)
    # Ensure base ends with '/', path starts with '/'.
    base_path = parts.path.rstrip("/")
    new_path = base_path + request_path
    return urlunsplit((parts.scheme, parts.netloc, new_path, query, ""))


# v0.12.2：客户端 API 路径归一化 —— 修「opencode 桌面版 openai 入口全部无回复」。
#
# 各客户端 baseURL 约定不一：opencode 桌面版捆绑的 @ai-sdk/openai-compatible
# （provider-utils 4.0.23）拼 `{baseURL}/chat/completions`，**不带 /v1**；官方
# SDK 则惯例把 /v1 放进 baseURL。上游配置同样两派（minnimax.chat 不带 /v1、
# opencode.ai/zen/v1 带 /v1、甚至整端点 URL）。直接拼接会产出
# `https://minnimax.chat/chat/completions` —— 上游 200 返回一个 HTML 页面，
# 客户端 SSE 解析器收不到任何 JSON，界面表现即「无回复」。
#
# 归一化规则（对 stripped 路径）：
#   1. base 已含完整端点且客户端路径就是该端点（±/v1）→ 返回 ""（不重复拼）
#   2. 客户端带 /v1 且 base 也以版本段结尾 → 剥掉客户端 /v1（防 /v1/v1）
#   3. 双方都没版本段且命中已知端点 → 补 /v1
#   4. 其余原样透传
_OPENAI_API_ENDPOINTS = (
    "/chat/completions", "/completions", "/embeddings",
    "/responses", "/models", "/moderations",
    # v0.X 鲁棒性增强：补 OpenAI 家族其他常见端点
    "/batches", "/files",
    "/audio/transcriptions", "/audio/translations", "/audio/speech",
    "/images/generations", "/images/edits", "/images/variations",
    "/fine_tuning/jobs", "/assistants", "/threads",
)
_ANTHROPIC_API_ENDPOINTS = (
    "/messages", "/messages/batches", "/count_tokens", "/complete", "/files",
)
# v0.X 鲁棒性增强：识别 /v1beta /v2alpha 等预发布版本段，避免双版本段 URL。
# base 含 vNbeta 客户端再发 /v1/... 时，剥 /v1 拼到 /v1beta 之后，
# 防止产出 /v1beta/v1/chat/completions 这种上游 404 的串。
_API_VERSION_RE = re.compile(r"/v\d+(?:alpha|beta)?$", re.IGNORECASE)


# v0.X 鲁棒性增强：路径末段 → 客户端 wire 推断表。
# 客户端拼 base URL 时按 SDK 约定补后缀（/v1/messages、/v1/responses 等），
# 末段足以区分协议 —— 这比 URL 前缀 (/anthropic /openai) 更准确。
# 规则：精确匹配 > startswith(端点+"/")。_ANTHROPIC_SUFFIXES 优先于 _OPENAI_
# 前缀，避免 /v1/messages 被错配成 openai（messages 也是 openai 老式端点名）。
_ANTHROPIC_PATH_SUFFIXES = (
    "/v1/messages",            # 主入口（含 count_tokens/batches 等子路径）
    "/v1/messages/batches",     # 批处理
    "/v1/messages/count_tokens",
    "/v1/files",                # Anthropic Files API
    "/v1/organizations",        # 管理面（罕见）
    "/complete",                # 老式 /complete（legacy）
    # v0.X 鲁棒性增强：客户端配 base_url=http://...:8088（无 /v1 前缀）
    # + SDK 不补 /v1 时发裸路径；与 _OPENAI_RESPONSES_PATH_SUFFIXES
    # 加裸 /responses 同款思路。bucket 检查顺序保证这些不会与 openai
    # 冲突（_OPENAI_CHAT 列表里只有 /v1/messages，没有裸 /messages）。
    "/messages",                # 主入口（裸）
    "/count_tokens",            # token 计数（裸）
    "/messages/batches",        # 批处理（裸）
)
_OPENAI_RESPONSES_PATH_SUFFIXES = (
    "/v1/responses",
    "/v1/responses/input_items",
    # v0.X 鲁棒性增强：客户端配 base_url=http://...:8088（无 /v1 前缀）
    # + SDK 不补 /v1 时发裸 /responses；与 _OPENAI_CHAT_PATH_SUFFIXES
    # 里的 /v1/completions ↔ /completions legacy 配对思路一致。
    "/responses",
)
_OPENAI_CHAT_PATH_SUFFIXES = (
    "/v1/chat/completions",
    "/v1/completions",          # legacy
    "/v1/embeddings",
    "/v1/models",
    "/v1/batches",
    "/v1/files",                # 与 Anthropic 重名 —— 见下方 fallback
    "/v1/moderations",
    "/v1/audio/transcriptions", "/v1/audio/translations", "/v1/audio/speech",
    "/v1/images/generations",   "/v1/images/edits", "/v1/images/variations",
    "/v1/fine_tuning/jobs",
    "/v1/assistants",           "/v1/threads",
    # v0.X 鲁棒性增强：客户端配 base_url=http://...:8088（无 /v1 前缀）
    # + SDK 不补 /v1 时发裸路径；与 _OPENAI_RESPONSES_PATH_SUFFIXES
    # 加裸 /responses、_ANTHROPIC_PATH_SUFFIXES 加裸 /messages 等同款思路。
    # bucket 检查顺序保证这些不会与 anthropic 冲突（_ANTHROPIC 列表里
    # 只有 /v1/files，没有裸 /files）。
    "/chat/completions",
    "/completions",             # legacy
    "/embeddings",
    "/models",
    "/batches",
    "/files",
    "/moderations",
    "/audio/transcriptions", "/audio/translations", "/audio/speech",
    "/images/generations",   "/images/edits",      "/images/variations",
    "/fine_tuning/jobs",
    "/assistants",           "/threads",
)


def _infer_client_wire_from_path(
    path: str, platform_hint: str,
) -> Optional[str]:
    """根据路径末段推断客户端 wire，用于覆盖 platform 隐式推断。

    目的：客户端把中继当上游，base URL 写成 ``http://.../anthropic`` 或
    ``http://.../openai``。客户端 SDK 按协议补后缀，但有时会"放错"——
    比如 OpenAI 客户端配了 ``/anthropic`` base 却发 ``/v1/chat/completions``，
    实际 wire 是 openai-chat。URL 前缀不可靠，末段才可靠。

    ``platform_hint`` 用于重名端点（``/v1/files``、``/v1/models`` 等 Anthropic
    和 OpenAI 都有的 API）的消歧：在哪个 prefix 下就推断为哪个协议。

    返回 ``None`` 表示无法从路径推断（走 platform 兜底 → body 嗅探）。

    注：FastAPI ``{path:path}`` 抓的 path 不带前导 ``/``，但 caller 也可能
    传 ``request.url.path``（带 ``/``）。统一在函数内补前导，避免 caller
    写错。
    """
    # v0.X 鲁棒性增强：折叠连续斜杠 —— 客户端 base_url 配错（如
    # ``http://...:8088//`` 双斜杠结尾）时 Starlette 把 path 透传给 router
    # 是 ``//v1/messages``，不折叠的话 startswith 匹配永远失败 → 末段
    # 推断返回 None → 降级 body 嗅探 → 没带 ``system`` 字段的 anthropic
    # 请求被错认成 openai-chat。折叠后 ``//v1/messages`` → ``/v1/messages``
    # 正常命中。split + filter + join 保持合法路径语义（中间空段一并折叠）。
    p = "/" + "/".join(seg for seg in path.split("/") if seg)
    if p == "/":
        return None
    # 按 platform_hint 决定检查顺序 —— 重名端点（/v1/files, /v1/models）
    # 在两边都有，按 prefix 选。
    if platform_hint == "anthropic":
        ordered = ("anthropic", "openai-responses", "openai-chat")
    else:
        ordered = ("openai-responses", "openai-chat", "anthropic")
    for bucket in ordered:
        if bucket == "anthropic":
            suffixes = _ANTHROPIC_PATH_SUFFIXES
            wire = WIRE_ANTHROPIC_MESSAGES
        elif bucket == "openai-responses":
            suffixes = _OPENAI_RESPONSES_PATH_SUFFIXES
            wire = WIRE_OPENAI_RESPONSES
        else:
            suffixes = _OPENAI_CHAT_PATH_SUFFIXES
            wire = WIRE_OPENAI_CHAT
        for suffix in suffixes:
            if p == suffix or p.startswith(suffix + "/"):
                return wire
    return None


# v0.X 鲁棒性增强：根路径 catchall 用 —— 把 wire 映到 platform bucket，
# 决定走 anthropic 还是 openai 的 active upstream。
#
# 隐式约定：openai-responses 走 openai 段（同 wire family，归 OpenAI 上游）；
# anthropic-messages 走 anthropic 段。openai-chat 也走 openai 段。
_WIRE_TO_PLATFORM: dict[str, str] = {
    WIRE_ANTHROPIC_MESSAGES: "anthropic",
    WIRE_OPENAI_CHAT: "openai",
    WIRE_OPENAI_RESPONSES: "openai",
}


def _resolve_platform_for_wire(wire: Optional[str]) -> Optional[str]:
    """按 wire 决定走哪个 platform 段（根路径分发用）。

    已知 wire → 对应 platform bucket；未知 wire → None。返回 None 时
    caller 应回 4xx 提示客户端用 /anthropic 或 /openai 前缀。
    """
    if wire is None:
        return None
    return _WIRE_TO_PLATFORM.get(wire)


# v0.X 鲁棒性增强：body 嗅探 —— 路径推断不出来时看 JSON body 顶部特征。
# 启发式只看 top-level 字段：
# - "system" + "messages" → anthropic-messages
# - "input" 数组（Responses API） → openai-responses
# - "messages" 数组首项含 "role" → openai-chat
# - 其它 → None（让调用方走 platform 兜底）
# 先试完整 JSON parse；失败回退子串扫描（覆盖超大 body 被截断的场景）。
def _sniff_client_wire(body: bytes) -> Optional[str]:
    """从 JSON body 顶部特征猜客户端 wire。失败返回 None。"""
    if not body:
        return None
    # 优先完整 parse（典型 chat body < 100KB，parse 几乎免费）
    data: Any = None
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        pass
    if isinstance(data, dict):
        # Anthropic：top-level system + messages
        if isinstance(data.get("system"), (str, list)) and isinstance(
            data.get("messages"), list
        ):
            return WIRE_ANTHROPIC_MESSAGES
        # Responses：top-level input 数组或字符串（OpenAI Responses API
        # 接受 string 形式的 user message 简写，原先只识别 list 会漏）
        if isinstance(data.get("input"), (str, list)):
            return WIRE_OPENAI_RESPONSES
        # OpenAI Chat：messages 数组首项含 role（string）
        msgs = data.get("messages")
        if isinstance(msgs, list) and msgs:
            first = msgs[0]
            if isinstance(first, dict) and isinstance(first.get("role"), str):
                return WIRE_OPENAI_CHAT
        return None
    # JSON parse 失败（截断/非 JSON）→ 子串扫描作 fallback。仅看前 8KB，
    # 避免扫描巨型 body。覆盖超大 message content 撑爆 buffer 的场景。
    head = body[:8192].decode("utf-8", errors="replace")
    has_system = '"system"' in head or "'system'" in head
    has_messages = '"messages"' in head or "'messages'" in head
    has_input = '"input"' in head or "'input'" in head
    has_role = '"role"' in head or "'role'" in head
    if has_system and has_messages:
        return WIRE_ANTHROPIC_MESSAGES
    if has_input and not has_messages:
        return WIRE_OPENAI_RESPONSES
    if has_role and has_messages:
        return WIRE_OPENAI_CHAT
    return None


def _normalize_api_path(platform: str, upstream_base: str, stripped: str) -> str:
    endpoints = _OPENAI_API_ENDPOINTS if platform == "openai" else _ANTHROPIC_API_ENDPOINTS
    base_path = urlsplit(upstream_base).path.rstrip("/")
    p = stripped.rstrip("/")
    if not p or p == "/":
        return stripped
    for ep in endpoints:
        if base_path.endswith(ep) and p in (ep, f"/v1{ep}"):
            return ""
    base_has_version = bool(_API_VERSION_RE.search(base_path))
    has_version = p.startswith("/v1/") or p == "/v1"
    if has_version:
        return p[3:] if base_has_version else stripped
    if base_has_version:
        return stripped
    for ep in endpoints:
        if p == ep or p.startswith(ep + "/"):
            return "/v1" + stripped
    return stripped


async def _extract_model(body: bytes) -> Optional[str]:
    """Best-effort: pull `model` out of the JSON request body, if present."""
    try:
        return json.loads(body or b"{}").get("model")
    except (ValueError, UnicodeDecodeError):
        return None


def _rewrite_model_in_body(body: bytes, new_model: str) -> bytes:
    """v0.65：把请求体里的 `model` 字段替换成 ``new_model``。

    不是 JSON 或没有 `model` 字段的 body 会被原样返回 — 没必要为了
    一个不是 JSON 的请求去猜解格式。`model` 字段类型如果不是字符串
    （罕见，比如 null），也跳过。替换失败时宁可透传也不丢请求。
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict):
        return body
    if "model" not in data:
        return body
    if not isinstance(data["model"], str):
        return body
    if data["model"] == new_model:
        return body
    data["model"] = new_model
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


# 挡位 → token 预算映射（effort 范式转 budget 范式用）。覆盖 OpenAI 全量
# effort 值 + opencode 系值；none/minimal 视为关闭档。
_THINKING_BUDGETS = {
    "none": 0, "minimal": 1024, "low": 4096, "medium": 16384,
    "high": 32000, "xhigh": 48000, "max": 64000,
}
# 客户端可能发的思考信号字段（入站解析用）：Anthropic thinking / OpenAI
# reasoning 对象 / OpenCode·OpenAI reasoning_effort 字符串 / Gemini thinkingConfig。
_THINKING_CLIENT_FIELDS = (
    "thinking", "reasoning", "reasoning_effort", "reasoningEffort", "thinkingConfig",
)
_THINKING_OFF_VALUES = ("off", "none", "disabled", "false", "0")


def _extract_adaptive_effort(data: dict) -> str:
    """从 Anthropic 自适应思考的 output_config.effort 取档位，缺省 medium。"""
    oc = data.get("output_config")
    if isinstance(oc, dict) and isinstance(oc.get("effort"), str):
        return oc["effort"]
    return "medium"


def _budget_to_effort(budget: int) -> str:
    """budget 范式 → 近似 effort 档（出站翻成 openai 形状时用）。"""
    if budget >= 48000:
        return "max"
    if budget >= 32000:
        return "high"
    if budget >= 16384:
        return "medium"
    if budget >= 4096:
        return "low"
    return "minimal"


def _strip_thinking_except(data: dict, keep: set) -> None:
    """清掉思考相关键，但保留 ``keep`` 里的（本协议原生字段，避免动到原本正确的请求）。"""
    for k in ("thinking", "reasoning", "reasoning_effort", "reasoningEffort", "thinkingConfig", "output_config"):
        if k not in keep and k in data:
            data.pop(k, None)


def _peek_thinking(data: dict) -> str:
    for k in ("thinking", "reasoning", "reasoning_effort", "thinkingConfig"):
        if k in data:
            return f"{k}={json.dumps(data[k], ensure_ascii=False)}"
    return "<none>"


def _client_thinking(body: bytes) -> Optional[dict]:
    """解析客户端请求里已有的思考信号 → 归一化 ``{type, effort, budget, wire}``。

    覆盖各协议形状：
      - ``thinking``（Anthropic）：{type: enabled/disabled, budget_tokens}，
        或自适应 {type: adaptive}（effort 在 output_config.effort）。
      - ``reasoning``（OpenAI 对象）：{effort, max_tokens, mode}。
      - ``reasoning_effort`` / ``reasoningEffort``（OpenCode 系字符串）。
    返回 None 表示客户端没发思考请求。
    """
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    th = data.get("thinking")
    if isinstance(th, dict):
        if th.get("type") == "disabled":
            return {"type": "disabled", "budget": 0, "wire": "anthropic"}
        if th.get("type") in (None, "enabled") and not th.get("budget_tokens"):
            return {"type": "adaptive", "effort": _extract_adaptive_effort(data),
                    "budget": 0, "wire": "anthropic"}
        if th.get("type") == "adaptive":
            return {"type": "adaptive", "effort": _extract_adaptive_effort(data),
                    "budget": 0, "wire": "anthropic"}
        budget = th.get("budget_tokens") or 0
        return {"type": "enabled",
                "budget": int(budget) if isinstance(budget, (int, float)) else 0,
                "wire": "anthropic"}

    rs = data.get("reasoning")
    if isinstance(rs, dict):
        eff = rs.get("effort")
        if isinstance(eff, str):
            if eff in _THINKING_OFF_VALUES:
                return {"type": "disabled", "budget": 0, "wire": "openai"}
            return {"type": "enabled", "effort": eff,
                    "budget": int(rs["max_tokens"]) if isinstance(rs.get("max_tokens"), int)
                    else _THINKING_BUDGETS.get(eff, 0), "wire": "openai"}
        if isinstance(rs.get("max_tokens"), int):
            return {"type": "enabled", "budget": int(rs["max_tokens"]), "wire": "openai"}
        return {"type": "enabled", "wire": "openai"}

    for key in ("reasoning_effort", "reasoningEffort"):
        eff = data.get(key)
        if isinstance(eff, str):
            if eff in _THINKING_OFF_VALUES:
                return {"type": "disabled", "budget": 0, "wire": "openai"}
            return {"type": "enabled", "effort": eff,
                    "budget": _THINKING_BUDGETS.get(eff, 0), "wire": "openai"}

    # --- Gemini thinkingConfig（防御性：客户端可能发 gemini 形状 body）---
    tc = data.get("thinkingConfig")
    if isinstance(tc, dict):
        lvl = tc.get("thinkingLevel")
        if isinstance(lvl, str) and lvl.upper() in ("LOW", "HIGH"):
            return {"type": "enabled", "effort": lvl.lower(), "budget": 0, "wire": "gemini"}
        budget = tc.get("thinkingBudget")
        if isinstance(budget, int):
            if budget == 0:
                return {"type": "disabled", "budget": 0, "wire": "gemini"}
            if budget < 0:  # -1 = 动态（按复杂度自适应）
                return {"type": "adaptive", "effort": "medium", "budget": 0, "wire": "gemini"}
            return {"type": "enabled", "budget": budget, "wire": "gemini"}

    return None


def _rewrite_thinking_for_upstream(body: bytes, cfg: PlatformConfig, platform: str = "") -> bytes:
    """对外重写思考：按目标上游能力 + 线协议翻译客户端传入的 thinking。

    出站按 ``cfg.effective_wire(platform)`` 翻成上游原生形状：
      - anthropic-messages → thinking:{type, budget_tokens}
      - openai-chat        → reasoning_effort:<effort>（Chat Completions 字段）
      - openai-responses   → reasoning:{effort}（Responses API 字段）
    上游不支持思考（thinking_options 空/只有 off）→ 剥离全部思考字段，避免 400。
    非 JSON body 原样返回。
    """
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict):
        return body

    client = _client_thinking(body)
    if client is None:
        return body  # 客户端没发思考，不干预

    opts = cfg.thinking_options or []
    supports = bool(opts) and any(o != "off" for o in opts)

    if not supports:
        changed = False
        for key in _THINKING_CLIENT_FIELDS:
            if key in data:
                data.pop(key, None)
                changed = True
        oc = data.get("output_config")
        if isinstance(oc, dict) and "effort" in oc:
            oc.pop("effort", None)
            if not oc:
                data.pop("output_config", None)
            changed = True
        if changed:
            log.info("thinking-level strip: upstream %s 不支持 thinking，已剥离", cfg.name)
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        return body

    # 上游支持 → 按 wire 翻译成本协议原生形状。
    # 剥离时按 wire 保留本协议原生字段：anthropic 保留 thinking+output_config，
    # 避免把原本正确的 anthropic 请求里的 output_config 误删。
    wire = cfg.effective_wire(platform) if hasattr(cfg, "effective_wire") else WIRE_ANTHROPIC_MESSAGES
    if client["type"] == "disabled":
        if wire == WIRE_OPENAI_CHAT:
            _strip_thinking_except(data, set())
            data["reasoning_effort"] = "none"
        elif wire == WIRE_OPENAI_RESPONSES:
            _strip_thinking_except(data, set())
            data["reasoning"] = {"effort": "none"}
        else:
            _strip_thinking_except(data, {"thinking", "output_config"})
            data["thinking"] = {"type": "disabled"}
    else:
        budget = client.get("budget") or 0
        effort = client.get("effort") or (_budget_to_effort(budget) if budget else "medium")
        if wire == WIRE_OPENAI_CHAT:
            _strip_thinking_except(data, set())
            data["reasoning_effort"] = effort
        elif wire == WIRE_OPENAI_RESPONSES:
            _strip_thinking_except(data, set())
            data["reasoning"] = {"effort": effort}
        else:
            _strip_thinking_except(data, {"thinking", "output_config"})
            data["thinking"] = {"type": "enabled", "budget_tokens": budget or 4096}
    log.info("thinking-level rewrite: upstream=%s wire=%s -> %s", cfg.name, wire, _peek_thinking(data))
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _strip_cache_control_scope_in_place(node) -> bool:
    """Mutate ``node`` in place, removing ``cache_control.scope`` keys.
    Returns True if anything changed."""
    changed = False
    if isinstance(node, dict):
        cc = node.get("cache_control")
        if isinstance(cc, dict):
            if "scope" in cc:
                cc.pop("scope", None)
                changed = True
            if not cc:
                node.pop("cache_control", None)
                changed = True
        for v in node.values():
            changed = _strip_cache_control_scope_in_place(v) or changed
    elif isinstance(node, list):
        for v in node:
            changed = _strip_cache_control_scope_in_place(v) or changed
    return changed


def _strip_thinking_blocks_in_place(node) -> bool:
    """Mutate ``node`` in place, removing ``type == "thinking"`` blocks.
    Returns True if anything changed."""
    changed = False
    if isinstance(node, list):
        i = 0
        while i < len(node):
            item = node[i]
            if isinstance(item, dict) and item.get("type") == "thinking":
                node.pop(i)
                changed = True
                continue  # 不增 i，list 已左移
            if isinstance(item, (dict, list)):
                changed = _strip_thinking_blocks_in_place(item) or changed
            i += 1
    elif isinstance(node, dict):
        for v in node.values():
            changed = _strip_thinking_blocks_in_place(v) or changed
    return changed


def _strip_cache_control_scope(body: bytes) -> bytes:
    """v0.66：递归剥掉所有 ``cache_control.scope`` 字段。

    OpenCode Zen 转给上游 provider 时拒绝带 ``scope`` 的 cache_control
    块（"unknown key 'scope'"）。Claude Code 客户端新版会发
    ``{"type": "ephemeral", "scope": "..."}``，得在转发前清理掉。

    只删 ``scope`` 子键，``type`` 保留 — 缓存语义不变。空对象顺手干掉。
    非 JSON / 解码失败一律原样返回，避免吞请求。
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not _strip_cache_control_scope_in_place(data):
        return body
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _strip_thinking_blocks(body: bytes) -> bytes:
    """v0.66：递归剥掉所有 ``type == "thinking"`` 块。

    OpenCode Zen 上游 provider 拒绝带 signature 的 thinking 块（"Invalid
    `signature` in `thinking` block"）。signature 是 Anthropic 对当前
    会话签的，跨上游/跨会话都验不过。

    整个 thinking 块一起删（text / tool_use / tool_result 等其他块保留）。
    剥掉的 thinking 在当前会话里丢上下文，模型下一轮自己重新想。Claude
    Code 客户端的状态自己留着，再次发起请求时不会真丢历史。
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not _strip_thinking_blocks_in_place(data):
        return body
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


# v0.199.1：模型不支持图片时的鲁棒性降级 —— 剥图 + 提示词注入。
# 图块在三种入站 wire 里的形态不同：
#   anthropic-messages  → messages[].content[] 里 type=="image"
#   openai-chat         → messages[].content[] 里 type=="image_url"
#   openai-responses    → input[].content[] 里 type=="input_image"
# 递归遍历（images 可能嵌在 tool_result 等子块里）。凡命中图片的 user
# message，在其末尾注入一段提示词告诉上游「用户发了图但我们剥了」，让
# 模型知道上下文里有缺失的视觉信息 —— 与「剥 namespace 工具」同一哲学：
# 能继续就继续，不硬报错。返回 (changed, dropped_count)。
_IMAGE_TYPE_HINTS = ("image", "image_url", "input_image")


def _strip_images_with_notice_in_place(node: Any) -> tuple[bool, int]:
    """剥掉所有图片块并在所在 block 列表注入提示词（原地）。

    返回 ``(changed, dropped)``：changed=是否有任何改动；dropped=剥掉的
    图片块总数。

    统一递归规则：
      - 列表若含 ``type`` 键的 dict（block 列表：message.content /
        tool_result.content / input[].content 等），剥掉 ``type in
        {image, image_url, input_image}`` 的块，剥到 ≥1 张时往**该列表**
        尾部补一个提示 text 块；messages/input 列表（item 带 role 不带
        type）不注入提示，只往下递归。
      - dict 对所有 value 递归。
    """
    if isinstance(node, list):
        # 直接在本层剥掉图片块（image/image_url/input_image）。
        dropped_here = sum(
            1 for item in node
            if isinstance(item, dict) and item.get("type") in _IMAGE_TYPE_HINTS
        )
        kept: list[Any] = []
        changed = False
        for item in node:
            if isinstance(item, dict) and item.get("type") in _IMAGE_TYPE_HINTS:
                continue  # 剥掉图片块
            c, d = _strip_images_with_notice_in_place(item)
            changed = changed or c
            dropped_here += d
            kept.append(item)
        if dropped_here:
            # 只在 block 列表（item 带 type）注入提示词；messages/input
            # 这类角色列表不注（它们的 content 子列表会各自注入）。剥到内容
            # 空列表（只剩图）时也补一句，让上游至少知道发了图。
            is_block_list = any(
                isinstance(x, dict) and "type" in x for x in kept
            ) or not kept
            # 提示词块类型按所在列表的 wire 适配：openai-responses 的 input
            # 只认 input_text，用 text 会再触发 schema 错误；其余 wire 用 text。
            notice_type = "input_text" if any(
                isinstance(x, dict) and x.get("type") == "input_text"
                for x in kept
            ) else "text"
            if is_block_list and not any(
                isinstance(b, dict) and b.get("type") in ("text", "input_text")
                and "中继已剥离" in (b.get("text") or "")
                for b in kept
            ):
                kept.append({"type": notice_type, "text": _IMAGE_NOTICE})
            # 原地改回（node 可能是 messages/input 等被外部持有的列表）。
            node[:] = kept
        return changed or bool(dropped_here), dropped_here
    if isinstance(node, dict):
        changed = False
        dropped = 0
        for v in node.values():
            c, d = _strip_images_with_notice_in_place(v)
            changed = changed or c
            dropped += d
        return changed, dropped
    return False, 0


# v0.199.1：非 vision 模型时剥图 —— 判定基于目标模型是否在顶层
# vision_models 名单里。空名单（未配置）= 全部按非 vision 处理（剥图 +
# 提示词）。与 models.py 的 modalities 判定保持一致：命中的模型能收图，
# 其余一律剥。
_IMAGE_NOTICE = ("（注意：用户本次发送了图片，但当前模型不支持多模态输入，"
                 "中继已剥离图片内容。请据此上下文继续回答；如必须查看图片"
                 "请让用户改用支持图片的模型。）")


def _strip_disallowed_content(
    body: bytes, *, vision_models: list[str] | None = None, model: str | None = None,
) -> bytes:
    """Combined single-pass version of the two strips above (v0.9).

    ``cache_control.scope`` removal and thinking-block removal each did a
    full JSON parse + serialize on every request. Multi-MB bodies from
    Claude Code make that cost real, so the proxy's hot path runs in one
    round-trip. The standalone helpers stay for callers that need one.

    v0.199.1：非 vision 模型时额外剥掉图片块（三种 wire 形态）并注入
    提示词。判定与 cross-wire 路径（_relay_cross_wire）一致：显式传入
    ``model`` 时，模型不在 vision 名单 → 剥图；名单为空（未配置）→ 全部
    按非 vision 剥（fail-open，图不转发就不报错）。热路径预检：绝大多数
    请求无图，字节级子串扫一眼直接跳过整树递归。
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    changed = _strip_cache_control_scope_in_place(data)
    changed = _strip_thinking_blocks_in_place(data) or changed
    changed = _strip_unsupported_tools_in_place(data) or changed
    # v0.199.1：剥图条件 —— 模型不在 vision 名单（或名单为空）且请求带图。
    # 之前误写成「名单整体为空才剥」，导致配置了 vision_models 后直连路径
    # 永不剥图（模型不在名单也照发），deepseek 仍 400。
    if model is None:
        # 未显式传模型时按旧语义兜底：空名单才剥（调用方一般会传 model）。
        strip_images = not vision_models
    else:
        strip_images = model not in (vision_models or [])
    if strip_images and (
        b'"image' in body or b'"image_url' in body or b'"input_image' in body
    ):
        c, d = _strip_images_with_notice_in_place(data)
        changed = changed or c
    if not changed:
        return body
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _strip_unsupported_tools_in_place(data: Any) -> bool:
    """Drop tools whose `type` the upstream OpenAI-compatible gateway rejects.

    Codex (OpenAI Responses API) emits a `namespace` tool type that
    中转站/上游（如 minnimax.chat）只认 `function` / `web_search*` /
    `custom` / `tool_search`，遇到 `namespace` 直接 400
    "unknown variant namespace". Stripping it lets the request through;
    Codex degrades gracefully to its remaining (function) tools.
    Blacklist-based: only KNOWN-bad types (e.g. Codex's `namespace`) are
    dropped. Any other type — including future/unknown standard tool types
    and Anthropic-style tools (no `type` field) — passes through untouched.
    """
    if not isinstance(data, dict):
        return False
    tools = data.get("tools")
    if not isinstance(tools, list):
        return False
    # BLACKLIST, not whitelist: a whitelist would silently drop any new
    # legitimate tool type and break clients later (exactly the class of bug
    # we just hit with Anthropic tools, which have no `type` field).
    drop_types = {"namespace"}
    kept = []
    for t in tools:
        if not isinstance(t, dict):
            kept.append(t)
            continue
        ttype = t.get("type")
        if ttype is None or ttype not in drop_types:
            kept.append(t)
    if len(kept) == len(tools):
        return False
    data["tools"] = kept
    return True


def _extract_last_user_message(body: bytes) -> tuple[Optional[str], Optional[str]]:
    """Pull the last user-role text from a JSON request body.

    Both Anthropic Messages and OpenAI Chat Completions use a `messages` array
    of `{"role": ..., "content": ...}` objects. Returns ``(plain_text, raw_json)``.
    The plain text is what we'll index for search; raw_json is the verbatim
    block so non-text payloads (tool_use, images) survive if we ever surface
    them in the GUI.
    """
    try:
        obj = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return None, None
    for entry in reversed(msgs):
        if isinstance(entry, dict) and entry.get("role") == "user":
            raw_json = json.dumps(entry, ensure_ascii=False)
            content = entry.get("content")
            if isinstance(content, str):
                return content, raw_json
            # Anthropic style: content is a list of blocks.
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        t = block.get("text")
                        if isinstance(t, str):
                            parts.append(t)
                    elif btype == "tool_result":
                        # Tool-execution turns: the user's last "message"
                        # is one or more tool_result blocks carrying tool
                        # stdout/stderr. Extract any text inside so the
                        # conversation dialog has something to show, and
                        # so /messages/search LIKE matches tool output.
                        # The inner `content` is itself either a string
                        # or a list of content blocks (text / image).
                        inner = block.get("content")
                        if isinstance(inner, str):
                            parts.append(inner)
                        elif isinstance(inner, list):
                            for sub in inner:
                                if (
                                    isinstance(sub, dict)
                                    and sub.get("type") == "text"
                                ):
                                    t = sub.get("text")
                                    if isinstance(t, str):
                                        parts.append(t)
                # Drop empty fragments so an all-empty content list
                # returns None (lets the dialog fall back to raw_json)
                # rather than a literal "".
                parts = [p for p in parts if p]
                return ("\n".join(parts) if parts else None), raw_json
            return None, raw_json
    return None, None


async def _check_auth(
    request: Request, required_token: str, platform: str = "unknown",
) -> Optional[Response]:
    """If a token is configured, verify the request carries it. Return 401 response or None.

    v0.98.2 决策钩子 ``decide_auth``：任何请求都先给插件一次判定机会
    （``info`` 含 platform / key_masked / path / require_token）。插件返回
    ``True`` = 放行（跳过默认校验），返回其它 = 走默认逻辑。这是"替换
    层"的最初落点 —— 插件可完全接管认证判定。
    """
    auth = request.headers.get("authorization", "")
    # V0.4 替换层：认证子系统覆盖。插件整体接管时直接返回其 Response；
    # None 则继续默认判定（decide_auth 钩子 → require_auth_token 校验）。
    covered, _provider = await arun_overrides("auth", request, platform)
    if covered is not None:
        return covered
    info = {
        "platform": platform,
        "kind": "require_token",
        "key_masked": _mask_key(auth),
        "path": request.url.path,
        "require_token": bool(required_token),
    }
    verdict = await run_hooks("decide_auth", info)
    if verdict is True:
        return None
    if not required_token:
        return None
    if auth.lower().startswith("bearer "):
        if auth[7:].strip() == required_token:
            return None
    if request.headers.get("x-relay-token") == required_token:
        return None
    emit_event(
        "auth.failed",
        platform=platform, kind="require_token",
        key=_mask_key(auth), model=None,
    )
    return Response(
        content=b'{"error":"unauthorized"}',
        status_code=401,
        media_type="application/json",
    )


def _apply_auth_override(
    headers: dict[str, str], cfg: PlatformConfig, platform: str = "anthropic"
) -> tuple[Optional[str], bool]:
    """Decide what auth header value the upstream sees.

    Returns ``(api_key_alias, needs_sentinel_error)``:

    - ``api_key_alias``: the upstream name (for DB bookkeeping) if an
      override was applied, else ``None``.
    - ``needs_sentinel_error``: ``True`` when the client sent the
      :data:`AUTH_AUTO_SENTINEL` value but the relay has no upstream
      ``api_key`` to fill in with. Caller should return 503.

    Header name follows ``cfg.effective_auth_style(platform)`` (v0.12.1):
    auth_style > auth_header > wire 默认 —— 用户在上游里配置的"可正常请求
    的格式"在直通 / 跨协议 / 探测所有路径上一致生效。

    Behavior matrix:

    +-------------------+--------------------+--------------------------------+
    | cfg.api_key       | client sent        | result                         |
    +===================+====================+================================+
    | set               | anything           | use cfg.api_key (override)     |
    +-------------------+--------------------+--------------------------------+
    | unset             | "auto" sentinel    | pass through; needs_sentinel_  |
    |                   |                    | error=True                     |
    +-------------------+--------------------+--------------------------------+
    | unset             | other              | pass through                   |
    +-------------------+--------------------+--------------------------------+
    """
    style = cfg.effective_auth_style(platform)
    header_name = {"bearer": "authorization", "x-api-key": "x-api-key"}.get(style)
    # v0.98.3 扩展层：auth_style 配成插件方案名时，用插件方案拼头。
    scheme = (
        auth_scheme_for(style)
        if header_name is None and style != "none"
        else None
    )

    if cfg.api_key:
        # Upstream key configured — always use it (regardless of what the
        # client sent). Remove every client auth header first so the
        # upstream only ever sees the relay's key. Some providers (DeepSeek)
        # prefer `Authorization` over `x-api-key`, so leaving the client's
        # `Authorization: Bearer <dummy>` in place makes them 401 even when
        # the correct `x-api-key` is present alongside it.
        for k in list(headers):
            if k.lower() in ("authorization", "x-api-key", "api-key"):
                headers.pop(k, None)
        if scheme is not None:
            name, value = scheme(cfg, platform)
        else:
            # v0.12.1：统一走 _auth_header_value —— 归一化去 "Bearer " 前缀，
            # bearer 补一个前缀、x-api-key 用裸 key。
            name, value = _auth_header_value(style, cfg.api_key)
        if value:
            headers[name] = value
        return cfg.name, False

    # No upstream key — inspect what the client sent.
    if not header_name:
        # auth_style=none：上游不收鉴权头，直接透传。
        return None, False
    client_value = ""
    for k, v in headers.items():
        if k.lower() == header_name.lower():
            client_value = v
            break
    if client_value.strip().lower() == AUTH_AUTO_SENTINEL:
        return None, True
    return None, False


# ---------------------------------------------------------------------------
# v0.12 分发判定 + 告警（docs/wire-dispatch-plan.md §2）
#
# 分发看客户端"钥匙"（认证头里的 key）和 model 两个字：
#   auto + auto        → 中继转发（active 上游 + 现有强映射/auto 兜底链）
#   真值 + 真值        → 透传（同平台段按归一化 key 匹配上游）
#   恰好一个 auto      → 混合态 = 配置错误，400 + 告警
#   key 未命中任何上游 → 502 relay_unknown_key + 告警
# 不跨平台：/anthropic 只搜 anthropic 段，/openai 只搜 openai 段。
# ---------------------------------------------------------------------------

_ALERT_MAX = 50          # 环形告警列表容量
_ALERT_DEDUPE_WINDOW = 300.0  # 同 (kind,platform,key,model) 5 分钟内只告警一次
_dispatch_alerts: list[dict] = []
_alert_seq = 0
_alert_dedup: dict[tuple, float] = {}


def _normalize_key(value: Optional[str]) -> str:
    """归一化认证 key：strip 空白 + 去大小写不敏感的 Bearer 前缀。

    openai 段历史配置存的是 "Bearer gw-xxx"，客户端发的是裸 key——
    两侧都过这个函数再比较。
    """
    if not value:
        return ""
    v = value.strip()
    low = v.lower()
    if low.startswith("bearer "):
        v = v[7:].strip()
    return v


def _auth_header_value(style: str, key: str) -> tuple[str, str]:
    """按鉴权风格拼出 (header_name, header_value)。

    key 先归一化去 "Bearer " 前缀 —— 历史配置把 "Bearer gw-xxx" 整个存
    进 api_key，跨协议路径再拼一次会变成 "Bearer Bearer gw-xxx"（v0.12.1
    修复）。bearer 统一补一个前缀，x-api-key 用裸 key；空 key 返回空值。
    """
    k = _normalize_key(key)
    if style == "bearer":
        return "authorization", (f"Bearer {k}" if k else "")
    return "x-api-key", k


def _client_auth_key(request: Request, platform: str) -> str:
    """读客户端认证 key（归一化后）。anthropic 入口优先 x-api-key，
    openai 入口优先 authorization；读不到返回 ""（=auto 侧）。"""
    if platform == "openai":
        candidates = (request.headers.get("authorization"),
                      request.headers.get("x-api-key"))
    else:
        candidates = (request.headers.get("x-api-key"),
                      request.headers.get("authorization"))
    for c in candidates:
        if c:
            return _normalize_key(c)
    return ""


def _mask_key(key: str) -> str:
    """告警展示用脱敏：前 6 后 4。过短则只留前 2。"""
    if len(key) <= 10:
        return (key[:2] + "…") if key else "（空）"
    return key[:6] + "…" + key[-4:]


def push_dispatch_alert(kind: str, platform: str, raw_key: str, model: Optional[str]) -> None:
    """记录一条分发告警（混合态 / 未知 key），GUI 经 /api/alerts 拉取展示。

    同 (kind, platform, key, model) 组合在 _ALERT_DEDUPE_WINDOW 内只记
    一次——同一个错误配置的客户端往往连发重试，不能弹一串。
    """
    global _alert_seq
    now = time.time()
    masked = _mask_key(raw_key)
    dedup = (kind, platform, masked, model or "")
    last = _alert_dedup.get(dedup, 0.0)
    if now - last < _ALERT_DEDUPE_WINDOW:
        return
    _alert_dedup[dedup] = now
    _alert_seq += 1
    _dispatch_alerts.append({
        "id": _alert_seq,
        "kind": kind,
        "platform": platform,
        "key": masked,
        "model": model,
        "at": now,
    })
    emit_event(
        "auth.failed",
        platform=platform, kind=kind,
        key=masked, model=model, at=now,
    )
    if len(_dispatch_alerts) > _ALERT_MAX:
        del _dispatch_alerts[: len(_dispatch_alerts) - _ALERT_MAX]


def take_dispatch_alerts(since_id: int = 0) -> list[dict]:
    """取 id 大于 since_id 的告警（/api/alerts 轮询用）。"""
    return [a for a in _dispatch_alerts if a["id"] > since_id]


def _match_upstream_by_key(
    settings: Any, platform: str, client_key: str, model: Optional[str],
) -> tuple[Optional[PlatformConfig], Optional[str]]:
    """透传：同平台段内按归一化 key 匹配上游。

    返回 (命中的上游, 需要改写的兜底模型)。兜底规则：
      - key 命中 + 模型在 allowed_models（或列表为空=任意）→ (上游, None)
      - key 命中 + 模型不在列表 → (上游, cfg.model 或 allowed_models[0])
      - key 未命中 → (None, None)
    同 key 多上游取配置顺序第一个（日志提示多义）。
    """
    ck = _normalize_key(client_key)
    if not ck:
        return None, None
    matches = [
        uc for uc in settings.upstreams_for(platform)
        if _normalize_key(uc.api_key or "") == ck
    ]
    if not matches:
        return None, None
    if len(matches) > 1:
        log.warning(
            "dispatch: key %s matches %d upstreams (%s); using the first",
            _mask_key(ck), len(matches), ",".join(m.name for m in matches),
        )
    uc = matches[0]
    if not uc.allowed_models or (model and model in uc.allowed_models):
        return uc, None
    fallback = uc.model or (uc.allowed_models[0] if uc.allowed_models else None)
    return uc, fallback


async def _reject_dispatch(
    app_state: Any,
    inflight_id: str,
    platform: str,
    model: Optional[str],
    *,
    status_code: int,
    error_code: str,
    message: str,
    alert_kind: str,
    client_key: str,
    # v0.155：客户端工具名（透传到拒绝行的 agent 列）。
    agent: str = "",
    # v0.157：原始 User-Agent 头原文（拒绝行的 raw_ua 列）。
    raw_ua: str = "",
) -> Response:
    """混合态 / 未知 key 的统一拒绝路径：告警 + DB 错误行 + 清 inflight + 返回错误包。"""
    push_dispatch_alert(alert_kind, platform, client_key, model)
    log.warning("dispatch reject [%s] platform=%s key=%s model=%s",
                alert_kind, platform, _mask_key(client_key), model)
    try:
        await app_state.db.record(
            platform=platform,
            model=model,
            request_id=None,
            usage=UsageAcc(),
            status_code=status_code,
            error=error_code,
            upstream=("未知key" if alert_kind == "unknown_key" else "混合态"),
            api_key_alias=None,
            # v0.143：拒绝行没有真实 wire 入向，endpoint 写 platform 名占位。
            # 不写 NULL 是因为 NULL 会被 COALESCE 退回 platform，给排查留线索。
            endpoint=platform,
            agent=agent,
            raw_ua=raw_ua,
        )
    except Exception as exc:
        log.exception("db.record failed in dispatch reject: %s", exc)
    try:
        await _complete_inflight(inflight_id)
    except Exception as exc:
        log.exception("_complete_inflight failed in dispatch reject: %s", exc)
    return Response(
        content=json.dumps({"error": error_code, "detail": message}).encode(),
        status_code=status_code,
        media_type="application/json",
    )


async def _relay_cross_wire(
    *,
    request: Request,
    cfg: PlatformConfig,
    inflight_id: str,
    model: Optional[str],
    body: bytes,
    platform: str,
    app_state: Any,
    # v0.X 鲁棒性增强：客户端实际 wire（路由层路径推断 + body 嗅探得出）。
    # 替代原 platform 硬编码 —— 客户端拼错前缀时仍能正确转换。
    # 缺省走 platform 隐式推断（向后兼容直接调用方）。
    client_wire: Optional[str] = None,
    # v0.155：客户端工具名（透传到内部 record() 写 agent 列）。
    agent: str = "",
    # v0.157：原始 User-Agent 头原文，透传到内部 record() 写 raw_ua 列。
    raw_ua: str = "",
) -> Response:
    """v0.12 跨协议转发（docs/wire-dispatch-plan.md §4）。

    客户端入口 wire != 上游 wire 时走这里：请求体按上游 wire 重装（恒发
    ``url + endpoint``），响应（含流式）翻译回客户端 wire。三种 wire
    （anthropic-messages / openai-chat / openai-responses）双向互转，
    转换内核是 martian-linguafranca（见 wire.py）。
    """
    from .config import (
        WIRE_OPENAI_CHAT as _OAI,
        WIRE_ANTHROPIC_MESSAGES as _ANT,
    )
    from .wire import (
        convert_request,
        convert_response,
        convert_stream,
        serialize_sse_event,
        error_to_client,
        WireConversionError,
    )
    from .parsers.openai import OpenAIUsageParser

    db: Database = app_state.db
    # v0.163：cross-wire 路径补绑 settings —— 底部 finally 里
    # ``settings.relay_save_messages`` 依赖它；此前未定义直接 NameError，
    # 上游 200 返回后整条流被掐断（opencode 表现为反复重试）。
    settings = app_state.settings

    # v0.X 鲁棒性增强：客户端 wire 优先用 caller 传入（路径推断+body 嗅探）；
    # 缺省时回退到 platform 隐式推断（向后兼容直接调用方）。
    if client_wire:
        platform_wire = client_wire
    else:
        platform_wire = _OAI if platform == "openai" else _ANT
    upstream_wire = cfg.effective_wire(platform)

    _tlog.info(
        "CROSSWIRE ENTER platform=%s upstream=%s url=%s model=%s wire=%s->%s auth=%s body=%s",
        platform, cfg.name, cfg.url, model, platform_wire, upstream_wire,
        cfg.effective_auth_style(platform), _body_preview(body, 2000),
    )

    if platform_wire == upstream_wire:
        # 调用方只在 wire 不同时进入，防御一下。
        await _complete_inflight(inflight_id)
        return Response(content=b'{"error":"relay misconfigured"}', status_code=500)

    try:
        inbound = json.loads(body.decode("utf-8"))
    except Exception:
        await _complete_inflight(inflight_id)
        return Response(
            content=b'{"error":"invalid_request_error","message":"bad json body"}',
            status_code=400, media_type="application/json",
        )

    try:
        # v0.164：Codex (openai-responses) 在 tools 里带 type=namespace 工具，
        # linguafranca convert_request 解析工具类型时直接抛 WireConversionError
        # "unknown variant `namespace`"。必须在转换**之前**剥掉黑名单工具类型，
        # 否则下游对 out_payload 的 strip 永远轮不到（转换第一步就炸）。
        _strip_unsupported_tools_in_place(inbound)
        # v0.199.1：非 vision 模型剥图 + 提示词（判定基于 settings.vision_models，
        # 与直连路径一致）。跨协议转换前处理，避免图片块经 linguafranca 转成
        # anthropic image 块后仍被上游 400 "Model do not support image input"。
        # v0.200：并入上游条目内 vision_models（cfg.vision_models）—— 同一个
        # 模型名在不同上游可分别声明能不能吃图；命中任一名单才保留图。
        vision = set(getattr(settings, "vision_models", None) or [])
        vision |= set(getattr(cfg, "vision_models", None) or [])
        if model not in vision:
            _strip_images_with_notice_in_place(inbound)
        out_payload = convert_request(inbound, platform_wire, upstream_wire)
        # Drop tool types the upstream OpenAI gateway rejects (Codex's
        # `namespace`). Blacklist-only: tools without a `type` field
        # (Anthropic/Claude) and all standard types pass through untouched.
        _strip_unsupported_tools_in_place(out_payload)
    except WireConversionError as exc:
        log.warning("%s cross-wire request convert failed: %s", platform, exc)
        await _complete_inflight(inflight_id)
        return Response(
            content=json.dumps(error_to_client(
                {"error": {"type": "invalid_request_error", "message": str(exc)}},
                platform_wire,
            )).encode(),
            status_code=400, media_type="application/json",
        )
    # v0.97.1：DeepSeek 思考型 openai 上游回传 assistant(tool_calls) 时必须
    # 带 reasoning_content —— 从 _REASONING_BY_TOOL_CALL 按 tool_call id
    # 注入（claude code 回传的历史 assistant 消息可能多个，逐个补）。
    # v0.97.2：注入前先合并 linguafranca 拆开的连续 assistant 消息（thinking
    # 块转出的 reasoning_content 归到带 tool_calls 的消息上），否则 DeepSeek
    # 看到 tool_calls 与 reasoning_content 分属两条消息 → 400。
    # v0.97.3：只对 DeepSeek 系思考型 openai 上游注入 —— 非思考 openai 上游
    # （MiniMax 等）不认 reasoning_content 字段，注入空串会被 400。
    if isinstance(out_payload, dict):
        _merge_split_assistant_messages(out_payload.get("messages"))
        _inject_reasoning_to_messages(
            out_payload.get("messages"),
            upstream_wire == _OAI and _upstream_needs_reasoning(cfg, model),
        )

    upstream_url = join_endpoint(cfg.url, cfg.effective_endpoint(platform))

    # v0.12 跨协议日志：转换方向 + 目标 URL，便于 tail 日志确认走了哪条路径。
    log.info(
        "CROSS-WIRE platform=%s wire=%s->%s upstream=%s url=%s",
        platform, platform_wire, upstream_wire, cfg.name, upstream_url,
    )

    headers = {"content-type": "application/json"}
    if upstream_wire == _ANT:
        headers["anthropic-version"] = "2023-06-01"
    style = cfg.effective_auth_style(platform)
    scheme = (
        auth_scheme_for(style)
        if style not in ("bearer", "x-api-key", "none")
        else None
    )
    if scheme is not None:
        hname, hval = scheme(cfg, platform)
    else:
        hname, hval = _auth_header_value(style, cfg.api_key or "")
    if hval:
        headers[hname] = hval

    # usage parser 跟上游 wire 走：注册表按 wire 选（v0.98 插件可注册
    # 新 wire 的 parser）；未注册回退 OpenAIUsageParser（认两种 openai wire）。
    parser_cls = parser_factory_for(upstream_wire) or OpenAIUsageParser

    # v0.98 插件钩子：pre_upstream（跨协议路径）—— 发送前最后一改。
    # body 已是转换后的上游 payload；插件可整体替换 body / headers，
    # 其余字段只读（info 见 plugin.py register_hook docstring）。
    out_body_bytes = json.dumps(out_payload).encode("utf-8")
    pinfo: dict[str, Any] = {
        "platform": platform,
        "cfg": cfg,
        "model": model,
        "body": out_body_bytes,
        "headers": headers,
        "upstream_url": upstream_url,
    }
    await run_hooks("pre_upstream", pinfo)
    headers = pinfo["headers"]
    out_body_bytes = pinfo["body"]

    await _set_inflight_phase(inflight_id, "calling")
    client = _get_client(upstream_url)
    try:
        upstream_req = client.build_request(
            method="POST", url=upstream_url, headers=headers,
            content=out_body_bytes,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        log.warning("%s cross-wire upstream connect failed: %s", platform, exc)
        try:
            await db.record(
                platform=platform, model=out_payload.get("model"),
                request_id=None, usage=UsageAcc(), status_code=0,
                error=f"upstream_error: {exc}", upstream=cfg.name,
                api_key_alias=None,
                # v0.143：客户端入口 wire 透传 → 写 endpoint。
                endpoint=client_wire,
                agent=agent,
                raw_ua=raw_ua,
            )
        except Exception as rec_exc:
            log.exception("db.record failed in cross-wire connect error: %s", rec_exc)
        try:
            await _complete_inflight(inflight_id)
        except Exception as com_exc:
            log.exception("_complete_inflight failed in cross-wire error: %s", com_exc)
        return Response(
            content=json.dumps(_error_body(platform, str(exc))).encode(),
            status_code=502, media_type="application/json",
        )

    status = upstream_resp.status_code
    request_id = upstream_resp.headers.get("request-id") or \
        upstream_resp.headers.get("x-request-id")
    content_type = upstream_resp.headers.get("content-type", "")
    is_sse = "text/event-stream" in content_type

    if not is_sse:
        content = await upstream_resp.aread()
        await upstream_resp.aclose()
        try:
            data = json.loads(content or b"{}")
        except Exception:
            data = {}
        if status >= 400:
            out_body = json.dumps(error_to_client(data, platform_wire)).encode()
        else:
            # v0.97.1：非流式响应同样捕获 reasoning_content → 绑定 tool_call id
            if upstream_wire == _OAI:
                try:
                    msg = (data.get("choices") or [{}])[0].get("message") or {}
                    _bind_reasoning_to_tool_calls(
                        msg.get("reasoning_content") or "", msg.get("tool_calls"),
                    )
                except Exception:
                    log.debug("non-stream reasoning bind failed", exc_info=True)
            try:
                out_body = json.dumps(
                    convert_response(data, upstream_wire, platform_wire)
                ).encode()
            except WireConversionError as exc:
                log.warning("%s cross-wire response convert failed: %s", platform, exc)
                out_body = json.dumps(error_to_client(
                    {"error": {"type": "api_error", "message": f"convert failed: {exc}"}},
                    platform_wire,
                )).encode()
        parser = parser_cls()
        usage = parser.extract_from_json(content or b"")
        try:
            req_db_id = await db.record(
                platform=platform, model=out_payload.get("model"),
                request_id=request_id, usage=usage, status_code=status,
                error=None if status < 400 else f"upstream_{status}",
                upstream=cfg.name, api_key_alias=None,
                # v0.143：客户端入口 wire 透传 → 写 endpoint。
                endpoint=client_wire,
                agent=agent,
                raw_ua=raw_ua,
            )
        except Exception as rec_exc:
            req_db_id = 0
            log.exception("db.record failed in cross-wire non-stream: %s", rec_exc)
        # v0.120：cross-wire 非流式路径也保存消息原文
        if req_db_id and settings.relay_save_messages:
            try:
                user_text, user_json = _extract_last_user_message(out_body_bytes)
                await db.record_messages(
                    req_db_id,
                    user_text=user_text,
                    user_json=user_json,
                    assistant_text=parser.assembled_text() or None,
                    assistant_json=content.decode("utf-8", "replace") if content else None,
                    thinking_text=parser.assembled_thinking() or None,
                )
            except Exception as exc:
                log.exception("db.record_messages failed in cross-wire non-stream: %s", exc)
        try:
            await _complete_inflight(inflight_id)
        except Exception as com_exc:
            log.exception("_complete_inflight failed in cross-wire non-stream: %s", com_exc)
        # v0.89：非流式响应一次性广播 done —— 侧栏看到"完整内容 + 终值"
        try:
            u = usage
            await _broadcast_live_event(
                inflight_id, "done",
                phase="done",
                assistant_text=parser.assembled_text(),
                thinking_text=parser.assembled_thinking() or "",
                tool_use_json=parser.assembled_tool_use_json() or "",
                usage_live={
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_input_tokens": u.cache_read_input_tokens,
                    "cache_creation_input_tokens": u.cache_creation_input_tokens,
                },
                error=None if status < 400 else f"upstream_{status}",
            )
        except Exception:
            log.exception("broadcast done event failed in cross-wire non-stream")
        # v0.98 插件钩子：post_response（跨协议非流式）+ request.done 事件。
        # 跨协议路径不落 record_messages，req_db_id 恒 None。
        try:
            rinfo = {
                "platform": platform,
                "cfg": cfg,
                "model": model,
                "status": status,
                "usage": usage,
                "error": None if status < 400 else f"upstream_{status}",
                "req_db_id": None,
                "upstream": cfg.name,
                "request_id": request_id,
                "streaming": False,
            }
            await run_hooks("post_response", rinfo)
            emit_event("request.done", **rinfo)
        except Exception:
            log.exception("plugin post_response hooks failed in cross-wire non-stream")
        return Response(
            content=out_body, status_code=status, media_type="application/json",
        )

    # ---- streaming：上游 SSE → JSON 事件 → linguafranca 转换 → 平台 SSE ----
    parser = parser_cls()
    error_msg: Optional[str] = None

    async def sse_events() -> "AsyncIterator[dict]":
        """把上游 SSE 字节流解析成 JSON 事件 async 迭代器。

        openai-chat 上游做归一化：丢弃非标准元数据 chunk（opencode.ai 末尾
        发 ``{"choices":[],"cost":...}`` 这种缺 id/object 的）、剥离 deepseek
        系非标准 ``reasoning_content`` 字段，否则 linguafranca 严格 schema 校验
        会挂（missing field id / 未知字段）。
        """
        buf = bytearray()
        # v0.97.1：本响应内累积的 reasoning_content（流式 delta 逐片追加），
        # 见 tool_calls 时绑定到对应 tool_call id。
        _current_reasoning: list[str] = []
        # v0.97.4：本响应内已开片（linguafranca 已建 content_block）的 tool_call
        # index。用于识别「首片即带完整参数」的危险 chunk，见 helper docstring。
        opened_tool_call_indexes: set = set()
        async for chunk in upstream_resp.aiter_bytes():
            await _bump_chunk_activity(inflight_id)
            try:
                parser.feed(chunk)
            except Exception as exc:
                log.debug("cross-wire parser feed error: %s", exc)
            # v0.89：每个上游字节都更新 inflight + 广播 —— 侧栏实时跳动。
            # 这里拿到的是"上游已发出 / 已被解析器累积"的状态（parser 已
            # 累计的文本与 usage），所以即使下游 linguafranca 转换还在飞
            # 也不影响侧栏显示。thinking 在此路径下基本为空（上游 reasoning
            # 在 gen() 上方就被剥掉了），但仍走同一路，保持三个流式路径
            # 行为一致。
            try:
                assistant_now = parser.assembled_text()
                thinking_now = parser.assembled_thinking() or ""
                u = parser.usage
                # v0.113x：output_tokens 估算（字符长度粗估），仅供侧栏实时
                # 跳动显示。OpenAI 上游 linguafranca 解析器逐帧更新
                # u.output_tokens，但跨协议路径帧间可能仍有空隙；估算补齐。
                output_est = _estimate_output_tokens(
                    (assistant_now or "") + (thinking_now or "")
                )
                usage_now = {
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "output_tokens_est": max(u.output_tokens, output_est),
                    "cache_read_input_tokens": u.cache_read_input_tokens,
                    "cache_creation_input_tokens": u.cache_creation_input_tokens,
                }
                await _update_inflight(
                    inflight_id,
                    assistant_text=assistant_now,
                    thinking_text=thinking_now,
                    usage_live=usage_now,
                )
                await _broadcast_live_event(
                    inflight_id, "delta",
                    assistant_text=assistant_now,
                    thinking_text=thinking_now,
                    usage_live=usage_now,
                )
            except Exception:
                log.exception("cross-wire live broadcast failed")
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl]).decode("utf-8", "replace").strip()
                del buf[: nl + 1]
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    continue
                try:
                    d = json.loads(data_str)
                except Exception:
                    continue
                if upstream_wire == _OAI:
                    # 丢弃缺 object 的非标准 chunk（cost 元数据等）
                    if not isinstance(d, dict) or d.get("object") != "chat.completion.chunk":
                        continue
                    # v0.97.1：剥离 reasoning_content（deepseek 推理模型的
                    # 思考文本）同时按 tool_call id 存一份 —— deepseek 要求
                    # 带工具调用的对话回传时必须原样带 reasoning_content，
                    # 否则下次请求 400。存好在请求侧 _inject_reasoning 补回。
                    # 思考在 tool_calls 出现前就已流完，绑定时取此刻累计值。
                    for ch in d.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        if delta.get("reasoning_content"):
                            _current_reasoning.append(delta.get("reasoning_content"))
                        if delta.get("tool_calls"):
                            _bind_reasoning_to_tool_calls(
                                "".join(_current_reasoning), delta.get("tool_calls"),
                            )
                        if "reasoning_content" in delta:
                            delta.pop("reasoning_content")
                    # v0.97.4：linguafranca 丢「首片即带完整参数」的 tool_calls
                    # arguments（MiniMax/DeepSeek）→ 客户端 tool_use input={} →
                    # 「Invalid tool parameters」。拆成 空参数首片 + 独立全量参数片。
                    split = _split_openai_tool_call_arguments(
                        d, opened_tool_call_indexes,
                    )
                    for c in split or [d]:
                        yield c
                else:
                    yield d
                # v0.87：上游是 anthropic 时，message_stop 后主动结束，不等
                # 上游关连接（部分上游 message_stop 后连接不关闭，否则 sse_events
                # 会一直 aiter_bytes 挂住，phase=streaming 直到 sweeper 90s 强清）。
                # 用**当前行解析出的 d** 判断（而非 parser.message_stop_seen）——
                # parser.feed 是一次处理完整个 chunk 的所有帧，会提前把
                # message_stop_seen 置 True，导致 buf 逐行还没 yield 完所有
                # 事件（如 content_block_delta 的 "Hi"）就提前 return，丢内容。
                if upstream_wire == _ANT and isinstance(d, dict) and d.get("type") == "message_stop":
                    return

    async def gen() -> "AsyncIterator[bytes]":
        nonlocal error_msg
        await _set_inflight_phase(inflight_id, "streaming")
        # v0.166：收尾逻辑抽成独立 async 闭包，finally 里用 asyncio.shield
        # 包裹调用 —— 客户端断开时 starlette 取消本任务，finally 内第一个
        # await 会被再次 CancelledError 打断；shield 后取消信号被外层吞掉，
        # 整段记录流程得以完整执行（codex 收完即断的请求也能入库）。
        async def _cross_wire_finalize() -> None:
            try:
                await upstream_resp.aclose()
            except Exception:
                pass
            nonlocal_usage = UsageAcc()
            try:
                nonlocal_usage = parser.finalize()
            except Exception:
                pass
            record_id = 0
            try:
                record_id = await db.record(
                    platform=platform, model=out_payload.get("model"),
                    request_id=request_id, usage=nonlocal_usage, status_code=status,
                    error=error_msg if error_msg else (None if status < 400 else f"upstream_{status}"),
                    upstream=cfg.name, api_key_alias=None,
                    # v0.143：客户端入口 wire 透传 → 写 endpoint。
                    endpoint=client_wire,
                    agent=agent,
                    raw_ua=raw_ua,
                )
            except Exception as exc:
                log.exception("db.record failed in cross-wire stream: %s", exc)
            # v0.120：cross-wire SSE 路径也保存消息原文
            if record_id and settings.relay_save_messages:
                try:
                    user_text, user_json = _extract_last_user_message(out_body_bytes)
                    await db.record_messages(
                        record_id,
                        user_text=user_text,
                        user_json=user_json,
                        assistant_text=parser.assembled_text() or None,
                        assistant_json=None,  # SSE 没有 raw_response
                        thinking_text=parser.assembled_thinking() or None,
                    )
                except Exception as exc:
                    log.exception("db.record_messages failed in cross-wire stream: %s", exc)
            try:
                await _complete_inflight(inflight_id)
            except Exception as exc:
                log.exception("_complete_inflight failed in cross-wire stream: %s", exc)
            # v0.89：广播 done —— 侧栏定稿
            try:
                tool_json = parser.assembled_tool_use_json() or ""
                thinking_final = parser.assembled_thinking() or ""
                u = nonlocal_usage
                await _broadcast_live_event(
                    inflight_id, "done",
                    phase="done",
                    assistant_text=parser.assembled_text(),
                    thinking_text=thinking_final,
                    tool_use_json=tool_json,
                    usage_live={
                        "input_tokens": u.input_tokens,
                        "output_tokens": u.output_tokens,
                        "cache_read_input_tokens": u.cache_read_input_tokens,
                        "cache_creation_input_tokens": u.cache_creation_input_tokens,
                    },
                    error=error_msg,
                )
            except Exception:
                log.exception("broadcast done event failed in cross-wire stream")
            # v0.98 插件钩子：post_response（跨协议流式 finally 内）+
            # request.done 事件。插件错误已由 run_hooks / emit_event 隔离。
            try:
                rinfo = {
                    "platform": platform,
                    "cfg": cfg,
                    "model": model,
                    "status": status,
                    "usage": nonlocal_usage,
                    "error": error_msg if error_msg else (
                        None if status < 400 else f"upstream_{status}"
                    ),
                    "req_db_id": None,
                    "upstream": cfg.name,
                    "request_id": request_id,
                    "streaming": True,
                }
                await run_hooks("post_response", rinfo)
                emit_event("request.done", **rinfo)
            except Exception:
                log.exception("plugin post_response hooks failed in cross-wire stream")
        try:
            async for ev in convert_stream(
                sse_events(), upstream_wire, platform_wire,
            ):
                if platform_wire == _OAI:
                    # v0.12.1：linguafranca 转换出的 openai chunk 常带
                    # created:0（非标准）。部分 openai 兼容客户端（opencode
                    # 桌面版）对此显示异常 —— 补一个真实时间戳再回传。
                    try:
                        if isinstance(ev, dict):
                            if ev.get("created") == 0 or "created" not in ev:
                                ev["created"] = int(time.time())
                        elif hasattr(ev, "created") and not getattr(ev, "created", None):
                            ev.created = int(time.time())
                    except Exception:
                        pass
                elif platform_wire == _ANT:
                    # v0.88：Claude Code 走 cross-wire（anthropic 入口 → openai
                    # 上游）时，linguafranca 从 openai usage 转出的 message_delta/
                    # message_start 不含 cache 字段。上游（openai）已返回缓存命中
                    # 数（cached_tokens 等，parser 已统计），这里补写回 anthropic
                    # 的 cache_read_input_tokens，否则 Claude Code 的 Cache hit
                    # rate 恒为 0%。parser.usage 在 feed 时实时更新。
                    try:
                        if isinstance(ev, dict) and ev.get("type") in (
                            "message_start", "message_delta"
                        ):
                            usage = ev.get("usage")
                            if isinstance(usage, dict):
                                usage["cache_read_input_tokens"] = max(
                                    int(usage.get("cache_read_input_tokens", 0) or 0),
                                    parser.usage.cache_read_input_tokens,
                                )
                                usage["cache_creation_input_tokens"] = max(
                                    int(usage.get("cache_creation_input_tokens", 0) or 0),
                                    parser.usage.cache_creation_input_tokens,
                                )
                    except Exception:
                        pass
                yield serialize_sse_event(ev, platform_wire)
            # openai-chat 客户端需要 data: [DONE] 结束标记（linguafranca 不产出）。
            if platform_wire == _OAI:
                yield b"data: [DONE]\n\n"
        except WireConversionError as exc:
            error_msg = f"wire_convert: {exc}"
            log.warning("%s cross-wire stream convert failed: %s", platform, exc)
        except httpx.RemoteProtocolError as exc:
            error_msg = f"upstream_disconnect: {exc}"
            log.info("%s cross-wire disconnect: %s", platform, exc)
        except httpx.ReadError as exc:
            error_msg = f"upstream_read_error: {exc}"
            log.info("%s cross-wire read error: %s", platform, exc)
        except GeneratorExit:
            error_msg = "client_disconnect"
        except Exception as exc:  # noqa: BLE001
            error_msg = f"stream_error: {exc}"
            log.exception("%s cross-wire unexpected stream error", platform)
        finally:
            # v0.166：客户端中途断开时 starlette 会 cancel 本任务（spec 2.3
            # 走 listen_for_disconnect → cancel_scope.cancel()）。任务一旦被
            # 取消，finally 里**第一个 await 就会立即再次抛 CancelledError**
            # 打断 finally —— 导致 codex 等「收完即断」的客户端请求永远写不
            # 进 DB（usage 统计漏记）。把整段收尾逻辑 shield 起来，让记录在
            # 取消信号到达后仍能跑完。
            try:
                await asyncio.shield(_cross_wire_finalize())
            except asyncio.CancelledError:
                log.info("%s cross-wire finalize cancelled during teardown", platform)
            except Exception:
                log.exception("%s cross-wire finalize failed during teardown", platform)

    return StreamingResponse(
        gen(), status_code=status,
        media_type="text/event-stream",
    )


async def relay(
    request: Request,
    *,
    platform: str,
    parser_factory: Callable[[], _HasFeed],
    # v0.X 鲁棒性增强：路由层从路径末段推断的客户端 wire；None 时在下面
    # 按 body 嗅探 → platform 兜底的顺序确定。取代旧的 "platform 决定
    # client_wire" 的隐式行为 —— 修客户端 SDK 拼错前缀时仍能正确分发。
    client_wire: Optional[str] = None,
) -> Response:
    """Forward `request` to the platform's active upstream and stream back.

    `platform` is one of "anthropic" | "openai" — used for
    bookkeeping in the SQLite row and for indexing the per-platform config.
    """
    app_state = request.app.state
    settings = app_state.settings
    if (resp := await _check_auth(request, settings.relay_require_auth_token, platform)) is not None:
        return resp

    # v0.155/v0.156/v0.157：从 User-Agent 识别客户端工具。v0.157 起入口
    # 走 resolve_agent：先查用户手配的 ua_rules（整串 UA → 平台名，优先级
    # 最高），未命中再走 sniff_agent（白名单 → 动态抽 token → 兜底桶
    # "其它"）。raw_ua 原文落库供设置页列出真实 UA 让用户归类。agent 独立
    # 于 platform（入口协议），只用于展示归组，不参与路由 / 计费。
    raw_ua = request.headers.get("user-agent")
    agent = resolve_agent(raw_ua, settings.ua_rules)

    # Register this request in the in-flight tracker before doing anything
    # else, so the live panel can show it as soon as the client starts
    # uploading the body. The model isn't known until we read at least one
    # chunk, so we register with model=None and patch it below.
    inf = await _register_inflight(
        platform=platform, model=None, agent=agent, raw_ua=raw_ua,
    )

    emit_event(
        "request.started",
        platform=platform,
        path=request.url.path,
        method=request.method,
        client_host=request.client.host if request.client else None,
    )

    _tlog.debug(
        "REQ ENTER platform=%s method=%s path=%s query=%s client_host=%s headers=%s",
        platform, request.method, request.url.path, request.url.query,
        request.client.host if request.client else None,
        _redact_headers(dict(request.headers)),
    )

    # 1. Buffer the request body. For small bodies we read in one shot (no
    #    chunked-reading overhead). For bodies ≥ 64 KB we stream chunk by
    #    chunk so the live panel can show real upload progress + partial
    #    user_text preview.
    cl_raw = request.headers.get("content-length")
    cl_known = cl_raw and cl_raw.isdigit()
    cl_value = int(cl_raw) if cl_known else None
    if cl_value is not None and cl_value >= BODY_LARGE_THRESHOLD:
        await _update_inflight(inf.request_id, content_length=cl_value, phase="uploading")
        chunks: list[bytes] = []
        async for chunk in request.stream():
            chunks.append(chunk)
            total = sum(len(c) for c in chunks)
            # Cheap text preview every ~64 KB so the panel updates feel
            # responsive on multi-MB uploads without parsing every chunk.
            preview = ""
            if total % BODY_LARGE_THRESHOLD < len(chunk):
                preview = _preview_user_text(b"".join(chunks))
            await _update_inflight(
                inf.request_id,
                bytes_received=total,
                user_text_preview=preview,
            )
        body = b"".join(chunks)
    else:
        body = await request.body()
        await _update_inflight(
            inf.request_id,
            bytes_received=len(body),
            content_length=cl_value,
            user_text_preview=_preview_user_text(body),
            phase="uploading",
        )
    model = await _extract_model(body)
    _tlog.debug(
        "REQ BODY (%d bytes) platform=%s: %s",
        len(body), platform, _body_preview(body),
    )
    # v0.11.21：client_model 记客户端原始请求里的模型（对内），
    # 后续 cfg.model / advanced-switch 改写的是 model（对外）。
    await _update_inflight(inf.request_id, model=model, client_model=model)
    # v0.12：分发日志用 —— 记住客户端原始模型（对内），后面 model 会被改写。
    client_model = model

    # v0.X 鲁棒性增强：确定客户端实际 wire（三级兜底）。
    # 1) 路由层从路径末段推断（最准，见 _infer_client_wire_from_path）
    # 2) 路由层没传 → body 嗅探（_sniff_client_wire，看 system/input/role）
    # 3) 都失败 → platform 兜底（向后兼容）
    if client_wire is None:
        client_wire = _sniff_client_wire(body)
    if client_wire is None:
        client_wire = infer_wire_for_platform(platform)
    # parser 按 client_wire 选 —— plugin.py 已注册所有 KNOWN_WIRES 的默认
    # parser（Anthropic→AnthropicUsageParser，其余→OpenAIUsageParser）。
    # adapter 路径不受影响（它自己管 SSE 过滤）。
    _resolved = parser_factory_for(client_wire)
    if _resolved is not None:
        parser_factory = _resolved

    # 2. Build the outbound URL by stripping the platform prefix from the path.
    full_path = request.url.path
    prefix = f"/{platform}"
    if full_path.startswith(prefix):
        stripped = full_path[len(prefix):] or "/"
    else:
        # 根路径 router 调用（routers/root.py）—— path 不带 platform 前缀，
        # 直接当 stripped 用。不再 500，保持 relay() 在根路径场景可用。
        stripped = full_path or "/"

    # 3. v0.12 分发判定（docs/wire-dispatch-plan.md §2）：以客户端 key 为
    #    唯一可靠信号。model 字段不可靠 —— Claude Code 会把 ANTHROPIC_MODEL
    #    =auto 解析成具体模型名（如 claude-haiku-4-5-20251001）再发出，所以
    #    不能拿 model 是否 auto 做判定。规则：
    #      key=auto/空 → 中继转发（active 上游 + 中继定模型，忽略客户端模型）
    #      key=真值   → 透传（同平台段按 key 匹配上游；未命中 502）
    client_key = _client_auth_key(request, platform)
    key_auto = client_key == "" or client_key.strip().lower() == AUTH_AUTO_SENTINEL

    cfg: Optional[PlatformConfig]
    passthrough = False
    if key_auto:
        # 中继转发：active 上游，模型由中继按上游配置决定（见下方模型链）。
        cfg = settings.active_config(platform)
    else:
        # 透传：同平台段按归一化 key 匹配上游。
        uc, fallback_model = _match_upstream_by_key(
            settings, platform, client_key, model,
        )
        if uc is None:
            # ：识别到 @@ 透传标识符但中继处于转换模式 —— 专属文案，
            # 否则用户会误以为是 key 本身配错（实际是透传开关没开）。
            marker = "@@" in client_key
            return await _reject_dispatch(
                app_state, inf.request_id, platform, model,
                status_code=502,
                error_code=(
                    "relay_passthrough_marker_in_conversion"
                    if marker else "relay_unknown_key"
                ),
                message=(
                    "识别到透传标识符（@@），但当前中继处于转换模式，"
                    "若将此api当作标准api，则未发现匹配上游，请检查你的配置"
                    if marker else
                    "收到透传请求，但项目中未找到该配置组"
                    "（key 未能匹配该平台的任何上游）。"
                ),
                alert_kind="unknown_key",
                client_key=client_key,
                agent=agent,
                raw_ua=raw_ua,
            )
        cfg = uc
        passthrough = True
        if fallback_model:
            # 模型不在该上游列表 → 兜底该上游强映射 model 或 allowed[0]。
            # inflight 的 client_model/model 天然体现改写前后差异。
            body = _rewrite_model_in_body(body, fallback_model)
            model = fallback_model
            await _update_inflight(inf.request_id, model=model)

    upstream_url = _join_upstream_url(
        cfg.url, _normalize_api_path(platform, cfg.url, stripped), request.url.query,
    )

    # v0.65/v0.74/v0.8.1 模型链 —— 仅中继转发路径。透传路径跳过：
    # 客户端模型语义优先（命中时模型已在上面的兜底分支处理完）。
    # v0.12：中继转发路径下模型**永远由中继定**（key=auto 即"请中继定"，
    # 忽略客户端解析出的模型名）：cfg.model 强映射 → default_model →
    # allowed_models[0]。都没有才原样透传客户端模型。
    if not passthrough:
        target_model: Optional[str] = (
            cfg.model
            or cfg.default_model
            or (cfg.allowed_models[0] if cfg.allowed_models else None)
        )
        if target_model:
            body = _rewrite_model_in_body(body, target_model)
            model = await _extract_model(body)
            await _update_inflight(inf.request_id, model=model)

    # v0.11.18 高级切换：客户端没显式指定模型时，判定该请求走弱还是强
    # 模型，可跨上游换 cfg。决策结果直接改写 cfg / model / body，后续
    # adapter 检查与转发照常处理新的目标。
    if cfg.advanced_switch:
        try:
            from .advanced_switch import decide

            cfg, model, _route, _reason = await decide(
                settings=settings, cfg=cfg, body=body, model=model, platform=platform,
                ctx=getattr(app_state, "ctx", None),
            )
            log.info("advanced-switch decided: cfg=%s model=%s route=%s", cfg.name, model, _route)
            if model:
                body = _rewrite_model_in_body(body, model)
                await _update_inflight(inf.request_id, model=model)
            # cfg 可能已被切到强目标（跨上游），重新拼接上游 URL。
            upstream_url = _join_upstream_url(
                cfg.url, _normalize_api_path(platform, cfg.url, stripped), request.url.query,
            )
        except Exception as exc:
            log.exception("advanced-switch decide failed: %s", exc)

    # v0.11.21：记录对外实际目标 —— advanced-switch 后 cfg 是最终上游。
    # client_model（对内）已在注册时记下；这里补上游名 + 最终模型（对外）。
    # v0.12：cfg 已由分发判定确定（中继转发=active 上游；透传=key 命中
    # 的上游），标签直接用 cfg.name —— v0.11.22 的按模型名匹配/「平台
    # 透传（模型）」虚拟上游已废弃（docs/wire-dispatch-plan.md §2.4）。
    # v0.91：同时补 live-panel 三字段（入向 / 出向 wire + 出向 api_key）。
    # 必须放在分发判定 + advanced-switch 之后、路径分叉（cross_wire /
    # adapter）之前 —— 否则后续两条路径不再各自补 wire/api_key。api_key
    # 取值：cfg.api_key 优先（中继转发场景，override 后发的是 cfg 真 key）；
    # cfg.api_key 为空时（passthrough 直传场景）用 client_key —— 那是
    # 客户端发的真 key 中继原样转发给上游，正是侧栏该展示的。明文只经
    # _broadcast 推 pywebview 桥，HTTP /live 走 _mask_key。
    await _update_inflight(inf.request_id, upstream=cfg.name)
    # v0.X 鲁棒性增强：inbound_wire 用推断/嗅探的 client_wire，
    # 不再用 platform 硬编码 —— 跨 wire 请求时 live panel 正确显示协议。
    inbound_wire = client_wire
    effective_api_key = cfg.api_key or client_key
    await _update_inflight(
        inf.request_id,
        inbound_wire=inbound_wire,
        outbound_wire=cfg.effective_wire(platform),
        api_key=effective_api_key,
    )

    # v0.98 插件钩子：pre_dispatch —— 分发判定 + 模型链 + advanced-switch
    # 之后、DISPATCH 日志之前。插件可改 model / body（info 见 plugin.py
    # register_hook docstring；cfg 只读 —— upstream_url 已按 cfg 拼好）。
    pinfo: dict[str, Any] = {
        "platform": platform,
        "cfg": cfg,
        "model": model,
        "body": body,
        "client_model": client_model,
        "passthrough": passthrough,
    }
    await run_hooks("pre_dispatch", pinfo)
    if pinfo["body"] is not body:
        # 插件整体替换了 body —— 重新提取 model 同步 inflight。
        body = pinfo["body"]
        try:
            model = await _extract_model(body)
        except Exception as exc:
            log.debug("pre_dispatch body changed; model extract failed: %s", exc)
        await _update_inflight(inf.request_id, model=model)
    elif pinfo["model"] != model:
        # 插件只改了 model —— 用现成的请求体重写同步，保持 body 一致。
        model = pinfo["model"]
        try:
            body = _rewrite_model_in_body(body, model)
        except Exception as exc:
            log.warning("pre_dispatch model rewrite failed: %s", exc)
        await _update_inflight(inf.request_id, model=model)

    # v0.12 分发日志 —— 每个请求一行，便于 tail 日志监听路由去向：
    #   DISPATCH platform=<anthropic|openai> mode=<relay|passthrough>
    #            key=<脱敏> model=<对内>-><对外> upstream=<名> wire=<..> auth=<..>
    log.info(
        "DISPATCH platform=%s mode=%s key=%s model=%s->%s upstream=%s wire=%s auth=%s",
        platform,
        "passthrough" if passthrough else "relay",
        _mask_key(client_key),
        client_model,
        model,
        cfg.name,
        cfg.effective_wire(platform),
        cfg.effective_auth_style(platform),
    )
    _tlog.info(
        "DISPATCH DETAIL platform=%s upstream=%s url=%s target_model=%s wire=%s auth_style=%s "
        "req_body_preview=%s",
        platform, cfg.name, cfg.url, model, cfg.effective_wire(platform),
        cfg.effective_auth_style(platform), _body_preview(body, 1200),
    )

    # v0.11.20：对外思考重写 —— 解析客户端请求里已有的 thinking /
    # reasoningEffort（用户在平台选的挡位），按目标上游（cfg）的
    # thinking_options 能力转换/剥离。
    # v0.12：挪到跨协议分叉**之前**，让 cross-wire 路径也享受思考剥离 ——
    # 否则 Claude Code 的 alwaysThinking 会经 linguafranca 转成 openai 的
    # `reasoning` 字段，而 opencode.ai 等上游不收（400 Extra inputs）。
    try:
        new_body = _rewrite_thinking_for_upstream(body, cfg, platform)
        body = new_body
    except Exception as exc:
        log.warning("thinking-level rewrite failed: %s", exc)

    # v0.12 跨协议转换分叉（docs/wire-dispatch-plan.md §3.3/§4）：客户端入口
    # wire != 上游 wire 时走转换路径（恒发 url+endpoint，请求/响应双向翻译）。
    # 同 wire 走下面的字节直通 + adapter，行为与 v0.11 一致。
    # v0.X 鲁棒性增强：用 client_wire（路径推断 + body 嗅探）替代 platform
    # 硬编码 —— 客户端拼错前缀时（/anthropic 实际发 openai）能正确识别。
    platform_wire = client_wire
    if platform_wire != cfg.effective_wire(platform):
        return await _relay_cross_wire(
            request=request, cfg=cfg, inflight_id=inf.request_id,
            model=model, body=body, platform=platform, app_state=app_state,
            client_wire=client_wire,
            agent=agent,
            raw_ua=raw_ua,
        )

    # v0.8.1: dual-protocol provider (OpenCode Go) needs full protocol
    # adaptation (auth replace, model normalize, thinking strip). Bypass the
    # normal byte-forward streaming path entirely.
    if cfg.requires_anthropic_adapter:
        return await _anthropic_adapter_relay(
            request=request,
            cfg=cfg,
            inflight_id=inf.request_id,
            model=model,
            body=body,
            platform=platform,
            db=app_state.db,
            parser_factory=parser_factory,
            api_key_alias=None,  # auth already handled below
            # v0.143：客户端入口 wire 透传到 adapter 内部 record 站点。
            endpoint=client_wire,
            agent=agent,
            raw_ua=raw_ua,
        )

    # v0.66: 剥掉 cache_control.scope + thinking 块 —— 一次解析一次序列化
    # （分开做是两次 JSON 全量往返）。OpenCode Zen 上游不认 scope 字段，
    # Anthropic 签的 signature 跨上游也验不过。非 adapter 路径才需要：
    # adapter 路径（requires_anthropic_adapter）在前面已按协议整块重发。
    # v0.199.1：非 vision 模型剥图 + 提示词（判定基于 settings.vision_models +
    # 当前目标 model）。model 是目标上游改写后的模型（DISPATCH 用同一个值），
    # 与 cross-wire 路径的判定口径一致。
    # v0.200：并入上游条目内 vision_models（cfg.vision_models）—— 命中
    # 任一（上游名单 ∪ 全局兜底名单）才保留图，否则剥图 + 提示词。
    _vm = set(getattr(settings, "vision_models", None) or [])
    _vm |= set(getattr(cfg, "vision_models", None) or [])
    body = _strip_disallowed_content(
        body,
        vision_models=sorted(_vm),
        model=model,
    )

    # 4. Forward headers (hop-by-hop filtered). Optionally override the
    #    auth header with the configured api_key, or detect the auto sentinel.
    fwd_headers = filter_request_headers(dict(request.headers))
    api_key_alias, sentinel_error = _apply_auth_override(fwd_headers, cfg, platform)
    # v0.91：wire/api_key 已在分发判定 + advanced-switch 之后、路径分叉
    # 之前的统一位置（forward_request 内、_update_inflight(upstream=...)
    # 后立刻）补过，此处不再重复。
    if sentinel_error:
        log.warning(
            "%s client sent sentinel %r but upstream %r has no api_key",
            platform, AUTH_AUTO_SENTINEL, cfg.name,
        )
        # We registered an inflight entry back at the top of forward_request
        # but never wrote anything upstream — complete it here so the live
        # panel doesn't show a ghost row. Wrap defensively: a failure here
        # is the same kind of leak the sweeper exists to catch, so log and
        # let the next sweep tick mop up.
        try:
            await _complete_inflight(inf.request_id)
        except Exception as com_exc:
            log.exception("_complete_inflight failed in sentinel_error path: %s", com_exc)
        return Response(
            content=json.dumps({
                "error": "relay_no_upstream_key",
                "detail": (
                    f"client sent sentinel {AUTH_AUTO_SENTINEL!r} on "
                    f"platform {platform!r} but the active upstream "
                    f"{cfg.name!r} has no api_key configured"
                ),
            }).encode(),
            status_code=503,
            media_type="application/json",
        )
    # Body upload is finished; transition phase to "calling" so the live
    # panel shows the request is waiting on the upstream. We must NOT call
    # _complete_inflight here — that would move the entry to the "done"
    # list before streaming even starts, making every subsequent
    # _update_inflight / _set_inflight_phase a no-op (they look up the
    # entry in _in_flight which is now empty). The entry stays in
    # _in_flight through the entire response; _complete_inflight runs in
    # the streaming generator's finally block.
    await _set_inflight_phase(inf.request_id, "calling")

    # v0.98 插件钩子：pre_upstream —— 发送前最后一改。插件可整体替换
    # info["headers"]（增删请求头），其余字段只读（info 见 plugin.py）。
    pinfo = {
        "platform": platform,
        "cfg": cfg,
        "model": model,
        "body": body,
        "headers": fwd_headers,
        "upstream_url": upstream_url,
    }
    await run_hooks("pre_upstream", pinfo)
    fwd_headers = pinfo["headers"]

    # 5. Open the upstream stream.
    db: Database = app_state.db

    client = _get_client(upstream_url)
    _tlog.debug(
        "UPSTREAM REQ %s %s headers=%s", request.method, upstream_url, _redact_headers(fwd_headers),
    )
    _tlog.debug("UPSTREAM REQ BODY (%d bytes): %s", len(body), _body_preview(body))
    try:
        upstream_req = client.build_request(
            method=request.method,
            url=upstream_url,
            headers=fwd_headers,
            content=body,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        log.warning("%s upstream connect failed: %s", platform, exc)
        req_db_id = 0
        try:
            req_db_id = await db.record(
                platform=platform,
                model=model,
                request_id=None,
                usage=UsageAcc(),
                status_code=0,
                error=f"upstream_error: {exc}",
                upstream=cfg.name,
                api_key_alias=api_key_alias,
                # v0.143：客户端入口 wire 透传 → 写 endpoint。
                endpoint=client_wire,
                agent=agent,
                raw_ua=raw_ua,
            )
        except Exception as rec_exc:
            log.exception("db.record failed in upstream_error path: %s", rec_exc)
        # v0.120：upstream_error 路径也保存用户请求（无回复）
        if req_db_id and settings.relay_save_messages:
            try:
                user_text, user_json = _extract_last_user_message(body)
                await db.record_messages(
                    req_db_id,
                    user_text=user_text,
                    user_json=user_json,
                    assistant_text=None,
                    assistant_json=None,
                )
            except Exception as exc:
                log.exception("db.record_messages failed in upstream_error path: %s", exc)
        try:
            await _complete_inflight(inf.request_id)
        except Exception as com_exc:
            log.exception("_complete_inflight failed in upstream_error path: %s", com_exc)
        return Response(
            content=json.dumps(_error_body(platform, str(exc))).encode(),
            status_code=502,
            media_type="application/json",
        )

    status = upstream_resp.status_code
    request_id = upstream_resp.headers.get("request-id") or upstream_resp.headers.get(
        "x-request-id"
    )
    response_headers = filter_response_headers(dict(upstream_resp.headers))
    _tlog.info(
        "UPSTREAM RESP status=%s headers=%s",
        status, _redact_headers(dict(upstream_resp.headers)),
    )
    # Body is fully uploaded by now; phase moves to "calling" until the
    # first response chunk arrives.
    await _set_inflight_phase(inf.request_id, "calling")

    content_type = upstream_resp.headers.get("content-type", "")
    is_sse = "text/event-stream" in content_type

    # 6a. Non-streaming fast path — read the whole body, extract usage from JSON,
    #     return as a normal Response.
    if not is_sse:
        try:
            content = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()
        parser = parser_factory()
        # Try JSON extraction first (covers `stream: false` and minnimax.chat's
        # default behavior); fall back to feeding as SSE bytes in case a
        # misconfigured upstream returned SSE with the wrong content-type.
        usage = parser.extract_from_json(content)
        if usage.input_tokens == 0 and usage.output_tokens == 0:
            parser.feed(content)
            usage = parser.finalize()
        # Same defensive layering as the streaming path: each step guarded,
        # _complete_inflight always runs last. A SQLite blip must never
        # leak the entry into _in_flight forever.
        req_db_id = 0
        try:
            req_db_id = await db.record(
                platform=platform,
                model=model,
                request_id=request_id,
                usage=usage,
                status_code=status,
                error=None if status < 400 else f"upstream_{status}",
                upstream=cfg.name,
                api_key_alias=api_key_alias,
                # v0.143：客户端入口 wire 透传 → 写 endpoint。
                endpoint=client_wire,
                agent=agent,
                raw_ua=raw_ua,
            )
        except Exception as exc:
            log.exception("db.record failed for %s: %s", inf.request_id, exc)
        try:
            await _update_inflight(
                inf.request_id,
                assistant_text=parser.assembled_text() or "",
            )
        except Exception as exc:
            log.exception("_update_inflight (non-stream) failed for %s: %s", inf.request_id, exc)
        if req_db_id and settings.relay_save_messages:
            try:
                user_text, user_json = _extract_last_user_message(body)
                await db.record_messages(
                    req_db_id,
                    user_text=user_text,
                    user_json=user_json,
                    assistant_text=parser.assembled_text() or None,
                    assistant_json=parser.raw_response.decode("utf-8", "replace")
                    if parser.raw_response
                    else None,
                    thinking_text=parser.assembled_thinking() or None,
                )
            except Exception as exc:
                log.exception("db.record_messages failed for %s: %s", inf.request_id, exc)
        try:
            await _complete_inflight(inf.request_id)
        except Exception as exc:
            log.exception("_complete_inflight failed for %s: %s", inf.request_id, exc)
        # v0.98 插件钩子：post_response（非流式）+ request.done 事件。
        rinfo: dict[str, Any] = {
            "platform": platform,
            "cfg": cfg,
            "model": model,
            "status": status,
            "usage": usage,
            "error": None if status < 400 else f"upstream_{status}",
            "req_db_id": req_db_id,
            "upstream": cfg.name,
            "request_id": request_id,
            "streaming": False,
        }
        await run_hooks("post_response", rinfo)
        emit_event("request.done", **rinfo)
        return Response(
            content=content,
            status_code=status,
            headers=response_headers,
            media_type=content_type or None,
        )

    # 6b. Streaming teeing path — generator yields chunks; each chunk is also
    #     fed to the parser. Finally block writes the row on completion.
    parser = parser_factory()
    error_msg: Optional[str] = None

    async def stream_iter() -> "AsyncIterator[bytes]":
        nonlocal error_msg
        await _set_inflight_phase(inf.request_id, "streaming")
        # v0.166：与 cross-wire 的 gen() 同款修复 —— 客户端中途断开时
        # starlette 会 cancel 本任务，finally 内第一个 await（aclose /
        # db.record）会被再次 CancelledError 打断，导致直连路径下「收完
        # 即断」的客户端（codex 等）请求漏记 DB。收尾逻辑抽成独立闭包，
        # finally 用 asyncio.shield 包裹，让记录在取消信号到达后仍能跑完。
        async def _direct_finalize() -> None:
            try:
                await upstream_resp.aclose()
            except Exception:
                pass
            _tlog.info(
                "STREAM END upstream=%s status=%s chunks=%d total_bytes=%d error=%s",
                cfg.name, status, _chunk_no, _total_bytes, error_msg,
            )
            # Finalize parser and write the row. This MUST run for every
            # request, but each step is independently guarded — a SQLite
            # blip on db.record / db.record_messages must NOT prevent
            # _complete_inflight from running, otherwise the entry gets
            # stuck in _in_flight forever and the "实时" panel shows a
            # ghost STREAMING row that never disappears.
            try:
                fin_usage = parser.finalize()
            except Exception as exc:
                log.warning("parser finalize error: %s", exc)
                fin_usage = UsageAcc()
            req_db_id = 0
            try:
                req_db_id = await db.record(
                    platform=platform,
                    model=model,
                    request_id=request_id,
                    usage=fin_usage,
                    status_code=status,
                    error=error_msg if error_msg else (None if status < 400 else f"upstream_{status}"),
                    upstream=cfg.name,
                    api_key_alias=api_key_alias,
                    # v0.143：客户端入口 wire 透传 → 写 endpoint。
                    endpoint=client_wire,
                    agent=agent,
                    raw_ua=raw_ua,
                )
            except Exception as exc:
                log.exception("db.record failed for %s: %s", inf.request_id, exc)
            if req_db_id and settings.relay_save_messages:
                try:
                    user_text, user_json = _extract_last_user_message(body)
                    await db.record_messages(
                        req_db_id,
                        user_text=user_text,
                        user_json=user_json,
                        assistant_text=parser.assembled_text() or None,
                        # Tool-use blocks get synthesised into a synthetic
                        # assistant message JSON so the GUI dialog can show
                        # what tools the model called. None for pure-text
                        # turns — no need to clutter the row with empty JSON.
                        assistant_json=parser.assembled_tool_use_json(),
                        # v0.89: extended-thinking 正文以独立消息行落盘
                        # （role='thinking'），由对话详情弹窗按 role 渲染。
                        # 解析器只在 Anthropic 路径上累积；OpenAI 路径
                        # 的 stub 永远返回 None —— record_messages 已经处理
                        # 空值跳过，这里传 None 即可。
                        thinking_text=parser.assembled_thinking(),
                    )
                except Exception as exc:
                    log.exception("db.record_messages failed for %s: %s", inf.request_id, exc)
            # _complete_inflight is the LAST step — by here we've already
            # given up on persistent state, so wrap defensively and let the
            # sweeper pick up anything that escapes even this.
            try:
                await _complete_inflight(inf.request_id)
            except Exception as exc:
                log.exception("_complete_inflight failed for %s: %s", inf.request_id, exc)
            # v0.89：广播 done —— 侧栏定稿。tool_use_json 此时已累完。
            try:
                tool_json = parser.assembled_tool_use_json() or ""
                u = fin_usage
                await _broadcast_live_event(
                    inf.request_id, "done",
                    phase="done",
                    assistant_text=parser.assembled_text(),
                    thinking_text=parser.assembled_thinking() or "",
                    tool_use_json=tool_json,
                    usage_live={
                        "input_tokens": u.input_tokens,
                        "output_tokens": u.output_tokens,
                        "cache_read_input_tokens": u.cache_read_input_tokens,
                        "cache_creation_input_tokens": u.cache_creation_input_tokens,
                    },
                    error=error_msg,
                )
            except Exception:
                log.exception("broadcast done event failed in stream_iter")
            # v0.98 插件钩子：post_response（流式，流结束 finally 内）+
            # request.done 事件。插件错误已由 run_hooks / emit_event 隔离。
            try:
                rinfo = {
                    "platform": platform,
                    "cfg": cfg,
                    "model": model,
                    "status": status,
                    "usage": fin_usage,
                    "error": error_msg,
                    "req_db_id": req_db_id,
                    "upstream": cfg.name,
                    "request_id": request_id,
                    "streaming": True,
                }
                await run_hooks("post_response", rinfo)
                emit_event("request.done", **rinfo)
            except Exception:
                log.exception("plugin post_response hooks failed in stream_iter")
        _chunk_no = 0
        _total_bytes = 0
        _first_sse_logged = 0
        try:
            async for chunk in upstream_resp.aiter_bytes():
                _chunk_no += 1
                _total_bytes += len(chunk)
                if _chunk_no <= 10 or _chunk_no % 50 == 0:
                    _tlog.debug(
                        "STREAM CHUNK #%d (%d bytes) sse=%s",
                        _chunk_no, len(chunk), _sse_preview(chunk),
                    )
                # 先喂给 parser，客户端中途断连也能留住 usage。
                try:
                    parser.feed(chunk)
                except Exception as exc:  # parser 的 bug 绝不能弄死流
                    log.debug("parser feed error: %s", exc)
                # 把实时助手文本推给 in-flight 追踪器，GUI / TUI 实时面板
                # 才能流式显示。在 yield 之前做完，让 chunk 和文本更新落在
                # 同一次 poll 里。
                # v0.89：顺手补 thinking / usage / 广播，供实时流侧栏实时
                # 跳动（侧栏面板要 token 四字段 + thinking 实时进）。
                assistant_now = parser.assembled_text() or ""
                thinking_now = parser.assembled_thinking() or ""
                u = parser.usage
                # v0.113x：output_tokens 估算（字符长度粗估），仅供侧栏实时
                # 跳动显示。db.record 在 finally 用 parser.finalize() 拿真值，
                # 不受估算影响。
                output_est = _estimate_output_tokens(
                    (assistant_now or "") + (thinking_now or "")
                )
                usage_now = {
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "output_tokens_est": max(u.output_tokens, output_est),
                    "cache_read_input_tokens": u.cache_read_input_tokens,
                    "cache_creation_input_tokens": u.cache_creation_input_tokens,
                }
                await _update_inflight(
                    inf.request_id,
                    assistant_text=assistant_now,
                    thinking_text=thinking_now,
                    usage_live=usage_now,
                )
                try:
                    await _broadcast_live_event(
                        inf.request_id, "delta",
                        assistant_text=assistant_now,
                        thinking_text=thinking_now,
                        usage_live=usage_now,
                    )
                except Exception:
                    log.exception("stream_iter live broadcast failed")
                # Mark this chunk has just arrived so the quiet-detector
                # can distinguish a live stream from one whose upstream
                # went silent mid-flight.
                await _bump_chunk_activity(inf.request_id)
                # v0.12.1：openai 直通流做 created 归一化 —— minnimax 等
                # 上游把 created 发成 0，opencode 桌面版等 openai 兼容
                # 客户端会显示异常（内容在但界面空白）。字节级替换成本低，
                # 且 created 字段总在每条 SSE data: 行内自包含，跨 chunk
                # 切开的概率可忽略。
                if platform == "openai" and _OPENAI_CREATED_ZERO_RE.search(chunk):
                    stamp = str(int(time.time())).encode()
                    chunk = _OPENAI_CREATED_ZERO_RE.sub(b'"created": ' + stamp, chunk)
                yield chunk
                # v0.87：Anthropic 流在收到 message_stop 后主动结束，不等上游
                # 关闭连接。部分上游（opencode.ai/zen/go）message_stop 后连接
                # 不关闭，若不提前 break，phase=streaming 会挂到 sweeper 90s
                # 强清 —— 实时面板出现"流已结束还在 STREAMING"的幽灵行。
                # 注意：chunk 可能跨事件边界，必须靠 parser 的逐帧状态判断，
                # 不能直接搜 chunk 字节里的 message_stop。
                if (platform == "anthropic" and getattr(parser, "message_stop_seen", False)):
                    break
        except httpx.RemoteProtocolError as exc:
            error_msg = f"upstream_disconnect: {exc}"
            log.info("%s upstream disconnect: %s", platform, exc)
        except httpx.ReadError as exc:
            error_msg = f"upstream_read_error: {exc}"
            log.info("%s read error: %s", platform, exc)
        except httpx.ReadTimeout as exc:
            error_msg = f"upstream_timeout: {exc}"
            log.info("%s timeout: %s", platform, exc)
        except GeneratorExit:
            # Client disconnected. The generator is being closed.
            error_msg = "client_disconnect"
        except Exception as exc:  # noqa: BLE001 - log & continue
            error_msg = f"stream_error: {exc}"
            log.exception("%s unexpected stream error", platform)
        finally:
            # v0.166：与 cross-wire 的 gen() 同款 shield 修复 —— 客户端断开
            # 时任务被 cancel，finally 内 await 会被再次 CancelledError 打断，
            # 收尾逻辑抽到 _direct_finalize() 里 shield 保护，DB 记录必达。
            try:
                await asyncio.shield(_direct_finalize())
            except asyncio.CancelledError:
                log.info("%s direct-stream finalize cancelled during teardown", platform)
            except Exception:
                log.exception("%s direct-stream finalize failed during teardown", platform)
    return StreamingResponse(
        stream_iter(),
        status_code=status,
        headers=response_headers,
        media_type=content_type or "text/event-stream",
    )
