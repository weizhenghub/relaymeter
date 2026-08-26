"""``AlertLog`` —— 分发告警服务（V0.118+）。

从 ``proxy.py:2413-...`` ``_ALERT_MAX`` / ``_ALERT_DEDUPE_WINDOW`` /
``_dispatch_alerts`` / ``_alert_seq`` / ``_alert_dedup`` 抽出。

设计要点（与原 proxy.py 一致）：

* **环形列表** —— 容量 ``_ALERT_MAX``（默认 50）；超出按插入淘汰；
* **去重窗口** —— 同一 ``(kind, platform, key, model)`` 在 5 分钟内只
  告警一次（防 `proxy_unknown_key` 类高频告警刷屏）；
* **monotonic seq** —— 每条告警发序号；前端可按 seq 去重 / 排序；
* **push() 公开 API** —— 横向模块 / 插件也可调，直接复用去重 + 环形；
* **list() 快照** —— 按时间倒序供读侧（如 routers/api.py）拿。

⚠ **STABLE since v0.118**：method signature 冻结。
"""

from __future__ import annotations

import collections
import itertools
import logging
import time
from typing import Any, Optional

from ..core import DisposerLike, RelayContext

__all__ = ["AlertLog"]

log = logging.getLogger("relay.services.alert_log")


_ALERT_MAX = 50
_ALERT_DEDUPE_WINDOW = 300.0  # 5 分钟


class AlertLog:
    """分发告警环形列表 + 去重。

    用法::

        alerts = AlertLog()
        alerts.apply(ctx)               # 注册 ctx.svc("alerts")
        alerts.push(kind="proxy_unknown_key", platform="anthropic",
                    key="sk-...abc", model="claude-3-5-sonnet")
        for entry in alerts.list(): ...
    """

    def __init__(
        self,
        *,
        max_size: int = _ALERT_MAX,
        dedupe_window_s: float = _ALERT_DEDUPE_WINDOW,
    ) -> None:
        self._alerts: collections.deque[dict] = collections.deque(maxlen=max_size)
        self._dedup: dict[tuple, float] = {}
        self._seq = itertools.count(1)
        self._max_size = max_size
        self._dedupe_window_s = dedupe_window_s

    # ---- service 注册 ----

    def apply(self, ctx: RelayContext) -> DisposerLike:
        ctx.register("alerts", self)

        def _dispose() -> None:
            self._alerts.clear()
            self._dedup.clear()

        ctx.add_disposer(_dispose)
        return _dispose

    # ---- 公开 API ----

    def push(
        self,
        *,
        kind: str,
        platform: str = "",
        key: str = "",
        model: str = "",
        message: str = "",
        severity: str = "warn",
        **extra: Any,
    ) -> Optional[dict]:
        """推一条告警；同 dedupe-key 在窗口内只发一次。

        返回写入的 entry dict，或 ``None``（被去重跳过）。
        """
        dedup_key = (kind, platform, key, model)
        now = time.monotonic()
        last = self._dedup.get(dedup_key)
        if last is not None and now - last < self._dedupe_window_s:
            return None
        self._dedup[dedup_key] = now

        seq = next(self._seq)
        entry = {
            "seq": seq,
            "ts": time.time(),
            "kind": kind,
            "platform": platform,
            "key": key,
            "model": model,
            "message": message,
            "severity": severity,
            **extra,
        }
        self._alerts.append(entry)
        log.warning(
            "alert seq=%d kind=%s platform=%s model=%s msg=%s",
            seq, kind, platform, model, message,
        )
        return entry

    def list(self, *, limit: Optional[int] = None) -> list[dict]:
        """按时间倒序返回；limit 截断。"""
        items = list(reversed(self._alerts))
        if limit is not None:
            items = items[:limit]
        return items

    def clear(self) -> None:
        """清空（测试 / 重置用）。"""
        self._alerts.clear()
        self._dedup.clear()

    def __len__(self) -> int:
        return len(self._alerts)
