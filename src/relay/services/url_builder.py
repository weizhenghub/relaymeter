"""``UrlBuilder`` —— 上游 URL 拼接服务（V0.118+）。

从 ``proxy.py:344-360`` ``_anthropic_messages_url(base_url)`` 抽出 +
适配 openai / openai-responses。

设计要点（与原 proxy.py 一致）：

* **三种上游 URL 形态**：
  - ``.../v1/messages`` → 原样（已完整）
  - ``.../v1`` → 补 ``/messages``
  - 其他 → 补 ``/v1/messages``
* **多平台路由** —— ``messages_url(platform, base)`` 自动选 platform 对应
  的 path template；
* **normalize** —— ``rstrip("/")`` 一律先做，避免 ``//v1`` 双斜杠；
* **纯函数** —— 不持有状态，单例即可。

⚠ **STABLE since v0.118**：method signature 冻结。
"""

from __future__ import annotations

from typing import Optional

from ..core import DisposerLike, RelayContext

__all__ = ["UrlBuilder"]


# platform -> 路径模板后缀
_PLATFORM_TAIL = {
    "anthropic": "/v1/messages",
    "openai": "/v1/chat/completions",
    "openai-responses": "/v1/responses",
    "anthropic-messages": "/v1/messages",
}


class UrlBuilder:
    """上游 URL 拼接；纯函数式，可单例。

    用法::

        ub = UrlBuilder()
        ub.apply(ctx)              # 注册 ctx.svc("url_builder")
        url = ub.messages_url("anthropic", "https://api.example.com/v1")
        # -> "https://api.example.com/v1/messages"
    """

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("url_builder", self)
        return lambda: None

    # ---- 公开 API ----

    def messages_url(self, platform: str, base_url: str) -> str:
        """根据 platform 选模板，返回拼好的完整 URL。"""
        tail = _PLATFORM_TAIL.get(platform, "/v1/messages")
        return self._join(base_url, tail)

    def anthropic_messages_url(self, base_url: str) -> str:
        return self.messages_url("anthropic", base_url)

    def openai_chat_url(self, base_url: str) -> str:
        return self.messages_url("openai", base_url)

    def openai_responses_url(self, base_url: str) -> str:
        return self.messages_url("openai-responses", base_url)

    # ---- 内部 ----

    @staticmethod
    def _join(base_url: str, tail: str) -> str:
        """拼接 base + tail，处理三种 tail 已存在形态：

        - base 已是 ``<tail>`` → 原样
        - base 是 ``<stem>/v1`` 且 tail 是 ``/v1/...`` → ``<stem>/v1/<inner>``
          （即 ``.../v1/messages``）
        - 其他 → ``<base>/<tail>``（自动补 ``/v1`` 段）
        """
        base = base_url.rstrip("/")
        if base.endswith(tail):
            return base
        if base.endswith("/v1") and tail.startswith("/v1/"):
            # 去掉 tail 的 /v1 前缀，避免双 /v1
            return base + tail[len("/v1"):]
        return base + tail
