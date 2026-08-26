"""根路径 catchall —— 客户端 SDK 配 ``base_url=http://...:8088`` 自动补后缀时走这里。

不要求客户端在 base URL 里加 ``/anthropic`` 或 ``/openai`` 前缀 —— 看 path 末段
+ body 顶部特征推断 wire，再按 wire 决定走哪个 platform 的 active upstream。

示例：
  POST /v1/messages          → anthropic platform + anthropic-messages wire
  POST /v1/chat/completions  → openai platform + openai-chat wire
  POST /v1/responses         → openai platform + openai-responses wire
  POST /                     → 400（路径空，无法推断）

无法推断时返回 400 + 提示用 /anthropic 或 /openai 前缀，不静默兜底（避免
把 OpenAI 协议请求误转到 anthropic upstream）。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..proxy import (
    _infer_client_wire_from_path,
    _resolve_platform_for_wire,
    _sniff_client_wire,
    relay,
)


log = logging.getLogger("relay.routers.root")


router = APIRouter()


def _bad_request(detail: str) -> Response:
    """返回 400 + 中文提示（客户端配错 base URL 时引导加前缀）。"""
    return Response(
        content=json.dumps(
            {"error": "无法识别协议", "detail": detail},
            ensure_ascii=False,
        ).encode("utf-8"),
        status_code=400,
        media_type="application/json; charset=utf-8",
    )


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def catchall(path: str, request: Request) -> Response:
    # 1. 路径推断 wire（无平台前缀场景，hint 只影响重名端点消歧；
    #    /v1/messages 仍优先 anthropic —— 与 clients 期望一致）。
    wire = _infer_client_wire_from_path(path, platform_hint="anthropic")

    # 2. 路径推不出 → 读 body 嗅探（FastAPI body 已 cache，后续 relay() 再读免费）。
    if wire is None:
        body = await request.body()
        wire = _sniff_client_wire(body)

    # 3. wire → platform bucket。
    platform = _resolve_platform_for_wire(wire)
    if platform is None:
        log.info("root catchall: cannot infer wire from path=%r", path)
        return _bad_request(
            "无法从路径或请求体识别协议。"
            "请用 /anthropic 或 /openai 前缀的 base URL（如 "
            "http://127.0.0.1:8088/anthropic），或确保请求路径含已知端点。"
        )

    return await relay(
        request,
        platform=platform,
        parser_factory=None,  # relay() 按 client_wire 自行解析
        client_wire=wire,
    )
