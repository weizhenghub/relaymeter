"""Terminal UI dashboard — live token consumption viewer.

Reads SQLite directly so it works without the relay server running. Auto-refreshes
every 2s. Press `q` to quit, `r` to force-refresh.

Usage:
    relay-dashboard                     # uses ./relay.db
    relay-dashboard --db /path/to/db    # explicit DB path
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

from .plugin import billing_unit_for
from .agent import AGENT_UNKNOWN


DEFAULT_DB = "./relay.db"
DEFAULT_RELAY_URL = "http://127.0.0.1:8088"
PLATFORMS = ("anthropic", "openai")


# ---------------------------------------------------------------------------
# DB read helpers (sync sqlite3 — read-only is fast and avoids aiosqlite setup)
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# v0.119：上游链接（合并统计）SQL helper。
#
# 虚拟 cfg 含 ``linked_names`` 字段时，SQL 要把 ``WHERE upstream = ?``
# 改为 ``WHERE upstream IN (?,?,?)``。本 helper 把这两条路径统一掉，
# 上游 fetcher 只调一次就拿到「该 cfg 应该查哪些真实 upstream + key 用
# 哪个字符串写出」两件套。
# ---------------------------------------------------------------------------


def _resolve_names(cfg: dict[str, object]) -> list[str]:
    """cfg → 要查的真实 upstream 名列表。

    真实 cfg：[cfg["name"]]。
    虚拟 cfg：cfg["linked_names"]（构造时按字典序排好）。
    兜底（缺字段）：[cfg["name"]] —— 旧 cfg dict 缺 _is_virtual 时仍能跑。
    """
    if cfg.get("_is_virtual") and isinstance(cfg.get("linked_names"), list):
        return [n for n in cfg["linked_names"] if isinstance(n, str) and n]
    name = cfg.get("name")
    return [name] if isinstance(name, str) and name else []


def _where_for_cfg(cfg: dict[str, object], column: str = "upstream") -> tuple[str, list[object]]:
    """生成「单 cfg 的 WHERE 子句 + 参数」。

    返回 ``(where_clause, params)``：
      真实 cfg: ``(f"{column} = ?", [cfg.name])``
      虚拟 cfg: ``(f"{column} IN (?,?,?)", [name1, name2, name3])``

    caller 自己拼到完整 SQL：``SELECT ... WHERE ts >= ? AND {where_clause}``。
    """
    names = _resolve_names(cfg)
    if not names:
        # 极端兜底（cfg 没 name），返回永假条件，避免意外扫全表
        return "1 = 0", []
    placeholders = ",".join("?" for _ in names)
    return f"{column} IN ({placeholders})", names


def fetch_totals(db_path: str, since: float | None = None) -> dict[str, dict[str, int]]:
    """Per-platform aggregate within a time window. `since` is unix-ts;
    `None` means "all time".

    v0.143 + 修平台分布卡片噪音：「平台分布」卡上始终只展示 v0.143 拆分
    后的 3 种协议端点（除非某 platform 在窗口内无任何调用）：

    - ``anthropic`` —— 一个 key，anthropic 永远单行（endpoint NULL /
      'anthropic-messages' 都折叠进 anthropic，不在卡上暴露 endpoint）。
    - ``openai:openai-chat`` —— OpenAI chat 端点。
    - ``openai:openai-responses`` —— OpenAI Responses API 端点。

    三种协议端点 永远是平台的「全集合」：DB 里没数据的端点会按 0 占位
    出现在 out（用户能看到「openai·responses 这一行目前 0」），而不是
    消失。这样切换 view / 重启 UI 时卡片结构稳定，不会突然从 2 行变
    3 行让用户误以为出了什么问题。

    其他 platform 字符串（``"openclaw"`` / ``"opendaw"`` 这类历史遗留
    拼写错乱）一律丢弃 —— 不污染平台分布卡。要看完整平台分布请走
    ``fetch_by_upstream`` 这种全列 GROUP BY 的入口。
    """
    # 三种协议端点 固定的 key 集合 —— 即便 DB 里没有 openai-responses 行，
    # 也保留这一行 0 占位（用户视觉一致、且后续真有 Responses API 接入
    # 时数字自然累加，不用改 key 列表）。
    EMPTY = {
        "anthropic",
        "openai:openai-chat",
        "openai:openai-responses",
    }
    out: dict[str, dict[str, int]] = {}
    if not Path(db_path).exists():
        return {k: {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "errors": 0}
                for k in EMPTY}
    where = "WHERE ts >= ?" if since is not None else ""
    params: tuple = (since,) if since is not None else ()
    with _connect(db_path) as c:
        for row in c.execute(
            f"""
            SELECT platform,
                   COALESCE(NULLIF(endpoint, ''), platform) AS endpoint,
                   COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                   COALESCE(SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errors
            FROM requests {where} GROUP BY platform, endpoint
            """,
            params,
        ):
            p = str(row["platform"])
            ep = str(row["endpoint"])
            # 未知 platform（openclaw / opendaw / 拼写错乱等）直接丢。
            if p not in PLATFORMS:
                continue
            if p == "anthropic":
                # 所有 anthropic 行（无论 endpoint）折叠到 'anthropic'。
                key = "anthropic"
            else:
                key = f"{p}:{ep}" if ep and ep != p else p
            cur = out.get(key)
            if cur is None:
                out[key] = dict(row)
            else:
                for f in ("requests", "input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens", "errors"):
                    cur[f] = (cur.get(f) or 0) + int(row[f] or 0)
    # 补齐 EMPTY 里缺失的 key —— 0 占位，保证三种协议端点 永远都在。
    for k in EMPTY:
        if k not in out:
            out[k] = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "errors": 0,
                      "platform": k.split(":", 1)[0], "endpoint": k.split(":", 1)[-1]}
    return out


def fetch_by_model(db_path: str, since: float | None = None) -> dict[str, dict[str, int]]:
    """Per-model aggregate within a time window. Cache read + cache write are
    summed into one `cache_tokens` bucket so the GUI can render a single bar
    segment for them.
    """
    out: dict[str, dict[str, int]] = {}
    if not Path(db_path).exists():
        return out
    where = "WHERE model IS NOT NULL AND model != ''"
    params: tuple = ()
    if since is not None:
        where += " AND ts >= ?"
        params = (since,)
    with _connect(db_path) as c:
        for row in c.execute(
            f"""
            SELECT model,
                   COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens
            FROM requests {where}
            GROUP BY model
            ORDER BY (input_tokens + output_tokens + cache_read_input_tokens + cache_creation_input_tokens) DESC
            """,
            params,
        ):
            cr = row["cache_read_input_tokens"]
            cw = row["cache_creation_input_tokens"]
            out[row["model"]] = {
                "requests": row["requests"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_tokens": cr + cw,
            }
    return out


def fetch_agent_totals(db_path: str, since: float | None = None) -> dict[str, dict[str, int]]:
    """Per-agent (client tool) aggregate within a time window.

    `agent` is the client-tool name written by the proxy from User-Agent
    (see `src/relay/agent.py`). Unrecognised / legacy rows (NULL agent) are
    bucketed under AGENT_UNKNOWN so their traffic is still visible as one
    group rather than silently dropped.
    """
    out: dict[str, dict[str, int]] = {}
    if not Path(db_path).exists():
        return out
    where = "WHERE ts >= ?" if since is not None else ""
    params: tuple = (since,) if since is not None else ()
    with _connect(db_path) as c:
        for row in c.execute(
            f"""
            SELECT COALESCE(NULLIF(agent, ''), ?) AS agent,
                   COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                   COALESCE(SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errors
            FROM requests {where}
            GROUP BY COALESCE(NULLIF(agent, ''), ?)
            ORDER BY (input_tokens + output_tokens + cache_read_input_tokens + cache_creation_input_tokens) DESC
            """,
            (AGENT_UNKNOWN, AGENT_UNKNOWN, *params),
        ):
            cr = row["cache_read_input_tokens"]
            cw = row["cache_creation_input_tokens"]
            out[row["agent"]] = {
                "requests": row["requests"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
                "errors": row["errors"],
            }
    return out


def fetch_recent_uas(db_path: str, limit: int = 100) -> list[dict[str, object]]:
    """Most recently seen raw User-Agent strings + their last-seen time.

    v0.157：设置页「UA 归类」的数据源 —— 列出真实出现过的 UA，让用户
    逐条配置 ua_rules（整串 UA → 平台名）。只返回有 raw_ua 的行（迁移
    前旧行 NULL 跳过），按最后一次出现时间倒序。返回形如
    ``[{"ua": "...", "last_ts": 123.0, "count": 3}, ...]``。
    """
    if not Path(db_path).exists():
        return []
    with _connect(db_path) as c:
        rows = c.execute(
            """
            SELECT raw_ua AS ua,
                   MAX(ts) AS last_ts,
                   COUNT(*) AS count
            FROM requests
            WHERE raw_ua IS NOT NULL AND raw_ua != ''
            GROUP BY raw_ua
            ORDER BY last_ts DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [{"ua": r["ua"], "last_ts": r["last_ts"], "count": r["count"]} for r in rows]


# Time windows used for per-upstream request-count display in the GUI's
# upstream selector. Only request counts (no token counts) — the user picks
# an upstream by name, so the signal that matters is "how much traffic has
# gone through this upstream recently".
UPSTREAM_WINDOWS: dict[str, int] = {
    "5h":    5 * 3600,
    "week":  7 * 24 * 3600,
    "month": 30 * 24 * 3600,
}


def fetch_by_upstream(
    db_path: str,
    *,
    upstreams: list[str] | tuple[str, ...],
) -> dict[str, dict[str, object]]:
    """Per-upstream request counts over UPSTREAM_WINDOWS plus the 5h-release
    countdown for the GUI stats panel.

    Single SQL pass. For each window a ``SUM(CASE WHEN ts >= ? THEN 1 END)``
    gives the count; one extra ``MIN(CASE WHEN ts >= ? THEN ts END)`` for
    the 5h window gives the oldest in-5h timestamp so the caller can show
    "how long until the 5h count ticks down by one". MIN ignores NULLs
    so groups with no in-window rows just leave that column NULL — the
    caller renders "—". SUM cutoffs are bound first, then the MIN cutoff,
    then the upstream names; both SUM and MIN share the same `now - 5h`
    value, just kept in separate slots so the placeholder order matches.

    Returns
    -------
    {upstream_name: {"counts":      {window: n},
                     "5h_release":  seconds_until_release_or_None,
                     "last_ts":     last_request_ts_or_None}}
    where ``5h_release`` is ``min_ts_5h + 5h - now`` — i.e. how long until
    the OLDEST in-5h request falls out. ``None`` means there are no in-5h
    rows. ``last_ts`` is the all-time row this upsteam — used by the GUI's
    auto-sort (active first, then by last call time, then by total tokens).
    Configured upstreams with zero rows are zero-filled so callers
    never have to handle missing keys.

    `upstreams` should be the currently-configured upstream names — rows
    whose `upstream` is no longer in that list are silently dropped, so we
    never surface stale counts for retired configs.
    """
    names = list(upstreams)
    empty: dict[str, dict[str, object]] = {
        n: {
            "counts": {k: 0 for k in UPSTREAM_WINDOWS},
            "5h_release": None,
            "last_ts": None,
        }
        for n in names
    }
    if not names or not Path(db_path).exists():
        return empty

    placeholders = ",".join("?" for _ in names)
    now = time.time()
    # We only need MIN(ts) for the 5h window — week/month release times
    # weren't useful at week/month scale (sub-day resolution doesn't help
    # for a 7-day or 30-day window). Compute the others as counts only.
    case_clauses: list[str] = []
    case_cutoffs: list[float] = []
    for win_name, secs in UPSTREAM_WINDOWS.items():
        # SQL alias can't start with a digit ("5h"), prefix with `cnt_`.
        case_clauses.append(
            f"SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS cnt_{win_name}"
        )
        case_cutoffs.append(now - secs)
    cutoff_5h = case_cutoffs[0]  # first window in UPSTREAM_WINDOWS is "5h"
    # SQL placeholder order: all SUMs, then MIN, then IN-list.
    params: list[object] = [*case_cutoffs, cutoff_5h, *names]

    sql = (
        f"SELECT upstream, {', '.join(case_clauses)}, "
        f"MIN(CASE WHEN ts >= ? THEN ts END) AS min_ts_5h, "
        f"MAX(ts) AS last_ts "
        f"FROM requests "
        f"WHERE upstream IS NOT NULL AND upstream != '' "
        f"  AND upstream IN ({placeholders}) "
        f"GROUP BY upstream"
    )

    out: dict[str, dict[str, object]] = {}
    with _connect(db_path) as c:
        for row in c.execute(sql, params):
            ups = row["upstream"]
            counts = {
                win_name: (row[f"cnt_{win_name}"] or 0)
                for win_name in UPSTREAM_WINDOWS
            }
            min_ts = row["min_ts_5h"]
            if min_ts is None:
                five_h_release: object = None
            else:
                # Time at which the oldest in-5h row will fall out.
                # Clamp at 0 in case of clock skew (min_ts somehow in the
                # future — shouldn't happen, but don't render negatives).
                five_h_release = max(0.0, (min_ts + UPSTREAM_WINDOWS["5h"]) - now)
            out[ups] = {
                "counts": counts,
                "5h_release": five_h_release,
                "last_ts": row["last_ts"],
            }
    # Zero-fill any configured upstream that had zero rows in the window.
    for n in names:
        out.setdefault(n, empty[n])
    return out


# ---- v0.19 weighted cost / quota ---------------------------------------
# Per-upstream weighted 5h usage, utilization, warning level, and ETA to
# exhaustion. Used by the GUI Settings view to surface quota on each
# upstream card and by the upstreams view to render utilization bars.
# The weighted cost model is: each request's cost = the upstream's
# configured multiplier for the request's model (or 1.0 if the model is
# not in the multiplier map / model is null). The 5h quota is in those
# weighted units — so a 1500 quota with m3 @ 3× lets through ~500 m3
# calls before exhaustion.

# Warning thresholds (utilization fraction). Hard-coded for v0.19; if
# needed later they can be made per-upstream settings.
_QUOTA_WARN = 0.7
_QUOTA_CRITICAL = 0.9
_QUOTA_EXHAUSTED = 1.0


def _classify_warning(utilization: float | None) -> str:
    """Map a utilization fraction to one of the warning levels the GUI
    renders. ``None`` means "no quota configured" — caller should show
    ``no_quota`` and skip the bar entirely."""
    if utilization is None:
        return "no_quota"
    if utilization >= _QUOTA_EXHAUSTED:
        return "exhausted"
    if utilization >= _QUOTA_CRITICAL:
        return "critical"
    if utilization >= _QUOTA_WARN:
        return "warn"
    return "ok"


def _format_eta(seconds: float | None) -> str:
    """Render an ETA in seconds as a short string ("3.2h", "47m", "now",
    "—"). Mirrors ``_format_release`` styling so the upstreams view
    stays consistent."""
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = seconds / 3600
    if hours < 48:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


# Default token field flags — applied when billing_unit == "token" and
# token_fields is absent from a upstream's config (backwards compat).
# Promoted to module scope (was nested in fetch_by_upstream_with_costs)
# so fetch_by_upstream_model can share the same weighting logic instead
# of re-implementing it.
_TOKEN_DEFAULTS = {
    "input_tokens": True,
    "output_tokens": True,
    "cache_read_input_tokens": False,
    "cache_creation_input_tokens": False,
}


def _token_cost(
    tok_in: int | None, tok_out: int | None,
    tok_cr: int | None, tok_cc: int | None,
    token_fields: dict[str, bool] | None,
) -> float:
    flags = token_fields if token_fields else _TOKEN_DEFAULTS
    return sum(
        v for k, v in [
            ("input_tokens", tok_in or 0),
            ("output_tokens", tok_out or 0),
            ("cache_read_input_tokens", tok_cr or 0),
            ("cache_creation_input_tokens", tok_cc or 0),
        ]
        if flags.get(k, False)
    )


def fetch_total_tokens_by_upstream(
    db_path: str,
    *,
    upstreams: list[str] | list[dict[str, object]],
) -> dict[str, int]:
    """All-time (no window) summed token count per upstream.

    Used by the "上游状态" card for ``billing_unit == "token"`` upstreams:
    those don't have a meaningful 5h/week/month request-count display (a
    single request can cost 100 tokens or 100k), so the card shows one
    running total instead. Deliberately unbounded by time — unlike
    ``fetch_by_upstream_with_costs``'s ``used_5h``, this number never
    drops as old rows age out of a window, since the point is "how much
    have I burned on this key overall", not a quota countdown.

    v0.119/v0.120：上游 cfg dict 含 ``linked_names``（虚拟 cfg）时，SQL
    把虚拟名展开成成员名列表（虚拟名不会出现在 requests.upstream 列）。
    结果仍按原 cfg.name（虚拟/真实名）作为 key —— 虚拟 cfg 的 token 数
    是该组所有成员 token 之和。

    Returns ``{upstream_name: total_tokens}`` (input + output + cache
    read + cache write, unconditionally — token_fields toggles only
    affect quota-weighted billing, not this raw display total).
    Configured upstreams with zero rows are zero-filled.
    """
    cfg_by_key: dict[str, dict[str, object]] = {}
    for u in upstreams:
        if isinstance(u, dict):
            name = u.get("name")
            if isinstance(name, str) and name:
                cfg_by_key[name] = u
        elif isinstance(u, str):
            cfg_by_key.setdefault(u, {})
    # 把每个 cfg 展开成要查的真实 upstream 名（虚拟 cfg → 成员列表）。
    real_names: list[str] = []
    seen: set[str] = set()
    for name, cfg in cfg_by_key.items():
        for rn in _resolve_names(cfg) if cfg else [name]:
            if rn and rn not in seen:
                seen.add(rn)
                real_names.append(rn)
    empty: dict[str, int] = {n: 0 for n in cfg_by_key}
    if not real_names or not Path(db_path).exists():
        return empty
    placeholders = ",".join("?" for _ in real_names)
    sql = (
        f"SELECT upstream, "
        f"COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) "
        f"+ COALESCE(SUM(cache_read_input_tokens), 0) "
        f"+ COALESCE(SUM(cache_creation_input_tokens), 0) AS total "
        f"FROM requests "
        f"WHERE upstream IN ({placeholders}) AND upstream IS NOT NULL AND upstream != '' "
        f"GROUP BY upstream"
    )
    # 先把 SQL 结果按真实 upstream 聚合
    real_totals: dict[str, int] = {}
    with _connect(db_path) as c:
        for upstream, total in c.execute(sql, real_names):
            real_totals[upstream] = int(total or 0)
    # 把真实 totals 按 cfg 反向聚合回 cfg name —— 虚拟 cfg = 成员之和
    for name, cfg in cfg_by_key.items():
        if cfg.get("_is_virtual") and isinstance(cfg.get("linked_names"), list):
            empty[name] = sum(real_totals.get(m, 0) for m in cfg["linked_names"] if isinstance(m, str))
        else:
            empty[name] = real_totals.get(name, 0)
    return empty


def fetch_by_upstream_with_costs(
    db_path: str,
    *,
    upstreams: list[dict[str, object]],
    window_seconds: int = 5 * 3600,
) -> dict[str, dict[str, object]]:
    """Per-upstream weighted 5h cost + utilization + ETA, layered on top
    of ``fetch_by_upstream``'s counts shape.

    Parameters
    ----------
    upstreams : list of dicts
        Each entry is a PlatformConfig-shaped dict with at least
        ``{"name": str, "quota_5h": int|None, "model_multipliers":
        dict, "allowed_models": list}``. Pass ``self.settings.upstreams_for(p)
        for p in PLATFORMS`` and let the caller flatten — keeps this
        function pure (no Settings dependency).
    window_seconds : int
        Quota window length. Default 5h. Mirrors UPSTREAM_WINDOWS["5h"].

    Returns
    -------
    {name: {...existing by_upstream fields...,
            "quota_5h":            int|None,
            "used_5h":             float,        # weighted cost in window
            "remaining_5h":        float|None,
            "utilization_5h":      float|None,   # 0-1+, None if no quota
            "burn_rate_per_hour":  float,
            "exhaustion_eta":      float|None,   # unix ts, None if no ETA
            "exhaustion_eta_text": "3.2h" | "now" | "—",
            "warning_level":       "ok"|"warn"|"critical"|"exhausted"|"no_quota",
            "warning_text":        "已用 29%" | "已用尽" | "—",
            "model_breakdown":     {model: float, ...},  # weighted cost per model
            "allowed_models":      list[str],
            "unauthorized_seen":   list[str],    # models used but not in allow list
            }}
    Configured upstreams with zero rows are zero-filled so the GUI never
    has to handle missing keys (same contract as ``fetch_by_upstream``).
    """
    now = time.time()
    cutoff = now - window_seconds
    # v0.119：每个 cfg 的 key = cfg["name"]（虚拟 cfg 是 "A<->B"），与
    # fetch_by_upstream 输出一致；这里直接把虚拟 cfg.name 透传给下游。
    cfg_keys = [u["name"] for u in upstreams]
    cfg_by_key = {u["name"]: u for u in upstreams}
    # 真实 upstream 名白名单（去重；供下游 SQL IN (...) 用）
    real_names: list[str] = []
    seen_real: set[str] = set()
    for cfg in upstreams:
        for rn in _resolve_names(cfg):
            if rn not in seen_real:
                seen_real.add(rn)
                real_names.append(rn)

    # Build the base shape by delegating to fetch_by_upstream — keeps
    # the counts / 5h_release contract intact and adds cost on top.
    base = fetch_by_upstream(db_path, upstreams=real_names)

    out: dict[str, dict[str, object]] = {}
    for n in cfg_keys:
        cfg = cfg_by_key[n]
        quota = cfg.get("quota_5h")
        multipliers = cfg.get("model_multipliers") or {}
        allowed = cfg.get("allowed_models") or []

        # Empty defaults — matches the "configured but no traffic" case
        # so the GUI doesn't have to special-case null fields.
        # v0.119 虚拟 cfg：base 不再有「精确 key」可查（fetch_by_upstream
        # 只输出真实 name），用合并成员的 zero-fill 兜底。
        empty_base = {
            "counts": {k: 0 for k in UPSTREAM_WINDOWS},
            "5h_release": None,
        }
        if cfg.get("_is_virtual"):
            # 虚拟 cfg 用成员 5h_release 中最早那个（保守：给最长释放时间）
            members = _resolve_names(cfg)
            best = empty_base
            for m in members:
                mb = base.get(m)
                if mb is None:
                    continue
                if best is empty_base or (mb.get("5h_release") is not None and (best.get("5h_release") is None or mb["5h_release"] < best["5h_release"])):
                    best = mb
            base_for_key = best
        else:
            base_for_key = base.get(n, empty_base)

        row: dict[str, object] = {
            **base_for_key,
            "quota_5h": quota,
            "used_5h": 0.0,
            "remaining_5h": None if quota is None else max(0.0, quota),
            "utilization_5h": None if quota is None else 0.0,
            "burn_rate_per_hour": 0.0,
            "exhaustion_eta": None,
            "exhaustion_eta_text": "—",
            "warning_level": "no_quota" if quota is None else "ok",
            "warning_text": "—",
            "model_breakdown": {},
            "allowed_models": list(allowed),
            "unauthorized_seen": [],
            # v0.66: forward billing_unit so GUI can render unit label
            "billing_unit": cfg.get("billing_unit", "count"),
            "token_fields": dict(cfg.get("token_fields") or {}),
        }
        # v0.119：把虚拟标志传给前端，UI 渲染「链接」chip
        if cfg.get("_is_virtual"):
            row["_is_virtual"] = True
            row["linked_names"] = list(cfg.get("linked_names") or [])
            row["_primary"] = cfg.get("_primary") or ""
        out[n] = row

    if not real_names or not Path(db_path).exists():
        return out

    # v0.119：单 SQL pass 拉所有真实 upstream 的 (upstream, model) 窗口内
    # 计数。虚拟 cfg 的合并在 Python 侧完成 —— 把同一 cfg 的多个真实成员
    # 行的 raw count / weighted 累加，再 roll-up 到虚拟 cfg.name。
    placeholders = ",".join("?" for _ in real_names)
    # v0.66: add 4 SUM columns for token billing mode.
    sql = (
        f"SELECT upstream, COALESCE(model, '') AS model, COUNT(*) AS n, "
        f"SUM(input_tokens) AS tok_in, SUM(output_tokens) AS tok_out, "
        f"SUM(cache_read_input_tokens) AS tok_cr, "
        f"SUM(cache_creation_input_tokens) AS tok_cc "
        f"FROM requests "
        f"WHERE ts >= ? AND upstream IN ({placeholders}) "
        f"  AND upstream IS NOT NULL AND upstream != '' "
        f"GROUP BY upstream, model"
    )
    per_upstream_per_model: dict[str, dict[str, tuple[int, float]]] = {}
    # value = (raw_count, weighted_count)
    # _TOKEN_DEFAULTS / _token_cost are module-level (shared with
    # fetch_by_upstream_model) — see definitions above.

    # v0.119：real_upstream → cfg_key 映射。真实 cfg 一对一；虚拟 cfg 把
    # 多个真实 upstream 映射到同一个虚拟 cfg.name。
    real_to_cfg_key: dict[str, str] = {}
    for cfg in upstreams:
        ck = cfg["name"]
        for rn in _resolve_names(cfg):
            # 多个虚拟 cfg 不会共享同一个真实 upstream（union-find 性质）
            real_to_cfg_key[rn] = ck

    with _connect(db_path) as c:
        for upstream, model, n, tok_in, tok_out, tok_cr, tok_cc in c.execute(sql, [cutoff, *real_names]):
            cfg = cfg_by_key.get(real_to_cfg_key.get(upstream, upstream), {})
            mults = cfg.get("model_multipliers") or {}
            mult = float(mults.get(model, 1.0)) if model else 1.0
            billing_unit = cfg.get("billing_unit", "count")
            if billing_unit == "token":
                token_fields = cfg.get("token_fields")
                base_cost = _token_cost(tok_in, tok_out, tok_cr, tok_cc, token_fields)
            else:
                # v0.98.3 扩展层：插件计费器（billing_unit = 插件名）。
                unit = billing_unit_for(billing_unit) if billing_unit != "count" else None
                if unit is not None:
                    base_cost = float(unit(
                        model or None,
                        raw_count=n,
                        input_tokens=tok_in,
                        output_tokens=tok_out,
                        cache_read_input_tokens=tok_cr,
                        cache_creation_input_tokens=tok_cc,
                    ))
                else:
                    base_cost = n
            weighted = base_cost * mult
            # v0.119：用 cfg_key（虚拟 cfg 时是 "A<->B"，真实 cfg 时就是
            # 上游名）聚合，virtual 模式下多个真实 upstream 行的 raw 与
            # weighted 都累加到同一个 cfg_key。
            cfg_key = real_to_cfg_key.get(upstream, upstream)
            per_upstream_per_model.setdefault(cfg_key, {})[model] = (
                per_upstream_per_model.get(cfg_key, {}).get(model, (0, 0.0))[0] + n,
                per_upstream_per_model.get(cfg_key, {}).get(model, (0, 0.0))[1] + weighted,
            )

    # Roll up to per-cfg totals + breakdown + utilization + ETA.
    for n in cfg_keys:
        row = out[n]
        breakdown = per_upstream_per_model.get(n, {})
        # model_breakdown: show the cost in the same weighted unit the
        # quota is denominated in. Skip empty-string models from the
        # breakdown (they're "no model reported", not a real model name).
        row["model_breakdown"] = {
            m: round(weighted, 2)
            for m, (_raw, weighted) in breakdown.items()
            if m
        }
        used = sum(weighted for (_raw, weighted) in breakdown.values())
        row["used_5h"] = round(used, 2)

        # Authorization check — flag models that hit this upstream but
        # aren't in the allow-list. Empty allow-list means "any model
        # allowed", so we don't flag anything.
        allowed_set = set(row["allowed_models"]) if row["allowed_models"] else None
        if allowed_set is not None:
            row["unauthorized_seen"] = sorted(
                m for m in breakdown.keys()
                if m and m not in allowed_set
            )

        quota = row["quota_5h"]
        if quota and quota > 0:
            util = used / quota
            row["utilization_5h"] = round(util, 4)
            row["remaining_5h"] = round(max(0.0, quota - used), 2)
            row["warning_level"] = _classify_warning(util)
            if util >= _QUOTA_EXHAUSTED:
                row["warning_text"] = "已用尽"
                row["exhaustion_eta"] = now
                row["exhaustion_eta_text"] = "now"
            else:
                # Burn rate over the elapsed window. ``elapsed`` is
                # how long ago the window started (now - cutoff); a
                # short window has less data but a known divisor.
                elapsed_h = window_seconds / 3600.0
                burn = used / elapsed_h
                row["burn_rate_per_hour"] = round(burn, 4)
                remaining = quota - used
                if burn > 0:
                    eta_seconds = remaining / burn * 3600.0
                    row["exhaustion_eta"] = now + eta_seconds
                    row["exhaustion_eta_text"] = _format_eta(eta_seconds)
                    row["warning_text"] = (
                        f"已用 {util * 100:.0f}%"
                        if util < _QUOTA_WARN
                        else f"剩 {_format_eta(eta_seconds)}"
                    )
                else:
                    row["warning_text"] = f"已用 {util * 100:.0f}%"
        # else: quota is None / 0 — fields already default-filled above.
    return out


def fetch_by_upstream_model(
    db_path: str,
    *,
    upstreams: list[dict[str, object]],
) -> dict[str, dict[str, dict[str, object]]]:
    """All-time per-(upstream, model) breakdown: requests, errors, the 4
    token columns, and a weighted cost consistent with
    ``fetch_by_upstream_with_costs`` (same ``billing_unit`` /
    ``token_fields`` / ``model_multipliers`` semantics, just re-grouped
    one level deeper and unbounded by the 5h window).

    Used by the "上游状态" card's per-upstream model-breakdown modal — the
    user wants to click an upstream and see how each individual model
    behind it is behaving, not just the upstream-wide roll-up.

    Returns ``{upstream_name: {model_name: {requests, errors,
    input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, cost, weighted_cost, cost_unit}}}``.
    ``cost`` is the raw base unit (token sum or request count depending
    on ``billing_unit``); ``weighted_cost`` has the model's multiplier
    applied; ``cost_unit`` is ``"tokens"`` or ``"次"`` for display.
    Configured upstreams with zero rows are present with an empty dict
    (contract mirrors the other fetch_* helpers — no missing keys).
    """
    name_to_cfg = {u["name"]: u for u in upstreams}
    out: dict[str, dict[str, dict[str, object]]] = {n: {} for n in name_to_cfg}
    if not upstreams or not Path(db_path).exists():
        return out
    # v0.119/v0.120：虚拟 cfg（linked_names）→ 展开成成员真实名列表进 SQL，
    # 真实 cfg → 自己一个。SQL 命中后按 cfg 反向聚合到 cfg.name 上：
    # 虚拟 cfg 的 model_breakdown = 成员同名 model 累加；cost/weighted_cost
    # 用 cfg 自身 multipliers（虚拟 cfg 沿用 _primary 的 multipliers）。
    real_names: list[str] = []
    seen: set[str] = set()
    for cfg in upstreams:
        for rn in _resolve_names(cfg):
            if rn and rn not in seen:
                seen.add(rn)
                real_names.append(rn)
    placeholders = ",".join("?" for _ in real_names)
    sql = (
        f"SELECT upstream, COALESCE(model, '') AS model, COUNT(*) AS n, "
        f"SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS err, "
        f"COALESCE(SUM(input_tokens), 0) AS tok_in, "
        f"COALESCE(SUM(output_tokens), 0) AS tok_out, "
        f"COALESCE(SUM(cache_read_input_tokens), 0) AS tok_cr, "
        f"COALESCE(SUM(cache_creation_input_tokens), 0) AS tok_cc "
        f"FROM requests "
        f"WHERE upstream IN ({placeholders}) AND upstream IS NOT NULL AND upstream != '' "
        f"GROUP BY upstream, model"
    )
    # 真实 upstream → member → cfg name 反向索引（SQL 返回真实名；需要找哪个
    # cfg 包含该真实名作为成员；虚拟 cfg 的 model_breakdown 累加该 cfg）
    name_to_owners: dict[str, list[str]] = {}
    for cfg_name, cfg in name_to_cfg.items():
        for rn in _resolve_names(cfg):
            if rn:
                name_to_owners.setdefault(rn, []).append(cfg_name)
    # 按 cfg_name 分桶暂存，待后处理聚合（同成员模型跨成员聚合仅发生在虚拟 cfg 下；
    # 真实 cfg 自身只会有一个 owner）
    by_cfg: dict[str, dict[str, dict[str, object]]] = {n: {} for n in name_to_cfg}
    with _connect(db_path) as c:
        for upstream, model, n, err, tok_in, tok_out, tok_cr, tok_cc in c.execute(
            sql, real_names
        ):
            owners = name_to_owners.get(upstream, [])
            if not owners:
                continue
            for cfg_name in owners:
                cfg = name_to_cfg.get(cfg_name, {})
                mults = cfg.get("model_multipliers") or {}
                mult = float(mults.get(model, 1.0)) if model else 1.0
                billing_unit = cfg.get("billing_unit", "count")
                if billing_unit == "token":
                    base = _token_cost(tok_in, tok_out, tok_cr, tok_cc, cfg.get("token_fields"))
                    cost_unit = "tokens"
                else:
                    unit = billing_unit_for(billing_unit) if billing_unit != "count" else None
                    if unit is not None:
                        base = float(unit(
                            model or None,
                            raw_count=n,
                            input_tokens=tok_in,
                            output_tokens=tok_out,
                            cache_read_input_tokens=tok_cr,
                            cache_creation_input_tokens=tok_cc,
                        ))
                        cost_unit = billing_unit
                    else:
                        base = int(n or 0)
                        cost_unit = "次"
                # Empty-string model means "no model reported" — still worth
                # showing (e.g. failed requests before model extraction), so
                # unlike model_breakdown in fetch_by_upstream_with_costs we
                # don't filter it out here; label it for the frontend instead.
                label = model or "(未知模型)"
                row = by_cfg[cfg_name].setdefault(label, {
                    "requests": 0,
                    "errors": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cost": 0.0,
                    "weighted_cost": 0.0,
                    "cost_unit": cost_unit,
                })
                # 跨成员聚合：同名 model 在虚拟 cfg 下累加。cost 用 base 之和
                # （成员各自的 base 之和），weighted_cost 用 base × mult 之和
                # （mult 来自 cfg 自身即虚拟 cfg 的 _primary multipliers）。
                row["requests"] += int(n or 0)
                row["errors"] += int(err or 0)
                row["input_tokens"] += int(tok_in or 0)
                row["output_tokens"] += int(tok_out or 0)
                row["cache_read_input_tokens"] += int(tok_cr or 0)
                row["cache_creation_input_tokens"] += int(tok_cc or 0)
                row["cost"] = round(row["cost"] + base, 2)
                row["weighted_cost"] = round(row["weighted_cost"] + base * mult, 2)
    return by_cfg


def fetch_by_hour(
    db_path: str,
    *,
    since: float | None = None,
    bucket: int = 3600,
) -> list[dict[str, object]]:
    """Hour-bucketed request + token totals for the GUI timeseries chart.

    Single SQL pass: bucketed ``COUNT(*)`` + ``SUM(input_tokens)`` +
    ``SUM(output_tokens)`` over rows newer than ``now - since``. Default
    window is 24 hours, default bucket is 1 hour.

    Returns
    -------
    [{hour:        float (unix seconds, bucket-aligned),
      requests:    int,
      in_tokens:   int,
      out_tokens:  int,
      tokens:      int (in + out)}]
    sorted ascending by ``hour`` so the chart can plot left-to-right.
    Empty hours are NOT zero-filled — callers can pad on the frontend.
    """
    if since is None:
        since = 24 * 3600
    if not Path(db_path).exists():
        return []
    cutoff = time.time() - since
    sql = (
        f"SELECT (CAST(ts AS INT) / ?) * ? AS hour_bucket, "
        f"COUNT(*) AS req_count, "
        f"COALESCE(SUM(input_tokens), 0) AS in_sum, "
        f"COALESCE(SUM(output_tokens), 0) AS out_sum "
        f"FROM requests "
        f"WHERE ts >= ? "
        f"GROUP BY hour_bucket "
        f"ORDER BY hour_bucket"
    )
    with _connect(db_path) as c:
        rows = c.execute(sql, (bucket, bucket, cutoff)).fetchall()
    return [
        {
            "hour": float(r["hour_bucket"]),
            "requests": int(r["req_count"] or 0),
            "in_tokens": int(r["in_sum"] or 0),
            "out_tokens": int(r["out_sum"] or 0),
            "tokens": int((r["in_sum"] or 0) + (r["out_sum"] or 0)),
        }
        for r in rows
    ]


def _format_release(seconds: float | int | None) -> str:
    """Compact release-time display for the per-upstream stats panel.

    ``seconds`` is "how long until the oldest in-window request falls out".
    Returns ``"—"`` when there's nothing to release (no rows in the window),
    ``"now"`` when the value is at or below zero, and otherwise a short
    ``Xd Yh`` / ``Xh Ym`` / ``Xm`` / ``<1m`` form. Lives next to the data
    so the rule moves with the source.
    """
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "now"
    total_min = int(seconds // 60)
    if total_min == 0:
        return "<1m"
    days, rem = divmod(total_min, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def fetch_recent(
    db_path: str,
    limit: int = 20,
    before_id: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Return the most recent ``limit`` requests, optionally excluding
    rows with id >= ``before_id`` (cursor pagination — pass the smallest
    id from the previous page to get the next page).

    Cursor uses id (autoincrement) rather than ts because multiple
    requests in the same second would otherwise repeat or skip rows
    on successive pages.
    """
    if not Path(db_path).exists():
        return []
    params: list[object] = []
    where = ""
    if before_id is not None:
        where = "WHERE id < ?"
        params.append(before_id)
    params.append(limit)
    with _connect(db_path) as c:
        return list(
            c.execute(
                f"""
                SELECT id, ts, platform, model, request_id,
                       input_tokens, output_tokens, cache_read_input_tokens,
                       cache_creation_input_tokens, status_code, error, upstream
                FROM requests
                {where}
                ORDER BY id DESC LIMIT ?
                """,
                tuple(params),
            )
        )


def fetch_conversation(db_path: str, request_id: int) -> Optional[dict[str, Any]]:
    """Sync mirror of ``Database.get_conversation`` for the GUI bridge.

    Returns one request row + all its saved messages (insertion order:
    user before assistant), or None when the request id doesn't exist.
    Used by ``Api.fetch_conversation`` — see the rationale there for why
    the GUI goes through this rather than ``GET /messages/by_request/{id}``.
    """
    if not Path(db_path).exists():
        return None
    with _connect(db_path) as c:
        req = c.execute(
            "SELECT id, ts, platform, model, request_id, "
            "input_tokens, output_tokens, "
            "cache_read_input_tokens, cache_creation_input_tokens, "
            "status_code, error, upstream "
            "FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if req is None:
            return None
        msgs = c.execute(
            "SELECT role, ts, content, content_json "
            "FROM messages WHERE request_id = ? ORDER BY id ASC",
            (request_id,),
        ).fetchall()
    return {
        "request": dict(req),
        "messages": [dict(m) for m in msgs],
    }


def fetch_live(relay_url: str = DEFAULT_RELAY_URL, timeout: float = 0.5) -> list[dict]:
    """GET <relay_url>/live — returns the in-flight + recently-completed
    requests. Empty list on any failure (server down, timeout, etc.).

    This is the live-panels backing data source for the TUI — distinct
    from the SQLite-backed `fetch_recent` / `fetch_totals` which read
    historical rows. State is held in the relay server's process memory.
    """
    try:
        with urllib.request.urlopen(f"{relay_url}/live", timeout=timeout) as r:
            if r.status != 200:
                return []
            data = json.loads(r.read().decode("utf-8"))
        reqs = data.get("requests") if isinstance(data, dict) else None
        return reqs if isinstance(reqs, list) else []
    except Exception:
        return []


def fetch_models_by_upstream(db_path: str) -> dict[str, list[str]]:
    """Distinct models observed per upstream, derived from the SQLite
    ``requests`` table.

    Used by the v0.64 GUI upstream-detail card: even when an upstream
    has no explicit ``allowed_models`` configured, the user still
    wants to see what models have been flowing through it (and delete
    any that shouldn't be). The query joins ``upstream`` + ``model``
    columns, dedupes, and groups.

    Returns ``{upstream_name: [model, ...]}`` with models sorted
    alphabetically. ``upstream`` is set by the relay for every row (it's
    a NOT-NULL column after the v0.x migration); rows where
    ``upstream`` is empty / NULL are skipped — those are pre-migration
    rows or rows that hit a relay bug, neither of which we want to
    attribute to a phantom upstream.
    """
    out: dict[str, list[str]] = {}
    if not Path(db_path).exists():
        return out
    try:
        with _connect(db_path) as c:
            rows = c.execute(
                """
                SELECT upstream, model
                FROM requests
                WHERE upstream IS NOT NULL AND upstream != ''
                  AND model IS NOT NULL AND model != ''
                GROUP BY upstream, model
                ORDER BY upstream, model
                """
            ).fetchall()
    except Exception:
        return out
    for upstream, model in rows:
        # ``model`` was already filtered to non-empty in SQL; coerce
        # defensively in case a future schema change relaxes that.
        if not model:
            continue
        out.setdefault(upstream, []).append(model)
    return out


def fetch_recent_filtered(
    db_path: str,
    *,
    limit: int = 50,
    query: str = "",
    platform: Optional[str] = None,
    require_assistant: bool = False,
) -> list[sqlite3.Row]:
    """Recent requests filtered by text / platform / assistant-presence.

    `query` is a substring matched against model name OR user-message content
    (the saved `messages` row with `role='user'`). Empty query disables the
    text filter. `platform` is an exact match on the platform column.
    `require_assistant` keeps only requests that have a non-empty saved
    assistant reply.

    Returned rows have the same shape as `fetch_recent` so the caller can
    format them identically. The optional `messages` join is left out of the
    SELECT — we only need to know the messages EXIST and match.
    """
    if not Path(db_path).exists():
        return []
    clauses: list[str] = []
    params: list[object] = []
    if query:
        like = f"%{query}%"
        # Subquery avoids the need for DISTINCT / GROUP BY when multiple
        # messages per request would otherwise multiply rows.
        clauses.append(
            "(r.model LIKE ? OR EXISTS ("
            "SELECT 1 FROM messages m "
            "WHERE m.request_id = r.id AND m.role = 'user' AND m.content LIKE ?"
            "))"
        )
        params.extend([like, like])
    if platform:
        clauses.append("r.platform = ?")
        params.append(platform)
    if require_assistant:
        clauses.append(
            "EXISTS (SELECT 1 FROM messages m "
            "WHERE m.request_id = r.id AND m.role = 'assistant' "
            "AND m.content IS NOT NULL AND m.content != '')"
        )
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect(db_path) as c:
        return list(
            c.execute(
                f"""
                SELECT r.id, r.ts, r.platform, r.model, r.request_id,
                       r.input_tokens, r.output_tokens,
                       r.cache_read_input_tokens, r.cache_creation_input_tokens,
                       r.status_code, r.error
                FROM requests r
                {where}
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (*params, limit),
            )
        )


def fetch_conversation(db_path: str, request_id: int) -> Optional[dict]:
    """Sync version of `Database.get_conversation`.

    Used by the GUI's double-click handler — runs on the Tk main thread,
    so sync sqlite3 is the right call. Returns `None` if the request_id
    isn't present (e.g. user clicked a stale row after DB rollover).

    If the `messages` table doesn't exist (old DB from before v0.4, opened
    by the GUI before the relay server has run init), returns the request
    row with `messages=[]` and `messages_missing=True` so the dialog can
    show a clear hint instead of crashing with OperationalError.
    """
    import sqlite3
    if not Path(db_path).exists():
        return None
    with _connect(db_path) as c:
        req = c.execute(
            "SELECT id, ts, platform, model, request_id, "
            "input_tokens, output_tokens, "
            "cache_read_input_tokens, cache_creation_input_tokens, "
            "status_code, error, upstream "
            "FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if req is None:
            return None
        try:
            msgs = c.execute(
                "SELECT role, ts, content, content_json "
                "FROM messages WHERE request_id = ? ORDER BY id ASC",
                (request_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: messages" in str(exc):
                return {"request": dict(req), "messages": [], "messages_missing": True}
            raise
    return {"request": dict(req), "messages": [dict(m) for m in msgs]}


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class PlatformCard(Static):
    """Compact per-platform summary. Title is the platform name."""

    def __init__(self, platform: str) -> None:
        super().__init__(id=f"card-{platform}")
        self.platform = platform
        self.border_title = platform

    def render_content(self, totals: dict[str, int]) -> Text:
        if totals["requests"] == 0:
            return Text("no requests yet", style="dim")
        text = Text()
        text.append(f"{totals['requests']:>5} requests\n", style="bold")
        text.append(f"{totals['input_tokens']:>7,}  in\n", style="cyan")
        text.append(f"{totals['output_tokens']:>7,}  out\n", style="magenta")
        text.append(f"{totals['cache_read_input_tokens']:>7,}  cache rd\n", style="yellow")
        text.append(f"{totals['cache_creation_input_tokens']:>7,}  cache wr\n", style="green")
        if totals["errors"]:
            text.append(f"{totals['errors']:>5}  errors\n", style="bold red")
        return text


class GrandTotal(Static):
    """Top-of-screen aggregate across all platforms."""

    totals: reactive[dict[str, dict[str, int]]] = reactive({})
    window: reactive[str] = reactive("total")

    def render_total(self) -> Text:
        agg = {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "errors": 0,
        }
        for p in PLATFORMS:
            row = self.totals.get(p, {})
            for k in agg:
                agg[k] += row.get(k, 0)
        text = Text()
        text.append(f"ALL PLATFORMS [{self.window}]  ", style="bold")
        text.append(f"{agg['requests']:>4} reqs   ", style="bold")
        text.append(f"in {agg['input_tokens']:>9,}", style="cyan")
        text.append("   ")
        text.append(f"out {agg['output_tokens']:>9,}", style="magenta")
        text.append("   ")
        text.append(f"cache rd {agg['cache_read_input_tokens']:>9,}", style="yellow")
        text.append("   ")
        text.append(f"cache wr {agg['cache_creation_input_tokens']:>9,}", style="green")
        if agg["errors"]:
            text.append(f"   ERRORS {agg['errors']}", style="bold red")
        return text

    def watch_totals(self, _) -> None:
        self.update(self.render_total())

    def watch_window(self, _) -> None:
        self.update(self.render_total())


class DashboardApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #grand-total {
        height: 1;
        padding: 1 2;
        background: $boost;
    }
    #cards {
        height: auto;
        padding: 1 2;
    }
    PlatformCard {
        width: 1fr;
        height: auto;
        padding: 1 2;
        margin: 0 1;
        border: round $primary;
    }
    #live-row {
        height: auto;
        padding: 0 2;
    }
    #live-upload {
        width: 1fr;
        height: auto;
        padding: 0 1;
        border: round $primary;
        margin: 0 1 0 0;
    }
    #live-output {
        width: 2fr;
        height: auto;
        padding: 0 1;
        border: round $primary;
        margin: 0 0 0 1;
    }
    #recent {
        height: 1fr;
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh", "refresh now"),
        ("1", "window('5h')", "5h"),
        ("2", "window('24h')", "24h"),
        ("3", "window('week')", "week"),
        ("0", "window('total')", "all"),
    ]

    def __init__(self, db_path: str, relay_url: str = DEFAULT_RELAY_URL) -> None:
        super().__init__()
        self.db_path = db_path
        self.relay_url = relay_url
        self.cards: dict[str, PlatformCard] = {}
        self.grand = GrandTotal(id="grand-total")
        self.recent: DataTable
        self.window: str = "total"  # current time window
        self._title_base = "Token consumption"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield self.grand
        with Horizontal(id="cards"):
            for p in PLATFORMS:
                card = PlatformCard(p)
                self.cards[p] = card
                yield card
        # Live panels: 1:2 horizontal split, above the recent table.
        with Horizontal(id="live-row"):
            yield Static("（加载中…）", id="live-upload")
            yield Static("（加载中…）", id="live-output")
        yield DataTable(id="recent", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#recent", DataTable)
        table.add_columns(
            "time", "platform", "model", "in", "out",
            "cache_rd", "cache_wr", "req_id", "status", "error",
        )
        # Defer first refresh until after children are mounted. set_interval
        # alone doesn't fire synchronously, and on_mount can run before child
        # widgets are ready for queries.
        self.call_after_refresh(self._refresh)
        self.set_interval(0.5, self._refresh)
        # Live panels need their own timer because they make a network call.
        # Reuses the same 0.5s cadence so /live is read every other tick.
        self.set_interval(0.5, self._refresh_live)

    def _refresh(self) -> None:
        # Resolve the time window to a unix-ts lower bound.
        from .routers.stats import WINDOWS
        import time
        secs = WINDOWS.get(self.window)
        since = None if secs is None else time.time() - secs
        try:
            totals = fetch_totals(self.db_path, since=since)
        except sqlite3.OperationalError:
            totals = {p: {"requests": 0} for p in PLATFORMS}
        self.grand.totals = totals
        self.grand.window = self.window
        for p, card in self.cards.items():
            card.update(card.render_content(totals.get(p, {"requests": 0})))

        # Recent activity table
        table = self.query_one("#recent", DataTable)
        table.clear()
        for row in fetch_recent(self.db_path, limit=20):
            ts = datetime.fromtimestamp(row["ts"]).strftime("%H:%M:%S")
            err = (row["error"] or "")[:30]
            status = row["status_code"] or 0
            status_style = "red" if status >= 400 or err else "dim"
            table.add_row(
                ts,
                row["platform"] or "",
                (row["model"] or "")[:18],
                f"{row['input_tokens']:,}",
                f"{row['output_tokens']:,}",
                f"{row['cache_read_input_tokens']:,}",
                f"{row['cache_creation_input_tokens']:,}",
                (row["request_id"] or "")[:14],
                Text(str(status), style=status_style),
                Text(err, style="red" if err else "dim"),
            )

    async def _refresh_live(self) -> None:
        """Update the live panels. Synchronous HTTP read inside run_worker.

        `urllib` blocks; running it via run_worker keeps the UI loop free.
        """
        import urllib.request as _ur
        loop = asyncio.get_event_loop()

        def _fetch() -> list[dict]:
            try:
                with _ur.urlopen(f"{self.relay_url}/live", timeout=0.5) as r:
                    if r.status != 200:
                        return []
                    data = json.loads(r.read().decode("utf-8"))
                reqs = data.get("requests") if isinstance(data, dict) else None
                return reqs if isinstance(reqs, list) else []
            except Exception:
                return []

        entries = await loop.run_in_executor(None, _fetch)
        self._render_live(entries)

    def _render_live(self, entries: list[dict]) -> None:
        upload_panel = self.query_one("#live-upload", Static)
        output_panel = self.query_one("#live-output", Static)

        if not entries:
            upload_panel.update("[dim]（无）[/dim]")
            output_panel.update("[dim]（无）[/dim]")
            return

        upload_entries = [e for e in entries if e.get("phase") in ("uploading", "calling")]
        output_entries = [e for e in entries if e.get("phase") in ("streaming", "done")]

        if not upload_entries:
            upload_panel.update("[dim]（无）[/dim]")
        else:
            upload_panel.update(self._render_live_upload(upload_entries))

        if not output_entries:
            output_panel.update("[dim]（等待输出）[/dim]")
        else:
            output_panel.update(self._render_live_output(output_entries))

    @staticmethod
    def _render_live_upload(entries: list[dict]) -> Text:
        out = Text()
        for e in entries:
            model = e.get("model") or "(未知模型)"
            phase = e.get("phase", "uploading")
            received = e.get("bytes_received", 0)
            total = e.get("content_length")
            out.append(f"[{phase}]  ", style="bold")
            out.append(f"{model}\n", style="bold")
            if total and total > 0:
                pct = min(100, int(received * 100 / total))
                bar_w = 20
                filled = int(pct / 100 * bar_w)
                out.append(
                    f"  [{'█' * filled}{'░' * (bar_w - filled)}] {pct:>3}%"
                    f"  {received:,}/{total:,}\n",
                    style="cyan",
                )
            else:
                out.append(f"  {received:,} 字节\n", style="cyan")
            preview = (e.get("user_text_preview") or "").replace("\n", " ").strip()
            if preview:
                out.append(f"  💬 {preview[:200]}\n", style="dim")
            out.append("\n")
        return out

    @staticmethod
    def _render_live_output(entries: list[dict]) -> Text:
        out = Text()
        for e in entries:
            model = e.get("model") or "(未知模型)"
            phase = e.get("phase", "streaming")
            marker = "●" if phase == "streaming" else "✓"
            color = "red" if e.get("platform") == "anthropic" else "green"
            out.append(f"{marker} {model} ", style=f"bold {color}")
            out.append(f"[{phase}]\n", style="dim")
            text = e.get("assistant_text") or ""
            if len(text) > 800:
                text = "…" + text[-800:]
            text = text.replace("\n", " ")
            if text:
                out.append(f"  {text}\n", style="white")
            out.append("\n")
        return out

    def action_refresh(self) -> None:
        self._refresh()
        # Force the live panels to update on demand so `r` doesn't make the
        # user wait up to 0.5s for the next tick.
        self.call_after_refresh(self._refresh_live)

    def action_window(self, name: str) -> None:
        if name not in ("5h", "24h", "week", "total"):
            return
        self.window = name
        # Show the current window in the header bar.
        self.title = f"{self._title_base} — window={name}"
        self._refresh()


# ---------------------------------------------------------------------------
# v0.99 统计页 fetchers（routers/stats.py /stats/aggregate 与 /stats/daily 的数据源）
# ---------------------------------------------------------------------------

# v0.99 时间段预设。单位：秒。``None`` 表示"全部历史"（不计大小只计时间）。
_RANGE_PRESETS: dict[str, int] = {
    "1d":  24 * 3600,
    "7d":  7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}


def fetch_aggregate_by_dim(
    db_path: str,
    *,
    dim: str,
    since: float | None,
    top: int = 10,
) -> dict[str, dict[str, int]]:
    """v0.99：按 dim 分组的 top-N 聚合（入/出/缓存读/缓存写/请求/错误）。

    ``dim`` 取值：``"platform"`` / ``"upstream"`` / ``"model"`` / ``"agent"``。
    ``since`` 是 unix 时间戳下界；``None`` 表示全部历史。
    ``top`` 限定返回行数（按总 token 降序）；0 / 负数表示不限。

    返回 ``{key: {requests, input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens, errors, total_tokens}}``，
    其中 ``total_tokens`` = input + output + cache_read + cache_creation（与
    ``fetch_total_tokens_by_upstream`` 同口径，前端「总」列直接用）。

    NULL/空 值的行：platform / upstream 维度跳过（无意义），model 维度把
    NULL 合并为 ``"(无)"``，agent 维度把 NULL 合并为 ``AGENT_UNKNOWN``。
    """
    valid_dims = {"platform", "upstream", "model", "agent"}
    if dim not in valid_dims:
        raise ValueError(f"dim must be one of {valid_dims}, got {dim!r}")

    if not Path(db_path).exists():
        return {}

    where = "WHERE ts >= ?" if since is not None else ""
    params: tuple = (since,) if since is not None else ()

    if dim == "model":
        # NULL model 合并成 "(无)"，避免 GROUP BY 把它丢出可见集。
        key_expr = "COALESCE(NULLIF(model, ''), '(无)')"
    elif dim == "agent":
        # NULL agent（迁移前旧行）合并成兜底桶 "其它"，避免丢出可见集。
        key_expr = f"COALESCE(NULLIF(agent, ''), '{AGENT_UNKNOWN}')"
    else:
        key_expr = dim
        where += " AND " + dim + " IS NOT NULL AND " + dim + " != ''" if where else (
            "WHERE " + dim + " IS NOT NULL AND " + dim + " != ''"
        )

    sql = (
        f"SELECT {key_expr} AS key, "
        f"COUNT(*) AS requests, "
        f"COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        f"COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        f"COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens, "
        f"COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens, "
        f"SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
        f"FROM requests {where} "
        f"GROUP BY key "
        f"ORDER BY (input_tokens + output_tokens + cache_read_input_tokens + cache_creation_input_tokens) DESC"
    )
    out: dict[str, dict[str, int]] = {}
    with _connect(db_path) as c:
        rows = c.execute(sql, params).fetchall()
    for r in rows:
        key = r["key"]
        it = int(r["input_tokens"] or 0)
        ot = int(r["output_tokens"] or 0)
        cr = int(r["cache_read_input_tokens"] or 0)
        cc = int(r["cache_creation_input_tokens"] or 0)
        out[key] = {
            "requests": int(r["requests"] or 0),
            "input_tokens": it,
            "output_tokens": ot,
            "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc,
            "errors": int(r["errors"] or 0),
            "total_tokens": it + ot + cr + cc,
        }
    # 截断在调用方做：route 层 / bridge 层先对完整结果算 total，再按 top 截
    # 前 N 行。tui 不在这里截 —— 截了之后上层算不出"全部"的合计。
    return out


def fetch_daily(
    db_path: str,
    *,
    days: int = 30,
) -> list[dict[str, int | float]]:
    """v0.99：每日一行聚合（UTC 日期桶对齐）。

    返回最近 ``days`` 天（含今天），按日期**倒序**。空日期用 0 补齐，
    让前端能稳定画出固定行数的表格。列：date (ISO YYYY-MM-DD) / requests /
    input_tokens / output_tokens / cache_read_input_tokens /
    cache_creation_input_tokens / errors。

    桶对齐：``(CAST(ts AS INT) / 86400) * 86400`` —— UTC 日界，与本地
    时区无关（用户深夜跨天的请求会按 UTC 切分；这是 v0.99 显式选定的
    简化方案，避免本地时区跨日带来的桶漂移）。
    """
    if not Path(db_path).exists():
        return []

    now = int(time.time())
    # 把 now 对齐到当天 UTC 起点（向下取整到当日 0 点）；今天这一天的桶
    # 尚未结束但仍保留，requests/0 即可。
    today_start = (now // 86400) * 86400
    cutoff = today_start - (days - 1) * 86400

    sql = (
        "SELECT (CAST(ts AS INT) / 86400) * 86400 AS day_bucket, "
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens, "
        "COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens, "
        "SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
        "FROM requests "
        "WHERE ts >= ? "
        "GROUP BY day_bucket"
    )
    with _connect(db_path) as c:
        rows = c.execute(sql, (cutoff,)).fetchall()

    # 索引化 + 0 补齐。
    by_day: dict[int, dict[str, int | float]] = {
        int(r["day_bucket"]): {
            "requests": int(r["requests"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "cache_read_input_tokens": int(r["cache_read_input_tokens"] or 0),
            "cache_creation_input_tokens": int(r["cache_creation_input_tokens"] or 0),
            "errors": int(r["errors"] or 0),
        }
        for r in rows
    }
    out: list[dict[str, int | float]] = []
    for offset in range(days - 1, -1, -1):  # 倒序：旧→新，从 days-1 → 0
        day_ts = today_start - offset * 86400
        bucket = by_day.get(day_ts, {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "errors": 0,
        })
        iso = time.strftime("%Y-%m-%d", time.gmtime(day_ts))
        bucket = {"date": iso, "ts": day_ts, **bucket}
        out.append(bucket)
    return out


# ---------------------------------------------------------------------------
# v0.100.1 统计页（高级可视化）数据源
#
# 8 张 D3 图的数据全部走这 4 个函数。schema 0 改动（路由热力/Sankey/日历
# 利用既有 platform + upstream + ts 字段；token_breakdown 利用既有 4 个
# token 列；timeseries 按 ts 桶 + upstream 拆色）。
# ---------------------------------------------------------------------------

# 时间段预设（与 v0.99 一致）
_RANGE_PRESETS: dict[str, int] = {
    "1d":  24 * 3600,
    "7d":  7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}


def fetch_token_breakdown(
    db_path: str,
    *,
    since: float | None,
) -> dict[str, int]:
    """v0.100.1：玫瑰图数据 —— 全模型合计 4 种 token + 总数。

    时间窗口由 ``since`` 决定；``None`` 表示全部历史。
    返回 ``{input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, total}``，字段名与 routers 一致便于前端
    直接映射。
    """
    out = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    if not Path(db_path).exists():
        out["total"] = out["input_tokens"] + out["output_tokens"] + out["cache_read_input_tokens"] + out["cache_creation_input_tokens"]
        return out
    where = "WHERE ts >= ?" if since is not None else ""
    params: tuple = (since,) if since is not None else ()
    sql = (
        f"SELECT COALESCE(SUM(input_tokens), 0), "
        f"COALESCE(SUM(output_tokens), 0), "
        f"COALESCE(SUM(cache_read_input_tokens), 0), "
        f"COALESCE(SUM(cache_creation_input_tokens), 0) "
        f"FROM requests {where}"
    )
    with _connect(db_path) as c:
        row = c.execute(sql, params).fetchone()
    if row:
        it, ot, cr, cc = row
        out = {
            "input_tokens": int(it or 0),
            "output_tokens": int(ot or 0),
            "cache_read_input_tokens": int(cr or 0),
            "cache_creation_input_tokens": int(cc or 0),
        }
    out["total"] = out["input_tokens"] + out["output_tokens"] + out["cache_read_input_tokens"] + out["cache_creation_input_tokens"]
    return out


def fetch_timeseries(
    db_path: str,
    *,
    since: float | None,
    bucket: str,
    metric: str,
) -> list[dict[str, int | float | str]]:
    """v0.100.1：stacked area 数据 —— 按时间桶 + upstream 拆色。

    ``bucket`` ∈ {"hour", "day"}：桶宽（hour ≈ 3600s，day = 86400s）。
    ``metric`` ∈ {"requests", "errors", "total_tokens"}：被求和的量。
    返回 ``[{ts, upstream, value}, ...]``，按 (ts ASC, upstream ASC) 排序。

    注：errors 桶用 ``status_code >= 400 OR error IS NOT NULL``（与
    fetch_aggregate_by_dim 一致）；total_tokens = 4 token 之和。
    """
    out: list[dict[str, int | float | str]] = []
    if not Path(db_path).exists():
        return out
    secs = {"hour": 3600, "day": 86400}.get(bucket)
    if secs is None:
        raise ValueError(f"bucket must be 'hour' or 'day', got {bucket!r}")
    if metric == "requests":
        metric_sql = "COUNT(*)"
    elif metric == "errors":
        metric_sql = "SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END)"
    elif metric == "total_tokens":
        metric_sql = (
            "COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) "
            "+ COALESCE(SUM(cache_read_input_tokens), 0) + COALESCE(SUM(cache_creation_input_tokens), 0)"
        )
    else:
        raise ValueError(f"metric must be 'requests' | 'errors' | 'total_tokens', got {metric!r}")
    where = "WHERE ts >= ?"
    params: tuple = (since,) if since is not None else ()
    sql = (
        f"SELECT (CAST(ts AS INT) / {secs}) * {secs} AS bucket_ts, "
        f"COALESCE(NULLIF(upstream, ''), '(无)') AS upstream, "
        f"{metric_sql} AS value "
        f"FROM requests {where} "
        f"GROUP BY bucket_ts, upstream "
        f"ORDER BY bucket_ts ASC, upstream ASC"
    )
    with _connect(db_path) as c:
        for row in c.execute(sql, params).fetchall():
            out.append({
                "ts": float(row["bucket_ts"]),
                "upstream": str(row["upstream"]),
                "value": int(row["value"] or 0),
            })
    return out


def fetch_route_heatmap(
    db_path: str,
    *,
    since: float | None,
) -> dict[str, dict[str, int]]:
    """v0.100.1：路由热力 + Sankey 数据 —— 每上游在各平台的请求数矩阵。

    返回 ``{upstream_name: {anthropic: n, openai: n}}``。缺失平台补 0（前端
    渲染稳定网格）。
    """
    out: dict[str, dict[str, int]] = {}
    if not Path(db_path).exists():
        return out
    where = "WHERE ts >= ? AND upstream IS NOT NULL AND upstream != ''"
    params: tuple = (since,) if since is not None else ()
    sql = (
        f"SELECT upstream, platform, COUNT(*) AS n "
        f"FROM requests {where} "
        f"GROUP BY upstream, platform"
    )
    with _connect(db_path) as c:
        for row in c.execute(sql, params).fetchall():
            ups, plat, n = row["upstream"], row["platform"], int(row["n"] or 0)
            out.setdefault(ups, {"anthropic": 0, "openai": 0})[plat] = n
    return out


def fetch_calendar(
    db_path: str,
    *,
    days: int,
) -> dict[str, object]:
    """v0.100.1：日历热力数据 —— 按日 token + requests，时间桶对齐到 UTC 日界。

    与 fetch_daily 桶对齐算法相同（``(ts // 86400) * 86400``），但额外输出
    ``year``（按今天所在 UTC 年）+ ``days``（固定长度，0 补齐）+ ``total``
    （所有日 total_tokens 之和，供前端标题用）。
    返回 ``{year, days, total, items:[{date(YYYY-MM-DD), ts, requests,
    input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens, errors, total_tokens}]}``，items 按
    日期升序（旧→新）。
    """
    out: dict[str, object] = {"year": 0, "days": days, "total": 0, "items": []}
    if not Path(db_path).exists() or days <= 0:
        return out
    now = int(time.time())
    today_start = (now // 86400) * 86400
    year_utc = time.gmtime(today_start).tm_year
    cutoff = today_start - (days - 1) * 86400
    out["year"] = year_utc
    sql = (
        "SELECT (CAST(ts AS INT) / 86400) * 86400 AS day_bucket, "
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens, "
        "COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens, "
        "SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
        "FROM requests WHERE ts >= ? "
        "GROUP BY day_bucket"
    )
    rows_by_day: dict[int, dict[str, int]] = {}
    with _connect(db_path) as c:
        for row in c.execute(sql, (cutoff,)).fetchall():
            rows_by_day[int(row["day_bucket"])] = {
                "requests": int(row["requests"] or 0),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cache_read_input_tokens": int(row["cache_read_input_tokens"] or 0),
                "cache_creation_input_tokens": int(row["cache_creation_input_tokens"] or 0),
                "errors": int(row["errors"] or 0),
            }
    items: list[dict[str, int | float | str]] = []
    grand_total = 0
    for offset in range(days - 1, -1, -1):  # 升序：days-1 → 0（最早→最新）
        day_ts = today_start - offset * 86400
        bucket = rows_by_day.get(day_ts, {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "errors": 0,
        })
        iso = time.strftime("%Y-%m-%d", time.gmtime(day_ts))
        total_tokens = (
            bucket["input_tokens"] + bucket["output_tokens"]
            + bucket["cache_read_input_tokens"] + bucket["cache_creation_input_tokens"]
        )
        grand_total += total_tokens
        items.append({
            "date": iso,
            "ts": day_ts,
            "requests": bucket["requests"],
            "input_tokens": bucket["input_tokens"],
            "output_tokens": bucket["output_tokens"],
            "cache_read_input_tokens": bucket["cache_read_input_tokens"],
            "cache_creation_input_tokens": bucket["cache_creation_input_tokens"],
            "errors": bucket["errors"],
            "total_tokens": total_tokens,
        })
    out["items"] = items
    out["total"] = grand_total
    return out


def fetch_model_daily(
    db_path: str,
    *,
    days: int = 30,
) -> dict[str, object]:
    """v0.153：按模型 × 日 的调用分布 —— 单模型 30 天按日柱状图数据源。

    时间桶对齐与 ``fetch_daily`` / ``fetch_calendar`` 相同（UTC 日界，
    ``(CAST(ts AS INT) / 86400) * 86400``）。按 ``model`` 拆行，产出：

    - ``models``: ``[{model, total, color}]`` —— 每个出现过的模型 + 其
      区间内 total_tokens 合计 + 稳定色（按出现顺序取自 ``MODEL_PALETTE``，
      保证切换模型按钮时颜色不变）。
    - ``days``: ``[{date, ts, total}]`` —— 30 天固定日历（0 补齐），
      ``total`` 为当天所有模型合计。
    - ``series``: ``{model: [{date, total_tokens}, ...]}`` —— 每模型一条
      30 天序列（0 补齐，升序），D3 直接按日期画柱状/折线。
    - ``items``: ``[{model, date, requests, total_tokens}]`` —— 明细行
      （tooltip 用）。

    空 DB / 无模型行 → 空结构（``models=[]``，``days`` 仍补齐），前端
    显示空态。``model`` 为 NULL/空串 的请求归入 ``"(无)"``。
    """
    out: dict[str, object] = {
        "models": [],
        "days": [],
        "series": {},
        "items": [],
    }
    if not Path(db_path).exists() or days <= 0:
        return out

    now = int(time.time())
    today_start = (now // 86400) * 86400
    cutoff = today_start - (days - 1) * 86400

    # 预生成固定日历（升序：最早 → 今天），空日期留待 0 补齐。
    calendar: list[dict[str, object]] = []
    for offset in range(days - 1, -1, -1):
        day_ts = today_start - offset * 86400
        calendar.append({
            "date": time.strftime("%Y-%m-%d", time.gmtime(day_ts)),
            "ts": day_ts,
        })

    sql = (
        "SELECT COALESCE(NULLIF(model, ''), '(无)') AS model, "
        "(CAST(ts AS INT) / 86400) * 86400 AS day_bucket, "
        "COUNT(*) AS requests, "
        "COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) "
        "  + COALESCE(SUM(cache_read_input_tokens), 0) "
        "  + COALESCE(SUM(cache_creation_input_tokens), 0) AS total_tokens "
        "FROM requests WHERE ts >= ? "
        "GROUP BY model, day_bucket"
    )

    # model → day_bucket → {requests, total_tokens}
    cell: dict[str, dict[int, dict[str, int]]] = {}
    model_totals: dict[str, int] = {}
    with _connect(db_path) as c:
        for row in c.execute(sql, (cutoff,)).fetchall():
            m, bucket = str(row["model"]), int(row["day_bucket"])
            cell.setdefault(m, {})[bucket] = {
                "requests": int(row["requests"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            model_totals[m] = model_totals.get(m, 0) + cell[m][bucket]["total_tokens"]

    if not model_totals:
        return out

    # 只保留区间内有 token 消耗的模型 —— 避免 45 个「零用量」模型刷爆
    # 按钮条/图例（这些模型只是历史遗留行，30 天内 0 调用 0 token）。
    ordered = sorted(model_totals, key=lambda m: model_totals[m], reverse=True)
    ordered = [m for m in ordered if model_totals[m] > 0]
    if not ordered:
        return out

    # 稳定色板（dark/light 下都清晰）：橙/青/紫/粉/蓝/绿/红 循环。
    MODEL_PALETTE = [
        "#f59e0b", "#06b6d4", "#8b5cf6", "#ec4899",
        "#3b82f6", "#22c55e", "#ef4444", "#f97316",
        "#14b8a6", "#a855f7", "#eab308", "#64748b",
    ]
    models_out: list[dict[str, object]] = []
    series_out: dict[str, list[dict[str, int]]] = {}
    items_out: list[dict[str, object]] = []
    for idx, m in enumerate(ordered):
        color = MODEL_PALETTE[idx % len(MODEL_PALETTE)]
        models_out.append({"model": m, "total": model_totals[m], "color": color})
        ser: list[dict[str, int]] = []
        for cal in calendar:
            day_ts = int(cal["ts"])
            cval = cell.get(m, {}).get(day_ts, {"requests": 0, "total_tokens": 0})
            ser.append({"date": cal["date"], "total_tokens": cval["total_tokens"]})
            if cval["total_tokens"] or cval["requests"]:
                items_out.append({
                    "model": m,
                    "date": cal["date"],
                    "requests": cval["requests"],
                    "total_tokens": cval["total_tokens"],
                })
        series_out[m] = ser

    days_out: list[dict[str, object]] = []
    for i, cal in enumerate(calendar):
        day_total = sum(series_out[m][i]["total_tokens"] for m in ordered)
        days_out.append({"date": cal["date"], "ts": cal["ts"], "total": day_total})

    out["models"] = models_out
    out["days"] = days_out
    out["series"] = series_out
    out["items"] = items_out
    return out


def fetch_upstream_quota_5h(
    db_path: str,
    *,
    upstreams: list[dict[str, object]],
) -> list[dict[str, object]]:
    """v0.100.1：⑦ 配额仪表盘数据 —— 每上游 5h 配额利用率。

    5h 配额概念与 range 无关（始终 5h 滚动窗口），与时间档切换无关。

    ``upstreams`` 是已配置的上游字典列表（每项含 name / quota_5h /
    billing_unit / model_multipliers / allowed_models），从 Settings 传。
    返回的 list 与 ``upstreams`` 同序，每项含：
    ``{upstream, billing_unit, quota_5h, used_5h, utilization_5h,
    warning_level, warning_text, model_breakdown}``。

    利用 v0.19 weighted cost（model_multipliers 计费）算出 utilization，
    与 tui.fetch_by_upstream_with_costs 行为完全一致。
    """
    out: list[dict[str, object]] = []
    if not Path(db_path).exists():
        return out
    for cfg in upstreams:
        name = cfg.get("name")
        if not name:
            continue
        # v0.119：虚拟 cfg 时 SQL 走 WHERE upstream IN (members)；真实
        # cfg 同款（[name] IN 列表），唯一区别是占位符数量。
        member_names = _resolve_names(cfg)
        if not member_names:
            continue
        quota = cfg.get("quota_5h") or 0
        billing = cfg.get("billing_unit") or "count"
        mults = cfg.get("model_multipliers") or {}
        mult = lambda m: float(mults.get(m, 1.0)) if m else 1.0   # noqa: E731
        # 5h 窗口：ts >= now - 18000
        cutoff = time.time() - 5 * 3600
        in_placeholders = ",".join("?" for _ in member_names)
        with _connect(db_path) as c:
            row = c.execute(
                f"SELECT COUNT(*), "
                f"COALESCE(SUM(input_tokens), 0), "
                f"COALESCE(SUM(output_tokens), 0), "
                f"COALESCE(SUM(cache_read_input_tokens), 0), "
                f"COALESCE(SUM(cache_creation_input_tokens), 0), "
                f"COALESCE(SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END), 0) "
                f"FROM requests "
                f"WHERE ts >= ? AND upstream IN ({in_placeholders})",
                (cutoff, *member_names),
            ).fetchone()
        if not row:
            # v0.119：虚拟 cfg 也要带 _is_virtual / linked_names 让 GUI
            # 渲染「链接」chip
            extra = {}
            if cfg.get("_is_virtual"):
                extra["_is_virtual"] = True
                extra["linked_names"] = list(cfg.get("linked_names") or [])
                extra["_primary"] = cfg.get("_primary") or ""
            out.append({
                "upstream": name, "billing_unit": billing,
                "quota_5h": quota, "used_5h": 0, "utilization_5h": None,
                "warning_level": "no_quota", "warning_text": "—",
                **extra,
            })
            continue
        n, it, ot, cr, cc, errs = row
        if billing == "token":
            used = float(it or 0) + float(ot or 0) + float(cr or 0) + float(cc or 0)
        else:
            used = float(n or 0)
        util = (used / quota) if quota and quota > 0 else None
        if util is None:
            warn_lv = "no_quota"
            warn_txt = "—"
        elif util >= 1.0:
            warn_lv = "exhausted"
            warn_txt = "已用尽"
        elif util >= 0.9:
            warn_lv = "critical"
            warn_txt = f"剩 {_format_eta(0 if util <= 0 else 0)}"
        elif util >= 0.7:
            warn_lv = "warn"
            warn_txt = "快用完"
        else:
            warn_lv = "ok"
            warn_txt = "正常"
        extra = {}
        if cfg.get("_is_virtual"):
            extra["_is_virtual"] = True
            extra["linked_names"] = list(cfg.get("linked_names") or [])
            extra["_primary"] = cfg.get("_primary") or ""
        out.append({
            "upstream": name,
            "billing_unit": billing,
            "quota_5h": quota,
            "used_5h": used,
            "utilization_5h": util,
            "warning_level": warn_lv,
            "warning_text": warn_txt,
            **extra,
        })
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    ap = argparse.ArgumentParser(
        prog="relay-dashboard",
        description="Live terminal dashboard for the token consumption relay.",
    )
    ap.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to relay.db (default: {DEFAULT_DB})",
    )
    ap.add_argument(
        "--relay-url",
        default=DEFAULT_RELAY_URL,
        help=f"Relay server base URL for /live (default: {DEFAULT_RELAY_URL})",
    )
    args = ap.parse_args()

    # Translate RELAY_DB env var if set (matches the server's config).
    import os
    db = os.environ.get("RELAY_DB", args.db)
    relay_url = os.environ.get("RELAY_URL", args.relay_url)

    app = DashboardApp(db, relay_url=relay_url)
    try:
        app.run()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    run()