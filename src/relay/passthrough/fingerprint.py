"""Auth-header parsing, model extraction, and deep usage search.

Three pure functions used by the passthrough middleware and the HTTP
layer. No I/O, no DB — all easy to unit-test.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .models import MODEL_FIELD_CANDIDATES, _USAGE_KEY_ALIASES


# Header names we scan for ``url@@api-key``. Case-insensitive lookup is the
# caller's job; we match the lowercase form here.
AUTH_HEADER_CANDIDATES: tuple[str, ...] = (
    "x-api-key",
    "authorization",
)


def parse_passthrough_auth(auth_value: str) -> tuple[str, str]:
    """Split ``url@@api-key`` into ``(url, api_key)``.

    Uses the **last** ``@@`` as the separator so the URL portion may
    legitimately contain a single ``@`` (RFC 3986 userinfo). Empty parts
    on either side raise ``ValueError`` with a Chinese error message that
    the middleware surfaces verbatim as a 400.

    >>> parse_passthrough_auth("https://api.openai.com@@sk-xxx")
    ('https://api.openai.com', 'sk-xxx')
    >>> parse_passthrough_auth("https://user:pw@host.com@@sk-xxx")
    ('https://user:pw@host.com', 'sk-xxx')
    """
    if not isinstance(auth_value, str):
        raise ValueError("鉴权头不是字符串")
    idx = auth_value.rfind("@@")
    if idx == -1:
        raise ValueError("缺少 @@ 分隔符（格式：url@@api-key）")
    url = auth_value[:idx].strip()
    key = auth_value[idx + 2 :].strip()
    if not url:
        raise ValueError("URL 不能为空")
    if not key:
        raise ValueError("api-key 不能为空")
    return url, key


def extract_model_field(body: bytes) -> tuple[Optional[str], str]:
    """Pull ``(model_value, field_name_used)`` from a JSON request body.

    Tries each candidate in :data:`MODEL_FIELD_CANDIDATES` order. The
    returned ``field_name`` is what the upstream is actually using — it
    becomes part of the passthrough fingerprint so two requests with the
    same URL + key but different model-field conventions land in
    different buckets (correct: they're structurally different).

    On parse failure or no match, returns ``(None, "model")`` — caller
    can still fingerprint by (url, key, "model") and just won't have a
    model value for stats.
    """
    field_used = "model"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, field_used
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, field_used
    if not isinstance(data, dict):
        return None, field_used
    for field in MODEL_FIELD_CANDIDATES:
        val = data.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip(), field
        field_used = field
    return None, field_used


def strip_auth_scheme(value: str, header_name: str) -> str:
    """Strip the ``Bearer `` prefix from ``Authorization`` headers.

    ``x-api-key`` carries the raw key with no scheme; ``Authorization``
    uses ``Bearer <key>``. The middleware calls this before
    :func:`parse_passthrough_auth` so the same parser works for both.
    """
    if header_name.lower() == "authorization" and value.lower().startswith("bearer "):
        return value[7:]
    return value


def deep_search_usage(obj: Any, *, depth: int = 0, max_depth: int = 10) -> dict[str, int]:
    """Recursively walk JSON and pick up token-usage fields.

    Matches the aliases in :data:`_USAGE_KEY_ALIASES` and takes the **max**
    value per canonical field across all hits. ``prompt_tokens`` and
    ``completion_tokens`` both fold into ``input_tokens`` /
    ``output_tokens`` respectively (OpenAI legacy naming).

    Max-depth of 10 protects against circular structures in pathological
    responses; responses in practice are ≤ 5 levels deep.
    """
    found: dict[str, int] = {}
    if depth >= max_depth:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, bool):
                continue  # bools are ints in Python; exclude explicitly
            if isinstance(v, int):
                canonical = _USAGE_KEY_ALIASES.get(k)
                if canonical is not None and v > found.get(canonical, -1):
                    found[canonical] = v
            elif isinstance(v, (dict, list)):
                sub = deep_search_usage(v, depth=depth + 1, max_depth=max_depth)
                for ck, cv in sub.items():
                    if cv > found.get(ck, -1):
                        found[ck] = cv
    elif isinstance(obj, list):
        for item in obj:
            sub = deep_search_usage(item, depth=depth + 1, max_depth=max_depth)
            for ck, cv in sub.items():
                if cv > found.get(ck, -1):
                    found[ck] = cv
    return found


def usage_from_sse_buffer(buffer: bytes) -> dict[str, int]:
    """Extract usage from a buffered SSE response.

    For SSE each ``data:`` line is a JSON object; we deep-search each.
    The full buffer is also deep-searched as a fallback in case the
    upstream uses a non-SSE envelope we didn't recognize.
    """
    found: dict[str, int] = {}
    try:
        text = buffer.decode("utf-8", errors="replace")
    except Exception:
        return found
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        sub = deep_search_usage(data)
        for k, v in sub.items():
            if v > found.get(k, -1):
                found[k] = v
    # Fallback: search the whole buffer (catches non-SSE envelopes).
    sub = deep_search_usage(_maybe_json(text))
    for k, v in sub.items():
        if v > found.get(k, -1):
            found[k] = v
    return found


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None