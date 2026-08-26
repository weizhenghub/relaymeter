"""``AuthHeader`` —— 上游鉴权头拼装服务（V0.118+）。

集中化所有上游鉴权头构造逻辑（旧 proxy.py 散在 ``_check_auth`` /
``_apply_auth_override`` / ``_normalize_key`` / ``_auth_header_value`` /
``_client_auth_key`` 等处）。Phase 2.11 起横向模块 / proxy 都走
``ctx.svc("auth")`` 注入，不再读 proxy 模块级常量。

设计要点：

* **两类 header** —— ``Authorization: Bearer``（openai-style）/
  ``x-api-key: <key>``（anthropic-style）；
* **auth_style** —— ``bearer`` / ``x-api-key`` / ``none`` / 插件 custom；
* **插件覆盖** —— 允许 plugin author 通过 ``register_auth_scheme`` 提供
  自定义 header 名 / 值生成；Phase 2.11 起 ``apply_auth_override`` 会优先
  调插件注册表；
* **Sentinel AUTO** —— 当下游发的 key 是 ``"auto"`` 时返回 ``(None, True)``
  触发主动接管逻辑（与原 v0.97.3 行为一致）。

⚠ **STABLE since v0.118**：method signature 冻结。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from ..core import DisposerLike, RelayContext

__all__ = ["AuthHeader", "AUTO_SENTINEL"]

# 与原 proxy.py:923 ``AUTH_AUTO_SENTINEL = "auto"`` 等价
AUTO_SENTINEL = "auto"


# 平台默认 auth style
_DEFAULT_AUTH_STYLE: dict[str, str] = {
    "anthropic": "x-api-key",
    "anthropic-messages": "x-api-key",
    "openai": "bearer",
    "openai-responses": "bearer",
}


class AuthHeader:
    """上游鉴权头拼装。

    用法::

        auth = AuthHeader()
        auth.apply(ctx)              # 注册 ctx.svc("auth")
        name, value = auth.build_header("anthropic", api_key="sk-...")
        # -> ("x-api-key", "sk-...")
    """

    def __init__(self) -> None:
        # platform -> header name（可由插件覆盖：register_auth_scheme）
        self._header_by_platform: dict[str, str] = dict(_DEFAULT_AUTH_STYLE)

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("auth", self)

        def _service_dispose() -> None:
            """service 内部状态还原 —— 由 ctx.dispose() 自动级联反序触发。"""
            self._header_by_platform.clear()
            self._header_by_platform.update(_DEFAULT_AUTH_STYLE)

        # 把 service 内部清理挂到 ctx 的 dispose 链上（与 register 的
        # _unregister 分开：前者清状态，后者删 key；都跑，反序）。
        ctx.add_disposer(_service_dispose)
        return _service_dispose

    # ---- 公开 API ----

    def register_header(self, platform: str, header_name: str) -> None:
        """插件覆盖：对 platform 使用非默认 header 名。

        例子：注册 ``auth_scheme(platform="minimax", header="X-Custom-Auth")``。
        """
        self._header_by_platform[platform] = header_name

    def build_header(
        self,
        platform: str,
        *,
        api_key: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """构造上游鉴权头。返回 ``(header_name, header_value)``。

        * ``api_key is None`` 或空串 → ``(None, None)``（无鉴权）
        * ``api_key == AUTO_SENTINEL`` → ``(None, None)`` + 标记 sentinel
          （由调用方决定下一步行为；约定返回第二个元素为 None 时
          视为「客户端 auth 字段不可用」）
        """
        if not api_key or api_key == AUTO_SENTINEL:
            return None, None
        header_name = self._header_by_platform.get(platform, "bearer")
        if header_name == "bearer":
            return "authorization", f"Bearer {api_key}"
        return header_name, api_key

    def default_style(self, platform: str) -> str:
        """查 platform 的默认 header 名。"""
        return self._header_by_platform.get(platform, "bearer")

    def is_auto_sentinel(self, value: Optional[str]) -> bool:
        """判定客户端发的 key 是否是 ``"auto"`` sentinel。"""
        return bool(value) and value.strip().lower() == AUTO_SENTINEL
