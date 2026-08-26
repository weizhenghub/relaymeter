"""插件平台 V0.0.1 —— 插件加载器与 ctx 契约。

插件 = ``plugins/`` 目录下一个 ``.py`` 文件（目录可用 ``RELAY_PLUGINS_DIR``
覆盖）。每个插件模块必须导出 ``apply(ctx)``：启动时被调用一次，插件在
apply 里注册 wire / parser / 钩子 / 事件订阅。

⚠ **STABLE API 自 v0.117 起冻结**（详见 ``docs/architecture/STABILITY.md``）。
本文件下列符号 / 方法的签名 / 行为 / 字段语义视为永久契约：
``load_plugins`` / ``plugins_dir`` / ``PluginContext`` 及其 ``register_*`` /
``on`` / ``emit`` / ``push_alert`` 方法 / ``emit_event`` / ``run_hooks`` /
``arun_overrides`` / ``override_providers`` / ``parser_factory_for`` /
``wire_converter_for`` / ``auth_scheme_for`` / ``prober_for`` /
``billing_unit_for`` / 模块级 ``register_wire_converter`` /
``register_auth_scheme`` / ``register_prober`` / ``register_billing_unit`` /
``register_override``。下划线开头的符号（``_HOOK_NAMES`` / ``_EVENTS`` 等）
是内部实现，**不在稳定 API**。

V0.0.1 能力清单：
  * ``register_wire``   —— 注册新 wire 的默认端点/认证风格（config 层，
                          校验/默认值查询全链路生效）
  * ``register_parser`` —— 注册按 wire 选择的 usage parser（跨线路径用）
  * ``register_hook``   —— 请求生命周期钩子（pre_dispatch / pre_upstream /
                          post_response），装饰器或直接传函数两用
  * ``on`` / ``emit``   —— 进程内事件总线（内置事件：request.done / alert）
  * ``ctx.db`` / ``ctx.settings`` / ``ctx.log`` —— 服务访问

设计红线：
  * 钩子全部异常隔离 —— 插件抛错只记日志，绝不阻断请求转发；
  * 钩子是直接函数调用（不走事件派发）—— 热路径零额外开销；
  * 插件加载失败只跳过该插件，不影响中继启动；
  * 注册表是活文档 —— 钩子签名 / 事件 payload 见各函数 docstring。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    # 加载器
    "load_plugins", "plugins_dir",
    # 模块级注册 / 查询
    "register_wire_converter", "wire_converter_for",
    "register_auth_scheme", "auth_scheme_for",
    "register_prober", "prober_for",
    "register_billing_unit", "billing_unit_for",
    "register_override", "override_providers", "arun_overrides",
    # 钩子 / 事件执行
    "run_hooks", "emit_event",
    # parser 工厂
    "parser_factory_for",
    # PluginContext 类
    "PluginContext",
]

log = logging.getLogger("relay.plugin")

# ---- 注册表（进程内单例）----

_HOOK_NAMES = (
    "pre_dispatch", "pre_upstream", "post_response",
    "decide_auth", "before_quota_deduct", "decide_quota_switch", "after_probe",
)

# hook 名 -> [(插件名, fn)]，调用顺序 = 注册顺序
_HOOKS: dict[str, list[tuple[str, Callable[..., Any]]]] = {n: [] for n in _HOOK_NAMES}

# 事件名 -> [(插件名, fn)]；payload 按关键字参数传给 fn
_EVENTS: dict[str, list[tuple[str, Callable[..., Any]]]] = {}

# wire 名 -> parser 工厂（满足 `_HasFeed` 契约：feed/finalize/assembled_*）
_PARSERS: dict[str, Callable[[], Any]] = {}

# (src_wire, dst_wire) -> 请求转换器。转换器契约：
# ``fn(payload: dict, src_wire: str, dst_wire: str) -> dict`` —— 与
# linguafranca 的 ``convert_request`` 同签名，插件注册后即整体替换内置转换。
_WIRE_CONVERTERS: dict[tuple[str, str], Callable[..., Any]] = {}

# 鉴权方案名 -> 方案实现。契约：
# ``fn(cfg, platform) -> (header_name, header_value)`` —— 上游配置
# ``auth_style: <方案名>`` 时，proxy 发往上游的请求头按插件方案拼装。
_AUTH_SCHEMES: dict[str, Callable[..., Any]] = {}

# 探活器名 -> 探活器实现。契约同 ``probe_upstream``：
# ``fn(url, api_key, *, timeout=..., model=...) -> dict``（返回
# probe_upstream 的标准结构 {wire, endpoint, auth_style, key_valid, ...}）。
_PROBERS: dict[str, Callable[..., Any]] = {}

# 计费单元名 -> 计费器实现。契约：
# ``fn(model, *, raw_count=..., input_tokens=..., output_tokens=...,
# cache_read_input_tokens=..., cache_creation_input_tokens=...) -> float``
# 返回该次请求的加权消耗基数（再乘 model_multipliers）。上游配置
# ``billing_unit: <插件名>`` 时替代内置 count/token 计算。
_BILLING_UNITS: dict[str, Callable[..., Any]] = {}

# ---- V0.4 替换层：子系统覆盖机制 ----
# 子系统名 -> [(priority, 插件名, fn)]，按 priority 降序（高优先先试）。
# ``run_overrides(subsystem, *args)`` 依次调用，第一个返回非 None 的生效
# （整体接管该子系统）；全部返回 None 则调用方走默认实现。异常隔离：
# 单个处理器抛错只记日志、继续下一个。防递归：同一子系统重入直接放弃。
_OVERRIDES: dict[str, list[tuple[int, str, Callable[..., Any]]]] = {}
_OVERRIDE_ACTIVE: dict[str, bool] = {}

# 插件名 -> 文件路径
_LOADED: dict[str, str] = {}


def _register_default_parsers() -> None:
    """内置三个 wire 的 parser 工厂（可被插件同名覆盖的兜底）。"""
    from .config import (
        WIRE_ANTHROPIC_MESSAGES,
        WIRE_OPENAI_CHAT,
        WIRE_OPENAI_RESPONSES,
    )
    from .parsers.anthropic import AnthropicUsageParser
    from .parsers.openai import OpenAIUsageParser

    _PARSERS.setdefault(WIRE_ANTHROPIC_MESSAGES, AnthropicUsageParser)
    _PARSERS.setdefault(WIRE_OPENAI_CHAT, OpenAIUsageParser)
    _PARSERS.setdefault(WIRE_OPENAI_RESPONSES, OpenAIUsageParser)


_register_default_parsers()


def parser_factory_for(wire: Optional[str]) -> Optional[Callable[[], Any]]:
    """按上游 wire 选 parser 工厂；未注册返回 None（调用方回退默认）。"""
    return _PARSERS.get(wire) if wire else None


def register_wire_converter(
    src_wire: str, dst_wire: str, fn: Callable[..., Any], *, name: str = "plugin",
) -> None:
    """注册 ``(src_wire, dst_wire)`` 请求转换器（V0.3 扩展层）。

    契约同 linguafranca ``convert_request(payload, src, dst) -> dict``。
    同一对 wire 重复注册时**新实现覆盖旧实现**（转换器是实现细节，
    插件希望自己掌控；与 parser 的"首个生效"策略不同）。返回 None 的
    转换器让调用方回退内置 linguafranca。
    """
    _WIRE_CONVERTERS[(src_wire, dst_wire)] = fn
    log.info(
        "plugin %s registered wire converter %s -> %s", name, src_wire, dst_wire,
    )


def wire_converter_for(src_wire: str, dst_wire: str) -> Optional[Callable[..., Any]]:
    """按 (src, dst) 取插件转换器；未注册返回 None（调用方回退 linguafranca）。"""
    return _WIRE_CONVERTERS.get((src_wire, dst_wire))


def register_auth_scheme(
    name: str, fn: Callable[..., Any], *, owner: str = "plugin",
) -> None:
    """注册鉴权方案。同名重复注册：新实现覆盖旧实现。"""
    _AUTH_SCHEMES[name] = fn
    log.info("plugin %s registered auth scheme %s", owner, name)


def auth_scheme_for(name: Optional[str]) -> Optional[Callable[..., Any]]:
    """按方案名取鉴权实现；未注册返回 None（调用方回退内置风格）。"""
    if not name:
        return None
    return _AUTH_SCHEMES.get(name)


def register_prober(name: str, fn: Callable[..., Any], *, owner: str = "plugin") -> None:
    """注册探活器。同名重复注册：新实现覆盖旧实现。"""
    _PROBERS[name] = fn
    log.info("plugin %s registered prober %s", owner, name)


def prober_for(name: Optional[str]) -> Optional[Callable[..., Any]]:
    """按名字取探活器；未注册返回 None（调用方回退内置探测）。"""
    if not name:
        return None
    return _PROBERS.get(name)


def register_billing_unit(
    name: str, fn: Callable[..., Any], *, owner: str = "plugin",
) -> None:
    """注册计费器。同名重复注册：新实现覆盖旧实现。"""
    _BILLING_UNITS[name] = fn
    log.info("plugin %s registered billing unit %s", owner, name)


def billing_unit_for(name: Optional[str]) -> Optional[Callable[..., Any]]:
    """按名字取计费器；未注册返回 None（调用方回退内置 count/token）。"""
    if not name:
        return None
    return _BILLING_UNITS.get(name)


# ---- V0.4 替换层：子系统覆盖执行器 ----

def register_override(
    subsystem: str,
    fn: Callable[..., Any],
    *,
    priority: int = 0,
    owner: str = "plugin",
) -> None:
    """注册一个子系统覆盖处理器（V0.4 替换层）。

    覆盖语义：``run_overrides(subsystem, *args)`` 按 priority 降序尝试，
    第一个返回非 None 的处理器整体接管该子系统，调用方跳过默认实现。
    每个子系统的处理器契约由调用方文档化（目前定义：``auth`` =
    ``fn(request, platform) -> Optional[Response]``）。

    覆盖处理器内部若要"借用内置实现"，先记住结果再返回 None 是安全的
    （防递归护栏会阻止同子系统重入导致的无限循环）。
    """
    _OVERRIDES.setdefault(subsystem, []).append((priority, owner, fn))
    _OVERRIDES[subsystem].sort(key=lambda t: -t[0])
    log.info("plugin %s registered override %s (priority=%d)", owner, subsystem, priority)


def override_providers(subsystem: str) -> list[tuple[int, str]]:
    """已注册的覆盖处理器（priority, 插件名）按优先级降序。"""
    return [(p, o) for p, o, _ in list(_OVERRIDES.get(subsystem, ()))]


async def arun_overrides(
    subsystem: str, *args: Any, **kwargs: Any,
) -> tuple[Any, Optional[str]]:
    """覆盖执行器（async 版）。返回 ``(result, provider)``；无生效处理器时
    返回 ``(None, None)``。异常隔离 + 同子系统防递归。"""
    if _OVERRIDE_ACTIVE.get(subsystem):
        return None, None
    _OVERRIDE_ACTIVE[subsystem] = True
    try:
        for _prio, owner, fn in list(_OVERRIDES.get(subsystem, ())):
            try:
                r = await _maybe_await(fn, *args, **kwargs)
            except Exception as exc:
                log.warning(
                    "plugin %s override %s 失败（已隔离）: %s", owner, subsystem, exc,
                )
                continue
            if r is not None:
                return r, owner
        return None, None
    finally:
        _OVERRIDE_ACTIVE[subsystem] = False


class PluginContext:
    """每个插件 ``apply(ctx)`` 收到的上下文。

    V0.0.1 的 ctx 是插件唯一入口：注册面（wire/parser/hook/事件）+
    服务面（db/settings/log）。跨插件共享同一个 ctx 实例 —— 不要给
    ctx 挂插件私有状态（存模块级变量）。
    """

    def __init__(self, *, settings: Any = None, db: Any = None) -> None:
        self.settings = settings
        self.db = db
        self.name = ""
        self._log: Optional[logging.Logger] = None
        # Phase 6：真实 RelayContext 后引用（lifespan 注入）。新风格插件
        # 用它拿 ``ctx.svc("xxx")`` / ``ctx.register(...)`` 服务容器能力；
        # 旧插件不感知，继续用注册面方法。
        self._relay_ctx: Optional[Any] = None

    # ---- Phase 6: 服务容器委托（新风格插件用）----

    def svc(self, key: str) -> Any:
        """从服务容器取服务（``ctx.svc("pool")`` 等）。

        lifespan 注入了 RelayContext 才可用；未注入抛 ``KeyError``。
        旧插件（只用注册面方法）不调它，无影响。
        """
        if self._relay_ctx is None:
            raise KeyError(f"service {key!r} not registered (relay ctx not bound)")
        return self._relay_ctx.svc(key)

    def register(self, key: str, instance: object) -> Any:
        """把对象注册为服务（``ctx.register("my_svc", self)``）。

        返回 Disposer（Phase 6 起 loader 支持 Service 风格插件返回
        Disposer；此处委托给 RelayContext.register 返回其 disposer）。
        """
        if self._relay_ctx is None:
            raise KeyError(f"cannot register {key!r} (relay ctx not bound)")
        return self._relay_ctx.register(key, instance)

    @property
    def log(self) -> logging.Logger:
        return self._log or log

    # ---- 注册面 ----

    def register_wire(
        self, name: str, *, endpoint: str, auth_style: str = "bearer",
    ) -> None:
        """注册新 wire 的默认端点/认证风格。

        注册后，upstreams.json 的 wire 校验、effective_endpoint /
        effective_auth_style、probe 探测默认值全部自动认这个 wire。
        协议转换器（linguafranca 不认新 wire）是 V0.1 的能力，V0.0.1
        只立注册表 —— 新 wire 的跨线转换仍会报 WireConversionError。
        """
        from .config import register_extra_wire

        register_extra_wire(name, endpoint=endpoint, auth_style=auth_style)
        self.log.info(
            "plugin %s registered wire %r endpoint=%s auth=%s",
            self.name, name, endpoint, auth_style,
        )

    def register_parser(self, wire: str, factory: Callable[[], Any]) -> None:
        """注册按 wire 选择的 usage parser 工厂（重复注册忽略）。

        factory 必须满足 `_HasFeed` 契约（proxy.py 的鸭子协议）：
        feed(chunk) / finalize() -> UsageAcc / assembled_text() /
        assembled_thinking() / assembled_tool_use_json()。
        """
        if wire in _PARSERS:
            self.log.warning(
                "plugin %s parser for wire %r already registered; ignored",
                self.name, wire,
            )
            return
        _PARSERS[wire] = factory
        self.log.info("plugin %s registered parser for wire %r", self.name, wire)

    def register_wire_converter(
        self, src_wire: str, dst_wire: str,
        fn: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """注册 (src_wire, dst_wire) 请求转换器（V0.3 扩展层）。装饰器用法::

            @ctx.register_wire_converter("openai-chat", "openai-responses")
            def convert(payload, src_wire, dst_wire): ...

        或直接 ``ctx.register_wire_converter(src, dst, fn)``。注册后跨协议
        路径（``convert_request``）优先走此实现，返回 None 时回退 linguafranca。
        """
        def _reg(f: Callable[..., Any]) -> Callable[..., Any]:
            register_wire_converter(
                src_wire, dst_wire, f, name=self.name,
            )
            return f

        return _reg(fn) if fn is not None else _reg

    def register_auth_scheme(
        self, name: str,
        fn: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """注册鉴权方案（V0.3 扩展层）。装饰器两用。上游 ``auth_style``
        配成方案名时，proxy 用 ``fn(cfg, platform) -> (header, value)``
        拼上游请求头。返回 None 的 fn 走默认风格兜底。"""
        def _reg(f: Callable[..., Any]) -> Callable[..., Any]:
            register_auth_scheme(name, f, owner=self.name)
            return f

        return _reg(fn) if fn is not None else _reg

    def register_prober(
        self, name: str,
        fn: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """注册探活器（V0.3 扩展层）。装饰器两用。契约同 probe_upstream
        （url, api_key, *, timeout, model -> 标准结构 dict）。"""
        def _reg(f: Callable[..., Any]) -> Callable[..., Any]:
            register_prober(name, f, owner=self.name)
            return f

        return _reg(fn) if fn is not None else _reg

    def register_billing_unit(
        self, name: str,
        fn: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """注册计费器（V0.3 扩展层）。装饰器两用。上游 ``billing_unit``
        配成插件名时，quota 成本计算用插件实现替代内置 count/token。"""
        def _reg(f: Callable[..., Any]) -> Callable[..., Any]:
            register_billing_unit(name, f, owner=self.name)
            return f

        return _reg(fn) if fn is not None else _reg

    def register_override(
        self, subsystem: str, priority: int = 0,
        fn: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """注册子系统覆盖处理器（V0.4 替换层）。装饰器与传函数两用::

            @ctx.register_override("auth", priority=10)
            async def my_auth(request, platform): ...

        priority 越高越先尝试（默认 0）。返回非 None 即整体接管该子系统。
        """
        def _reg(f: Callable[..., Any]) -> Callable[..., Any]:
            register_override(subsystem, f, priority=priority, owner=self.name)
            return f

        return _reg(fn) if fn is not None else _reg

    def register_hook(
        self, name: str, fn: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """注册请求生命周期钩子。装饰器与直接传函数两用：:

            @ctx.register_hook("pre_dispatch")
            async def h(info): ...

            ctx.register_hook("pre_upstream", my_fn)

        钩子签名：``fn(info: dict) -> None``，``info`` 为可变 dict，各钩子
        可写字段见下（只读字段写了不生效）：

        * ``pre_dispatch`` —— 分发判定 + 模型链 + advanced-switch 之后、
          DISPATCH 日志之前。可写 ``model``（relay 会用 _rewrite_model_in_body
          同步重写请求体）、``body``（整体替换；relay 会重新提取 model）。
          ``cfg`` 只读（upstream_url 已按 cfg 拼好，V0.0.1 不支持换上游）。
        * ``pre_upstream`` —— 发往上游前最后一改。可写 ``headers``
          （dict，增删请求头）。
        * ``post_response`` —— 落库完成后（流式在流结束 finally 里）。
          只读快照：platform / cfg / model / status / usage（UsageAcc）/
          error / req_db_id / upstream / request_id / streaming。

        任何异常都被隔离（记日志，不阻断转发）。
        """
        if name not in _HOOKS:
            raise ValueError(f"未知钩子 {name!r}，可用：{list(_HOOKS)}")

        def _reg(f: Callable[..., Any]) -> Callable[..., Any]:
            if not callable(f):
                raise TypeError(f"钩子 {name} 注册的不是可调用对象: {f!r}")
            _HOOKS[name].append((self.name, f))
            self.log.info("plugin %s registered hook %s", self.name, name)
            return f

        return _reg(fn) if fn is not None else _reg

    hook = register_hook  # 别名，装饰器读起来更顺

    def on(
        self, event: str, fn: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """订阅进程内事件。装饰器与直接传函数两用。

        内置事件：
        * ``request.done`` —— 每次请求落库完成后触发，payload 与
          post_response 钩子的 info 同字段（usage 是 UsageAcc 对象）。
        * ``alert`` —— 告警通道（ctx.push_alert 推送，GUI 右上角卡片同源）。

        订阅者建议用同步函数；异步订阅者会被调度到事件循环（仅限
        async 上下文内 emit 时）。
        """

        def _reg(f: Callable[..., Any]) -> Callable[..., Any]:
            _EVENTS.setdefault(event, []).append((self.name, f))
            self.log.info("plugin %s subscribed event %s", self.name, event)
            return f

        return _reg(fn) if fn is not None else _reg

    def emit(self, event: str, **payload: Any) -> None:
        emit_event(event, **payload)

    def push_alert(self, message: str, **extra: Any) -> None:
        """推一条告警（走事件总线，插件可再订阅处理）。"""
        self.emit("alert", message=str(message), **extra)


# ---- 加载器 ----

def plugins_dir() -> Path:
    """插件目录：RELAY_PLUGINS_DIR 优先，否则项目根/plugins。

    PyInstaller 冻结态下 ``__file__`` 指向解包临时目录，源码树的
    ``parent.parent.parent`` 失效 —— 退回 exe 同目录的 plugins/。
    """
    env = os.environ.get("RELAY_PLUGINS_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "plugins"
    return Path(__file__).resolve().parent.parent.parent / "plugins"


def load_plugins(app: Any = None, directory: Optional[Path] = None) -> list[str]:
    """扫描插件目录，加载每个 ``*.py``（下划线开头跳过）并调用 ``apply(ctx)``。

    app 提供 ctx 的 db / settings（来自 app.state）。单个插件加载失败
    只跳过该插件（记日志），不影响其余插件与中继启动。
    """
    ctx = PluginContext(
        settings=getattr(app.state, "settings", None) if app is not None else None,
        db=getattr(app.state, "db", None) if app is not None else None,
    )
    # Phase 6：把真实 RelayContext 绑给插件 ctx，让新风格插件能用
    # ``ctx.svc()`` / ``ctx.register()``（旧插件不感知）。
    relay_ctx = getattr(app.state, "ctx", None) if app is not None else None
    ctx._relay_ctx = relay_ctx
    pdir = Path(directory) if directory is not None else plugins_dir()
    if not pdir.is_dir():
        return []
    loaded: list[str] = []
    for path in sorted(pdir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"relay_plugins.{name}", path)
            if spec is None or spec.loader is None:
                log.warning("plugin %s: 无法解析模块，跳过", name)
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            apply = getattr(mod, "apply", None)
            if not callable(apply):
                log.warning("plugin %s: 没有 apply(ctx) 函数，跳过", name)
                continue
            ctx.name = name
            ctx._log = logging.getLogger(f"relay.plugin.{name}")
            apply(ctx)
            _LOADED[name] = str(path)
            loaded.append(name)
            log.info("plugin loaded: %s (%s)", name, path)
        except Exception as exc:
            log.exception("plugin %s 加载失败，已跳过: %s", name, exc)
    return loaded


# ---- 钩子 / 事件执行（全部异常隔离）----

async def _maybe_await(fn: Callable[..., Any], *args: Any) -> Any:
    r = fn(*args)
    if inspect.isawaitable(r):
        return await r
    return r


async def run_hooks(name: str, info: dict) -> Any:
    """顺序执行钩子链；每个钩子异常隔离（插件错误绝不阻断转发）。

    返回第一个钩子的非 ``None`` 返回值（按注册顺序），供决策类钩子
    （``decide_*`` / ``before_*``）让插件表达"表态"。普通观察类钩子
    返回 ``None``，聚合结果也自然是 ``None``，互不影响。
    """
    result = None
    for plugin, fn in list(_HOOKS.get(name, ())):
        try:
            rv = await _maybe_await(fn, info)
        except Exception as exc:
            log.warning("plugin %s hook %s 失败（已隔离）: %s", plugin, name, exc)
            continue
        if rv is not None and result is None:
            result = rv
    return result


def emit_event(event: str, **payload: Any) -> None:
    """进程内事件广播；订阅者异常隔离。

    异步订阅者用 ``asyncio.ensure_future`` 调度（只在事件循环运行时
    有效；sync 订阅者不受影响）。
    """
    for plugin, fn in list(_EVENTS.get(event, ())):
        try:
            r = fn(**payload)
            if inspect.isawaitable(r):
                asyncio.ensure_future(r)
        except Exception as exc:
            log.warning("plugin %s event %s handler 失败（已隔离）: %s", plugin, event, exc)