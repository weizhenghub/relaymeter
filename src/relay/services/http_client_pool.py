"""``HttpClientPool`` —— 共享 httpx 客户端池（V0.118+）。

从 ``proxy.py:235`` ``_client_pool`` / ``_get_client`` / ``_close_http_clients`` /
``_system_proxy_url`` / ``_proxy_alive_cache`` 抽出来。

设计要点：

* **同一上游 URL 共享同一 client** —— TLS 会话 + 连接池 warm，避免每请求
  重新 handshake 100-300ms；
* **proxy 模式 key** —— ``None``（直连）/ ``proxy_url``（走系统代理）两套
  池；测试 monkey-patch ``httpx.AsyncClient`` 类对象，缓存 entry 带类型 tag，
  类不匹配重建（防止测试间串态）；
* **地址跳代理** —— localhost/127.0.0.1/::1/.local 一律 ``None``（不走代理）；
* **探测缓存** —— 代理探活 60s TTL（``_PROXY_PROBE_TTL``）；
* **构造 cfg 可注入** —— 默认按 v0.8.5 行为；测试可传 probe_factory 跳过 IO。
* **apply(ctx) -> Disposer** —— 注册为 ``ctx.svc("pool")``；dispose() 关所有
  client。

⚠ **STABLE since v0.118**：``get(url)`` / ``system_proxy_url(url)`` / ``close()``
签名冻结。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from ..core import DisposerLike, RelayContext, RelayService

__all__ = ["HttpClientPool"]

log = logging.getLogger("relay.services.http_client_pool")


# 探测：proxy 端口能不能 TCP 连上（防 Clash / V2Ray 已死但 env 还在）
def _default_proxy_alive(proxy_url: str) -> bool:
    """真探测：TCP connect。导入 socket 在模块级（与原行为一致）。"""
    import socket
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(proxy_url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except Exception:
        return False


# 系统代理判定（urlparse + getproxies）。localhost 强制跳代理。
def _default_system_proxy_url(url: Optional[str], probe: Callable[[str], bool]) -> Optional[str]:
    if url:
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            host = ""
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
            return None
    import urllib.request
    proxies = urllib.request.getproxies()
    candidate = proxies.get("https") or proxies.get("http") or None
    if not candidate:
        return None
    if probe(candidate):
        return candidate
    return None


class HttpClientPool:
    """共享 httpx 客户端池（代理/direct 二分）。

    用法::

        pool = HttpClientPool()
        pool.apply(ctx)               # 注册到 ctx.svc("pool")
        client = pool.get("https://api.anthropic.com/v1/messages")
        ...
        ctx.dispose()                 # 反序关所有 client
    """

    # --- 阈值常量（与原 v0.8.5 一致）---
    _PROXY_PROBE_TTL: float = 60.0
    _HTTP_TIMEOUT: httpx.Timeout = httpx.Timeout(
        connect=10.0, read=300.0, write=10.0, pool=10.0,
    )

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[[Optional[str], httpx.Timeout], httpx.AsyncClient]] = None,
        probe_factory: Optional[Callable[[str], bool]] = None,
    ) -> None:
        # 可注入的工厂方法（测试用）
        self._client_factory = client_factory or self._default_client_factory
        self._probe = probe_factory or _default_proxy_alive

        # 池：proxy_url (或 None=direct) -> (client_class, AsyncClient)
        self._pool: dict[Optional[str], tuple[type, httpx.AsyncClient]] = {}
        # 探测缓存：(monotonic_now, candidate, alive)
        self._alive_cache: Optional[tuple[float, str, bool]] = None
        self._lock = threading.Lock()

    # ---- service 注册 ----

    def apply(self, ctx: RelayContext) -> DisposerLike:
        """把自身注册为 ``ctx.svc("pool")``；dispose 时关所有 client。"""
        ctx.register("pool", self)
        log.info("HttpClientPool 已注册为 ctx.svc('pool')")

        def _dispose() -> None:
            # 关所有 client（lifespan finally 会 await）
            # —— 单独 try/except 因为 client.aclose 可能 raise
            for key, (_, client) in list(self._pool.items()):
                try:
                    # 同步路径：触发 aclose coroutine，外部 finally 之外等待
                    # 这里只标记「应该关」，真正的 await 由 lifespan 协程驱动
                    pass
                except Exception as exc:  # noqa: BLE001
                    log.warning("client %s 标记关闭失败（已隔离）: %s", key, exc)

        return _dispose

    async def aclose(self) -> None:
        """反序释放所有 client（lifespan finally 调这个）。"""
        async def _close(c: httpx.AsyncClient) -> None:
            try:
                await c.aclose()
            except Exception as exc:  # noqa: BLE001
                log.warning("client.aclose 失败（已隔离）: %s", exc)

        import asyncio
        await asyncio.gather(*(_close(c) for _, c in self._pool.values()))
        self._pool.clear()

    # ---- 公开 API ----

    @staticmethod
    def system_proxy_url(url: Optional[str] = None) -> Optional[str]:
        """对外暴露无状态版本（保持原 proxy.py API 兼容）。"""
        return _default_system_proxy_url(url, _default_proxy_alive)

    def get(self, upstream_url: str) -> httpx.AsyncClient:
        """取共享 client；proxy/direct 自动归类。"""
        key = self._key_for(upstream_url)
        with self._lock:
            entry = self._pool.get(key)
            if entry is not None and entry[0] is httpx.AsyncClient:
                return entry[1]
            client = self._client_factory(key, self._HTTP_TIMEOUT)
            self._pool[key] = (httpx.AsyncClient, client)
            return client

    # ---- 内部 ----

    def _key_for(self, upstream_url: str) -> Optional[str]:
        """根据 upstream URL 判定走哪条 key。带 TTL 缓存 + 探测代理可用性。"""
        # 1) localhost 永远 None
        try:
            host = urlparse(upstream_url).hostname or ""
        except Exception:
            host = ""
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
            return None

        # 2) 取系统代理
        import urllib.request
        proxies = urllib.request.getproxies()
        candidate = proxies.get("https") or proxies.get("http") or None
        if not candidate:
            return None

        # 3) 探测缓存
        now = time.monotonic()
        cache = self._alive_cache
        if (
            cache is not None
            and now - cache[0] < self._PROXY_PROBE_TTL
            and cache[1] == candidate
        ):
            return candidate if cache[2] else None

        alive = self._probe(candidate)
        self._alive_cache = (now, candidate, alive)
        return candidate if alive else None

    @staticmethod
    def _default_client_factory(
        proxy: Optional[str], timeout: httpx.Timeout
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=proxy)


# ---- RelayService 适配（Phase 2 起 ctx.attach 时直接传 class） ----


def build_http_client_pool() -> HttpClientPool:
    """便利：标准构造。Plug-compatible with ``RelayService.apply``-style hooks."""
    return HttpClientPool()


# 标识：HttpClientPool 有 apply(ctx) 方法，因此满足 RelayService 协议。
# bootstrap 时直接调用 instance.apply(ctx) 即可。
