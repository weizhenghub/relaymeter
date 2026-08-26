"""proxy/_http_shim.py —— HttpClientPool 兼容层（V0.119+）。

* ctx 优先：``ctx.svc("pool")`` 返回 :class:`HttpClientPool`，所有
  ``_get_client`` / ``_close_http_clients`` / ``_system_proxy_url`` 都
  走 service。
* 回退：test / 早期 lifespan 走 :mod:`relay.proxy_legacy` 模块全局。
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .. import proxy_legacy


_ctx_singleton: dict[str, Any] = {"ctx": None}


def bind_ctx(ctx: Any) -> None:
    _ctx_singleton["ctx"] = ctx


def _pool() -> Optional[Any]:
    ctx = _ctx_singleton["ctx"]
    if ctx is None:
        return None
    try:
        return ctx.svc("pool")
    except Exception:  # noqa: BLE001
        return None


def _system_proxy_url(url: Optional[str] = None) -> Optional[str]:
    # V0.119 兼容：test 通过 monkeypatch ``relay.proxy._proxy_alive`` /
    # ``relay.proxy._proxy_alive_cache`` 来控制系统代理探测；HttpClientPool
    # 自己注入的探测函数 patch 不进去。这里**始终走 legacy** 路径，且
    # 动态把 test patch 转发到 proxy_legacy._proxy_alive / _proxy_alive_cache
    # 让 monkeypatch 能命中（test 每次只对单 url 探测，状态切换兼容）。
    import sys
    pl = sys.modules.get("relay.proxy_legacy")
    proxy_mod = sys.modules.get("relay.proxy")
    if pl is None or proxy_mod is None:
        return None
    original_alive = pl._proxy_alive
    original_cache = pl._proxy_alive_cache
    # 把 test patch 的 attr 同步到 proxy_legacy，让 _system_proxy_url 用到
    patched_alive = getattr(proxy_mod, "_proxy_alive", None)
    if patched_alive is not None and patched_alive is not original_alive:
        pl._proxy_alive = patched_alive
    try:
        return pl._system_proxy_url(url)
    finally:
        # 还原避免污染后续非 patch 调用
        pl._proxy_alive = original_alive
        pl._proxy_alive_cache = original_cache


def _get_client(upstream_url: str) -> httpx.AsyncClient:
    p = _pool()
    if p is not None:
        return p.get(upstream_url)
    return proxy_legacy._get_client(upstream_url)


async def _close_http_clients() -> None:
    p = _pool()
    if p is not None:
        await p.aclose()
        return
    await proxy_legacy._close_http_clients()


# 兼容：客户端拿到的旧符号（如 ``_HTTP_TIMEOUT`` / ``_client_pool``）继续
# 暴露（部分 test 可能 import）。
_HTTP_TIMEOUT = proxy_legacy._HTTP_TIMEOUT
_client_pool = proxy_legacy._client_pool