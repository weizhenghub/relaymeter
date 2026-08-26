"""V0.3 扩展层首个真实插件 —— 火山引擎 Agent-Plan 协议转换适配。

把 ``C:\\Claude-Code\\anthropic_to_responses`` 转换库（纯标准库、实测
9 个模型可用）接入 wire 转换器注册表：

  - (anthropic-messages, openai-responses) -> anthropic_to_responses
  - (openai-chat,        openai-responses) -> 经 linguafranca 中转 anthropic 再转

注册后，任何 ``anthropic-messages -> openai-responses`` 的跨线请求都走
本插件的转换实现（不再用 linguafranca）。配置文件（upstreams.json）里给
火山上游写 ``adapter: "volcagent"`` 即声明由本插件接管（元数据，见
docs/plugins-api.md）。想限定时只要把 ``ANTHROPIC_TO_RESPONSES_PATH``
环境变量指向别处，或删掉本文件即可整体退回 linguafranca。

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


def _anthropic_to_responses(payload, src_wire, dst_wire):
    """(anthropic-messages -> openai-responses)：直接走火山转换库。"""
    return _a2r.anthropic_to_responses(payload)


def _openai_chat_to_responses(payload, src_wire, dst_wire):
    """(openai-chat -> openai-responses)：先经 linguafranca 转 anthropic，
    再走火山库转 responses。返回 None 让调用方整体回退 linguafranca。"""
    from relay.wire import convert_request

    anthropic = convert_request(payload, "openai-chat", "anthropic-messages")
    return _a2r.anthropic_to_responses(anthropic)


def apply(ctx) -> None:
    if not _LIB_OK:
        ctx.log.warning(
            "volcagent: anthropic_to_responses 库不可用（%s），跳过注册",
            _LIB_ROOT,
        )
        ctx.push_alert("volcagent 插件未生效：转换库不可用")
        return

    ctx.register_wire_converter(
        "anthropic-messages", "openai-responses", _anthropic_to_responses,
    )
    ctx.register_wire_converter(
        "openai-chat", "openai-responses", _openai_chat_to_responses,
    )
    ctx.log.info(
        "volcagent: registered wire converters (anthropic-messages|openai-chat) -> openai-responses"
    )
    ctx.push_alert("volcagent 插件已加载（V0.3 火山 Responses 转换）")
