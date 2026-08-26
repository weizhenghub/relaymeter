"""Pure ASGI middleware implementing passthrough mode.

Why pure ASGI (not BaseHTTPMiddleware, not a catch-all router):

- ``BaseHTTPMiddleware`` buffers the full response body, breaking SSE.
- A catch-all FastAPI router steals admin paths (``/api/*`` etc.) — and
  we need the admin API reachable even when passthrough is ON so the user
  can flip the switch back off.

So we sit at the outermost ASGI layer, **conditionally** handle the
request ourselves when passthrough mode is enabled, and pass through to
the inner Starlette app otherwise. When OFF the overhead is one bool
read + one ``await`` — no body buffering.

Body handling: we read the full request body in one shot, since the
upstream ``url`` is in the header (not derivable from path) and we
need it before constructing the outbound request. Bodies are typically
a few KB for chat traffic; if a real-world deployment sees multi-MB
bodies, swap to chunked reads — but keep the URL-parsing-first design.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI

from ..headers import filter_request_headers
from .fingerprint import (
    AUTH_HEADER_CANDIDATES,
    deep_search_usage,
    extract_model_field,
    parse_passthrough_auth,
    strip_auth_scheme,
    usage_from_sse_buffer,
)


# Platform prefixes the relay mounts under (see main.py include_router).
# The ASGI middleware sees the full client path in scope["path"]; the part
# stripped by the mount lives in scope["root_path"]. We re-join the
# remainder onto the user-supplied upstream URL so that
# ``ANTHROPIC_BASE_URL=http://127.0.0.1:8088/anthropic`` + Claude Code SDK
# which appends ``/v1/messages`` works the same as in conversion mode where
# proxy._anthropic_messages_url() does the same joining.
_PLATFORM_PREFIXES: tuple[str, ...] = ("/anthropic", "/openai")


log = logging.getLogger("relay.passthrough")


# Paths the middleware NEVER intercepts, even when passthrough mode is ON.
# These are the control plane (toggle mode off, view stats, debug) so the
# user can always recover from a bad config.
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/",          # control API (mode toggle, upstreams list)
    "/stats",         # stats aggregation endpoints
    "/healthz",       # liveness probe
    "/models",        # OpenCode models endpoint
    "/live",          # live streaming panel
    "/messages",      # message search
    "/requests",      # request list
    "/favicon.ico",   # browser favicon (GET, harmless)
    "/openapi.json",  # FastAPI schema
    "/docs",          # FastAPI swagger UI
    "/redoc",         # FastAPI redoc
)


_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)


# Shared client pool keyed by proxy mode (None = direct). Reusing keeps
# TLS session + connection pool warm across requests. Same shape as
# proxy._client_pool — kept independent so a buggy teardown here can't
# disturb the conversion path.
_client_pool: dict[Optional[str], tuple[type, httpx.AsyncClient]] = {}


def _system_proxy_url(url: str) -> Optional[str]:
    """Reuse the system-proxy decision from proxy.py — kept inline so
    passthrough stays self-contained. Localhost bypasses the proxy.
    """
    import socket
    import urllib.request

    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        host = ""
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return None
    proxies = urllib.request.getproxies()
    candidate = proxies.get("https") or proxies.get("http")
    return candidate


def _get_client(upstream_url: str) -> httpx.AsyncClient:
    key = _system_proxy_url(upstream_url)
    entry = _client_pool.get(key)
    if entry is not None and entry[0] is httpx.AsyncClient:
        return entry[1]
    client = httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False, proxy=key)
    _client_pool[key] = (httpx.AsyncClient, client)
    return client


async def _close_clients() -> None:
    for _, client in _client_pool.values():
        try:
            await client.aclose()
        except Exception:
            pass
    _client_pool.clear()


class PassthroughMiddleware:
    """Pure ASGI middleware.

    Args:
        app: the inner ASGI application (the Starlette/FastAPI router stack).
        fastapi_app: the FastAPI app instance — we read
            ``fastapi_app.state.settings`` and ``fastapi_app.state.pt_db``
            at request time so runtime toggles of ``passthrough_mode``
            take effect without re-creating the middleware.
    """

    def __init__(self, app, fastapi_app: FastAPI) -> None:
        self.app = app
        self.fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send) -> None:
        # Lifespan / websocket: defer to inner app.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Settings / db not ready yet (lifespan pre-yield) — pass through.
        try:
            settings = self.fastapi_app.state.settings
            pt_db = self.fastapi_app.state.pt_db
        except AttributeError:
            await self.app(scope, receive, send)
            return

        if not getattr(settings, "passthrough_mode", False):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        await self._handle(scope, receive, send, pt_db)

    # ------------------------------------------------------------------
    # The actual passthrough handler
    # ------------------------------------------------------------------

    async def _handle(self, scope, receive, send, pt_db) -> None:
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers") or []

        # 1. Read request body.
        body = await _read_body(receive)

        # 2. Parse auth header (case-insensitive scan for x-api-key / Authorization).
        header_dict = _decode_headers(raw_headers)
        auth_name, auth_value = _find_auth_header(header_dict)
        if not auth_value:
            await _send_json(send, 400, {"error": "缺少鉴权头（需要 x-api-key 或 Authorization）"})
            return
        try:
            stripped = strip_auth_scheme(auth_value, auth_name)
            upstream_url_raw, api_key = parse_passthrough_auth(stripped)
        except ValueError as exc:
            await _send_json(send, 400, {"error": str(exc)})
            return

        # 2b. Re-join the client's path suffix onto the upstream URL so
        # base URLs like ``https://api.deepseek.com/anthropic`` (what
        # Claude Code writes in its ANTHROPIC_BASE_URL) work the same as
        # in conversion mode. The platform mount prefix (``/anthropic``,
        # ``/openai``) is stripped before joining.
        upstream_url = _join_client_path(upstream_url_raw, root_path, path)
        # 保留 query string —— minnimax 中转站等上游依赖
        # ``?beta=true`` 参数（转换模式转发带 query 才能 200）。
        qs = scope.get("query_string") or b""
        if qs:
            upstream_url += "?" + qs.decode("latin-1")

        # 3. Extract model field from body JSON.
        model_value, model_field_name = extract_model_field(body)

        # 4. Build outbound headers: drop hop-by-hop, swap auth value.
        outbound_headers = filter_request_headers(header_dict)
        outbound_headers[auth_name] = _format_auth_value(auth_name, api_key)

        # 5. Forward.
        started = time.monotonic()
        error: Optional[str] = None
        status_code: Optional[int] = None
        response_buf = bytearray()

        try:
            client = _get_client(upstream_url)
            async with client.stream(
                method, upstream_url, content=body, headers=outbound_headers,
            ) as resp:
                status_code = resp.status_code
                # Forward response headers + status verbatim.
                resp_headers = _filter_response_headers(resp.headers.items())
                await send(
                    {
                        "type": "http.response.start",
                        "status": status_code,
                        "headers": _to_asgi_headers(resp_headers),
                    }
                )
                async for chunk in resp.aiter_bytes():
                    response_buf.extend(chunk)
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except httpx.RequestError as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.warning("passthrough forward failed url=%s err=%s", upstream_url, error)
            # If we already started a response we can't change status;
            # surface as a streaming error trailer so the client sees it.
            if status_code is None:
                await _send_json(send, 502, {"error": f"上游连接失败: {exc}"})
            status_code = status_code if status_code is not None else 502
        except Exception as exc:  # pragma: no cover — defensive
            error = f"{type(exc).__name__}: {exc}"
            log.exception("passthrough unexpected error url=%s", upstream_url)
            if status_code is None:
                await _send_json(send, 502, {"error": f"中继内部错误: {exc}"})

        # 6. Parse usage from response buffer (SSE-aware + full-buffer fallback).
        usage = _extract_usage(response_buf)

        # 7. Record upstream (auto-discover) + request row.
        # Fingerprint uses the RAW user-supplied URL (before path joining)
        # so two clients with the same ``url@@key`` config but different
        # mount paths (``/anthropic`` vs ``/openai``) are still recognized
        # as the same upstream.
        try:
            await pt_db.upsert_upstream(
                url=upstream_url_raw,
                api_key_alias=api_key,
                model_field_name=model_field_name,
            )
            await pt_db.record(
                url=upstream_url_raw,
                api_key_alias=api_key,
                model_field_name=model_field_name,
                model=model_value,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
                status_code=status_code,
                error=error,
                request_method=method,
                request_path=path,
            )
        except Exception as exc:  # pragma: no cover — DB failure must not crash request
            log.warning("passthrough DB record failed: %s", exc)

        log.info(
            "passthrough %s %s -> %s status=%s model=%s in=%d out=%d ms=%.0f",
            method, path, upstream_url, status_code, model_value,
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            (time.monotonic() - started) * 1000,
        )


# ---------------------------------------------------------------------------
# ASGI helpers — kept private to this module
# ---------------------------------------------------------------------------


async def _read_body(receive) -> bytes:
    """Drain the ASGI receive channel into bytes."""
    chunks: list[bytes] = []
    more = True
    while more:
        msg = await receive()
        if msg.get("type") != "http.request":
            continue
        chunks.append(msg.get("body", b"") or b"")
        more = msg.get("more_body", False)
    return b"".join(chunks)


def _join_client_path(upstream_url: str, root_path: str, full_path: str) -> str:
    """Append the client's request path (minus the relay's platform mount
    prefix) onto the user-supplied upstream URL.

    This mirrors what ``proxy._anthropic_messages_url()`` does for the
    conversion path: a base URL of ``https://api.deepseek.com/anthropic``
    plus Claude Code's SDK-append of ``/v1/messages`` becomes
    ``https://api.deepseek.com/anthropic/v1/messages``.

    Three rules keep the joining safe:
      1. If ``upstream_url`` already ends with the same path tail the
         client is asking for — or the client's path is a strict
         sub-path under ``upstream_url`` — return as-is. This catches
         two flavours of double-append:
           - ``.../v1/messages`` + suffix ``/v1/messages`` →
             avoids ``.../v1/messages/v1/messages``
           - ``.../anthropic/v1/messages`` + suffix
             ``/v1/messages/count_tokens`` → the client is asking for
             an endpoint under a path the upstream URL already names;
             we trust the user-supplied URL and don't re-append.
      2. If ``upstream_url`` ends with ``/v1``, only append the part of
         the suffix after ``/v1`` (avoids ``/v1/v1/messages`` when the
         upstream URL is e.g. ``https://opencode.ai/zen/go/v1`` and the
         client asks for ``/v1/...``).
      3. Otherwise append the full path-after-mount verbatim.

    ``root_path`` is the platform prefix the relay was mounted under
    (``/anthropic`` or ``/openai``); it's stripped before joining so we
    don't end up with paths like ``.../anthropic/anthropic/v1/messages``.
    When ``root_path`` is empty (no mount), the full path is used.
    """
    base = upstream_url.rstrip("/")
    suffix = full_path
    if root_path and (suffix == root_path or suffix.startswith(root_path + "/")):
        suffix = suffix[len(root_path):]
    elif not root_path:
        # ASGI 外层中间件拿到的 scope["root_path"] 恒为空 —— mount 的
        # 前缀（/anthropic /openai）只在内层路由 scope 里可见。这里显式
        # 按平台前缀剥离，否则上游 URL 会拼成 /anthropic/v1/messages。
        for _pref in _PLATFORM_PREFIXES:
            if suffix == _pref or suffix.startswith(_pref + "/"):
                suffix = suffix[len(_pref):]
                break
    if not suffix or suffix == "/":
        return base
    # Compare only the path portion of the upstream URL so ``https://`` /
    # ``http://`` / userinfo don't trip up path-prefix matching.
    base_path = urlsplit(base).path or ""

    # Rule 1: avoid double-append. Look for the longest suffix of
    # base_path that is also a prefix of suffix (scan all positions,
    # not just '/' boundaries — the overlap can land in the middle of
    # a segment). As soon as one matches, the upstream URL already
    # names that endpoint prefix — we only need to add the leftover
    # tail of the suffix.
    #
    # Examples (base_path → suffix → final):
    #   /v1/messages            + /v1/messages            → as-is
    #     (overlap "/v1/messages" equals both → leftover "")
    #   /anthropic/v1/messages  + /v1/messages            → as-is
    #     (overlap "/v1/messages" — base tail equals full suffix)
    #   /anthropic/v1/messages  + /v1/messages/count_tokens
    #     → /anthropic/v1/messages + /count_tokens
    #     (overlap "/v1/messages" → leftover "/count_tokens")
    #   /api.deepseek.com/openai + /v1/chat/completions    → Rule 2
    #   /opencode/zen/go/v1     + /v1/messages            → Rule 2
    for i in range(len(base_path), 0, -1):
        tail = base_path[i:]
        if not tail:
            continue
        if suffix == tail or suffix.startswith(tail):
            return base + suffix[len(tail):]

    # Rule 2: upstream path ends with /v1 and suffix starts with /v1 →
    # strip the duplicate /v1 (opencode.ai/zen/go/v1 case, when there's
    # no segment-aligned overlap from Rule 1).
    if base_path.endswith("/v1") and suffix.startswith("/v1"):
        return base + suffix[len("/v1"):]
    return base + suffix


def _decode_headers(raw: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    """ASGI headers are list[tuple[bytes, bytes]]; convert to case-insensitive dict."""
    out: dict[str, str] = {}
    for k, v in raw:
        try:
            ks = k.decode("latin-1").lower()
        except Exception:
            continue
        try:
            vs = v.decode("latin-1")
        except Exception:
            vs = ""
        # First wins on duplicates (matching HTTP semantics).
        out.setdefault(ks, vs)
    return out


def _find_auth_header(headers: dict[str, str]) -> tuple[str, str]:
    """Return (header_name, header_value) for the first matching auth header.

    Lookup order: x-api-key, then authorization. Returns ("", "") when
    none is present. Header name is returned in the original lowercase
    form (the outbound side uses it verbatim so the upstream matches).
    """
    for name in AUTH_HEADER_CANDIDATES:
        v = headers.get(name)
        if v:
            return name, v
    return "", ""


def _format_auth_value(header_name: str, api_key: str) -> str:
    """Re-attach the ``Bearer `` prefix for Authorization; raw for x-api-key."""
    if header_name == "authorization":
        return f"Bearer {api_key}"
    return api_key


_RESPONSE_STRIP = frozenset({"content-encoding", "transfer-encoding", "connection"})


def _filter_response_headers(items) -> list[tuple[str, str]]:
    """Strip hop-by-hop + content-encoding so the client gets plain bytes.

    httpx transparently decompresses; passing content-encoding through
    would make the client try to gunzip a body that's already plain.
    """
    out: list[tuple[str, str]] = []
    for k, v in items:
        kl = k.lower() if isinstance(k, str) else k.decode("latin-1").lower()
        if kl in _RESPONSE_STRIP:
            continue
        ks = k if isinstance(k, str) else k.decode("latin-1")
        vs = v if isinstance(v, str) else v.decode("latin-1")
        out.append((ks, vs))
    return out


def _to_asgi_headers(items: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    """Convert (str, str) headers to ASGI's (bytes, bytes)."""
    return [(k.encode("latin-1"), v.encode("latin-1")) for k, v in items]


async def _send_json(send, status: int, payload: dict) -> None:
    """Convenience: send a JSON error response and close the stream."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _extract_usage(buffer: bytearray) -> dict[str, int]:
    """Pick the best usage extraction based on response content type.

    - SSE (text/event-stream): parse each ``data:`` line.
    - JSON: deep_search_usage on the parsed body.
    - Anything else: deep_search_usage on the raw text.
    """
    if not buffer:
        return {}
    head = bytes(buffer[:200]).lower()
    if b"event-stream" in head or b"data:" in bytes(buffer[:4096]).lower():
        return usage_from_sse_buffer(bytes(buffer))
    try:
        text = bytes(buffer).decode("utf-8", errors="replace")
        obj = json.loads(text)
        return deep_search_usage(obj)
    except json.JSONDecodeError:
        return deep_search_usage(text)