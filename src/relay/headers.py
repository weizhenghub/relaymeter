"""HTTP header rewriting for the relay.

Forwarding rules:
- Strip hop-by-hop headers (RFC 7230 §6.1) so httpx can rebuild them for the
  new upstream connection.
- Preserve authentication, API versioning, and any custom headers the platform
  sends. The relay is transparent — it must not rewrite request semantics.
"""

from __future__ import annotations

from typing import Mapping


# Headers that must not be forwarded verbatim (case-insensitive). httpx will
# recompute Host and Content-Length; the rest are connection-scoped.
HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def filter_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `headers` with hop-by-hop entries removed.

    Header names are stored lowercase; httpx will set proper case on the wire.
    """
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


# Response-side headers that must never reach the client. The relay's upstream
# client (httpx) sends Accept-Encoding and transparently decompresses the
# body; forwarding content-encoding/gzip to the client would make it try to
# gunzip an already-plain body (DecodingError: incorrect header check).
_RESPONSE_STRIP: frozenset[str] = frozenset({"content-encoding"})


def filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Response-side filter: strip hop-by-hop + content-encoding but keep
    content-type etc. (the relay always forwards decompressed bodies)."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() not in _RESPONSE_STRIP
    }