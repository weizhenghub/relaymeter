"""报错分析（v0.113o）—— 把一次请求失败的上下文发给用户所选的小模型，
判断错误类型（余额耗尽 / 网络错误 / 达到次数限制等），返回给用户看的
中文提示。

架构：**纯 GUI 进程**。GUI 轮询线程在 relay.db 里读到新出现的请求
错误后，于后台线程 ``asyncio.run(classify_error(...))`` 外呼分析模型，
结果经 ``snapshot["error_hints"]`` 推给前端渲染成 toast。不碰 relay
进程、不碰 proxy 错误路径，relay 停/起不影响（只读 DB + 独立外呼）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .config import Settings

log = logging.getLogger("relay.error_analyzer")

CLASSIFIER_TIMEOUT = 15.0
CLASSIFIER_MAX_TOKENS = 150
MAX_CONTEXT_CHARS = 600

# 错误分类 prompt：输出 JSON {"type", "hint"}。type 只取下列类别。
ERROR_SYSTEM_PROMPT = """你是报错诊断助手。给定一次大模型 API 请求失败的上下文（平台/模型/上游/状态码/内部错误码/错误消息），判断错误类别，并给用户一条不超过30字的中文提示。

错误类别（type）只允许取以下值：
- balance        余额不足 / 额度耗尽（如 402、insufficient_quota、余额相关报错）
- rate_limit     达到次数/速率限制（如 429、rate_limit_error、overloaded_error）
- auth           鉴权失败（如 401/403、authentication_error、permission_error、key 无效）
- network        网络错误（连接失败、超时、上游断开、upstream_disconnect/timeout）
- server         上游服务端错误（如 5xx、api_error、overloaded、中继内部错误）
- config         配置问题（未知 api-key、无匹配上游、relay_unknown_key、模型名错误）
- other          以上都不匹配

【必答】只输出一个 JSON 对象，不要任何额外文字或 Markdown 代码块：
{"type": "rate_limit", "hint": "请求过于频繁，触发了限流，请稍后再试"}
其中 hint 是给用户看的简短中文提示，要具体、可行动。"""

# 前端 toast 上显示的类别标签。
CATEGORY_LABELS = {
    "balance": "余额不足",
    "rate_limit": "达到限制",
    "auth": "鉴权失败",
    "network": "网络错误",
    "server": "服务端错误",
    "config": "配置问题",
    "other": "其他",
}

# 设置页「测试」按钮用的示例报错。
SAMPLE_ERROR_CONTEXT = {
    "platform": "anthropic",
    "model": "—",
    "upstream": "示例上游",
    "status_code": 429,
    "error": "upstream_429",
    "message": '{"error":{"type":"rate_limit_error","message":"You have reached the maximum number of requests per minute"}}',
}


def build_context(row: dict) -> dict:
    """把 requests 表一行转成给分类器的上下文。"""
    return {
        "platform": row.get("platform") or "",
        "model": row.get("model") or "",
        "upstream": row.get("upstream") or "",
        "status_code": row.get("status_code"),
        "error": row.get("error") or "",
        "message": (row.get("error") or "")[:MAX_CONTEXT_CHARS],
    }


def build_prompt(context: dict) -> str:
    """把上下文拼成 user prompt。"""
    sc = context.get("status_code")
    return (
        "一次 API 请求失败，请分类并给提示。\n"
        f"平台：{context.get('platform') or '—'}\n"
        f"模型：{context.get('model') or '—'}\n"
        f"上游：{context.get('upstream') or '—'}\n"
        f"状态码：{sc if sc is not None else '—'}\n"
        f"内部错误码：{context.get('error') or '—'}\n"
        f"错误消息：{context.get('message') or '—'}"
    )


def parse_verdict(text: str) -> Optional[dict]:
    """JSON-tolerant 解析，镜像 advanced_switch._parse_verdict。"""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict) and obj.get("type"):
                return obj
        except json.JSONDecodeError:
            pass
    m = re.search(r'"type"\s*:\s*"([^"]*)"', text)
    if not m:
        return None
    return {"type": m.group(1), "hint": ""}


def resolve_target(settings: Settings, context: dict):
    """定位分析目标上游。

    error_analysis_upstream 优先（精确定位同名模型）；否则找提供所选
    error_analysis_model 的上游；再找不到用第一个 anthropic 上游的兜底
    模型。返回 (cfg, model)；无模型可解析返回 (None, None)。
    """
    upstream = settings.error_analysis_upstream
    model = settings.error_analysis_model

    def _scan(plat):
        for c in settings.upstreams_for(plat):
            if upstream:
                if c.name == upstream:
                    return c, (model or c.default_model or c.model)
            elif model:
                if c.model == model or model in (c.allowed_models or []):
                    return c, model
                if (c.default_model or c.model) == model:
                    return c, model
            else:
                return c, (c.default_model or c.model)
        return None, None

    for plat in ("anthropic", "openai", "openclaw"):
        cfg, m = _scan(plat)
        if cfg is not None and m:
            return cfg, m
    return None, None


async def classify_error(
    settings: Settings,
    context: dict,
    *,
    ctx: Any = None,
) -> dict:
    """向分析模型发一次非流式 Anthropic messages 请求，返回分类结果。

    返回 ``{"ok": True, "type", "hint", "category"}`` 或
    ``{"ok": False, "error"}``。任何异常都不抛 —— 调用方 fire-and-forget。
    HTTP 段镜像 advanced_switch._call_analysis_model（同款 auth 头 /
    /v1 补全 / 鉴权样式）。

    Phase 2.11：可选 ``ctx`` —— 提供时从 ctx.svc("pool") / ctx.svc("url_builder")
    取服务；不提供时走 ``proxy._get_client`` / ``proxy._anthropic_messages_url`` 旧路径。
    """
    try:
        cfg, model = resolve_target(settings, context)
        if cfg is None or not model:
            return {"ok": False, "error": "未配置可用的分析模型"}

        if ctx is not None:
            client = ctx.svc("pool").get(cfg.url)
            url = ctx.svc("url_builder").anthropic_messages_url(cfg.url)
        else:
            from . import proxy
            client = proxy._get_client(cfg.url)
            url = proxy._anthropic_messages_url(cfg.url)
        payload = {
            "model": model,
            "max_tokens": CLASSIFIER_MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": ERROR_SYSTEM_PROMPT + "\n\n" + build_prompt(context),
                }
            ],
        }
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
        # 鉴权头跟主路径一致 —— auth_style > auth_header > wire 默认。
        style = cfg.effective_auth_style("anthropic")
        if style == "bearer":
            headers["authorization"] = cfg.api_key or ""
        elif style == "x-api-key":
            headers["x-api-key"] = cfg.api_key or ""
        req = client.build_request(
            "POST", url, headers=headers, content=json.dumps(payload).encode("utf-8")
        )
        resp = await client.send(req)
        if resp.status_code != 200:
            return {"ok": False, "error": f"分析上游 {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        text = "".join(
            blk.get("text", "")
            for blk in data.get("content", [])
            if blk.get("type") == "text"
        )
        verdict = parse_verdict(text)
        if not verdict:
            return {"ok": False, "error": "分类结果无法解析"}
        etype = verdict.get("type") or "other"
        return {
            "ok": True,
            "type": etype,
            "hint": verdict.get("hint") or "",
            "category": CATEGORY_LABELS.get(etype, etype),
        }
    except Exception as exc:  # noqa: BLE001 —— 分类失败绝不能抛给调用方
        return {"ok": False, "error": str(exc)}
