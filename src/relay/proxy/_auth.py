"""proxy/_auth.py —— 鉴权 header 拼装 / key normalize / mask。

V0.119+ 从 ``proxy.py`` 拆出。

公开稳定（re-export via ``relay.proxy.__init__``）：

* ``_normalize_key(value)``
* ``_auth_header_value(style, key) -> (name, value)``
* ``_client_auth_key(request, platform)``
* ``_mask_key(key)``

⚠ deprecated for new code —— Phase 4 起由 :class:`relay.services.AuthHeader`
接管。
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request

__all__ = [
    "_normalize_key",
    "_auth_header_value",
    "_client_auth_key",
    "_mask_key",
]


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