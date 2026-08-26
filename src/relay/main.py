"""FastAPI app factory and entry point.

Wires up the database, config, and routers. Run via:

    uvicorn relay.main:app                       # module:app
    python -m relay.main                         # if __main__.py dispatches here
    python -m relay                              # same, via package __main__
    python main.py serve                         # project-root main.py CLI
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

# Allow this file to be run directly (e.g. `python src/relay/main.py`) by
# making sure the `relay` package is importable and `__package__` is set.
if __name__ == "__main__" and __package__ in (None, ""):
    import sys
    from pathlib import Path

    _SRC = Path(__file__).resolve().parent.parent  # <workspace>/src
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    __package__ = "relay"

from .config import get_settings
from .db import Database
from .passthrough import PassthroughDatabase
from .routers import stats


log = logging.getLogger("relay")

# v0.12：模块级配置 root logger 到 INFO。之前只在 run()（`relay serve`
# 路径）里 basicConfig，`python -m uvicorn relay.main:app` 启动时 root 停在
# WARNING，relay.proxy 的 INFO 日志（DISPATCH/CROSS-WIRE 等）全被过滤，
# 导致 tail 日志看不到分发去向。放模块级让两种启动方式都能看到 INFO。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # First run after upgrading from the .env-only setup: drop a pre-filled
    # upstreams.json next to the DB so the user has something to edit. No-op
    # once the file exists — we never overwrite hand-edited config.
    from .upstreams_file import seed_from_settings

    seed_from_settings(settings)
    db = Database(settings.relay_db)
    await db.init()
    app.state.db = db
    app.state.settings = settings
    # 完全透传模式独立 DB —— 与现有 relay.db 物理隔离。
    pt_db = PassthroughDatabase(settings.relay_passthrough_db)
    await pt_db.init()
    app.state.pt_db = pt_db

    # ---- V0.117+ 服务容器（Phase 1 + Phase 2）----
    # 建一个进程级 RelayContext；Phase 2 起注入 9 大 services：
    # HttpClientPool / AlertLog / ReasoningCache / LiveBus / UrlBuilder /
    # AuthHeader / InflightStore / SettingsService / SettingsMutator。
    # attach_legacy_plugin_platform 把旧 plugin.py 的模块级注册表转发到 ctx.bus
    # （兼容期双向投递）。
    from .core import RelayContext, bootstrap_builtin_services, attach_legacy_plugin_platform
    ctx = RelayContext(app=app, settings=settings, db=db)
    bootstrap_builtin_services(ctx)
    attach_legacy_plugin_platform(ctx)
    app.state.ctx = ctx  # 横向模块 / 插件作者可直接 ``app.state.ctx.svc(...)``

    # v0.98 插件平台：加载 plugins/ 目录的插件（apply(ctx) 里注册
    # wire/parser/钩子/事件）。单个插件失败只跳过，不影响中继启动。
    from .plugin import load_plugins

    loaded = load_plugins(app)
    if loaded:
        log.info("plugins loaded: %s", ", ".join(loaded))
    log.info(
        "relay ready: listen=%s db=%s upstreams_file=%s upstreams=%s",
        settings.relay_listen,
        settings.relay_db,
        settings.relay_upstreams_file,
        {p: [c.name for c in settings.upstreams_for(p)] for p in ("anthropic", "openai")},
    )
    # Phase 4：quota monitor 收进 QuotaMonitorService（ctx.svc("quota_monitor")），
    # 由 bootstrap 注册；start() 在 lifespan 里显式起。task cancel 由
    # ctx.dispose() 反序释放时统一处理。
    from .services import QuotaMonitorService
    from .quota_monitor import start as start_quota_monitor

    monitor = None
    qms = ctx.svc("quota_monitor") if "quota_monitor" in ctx else None
    if isinstance(qms, QuotaMonitorService):
        qms.start()
        monitor = qms.get_task()
    else:
        # 兼容：service 未注册时回退旧路径（test / 早期 lifespan）。
        monitor = start_quota_monitor(app)
    # Phase 2：sweeper 由 InflightStore.apply() 自动起；ctx.dispose() 自动 cancel。
    try:
        yield
    finally:
        # Phase 2：sweeper tasks 由 ctx.svc("inflight") 自己管；
        # dispose 反序释放时会 cancel。
        # Passthrough middleware maintains its own client pool.
        from .passthrough import _close_clients
        with suppress(Exception):
            await _close_clients()
        if monitor is not None:
            monitor.cancel()
            # Give the task a tick to unwind so its cancellation isn't
            # reported as "never retrieved" noise at interpreter exit.
            with suppress(asyncio.CancelledError):
                await monitor
        # ---- V0.117+ ctx.dispose() 反序释放 ----
        # Phase 2：释放 9 大 services（httpx client / asyncio.Task / Lock / bus
        # 订阅 / alert 缓冲等），并 trigger attach_legacy_plugin_platform 注册的
        # bridge services（settings/db/app）释放。
        with suppress(Exception):
            ctx.dispose()
        log.info("relay shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Token Consumption Relay",
        version="0.99",
        lifespan=lifespan,
    )

    # 完全透传 ASGI 中间件 —— 必须最先加，挂到所有路由前面。
    # OFF 时是零开销透传；ON 时拦截所有 HTTP 请求。
    # 注：fastapi_app=app 把 app 实例传给中间件，让它读 app.state.settings
    # / app.state.pt_db 做动态判断。中间件 __init__ 的 fastapi_app 形参
    # 由 Starlette 的 add_middleware 通过 kwargs 传入。
    from .passthrough import PassthroughMiddleware
    app.add_middleware(PassthroughMiddleware, fastapi_app=app)

    # Phase 5/6：passthrough-only profile —— 不挂业务 routers，只保留
    # 透传中间件 + 一个健康检查路由。用于专项调试（转发到透传目标，
    # 不碰中继业务 / quota / GUI）。
    from .profile import resolve_profile

    profile = resolve_profile()
    if not profile.web:
        @app.get("/healthz")
        async def _healthz():
            return {"ok": True, "profile": profile.name}

        @app.get("/")
        async def _passthrough_only_root():
            return {"service": "relay", "profile": profile.name,
                    "note": "passthrough-only: 无业务路由"}

        return app

    # Platform routers (catch-all). Each is mounted at /<platform>; the
    # proxy reads the platform's active config from app.state.settings
    # at request time so upstreams can be swapped without a restart.
    from .routers import anthropic, openai, api as api_router
    from .routers import models as models_router
    from .routers import root as root_router

    app.include_router(anthropic.router, prefix="/anthropic")
    app.include_router(openai.router, prefix="/openai")
    app.include_router(api_router.router)
    app.include_router(models_router.router)
    app.include_router(stats.router)
    # v0.X 鲁棒性增强：根路径 catchall —— 客户端 SDK 配 base_url=中继根地址
    # 时自动补后缀的场景。按 path 末段 + body 嗅探推断 wire→platform 分发。
    # 必须最后注册（FastAPI 按注册顺序匹配，具体路由优先于 /{path:path} 通配）。
    app.include_router(root_router.router)

    return app


app = create_app()


def run() -> None:
    """Entry point for the `relay` console script."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    uvicorn.run(
        "relay.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    run()
