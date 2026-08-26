"""V0.122 新风格插件示例 —— 火山 Agent-Plan 协议转换（Service 风格）。

与 ``plugins/volc_agent.py``（V0.3 旧风格）**同一套转换实现**，但以
Phase 1+ 的 Service 形态书写：

* ``class VolcAgentService`` 实现 ``apply(ctx) -> DisposerLike``；
* 模块级 ``apply(ctx)`` 是 loader 契约的胶水 —— ``load_plugins`` 只认
  ``apply`` 函数，这里实例化 Service 并调用其 ``apply``；
* 返回 Disposer（本次无外部资源要释放，no-op）。

同时加载旧 ``volc_agent.py`` + 新 ``volc_agent_v2.py`` 不冲突 —— 转换器
是「覆盖式」语义（后者覆盖前者），二者实现一致，用户用哪个加载顺序都
得到相同行为。

插件契约：模块必须导出 ``apply(ctx)``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_LIB_ROOT = Path(os.environ.get(
    "ANTHROPIC_TO_RESPONSES_PATH",
    r"C:\Claude-Code\anthropic_to_responses",
))
# 包目录的父目录加入 sys.path 才能 `import anthropic_to_responses`
if str(_LIB_ROOT.parent) not in sys.path and _LIB_ROOT.is_dir():
    sys.path.insert(0, str(_LIB_ROOT.parent))

try:
    from anthropic_to_responses import request as _a2r  # noqa: F401
    _LIB_OK = True
except Exception:  # noqa: BLE001 - 库不可用则整体回退 linguafranca
    _a2r = None
    _LIB_OK = False


class VolcAgentService:
    """火山 Responses 转换（Service 风格）。"""

    name = "volcagent_v2"

    def apply(self, ctx) -> callable:
        """注册两个 wire converter + 推一条加载告警。返回 Disposer。"""
        if not _LIB_OK:
            ctx.log.warning(
                "volcagent_v2: anthropic_to_responses 库不可用（%s），跳过注册",
                _LIB_ROOT,
            )
            ctx.push_alert("volcagent_v2 插件未生效：转换库不可用")
            return lambda: None

        ctx.register_wire_converter(
            "anthropic-messages", "openai-responses", _anthropic_to_responses,
        )
        ctx.register_wire_converter(
            "openai-chat", "openai-responses", _openai_chat_to_responses,
        )
        ctx.log.info(
            "volcagent_v2: registered wire converters "
            "(anthropic-messages|openai-chat) -> openai-responses"
        )
        ctx.push_alert("volcagent_v2 插件已加载（Service 风格，V0.122）")

        # Service 风格要求 apply 返回 Disposer —— 本插件无外部资源要释放。
        def _dispose() -> None:
            ctx.log.info("volcagent_v2 dispose（无资源）")

        return _dispose


def _anthropic_to_responses(payload, src_wire, dst_wire):
    """(anthropic-messages -> openai-responses)：直接走火山转换库。"""
    return _a2r.anthropic_to_responses(payload)


def _openai_chat_to_responses(payload, src_wire, dst_wire):
    """(openai-chat -> openai-responses)：先经 linguafranca 转 anthropic，
    再走火山库转 responses。返回 None 让调用方整体回退 linguafranca。"""
    from relay.wire import convert_request

    anthropic = convert_request(payload, "openai-chat", "anthropic-messages")
    return _a2r.anthropic_to_responses(anthropic)


# ---- loader 契约胶水 ----
# load_plugins 只认模块级 apply(ctx)。这里实例化 Service 并委托其 apply。

_inst = VolcAgentService()


def apply(ctx) -> callable:
    return _inst.apply(ctx)
