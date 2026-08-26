"""Token Manager GUI — pywebview shell + data bridge.

The previous Tk implementation (``App``/``StatCard``/``PillButton``/
``GlassCard``/``NavItem``/``PillBadge``/``HeroGreeting``/``Sidebar``/
``WindowTab``/``StatusDot``/``ModelUsageBar``/``MetricBar``/
``FadeRow``/``ConversationDialog``) was deleted along with
``gui_theme.py`` and ``gui_animation.py`` because pywebview now owns
every pixel. All rendering lives in ``relay/web/{index.html,app.js,
styles.css}``; this module exposes a thin ``Api`` to JS and keeps a
snapshot cache fresh on a daemon thread.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import webview

from relay.config import (
    Settings,
    add_upstream,
    apply_quota_edit,
    get_settings,
    reload_settings,
    remove_upstream as remove_upstream_cfg,
    replace_quick_switch,
    save_agent_aliases,
    save_ua_rules,
    save_upstreams_json,
    set_upstream_model as set_upstream_model_cfg,
    set_upstream_default_model as set_upstream_default_model_cfg,
)
from relay.tui import PLATFORMS
from relay.services.link_resolver import resolve_links


# Directory that holds the static frontend assets bundled with the package.
_WEB_DIR = Path(__file__).resolve().parent / "web"

# v0.141：WebView2 持久化目录。pywebview 的 private_mode 默认 True ——
# localStorage/cookies 落进临时目录，重启即丢（顶栏「关闭顶部调试栏」
# 等 prefs 设置页偏好全部失效）。关掉 private_mode 并固定 storage_path，
# localStorage 才能跨重启保留。目录放在 %LOCALAPPDATA%\Relay\webview-data，
# 与 relay.db / upstreams.json 同根，统一管理。
_APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Relay"
_WEBVIEW_DATA_DIR = _APP_DATA_DIR / "webview-data"

# How often the Python polling thread rebuilds the snapshot dict.
# JS also polls every 500 ms (matches the Tk version's `after(500, ...)`)
# so the two cadences align and the UI feels the same.
_POLL_INTERVAL = 0.5

# 侧栏磁吸（v0.97）：拖侧栏到主窗右缘 ≤_SNAP_THRESHOLD px 且纵向有重叠
# 就自动磁吸 dock（右缘对齐 + 高度同步 + 随后随主窗移动）；拖出阈值则
# 脱开自由浮动。_SNAP_SLACK 是纵向重叠判定的放宽量。
_SNAP_THRESHOLD = 40
_SNAP_SLACK = 20

# ``owner`` field values returned by ``Api.get_status`` and read by the
# JS frontend + the stop/restart refusal guard. Single-sourced as
# constants so a typo here can't silently re-enable the kill path
# against the live session transport on :8088.
OWNER_SELF = "self"
OWNER_EXTERNAL = "external"
OWNER_NONE = "none"

# Diagnostic logger. ``run()`` configures the handler/level when the
# caller passes ``--diag``; otherwise the logger is silent and the hot
# path stays zero-cost.
_logger = logging.getLogger("relay.gui")
_DIAG_LOG_PATH = Path(__file__).resolve().parents[2] / "relay-gui.log"

# v0.107: 启动计时。每次 GUI 启动都在 stderr 打一条带时间戳的进度行
# （PyCharm 控制台直接可见，不需要 --diag），用来定位"按下运行后半分钟
# 才出窗口"这类启动变慢问题 —— 最可能的时间都耗在单实例接管
# （_acquire_exclusive 等旧实例退出）或 force-replace 的 PowerShell 扫描上。
_boot_t0 = time.monotonic()


def _boot_tick(label: str) -> None:
    """Print ``[boot] t+{s:.2f}s {label}`` to stderr. Never raises."""
    try:
        print(
            f"[boot] t+{time.monotonic() - _boot_t0:6.2f}s {label}",
            file=sys.stderr,
            flush=True,
        )
    except Exception:
        pass


# Theme presets. Three presets cycle in ``_on_toggle_theme``: a cool
# neutral "light", a warm cream "day", and a deep "dark". Anything
# outside this whitelist falls back to "light" so a corrupted .env
# can't lock the GUI on an unknown value.
_THEMES = ("light", "day", "dark")


def _resolve_theme(name: str) -> str:
    return name if name in _THEMES else "light"


def _hex_bg(name: str) -> str:
    # The native WinForms window only needs *some* opaque colour to
    # render behind the WebView2 — the actual painted surface comes
    # from CSS, so picking close-but-not-pixel-perfect equivalents is
    # fine and avoids duplicating the full palette.
    if name == "dark":
        return "#0a0a0a"
    if name == "day":
        return "#fdf6ec"
    return "#f5f5f7"


# How long a /healthz probe may block the JS poll before we call the
# relay unreachable. Loopback answers in single-digit ms, so 0.4s only
# trips on a genuinely wedged server.
_HEALTH_TIMEOUT = 0.4

# v0.9: health-probe TTL cache. ``get_status`` runs on every 500 ms JS
# poll tick and each call did a real HTTP GET /healthz (0.4 s worst-case
# block on a busy relay) through the SAME bridge thread pool that handles
# button callbacks — a busy relay could stall every UI interaction. The
# health signal doesn't change faster than ~1 s, so cache it. Keyed by
# base_url (only one in practice).
_HEALTH_PROBE_TTL = 1.0
_probe_cache: dict[str, tuple[float, bool]] = {}

# The listener-PID lookup shells out to netstat/lsof, which is far too
# expensive to run on every 500ms poll tick. Cache it — a relay's PID
# doesn't change without a restart, and a stale-by-5s PID in the topbar
# is harmless.
_PID_CACHE_TTL = 5.0
_pid_cache: dict[int, tuple[float, int | None]] = {}
# v0.11.2: the GUI runs under pythonw at login (no console). Spawning
# console helpers (netstat every 5s for the PID display, taskkill on
# takeover) then flashes a console window each time. CREATE_NO_WINDOW
# suppresses it; 0 on POSIX is a harmless no-op.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
# Hard ceiling on how long a single netstat lookup may keep the bridge
# busy. pywebview marshals ``Api.get_status`` through the same thread
# pool that handles button callbacks, so a wedged ``netstat`` (rare but
# observed when many ephemeral TCP sockets churn) freezes every UI
# interaction. Running the probe in a dedicated worker and timing out at
# 1.5 s keeps the worst-case bridge stall bounded.
_PID_LOOKUP_TIMEOUT = 1.5
_pid_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="relay-pid-lookup"
)

# The passthrough-mode GET/PUT also round-trips to the relay over loopback.
# The relay event loop is single-threaded per worker, so while it is mid-
# stream on a slow upstream response (a big vision image can hold it for
# several seconds), a fresh /api/passthrough/mode request queues behind that
# stream. A tight sync ``urlopen(timeout=2.0)`` from the GUI bridge thread
# then fires "timed out" even though the relay is fine — exactly the busy
# window that caused the v0.191 "切换失败: timed out" popup. Run the request
# on a dedicated worker and give it a generous ceiling so the GUI thread
# stays responsive and the busy-stream window is absorbed, not a failure.
_PASSTHROUGH_HTTP_TIMEOUT = 15.0
_passthrough_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="relay-passthrough-http"
)


def _probe_relay(base_url: str, timeout: float = _HEALTH_TIMEOUT) -> bool:
    """True if the relay at ``base_url`` answers ``/healthz`` with 200.

    This is the canonical "is the relay up?" signal, deliberately chosen
    over ``ServerProcess.is_running``: the relay is commonly started by
    the Windows autostart entry, a terminal, or a previous GUI session,
    and in all of those cases this process owns no child yet the relay
    is perfectly healthy. Keying status off child ownership made the GUI
    report "已停止" while 8088 was serving traffic.

    Probing the HTTP endpoint rather than merely connecting to the port
    also catches the wedged case — bound socket, dead app.

    Results are cached for ``_HEALTH_PROBE_TTL`` seconds so the 2 Hz poll
    doesn't hammer the relay with a fresh TCP connection every tick.
    """
    now = time.monotonic()
    cached = _probe_cache.get(base_url)
    if cached is not None and now - cached[0] < _HEALTH_PROBE_TTL:
        return cached[1]
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=timeout) as r:
            result = r.status == 200
    except Exception:
        # Connection refused, timeout, non-200, malformed response — all
        # mean "not usable" as far as the UI is concerned.
        result = False
    _probe_cache[base_url] = (now, result)
    return result


def _start_debug_http(app: "App") -> "http.server.HTTPServer | None":
    """Spin up a localhost-only HTTP server on 127.0.0.1:8089 that routes
    ``GET /<view>`` to ``app.switch_view(view)``. Used for unattended
    verification of sidebar-nav wiring — see
    ``feedback_unattended_verification.md`` for the rationale.

    Returns the running server, or ``None`` if the port is in use (the
    caller ignores that and falls back to driving the UI by hand).
    """
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
            view = self.path.lstrip("/").split("?", 1)[0]
            if app.switch_view(view):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"ok:switched:{view}\n".encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args):  # silence default stderr noise
            return

    try:
        httpd = http.server.HTTPServer(("127.0.0.1", 8089), _Handler)
    except OSError as exc:
        _logger.warning("_start_debug_http bind 8089 failed: %r", exc)
        return None
    threading.Thread(
        target=httpd.serve_forever, daemon=True, name="relay-gui-debug-http"
    ).start()
    _logger.info("_start_debug_http listening on 127.0.0.1:8089")
    return httpd


def _listener_pid(port: int) -> int | None:
    """PID of whatever process listens on ``port``, or None.

    READ-ONLY BY DESIGN. This looks the PID up so the topbar can show
    it; it must never be wired to a kill path. The relay on 8088 is the
    live session's transport — see the "external process" branches in
    ``Api.stop_server`` / ``restart_server``, which refuse rather than
    take the port over the way the old Tk GUI did.

    Cached for ``_PID_CACHE_TTL``: this shells out, and the JS side polls
    ``get_status`` at 2Hz. A PID that's up to 5s stale is harmless for a
    display-only field.
    """
    now = time.time()
    hit = _pid_cache.get(port)
    if hit is not None and now - hit[0] < _PID_CACHE_TTL:
        return hit[1]
    # ``netstat`` runs on a dedicated worker so a wedged lookup cannot
    # freeze the pywebview bridge thread (and with it every button click).
    # A future timeout degrades gracefully: the previous cached value (or
    # ``None`` on the very first call) is returned, and the cache is left
    # untouched so the next tick retries — the PID is a display field, so
    # a stale row beats a 1.5 s UI freeze every time.
    pid: int | None = None
    try:
        future = _pid_executor.submit(_do_listener_lookup, port)
        pid = future.result(timeout=_PID_LOOKUP_TIMEOUT)
    except concurrent.futures.TimeoutError:
        return hit[1] if hit is not None else None
    except Exception:
        # netstat/lsof missing, permission denied, unparseable output —
        # the PID is cosmetic, so degrade to "—" rather than raise into
        # the poll loop.
        pid = None
    _pid_cache[port] = (now, pid)
    return pid


def _do_listener_lookup(port: int) -> int | None:
    """Body of ``_listener_pid`` — runs on the ``_pid_executor`` worker."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, check=False, timeout=2.0,
            creationflags=_NO_WINDOW,
        ).stdout
        # "  TCP  127.0.0.1:8088  0.0.0.0:0  LISTENING  8944"
        # Match on the ``:port`` suffix so IPv4/IPv6/loopback all hit.
        pat = re.compile(rf":{port}\s.*\sLISTENING\s+(\d+)", re.IGNORECASE)
        for line in out.splitlines():
            m = pat.search(line)
            if m:
                return int(m.group(1))
        return None
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True, check=False, timeout=2.0,
    ).stdout.strip()
    if out:
        return int(out.splitlines()[0])
    return None


def _http_get_json(base_url: str, path: str, timeout: float = 2.0) -> dict:
    """GET ``base_url + path`` and parse the JSON body.

    Runs on the ``_passthrough_executor`` worker, off the GUI bridge thread.
    ``timeout`` is the per-request socket timeout inside the worker; it can
    be looser than the old bridge-thread value because a slow relay no
    longer stalls the UI. Any failure raises, which the caller maps to an
    ``{"error": ...}`` result.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _http_put_json(
    base_url: str, path: str, payload: dict, timeout: float = 2.0
) -> dict:
    """PUT JSON ``payload`` to ``base_url + path`` and parse the response.

    Runs on the ``_passthrough_executor`` worker. See ``_http_get_json``.
    """
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _json_safe(value: Any) -> Any:
    """Coerce sqlite3.Row and other non-JSON types into JSON-friendly
    primitives.

    The frontend can't deal with Row objects, datetimes, Decimals, etc.
    Calling ``_json_safe`` on the snapshot dict before returning makes
    sure ``json.dumps`` won't choke during ``evaluate_js`` round-trips
    and the JS bridge gets primitives all the way down.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    # sqlite3.Row is indexable + supports keys() like a dict.
    if hasattr(value, "keys") and hasattr(value, "__getitem__"):
        return {str(k): _json_safe(value[k]) for k in value.keys()}
    return str(value)


def _upstream_model_catalog(settings: "Settings") -> list[dict]:
    """可用模型清单 = 扁平 "upstream / model" 项，同名模型带上游前缀。

    每个 option 同时携带 upstream 与 model，避免同名模型无法定位。
    v0.113o 从 get_advanced_switch 抽出，报错分析的下拉共用同一来源。
    """
    models: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for plat in PLATFORMS:
        for c in settings.upstreams_for(plat):
            cands = []
            if c.model:
                cands.append(c.model)
            for m in (c.allowed_models or []):
                if m not in cands:
                    cands.append(m)
            for m in cands:
                key = (c.name, m)
                if key not in seen:
                    seen.add(key)
                    models.append({
                        "upstream": c.name,
                        "model": m,
                        "label": f"{c.name} / {m}",
                    })
    models.sort(key=lambda x: x["label"])
    return models


class Api:
    """Methods exposed to JS as ``window.pywebview.api.*``.

    Every public method here is callable from JS as ``await pywebview.api.<name>(...)``.
    pywebview marshals return values via JSON, so the Python side must
    return primitives (dicts / lists / strings / numbers / None / bool).
    """

    def __init__(self, app: "App") -> None:
        self._app = app

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return the current relay/server status. Mirrors the keys the
        frontend's ``poll()`` consumer reads.

        ``running`` is the OR of two independent signals:

        * this GUI's own ``ServerProcess`` child is alive, and
        * something answers ``/healthz`` on the configured port.

        The second term is what makes an externally-started relay (the
        Windows autostart entry, a terminal, a previous GUI session)
        show up as 运行中 instead of 已停止. ``owner`` disambiguates:
        ``"self"`` when we spawned it, ``"external"`` when someone else
        did, ``"none"`` when nothing is listening. The frontend uses
        ``owner`` to decide whether 停止 / 重启 are offered at all.

        ``active`` is the per-platform active upstream name (anthropic /
        openai) since the old single-string is gone — the topbar shows
        whichever platform the user last touched.
        """
        own = False
        pid: int | None = None
        try:
            own = bool(self._app.server.is_running)
            pid = getattr(self._app.server, "pid", None)
        except Exception:
            own = False
            pid = None
        port = self._app.settings.port
        healthy = _probe_relay(self._app.settings.base_url)
        # If our child reports alive but the OS port-owner is a *different*
        # PID, we're in the brief race between Popen() returning and the
        # child dying on `WinError 10048` — the port really belongs to
        # someone else. Claiming it as ours would briefly enable 停止
        # against a port we don't own. Cross-check by PID; the
        # ``_listener_pid`` cache keeps the netstat cost to once per 5 s.
        if own and pid is not None and healthy:
            port_pid = _listener_pid(port)
            if port_pid != pid:
                own = False
                pid = port_pid
        elif not own and healthy:
            pid = _listener_pid(port)
        result = {
            "running": own or healthy,
            "owner": OWNER_SELF if own else (OWNER_EXTERNAL if healthy else OWNER_NONE),
            "pid": pid,
            "port": port,
            "active": self._app.active_label(),
            "active_per_platform": {
                p: self._app.settings.active_for(p) for p in PLATFORMS
            },
            "theme": self._app.theme_name,
            # v0.90：上游页"耗尽后切换到 xx"预览需要这三个字段。
            # 之前只在 /api/settings 返回，前端上游页要再发一次请求才拿
            # 得到；放到 /api/status 后端到 500ms 轮询链路里，前端零成本。
            "autoswitch_enabled": bool(
                getattr(self._app.settings, "relay_quota_autoswitch", False)
            ),
            "autoswitch_pool": list(
                getattr(self._app.settings, "relay_autoswitch_pool", None) or []
            ),
            "autoswitch_at": float(
                getattr(self._app.settings, "relay_quota_switch_at", 0.9)
            ),
            # 完全透传模式开关。中间件直接读 app.state.settings，
            # GUI 副本 self._app.settings 由 set_passthrough_mode 同步。
            "passthrough_mode": bool(
                getattr(self._app.settings, "passthrough_mode", False)
            ),
        }
        return result

    def get_snapshot(self) -> dict:
        """Return the latest cached snapshot from the polling thread.

        Cheap: just returns ``self._app._latest_snapshot`` (a precomputed
        dict). The polling thread rebuilds it every 500 ms, so by the
        time JS asks the data is at most half a tick stale.

        Returns ``{}`` if the first poll hasn't completed yet — the JS
        side handles empty snapshots as "still loading".
        """
        snap = self._app._latest_snapshot
        return _json_safe(snap) or {}

    def refresh_now(self) -> dict:
        """Force a snapshot rebuild on the main thread and return the
        result synchronously.

        Used by the topbar's "刷新" button so the user can pull fresh
        data without waiting for the next poll tick. The rebuild runs
        synchronously which keeps it predictable — the user clicked
        "refresh" so a sub-second pause is acceptable.
        """
        try:
            self._app._rebuild_snapshot()
        except Exception:
            # Swallow — the next poll tick will retry. Returning the
            # cached value is still better than a 500 in JS land.
            pass
        return self.get_snapshot()

    # ------------------------------------------------------------------
    # Read — paginated history (v0.36)
    #
    # The GUI history view used to fetch ``GET /requests`` directly from
    # the page, but pywebview loads index.html via a ``file://`` URI
    # (``gui.py:_WEB_DIR``), so ``window.location.origin`` resolves to
    # ``"null"`` and ``new URL("/requests", origin)`` produces an URL
    # that fetch() can't route to localhost — leading to a silent
    # ``TypeError: Failed to fetch`` and a stuck sentinel. Routing
    # through the bridge sidesteps the origin entirely; same trick for
    # the per-request conversation.
    #
    # The HTTP endpoints on stats.py still exist for external callers
    # (CLI scripts, future search UI) — they just aren't the GUI path.
    # ------------------------------------------------------------------

    def fetch_requests(self, limit: int = 100, before_id: Optional[int] = None) -> dict:
        """Paginated recent-requests list — mirror of ``GET /requests``.

        ``limit`` is clamped to [1, 500]; ``before_id`` is the SQLite
        row id cursor (smallest id from the previous page). Returns
        ``{limit, before_id, items, next_before_id}``; ``next_before_id``
        is ``None`` when the page came back short of ``limit`` (no more
        rows to paginate).
        """
        try:
            lim = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            lim = 100
        bid = int(before_id) if before_id is not None else None
        from . import tui
        rows = tui.fetch_recent(self._app.settings.relay_db, lim, bid)
        items = [dict(r) for r in rows]
        return {
            "limit": lim,
            "before_id": bid,
            "items": items,
            "next_before_id": items[-1]["id"] if len(items) == lim else None,
        }

    def fetch_conversation(self, request_id: int) -> dict:
        """Return one request row + saved messages, or an error dict.

        Mirror of ``GET /messages/by_request/{id}`` — see the comment
        on ``fetch_requests`` for why the GUI uses the bridge. The
        ``error: "not_found"`` shape is preserved so the JS modal
        handles both HTTP-flavour and bridge-flavour responses the
        same way.
        """
        try:
            from . import tui
            convo = tui.fetch_conversation(self._app.settings.relay_db, int(request_id))
        except (TypeError, ValueError):
            return {"error": "bad_request_id", "request_id": request_id}
        if convo is None:
            return {"error": "not_found", "request_id": request_id}
        return convo

    # ------------------------------------------------------------------
    # v0.99：统计页（左侧菜单"统计"视图）两个桥方法
    #
    # 与 fetch_requests 同款理由：pywebview 用 file:// 加载 index.html，
    # origin 是 "null"，fetch() 拿不到 127.0.0.1:8088 的中继 HTTP，
    # 必须走桥。直接调 tui.fetch_* 而不是 HTTP 中继，路径短 + 不需要
    # 配置 cors / 跨源策略，与既有 fetch_requests 模式一致。
    # ------------------------------------------------------------------

    # 时间段预设常量，与 routers/stats.py / tui._RANGE_PRESETS 同源。
    # JS 不传 range 时回退 30d；非法值原样传给 tui 让它返回错误结构
    # （routes 层用同样手法：HTTP 仍 200，前端按 error 字段渲染）。
    _STATS_RANGE_PRESETS: dict[str, int] = {
        "1d":  24 * 3600,
        "7d":  7 * 24 * 3600,
        "30d": 30 * 24 * 3600,
    }

    def stats_aggregate(
        self,
        dim: str = "upstream",
        range: str = "30d",
        top: int = 10,
        mode: str = "relay",
    ) -> dict:
        """v0.99 统计页：按维度聚合（platform / upstream / model），时间段内 top-N。

        返回结构与 HTTP /stats/aggregate 同步：
        ``{range, dim, since, top, total, rows}``；``rows`` 是 list（前端
        直接 for，避免 dict 顺序假设）。

        ``mode="passthrough"`` 时透传 HTTP 给中继的 /api/passthrough/stats，
        避免 GUI 进程里跑独立 asyncio.run 与 webview 事件循环冲突。
        """
        if mode == "passthrough":
            try:
                top_n = max(0, min(100, int(top)))
            except (TypeError, ValueError):
                top_n = 10
            try:
                qs = f"dim={dim}&range={range}&top={top_n}"
                with urllib.request.urlopen(
                    f"{self._app.settings.base_url}/api/passthrough/stats?{qs}",
                    timeout=3.0,
                ) as r:
                    return json.loads(r.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                return {"error": f"HTTP{exc.code}: {body}".strip()}
            except Exception as exc:
                return {"error": str(exc)}

        # ---- 转换模式默认分支 ----
        import time as _time
        from . import tui

        if range not in self._STATS_RANGE_PRESETS:
            return {"error": f"invalid range {range!r}",
                    "valid": list(self._STATS_RANGE_PRESETS.keys())}
        if dim not in {"platform", "upstream", "model", "agent"}:
            return {"error": f"invalid dim {dim!r}",
                    "valid": ["platform", "upstream", "model", "agent"]}
        try:
            top_n = max(0, min(100, int(top)))
        except (TypeError, ValueError):
            top_n = 10
        since = time.time() - self._STATS_RANGE_PRESETS[range]
        # 全量取，自己算 total 再截前 top（与 HTTP 路由同步）。
        rows_map = tui.fetch_aggregate_by_dim(
            self._app.settings.relay_db, dim=dim, since=since,
        )
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
        sorted_rows = sorted(
            rows_map.items(), key=lambda kv: kv[1]["total_tokens"], reverse=True,
        )
        if top_n > 0:
            sorted_rows = sorted_rows[:top_n]
        return {
            "range": range,
            "dim": dim,
            "since": since,
            "top": top_n,
            "total": total,
            "rows": [{"key": k, **v} for k, v in sorted_rows],
        }

    def stats_daily(self, days: int = 30) -> dict:
        """v0.99 统计页：每日一行聚合，按日期倒序；空日期 0 补齐。

        与 HTTP /stats/daily 同步：``{days, items}``，``items`` 每项
        含 date(ISO) / ts / requests / 4 种 token / errors。
        """
        from . import tui

        try:
            n = max(1, min(365, int(days)))
        except (TypeError, ValueError):
            n = 30
        items = tui.fetch_daily(self._app.settings.relay_db, days=n)
        return {"days": n, "items": items}

    def stats_model_daily(self, days: int = 30) -> dict:
        """v0.153 统计页：单模型 30 天按日调用分布 —— 按 model × 日桶聚合。

        与 HTTP /stats/model_daily 同步：``{models, days, series,
        items}``；``models`` 每项 ``{model, total, color}``（按 total 降序，
        稳定色板），``series`` 每模型一条 30 天序列（0 补齐，升序）。
        数据源 = relay.db（透传档走 HTTP 路由读 passthrough.db）。
        """
        from . import tui

        try:
            n = max(1, min(365, int(days)))
        except (TypeError, ValueError):
            n = 30
        return tui.fetch_model_daily(self._app.settings.relay_db, days=n)

    def passthrough_overview(self, range: str = "30d") -> dict:
        """透传模式总览：代理 HTTP /api/passthrough/overview。

        返回 {totals, by_upstream, by_model, by_platform, by_hour, recent}，
        供总览页"仅透传 / 全部"两档使用（relay 档直接用快照）。
        """
        # 临时诊断（v0.102）：记录桥调用与被调用的真实 range。
        try:
            import os
            _log = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                ".relay-logs", "pt_overview.log",
            )
            with open(_log, "a", encoding="utf-8") as _f:
                _f.write(f"[{time.strftime('%H:%M:%S')}] passthrough_overview range={range!r}\n")
        except Exception:
            pass
        try:
            with urllib.request.urlopen(
                f"{self._app.settings.base_url}/api/passthrough/overview?range={range}",
                timeout=3.0,
            ) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            return {"error": f"HTTP{exc.code}: {body}".strip()}
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # v0.100.1：统计页（高级可视化）四个新桥方法。
    #
    # 数据源与 routers/stats.py /stats/* 完全同步：直接调 tui.fetch_* 而不
    # 走 HTTP（pywebview 的 file:// origin 让 fetch() 拿不到中继，必须走桥；
    # 桥直接读 DB 路径短，与 fetch_requests / fetch_conversation 同款）。
    # 错误结构与 HTTP 路由对齐（非法 range/bucket/metric 返回 error 字段）。
    # ------------------------------------------------------------------

    _STATS_RANGE_PRESETS: dict[str, int] = {
        "1d":  24 * 3600,
        "7d":  7 * 24 * 3600,
        "30d": 30 * 24 * 3600,
    }

    def stats_token_breakdown(self, range: str = "7d") -> dict:
        """v0.100.1 玫瑰图：全模型合计 4 种 token + 总数。"""
        from . import tui
        if range not in self._STATS_RANGE_PRESETS:
            return {"error": f"invalid range {range!r}",
                    "valid": list(self._STATS_RANGE_PRESETS.keys())}
        since = time.time() - self._STATS_RANGE_PRESETS[range]
        data = tui.fetch_token_breakdown(self._app.settings.relay_db, since=since)
        return {"range": range, "since": since, **data}

    def stats_timeseries(
        self,
        range: str = "7d",
        bucket: str = "hour",
        metric: str = "requests",
    ) -> dict:
        """v0.100.1 stacked area：按 upstream 拆色的时间序列。"""
        from . import tui
        if range not in self._STATS_RANGE_PRESETS:
            return {"error": f"invalid range {range!r}",
                    "valid": list(self._STATS_RANGE_PRESETS.keys())}
        since = time.time() - self._STATS_RANGE_PRESETS[range]
        try:
            items = tui.fetch_timeseries(
                self._app.settings.relay_db, since=since,
                bucket=bucket, metric=metric,
            )
        except ValueError as e:
            return {"error": str(e),
                    "valid_bucket": ["hour", "day"],
                    "valid_metric": ["requests", "errors", "total_tokens"]}
        return {
            "range": range, "bucket": bucket, "metric": metric,
            "since": since, "items": items,
        }

    def stats_route_heatmap(self, range: str = "7d") -> dict:
        """v0.100.1 路由热力 + Sankey：每上游在各平台的请求数矩阵。"""
        from . import tui
        if range not in self._STATS_RANGE_PRESETS:
            return {"error": f"invalid range {range!r}",
                    "valid": list(self._STATS_RANGE_PRESETS.keys())}
        since = time.time() - self._STATS_RANGE_PRESETS[range]
        data = tui.fetch_route_heatmap(self._app.settings.relay_db, since=since)
        rows = [
            {"upstream": ups, "anthropic_n": n["anthropic"], "openai_n": n["openai"]}
            for ups, n in data.items()
        ]
        return {"range": range, "since": since, "items": rows}

    def stats_calendar(self, days: int = 365) -> dict:
        """v0.100.1 日历热力：按日 token + requests，0 补齐到 days 天。"""
        from . import tui
        try:
            n = max(1, min(365, int(days)))
        except (TypeError, ValueError):
            n = 365
        return tui.fetch_calendar(self._app.settings.relay_db, days=n)

    def stats_quota_5h(self) -> dict:
        """v0.100.1 配额仪表盘：每上游当前 5h 配额利用率（与 range 无关）。"""
        from . import tui
        upstreams: list[dict[str, object]] = []
        for plat in ("anthropic", "openai"):
            for cfg in self._app.settings.upstreams_for(plat):
                upstreams.append({
                    "name": cfg.name,
                    "quota_5h": cfg.quota_5h or 0,
                    "billing_unit": cfg.billing_unit or "count",
                    "model_multipliers": cfg.model_multipliers or {},
                    "allowed_models": cfg.allowed_models or [],
                })
        items = tui.fetch_upstream_quota_5h(self._app.settings.relay_db, upstreams=upstreams)
        return {"items": items}

    # ------------------------------------------------------------------
    # Write — server lifecycle
    # ------------------------------------------------------------------

    # ``stop`` still refuses to act on an external relay — that's a
    # destructive read-the-room action ("kill whatever is on 8088")
    # that doesn't have a recovery path the user can see in this
    # window. ``restart`` is different: the user has explicitly asked
    # for it, so we go ahead and take the port over. The cost is the
    # same as the old Tk GUI's "netstat + taskkill /F" takeover: the
    # in-flight Claude Code session is severed mid-request, and the
    # next request is served by the freshly-spawned child. The user
    # signed up for that by pressing 重启.
    _EXTERNAL_REFUSAL = (
        "中继由外部进程启动（非本窗口），仅监控不接管。"
        "如需停止请在启动它的终端或任务管理器中操作。"
    )

    def _guarded(self, action: str) -> dict:
        """Run a ``ServerProcess`` method only when we own the relay.

        ``get_status`` already determines ownership; this helper routes
        the call through that single decision point so the stop refusal
        lives in exactly one place. ``restart`` no longer goes through
        here — it does its own takeover when the relay is external.
        """
        status = self.get_status()
        if status["owner"] == OWNER_EXTERNAL:
            status["error"] = self._EXTERNAL_REFUSAL
            return status
        try:
            getattr(self._app.server, action)()
        except Exception:
            pass
        return self.get_status()

    def _takeover_external_listener(self, port: int) -> Optional[str]:
        """Kill whatever PID is bound to ``port`` and wait briefly for
        the socket to release. Returns ``None`` on success, or a
        human-readable reason when the owner survived both kill
        attempts.

        直白版：netstat 一次拿到 PID，taskkill /F /T，等 500 ms 让 OS
        释放 socket，然后验证端口真的空了；没空就再杀一次，还不行就
        返回失败原因让 UI 能看见，而不是静默起一个撞 10048 就死的
        新进程。如果该进程被 NSSM / 服务包了，taskkill 之后还会被
        respawn 出来，那属于用户侧的进程管理问题，重启按钮只管
        "杀一次 + 接管"，不负责把守护进程也干掉。

        v0.8.3：PID 查询改为直接同步 ``_do_listener_lookup`` —— 之前
        走共享 ``_pid_executor``，JS 状态轮询是 2 Hz，每次 tick 都往
        单线程队列里塞任务，接管那一刻的查询往往排在队尾被 1.5 s
        超时截断返回 None，于是 ``if pid is None: return`` 静默放弃
        了接管。按钮点击是一次性的，桥线程阻塞两三秒没关系（UI 已
        在显示 重启中…）。杀完还要验证端口释放、失败重试一次。
        """
        _pid_cache.pop(port, None)
        pid = _do_listener_lookup(port)
        if pid is None:
            return None
        for _attempt in range(2):
            if sys.platform == "win32":
                res = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, check=False, timeout=10.0,
                    creationflags=_NO_WINDOW,
                )
                if res.returncode != 0:
                    # taskkill 本身就失败了（权限拒绝 / 跨会话）——同
                    # PID 再杀一次只会同样失败，直接报错让 UI 显示。
                    return (
                        f"taskkill 未能结束 PID {pid}（rc={res.returncode}）"
                        "（可能是提权或跨会话进程，请到任务管理器手动结束）"
                    )
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            # 给 OS 一点时间真正回收 socket，否则紧接着的 Popen 仍然
            # 会撞 WinError 10048。
            time.sleep(0.5)
            _pid_cache.pop(port, None)
            now = _do_listener_lookup(port)
            if now is None:
                return None
            if now != pid:
                # 原进程已被杀掉，端口又被别的进程抢占了 —— 那不是
                # 我们要接管的目标，绝不能再 taskkill 它。
                return (
                    f"端口 {port} 原占用者 PID {pid} 已停，但端口已被新"
                    f"进程 PID {now} 占用"
                )
        return (
            f"端口 {port} 仍被 PID {pid} 占用，taskkill 两次都未生效"
            "（可能是提权或跨会话进程，请到任务管理器手动结束）"
        )

    def start_server(self) -> dict:
        """Start the uvicorn subprocess. No-op when anything already
        answers on the port — spawning a second uvicorn against a bound
        port just produces a child that dies with ``WinError 10048``
        and leaves the UI stuck on 已停止."""
        status = self.get_status()
        if status["running"]:
            return status
        try:
            self._app.server.start()
        except Exception:
            pass
        return self.get_status()

    def stop_server(self) -> dict:
        """Stop the uvicorn subprocess we started. Returns the updated
        status, with ``error`` set when the relay isn't ours to stop."""
        return self._guarded("stop")

    def restart_server(self) -> dict:
        """停止并重启。若端口上的中继属于其他进程，先接管：杀掉端口
        占用者，等待 socket 释放，再拉起自己的子进程。原本搭在外部
        中继上的在途请求会被拦腰截断 —— 这是有意为之。

        直白版：netstat → taskkill /F /T → Popen 自己。中间不再做
        "5 s 内反复 netstat 再 taskkill" 的 respawn 循环，也不再做
        /healthz 重试 —— 那是上一版过度设计。按重启按钮的语义就是
        "kill 一次 + 起一个新的"，多出来的 respawn 兜底反而把按钮
        按下去之后的 5 s 都堵在原地，体感很糟。respawn 守护进程类
        的场景属于用户侧进程管理问题，让用户自己去源头解决。

        v0.8.3：接管不再依赖 ``get_status`` 的 owner 判定 —— 那个
        判定要过 0.4 s 的 /healthz 探测，中继忙时探测失败会把 owner
        判成 none，接管整个被跳过。现在只要端口上有监听者（不管健
        康不健康）就接管；接管失败会返回可读原因而不是静默起一个
        撞 10048 就死的新进程。
        """
        port = self._app.settings.port
        own_pid = self._app.server.pid
        takeover_error: Optional[str] = None
        if own_pid is not None:
            # 自己的 child 还在 —— 看端口到底是谁的。存在"child 活
            # 着但端口被别的进程占着"的竞态（Popen 返回后 child 撞
            # 10048 死掉前的窗口期），那种情况不能 stop 自己了事。
            _pid_cache.pop(port, None)
            port_pid = _do_listener_lookup(port)
            if port_pid is None or port_pid == own_pid:
                try:
                    self._app.server.restart()
                except Exception as exc:
                    # v0.93：原版 silent pass 让"切了但没重启"的现象完全不可
                    # 追溯 —— 用户看到 PID 不变却找不到任何错误日志。这里
                    # 至少记下，回头上层可以从 status["error"] 取。
                    log.exception("server.restart() failed in restart_server")
                    takeover_error = f"restart_failed: {exc}"
            else:
                takeover_error = self._takeover_external_listener(port)
                if takeover_error is None:
                    try:
                        self._app.server.start()
                    except Exception as exc:
                        log.exception("server.start() failed after takeover")
                        takeover_error = f"start_after_takeover_failed: {exc}"
        else:
            # 没有自己的 child —— 端口上有监听者就接管，没有就直接
            # 起新的。接管失败就不 spawn（起一个撞 10048 就死的 child
            # 只会把 UI 卡在 已停止）。
            _pid_cache.pop(port, None)
            if _do_listener_lookup(port) is not None:
                takeover_error = self._takeover_external_listener(port)
            if takeover_error is None:
                try:
                    self._app.server.start()
                except Exception as exc:
                    log.exception("server.start() failed in cold path")
                    takeover_error = f"start_failed: {exc}"
        # 清掉 _pid_cache：start 完端口已经是我们的 PID 了，但 cache
        # 里可能还残留被 kill 的旧 PID —— 不清的话下个 500 ms tick
        # 的 get_status 看到 port_pid != self_pid 会把 own 改成 False，
        # 顶栏又退回到 "运行中（外部）"。
        _pid_cache.pop(port, None)
        status = self.get_status()
        if takeover_error:
            status["error"] = takeover_error
        return status

    # ------------------------------------------------------------------
    # Write — upstream selection
    # ------------------------------------------------------------------

    def apply_upstream(self, platform: str, name: str, model: str | None = None) -> dict:
        """Switch the active upstream for ``platform`` to ``name``.

        POSTs to the relay's ``/api/upstreams/{platform}/select`` endpoint
        so the running uvicorn process picks up the switch without a
        restart. Optional ``model`` (v0.65) — the sidebar dropdown lists
        every (upstream, model) pair separately, so the chosen model
        arrives with the switch and gets persisted in the same call.

        Returns the relay's JSON body.
        """
        if platform not in PLATFORMS:
            return {"platform": platform, "name": name, "ok": False,
                    "error": f"unknown platform: {platform}"}
        # The control router mounts under ``/api`` (see
        # ``routers/api.py``: ``APIRouter(prefix="/api")``). Omitting it
        # here silently 404'd every upstream switch.
        url = (
            f"{self._app.settings.base_url}"
            f"/api/upstreams/{platform}/select"
        )
        payload: dict = {"name": name}
        if model is not None:
            payload["model"] = model
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=1.5) as r:
                raw = r.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            # v0.93：默认 urllib.HTTPError.strerror 只给 "Bad Request"，没有 FastAPI
            # 的 {"detail": ...} 字段。把响应体读出来一起报，前端 alert 能告诉用户
            # 真正的失败原因（model 不在 allowed_models / 平台未识别 / 上游没找到）。
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            return {"platform": platform, "name": name, "ok": False,
                    "error": f"HTTP{exc.code}: {exc.reason or 'request failed'} | {body}".strip()}
        except urllib.error.URLError as exc:
            err_msg = str(exc)
            if "timed out" in err_msg:
                return {"platform": platform, "name": name, "ok": False,
                        "error": "后端未响应（正在重启或未运行），请稍后重试"}
            return {"platform": platform, "name": name, "ok": False,
                    "error": f"连接失败: {err_msg}"}
        except Exception as exc:
            return {"platform": platform, "name": name, "ok": False,
                    "error": str(exc)}
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"ok": False, "error": "invalid relay response"}
        # Mirror on the local Settings so the GUI's get_status() reflects
        # the change without re-querying the relay.
        try:
            self._app.settings.set_active(platform, name)
        except KeyError:
            return {"platform": platform, "name": name, "ok": False,
                    "error": f"upstream {name!r} not in {platform!r} config"}
        # Re-bind so the in-memory model field is current (the relay may
        # have just persisted it).
        if model is not None and parsed.get("ok", True):
            from .upstreams_file import apply_to_settings
            apply_to_settings(self._app.settings, self._app.settings.relay_upstreams_file)
        return parsed

    def probe_upstream(self, url: str, api_key: str, model: str = "") -> dict:
        """v0.12 新建上游自动探测 —— POST 到中继 /api/probe_upstream。

        返回探测结果 dict（wire/endpoint/auth_style/models/key_valid/
        evidence）。中继没起或旧版本无此端点时返回 {"error": ...}。

        ``model``（v0.95+ P2）：表单里填的真实模型名，透传给中继端探测，
        避免占位名被严格校验模型名的上游（DeepSeek V4 等）400 拒绝。
        """
        try:
            body = json.dumps({"url": url, "api_key": api_key, "model": model}).encode("utf-8")
            req = urllib.request.Request(
                f"{self._app.settings.base_url}/api/probe_upstream",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8") or "{}"
            return json.loads(raw)
        except Exception as exc:
            return {"error": str(exc)}

    def test_upstream(self, url: str, api_key: str, model: str = "") -> dict:
        """v0.12.1 新建上游"测试"按钮 —— 后台线程跑探测，日志实时推给页面。

        探测在 GUI 进程内直接跑（不依赖中继是否在线），每步日志经
        ``window.evaluate_js`` 推 ``window.relayProbeLog(line)``，结束推
        ``window.relayProbeDone(result)``（EdgeChromium 的 evaluate_js 走
        Control.Invoke，跨线程调用安全）。立即返回 {"started": true}。

        ``model``（v0.95+ P2）：表单里填的真实模型名，优先用它发最小
        请求（严格校验模型名的上游用占位名会被 400 拒）。
        """
        url = (url or "").strip()
        if not url:
            return {"error": "URL 不能为空"}
        key = (api_key or "").strip()
        model = (model or "").strip()
        win = self._app.window

        def _push(js: str) -> None:
            try:
                win.evaluate_js(js)
            except Exception:
                pass  # 窗口已关 / 页面重载 —— 静默丢弃

        def _worker() -> None:
            import asyncio

            from .probe import probe_upstream

            async def _emit(line: str) -> None:
                _push(f"relayProbeLog({json.dumps(line, ensure_ascii=False)})")

            try:
                result = asyncio.run(probe_upstream(url, key, emit=_emit, model=model or None))
            except Exception as exc:  # noqa: BLE001
                result = {"error": f"探测失败: {exc}"}
            _push(f"relayProbeDone({json.dumps(result, ensure_ascii=False, default=str)})")

        threading.Thread(
            target=_worker, daemon=True, name="relay-upstream-test",
        ).start()
        return {"started": True}

    def connectivity_test(
        self,
        url: str,
        api_key: str,
        wire: str | None = None,
        auth_style: str | None = None,
        model: str | None = None,
    ) -> dict:
        """v0.84 "新建上游"连通性测试按钮 —— 严格按当前表单配置构造一条
        真实消息发到上游，验证配置能否真正通。

        与 test_upstream（自动探测）互补：探测负责"猜协议/端点"，连通性
        测试负责"按用户填的配置原样发一条，看通不通"。后台线程跑，日志
        经 relayProbeLog 实时推给页面，结束推 relayProbeDone。
        """
        url = (url or "").strip()
        if not url:
            return {"error": "URL 不能为空"}
        key = (api_key or "").strip()
        win = self._app.window

        def _push(js: str) -> None:
            try:
                win.evaluate_js(js)
            except Exception:
                pass  # 窗口已关 / 页面重载 —— 静默丢弃

        def _worker() -> None:
            import asyncio

            from .probe import connectivity_test as run_test

            async def _emit(line: str) -> None:
                _push(f"relayProbeLog({json.dumps(line, ensure_ascii=False)})")

            try:
                result = asyncio.run(run_test(
                    url, key, wire=wire or None, auth_style=auth_style or None,
                    model=model or None, emit=_emit,
                ))
            except Exception as exc:  # noqa: BLE001
                result = {"error": f"连通性测试失败: {exc}"}
            _push(f"relayProbeDone({json.dumps(result, ensure_ascii=False, default=str)})")

        threading.Thread(
            target=_worker, daemon=True, name="relay-connectivity-test",
        ).start()
        return {"started": True}

    def update_upstream_quota(
        self,
        platform: str,
        name: str,
        payload: dict | None,
    ) -> dict:
        """Persist ``quota_5h`` / ``model_multipliers`` / ``allowed_models``
        for the named upstream.

        Writes through ``save_upstreams_json`` (atomic + validates every
        upstream entry through ``PlatformConfig`` after mutation, so a
        bad payload can't poison the file). On success reloads
        ``self.settings`` so the very next snapshot picks up the new
        quota / multipliers without a GUI restart.

        Returns ``{"ok": True, "path": "..."}`` on success,
        ``{"ok": False, "error": "..."}`` on validation / IO failure.
        The frontend re-polls on success and shows a pill on failure.
        """
        ok, msg = apply_quota_edit(self._app.settings, platform, name, payload)
        if not ok:
            return {"ok": False, "error": msg}
        # Reload settings so the next snapshot picks up the new config.
        # settings is mutable in-place (see reload_settings); the same
        # Settings instance is referenced everywhere downstream, so an
        # explicit re-load of the upstreams JSON into it is enough.
        try:
            reload_settings()
            # The reload returns a new Settings instance; rebind on the
            # App so the snapshot path sees the new values.
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "path": msg,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        return {"ok": True, "path": msg}

    def save_quick_switch(self, items: list | None) -> dict:
        """整盘替换 upstreams.json 顶层 quick_switch 数组（v0.70）。

        模板完全照搬 update_upstream_quota：调 replace_quick_switch
        helper → reload_settings + rebind → 下一次 poll 自动带新列
        表给 sidebar 和 Settings 卡片渲染。quick_switch 只被 GUI 消
        费（relay 子进程的代理链路不读），所以不用走 HTTP 通知子进
        程 —— 子进程下次 reload 时也会从 JSON 读到新值。

        Returns ``{"ok": True, "path": "..."}`` on success,
        ``{"ok": False, "error": "..."}`` on validation / IO failure.
        """
        if not isinstance(items, list):
            return {"ok": False, "error": "items 必须是 list"}
        try:
            ok, msg = replace_quick_switch(self._app.settings, items)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "path": msg,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        return {"ok": True, "path": msg}

    def get_agent_aliases(self) -> dict[str, str]:
        """返回 upstreams.json 顶层 ``agent_aliases`` 映射（v0.155）。

        纯展示层（统计归组显示名）。空 dict 表示用户尚未重命名任何
        工具名。前端据此把原始 agent 名映射成显示名。
        """
        aliases = getattr(self._app.settings, "agent_aliases", None)
        return dict(aliases or {})

    def save_agent_aliases(self, aliases: dict | None) -> dict:
        """整盘替换 upstreams.json 顶层 ``agent_aliases`` 映射（v0.155）。

        仿 save_quick_switch 模板：save_agent_aliases helper → reload
        → 返回 {ok, path}。纯展示层映射，中继子进程不消费，无需 HTTP
        通知子进程。
        """
        if not isinstance(aliases, dict):
            return {"ok": False, "error": "aliases 必须是对象"}
        try:
            ok, msg = save_agent_aliases(self._app.settings, aliases)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "path": msg,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        return {"ok": True, "path": msg}

    def get_ua_rules(self) -> dict[str, str]:
        """返回 upstreams.json 顶层 ``ua_rules`` 映射（v0.157）。

        用户手配的「整串 UA → 平台名」归类规则。中继进程在请求入口消费，
        前端据此回填设置页「UA 归类」每行的当前值。
        """
        rules = getattr(self._app.settings, "ua_rules", None)
        return dict(rules or {})

    def save_ua_rules(self, rules: dict | None) -> dict:
        """整盘替换 upstreams.json 顶层 ``ua_rules`` 映射（v0.157）。

        与 save_agent_aliases 不同，**中继进程消费这些规则**，所以写盘 +
        reload GUI settings 后必须 POST /api/upstreams/refresh 让中继子进程
        也 reload（否则新规则要等重启才生效）。仿 add_upstream 的刷新通知。
        """
        if not isinstance(rules, dict):
            return {"ok": False, "error": "rules 必须是对象"}
        try:
            ok, msg = save_ua_rules(self._app.settings, rules)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "path": msg,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        # 同步中继进程内存里的 settings（同 add_upstream 尾部注释）。中继
        # fork 时的 settings 快照没有新规则，入口 resolve_agent 读不到；
        # 发本地刷新让子进程 reload。中继拒接时静默吞掉（保存本身已成功）。
        try:
            import urllib.request as _ur
            base = getattr(self._app.settings, "base_url", "") or "http://127.0.0.1:8088"
            req = _ur.Request(
                f"{base}/api/upstreams/refresh",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=2.0) as r:
                _ = r.read()
        except Exception:
            pass
        return {"ok": True, "path": msg}

    def get_recent_uas(self, limit: int = 100) -> list[dict]:
        """返回最近出现过的原始 User-Agent 串（v0.157）。

        设置页「UA 归类」数据源：从 requests.raw_ua 取 distinct UA，按
        最后出现时间倒序。返回形如 ``[{"ua", "last_ts", "count"}, ...]``。
        """
        try:
            from relay import tui
            db = getattr(self._app.settings, "relay_db", "")
            if not db:
                return []
            return tui.fetch_recent_uas(db, limit=int(limit or 100))
        except Exception:
            return []

    def get_relay_settings(self) -> dict:
        """Relay-side autoswitch settings (v0.11.3 settings page).

        Fetched over HTTP from the relay's own ``/api/settings`` so the
        GUI shows the *live* values the quota monitor uses. Falls back
        to this process's Settings copy when the relay is unreachable —
        env-derived, so it agrees on a freshly started relay.
        """
        try:
            with urllib.request.urlopen(
                f"{self._app.settings.base_url}/api/settings", timeout=2.0
            ) as r:
                return json.loads(r.read().decode("utf-8")) or {}
        except Exception:
            s = self._app.settings
            return {
                "autoswitch_enabled": bool(s.relay_quota_autoswitch),
                "autoswitch_pool": list(getattr(s, "relay_autoswitch_pool", None) or []),
                "autoswitch_at": float(s.relay_quota_switch_at),
            }

    def update_relay_settings(self, payload: dict | None) -> dict:
        """Persist autoswitch on/off + pool on the relay side (v0.11.3).

        The relay mutates ``app.state.settings`` in place and writes the
        values to ``.env``; the quota monitor re-reads its settings every
        tick, so the change applies without a restart.

        v0.91 fix: GUI 进程的 ``self._app.settings`` 与 relay 进程的
        ``app.state.settings`` 是两份独立的内存对象 —— PUT 改的是 relay
        那份，GUI 这份不变。下一次 ``Api.get_status()`` 读 GUI 副本拿到
        旧值 ``autoswitch_enabled=false``，前端上游页 sig 不变 → hint
        一直不出现。修复：成功后 reload_settings() 让 GUI 副本从 .env
        重读，与 relay 同步。其它写 .env 的 handler
        （replace_quick_switch / save_advanced_switch）走同样模式。
        """
        try:
            req = urllib.request.Request(
                f"{self._app.settings.base_url}/api/settings/autoswitch",
                data=json.dumps(payload or {}).encode("utf-8"),
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as r:
                result = json.loads(r.read().decode("utf-8")) or {}
        except Exception as exc:
            return {"error": str(exc)}
        if isinstance(result, dict) and not result.get("error"):
            try:
                reload_settings()
                self._app.settings = get_settings()
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # v0.113n 存储管理 / 存储位置管理 —— 设置页「存储管理」区块。
    # 读（占用信息）GUI 本地算（relay 停了也能看）；写（删/VACUUM/清日志）
    # 走 relay HTTP 端点；位置迁移写 .env + 搬文件（运行中被锁则回滚，
    # 不自动重启 —— 严禁 kill 8088 中断用户会话）。
    # ------------------------------------------------------------------

    def get_storage_info(self) -> dict:
        """本地计算各存储占用（只读，不依赖 relay 运行）。"""
        s = self._app.settings
        from relay.config import _project_root

        root = _project_root()

        def _file_info(path: str) -> dict:
            p = Path(path)
            return {
                "path": str(p),
                "exists": p.is_file(),
                "size": p.stat().st_size if p.is_file() else 0,
            }

        def _db_counts(path: str) -> dict:
            """requests / messages 条数 + ts 范围；表不存在兜底 0。"""
            out = {"requests": 0, "messages": 0, "ts_min": None, "ts_max": None}
            if not Path(path).is_file():
                return out
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    try:
                        row = conn.execute(
                            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM requests"
                        ).fetchone()
                        out["requests"] = int(row[0] or 0)
                        out["ts_min"] = row[1]
                        out["ts_max"] = row[2]
                    except Exception:
                        pass
                    try:
                        out["messages"] = int(
                            conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] or 0
                        )
                    except Exception:
                        pass
                finally:
                    conn.close()
            except Exception:
                pass
            return out

        def _pt_counts(path: str) -> int:
            if not Path(path).is_file():
                return 0
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    return int(
                        conn.execute(
                            "SELECT COUNT(*) FROM passthrough_requests"
                        ).fetchone()[0] or 0
                    )
                finally:
                    conn.close()
            except Exception:
                return 0

        # 日志目录：文件数 + 总字节。
        log_dir = root / ".relay-logs"
        log_files = 0
        log_size = 0
        if log_dir.is_dir():
            for entry in log_dir.iterdir():
                if entry.is_file():
                    log_files += 1
                    try:
                        log_size += entry.stat().st_size
                    except OSError:
                        pass

        db_info = _file_info(s.relay_db)
        db_info.update(_db_counts(s.relay_db))
        pt_info = _file_info(s.relay_passthrough_db)
        pt_info["requests"] = _pt_counts(s.relay_passthrough_db)
        pt_info["ts_min"] = None
        pt_info["ts_max"] = None
        return {
            "db": db_info,
            "pt_db": pt_info,
            "logs": {
                "dir": str(log_dir),
                "files": log_files,
                "size": log_size,
            },
            "upstreams": _file_info(s.relay_upstreams_file),
        }

    # ------------------------------------------------------------------
    # 临时诊断：前端把自由模式状态写到 .relay-logs/diag-free-pos.json
    # 用来排查"蔓延到侧栏"bug —— 不让用户自己在 dev tools 跑 JS。
    # ------------------------------------------------------------------
    def _diag_write_free_pos(self, payload: str) -> dict:
        """把前端序列化好的 JSON 字符串写到 .relay-logs/diag-free-pos.json。

        每次调用覆盖写（latest wins）。前端在自由模式 seed/apply/drag 时
        各调一次。
        """
        try:
            from relay.config import _project_root
            root = _project_root()
            log_dir = root / ".relay-logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / "diag-free-pos.json"
            path.write_text(payload, encoding="utf-8")
            return {"ok": True, "path": str(path), "size": path.stat().st_size}
        except Exception as e:
            return {"ok": False, "error": repr(e)}

    def _storage_post(self, path: str, body: dict | None = None) -> dict:
        """POST 到 relay 的 /api/storage/*；不可达返回 {ok:false, error}。"""
        try:
            req = urllib.request.Request(
                f"{self._app.settings.base_url}/api/storage/{path}",
                data=json.dumps(body or {}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10.0) as r:
                result = json.loads(r.read().decode("utf-8")) or {}
            if isinstance(result, dict) and result.get("ok"):
                return result
            return {"ok": False, "error": result.get("error", "relay 返回失败")}
        except Exception as exc:
            return {"ok": False, "error": f"中继未运行：{exc}"}

    def cleanup_messages(self, days: int, target: str = "both") -> dict:
        """按天清理消息记录。days<=0 清空全部；target 限定 relay/passthrough。"""
        return self._storage_post(
            "cleanup-messages", {"days": int(days or 0), "target": target or "both"}
        )

    def vacuum_storage(self, target: str = "both") -> dict:
        """VACUUM 回收空间；target 限定 relay/passthrough。"""
        return self._storage_post("vacuum", {"target": target or "both"})

    def clear_logs(self) -> dict:
        """清空 .relay-logs/ 目录。"""
        return self._storage_post("clear-logs")

    def move_storage(self, kind: str, new_path: str) -> dict:
        """迁移 relay.db / passthrough.db 到新位置。

        流程：写 .env 新路径 → 搬文件（含 -wal/-shm 副件）→ reload settings。
        Windows 下中继运行中文件被锁 → 回滚 .env 返回可读错误；**不自动
        重启中继**（严禁 kill 8088 中断用户会话，让用户自己停/起）。
        """
        if kind not in ("relay", "passthrough"):
            return {"ok": False, "error": "未知存储类型"}
        s = self._app.settings
        old_path = s.relay_db if kind == "relay" else s.relay_passthrough_db
        env_key = "RELAY_DB" if kind == "relay" else "RELAY_PASSTHROUGH_DB"
        try:
            new = os.path.abspath(new_path)
        except Exception as exc:
            return {"ok": False, "error": f"路径无效：{exc}"}
        old = os.path.abspath(old_path)
        if not new:
            return {"ok": False, "error": "路径不能为空"}
        if new == old:
            return {"ok": False, "error": "新位置与当前相同"}
        if not Path(old).is_file():
            return {"ok": False, "error": f"原文件不存在：{old}"}
        try:
            Path(new).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"ok": False, "error": f"无法创建目录：{exc}"}
        # 1) 写 .env
        from relay.config import update_env_var
        if not update_env_var(env_key, new):
            return {"ok": False, "error": "写入 .env 失败"}
        # 2) 搬文件（含 WAL 副件）
        try:
            for side in ("", "-wal", "-shm"):
                src = old + side
                if Path(src).is_file():
                    shutil.move(src, new + side)
        except PermissionError:
            # 回滚 .env，保持旧位置。
            update_env_var(env_key, old)
            return {
                "ok": False,
                "error": (
                    "中继正在运行，数据库文件被占用。请先在「中继状态」"
                    "停止中继，再执行迁移。"
                ),
            }
        except Exception as exc:
            update_env_var(env_key, old)
            return {"ok": False, "error": f"迁移失败：{exc}"}
        # 3) 同步 GUI 副本 Settings。
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception:
            pass
        return {"ok": True, "path": new}

    def get_advanced_switch(self) -> dict:
        """#高级切换：返回当前配置 + 可用模型清单 + 三类调用统计。

        - config: 顶层 advanced_switch 配置（Settings 字段）。
        - models: 当前可用模型清单 —— 从 anthropic 上游的 model /
          allowed_models 汇总去重。
        - stats:  弱 / 强 / 分析 三类模型的调用次数与 token 消耗。
          弱/强从 relay.db 按 model 聚合；分析（分类器）用进程内计数。
        """
        s = self._app.settings
        models = _upstream_model_catalog(s)

        config = {
            "enabled": bool(s.advanced_switch),
            "weak_upstream": s.advanced_weak_upstream,
            "weak_model": s.advanced_weak_model,
            "strong_upstream": s.advanced_strong_upstream,
            "strong_model": s.advanced_strong_model,
            "analysis_upstream": s.advanced_analysis_upstream,
            "analysis_model": s.advanced_analysis_model,
            "strong_types": list(s.advanced_strong_types or []),
            "aggressive": bool(s.advanced_aggressive),
            "learning": bool(s.advanced_learning),
        }

        # 弱 / 强模型调用统计：从 relay.db 按 model 聚合次数 + token。
        stats = {"weak": {"count": 0, "tokens": 0},
                 "strong": {"count": 0, "tokens": 0},
                 "analysis": {"count": 0, "tokens": 0}}
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{s.relay_db}?mode=ro", uri=True)
            try:
                for key, ups, mdl in (("weak", s.advanced_weak_upstream, s.advanced_weak_model),
                                      ("strong", s.advanced_strong_upstream, s.advanced_strong_model)):
                    if not mdl:
                        continue
                    if ups:
                        row = conn.execute(
                            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) "
                            "FROM requests WHERE model = ? AND upstream = ?",
                            (mdl, ups),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) "
                            "FROM requests WHERE model = ?",
                            (mdl,),
                        ).fetchone()
                    stats[key] = {"count": row[0] or 0, "tokens": row[1] or 0}
            finally:
                conn.close()
        except Exception:
            pass
        # 分析（分类器）调用次数 —— 进程内计数。
        try:
            from relay.advanced_switch import get_analysis_stats
            stats["analysis"] = get_analysis_stats()
        except Exception:
            pass

        return {"config": config, "models": models, "stats": stats}

    def update_advanced_switch(self, payload: dict | None) -> dict:
        """#高级切换：保存配置到 upstreams.json 并 reload settings。"""
        from relay.config import reload_settings, save_advanced_switch

        ok, msg = save_advanced_switch(self._app.settings, payload or {})
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {"ok": True, "warning": f"已写入但 reload 失败: {exc}"}
        return {"ok": True, "path": msg}

    def get_error_analysis(self) -> dict:
        """#报错分析：返回当前配置 + 可用模型清单（v0.113o）。

        config 来自 Settings 的 error_analysis_*（upstreams.json 顶层
        error_analysis）；models 与高级切换共用同一模型清单。
        """
        s = self._app.settings
        config = {
            "enabled": bool(s.error_analysis_enabled),
            "upstream": s.error_analysis_upstream,
            "model": s.error_analysis_model,
        }
        return {"config": config, "models": _upstream_model_catalog(s)}

    def set_error_analysis(self, payload: dict | None) -> dict:
        """#报错分析：保存配置到 upstreams.json 并 reload settings。"""
        from relay.config import reload_settings, save_error_analysis

        ok, msg = save_error_analysis(self._app.settings, payload or {})
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {"ok": True, "warning": f"已写入但 reload 失败: {exc}"}
        return {"ok": True, "path": msg}

    def get_vision_models(self) -> dict:
        """#支持图片的模型：返回可用模型名（去重）+ 当前勾选集（v0.188）。

        models 从中继声明过的模型清单里抽裸模型名（OpenCode 认模型 id，
        不区分上游）；vision_models 是当前勾选的名单（upstreams.json
        顶层 vision_models）。
        """
        s = self._app.settings
        # v0.192：图标带上游前缀 —— 同名模型可来自多个上游（catalog 用
        # (c.name, m) 去重），chips 里只显示裸模型名会看不出是哪家的。
        # 返回 {model, label} 富结构：label 供前端显示（"上游 / 模型"），
        # model 仍是裸名（OpenCode 认模型 id），保存/判等继续用裸名。
        by_model: dict[str, str] = {}

        for model in _upstream_model_catalog(s):

            by_model.setdefault(model["model"], model["label"])
        models = [
            {"model": m, "label": by_model[m]}
            for m in sorted(by_model)
        ]
        return {
            "models": models,
            "vision_models": list(getattr(s, "vision_models", []) or []),
        }

    def set_vision_models(self, payload: list | None) -> dict:
        """#支持图片的模型：保存勾选名单到 upstreams.json 并 reload settings。"""
        from relay.config import reload_settings, save_vision_models

        ok, msg = save_vision_models(self._app.settings, payload or [])
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {"ok": True, "warning": f"已写入但 reload 失败: {exc}"}
        return {"ok": True, "path": msg}

    def test_error_analysis(self) -> dict:
        """#报错分析：「测试」按钮 —— 用一条示例 429 报错真实跑一次分类。

        后台线程跑（不冻 GUI 主线程），结果经 ``window.evaluate_js`` 推
        ``window.relayErrorAnalysisDone(result)``（镜像 test_upstream）。
        立即返回 {"started": true}。
        """
        win = self._app.window

        def _worker() -> None:
            import asyncio

            from .error_analyzer import SAMPLE_ERROR_CONTEXT, classify_error

            try:
                result = asyncio.run(classify_error(self._app.settings, SAMPLE_ERROR_CONTEXT))
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": f"分析失败: {exc}"}
            try:
                win.evaluate_js(
                    "window.relayErrorAnalysisDone && window.relayErrorAnalysisDone("
                    + json.dumps(_json_safe(result), ensure_ascii=False, default=str)
                    + ")"
                )
            except Exception:
                pass  # 窗口已关 / 页面重载 —— 静默丢弃

        threading.Thread(target=_worker, daemon=True, name="relay-error-analysis-test").start()
        return {"started": True}

    def _thinking_options_for(self, platform: str, name: str) -> list[str]:
        """返回某上游的思考挡位列表（显式 thinking_options 或按模型兜底）。"""
        from relay.config import default_thinking_options
        cfg = self._app.settings.find_upstream(platform, name)
        if cfg is None:
            cfg = self._app.settings.active_config(platform)
        if cfg is None:
            return ["off", "enabled"]
        if cfg.thinking_options:
            return list(cfg.thinking_options)
        return default_thinking_options(cfg.model or "")

    def get_thinking_options(self, platform: str | None = None, name: str | None = None) -> dict:
        """#思考挡位：返回某上游（默认 anthropic 活跃上游）的可用挡位列表。"""
        platform = platform or "anthropic"
        name = name or self._app.settings.active_for(platform)
        options = self._thinking_options_for(platform, name)
        return {"platform": platform, "upstream": name, "options": options}

    def set_theme(self, name: str) -> str:
        """Set a specific theme preset and persist it (v0.11.3).

        The default-theme picker on the settings page — unlike
        ``toggle_theme`` this jumps straight to the chosen preset
        instead of cycling. Returns the applied theme name.
        """
        try:
            if name not in _THEMES:
                return self._app.theme_name
            self._app.theme_name = name
            self._app.settings.relay_gui_theme = name
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_THEME", name)
            # v0.89：实时流侧栏也是一个独立 webview 窗口，需要同步主题。
            # v0.108：精简版网格窗口一起广播。静默吞异常（侧栏 DOM 还没好
            # 时 evaluate_js 会抛）。
            try:
                self._app._push_theme_to_panels(name)
            except Exception:
                pass
            return name
        except Exception:
            return self._app.theme_name
    def get_autostart(self) -> dict:
        """Current boot-autostart state (v0.11.3 settings page)."""
        try:
            from relay import autostart
            return {"enabled": autostart.is_enabled()}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    # v0.11.21：内外转换显示开关（设置页 + snapshot）。
    def get_show_io_map(self) -> dict:
        """读取"内外转换显示"开关状态。"""
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_show_io_map", False))}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def set_show_io_map(self, enabled: bool) -> dict:
        """保存"内外转换显示"开关并持久化到 .env。"""
        try:
            self._app.settings.relay_gui_show_io_map = bool(enabled)
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_SHOW_IO_MAP", "1" if enabled else "0")
            return {"enabled": bool(enabled)}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_show_io_map", False)), "error": str(exc)}

    # v0.89：实时流侧栏开关。打开 → show + dock；关闭 → hide。
    # 跟随主窗的 min/restore 自动 hide/show（_on_main_minimized / _on_main_restored）。
    def get_live_panel(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel", False))}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def get_live_panel_api_key(self, request_id: str | None = None) -> dict:
        """v0.89 侧栏专用：返回某 in-flight 请求的**明文** api-key。

        SSE `/live/stream` 端点把 api_key 走 ``_mask_key`` 掩码后推送
        （与 `/live` HTTP 端点一致 —— 明文 key 绝不能进 HTTP 响应）。侧
        栏窗口是 pywebview 内嵌页面，桥调用只本机可达；这里直接读
        ``proxy._in_flight[rid].api_key`` 返回原值。request_id 缺省时
        取当前 SSE 最新一条（最近 done + inflight 的最前面那条）。

        返回 ``{"api_key": "..."}`` 或 ``{"api_key": "", "error": "..."}``。
        """
        try:
            from relay import proxy as _proxy
            target = None
            in_flight = getattr(_proxy, "_in_flight", {})
            in_done = getattr(_proxy, "_in_flight_done", [])
            if request_id and request_id in in_flight:
                target = in_flight[request_id]
            else:
                # 最近的：先 done 列表头部（最新 done），再 inflight
                # 第一条（按 started_at 排序的话略麻烦，简单取 dict 第
                # 一个 —— inflight 通常只有 1-2 条）。
                if in_done:
                    target = in_done[0]
                elif in_flight:
                    target = next(iter(in_flight.values()))
            if target is None:
                return {"api_key": "", "error": "no_inflight"}
            return {"api_key": getattr(target, "api_key", "") or ""}
        except Exception as exc:
            return {"api_key": "", "error": str(exc)}

    def set_live_panel(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_live_panel = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL", "1" if enabled else "0")
            try:
                if enabled:
                    # v0.97：打开开关 = 重新展示 → 强制 dock 贴右。
                    # v0.165e：球模式不主动 show 侧栏（空闲隐藏），由球 S1 流式
                    # 弹开。
                    # v0.176：dock 模式同样不主动 show —— 固定右侧空位，
                    # 有请求才弹（always_one 已中性化）。_dock_panel 先贴好位，
                    # 首个请求到达 _show_always_one 时从正确位置弹出。
                    self._app._dock_panel(force=True)
                    # 兜底：用户可能在主窗 loaded 之后才打开开关——
                    # 那时 _on_panel_loaded 不会再触发，SSE 线程不会自
                    # 动起。幂等起一下即可。
                    self._app._start_live_panel_stream()
                else:
                    pool = getattr(self._app, "_panel_pool", None)
                    if pool is not None:
                        # v0.176：走池的规范隐藏（复位 always_one_visible +
                        # 移屏外），避免标志位残留导致下次请求弹不出来。
                        pool._hide_always_one()
                    else:
                        self._app.panel_window.hide()
                # v0.165e：主开关开/关都重算球可见性（关→藏球，开→按规则恢复）。
                pool = getattr(self._app, "_panel_pool", None)
                if pool is not None:
                    pool.refresh_ball_visibility()
            except Exception:
                _logger.debug("setLivePanel show/hide failed", exc_info=True)
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel", False)), "error": str(exc)}

    # v0.113c：实时流侧栏「无边框」开关 —— 持久化 + 广播到侧栏窗口。
    def get_live_panel_frameless(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_panel_frameless", False))}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def set_live_panel_frameless(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_panel_frameless = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_PANEL_FRAMELESS", "1" if enabled else "0")
            self._app._push_frameless_to_panels(enabled)
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_panel_frameless", False)), "error": str(exc)}

    # ----- v0.104：实时栏管理三件套（concurrent / max / always_one） -----
    # 仅持久化 + 更新内存；是否真要 spawn/hide 由 panel pool 根据 start
    # 事件决策 —— Api 这一层不直接动窗口（除顶层 set_live_panel 因要
    # show/hide 主面板）。这样并发策略可以集中在一个地方。
    def get_live_panel_concurrent(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_concurrent", True))}
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}

    def set_live_panel_concurrent(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_live_panel_concurrent = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL_CONCURRENT", "1" if enabled else "0")
            # 关掉并发 → 池里多出来的 panel 隐藏（不销毁，done 后 10s
            # 自然清；用户在 done 前关了，看不到内容也无副作用）。
            # v0.165f：无论开/关都推送 setConcurrent 到侧栏前端，让前端在
            # 非并发模式固定显示三容器骨架（1 端点 + 1 完整内容 + 1 工具）。
            try:
                if getattr(self._app, "_panel_pool", None):
                    if not enabled:
                        self._app._panel_pool.enforce_concurrent_off()
                    self._app._panel_pool.set_concurrent(enabled)
            except Exception:
                _logger.debug("set_live_panel_concurrent pool adjust failed", exc_info=True)
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_concurrent", True)), "error": str(exc)}

    def get_live_panel_max(self) -> dict:
        try:
            n = int(getattr(self._app.settings, "relay_gui_live_panel_max", 3))
            n = max(1, min(8, n))
            return {"max": n}
        except Exception as exc:
            return {"max": 3, "error": str(exc)}

    def set_live_panel_max(self, n: int) -> dict:
        try:
            n = int(n)
            n = max(1, min(8, n))
            self._app.settings.relay_gui_live_panel_max = n
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL_MAX", str(n))
            # 调低 max：渐进隐藏超出（不销毁）。调高：下次 start 自
            # 然用新上限。
            try:
                if getattr(self._app, "_panel_pool", None):
                    self._app._panel_pool.enforce_max()
            except Exception:
                _logger.debug("set_live_panel_max pool adjust failed", exc_info=True)
            return {"max": n}
        except Exception as exc:
            cur = int(getattr(self._app.settings, "relay_gui_live_panel_max", 3) or 3)
            return {"max": max(1, min(8, cur)), "error": str(exc)}

    def get_live_panel_always_one(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_always_one", True))}
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}

    def set_live_panel_always_one(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_live_panel_always_one = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL_ALWAYS_ONE", "1" if enabled else "0")
            try:
                if getattr(self._app, "_panel_pool", None):
                    self._app._panel_pool.enforce_always_one()
            except Exception:
                _logger.debug("set_live_panel_always_one pool adjust failed", exc_info=True)
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_always_one", True)), "error": str(exc)}

    # ----- v0.130：自动延展侧栏（宽度）-----
    # 并发请求多时，实时栏窗口按 JS 上报的容器列数自动加宽。get/set 持久化
    # 到 .env；live_panel_layout(ncols) 是 JS→Python 的宽度上报入口。
    def get_live_panel_auto_extend(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_auto_extend", True))}
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}

    def set_live_panel_auto_extend(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_live_panel_auto_extend = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL_AUTO_EXTEND", "1" if enabled else "0")
            try:
                pool = getattr(self._app, "_panel_pool", None)
                if pool is not None:
                    pool.set_auto_extend(enabled)
            except Exception:
                _logger.debug("set_live_panel_auto_extend pool adjust failed", exc_info=True)
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_auto_extend", True)), "error": str(exc)}

    # v0.165：悬浮球 —— 桌面可拖动小圆球作为侧栏锚点，侧栏从球位置展开。
    def get_float_ball(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_float_ball", False))}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def set_float_ball(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_float_ball = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_FLOAT_BALL", "1" if enabled else "0")
            try:
                # v0.165e：球开关与磁吸(dock)互斥 —— 开球=浮动(脱离磁吸)，
                # 关球=磁吸(贴主窗右缘)。
                # v0.166：同步侧栏 owned 层级 —— 开球(浮动)→解耦，关球(磁吸)→绑定。
                self._app._panel_docked = not enabled
                self._app._sync_panel_owner()
                pool = getattr(self._app, "_panel_pool", None)
                if pool is not None:
                    pool.set_float_ball(enabled)
                if not enabled:
                    self._app._dock_panel(force=True)
            except Exception:
                _logger.debug("set_float_ball pool adjust failed", exc_info=True)
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_float_ball", False)), "error": str(exc)}

    # v0.170：悬浮球置顶 —— 球与侧栏一起保持 WS_EX_TOPMOST（同层级）。
    def get_float_ball_topmost(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_float_ball_topmost", True))}
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}

    def set_float_ball_topmost(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_float_ball_topmost = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_FLOAT_BALL_TOPMOST", "1" if enabled else "0")
            try:
                # 球：pool 里已建则运行时切置顶；未建则只记标志（建球时初始化）。
                pool = getattr(self._app, "_panel_pool", None)
                if pool is not None:
                    pool.set_float_ball_topmost(enabled)
                # 侧栏：跟随球置顶状态 —— 保证「球与侧栏恒同层级」。
                self._app._sync_panel_topmost()
            except Exception:
                _logger.debug("set_float_ball_topmost pool/sync failed", exc_info=True)
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_float_ball_topmost", True)), "error": str(exc)}

    def ball_clicked(self) -> dict:
        """悬浮球被点击（ghost_ball.html → 桥）→ pool 处理收起侧栏 / 切 S1<->S2。"""
        try:
            pool = getattr(self._app, "_panel_pool", None)
            if pool is not None:
                pool.ball_clicked()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def live_panel_layout(self, ncols: int) -> dict:
        """JS 布局后上报当前容器列数 → PanelPool 据 auto_extend 决定窗口宽度。"""
        try:
            pool = getattr(self._app, "_panel_pool", None)
            if pool is not None:
                pool.request_width(int(ncols))
            return {"ok": True, "cols": int(ncols)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_live_panel_layout_hint(self) -> dict:
        """侧栏初始化：返回布局常量（面板宽 / 最大列数 / auto_extend 开关）。"""
        try:
            pool = getattr(self._app, "_panel_pool", None)
            return {
                "panel_width": pool._panel_width if pool is not None else 400,
                "max_cols": pool._max_cols if pool is not None else 6,
                "auto_extend": bool(getattr(self._app.settings, "relay_gui_live_panel_auto_extend", True)),
            }
        except Exception as exc:
            return {"panel_width": 400, "max_cols": 6, "auto_extend": True, "error": str(exc)}

    def get_main_geometry(self) -> dict:
        """v0.137：返回主窗几何 + 侧栏 dock 状态 —— 侧栏用它把背景光晕
        （.bg-glow）对齐到主窗的屏幕坐标，使 dock 贴右时两个窗口的模糊
        光晕跨缝连续，不再有色层分裂。浮动时 docked=False，JS 把光晕还原
        成窗口相对（自包含）。"""
        try:
            app = self._app
            if getattr(app, "window", None) is None:
                return {"ok": False, "error": "no main window"}
            g = {
                "x": app.window.x,
                "y": app.window.y,
                "w": app.window.width,
                "h": app.window.height,
                "docked": bool(getattr(app, "_panel_docked", False)),
            }
            return {"ok": True, **g}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ----- v0.111：实时栏阶段感知 stale 超时三档 -----
    # 思考出字中 / 思考→正文衔接 / 正文出字中。设置页可改；写 .env +
    # 实时改 PanelPool 阈值（无需重启）。
    def _live_panel_timeout(self, attr: str, default: float) -> float:
        try:
            v = float(getattr(self._app.settings, attr, default) or default)
            return max(1.0, min(600.0, v))
        except Exception:
            return float(default)

    def _apply_live_panel_timeouts(self) -> None:
        """把 settings 里的三档阈值刷到 PanelPool（改一处即全量刷新）。"""
        try:
            pool = getattr(self._app, "_panel_pool", None)
            if pool is None:
                return
            pool._stale_thinking_secs = self._live_panel_timeout(
                "relay_live_panel_thinking_timeout", 60.0)
            pool._stale_gap_secs = self._live_panel_timeout(
                "relay_live_panel_gap_timeout", 20.0)
            pool._stale_text_secs = self._live_panel_timeout(
                "relay_live_panel_text_timeout", 10.0)
            _logger.info(
                "panel_pool timeouts updated: thinking=%.0fs gap=%.0fs text=%.0fs",
                pool._stale_thinking_secs, pool._stale_gap_secs, pool._stale_text_secs,
            )
        except Exception:
            _logger.debug("apply live panel timeouts failed", exc_info=True)

    def get_live_panel_thinking_timeout(self) -> dict:
        return {"seconds": self._live_panel_timeout("relay_live_panel_thinking_timeout", 60.0)}

    def set_live_panel_thinking_timeout(self, seconds: float) -> dict:
        try:
            seconds = max(1.0, min(600.0, float(seconds)))
            self._app.settings.relay_live_panel_thinking_timeout = seconds
            from relay.config import update_env_var
            update_env_var("RELAY_LIVE_PANEL_THINKING_TIMEOUT", str(seconds))
            self._apply_live_panel_timeouts()
            return {"seconds": seconds}
        except Exception as exc:
            return {"seconds": self._live_panel_timeout("relay_live_panel_thinking_timeout", 60.0), "error": str(exc)}

    def get_live_panel_gap_timeout(self) -> dict:
        return {"seconds": self._live_panel_timeout("relay_live_panel_gap_timeout", 20.0)}

    def set_live_panel_gap_timeout(self, seconds: float) -> dict:
        try:
            seconds = max(1.0, min(600.0, float(seconds)))
            self._app.settings.relay_live_panel_gap_timeout = seconds
            from relay.config import update_env_var
            update_env_var("RELAY_LIVE_PANEL_GAP_TIMEOUT", str(seconds))
            self._apply_live_panel_timeouts()
            return {"seconds": seconds}
        except Exception as exc:
            return {"seconds": self._live_panel_timeout("relay_live_panel_gap_timeout", 20.0), "error": str(exc)}

    def get_live_panel_text_timeout(self) -> dict:
        return {"seconds": self._live_panel_timeout("relay_live_panel_text_timeout", 10.0)}

    def set_live_panel_text_timeout(self, seconds: float) -> dict:
        try:
            seconds = max(1.0, min(600.0, float(seconds)))
            self._app.settings.relay_live_panel_text_timeout = seconds
            from relay.config import update_env_var
            update_env_var("RELAY_LIVE_PANEL_TEXT_TIMEOUT", str(seconds))
            self._apply_live_panel_timeouts()
            return {"seconds": seconds}
        except Exception as exc:
            return {"seconds": self._live_panel_timeout("relay_live_panel_text_timeout", 10.0), "error": str(exc)}

    # ----- v0.134：请求 done/error 后容器自动清除超时 -----
    def get_live_panel_done_clear_timeout(self) -> dict:
        return {"seconds": self._live_panel_timeout("relay_live_panel_done_clear_timeout", 10.0)}

    def set_live_panel_done_clear_timeout(self, seconds: float) -> dict:
        try:
            seconds = max(1.0, min(600.0, float(seconds)))
            self._app.settings.relay_live_panel_done_clear_timeout = seconds
            from relay.config import update_env_var
            update_env_var("RELAY_LIVE_PANEL_DONE_CLEAR_TIMEOUT", str(seconds))
            pool = getattr(self._app, "_panel_pool", None)
            if pool is not None:
                pool.set_done_clear_timeout(seconds)
            return {"seconds": seconds}
        except Exception as exc:
            return {"seconds": self._live_panel_timeout("relay_live_panel_done_clear_timeout", 10.0), "error": str(exc)}

    # ----- v0.134：侧栏前端可调项（工具清除超时 / 工具上限 / 端点列表高） -----
    def _set_live_panel_float_setting(self, attr: str, env_key: str, seconds: float, default: float) -> dict:
        try:
            seconds = float(seconds)
            setattr(self._app.settings, attr, seconds)
            from relay.config import update_env_var
            update_env_var(env_key, str(seconds))
            pool = getattr(self._app, "_panel_pool", None)
            if pool is not None and pool.always_one_window is not None:
                fn = self._live_panel_js_fn(attr)
                if fn:
                    pool._enqueue_op(lambda w=pool.always_one_window, j=f"{fn}({seconds})": w.evaluate_js(j))
            return {"value": seconds}
        except Exception as exc:
            return {"value": float(default), "error": str(exc)}

    def _live_panel_js_fn(self, attr: str) -> str:
        return {
            "relay_gui_live_panel_tools_clear_timeout": "setToolsClearSec",
            "relay_gui_live_panel_tools_cap": "setToolsCap",
            "relay_gui_live_panel_ep_list_vh": "setEpListVh",
        }.get(attr, "")

    def get_live_panel_tools_clear_timeout(self) -> dict:
        return {"seconds": float(getattr(self._app.settings, "relay_gui_live_panel_tools_clear_timeout", 5.0) or 5.0)}

    def set_live_panel_tools_clear_timeout(self, seconds: float) -> dict:
        return self._set_live_panel_float_setting(
            "relay_gui_live_panel_tools_clear_timeout", "RELAY_GUI_LIVE_PANEL_TOOLS_CLEAR_TIMEOUT", seconds, 5.0)

    def get_live_panel_tools_cap(self) -> dict:
        return {"value": int(getattr(self._app.settings, "relay_gui_live_panel_tools_cap", 30) or 30)}

    def set_live_panel_tools_cap(self, n: int) -> dict:
        return self._set_live_panel_float_setting(
            "relay_gui_live_panel_tools_cap", "RELAY_GUI_LIVE_PANEL_TOOLS_CAP", n, 30.0)

    def get_live_panel_ep_list_vh(self) -> dict:
        return {"value": float(getattr(self._app.settings, "relay_gui_live_panel_ep_list_vh", 30.0) or 30.0)}

    def set_live_panel_ep_list_vh(self, vh: float) -> dict:
        return self._set_live_panel_float_setting(
            "relay_gui_live_panel_ep_list_vh", "RELAY_GUI_LIVE_PANEL_EP_LIST_VH", vh, 30.0)

    # ----- v0.135：工具常驻开关 / 最小列数 / 列表字号档位 -----
    def get_live_panel_tools_always(self) -> dict:
        try:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_tools_always", False))}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def set_live_panel_tools_always(self, enabled: bool) -> dict:
        try:
            enabled = bool(enabled)
            self._app.settings.relay_gui_live_panel_tools_always = enabled
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL_TOOLS_ALWAYS", "1" if enabled else "0")
            # 即时下发侧栏：setToolsAlways 切换常驻语义 + scheduleLayout。
            pool = getattr(self._app, "_panel_pool", None)
            if pool is not None and pool.always_one_window is not None:
                try:
                    pool._enqueue_op(lambda w=pool.always_one_window, v=enabled: w.evaluate_js(f"setToolsAlways({json.dumps(v)})"))
                except Exception:
                    pass
            return {"enabled": enabled}
        except Exception as exc:
            return {"enabled": bool(getattr(self._app.settings, "relay_gui_live_panel_tools_always", False)), "error": str(exc)}

    def get_live_panel_min_cols(self) -> dict:
        try:
            n = int(getattr(self._app.settings, "relay_gui_live_panel_min_cols", 1) or 1)
            n = max(1, min(6, n))
            return {"value": n}
        except Exception as exc:
            return {"value": 1, "error": str(exc)}

    def set_live_panel_min_cols(self, n: int) -> dict:
        try:
            n = max(1, min(6, int(n) or 1))
            self._app.settings.relay_gui_live_panel_min_cols = n
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL_MIN_COLS", str(n))
            # 即时下发：setMinCols 在 layout() 里生效，约束 _minCols 下限。
            pool = getattr(self._app, "_panel_pool", None)
            if pool is not None and pool.always_one_window is not None:
                try:
                    pool._enqueue_op(lambda w=pool.always_one_window, v=n: w.evaluate_js(f"setMinCols({json.dumps(v)})"))
                except Exception:
                    pass
            return {"value": n}
        except Exception as exc:
            cur = int(getattr(self._app.settings, "relay_gui_live_panel_min_cols", 1) or 1)
            return {"value": max(1, min(6, cur)), "error": str(exc)}

    _LIST_FONT_TIERS = ("small", "medium", "large")

    def get_live_panel_list_font(self) -> dict:
        try:
            cur = str(getattr(self._app.settings, "relay_gui_live_panel_list_font", "medium") or "medium")
            if cur not in self._LIST_FONT_TIERS:
                cur = "medium"
            return {"value": cur}
        except Exception as exc:
            return {"value": "medium", "error": str(exc)}

    def set_live_panel_list_font(self, v: str) -> dict:
        try:
            v = str(v or "medium")
            if v not in self._LIST_FONT_TIERS:
                v = "medium"
            self._app.settings.relay_gui_live_panel_list_font = v
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL_LIST_FONT", v)
            # 即时下发：setListFont 切换 .live-panel-body 数据属性 → CSS 改写
            # .lp-ep-list 字号。
            pool = getattr(self._app, "_panel_pool", None)
            if pool is not None and pool.always_one_window is not None:
                try:
                    pool._enqueue_op(lambda w=pool.always_one_window, s=v: w.evaluate_js(f"setListFont({json.dumps(s)})"))
                except Exception:
                    pass
            return {"value": v}
        except Exception as exc:
            cur = str(getattr(self._app.settings, "relay_gui_live_panel_list_font", "medium") or "medium")
            return {"value": cur if cur in self._LIST_FONT_TIERS else "medium", "error": str(exc)}

    # ----- v0.104：桥接"一键全部显示/隐藏所有实时栏" -----
    # 顶栏 btn-live-panel 的新语义：不改 settings，按内存里现有的 panel
    # 池 toggle show/hide。返回当前"是否可见"给前端做按钮文案同步。
    def toggle_all_panels(self) -> dict:
        try:
            if not getattr(self._app, "_panel_pool", None):
                return {"visible": False}
            return {"visible": self._app._panel_pool.toggle_all_visible()}
        except Exception as exc:
            return {"visible": False, "error": str(exc)}

    # ----- v0.104：桥接"用户手动 X 关掉某个并发 panel" -----
    # panel 内的 X 按钮 → JS → 调本方法 → 仅 hide，不销毁，不改 settings。
    # rid 为空表示关 always_one。
    def hide_panel_for_rid(self, rid: str | None = None) -> dict:
        try:
            if getattr(self._app, "_panel_pool", None):
                self._app._panel_pool.hide_panel(rid)
            return {"hidden": True}
        except Exception as exc:
            return {"hidden": False, "error": str(exc)}

    def set_autostart(self, enabled: bool) -> dict:
        """Enable / disable the login-time autostart entry (v0.11.3).

        Writes the HKCU Run entry via ``relay.autostart`` — the same
        path ``--install-autostart`` uses. The entry launches the GUI
        under pythonw in tray-only mode; ``enable()`` also clears the
        Task Manager "disabled" marker.
        """
        try:
            from relay import autostart
            if enabled:
                autostart.enable()
            else:
                autostart.disable()
            return {"enabled": autostart.is_enabled()}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def get_start_hidden(self) -> dict:
        """#6：当前"启动后隐藏窗口"偏好（GUI 侧，持久化到 .env）。"""
        try:
            return {"enabled": bool(self._app.settings.relay_gui_start_hidden)}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def get_sysinfo(self) -> dict:
        """#14：电脑当前状态（磁盘 / 内存 / CPU / 网络接口数）。

        纯标准库实现，避免在 GUI 环境额外装 psutil。每个子项单独
        try/except，读不到就返回 None，前端显示 "—"。
        """
        import ctypes
        import shutil
        import socket

        def _fmt(n: float) -> str:
            g = 1024 ** 3
            if n >= g:
                return f"{n / g:.1f} GB"
            m = 1024 ** 2
            return f"{n / m:.0f} MB"

        info: dict = {}
        # 磁盘
        try:
            du = shutil.disk_usage(self._app.settings.project_root)
            info["disk"] = {"total": du.total, "used": du.used, "free": du.free,
                            "text": f"{_fmt(du.free)} 可用 / {_fmt(du.total)} 总量"}
        except Exception:
            info["disk"] = None
        # 内存（Windows 全局内存状态）
        try:
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32),
                            ("dwMemoryLoad", ctypes.c_uint32),
                            ("ullTotalPhys", ctypes.c_uint64),
                            ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64),
                            ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64),
                            ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                info["memory"] = {
                    "percent": int(ms.dwMemoryLoad),
                    "text": f"{_fmt(ms.ullAvailPhys)} 可用 / {_fmt(ms.ullTotalPhys)}",
                }
            else:
                info["memory"] = None
        except Exception:
            info["memory"] = None
        # CPU
        try:
            import os
            info["cpu"] = {"cores": os.cpu_count() or 0,
                           "text": f"{os.cpu_count() or 0} 核"}
        except Exception:
            info["cpu"] = None
        # 网络接口数（尽力而为）
        try:
            info["net"] = {"count": len(socket.if_nameindex()),
                           "text": f"{len(socket.if_nameindex())} 个网络接口"}
        except Exception:
            info["net"] = None
        return info

    def get_quota(self, name: str | None = None) -> dict:
        """#11：查询上游 provider 的额度 / 用量（opencode / minimax /
        deepseek）。名字匹配上游名，未命中时逐个尝试已知 provider。
        """
        from relay import quota_client
        from relay.config import Settings

        api_key = None
        if name:
            try:
                for plat in PLATFORMS:
                    for c in (self._app.settings.upstreams_for(plat) or []):
                        if c.name == name:
                            api_key = getattr(c, "api_key", None)
                            break
            except Exception:
                api_key = None
        if name:
            result = quota_client.query_provider(name, api_key)
            if result is not None:
                return {"provider": name, **result}
            return {"provider": name, "error": "查询失败或未配置凭证"}
        # 未指定名字：逐个尝试
        for pname in ("opencode-go", "minimax", "deepseek"):
            result = quota_client.query_provider(pname)
            if result is not None:
                return {"provider": pname, **result}
        return {"error": "无可用额度查询"}

    def set_start_hidden(self, enabled: bool) -> dict:
        """#6：保存"启动后隐藏窗口"偏好。"""
        try:
            self._app.settings.relay_gui_start_hidden = bool(enabled)
            from relay.config import update_env_var
            update_env_var("RELAY_GUI_START_HIDDEN", "1" if enabled else "0")
            return {"enabled": bool(enabled)}
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def set_upstream_model(
        self,
        platform: str,
        name: str,
        model: str | None,
    ) -> dict:
        """Persist the model-name override for one upstream (v0.65).

        Bridge for the GUI's "切 API 时弹窗选模型" flow. Writes through
        ``config.set_upstream_model`` (atomic + validates) and reloads
        so the backend immediately starts rewriting the request bodies.
        """
        ok, msg = set_upstream_model_cfg(self._app.settings, platform, name, model)
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "path": msg,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        return {"ok": True, "path": msg}

    def set_upstream_default_model(
        self,
        platform: str,
        name: str,
        default_model: str | None,
    ) -> dict:
        """Persist the v0.74 "auto 兜底" model for one upstream.

        Bridge for the GUI's Settings 页 default_model 输入框。语义跟
        ``set_upstream_model`` 平行,只是改写优先级更宽松(只在客户
        端发 "auto" 时触发,见 proxy.py 的回退链)。
        """
        ok, msg = set_upstream_default_model_cfg(
            self._app.settings, platform, name, default_model
        )
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "path": msg,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        return {"ok": True, "path": msg}

    def create_upstream(self, platform: str, payload: dict | None) -> dict:
        """Append a new upstream entry under ``platform``.

        v0.46 GUI "新建" 按钮背后。校验 + 持久化走 config.add_upstream
        （跟 apply_quota_edit 同样的 save_upstreams_json 流水线，
        PlatformConfig 二次校验保证坏 payload 写不进文件）。成功后
        reload settings 让下次 snapshot 直接看到新条目。

        Returns ``{"ok": True, "name": "..."}`` on success,
        ``{"ok": False, "error": "..."}`` on validation failure.
        """
        ok, msg = add_upstream(self._app.settings, platform, payload or {})
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "name": msg,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        # 同步中继进程内存里的 settings：GUI reload 只刷 GUI 进程的
        # _settings 缓存，中继 fork 时的 settings 快照还停在 fork 时
        # 的 upstreams.json 状态，里面没有刚写入的新条目。用户紧接着
        # 顶栏下拉切到这条新上游时，前端发 /api/upstreams/.../select
        # → 中继进程的 set_active 找不到 → HTTP404 no upstream named。
        # 这里发个本地刷新请求让中继也 reload settings。中继进程拒接
        # 时静默吞掉（不影响 add_upstream 本身已成功的事实）。
        try:
            import urllib.request as _ur
            base = getattr(self._app.settings, "base_url", "") or "http://127.0.0.1:8088"
            req = _ur.Request(
                f"{base}/api/upstreams/refresh",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=2.0) as r:
                _ = r.read()
        except Exception:
            pass
        return {"ok": True, "name": msg}

    def remove_upstream(
        self,
        platform: str,
        name: str,
    ) -> dict:
        """Drop an upstream entry entirely from the platform config.

        Wired to the "×" button in the top-right corner of each
        upstream-detail card on the 上游 page (v0.64). If the upstream
        being removed was the platform's currently-active one, the
        ``active`` pointer is cleared on disk too — see
        ``relay.config.remove_upstream``.

        Same atomic write + reload dance as ``update_upstream_quota`` /
        ``create_upstream`` — bad inputs are caught by ``PlatformConfig``
        validation before they touch the file, and on success
        ``self.settings`` is rebound so the next snapshot reflects the
        change.

        Returns ``{"ok": True, "platform": ..., "name": ...}`` on success,
        ``{"ok": False, "error": "..."}`` on validation or not-found.
        The frontend re-polls on success and surfaces a pill on failure.
        """
        ok, msg = remove_upstream_cfg(
            self._app.settings, platform, name,
        )
        if not ok:
            return {"ok": False, "error": msg}
        try:
            reload_settings()
            self._app.settings = get_settings()
        except Exception as exc:
            return {
                "ok": True,
                "platform": platform,
                "name": name,
                "warning": f"已写入但 reload 失败: {exc}",
            }
        return {
            "ok": True,
            "platform": platform,
            "name": name,
        }


# ------------------------------------------------------------------
# Theme
# ------------------------------------------------------------------

    def toggle_theme(self) -> str:
        """Flip between light and dark, persist to ``.env``, and return
        the new name so JS can update ``data-theme`` immediately."""
        return self._app._on_toggle_theme()

    # ------------------------------------------------------------------
    # Window controls (frameless mode — JS drives the buttons in the
    # custom HTML title bar)
    # ------------------------------------------------------------------

    def window_minimize(self) -> None:
        try:
            self._app.window.minimize()
        except Exception:
            pass

    def window_toggle_maximize(self) -> bool:
        """Toggle between maximised and the previous size. Returns the
        new state (``True`` when now maximised) so JS can swap icons."""
        try:
            win = self._app.window
            # pywebview's Window has ``maximize`` / ``restore`` but no
            # ``is_maximized`` getter on the WinForms backend. Probe via
            # ``GetWindowPlacement`` instead so the button can flip its
            # icon correctly.
            user32 = ctypes.windll.user32
            placement = ctypes.wintypes.WINDOWPLACEMENT()
            placement.length = ctypes.sizeof(placement)
            user32.GetWindowPlacement(win.native, ctypes.byref(placement))
            is_maxed = placement.showCmd == 2  # SW_SHOWMAXIMIZED
            if is_maxed:
                win.restore()
                return False
            win.maximize()
            return True
        except Exception:
            return False

    def window_close(self) -> None:
        # v0.21.1: 直接走托盘路径，不再依赖 FormClosing 事件的 cancel 机制。
        #
        # 原实现调 ``self._app.window.destroy()`` → pywebview 内部触发
        # WinForms FormClosing → fire closing event → ``_on_closing`` 返
        # 回 True 让 ``args.Cancel = True`` 取消关闭。
        #
        # 但 pywebview 的 ``Event.set()`` 把回调扔到独立线程
        # (``t.start()``)，并**立即**读 ``return_values``，那时线程
        # 还没填值——所以 ``should_cancel`` 永远是 False，args.Cancel
        # 永远没设，窗口照样销毁。详见 ``webview/event.py:Event.set``。
        #
        # 直接调 ``_hide_to_tray()`` 绕过这个机制：托盘是用户点击关闭
        # 时的预期行为，"真的退出"走托盘菜单的 ``_quit_from_tray()`` 那
        # 条路径（那里设了 ``_tray_quit_requested``，destroy 后会正常
        # 走 shutdown）。
        self._app._hide_to_tray()

    def panel_close(self) -> None:
        """侧栏 X 按钮：frameless 后没有原生 closing 事件，改由 HTML
        的 .win-ctrl-close 按钮 → JS → 此 bridge → 复用 _hide_panel_and_sync_off。
        与原生 _on_panel_closing 等价，只是触发源不同。"""
        self._app._hide_panel_and_sync_off()

    def resize_window(self, width: int, height: int, fix_bits: int) -> bool:
        """Drag-resize for the frameless window.

        JS-side ``wireEdgeResize`` detects edge proximity on mousedown
        and computes the new width / height + the ``FixPoint`` bits
        that say which corner / edge the user is dragging from. We
        delegate to ``pywebview.window.Window.resize``, which itself
        calls Win32 ``SetWindowPos`` (with the per-window DPI scale
        baked in) — so HiDPI placement lands on the right pixel.

        The frameless window has no native resize border, so this is
        the only way the user can size the window short of
        power-sizing the title-bar buttons. Maxes out at nothing
        here — let the JS guard against the maximized state so a
        maximized window doesn't get a weird "resize" into a tiny
        corner of the monitor.
        """
        from webview.window import FixPoint
        try:
            self._app.window.resize(int(width), int(height), FixPoint(int(fix_bits)))
            return True
        except Exception:
            return False

    def reload_gui(self) -> bool:
        """Reload the webview page — dev tool (v0.11.4).

        Appends a fresh ``_dev`` query param so the document URL changes
        and the page re-reads ``index.html`` from disk. The referenced
        assets (``app.js`` / ``styles.css``) are file:// URLs — Chromium
        serves those straight off disk on every request, so edits show
        up without restarting the whole GUI process.
        """
        try:
            from relay.gui import _WEB_DIR
            url = (_WEB_DIR / "index.html").as_uri()
            ts = int(time.time() * 1000)
            self._app.window.load_url(f"{url}?_dev={ts}")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 完全透传模式
    # ------------------------------------------------------------------

    def get_passthrough_mode(self) -> dict:
        """读透传模式开关。"""
        try:
            future = _passthrough_executor.submit(
                _http_get_json,
                self._app.settings.base_url,
                "/api/passthrough/mode",
                _PASSTHROUGH_HTTP_TIMEOUT,
            )
            return future.result(timeout=_PASSTHROUGH_HTTP_TIMEOUT)
        except Exception as exc:
            return {"passthrough_mode": False, "error": str(exc)}

    def set_passthrough_mode(self, enabled: bool) -> dict:
        """切换透传模式开关。"""
        try:
            future = _passthrough_executor.submit(
                _http_put_json,
                self._app.settings.base_url,
                "/api/passthrough/mode",
                {"enabled": bool(enabled)},
                _PASSTHROUGH_HTTP_TIMEOUT,
            )
            result = future.result(timeout=_PASSTHROUGH_HTTP_TIMEOUT)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP{exc.code}: {body}".strip()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        # 同步到本地 Settings —— 中间件读 app.state.settings，但 GUI
        # 副本 self._app.settings 用于 get_status 展示，需要一起更新。
        try:
            self._app.settings.passthrough_mode = bool(enabled)
        except Exception:
            pass
        return {"ok": True, "passthrough_mode": bool(enabled)}

    def get_passthrough_upstreams(self) -> dict:
        """列出自动发现的透传上游。"""
        try:
            with urllib.request.urlopen(
                f"{self._app.settings.base_url}/api/passthrough/upstreams",
                timeout=2.0,
            ) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except Exception as exc:
            return {"upstreams": [], "error": str(exc)}

    def rename_passthrough_upstream(
        self, url: str, model_field_name: str, api_key_alias: str,
        display_name: str,
    ) -> dict:
        """重命名一个透传上游。display_name="" 清除用户命名。"""
        try:
            from urllib.parse import quote
            encoded = quote(url, safe="")
            qs = (
                f"model_field_name={quote(model_field_name)}"
                f"&api_key_alias={quote(api_key_alias)}"
                f"&display_name={quote(display_name)}"
            )
            req = urllib.request.Request(
                f"{self._app.settings.base_url}"
                f"/api/passthrough/upstreams/{encoded}/rename?{qs}",
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=2.0) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP{exc.code}: {body}".strip()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


class App:
    """Webview shell + data bridge.

    Owns the ``ServerProcess``, the snapshot cache, and a polling thread
    that rebuilds the cache from SQLite + the relay's ``/live`` endpoint.
    """

    def __init__(self, settings: Settings, autostart: bool = True, diag: bool = False) -> None:
        self.settings = settings
        self.theme_name: str = _resolve_theme(settings.relay_gui_theme)
        self.diag = diag

        # ServerProcess is owned here so the JS bridge can call into it
        # without re-importing.
        from relay.server import ServerProcess

        self.server = ServerProcess(settings)
        self.autostart = autostart

        # v0.21 tray. Created lazily the first time the user hides the
        # window; before then the icon isn't useful and the pystray
        # thread is just dead weight. ``_tray_quit_requested`` is set by
        # the tray's "退出中继" menu item and is what finally tears the
        # app down — without it, closing the window would loop forever.
        self._tray_quit_requested = False
        # v0.94: set when the app is genuinely tearing down (tray "退出
        # 中继" or the pipe "quit" takeover). ``_on_panel_closing`` then
        # skips its evaluate_js sync and lets the destroy proceed — the
        # panel's FormClosing→evaluate_js path deadlocks the UI thread
        # during shutdown (WebView2 result never dispatches → UI thread
        # never returns to the message pump → the caller's Invoke waits
        # forever).
        self._shutting_down = False
        self._tray = None
        self._latest_snapshot: dict = {}
        # v0.12 分发告警轮询游标（/api/alerts?since=），随快照一起拉取。
        self._last_alert_id: int = 0
        # v0.113o 报错分析轮询状态：只分析 GUI 启动后**新出现**的请求
        # 错误（游标首次置为当前 MAX(id)，历史错误不重放）。分类在后台
        # 线程外呼小模型，结果经挂起队列进下一帧 snapshot["error_hints"]。
        self._last_error_hint_cursor: int = 0
        self._pending_error_hints: list[dict] = []
        self._hint_lock = threading.Lock()
        self._hint_debounce: dict[tuple, float] = {}
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Build the webview window. ``js_api=Api(self)`` exposes Api
        # methods to JS as ``window.pywebview.api.<method>``.
        self.api = Api(self)
        index_url = (_WEB_DIR / "index.html").as_uri()

        self.window = webview.create_window(
            title="RelayMeter",
            url=index_url,
            js_api=self.api,
            width=1180,
            height=900,
            background_color=_hex_bg(self.theme_name),
            # Frameless = no OS title bar / borders. The HTML/CSS layer
            # below builds its own glass title bar at the top. Drag is
            # delegated to pywebview's ``easy_drag`` handler — WebView2
            # does NOT reliably honour ``-webkit-app-region: drag`` on
            # body-level elements, and easy_drag already intercepts
            # mousedown on any non-interactive region (everything that
            # isn't a button / input / nav-item). The existing
            # light/dark body is already full-bleed, so removing the
            # system chrome is what makes the GUI feel like "one piece
            # of glass" rather than an app sitting inside a Windows
            # window.
            frameless=True,
            resizable=True,
            easy_drag=True,
        )
        # Push the initial theme once the page DOM is ready.
        self.window.events.loaded += self._on_loaded
        # Stop the server cleanly on window close.
        self.window.events.closing += self._on_closing

        # v0.89：实时流侧栏窗口。
        #   * 启动即建（hidden），由 .env 里的 RELAY_GUI_LIVE_PANEL 决定是否
        #     可见 —— 开关管 show()/hide()，不重新建/销毁（避免 WebView2
        #     启动延迟）。
        #   * **frameless=True + easy_drag=True**：与主窗同款无系统标题栏
        #     设计，HTML 顶部复用 styles-20260817.css 的 .window-controls
        #     / .win-ctrl / .win-ctrl-close 自绘右上角关闭按钮。窗口拖动
        #     交给 pywebview easy_drag（自动跳过 button/input/select/a），
        #     反向联动仍走 events.moved → _on_panel_moved。
        #   * 起始位置 = 主窗右侧（load 时就 dock 好，免得第一次显示时
        #     出现一秒的"窗口飞出去"动画）。
        #   * 复用同一个 Api 实例 —— js_api 可共享，方法按 method 名分派。
        # v0.170：侧栏启动白屏修复 —— URL 带 #theme=<当前主题>（**必须用
        # hash fragment，不能用 ?query**：WebView2 顶层 file:// 导航会把
        # ?query 当文件系统路径 → ERR_FILE_NOT_FOUND → 整页白屏无 UI）。
        # fragment 只在客户端参与，文件正常加载；live_panel.html 头部同步
        # 脚本首帧读 location.hash 设 data-theme，--root-bg 立即取到正确
        # 底色（深色 #0a0a0a），不再白到 _on_panel_loaded 异步 setTheme。
        live_panel_url = (
            (_WEB_DIR / "live_panel.html").as_uri()
            + "#theme=" + self.theme_name
        )
        # v0.109：实时栏 —— **单窗口**（主栏区 + 网格区）。并发时窗口宽度
        # 向右侧延长，右侧露出网格区（流块）。self.panel_window 别名保留给
        # 该唯一窗口（_dock_panel / _on_panel_closing / _on_panel_moved 等
        # 老代码不动），宽度动态变化由 PanelPool._relayout 管理。
        from .panel_pool import PanelPool, attach_main_moved_to_pool, attach_sse_push_to_pool

        def _dock_getter():
            try:
                # v0.107：主窗 native 窗口还没创建/显示（webview.start()
                # 之前，App.__init__ 阶段）时，window.x/y/width/height 会在
                # events.shown.wait(15) 上**各**阻塞最多 15s —— PanelPool.start
                # 里 3 次调用 _dock_getter 就是 45s 启动慢的根因。用 shown 事件
                # 短路：还没显示就返回 0 尺寸让调用方跳过定位；真正的 dock 由
                # webview.start() 之后的 loaded / moved 事件用真实几何补上。
                # （hidden=True 的窗口在 create 时也走 Show→Hide，shown 会置位，
                # 所以 start 之后这里不会阻塞。）
                if not getattr(self.window.events, "shown", None).is_set():
                    return (0, 0, 0, 0)
                return (self.window.x, self.window.y, self.window.width, self.window.height)
            except Exception:
                return (0, 0, 0, 0)

        def _screen_getter():
            try:
                import webview as _wv
                s = _wv.screen
                w = int(getattr(s, "width", 0) or 0)
                h = int(getattr(s, "height", 0) or 0)
                if w <= 0 or h <= 0:
                    return (0, 0)
                return (w, h)
            except Exception:
                return (0, 0)

        # v0.97/v0.136：侧栏 dock 状态。True = 贴右（随主窗 moved/resized
        # 重新贴）；False = 浮动（主窗移动不拽，拖回右缘 ≤_SNAP_THRESHOLD
        # 才重新磁吸）。必须在 PanelPool 构造/start 之前初始化，因为池的
        # _apply_geometry 会经 docked_getter 读它。
        # v0.165e：dock 与悬浮球互斥 —— 开球即浮动(False)，关球即磁吸(True)。
        # 初始值跟浮球开关反相，避免启动时 docked 与球模式不一致。
        self._panel_docked = not bool(getattr(settings, "relay_gui_float_ball", False))
        # v0.184：悬浮球页 URL —— 球+侧栏合并单容器渲染层 ghost_panel.html
        # （球帽 + 右侧实时面板 surface，body.collapsed 收起/展开）。
        # ghost_ball.html（v0.179 纯幽灵球）/ float_ball.html（v0.165 水波球）
        # 保留不删。
        float_ball_url = (_WEB_DIR / "ghost_panel.html").as_uri()
        self._panel_pool = PanelPool(
            api=self.api,
            url=live_panel_url,
            theme_bg=_hex_bg(self.theme_name),
            panel_width=400,
            panel_height=900,
            dock_getter=_dock_getter,
            screen_getter=_screen_getter,
            docked_getter=lambda: self._panel_docked,
            float_ball_url=float_ball_url,
            destroy_after_done_sec=int(getattr(settings, "relay_live_panel_done_clear_timeout", 10) or 10),
            # v0.111：阶段感知 stale 超时三档 —— 从设置注入，设置页改后
            # 由 set_*_timeout 桥实时改池阈值（无需重启）。
            stale_thinking_secs=float(getattr(settings, "relay_live_panel_thinking_timeout", 60.0) or 60.0),
            stale_gap_secs=float(getattr(settings, "relay_live_panel_gap_timeout", 20.0) or 20.0),
            stale_text_secs=float(getattr(settings, "relay_live_panel_text_timeout", 10.0) or 10.0),
        )
        self._panel_pool.start(int(getattr(settings, "relay_gui_live_panel_max", 3) or 3))
        # 别名：让所有老代码（_dock_panel/_on_panel_closing 等）继续走
        # always_one 窗口，不动业务逻辑。
        self.panel_window = self._panel_pool.always_one_window
        # always_one 老 hooks（loaded/closing/moved）保留。
        try:
            self.panel_window.events.loaded += self._on_panel_loaded
        except Exception:
            _logger.debug("always_one loaded hook failed", exc_info=True)
        try:
            self.panel_window.events.closing += self._on_panel_closing
        except Exception:
            _logger.debug("always_one closing hook failed", exc_info=True)
        try:
            self.panel_window.events.moved += self._on_panel_moved
        except Exception:
            _logger.debug("always_one moved hook failed", exc_info=True)
        # v0.167：注册侧栏拖动结束回调 —— settle 定时器到点（moved 静默
        # ~200ms）调本回调，按最后缓存的 _pending_snap 真实执行 dock/undock。
        try:
            self._panel_pool.set_drag_settle_callback(self._on_drag_settle_apply)
        except Exception:
            _logger.debug("register drag settle callback failed", exc_info=True)
        # 主窗 moved/resized 钩到池（重排 always_one + grid）。
        attach_main_moved_to_pool(self, self._panel_pool)
        # 把 SSE 推送路径从单一窗口改成池（按 rid 路由）。
        attach_sse_push_to_pool(self, self._panel_pool)
        # 锚接：主窗 moved/resized → **仅当面板处于 dock 状态**才重新贴右
        # （浮动时主窗移动不拽）；minimized → 侧栏隐；restored → 重显+dock。
        self.window.events.moved += self._dock_panel
        self.window.events.resized += self._dock_panel
        self.window.events.minimized += self._on_main_minimized
        self.window.events.restored += self._on_main_restored
        # Reentrancy guard: _dock_panel 调 panel_window.move → 触发侧栏
        # moved 事件 → _on_panel_moved → _update_panel_snap → 又回到
        # _dock_panel ... 无限递归（WinForms `Move` 拖拽期间高频率触发，
        # 每秒几十次）。_dock_panel 在改侧栏几何前**置**本标志，_on_panel_
        # moved 入口先查它短路 —— 锁住回调里的 chain reaction。
        self._dock_panel_semaphore = False
        # v0.166：侧栏 owned 层级同步态（None=未同步）。True=已绑定主窗，
        # False=已解耦（浮动）。_sync_panel_owner 按 _panel_docked 幂等切换。
        self._panel_owner_bound = None
        # 注：self._panel_docked 已在 PanelPool 构造前初始化（初始 True）。
        # v0.89：实时流 SSE 订阅线程（连 8088 的 /live/stream，解析
        # `data: ...` 帧，evaluate_js 推到侧栏）。daemon 线程常驻，断
        # 线指数退避（顶 10s）；侧栏关掉开关时短暂停拉新事件以省
        # 带宽，再次开启时无缝续上。启动时即起（_on_panel_loaded 里
        # 二次检查一次，避免页面脚本没就绪前先推 JS 报错）。
        self._sse_thread: threading.Thread | None = None
        self._sse_stop_event = threading.Event()
        _logger.info(
            "App.__init__ done theme=%s port=%d url=%s",
            self.theme_name, settings.port, index_url,
        )
        # Debug-only view switcher on a dedicated localhost port. Lets
        # unattended verification trigger setView from outside the
        # WebView2 (where pyautogui clicks get filtered by Chromium).
        # Gated on ``diag`` so constructing an App in a test — or in any
        # non-diagnostic run — never binds a real socket.
        self._debug_http = _start_debug_http(self) if diag else None

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def switch_view(self, name: str) -> bool:
        """Evaluate ``setView(name)`` in the WebView2.

        Returns True when the request name is one of the known views and
        the underlying window is alive. Used by the debug HTTP helper and
        any future deep-link / IPC entry point."""
        valid = {"overview", "live", "history", "upstreams", "settings"}
        if name not in valid:
            return False
        if not getattr(self, "window", None):
            return False
        try:
            self.window.evaluate_js(f"setView({name!r})")
        except Exception as exc:
            _logger.warning("switch_view %r failed: %r", name, exc)
            return False
        return True

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def active_label(self) -> str:
        """Return a short label of the currently-active upstream config
        for the topbar.

        Falls back to "" if no platform has been configured yet — keeps
        the JS layer from rendering "undefined" before the user has
        touched the upstream selector. Called by ``Api.get_status``
        from a different class, so no leading underscore.
        """
        try:
            for p in PLATFORMS:
                name = self.settings.active_for(p)
                if name:
                    return f"{p}:{name}"
        except Exception:
            pass
        return ""

    def _rebuild_snapshot(self) -> None:
        """Build a single JSON-serialisable snapshot from the various
        data sources (SQLite for history, /live for in-flight) and
        cache it on ``self._latest_snapshot``.

        Each sub-fetch is wrapped in a try/except so a single broken
        source (DB locked, server down) doesn't take down the whole
        snapshot — the others still flow through. The cache is the
        only source of truth; callers read via ``Api.get_snapshot()``.
        """
        snapshot: dict[str, Any] = {
            "ts": time.time(),
            "by_platform": {},
            "by_model": {},
            "by_upstream": {},
            # v0.155：客户端工具名聚合（平台流量卡片数据源）。
            "by_agent": {},
            "by_hour": [],
            "recent": [],
            "live": [],
            "upstreams": {},
            "active_per_platform": {},
            "models_by_upstream": {},
            # v0.11.21：内外转换显示开关（前端渲染 live 转换条用）。
            "show_io_map": bool(getattr(self.settings, "relay_gui_show_io_map", False)),
            # v0.89：实时流侧栏开关（顶栏 btn-live-panel 用，避免切页回弹）。
            "live_panel": bool(getattr(self.settings, "relay_gui_live_panel", False)),
            # v0.104：实时栏管理三件套 —— 设置页渲染初始值。
            "live_panel_concurrent": bool(getattr(self.settings, "relay_gui_live_panel_concurrent", True)),
            "live_panel_max": max(1, min(8, int(getattr(self.settings, "relay_gui_live_panel_max", 3) or 3))),
            "live_panel_always_one": bool(getattr(self.settings, "relay_gui_live_panel_always_one", True)),
            # v0.130：自动延展侧栏（宽度）—— 设置页初始值。
            "live_panel_auto_extend": bool(getattr(self.settings, "relay_gui_live_panel_auto_extend", True)),
            # v0.165：悬浮球 —— 设置页初始值。
            "float_ball": bool(getattr(self.settings, "relay_gui_float_ball", False)),
            # v0.170：悬浮球置顶 —— 设置页初始值（默认 True，球与侧栏同层级）。
            "float_ball_topmost": bool(getattr(self.settings, "relay_gui_float_ball_topmost", True)),
            # v0.111：实时栏阶段感知 stale 超时三档（秒）—— 设置页渲染初始值。
            "live_panel_thinking_timeout": float(getattr(self.settings, "relay_live_panel_thinking_timeout", 60.0) or 60.0),
            "live_panel_gap_timeout": float(getattr(self.settings, "relay_live_panel_gap_timeout", 20.0) or 20.0),
            "live_panel_text_timeout": float(getattr(self.settings, "relay_live_panel_text_timeout", 10.0) or 10.0),
            # v0.134：请求 done/error 后容器自动清除超时（秒）—— 设置页渲染初始值。
            "live_panel_done_clear_timeout": float(getattr(self.settings, "relay_live_panel_done_clear_timeout", 10.0) or 10.0),
            # v0.134：侧栏前端可调项 —— 设置页渲染初始值。
            "live_panel_tools_clear_timeout": float(getattr(self.settings, "relay_gui_live_panel_tools_clear_timeout", 5.0) or 5.0),
            "live_panel_tools_cap": int(getattr(self.settings, "relay_gui_live_panel_tools_cap", 30) or 30),
            # v0.135：工具常驻 / 最小列数 / 列表字号档位 —— 设置页渲染初始值。
            "live_panel_tools_always": bool(getattr(self.settings, "relay_gui_live_panel_tools_always", False)),
            "live_panel_min_cols": max(1, min(6, int(getattr(self.settings, "relay_gui_live_panel_min_cols", 1) or 1))),
            "live_panel_list_font": str(getattr(self.settings, "relay_gui_live_panel_list_font", "medium") or "medium"),
            "live_panel_ep_list_vh": float(getattr(self.settings, "relay_gui_live_panel_ep_list_vh", 30.0) or 30.0),
            # v0.104：一键全部隐藏状态（前端顶栏「实时流」按钮用）。
            "live_panel_all_hidden": bool(getattr(self, "_panel_pool", None) and self._panel_pool._all_hidden),
            # v0.113c：实时栏侧栏无边框 —— 设置页初始值。
            "live_panel_frameless": bool(getattr(self.settings, "relay_gui_panel_frameless", False)),
            # 透传模式：前端 body class 驱动的权威来源。GUI 副本由
            # set_passthrough_mode 与中继同步，重启 GUI 后从 .env 读回。
            "passthrough_mode": bool(getattr(self.settings, "passthrough_mode", False)),
            # v0.113u：设置页首帧用 —— autostart 从 Windows 注册表读，
            # start_hidden 从 .env 读，save_messages 从 relay /api/settings 读。
            "start_hidden": bool(getattr(self.settings, "relay_gui_start_hidden", False)),
            "save_messages": bool(getattr(self.settings, "relay_save_messages", True)),
            # autoswitch（设置页初始值，运行时由 /api/settings 刷过来）——
            # relay 进程持有权威源，GUI 这份作为快照供首帧渲染。
            "autoswitch_enabled": bool(getattr(self.settings, "relay_quota_autoswitch", False)),
            "autoswitch_pool": list(getattr(self.settings, "relay_autoswitch_pool", None) or []),
            "autoswitch_at": float(getattr(self.settings, "relay_quota_switch_at", 0.9)),
            # v0.113u：报错分析设置（upstreams.json 顶层 error_analysis）
            # —— 设置页首帧用。模型清单过大不进 snapshot（按需异步拉）。
            "error_analysis_enabled": bool(getattr(self.settings, "error_analysis_enabled", False)),
            "error_analysis_upstream": getattr(self.settings, "error_analysis_upstream", None),
            "error_analysis_model": getattr(self.settings, "error_analysis_model", None),
            # v0.188：支持图片的模型（顶层 vision_models）—— 勾选集供首帧。
            # 模型名清单过大不进 snapshot（按需异步 get_vision_models 拉）。
            "vision_models": list(getattr(self.settings, "vision_models", None) or []),
        }
        # v0.113u：autostart 从 Windows 注册表读（self.autostart.is_enabled()）
        # —— 放到 dict 外避免内嵌 try/except 语法歧义。异常则默认 False。
        try:
            snapshot["autostart_enabled"] = (
                self.autostart.is_enabled() if self.autostart else False
            )
        except Exception:
            snapshot["autostart_enabled"] = False
        # Lazy import: these touch SQLite / the network and we don't
        # want to pay the cost when the GUI was launched purely to show
        # a window.
        from relay import tui

        try:
            db = self.settings.relay_db
            snapshot["by_platform"] = tui.fetch_totals(db)
            # 今日用量：总览页"今日用量"卡片过去误读 by_platform
            # (全量汇总)，任何时候看到的都是历史总和。单独算一次 today
            # 窗口的 by_platform 给前端用。UTC 日界，与 tui.fetch_recent
            # 等其它 today 桶一致（见 tui.py:1350 的 UTC midnight 模式）。
            import time as _time
            _today_start = (int(_time.time()) // 86400) * 86400
            snapshot["by_platform_today"] = tui.fetch_totals(
                db, since=_today_start,
            )
            snapshot["by_model"] = tui.fetch_by_model(db)
            # v0.155：客户端工具名聚合（平台流量卡片）。失败回落空 dict。
            try:
                snapshot["by_agent"] = tui.fetch_agent_totals(db)
            except Exception:
                snapshot["by_agent"] = {}
            snapshot["recent"] = [
                dict(row) for row in tui.fetch_recent(db, limit=20)
            ]
            # v0.64：每个上游实际跑过的 distinct model 列表。
            # 即便 ``allowed_models`` 没配置，上游详情卡片也有可删
            # 的 chip 显示。失败回落到空 dict —— 上游详情卡片就
            # 只显示 allowed_models 那部分（原来 v0.63 之前的行为）。
            try:
                snapshot["models_by_upstream"] = tui.fetch_models_by_upstream(db)
            except Exception:
                snapshot["models_by_upstream"] = {}
            # fetch_by_upstream needs the configured names so it can
            # zero-fill missing ones. Also pre-format 5h_release via
            # tui._format_release so the frontend never has to duplicate
            # the <1m / Xm / Xh Ym / now / — rule (single source of truth).
            try:
                # Flatten all configured upstreams (across both platforms)
                # into a list of dicts that includes the v0.19 quota +
                # multiplier + allow-list config, then hand to the cost
                # helper which combines them with the rolling-window SQL.
                # v0.119：先构造真实 cfg 列表，再过 link_resolver.resolve_links
                # 加虚拟 cfg（合并统计用），让 fetch_by_upstream_with_costs /
                # fetch_by_upstream_model 一次拿到所有「待聚合的 cfg」。
                real_upstream_configs = [
                    {
                        "name": c.name,
                        "quota_5h": c.quota_5h,
                        "model_multipliers": c.model_multipliers,
                        "allowed_models": c.allowed_models,
                        "billing_unit": c.billing_unit,
                        "token_fields": c.token_fields,
                    }
                    for c in self.settings.upstreams_for()
                ]
                upstream_configs = resolve_links(real_upstream_configs)
                raw_up = tui.fetch_by_upstream_with_costs(
                    db, upstreams=upstream_configs
                )
                # v0.67: token-billed upstreams show an all-time running
                # total instead of 5h/week/month counts + release countdown
                # (see renderUpstream in app.js) — a single request can be
                # 100 tokens or 100k, so per-window request counts aren't
                # a meaningful signal for them the way they are for
                # count-billed upstreams.
                # v0.77: 累计 tokens 在所有上游上都要算 —— 上游状态卡片的
                # 自动排序用它的"消耗量"（tokens 多的排前），不限于 token
                # 计费上游。
                upstream_cfgs_for_total = [
                    {"name": cfg["name"],
                     "_is_virtual": bool(cfg.get("_is_virtual")),
                     "linked_names": list(cfg.get("linked_names") or [])}
                    for cfg in upstream_configs
                    if isinstance(cfg.get("name"), str) and cfg["name"]
                ]
                total_tokens = tui.fetch_total_tokens_by_upstream(
                    db, upstreams=upstream_cfgs_for_total
                )
                snapshot["by_upstream"] = {
                    name: {
                        **data,
                        "5h_release_text": tui._format_release(
                            data.get("5h_release")
                        ),
                        "total_tokens": total_tokens.get(name),
                    }
                    for name, data in raw_up.items()
                }
            except Exception:
                snapshot["by_upstream"] = {}
            try:
                # v0.68：按 (upstream, model) 拆分的调用详情，供"上游状态"
                # 卡片点击弹出的小窗口使用。复用同一份 upstream_configs
                # （已经带 billing_unit/token_fields/model_multipliers），
                # 不重新拼一份。
                snapshot["by_upstream_model"] = tui.fetch_by_upstream_model(
                    db, upstreams=upstream_configs
                )
            except Exception:
                snapshot["by_upstream_model"] = {}
            try:
                # 24h hour-bucketed aggregation for the token consumption
                # chart. Cheap (one indexed scan + group by integer bucket).
                snapshot["by_hour"] = tui.fetch_by_hour(db, since=86400)
            except Exception:
                snapshot["by_hour"] = []
            snapshot["upstreams"] = {
                p: [
                    {
                        "name": c.name,
                        "url": c.url,
                        "note": c.note,
                        "quota_5h": c.quota_5h,
                        "model_multipliers": dict(c.model_multipliers),
                        "allowed_models": list(c.allowed_models),
                        # v0.65 起 sidebar 下拉按 (upstream, model) 拆成
                        # 单独 option，靠这个字段判定哪个 option 该带
                        # selected。漏了这个字段会导致下拉重绘时选不中
                        # 刚切过去的那个 model，看起来像"切了又跳回去"。
                        "model": c.model,
                        # v0.12.1：单池下 dict key 不再区分平台，前端
                        # 靠 wire 把上游归到 anthropic / openai 分组
                        # （上半 anthropic、下半 openai）。
                        "wire": c.wire,
                        # v0.119：把该真实上游的 linked_upstreams 透传给
                        # 前端；设置页「链接上游」chip 直接读这个字段显示。
                        "linked_upstreams": list(c.linked_upstreams or []),
                    }
                    for c in self.settings.upstreams_for(p)
                ]
                for p in PLATFORMS
            }
            # v0.119：链接组清单（按 size>1 闭包）—— 前端供「合并统计」
            # 标识 / 实时提示 / 设置页 chip 反查使用。[["A","B","C"], [...], ...]
            try:
                snapshot["link_groups"] = [
                    sorted(grp) for grp in resolve_links(real_upstream_configs)
                    if grp.get("_is_virtual") and len(grp.get("linked_names") or []) >= 2
                ]
                # 取链接组里虚拟 cfg 的 linked_names（resolve_links 返回 dict）
                snapshot["link_groups"] = [
                    grp["linked_names"] for grp in resolve_links(real_upstream_configs)
                    if grp.get("_is_virtual") and len(grp.get("linked_names") or []) >= 2
                ]
            except Exception:
                snapshot["link_groups"] = []
            snapshot["active_per_platform"] = {
                p: self.settings.active_for(p) for p in PLATFORMS
            }
            # v0.69：把内存里的 quick_switch 配置原样转发给前端。失败
            # 兜底空 list,前端看到空就 hidden,跟没配一样。
            try:
                snapshot["quick_switch"] = list(
                    getattr(self.settings, "quick_switch", None) or []
                )
            except Exception:
                snapshot["quick_switch"] = []
        except Exception:
            # DB missing / locked / unreadable — keep the empty defaults
            # so the UI shows "no data" instead of an error banner.
            pass
        # Live data needs the relay running. fetch_live already swallows
        # connection errors and returns [] on failure.
        try:
            snapshot["live"] = tui.fetch_live(
                self.settings.base_url, timeout=0.5
            )
        except Exception:
            pass
        # v0.12 分发告警（混合态 / 未知 key）——从中继 /api/alerts 增量
        # 拉取，失败静默（中继没起或旧版本无此端点时前端看到空列表）。
        try:
            import urllib.request as _ur
            url = (
                f"{self.settings.base_url}/api/alerts?since={self._last_alert_id}"
            )
            with _ur.urlopen(url, timeout=1.0) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
            new_alerts = list(data.get("alerts") or [])
            if new_alerts:
                self._last_alert_id = max(a.get("id", 0) for a in new_alerts)
            snapshot["alerts"] = new_alerts
        except Exception:
            snapshot["alerts"] = []

        # v0.113o 报错分析 —— 只读 relay.db 扫新出现的请求错误，后台线程
        # 分类成给用户的中文提示，经 snapshot["error_hints"] 推前端 toast。
        try:
            if self.settings.error_analysis_enabled:
                self._schedule_error_hint_classifications()
        except Exception:
            pass
        snapshot["error_hints"] = self._drain_error_hints()

        self._latest_snapshot = snapshot

    # ----- v0.113o 报错分析：扫错误 + 后台分类 + 挂起队列 -----

    def _schedule_error_hint_classifications(self) -> None:
        """扫 relay.db 里新出现的请求错误，起后台线程分类。

        幂等：游标只前进；同 (upstream, status_code 或 error 前缀) 60s
        内去重（防 429 风暴刷爆分类调用与 toast）。fire-and-forget ——
        分类失败不重试，游标照常推进（不卡轮询）。
        """
        import sqlite3

        try:
            s = self.settings
            if not getattr(s, "relay_db", None) or not Path(s.relay_db).is_file():
                return
            conn = sqlite3.connect(f"file:{s.relay_db}?mode=ro", uri=True)
            try:
                if self._last_error_hint_cursor == 0:
                    # 首次：游标置为当前最大 id —— 只分析 GUI 启动后新错误。
                    row = conn.execute("SELECT COALESCE(MAX(id),0) FROM requests").fetchone()
                    self._last_error_hint_cursor = int(row[0] or 0)
                    return
                rows = conn.execute(
                    "SELECT id, ts, platform, model, upstream, status_code, error "
                    "FROM requests "
                    "WHERE error IS NOT NULL AND error != '' "
                    "  AND error != 'client_disconnect' AND id > ? "
                    "ORDER BY id LIMIT 20",
                    (self._last_error_hint_cursor,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            _logger.debug("error-hint scan failed: %r", exc)
            return

        now = time.time()
        new_cursor = self._last_error_hint_cursor
        for row in rows:
            rid = int(row[0] or 0)
            if rid > new_cursor:
                new_cursor = rid
            upstream = row[4] or ""
            status = row[5]
            err = row[6] or ""
            key = (upstream, status if status is not None else err.split(":")[0])
            with self._hint_lock:
                last = self._hint_debounce.get(key, 0.0)
            if last and (now - last) < 60.0:
                continue
            with self._hint_lock:
                self._hint_debounce[key] = now
            ctx = {
                "id": rid,
                "ts": row[1],
                "platform": row[2],
                "model": row[3],
                "upstream": upstream,
                "status_code": status,
                "error": err,
            }
            threading.Thread(
                target=self._classify_error_hint,
                args=(ctx,),
                daemon=True,
                name=f"relay-error-hint-{rid}",
            ).start()
        self._last_error_hint_cursor = new_cursor

    def _classify_error_hint(self, ctx: dict) -> None:
        """后台线程：分类一条请求错误，成功结果进挂起队列。"""
        import asyncio

        from .error_analyzer import build_context, classify_error

        try:
            result = asyncio.run(classify_error(self.settings, build_context(ctx)))
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        if not result.get("ok"):
            return
        with self._hint_lock:
            self._pending_error_hints.append({
                "id": ctx.get("id"),
                "type": result.get("type", "other"),
                "category": result.get("category", result.get("type", "other")),
                "hint": result.get("hint", ""),
                "platform": ctx.get("platform") or "",
                "upstream": ctx.get("upstream") or "",
                "model": ctx.get("model") or "",
                "ts": ctx.get("ts"),
            })

    def _drain_error_hints(self) -> list[dict]:
        """取走挂起队列里的分类结果（锁内）。"""
        with self._hint_lock:
            out = self._pending_error_hints
            self._pending_error_hints = []
            return out

    def _poll_loop(self) -> None:
        """Run ``_rebuild_snapshot`` every ``_POLL_INTERVAL`` seconds.

        Daemon thread — killed when the GUI process exits. ``stop_event``
        is checked between ticks so the loop exits promptly on close
        instead of waiting for the next interval boundary.
        """
        while not self._stop_event.is_set():
            try:
                self._rebuild_snapshot()
            except Exception:
                # Snapshot rebuild must never crash the thread — the
                # frontend would just see a stale dict.
                pass
            # ``Event.wait`` returns as soon as ``_stop_event`` is set,
            # so closing the window exits the loop promptly instead of
            # waiting for the next interval boundary.
            self._stop_event.wait(_POLL_INTERVAL)

    def _start_poll(self) -> None:
        """Spawn the polling daemon thread. No-op if already running."""
        if self._poll_thread is not None and self._poll_thread.is_alive():
            _logger.info("_start_poll skipped (already running)")
            return
        # Prime the snapshot synchronously so the first JS poll has
        # something to render instead of an empty dict.
        _logger.info("_start_poll priming snapshot")
        try:
            self._rebuild_snapshot()
            _logger.info("_start_poll primed keys=%s", list(self._latest_snapshot.keys()))
        except Exception as e:
            _logger.warning("_start_poll prime failed: %r", e)
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="relay-gui-poll", daemon=True
        )
        self._poll_thread.start()
        _logger.info("_start_poll thread started tid=%s", self._poll_thread.name)

    # ------------------------------------------------------------------
    # Webview event handlers
    # ------------------------------------------------------------------

    def _apply_window_icons(self) -> None:
        """v0.111 自定义任务栏图标。

        pywebview 6 的 ``create_window`` 没有 icon 参数 —— 主窗默认挂
        WebView2/python 通用图标。frameless 窗口仍出现在任务栏，这里用
        Win32 ``LoadImageW``（LR_LOADFROMFILE 读多尺寸 .ico）拿 HICON，
        再 ``WM_SETICON`` 挂到主窗（实时侧栏窗口 native 就绪时也挂）。
        纯 ctypes，不依赖 pythonnet。best-effort：失败只记日志。

        .ico 由 relay.icon.write_ico() 生成到系统临时目录（运行时合成，
        不随仓库带资产文件 —— 与 tray.py 同款哲学）。

        HICON 缓存到 self._taskbar_hicon：窗口存活期内要保持 handle 有效，
        重复调用（_on_loaded / _on_panel_loaded 都可能触发）复用同一份，
        避免每次 LoadImageW 泄漏 GDI 句柄。进程退出时由 OS 回收。
        """
        try:
            if getattr(self, "_taskbar_hicon", None):
                hicon = self._taskbar_hicon
            else:
                import tempfile

                from .icon import write_ico

                ico_path = Path(tempfile.gettempdir()) / "relay_app_icon.ico"
                try:
                    if not ico_path.exists():
                        write_ico(ico_path)
                except Exception:
                    _logger.warning("window icon gen failed", exc_info=True)
                    return
                user32 = ctypes.windll.user32
                user32.LoadImageW.restype = ctypes.wintypes.HANDLE
                user32.LoadImageW.argtypes = [
                    ctypes.wintypes.HINSTANCE, ctypes.wintypes.LPCWSTR,
                    ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                ]
                user32.SendMessageW.argtypes = [
                    ctypes.wintypes.HWND, ctypes.c_uint, ctypes.wintypes.WPARAM,
                    ctypes.wintypes.LPARAM,
                ]
                LR_LOADFROMFILE = 0x10
                IMAGE_ICON = 1
                hicon = user32.LoadImageW(
                    None, str(ico_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE,
                )
                if not hicon:
                    _logger.warning("LoadImageW returned null for %s", ico_path)
                    return
                self._taskbar_hicon = hicon
            WM_SETICON = 0x0080
            ICON_SMALL, ICON_BIG = 0, 1
            user32 = ctypes.windll.user32
            applied = 0
            for win in (self.window, getattr(self, "panel_window", None)):
                native = getattr(win, "native", None)
                if native is None:
                    continue
                try:
                    hwnd = native.Handle.ToInt32()
                except Exception:
                    continue
                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
                applied += 1
            if applied:
                _logger.info("window taskbar icons applied (%d window(s))", applied)
        except Exception:
            _logger.warning("window icon apply failed", exc_info=True)

    def _on_loaded(self) -> None:
        """Set the theme attribute on <html> as soon as the page loads
        so CSS custom properties pick up the right palette before the
        first paint. Also kicks off the polling thread so JS's first
        poll() returns a real snapshot instead of {}."""
        _logger.info("_on_loaded fired (window ready)")
        self._apply_window_icons()
        # v0.166：悬浮球在 GUI 启动（主窗 loaded）时就激活，不等侧栏首条请求流。
        # 侧栏窗口创建为 hidden（panel_pool.start），浮动(球)模式下空闲隐藏，
        # _on_panel_loaded 可能要等首条请求才触发 → 球被延迟到首条流才出现
        # （用户反馈）。主窗 loaded 时 native 已就绪，直接创建/显示；幂等且
        # 自带 _ball_should_show 门（实时流侧栏 OFF 时不弹球）。
        try:
            pool = getattr(self, "_panel_pool", None)
            if pool is not None:
                pool.refresh_ball_visibility()
        except Exception:
            _logger.debug("ball eager activate failed", exc_info=True)
        try:
            self.window.evaluate_js(f"setTheme('{self.theme_name}')")
            _logger.info("_on_loaded setTheme OK")
        except Exception as e:
            # v0.12.1：页面脚本可能还没执行完（loaded 竞态）——后台重试
            # 几次，避免主题一直停在默认值。
            _logger.warning("_on_loaded setTheme failed (will retry): %r", e)

            def _retry_set_theme(attempt: int = 0) -> None:
                if attempt >= 4:
                    _logger.warning("_on_loaded setTheme retry exhausted")
                    return
                import time as _time
                _time.sleep(0.5)
                try:
                    self.window.evaluate_js(f"setTheme('{self.theme_name}')")
                    _logger.info("_on_loaded setTheme OK (retry #%d)", attempt + 1)
                except Exception:
                    _retry_set_theme(attempt + 1)

            threading.Thread(
                target=_retry_set_theme, daemon=True, name="relay-settheme-retry",
            ).start()
        # v0.11.17：#6 启动后隐藏窗口 —— 立即缩到托盘（tray 懒创建）。
        if self.settings.relay_gui_start_hidden:
            try:
                self.window.hide()
                _logger.info("_on_loaded start-hidden applied (window hidden to tray)")
            except Exception as e:
                _logger.warning("_on_loaded start-hidden hide failed: %r", e)
        # v0.89：实时流侧栏开关（顶栏图标 + 设置页共用后端）。开关为开
        # 时启动就让侧栏 dock 好 + 显示。
        # 这个分支在 _on_panel_loaded 里**也**有副本（侧栏 DOM 比主窗
        # 晚到），两处都需要：主窗先到（判定是否初始开）→ 侧栏到（实际
        # show + dock）。
        # v0.166：gate 收紧 —— 主开关 ON + 磁吸 + always_one 才启动强显；
        # 原 `live_panel OR always_one` 因 always_one 默认 True，主开关关或
        # 悬浮球模式下侧栏也被强显（用户反馈「没开实时流侧栏侧栏却在」）。
        try:
            pool = getattr(self, "_panel_pool", None)
            if pool is not None and pool._should_show_on_startup():
                self._dock_panel(force=True)
                self.panel_window.show()
        except Exception as e:
            _logger.warning("_on_loaded show panel failed: %r", e)
        # Diag probe — check if the page's DOM is actually ready and
        # whether the JS bridge object landed on window. Runs only when
        # ``--diag`` was passed; logs ``readyState``, the pywebview flag,
        # and whether the expected DOM nodes exist. Anything below the
        # script-tag line means the DOMContentLoaded handler never ran.
        if self.diag:
            try:
                probe = self.window.evaluate_js(
                    "JSON.stringify({"
                    "rs: document.readyState,"
                    "pw: typeof window.pywebview,"
                    "api: typeof (window.pywebview && window.pywebview.api),"
                    "boot: !!window.__bootMarker,"
                    "hero: !!document.getElementById('hero-status'),"
                    "tickCount: window.__tickCount|0,"
                    "tickEntry: window.__tickEntry|0,"
                    "tickExit: window.__tickExit|0,"
                    "lastErr: window.__lastErr|''"
                    "})"
                )
                _logger.info("_on_loaded diag probe=%s", probe)
            except Exception as e:
                _logger.warning("_on_loaded diag probe failed: %r", e)
            # Warmup: call get_status from Python to confirm the bridge
            # round-trips at all. If this works but JS-driven calls never
            # land, the problem is in the JS side (setInterval not
            # firing, exception swallowed silently, etc).
            try:
                warmup = self.api.get_status()
                _logger.info(
                    "_on_loaded warmup OK keys=%s running=%s owner=%s",
                    list(warmup.keys()),
                    warmup.get("running"), warmup.get("owner"),
                )
            except Exception as e:
                _logger.warning("_on_loaded warmup failed: %r", e)
            # Also probe what the JS side sees — explicitly call the
            # bridge from JS and report any synchronous throw.
            try:
                js_probe = self.window.evaluate_js(
                    "(async()=>{try{"
                    "  var r=await window.pywebview.api.get_status();"
                    "  return 'OK '+JSON.stringify(r).slice(0,200);"
                    "}catch(e){return 'THROW '+e.message;}})()"
                )
                _logger.info("_on_loaded js-direct=%s", str(js_probe)[:300])
            except Exception as e:
                _logger.warning("_on_loaded js-direct failed: %r", e)
            # Delayed probe — runs 8 s later so we can see whether the
            # JS poll loop is still alive past the initial paint. Most
            # "frozen bridge" bugs only surface after the first couple
            # of setInterval ticks.
            import threading as _t
            def _delayed_probe():
                try:
                    p2 = self.window.evaluate_js(
                        "JSON.stringify({"
                        "tickCount: window.__tickCount|0,"
                        "tickEntry: window.__tickEntry|0,"
                        "tickExit: window.__tickExit|0,"
                        "lastErr: window.__lastErr|''"
                        "})"
                    )
                    _logger.info("_delayed_probe (8s) %s", p2)
                except Exception as e:
                    _logger.warning("_delayed_probe failed: %r", e)
            _t.Timer(8.0, _delayed_probe).start()
        # Start the polling thread once the window is loaded — running
        # it before webview.start() would be wasted cycles if the user
        # closed the window before the page rendered.
        self._start_poll()
        # v0.105：pool 窗口改为**按需创建**（有并发请求 assign() 时才
        # create_window，创建后立即 show → 页面边加载边渲染，天然有
        # 内容）。不再需要启动期 warm-up（warmup_pool_windows 已删）。
        # 启动只预建 always_one 一个 hidden 窗口，无可见窗口残留。

    def _on_closing(self) -> None:
        """Decide between "hide to tray" and "shut down" on window close.

        v0.21: closing the window no longer kills the relay. The window
        hides, a tray icon appears, and the proxy keeps running. A
        follow-up close (driven by the tray's "退出中继" menu item) sets
        ``_tray_quit_requested`` and falls through to the legacy cleanup
        path, which still only tears down a child this process started.

        Returning ``True`` from this handler is the winforms-BrowserView
        event mechanism's "cancel the close" signal (see
        ``webview.platforms.winforms.BrowserView.on_closing``).
        """
        if not self._tray_quit_requested:
            self._hide_to_tray()
            return True  # cancel the WinForms FormClosing event
        self._stop_event.set()
        try:
            if self.server.is_running:
                self.server.stop()
        except Exception:
            pass
        if self._debug_http is not None:
            try:
                self._debug_http.shutdown()
                self._debug_http.server_close()
            except Exception:
                pass
            self._debug_http = None
        return None  # explicit, lets the close proceed

    def _hide_to_tray(self) -> None:
        """First-time setup of the tray icon, then hide the window."""
        if self._tray is None:
            from relay.tray import Tray

            self._tray = Tray(
                on_show=self._show_from_tray,
                on_quit=self._quit_from_tray,
            )
        # ensure_started is idempotent and swallows errors — a tray
        # failure mustn't keep the window stuck open.
        self._tray.ensure_started()
        try:
            self.window.hide()
        except Exception:
            _logger.warning("window.hide() failed", exc_info=True)

    def _show_from_tray(self) -> None:
        """Tray "显示窗口" callback. Brings the window back."""
        try:
            self.window.show()
        except Exception:
            _logger.warning("window.show() from tray failed", exc_info=True)
        # v0.95：侧栏已是主窗 owned form —— 主窗 hide→tray 时侧栏随 owner
        # 一起被 WinForms 隐藏，这里必须按开关显式补回，否则侧栏永远消失
        # 在托盘里（_on_main_restored 同款逻辑）。
        try:
            if getattr(self, "settings", None) and getattr(
                self.settings, "relay_gui_live_panel", False
            ):
                self.panel_window.show()
                # v0.136：托盘重新展示同样尊重浮动状态 —— 浮动则不拽回磁吸位
                if self._panel_docked:
                    self._dock_panel(force=True)
        except Exception:
            _logger.debug("panel show from tray failed", exc_info=True)

    def _quit_from_tray(self) -> None:
        """Tray "退出中继" callback. Tears the relay down via the normal
        path: set the quit flag, destroy the window, which then re-enters
        ``_on_closing`` — now with ``_tray_quit_requested`` true."""
        self._tray_quit_requested = True
        # PyInstaller frozen windowed exe：托盘菜单回调跑在 pystray 的
        # daemon 线程，往下 destroy() 要跨线程 marshal 回主线程的
        # WinForms 循环 —— 冻结态下这条链会挂住（点"退出中继"无反应，
        # 实测进程不退出）。不依赖窗口消息循环，直接硬清理 + 强退：
        # stop 掉自己拉起的 relay 子进程，再 os._exit。源码态（pythonw）
        # 继续走下方正常 teardown。
        if getattr(sys, "frozen", False):
            self._stop_event.set()
            # server.stop() 最坏能阻塞到 taskkill 超时（~10s）；托盘点
            # 退出不该等，给一个总闸：起 daemon 线程尽力杀子进程，主
            # 进程带固定小超时等它，到点就走 os._exit。
            _stop_thread = threading.Thread(
                target=self.server.stop, name="frozen-quit-stop", daemon=True
            )
            _stop_thread.start()
            _stop_thread.join(timeout=3.0)
            _logger.info("frozen quit: exiting")
            os._exit(0)
        # v0.94: signal the teardown so the sidebar's FormClosing handler
        # neither evaluates JS nor cancels its own destroy (that path
        # deadlocks the UI thread while a caller waits on Invoke).
        self._shutting_down = True
        # v0.89：必须先 destroy 侧栏再 destroy 主窗。WinForms 的
        # `webview.start()` 仅在 `BrowserView.instances` 为空时返回
        # (winforms.py 的 Application.Exit 分支) —— 留着侧栏不销毁，
        # 主窗 destroy 之后 `webview.start()` 永不返回，进程挂住。
        try:
            self.panel_window.destroy()
        except Exception:
            _logger.warning("panel_window.destroy() from tray failed", exc_info=True)
        # v0.109：单窗口合并后无独立 grid_window —— panel_window（唯一
        # 侧栏窗口）已 destroy，无需再额外销毁网格窗口。
        # Closing the window will synchronously call _on_closing, which
        # sees the flag and runs the original teardown. There's a
        # possibility the close happens before this method returns
        # (re-entrant); that's fine — the flag is already set.
        try:
            self.window.destroy()
        except Exception:
            _logger.warning("window.destroy() from tray failed", exc_info=True)
        # v0.94: ``Application.Run()`` only returns after pywebview's
        # on_close calls ``Application.Exit()`` — which happens only when
        # the *last* window's FormClosed empties ``BrowserView.instances``.
        # Both windows must actually close, so both closing handlers must
        # return ``None`` (not ``False`` — pywebview cancels the close on
        # False). ``_on_panel_closing`` handles that via ``_shutting_down``.

    # ------------------------------------------------------------------
    # v0.89 实时流侧栏（live panel）—— 锚接 / 磁吸 / 生命周期
    # ------------------------------------------------------------------
    #
    # 设计回顾：
    #   * 主窗移 → **仅当侧栏 dock 时**重新贴右跟随（_dock_panel）。
    #   * 侧栏拖动（easy_drag）→ 只动侧栏（v0.97 起去掉反向联动主窗）；
    #     拖到主窗右缘 ≤_SNAP_THRESHOLD 且纵向重叠 → 磁吸 dock
    #     （_update_panel_snap），拖出阈值 → 脱开浮动。
    #   * 重入防护：move 回调必须互相忽略，否则 WinForms 高频 Move
    #     事件把两侧来回拉八九次（实测抖动过）。用 _dock_panel_semaphore
    #     这个布尔标志位：进入回调时检查，置 True 后改对方几何，改完清。
    #   * 抖动真正出现时退化方案：换 native.ResizeEnd 再 dock 一次（但
    #     这一版先不上，标志位能解决 95% 的场景）。

    def _dock_panel(self, *args: object, force: bool = False) -> None:
        """把侧栏贴到主窗右边缘（右缘对齐 + 高度同步）。

        ``force=False``（主窗 moved/resized 事件默认路径）：仅当侧栏处于
        dock 状态才重新贴 —— 浮动时主窗移动不拽面板。
        ``force=True``（启动 / 开关打开 / 主窗还原 / 磁吸命中）：无条件
        贴右并标记 docked。
        """
        if self._dock_panel_semaphore:
            return
        # v0.165：球模式 —— 侧栏从球位置展开（_apply_geometry 球分支），
        # 主窗 moved/resized 不再把侧栏拽回右缘。
        try:
            if getattr(self, "_panel_pool", None) and self._panel_pool.ball_mode:
                self._panel_pool.refresh_geometry()
                return
        except Exception:
            _logger.debug("dock_panel ball_mode guard failed", exc_info=True)
        if not force and not self._panel_docked:
            return
        self._panel_docked = True
        # v0.166：dock 态绑定侧栏与主窗同一层级。注意 ball_mode 已在上面
        # return（浮动不会进到这里），所以这里必为磁吸 → AddOwnedForm。
        self._sync_panel_owner()
        try:
            mw_x = self.window.x
            mw_y = self.window.y
            mw_w = self.window.width
            mw_h = self.window.height
            pw_w = self.panel_window.width
            pw_h = self.panel_window.height
            if mw_w <= 0 or mw_h <= 0 or pw_w <= 0:
                return
            # 侧栏新位置 = (主窗右沿, 主窗顶)。尺寸同步高度。
            new_x = mw_x + mw_w
            new_y = mw_y
            # 几何无变化时跳过 —— 减少无效 native 移动（每次 window.move
            # 都会触发 WinForms.Move 事件，可能造成自循环）。
            if self.panel_window.x == new_x and self.panel_window.y == new_y and \
               pw_h == mw_h:
                return
            self._dock_panel_semaphore = True
            try:
                self.panel_window.move(new_x, new_y)
                if pw_h != mw_h:
                    self.panel_window.resize(pw_w, mw_h)
            finally:
                self._dock_panel_semaphore = False
        except Exception:
            _logger.debug("dock_panel failed", exc_info=True)
        # v0.104：always_one 之外的并发池窗口也要跟主窗移动 → 重排
        # 网格。pool.refresh_geometry 内部会重算所有可见窗口位置。
        try:
            if getattr(self, "_panel_pool", None):
                self._panel_pool.refresh_geometry()
        except Exception:
            _logger.debug("dock_panel pool refresh failed", exc_info=True)

    def _on_drag_settle_apply(self) -> None:
        """v0.167：侧栏拖动结束（settle 定时器到点）→ 按 _pending_snap 落地。

        流程：池侧 _panel_dragging=False、_pending_snap 仍存；本回调先取
        出缓存意图、清池标志，再以真路径重新走一次 snap（_update_panel_snap
        内部已恢复成「非拖动态」路径，直接执行 dock/undock）。
        """
        pool = getattr(self, "_panel_pool", None)
        if pool is None:
            return
        pending = pool.pending_snap()
        # 清标志位（apply_pending_snap 也清，这里再清一次保险）
        pool.apply_pending_snap()
        if pending is None:
            return
        try:
            # 重跑 snap 真路径：以 (x, y) 当前坐标 + 非拖动态门控 → 真落地。
            # 注意：pool._panel_dragging 已在 apply_pending_snap 置 False，
            # _update_panel_snap 见 panel_dragging()=False 直接执行。
            self._update_panel_snap(int(pending["x"]), int(pending["y"]))
            _logger.info("drag settle apply pending=%s", pending)
        except Exception:
            _logger.debug("drag settle apply failed", exc_info=True)

    def _on_panel_moved(self, x: int, y: int) -> None:
        """v0.97：侧栏拖动事件 —— 磁吸判定，不再反向联动主窗。

        拖侧栏只动侧栏（easy_drag 已移动窗口）；本回调只在每次 moved
        时检查新位置是否进入主窗右缘磁吸区：命中 → _dock_panel(force=True)
        磁吸（内部几何短路 + semaphore 防递归）；未命中 → 标记浮动。

        v0.167：拖动期间只预览、不落地。pool._panel_dragging=True 期间每次
        moved 都重置 settle 定时器（200ms 静默视为松手）；松手后一次性
        按最后缓存的意图（_pending_snap）执行。
        """
        if self._dock_panel_semaphore:
            return
        # 隐藏侧栏会 move 到 (-32000,-32000) 屏外 —— 哨兵位直接跳过，不触发
        # 拽回/磁吸判定（原有逻辑在 _update_panel_snap 内，提到这里统一拦）。
        if x < -1000 or y < -1000:
            return
        # v0.176：dock 模式（球关）—— 侧栏固定贴主窗右缘，不允许拖离。
        # easy_drag 在 edgechromium 下是页面加载时注入 JS、运行时不生效，
        # 所以这里兜底：任何 moved（含用户拖动）立即拽回 dock 位。只在实际
        # 偏离 dock 位时才拽（_dock_panel 内部有几何短路，这里再减一次噪音）。
        if not bool(getattr(self.settings, "relay_gui_float_ball", False)):
            try:
                pw = self.panel_window
                mw = self.window
                if not (pw.x == mw.x + mw.width and pw.y == mw.y
                        and pw.height == mw.height):
                    self._dock_panel(force=True)
            except Exception:
                _logger.debug("dock re-glue failed", exc_info=True)
            return
        try:
            pool = getattr(self, "_panel_pool", None)
            if pool is not None:
                # 球拖动期间完全屏蔽 snap（防「球拖到主窗右缘 → 面板跟过去 →
                # 触发 moved → 误磁吸」连锁）。
                if pool.ball_dragging():
                    return
                # 每次 moved 都进入 drag-settle 路径：arm 定时器 + （如未拖动）
                # 翻 panel_dragging=True。需求：「只要没松手，磁吸就不真的
                # 发生」—— moved 触发即视为拖动中，纯预览缓存意图，松手
                # （200ms 静默）由 _on_drag_settle_apply 按最后缓存落地。
                # 副作用：启动 loaded / 单次 resize 联动也会跑 settle 路径
                # （200ms 延迟后执行常规 snap），但功能等价于直接执行，
                # 用户感知不到差异。
                if not pool.panel_dragging():
                    pool.begin_panel_drag()
                pool.arm_drag_settle()
            self._update_panel_snap(x, y)
        except Exception:
            _logger.debug("panel snap failed", exc_info=True)

    def _update_panel_snap(self, x: int, y: int) -> None:
        """磁吸判定：|侧栏左缘 − 主窗右缘| ≤ _SNAP_THRESHOLD 且纵向重叠
        → 磁吸 dock（磁吸模式）；否则视为浮动（球模式）。

        v0.165e：磁吸与悬浮球互斥 ——
          拖侧栏近右缘 → 关球 + 贴右（磁吸）；
          拖侧栏离右缘 → 开球 + 球跟随侧栏左上角（浮动锚点不变式）。
        v0.136：脱开时把浮动位置（x, y）记到池 —— 浮动状态下窗口可能被
        X 隐藏后重新显示，需要恢复到用户拖到的位置而不是拽回磁吸位。
        v0.167：拖动期间只预览、不落地 —— 球拖动直接 return；侧栏拖动把意图
        暂存到 pool._pending_snap，松手（settle 定时器 200ms 静默）由
        _on_drag_settle 一次性执行。
        """
        # v0.176：dock 模式（球关）—— 侧栏固定贴右，无磁吸/浮动判定
        # （拖离路径已由 _on_panel_moved 直接拽回；本守卫兜住
        # _on_drag_settle_apply 的 settle 路径）。
        if not bool(getattr(self.settings, "relay_gui_float_ball", False)):
            return
        # 隐藏侧栏会把窗口 move 到 (-32000,-32000) 屏外，moved 事件可能带
        # 这个哨兵值 —— 直接跳过，不触发磁吸/开球判定。
        if x < -1000 or y < -1000:
            return
        mw_x = self.window.x
        mw_y = self.window.y
        mw_w = self.window.width
        mw_h = self.window.height
        pw_h = self.panel_window.height
        if mw_w <= 0 or mw_h <= 0 or pw_h <= 0:
            return
        pool = getattr(self, "_panel_pool", None)
        overlap = not (y + pw_h < mw_y - _SNAP_SLACK or y > mw_y + mw_h + _SNAP_SLACK)
        gap = abs(x - (mw_x + mw_w))
        would_dock = bool(overlap and gap <= _SNAP_THRESHOLD)
        # v0.167：拖动期间只缓存意图、不真执行（_on_drag_settle 松手时按最后
        # 一次缓存的 would_dock 落地）。侧栏已停下（_panel_dragging=False）的
        # 常规 snap 路径直接走真实执行。
        if pool is not None and pool.panel_dragging():
            pool.preview_snap(x, y, would_dock)
            return
        if would_dock:
            # 磁吸：关球（若开）+ 贴右。先关球再贴右 —— 关球后 _dock_panel
            # 的 ball_mode guard 失效，能真正贴右。
            # v0.166：磁吸 → 绑定侧栏与主窗同一层级（AddOwnedForm）。
            # v0.167：set_float_ball(False) 与 _dock_panel 都会派发 panel.moved
            # → _on_panel_moved。整体包 _dock_panel_semaphore=True 防同一帧
            # 内的 snap 自递归（settle → snap → 反向 moved → settle ...）。
            self._dock_panel_semaphore = True
            try:
                try:
                    if pool is not None and pool.ball_mode:
                        self.api.set_float_ball(False)
                except Exception:
                    _logger.debug("snap: set_float_ball(False) failed", exc_info=True)
                self._dock_panel(force=True)
                self._panel_docked = True
                self._sync_panel_owner()
            finally:
                # worker 后续派发的 moved 视为新事件，下次 moved 进入 drag-settle
                # 路径即可（pending 缓存最新意图、settle 自然收敛），不会无限递归。
                self._dock_panel_semaphore = False
        else:
            # 浮动：开球（若关）+ 球跟随侧栏。先定球位置再开球，避免开球
            # 内部 _relayout 把侧栏先拽到旧球位再跳回来。
            # v0.166：浮动 → 解耦侧栏与主窗层级（RemoveOwnedForm，独立顶层窗）。
            # v0.167：set_float_ball(True) 会触发 _relayout → move → moved 事件
            # → _on_panel_moved。整体包 _dock_panel_semaphore=True 屏蔽同一帧
            # 内 snap 自递归。
            self._panel_docked = False
            self._sync_panel_owner()
            self._dock_panel_semaphore = True
            try:
                try:
                    if pool is not None:
                        if not pool.ball_mode:
                            pool.set_float_ball_pos_from_panel(int(x), int(y))
                            self.api.set_float_ball(True)
                        else:
                            pool.set_float_ball_pos_from_panel(int(x), int(y))
                except Exception:
                    _logger.debug("snap: float branch failed", exc_info=True)
            finally:
                self._dock_panel_semaphore = False

    def _on_main_minimized(self) -> None:
        """主窗最小化时把侧栏跟着藏。还原时再 dock 回来。"""
        # v0.165：球模式 —— 侧栏从球位置展开（独立于主窗），主窗最小化
        # 不藏侧栏/球（球 on_top 置顶独立）。
        try:
            if getattr(self, "_panel_pool", None) and self._panel_pool.ball_mode:
                return
        except Exception:
            _logger.debug("main minimize ball_mode guard failed", exc_info=True)
        try:
            self.panel_window.hide()
        except Exception:
            _logger.debug("panel hide on main minimize failed", exc_info=True)

    def _on_main_restored(self) -> None:
        """主窗还原时按当前是否开了 panel 决定显眼 + 重新 dock。

        v0.136：尊重浮动状态 —— 仅 dock 状态下还原才重新贴右；用户已把
        面板拖离磁吸区（浮动）则只在原位置重新显示，不拽回磁吸位。
        v0.165：球模式 —— 不重新 dock，侧栏保持从球位置展开。
        """
        # v0.165：球模式跳过（见 _on_main_minimized）。
        try:
            if getattr(self, "_panel_pool", None) and self._panel_pool.ball_mode:
                return
        except Exception:
            _logger.debug("main restored ball_mode guard failed", exc_info=True)
        try:
            if getattr(self, "settings", None) and getattr(
                self.settings, "relay_gui_live_panel", False
            ):
                # v0.176：有请求才弹 —— 还原时仅当存在活跃请求才重新显示；
                # 无请求（空闲收起态）还原保持隐藏，等下一个请求。
                pool = getattr(self, "_panel_pool", None)
                has_req = bool(pool is not None and pool._rids)
                if self._panel_docked:
                    self._dock_panel(force=True)
                if has_req:
                    self.panel_window.show()
        except Exception:
            _logger.debug("panel restore on main restored failed", exc_info=True)

    def _sync_panel_owner(self) -> None:
        """按当前模式同步侧栏与主窗的 WinForms owned 层级（幂等）。

        v0.95：把侧栏登记为主窗 owned form —— 主窗被遮挡时侧栏跟着退后、
        主窗回顶时侧栏跟着回（"一个 app 多窗一体"）。当时侧栏恒 dock 贴右，
        所以无条件 AddOwnedForm。

        v0.166：浮动（球）模式要求侧栏与主窗**解耦层级** —— 侧栏从球左上角
        展开，是桌面上的独立窗（依赖 ball_layer 的 WS_EX_TOPMOST 球），不再
        跟随主窗遮挡/最小化/回顶（主窗最小化不藏侧栏，见 _on_main_minimized
        ball_mode guard）。仅磁吸 dock 态才需要 AddOwnedForm 绑定同一层级。

        本方法幂等：只在目标态与实际态不一致时才改。native 变更必须回 UI
        线程（BeginInvoke 异步，绝不 Invoke 阻塞 —— 历史 RecreateHandle 死锁
        教训），且 AddOwnedForm/RemoveOwnedForm 需在 UI 线程调用。

        docked=True → 绑定（AddOwnedForm）；docked=False → 解耦（RemoveOwnedForm）。
        """
        docked = bool(getattr(self, "_panel_docked", True))
        # v0.170：侧栏层级 = owned 绑定 + 置顶跟随（球与侧栏恒同层级）。
        # 置顶同步放在 bound 早退**之前** —— bound 状态未变也要重算置顶。
        self._sync_panel_topmost()
        if getattr(self, "_panel_owner_bound", None) == docked:
            return
        main_native = getattr(self.window, "native", None)
        panel_native = getattr(self.panel_window, "native", None)
        if main_native is None or panel_native is None:
            _logger.debug("panel owner sync skipped: native not ready")
            return
        try:
            from System.Windows.Forms import MethodInvoker
            if docked:
                main_native.BeginInvoke(MethodInvoker(
                    lambda: main_native.AddOwnedForm(panel_native)))
                _logger.info("panel owner bind scheduled (dock mode)")
            else:
                main_native.BeginInvoke(MethodInvoker(
                    lambda: main_native.RemoveOwnedForm(panel_native)))
                _logger.info("panel owner decouple scheduled (float mode)")
            self._panel_owner_bound = docked
        except Exception:
            _logger.debug("panel owner sync failed", exc_info=True)

    def _sync_panel_topmost(self) -> None:
        """v0.170：侧栏与悬浮球同层级 —— 跟随「悬浮球置顶」开关。

        悬浮球恒 WS_EX_TOPMOST（v0.165 起），而侧栏此前从没置顶：浮动(球)
        模式下侧栏解耦主窗（RemoveOwnedForm）后是普通桌面窗，会被其它窗口
        盖住，而球永远在最上面 —— 两窗层级不一致（用户反馈「侧栏始终要
        和悬浮球在同一层级」）。修复：侧栏置顶态恒等于 relay_gui_float_ball_topmost
        （开关 ON → 一起置顶；OFF → 一起降级），保证球与侧栏恒同层级。

        实现：直接用 WinForms 属性 ``panel_native.TopMost = True/False`` ——
        与悬浮球（ball_layer 的 ``f.TopMost = True``）同一机制，本机已验证；
        Form.TopMost setter 内部走 SetWindowPos(HWND_TOPMOST/NOTOPMOST)，
        **不触发 RecreateHandle**（会重建句柄的是 ShowInTaskbar，见
        _hide_panel_taskbar 的死锁教训）。属性比 ctypes SetWindowPos 更稳：
        WinForms 会把 TopMost 状态缓存到 form 对象，跨 show/hide 持久保持
        （ctypes 直接改样式不会更新该缓存，后续 WinForms 操作可能把置顶
        又刷掉）。

        注意：**不能用 ``int(panel_native.Handle)``** —— pythonnet 3.x 的
        IntPtr 不支持 int()（抛 TypeError），要拿句柄必须 ToInt64()（见
        ball_layer._handle_int）。本方法不直接碰句柄，靠属性，天然规避。

        仅浮动(球)模式生效：磁吸(dock)模式下没有球，侧栏是主窗 owned 窗体、
        应跟随主窗层级（AddOwnedForm），不能被「悬浮球置顶」拔成置顶（否则
        dock 态侧栏会飘在所有窗口上面，是 v0.169 没有的行为回归）。用
        ``not self._panel_docked`` 判浮动 —— 与 set_float_ball 里
        ``_panel_docked = not enabled`` 口径一致。

        BeginInvoke 异步派发到 UI 线程（绝不 Invoke 阻塞，防死锁）。
        """
        try:
            panel_native = getattr(self.panel_window, "native", None)
            if panel_native is None:
                return
            # 仅浮动(球)模式 + 开关 ON 才置顶；磁吸(dock)模式恒不置顶。
            float_mode = not bool(getattr(self, "_panel_docked", True))
            topmost = float_mode and bool(getattr(
                self.settings, "relay_gui_float_ball_topmost", True))
            from System.Windows.Forms import MethodInvoker

            def _apply() -> None:
                try:
                    panel_native.TopMost = bool(topmost)
                    _logger.debug("panel topmost sync -> %s", topmost)
                except Exception:
                    _logger.debug("panel topmost apply failed", exc_info=True)

            panel_native.BeginInvoke(MethodInvoker(_apply))
            _logger.debug("panel topmost sync scheduled -> %s", topmost)
        except Exception:
            _logger.debug("panel topmost sync failed", exc_info=True)

    def _hide_panel_taskbar(self) -> None:
        """v0.163：让侧栏不占独立任务栏位。

        不能用 ``panel_native.ShowInTaskbar = False`` 直接设：该 setter 在
        窗口 handle 已创建（BrowserForm 构造即建）且已显示时触发 WinForms
        RecreateHandle（重建句柄），需要回主 UI 线程执行。而
        ``_on_panel_loaded`` 跑在 WebView2 的 execute 线程，UI 线程此刻正卡
        在 create_window 的 ``browser.Show()``（WebView2 初始化），消息泵
        尚未启动 → RecreateHandle 的跨线程 SendMessage 无人泵 → execute
        线程永久阻塞 → GUI 假死（py-spy 实测 Thread-11 卡在
        ``ShowInTaskbar = False`` 那行）。

        正确做法：BeginInvoke 异步投递到 UI 线程（不阻塞 execute 线程，
        UI 线程消息泵启动后自然会执行）；回调内用 Win32 SetWindowLongPtrW
        直接改扩展样式（去 WS_EX_APPWINDOW、加 WS_EX_TOOLWINDOW），不触发
        句柄重建，无闪烁。纯 ctypes，不依赖 pythonnet 的窗口属性。
        """
        try:
            panel_native = getattr(self.panel_window, "native", None)
            if panel_native is None:
                return
            from System.Windows.Forms import MethodInvoker

            def _remove() -> None:
                try:
                    import ctypes
                    from ctypes import wintypes as _wt
                    hwnd = int(panel_native.Handle)
                    GWL_EXSTYLE = -20
                    WS_EX_APPWINDOW = 0x00040000
                    WS_EX_TOOLWINDOW = 0x00000080
                    u = ctypes.windll.user32
                    u.GetWindowLongPtrW.restype = _wt.LPARAM
                    u.GetWindowLongPtrW.argtypes = [_wt.HWND, ctypes.c_int]
                    u.SetWindowLongPtrW.argtypes = [_wt.HWND, ctypes.c_int, _wt.LPARAM]
                    ex = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                    ex = (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
                    u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)
                    _logger.debug("panel taskbar slot removed (WS_EX_TOOLWINDOW)")
                except Exception:
                    _logger.debug("panel taskbar style remove failed", exc_info=True)

            panel_native.BeginInvoke(MethodInvoker(_remove))
            _logger.debug("panel taskbar removal scheduled via BeginInvoke")
        except Exception:
            _logger.debug("panel taskbar hide failed", exc_info=True)

    def _on_panel_loaded(self) -> None:
        """侧栏页面加载完成 —— 推主题 + 是否可见取决于开关状态。"""
        # v0.95：页面加载时 native 两窗都已建好，此时建立 owned 关系最稳
        # （早于 webview.start() 的 create_window 阶段没有 native 可操作）。
        # v0.166：改为按模式同步 —— 磁吸 dock 态绑定、浮动(球)态解耦。
        self._panel_owner_bound = None
        self._sync_panel_owner()
        # v0.163：侧栏不占独立任务栏位 —— AddOwnedForm 只保证跟随主窗层级，
        # 任务栏按钮仍会单独出现，需显式移除。实现细节见 _hide_panel_taskbar。
        self._hide_panel_taskbar()
        # v0.111：侧栏窗口任务栏图标 —— 复用已缓存的 HICON（_on_loaded 里
        # 可能还没生成 panel 的 native，这里补上；幂等）。
        self._apply_window_icons()
        # v0.184：容器即球（pool.start 已建 ElectronBallWindow）。此处按悬浮球
        # 开关初始化容器模式：ON → 球模式（容器=球帽锚点，可展开成球帽+侧栏）；
        # OFF → 磁吸模式（下方 _should_show_on_startup 强显 dock 贴右）。
        try:
            pool = getattr(self, "_panel_pool", None)
            ball_en = bool(getattr(self.settings, "relay_gui_float_ball", False))
            _logger.info("panel_loaded fired ball_en=%s pool=%s", ball_en, pool is not None)
            if pool is not None:
                pool.set_float_ball(ball_en)
        except Exception:
            _logger.debug("container float-ball mode init failed", exc_info=True)
        try:
            self.panel_window.evaluate_js(
                f"setTheme({json.dumps(self.theme_name)})"
            )
        except Exception:
            _logger.debug("panel setTheme push failed", exc_info=True)
        # v0.113c：侧栏加载时同步一次「无边框」状态（开关持久化在后端）。
        try:
            frameless = bool(getattr(self.settings, "relay_gui_panel_frameless", False))
            self.panel_window.evaluate_js(f"setPanelFrameless({json.dumps(frameless)})")
        except Exception:
            _logger.debug("panel frameless push failed", exc_info=True)
        # v0.130：侧栏加载时推送「自动延展」开关 + 按屏幕工作区余宽算最大列数
        # 上限（JS 布局用它决定开几列，兜底 ≤6）。页面可能先于本函数触发
        # JS init，所以这里 push 让 JS 布局收敛到真实上限。
        try:
            pool = getattr(self, "_panel_pool", None)
            if pool is not None:
                auto_extend = bool(getattr(
                    self.settings, "relay_gui_live_panel_auto_extend", True))
                max_cols = 6
                try:
                    import webview as _wv
                    sw = int(getattr(_wv.screen, "width", 0) or 0)
                    if sw > 0:
                        avail = max(400, sw - (self.window.x + self.window.width) - 4)
                        max_cols = max(1, avail // 400)
                except Exception:
                    max_cols = 6
                pool.set_max_cols(max_cols)
                pool.set_auto_extend(auto_extend)
                self.panel_window.evaluate_js(
                    f"setMaxCols({int(max_cols)}); setAutoExtend({str(auto_extend).lower()});"
                )
        except Exception:
            _logger.debug("panel auto_extend/max_cols init push failed", exc_info=True)
        # 启动时如果开关为开，立即 dock + 显示（_on_loaded 的尾巴有副本）。
        # v0.107：guard 扩展成 live_panel 或 always_one —— always_one 始终
        # 开启时启动也要 dock+show（否则 _show_always_one 在 webview.start
        # 之前因几何未知已 return，这里必须补上）。同时把池状态补成 visible，
        # 免得 _relayout / watchdog 看到 always_one_visible=False 而乱动它。
        # v0.166：gate 收紧为 _should_show_on_startup —— 主开关 ON + 磁吸 +
        # always_one 才强显；原 `live_panel OR always_one` 漏主开关/悬浮球模式。
        try:
            pool = getattr(self, "_panel_pool", None)
            if getattr(self, "settings", None) and pool is not None \
                    and pool._should_show_on_startup():
                # v0.97：启动自动显示 → 强制 dock 贴右
                self._dock_panel(force=True)
                self.panel_window.show()
                pool.always_one_visible = True
        except Exception:
            _logger.debug("panel auto-show failed", exc_info=True)
        # 启动 SSE 订阅线程。线程本身幂等：重复调不会起第二份。线程
        # 不论开关是否打开都跑 —— 关的时候 evaluate_js 静默丢帧（侧栏
        # DOM 仍在，只是 hide() 了），下次开开关没有数据缺口。
        try:
            self._start_live_panel_stream()
        except Exception:
            _logger.debug("panel SSE thread start failed", exc_info=True)

    def _hide_panel_and_sync_off(self) -> None:
        """侧栏"关闭"路径的共享逻辑：隐藏 + 同步开关 + 推主窗 JS 同步。

        两条入口共用：
          * 原生 _on_panel_closing（pywebview 仍能触发时的兜底；frameless
            后正常不再触发，但留着不浪费）
          * Api.panel_close（HTML X 按钮 → JS → 此函数；frameless 主要路径）
        """
        try:
            self.panel_window.hide()
        except Exception:
            _logger.debug("panel hide failed", exc_info=True)
        # 同步两侧：开关置为关、设置页 checkbox 取消、顶栏图标 aria-pressed=false
        try:
            from .config import update_env_var
            update_env_var("RELAY_GUI_LIVE_PANEL", "0")
            if getattr(self, "settings", None):
                self.settings.relay_gui_live_panel = False
        except Exception:
            _logger.debug("panel close → env sync failed", exc_info=True)
        try:
            self.window.evaluate_js("syncSidePanelToggle(false)")
        except Exception:
            _logger.debug("panel close → JS sync failed", exc_info=True)

    def _on_panel_closing(self) -> bool:
        """侧栏原生 closing 事件兜底（frameless 模式下通常不会触发）。

        保留是因为：frameless 仍可能在某些边缘场景里触发 OS 关闭窗口
        事件（如外部 close 调用），不希望侧栏真的被销毁。

        之所以不销毁：让 settings 切换开关立刻可见，少一个 WebView2 重
        启延迟；click X 相当于"关开关"的快捷操作。

        v0.94: 真正退出（托盘/管道 quit）时放行销毁并跳过 JS 同步——
        FormClosing 期间的 evaluate_js 会死锁 UI 线程（WebView2 结果不
        再分发，UI 线程回不到消息泵，发出 Invoke 的线程永久等待）。

        注意：pywebview 的 closing 事件把 **任意 handler 返回 False** 都
        当作"取消关闭"（``should_cancel = closing.set()`` 检查
        ``False``）。放行必须返回 ``None``——返回 ``False`` 会把窗口保
        留下来，FormClosed 永不触发，``Application.Exit()`` 永远不被
        pywebview 调用，消息循环挂死。
        """
        if self._shutting_down:
            return None
        self._hide_panel_and_sync_off()
        # 返回 True 取消销毁（pywebview 的 closing 事件 cancel 语义）
        return True

    # ------------------------------------------------------------------
    # v0.89 SSE 订阅线程
    #
    # 设计：
    #   * 单一 daemon 线程常驻（_sse_thread / _sse_stop_event），启动即
    #     起；侧栏开关/隐藏不影响订阅 —— 反正中继在广播，丢掉的话下次
    #     重新 show 侧栏会看到过时数据。让线程一直跑，恢复 push 时无
    #     缝。
    #   * 解析 SSE `data: <json>\n\n` 帧；冒号开头（`: keepalive`）跳过。
    #   * evaluate_js 走 Control.Invoke 跨线程安全，调用包在 try/except
    #     里静默吞异常（窗口销毁 / 页面重载 / 控件句柄失效等情况下会
    #     抛，丢一帧不影响后续事件）。
    #   * 连接失败：指数退避，1s/2s/4s/8s 顶 10s；连接成功并保持心跳
    #     期间退避重置。中继重启后自动续上。
    #   * 整个线程生命周期不阻塞 webview.start() / 任何按钮回调。
    # ------------------------------------------------------------------

    def _start_live_panel_stream(self) -> None:
        """启动一次 SSE 订阅线程（幂等：已在跑就 no-op）。"""
        if self._sse_thread is not None and self._sse_thread.is_alive():
            _logger.info("_start_live_panel_stream skipped (already running)")
            return
        self._sse_stop_event.clear()
        self._sse_thread = threading.Thread(
            target=self._sse_loop,
            name="relay-gui-live-panel-sse",
            daemon=True,
        )
        self._sse_thread.start()
        _logger.info("_start_live_panel_stream thread started")

    def _sse_loop(self) -> None:
        """SSE 订阅主循环：连 8088 的 /live/stream，逐行解 SSE 帧并推给侧栏。"""
        backoff = 1.0
        backoff_max = 10.0
        while not self._sse_stop_event.is_set():
            try:
                self._sse_connect_once()
                # 正常结束（服务端关连接）—— 重置退避，立即重连。
                backoff = 1.0
            except Exception as exc:
                _logger.debug("sse loop error (will retry in %.1fs): %r", backoff, exc)
                # 退避等一段时间，期间也响应 stop_event 提前退出。
                if self._sse_stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, backoff_max)
        _logger.info("sse loop exited")

    def _sse_connect_once(self) -> None:
        """建立一次 SSE 连接并消费到断开。返回时正常关闭，异常向上冒。"""
        url = f"{self.settings.base_url}/live/stream"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            },
        )
        # Readinto-based streaming response. ``urllib`` 不会自动按行切
        # chunk，得自己 buffer；用 io.BufferedReader 包一层走 readline
        # 即可，按 b'\n' 切。读不到心跳（15s）视作连接健康；连接断了
        # 抛异常，交给 _sse_loop 退避重连。
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            stream = resp
            buf = b""
            while not self._sse_stop_event.is_set():
                chunk = stream.read(4096)
                if not chunk:
                    # EOF —— 服务端关连接 / 中继重启过渡期。
                    raise ConnectionError("SSE stream closed (EOF)")
                buf += chunk
                # 按 \n\n 切事件（一个事件以空行结尾）。一次可能含多
                # 个事件，逐个解析。
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    self._sse_handle_frame(raw)

    def _sse_handle_frame(self, frame: bytes) -> None:
        """单条 SSE 事件（不含末尾空行）。冒号开头行（注释/keepalive）跳过。"""
        data_lines: list[str] = []
        for line in frame.splitlines():
            if not line or line.startswith(b":"):
                # 空行 / 注释 / keepalive —— 跳过；SSE 规范允许任意数
                # 量的 ": ..." 行。
                continue
            # data: 后的内容才是 payload。多个 data: 行按规范要拼起来
            # （我们只有一行 JSON 场景，先只取第一个非空 data:）。
            if line.startswith(b"data:"):
                payload = line[5:].lstrip(b" \t")
                if payload:
                    data_lines.append(payload.decode("utf-8", errors="replace"))
        if not data_lines:
            return
        text = "\n".join(data_lines)
        try:
            ev = json.loads(text)
        except (ValueError, TypeError):
            _logger.debug("sse non-JSON frame: %r", text[:200])
            return
        self._sse_push_to_panel(ev)

    def _sse_push_to_panel(self, ev: dict) -> None:
        """把一个事件推给侧栏窗口的 JS（window.relayLiveEvent）。"""
        try:
            payload = json.dumps(ev, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        try:
            self.panel_window.evaluate_js(f"relayLiveEvent({payload})")
        except Exception:
            # 侧栏未就绪 / 已销毁 / 页面正在重载 —— 静默丢一帧，线程
            # 持续，连接不断；下一帧如果侧栏恢复了会继续推。
            pass

    # ------------------------------------------------------------------
    # Theme toggle (re-used by JS bridge)
    # ------------------------------------------------------------------

    def _push_theme_to_panels(self, name: str) -> None:
        """把主题广播到实时栏窗口（v0.109 起主栏 + 网格区同窗口）。

        主题色（CSS 变量）靠 document 的 [data-theme] 属性切换，网格区
        复用同一 document，一次 setTheme 整窗生效。静默吞异常（窗口 DOM
        未就绪时 evaluate_js 会抛）。"""
        try:
            self.panel_window.evaluate_js(f"setTheme({json.dumps(name)})")
        except Exception:
            pass

    def _push_frameless_to_panels(self, enabled: bool) -> None:
        """把侧栏「无边框」状态广播到实时栏窗口（v0.113c）。

        切 body.panel-no-frame class —— live_panel.css 据此去卡片玻璃容器。
        静默吞异常（侧栏窗口未建/隐藏时 evaluate_js 会抛）。"""
        try:
            self.panel_window.evaluate_js(f"setPanelFrameless({json.dumps(bool(enabled))})")
        except Exception:
            pass

    def _on_toggle_theme(self) -> str:
        """Advance to the next theme preset and persist it.

        Cycle: light → day → dark → light. Returns the new name so JS
        can update ``data-theme`` immediately."""
        idx = _THEMES.index(self.theme_name) if self.theme_name in _THEMES else 0
        new_name = _THEMES[(idx + 1) % len(_THEMES)]
        self.theme_name = new_name
        self.settings.relay_gui_theme = new_name
        # Persist via the existing settings helper so the choice
        # survives a restart.
        try:
            from relay.config import update_env_var

            update_env_var("RELAY_GUI_THEME", new_name)
        except Exception:
            # Persistence is best-effort — fall back to in-memory only
            # if the .env write fails (e.g. read-only filesystem).
            pass
        # v0.89：侧栏窗口同步主题；v0.108：网格窗口一起广播。
        self._push_theme_to_panels(new_name)
        return new_name

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the server (if autostart), launch the webview, and
        block until the window closes.

        The polling thread is started in ``_on_loaded`` (i.e. after
        webview.start() spins the event loop and the page DOM is ready)
        so we don't pay for SQLite polling when the user closes the
        window before paint.
        """
        _logger.info("App.run entering autostart=%s diag=%s", self.autostart, self.diag)
        _boot_tick("App.run entering")
        if self.autostart and not _probe_relay(self.settings.base_url):
            # Guard on the probe: with a relay already on the port, a
            # blind ``start()`` spawns a child that dies immediately on
            # ``WinError 10048`` and writes a confusing log file every
            # time the GUI opens.
            #
            # The probe-then-start pair is racy in principle (another
            # process could bind between the two), but the window is
            # microseconds and the worst case is one extra doomed
            # child. ``Api.start_server`` enforces the same no-op-when-
            # busy defense for the JS-initiated path; if you ever need
            # harder guarantees here, route through that instead of
            # calling ``self.server.start()`` directly.
            _logger.info("App.run autostart enabled, port free → spawning server")
            try:
                self.server.start()
                _logger.info("App.run server.start OK")
                _boot_tick("server.start done")
            except Exception as e:
                # Don't kill the UI just because the server failed to
                # start — the user can retry from the topbar.
                _logger.warning("App.run server.start failed: %r", e)
        else:
            _logger.info("App.run autostart skipped (relay already answering /healthz)")
            _boot_tick("autostart skipped (relay already up)")
        _logger.info("App.run calling webview.start(debug=%s)", self.diag)
        _boot_tick("calling webview.start")
        # ``debug=True`` routes JS console output (incl. console.error)
        # through pywebview's logger so a frozen bridge shows up as
        # missing "tick()" lines or an unhandled exception in the JS
        # boot path.
        # v0.141：private_mode=False + storage_path —— 否则 WebView2 用临时
        # profile，localStorage 每次重启清空，「关闭顶部调试栏」等 prefs 设置
        # 关了重启又自己开（用户实测反馈）。固定目录后数据跨重启保留。
        webview.start(
            debug=self.diag,
            private_mode=False,
            storage_path=str(_WEBVIEW_DATA_DIR),
        )
        _boot_tick("webview.start returned — window closed")
        # webview.start() returns once the WinForms message loop exits,
        # which only happens when the window is *actually* destroyed
        # (i.e. after _on_closing ran with quit-requested = True). When
        # the user merely hid to tray, we never get here until the
        # tray's "退出中继" path calls window.destroy().
        if self._tray is not None:
            self._tray.stop()
        _logger.info("App.run webview.start returned — window closed")


def _gui_pid_path() -> Path:
    """Path of the file where the sole GUI records its PID.

    Lives next to the relay diagnostics under ``.relay-logs/`` so a
    second launch can find and force-replace a hung first instance
    (whose command pipe is dead and which won't release the mutex).
    """
    from relay.config import _project_root

    return _project_root() / ".relay-logs" / "gui.pid"


def _write_gui_pid() -> None:
    path = _gui_pid_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        _logger.warning("could not write %s: %s", path, exc_info=True)


def _clear_gui_pid() -> None:
    try:
        _gui_pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _pid_is_live_gui(pid: int) -> bool:
    """True iff ``pid`` is a running process that looks like this GUI."""
    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
            False,
            pid,
        )
        if not handle:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(len(buf))
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return False
            exe = buf.value.lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False
    # Only ever force-kill something that is actually a Python GUI host.
    return "python" in exe


def _scan_stale_gui_pids() -> list[int]:
    """Best-effort PIDs whose command line looks like this GUI.

    Covers instances started before the PID-file feature existed (no
    ``gui.pid`` on disk), or whose PID file went missing. Matches the
    ``relay-gui`` console script and ``python main.py`` launches; the
    relay child uses ``-m uvicorn relay.main:app`` so it never matches.
    """
    try:
        script = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "($_.CommandLine -match 'relay-gui|main\\.py') -and "
            f"($_.ProcessId -ne {os.getpid()}) }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    pids = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _force_replace_stale_gui() -> bool:
    """taskkill the recorded previous GUI when it won't answer the pipe.

    Called after ``_acquire_exclusive`` timed out: the first instance's
    command pipe is dead (hung listener) so the ``quit`` command never
    got through and it never released the mutex. The relay is a detached
    child process and survives this — the new GUI simply sees it as
    ``外部``. Returns True once the stale process is confirmed gone.
    """
    candidates: list[int] = []
    try:
        pid = int(_gui_pid_path().read_text(encoding="utf-8").strip())
        if pid > 0:
            candidates.append(pid)
    except Exception:
        pass
    if not candidates:
        candidates = _scan_stale_gui_pids()
    for pid in candidates:
        if pid == os.getpid() or pid <= 0 or not _pid_is_live_gui(pid):
            continue
        _logger.warning("force-stopping stale relay-gui pid=%d (pipe unresponsive)", pid)
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=10.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            continue
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _pid_is_live_gui(pid):
                return True
            time.sleep(0.25)
    return False


def _bootstrap_portable_assets() -> None:
    """v0.195：便携附件包（frozen onefile + 旁置 relay_assets/）自动发现。

    PyInstaller onefile 解开后 ``__file__`` 指向临时解包目录，Electron 运行时
    （electron.exe，约 244MB）与 ball/panel 渲染资产无法冻结进 exe，必须落在
    exe 同目录的 ``relay_assets/``（由 build_release.py 组装）。启动时检测到
    relay_assets 就把 RELAY_ELECTRON_EXE / RELAY_ELECTRON_APP_ROOT 指过去，
    使悬浮球 + 实时面板在便携形态下可用；源码态（非 frozen）无旁置目录，
    完全不影响原有逻辑。
    """
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except Exception:
        return
    assets = exe_dir / "relay_assets"
    if not assets.is_dir():
        return
    elec_exe = assets / "electron" / "electron.exe"
    if elec_exe.is_file():
        os.environ["RELAY_ELECTRON_EXE"] = str(elec_exe)
        _logger.info("portable: RELAY_ELECTRON_EXE=%s", elec_exe)
    app_root = assets / "electron_app"
    if app_root.is_dir():
        os.environ["RELAY_ELECTRON_APP_ROOT"] = str(app_root)
        _logger.info("portable: RELAY_ELECTRON_APP_ROOT=%s", app_root)


def run() -> None:
    """Console-script entry point: ``relay-gui``."""
    import argparse

    parser = argparse.ArgumentParser(prog="relay-gui", add_help=False)
    parser.add_argument(
        "--diag", action="store_true",
        help="Write bridge/poll diagnostics to relay-gui.log (default off)",
    )
    parser.add_argument(
        "--no-diag", dest="diag", action="store_false",
        help="Disable diagnostic logging",
    )
    parser.add_argument("-h", "--help", action="store_true")
    args, _unknown = parser.parse_known_args()
    if args.help:
        print("usage: relay-gui [--diag|--no-diag] [--tray-only]")
        return

    # v0.107: 每次启动都在 stderr 打进度（PyCharm 控制台可见，无需 --diag），
    # 用来定位启动变慢 —— 尤其单实例接管 / force-replace 的耗时。
    _boot_tick("run() entered")

    # v0.195: 便携附件包（frozen onefile + 旁置 relay_assets/）自动发现。
    # PyInstaller onefile 解开后 __file__ 指向临时解包目录，web 资产可从
    # _MEIPASS/relay/web 取；但 Electron 运行时（electron.exe 244MB）和
    # ball/panel 渲染资产无法冻结进 exe，必须落在 exe 旁的 relay_assets/。
    # 启动时检测 exe 同目录若有 relay_assets，就指向它（设 RELAY_ELECTRON_EXE
    # / RELAY_ELECTRON_APP_ROOT），使悬浮球 + 实时面板在便携形态下可用。
    # 源码态（非 frozen）无旁置目录，完全不影响原有逻辑。
    if getattr(sys, "frozen", False):
        _bootstrap_portable_assets()

    # Diagnostic logging is opt-in (--diag). A frozen bridge in a
    # PyCharm-launched ``python main.py`` run can still be diagnosed by
    # re-launching with --diag.
    if args.diag:
        _configure_diag_logging()

    settings = Settings.load()
    _boot_tick("Settings.load done")
    _logger.info("run() boot pid=%d diag=%s python=%s pywebview=%s",
                 os.getpid(), args.diag, sys.version.split()[0],
                 getattr(webview, "__version__", "?"))
    # Clear any tray icons orphaned by a previously-killed relay GUI (reboot,
    # taskkill /F, crash) before we become the sole instance — see
    # ``relay.tray.refresh_taskbar``. Best-effort; never blocks startup.
    try:
        from relay.tray import refresh_taskbar

        refresh_taskbar()
    except Exception:
        _logger.warning("refresh_taskbar failed", exc_info=True)
    _boot_tick("refresh_taskbar done")
    # Honour the existing single-instance guard so a second ``relay-gui``
    # invocation takes over instead of fighting for the port. The guard
    # is just an OS lock — works for the webview window too.
    #
    # v0.11.4: when the mutex is already owned, we no longer just bring
    # the first window forward and exit — we tell the first instance to
    # quit (via the named pipe), wait for it to release the mutex, then
    # take over as the sole GUI. Spawning a second App would otherwise
    # race the first one on the relay port and produce a doomed 10048
    # child.
    try:
        from relay.single_instance import (
            CommandListener,
            SingleInstanceGuard,
            _acquire_exclusive,
            send_command,
        )
    except ImportError:
        App(settings, diag=args.diag).run()
        return

    guard = SingleInstanceGuard("relay-gui")
    _boot_tick(f"SingleInstanceGuard acquired={guard.acquired}")
    if not guard.acquired:
        # v0.93: keep re-asking the existing instance to quit while we
        # wait for the mutex — its pipe may not exist yet (e.g. a racing
        # second launch that hasn't finished booting), so a single
        # send_command fired at startup could miss it and force a needless
        # kill of a healthy instance.
        _logger.warning(
            "second relay-gui launch: mutex taken — asking the existing "
            "instance to quit, taking over when it exits"
        )
        _boot_tick("mutex taken — starting takeover")
        try:
            guard = _acquire_exclusive(
                "relay-gui", timeout=10.0, notify=lambda: send_command("quit")
            )
            _boot_tick("takeover: old instance released mutex (clean quit)")
        except RuntimeError:
            # The first instance's pipe is dead (hung listener) so the
            # ``quit`` command never got through and it never released
            # the mutex. Fall back to force-replacing it: taskkill the PID
            # we recorded at startup (the relay child is detached and
            # survives), then take the mutex. Without this, a broken first
            # GUI could never be replaced by relaunching the app.
            _boot_tick("takeover: first _acquire_exclusive timed out — force-replacing")
            if _force_replace_stale_gui():
                guard = _acquire_exclusive("relay-gui", timeout=10.0)
                _boot_tick("takeover: force-replace done, re-acquired")
            else:
                _logger.error(
                    "relay-gui: existing instance did not exit within 10s "
                    "and could not be force-stopped; kill it manually"
                )
                return
        _logger.info("previous relay-gui released the mutex; taking over as sole instance")

    app = App(settings, diag=args.diag)
    _boot_tick("App.__init__ done")

    def _on_pipe_command(line: str) -> None:
        # Callback runs on the pipe listener thread; window.show() /
        # window.destroy() from a non-main thread is already the
        # established pattern (tray callbacks call these the same way,
        # and pywebview marshals via Invoke to the UI thread).
        if line == "show":
            app._show_from_tray()
        elif line == "quit":
            # v0.11.4: a duplicate launch asked us to step aside — it's
            # about to take over as the sole GUI. Quit for real (the
            # window is destroyed, the relay child is stopped, and the
            # mutex is released on process exit).
            _logger.info("relay-gui pipe command 'quit' — stepping aside for the new instance")
            app._quit_from_tray()
        else:
            _logger.info("relay-gui pipe command %r ignored", line)

    listener = CommandListener(on_command=_on_pipe_command)
    try:
        with guard:
            # Record our PID so a hung/pipe-dead sibling can be force-
            # replaced by a relaunch (v0.93).
            _write_gui_pid()
            app.run()
    finally:
        listener.stop()
        _clear_gui_pid()


def _configure_diag_logging() -> None:
    """Append bridge/poll traces to ``relay-gui.log``. Idempotent.

    INFO captures boot / window-loaded / warmup / delayed-probe / stuck-
    bridge alerts; DEBUG captures per-tick ``get_status`` / ``get_snapshot``
    lines (≈4 lines/s — useful for diagnosing wedged polls but noisy in
    the default log). The file is appended, never truncated, so previous
    sessions survive a restart.
    """
    _DIAG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(_DIAG_LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    # Avoid stacking handlers when ``run()`` is called twice in-process.
    _logger.handlers.clear()
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


if __name__ == "__main__":  # pragma: no cover
    run()
