"""Background 5h-quota watch: warn, and optionally switch away (v0.20).

The relay already knows, per upstream, how much weighted quota has been
burned in the trailing 5h window (``tui.fetch_by_upstream_with_costs``).
That number is only useful if someone is looking at the GUI, though —
this module is the part that watches it while nobody is looking.

Every ``relay_quota_check_interval`` seconds it evaluates each platform's
*active* upstream and:

  * logs a warning when utilization crosses warn / critical / exhausted,
    once per level change rather than once per tick (a 60s log spam for
    five hours helps nobody);
  * when ``relay_quota_autoswitch`` is on and utilization is at or above
    ``relay_quota_switch_at``, switches to the least-utilized sibling
    that has headroom, and persists the choice so a restart keeps it.

Design notes:

  * The DB read is synchronous SQLite. It runs in a thread executor so a
    slow disk can't stall the event loop serving proxied requests.
  * A failure here must never take the relay down: the loop catches
    everything per-iteration and keeps going. A quota monitor that
    crashes the proxy would be far worse than one that misses a warning.
  * Switching only ever considers siblings *within the same platform* —
    an anthropic upstream is never swapped for an openai one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .config import PLATFORMS, Settings
from .plugin import emit_event, run_hooks
from .tui import fetch_by_upstream_with_costs


log = logging.getLogger("relay.quota")

# Levels in ascending severity, used to decide whether a transition is
# worth logging (we announce escalations, and the drop back to "ok").
_LEVEL_ORDER = {"no_quota": -1, "ok": 0, "warn": 1, "critical": 2, "exhausted": 3}


def _cfg_dicts(settings: Settings, platform: str) -> list[dict[str, Any]]:
    """PlatformConfig-shaped dicts, the input contract of
    ``fetch_by_upstream_with_costs`` (which stays Settings-free)."""
    return [
        {
            "name": c.name,
            "quota_5h": c.quota_5h,
            "model_multipliers": dict(c.model_multipliers),
            "allowed_models": list(c.allowed_models),
            "billing_unit": c.billing_unit,
            "token_fields": dict(c.token_fields) if c.token_fields else {},
        }
        for c in settings.upstreams_for(platform)
    ]


def pick_replacement(
    costs: dict[str, dict[str, Any]],
    candidates: list[str],
    current: str,
    *,
    threshold: float,
) -> Optional[str]:
    """The best upstream to move to, or None to stay put.

    "Best" is the lowest utilization among siblings below ``threshold``.
    An upstream with no quota configured counts as fully available — the
    user declined to cap it, so we take them at their word. Returns None
    when nothing is better than where we already are, which includes the
    everything-is-exhausted case: thrashing between two dead upstreams
    only multiplies the failures.
    """
    best: Optional[str] = None
    best_util = float("inf")
    for name in candidates:
        if name == current:
            continue
        util = costs.get(name, {}).get("utilization_5h")
        u = 0.0 if util is None else float(util)
        if u >= threshold:
            continue
        if u < best_util:
            best, best_util = name, u
    return best


async def _check_once(app, settings: Settings, last_level: dict[str, str]) -> None:
    """One evaluation pass over every platform. Mutates ``last_level``."""
    for platform in PLATFORMS:
        cfgs = _cfg_dicts(settings, platform)
        if not cfgs:
            continue
        active = settings.active_for(platform)
        costs = await asyncio.to_thread(
            fetch_by_upstream_with_costs, settings.relay_db, upstreams=cfgs
        )
        row = costs.get(active)
        if not row:
            continue

        level = str(row.get("warning_level") or "no_quota")
        key = f"{platform}/{active}"
        if level != last_level.get(key) and _LEVEL_ORDER.get(level, 0) > 0:
            log.warning(
                "quota %s on %s/%s: %s/%s used (%s), 预计 %s 用尽",
                level, platform, active,
                round(float(row.get("used_5h") or 0)),
                row.get("quota_5h"),
                row.get("warning_text"),
                row.get("exhaustion_eta_text"),
            )
        elif level == "ok" and _LEVEL_ORDER.get(last_level.get(key, "ok"), 0) > 0:
            log.info("quota recovered on %s/%s", platform, active)
            emit_event(
                "quota.recovered",
                platform=platform, upstream=active,
                utilization_pct=float(row.get("utilization_5h") or 0) * 100,
            )
        last_level[key] = level

        if not settings.relay_quota_autoswitch:
            continue
        util = row.get("utilization_5h")
        if util is None or float(util) < settings.relay_quota_switch_at:
            continue
        # v0.11.3: 只允许在用户勾选的池子里切换目标。空池 = 全部兄弟
        # 候选（旧行为）。当前激活的上游被排除在池外时也要能切走，所
        # 以这里只过滤"目标"，当前 active 始终允许离开。
        candidates = [c["name"] for c in cfgs]
        pool = list(getattr(settings, "relay_autoswitch_pool", None) or [])
        if pool:
            candidates = [n for n in candidates if n in pool]
        target = pick_replacement(
            costs,
            candidates,
            active,
            threshold=settings.relay_quota_switch_at,
        )
        if target is None:
            log.warning(
                "quota on %s/%s is at %.0f%% but no sibling has headroom; staying put",
                platform, active, float(util) * 100,
            )
            continue
        # v0.98.2 决策钩子：decide_quota_switch —— 插件返回 False 即
        # 否决本次自动切换。
        decide_info = {
            "platform": platform,
            "from_upstream": active,
            "to_upstream": target,
            "utilization_pct": float(util) * 100,
        }
        if await run_hooks("decide_quota_switch", decide_info) is False:
            continue
        await _switch(app, settings, platform, target, active, float(util))


async def _switch(
    app, settings: Settings, platform: str, target: str, previous: str, util: float
) -> None:
    """Repoint the platform at ``target`` and persist it, best-effort."""
    try:
        settings.set_active(platform, target)
    except KeyError as exc:
        log.error("quota autoswitch on %s failed: %s", platform, exc)
        emit_event(
            "quota.autoswitch",
            platform=platform, from_upstream=previous, to_upstream=target,
            reason=f"switch_failed: {exc}", utilization_pct=util * 100,
            ok=False, error=str(exc),
        )
        return
    log.warning(
        "quota autoswitch: %s %s → %s (was at %.0f%% of its 5h quota)",
        platform, previous, target, util * 100,
    )
    emit_event(
        "quota.autoswitch",
        platform=platform, from_upstream=previous, to_upstream=target,
        reason="utilization_threshold", utilization_pct=util * 100,
        ok=True, error=None,
    )
    try:
        # Phase 4：ctx 存在时走 SettingsMutator.persist_active（单一路径）；
        # test / 无 ctx 时回退旧 _persist_active（模块全局，test monkey-patch 命中）。
        ctx = getattr(app.state, "ctx", None)
        if ctx is not None and "mutator" in ctx:
            await asyncio.to_thread(
                ctx.svc("mutator").persist_active, settings, platform, target
            )
        else:
            from .routers.api import _persist_active

            await asyncio.to_thread(_persist_active, settings, platform, target)
    except OSError as exc:
        # In-memory switch already took effect; it just won't survive a
        # restart. Worth a log, not worth reverting.
        log.warning("could not persist quota autoswitch for %s: %s", platform, exc)


async def run_monitor(app) -> None:
    """The background loop. Cancelled on shutdown by ``start``'s task."""
    settings: Settings = app.state.settings
    interval = max(1, int(settings.relay_quota_check_interval))
    last_level: dict[str, str] = {}
    log.info(
        "quota monitor started: every %ds, autoswitch=%s at %.0f%%",
        interval, settings.relay_quota_autoswitch,
        settings.relay_quota_switch_at * 100,
    )
    while True:
        try:
            await asyncio.sleep(interval)
            # Re-read from app.state each tick: a PUT to the quota API
            # rebinds it, and we want the new limits without a restart.
            await _check_once(app, app.state.settings, last_level)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("quota monitor iteration failed; continuing")


def start(app) -> Optional[asyncio.Task]:
    """Spawn the monitor unless disabled. Returns the task, or None."""
    settings: Settings = app.state.settings
    if int(settings.relay_quota_check_interval) <= 0:
        log.info("quota monitor disabled (relay_quota_check_interval <= 0)")
        return None
    return asyncio.create_task(run_monitor(app), name="quota-monitor")
