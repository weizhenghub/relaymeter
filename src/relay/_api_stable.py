"""中继稳定 API re-export 入口（v0.117+）。

⚠ 本模块的**符号集** = 中继对插件作者 / 横向模块开发者**永久稳定**的 API 表面。
详见 `docs/architecture/STABILITY.md`。每条 `from .x import y` 都是一条契约。
"""
from __future__ import annotations

# ── 插件加载器 ────────────────────────────────────────────────────────
from .plugin import (
    PluginContext,
    arun_overrides,
    emit_event,
    load_plugins,
    override_providers,
    parser_factory_for,
    plugins_dir,
    run_hooks,
)
# 模块级 register_* 函数（PluginContext 同款方法的同形函数入口，给不用 ctx 的极简插件用）
# 注：`register_wire` / `register_parser` 不是模块级函数，
# 它们是 `PluginContext.register_wire` / `register_parser` 方法。
# 插件作者用 `ctx.register_wire(...)` / `ctx.register_parser(...)`，
# 不在 _api_stable 模块级入口暴露。
from .plugin import (
    register_auth_scheme,
    register_billing_unit,
    register_override,
    register_prober,
    register_wire_converter,
)
# 同名查询函数（V0.3+ 扩展注册表）
from .plugin import (
    auth_scheme_for,
    prober_for,
    billing_unit_for,
    wire_converter_for,
)

# ── 解析器工厂（V0.0.1 起冻结） ─────────────────────────────────────────
from .parsers.anthropic import AnthropicUsageParser
from .parsers.openai import OpenAIUsageParser

# ── 钩子名常量（防止插件写错字符串） ────────────────────────────────────
# 这些字符串本身冻结，避免 plugin.py 改名后插件跟着改。
_HOOK_NAMES = (
    "pre_dispatch", "pre_upstream", "post_response",
    "decide_auth", "before_quota_deduct", "decide_quota_switch", "after_probe",
)

# ── 内置事件名常量（同上） ──────────────────────────────────────────────
_EVENT_NAMES = (
    "request.started", "request.done", "db.recorded",
    "quota.autoswitch", "quota.recovered",
    "probe.finished", "config.upstreams_changed",
    "auth.failed", "alert",
)

__all__ = [
    # 插件加载器
    "load_plugins", "plugins_dir",
    "emit_event", "run_hooks", "arun_overrides", "override_providers",
    "parser_factory_for", "wire_converter_for",
    "auth_scheme_for", "prober_for", "billing_unit_for",
    # 注册函数（模块级；PluginContext 同名方法见 PluginContext 节）
    "register_wire_converter",
    "register_auth_scheme", "register_prober", "register_billing_unit",
    "register_override",
    # PluginContext 类
    "PluginContext",
    # 解析器
    "AnthropicUsageParser", "OpenAIUsageParser",
    # 字符串常量
    "_HOOK_NAMES", "_EVENT_NAMES",
]
