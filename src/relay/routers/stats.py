"""Health, aggregation, and time-window stats endpoints.

These endpoints return only aggregate token counts — no request bodies,
no headers, no PII. They are always safe to expose locally.

Time windows
------------

The `?window=<name>` query param selects a preset. `?since=<unix_ts>`
overrides with an explicit cutoff (useful for ad-hoc ranges).

    5h    last 5 hours
    24h   last 24 hours
    week  last 7 days
    total all rows  (default if no param is set)
"""

from __future__ import annotations

import time
from typing import Optional

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse


router = APIRouter()

# Window name → seconds. None means "no time filter".
WINDOWS: dict[str, Optional[int]] = {
    "5h":    5 * 3600,
    "24h":   24 * 3600,
    "week":  7 * 24 * 3600,
    "total": None,
}


def _resolve_since(window: Optional[str], since: Optional[float]) -> Optional[float]:
    """Return the unix-ts lower bound, or None for "all time"."""
    if since is not None:
        return since
    if window is None or window == "total":
        return None
    secs = WINDOWS.get(window)
    if secs is None:
        return None
    return time.time() - secs


@router.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"ok": True}


@router.get("/stats")
async def stats_all(
    request: Request,
    window: Optional[str] = Query(None, description="One of 5h, 24h, week, total"),
    since: Optional[float] = Query(None, description="Unix timestamp lower bound"),
) -> dict[str, object]:
    from ..models import StatsRow

    db = request.app.state.db
    eff_since = _resolve_since(window, since)
    rows = await db.aggregate(platform=None, since=eff_since)
    # Ensure both platforms are present in the response (even with zeros)
    # so consumers can render a stable schema.
    for platform in ("anthropic", "openai"):
        rows.setdefault(platform, StatsRow())
    return {
        "window": window or "total",
        "since": eff_since,
        "platforms": {k: v.model_dump() for k, v in rows.items()},
    }


@router.get("/stats/platform/{name}")
async def stats_platform(
    request: Request,
    name: str,
    window: Optional[str] = Query(None),
    since: Optional[float] = Query(None),
) -> dict[str, object]:
    if name not in {"anthropic", "openai"}:
        return {"error": f"unknown platform {name!r}"}
    db = request.app.state.db
    eff_since = _resolve_since(window, since)
    rows = await db.aggregate(platform=name, since=eff_since)
    from ..models import StatsRow

    return {
        "platform": name,
        "window": window or "total",
        "since": eff_since,
        "stats": rows.get(name, StatsRow()).model_dump(),
    }


@router.get("/windows")
async def list_windows() -> dict[str, object]:
    """List the time-window presets the relay recognises."""
    return {
        "windows": [
            {"name": name, "seconds": secs}
            for name, secs in WINDOWS.items()
        ]
    }


@router.get("/messages/search")
async def search_messages(
    request: Request,
    q: str = Query(..., min_length=1, description="Substring to match against saved message content"),
    platform: Optional[str] = Query(None, description="Filter by platform (anthropic / openai)"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, object]:
    """LIKE search over saved conversation text.

    Only enabled when `RELAY_SAVE_MESSAGES=1`; otherwise returns an empty
    list. Matched rows are most-recent first.
    """
    settings = request.app.state.settings
    if not settings.relay_save_messages:
        return {"q": q, "platform": platform, "matches": [], "disabled": True}
    db = request.app.state.db
    matches = await db.search_messages(q, limit=limit, platform=platform)
    return {"q": q, "platform": platform, "matches": matches, "disabled": False}


@router.get("/messages/by_request/{request_id}")
async def conversation_by_request(
    request: Request,
    request_id: int,
) -> dict[str, object]:
    """Return one request row + its saved messages, or an error.

    Drives the GUI's "double-click a recent row to see the full conversation"
    dialog. If `RELAY_SAVE_MESSAGES=0` the messages list will be empty but the
    request row is still returned so the user can see token counts and
    metadata.
    """
    db = request.app.state.db
    convo = await db.get_conversation(request_id)
    if convo is None:
        return {"error": "not_found", "request_id": request_id}
    return convo


@router.get("/requests")
async def list_requests(
    request: Request,
    limit: int = Query(100, ge=1, le=500, description="Page size (1..500)"),
    before_id: Optional[int] = Query(
        None,
        description="Cursor: return only rows with id < before_id. "
                    "Pass the smallest id from the previous page to fetch the next.",
    ),
) -> dict[str, object]:
    """Paginated recent-requests list for the GUI history view.

    Cursor-based on the SQLite row id (autoincrement, monotonically
    increasing per insert). Using id rather than ts avoids duplicate
    / skipped rows when many requests land in the same second.
    """
    import asyncio
    from .. import tui

    settings = request.app.state.settings
    rows = await asyncio.to_thread(
        tui.fetch_recent,
        settings.relay_db,
        limit,
        before_id,
    )
    return {
        "limit": limit,
        "before_id": before_id,
        "items": [dict(r) for r in rows],
        # Hint for the next page — None means "this page was short, no more".
        "next_before_id": rows[-1]["id"] if len(rows) == limit else None,
    }


@router.get("/live")
async def live_requests() -> dict[str, object]:
    """In-flight + recently-completed request snapshots.

    Powers the GUI / TUI "正在上传" and "模型输出" live panels — they need
    to see requests *before* the row lands in the SQLite `requests` table
    (which happens only at end of stream). State is held in process memory
    by `proxy.get_inflight_snapshot()` and lost on server restart.
    """
    from ..proxy import get_inflight_snapshot
    return {"requests": get_inflight_snapshot()}


# v0.99：统计页两个聚合接口（前端 stats 视图专用）。
# 数据源 = relay.db，已在 main.py lifespan 中挂在 app.state.db 上。
# 路由顺序：先注册具体路径（/stats/aggregate, /stats/daily），
# 再注册通用占位 /stats/{platform}（带路径参数）—— FastAPI 按
# 顺序匹配，先到先得。
@router.get("/stats/aggregate")
async def stats_aggregate(
    request: Request,
    dim: str = Query("upstream", description="聚合维度：platform | upstream | model | agent"),
    range: str = Query("30d", description="时间段预设：1d | 7d | 30d (默认 30d)"),
    top: int = Query(10, ge=0, le=100, description="返回行数上限，0 表示全部"),
) -> dict[str, object]:
    """v0.99 统计页：按 dim 维度返回时间段内的 top-N 用量聚合。

    ``range`` 预设：1d / 7d / 30d。非法值返回错误结构（HTTP 仍 200，
    前端按 ``error`` 字段提示，避免错误处理态额外引入 4xx/5xx 渲染）。
    返回 ``{rows, total, range, dim, since}``；``rows`` 是 list
    （前端直接 for 渲染表格，避免 dict 顺序假设）。
    """
    import asyncio
    from .. import tui

    preset = {"1d": 24*3600, "7d": 7*24*3600, "30d": 30*24*3600}
    if range not in preset:
        return {"error": f"invalid range {range!r}", "valid": list(preset.keys())}
    if dim not in {"platform", "upstream", "model", "agent"}:
        return {"error": f"invalid dim {dim!r}",
                "valid": ["platform", "upstream", "model", "agent"]}

    settings = request.app.state.settings
    since = time.time() - preset[range]
    # 全量取（无 top），route 自己算 total + 截前 top —— tui 不裁剪。
    rows_map = await asyncio.to_thread(
        tui.fetch_aggregate_by_dim,
        settings.relay_db,
        dim=dim,
        since=since,
    )
    # total 是"全部维度合计"，不受 top 影响 —— top 只控制表格显示前 N 行。
    total = {
        "requests": sum(r["requests"] for r in rows_map.values()),
        "input_tokens": sum(r["input_tokens"] for r in rows_map.values()),
        "output_tokens": sum(r["output_tokens"] for r in rows_map.values()),
        "cache_read_input_tokens": sum(r["cache_read_input_tokens"] for r in rows_map.values()),
        "cache_creation_input_tokens": sum(r["cache_creation_input_tokens"] for r in rows_map.values()),
        "errors": sum(r["errors"] for r in rows_map.values()),
    }
    total["total_tokens"] = (
        total["input_tokens"] + total["output_tokens"]
        + total["cache_read_input_tokens"] + total["cache_creation_input_tokens"]
    )
    # 按 total_tokens 降序截前 top 行（保持 SQL 顺序已 DESC，此处重排保险）。
    sorted_rows = sorted(
        rows_map.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True,
    )
    if top and top > 0:
        sorted_rows = sorted_rows[:top]
    return {
        "range": range,
        "dim": dim,
        "since": since,
        "top": top,
        "total": total,
        "rows": [{"key": k, **v} for k, v in sorted_rows],
    }


@router.get("/stats/daily")
async def stats_daily(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="返回最近 N 天（默认 30）"),
) -> dict[str, object]:
    """v0.99 统计页：每日一行聚合，按日期倒序；空日期 0 补齐。"""
    import asyncio
    from .. import tui

    settings = request.app.state.settings
    rows = await asyncio.to_thread(tui.fetch_daily, settings.relay_db, days=days)
    return {
        "days": days,
        "items": rows,
    }


@router.get("/stats/model_daily")
async def stats_model_daily(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="返回最近 N 天（默认 30）"),
) -> dict[str, object]:
    """v0.153 统计页：按模型 × 日的调用分布（单模型 30 天按日柱状图）。

    与 tui.fetch_model_daily 同步：``{models, days, series, items}``。
    ``models`` 每项 ``{model, total, color}``（按 total_tokens 降序，
    稳定色板）；``series`` 每模型一条 ``{date, total_tokens}`` 序列（0 补齐、
    升序）；``items`` 明细行（tooltip 用）。空 DB 返回空结构。
    """
    import asyncio
    from .. import tui

    settings = request.app.state.settings
    data = await asyncio.to_thread(tui.fetch_model_daily, settings.relay_db, days=days)
    return data


# ---------------------------------------------------------------------------
# v0.100.1 统计页（高级可视化）四个聚合端点。
#
# 设计见 docs/stats-page-design.md —— 数据源 4 个纯函数 tui.fetch_*。
# 路由顺序：先注册具体路径（/stats/*），再注册通用占位 /stats/{platform}
# （带路径参数，line 81）。FastAPI 按顺序匹配，先到先得。
# ---------------------------------------------------------------------------

# 与 routers/stats.py 顶部 _RANGE_PRESETS 同步；放这里给本节端点复用。
_RANGE_PRESETS: dict[str, int] = {
    "1d":  24 * 3600,
    "7d":  7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}


@router.get("/stats/token_breakdown")
async def stats_token_breakdown(
    request: Request,
    range: str = Query("7d", description="时间段预设：1d | 7d | 30d"),
) -> dict[str, object]:
    """v0.100.1 玫瑰图数据：全模型合计 4 种 token + 总数。

    返回 ``{range, since, input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens, total}``。
    """
    import asyncio
    from .. import tui

    if range not in _RANGE_PRESETS:
        return {"error": f"invalid range {range!r}",
                "valid": list(_RANGE_PRESETS.keys())}
    since = time.time() - _RANGE_PRESETS[range]
    settings = request.app.state.settings
    data = await asyncio.to_thread(
        tui.fetch_token_breakdown, settings.relay_db, since=since
    )
    return {"range": range, "since": since, **data}


@router.get("/stats/timeseries")
async def stats_timeseries(
    request: Request,
    range: str = Query("7d", description="时间段预设：1d | 7d | 30d"),
    bucket: str = Query("hour", description="桶宽：hour | day"),
    metric: str = Query("requests", description="求和量：requests | errors | total_tokens"),
) -> dict[str, object]:
    """v0.100.1 stacked area：按 upstream 拆色的时间序列。

    返回 ``{range, bucket, metric, since, items:[{ts, upstream, value}, ...]}``，
    items 按 (ts ASC, upstream ASC) 排序。非法 bucket/metric 返回错误结构
    （HTTP 仍 200，前端按 error 字段分支处理 —— 与 v0.99 端点风格一致）。
    """
    import asyncio
    from .. import tui

    if range not in _RANGE_PRESETS:
        return {"error": f"invalid range {range!r}",
                "valid": list(_RANGE_PRESETS.keys())}
    since = time.time() - _RANGE_PRESETS[range]
    try:
        settings = request.app.state.settings
        items = await asyncio.to_thread(
            tui.fetch_timeseries,
            settings.relay_db, since=since,
            bucket=bucket, metric=metric,
        )
    except ValueError as e:
        return {"error": str(e),
                "valid_bucket": ["hour", "day"],
                "valid_metric": ["requests", "errors", "total_tokens"]}
    return {
        "range": range,
        "bucket": bucket,
        "metric": metric,
        "since": since,
        "items": items,
    }


@router.get("/stats/route_heatmap")
async def stats_route_heatmap(
    request: Request,
    range: str = Query("7d", description="时间段预设：1d | 7d | 30d"),
) -> dict[str, object]:
    """v0.100.1 路由热力 + Sankey 数据：每上游在各平台的请求数矩阵。

    返回 ``{range, since, items:[{upstream, anthropic_n, openai_n}, ...]}``。
    前端既可铺成热力网格，也可转成 Sankey links（source=upstream,
    target=platform, value=requests）。
    """
    import asyncio
    from .. import tui

    if range not in _RANGE_PRESETS:
        return {"error": f"invalid range {range!r}",
                "valid": list(_RANGE_PRESETS.keys())}
    since = time.time() - _RANGE_PRESETS[range]
    settings = request.app.state.settings
    data = await asyncio.to_thread(
        tui.fetch_route_heatmap, settings.relay_db, since=since
    )
    rows = [
        {"upstream": ups, "anthropic_n": n["anthropic"], "openai_n": n["openai"]}
        for ups, n in data.items()
    ]
    return {"range": range, "since": since, "items": rows}


@router.get("/stats/calendar")
async def stats_calendar(
    request: Request,
    days: int = Query(365, ge=1, le=365, description="返回最近 N 天（默认 365，全年日历）"),
) -> dict[str, object]:
    """v0.100.1 日历热力数据：按日 token + requests，0 补齐到 days 天。

    返回 ``{year, days, total, items:[{date(YYYY-MM-DD), ts, requests,
    input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, errors, total_tokens}, ...]}``，items 按
    日期升序（旧→新）。``year`` 按今天所在 UTC 年算 —— 跨年时 D3 端按需
    自动切换年。
    """
    import asyncio
    from .. import tui

    settings = request.app.state.settings
    data = await asyncio.to_thread(
        tui.fetch_calendar, settings.relay_db, days=days
    )
    return data


@router.get("/stats/quota_5h")
async def stats_quota_5h(request: Request) -> dict[str, object]:
    """v0.100.1 配额仪表盘数据：每上游当前 5h 配额利用率。

    与 range 无关（5h 窗口是独立的时间语义）。从 settings 读已配置上游
    列表 + 5h 窗口内的请求，计算 used/utilization/warning_level。返回
    ``{items:[{upstream, billing_unit, quota_5h, used_5h, utilization_5h,
    warning_level, warning_text}, ...]}``。
    """
    import asyncio
    from .. import tui

    settings = request.app.state.settings
    # 拼 upstreams 列表给 tui（v0.19 / v0.99 的 by_upstream_with_costs 用同样字段）
    upstreams: list[dict[str, object]] = []
    for plat in ("anthropic", "openai"):
        for cfg in settings.upstreams_for(plat):
            upstreams.append({
                "name": cfg.name,
                "quota_5h": cfg.quota_5h or 0,
                "billing_unit": cfg.billing_unit or "count",
                "model_multipliers": cfg.model_multipliers or {},
                "allowed_models": cfg.allowed_models or [],
            })
    items = await asyncio.to_thread(
        tui.fetch_upstream_quota_5h, settings.relay_db, upstreams=upstreams
    )
    return {"items": items}


# v0.89：实时流广播 —— 给 GUI 侧栏窗口（live_panel.html）用的 SSE 端点。
# 与 /live 的轮询模型互补：本端点是 push 模型，由 proxy._broadcast 在每条
# SSE chunk / 完成事件 / done 事件时往订阅者队列里推，订阅者（=本生成器）
# 立刻把它们包装成 SSE 帧 yield 出去。
#
# 心跳设计：15s 无事件就发一行 `: keepalive\n\n`（SSE 注释）—— 防止中间
# 代理 / 客户端 socket idle 掐断，也方便 GUI 侧探活。
@router.get("/live/stream")
async def live_stream(request: Request) -> StreamingResponse:
    # Phase 2.11：从 app.state.ctx 拿服务（lifespan 已注入）。
    ctx = getattr(request.app.state, "ctx", None)
    inflight = ctx.svc("inflight") if ctx is not None else None
    if inflight is None:
        # 兼容期：ctx 未就绪（极早的 unit test / 单文件 lifespan 缺失），
        # 回退到 proxy 模块全局（旧路径）。Phase 3 整文件拆后即可删除。
        from ..proxy import (
            _subscribe_live_stream,
            _unsubscribe_live_stream,
            get_inflight_snapshot,
        )
        queue = _subscribe_live_stream()
        snapshot_fn = lambda: get_inflight_snapshot()
        cleartext_fn = lambda rid: _legacy_cleartext(rid)
        unsubscribe_fn = lambda q: _unsubscribe_live_stream(q)  # async; called with await below
    else:
        queue = inflight.subscribe()
        snapshot_fn = lambda: inflight.snapshot()
        cleartext_fn = lambda rid: inflight.get_with_cleartext(rid)
        unsubscribe_fn = lambda q: inflight.unsubscribe(q)  # async; called with await below

    async def gen() -> AsyncIterator[bytes]:
        try:
            # 首连先推一条 snapshot 事件 —— 侧栏刚开 / 切到新页时如果正好
            # 没有任何请求在飞，先看到当前最近的 inflight（如果有）就比
            # 一片空白要友好。snapshot 事件不含 request_id，前端按需消费。
            # v0.94：不再跳过 phase=done 的条目 —— 侧栏也展示最近一次
            # **已完成**的请求，上游/模型/api-key/wire 没有"in-flight 才
            # 能展示"的理由。break 只取最近一条，避免重复。
            for snap in snapshot_fn():
                # v0.95：snapshot 也带 api_key_cleartext（明文）—— 让
                # 侧栏首连时不需要再调 pywebview 桥拿明文 key（GUI 进程
                # inflight 永远为空，桥永远返回 ""，侧栏一直显示「（无）」）。
                # SSE loopback 不扩大攻击面。cleartext 字段由 InflightStore
                # 直接读 _InFlight.api_key。
                rid = snap.get("request_id")
                inf = cleartext_fn(rid) if rid else None
                if inf is not None:
                    cleartext = getattr(inf, "api_key", "") or ""
                    if cleartext:
                        snap = {**snap, "api_key_cleartext": cleartext}
                payload = json.dumps(
                    {"type": "snapshot", **snap}, ensure_ascii=False
                )
                yield f"data: {payload}\n\n".encode("utf-8")
                break                  # 只取最近一条 —— 侧栏只展示"最近一次"
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # SSE 注释行 —— 标准 keepalive 形态，前端 JS EventSource
                    # 不会触发 onmessage（注释行不在 onmessage 通道里）。
                    yield b": keepalive\n\n"
                    continue
                payload = json.dumps(ev, ensure_ascii=False, default=str)
                yield f"data: {payload}\n\n".encode("utf-8")
        finally:
            # 客户端断开（SSE GeneratorExit） / 服务端异常 / Ctrl-C 都走这里。
            # 必须摘掉订阅，否则队列堆积 + 引用泄漏，最终可能把内存吃爆。
            await unsubscribe_fn(queue)

    response_headers = {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache",
        # 防 nginx / 中间代理 / EdgeChromium 把 SSE 缓冲掉。proxy.py:658-664
        # 也用了同一组 header。
        "x-accel-buffering": "no",
    }
    return StreamingResponse(
        gen(),
        headers=response_headers,
        media_type="text/event-stream",
    )


def _legacy_cleartext(rid: str):
    """Phase 2.11 兼容：当 ctx 未注入时，fallback 到 proxy._in_flight/_in_flight_done。"""
    from ..proxy import _in_flight, _in_flight_done
    if rid in _in_flight:
        return _in_flight[rid]
    for inf in _in_flight_done:
        if inf.request_id == rid:
            return inf
    return None
