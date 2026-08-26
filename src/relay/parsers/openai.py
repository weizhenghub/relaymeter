"""OpenAI Chat Completions / Responses API SSE usage parser.

Chat Completions (legacy + current):
- `data: {choices: [...], usage?: {prompt_tokens, completion_tokens, total_tokens}}`
- `data: [DONE]` ends the stream.
- Usage is normally only present in the final chunk when the client set
  `stream_options.include_usage: true`. Without it we record zeros.
- Each chunk may carry `choices[0].delta.content` — a text fragment.

Responses API (newer, used by some Codex flows):
- Multiple event types: `response.created`, `response.in_progress`, ...
- `event: response.completed` -> data.response.usage.{input_tokens, output_tokens}
- We only react to `response.completed`.

Edge cases (same as the Anthropic parser):
- Lines split across chunks, `data:` with/without leading space, malformed JSON.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import UsageAcc
from .think_split import ThinkTagSplitter


log = logging.getLogger("relay.parsers.openai")


def _extract_openai_cached_tokens(u: dict) -> int:
    """从 OpenAI usage dict 里提取缓存命中 token 数（各家命名不同，宽匹配）。

    返回 0 表示没缓存命中或字段缺失。
    """
    try:
        # 1. prompt_tokens_details.cached_tokens（OpenAI 官方）
        details = u.get("prompt_tokens_details") or {}
        v = int(details.get("cached_tokens", 0) or 0)
        if v:
            return v
        # 2. input_tokens_details.cached_tokens（部分兼容端）
        idets = u.get("input_tokens_details") or {}
        v = int(idets.get("cached_tokens", 0) or 0)
        if v:
            return v
        # 3. 顶层 prompt_cache_hit_tokens（opencode.ai/zen 实测返回）
        v = int(u.get("prompt_cache_hit_tokens", 0) or 0)
        if v:
            return v
        # 4. 顶层 cache_read_input_tokens（若上游已用 anthropic 命名）
        return int(u.get("cache_read_input_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


class OpenAIUsageParser:
    def __init__(self) -> None:
        self._buf = bytearray()
        self.usage = UsageAcc()
        self._saw_done = False
        # Streaming assistant text fragments; final reply is `assembled_text()`.
        self._text_chunks: list[str] = []
        # v0.97.2：推理内容累积（DeepSeek 系思考型上游的 `delta.reasoning_content`）。
        # 与 `_text_chunks` 严格分开——思考不能混进正文。proxy cross-wire 路径
        # 会在 `parser.feed` 之后才从 delta 上剥离 reasoning_content，这里先采
        # 集到，侧栏 thinking 栏与 messages 表 `role='thinking'` 行就都有值。
        self._thinking_chunks: list[str] = []
        # v0.103：识别 ``<think>...</think>`` 内联标记 —— 部分思考型上游
        # （DeepSeek R1 / GLM / Qwen QwQ / MiniMax M2 等）把推理直接塞在
        # ``delta.content`` 里。splitter 是字符级状态机，跨 chunk 边界安全
        # （``<think>`` 可能被切成 ``"thin"`` + ``"k>..."``）。
        self._think_split = ThinkTagSplitter()
        # v0.97.2：工具调用累积。OpenAI Chat 流式把 tool_call 按
        # `delta.tool_calls[].index` 分片推送：首片带 id/name，后续只带
        # function.arguments 碎片。按 index 归并，末尾组装成 anthropic
        # tool_use JSON 供侧栏「工具调用」栏展示。
        self._tool_call_blocks: dict[int, dict] = {}
        self.raw_response: Optional[bytes] = None

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf.extend(chunk)
        # OpenAI emits one JSON payload per `data:` line; lines are \n-terminated.
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                return
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            self._parse_line(line)

    def _parse_line(self, line: bytes) -> None:
        if not line or line.startswith(b":"):
            # Comment / heartbeat line.
            return
        if not line.startswith(b"data:"):
            return
        payload = line[len(b"data:") :].lstrip(b" ")
        if payload.strip() == b"[DONE]":
            self._saw_done = True
            return
        try:
            obj = json.loads(payload)
        except ValueError as exc:
            log.debug("openai: skip malformed chunk: %s", exc)
            return

        # Path 1: chat.completions — usage chunk (when stream_options.include_usage=true)
        if isinstance(obj.get("usage"), dict) and obj["usage"]:
            u = obj["usage"]
            self.usage.input_tokens = max(
                self.usage.input_tokens, int(u.get("prompt_tokens", 0) or 0)
            )
            self.usage.output_tokens = max(
                self.usage.output_tokens, int(u.get("completion_tokens", 0) or 0)
            )
            # v0.88：解析 OpenAI 缓存命中 token。命名各家不统一：
            #   - prompt_tokens_details.cached_tokens   （OpenAI 官方）
            #   - prompt_cache_hit_tokens               （opencode.ai/zen 实测返回）
            #   - input_tokens_details.cached_tokens    （某些兼容端）
            self.usage.cache_read_input_tokens = max(
                self.usage.cache_read_input_tokens,
                _extract_openai_cached_tokens(u),
            )
            return

# Path 2: Responses API — response.completed / response.incomplete / response.failed
        # 三个事件都可能携带 usage（被截断的 incomplete 也带 usage，含 cache 字段）。
        # 缺漏 incomplete 会导致 max_tokens 截断等场景下 cache 漏记。
        if obj.get("type") in ("response.completed", "response.incomplete", "response.failed"):
            u = obj.get("response", {}).get("usage", {})
            self.usage.input_tokens = max(
                self.usage.input_tokens, int(u.get("input_tokens", 0) or 0)
            )
            self.usage.output_tokens = max(
                self.usage.output_tokens, int(u.get("output_tokens", 0) or 0)
            )
            self.usage.cache_read_input_tokens = max(
                self.usage.cache_read_input_tokens,
                _extract_openai_cached_tokens(u),
            )

        # Path 3: chat.completions streaming text delta. Each choice carries
        # `delta.content` which is either a string (legacy) or null (when the
        # chunk only has a role/finish_reason). We only append when present.
        for choice in obj.get("choices") or []:
            delta = (choice or {}).get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                # v0.103：上游若在 ``delta.content`` 里嵌了 ``<think>...</think>``
                # 标记（DeepSeek R1 / GLM / QwQ / MiniMax M2 等思考型模型），按标记
                # 拆开归到 thinking / text 各自的累加器。splitter 是字符级状态机
                # —— 跨 SSE chunk 边界被切成 ``"thin"`` + ``"k>..."`` 也安全。
                think_part, text_part = self._think_split.feed(piece)
                if think_part:
                    self._thinking_chunks.append(think_part)
                if text_part:
                    self._text_chunks.append(text_part)
            # v0.97.2：思考型上游把推理放在 `delta.reasoning_content`（与 content
            # 同级，非标准字段）。采进独立 buffer，避免与正文混叠。
            rp = delta.get("reasoning_content")
            if isinstance(rp, str) and rp:
                self._thinking_chunks.append(rp)
            # v0.97.2：工具调用分片累积。`delta.tool_calls` 是数组，每个元素
            # 带 index；首片带 id/function.name，后续只带 function.arguments
            # 碎片。按 index 归并。
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index")
                if not isinstance(idx, int):
                    continue
                blk = self._tool_call_blocks.setdefault(
                    idx, {"id": "", "name": "", "partial_json": ""}
                )
                if tc.get("id"):
                    blk["id"] = tc["id"]
                fn = tc.get("function") or {}
                if isinstance(fn, dict):
                    if fn.get("name"):
                        blk["name"] = fn["name"]
                    args = fn.get("arguments")
                    if isinstance(args, str) and args:
                        blk["partial_json"] += args

    def finalize(self) -> UsageAcc:
        if self._buf.strip():
            self._parse_line(bytes(self._buf))
            self._buf.clear()
        # v0.103：把 ``ThinkTagSplitter`` 残留尾部冲出来 —— ``<think>...`` 或
        # ``...thin`` 这种半个标签若不冲，末尾几个字符会留在 carry 里丢给
        # 下一条消息（不同 request），造成跨请求串味。半个标签按字面归 text。
        tail_think, tail_text = self._think_split.flush()
        if tail_think:
            self._thinking_chunks.append(tail_think)
        if tail_text:
            self._text_chunks.append(tail_text)
        return self.usage

    @property
    def done(self) -> bool:
        return self._saw_done

    def assembled_text(self) -> str:
        """Concatenated assistant text from streaming delta.content chunks."""
        return "".join(self._text_chunks)

    def assembled_tool_use_json(self) -> Optional[str]:
        """v0.97.2：把流式累积的 tool_call 碎片组装成 anthropic assistant
        message JSON（与 AnthropicUsageParser.assembled_tool_use_json 同格式）。

        OpenAI Chat 流式 tool_call 按 `delta.tool_calls[].index` 分片，首片带
        id/name，后续只带 function.arguments 碎片；`_parse_line` 已按 index
        归并。无 tool_call 时返回 None（纯文本回合不建 assistant 行）。
        若 arguments 碎片被流中断成畸形 JSON，input 回退成 `{"_raw_partial"}`。
        """
        if not self._tool_call_blocks:
            return None
        blocks: list[dict] = []
        for idx in sorted(self._tool_call_blocks):
            b = self._tool_call_blocks[idx]
            pj = b["partial_json"]
            try:
                input_obj = json.loads(pj) if pj else {}
            except ValueError:
                input_obj = {"_raw_partial": pj}
            blocks.append({
                "type": "tool_use",
                "id": b["id"],
                "name": b["name"],
                "input": input_obj,
            })
        return json.dumps(
            {"role": "assistant", "content": blocks},
            ensure_ascii=False,
        )

    def assembled_thinking(self) -> Optional[str]:
        """v0.97.2：拼接流式累积的 `delta.reasoning_content` 推理正文。

        无推理内容时返回 None —— 调用方据此不建 `role='thinking'` 行，语义
        与 AnthropicUsageParser.assembled_thinking() 一致。proxy cross-wire
        路径虽然会把 reasoning_content 从 delta 上剥掉（避免污染给客户端的
        anthropic 流），但剥离发生在 `parser.feed` 之后，这里先采集到了，
        侧栏 thinking 栏与 messages 表都能拿到。
        """
        if not self._thinking_chunks:
            return None
        return "".join(self._thinking_chunks)

    @property
    def thinking_finished(self) -> bool:
        """v0.111：思考内容是否已结束。

        仅 ``<think>...</think>`` 内联标记有可靠信号（splitter 见过
        ``</think>``）。``reasoning_content`` 流没有结束标记 —— 只能靠
        正文出现才反推，那时衔接期已过。无信号时返回 False，调用方按
        思考档兜底（衔接期并入思考超时，与 v0.110 行为一致）。
        """
        return self._think_split.thinking_closed

    def extract_from_json(self, body: bytes) -> UsageAcc:
        """Extract usage from a non-streaming JSON response body.

        Handles both Chat Completions (`usage.prompt_tokens/completion_tokens`)
        and Responses API (`usage.input_tokens/output_tokens`).
        """
        import json

        self.raw_response = body
        try:
            obj = json.loads(body or b"{}")
        except ValueError:
            return self.usage
        u = obj.get("usage", {}) if isinstance(obj, dict) else {}
        self.usage.input_tokens = max(
            self.usage.input_tokens,
            int(u.get("prompt_tokens", u.get("input_tokens", 0)) or 0),
        )
        self.usage.output_tokens = max(
            self.usage.output_tokens,
            int(u.get("completion_tokens", u.get("output_tokens", 0)) or 0),
        )
        self.usage.cache_read_input_tokens = max(
            self.usage.cache_read_input_tokens,
            _extract_openai_cached_tokens(u),
        )
        # Non-streaming chat.completions has the full reply at
        # `choices[0].message.content`; Responses API stores it on
        # `output[].content[].text`.
        if isinstance(obj, dict) and not self._text_chunks:
            for choice in obj.get("choices") or []:
                msg = (choice or {}).get("message") or {}
                content = msg.get("content")
                if isinstance(content, str) and content:
                    # v0.103：同流式路径，按 ``<think>...</think>`` 拆。
                    think_part, text_part = self._think_split.feed(content)
                    if think_part:
                        self._thinking_chunks.append(think_part)
                    if text_part:
                        self._text_chunks.append(text_part)
                # v0.97.2：非流式同样采集思考 + 工具调用（思考型上游把推理放
                # `message.reasoning_content`，工具调用放 `message.tool_calls`）。
                rp = msg.get("reasoning_content")
                if isinstance(rp, str) and rp and not self._thinking_chunks:
                    self._thinking_chunks.append(rp)
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    args = ""
                    if isinstance(fn, dict):
                        a = fn.get("arguments")
                        args = a if isinstance(a, str) else json.dumps(fn, ensure_ascii=False)
                    blk = self._tool_call_blocks.setdefault(
                        i, {"id": "", "name": "", "partial_json": ""}
                    )
                    blk["id"] = tc.get("id") or blk["id"]
                    if isinstance(fn, dict) and fn.get("name"):
                        blk["name"] = fn["name"]
                    if args:
                        blk["partial_json"] += args
            # Responses API: output is a list of message objects.
            for out in obj.get("output") or []:
                for block in (out.get("content") or []) if isinstance(out, dict) else []:
                    if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                        t = block.get("text")
                        if isinstance(t, str) and t:
                            # v0.103：同流式路径，按 ``<think>...</think>`` 拆。
                            think_part, text_part = self._think_split.feed(t)
                            if think_part:
                                self._thinking_chunks.append(think_part)
                            if text_part:
                                self._text_chunks.append(text_part)
        return self.usage