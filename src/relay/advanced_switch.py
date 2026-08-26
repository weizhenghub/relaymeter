"""高级切换（实验性）—— 客户端没显式指定模型时，判定请求走弱还是强模型。

配置是**全局**的（upstreams.json 顶层 ``advanced_switch``，经
``Settings.advanced_*`` 读入），由设置页面板维护：

- ``weak_model``      弱模型（默认处理）
- ``strong_model``    强模型（复杂任务）
- ``analysis_model``  分析/判定模型（默认 = 弱模型）
- ``strong_types``    需要发到强模型的类型多选（分类器输出 type 命中即强）
- ``aggressive``      判定模糊时优先强模型；保守则用弱模型
- ``learning``        历史学习改进开关（预留，未实现）

流程：
  1. ``decide()`` —— 收到 body + 当前活跃 cfg + settings。
  2. L1 规则预筛：纯工具调用 → 弱；命中强/弱关键词 → 对应；反复失败信号 → 强。
  3. L2：把截断的用户文本发给 analysis_model，要它返回 JSON verdict：
     {"type": "...", "reason": "...", "ambiguous": true|false}。
     type 命中 strong_types → 强；ambiguous 且 aggressive → 强，否则弱。
  4. 按 verdict 解析强目标（找到提供 strong_model 的上游，同协议），
     回退默认弱。
  5. 返回 (final_cfg, final_model, route, reason) 供 proxy 转发。

所有异常路径都回退 weak（判定器挂了不丢请求，也不烧强模型额度）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Optional

from .config import PlatformConfig, Settings

log = logging.getLogger("relay.advanced_switch")

STRONG_KEYWORDS = ("规划", "设计", "架构", "重构", "总结", "评审", "排查", "从零写", "分析", "复杂")
WEAK_KEYWORDS = ("补全", "翻译", "改这一行", "加注释", "格式化", "改个", "问候", "你好", "天气")
RETRY_SIGNALS = ("还是不行", "仍然报错", "还是报错", "再试", "还是错", "依旧失败")

CLASSIFIER_MAX_CHARS = 4000
CLASSIFIER_TIMEOUT = 15.0
CLASSIFIER_MAX_TOKENS = 120

# 分类器 prompt：输出任务 type + 是否模糊。type 会跟 strong_types 比对。
CLASSIFIER_SYSTEM = """你是一个任务路由判定器。给定用户最近一次的请求文本，判断这个任务属于什么类型，以及该用【强模型】还是【弱模型】处理。

常见的任务类型（不限于此）：planning(规划) / architecture(架构) / refactor(重构) / summary(总结) / code_review(代码审查) / debug(调试) / question(问答) / translation(翻译) / tool_call(工具调用) / small_edit(小改动) / status(状态查询)

【强模型】用于：多步骤规划、架构/系统设计、大范围重构、长文总结与撰写、跨文件协同改动、复杂推理、code review、需求/安全边界分析、反复未解决的 bug、研究型调研。

【弱模型】用于：工具/函数调用、代码补全、单文件小改、翻译、格式转换、问答、问候、状态查询。

【必答】只输出一个 JSON 对象，不要任何额外文字或 Markdown 代码块：
{"type": "planning", "reason": "不超过20字的中文原因", "ambiguous": false}
其中 ambiguous 表示你是否无法确定该任务该用强还是弱（拿不准时设为 true）。"""

# 进程内统计：分析（分类器）调用次数与 token；弱/强决策次数。
_analysis_stats = {"count": 0, "tokens": 0}
_route_stats = {"weak": 0, "strong": 0}
_stats_lock = threading.Lock()


def _bump_analysis(tokens: int = 0) -> None:
    with _stats_lock:
        _analysis_stats["count"] += 1
        _analysis_stats["tokens"] += tokens


def _bump_route(route: str) -> None:
    with _stats_lock:
        _route_stats[route] = _route_stats.get(route, 0) + 1


def get_analysis_stats() -> dict:
    with _stats_lock:
        return {"count": _analysis_stats["count"], "tokens": _analysis_stats["tokens"]}


def get_route_stats() -> dict:
    with _stats_lock:
        return {"weak": _route_stats["weak"], "strong": _route_stats["strong"]}


_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE
)


def _strip_system_reminders(text: str) -> str:
    stripped = _SYSTEM_REMINDER_RE.sub("", text)
    return stripped.strip()


def _extract_user_text(body: bytes) -> str:
    """从 Anthropic messages body 里拼出 user 文本，剥离 system-reminder。"""
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return ""
    parts: list[str] = []
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") in (None, "text", "input_text") and blk.get("text"):
                    parts.append(blk["text"])
                elif isinstance(blk, dict) and blk.get("type") == "tool_result":
                    inner = blk.get("content")
                    if isinstance(inner, str):
                        parts.append(inner)
                    elif isinstance(inner, list):
                        for tb in inner:
                            if isinstance(tb, dict) and tb.get("text"):
                                parts.append(tb["text"])
    joined = "\n".join(parts).strip()
    return _strip_system_reminders(joined)


def _has_tool_activity(body: bytes) -> bool:
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return False
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") in ("tool_use", "tool_result"):
                    return True
    return False


def _apply_rules(model: Optional[str], user_text: str, has_tool: bool) -> Optional[tuple[str, str]]:
    """L1 规则预筛。命中返回 (route, reason)，未命中返回 None。"""
    if model and model != "auto":
        return None  # 显式指定模型，不拦截
    if has_tool and not user_text:
        return ("weak", "纯工具调用，无实质用户文本")
    if any(k in user_text for k in RETRY_SIGNALS):
        return ("strong", "检测到反复失败的 bug 信号")
    if any(k in user_text for k in STRONG_KEYWORDS):
        return ("strong", "命中强任务关键词")
    if any(k in user_text for k in WEAK_KEYWORDS):
        return ("weak", "命中弱任务关键词")
    return None


def _build_user_prompt(user_text: str, n_chars: int) -> str:
    return (
        f"用户最近一次请求的文本：\n---\n{user_text[:CLASSIFIER_MAX_CHARS]}\n---\n\n"
        f"附加信息：用户文本总长 {n_chars} 字符。\n\n"
        "请判定任务类型与强弱，严格输出 JSON。"
    )


def _parse_verdict(text: str) -> Optional[dict]:
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
    return {"type": m.group(1), "reason": ""}


async def _call_analysis_model(
    settings: Settings,
    cfg: PlatformConfig,
    analysis_upstream: Optional[str],
    analysis_model: str,
    prompt_user: str,
    *,
    ctx: Any = None,
) -> str:
    """向 analysis_model 上游发一次非流式 Anthropic messages 请求，返回文本。

    analysis_upstream 优先（设置面板选的精确上游）；否则回退当前活跃 cfg。

    Phase 2.11：ctx 可选 —— 提供时从 ``ctx.svc("pool")`` / ``ctx.svc("url_builder")``
    取服务；不提供时走 ``proxy._get_client`` / ``proxy._anthropic_messages_url``
    旧路径（兼容 test 路径）。
    """
    if ctx is not None:
        pool = ctx.svc("pool")
        url_builder = ctx.svc("url_builder")
        client = pool.get(cfg.url)
        url = url_builder.anthropic_messages_url(cfg.url)
    else:
        from . import proxy
        client = proxy._get_client(cfg.url)
        url = proxy._anthropic_messages_url(cfg.url)

    if analysis_upstream:
        for plat in ("anthropic", "openai", "openclaw"):
            c = settings.find_upstream(plat, analysis_upstream)
            if c is not None:
                cfg = c
                break

    client = proxy._get_client(cfg.url)
    url = proxy._anthropic_messages_url(cfg.url)
    payload = {
        "model": analysis_model,
        "max_tokens": CLASSIFIER_MAX_TOKENS,
        "messages": [{"role": "user", "content": CLASSIFIER_SYSTEM + "\n\n" + prompt_user}],
    }
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    # v0.12.1：鉴权头跟主路径一致 —— auth_style > auth_header > wire 默认。
    style = cfg.effective_auth_style("anthropic")
    if style == "bearer":
        headers["authorization"] = cfg.api_key or ""
    elif style == "x-api-key":
        headers["x-api-key"] = cfg.api_key or ""
    req = client.build_request("POST", url, headers=headers, content=json.dumps(payload).encode("utf-8"))
    resp = await client.send(req)
    if resp.status_code != 200:
        log.warning("analysis upstream %d: %s", resp.status_code, resp.text[:200])
        return ""
    data = resp.json()
    usage = data.get("usage") or {}
    _bump_analysis(int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0))
    text = "".join(blk.get("text", "") for blk in data.get("content", []) if blk.get("type") == "text")
    return text


def _resolve_strong(settings: Settings, cfg: PlatformConfig,
                    strong_upstream: Optional[str], strong_model: str):
    """找到强目标上游。strong_upstream 精确定位（同名模型靠它区分），
    找不到时回退提供 strong_model 的上游；再找不到 → (None, None) 上层回退弱。
    """
    if not strong_model:
        return None, None
    if strong_upstream:
        for plat in ("anthropic", "openai", "openclaw"):
            c = settings.find_upstream(plat, strong_upstream)
            if c is not None:
                return c, strong_model
        log.warning("advanced-switch: 强上游 %s 未找到，回退弱", strong_upstream)
        return None, None
    for plat in ("anthropic", "openai", "openclaw"):
        for c in settings.upstreams_for(plat):
            if c.model == strong_model or strong_model in (c.allowed_models or []):
                return c, strong_model
    return cfg, strong_model


async def decide(
    settings: Settings,
    cfg: PlatformConfig,
    body: bytes,
    model: Optional[str],
    platform: str = "anthropic",
    *,
    ctx: Any = None,
) -> tuple[PlatformConfig, Optional[str], str, str]:
    """高级切换决策。返回 (final_cfg, final_model, route, reason)。

    route ∈ {"weak", "strong"}。任何异常都回退 weak。
    """
    if not settings.advanced_switch:
        return cfg, model, "weak", "未启用"

    weak_upstream = settings.advanced_weak_upstream
    weak_model = settings.advanced_weak_model or cfg.model
    strong_upstream = settings.advanced_strong_upstream
    strong_model = settings.advanced_strong_model or ""
    analysis_upstream = settings.advanced_analysis_upstream
    analysis_model = settings.advanced_analysis_model or weak_model
    strong_types = [t.strip() for t in (settings.advanced_strong_types or [])]
    aggressive = bool(settings.advanced_aggressive)

    user_text = _extract_user_text(body)
    has_tool = _has_tool_activity(body)
    log.info("advanced-switch extract: %d chars model=%s has_tool=%s 原文=%r",
             len(user_text), model, has_tool, user_text[:120])

    # L1 规则预筛
    hit = _apply_rules(model, user_text, has_tool)
    if hit is not None:
        route, reason = hit
        log.info("advanced-switch rule-hit: route=%s reason=%s", route, reason)
        _bump_route(route)
        return _finish(settings, cfg, route, weak_upstream, weak_model,
                       strong_upstream, strong_model, reason, platform)

    # L2：analysis 模型判定 → 得到任务 type
    t0 = time.monotonic()
    try:
        reply = await _call_analysis_model(
            settings, cfg, analysis_upstream, analysis_model,
            _build_user_prompt(user_text, len(user_text)),
            ctx=ctx,
        )
    except Exception as exc:
        log.warning("analysis call failed: %r", exc)
        reply = ""
    elapsed = time.monotonic() - t0

    route: str
    reason: str
    if not reply.strip():
        route = "strong" if aggressive else "weak"
        reason = f"判定器无响应({elapsed:.1f}s)，按{'激进' if aggressive else '保守'}回退{route}"
    else:
        verdict = _parse_verdict(reply)
        if not verdict:
            route = "strong" if aggressive else "weak"
            reason = f"判定结果无法解析，按{'激进' if aggressive else '保守'}回退{route}"
        else:
            task_type = verdict.get("type", "")
            ambiguous = bool(verdict.get("ambiguous"))
            reason = verdict.get("reason", "")
            log.info("advanced-switch classify: type=%s ambiguous=%s reason=%s %.1fs",
                     task_type, ambiguous, reason, elapsed)
            if task_type in strong_types:
                route = "strong"
                reason = reason or f"类型 {task_type} 需强模型"
            elif ambiguous:
                route = "strong" if aggressive else "weak"
                reason = f"判定模糊({task_type})，按{'激进' if aggressive else '保守'}走{route}"
            else:
                route = "weak"
                reason = reason or "普通任务"

    _bump_route(route)
    return _finish(settings, cfg, route, weak_upstream, weak_model,
                   strong_upstream, strong_model, reason, platform)


def _finish(settings: Settings, cfg: PlatformConfig, route: str,
            weak_upstream: Optional[str], weak_model: str,
            strong_upstream: Optional[str], strong_model: str,
            reason: str, platform: str):
    """按 verdict 决定 final cfg/model。"""
    if route == "strong" and strong_model:
        target_cfg, target_model = _resolve_strong(settings, cfg, strong_upstream, strong_model)
        if target_cfg is not None:
            log.info("advanced-switch decided: cfg=%s model=%s route=strong (%s)",
                     target_cfg.name, target_model, reason)
            return target_cfg, target_model, "strong", reason
    log.info("advanced-switch decided: cfg=%s model=%s route=weak (%s)",
             cfg.name, weak_model, reason)
    return cfg, weak_model, "weak", reason
