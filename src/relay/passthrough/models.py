"""Pydantic models for passthrough mode.

Kept independent of ``relay.models`` so the passthrough code path can evolve
without touching (or being touched by) the conversion / dispatch path.
"""

from __future__ import annotations

from pydantic import BaseModel


# Field names tried in order when extracting the model from a request body.
# First match wins; ``"model"`` is the universal Anthropic / OpenAI default.
MODEL_FIELD_CANDIDATES: tuple[str, ...] = ("model", "model_id", "engine")


# Token-usage field aliases accepted by the deep search. ``prompt_tokens``
# / ``completion_tokens`` are OpenAI's legacy names; ``input_tokens`` /
# ``output_tokens`` are Anthropic + Responses. We take max across all hits.
_USAGE_KEY_ALIASES: dict[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "prompt_tokens": "input_tokens",          # OpenAI legacy
    "completion_tokens": "output_tokens",     # OpenAI legacy
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_input_tokens": "cache_creation_input_tokens",
}


class PassthroughUpstream(BaseModel):
    """One auto-discovered upstream in passthrough mode.

    The composite ``(url, api_key_alias, model_field_name)`` IS the
    fingerprint — two requests hit the same upstream iff all three match.
    """

    url: str
    api_key_alias: str
    model_field_name: str = "model"
    display_name: str | None = None
    first_seen: float
    last_seen: float
    request_count: int = 0

    @property
    def fingerprint(self) -> tuple[str, str, str]:
        return (self.url, self.api_key_alias, self.model_field_name)

    @property
    def upstream_name(self) -> str:
        """Auto-generated display name when the user hasn't set one.

        Format: ``<host_path>|<model_field_name>|<last4 of key>``.
        Strip ``http://`` / ``https://`` prefix so the name stays compact.
        """
        if self.display_name:
            return self.display_name
        host = self.url
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        key = self.api_key_alias or ""
        tail = key[-4:] if len(key) >= 4 else key
        return f"{host}|{self.model_field_name}|{tail}"


class PassthroughStatsRow(BaseModel):
    """Aggregate row per (url, model_field_name) grouping."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    errors: int = 0
    total_tokens: int = 0