"""Provider quota / usage clients (v0.11.17, #11).

Ported from the opencode-tui-usage plugin
(``C:\\Users\\weizheng\\Desktop\\opencode-tui-usage-master\\src\\quota``),
which ships TypeScript adapters for DeepSeek (balance), MiniMax
(coding-plan remains) and OpenCode-Go (opencode.ai internal RPC).

The relay has no session provider-ID signal like the plugin does, so the
active provider is picked by name (matching an upstream's name or
platform). Credentials come from the relay upstream config (api_key) or
environment variables mirroring the plugin's fallbacks:

* ``OPENCODE_GO_AUTH_COOKIE`` + ``OPENCODE_GO_WORKSPACE_ID``
  (OpenCode-Go)
* ``MINIMAX_API_KEY``                    (MiniMax CN/IO)
* ``DEEPSEEK_API_KEY``                   (DeepSeek)

All fetches are best-effort and never raise: a quota query failure
returns ``None`` for that provider so the GUI can show "—".
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

OPENCODE_GO_SERVICE_ID = "c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd"


def _fetch(url: str, headers: dict[str, str], timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fmt_seconds(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{round(sec / 60)}m"
    if sec < 86400:
        return f"{round(sec / 3600)}h"
    return f"{round(sec / 86400)}d"


def query_opencode_go(cookie: str, workspace_id: str) -> dict[str, Any] | None:
    """OpenCode-Go (opencode.ai) plan usage — cookie-authenticated RPC.

    Response is plain text; usage is embedded as JS-ish literals and
    extracted with regex (mirrors the plugin exactly).
    """
    if not cookie or not workspace_id:
        return None
    args = json.dumps({
        "t": {"t": 9, "i": 0, "l": 1, "a": [{"t": 1, "s": workspace_id}], "o": 0},
        "f": 31,
        "m": [],
    })
    url = (
        f"https://opencode.ai/_server?id={OPENCODE_GO_SERVICE_ID}"
        f"&args={urllib.parse.quote(args)}"
    )
    headers = {
        "accept": "*/*",
        "cookie": cookie,
        "x-server-id": OPENCODE_GO_SERVICE_ID,
        "x-server-instance": "server-fn:3",
    }
    try:
        text = _fetch(url, headers)
    except Exception:
        return None
    pattern = re.compile(
        r'(\w+Usage):\$R\[(\d+)\]=\{status:"([^"]+)",resetInSec:(\d+),usagePercent:(\d+)\}'
    )
    found: dict[str, dict[str, Any]] = {}
    for m in pattern.finditer(text):
        dim = m.group(1)  # rollingUsage / weeklyUsage / monthlyUsage
        status = m.group(3)
        reset = int(m.group(4))
        percent = int(m.group(5))
        found[dim] = {
            "usage": percent,
            "reset": _fmt_seconds(reset),
            "status": status,
        }
    if not found.get("rollingUsage") or not found.get("weeklyUsage"):
        return None
    monthly = found.get("monthlyUsage")
    return {
        "rolling": found["rollingUsage"],
        "weekly": found["weeklyUsage"],
        "monthly": None if not monthly or monthly["status"] == "unlimited" else monthly,
    }


def query_minimax(api_key: str, base_url: str = "https://api.minimax.io") -> dict[str, Any] | None:
    """MiniMax coding-plan quota (CN / IO share the same endpoint)."""
    if not api_key:
        return None
    url = f"{base_url}/v1/api/openplatform/coding_plan/remains"
    headers = {
        "authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        data = json.loads(_fetch(url, headers))
    except Exception:
        return None
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code") != 0:
        return None
    models = [m for m in (data.get("model_remains") or []) if (m.get("model_name") or "").startswith("MiniMax-M")]
    if not models:
        return None
    now_ms = __import__("time").time() * 1000
    rolling_limit = sum(m.get("current_interval_total_count") or 0 for m in models)
    rolling_avail = sum(m.get("current_interval_usage_count") or 0 for m in models)
    weekly_limit = sum(m.get("current_weekly_total_count") or 0 for m in models)
    weekly_avail = sum(m.get("current_weekly_usage_count") or 0 for m in models)
    end_ms = max((m.get("end_time") or 0) for m in models)
    weekly_end_ms = max((m.get("weekly_end_time") or 0) for m in models)

    def _pct(used: int, limit: int) -> int:
        return round(used / limit * 100) if limit > 0 else 0

    return {
        "rolling": {
            "usage": _pct(max(0, rolling_limit - rolling_avail), rolling_limit),
            "reset": _fmt_seconds(max(0, round((end_ms - now_ms) / 1000))),
        },
        "weekly": {
            "usage": _pct(max(0, weekly_limit - weekly_avail), weekly_limit),
            "reset": _fmt_seconds(max(0, round((weekly_end_ms - now_ms) / 1000))),
        },
        "monthly": None,
    }


def query_deepseek(api_key: str) -> dict[str, Any] | None:
    """DeepSeek balance (pay-as-you-go)."""
    if not api_key:
        return None
    url = "https://api.deepseek.com/user/balance"
    headers = {"Accept": "application/json", "authorization": f"Bearer {api_key}"}
    try:
        data = json.loads(_fetch(url, headers))
    except Exception:
        return None
    if not data.get("is_available") or not data.get("balance_infos"):
        return None
    info = data["balance_infos"][0]
    total = float(info.get("total_balance") or 0)
    return {
        "balance": {
            "currency": info.get("currency", "?"),
            "total": total,
            "text": f"{total:.2f} {info.get('currency', '')}".strip(),
        }
    }


def query_provider(name: str, api_key: str | None = None) -> dict[str, Any] | None:
    """Dispatch a quota query by provider name (best-effort)."""
    name = (name or "").lower()
    if "opencode" in name:
        cookie = os.environ.get("OPENCODE_GO_AUTH_COOKIE")
        wid = os.environ.get("OPENCODE_GO_WORKSPACE_ID")
        if not cookie and api_key and "cookie=" in api_key:
            # allow api_key = "cookie=...;workspace=..."
            cookie = api_key.split("cookie=")[-1].split(";")[0]
        if not wid and api_key and "workspace=" in api_key:
            wid = api_key.split("workspace=")[-1]
        return query_opencode_go(cookie or "", wid or "")
    if "minimax" in name:
        return query_minimax(api_key or os.environ.get("MINIMAX_API_KEY") or "")
    if "deepseek" in name:
        return query_deepseek(api_key or os.environ.get("DEEPSEEK_API_KEY") or "")
    return None
