"""`python -m relay` entry point.

Dispatches to one of the subcommands. Default is the GUI; pass
`serve` to start the HTTP relay without the GUI, or `stats` for a
one-shot stats print.

    python -m relay                 # GUI
    python -m relay serve           # HTTP relay, no GUI
    python -m relay gui             # GUI explicitly
    python -m relay stats           # one-shot stats table
    python -m relay --help          # this help

    python -m relay serve --profile=headless        # HTTP relay, no GUI
    python -m relay gui --profile=headless          # no-op (headless has no GUI)
    python -m relay --profile=passthrough-only      # passthrough debug only

`--profile` (Phase 5, v0.121+) selects which subsystems load:
  web            default; GUI + HTTP relay + passthrough
  headless       HTTP relay + passthrough, no GUI
  passthrough-only  passthrough + db + client pool only
"""

from __future__ import annotations

import os
import sys


_USAGE = """\
usage: python -m relay [serve|gui|stats] [options]

subcommands:
  serve    start the HTTP relay (uvicorn) — no GUI
  gui      launch the desktop GUI (default)
  stats    print a one-shot token-usage table

`serve` and `gui` accept these options:
  --listen HOST:PORT     override the listen address
  --profile NAME         web | headless | passthrough-only
`stats` accepts the relay-stats flags (--window, --since, --db).
"""


def _pop_profile(args: list[str]) -> str | None:
    """Pull ``--profile=NAME`` (or ``--profile NAME``) out of ``args``.

    Sets ``RELAY_PROFILE`` env (so the child / server sees it), returns
    the raw name, or None when absent.
    """
    name: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--profile":
            if i + 1 < len(args):
                name = args[i + 1]
                i += 2
                continue
            print("--profile requires a value", file=sys.stderr)
            return None
        if tok.startswith("--profile="):
            name = tok.split("=", 1)[1]
            i += 1
            continue
        rest.append(tok)
        i += 1
    if name is not None:
        os.environ["RELAY_PROFILE"] = name
    args[:] = rest
    return name


def main() -> int:
    # v0.177：frozen exe 首启时 seed 发布默认 .env / upstreams.json。
    # 仅 frozen 路径生效（内部判 sys.frozen），源码/wheel 启动零开销。
    # 必须在 Settings.load() 之前完成，否则 _resolve_config_path 找不到
    # %LOCALAPPDATA%\Relay\upstreams.json，upstreams.json 退化到无。
    from .config import _seed_default_config
    _seed_default_config()

    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(_USAGE, end="")
        return 0
    if args and args[0] in ("serve", "gui", "stats"):
        cmd, rest = args[0], args[1:]
    else:
        # No args, or unknown first arg → launch the GUI.
        cmd, rest = "gui", args

    # Phase 5: --profile applies to serve/gui; stats ignores it.
    profile_name = _pop_profile(rest) if cmd != "stats" else None

    if cmd == "serve":
        from .main import run
        _apply_serve_args(rest)
        run()
        return 0
    if cmd == "gui":
        from .profile import profile_from_name, resolve_profile
        from .gui import run

        profile = resolve_profile()
        if profile_name is not None:
            # CLI --profile wins over env; re-resolve with the explicit name.
            profile = profile_from_name(profile_name)
        if not profile.gui:
            print(f"relay: profile={profile.name!r} has no GUI; nothing to launch",
                  file=sys.stderr)
            return 0
        sys.argv = ["relay-gui", *rest]
        run()
        return 0
    if cmd == "stats":
        from .cli import print_stats
        sys.argv = ["relay-stats", *rest]
        print_stats()
        return 0
    print(f"unknown subcommand {cmd!r}; expected one of: serve, gui, stats", file=sys.stderr)
    return 2


def _apply_serve_args(rest: list[str]) -> None:
    """Best-effort: forward --listen from the CLI to env so Settings picks it up."""
    if not rest:
        return
    it = iter(rest)
    for tok in it:
        if tok == "--listen":
            try:
                os.environ["RELAY_LISTEN"] = next(it)
            except StopIteration:
                print("serve: --listen requires a value", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
