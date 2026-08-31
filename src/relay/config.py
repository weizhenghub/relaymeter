"""Pydantic Settings for the relay — all knobs come from env vars.

Multi-API configuration
-----------------------

Each platform (anthropic / openai / openclaw) accepts a list of named
upstreams. At runtime exactly one upstream per platform is "active";
the proxy uses its URL (and optionally its API key, if configured) for
that platform's traffic.

Two ways to declare that list, in order of precedence:

1. **``upstreams.json``** (v0.8, preferred) — a hand-editable JSON file;
   see :mod:`relay.upstreams_file`. Read by :meth:`Settings.load`.
2. **``*_UPSTREAMS`` env vars** — a single-line JSON list per platform.
   Still supported, used whenever the file is missing or has no section
   for that platform.

The env form::

    ANTHROPIC_UPSTREAMS='[
      {"name":"prod",    "url":"https://api.anthropic.com"},
      {"name":"minnimax","url":"https://minnimax.chat",
       "api_key":"sk-xxx", "auth_header":"x-api-key"}
    ]'
    ANTHROPIC_ACTIVE=prod

    OPENAI_UPSTREAMS='[{"name":"openai","url":"https://api.openai.com"}]'
    OPENAI_ACTIVE=openai

`api_key` is optional. If present, the relay REPLACES the client's
`auth_header` (default `x-api-key` for Anthropic-compatible,
`authorization` for OpenAI-compatible) with the value the client would
have sent. The client's own header is dropped — useful for sharing a
single key among multiple tools.

If `api_key` is absent, the relay passes the client's auth header
through untouched (legacy transparent-proxy behavior).

Backward compat
---------------

If `*_UPSTREAMS` is unset but the legacy `*_UPSTREAM` single value is
set, the relay synthesizes a one-entry list ``[{"name":"default",
"url":"<value>"}]``. The legacy `RELAY_REQUIRE_AUTH_TOKEN` env var
still gates the relay-to-relay auth header.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import warnings
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


log = logging.getLogger("relay.config")


def _deprecated(replacement: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """v0.117 起：标记 mutator 函数为「Phase 2 起走 ctx.mutators」，仅打 warning 不抛错。

    中继必须能继续跑（旧 GUI / 旧 router 还要工作），所以用
    ``warnings.warn`` 而不是抛异常。Plugin 作者/横向模块应改用
    ``ctx.mutators.<name>``（见 ``docs/architecture/STABILITY.md``）。
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        msg = (
            f"relay.config.{fn.__name__} 是 Phase 0 标记为 deprecated 的内部 mutator；"
            f"新代码请改用 {replacement}（详见 docs/architecture/STABILITY.md §2.3）。"
        )

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
            return fn(*args, **kwargs)

        wrapper.__deprecated__ = True  # type: ignore[attr-defined]
        return wrapper

    return deco


# Auth header used by Anthropic / Anthropic-compatible APIs.
ANTHROPIC_AUTH_HEADER = "x-api-key"
# Auth header used by OpenAI / OpenAI-compatible APIs.
OPENAI_AUTH_HEADER = "authorization"

# Every platform the relay routes, stats, and configures. Canonical home
# for this tuple — routers, GUI, CLI and the upstreams file all import it
# from here so adding a platform is one edit, not five.
# OpenClaw is absent on purpose: it speaks the Anthropic wire format and
# goes through /anthropic, so it needs no platform of its own.
PLATFORMS: tuple[str, ...] = ("anthropic", "openai")


# ---- v0.12 wire 常量（docs/wire-dispatch-plan.md §3）----
# wire = 上游线协议。平台（客户端入口协议族）与 wire 是两个概念：
# 平台看 URL 前缀（/anthropic、/openai），wire 看上游线协议长相。
WIRE_ANTHROPIC_MESSAGES = "anthropic-messages"
WIRE_OPENAI_CHAT = "openai-chat"
WIRE_OPENAI_RESPONSES = "openai-responses"
KNOWN_WIRES: tuple[str, ...] = (
    WIRE_ANTHROPIC_MESSAGES, WIRE_OPENAI_CHAT, WIRE_OPENAI_RESPONSES,
)
KNOWN_AUTH_STYLES: tuple[str, ...] = ("bearer", "x-api-key", "none")

# ---- v0.98 插件平台：可扩展 wire 注册表 ----
# 插件（relay.plugin 加载）可注册新 wire 的默认端点/认证风格。内置三个
# wire 常驻 KNOWN_WIRES；插件注册的进 _EXTRA_WIRE_DEFAULTS。校验点统一走
# is_known_wire()，默认值查询统一走 all_wire_defaults() —— 否则插件 wire
# 会在 effective_endpoint / effective_auth_style / 校验处 KeyError。
_EXTRA_WIRE_DEFAULTS: dict[str, dict[str, str]] = {}


def register_extra_wire(name: str, *, endpoint: str, auth_style: str = "bearer") -> None:
    """插件注册新 wire 的默认端点与认证风格（同名覆盖）。"""
    if not name or not isinstance(name, str):
        raise ValueError("wire 名必须是非空字符串")
    _EXTRA_WIRE_DEFAULTS[name] = {"endpoint": endpoint, "auth_style": auth_style}


def extra_wires() -> tuple[str, ...]:
    return tuple(_EXTRA_WIRE_DEFAULTS)


def is_known_wire(wire: str) -> bool:
    """内置 + 插件注册的 wire 都算已知。"""
    return wire in KNOWN_WIRES or wire in _EXTRA_WIRE_DEFAULTS


def all_wire_defaults() -> dict[str, dict[str, str]]:
    """内置 + 插件注册的 wire 默认值合并视图（插件同名不覆盖内置）。"""
    merged = dict(WIRE_DEFAULTS)
    merged.update(_EXTRA_WIRE_DEFAULTS)
    return merged


# 每个 wire 的默认端点 / 默认认证风格 / 协议必带请求头。
WIRE_DEFAULTS: dict[str, dict[str, str]] = {
    WIRE_ANTHROPIC_MESSAGES: {
        "endpoint": "/v1/messages",
        "auth_style": "x-api-key",
    },
    WIRE_OPENAI_CHAT: {
        "endpoint": "/v1/chat/completions",
        "auth_style": "bearer",
    },
    WIRE_OPENAI_RESPONSES: {
        "endpoint": "/v1/responses",
        "auth_style": "bearer",
    },
}


def infer_wire_for_platform(platform: str) -> str:
    """wire 未在 upstreams.json 里声明时的兜底推断：按平台段推。

    anthropic 段 → anthropic-messages，openai 段 → openai-chat。这与
    v0.11 的隐式约定一致（段内上游必须说该平台协议），所以存量配置
    迁移后行为零变化。
    """
    return WIRE_OPENAI_CHAT if platform == "openai" else WIRE_ANTHROPIC_MESSAGES


# v0.163：base URL 里的协议命名空间段。平台看 URL 前缀（config.py:106 设计说明），
# 但 infer_wire_for_platform 只认进门平台段，从不看上游地址 —— 于是 `dp官方`
# 这类 ``url=https://.../anthropic`` 的上游（wire=None、只说 anthropic-messages）
# 会被错判成 openai-chat，导致 opencode 的 openai 请求被当成"同 wire 直通"，
# 硬拼出 ``.../anthropic/v1/chat/completions`` 上游 404。这里补一层：wire=None
# 时先按地址命名空间认语言，认不出才退回平台段推断。
_URL_NAMESPACE_WIRE = (
    (WIRE_ANTHROPIC_MESSAGES, ("/anthropic",)),
    (WIRE_OPENAI_CHAT, ("/openai",)),
)


def infer_wire_from_url(url: str) -> Optional[str]:
    """按 base URL 末段命名空间推断上游 wire；认不出返回 None。

    ``url=https://api.deepseek.com/anthropic`` → anthropic-messages
    ``url=https://api.deepseek.com/openai``      → openai-chat
    ``url=https://minnimax.chat``（无命名空间）  → None（交给平台段推断兜底）
    """
    if not url:
        return None
    base = url.rstrip("/").lower()
    for wire, namespaces in _URL_NAMESPACE_WIRE:
        for ns in namespaces:
            if ns in base:
                return wire
    return None


# base 末段版本段：v1 / v1beta / v2 / v2beta …（可选 api/ 前缀），忽略大小写。
# 命中 → endpoint 自带的 /vN 前缀视为冗余（避免 /v1beta/v1/messages 双版本段）。
_BASE_VERSION_SEG = re.compile(r"^v\d+[a-z]*$", re.IGNORECASE)
# endpoint 开头的版本段：/v1/messages → 匹配 /v1/，/v1beta/messages → /v1beta/。
_ENDPOINT_VERSION_PREFIX = re.compile(r"^/(v\d+[a-z]*)/", re.IGNORECASE)


def join_endpoint(base_url: str, endpoint: str) -> str:
    """拼接上游端点 URL，规避 base 已带版本段时的双版本段。

    判定规则（通用，不针对某一家）：

    - **base 末段是版本段**（`v1` / `v1beta` / `v2`…，可带 `api/` 前缀，如
      `opencode.ai/zen/go/v1`、`generativelanguage.googleapis.com/v1beta`、
      `openrouter.ai/api/v1`）→ 版本信息已由 base 给出，endpoint 自带的
      `/vN` 前缀剥掉（`/v1/messages` → `/messages`），结果
      `.../v1/messages` / `.../v1beta/messages` / `.../api/v1/messages`。
    - **base 末段是命名空间段**（如 DeepSeek Anthropic 的 `/anthropic`、
      OpenAI-family 的 `/openai`）→ 命名空间不是版本段，endpoint 的 `/vN`
      保留，结果 `.../anthropic/v1/messages` —— 与 Anthropic SDK 的
      `base_url + "/v1/messages"` 约定一致。
    - **base 是根地址**（如 minnimax.chat）→ 原样拼 `/v1/messages`。

    与 proxy._anthropic_messages_url 同语义，但通用到任意端点
    （chat/completions、responses 等）。

    v0.207 补充：**base 已含完整 endpoint → 原样返回**。用户在新建上游
    时可能直接把完整端点 URL 填进 base（如
    ``https://token.sensenova.cn/v1/chat/completions``），此前会再拼一层
    ``/v1/chat/completions`` 得到 ``.../chat/completions/v1/chat/completions``
    上游 404。与主线 proxy（``_normalize_api_path`` 的 ``return ""`` 分支）
    同语义：base 以 endpoint（或去掉 /v1 前缀后的端点）结尾即视为已含。
    """
    base = base_url.rstrip("/")
    m = _ENDPOINT_VERSION_PREFIX.match(endpoint)
    last = base.rsplit("/", 1)[-1]
    if m and _BASE_VERSION_SEG.match(last):
        # 剥掉 endpoint 的 /vN/（m.end() 落在 "messages" 处），保留前导 /
        endpoint = "/" + endpoint[m.end():]
    # v0.207：base 已含完整端点 → 直接返回，不再重复拼接。
    # endpoint 去掉前导 /v1 后的裸形态（/chat/completions、/messages…）也认
    # —— base 可能是 ``.../v1/chat/completions``（带 /v1）或
    # ``.../v1beta/chat/completions``（去 /v1 后仍以 /chat/completions 结尾）。
    bare = endpoint[len("/v1"):] if endpoint.startswith("/v1/") else endpoint
    if base.endswith(endpoint) or (bare and base.endswith(bare)):
        return base
    return base + endpoint


class PlatformConfig(BaseModel):
    """平台下的一个具名上游。"""

    name: str
    url: str
    api_key: Optional[str] = None
    # api_key 非空时要覆盖的请求头。不设则按该配置挂载的平台取默认
    # ANTHROPIC_AUTH_HEADER / OPENAI_AUTH_HEADER。
    auth_header: Optional[str] = None
    # 给人编辑 upstreams.json 时看的自由文本标签（如「香港线路」、
    # 「备用 key」）。不会发往上游；/api/upstreams 会把它暴露出来，
    # 让 GUI 能显示每个节点是哪个。
    note: Optional[str] = None
    # ---- v0.19 quota / model 配置 ----
    # ``None`` 表示「不限额度」—— GUI 不显示利用率条，快照返回
    # ``utilization_5h=None``。用户在 Settings 视图切换；两者都持久化
    # 在 upstreams.json 的 url / api_key 旁边。成本模型见 tui.py 的
    # ``fetch_by_upstream_with_costs``。
    quota_5h: Optional[int] = None
    # v0.46：周 / 月额度。跟 quota_5h 一样语义：None = 不限制。
    # 当前 quota_monitor 只检查 5h 这条；week/month 字段先存进 JSON
    # 留给后续报告 / 告警用。新建上游的 GUI 表单把这三个窗口一起收。
    quota_week: Optional[int] = None
    quota_month: Optional[int] = None
    # 各模型的加权成本。key 是 ``requests.model`` 里记录的确切模型名；
    # value 是乘数（>=0）。没匹配到的模型回退 ``1.0``，让已有日志不会
    # 静默漂移。示例：{"minimax2/7": 1.0, "m3": 3.0} —— 便宜模型按 1×、
    # 贵模型按 3× 计费的线路。
    model_multipliers: dict[str, float] = {}
    # 该上游服务的模型名白名单。空列表 = "任意模型" —— 与 v0.19 之前的
    # 行为一致。已填的条目若与实际日志里出现的模型对不上，会以
    # ``unauthorized_seen`` 暴露在快照里，方便用户发现配置已经跟真实
    # 流量脱节。
    allowed_models: list[str] = []
    # v0.119：上游链接（合并统计）。同平台 peer 名字列表，把这两个上游
    # 的统计合并显示为「A<->B」（传递闭包：若 A+B 且 A+C ⇒ A+B+C）。
    # **不影响路由 / 计费 / quota 配置**，仅影响快照里的统计聚合。
    # 空列表 = 不链接（默认；与旧配置完全兼容）。
    linked_upstreams: list[str] = []
    # v0.65: 当此字段非空时，中继会在转发前把请求体里的 `model` 字段
    # 强制替换成这个值，让客户端可以发 `model: "auto"` 或任意占位字
    # 符，新接入一个上游不用同步去改客户端的模型名。
    model: Optional[str] = None
    # v0.74: "客户端永远发 auto" 场景的兜底字段。当 ``model`` 字段为
    # None、客户端又发 ``model: "auto"`` 时，中继会先看这个字段，
    # 再退回 ``allowed_models[0]`` —— 都为空才把 "auto" 原样透传给
    # 上游。优先级链:
    #   cfg.model (强制映射,无视 client)
    #   > cfg.default_model (仅 client 发 "auto" 时用)
    #   > cfg.allowed_models[0] (仅 client 发 "auto" 时用)
    #   > 原样透传 (留给上游 4xx)
    default_model: Optional[str] = None
    # v0.66: 计费模式。"count" = 按请求数 × multiplier 算消耗（默认，兼容已有配置）；
    # "token" = 按各 token 字段 × multiplier 算消耗，适合按实际 token 消耗计费的平台。
    # v0.98.3: 其它非空字符串 = 插件注册的计费器名（V0.3 扩展层），由
    # plugin.billing_unit_for 解析；未注册时回退 count 行为。
    billing_unit: str = "count"
    # 仅当 billing_unit == "token" 时生效：哪些 token 字段计入消耗。
    # key 必须是 input_tokens / output_tokens / cache_read_input_tokens /
    # cache_creation_input_tokens 之一，value = 是否计入。
    # 未出现的 key 在 merge 时取默认值（input+output=True, cache 两字段=False），
    # 所以 token 模式下只存非默认的覆盖值即可（节省 JSON 体积）。
    token_fields: dict[str, bool] = {}
    # v0.8.1: 当此字段为 True 时，proxy.py 会把 Anthropic 协议请求按
    # OpenCode Go 规范做协议适配：
    #   1. 删掉客户端的 Authorization: Bearer，换成 x-api-key: <cfg.api_key>
    #   2. model 字段做 .lower()（OpenCode anthropic 端点只接受 lowercase）
    #   3. 响应里的 thinking 块剥离，只回 text
    # 双协议上游（OpenCode Go 等）在 upstreams.json 里写 ``true``。
    requires_anthropic_adapter: bool = False
    # v0.98.3 插件扩展层：该上游指定用哪个插件适配器实现（模块/标识符）。
    # 纯元数据 —— 加载 / 序列化保留，插件注册转换器时按此值决定是否接管
    # 该上游的 wire 转换。None = 未指定。
    adapter: Optional[str] = None
    # v0.11.19 思考挡位：该上游可用的 thinking 挡位列表。空列表 = 用默认
    # （deepseek 系推断 4 挡，否则 off/enabled 两档）。值 ∈ {off, low,
    # medium, high, max}。upstreams.json 里写 thinking_options: [...]。
    thinking_options: list[str] = []
    # v0.200 该上游支持图片输入的模型名（裸模型名，OpenCode 认模型 id）。
    # 空列表 = 该上游无多模态模型（或未声明）。剥图判定合并
    # cfg.vision_models ∪ settings.vision_models（全局兜底名单）：
    # 目标模型命中任一才不剥图，否则剥图 + 注入提示词。存上游条目内，
    # 同一个模型名跨上游可分别声明（比全局名单更精确）。新建上游 GUI
    # 的「允许的模型」每行勾选框写这个字段。
    vision_models: list[str] = []
    # v0.11.18 高级切换（实验性）—— 客户端没显式指定模型（model 为空或
    # "auto"）时，先让弱模型判定该请求应走弱还是强模型，再路由：
    #   advanced_switch        开启后启用
    #   advanced_weak_model    弱模型名（默认当前活跃上游的 model）
    #   advanced_strong_platform / advanced_strong_upstream / advanced_strong_model
    #                         强目标（可跨上游/跨平台，须同协议）
    #   advanced_fallback      判定失败/超时时走 "weak" 还是 "strong"
    #   advanced_rules         规则预筛开关
    advanced_switch: bool = False
    advanced_weak_model: Optional[str] = None
    advanced_strong_platform: Optional[str] = None
    advanced_strong_upstream: Optional[str] = None
    advanced_strong_model: Optional[str] = None
    advanced_fallback: Literal["weak", "strong"] = "weak"
    advanced_rules: bool = True
    # v0.11.18 验证期 dry-run：只分类 + 记录判定，不真正切换上游/模型。
    # 开启后请求永远走当前活跃上游（弱），配合日志观察提取+分类效果。
    advanced_dry_run: bool = False
    # ---- v0.12 协议声明（docs/wire-dispatch-plan.md §3）----
    # wire = 上游线协议（anthropic-messages / openai-chat / openai-responses）。
    # None = 由加载器按平台段推断（anthropic 段→anthropic-messages，openai
    # 段→openai-chat），与 v0.11 行为一致。P0 只声明不改行为；P2/P3 转换
    # 路径按 wire 分叉，直通路径不受影响。
    wire: Optional[str] = None
    # 消息端点路径。None = 按 wire 默认（/v1/messages、/v1/chat/completions、
    # /v1/responses）。仅转换路径使用（恒发 url+endpoint）；同 wire 直通路径
    # 仍拼接客户端请求路径（/models、/count_tokens 等附属端点继续工作）。
    endpoint: Optional[str] = None
    # 认证风格：bearer | x-api-key | none。None = 由 auth_header 推导
    # （authorization→bearer，x-api-key→x-api-key，其余按 wire 默认）。
    auth_style: Optional[str] = None

    # ---- v0.12 wire 推导辅助（读时推断，不回写用户文件）----

    def effective_wire(self, platform: str) -> str:
        """该上游实际线协议。wire 未声明时按平台段推断。"""
        if is_known_wire(self.wire):
            return self.wire  # type: ignore[return-value]
        # v0.163：wire=None 时先按地址命名空间认语言（/anthropic→anthropic-messages，
        # /openai→openai-chat）。认不出才退回平台段推断，保证存量配置 zero 变化。
        url_wire = infer_wire_from_url(self.url)
        if url_wire is not None:
            return url_wire
        return infer_wire_for_platform(platform)

    def effective_endpoint(self, platform: str) -> str:
        """消息端点路径（转换路径用）。未配置时按 wire 默认。"""
        if self.endpoint:
            return self.endpoint
        return all_wire_defaults()[self.effective_wire(platform)]["endpoint"]

    def effective_auth_style(self, platform: str) -> str:
        """认证风格。优先级：auth_style（显式）> auth_header（显式）> wire 默认。

        auth_header 参与推导是安全的，因为加载器只会在一种情况下给它填
        默认值：legacy 无 wire 条目（旧格式 platform 段）套平台默认头。
        扁平格式（platform=None）从不填默认 —— auth_header 只可能是用户
        显式配置的"上游能正常请求的格式"，必须被所有路径尊重。wire 已声明
        但 auth_header 缺失时落回 wire 默认（openai→bearer, anthropic→
        x-api-key），保证不配置也能走通用约定。
        """
        if self.auth_style:
            return self.auth_style  # type: ignore[return-value]
        ah = (self.auth_header or "").lower()
        if "authorization" in ah or "bearer" in ah:
            return "bearer"
        if "x-api-key" in ah or "api-key" in ah:
            return "x-api-key"
        return all_wire_defaults()[self.effective_wire(platform)]["auth_style"]


# ---------------------------------------------------------------------------
# Project-root resolution
# ---------------------------------------------------------------------------
#
# `.env`, `upstreams.json` and `relay.db` are all default-resolved from the
# current working directory, which is a trap: a relay launched from a desktop
# shortcut, a Windows autostart entry, or PyCharm's run config can easily
# have a CWD that isn't the project root. When that happens pydantic-settings
# silently finds no .env, ``Settings.load()`` silently finds no
# upstreams.json, and the relay starts up with a single built-in
# ``default`` upstream pointing at the public API — quietly losing the
# user's local config. We resolve these paths against the project root
# (the directory holding pyproject.toml) instead, falling back to the
# CWD when the file is found there. That keeps existing test setups that
# write a fixture .env in tmp_path working unchanged.
#
# Absolute paths are passed through untouched, so a user can still point
# at a file anywhere on disk by setting RELAY_UPSTREAMS_FILE / RELAY_DB
# to a full path.
_PROJECT_ROOT: Optional[Path] = None


def _project_root() -> Path:
    """Locate the project root (directory containing ``pyproject.toml``).

    Searched once and cached. The search walks upward from this file's
    directory, so the same answer comes back whether the package is
    imported from a checkout, an editable install, or a wheel — as long
    as the source tree contains ``pyproject.toml`` (the normal case).
    Falls back to ``Path.cwd()`` when no marker is found (e.g. when
    installed as a wheel into site-packages without the source tree).

    PyInstaller 冻结态：包里没有 pyproject.toml，源码树路径失效，固定回
    ``%LOCALAPPDATA%\\Relay\\`` —— 与 .env 既定意图、gui.py 的
    ``_APP_DATA_DIR`` 一致，让 relay.db / upstreams.json / .env 落在固定
    数据目录，不随 exe 所在目录（如 Downloads）漂移。
    """
    global _PROJECT_ROOT
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        _PROJECT_ROOT = (base / "Relay").resolve()
        return _PROJECT_ROOT
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            _PROJECT_ROOT = candidate
            return _PROJECT_ROOT
    _PROJECT_ROOT = Path.cwd().resolve()
    return _PROJECT_ROOT


def _seed_default_config() -> None:
    """v0.177: frozen exe 首启时把发布默认 seed 到 LOCALAPPDATA\\Relay\\。

    行为：
      - 仅在 ``sys.frozen`` 下生效（源码 / wheel 装入 site-packages 后无
        _MEIPASS/_MEIPASS-relay/packaging 这一路径，直接早退）。
      - 目标文件存在 → 不动（用户已编辑，留给他自己的真值）。
      - 仅 seed 两份占位文件：``.env`` 留发布默认（账号字段已注释掉，
        用户补 key 后由 gui.py 写回）、``upstreams.json`` 留空骨架 +
        _comment 字段说明。
      - 复制到 ``_project_root()``（frozen 时即 LOCALAPPDATA\\Relay\\），
        与 Settings 后续 ``_resolve_config_path`` 的解析同路径，确保
        Settings.load() 能找到刚 seed 出的文件。
      - 任何 IOError / PermissionError → 静默吞（不致命；用户首次启
        动若权限不够，就走代码默认值 + 缺 upstreams.json 的退化路径，
        等同 v0.176 之前的行为）。

    ⚠ 关键约束：不覆盖 .env —— 一旦用户填了真 key 的 .env 在包里，frozen
    用户在自己机器上首次启动时也是干净的 LOCALAPPDATA，可以安全覆盖；
    但**已存在**的 .env 必须跳过（避免把用户之前手动配置的真值洗掉）。
    """
    if not getattr(sys, "frozen", False):
        return

    # frozen 下的 _MEIPASS 路径。PyInstaller bootloader 在启动时设置该属性，
    # 资源 datas=(src,dst) 把 release seed 文件拷到 _MEIPASS/relay/packaging/
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    src_dir = Path(meipass) / "relay" / "packaging"
    if not src_dir.is_dir():
        return

    target_dir = _project_root()  # frozen 时 = %LOCALAPPDATA%\Relay\
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    for name, src_name in ((".env", "relay.env.dist"),
                           ("upstreams.json", "relay.upstreams.dist.json")):
        target = target_dir / name
        if target.exists():
            continue  # 用户已配置 → 不动
        src = src_dir / src_name
        if not src.is_file():
            continue
        try:
            shutil.copy2(src, target)
        except OSError:
            # 权限不足 / 磁盘满 / 文件被占用 —— 不致命，让后续 Settings.load()
            # 自然走代码默认值 + 缺 upstreams.json 的退化路径
            pass


def _resolve_config_path(value: str) -> str:
    """Resolve a relative config path against the project root.

    Absolute paths and paths that already resolve to an existing file in
    the current working directory are returned unchanged — this preserves
    test setups that write fixtures into tmp_path and chdir there.
    Relative paths that don't exist in CWD are rewritten relative to the
    project root so the same file is found no matter how the relay was
    launched.
    """
    p = Path(value)
    if p.is_absolute():
        return str(p)
    cwd_hit = (Path.cwd() / p).resolve()
    if cwd_hit.exists():
        return str(cwd_hit)
    return str((_project_root() / p).resolve())


class Settings(BaseSettings):
    # pydantic-settings reads env_file entries left-to-right with later
    # entries winning on duplicate keys. We feed it both the CWD .env
    # (used by tests that chdir into tmp_path, and by an explicit
    # per-project setup) and the project-root .env (the common case for
    # `python main.py` from the checkout, and for autostart where CWD is
    # the user profile). The non-existent one is silently skipped, so
    # there's no error when one of them is missing.
    model_config = SettingsConfigDict(
        env_file=(str(Path(".env").resolve()), str(_project_root() / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Listen address parsed by uvicorn. Default 127.0.0.1:8088.
    relay_listen: str = "127.0.0.1:8088"

    # SQLite file path. Relative paths are resolved against the project
    # root by :func:`_resolve_config_path`, so the same DB is found no
    # matter what CWD the relay was launched from.
    relay_db: str = "./relay.db"

    # 完全透传模式：开启后所有 HTTP 请求绕过平台路由器，由
    # passthrough.middleware 直接转发。客户端在鉴权头（x-api-key 或
    # Authorization）里携带 ``url@@api-key``，中继拆开解析目标 URL 与
    # key。统计写入独立的 passthrough.db（见 relay_passthrough_db）。
    passthrough_mode: bool = False

    # 透传模式的独立 sqlite 文件。passthrough.db 与 relay.db 物理隔离，
    # 一份坏掉不影响另一份。
    relay_passthrough_db: str = "./passthrough.db"

    # v0.8: hand-editable JSON file holding the upstream lists. When it
    # exists it WINS over the `*_UPSTREAMS` / `*_ACTIVE` env vars below —
    # see relay.upstreams_file. Only consulted by `Settings.load()`, so
    # tests that construct Settings(...) directly are never affected by a
    # file sitting in the CWD.
    relay_upstreams_file: str = "./upstreams.json"

    # v0.20 quota monitor. Seconds between checks; 0 disables the
    # background task entirely. Auto-switch is opt-in: when the active
    # upstream crosses `relay_quota_switch_at` utilization the monitor
    # moves traffic to the least-utilized sibling on the same platform.
    # With it off the monitor still logs warnings, which is the safe
    # default — silently rerouting to a different vendor is not something
    # to enable behind the user's back.
    relay_quota_check_interval: int = 60
    relay_quota_autoswitch: bool = False
    # v0.90：阈值写死为 0.7（剩余 30%）。前端"耗尽后/即将耗尽"双态
    # 文案的触发边界与 quota_monitor 实际切换时机同步。仍走 .env 可
    # 覆盖（RELAY_QUOTA_SWITCH_AT），但设置页 UI 不再暴露。
    relay_quota_switch_at: float = 0.7
    # v0.11.3: 自动切换的允许池 —— 空列表 = 平台内所有兄弟都可作为切换
    # 目标（旧行为）。非空时 quota_monitor 只会在池子列出的上游里选新
    # 目标，用户可以用设置页勾选哪些 API 允许被自动接管。
    relay_autoswitch_pool: list[str] = []

    # v0.11.18 高级切换（实验性）—— 顶层 advanced_switch 配置，存
    # upstreams.json，由 upstreams_file.apply_to_settings() 读进来。
    # enabled / weak_model / strong_model / analysis_model 从模型清单选；
    # strong_types 是需要发到强模型的类型多选；aggressive=True 时判定
    # 模糊优先强模型，否则保守用弱；learning 是"历史学习改进"预留开关。
    advanced_switch: bool = False
    # 三个模型的 (upstream, model) 对 —— 同名模型可能属于多个上游
    # （如 f3af39d7 / e1701fa6 都是 MiniMax-M3），必须靠 upstream 精确定位。
    advanced_weak_upstream: Optional[str] = None
    advanced_weak_model: Optional[str] = None
    advanced_strong_upstream: Optional[str] = None
    advanced_strong_model: Optional[str] = None
    advanced_analysis_upstream: Optional[str] = None
    advanced_analysis_model: Optional[str] = None
    advanced_strong_types: list[str] = []
    advanced_aggressive: bool = False
    advanced_learning: bool = False

    # v0.113o 报错分析 —— 顶层 error_analysis 配置，存 upstreams.json，
    # 由 upstreams_file.apply_to_settings() 读进来。GUI 进程在后台把
    # relay.db 里新出现的请求错误发给所选小模型，判断错误类型并给用户
    # 中文提示（余额耗尽 / 网络错误 / 达到次数限制等）。upstream / model
    # 从模型清单里选，upstream 为空时按 model 兜底定位。
    error_analysis_enabled: bool = False
    error_analysis_upstream: Optional[str] = None
    error_analysis_model: Optional[str] = None

    # v0.188 支持图片的模型 —— 顶层 vision_models 配置，存 upstreams.json，
    # 由 upstreams_file.apply_to_settings() 读进来。OpenCode 读
    # /models/api.json 的 modalities.input 判断模型能否收图；只有列表里的
    # 模型名才被 relay 标成 image-capable，其余保持纯文本（image 不标）。
    # 存裸模型名（OpenCode 认模型 id），不区分上游。
    vision_models: list[str] = []

    # Per-platform upstream lists, parsed from JSON env vars.
    # Populated by _parse_upstreams(); left as None here so pydantic
    # doesn't try to JSON-decode them twice.
    anthropic_upstreams: Optional[list[PlatformConfig]] = None
    openai_upstreams: Optional[list[PlatformConfig]] = None
    openclaw_upstreams: Optional[list[PlatformConfig]] = None

    # v0.69 sidebar "快捷切换" 按钮组 —— 每条预绑一个
    # (platform, upstream, model) 三元组。值由
    # upstreams_file.apply_to_settings() 从 upstreams.json 顶层
    # ``quick_switch`` 数组里读进来，文本写死在配置文件里。snapshot
    # 直接转发给前端，前端在 sidebar 里"平台下拉"下方渲染成一排按钮。
    quick_switch: Optional[list[dict[str, Any]]] = None

    # v0.155/v0.159：客户端工具名 → 自定义显示名 + 颜色 的别名映射。
    # 值是对象 ``{name: str, color: str|None}``（v0.159 升级：之前是
    # 字符串值，仅 name），读侧 upstreams_file.apply_to_settings 自动从
    # 旧 string 升级。name 是显示名（覆盖 agent 原值）；color 是用户
    # 选的徽标底色（CSS color 字符串，可空 —— 空时落回 css 默认配色）。
    # 值由 upstreams_file.apply_to_settings() 从 upstreams.json 顶层
    # ``agent_aliases`` 对象读进来。纯展示层，不参与路由 / 计费。
    agent_aliases: Optional[dict[str, dict[str, Optional[str]]]] = None

    # v0.157：用户手配的「整串 UA → 平台名」归类规则。值由
    # upstreams_file.apply_to_settings() 从 upstreams.json 顶层 ``ua_rules``
    # 对象读进来。**中继进程在请求入口消费**（resolve_agent 精确匹配），
    # 命中时平台名直接落库 requests.agent。GUI 经桥读写，写后必须 POST
    # /api/upstreams/refresh 通知中继子进程 reload（否则新规则不生效）。
    ua_rules: Optional[dict[str, str]] = None

    # Active upstream NAME per platform. The proxy looks up the named
    # entry in the list above at request time.
    anthropic_active: str = "default"
    openai_active: str = "default"
    openclaw_active: str = "default"

    # v0.12：单池单 active —— 扁平上游列表 + 单一 active 名，取代 per-platform
    # 分组。由 upstreams_file.apply_to_settings() 从扁平 upstreams.json 填充；
    # 纯 env 配置（无 upstreams.json）时为空，方法里回退到 per-platform 字段。
    upstreams: list[PlatformConfig] = []
    active: str = ""

    # Legacy single-upstream env vars. Kept so old .env files still work;
    # synthesized into a one-entry list during _parse_upstreams().
    anthropic_upstream: str = "https://api.anthropic.com"
    openai_upstream: str = "https://api.openai.com"
    openclaw_upstream: str = "https://minnimax.chat"

    # v0.4: persist request bodies + assembled responses in the `messages`
    # table so the user can search past conversations. Doubles DB size on
    # average. Set RELAY_SAVE_MESSAGES=0 to disable.
    relay_save_messages: bool = True

    # Optional relay-to-relay auth: if non-empty, requests must include
    # `Authorization: Bearer <value>` or `X-Relay-Token: <value>`.
    relay_require_auth_token: str = ""

    # GUI night-mode preference. Read from RELAY_GUI_THEME in .env; the GUI
    # writes it back when the user toggles the theme button. Unknown
    # values (typo, "auto", …) are tolerated by the GUI and treated as
    # light, but they are still stored verbatim here.
    # v0.177：发布默认改为「day」暖白主题，与发布标准的「我的偏好」一致。
    relay_gui_theme: str = "day"

    # v0.11.17：#6 启动后隐藏窗口 —— 登录启动时直接缩到托盘、不弹窗。
    relay_gui_start_hidden: bool = False

    # v0.11.21：GUI "内外转换显示"开关 —— 开启后 topbar 状态条右侧实时
    # 显示 对内（客户端→中继）模型 与 对外（中继→上游）模型的映射。
    # v0.177：发布默认 ON（与发布标准的「我的偏好」一致）。
    relay_gui_show_io_map: bool = True

    # v0.89：实时流侧栏（live panel）开关 —— 开启后 GUI 启动多建一个
    # webview 窗口，贴在主窗右侧；展示最近一次模型调用的全貌（上游 /
    # 模型 / api-key / 入向→出向 wire / 内容流 / token）。同时支持
    # 顶栏 🎬 快捷按钮。.env 键：RELAY_GUI_LIVE_PANEL=1/0。
    # v0.177：发布默认 ON（与发布标准的「我的偏好」一致）。
    relay_gui_live_panel: bool = True

    # v0.113c：实时流侧栏「无边框」—— 开启后侧栏所有卡片去掉玻璃容器
    # （背景/边框/阴影），裸悬浮在窗口底背景上。与主窗总览页「无边框」
    # 开关互相独立。.env 键：RELAY_GUI_PANEL_FRAMELESS=1/0。
    relay_gui_panel_frameless: bool = False

    # v0.104：实时栏管理三件套。
    # - relay_gui_live_panel_concurrent：是否允许多个实时栏同时弹出。
    #   关闭 → 跟 v0.89 一样永远只有 1 个；开启 → 多个并发请求各自
    #   跟一个实时栏。.env 键：RELAY_GUI_LIVE_PANEL_CONCURRENT=1/0。
    relay_gui_live_panel_concurrent: bool = True
    # - relay_gui_live_panel_max：同时存在的实时栏上限（含「始终开启」
    #   那 1 个）。1-8，越界 clamp。.env 键：RELAY_GUI_LIVE_PANEL_MAX。
    relay_gui_live_panel_max: int = 3
    # - relay_gui_live_panel_always_one：始终保留一个实时栏（即使无请
    #   求）。与「允许多个」互相独立 —— 关掉多并发也能保留这一个。
    #   .env 键：RELAY_GUI_LIVE_PANEL_ALWAYS_ONE=1/0。
    #   v0.104：默认改为 False —— 用户要求"没流时完全隐藏"（之前默认
    #   True 导致 idle 时 also_one 一直 idle 可见，违反此语义）。如需
    #   旧行为，把 .env 里 RELAY_GUI_LIVE_PANEL_ALWAYS_ONE=1 即可。
    relay_gui_live_panel_always_one: bool = True
    # - relay_gui_live_panel_auto_extend：自动延展侧栏（宽度）—— 并发请
    #   求多时，实时栏窗口按容器列数自动加宽，让更多并发同时可见；关掉
    #   则保持单列，并发在列内纵向拼接 + 滚动。v0.130 新增。
    #   .env 键：RELAY_GUI_LIVE_PANEL_AUTO_EXTEND=1/0。
    relay_gui_live_panel_auto_extend: bool = True
    # - relay_gui_float_ball：悬浮球 —— 桌面可拖动小圆球作为侧栏锚点，
    #   侧栏出现时从球位置展开（替代贴主窗右缘 dock）。v0.165 新增。
    #   .env 键：RELAY_GUI_FLOAT_BALL=1/0。
    #   v0.177：发布默认 ON（与发布标准的「我的偏好」一致）。
    relay_gui_float_ball: bool = True
    # - relay_gui_float_ball_x/y：悬浮球最后位置（屏幕坐标，**逻辑像素**，与侧栏
    #   几何 / _screen_size 同口径，仅在 UpdateLayeredWindow 边界乘 scale 转物理）。
    #   球被拖动时实时写回，重启后回到上次位置。-32000 表示从未定过
    #   （未显示/未拖过）。.env 键：RELAY_GUI_FLOAT_BALL_X / _Y。
    relay_gui_float_ball_x: int = -32000
    relay_gui_float_ball_y: int = -32000
    # - relay_gui_float_ball_topmost：悬浮球置顶 —— 球与侧栏一起保持
    #   WS_EX_TOPMOST（始终置顶、同层级）。v0.170 新增。关掉后球与侧栏
    #   一起降到普通层级（不再盖住其它窗口）。.env 键：RELAY_GUI_FLOAT_BALL_TOPMOST=1/0。
    relay_gui_float_ball_topmost: bool = True

    # v0.111：实时栏「阶段感知 stale 超时」三档（秒）—— 上游流式静默
    # 超过对应档位阈值 → 判定连接中断（清窗 / 推超时 done）。设置页
    # 「实时栏管理」里可改，GUI 即时生效 + 写回 .env。
    # - relay_live_panel_thinking_timeout：思考出字中 —— 有思考无正文，
    #   可能只是思考暂停。.env 键：RELAY_LIVE_PANEL_THINKING_TIMEOUT。
    #   v0.177：发布默认 3.0（与发布标准的「我的偏好」一致）。
    relay_live_panel_thinking_timeout: float = 3.0
    # - relay_live_panel_gap_timeout：思考→正文衔接 —— 思考已完成（收到
    #   content_block_stop / </think>）但正文还没出，静默即衔接卡死。
    #   .env 键：RELAY_LIVE_PANEL_GAP_TIMEOUT。
    #   v0.177：发布默认 7.0（与发布标准的「我的偏好」一致）。
    relay_live_panel_gap_timeout: float = 7.0
    # - relay_live_panel_text_timeout：正文出字中 —— 正文一旦开始通常稳定
    #   输出，静默很短就可判定。.env 键：RELAY_LIVE_PANEL_TEXT_TIMEOUT。
    #   v0.177：发布默认 5.0（与发布标准的「我的偏好」一致）。
    relay_live_panel_text_timeout: float = 5.0
    # - relay_live_panel_done_clear_timeout：请求 done/error 后容器保留该秒数
    #   自动清除（可读窗口），到点由 Python 调度 relayLiveClear。v0.134 新增，
    #   替代原 panel_pool 硬编码 destroy_after_done_sec=10。.env 键：
    #   RELAY_LIVE_PANEL_DONE_CLEAR_TIMEOUT。
    #   v0.177：发布默认 5.0（与发布标准的「我的偏好」一致）。
    relay_live_panel_done_clear_timeout: float = 5.0

    # v0.134：实时栏侧栏前端可调项（走设置页，写回 .env）。
    # - relay_gui_live_panel_tools_clear_timeout：每张工具卡各自的自动清除间隔
    #   （秒），v0.141 默认 10 → 5（独立计时，各自到期各自删）。.env 键：
    #   RELAY_GUI_LIVE_PANEL_TOOLS_CLEAR_TIMEOUT。
    relay_gui_live_panel_tools_clear_timeout: float = 5.0
    # - relay_gui_live_panel_tools_cap：工具调用最多保留条数，默认 30。
    #   .env 键：RELAY_GUI_LIVE_PANEL_TOOLS_CAP。
    relay_gui_live_panel_tools_cap: int = 30
    # - relay_gui_live_panel_ep_list_vh：端点并发列表最大高度（vh 单位，默认 30）。
    #   .env 键：RELAY_GUI_LIVE_PANEL_EP_LIST_VH。
    relay_gui_live_panel_ep_list_vh: float = 30.0

    # v0.135：实时栏侧栏扩展可调项（开关/档位/数字）。
    # - relay_gui_live_panel_tools_always：工具容器常驻 —— 开启后即使无工
    #   具调用也保留占位（默认 False，与 v0.130「无 tool 整卡隐藏」一致）。
    #   .env 键：RELAY_GUI_LIVE_PANEL_TOOLS_ALWAYS。
    relay_gui_live_panel_tools_always: bool = False
    # - relay_gui_live_panel_min_cols：最小列数（auto_extend 模式下也至少
    #   开这么多列，1-6，默认 1 即行为不变）。.env 键：
    #   RELAY_GUI_LIVE_PANEL_MIN_COLS。
    relay_gui_live_panel_min_cols: int = 1
    # - relay_gui_live_panel_list_font：端点并发列表字号档位（"small" /
    #   "medium" / "large" 三档，默认 "medium"）。.env 键：
    #   RELAY_GUI_LIVE_PANEL_LIST_FONT。
    relay_gui_live_panel_list_font: str = "medium"

    @model_validator(mode="before")
    @classmethod
    def _parse_upstreams(cls, values: dict) -> dict:
        """Decode `*_UPSTREAMS` JSON env vars and fill in missing lists
        from the legacy `*_UPSTREAM` single-value env var.
        """
        legacy = {
            "anthropic": values.get("anthropic_upstream", "https://api.anthropic.com"),
            "openai": values.get("openai_upstream", "https://api.openai.com"),
            "openclaw": values.get("openclaw_upstream", "https://minnimax.chat"),
        }
        for plat, default_url in legacy.items():
            key = f"{plat}_upstreams"
            raw = values.get(key)
            parsed: list[dict] = []
            if isinstance(raw, str) and raw.strip():
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"env {key.upper()} is not valid JSON: {exc}"
                    ) from exc
                if not isinstance(obj, list):
                    raise ValueError(
                        f"env {key.upper()} must be a JSON list of objects"
                    )
                parsed = obj
            elif isinstance(raw, list):
                parsed = raw  # type: ignore[unreachable]
            else:
                # Synthesize from legacy single value.
                parsed = [{"name": "default", "url": default_url}]
            values[key] = [
                PlatformConfig.model_validate(item) for item in parsed
            ]
            # Pick a default auth_header per platform if not set.
            default_header = (
                ANTHROPIC_AUTH_HEADER if plat != "openai" else OPENAI_AUTH_HEADER
            )
            for cfg in values[key]:
                # 声明了 wire 的条目认证跟 wire 走，不套平台默认头。
                if cfg.auth_header is None and cfg.wire is None:
                    cfg.auth_header = default_header
        return values

    @property
    def host(self) -> str:
        return self.relay_listen.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.relay_listen.rsplit(":", 1)[1])

    @property
    def base_url(self) -> str:
        """Base URL a *client on this machine* uses to reach the relay.

        Wildcard bind addresses are rewritten to loopback: ``0.0.0.0`` and
        ``::`` mean "listen on every interface", which is not an address
        you can connect to. Anything else (including a specific LAN IP)
        is used as-is.
        """
        host = self.host.strip("[]")
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        # IPv6 literals need brackets in a URL authority.
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    # ---- Construction ----

    @classmethod
    def load(cls, upstreams_file: Optional[str] = None) -> "Settings":
        """Build Settings from env/.env, then overlay `upstreams.json`.

        This — not the bare constructor — is what every entry point should
        call. Keeping the file overlay out of the validator means
        ``Settings(anthropic_upstreams=[...])`` stays a pure, hermetic
        constructor: tests and callers that pass explicit configs never get
        them silently replaced by whatever JSON file happens to be in the
        working directory.

        The upstreams file path (whether the default ``./upstreams.json`` or
        one passed in) and ``relay_db`` are anchored to the project root
        before the file is read, so a relay launched from any CWD — desktop
        shortcut, autostart, PyCharm run config — finds the same config as
        one launched from the project root.
        """
        from .upstreams_file import apply_to_settings

        settings = cls()
        settings.relay_upstreams_file = _resolve_config_path(settings.relay_upstreams_file)
        settings.relay_db = _resolve_config_path(settings.relay_db)
        settings.relay_passthrough_db = _resolve_config_path(settings.relay_passthrough_db)
        apply_to_settings(settings, upstreams_file)
        return settings

    # ---- v0.12 单池单 active 查找（platform 参数保留但不再用于选池）----

    def upstreams_for(self, platform: str | None = None) -> list[PlatformConfig]:
        """返回单一上游池。扁平 upstreams.json 填充 self.upstreams；纯 env
        配置回退到 per-platform 列表合并（旧行为兼容）。"""
        if self.upstreams:
            return self.upstreams
        return (self.anthropic_upstreams or []) + (self.openai_upstreams or [])

    def find_upstream(self, platform: str | None, name: str) -> PlatformConfig | None:
        """按名查上游（单池内），或 None。"""
        for c in self.upstreams_for(platform):
            if c.name == name:
                return c
        return None

    def active_config(self, platform: str | None = None) -> PlatformConfig:
        """返回当前选中的（单）上游。找不到命名项时回退到第一个，改名不致崩。"""
        cfgs = self.upstreams_for(platform)
        if not cfgs:
            raise RuntimeError("no upstreams configured")
        wanted = self.active or getattr(self, f"{platform}_active", "") if platform else self.active
        for c in cfgs:
            if c.name == wanted:
                return c
        if wanted:
            log.warning("active=%r not found, falling back to %r", wanted, cfgs[0].name)
        return cfgs[0]

    def active_for(self, platform: str | None = None) -> str:
        """返回当前选中上游的 NAME（单池）。"""
        return self.active_config(platform).name

    def set_active(self, platform: str | None, name: str) -> PlatformConfig:
        """选一个新的 active 上游（单池，platform 参数忽略）。返回解析后的配置。

        就地改 Settings，运行进程不用重启即生效；HTTP API 另持久化到文件。
        """
        cfgs = self.upstreams_for(platform)
        match = next((c for c in cfgs if c.name == name), None)
        if match is None:
            raise KeyError(
                f"no upstream named {name!r}; known: {[c.name for c in cfgs]}"
            )
        self.active = name
        return match


_settings: Optional[Settings] = None


def default_thinking_options(model: str) -> list[str]:
    """兜底思考挡位：一律只允许 off。

    只有**显式声明 thinking_options** 的 opencode 上游（go/zen）才启用
    思考；其它上游不声明即完全不支持（只有"关闭"），thinking 标签绝不
    注入到未知模型 —— 不污染其它模型，也不会有"上游不认 thinking 而
    400"的问题。
    """
    return ["off"]


def get_settings() -> Settings:
    """Cached settings accessor."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reload_settings() -> Settings:
    """Force-reload settings from env. Used by the upstreams API after a
    .env write so subsequent calls see the new value."""
    global _settings
    _settings = Settings.load()
    return _settings


def update_env_var(key: str, value: str) -> bool:
    """Set ``key=value`` in the project-root `.env` file.

    Replaces the matching ``<key>=`` line in place when present, otherwise
    appends. Other lines are preserved verbatim. Returns True on a
    successful write, False if the file is locked or the caller doesn't
    have write permission — callers decide how to surface that (the
    settings API reloads after success; the GUI shows a warning).

    Always operates against the project-root `.env` (see `_project_root`)
    rather than the CWD, so a relay launched from a desktop shortcut,
    autostart, or PyCharm run config still edits the right file.
    """
    env_path = _project_root() / ".env"
    try:
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        new_line = f"{key}={value}"
        prefix = f"{key}="
        for i, line in enumerate(lines):
            if line.strip().startswith(prefix):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        env_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


@_deprecated("ctx.mutators.save_upstreams_json")
def save_upstreams_json(
    settings: "Settings",
    mutator: Callable[[dict], None],
    *,
    path: Optional[str] = None,
) -> tuple[bool, str]:
    """Atomically rewrite ``upstreams.json`` after running ``mutator`` on
    the parsed dict.

    Used by the v0.19 GUI quota editor to persist ``quota_5h`` /
    ``model_multipliers`` / ``allowed_models`` alongside the existing
    url / api_key fields, without going through `.env` (which is a
    flat key=value file and can't hold nested structures).

    The flow is:
    1. Read current JSON (or start from a per-platform skeleton when
       the file is missing — matches ``upstreams_file.py``'s bootstrap).
    2. Run ``mutator(data)`` so the caller can mutate in place.
    3. Validate every upstream through ``PlatformConfig`` — if any
       fails, abort without touching the file.
    4. Write to ``path + ".tmp"`` and ``os.replace`` for atomicity.
       The user never sees a half-written config.

    Returns ``(ok, message)``: ``(True, str(path))`` on success,
    ``(False, error_text)`` on validation / IO failure. The caller
    surfaces this to the GUI as ``{"ok": True/False, "error": "..."}``.
    """
    target = path or settings.relay_upstreams_file
    target_path = Path(target)
    try:
        try:
            data = json.loads(target_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            data = {}
        except (OSError, json.JSONDecodeError):
            return False, f"无法读取 {target_path}: 文件存在但不是合法 JSON"

        # Caller mutates in place. ``upstreams_file.py`` already validates
        # on load, but we re-validate post-mutation so a bad quota value
        # can't poison the file.
        mutator(data)

        # Validate every upstream in the file. v0.12 扁平格式
        # {"active": str, "upstreams": [...]}；旧格式 {"platform": {...}}。
        flat_list = data.get("upstreams") if isinstance(data, dict) else None
        if isinstance(flat_list, list):
            for entry in flat_list:
                try:
                    PlatformConfig.model_validate(entry)
                except Exception as exc:
                    return False, f"{entry.get('name', '?')} 校验失败: {exc}"
        else:
            for plat, body in data.items():
                if not isinstance(body, dict):
                    continue
                upstreams = body.get("upstreams") or []
                for entry in upstreams:
                    try:
                        PlatformConfig.model_validate(entry)
                    except Exception as exc:
                        return False, f"{plat}/{entry.get('name', '?')} 校验失败: {exc}"

        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, target_path)
        except OSError as exc:
            return False, f"写入失败: {exc}"
    except Exception as exc:
        return False, f"未预期错误: {exc}"
    from .plugin import emit_event
    emit_event("config.upstreams_changed", path=str(target_path))
    return True, str(target_path)


@_deprecated("ctx.mutators.apply_quota_edit")
def apply_quota_edit(
    settings: "Settings",
    platform: str,
    name: str,
    payload: dict,
) -> tuple[bool, str]:
    """Validate and persist one upstream's v0.20 quota fields.

    Shared by the GUI bridge (``gui.Api.update_upstream_quota``) and the
    control API (``PUT /api/upstreams/{platform}/{name}/quota``) so both
    entry points enforce identical rules — a value the GUI rejects can't
    sneak in over HTTP.

    ``payload`` keys, all optional:
      ``quota_5h``           positive int, or ``None`` to clear the limit
      ``model_multipliers``  ``{model: factor}``, factor ≥ 0
      ``allowed_models``     list of model-name strings
      ``model``              optional string; ``None`` clears the override
      ``default_model``      v0.74: "auto" 兜底模型名; ``None`` clears it
      ``linked_upstreams``   v0.119: 同平台 peer 上游名列表；空 list 表示不链接

    Returns ``(ok, message)`` — message is the written path on success,
    a human-readable Chinese error otherwise. Does **not** reload
    settings; the caller decides when to rebind (the GUI and the HTTP
    app hold the Settings instance in different places).
    """
    if platform not in PLATFORMS:
        return False, f"未知平台: {platform}"
    if not isinstance(payload, dict):
        return False, "payload 必须是对象"

    quota_v = payload.get("quota_5h", None)
    if quota_v is not None:
        if not isinstance(quota_v, int) or isinstance(quota_v, bool):
            return False, "quota_5h 必须是整数或 null"
        if quota_v < 0:
            return False, "quota_5h 不能为负"

    mults = payload.get("model_multipliers", {})
    if not isinstance(mults, dict):
        return False, "model_multipliers 必须是对象"
    for k, v in mults.items():
        if not isinstance(k, str) or not k:
            return False, "model name 必须是非空字符串"
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return False, f"multiplier {k!r} 必须 ≥ 0"

    allowed = payload.get("allowed_models", [])
    if not isinstance(allowed, list) or not all(isinstance(m, str) for m in allowed):
        return False, "allowed_models 必须是字符串列表"

    model_v = payload.get("model", None)
    if model_v is not None and (not isinstance(model_v, str) or not model_v.strip()):
        return False, "model 必须是非空字符串或 null"

    default_model_v = payload.get("default_model", None)
    if default_model_v is not None and (
        not isinstance(default_model_v, str) or not default_model_v.strip()
    ):
        return False, "default_model 必须是非空字符串或 null"

    billing_unit = payload.get("billing_unit", None)
    if billing_unit is not None and (
        not isinstance(billing_unit, str) or not billing_unit.strip()
    ):
        return False, "billing_unit 必须是非空字符串（count / token / 插件计费器名）"

    token_fields_raw = payload.get("token_fields", None)
    _KNOWN_TOKEN_FIELDS = {
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    }
    if token_fields_raw is not None:
        if not isinstance(token_fields_raw, dict):
            return False, "token_fields 必须是对象"
        for k, v in token_fields_raw.items():
            if k not in _KNOWN_TOKEN_FIELDS:
                return False, f"token_fields 的 key 必须是 {sorted(_KNOWN_TOKEN_FIELDS)} 之一"
            if not isinstance(v, bool):
                return False, f"token_fields[{k}] 必须是 bool"

    # v0.119：linked_upstreams —— 同平台 peer 上游名列表。空 list 表示
    # 不链接；传 None 时不动（保留 JSON 旧值）。字符串内部允许空格 / 空串
    # 被 trim 后去重（与 allowed_models 同款），但同平台不存在的 name 暂
    # 不在这里校验 —— 加载器 _coerce_entry 会按 warn + 忽略处理，避免
    # 设置页编辑时死锁「先存 A 才能存 B」的鸡生蛋顺序。
    linked_v = payload.get("linked_upstreams", None)
    if linked_v is not None:
        if not isinstance(linked_v, list) or not all(
            isinstance(m, str) for m in linked_v
        ):
            return False, "linked_upstreams 必须是字符串列表"
        # 不能把自己加进去 —— UI 永远不会让选，但恶意 payload 仍能触发；
        # 这里 hard-stop，省得 group 永远空集时 union-find 误判。
        if name in [s.strip() for s in linked_v if isinstance(s, str) and s.strip()]:
            return False, "linked_upstreams 不能包含自己"

    def _mutate(data: dict) -> None:
        # v0.12 扁平格式。旧格式就地迁移。
        if not isinstance(data.get("upstreams"), list):
            merged: list = []
            for plat in PLATFORMS:
                section = data.get(plat)
                if isinstance(section, dict) and isinstance(section.get("upstreams"), list):
                    merged.extend(section["upstreams"])
                    data.pop(plat, None)
            data["upstreams"] = merged
            data.setdefault("active", "")
        for entry in data["upstreams"]:
            if entry.get("name") == name:
                entry["quota_5h"] = quota_v
                entry["model_multipliers"] = {str(k): float(v) for k, v in mults.items()}
                entry["allowed_models"] = list(allowed)
                entry["model"] = model_v.strip() if isinstance(model_v, str) and model_v.strip() else None
                # v0.74 default_model: payload 不带这个字段时(None)清掉,
                # 跟 model 字段同款"未传则清"语义,保持存量 JSON 不留
                # 残留旧值。
                if default_model_v is None:
                    entry.pop("default_model", None)
                else:
                    entry["default_model"] = default_model_v.strip()
                if billing_unit is not None:
                    entry["billing_unit"] = billing_unit
                if token_fields_raw is not None:
                    entry["token_fields"] = token_fields_raw
                if linked_v is not None:
                    # trim + 去重 + 丢弃空串 + 排除自己（上面已 hard-stop，
                    # 这里再防御一次），跟 allowed_models 同款语义。
                    cleaned: list[str] = []
                    for s in linked_v:
                        if not isinstance(s, str):
                            continue
                        s = s.strip()
                        if not s or s == name or s in cleaned:
                            continue
                        cleaned.append(s)
                    entry["linked_upstreams"] = cleaned
                return
        # Refuse to invent an upstream: a typo'd name would otherwise
        # silently create a half-formed entry with no url.
        raise KeyError(f"找不到名为 {name!r} 的上游")

    try:
        return save_upstreams_json(settings, _mutate)
    except KeyError as exc:
        return False, str(exc)


@_deprecated("ctx.mutators.replace_quick_switch")
def replace_quick_switch(
    settings: "Settings",
    items: list[dict[str, Any]],
) -> tuple[bool, str]:
    """整盘替换 settings 关联的 upstreams.json 顶层 ``quick_switch``
    数组（v0.70）。

    仿 :func:`apply_quota_edit` 的 mutator 模式 —— save_upstreams_json
    内置的 PlatformConfig 校验循环对顶层 list（quick_switch 不是
    platform dict）天然放过，所以 mutator 只一行赋值即可；客户端发
    来的 4 字段非空字符串校验在这里完成。

    Returns ``(ok, msg)`` ：``ok`` 为 True 时 ``msg`` 是写入路径，为
    False 时 ``msg`` 是中文错误信息。
    """
    if not isinstance(items, list):
        return False, "items 必须是 list"

    clean: list[dict[str, str]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return False, f"第 {i + 1} 条不是对象"
        label = item.get("label")
        plat  = item.get("platform")
        ups   = item.get("upstream")
        model = item.get("model")
        # 跟 upstreams_file.py:305-315 的加载时校验同款——四字段全部
        # 是 strip() 后非空字符串才算合法,落盘就一定能反序列化回来。
        if not (
            isinstance(label, str) and label.strip()
            and isinstance(plat, str) and plat.strip()
            and isinstance(ups, str) and ups.strip()
            and isinstance(model, str) and model.strip()
        ):
            return False, f"第 {i + 1} 条缺字段或字段为空"
        clean.append({
            "label": label.strip(),
            "platform": plat.strip(),
            "upstream": ups.strip(),
            "model": model.strip(),
        })

    def _mutate(data: dict) -> None:
        # save_upstreams_json 走 PlatformConfig.model_validate 循环时
        # 对顶层 list key 一律跳过（看 :519-521），所以这里直接覆盖
        # 整个 quick_switch 数组是安全的。
        data["quick_switch"] = clean

    return save_upstreams_json(settings, _mutate)


def save_agent_aliases(
    settings: "Settings",
    aliases: dict[str, dict],
) -> tuple[bool, str]:
    """持久化顶层 ``agent_aliases`` 映射到 upstreams.json（v0.155/v0.159/v0.162）。

    原始工具名 → ``{name, color, view}``。``name`` 是显示名（覆盖落库的
    agent 原值），``color`` 是用户选的徽标底色（CSS color 字符串，如
    ``#d97706`` / ``hsl(20 90% 50%)``；空 / None = 落回 css 默认配色），
    ``view`` 是 v0.162 新增的 per-row 显示模式（``"both"`` / ``"requests"``
    / ``"tokens"`` —— 控制总览「平台流量」该行的 meta 文案只显示请求数
    / 只显示 token / 全部都显示）。

    仿 :func:`replace_quick_switch` 的 mutator 模式 —— 顶层 dict key
    天然被 PlatformConfig 校验循环放过。``name`` 空串（用户清空）+ ``color``
    空 / 缺失会被整体丢弃（该 raw 的别名条目不再写入）。``view`` 不在白
    名单（既不是三个允许值之一也不是 None）会被静默丢弃（保留 ``both``
    默认行为，向前兼容旧 / 错数据）。
    """
    if not isinstance(aliases, dict):
        return False, "aliases 必须是对象"

    clean: dict[str, dict[str, Optional[str]]] = {}
    for k, v in aliases.items():
        if not (isinstance(k, str) and k.strip()):
            continue
        if not isinstance(v, dict):
            continue
        name = v.get("name")
        color = v.get("color")
        # name 空 → 整条丢弃；color 空 / None → 仍写一条 ``{name, color: None}``
        if not (isinstance(name, str) and name.strip()):
            continue
        entry: dict[str, Optional[str]] = {"name": name.strip(), "color": None}
        if isinstance(color, str) and color.strip():
            entry["color"] = color.strip()
        # v0.162：view 字段白名单（both / requests / tokens），其它值落
        # 默认 both（不写 None —— 配置文件没必要存显式 None，与 color 策略不同，
        # color 写 None 是为了透传「无色」语义给前端）。
        view = v.get("view")
        if view in ("both", "requests", "tokens"):
            entry["view"] = view
        clean[k.strip()] = entry

    def _mutate(data: dict) -> None:
        data["agent_aliases"] = clean

    return save_upstreams_json(settings, _mutate)


def save_ua_rules(
    settings: "Settings",
    rules: dict[str, str],
) -> tuple[bool, str]:
    """持久化顶层 ``ua_rules`` 映射到 upstreams.json（v0.157）。

    「整串 UA → 平台名」归类规则。与 save_agent_aliases 同款 mutator
    模式：strip 过滤非空 k/v（UA 去首尾空白，空平台名丢弃）。**中继
    进程在请求入口消费这些规则**，所以 GUI 写盘后还要 POST
    /api/upstreams/refresh 让子进程 reload —— 这一步由桥方法负责，
    这里只保证落盘 + 校验。
    """
    if not isinstance(rules, dict):
        return False, "rules 必须是对象"

    clean: dict[str, str] = {}
    for k, v in rules.items():
        if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip():
            clean[k.strip()] = v.strip()

    def _mutate(data: dict) -> None:
        data["ua_rules"] = clean

    return save_upstreams_json(settings, _mutate)


@_deprecated("ctx.mutators.save_advanced_switch")
def save_advanced_switch(
    settings: "Settings",
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """持久化顶层 ``advanced_switch`` 配置到 upstreams.json（v0.11.18）。

    字段语义见 Settings 里的 advanced_* 注释。model 字段从设置面板的
    "当前可用模型清单"里选；strong_types 是需要发到强模型的类型多选；
    aggressive = 判定模糊时优先强模型；learning = 历史学习开关（预留）。

    用 save_upstreams_json 的 mutator 模式原子写盘；顶层对象 key 天然
    被 PlatformConfig 校验循环放过（同 quick_switch）。
    """
    if not isinstance(payload, dict):
        return False, "payload 必须是对象"

    def _s(key: str) -> Optional[str]:
        v = payload.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def _b(key: str) -> bool:
        v = payload.get(key)
        return bool(v) if isinstance(v, bool) else False

    types_raw = payload.get("strong_types")
    strong_types = (
        [t.strip() for t in types_raw if isinstance(t, str) and t.strip()]
        if isinstance(types_raw, list) else []
    )
    clean = {
        "enabled": _b("enabled"),
        "weak_upstream": _s("weak_upstream"),
        "weak_model": _s("weak_model"),
        "strong_upstream": _s("strong_upstream"),
        "strong_model": _s("strong_model"),
        "analysis_upstream": _s("analysis_upstream"),
        "analysis_model": _s("analysis_model"),
        "strong_types": strong_types,
        "aggressive": _b("aggressive"),
        "learning": _b("learning"),
    }

    def _mutate(data: dict) -> None:
        data["advanced_switch"] = clean

    return save_upstreams_json(settings, _mutate)


def save_error_analysis(
    settings: "Settings",
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """持久化顶层 ``error_analysis`` 配置到 upstreams.json（v0.113o）。

    字段语义见 Settings 里的 error_analysis_* 注释。upstream / model 从
    设置面板的"当前可用模型清单"里选，upstream 可为空（按 model 兜底
    定位）。用 save_upstreams_json 的 mutator 模式原子写盘；顶层对象
    key 天然被 PlatformConfig 校验循环放过（同 quick_switch）。
    """
    if not isinstance(payload, dict):
        return False, "payload 必须是对象"

    def _s(key: str) -> Optional[str]:
        v = payload.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def _b(key: str) -> bool:
        v = payload.get(key)
        return bool(v) if isinstance(v, bool) else False

    clean = {
        "enabled": _b("enabled"),
        "upstream": _s("upstream"),
        "model": _s("model"),
    }

    def _mutate(data: dict) -> None:
        data["error_analysis"] = clean

    return save_upstreams_json(settings, _mutate)


def save_vision_models(
    settings: "Settings",
    payload: Any,
) -> tuple[bool, str]:
    """持久化顶层 ``vision_models`` 到 upstreams.json（v0.188）。

    字段语义见 Settings 里的 vision_models 注释。payload 应为模型名
    list[str]（OpenCode 认模型 id，所以存裸模型名，不区分上游）。用
    save_upstreams_json 的 mutator 模式原子写盘；顶层 key 天然被
    PlatformConfig 校验循环放过（同 quick_switch）。
    """
    if not isinstance(payload, list):
        return False, "payload 必须是模型名列表"

    clean = [s.strip() for s in payload if isinstance(s, str) and s.strip()]
    # 去重但保持顺序
    seen: set[str] = set()
    clean = [m for m in clean if not (m in seen or seen.add(m))]

    def _mutate(data: dict) -> None:
        data["vision_models"] = clean

    return save_upstreams_json(settings, _mutate)


def set_upstream_default_model(
    settings: "Settings",
    platform: str,
    name: str,
    default_model: str | None,
) -> tuple[bool, str]:
    """Persist the ``default_model`` field for one upstream (v0.74).

    Mirror of :func:`set_upstream_model` for the v0.74 "auto 兜底" 字段。
    ``default_model=None`` 清除兜底,中继会回退到 ``allowed_models[0]``
    或原样透传。

    Validation rules:
      - ``default_model`` must be ``None`` or a non-empty string.
      - The named upstream must already exist (typo'd names would
        otherwise silently create a half-formed entry without url).

    Returns ``(ok, message)`` — message is the written path on success,
    a human-readable Chinese error otherwise. Does not reload settings.
    """
    if platform not in PLATFORMS:
        return False, f"未知平台: {platform}"
    if not isinstance(name, str) or not name:
        return False, "name 必须是非空字符串"
    if default_model is not None and (not isinstance(default_model, str) or not default_model.strip()):
        return False, "default_model 必须是非空字符串或 null"

    cleaned = default_model.strip() if isinstance(default_model, str) and default_model.strip() else None

    def _mutate(data: dict) -> None:
        # v0.12 扁平格式 {"active": str, "upstreams": [...]}。旧格式就地迁移。
        if not isinstance(data.get("upstreams"), list):
            merged: list = []
            for plat in PLATFORMS:
                section = data.get(plat)
                if isinstance(section, dict) and isinstance(section.get("upstreams"), list):
                    merged.extend(section["upstreams"])
                    data.pop(plat, None)
            data["upstreams"] = merged
            data.setdefault("active", "")
        for entry in data["upstreams"]:
            if entry.get("name") == name:
                if cleaned is None:
                    entry.pop("default_model", None)
                else:
                    entry["default_model"] = cleaned
                return
        raise KeyError(f"找不到名为 {name!r} 的上游")

    try:
        return save_upstreams_json(settings, _mutate)
    except KeyError as exc:
        return False, str(exc)


def set_upstream_model(
    settings: "Settings",
    platform: str,
    name: str,
    model: str | None,
) -> tuple[bool, str]:
    """Persist the model-name override for one upstream (v0.65).

    When ``model`` is non-empty, the relay rewrites the request body's
    ``model`` field to this value before forwarding. When ``None``, the
    override is cleared and the relay passes the client-supplied model
    through verbatim.

    Validation rules:
      - ``model`` must be ``None`` or a non-empty string.
      - The named upstream must already exist (typo'd names would
        otherwise silently create a half-formed entry without url).

    Returns ``(ok, message)`` — message is the written path on success,
    a human-readable Chinese error otherwise. Does not reload settings.
    """
    if platform not in PLATFORMS:
        return False, f"未知平台: {platform}"
    if not isinstance(name, str) or not name:
        return False, "name 必须是非空字符串"
    if model is not None and (not isinstance(model, str) or not model.strip()):
        return False, "model 必须是非空字符串或 null"

    model_clean = model.strip() if isinstance(model, str) and model.strip() else None

    def _mutate(data: dict) -> None:
        # v0.12 扁平格式 {"active": str, "upstreams": [...]}。旧格式就地迁移。
        if not isinstance(data.get("upstreams"), list):
            merged: list = []
            for plat in PLATFORMS:
                section = data.get(plat)
                if isinstance(section, dict) and isinstance(section.get("upstreams"), list):
                    merged.extend(section["upstreams"])
                    data.pop(plat, None)
            data["upstreams"] = merged
            data.setdefault("active", "")
        for entry in data["upstreams"]:
            if entry.get("name") == name:
                if model_clean is None:
                    entry.pop("model", None)
                else:
                    entry["model"] = model_clean
                return
        raise KeyError(f"找不到名为 {name!r} 的上游")

    try:
        return save_upstreams_json(settings, _mutate)
    except KeyError as exc:
        return False, str(exc)


def add_upstream(
    settings: "Settings",
    platform: str,
    payload: dict,
) -> tuple[bool, str]:
    """Append a new upstream entry under ``platform``.

    Driven by the v0.46 "新建" button on the GUI's Upstreams page. Same
    validation pipeline as ``apply_quota_edit`` — names must be unique
    per platform, ``quota_*`` must be non-negative ints or ``None``,
    multipliers must be ``{name: factor ≥ 0}``, ``allowed_models`` must
    be a list of strings.

    ``payload`` keys (all required unless noted):
      ``name``              unique non-empty string within the platform
      ``url``               non-empty string (no URL-shape check — relay
                            accepts anything the platform-specific client
                            will dial; e.g. anthropic expects an HTTPS
                            base, openai expects /v1 base)
      ``api_key``           optional string
      ``note``              optional string
      ``allowed_models``    optional list[str]
      ``model_multipliers`` optional dict[str, number ≥ 0]
      ``quota_5h``          optional non-negative int
      ``quota_week``        optional non-negative int  (v0.46)
      ``quota_month``       optional non-negative int  (v0.46)

    Returns ``(ok, message)``: ``(True, str(name))`` on success,
    ``(False, error_text)`` on validation / IO failure.
    """
    if platform not in PLATFORMS:
        return False, f"未知平台: {platform}"
    if not isinstance(payload, dict):
        return False, "payload 必须是对象"

    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    if not name:
        return False, "name 不能为空"
    if not url:
        return False, "url 不能为空"
    # 'active' is a reserved field name (the platform-level pointer at
    # the currently selected upstream). A user creating an upstream
    # called "active" would silently shadow the selector on the next
    # load — reject up front so the failure is loud, not silent.
    if name == "active":
        return False, 'name 不能为 "active"（与平台选择键同名）'

    allowed = payload.get("allowed_models") or []
    if not isinstance(allowed, list) or not all(isinstance(m, str) for m in allowed):
        return False, "allowed_models 必须是字符串列表"

    # v0.200 该上游支持图片输入的模型名（裸模型名）。须是字符串列表，
    # 建议是 allowed_models 的子集（不强校验 —— 允许手填）。
    vision_v = payload.get("vision_models", None)
    if vision_v is not None:
        if not isinstance(vision_v, list) or not all(isinstance(m, str) for m in vision_v):
            return False, "vision_models 必须是字符串列表"
        vision_clean = [m.strip() for m in vision_v if m.strip()]
    else:
        vision_clean = None

    mults = payload.get("model_multipliers") or {}
    if not isinstance(mults, dict):
        return False, "model_multipliers 必须是对象"
    for k, v in mults.items():
        if not isinstance(k, str) or not k:
            return False, "model name 必须是非空字符串"
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return False, f"multiplier {k!r} 必须 ≥ 0"

    # Validate the three quota windows with the same shape rules as
    # quota_5h (None or non-negative int). Saves us a copy-paste loop.
    quota_payload: dict[str, Optional[int]] = {}
    for field in ("quota_5h", "quota_week", "quota_month"):
        v = payload.get(field, None)
        if v is None:
            quota_payload[field] = None
            continue
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return False, f"{field} 必须是非负整数或 null"
        quota_payload[field] = v

    api_key = payload.get("api_key") or None
    note = payload.get("note") or None

    # v0.65: model 字段，保存到上游配置里
    model_v = payload.get("model", None)
    if model_v is not None and (not isinstance(model_v, str) or not model_v.strip()):
        return False, "model 必须是非空字符串或 null"
    model_clean = model_v.strip() if isinstance(model_v, str) and model_v.strip() else None

    # v0.67: auth_header 字段。None 走 PlatformConfig 自带的平台默认
    # 值（ANTHROPIC_AUTH_HEADER / OPENAI_AUTH_HEADER），只有用户显式
    # 选了非默认值才落盘，跟 _coerce_entry 里的处理一致。
    auth_header_v = payload.get("auth_header", None)
    if auth_header_v is not None:
        if not isinstance(auth_header_v, str) or not auth_header_v.strip():
            return False, "auth_header 必须是非空字符串或 null"
        auth_header_clean = auth_header_v.strip()
    else:
        auth_header_clean = None

    # v0.12 协议声明：wire / endpoint / auth_style（docs/wire-dispatch-plan.md
    # §3）。wire 须是已知枚举（含 v0.98 插件注册的）；endpoint/auth_style
    # 是可选非空字符串。
    wire_v = payload.get("wire", None)
    if wire_v is not None and not is_known_wire(wire_v):
        return False, f"wire 必须是 {list(KNOWN_WIRES) + list(extra_wires())} 之一"
    endpoint_v = payload.get("endpoint", None)
    if endpoint_v is not None and (not isinstance(endpoint_v, str) or not endpoint_v.strip()):
        return False, "endpoint 必须是非空字符串或 null"
    auth_style_v = payload.get("auth_style", None)
    if auth_style_v is not None and (
        not isinstance(auth_style_v, str) or not auth_style_v.strip()
    ):
        return False, "auth_style 必须是非空字符串（bearer / x-api-key / none / 插件方案名）"

    # v0.66: billing_unit / token_fields
    billing_unit = payload.get("billing_unit", None)
    if billing_unit is not None and (
        not isinstance(billing_unit, str) or not billing_unit.strip()
    ):
        return False, "billing_unit 必须是非空字符串（count / token / 插件计费器名）"

    _KNOWN_TOKEN_FIELDS = {
        "input_tokens", "output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    }
    token_fields_raw = payload.get("token_fields", None)
    if token_fields_raw is not None:
        if not isinstance(token_fields_raw, dict):
            return False, "token_fields 必须是对象"
        for k, v in token_fields_raw.items():
            if k not in _KNOWN_TOKEN_FIELDS:
                return False, f"token_fields 的 key 必须是 {sorted(_KNOWN_TOKEN_FIELDS)} 之一"
            if not isinstance(v, bool):
                return False, f"token_fields[{k}] 必须是 bool"

    def _mutate(data: dict) -> None:
        # v0.12 扁平格式 {"active": str, "upstreams": [...]}。旧格式就地迁移。
        if not isinstance(data.get("upstreams"), list):
            merged: list = []
            for plat in PLATFORMS:
                section = data.get(plat)
                if isinstance(section, dict) and isinstance(section.get("upstreams"), list):
                    merged.extend(section["upstreams"])
                    data.pop(plat, None)
            data["upstreams"] = merged
            data.setdefault("active", "")
        # Refuse to silently overwrite an existing entry with the same
        # name. The GUI form should disable save when name conflicts,
        # but the bridge is the last line of defense.
        for entry in data["upstreams"]:
            if entry.get("name") == name:
                raise ValueError(f"已存在名为 {name!r} 的上游")
        new_entry = {
            "name": name,
            "url": url,
            "api_key": api_key,
            "note": note,
            "quota_5h": quota_payload["quota_5h"],
            "quota_week": quota_payload["quota_week"],
            "quota_month": quota_payload["quota_month"],
            "model_multipliers": {str(k): float(v) for k, v in mults.items()},
            "allowed_models": list(allowed),
        }
        if model_clean is not None:
            new_entry["model"] = model_clean
        if billing_unit is not None:
            new_entry["billing_unit"] = billing_unit
        if token_fields_raw is not None:
            new_entry["token_fields"] = token_fields_raw
        if auth_header_clean is not None:
            new_entry["auth_header"] = auth_header_clean
        # v0.200 该上游支持图片输入的模型名。非空才落盘（空 = 未声明，
        # 加载时默认 []）。
        if vision_clean:
            new_entry["vision_models"] = vision_clean
        # v0.12 协议声明（None 不落盘，加载时按平台段推断）
        if wire_v is not None:
            new_entry["wire"] = wire_v
        if endpoint_v is not None:
            new_entry["endpoint"] = endpoint_v.strip()
        if auth_style_v is not None:
            new_entry["auth_style"] = auth_style_v
        # v0.8.1: requires_anthropic_adapter。upstreams.json 里写了
        # ``true`` 时必须透传到 PlatformConfig —— 不在这一处把它从
        # payload 取出来落盘的话,reload 之后 pydantic 默认值是 False,
        # proxy.py 的 adapter 入口永远进不去,客户端的 Authorization:
        # Bearer / 模型大小写 / thinking 块全交给上游处理,会话断。
        # pydantic 默认值是 False,所以不传等价于不开 adapter,不需要
        # 单独 else 分支。
        if payload.get("requires_anthropic_adapter"):
            new_entry["requires_anthropic_adapter"] = True
        data["upstreams"].append(new_entry)

    try:
        ok, msg = save_upstreams_json(settings, _mutate)
    except ValueError as exc:
        return False, str(exc)
    if not ok:
        return False, msg
    return True, name


def remove_upstream(
    settings: "Settings",
    platform: str,
    name: str,
) -> tuple[bool, str]:
    """Drop an upstream entry entirely from ``platform``.

    Driven by the v0.64 GUI "删除上游" button (top-right × on each
    upstream-detail card on the Upstreams page). If the upstream being
    removed is also the platform's currently-active one, the ``active``
    pointer is cleared so PlatformConfig validation doesn't reject the
    file with "active points at a name that doesn't exist".

    Same validation pipeline as ``apply_quota_edit`` — bad inputs are
    rejected before they touch disk, and ``PlatformConfig`` re-validates
    the resulting entry so a half-removed state can't poison the file.

    Returns ``(ok, message)`` — message is the written path on success,
    a human-readable Chinese error otherwise. Does **not** reload
    settings; the caller decides when to rebind.
    """
    if not isinstance(name, str) or not name:
        return False, "name 必须是非空字符串"

    def _mutate(data: dict) -> None:
        # v0.12 扁平格式。旧格式就地迁移。
        if not isinstance(data.get("upstreams"), list):
            merged: list = []
            for plat in PLATFORMS:
                section = data.get(plat)
                if isinstance(section, dict) and isinstance(section.get("upstreams"), list):
                    merged.extend(section["upstreams"])
                    data.pop(plat, None)
            data["upstreams"] = merged
            data.setdefault("active", "")
        before = len(data["upstreams"])
        data["upstreams"] = [
            entry for entry in data["upstreams"] if entry.get("name") != name
        ]
        if len(data["upstreams"]) == before:
            raise KeyError(f"找不到名为 {name!r} 的上游")
        # If this upstream was active, clear the pointer.
        if data.get("active") == name:
            data["active"] = ""

    try:
        return save_upstreams_json(settings, _mutate)
    except KeyError as exc:
        return False, str(exc)
