"""Anthropic Messages API SSE usage parser.

Spec we care about (subset):
- Frames are delimited by a blank line (\\n\\n).
- Each frame has zero or more `event: <name>` and `data: <json>` lines.
- `event: message_start`  -> data.message.usage.{input_tokens, cache_creation_input_tokens, cache_read_input_tokens}
- `event: message_delta`   -> data.usage.output_tokens (CUMULATIVE — overwrite, don't sum)
- `event: content_block_delta` -> data.delta.text (TEXT FRAGMENT — append)
- `event: message_stop`    -> stream end (not a usage event; just a marker)

Edge cases:
- Frame split across chunk boundaries — partial frames stay in self._buf.
- Multiple `data:` lines per frame — concatenated before JSON parse.
- `data:` with or without leading space — stripped either way.
- Malformed JSON — silently ignored, parser continues.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import UsageAcc
from .think_split import ThinkTagSplitter


log = logging.getLogger("relay.parsers.anthropic")


class AnthropicUsageParser:
    """Stateful parser fed raw response bytes; returns a UsageAcc on finalize.

    Also accumulates the assistant text so callers can save the full reply.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.usage = UsageAcc()
        # Text accumulation: list of string fragments in arrival order. Used
        # to assemble the final assistant reply for session storage.
        self._text_chunks: list[str] = []
        # Tool-use block accumulator. Keyed by the SSE content_block index.
        # Each entry: {"id": str, "name": str, "partial_json": str}.
        # Populated on `content_block_start` (id/name), grown on every
        # `input_json_delta` (partial_json concatenated). Used to
        # synthesise an Anthropic assistant message JSON for tool-using
        # turns — otherwise the messages table would have `assistant_text=NULL`
        # and the GUI dialog has nothing to show.
        self._tool_use_blocks: dict[int, dict] = {}
        # v0.89：extended-thinking 正文累积。此前 `thinking_delta` 被直接
        # 丢弃，实时流面板与 messages 表都看不到模型的推理过程。与
        # `_text_chunks` 严格分开——thinking 不能混进正文，否则对话记录里
        # 推理和回答会串成一团。`signature_delta` 仍然跳过（那是签名校验
        # 数据，不是给人看的内容）。
        self._thinking_chunks: list[str] = []
        # v0.103：识别 ``<think>...</think>`` 内联标记 —— 部分思考型上游
        # （DeepSeek R1 / GLM / Qwen QwQ / MiniMax M2 等）把推理直接塞在
        # ``delta.text`` 里。splitter 是字符级状态机，跨 chunk 边界安全
        # （``<think>`` 可能被切成 ``"thin"`` + ``"k>..."``）。
        self._think_split = ThinkTagSplitter()
        # v0.111：extended-thinking 的 content_block 索引。Anthropic 协议
        # 流式响应里思考块 index 通常为 0（随后 text 块 index 为 1）。只有
        # 在 content_block_start 里看到 type=="thinking" 才记录 —— 其它
        # 块（text / tool_use）不设，避免把 text 块的 stop 误判成思考结束。
        self._thinking_block_index: Optional[int] = None
        # v0.111：思考块是否已结束（content_block_stop）。见
        # ``thinking_finished`` —— 它是「思考→正文衔接期（gap）」的权威信号。
        self._thinking_finished: bool = False
        # The full raw JSON response (non-streaming path only). Streaming
        # responses can't be cleanly re-serialised, so we leave this None.
        self.raw_response: Optional[bytes] = None
        # v0.87：Anthropic 协议的 `message_stop` 事件标记回复结束。某些上游
        # （opencode.ai/zen/go 等）在 message_stop 后**不主动关闭 SSE 连接**，
        # 中继若一直 `aiter_bytes()` 等到上游关连接，phase=streaming 会挂着
        # 90s 直到 sweeper 强清（实时面板出现"流已结束还在 STREAMING"的幽灵
        # 行）。置 True 供 proxy 在收到 message_stop 后主动结束流。
        self.message_stop_seen = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf.extend(chunk)
        # Anthropic uses CRLF per the SSE spec; some clients send LF only.
        # Find the earliest frame-terminator match (whichever pattern occurs first).
        while True:
            sep_lf = self._buf.find(b"\n\n")
            sep_crlf = self._buf.find(b"\r\n\r\n")
            candidates = [s for s in (sep_lf, sep_crlf) if s >= 0]
            if not candidates:
                return
            sep = min(candidates)
            term_len = 4 if sep == sep_crlf else 2
            raw = bytes(self._buf[:sep])
            del self._buf[: sep + term_len]
            self._parse_frame(raw)

    def _parse_frame(self, raw: bytes) -> None:
        event: Optional[str] = None
        data_parts: list[bytes] = []
        for line in raw.split(b"\n"):
            # Strip a trailing CR just in case a stray byte survived normalization.
            if line.endswith(b"\r"):
                line = line[:-1]
            if line.startswith(b"event:"):
                event = line[len(b"event:") :].strip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                # Strip the optional single leading space per the SSE spec.
                data_parts.append(line[len(b"data:") :].lstrip(b" "))
        if event is None or not data_parts:
            return
        try:
            payload = json.loads(b"".join(data_parts).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            log.debug("anthropic: skip malformed frame event=%s err=%s", event, exc)
            return

        if event == "message_start":
            u = payload.get("message", {}).get("usage", {})
            self._absorb_usage(u)
        elif event == "message_delta":
            u = payload.get("usage", {})
            # Anthropic emits cumulative output_tokens across deltas — overwrite.
            # Some Anthropic-compatible upstreams (e.g. minnimax.chat) put the
            # final input_tokens + cache fields here instead of message_start.
            # Absorb everything; max() keeps the larger of conflicting values.
            self._absorb_usage(u)
        elif event == "content_block_start":
            # Tool-use blocks: capture id/name and prepare the
            # partial_json accumulator so subsequent input_json_delta
            # frames can be concatenated. Text blocks need no setup
            # here — their content arrives via content_block_delta.
            cb = payload.get("content_block") or {}
            if cb.get("type") == "tool_use":
                idx = payload.get("index")
                if idx is not None:
                    self._tool_use_blocks[idx] = {
                        "id": cb.get("id", ""),
                        "name": cb.get("name", ""),
                        "partial_json": "",
                    }
            elif cb.get("type") == "thinking":
                # v0.111：记录思考块的 index —— 它的 content_block_stop 就是
                # 「思考结束、正文待发」衔接期（gap）的可靠信号。
                idx = payload.get("index")
                if idx is not None:
                    self._thinking_block_index = idx
        elif event == "content_block_stop":
            # v0.111：思考块结束 → 思考内容已完成，之后只等正文。见
            # ``thinking_finished``。其它块（text/tool_use）的 stop 忽略。
            idx = payload.get("index")
            if idx is not None and idx == self._thinking_block_index:
                self._thinking_block_index = None
                self._thinking_finished = True
        elif event == "content_block_delta":
            # Extract text from any content_block_delta that carries a
            # `text` field. The Anthropic spec uses delta.type == "text_delta"
            # but compatible upstreams (e.g. minnimax.chat) sometimes omit
            # or mislabel the type — being permissive here means the live
            # panel + DB both capture assistant text. Known non-text deltas
            # are still skipped so we don't pollute the text stream with
            # tool-input JSON or thinking blocks.
            delta = payload.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "input_json_delta":
                # Tool-call input is delivered as a stream of JSON
                # fragments. Concatenate them so we can json.loads the
                # whole thing at the end.
                idx = payload.get("index")
                if idx is not None and idx in self._tool_use_blocks:
                    pj = delta.get("partial_json", "")
                    if pj:
                        self._tool_use_blocks[idx]["partial_json"] += pj
            elif delta_type == "thinking_delta":
                # v0.89：extended thinking 正文。累积到独立的 buffer，
                # **绝不能**落进 `_text_chunks`——否则推理过程会污染
                # 助手回复正文。注意这个分支必须排在下面的宽松 fallback
                # 之前：thinking_delta 的载荷字段是 `thinking`，但部分
                # 上游同时塞了 `text`，若先走 fallback 就串了。
                t = delta.get("thinking")
                if t:
                    self._thinking_chunks.append(t)
            elif delta_type == "signature_delta":
                # 签名校验数据，不是给人看的内容 —— 照旧跳过。
                pass
            else:
                t = delta.get("text")
                if t:
                    # v0.103：上游若在 ``delta.text`` 里嵌了 ``<think>...</think>``
                    # 标记（DeepSeek R1 / GLM / QwQ / MiniMax M2 等思考型模型），按标记
                    # 拆开归到 thinking / text 各自的累加器。splitter 本身是字符级
                    # 状态机 —— 标签跨 SSE chunk 边界被切成 ``"thin"`` + ``"k>..."``
                    # 也安全。
                    think_part, text_part = self._think_split.feed(t)
                    if think_part:
                        self._thinking_chunks.append(think_part)
                    if text_part:
                        self._text_chunks.append(text_part)
        elif event == "message_stop":
            # v0.87：流式回复结束标记。上游可能不关连接，proxy 靠这个提前 break。
            self.message_stop_seen = True
            # v0.103：把 ``ThinkTagSplitter`` 残留尾部冲出来 —— ``<think>...`` 或
            # ``...thin`` 这种半个标签若不冲，末尾几个字符会留在 carry 里丢给
            # 下一条消息（不同 request），造成跨请求串味。半个标签按字面归 text。
            tail_think, tail_text = self._think_split.flush()
            if tail_think:
                self._thinking_chunks.append(tail_think)
            if tail_text:
                self._text_chunks.append(tail_text)
        # Other events (content_block_*, ping) carry no usage.

    def _absorb_usage(self, u: dict) -> None:
        """Take the max of each usage field from a usage dict (if non-zero)."""
        for attr, key in (
            ("input_tokens", "input_tokens"),
            ("cache_creation_input_tokens", "cache_creation_input_tokens"),
            ("cache_read_input_tokens", "cache_read_input_tokens"),
        ):
            v = int(u.get(key, 0) or 0)
            if v:
                cur = getattr(self.usage, attr)
                setattr(self.usage, attr, max(cur, v))
        # output_tokens is special: cumulative, always take max.
        out = int(u.get("output_tokens", 0) or 0)
        self.usage.output_tokens = max(self.usage.output_tokens, out)

    def finalize(self) -> UsageAcc:
        """Process any leftover bytes (stream may end without a trailing blank line)."""
        if self._buf.strip():
            self._parse_frame(bytes(self._buf))
            self._buf.clear()
        return self.usage

    def assembled_text(self) -> str:
        """Concatenated assistant text from streaming content_block_delta frames."""
        return "".join(self._text_chunks)

    def assembled_thinking(self) -> Optional[str]:
        """v0.89：拼接 extended-thinking 正文（`thinking_delta` 累积）。

        无 thinking 内容时返回 None —— 与 `assembled_tool_use_json()` 的
        None 语义一致，调用方据此决定是否给 messages 表建 `role='thinking'`
        的行（纯文本回合不建，避免污染对话记录）。
        """
        if not self._thinking_chunks:
            return None
        return "".join(self._thinking_chunks)

    @property
    def thinking_finished(self) -> bool:
        """v0.111：思考内容是否已结束。

        True 的条件（任一命中即算）：
          * Anthropic extended thinking —— 收到思考块的 ``content_block_stop``；
          * ``<think>...</think>`` 内联标记 —— splitter 已见过 ``</think>``。

        用作实时流面板「思考→正文衔接期（gap）」的可靠信号：一旦思考结束，
        之后只有正文，静默就是衔接卡死而不是思考暂停。
        """
        return self._thinking_finished or self._think_split.thinking_closed

    def assembled_tool_use_json(self) -> Optional[str]:
        """Synthesise a synthetic Anthropic assistant message JSON for any
        tool_use blocks observed during streaming.

        Returns None if no tool_use blocks were seen (so the caller can
        leave `assistant_json=NULL` for pure-text replies). Otherwise
        returns a JSON string shaped like::

            {"role": "assistant",
             "content": [
                {"type": "tool_use", "id": "toolu_xxx",
                 "name": "read_file", "input": {"path": "..."}},
                ...
             ]}

        Block order matches the SSE `index` so the GUI's JSON view is
        stable across renders. If `partial_json` is malformed (stream
        interrupted mid-delta), the `input` field falls back to
        `{"_raw_partial": "..."}` so the dialog still has something to
        show instead of crashing.
        """
        if not self._tool_use_blocks:
            return None
        blocks: list[dict] = []
        for idx in sorted(self._tool_use_blocks):
            b = self._tool_use_blocks[idx]
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

    def extract_from_json(self, body: bytes) -> UsageAcc:
        """Extract usage from a non-streaming JSON response body.

        Anthropic's Messages endpoint returns `{"usage": {input_tokens, ...}}`
        when called without `stream: true`. OpenClaw's minnimax-compatible
        endpoint follows the same shape.
        """
        import json

        self.raw_response = body
        try:
            obj = json.loads(body or b"{}")
        except ValueError:
            return self.usage
        u = obj.get("usage", {}) if isinstance(obj, dict) else {}
        self.usage.input_tokens = max(
            self.usage.input_tokens, int(u.get("input_tokens", 0) or 0)
        )
        self.usage.output_tokens = max(
            self.usage.output_tokens, int(u.get("output_tokens", 0) or 0)
        )
        self.usage.cache_creation_input_tokens = max(
            self.usage.cache_creation_input_tokens,
            int(u.get("cache_creation_input_tokens", 0) or 0),
        )
        self.usage.cache_read_input_tokens = max(
            self.usage.cache_read_input_tokens,
            int(u.get("cache_read_input_tokens", 0) or 0),
        )
        # 非流式路径的完整助手文本同样在 `content[]` 里，块类型是 "text"。
        if isinstance(obj, dict) and not self._text_chunks:
            for block in obj.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    txt = block.get("text", "") or ""
                    if txt:
                        # v0.103：同流式路径，按 ``<think>...</think>`` 拆。
                        think_part, text_part = self._think_split.feed(txt)
                        if think_part:
                            self._thinking_chunks.append(think_part)
                        if text_part:
                            self._text_chunks.append(text_part)
        # v0.89：非流式响应的 thinking 同样在 `content[]` 里，块类型是
        # "thinking"，载荷字段是 `thinking`（不是 `text`）。与正文分开收，
        # 保持与流式路径一致的语义。
        if isinstance(obj, dict) and not self._thinking_chunks:
            for block in obj.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    t = block.get("thinking", "") or ""
                    if t:
                        self._thinking_chunks.append(t)
        return self.usage