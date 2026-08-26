"""proxy/_inflight_shim.py —— inflight 状态 shim（V0.119 兼容层）。

策略（V0.119+）：

* **优先 ctx**：lifespan 已注入 ``ctx.svc("inflight")``（InflightStore），
  所有 inflight 写操作（register / update / bump / set_phase / complete /
  find_done）**直接走 service**。读操作（snapshot）同理。

* **回退 module-global**：test / 早期 lifespan 路径可能没有 ctx。这时
  shim 走 :mod:`relay.proxy_legacy` 的模块全局 (``_in_flight`` /
  ``_in_flight_done``)。``proxy._in_flight[rid]`` 这类下划线访问仍
  能工作（兼容 test_wire.py / _legacy_cleartext 等）。

* **最终目标（Phase 4）**：所有 ``from . import proxy; proxy._in_flight``
  转 ``ctx.svc("inflight")``，本 shim 只剩 ctx 优先路径。

公开（通过 ``relay.proxy.__init__`` 间接 re-export）：

* ``_in_flight`` / ``_in_flight_done`` / ``_in_flight_lock`` —— 兼容访问
* ``MAX_DONE_VISIBLE`` / ``BODY_LARGE_THRESHOLD`` —— 兼容常量
* ``_register_inflight`` / ``_update_inflight`` / ``_bump_chunk_activity`` /
  ``_set_inflight_phase`` / ``_complete_inflight`` / ``_find_done`` /
  ``get_inflight_snapshot``
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger("relay.proxy._inflight_shim")


# ---- ctx 探测 ----

_ctx_singleton: dict[str, Any] = {"ctx": None}


def bind_ctx(ctx: Any) -> None:
    """lifespan 注入 ctx（Phase 3 兼容层内部用）。"""
    _ctx_singleton["ctx"] = ctx


def _store() -> Optional[Any]:
    """返回当前 InflightStore；没 ctx 返回 None（回退模块全局）。"""
    ctx = _ctx_singleton["ctx"]
    if ctx is None:
        return None
    try:
        return ctx.svc("inflight")
    except Exception:  # noqa: BLE001
        return None


# ---- 兼容：模块全局（被 proxy_legacy 的 dataclass + 常量占据） ----
# 实际写操作不直接修改这里；proxy_legacy 的旧 fn 仍写自己的 dict，新
# 路径（ctx 优先）直接走 InflightStore。test 路径既可能读这里也可能
# 走 InflightStore，**两者不互通**（旧 global dict 已经是 frozen 旧
# 行为；新 InflightStore 是 ctx 绑定路径）。Phase 4 起全部 ctx 化。

# Re-export the module-level globals that some legacy tests still touch.
# Importing proxy_legacy here is intentional: this is a compat shim.
from .. import proxy_legacy  # noqa: E402,F401  (intentional compat)

_in_flight = proxy_legacy._in_flight
_in_flight_done = proxy_legacy._in_flight_done
_in_flight_lock = proxy_legacy._in_flight_lock
MAX_DONE_VISIBLE = proxy_legacy.MAX_DONE_VISIBLE
BODY_LARGE_THRESHOLD = proxy_legacy.BODY_LARGE_THRESHOLD


# ---- 写操作：ctx 优先 ----

async def _register_inflight(*, platform: str, model: Optional[str], **fields: Any) -> Any:
    s = _store()
    if s is not None:
        return await s.register(platform=platform, model=model, **fields)
    # 回退到 legacy module-fn
    return await proxy_legacy._register_inflight(platform=platform, model=model, **fields)


async def _update_inflight(rid: str, **fields: Any) -> None:
    s = _store()
    if s is not None:
        await s.update(rid, **fields)
        return
    await proxy_legacy._update_inflight(rid, **fields)


async def _bump_chunk_activity(rid: str) -> None:
    s = _store()
    if s is not None:
        await s.bump_chunk_activity(rid)
        return
    await proxy_legacy._bump_chunk_activity(rid)


async def _set_inflight_phase(rid: str, phase: str) -> None:
    s = _store()
    if s is not None:
        await s.set_phase(rid, phase)
        return
    await proxy_legacy._set_inflight_phase(rid, phase)


async def _complete_inflight(rid: str) -> None:
    s = _store()
    if s is not None:
        await s.complete(rid)
        return
    await proxy_legacy._complete_inflight(rid)


def _find_done(rid: str) -> Optional[Any]:
    """同步读：优先 ctx（快照 + done deque），回退 legacy dict。"""
    s = _store()
    if s is not None:
        return s.find_done(rid)
    return proxy_legacy._find_done(rid)


# ---- 读操作 ----

def get_inflight_snapshot() -> list[dict]:
    s = _store()
    if s is not None:
        return s.snapshot()
    return proxy_legacy.get_inflight_snapshot()


# ---- 同步锁（legacy 路径可能需要） ----

def _get_inflight_lock() -> asyncio.Lock:
    """回退路径返回 legacy 的 asyncio.Lock；ctx 路径用 service 内部锁（无需外暴露）。"""
    return proxy_legacy._in_flight_lock