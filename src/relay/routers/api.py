"""Runtime control API: list / select upstream configs.

Endpoints (all under /api):

  GET  /api/upstreams                          → all platforms, all configs
  GET  /api/upstreams/{platform}               → one platform's configs
  POST /api/upstreams/{platform}/select        → switch the active config
                                                 body: {"name": "<name>",
                                                        "model": "..."?}
  GET  /api/upstreams/{platform}/active        → currently active config
  PUT  /api/upstreams/{platform}/{name}/quota  → set quota_5h /
                                                 model_multipliers /
                                                 allowed_models (v0.20)
  PUT  /api/upstreams/{platform}/{name}/model         → set model override
                                                          body: {"model": "..." | null}
  PUT  /api/upstreams/{platform}/{name}/default-model → set auto 兜底 model (v0.74)
                                                          body: {"default_model": "..." | null}

The list/active responses never include the raw `api_key` — only a
fingerprint (first 6 chars + "…") so the UI can show "which key" without
leaking the secret. Selecting persists the choice to `upstreams.json`
(or `.env` when no such file exists) so a restart preserves it.

v0.65: model-name override. The sidebar dropdown lists every
(upstream, model) pair as its own option, so ``select`` receives the
chosen model alongside the upstream name and persists both at once.
"""

from __future__ import annotations

import logging
import json
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..config import (
    PLATFORMS,
    PlatformConfig,
    Settings,
    apply_quota_edit,
    reload_settings,
    set_upstream_default_model,
    set_upstream_model,
    update_env_var,
)


log = logging.getLogger("relay.api")

router = APIRouter(prefix="/api", tags=["control"])


def _public(cfg: PlatformConfig) -> dict[str, Any]:
    """Strip the secret, return a JSON-safe dict."""
    fp: str | None = None
    if cfg.api_key:
        # First 6 chars, then "…" — enough to distinguish, not enough to leak.
        fp = (cfg.api_key[:6] + "…") if len(cfg.api_key) > 6 else "…"
    return {
        "name": cfg.name,
        "url": cfg.url,
        "has_api_key": cfg.api_key is not None,
        "api_key_fingerprint": fp,
        "auth_header": cfg.auth_header,
        "note": cfg.note,
        "quota_5h": cfg.quota_5h,
        "model_multipliers": dict(cfg.model_multipliers),
        "allowed_models": list(cfg.allowed_models),
        "model": cfg.model,
        "default_model": cfg.default_model,
        "billing_unit": cfg.billing_unit,
        "token_fields": dict(cfg.token_fields) if cfg.token_fields else {},
        # v0.12 协议声明（wire 未声明时给推导结果，GUI 直接展示）
        "wire": cfg.wire,
        "endpoint": cfg.endpoint,
        "auth_style": cfg.auth_style,
    }


def _public_with_active(cfgs: list[PlatformConfig], active: str) -> list[dict[str, Any]]:
    out = []
    for c in cfgs:
        item = _public(c)
        item["active"] = c.name == active
        out.append(item)
    return out


def _ensure_known(platform: str) -> None:
    if platform not in PLATFORMS:
        raise HTTPException(404, f"unknown platform {platform!r}")


@router.get("/upstreams")
async def list_all(request: Request) -> dict[str, Any]:
    """v0.12 单池：返回扁平 {"active": str, "upstreams": [...]}。"""
    settings = request.app.state.settings
    upstreams = settings.upstreams_for()
    active = settings.active_for()
    return {
        "active": active,
        "upstreams": _public_with_active(upstreams, active),
    }


@router.get("/alerts")
async def alerts(since: int = 0) -> dict[str, Any]:
    """v0.12 分发告警（混合态 / 未知 key）。GUI 轮询拉取新告警展示。

    ``since`` 是上次收到的最大 id；只返回更新的。进程内环形列表（容量
    50），重启即清空 —— 告警是"当下要修配置"的提示，不需要持久化。
    """
    from ..proxy import take_dispatch_alerts

    return {"alerts": take_dispatch_alerts(since)}


@router.post("/probe_upstream")
async def probe_upstream(request: Request) -> dict[str, Any]:
    """v0.12 新建上游自动探测（docs/wire-dispatch-plan.md §5）。

    body: {"url": str, "api_key": str} → 探测 wire/端点/认证/模型/key
    有效性。best-effort，永不抛 5xx。
    """
    from pydantic import BaseModel

    from ..probe import probe_upstream as _probe

    class _Body(BaseModel):
        url: str = ""
        api_key: str = ""
        model: str = ""  # v0.95+（P2）：真实模型名，避免占位名被严格校验模型名的上游 400 拒

    body = _Body(**(await request.json() if await request.body() else {}))
    if not body.url.strip():
        return {"error": "url required"}
    return await _probe(body.url.strip(), body.api_key, model=body.model or None)


@router.get("/upstreams/{platform}")
async def list_platform(request: Request, platform: str) -> dict[str, Any]:
    _ensure_known(platform)
    settings = request.app.state.settings
    return {
        "platform": platform,
        "active": settings.active_for(platform),
        "upstreams": _public_with_active(settings.upstreams_for(platform), settings.active_for(platform)),
    }


@router.get("/upstreams/{platform}/active")
async def get_active(request: Request, platform: str) -> dict[str, Any]:
    _ensure_known(platform)
    settings = request.app.state.settings
    return _public(settings.active_config(platform))


class SelectBody(BaseModel):
    name: str
    # v0.65: optional model override. When the upstream has multiple
    # allowed_models and no model is yet configured, the frontend must
    # prompt the user to pick one and re-issue with this field.
    model: str | None = None


@router.post("/upstreams/{platform}/select")
async def select_active(request: Request, platform: str, body: SelectBody) -> dict[str, Any]:
    _ensure_known(platform)
    settings = request.app.state.settings
    # v0.197 fix：先把磁盘上游 overlay 到当前 settings，再 set_active。
    #
    # 背景：新建/删除上游走 GUI 桥（config.add_upstream/remove_upstream）写
    # upstreams.json，但中继 uvicorn 子进程的 app.state.settings 是 lifespan
    # 启动时的快照，除非有别的调用刷新它，否则**不知道**磁盘上新加的条目。
    # GUI 桥 create_upstream 里的补救是 POST /api/upstreams/refresh，但那趟
    # 请求可能因 setTimeout 前子进程正在 restart / 或 base_url 与监听址不一致
    # 而静默失败（gui.py 里 ``except: pass`` 吞掉）。于是一旦用户「新建后紧接着
    # 顶栏切换」（作者注释明说这个场景），set_active 找不到新条目 → KeyError →
    # 404「no upstream named ...」→ GUI 弹窗。
    #
    # 这里让 select 端点**自身**在每次切换前从磁盘 reload 一份上游列表，彻底
    # 消除「中继内存 vs 上游文件」的不同步 —— 新建/删除后不需要 restart、
    # 也不需要赌那趟 refresh 是否送达。仅 overlay 上游列表 + active 指针到
    # **当前** settings 对象（apply_to_settings 就地改），不重建整个 Settings，
    # 避免把 passthrough_mode 等运行时状态一起重置。
    try:
        from ..upstreams_file import apply_to_settings
        apply_to_settings(settings, settings.relay_upstreams_file)
    except Exception as exc:
        # overlay 失败不硬拒切换：加载已在 lifespan 里做过一次，这里只是补一份
        # 最新盘，极端情况下（文件被临时占用）退回快照也还能切到旧上游。
        log.warning("select: apply_to_settings failed: %s", exc)
    try:
        cfg = settings.set_active(platform, body.name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    # v0.66: the sidebar dropdown lists every (upstream, model) pair as
    # its own option, so a model always arrives alongside the switch
    # whenever the upstream declares any allowed_models. When model is
    # None the upstream has none declared and the proxy passes the
    # client's model through untouched.
    if body.model is not None:
        ok, msg = set_upstream_model(settings, platform, body.name, body.model)
        if not ok:
            raise HTTPException(400, msg)
        # Re-apply config so the in-memory cfg reflects the new model.
        from ..upstreams_file import apply_to_settings
        apply_to_settings(settings, settings.relay_upstreams_file)
        cfg = next(
            (c for c in settings.upstreams_for(platform) if c.name == body.name),
            cfg,
        )

    # Best-effort persist. Failure here is non-fatal — the in-memory
    # switch already took effect, the user can re-issue after fixing perms.
    try:
        _persist_active(settings, platform, body.name)
    except OSError as exc:
        log.warning("could not persist %s active=%s: %s", platform, body.name, exc)
    return {
        "platform": platform,
        "selected": _public(cfg),
    }


class QuotaBody(BaseModel):
    """Editable v0.20 quota fields. Omitted keys reset to their default —
    a PUT is a full replacement of the quota block, not a patch, so the
    UI can clear a multiplier by simply not sending it."""

    quota_5h: int | None = None
    model_multipliers: dict[str, float] = {}
    allowed_models: list[str] = []
    model: str | None = None
    # v0.66: billing_unit / token_fields — Optional so an API call that
    # doesn't mention them is a true no-op for these fields.
    billing_unit: str | None = None
    token_fields: dict[str, bool] | None = None
    # v0.205: 该上游「支持图片输入」模型名单 —— 与 GUI 桥同一口径
    # （apply_quota_edit：None 不动、[] 清空）。缺了这个字段，外部
    # HTTP PUT 带 vision_models 会被 pydantic 静默剥掉，两条写入口径
    # 不一致。
    vision_models: list[str] | None = None


@router.post("/upstreams/refresh")
async def refresh_upstreams(request: Request) -> dict[str, Any]:
    """GUI 在 add_upstream / remove_upstream / select_active 等写文件动
    作完成后，主动调这个端点让中继进程 reload settings（默认中继
    的 settings 是 fork 时的快照，文件改了它也不知道）。"""
    try:
        request.app.state.settings = reload_settings()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@router.put("/upstreams/{platform}/{name}/quota")
async def update_quota(
    request: Request, platform: str, name: str, body: QuotaBody
) -> dict[str, Any]:
    """Persist quota / multipliers / allow-list for one upstream.

    Shares `apply_quota_edit` with the GUI bridge, so both paths validate
    identically and write the same atomic `upstreams.json`.
    """
    _ensure_known(platform)
    settings = request.app.state.settings
    ok, msg = apply_quota_edit(settings, platform, name, body.model_dump())
    if not ok:
        # 404 when the name doesn't exist, 400 when the payload is bad —
        # the helper folds both into one message, so discriminate here.
        raise HTTPException(404 if "找不到" in msg else 400, msg)
    # Re-apply the file onto the live Settings so the very next request
    # routes with the new quota. Deliberately *not* reload_settings():
    # that rebuilds from the process-wide env and would discard whatever
    # this app instance was configured with.
    from ..upstreams_file import apply_to_settings

    apply_to_settings(settings, settings.relay_upstreams_file)
    cfg = next(
        (c for c in settings.upstreams_for(platform) if c.name == name),
        None,
    )
    return {"platform": platform, "path": msg, "upstream": _public(cfg) if cfg else None}


class ModelBody(BaseModel):
    model: str | None = None


class DefaultModelBody(BaseModel):
    default_model: str | None = None


@router.put("/upstreams/{platform}/{name}/model")
async def update_model(
    request: Request, platform: str, name: str, body: ModelBody
) -> dict[str, Any]:
    """Persist just the model-name override for one upstream (v0.65).

    ``model=None`` clears the override so the relay stops rewriting.
    """
    _ensure_known(platform)
    settings = request.app.state.settings
    ok, msg = set_upstream_model(settings, platform, name, body.model)
    if not ok:
        raise HTTPException(404 if "找不到" in msg else 400, msg)
    from ..upstreams_file import apply_to_settings
    apply_to_settings(settings, settings.relay_upstreams_file)
    cfg = next(
        (c for c in settings.upstreams_for(platform) if c.name == name),
        None,
    )
    return {"platform": platform, "path": msg, "upstream": _public(cfg) if cfg else None}


@router.put("/upstreams/{platform}/{name}/default-model")
async def update_default_model(
    request: Request, platform: str, name: str, body: DefaultModelBody
) -> dict[str, Any]:
    """Persist the v0.74 "auto 兜底" model for one upstream.

    当 ``cfg.model=None`` 且客户端发了 ``model: "auto"``,中继会用这个
    字段的值改写请求体(回退链 ``cfg.model → cfg.default_model →
    cfg.allowed_models[0] → 原样透传``)。``default_model=None`` 清除
    兜底,让中继走到 ``allowed_models[0]`` 这一档或透传。
    """
    _ensure_known(platform)
    settings = request.app.state.settings
    ok, msg = set_upstream_default_model(settings, platform, name, body.default_model)
    if not ok:
        raise HTTPException(404 if "找不到" in msg else 400, msg)
    from ..upstreams_file import apply_to_settings
    apply_to_settings(settings, settings.relay_upstreams_file)
    cfg = next(
        (c for c in settings.upstreams_for(platform) if c.name == name),
        None,
    )
    return {"platform": platform, "path": msg, "upstream": _public(cfg) if cfg else None}


class AutoSwitchBody(BaseModel):
    """v0.11.3 settings-page payload for the autoswitch behaviour."""
    enabled: bool | None = None
    pool: list[str] | None = None
    # v0.12：保存消息原文与回复开关。
    save_messages: bool | None = None


@router.get("/settings")
async def get_runtime_settings(request: Request) -> dict[str, Any]:
    """Live relay-side behaviour settings for the GUI settings page.

    Returns the values the quota monitor actually uses, not a stale copy.
    """
    s = request.app.state.settings
    return {
        "autoswitch_enabled": bool(s.relay_quota_autoswitch),
        "autoswitch_pool": list(getattr(s, "relay_autoswitch_pool", None) or []),
        "autoswitch_at": float(s.relay_quota_switch_at),
        "save_messages": bool(getattr(s, "relay_save_messages", True)),
        "passthrough_mode": bool(getattr(s, "passthrough_mode", False)),
    }


@router.put("/settings/autoswitch")
async def update_autoswitch(
    request: Request, body: AutoSwitchBody
) -> dict[str, Any]:
    """Toggle quota autoswitch and/or the allowed-switch pool (v0.11.3).

    Mutates ``app.state.settings`` in place — the quota monitor re-reads
    its settings every tick, so the change applies without a restart —
    and persists to ``.env`` so a restart keeps it.
    """
    s = request.app.state.settings
    if body.enabled is not None:
        s.relay_quota_autoswitch = bool(body.enabled)
        update_env_var(
            "RELAY_QUOTA_AUTOSWITCH",
            "true" if body.enabled else "false",
        )
    if body.pool is not None:
        s.relay_autoswitch_pool = list(body.pool)
        update_env_var("RELAY_AUTOSWITCH_POOL", json.dumps(list(body.pool)))
    if body.save_messages is not None:
        s.relay_save_messages = bool(body.save_messages)
        update_env_var("RELAY_SAVE_MESSAGES", "1" if body.save_messages else "0")
    return {
        "autoswitch_enabled": bool(s.relay_quota_autoswitch),
        "autoswitch_pool": list(s.relay_autoswitch_pool),
        "autoswitch_at": float(s.relay_quota_switch_at),
        "save_messages": bool(getattr(s, "relay_save_messages", True)),
    }


def _persist_active(settings: Settings, platform: str, name: str) -> None:
    """Record the new active upstream wherever the config actually lives.

    `upstreams.json` is the source of truth when it exists, so writing the
    choice to `.env` instead would be silently undone on the next restart
    (the file overrides the env var). Falls back to `.env` only when there
    is no file / no section for this platform — i.e. the pre-v0.8 setup.
    """
    from ..upstreams_file import set_active

    path = settings.relay_upstreams_file
    if set_active(path, platform, name):
        log.info("persisted %s active=%s to %s", platform, name, path)
        return
    _persist_active_to_env(platform, name)


def _persist_active_to_env(platform: str, name: str) -> None:
    """Write `<PLATFORM>_ACTIVE=<name>` to the .env file used by Settings.

    Other lines are preserved verbatim. We don't quote values — names
    are simple identifiers. Failures bubble up as `False` from
    `update_env_var`; this function returns silently in that case, same
    as the previous implementation did when the file wasn't writable.
    """
    if not update_env_var(f"{platform.upper()}_ACTIVE", name):
        return
    # Re-read settings so the persisted choice is in memory even if the
    # caller had loaded settings before the file write.
    reload_settings()


# ---------------------------------------------------------------------------
# 完全透传模式 API
# ---------------------------------------------------------------------------

class PassthroughModeBody(BaseModel):
    enabled: bool


@router.get("/passthrough/mode")
async def get_passthrough_mode(request: Request) -> dict[str, Any]:
    """当前透传模式开关状态。"""
    s = request.app.state.settings
    return {"passthrough_mode": bool(getattr(s, "passthrough_mode", False))}


@router.put("/passthrough/mode")
async def set_passthrough_mode(
    request: Request, body: PassthroughModeBody,
) -> dict[str, Any]:
    """切换透传模式开关。持久化到 .env（PASSTHROUGH_MODE），同时
    更新内存中的 Settings —— 中间件每次请求读 settings.passthrough_mode，
    所以新值立即生效。"""
    s = request.app.state.settings
    s.passthrough_mode = bool(body.enabled)
    update_env_var(
        "PASSTHROUGH_MODE",
        "true" if body.enabled else "false",
    )
    return {"passthrough_mode": s.passthrough_mode}


# ---------------------------------------------------------------------------
# 存储管理 API（v0.113n）—— 写操作走 relay 进程（GUI 只读、不可写 DB）。
# 读（占用信息）由 GUI 本地算，这里只做破坏性清理。
# ---------------------------------------------------------------------------

class CleanupMessagesBody(BaseModel):
    """days>0 保留近 N 天；days<=0 清空全部。target 指定清理哪个库。"""
    days: int
    target: str = "both"  # "relay" | "passthrough" | "both"


@router.post("/storage/cleanup-messages")
async def cleanup_messages(
    request: Request, body: CleanupMessagesBody,
) -> dict[str, Any]:
    """按时间清理消息记录：relay.db（requests 级联 messages）+ passthrough.db。

    target 让前端按库就地清理（v0.113p）—— 只清 relay / 只清 passthrough /
    两个都清。删除是 relay 进程的职责 —— GUI 进程不写 DB，避免两个进程同时写。
    """
    db = request.app.state.db
    pt_db = request.app.state.pt_db
    if body.days <= 0:
        relay_deleted = await db.delete_all_requests() if body.target in ("relay", "both") else 0
        pt_deleted = await pt_db.delete_all() if body.target in ("passthrough", "both") else 0
    else:
        cutoff = time.time() - body.days * 86400
        relay_deleted = await db.delete_requests_before(cutoff) if body.target in ("relay", "both") else 0
        pt_deleted = await pt_db.delete_before(cutoff) if body.target in ("passthrough", "both") else 0
    return {
        "ok": True,
        "days": body.days,
        "relay_deleted": relay_deleted,
        "passthrough_deleted": pt_deleted,
    }


class VacuumBody(BaseModel):
    """target 指定压缩哪个库（v0.113p）。"""
    target: str = "both"  # "relay" | "passthrough" | "both"


@router.post("/storage/vacuum")
async def vacuum_storage(request: Request, body: VacuumBody) -> dict[str, Any]:
    """对 DB 执行 VACUUM 回收删除后未归还的空间，可按 target 只压一个库。"""
    db = request.app.state.db
    pt_db = request.app.state.pt_db
    if body.target in ("relay", "both"):
        await db.vacuum()
    if body.target in ("passthrough", "both"):
        await pt_db.vacuum()
    return {"ok": True}


class DeleteUpstreamBody(BaseModel):
    """彻底删除某上游的全部请求行（配置移除由 GUI 侧负责）。"""
    upstream: str


@router.post("/storage/delete-upstream")
async def delete_upstream_storage(
    request: Request, body: DeleteUpstreamBody,
) -> dict[str, Any]:
    """按上游名删除 relay 库里该上游的全部请求行（messages 级联）。

    历史上游「彻底删除」的 DB 半场 —— relay 进程持有 Database 写句柄，
    GUI 进程只读不写。纯 DB 清理：配置移除由 GUI 调 remove_upstream_cfg
    完成后，再 POST 到这里剥掉该上游的 DB 行。幂等：名不存在 → 删 0 行。
    """
    if not body.upstream or not body.upstream.strip():
        return {"ok": False, "error": "upstream 不能为空"}
    db = request.app.state.db
    deleted = await db.delete_request_rows_by_upstream(body.upstream.strip())
    return {"ok": True, "upstream": body.upstream.strip(), "deleted": deleted}


class DeleteMessagesRangeBody(BaseModel):
    """清理原文：按 ts 范围删 messages（保留 requests）。"""
    start: float
    end: float


@router.post("/storage/delete-messages-range")
async def delete_messages_range_storage(
    request: Request, body: DeleteMessagesRangeBody,
) -> dict[str, Any]:
    """按 ts 范围删除 messages 行（设置页「清理原文」按天全部清除）。"""
    if body.end <= body.start:
        return {"ok": False, "error": "end 必须大于 start"}
    db = request.app.state.db
    deleted = await db.delete_messages_between(body.start, body.end)
    return {"ok": True, "deleted": deleted}


class DeleteMessageBody(BaseModel):
    """清理原文：删除单条 message（列表右键菜单）。"""
    id: int


@router.post("/storage/delete-message")
async def delete_message_storage(
    request: Request, body: DeleteMessageBody,
) -> dict[str, Any]:
    """删除单条 message 行。"""
    if body.id <= 0:
        return {"ok": False, "error": "id 必须为正数"}
    db = request.app.state.db
    deleted = await db.delete_message_by_id(body.id)
    return {"ok": True, "id": body.id, "deleted": deleted}


class DeleteUpstreamMessagesBody(BaseModel):
    """清理原文：按上游删除 messages（保留 requests 与配置）。"""
    upstream: str


@router.post("/storage/delete-upstream-messages")
async def delete_upstream_messages_storage(
    request: Request, body: DeleteUpstreamMessagesBody,
) -> dict[str, Any]:
    """删除某上游名下所有 message 行（设置页「清理原文」按上游全部清除）。"""
    if not body.upstream or not body.upstream.strip():
        return {"ok": False, "error": "upstream 不能为空"}
    db = request.app.state.db
    deleted = await db.delete_messages_by_upstream(body.upstream.strip())
    return {"ok": True, "upstream": body.upstream.strip(), "deleted": deleted}


@router.post("/storage/clear-logs")
async def clear_logs(request: Request) -> dict[str, Any]:
    """清空 `<project root>/.relay-logs/` 下的常规日志文件。

    只删除该目录内的文件，不做任何递归；realpath 前缀校验防目录穿越。
    """
    from ..config import _project_root

    log_dir = (_project_root() / ".relay-logs").resolve()
    removed = 0
    if log_dir.is_dir():
        for entry in log_dir.iterdir():
            if not entry.is_file():
                continue
            # 只删 .relay-logs 正下方的常规文件，绝不碰子目录/符号链接。
            if entry.resolve().parent != log_dir:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                log.warning("clear-logs: 无法删除 %s", entry)
    return {"ok": True, "removed": removed}


@router.get("/passthrough/upstreams")
async def list_passthrough_upstreams(request: Request) -> dict[str, Any]:
    """列出自动发现的透传上游。api_key_alias 在返回前 mask。

    注：同时返回 ``api_key_alias`` 完整值（用于前端重命名接口的 fingerprint
    匹配）。这是 local GUI + 同进程通信场景，不出公网；正式联网时把
    这行删掉即可。
    """
    pt_db = request.app.state.pt_db
    rows = await pt_db.list_upstreams()
    items: list[dict[str, Any]] = []
    for r in rows:
        key = r.get("api_key_alias", "") or ""
        if len(key) > 8:
            masked = key[:4] + "..." + key[-4:]
        elif key:
            masked = "***"
        else:
            masked = ""
        # 计算自动生成名（display_name 未设时）
        host = r["url"]
        for prefix in ("https://", "http://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        tail = (key or "")[-4:] if len(key) >= 4 else key
        auto_name = f"{host}|{r['model_field_name']}|{tail}"
        items.append({
            "url": r["url"],
            "api_key_alias": key,
            "api_key_masked": masked,
            "model_field_name": r["model_field_name"],
            "display_name": r.get("display_name"),
            "upstream_name": auto_name,
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "request_count": r["request_count"],
        })
    return {"upstreams": items}


@router.put("/passthrough/upstreams/{url:path}/rename")
async def rename_passthrough_upstream_by_fingerprint(
    request: Request,
    url: str,
    model_field_name: str = Query(...),
    api_key_alias: str = Query(...),
    display_name: str = Query(""),
) -> dict[str, Any]:
    """按 fingerprint (url + model_field_name + api_key_alias) 重命名。

    url 字段在 path 里（``{url:path}`` 兜住所有字符），另两个 fingerprint
    字段和 display_name 通过 query 传入，避免 url 含 @@ / 斜杠时的路径解析
    歧义。``display_name=""`` 表示清除用户命名，回到自动生成名。
    """
    pt_db = request.app.state.pt_db
    ok = await pt_db.rename_upstream(
        url=url,
        api_key_alias=api_key_alias,
        model_field_name=model_field_name,
        display_name=display_name,
    )
    if not ok:
        return {"ok": False, "error": "未找到匹配 (url, model_field_name, api_key_alias) 的上游"}
    return {"ok": True}


@router.get("/passthrough/stats")
async def passthrough_stats(
    request: Request,
    dim: str = Query("upstream"),
    range: str = Query("30d"),
    top: int = Query(10, ge=0, le=100),
) -> dict[str, Any]:
    """透传模式的统计聚合。

    返回结构与 /api/stats/aggregate 同步：``{range, dim, since, top,
    total, rows, mode}``，前端渲染逻辑可复用。
    """
    import time
    valid_ranges = {"1d": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0}
    if range not in valid_ranges:
        return {"error": f"invalid range {range!r}",
                "valid": list(valid_ranges.keys())}
    if dim not in {"upstream", "model", "model_field"}:
        return {"error": f"invalid dim {dim!r}",
                "valid": ["upstream", "model", "model_field"]}
    since = time.time() - valid_ranges[range]
    pt_db = request.app.state.pt_db
    rows = await pt_db.aggregate_by_dim(dim=dim, since=since)
    total = {
        "requests": sum(r["requests"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "cache_read_input_tokens": sum(r["cache_read_input_tokens"] for r in rows),
        "cache_creation_input_tokens": sum(r["cache_creation_input_tokens"] for r in rows),
        "errors": sum(r["errors"] for r in rows),
    }
    total["total_tokens"] = (
        total["input_tokens"] + total["output_tokens"]
        + total["cache_read_input_tokens"] + total["cache_creation_input_tokens"]
    )
    sorted_rows = sorted(rows, key=lambda r: r["total_tokens"], reverse=True)
    if top > 0:
        sorted_rows = sorted_rows[:top]
    return {
        "range": range,
        "dim": dim,
        "since": since,
        "top": top,
        "total": total,
        "rows": sorted_rows,
        "mode": "passthrough",
    }


@router.get("/passthrough/overview")
async def passthrough_overview(
    request: Request,
    range: str = Query("30d"),
) -> dict[str, Any]:
    """透传模式总览数据（一张接口喂总览页全部卡片）。

    供"仅查看透传消耗"档使用；总览页无需逐卡发请求。返回结构与前端
    snapshot 的卡片字段对齐（by_upstream / by_model / by_hour / recent /
    totals），前端可和 relay 快照合并出"全部"档。
    """
    import time
    valid_ranges = {"1d": 86400.0, "7d": 7 * 86400.0, "30d": 30 * 86400.0}
    if range not in valid_ranges:
        return {"error": f"invalid range {range!r}",
                "valid": list(valid_ranges.keys())}
    since = time.time() - valid_ranges[range]
    pt_db = request.app.state.pt_db

    def _totals(rows: list[dict]) -> dict[str, int]:
        t = {
            "requests": sum(r["requests"] for r in rows),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "cache_read_input_tokens": sum(r["cache_read_input_tokens"] for r in rows),
            "cache_creation_input_tokens": sum(r["cache_creation_input_tokens"] for r in rows),
            "errors": sum(r["errors"] for r in rows),
        }
        t["total_tokens"] = (
            t["input_tokens"] + t["output_tokens"]
            + t["cache_read_input_tokens"] + t["cache_creation_input_tokens"]
        )
        return t

    by_upstream = await pt_db.aggregate_by_dim(dim="upstream", since=since)
    by_model = await pt_db.aggregate_by_dim(dim="model", since=since)
    by_hour = await pt_db.fetch_by_hour(since=86400.0)
    recent = await pt_db.fetch_recent(limit=20)
    # 透传无平台概念 —— 按 URL host 归组作为"平台分布"的替代维度。
    host_map: dict[str, list[dict]] = {}
    for r in by_upstream:
        url = r["key"].split("|")[0]
        host = url.split("//")[-1].split("/")[0] if "//" in url else url
        host_map.setdefault(host, []).append(r)
    by_platform: list[dict] = []
    for host, rows in host_map.items():
        agg = _totals(rows)
        agg["key"] = host
        agg["requests"] = sum(r["requests"] for r in rows)
        agg["errors"] = sum(r["errors"] for r in rows)
        by_platform.append(agg)
    by_platform.sort(key=lambda r: r["total_tokens"], reverse=True)

    return {
        "range": range,
        "since": since,
        "totals": _totals(by_upstream),
        "by_upstream": sorted(by_upstream, key=lambda r: r["total_tokens"], reverse=True),
        "by_model": sorted(by_model, key=lambda r: r["total_tokens"], reverse=True),
        "by_platform": by_platform,
        "by_hour": by_hour,
        "recent": recent,
        "mode": "passthrough",
    }


