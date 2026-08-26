"""Independent SQLite layer for passthrough mode.

Mirrors ``relay.db.Database`` (aiosqlite + WAL + ``_lock`` for serialized
writes) but lives in a separate file and a separate sqlite file. No shared
schema, no shared connection pool — passthrough bookkeeping is fully
isolated from the conversion / dispatch path so a bug here can't corrupt
``relay.db``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS passthrough_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    url TEXT NOT NULL,
    api_key_alias TEXT NOT NULL,
    model_field_name TEXT NOT NULL DEFAULT 'model',
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    status_code INTEGER,
    error TEXT,
    request_method TEXT,
    request_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_pt_ts ON passthrough_requests(ts);
CREATE INDEX IF NOT EXISTS idx_pt_url ON passthrough_requests(url);

CREATE TABLE IF NOT EXISTS passthrough_upstreams (
    url TEXT NOT NULL,
    api_key_alias TEXT NOT NULL,
    model_field_name TEXT NOT NULL DEFAULT 'model',
    display_name TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    request_count INTEGER DEFAULT 0,
    PRIMARY KEY (url, api_key_alias, model_field_name)
);
"""


class PassthroughDatabase:
    """Independent SQLite store for passthrough mode.

    Schema is narrow by design: one row per passthrough call, no per-token
    messages table. Aggregation is done in SQL at query time.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        """Create tables / indexes if missing. Idempotent."""
        # 自定义路径（.env RELAY_PASSTHROUGH_DB）的父目录可能不存在；
        # 不建的话 aiosqlite 打开直接抛错，lifespan 启动失败。
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        async with aiosqlite.connect(self.path) as c:
            await c.executescript(SCHEMA)
            await c.execute("PRAGMA journal_mode=WAL")
            await c.execute("PRAGMA synchronous=NORMAL")
            await c.execute("PRAGMA busy_timeout=5000")
            await c.commit()

    async def upsert_upstream(
        self,
        *,
        url: str,
        api_key_alias: str,
        model_field_name: str,
        display_name: Optional[str] = None,
    ) -> None:
        """Auto-discover or refresh one upstream row.

        First-seen is preserved on conflict; last_seen + request_count are
        bumped. ``display_name`` is only set on insert (so a later rename
        reset to NULL doesn't get clobbered by an old ``None``).
        """
        now = time.time()
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                # SQLite's ON CONFLICT upsert: insert with first_seen=now; on
                # conflict keep first_seen, bump last_seen + request_count.
                # display_name is set only when caller provides a value AND
                # the row is brand new (display_name IS NULL).
                await c.execute(
                    """
                    INSERT INTO passthrough_upstreams
                        (url, api_key_alias, model_field_name, display_name,
                         first_seen, last_seen, request_count)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(url, api_key_alias, model_field_name) DO UPDATE SET
                        last_seen = excluded.last_seen,
                        request_count = request_count + 1,
                        display_name = COALESCE(
                            passthrough_upstreams.display_name,
                            excluded.display_name
                        )
                    """,
                    (url, api_key_alias, model_field_name, display_name, now, now),
                )
                await c.commit()

    async def delete_before(self, ts: float) -> int:
        """Delete passthrough request rows older than ``ts``.

        v0.113n 存储管理 —— 按时间清理透传记录。``passthrough_upstreams``
        是发现表（不清），只清 ``passthrough_requests``。返回删除条数。
        """
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute(
                    "DELETE FROM passthrough_requests WHERE ts < ?", (ts,)
                )
                await c.commit()
                return cur.rowcount or 0

    async def delete_all(self) -> int:
        """Delete every passthrough request row. Returns deleted count."""
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute("DELETE FROM passthrough_requests")
                await c.commit()
                return cur.rowcount or 0

    async def vacuum(self) -> None:
        """VACUUM to reclaim space freed by deletions."""
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                await c.execute("VACUUM")
                await c.commit()

    async def rename_upstream(
        self,
        *,
        url: str,
        api_key_alias: str,
        model_field_name: str,
        display_name: str,
    ) -> bool:
        """Set ``display_name`` for one upstream. Empty string clears it."""
        clean = display_name.strip() or None
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute(
                    """
                    UPDATE passthrough_upstreams
                       SET display_name = ?
                     WHERE url = ? AND api_key_alias = ? AND model_field_name = ?
                    """,
                    (clean, url, api_key_alias, model_field_name),
                )
                await c.commit()
                return cur.rowcount > 0

    async def list_upstreams(self) -> list[dict]:
        """Return all discovered upstreams, newest first.

        ``api_key_alias`` is left intact (needed by GUI for rename / future
        edit); the HTTP layer masks it before returning to JS.
        """
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(
                """
                SELECT url, api_key_alias, model_field_name, display_name,
                       first_seen, last_seen, request_count
                  FROM passthrough_upstreams
                 ORDER BY last_seen DESC
                """
            )
            rows = await cur.fetchall()
        return [
            {
                "url": r[0],
                "api_key_alias": r[1],
                "model_field_name": r[2],
                "display_name": r[3],
                "first_seen": r[4],
                "last_seen": r[5],
                "request_count": r[6],
            }
            for r in rows
        ]

    async def record(
        self,
        *,
        url: str,
        api_key_alias: str,
        model_field_name: str,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int,
        cache_creation_input_tokens: int,
        status_code: Optional[int],
        error: Optional[str],
        request_method: Optional[str] = None,
        request_path: Optional[str] = None,
    ) -> int:
        """Insert one passthrough request row. Returns new row id."""
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute(
                    """
                    INSERT INTO passthrough_requests
                        (ts, url, api_key_alias, model_field_name, model,
                         input_tokens, output_tokens,
                         cache_read_input_tokens, cache_creation_input_tokens,
                         status_code, error, request_method, request_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        url,
                        api_key_alias,
                        model_field_name,
                        model,
                        input_tokens,
                        output_tokens,
                        cache_read_input_tokens,
                        cache_creation_input_tokens,
                        status_code,
                        error,
                        request_method,
                        request_path,
                    ),
                )
                await c.commit()
                return cur.lastrowid or 0

    async def aggregate_by_dim(
        self,
        *,
        dim: str = "upstream",
        since: Optional[float] = None,
    ) -> list[dict]:
        """Group rows by ``dim`` and sum token fields + count requests.

        ``dim`` ∈ {"upstream", "model", "model_field"}:
          - upstream:  group by (url + model_field_name)
          - model:     group by model value
          - model_field: group by model_field_name (the JSON key used)
        """
        if dim == "upstream":
            group_cols = "url, model_field_name"
            key = "url || '|' || model_field_name"
        elif dim == "model":
            group_cols = "model"
            key = "COALESCE(model, '(unknown)')"
        elif dim == "model_field":
            group_cols = "model_field_name"
            key = "model_field_name"
        else:
            raise ValueError(f"unknown dim: {dim!r}")

        where = "WHERE ts >= ?" if since is not None else ""
        params: tuple = (since,) if since is not None else ()

        sql = f"""
            SELECT {group_cols},
                   COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens,
                   COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errors
              FROM passthrough_requests
              {where}
             GROUP BY {group_cols}
             ORDER BY requests DESC
        """
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(sql, params)
            rows = await cur.fetchall()

        out: list[dict] = []
        # dim=upstream 时 SELECT 多一列（url, model_field_name, ...），
        # 聚合列整体后移一位；其余 dim 只有 1 个分组列。
        offset = 1 if dim == "upstream" else 0
        for r in rows:
            in_t = int(r[2 + offset] or 0)
            out_t = int(r[3 + offset] or 0)
            cr_t = int(r[4 + offset] or 0)
            cc_t = int(r[5 + offset] or 0)
            total = in_t + out_t + cr_t + cc_t
            out.append(
                {
                    "key": f"{r[0]}|{r[1]}" if dim == "upstream" else r[0],
                    "requests": int(r[1 + offset]),
                    "input_tokens": in_t,
                    "output_tokens": out_t,
                    "cache_read_input_tokens": cr_t,
                    "cache_creation_input_tokens": cc_t,
                    "errors": int(r[6 + offset]),
                    "total_tokens": total,
                }
            )
        return out

    async def fetch_daily(self, *, days: int = 30) -> list[dict]:
        """Per-day totals for the most recent ``days`` days.

        Returns one row per day that has at least one record (no zero-fill
        gaps) so the frontend can stitch its own continuous timeline.
        """
        sql = """
            SELECT CAST(ts / 86400 AS INTEGER) * 86400 AS day_bucket,
                   COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read_input_tokens,
                   COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_creation_input_tokens
              FROM passthrough_requests
             GROUP BY day_bucket
             ORDER BY day_bucket DESC
             LIMIT ?
        """
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(sql, (days,))
            rows = await cur.fetchall()
        return [
            {
                "ts": float(r[0]),
                "requests": int(r[1]),
                "input_tokens": int(r[2]),
                "output_tokens": int(r[3]),
                "cache_read_input_tokens": int(r[4]),
                "cache_creation_input_tokens": int(r[5]),
            }
            for r in rows
        ]

    async def fetch_by_hour(self, *, since: float | None = None, bucket: int = 3600) -> list[dict]:
        """Hour-bucketed totals, same shape as ``tui.fetch_by_hour`` so the
        frontend can mix relay + passthrough series on one chart.

        Returns ``[{hour, requests, in_tokens, out_tokens, tokens}]``
        ascending, unzero-filled (frontend pads).
        """
        if since is None:
            since = 24 * 3600
        if not Path(self.path).exists():
            return []
        cutoff = time.time() - since
        sql = (
            "SELECT (CAST(ts AS INT) / ?) * ? AS hour_bucket, "
            "COUNT(*) AS req_count, "
            "COALESCE(SUM(input_tokens), 0) AS in_sum, "
            "COALESCE(SUM(output_tokens), 0) AS out_sum "
            "FROM passthrough_requests "
            "WHERE ts >= ? "
            "GROUP BY hour_bucket "
            "ORDER BY hour_bucket"
        )
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(sql, (bucket, bucket, cutoff))
            rows = await cur.fetchall()
        return [
            {
                "hour": float(r[0]),
                "requests": int(r[1]),
                "in_tokens": int(r[2]),
                "out_tokens": int(r[3]),
                "tokens": int(r[2] + r[3]),
            }
            for r in rows
        ]

    async def fetch_recent(self, *, limit: int = 50) -> list[dict]:
        """Return the most recent N rows for the live panel / debug."""
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(
                """
                SELECT id, ts, url, api_key_alias, model_field_name, model,
                       input_tokens, output_tokens, status_code, error
                  FROM passthrough_requests
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "ts": float(r[1]),
                "url": r[2],
                "api_key_alias": r[3],
                "model_field_name": r[4],
                "model": r[5],
                "input_tokens": int(r[6] or 0),
                "output_tokens": int(r[7] or 0),
                "status_code": r[8],
                "error": r[9],
            }
            for r in rows
        ]