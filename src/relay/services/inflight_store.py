"""``InflightStore`` —— inflight 状态存储 + sweeper 控制（V0.118+）。

从 ``proxy.py:945-1571`` ``_InFlight`` / ``_in_flight`` / ``_in_flight_done`` /
``_in_flight_lock`` / ``_register_inflight`` / ``_update_inflight`` /
``_bump_chunk_activity`` / ``_set_inflight_phase`` / ``_complete_inflight`` /
``_find_done`` / ``_sweep_inflight_once`` / ``_sweep_loop`` /
``_monitor_quiet_streaming`` + 常量 ``STREAMING_STALE_AFTER`` /
``QUIET_STREAMING_AFTER`` / ``MAX_INFLIGHT_AGE`` / ``SWEEP_INTERVAL`` /
``QUIET_MONITOR_INTERVAL`` / ``STREAM_QUIET_AFTER`` / ``UPLOAD_QUIET_AFTER`` 抽出。

设计要点（与原 proxy.py 一致）：

* **bump_chunk_activity 仅 mutex** —— 不进入 setattr 循环，热路径每字节
  都能扛。无 setattr 是热路径红线，**不动**；
* **Done 列表 capped** —— ``MAX_DONE_VISIBLE``（默认 5），超出按插入淘汰；
  单用户语义：新的 request 进来最老 done 被挤掉；
* **两个 sweeper task**：``_sweep_loop``（30s 一次，扫 stale streaming /
  hard ceiling）+ ``_monitor_quiet_streaming``（短间隔，扫「chunk 停了但
  TCP 还活」）；
* **start_sweepers() -> (tasks, stops)** —— 由 lifespan 持有，dispose 时
  set stop event + cancel task；
* **get_with_cleartext(rid)** —— 返回 _InFlight 或 None，明文 api_key
  唯一入口（GUI 进程永远拿 None —— 见原注释）；
* **snapshot()** —— 掩码版本，HTTP /live 端点用。

⚠ **STABLE since v0.118**：method signature 冻结；新增字段可以但先标
deprecation warning。
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
import time
import uuid
from typing import Any, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["InflightStore", "_InFlight"]

log = logging.getLogger("relay.services.inflight_store")


# ---------------------------------------------------------------------------
# Inflight 状态对象（与原 _InFlight 同字段）
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _InFlight:
    """in-flight 请求条目；GUI/TUI 实时栏数据源。

    字段演化见 ``docs/architecture/20_registries_to_services_v0.118.md``。
    """
    request_id: str
    started_at: float
    platform: str
    model: Optional[str]
    phase: str = "uploading"  # uploading | calling | streaming | done
    bytes_received: int = 0
    content_length: Optional[int] = None
    user_text_preview: str = ""
    assistant_text: str = ""
    last_update: float = dataclasses.field(default_factory=time.time)
    last_chunk_at: float = dataclasses.field(default_factory=time.time)
    client_model: Optional[str] = None
    upstream: str = ""
    api_key: str = ""                       # 明文；snapshot 掩码
    inbound_wire: str = ""
    outbound_wire: str = ""
    usage_live: dict = dataclasses.field(default_factory=dict)
    thinking_text: str = ""
    tool_use_json: str = ""
    thinking_done: bool = False


# ---------------------------------------------------------------------------
# 阈值常量（与原 proxy.py 一致）
# ---------------------------------------------------------------------------

STREAMING_STALE_AFTER = 90.0     # 主 sweeper：streaming 阶段 last_update 超 90s 视 stuck
QUIET_STREAMING_AFTER = 30.0     # 保留兼容字段（实际由 STREAM_QUIET_AFTER 替代）
MAX_INFLIGHT_AGE = 600.0         # 10 分钟硬上限；任何 phase 超这个强制 done

SWEEP_INTERVAL = 30.0            # 主 sweeper 周期
QUIET_MONITOR_INTERVAL = 1.0     # quiet monitor 周期（更敏感）
STREAM_QUIET_AFTER = 30.0        # streaming 阶段 last_chunk_at 超 30s 视 stuck
UPLOAD_QUIET_AFTER = 60.0        # uploading 阶段 last_update 超 60s 视 stuck

MAX_DONE_VISIBLE = 5             # done 列表容量
_BROADCAST_QUEUE_MAX = 200       # LiveBus 队列大小上限（移到 LiveBus）


def _mask_key(k: str) -> str:
    """掩码 api_key（与原 proxy.py 一致）。"""
    if not k:
        return ""
    if len(k) <= 8:
        return "***"
    return f"{k[:4]}***{k[-4:]}"


# ---------------------------------------------------------------------------
# InflightStore
# ---------------------------------------------------------------------------


class InflightStore:
    """进程内 inflight 状态 + sweeper 控制。

    用法::

        store = InflightStore()
        store.apply(ctx)                 # 注册 ctx.svc("inflight")
        inf = await store.register(platform="anthropic", model="...")
        await store.bump_chunk_activity(inf.request_id)
        await store.complete(inf.request_id)
        ...
        tasks, stops = store.start_sweepers()
        # lifespan 退出：set stop + cancel tasks
    """

    # 把阈值常量挂到类上，方便外部读（与原 module-level 常量一致语义）
    STREAMING_STALE_AFTER = STREAMING_STALE_AFTER
    QUIET_STREAMING_AFTER = QUIET_STREAMING_AFTER
    MAX_INFLIGHT_AGE = MAX_INFLIGHT_AGE
    SWEEP_INTERVAL = SWEEP_INTERVAL
    QUIET_MONITOR_INTERVAL = QUIET_MONITOR_INTERVAL
    STREAM_QUIET_AFTER = STREAM_QUIET_AFTER
    UPLOAD_QUIET_AFTER = UPLOAD_QUIET_AFTER
    MAX_DONE_VISIBLE = MAX_DONE_VISIBLE

    def __init__(self) -> None:
        self._live: dict[str, _InFlight] = {}
        self._done: collections.deque[_InFlight] = collections.deque(maxlen=MAX_DONE_VISIBLE)
        self._lock = asyncio.Lock()
        self._bus: Any = None  # LiveBus 注入（Phase 2.4 之后连）

    # ---- service 注册 ----

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("inflight", self)

        # Phase 2.10：apply 时启动 sweeper；dispose 时一起收尾。
        # 注：必须 ctx._loop / running loop 已就绪；lifespan 在 yield 之前调用 apply，
        # 主事件循环已开，OK。
        try:
            tasks, stops = self.start_sweepers()
        except RuntimeError:
            # 没有 running loop（极少数 test 路径）；跳过 sweeper
            tasks, stops = [], []
        self._sweeper_tasks: list[asyncio.Task] = tasks
        self._sweeper_stops: list[asyncio.Event] = stops

        def _dispose() -> None:
            # 1. 停 sweeper（set stop_event，cancel task）
            for s in self._sweeper_stops:
                s.set()
            for t in self._sweeper_tasks:
                if not t.done():
                    t.cancel()
            # 2. 清空状态（不 await lock：dispose 时 loop 可能半停）
            self._live.clear()
            self._done.clear()
            self._sweeper_tasks.clear()
            self._sweeper_stops.clear()

        ctx.add_disposer(_dispose)
        return _dispose

    # ---- 数据 CRUD（外部 API；与原 module-level fn 同语义）----

    async def register(
        self,
        *,
        platform: str,
        model: Optional[str],
        inbound_wire: str = "",
        outbound_wire: str = "",
        api_key: str = "",
    ) -> _InFlight:
        """新建一条 inflight；返回 _InFlight 引用（request_id 唯一）。"""
        inf = _InFlight(
            request_id=uuid.uuid4().hex,
            started_at=time.time(),
            platform=platform,
            model=model,
            client_model=model,
            inbound_wire=inbound_wire,
            outbound_wire=outbound_wire,
            api_key=api_key,
        )
        async with self._lock:
            # Cap 总可见 (live + done)
            while len(self._live) + len(self._done) >= MAX_DONE_VISIBLE:
                if self._done:
                    self._done.pop()
                else:
                    break
            self._live[inf.request_id] = inf
        return inf

    async def update(self, rid: str, **fields: object) -> None:
        """patch 字段；entry 已 done/不存在时 no-op。"""
        async with self._lock:
            inf = self._live.get(rid)
            if inf is None:
                return
            for k, v in fields.items():
                setattr(inf, k, v)
            inf.last_update = time.time()

    async def bump_chunk_activity(self, rid: str) -> None:
        """**热路径**：仅更新 last_chunk_at + last_update；不 setattr。"""
        async with self._lock:
            inf = self._live.get(rid)
            if inf is None:
                return
            now = time.time()
            inf.last_chunk_at = now
            inf.last_update = now

    async def set_phase(self, rid: str, phase: str) -> None:
        async with self._lock:
            inf = self._live.get(rid)
            if inf is None:
                return
            inf.phase = phase
            inf.last_update = time.time()

    async def complete(self, rid: str) -> None:
        """把 live 条目移到 done 列表；幂等。"""
        async with self._lock:
            inf = self._live.pop(rid, None)
            if inf is None:
                return
            inf.phase = "done"
            inf.last_update = time.time()
            # newest-first
            self._done.appendleft(inf)
            while len(self._done) > MAX_DONE_VISIBLE:
                self._done.pop()

    def find_done(self, rid: str) -> Optional[_InFlight]:
        """从 done 列表查（不弹出）。"""
        for inf in self._done:
            if inf.request_id == rid:
                return inf
        return None

    def get_with_cleartext(self, rid: str) -> Optional[_InFlight]:
        """拿明文 api_key；GUI 进程永远拿 None。"""
        inf = self._live.get(rid) or self.find_done(rid)
        return inf

    # ---- live 事件订阅（/live/stream 数据源）----
    # 委托 legacy 广播（proxy_legacy._SUBSCRIBERS + _broadcast），因为
    # relay() 转发主路当前仍在 proxy_legacy 模块内跑，流式事件全部发往
    # legacy 队列。Phase 4 完成 relay 迁移到 ctx 服务后，这里再改为委托
    # LiveBus（ctx.svc("livebus")），两者 API 同构（subscribe/unsubscribe/
    # publish）。延迟 import 避免 services → proxy_legacy 潜在环。
    def subscribe(self) -> "asyncio.Queue":
        from ..proxy_legacy import _subscribe_live_stream
        return _subscribe_live_stream()

    async def unsubscribe(self, q: "asyncio.Queue") -> None:
        from ..proxy_legacy import _unsubscribe_live_stream
        await _unsubscribe_live_stream(q)

    async def publish(self, event: dict) -> None:
        from ..proxy_legacy import _broadcast
        await _broadcast(event)

    def snapshot(self) -> list[dict]:
        """掩码版快照（HTTP /live 数据源）。"""
        out: list[dict] = []
        for inf in list(self._live.values()) + list(self._done):
            out.append(self._to_masked_dict(inf))
        return out

    @staticmethod
    def _to_masked_dict(inf: _InFlight) -> dict:
        d = dataclasses.asdict(inf)
        # 掩码 api_key（清空明文，留字段供前端调试）
        d["api_key"] = _mask_key(inf.api_key)
        return d

    # ---- sweeper 控制（lifespan 启动时调，dispose 时取消）----

    def start_sweepers(self) -> tuple[list[asyncio.Task], list[asyncio.Event]]:
        """起两个 sweeper task；返回 (tasks, stop_events)。

        lifespan 退出::

            tasks, stops = store.start_sweepers()
            ...
            for s in stops: s.set()
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        """
        stop1 = asyncio.Event()
        stop2 = asyncio.Event()
        t1 = asyncio.create_task(self._sweep_loop(stop1), name="inflight-sweeper")
        t2 = asyncio.create_task(
            self._monitor_quiet_streaming(stop2), name="inflight-quiet-monitor",
        )
        return [t1, t2], [stop1, stop2]

    async def _sweep_once(self) -> int:
        """sweep stale streaming + hard ceiling。返回本次 force-complete 数。"""
        now = time.time()
        stuck: list[str] = []
        async with self._lock:
            for rid, inf in list(self._live.items()):
                age = now - inf.last_update
                if age < 0:
                    continue
                if inf.phase == "streaming" and age >= STREAMING_STALE_AFTER:
                    stuck.append(rid)
                elif age >= MAX_INFLIGHT_AGE:
                    stuck.append(rid)
        for rid in stuck:
            await self.complete(rid)
        if stuck:
            log.warning(
                "inflight sweeper force-completed %d entr%s",
                len(stuck), "y" if len(stuck) == 1 else "ies",
            )
        return len(stuck)

    async def _sweep_loop(self, stop_event: asyncio.Event) -> None:
        log.info("inflight sweeper started (interval=%ss)", SWEEP_INTERVAL)
        while not stop_event.is_set():
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("inflight sweep tick failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=SWEEP_INTERVAL)
            except asyncio.TimeoutError:
                pass
        log.info("inflight sweeper stopped")

    async def _monitor_quiet_streaming(self, stop_event: asyncio.Event) -> None:
        log.info(
            "inflight quiet monitor started (interval=%ss threshold=%ss upload=%ss)",
            QUIET_MONITOR_INTERVAL, STREAM_QUIET_AFTER, UPLOAD_QUIET_AFTER,
        )
        while not stop_event.is_set():
            try:
                now = time.time()
                stuck: list[tuple[str, float, str]] = []
                async with self._lock:
                    for rid, inf in list(self._live.items()):
                        if inf.phase == "streaming":
                            silent_for = now - inf.last_chunk_at
                            if silent_for >= STREAM_QUIET_AFTER:
                                stuck.append((rid, silent_for, inf.phase))
                        elif inf.phase == "uploading":
                            silent_for = now - inf.last_update
                            if silent_for >= UPLOAD_QUIET_AFTER:
                                stuck.append((rid, silent_for, inf.phase))
                for rid, _, _ in stuck:
                    await self.complete(rid)
                if stuck:
                    log.warning(
                        "inflight quiet monitor force-completed %d entr%s",
                        len(stuck), "y" if len(stuck) == 1 else "ies",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("inflight quiet monitor tick failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=QUIET_MONITOR_INTERVAL)
            except asyncio.TimeoutError:
                pass
        log.info("inflight quiet monitor stopped")
