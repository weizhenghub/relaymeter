"""per-request contextvars scope —— Cordis Context 风格的轻量版（v0.117+）。

为什么需要 ``scope()``：

proxy.py 的 ``stream_iter`` 在 SSE 每 chunk 上跑，热路径上**绝对不能** await
ctx 调用。但又需要在每个请求生命周期内绑一些临时值（rid / platform /
start time / in-flight 引用），让 ``post_response`` 钩子和 ``request.done``
事件能拿到——跨函数 / 跨生成器传递 dict 太丑。

``contextvars.ContextVar`` 是 Python 标准库专为「异步上下文隔离」设计的：
在 ``ctx.scope(rid=...)`` 块内启动的协程 + 生成器都能 ``ctx.current.rid``
拿到这个值；块退出自动还原。

⚠ **红线**：热路径不调 ``ctx.scope()``。scope 只在路由入口 / 中间件层用一次，
绑定后所有下游（async 生成器、async 函数）通过 ``ctx.current.rid`` 同步读。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any, Iterator, Optional

__all__ = ["RequestScope", "current_scope"]


log = logging.getLogger("relay.core.scope")


class RequestScope:
    """per-request 临时绑定的容器。

    设计：薄薄一层 dict + contextvars 包装。Phase 1 只承载 ``rid`` 字段
    （请求 ID）；Phase 2+ 横向模块可以加 ``platform`` / ``model`` /
    ``upstream`` 等。

    典型用法::

        # 路由入口
        with ctx.scope(rid=uuid4().hex, platform="anthropic") as req:
            return await relay(request, platform="anthropic", ...)

        # 下游任意位置
        rid = ctx.current.rid
    """

    __slots__ = ("_data", "_var", "_token")

    def __init__(self) -> None:
        # 默认空 scope；进程级 fallback 用
        self._data: dict[str, Any] = {}
        self._var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
            "relay_request_scope", default={},
        )
        self._token: Optional[contextvars.Token] = None

    # ---- 读写 ----

    def __getitem__(self, key: str) -> Any:
        return self._var.get()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        data = dict(self._var.get())
        data[key] = value
        self._var.set(data)

    def __contains__(self, key: str) -> bool:
        return key in self._var.get()

    def get(self, key: str, default: Any = None) -> Any:
        return self._var.get().get(key, default)

    def as_dict(self) -> dict[str, Any]:
        """返回当前 scope 的不可变快照（dict 拷贝）。"""
        return dict(self._var.get())

    # ---- context manager ----

    @contextlib.contextmanager
    def bind(self, **fields: Any) -> Iterator["RequestScope"]:
        """绑定一组字段到当前 contextvar；yield 期间 ``self[key]`` 可读，退出还原。"""
        parent = self._var.get()
        merged = dict(parent)
        merged.update(fields)
        token = self._var.set(merged)
        try:
            yield self
        finally:
            self._var.reset(token)


# 进程级单例——``RelayContext.current`` 直接返回它
current_scope = RequestScope()
