"""Passthrough mode: forward requests verbatim with independent metering.

Public surface used by the rest of the relay:

- :class:`PassthroughDatabase` -- the bookkeeping DB
- :class:`PassthroughMiddleware` -- the ASGI middleware that does the
  actual forwarding when ``settings.passthrough_mode`` is True

Everything else in this package is internal implementation detail
(``fingerprint``, ``models``, ``usage_parser``).
"""

from .db import PassthroughDatabase
from .fingerprint import (
    AUTH_HEADER_CANDIDATES,
    deep_search_usage,
    extract_model_field,
    parse_passthrough_auth,
    strip_auth_scheme,
    usage_from_sse_buffer,
)
from .middleware import PassthroughMiddleware, _close_clients
from .models import (
    MODEL_FIELD_CANDIDATES,
    PassthroughStatsRow,
    PassthroughUpstream,
)


__all__ = [
    "AUTH_HEADER_CANDIDATES",
    "MODEL_FIELD_CANDIDATES",
    "PassthroughDatabase",
    "PassthroughMiddleware",
    "PassthroughStatsRow",
    "PassthroughUpstream",
    "_close_clients",
    "deep_search_usage",
    "extract_model_field",
    "parse_passthrough_auth",
    "strip_auth_scheme",
    "usage_from_sse_buffer",
]