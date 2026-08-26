"""OpenCode models.dev 嗅探端点（v0.11.20）。

OpenCode 客户端用 ``OPENCODE_MODELS_URL`` 覆盖 models.dev 源，拉
``{source}/api.json``。本路由让中继充当该源：

- 代理真实的 models.dev（官方所有 provider/模型元数据照常保留）；
- 把 relay 配置里每个 anthropic 上游的 ``thinking_options`` 翻译成
  opencode 的 ``reasoning_options``，注入一个 ``relay`` provider，
  opencode TUI 就能像官方模型一样显示思考挡位。

所以官方模型 + relay 挡位并存，不会互相挤掉。

对内（嗅探）：告诉 opencode 每个模型支持哪些挡位。
对外（发送）：真正的请求转发时，proxy 再按目标上游能力重写 thinking
（见 proxy._rewrite_thinking_for_upstream）——两者完全独立。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from fastapi import APIRouter, Request

logger = logging.getLogger("relay.models")

router = APIRouter(tags=["models"])

# opencode 默认的 models 源（不是 models.dev —— 那是给 github 用的，
# 本机直连被墙；opencode 自己的域名在国内可达）。代理它而不是替换它，
# 官方模型元数据才不丢。
_MODELS_DEV_URL = "https://models.opencode.ai/api.json"
_CACHE_TTL = 3600.0  # 官方数据基本不变，缓存 1 小时
_cache: dict[str, Any] | None = None
_cache_at = 0.0

# opencode api.json 的 Provider 结构。当前只暴露 opencode 自身提供的
# 模型（thinking_options 非空的上游），其它上游不声明即不出现，opencode
# 里看不到思考挡位 —— 与"只有 go/zen 需要思考"的原则一致。
MODEL_LIMITS = {"context": 200000, "input": 200000, "output": 65536}
# v0.188 支持图片的模型 —— OpenCode 客户端读这个 api.json 的
# modalities.input 判断模型能否收图，不含 "image" 就在本地把图拦下、请求
# 根本不会发到中继。原实现给 relay provider 统一声明 image，但那只覆盖
# 声明了 thinking_options 的 cldaude-opus-5 等；多模态模型（如
# deepseek-v4-flash-vision-exp）走非 anthropic-thinking 上游时根本不在
# 列表里，OpenCode 收不到它的 modalities → 默认不能收图。
# 现在按 upstreams.json 顶层 vision_models（裸模型名多选）决定 modalities：
# 命中的模型标 image-capable（input=text/image/pdf），其余保持纯文本。两个
# 常量按 support_image 分支选用，不再统一拷贝。
_IMAGE_MODALITIES = {"input": ["text", "image", "pdf"], "output": ["text"]}
_TEXT_MODALITIES = {"input": ["text"], "output": ["text"]}


def _fetch_models_dev() -> dict[str, Any]:
    """拉取官方 models.opencode.ai 数据，带进程内缓存。失败时回退到上次缓存。"""
    global _cache, _cache_at
    now = time.time()
    if _cache is not None and now - _cache_at < _CACHE_TTL:
        return _cache
    try:
        req = urllib.request.Request(
            _MODELS_DEV_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) relay/0.11"},
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict):
            _cache = data
            _cache_at = now
            return data
        logger.warning("models.opencode.ai 返回非 dict，跳过缓存: %s", type(data).__name__)
    except Exception as exc:
        logger.warning("拉取 models.opencode.ai 失败，回退缓存: %s", exc)
    return _cache or {}



# v0.12 全量 effort 挡位（含 OpenAI 的 minimal/xhigh/max，不止 low/medium/high）。
# 各厂商真实取值差异见 docs/thinking-level.md；中继原样透传这些字符串，
# 由目标上游决定是否认（不认则 400 —— 由 operators 靠 thinking_options 如实声明规避）。
_EFFORT_VALUES: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")


def _reasoning_options(thinking_options: list[str]) -> list[dict[str, Any]] | None:
    """把 thinking_options（off/none/minimal/low/medium/high/xhigh/max/enabled）翻译成
    opencode 的 reasoning_options。

    - 只有 off（不支持思考）→ None（不声明 reasoning_options，opencode
      不显示挡位）。
    - 含 enabled（开/关两态）→ reasoning: true + budget_tokens min/max。
    - 含任意 effort 挡位（含 minimal/xhigh/max）→ effort values 挡位。
    """
    if not thinking_options:
        return None
    opts = [o for o in thinking_options if o != "off"]
    if not opts:
        return None
    if "enabled" in opts and not any(o in opts for o in _EFFORT_VALUES):
        return None  # 开/关两态走 reasoning:true，不加挡位
    values = [o for o in _EFFORT_VALUES if o in opts]
    if values:
        return [{"type": "effort", "values": values}]
    return None


def _model_entry(cfg: Any, *, support_image: bool) -> dict[str, Any]:
    """一个上游 → opencode Model 结构。

    ``support_image`` 决定 modalities：true（模型名在顶层 vision_models 里）
    标 image-capable（input=text/image/pdf）；false 保持纯文本（image 不标），
    OpenCode 据此拦下对该模型的图片请求。
    """
    model_id = cfg.model or (cfg.allowed_models[0] if cfg.allowed_models else cfg.name)
    reasoning_opts = _reasoning_options(cfg.thinking_options or [])
    reasoning = reasoning_opts is not None or bool(cfg.thinking_options)
    entry: dict[str, Any] = {
        "id": model_id,
        "name": cfg.name,
        "release_date": "2025-01-01",
        "attachment": True,
        "reasoning": reasoning,
        "temperature": True,
        "tool_call": True,
        "limit": dict(MODEL_LIMITS),
        # 按 support_image 现场构建，避免污染共享常量。
        "modalities": dict(_IMAGE_MODALITIES if support_image else _TEXT_MODALITIES),
    }
    if reasoning_opts:
        entry["reasoning_options"] = reasoning_opts
    return entry


@router.get("/models/api.json")
async def models_api(request: Request) -> dict[str, Any]:
    """返回 opencode models.dev 兼容的 api.json。

    官方 models.dev 全量数据 + 一个 ``relay`` provider（匹配 opencode
    config 里的 provider id），models = 中继配置里所有上游声明过的模型
    （cfg.model + allowed_models 去重）。modalities 按顶层 vision_models
    判定：命中的模型标 image-capable，其余纯文本。
    """
    providers = _fetch_models_dev()
    relay_models: dict[str, Any] = {}
    settings = request.app.state.settings
    vision = set(settings.vision_models or [])
    for cfg in settings.upstreams_for("anthropic"):
        # v0.188：不再用 thinking_options 过滤 —— 让所有声明过的模型都
        # 出现在 relay provider 里，用户才能勾选（否则非 thinking 上游的
        # 多模态模型根本进不了清单）。modalities 由 vision_models 决定。
        # 一个上游可产出多个模型 entry：cfg.model + allowed_models（去重）。
        cands: list[str] = []
        if cfg.model:
            cands.append(cfg.model)
        for m in (cfg.allowed_models or []):
            if m not in cands:
                cands.append(m)
        if not cands:
            cands = [cfg.name or "auto"]
        for model_id in cands:
            support_image = model_id in vision
            entry = _model_entry(cfg, support_image=support_image)
            entry["id"] = model_id
            relay_models[model_id] = entry
    if relay_models:
        providers["relay"] = {
            "api": "anthropic",
            "name": "中继 (Relay)",
            "env": [],
            "id": "relay",
            "npm": "@ai-sdk/anthropic",
            "models": relay_models,
        }
    return providers
