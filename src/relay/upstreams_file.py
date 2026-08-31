"""Hand-editable JSON config for upstream APIs / nodes (v0.8).

Why this exists
---------------

Before v0.8 the only way to configure upstreams was a single-line JSON blob
inside `.env`::

    ANTHROPIC_UPSTREAMS=[{"name":"a","url":"..."},{"name":"b","url":"..."}]

pydantic-settings reads one value per line, so that blob can't be
pretty-printed, can't carry comments, and adding a node means editing a
200-character line without a syntax check. This module moves the same data
into a real JSON file you can open and edit by hand::

    {
      "anthropic": {
        "active": "minimax-cn",
        "upstreams": [
          {"name": "minimax-cn", "url": "https://minnimax.chat",
           "api_key": "sk-xxx", "auth_header": "x-api-key",
           "note": "主力节点"},
          {"name": "official", "url": "https://api.anthropic.com"}
        ]
      },
      "openai": {"active": "openai", "upstreams": [...]}
    }

Precedence
----------

The file WINS over `.env`. When a platform section in the file has a
non-empty `upstreams` list, it replaces whatever `*_UPSTREAMS` produced;
`active` likewise overrides `*_ACTIVE`. Platforms absent from the file keep
their env-derived config, so a partially-filled file is fine.

Read once at startup — :func:`apply_to_settings` is called from
``Settings.load()``. Edit the file, restart the relay (the GUI's 重启 button
is enough). There is deliberately no hot-reload watcher: config that changes
under a running process is hard to reason about when something misbehaves.

Fail-open
---------

A malformed file must never brick the relay: we log an error naming the file
and the parse position, then fall back to the env config. Same for individual
entries — one bad node is skipped, the rest still load. The alternative
(crash on startup) would leave the GUI showing a dead server with the reason
buried in a subprocess log.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .config import (
    ANTHROPIC_AUTH_HEADER,
    OPENAI_AUTH_HEADER,
    KNOWN_AUTH_STYLES,
    KNOWN_WIRES,
    PlatformConfig,
    extra_wires,
    is_known_wire,
)


log = logging.getLogger("relay.upstreams_file")


DEFAULT_FILENAME = "upstreams.json"

# Platforms we read sections for. OpenClaw speaks the Anthropic wire format
# and routes through /anthropic, so it has no section of its own.
PLATFORMS: tuple[str, ...] = ("anthropic", "openai")

_DEFAULT_AUTH_HEADER = {
    "anthropic": ANTHROPIC_AUTH_HEADER,
    "openai": OPENAI_AUTH_HEADER,
}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def load(path: str | os.PathLike[str]) -> Optional[dict[str, Any]]:
    """Parse the JSON config file. Returns ``None`` when unusable.

    ``None`` means "caller should keep the env config" and covers three
    cases: the file doesn't exist, it isn't valid JSON, or its top level
    isn't an object. All three are logged; only the first is logged at
    debug level since "no file" is the default state for a fresh checkout.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.debug("upstreams file %s not present; using env config", p)
        return None
    except OSError as exc:
        log.error("cannot read upstreams file %s: %s", p, exc)
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error(
            "upstreams file %s is not valid JSON (line %d col %d: %s); "
            "falling back to env config",
            p, exc.lineno, exc.colno, exc.msg,
        )
        return None
    if not isinstance(obj, dict):
        log.error(
            "upstreams file %s must contain a JSON object keyed by platform, "
            "got %s; falling back to env config",
            p, type(obj).__name__,
        )
        return None
    return obj


def _opt_nonempty_str(entry: dict, key: str) -> Optional[str]:
    """v0.12：读一个可选非空字符串字段（endpoint 等）。空/非字符串→None。"""
    v = entry.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _opt_known(
    entry: dict, key: str, allowed: tuple[str, ...], path: Path, name: str
) -> Optional[str]:
    """v0.12：读一个可选枚举字段（wire / auth_style）。未知值 warn + None。"""
    v = entry.get(key)
    if v is None:
        return None
    if isinstance(v, str) and v.strip() in allowed:
        return v.strip()
    log.warning(
        "%s: upstream %r has invalid %s=%r (want one of %s); ignored",
        path, name, key, v, ",".join(allowed),
    )
    return None


def _coerce_entry(platform: Optional[str], entry: Any, index: int, path: Path) -> Optional[PlatformConfig]:
    """Turn one raw JSON entry into a PlatformConfig, or None if unusable.

    ``platform`` 为 None 表示 v0.12 扁平格式（无平台分组）——此时不套
    平台默认 auth_header，交给 wire/运行时推导（effective_auth_style）。
    """
    if not isinstance(entry, dict):
        log.warning("%s: %s.upstreams[%d] is not an object; skipped", path, platform, index)
        return None
    name = entry.get("name")
    url = entry.get("url")
    if not isinstance(name, str) or not name.strip():
        log.warning("%s: %s.upstreams[%d] has no usable 'name'; skipped", path, platform, index)
        return None
    if not isinstance(url, str) or not url.strip():
        log.warning(
            "%s: upstream %r has no usable 'url'; skipped", path, name,
        )
        return None
    api_key = entry.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        log.warning("%s: upstream %r has non-string api_key; treated as absent", path, name)
        api_key = None
    auth_header = entry.get("auth_header")
    if not isinstance(auth_header, str) or not auth_header.strip():
        # 平台默认头只套给 legacy（未声明 wire）条目：声明了 wire 的条目
        # 认证跟 wire 走（effective_auth_style 落回 wire 默认），用户显式
        # 配置的 auth_header 永远优先。扁平格式（platform=None）从不套默认。
        wire_declared = is_known_wire(entry.get("wire"))
        auth_header = (
            _DEFAULT_AUTH_HEADER.get(platform)
            if (platform and not wire_declared)
            else None
        )
    note = entry.get("note")

    # v0.20 quota fields. Each is optional and independently degradable: a
    # malformed one is dropped with a warning rather than killing the whole
    # upstream, on the same fail-open principle as the rest of this loader.
    quota_5h = entry.get("quota_5h")
    if quota_5h is not None:
        if isinstance(quota_5h, bool) or not isinstance(quota_5h, int) or quota_5h <= 0:
            log.warning(
                "%s: upstream %r has invalid quota_5h %r (want a positive int); ignored",
                path, name, quota_5h,
            )
            quota_5h = None

    multipliers: dict[str, float] = {}
    raw_mult = entry.get("model_multipliers")
    if isinstance(raw_mult, dict):
        for model, factor in raw_mult.items():
            if not isinstance(model, str) or not model.strip():
                continue
            if isinstance(factor, bool) or not isinstance(factor, (int, float)) or factor <= 0:
                log.warning(
                    "%s: upstream %r model_multipliers[%r] = %r is not a positive "
                    "number; that model falls back to 1.0",
                    path, name, model, factor,
                )
                continue
            multipliers[model.strip()] = float(factor)
    elif raw_mult is not None:
        log.warning(
            "%s: upstream %r has non-object model_multipliers; ignored", path, name,
        )

    allowed: list[str] = []
    raw_allowed = entry.get("allowed_models")
    if isinstance(raw_allowed, list):
        for model in raw_allowed:
            if isinstance(model, str) and model.strip() and model.strip() not in allowed:
                allowed.append(model.strip())
    elif raw_allowed is not None:
        log.warning(
            "%s: upstream %r has non-list allowed_models; ignored", path, name,
        )

    # v0.119：linked_upstreams —— 同平台 peer 上游名列表。把这些上游的统
    # 计合并显示（传递闭包：若 A+B 且 A+C ⇒ A+B+C），但不影响路由/计费
    # /quota 配置。空 list = 不链接（默认；与旧配置完全兼容）。
    # 同平台不存在的 name 暂时只 warn + 忽略 —— 不让链接关系死锁「先存
    # A 才能存 B」（用户在 GUI 同一面板编辑两个上游时的典型时序）。
    linked: list[str] = []
    raw_linked = entry.get("linked_upstreams")
    if isinstance(raw_linked, list):
        for peer in raw_linked:
            if not isinstance(peer, str):
                continue
            peer = peer.strip()
            if not peer or peer == name or peer in linked:
                continue
            linked.append(peer)
    elif raw_linked is not None:
        log.warning(
            "%s: upstream %r has non-list linked_upstreams; ignored", path, name,
        )

    # v0.65: model 字段。客户端发的 model 字段会被强制替换成这个值。
    raw_model = entry.get("model")
    model: Optional[str] = None
    if isinstance(raw_model, str) and raw_model.strip():
        model = raw_model.strip()
    elif raw_model is not None:
        log.warning(
            "%s: upstream %r has non-string model %r; ignored",
            path, name, raw_model,
        )

    # v0.74: default_model 字段。客户端发 "auto" 时的兜底模型名。
    # 跟 model 同款 fail-open —— 非字符串或空串都退回到 None,
    # 让 proxy.py 自己走 allowed_models[0] 兜底链。
    raw_default_model = entry.get("default_model")
    default_model: Optional[str] = None
    if isinstance(raw_default_model, str) and raw_default_model.strip():
        default_model = raw_default_model.strip()
    elif raw_default_model is not None:
        log.warning(
            "%s: upstream %r has non-string default_model %r; ignored",
            path, name, raw_default_model,
        )

    # v0.66: billing_unit / token_fields
    # v0.98.3: 非空字符串一律保留（count / token / 插件计费器名）。
    raw_bu = entry.get("billing_unit")
    billing_unit: Optional[str] = None
    if isinstance(raw_bu, str) and raw_bu.strip():
        billing_unit = raw_bu.strip()

    # v0.11.18 高级切换字段（实验性）。fail-open：非预期类型一律忽略，
    # 落到 PlatformConfig 默认值。
    advanced_switch = bool(entry.get("advanced_switch", False))

    def _opt_str(key: str) -> Optional[str]:
        v = entry.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    advanced_fallback = entry.get("advanced_fallback", "weak")
    if advanced_fallback not in ("weak", "strong"):
        advanced_fallback = "weak"

    # v0.11.19 thinking_options：该上游可用思考挡位列表。
    # 词表覆盖各厂商真实取值（不止 OpenAI 7 档）：
    #   off/none 关闭；minimal/low/medium/high/xhigh/max 强度档；enabled 开关节点。
    #   none 与 off 语义等价（OpenAI/Gemini 用 none 表示关闭），一并收下以免误删。
    _KNOWN_THINKING = {"off", "none", "minimal", "low", "medium", "high", "xhigh", "max", "enabled"}
    raw_to = entry.get("thinking_options")
    thinking_options: list[str] = []
    if isinstance(raw_to, list):
        thinking_options = [s.strip() for s in raw_to if isinstance(s, str) and s.strip() in _KNOWN_THINKING]

    _KNOWN = {"input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens"}
    token_fields: dict[str, bool] = {}
    raw_tf = entry.get("token_fields")
    if isinstance(raw_tf, dict):
        for k, v in raw_tf.items():
            if k in _KNOWN and isinstance(v, bool):
                token_fields[k] = v
        if raw_tf and not token_fields:
            log.warning(
                "%s: upstream %r has invalid token_fields %r; ignored",
                path, name, raw_tf,
            )

    return PlatformConfig(
        name=name.strip(),
        url=url.strip(),
        api_key=api_key or None,
        auth_header=auth_header.strip() if auth_header else None,
        note=note if isinstance(note, str) else None,
        quota_5h=quota_5h,
        model_multipliers=multipliers,
        allowed_models=allowed,
        linked_upstreams=linked,
        model=model,
        default_model=default_model,
        billing_unit=billing_unit or "count",
        token_fields=token_fields,
        # v0.8.1: dual-protocol flag。upstreams.json 里如果写了
        # ``true``，必须显式读出来传给 PlatformConfig；不显式读的
        # 话 pydantic 实例里永远是默认 False，proxy.py 的 adapter 入
        # 口永远进不去。bool() 把 True/False 都吃进来，缺失时落回
        # False —— 跟 pydantic 字段默认值一致，不影响其他上游。
        requires_anthropic_adapter=bool(entry.get("requires_anthropic_adapter", False)),
        # v0.98.3: 插件适配器标识（元数据）。None = 未指定。
        adapter=_opt_str("adapter"),
        thinking_options=thinking_options,
        # v0.200 该上游支持图片输入的模型名（裸模型名）。容错：非字符串
        # 项丢弃，缺失 = 空列表（该上游无多模态声明）。
        vision_models=[
            s.strip() for s in (entry.get("vision_models") or [])
            if isinstance(s, str) and s.strip()
        ],
        # v0.12 协议声明（docs/wire-dispatch-plan.md §3）。fail-open：未知
        # wire / auth_style 一律 warn + 当未声明（None），加载时按平台段推断。
        # v0.98：KNOWN_WIRES + 插件注册的 wire（extra_wires()）都算已知。
        wire=_opt_known(entry, "wire", KNOWN_WIRES + tuple(extra_wires()), path, name),
        endpoint=_opt_nonempty_str(entry, "endpoint"),
        # v0.98.3：auth_style 允许插件鉴权方案名（非空字符串即保留）。
        auth_style=_opt_nonempty_str(entry, "auth_style"),
        advanced_switch=advanced_switch,
        advanced_weak_model=_opt_str("advanced_weak_model"),
        advanced_strong_platform=_opt_str("advanced_strong_platform"),
        advanced_strong_upstream=_opt_str("advanced_strong_upstream"),
        advanced_strong_model=_opt_str("advanced_strong_model"),
        advanced_fallback=advanced_fallback,
        advanced_rules=bool(entry.get("advanced_rules", True)),
        advanced_dry_run=bool(entry.get("advanced_dry_run", False)),
    )


def apply_to_settings(settings, path: str | os.PathLike[str] | None = None) -> Optional[Path]:
    """Overlay the JSON file onto an existing `Settings` instance.

    Mutates `settings` in place and returns the path that was applied, or
    ``None`` if the file contributed nothing (missing / malformed / empty).

    A platform section only takes effect if it yields at least one valid
    upstream — otherwise a typo that invalidates every entry would leave the
    relay with an empty list and every request would 500.
    """
    p = Path(path if path is not None else getattr(settings, "relay_upstreams_file", DEFAULT_FILENAME))
    raw = load(p)
    if raw is None:
        return None

    applied = False
    # v0.12：单池单 active。两种格式都认：
    #   扁平：{"active": "...", "upstreams": [...]}（无平台 section）
    #   旧：{"anthropic": {...}, "openai": {...}} → 合并成单池（跨段重名加后缀）
    has_platform_section = any(k in raw for k in PLATFORMS)
    flat_raw = raw.get("upstreams")
    if not has_platform_section:
        if isinstance(flat_raw, list):
            configs: list[PlatformConfig] = []
            for i, entry in enumerate(flat_raw):
                cfg = _coerce_entry(None, entry, i, p)
                if cfg is not None:
                    configs.append(cfg)
            if configs:
                settings.upstreams = configs
                applied = True
        active = raw.get("active")
        if isinstance(active, str) and active.strip():
            settings.active = active.strip()
            applied = True
    else:
        merged: list[PlatformConfig] = []
        seen: set[str] = set()
        active_name = ""
        for platform in PLATFORMS:
            section = raw.get(platform)
            if not isinstance(section, dict):
                continue
            entries = section.get("upstreams")
            if isinstance(entries, list):
                for i, entry in enumerate(entries):
                    cfg = _coerce_entry(platform, entry, i, p)
                    if cfg is None:
                        continue
                    if cfg.name in seen:
                        cfg = cfg.model_copy(update={"name": f"{cfg.name}-{platform}"})
                    seen.add(cfg.name)
                    merged.append(cfg)
            elif entries is not None:
                log.warning("%s: %s.upstreams must be a list; ignored", p, platform)
            act = section.get("active")
            if isinstance(act, str) and act.strip() and not active_name:
                active_name = act.strip()
        if merged:
            settings.upstreams = merged
            settings.active = active_name or merged[0].name
            applied = True

    # v0.69 sidebar "快捷切换" —— 顶层 quick_switch 数组不在 PLATFORMS
    # 循环里,所以单独处理。每条必须同时有 label / platform / upstream
    # / model 四个字符串字段,任何一条缺字段就 warn + skip(不阻塞整体 load)。
    # 空 list 也合法 —— 用户暂未配置时 settings.quick_switch = []。
    qs_raw = raw.get("quick_switch")
    if qs_raw is not None:
        if not isinstance(qs_raw, list):
            log.warning("%s: quick_switch must be a list; ignored", p)
        else:
            valid: list[dict[str, str]] = []
            for i, item in enumerate(qs_raw):
                if not isinstance(item, dict):
                    log.warning("%s: quick_switch[%d] is not an object; skipped", p, i)
                    continue
                label = item.get("label")
                plat  = item.get("platform")
                ups   = item.get("upstream")
                model = item.get("model")
                if not (
                    isinstance(label, str) and label.strip()
                    and isinstance(plat, str) and plat.strip()
                    and isinstance(ups, str) and ups.strip()
                    and isinstance(model, str) and model.strip()
                ):
                    log.warning(
                        "%s: quick_switch[%d] missing/invalid label/platform/upstream/model; skipped",
                        p, i,
                    )
                    continue
                valid.append({
                    "label": label,
                    "platform": plat,
                    "upstream": ups,
                    "model": model,
                })
            settings.quick_switch = valid

    # v0.155/v0.159 平台别名 —— 顶层 agent_aliases 对象。fail-open：非
    # 预期类型一律落空 dict，绝不因配置损坏阻塞整个 load。纯展示层映射。
    # v0.159 起 value 是 ``{name, color}`` 对象；旧 string value（v0.155
    # 仅 name）自动升级成 ``{name: <v>, color: None}``，下次写盘会被落
    # 成新格式 —— 透明迁移，老配置文件无需手动改。
    aa_raw = raw.get("agent_aliases")
    if aa_raw is not None:
        if not isinstance(aa_raw, dict):
            log.warning("%s: agent_aliases must be an object; ignored", p)
        else:
            aliases: dict[str, dict[str, Optional[str]]] = {}
            for k, v in aa_raw.items():
                if not (isinstance(k, str) and k.strip()):
                    continue
                if isinstance(v, str):
                    # v0.155 旧格式：字符串 = 仅 name
                    if v.strip():
                        aliases[k.strip()] = {"name": v.strip(), "color": None}
                elif isinstance(v, dict):
                    name = v.get("name")
                    color = v.get("color")
                    if not (isinstance(name, str) and name.strip()):
                        continue
                    entry: dict[str, Optional[str]] = {
                        "name": name.strip(),
                        "color": (
                            color.strip()
                            if isinstance(color, str) and color.strip()
                            else None
                        ),
                    }
                    # v0.162：per-row 显示模式。view 必须是三个白名单值之一，
                    # 其它（坏数据 / 旧字段名 / None）落默认 both（不写 view 键）。
                    view = v.get("view")
                    if view in ("both", "requests", "tokens"):
                        entry["view"] = view
                    aliases[k.strip()] = entry
                # 其它类型（非字符串非对象）静默跳过
            settings.agent_aliases = aliases

    # v0.157 UA 归类 —— 顶层 ua_rules 对象（整串 UA → 平台名）。fail-open
    # 同 agent_aliases。**中继进程在请求入口消费**（resolve_agent 精确匹
    # 配，命中时平台名直接落库 requests.agent），不是纯展示层。
    ur_raw = raw.get("ua_rules")
    if ur_raw is not None:
        if not isinstance(ur_raw, dict):
            log.warning("%s: ua_rules must be an object; ignored", p)
        else:
            rules: dict[str, str] = {}
            for k, v in ur_raw.items():
                if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip():
                    rules[k.strip()] = v.strip()
            settings.ua_rules = rules

    # v0.11.18 高级切换 —— 顶层 advanced_switch 对象。fail-open：非
    # 预期类型一律落默认值，绝不因配置损坏阻塞整个 load。
    adv_raw = raw.get("advanced_switch")
    if adv_raw is not None:
        if not isinstance(adv_raw, dict):
            log.warning("%s: advanced_switch must be an object; ignored", p)
        else:
            def _adv_str(key: str) -> Optional[str]:
                v = adv_raw.get(key)
                return v.strip() if isinstance(v, str) and v.strip() else None

            def _adv_bool(key: str) -> bool:
                v = adv_raw.get(key)
                return bool(v) if isinstance(v, bool) else False

            adv_types = adv_raw.get("strong_types")
            settings.advanced_switch = _adv_bool("enabled")
            settings.advanced_weak_upstream = _adv_str("weak_upstream")
            settings.advanced_weak_model = _adv_str("weak_model")
            settings.advanced_strong_upstream = _adv_str("strong_upstream")
            settings.advanced_strong_model = _adv_str("strong_model")
            settings.advanced_analysis_upstream = _adv_str("analysis_upstream")
            settings.advanced_analysis_model = _adv_str("analysis_model")
            settings.advanced_strong_types = (
                [t.strip() for t in adv_types if isinstance(t, str) and t.strip()]
                if isinstance(adv_types, list) else []
            )
            settings.advanced_aggressive = _adv_bool("aggressive")
            settings.advanced_learning = _adv_bool("learning")

    # v0.113o 报错分析 —— 顶层 error_analysis 对象。fail-open：非预期
    # 类型一律落默认值，绝不因配置损坏阻塞整个 load。
    ea_raw = raw.get("error_analysis")
    if ea_raw is not None:
        if not isinstance(ea_raw, dict):
            log.warning("%s: error_analysis must be an object; ignored", p)
        else:
            def _ea_str(key: str) -> Optional[str]:
                v = ea_raw.get(key)
                return v.strip() if isinstance(v, str) and v.strip() else None

            settings.error_analysis_enabled = (
                bool(ea_raw.get("enabled")) if isinstance(ea_raw.get("enabled"), bool) else False
            )
            settings.error_analysis_upstream = _ea_str("upstream")
            settings.error_analysis_model = _ea_str("model")

    # v0.188 支持图片的模型 —— 顶层 vision_models 列表。fail-open：非
    # list 一律落默认空列表，绝不因配置损坏阻塞整个 load。
    vm_raw = raw.get("vision_models")
    if isinstance(vm_raw, list):
        settings.vision_models = [
            s.strip() for s in vm_raw if isinstance(s, str) and s.strip()
        ]

    if applied:
        log.info("upstreams loaded from %s", p)
        return p
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _dump(path: Path, obj: dict[str, Any]) -> None:
    """Write JSON to `path` via a temp file + replace, so a crash mid-write
    can't leave a truncated config behind."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def set_active(path: str | os.PathLike[str], platform: str, name: str) -> bool:
    """Persist 单池 `active = name` 到 JSON 文件（v0.12 扁平格式）。

    旧 per-platform 格式兼容：若文件还是 {"anthropic": {...}} 结构，则
    写进对应 section 的 active。返回 False 表示写不进（文件缺失/损坏）。
    """
    p = Path(path)
    raw = load(p)
    if raw is None:
        return False
    # v0.12 扁平格式优先
    if isinstance(raw.get("upstreams"), list):
        raw["active"] = name
    else:
        section = raw.get(platform)
        if not isinstance(section, dict):
            log.warning("%s: no %r section to record active=%r into", p, platform, name)
            return False
        section["active"] = name
    try:
        _dump(p, raw)
    except OSError as exc:
        log.warning("could not write %s: %s", p, exc)
        return False
    return True


def seed_from_settings(settings, path: str | os.PathLike[str] | None = None) -> Optional[Path]:
    """Create the JSON file from the current env-derived config, once.

    v0.12 写扁平格式 {"active": ..., "upstreams": [...]}。返回写出的路径，
    已存在或写失败返回 None。
    """
    p = Path(path if path is not None else getattr(settings, "relay_upstreams_file", DEFAULT_FILENAME))
    if p.exists():
        return None
    cfgs = settings.upstreams_for()
    if not cfgs:
        return None
    obj: dict[str, Any] = {
        "active": settings.active_for(),
        "upstreams": [
            {
                k: v
                for k, v in (
                    ("name", c.name),
                    ("url", c.url),
                    ("api_key", c.api_key),
                    ("auth_header", c.auth_header),
                    ("note", c.note),
                    ("quota_5h", c.quota_5h),
                    ("model_multipliers", c.model_multipliers or None),
                    ("allowed_models", c.allowed_models or None),
                    ("linked_upstreams", c.linked_upstreams or None),
                    ("model", c.model),
                    ("default_model", c.default_model),
                    ("billing_unit", c.billing_unit),
                    ("token_fields", c.token_fields or None),
                    # v0.200 该上游支持图片输入的模型名。空列表不落盘
                    # （加载时默认 []，与未声明一致）。
                    ("vision_models", c.vision_models or None),
                    # v0.12 协议声明（None 不落盘，加载时按入口推导）
                    ("wire", c.wire),
                    ("endpoint", c.endpoint),
                    ("auth_style", c.auth_style),
                    ("adapter", c.adapter),
                )
                if v is not None
            }
            for c in cfgs
        ],
    }
    try:
        _dump(p, obj)
    except OSError as exc:
        log.warning("could not seed %s: %s", p, exc)
        return None
    log.info("seeded %s from env config — edit it to add APIs / nodes", p)
    return p
