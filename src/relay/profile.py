"""启动 Profile —— ``RELAY_PROFILE`` 决定哪些子系统加载（V0.121+）。

Phase 5 目标：参考 cordis ``packages/bundle/{base,web-app,headless}/``
实现 web / headless 两套启动配置。本模块是纯函数判定 + 解析：

* ``resolve_profile() -> Profile`` —— 读 ``RELAY_PROFILE`` 环境变量；
* ``Profile`` dataclass —— 一组布尔开关（gui / web / passthrough 等）；
* ``profile_from_name(name) -> Profile`` —— 字符串 → Profile。

⚠ **STABLE since v0.121**：``resolve_profile()`` / ``profile_from_name()`` /
``Profile`` 字段冻结。新 profile 名走 STABILITY.md 流程。

设计约束：

* **向后兼容**：空 / 未设 ``RELAY_PROFILE`` → ``web``（= 现状默认，GUI +
  完整 HTTP relay）。
* ``headless`` → 无 GUI、无 pywebview；HTTP relay + passthrough 保留。
* ``passthrough-only`` → 只加载 passthrough + http client pool + db
  （专项调试），不加载 GUI / 常规 router 业务。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Profile", "resolve_profile", "profile_from_name", "PROFILES"]

PROFILES = ("web", "headless", "passthrough-only")


@dataclass(frozen=True)
class Profile:
    """一组子系统开关。"""

    name: str
    gui: bool = True      # 桌面 GUI（pywebview）
    web: bool = True      # HTTP relay（uvicorn / FastAPI app）
    passthrough: bool = True  # 完全透传中间件

    def describe(self) -> str:
        parts = []
        if self.gui:
            parts.append("gui")
        if self.web:
            parts.append("web")
        if self.passthrough:
            parts.append("passthrough")
        return "+".join(parts) if parts else "(none)"


# web = 现状默认（GUI + HTTP relay + passthrough）
_PROFILES: dict[str, Profile] = {
    "web": Profile(name="web", gui=True, web=True, passthrough=True),
    # headless = 无 GUI；HTTP relay + passthrough 保留
    "headless": Profile(name="headless", gui=False, web=True, passthrough=True),
    # passthrough-only = 只透传 + db + client pool（专项调试）
    "passthrough-only": Profile(
        name="passthrough-only", gui=False, web=False, passthrough=True,
    ),
}


def profile_from_name(name: str) -> Profile:
    """字符串 → Profile。未知名回退到 web（= 向后兼容）。"""
    return _PROFILES.get(name, _PROFILES["web"])


def resolve_profile() -> Profile:
    """读 ``RELAY_PROFILE`` 环境变量 → Profile。未设 / 空 → web。"""
    return profile_from_name(os.environ.get("RELAY_PROFILE", "").strip())
