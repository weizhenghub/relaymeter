"""CLI utilities — one-shot pretty print of stats.

Usage:
    relay-stats                                  # total (all time)
    relay-stats --window 5h                      # last 5 hours
    relay-stats --window 24h                     # last 24 hours
    relay-stats --window week                    # last 7 days
    relay-stats --since 1735606400               # explicit unix-ts lower bound
    relay-stats --db /path/to/db                 # explicit DB path
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from .db import Database
from .routers.stats import WINDOWS


def _resolve_since(window: str | None, since: float | None) -> float | None:
    if since is not None:
        return since
    if window is None or window == "total":
        return None
    secs = WINDOWS.get(window)
    if secs is None:
        return None
    return time.time() - secs


def print_stats() -> None:
    ap = argparse.ArgumentParser(
        prog="relay-stats",
        description="Print per-platform aggregate token usage.",
    )
    ap.add_argument("--db", default="./relay.db", help="Path to relay.db")
    ap.add_argument(
        "--window",
        choices=list(WINDOWS.keys()),
        default="total",
        help="Time window (default: total)",
    )
    ap.add_argument(
        "--since",
        type=float,
        default=None,
        help="Filter by ts >= N (unix seconds); overrides --window",
    )
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(
            f"relay.db not found at {args.db}. Start the relay first to create it.",
            file=sys.stderr,
        )
        sys.exit(1)

    db = Database(args.db)
    eff_since = _resolve_since(args.window, args.since)
    rows = asyncio.run(db.aggregate(platform=None, since=eff_since))

    console = Console(force_terminal=False, no_color=False, force_interactive=False)
    # box.ASCII uses +/-| characters that survive GBK encoding on Windows
    # when conda re-encodes the captured stdout.
    table = Table(
        title=f"Token consumption — window={args.window}",
        header_style="bold",
        show_footer=False,
        box=box.ASCII,
    )
    table.add_column("platform", style="bold", width=10)
    table.add_column("reqs", justify="right", width=6)
    table.add_column("input", justify="right", style="cyan", width=10)
    table.add_column("output", justify="right", style="magenta", width=10)
    table.add_column("cache_rd", justify="right", style="yellow", width=9)
    table.add_column("cache_wr", justify="right", style="green", width=9)
    table.add_column("errs", justify="right", style="red", width=6)

    grand = {
        "requests": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "errors": 0,
    }
    for platform in ("anthropic", "openai"):
        row = rows.get(platform)
        if row is None:
            table.add_row(platform, "0", "0", "0", "0", "0", "0")
            continue
        for k in grand:
            grand[k] += getattr(row, k)
        table.add_row(
            platform,
            f"{row.requests:,}",
            f"{row.input_tokens:,}",
            f"{row.output_tokens:,}",
            f"{row.cache_read_input_tokens:,}",
            f"{row.cache_creation_input_tokens:,}",
            str(row.errors) if row.errors else "-",
        )

    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{grand['requests']:,}[/bold]",
        f"[bold cyan]{grand['input_tokens']:,}[/bold cyan]",
        f"[bold magenta]{grand['output_tokens']:,}[/bold magenta]",
        f"[bold yellow]{grand['cache_read_input_tokens']:,}[/bold yellow]",
        f"[bold green]{grand['cache_creation_input_tokens']:,}[/bold green]",
        f"[bold red]{grand['errors']}[/bold red]" if grand["errors"] else "-",
    )

    if eff_since is not None:
        when = datetime.fromtimestamp(eff_since, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        table.caption = f"rows with ts >= {when}"
    console.print(table)


if __name__ == "__main__":
    print_stats()
