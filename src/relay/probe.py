"""v0.12 上游自动探测（docs/wire-dispatch-plan.md §5）。

新建上游时中继发一组便宜请求，自动判明 wire / 端点 / 认证方式 /
模型列表 / key 有效性。best-effort：任何失败都不阻塞，返回已判明部分。
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Optional

import httpx

from .plugin import (
    emit_event,
    prober_for,
    run_hooks,
)
from .config import (
    WIRE_ANTHROPIC_MESSAGES,
    WIRE_OPENAI_CHAT,
    WIRE_OPENAI_RESPONSES,
    all_wire_defaults,
    join_endpoint,
)

# 判定"端点存在"的状态码：404/405 = 没这个端点；其余（含 400/401/422/429）
# 都说明端点存在（只是 key/model 不对）。200 = 完全可用。
_ENDPOINT_MISSING = (404, 405)

# 404 响应体里的"模型名不存在"特征 —— 这类 404 不是端点缺失，而是占位
# 模型名被严格校验模型名的上游（DeepSeek V4 官方、token.sensenova.cn 等）
# 拒绝。命中时端点其实存在，只差真实模型名。
_MODEL_NOT_FOUND_RE = re.compile(
    r"model[ _-]?not[ _-]?found|model[ _-]?is[ _-]?not[ _-]?found|"
    r"model[ _-]?not[ _-]?exist|not[ _-]?found.*model|"
    r"model.*(?:not[ _-]?available|doesn'?t exist|unknown)",
    re.IGNORECASE,
)


def _is_model_missing(st: Optional[int], body: str) -> bool:
    """404 且 body 明示"模型名不存在" → 端点存在但模型名不对。"""
    if st not in _ENDPOINT_MISSING:
        return False
    if not body:
        return False
    if _MODEL_NOT_FOUND_RE.search(body):
        return True
    try:
        d = json.loads(body)
    except Exception:
        return False
    msg = ""
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type") or ""
        elif isinstance(err, str):
            msg = err
        else:
            msg = d.get("message") or ""
    return bool(_MODEL_NOT_FOUND_RE.search(msg))


# 用户配置 URL 的完整端点形态（base 末段即端点本身）。
_FULL_ENDPOINT_TAILS = (
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
    "/v1/embeddings",
    "/v1/completions",
)


def _models_root(base: str) -> str:
    """base 已含完整端点时剥出根 URL（打 /v1/models 用）。

    ``https://token.sensenova.cn/v1/chat/completions`` →
    ``https://token.sensenova.cn``。否则原样返回。
    """
    for tail in _FULL_ENDPOINT_TAILS:
        if base.endswith(tail):
            return base[: -len(tail)].rstrip("/")
    return base

# 连通性测试的最小请求 max_tokens。v0.96.1：不能用 1 —— 思考型上游
# （DeepSeek V4 官方端点等）响应开头是 thinking 块，1 个 token 全被思考
# 吃掉，正文 text 块根本不会出现，2xx 也判"没解析到回复"。给足余量让
# 思考后还能吐出正文。对各 wire 一致（省一个分支常量）。
_CONNECT_MAX_TOKENS = 512


async def _req(
    client: httpx.AsyncClient, method: str, path: str,
    headers: dict, body: Optional[dict],
) -> tuple[Optional[int], str]:
    try:
        r = await client.request(method, path, headers=headers, json=body)
        return r.status_code, r.text
    except Exception as exc:  # noqa: BLE001 - probe is best-effort
        return None, f"error: {exc}"


def _auth_headers(style: str, key: str) -> dict:
    h = {"content-type": "application/json"}
    if style == "bearer":
        h["authorization"] = f"Bearer {key}"
    else:
        h["x-api-key"] = key
    return h


def _extract_reply(wire: str, body: str) -> tuple[Optional[str], str]:
    """从上游响应体里提取模型文本回复。返回 (reply, 失败原因)。

    v0.85：连通性测试不满足于 2xx —— 必须真正解析出模型回复文本。
    按 wire 兼容三种主流结构；解析失败返回 (None, 失败原因) 用于日志。
    """
    try:
        d = json.loads(body)
    except Exception:
        return None, "（响应不是 JSON）"
    if not isinstance(d, dict):
        return None, "（响应结构不是对象）"

    reply: Optional[str] = None
    if wire == WIRE_ANTHROPIC_MESSAGES:
        content = d.get("content")
        if isinstance(content, list):
            # v0.96.1：不只取 content[0] —— 思考型上游（DeepSeek V4 官方
            # Anthropic 端点等）响应开头是 {"type":"thinking",...} 块，正文
            # text 块排在后面。只读第一块会漏掉正文，2xx 也判"没解析到回复"。
            # 遍历所有块，取第一个带 text 的（自然跳过 thinking 块）。
            for blk in content:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    reply = blk["text"]
                    break
        if reply is None:
            err = d.get("error")
            if isinstance(err, dict) and err.get("message"):
                return None, f"（响应含错误：{err.get('message')}）"
    elif wire == WIRE_OPENAI_RESPONSES:
        # Responses API：output[0].content[0].text 或顶层 output_text
        out = d.get("output")
        if isinstance(out, list) and out:
            first = out[0]
            if isinstance(first, dict):
                c0 = first.get("content")
                if isinstance(c0, list) and c0 and isinstance(c0[0], dict):
                    reply = c0[0].get("text")
        if reply is None and isinstance(d.get("output_text"), str):
            reply = d["output_text"]
        if reply is None and isinstance(d.get("error"), dict):
            return None, f"（响应含错误：{d['error'].get('message', d['error'])}）"
    else:
        # openai-chat：choices[0].message.content（可能是 str 或 list）
        choices = d.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            if isinstance(msg, dict):
                c = msg.get("content")
                if isinstance(c, str):
                    reply = c
                elif isinstance(c, list) and c:
                    # 新版多模态 content 数组
                    parts = [p.get("text") for p in c if isinstance(p, dict) and isinstance(p.get("text"), str)]
                    reply = "".join(parts)
        if reply is None and isinstance(d.get("error"), dict):
            return None, f"（响应含错误：{d['error'].get('message', d['error'])}）"

    if reply is None:
        return None, ""
    reply = (reply or "").strip()
    if not reply:
        return None, "（回复为空文本）"
    return reply, ""


async def probe_upstream(
    url: str, api_key: str, timeout: float = 15.0,
    emit: Optional[Any] = None,
    model: Optional[str] = None,
    prober: Optional[str] = None,
) -> dict[str, Any]:
    """探测一个上游。返回 {wire, endpoint, auth_style, models, key_valid,
    evidence, quirks}。任何子步骤失败只记录 evidence，不抛异常。

    ``emit``：可选 async 回调 emit(line)，每步实时推送日志（v0.12.1
    新建上游"测试"按钮用 —— GUI 进程直接跑探测，日志推给页面）。

    ``model``：优先用调用方给的真实模型名发最小请求（v0.95+，P2）——
    部分上游（DeepSeek V4 等）严格校验模型名，占位名 ``relay-probe``
    会被 400 拒绝，导致端点误判为"不可达"。传入后探测请求用真实名。

    ``prober``：v0.98.3 插件探活器名。命中插件注册的实现时整段探测
    委托给它（返回同一标准结构）；未注册回退内置探测。
    """
    if prober:
        fn = prober_for(prober)
        if fn is not None:
            result = fn(url, api_key, timeout=timeout, model=model)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict):
                result = {"wire": None, "endpoint": None, "auth_style": None,
                          "models": [], "key_valid": False,
                          "evidence": [], "quirks": []}
            probe_info = {
                "wire": result.get("wire"),
                "endpoint": result.get("endpoint"),
                "auth_style": result.get("auth_style"),
                "key_valid": result.get("key_valid"),
                "ok": bool(result.get("wire")) if result.get("ok") is None
                else bool(result.get("ok")),
                "models": result.get("models") or [],
                "url": (url or "").rstrip("/"),
                "evidence": result.get("evidence") or [],
            }
            await run_hooks("after_probe", probe_info)
            result["wire"] = probe_info["wire"]
            result["endpoint"] = probe_info["endpoint"]
            result["auth_style"] = probe_info["auth_style"]
            result["key_valid"] = probe_info["key_valid"]
            emit_event(
                "probe.finished",
                upstream=probe_info["url"], url=probe_info["url"],
                wire=probe_info["wire"], auth_style=probe_info["auth_style"],
                model=model, key_valid=probe_info["key_valid"],
                ok=probe_info["ok"],
                error=None if probe_info["wire"] else "prober could not identify protocol",
            )
            return result
        log.warning("prober %r 未注册，回退内置探测", prober)
    base = (url or "").rstrip("/")
    key = api_key or ""
    evidence: list[str] = []
    quirks: list[str] = []
    wire: Optional[str] = None
    endpoint: Optional[str] = None
    auth_style: Optional[str] = None
    models: list[str] = []
    key_valid = False

    async def log(msg: str) -> None:
        evidence.append(msg)
        if emit is not None:
            await emit(msg)

    await log(f"开始探测 {base}")

    # 保持默认 trust_env（读系统代理/环境代理）—— 访问国外上游时需要走
    # 梯子。不要强制直连。
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        # 1) GET /v1/models —— 认证方式 + 协议线索 + 模型列表 + key 有效性
        await log("步骤 1/2：模型列表与认证方式（GET /v1/models，两种头各试一次）")
        # v0.208：base 已含完整端点时（如 .../v1/chat/completions）打在根，
        # 否则 join_endpoint 会拼出 .../chat/completions/models 这种 404 路径。
        models_url = join_endpoint(_models_root(base), "/v1/models")
        for style in ("bearer", "x-api-key"):
            st, txt = await _req(client, "GET", models_url,
                                 _auth_headers(style, key), None)
            if st == 200:
                auth_style = style
                key_valid = True
                try:
                    d = json.loads(txt)
                    for m in d.get("data") or []:
                        if isinstance(m, dict) and isinstance(m.get("id"), str):
                            models.append(m["id"])
                except Exception:
                    pass
                shown = ", ".join(models[:10])
                if len(models) > 10:
                    shown += f" …共 {len(models)} 个"
                await log(f"  {style}: 200 ✓（认证方式={style}，key 有效{('，模型：' + shown) if shown else ''}）")
                break
            await log(f"  {style}: {st if st is not None else 'ERR'}")

        # 2) 端点探测：POST chat/completions 与 /v1/messages，谁端点存在谁就是 wire
        await log("步骤 2/2：协议端点（发最小测试请求）")
        styles = [auth_style] if auth_style else ["bearer", "x-api-key"]
        # v0.95+（P2）：优先用调用方给的真实模型名，其次 models[0]，
        # 最后才回退占位名 relay-probe —— 严格校验模型名的上游不至于
        # 因占位名被 400 而误判端点"不可达"。
        model = model or (models[0] if models else "relay-probe")

        async def post_probe(url: str, body: dict, style: str):
            """发最小请求探测端点；占位模型名被拒（404 model-missing）时
            新建 client + 换真实模型名重试一次（v0.208：复用一个 client
            连发会让部分网关对"404→重试"挂限流，第二次超时）。"""
            hdrs = _auth_headers(style, key)
            st, txt = await _req(client, "POST", url, hdrs, body)
            if _is_model_missing(st, txt) and models:
                await log(f"  POST {url.split('/')[-2]}/{url.split('/')[-1]} ({style}): {st}（模型名被拒，换真实模型名重试）")
                body = dict(body, model=models[0])
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c2:
                    st, txt = await _req(c2, "POST", url, hdrs, body)
            return st, txt

        for style in styles:
            oai_body = {"model": model, "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}]}
            st, txt = await post_probe(join_endpoint(base, "/v1/chat/completions"), oai_body, style)
            if st is not None and st not in _ENDPOINT_MISSING:
                wire = WIRE_OPENAI_CHAT
                endpoint = "/v1/chat/completions"
                if st == 200:
                    key_valid = True
                if auth_style is None:
                    auth_style = style
                await log(f"  POST /v1/chat/completions ({style}): {st} → openai-chat")
                break
            await log(f"  POST /v1/chat/completions ({style}): {st if st is not None else 'ERR'}")

            ant_body = {"model": model, "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}]}
            st2, txt2 = await post_probe(join_endpoint(base, "/v1/messages"), ant_body, style)
            if st2 is not None and st2 not in _ENDPOINT_MISSING:
                wire = WIRE_ANTHROPIC_MESSAGES
                endpoint = "/v1/messages"
                if st2 == 200:
                    key_valid = True
                if auth_style is None:
                    auth_style = style
                await log(f"  POST /v1/messages ({style}): {st2} → anthropic-messages")
                break
            await log(f"  POST /v1/messages ({style}): {st2 if st2 is not None else 'ERR'}")

    if wire:
        await log(
            f"结论：wire={wire}  endpoint={endpoint}  鉴权={auth_style}  "
            f"key={'有效' if key_valid else '无效'}"
        )
    else:
        await log("结论：未能识别该上游（检查 URL 是否可达、路径是否正确）")

    # v0.98.2 决策钩子：after_probe —— 插件可改判探测结论（修正 wire /
    # endpoint / auth_style / key_valid / ok），改后即作为最终结论发出事件
    # 并返回。
    probe_info = {
        "wire": wire,
        "endpoint": endpoint,
        "auth_style": auth_style,
        "key_valid": key_valid,
        "ok": bool(wire),
        "models": models,
        "url": base,
        "evidence": evidence,
    }
    await run_hooks("after_probe", probe_info)
    wire = probe_info["wire"]
    endpoint = probe_info["endpoint"]
    auth_style = probe_info["auth_style"]
    key_valid = probe_info["key_valid"]
    emit_event(
        "probe.finished",
        upstream=base, url=base, wire=wire,
        auth_style=auth_style, model=model,
        key_valid=key_valid, ok=probe_info["ok"],
        error=None if wire else "could not identify protocol",
    )
    return {
        "wire": wire,
        "endpoint": endpoint,
        "auth_style": auth_style,
        "models": models,
        "key_valid": key_valid,
        "evidence": evidence,
        "quirks": quirks,
    }


async def _suggest_after_fail(
    base: str,
    key: str,
    model: str,
    configured_wire: str,
    configured_style: str,
    log,
) -> None:
    """连通性测试失败后的智能兜底：反向探测真实协议/鉴权，给出纠错建议。

    v0.95+（协议鲁棒）：按用户配置发失败不代表 URL/key 错 —— 常见是
    **协议选错**（anthropic ↔ openai 颠倒）。这里用调用方给的真实模型
    名重新跑一遍 ``probe_upstream``（自动试两种鉴权头 + 两个端点），
    把探测出的 wire / auth_style 与用户配置对比，输出明确纠错建议。

    best-effort：探测失败不抛、不改返回结果，只追加日志。用真实模型
    名探测顺带验证"占位模型名是不是失败原因"（严格校验模型名的上游
    用占位名会被 400 拒，用真实名能通）。
    """
    try:
        probe = await probe_upstream(base, key, model=model or None, emit=log)
    except Exception as exc:  # noqa: BLE001
        await log(f"（反向探测异常：{exc}）")
        return False
    pw = probe.get("wire")
    pa = probe.get("auth_style")
    if not pw:
        await log("⚠ 反向探测未能识别协议 —— 请检查 URL 是否可达、路径是否正确。")
        return False
    if pw != configured_wire:
        hint_auth = f"、鉴权方式改成 {pa}" if pa and pa != configured_style else ""
        await log(
            f"⚠ 反向探测发现该端点的实际协议是 {pw}（你配置的是 {configured_wire}）。"
            f"建议把协议改成 {pw}{hint_auth} 后重试。"
        )
        return False
    if pa and pa != configured_style:
        await log(
            f"⚠ 协议匹配（{pw}），但鉴权方式不对：实际是 {pa}"
            f"（你配置的是 {configured_style}）。"
        )
        return False
    if probe.get("key_valid"):
        await log(
            f"✓ 反向探测用真实模型名验证通过 —— 协议 {pw} 与 key 均正确；"
            f"若连通性仍失败，请检查模型名是否正确。"
        )
        return True
    else:
        await log(
            f"⚠ 协议匹配（{pw}），但 key 未通过验证 —— 请检查 API key 是否正确。"
        )
        return False


async def connectivity_test(
    url: str,
    api_key: str,
    wire: Optional[str] = None,
    auth_style: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 15.0,
    emit: Optional[Any] = None,
) -> dict[str, Any]:
    """v0.84 连通性测试 —— 严格按调用方给定的配置（wire / 鉴权 / 模型）
    构造一条最小消息真实发到上游，验证"当前填的配置能否通"。

    与 ``probe_upstream`` 不同：probe 是自动探测（试两种鉴权头、自动
    判 wire），这里**不猜**，用传入的 wire / auth_style 原样发，命中即
    真通，与保存后的真实行为一致。返回 {ok, status_code, model_used,
    evidence}，任何异常不抛。
    """
    base = (url or "").rstrip("/")
    key = api_key or ""
    wire = wire or WIRE_OPENAI_CHAT
    style = auth_style or all_wire_defaults().get(wire, {}).get("auth_style", "bearer")
    model = model or "relay-connect-test"
    evidence: list[str] = []

    async def log(msg: str) -> None:
        evidence.append(msg)
        if emit is not None:
            await emit(msg)

    await log(f"连通性测试 {base}")
    await log(f"协议={wire}  鉴权={style}  模型={model}")

    # 按 wire 决定端点 + 请求体。model 用调用方给的（或后端已填的）。
    # max_tokens 用 _CONNECT_MAX_TOKENS（v0.96.1）：1 会被思考模型全部
    # 吃掉，正文出不来 → 2xx 误判"没解析到回复"。
    if wire == WIRE_ANTHROPIC_MESSAGES:
        endpoint = all_wire_defaults()[wire]["endpoint"]
        body: dict = {
            "model": model,
            "max_tokens": _CONNECT_MAX_TOKENS,
            "messages": [{"role": "user", "content": "ping"}],
        }
    elif wire == WIRE_OPENAI_RESPONSES:
        endpoint = all_wire_defaults()[wire]["endpoint"]
        body = {
            "model": model,
            "max_output_tokens": _CONNECT_MAX_TOKENS,
            "input": "ping",
        }
    else:
        endpoint = all_wire_defaults()[WIRE_OPENAI_CHAT]["endpoint"]
        body = {
            "model": model,
            "max_tokens": _CONNECT_MAX_TOKENS,
            "messages": [{"role": "user", "content": "ping"}],
        }

    await log(f"POST {join_endpoint(base, endpoint)}")
    # 保持默认 trust_env（读系统代理）—— 国外上游需要走梯子。
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        hdrs = _auth_headers(style, key)
        st, txt = await _req(client, "POST", join_endpoint(base, endpoint), hdrs, body)

    # v0.86：把本次发出的**完整原始请求**（方法 + URL + 头 + JSON body）
    # 附在日志末尾，便于核对配置到底发出了什么。
    raw_headers = "\n".join(f"  {k}: {v}" for k, v in hdrs.items())
    raw_body = json.dumps(body, ensure_ascii=False, indent=2)
    request_text = (
        f"───── 原始请求 ─────\n"
        f"POST {join_endpoint(base, endpoint)}\n"
        f"{raw_headers}\n"
        f"\n{raw_body}\n"
        f"────────────────────"
    )

    # v0.95+（协议鲁棒）：按用户配置失败后，反向探测真实协议/鉴权，
    # 输出纠错建议（best-effort，日志进 evidence 并实时推页面）。
    async def fail_return(st, extra=None):
        # v0.208：若反向探测已用真实模型名验证通过（协议/key 都对），
        # 说明配置只差模型名 —— 汇总成明确结论。
        real_model_ok = await _suggest_after_fail(base, key, model, wire, style, log)
        if real_model_ok:
            await log(
                f"✗ 配置的协议/鉴权/URL 均正确，但模型名 {model!r} 在该上游"
                f"不存在 —— 请填写真实模型名后重试。"
            )
        # 原始请求文本必须是 evidence 最后一条（前端取最后一条打印）。
        evidence.append(request_text)
        emit_event(
            "probe.finished",
            upstream=base, url=base, wire=wire,
            auth_style=style, model=model,
            key_valid=False, ok=False,
            error=f"status={st}",
        )
        result = {"ok": False, "status_code": st, "model_used": model,
                  "evidence": evidence}
        if extra:
            result.update(extra)
        return result

    if st is None:
        await log(f"请求失败：{txt}")
        return await fail_return(None)
    await log(f"HTTP {st}")
    if 200 <= st < 300:
        # v0.85：连通与否不以 2xx 为准 —— 必须从响应体里**解析出模型的
        # 文本回复**才算连通。有些上游 2xx 但 body 是错误对象（或空），
        # 那种不算通。解析失败记 reply_error 日志，ok=False。
        reply, reply_error = _extract_reply(wire, txt)
        if reply:
            shown = reply if len(reply) <= 60 else reply[:57] + "…"
            await log(f"✓ 收到模型回复：{shown!r}")
            evidence.append(request_text)
            emit_event(
                "probe.finished",
                upstream=base, url=base, wire=wire,
                auth_style=style, model=model,
                key_valid=True, ok=True, error=None,
            )
            return {"ok": True, "status_code": st, "model_used": model,
                    "reply": reply, "evidence": evidence}
        await log(f"✗ 上游返回 2xx 但没解析到模型回复{reply_error}")
        return await fail_return(st)
    # 非 2xx —— 列出常见语义，方便用户对症
    hint = {
        400: "（参数/格式问题，可能模型名不对或请求体有误）",
        401: "（鉴权失败：API Key 错误或鉴权头类型不对）",
        402: "（余额不足或支付需要）",
        403: "（被拒绝：key 无权限或上游封禁）",
        404: "（端点不存在：检查 URL 与协议是否匹配）",
        405: "（端点不存在：协议与 URL 不匹配）",
        422: "（参数错误：模型名或请求体不匹配该协议）",
        429: "（限流/免费额度用尽）",
        500: "（上游服务端错误）",
        502: "（上游网关错误）",
        503: "（上游不可用）",
    }.get(st, "")
    # v0.208：404 且响应体明示"模型名不存在" → 是模型名问题不是端点问题，
    # 覆盖通用 404 hint，别把用户引到"检查 URL"上。
    if st == 404 and _is_model_missing(st, txt):
        hint = "（模型名不存在：该上游严格校验模型名，请填写真实模型名）"
    await log(f"✗ 上游返回 {st}{hint}")
    return await fail_return(st)
