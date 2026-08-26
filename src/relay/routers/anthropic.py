"""Catch-all router for /anthropic/* — relays to the platform's active upstream."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..parsers.anthropic import AnthropicUsageParser
from ..proxy import _infer_client_wire_from_path, relay


router = APIRouter()


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def catchall(path: str, request: Request) -> Response:
    # v0.X 鲁棒性增强：路径末段推断客户端 wire —— 覆盖客户端 SDK 把
    # 中继当上游、却按别的协议拼后缀的场景（如 /anthropic/v1/chat/completions
    # 实际是 openai wire）。None 时让 relay() 自己 body 嗅探或按 platform 兜底。
    client_wire = _infer_client_wire_from_path(path, platform_hint="anthropic")
    return await relay(
        request,
        platform="anthropic",
        parser_factory=AnthropicUsageParser,
        client_wire=client_wire,
    )
