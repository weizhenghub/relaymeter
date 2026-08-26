"""识别 ``<think>...</think>`` 标记，把上游塞进正文的思考拆出来。

**背景**
部分思考型上游（DeepSeek R1 / GLM / Qwen QwQ / MiniMax M2 等）不把推理内容
放在专有字段（`reasoning_content` / `thinking_delta`）里，而是直接以
``<think>...</think>`` 标记包起来，混在普通 ``delta.content`` /
`` ``delta.text`` 流里。中继原本把整段当成正文，导致实时流面板的「思考」栏
空着、messages 表的 ``assistant_text`` 反带大段推理。

**做法**
``ThinkTagSplitter`` 是个字符级状态机，逐 chunk 喂入：
- 维护一个 *carry* 字符串，长度上界 = ``_MAX_TAG_LEN - 1``，专门装「可能
  还未成型的标签前缀」。SSE 切片可能把 ``<think>`` 切成 ``"thin"`` +
  ``"k>..."``，carry 就是用来接这种龙的。
- 每次 ``feed``：拼 carry+chunk 后，从头扫，**每扫到一个完整标签立刻切换
  模式并归类**；扫完一遍后，剩余尾巴里「能匹配某个标签前缀的最长后缀」留
  在 carry，其余全发射给调用方。
- 状态机有两态：``OUTSIDE``（找开标签）、``INSIDE``（找闭标签）。
- ``flush()`` 在流结束（``message_stop`` / ``[DONE]`` / 非流式响应结尾）
  时调用，把残余 carry 按当前状态归类（半个标签按字面归 text，**绝不**
  丢字符）。

**为什么不用正则回溯**
SSE 分片可能把 ``<think>`` 切成 ``"thin"`` + ``"k>..."``，简单正则要 lookahead
才知道那是标签；用状态机每字符 O(1) 推进更稳，且不会因为长正文回溯卡 CPU。

**单位**
入参 / 出参都是 ``str``（Python ``str`` 内部已是 Unicode，长度按 char 计）。
"""

from __future__ import annotations


# 标签常量。v0.103：先识别最常见的两种长度 —— ``<think>...</think>`` 和
# ``<think>...</think>``。其它变体（``<reasoning>`` 等）暂不识别，避免误拆。
# 用字符串拼接构造避免源码里 ``</think>`` / ``</thinking>`` 视觉混淆。
_OPEN_TAGS: tuple[str, ...] = (
    "<" + "think" + ">",
    "<" + "think" + ">",
)
_CLOSE_TAGS: tuple[str, ...] = (
    "<" + "/" + "think" + ">",
    "<" + "/" + "thinking" + ">",
)

# 滑动窗口上界。最长标签是 ``</think>`` 11 字符；上限取 10（即
# ``_MAX_TAG_LEN - 1``）就足够覆盖标签前缀（标签前 10 字符已经唯一确定
# 是哪个标签）；carry 永远不超过这个长度。
_MAX_TAG_LEN = 11


def _longest_tag_prefix_suffix(s: str, tags: tuple[str, ...]) -> str:
    """返回 ``s`` 中「能作为任一标签前缀的最长后缀」。

    用于确定 carry 的下界 —— 扫描完一轮后，剩下的尾巴只有这部分可能继续接
    成完整标签；其它内容都可以放心发射给调用方。
    """
    # 倒序遍历 s，找最长后缀使其是任一标签的前缀
    for length in range(min(len(s), _MAX_TAG_LEN - 1), 0, -1):
        suffix = s[-length:]
        for tag in tags:
            if tag.startswith(suffix):
                return suffix
    return ""


class ThinkTagSplitter:
    """把一段上游文本里 ``<think>...</think>`` 包裹的内容拆成单独的 thinking。

    用法::

        sp = ThinkTagSplitter()
        for chunk in chunks:
            thinking_part, text_part = sp.feed(chunk)
            if thinking_part:
                emit_to_thinking(thinking_part)
            if text_part:
                emit_to_text(text_part)
        # 流结束后 flush 残留 carry（最后一段可能挂在标签内）
        t_tail, x_tail = sp.flush()
        emit(t_tail); emit(x_tail)

    线程安全：本类无共享状态，单次调用须独占；每个 SSE parser 实例化一个。
    """

    __slots__ = ("_inside", "_carry", "_thinking_closed")

    def __init__(self) -> None:
        self._inside: bool = False  # 当前是否在 <think> 内部
        self._carry: str = ""       # 上次未判定的尾部，下一次再决定归属
        # v0.111：是否已见过 `</think>`（思考内容结束）。供上层做
        # 「思考→正文衔接」阶段判定 —— 一旦闭合标记出现，思考内容不会再
        # 增长，此后只等正文。见 panel_pool._note_stage 的 "gap" 档。
        self._thinking_closed: bool = False

    # ---- 公共 API ------------------------------------------------------

    def feed(self, chunk: str) -> tuple[str, str]:
        """喂一段上游文本，返回 ``(thinking, text)`` —— *本次* 增量。

        本次调用不会保留已发射的正文 / 思考 —— 调用方应自己用 list 累积。
        仅 ``_carry`` 跨调用保留，用来跨 SSE chunk 边界拼接可能未完的标签。
        """
        if not chunk:
            return "", ""

        # 把 carry 拼起来，得到本轮的待扫描串
        s = self._carry + chunk
        out_think: list[str] = []
        out_text: list[str] = []

        i = 0
        n = len(s)
        while i < n:
            # 本轮扫描的目标标签 = 当前状态下应该出现的标签
            target_tags = _CLOSE_TAGS if self._inside else _OPEN_TAGS
            # 在 s[i:] 里找 target_tags 任一的最早出现位置
            pos, tag = _find_first_with_tag(s, i, target_tags)
            if pos < 0:
                # 没找到 —— 本轮扫描到此为止
                break
            if self._inside:
                # INSIDE：pos 之前归思考
                if pos > i:
                    out_think.append(s[i:pos])
                i = pos + len(tag)
                self._inside = False
                self._thinking_closed = True
            else:
                # OUTSIDE：pos 之前归正文
                if pos > i:
                    out_text.append(s[i:pos])
                i = pos + len(tag)
                self._inside = True

        # 扫描结束后，剩下的尾巴 s[i:] 不完整标签里可能的前缀作为下次 carry。
        # 计算「s[i:] 中能作为 target_tags 任一前缀的最长后缀」= carry 候选。
        tail_tags = _CLOSE_TAGS if self._inside else _OPEN_TAGS
        carry = _longest_tag_prefix_suffix(s[i:], tail_tags)
        self._carry = carry
        # 把 *carry 之前* 的剩余部分按当前状态发射 —— 注意：如果状态在循环中
        # 翻转过（最后停在 INSIDE），剩余部分里的「开标签前缀」也可能是闭标签
        # 的前缀（在 INSIDE 模式下我们关心的是闭标签），所以 carry 候选就是
        # 当前状态对应标签集的前缀；其余字符按状态发射。
        leftover = s[i:][:len(s[i:]) - len(carry)]
        if leftover:
            if self._inside:
                out_think.append(leftover)
            else:
                out_text.append(leftover)

        return "".join(out_think), "".join(out_text)

    def flush(self) -> tuple[str, str]:
        """流结束时调用一次，把 ``_carry`` 残余按当前状态归类。

        残余可能是 ``<thin`` 这种半个标签 —— 此时按字面落进 text 即可，
        既不误吞也不丢失（用户看到半个标签本身已是上游 bug，不该再加工）。
        """
        tail = self._carry
        self._carry = ""
        if not tail:
            return "", ""
        if self._inside:
            return tail, ""
        return "", tail

    # ---- 状态查询 ------------------------------------------------------

    @property
    def thinking_closed(self) -> bool:
        """是否已见过 `</think>`（思考内容结束，之后只有正文）。"""
        return self._thinking_closed


def _find_first_with_tag(s: str, start: int, tags: tuple[str, ...]) -> tuple[int, str]:
    """在 ``s[start:]`` 里找 tags 中任一的最早出现位置。

    返回 ``(position, matched_tag)``；未找到返回 ``(-1, "")``。
    调用方用 ``len(matched_tag)`` 跳到标签之后 —— 开闭标签长度不同（7 vs 8
    vs 11），不可假设常数。
    """
    best_pos = -1
    best_tag = ""
    for tag in tags:
        p = s.find(tag, start)
        if p >= 0 and (best_pos < 0 or p < best_pos):
            best_pos = p
            best_tag = tag
    return best_pos, best_tag