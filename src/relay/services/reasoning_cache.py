"""``ReasoningCache`` —— cross-wire reasoning_content 绑定缓存（V0.118+）。

从 ``proxy.py:1002-1073`` ``_REASONING_BY_TOOL_CALL`` / ``_MAX_REASONING_ENTRIES``
/ ``_bind_reasoning_to_tool_calls`` / ``_upstream_needs_reasoning`` /
``_inject_reasoning_to_messages`` 抽出。

设计要点（与原 proxy.py 一致，不允许行为漂移）：

* **key = openai tool_call id** —— 跨线（anthropic → openai-responses 等）
  往返保持不变；
* **有界防泄漏** —— 默认 512 entries，超出按插入序淘汰老的；
* **reasoning 字符串** —— 与 openai-responses 上游兼容（DeepSeek 系
  思考型上游必须 ``reasoning_content`` 字段存在）。
* **needs_reasoning 判定** —— DeepSeek 思考型 openai 上游才注入，
  MiniMax 等非思考上游不注入（注入空串反而 400）。
* **messages 注入** —— 就地改 messages；空串兜底（v0.97.2）。

⚠ **STABLE since v0.118**：method signature 冻结。
"""

from __future__ import annotations

import collections
import logging
from typing import Any, Iterable, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["ReasoningCache"]

log = logging.getLogger("relay.services.reasoning_cache")


_DEFAULT_MAX_ENTRIES = 512
_DEEPSEEK_NEEDLE = "deepseek"


class ReasoningCache:
    """cross-wire reasoning_content 绑定缓存。

    用法::

        cache = ReasoningCache()
        cache.apply(ctx)              # 注册 ctx.svc("reasoning")
        cache.bind("call_abc", "the reasoning text...")
        if cache.upstream_needs_reasoning(cfg, model):
            cache.inject(messages)
    """

    def __init__(self, *, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        # OrderedDict 行为：插入序 + 命中时移到末尾 + 超过 max 时淘汰最老
        self._by_tool_call: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._max = max_entries

    # ---- service 注册 ----

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("reasoning", self)

        def _dispose() -> None:
            self._by_tool_call.clear()

        ctx.add_disposer(_dispose)
        return _dispose

    # ---- 公开 API ----

    def bind(self, reasoning: str, tool_calls: Optional[Iterable[dict]]) -> None:
        """把 reasoning_content 绑定到所有带 id 的 tool_call。

        只处理 ``isinstance(tc, dict) and tc.get("id")`` 的条目；跨线
        转换 id 保持不变，所以 anthropic → openai-responses 等路由直接
        用同一 key 查回。
        """
        if not reasoning:
            return
        for tc in tool_calls or []:
            if isinstance(tc, dict) and tc.get("id"):
                self._by_tool_call[tc["id"]] = reasoning
                self._by_tool_call.move_to_end(tc["id"])
        # 超过 max_entries 按插入序淘汰
        while len(self._by_tool_call) > self._max:
            self._by_tool_call.popitem(last=False)

    def lookup(self, tool_call_id: str) -> Optional[str]:
        """按 tool_call id 查 reasoning_content。命中并 move_to_end（LRU）。"""
        if tool_call_id not in self._by_tool_call:
            return None
        self._by_tool_call.move_to_end(tool_call_id)
        return self._by_tool_call[tool_call_id]

    def upstream_needs_reasoning(self, cfg: Any, model: Optional[str]) -> bool:
        """判断该上游是否是 DeepSeek 系思考型（需要回传 ``reasoning_content``）。

        按 ``cfg.url / cfg.model / model`` 三处任一含 ``"deepseek"`` 子串
        即判定为需要。
        """
        needle = _DEEPSEEK_NEEDLE
        for s in (getattr(cfg, "url", "") or "",
                  getattr(cfg, "model", "") or "",
                  model or ""):
            if needle in s.lower():
                return True
        return False

    def inject(self, messages: Any, needs_reasoning: bool) -> None:
        """**就地改 messages**：给带 tool_calls 的 assistant 消息补
        ``reasoning_content`` 字段（查不到则空串兜底）。

        不动 ``messages`` 内已经存在的非空 ``reasoning_content``（那是
        真实推理内容，优先于兜底）。
        """
        if not needs_reasoning:
            return
        if not messages:
            return
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            tool_calls = m.get("tool_calls")
            if not tool_calls:
                continue
            if m.get("reasoning_content"):
                continue
            m["reasoning_content"] = ""
            for tc in tool_calls:
                rid = tc.get("id") if isinstance(tc, dict) else None
                if rid and rid in self._by_tool_call:
                    m["reasoning_content"] = self._by_tool_call[rid]
                    break

    def __len__(self) -> int:
        return len(self._by_tool_call)

    def clear(self) -> None:
        self._by_tool_call.clear()
