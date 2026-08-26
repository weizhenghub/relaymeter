"""Client-tool identification from the request's User-Agent header.

The relay sits in front of several AI coding agents (Claude Code, Codex,
OpenCode, OpenClaw, …). They all hit the same `/anthropic` or `/openai`
entry, so the request body / path alone can't tell them apart. The one
reliable, protocol-independent signal is the `User-Agent` header — each SDK
identifies itself there.

This module turns a raw User-Agent string into a canonical tool key stored
in the `requests.agent` column. The mapping has three tiers:

1. Whitelist rules below (order matters — first match wins) return a stable
   canonical key (e.g. `claude-code`, `opencode`).
2. Any other request *with* a User-Agent header gets a token extracted from
   that header and used verbatim as its agent key — e.g. `Bun/1.3.14` → `bun`,
   `python-httpx/0.28.1` → `python-httpx`, `undici` → `undici`. 基础设施标记
   （Mozilla / Chrome / Electron / Windows NT …）会被跳过，所以一个没在
   白名单里的 Electron 应用会得到它自己的 app 名，而不是 "mozilla"。
3. Only requests with *no* User-Agent header at all fall into the AGENT_UNKNOWN
   bucket (renamed `其它`, previously `未知`). Nothing else lands there anymore:
   a present-but-unrecognised UA always gets its own dynamic key.
"""

from __future__ import annotations

# Stored verbatim in `requests.agent` for requests that carry no User-Agent.
# It's a stable key so users can rename the whole bucket via `agent_aliases`.
AGENT_UNKNOWN = "其它"

# Ordered list of (canonical key, substrings to match, case-insensitive).
# First match wins — order matters (e.g. OpenCode's UA also contains "ai-sdk",
# so "opencode" must be checked before any ai-sdk fallback).
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("claude-code", ("claude-cli", "claude code")),
    ("opencode", ("opencode",)),
    ("codex", ("codex",)),
    # OpenClaw 桌面客户端（ClawX）是 Electron 应用，UA 是
    # "… clawx/0.5.2 Chrome/… Electron/40.10.6 …"（不含 "openclaw"）。
    # 实测（relay_trace.log 2026-08-24）：clawx/0.5.2 请求的 agent 被归
    # 成「未知」，用户反馈"平台流量里没有 openclaw"。加 "clawx" 子串。
    ("openclaw", ("openclaw", "clawx")),
    # Legacy / generic Anthropic SDK without a specific tool identity. Kept
    # separate from AGENT_UNKNOWN so it can be renamed independently.
    ("ai-sdk", ("ai-sdk/",)),
]

# UA 里的基础设施标记：浏览器引擎 / 操作系统 / 平台标识，不带任何工具
# 身份。动态抽 token 时跳过它们，否则一个 Electron 应用的 UA 会抽出
# "mozilla" 或 "chrome" 而不是它自己。
_INFRA: frozenset[str] = frozenset({
    # 浏览器引擎与外壳
    "mozilla", "applewebkit", "khtml", "gecko", "like", "compatible",
    "chrome", "chromium", "electron", "safari", "opr", "edg", "trident",
    "version", "rv",
    # 操作系统 / 硬件平台
    "windows", "win64", "win32", "nt", "wow64", "x64", "x86", "arm",
    "macintosh", "mac", "intel", "linux", "x11", "android", "iphone",
    "ipad", "cros",
})


def _extract_ua_token(user_agent: str) -> str | None:
    """Return the first meaningful product name of a UA, else None.

    A UA is a space-separated list of `product/version` tokens plus
    parenthesised comments (OS / rendering-engine notes). We keep the first
    token that names a real client: its leading segment before `/` or `:`
    (e.g. `Bun/1.3.14` → `bun`), skipping parenthesised comment tokens, infra
    markers and pure-numeric tokens.
    """
    for raw in user_agent.split():
        # 带括号的 token 是注释（OS / 引擎说明），如 "(Windows NT 10.0; zh-CN)"
        # —— 跳过，否则 PowerShell 的 UA 会抽出 "zh-cn" 而不是 Windowspowershell。
        if "(" in raw or ")" in raw:
            continue
        tok = raw.strip("();,")
        if not tok:
            continue
        name = tok.split("/", 1)[0].split(":", 1)[0].lower()
        if name in _INFRA or not any(ch.isalpha() for ch in name):
            continue
        return name
    return None


def sniff_agent(user_agent: str | None) -> str:
    """Return a canonical tool key for `user_agent`.

    - Whitelist rule hit → stable canonical key (claude-code / opencode / …).
    - A present-but-unmatched UA → dynamic key extracted from the header.
    - No UA at all → AGENT_UNKNOWN (`其它`).
    """
    if not user_agent or not user_agent.strip():
        return AGENT_UNKNOWN
    ua = user_agent.lower()
    for key, needles in _RULES:
        if any(n in ua for n in needles):
            return key
    token = _extract_ua_token(ua)
    return token if token else AGENT_UNKNOWN


def resolve_agent(
    user_agent: str | None,
    ua_rules: dict[str, str] | None,
) -> str:
    """Entry-point resolution: user-defined UA→platform rules first.

    `ua_rules`（upstreams.json 顶层 `ua_rules`）是用户在设置页手配的
    「整串 UA → 平台名」映射，**优先级最高**：一个 UA 只要精确命中
    （去首尾空白、大小写敏感），就归用户指定的平台，跳过白名单 / 动态
    抽 token / 兜底桶。未命中或没配置规则时回落到 `sniff_agent`。

    返回的平台名直接落库 `requests.agent`，总览 / 统计天然按新平台显示。
    """
    if user_agent and ua_rules:
        hit = ua_rules.get(user_agent.strip())
        if hit and hit.strip():
            return hit.strip()
    return sniff_agent(user_agent)
