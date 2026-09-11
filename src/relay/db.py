"""SQLite layer — aiosqlite + WAL, one connection per write.

Schema is intentionally narrow: one row per API call, regardless of how
many streaming events came back. Aggregation is done in SQL at query
time.

The optional `messages` table stores the actual conversation content
(request body + assembled response) so the user can search any past
dialogue. Enable with `RELAY_SAVE_MESSAGES=1`; storage cost scales with
your traffic, so it's opt-in.

Schema migrations
----------------

`init()` is idempotent. The CREATE TABLE uses the latest schema; then we
inspect `PRAGMA table_info(requests)` and ALTER TABLE ADD COLUMN for
anything missing. Old DBs from before multi-API support pick up the
new `upstream` / `api_key_alias` columns on first run. The `messages`
table is added in v0.4.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import aiosqlite

from .plugin import emit_event, run_hooks

from .models import StatsRow, UsageAcc


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    platform TEXT NOT NULL,
    model TEXT,
    request_id TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_input_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    status_code INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_platform_ts ON requests(platform, ts);
CREATE INDEX IF NOT EXISTS idx_request_id ON requests(request_id);
-- v0.143：客户端入口端点（chat / responses）拆分的复合索引在 init()
-- 里 ALTER TABLE 加完 endpoint 列后再建 —— 因为旧 DB 的 requests
-- 表还没有 endpoint 列，CREATE INDEX 不能引用不存在的列。这里不写
-- SCHEMA 里，由 init() 负责创建（保证 SCHEMA 自身对存量 DB 不出错）。

/* v0.4 — full conversation storage. One row per (request_id, role).
   `content` is the extracted plain text; `content_json` is the original
   JSON block (e.g. Anthropic content blocks, OpenAI tool calls) for
   non-text payloads like images. */
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,            /* 'user' | 'assistant' */
    content TEXT,                  /* extracted plain text */
    content_json TEXT,             /* raw JSON block, if applicable */
    FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_msg_request ON messages(request_id);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
"""

# Columns that may be added by later migrations. Listed in apply order.
MIGRATIONS: list[tuple[str, str]] = [
    ("upstream", "TEXT"),
    ("api_key_alias", "TEXT"),
    # v0.143：客户端入口 wire（platform 内部分桶键）。
    # 旧值 NULL（迁移前存的行）→ 由 init() 一次性回填到 'openai-chat'；
    # anthropic 行 NULL 是预期，COALESCE 让其走 platform 兜底聚合。
    ("endpoint", "TEXT"),
    # v0.155：客户端工具名（claude-code / opencode / codex / …），由
    # proxy 入口从 User-Agent 识别。v0.156 起：白名单命中存规范名；有 UA
    # 但没命中时从 UA 抽 token 动态建平台（如 Bun/1.3.14 → "bun"）；只有
    # 完全无 UA 的请求才存兜底桶 "其它"（原 "未知"，v0.156 改名 + 存量回填）。
    # 旧行 NULL 是预期（迁移前没有 UA 识别），聚合时 COALESCE 归入兜底桶。
    ("agent", "TEXT"),
    # v0.157：原始 User-Agent 头（未识别/归类前的原文）。供设置页「UA
    # 归类」列出真实出现过的 UA，让用户逐条配置 ua_rules 归类。旧行
    # NULL（迁移前没存），GUI 只展示存量之后新到的请求；不参与聚合。
    ("raw_ua", "TEXT"),
    # v0.NNN：思考流独立列 —— thinking 从 messages 的 role='thinking' 行
    # 迁到 requests.thinking（per-request 单块，天然落在 user/assistant
    # 之间，不再依赖插入/排序）。旧行由 init() 一次性回填 + 删旧行。
    ("thinking", "TEXT"),
]


async def _existing_columns(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute("PRAGMA table_info(requests)")
    rows = await cur.fetchall()
    return {row[1] for row in rows}


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as c:
            await c.executescript(SCHEMA)
            existing = await _existing_columns(c)
            for col, decl in MIGRATIONS:
                if col not in existing:
                    await c.execute(f"ALTER TABLE requests ADD COLUMN {col} {decl}")
            # v0.NNN：thinking 迁移 —— 把 messages 表 role='thinking' 行并入
            # requests.thinking，再删旧行。幂等：二次运行时已无 thinking 行可搬。
            # messages 表对极旧库可能不存在，包 try 兜底。
            try:
                await c.execute(
                    "UPDATE requests SET thinking = ("
                    "  SELECT m.content FROM messages m "
                    "  WHERE m.request_id = requests.id AND m.role = 'thinking' "
                    "  ORDER BY m.id ASC LIMIT 1"
                    ") WHERE thinking IS NULL"
                )
                await c.execute("DELETE FROM messages WHERE role = 'thinking'")
            except Exception:
                pass
            # v0.143：endpoint 列加完后建复合索引 + 一次性回填 openai 旧行。
            # 顺序：索引必须在 endpoint 列存在后再建（CREATE INDEX 不能引用
            # 不存在的列）；回填同样必须在列存在后再写 UPDATE。SQLite 的
            # ALTER TABLE / CREATE INDEX / UPDATE 都隐式开事务，下面 PRAGMA
            # journal_mode=WAL 必须在事务外执行（不能在同一连接里 WAL + DML
            # 混用），所以先提交一次。
            if "endpoint" in existing:
                # 列已存在（极旧的 DB 之前手工建过；或 init 重跑）—— 只补索引
                await c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_platform_endpoint_ts "
                    "ON requests(platform, endpoint, ts)"
                )
            else:
                # 全新迁移路径：建列 → 建索引 → 回填 openai 旧行。
                await c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_platform_endpoint_ts "
                    "ON requests(platform, endpoint, ts)"
                )
                # 一次性回填：把 openai 平台存量行标成 'openai-chat'。
                # 逻辑保证（迁移前所有 openai 入向都是 /v1/chat/completions，
                # 见 .relay-logs/* uvicorn 访问日志统计）。anthropic 行 NULL
                # 不回填：聚合时 COALESCE 让它们走 platform 兜底，视觉上
                # 仍然是 'anthropic' 一行。
                await c.execute(
                    "UPDATE requests SET endpoint = 'openai-chat' "
                    "WHERE platform = 'openai' AND endpoint IS NULL"
                )
            # v0.156：agent 兜底桶改名「未知」→「其它」（有 UA 的请求改为
            # 动态抽 token 建新平台，只有完全无 UA 才归兜底桶）。一次性回填
            # 存量「未知」行到新桶名（幂等：二次运行时已无「未知」行可改）。
            # 只对列已存在的库做 —— 全新迁移时表是空的，不必跑。
            if "agent" in existing:
                await c.execute(
                    "UPDATE requests SET agent = '其它' WHERE agent = '未知'"
                )
            await c.commit()
            await c.execute("PRAGMA journal_mode=WAL")
            await c.execute("PRAGMA synchronous=NORMAL")
            await c.execute("PRAGMA busy_timeout=5000")
            await c.commit()

    @staticmethod
    def ensure_schema(path: str | Path) -> None:
        """Sync version of `init()` for non-async callers (GUI / TUI).

        Runs the CREATE TABLE IF NOT EXISTS statements from SCHEMA. Idempotent
        and safe to call while the relay server is also running — it only
        creates tables that don't exist yet and never touches existing ones.

        Does NOT run column migrations (ALTER TABLE ADD COLUMN) or PRAGMA
        tuning — those are the server's job, since the GUI / TUI only
        consume the DB. SQLite file is created on first `connect` if it
        doesn't exist.
        """
        import sqlite3
        with sqlite3.connect(str(path)) as c:
            c.executescript(SCHEMA)
            c.commit()

    async def record(
        self,
        *,
        platform: str,
        model: str | None,
        request_id: str | None,
        usage: UsageAcc,
        status_code: int,
        error: str | None,
        upstream: str | None = None,
        api_key_alias: str | None = None,
        # v0.143：客户端入口 wire（chat / responses / messages）。
        # None → INSERT NULL（罕见拒绝行：_reject_dispatch 写 platform
        # 占位；正常路径 proxy_legacy 都会传 client_wire）。
        endpoint: Optional[str] = None,
        # v0.155：客户端工具名（由 proxy 入口从 User-Agent 识别）。v0.156：
        # 白名单命中存规范名，有 UA 未命中抽 token 动态建平台，只有完全无
        # UA 才传兜底桶 "其它"。v0.157：resolve_agent 先查用户 ua_rules。
        # None → INSERT NULL（迁移前的旧行）。
        agent: Optional[str] = None,
        # v0.157：原始 User-Agent 头原文（供设置页列出真实 UA 让用户归类）。
        # None → INSERT NULL（无 UA 头 / 旧路径）。
        raw_ua: Optional[str] = None,
    ) -> int:
        """Insert one row. Returns the new row id (used by `record_messages`)."""
        # v0.98.2 决策钩子：before_quota_deduct —— 插件可修改 token 计数
        #（影响 5h 配额）或返回 False 跳过本次记录（不扣配额）。
        info = {
            "platform": platform,
            "model": model,
            "request_id": request_id,
            "usage": usage,
            "status_code": status_code,
            "error": error,
            "upstream": upstream,
            "api_key_alias": api_key_alias,
            "endpoint": endpoint,
            "agent": agent,
            "raw_ua": raw_ua,
        }
        veto = await run_hooks("before_quota_deduct", info)
        if veto is False:
            return 0
        # Serialize writers — SQLite WAL allows many readers but only one writer.
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute(
                    """
                    INSERT INTO requests(
                        ts, platform, model, request_id,
                        input_tokens, output_tokens,
                        cache_read_input_tokens, cache_creation_input_tokens,
                        status_code, error,
                        upstream, api_key_alias,
                        endpoint, agent, raw_ua
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        platform,
                        model,
                        request_id,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_read_input_tokens,
                        usage.cache_creation_input_tokens,
                        status_code,
                        error,
                        upstream,
                        api_key_alias,
                        endpoint,
                        agent,
                        raw_ua,
                    ),
                )
                await c.commit()
                row_id = cur.lastrowid or 0
                emit_event(
                    "db.recorded",
                    platform=platform,
                    model=model,
                    status_code=status_code,
                    error=error,
                    upstream=upstream,
                    api_key_alias=api_key_alias,
                    agent=agent,
                    request_id=request_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    row_id=row_id,
                )
                return row_id

    async def record_messages(
        self,
        request_id: int,
        *,
        user_text: Optional[str] = None,
        user_json: Optional[str] = None,
        assistant_text: Optional[str] = None,
        assistant_json: Optional[str] = None,
        thinking_text: Optional[str] = None,
    ) -> None:
        """Persist the user prompt + assembled assistant reply (+ thinking).

        v0.NNN：thinking 重写 —— 思考流不再作为 messages 的 role='thinking'
        独立行（旧方案依赖插入/排序，历史页渲染不可靠），改为写进
        ``requests.thinking`` 单列（per-request 单块）。流式过程中可能多次
        调用本方法，每次 UPDATE 覆盖成当前累计值即可。

        Either side may be omitted (e.g. the request body was malformed or
        the upstream stream was cut short). Empty strings are not stored —
        we just skip that row.
        """
        ts = time.time()
        rows = []
        if user_text or user_json:
            rows.append((request_id, ts, "user", user_text, user_json))
        if assistant_text or assistant_json:
            rows.append((request_id, ts, "assistant", assistant_text, assistant_json))
        if not rows and not thinking_text:
            return
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                if rows:
                    await c.executemany(
                        "INSERT INTO messages(request_id, ts, role, content, content_json) "
                        "VALUES(?, ?, ?, ?, ?)",
                        rows,
                    )
                if thinking_text:
                    await c.execute(
                        "UPDATE requests SET thinking = ? WHERE id = ?",
                        (thinking_text, request_id),
                    )
                await c.commit()

    async def search_messages(
        self,
        query: str,
        *,
        limit: int = 100,
        platform: Optional[str] = None,
    ) -> list[dict]:
        """LIKE-based search over saved messages. Returns most recent first."""
        like = f"%{query}%"
        clauses = ["m.content LIKE ?"]
        params: list[object] = [like]
        if platform:
            clauses.append("r.platform = ?")
            params.append(platform)
        where = " AND ".join(clauses)
        async with aiosqlite.connect(self.path) as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute(
                f"""
                SELECT m.id, m.request_id, m.ts, m.role, m.content,
                       r.platform, r.model, r.status_code, r.error,
                       substr(m.content, 1, 200) AS snippet
                FROM messages m
                JOIN requests r ON r.id = m.request_id
                WHERE {where}
                ORDER BY m.ts DESC
                LIMIT ?
                """,
                (*params, limit),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_conversation(self, request_id: int) -> Optional[dict]:
        """Return one request row + all its saved messages, or None if absent.

        Used by the GUI's "view conversation" dialog (double-click on the
        recent table). Messages are returned in insertion order so a request
        row's user message comes before its assistant reply. Thinking 块在
        ``request.thinking``（per-request 单列，前端在 user/assistant 之间
        渲染）。
        """
        async with aiosqlite.connect(self.path) as c:
            c.row_factory = aiosqlite.Row
            req = await (
                await c.execute(
                    "SELECT id, ts, platform, model, request_id, "
                    "input_tokens, output_tokens, "
                    "cache_read_input_tokens, cache_creation_input_tokens, "
                    "status_code, error, upstream, thinking "
                    "FROM requests WHERE id = ?",
                    (request_id,),
                )
            ).fetchone()
            if req is None:
                return None
            msgs = await (
                await c.execute(
                    "SELECT role, ts, content, content_json "
                    "FROM messages WHERE request_id = ? "
                    "ORDER BY "
                    "  CASE role WHEN 'user' THEN 0 WHEN 'thinking' THEN 1 WHEN 'assistant' THEN 2 ELSE 3 END, "
                    "  id ASC",
                    (request_id,),
                )
            ).fetchall()
        return {
            "request": dict(req),
            "messages": [dict(m) for m in msgs],
        }

    async def delete_requests_before(self, ts: float) -> int:
        """Delete request rows older than ``ts`` (plus their cascade messages).

        v0.113n 存储管理 —— 按时间清理历史消息。``messages`` 行随
        ``requests`` 级联删除，无需单独处理。返回删除的请求条数。
        """
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute("DELETE FROM requests WHERE ts < ?", (ts,))
                await c.commit()
                return cur.rowcount or 0

    async def delete_all_requests(self) -> int:
        """Delete every request row (and cascade every saved message).

        v0.113n 存储管理 —— 一键清空全部消息记录。"""
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute("DELETE FROM requests")
                await c.commit()
                return cur.rowcount or 0

    async def delete_request_rows_by_upstream(self, upstream: str) -> int:
        """Delete every request row for one upstream name, plus its messages.

        历史上游「彻底删除」—— 按上游名删行。messages 行随 requests 一起
        清掉（本工程从不开 PRAGMA foreign_keys，FK 级联不生效，所以这里
        显式先删 messages 再删 requests，保证该上游的对话内容也一并剥离）。
        返回删除的请求条数（0 = 该名本就没有行，幂等）。"""
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute(
                    "DELETE FROM messages WHERE request_id IN ("
                    "  SELECT id FROM requests WHERE upstream = ?"
                    ")",
                    (upstream,),
                )
                cur2 = await c.execute(
                    "DELETE FROM requests WHERE upstream = ?", (upstream,)
                )
                await c.commit()
                return cur2.rowcount or 0

    async def vacuum(self) -> None:
        """VACUUM the database to reclaim space freed by deletions.

        SQLite does not shrink the file on DELETE; deleted pages stay
        allocated until a VACUUM. Safe only when no other connection is
        mid-write, which the relay serializes through ``_lock``.
        """
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                await c.execute("VACUUM")
                await c.commit()

    async def delete_messages_between(self, start_ts: float, end_ts: float) -> int:
        """Delete message rows with ts in [start_ts, end_ts).

        v0.NNN 设置页「清理原文」按天清除 —— 只删 messages（原文），保留
        requests（请求统计/令牌消耗还在）。thinking 存于 requests 单列，
        这里一并清掉（否则对话视图只剩 thinking 没有正文）。返回删除的
        message 条数。
        """
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                # 先清受影响 requests 的 thinking，再删 messages。
                await c.execute(
                    "UPDATE requests SET thinking = NULL WHERE id IN ("
                    "  SELECT DISTINCT request_id FROM messages WHERE ts >= ? AND ts < ?"
                    ")",
                    (start_ts, end_ts),
                )
                cur = await c.execute(
                    "DELETE FROM messages WHERE ts >= ? AND ts < ?",
                    (start_ts, end_ts),
                )
                await c.commit()
                return cur.rowcount or 0

    async def delete_message_by_id(self, message_id: int) -> int:
        """Delete a single message row by its id (清理原文右键「删除此条」)."""
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                cur = await c.execute(
                    "DELETE FROM messages WHERE id = ?", (message_id,)
                )
                await c.commit()
                return cur.rowcount or 0

    async def delete_messages_by_upstream(self, upstream: str) -> int:
        """Delete every message row whose request went to ``upstream``.

        清理原文「按上游筛选」的全部清除 —— 只删 messages，保留 requests。
        同样清掉该上游 requests 的 thinking。
        """
        async with self._lock:
            async with aiosqlite.connect(self.path) as c:
                await c.execute(
                    "UPDATE requests SET thinking = NULL WHERE upstream = ?",
                    (upstream,),
                )
                cur = await c.execute(
                    "DELETE FROM messages WHERE request_id IN ("
                    "  SELECT id FROM requests WHERE upstream = ?"
                    ")",
                    (upstream,),
                )
                await c.commit()
                return cur.rowcount or 0

    async def aggregate(
        self, platform: str | None = None, since: float | None = None
    ) -> dict[str, StatsRow]:
        """Return per-platform aggregate rows. Optionally filtered by platform and timestamp."""
        clauses: list[str] = []
        params: list[object] = []
        if platform is not None:
            clauses.append("platform = ?")
            params.append(platform)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        async with aiosqlite.connect(self.path) as c:
            if platform is not None:
                rows = await (
                    await c.execute(
                        f"""
                        SELECT COUNT(*),
                               COALESCE(SUM(input_tokens), 0),
                               COALESCE(SUM(output_tokens), 0),
                               COALESCE(SUM(cache_read_input_tokens), 0),
                               COALESCE(SUM(cache_creation_input_tokens), 0),
                               COALESCE(SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END), 0)
                        FROM requests {where}
                        """,
                        params,
                    )
                ).fetchall()
                return {
                    platform: StatsRow(
                        requests=row[0] or 0,
                        input_tokens=row[1] or 0,
                        output_tokens=row[2] or 0,
                        cache_read_input_tokens=row[3] or 0,
                        cache_creation_input_tokens=row[4] or 0,
                        errors=row[5] or 0,
                    )
                    for row in rows
                }
            else:
                rows = await (
                    await c.execute(
                        f"""
                        SELECT platform,
                               COUNT(*),
                               COALESCE(SUM(input_tokens), 0),
                               COALESCE(SUM(output_tokens), 0),
                               COALESCE(SUM(cache_read_input_tokens), 0),
                               COALESCE(SUM(cache_creation_input_tokens), 0),
                               COALESCE(SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END), 0)
                        FROM requests {where}
                        GROUP BY platform
                        """,
                        params,
                    )
                ).fetchall()
                out: dict[str, StatsRow] = {}
                for row in rows:
                    out[row[0]] = StatsRow(
                        requests=row[1] or 0,
                        input_tokens=row[2] or 0,
                        output_tokens=row[3] or 0,
                        cache_read_input_tokens=row[4] or 0,
                        cache_creation_input_tokens=row[5] or 0,
                        errors=row[6] or 0,
                    )
                return out
