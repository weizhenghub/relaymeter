"""Pydantic models shared by parsers, proxy, and stats endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UsageAcc(BaseModel):
    """Accumulates token usage as SSE events stream in.

    All fields default to 0 so partial streams produce a row with whatever
    values we managed to capture before disconnect / error.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def merge(self, other: "UsageAcc") -> None:
        """Take the max of each field — handles Anthropic cumulative output_tokens."""
        for f in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            setattr(self, f, max(getattr(self, f), getattr(other, f)))


class StatsRow(BaseModel):
    """Aggregate row per platform."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    errors: int = 0


class StatsResponse(BaseModel):
    """Aggregate totals keyed by platform name."""

    totals: dict[str, StatsRow] = Field(default_factory=dict)