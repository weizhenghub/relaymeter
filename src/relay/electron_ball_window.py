"""鸭型替代 ``webview.Window`` —— 实时悬浮球的 Electron 后端。

与 ``electron_window.ElectronWindowBase`` 同构：常驻一个 Electron 子进程
（``electron_app/ball_main.js`` + ``ball_preload.js``），由本控制器通过 TCP
loopback JSON-RPC 驱动。对外暴露 Duck-typed 面（``evaluate_js``/``move``/
``show``/``hide``/``resize``/``set_topmost``/``set_ignore``/``set_expanded``/
``capture``/``destroy``/``width``/``height``/``x``/``y``/``get_size``/``hwnd``/
``events``/``native=None``），因此 ``ball_layer2.BallLayer2`` 的公开 API 面
几乎零改动即可切到 Electron 后端。

共享机制（TCP 连接、reader 线程、JSON-RPC、事件派发、api-request 上行、
``_find_electron_exe``）全部收编到 ``electron_window.ElectronWindowBase``，
本模块只剩球窗差异：透明 + hover 穿透由 Electron 侧 ``transparent:true`` +
``setIgnoreMouseEvents`` 处理；``capture``/``set_ignore``/``set_expanded``/
``hwnd`` 是球窗专属能力。

线程模型：与 Base 一致 —— reader 线程只同步 resolve ``reply``，``event``/
``api-request`` 扔到独立线程派发（避免 handler 回调 ``evaluate_js`` 时阻塞
reader 读 reply 死锁）。
"""

from __future__ import annotations

from typing import Any, Callable

from .electron_window import ElectronWindowBase


class ElectronBallWindow(ElectronWindowBase):
    """鸭型替代 ``webview.Window``，后端常驻一个透明 Electron 子进程球窗。

    构造即预读 url/宽高；``start()`` 拉起子进程并建立 TCP 连接。
    所有方法线程安全（可被 panel_pool 的 op-worker / ball 轮询线程调用）。
    """

    def __init__(
        self,
        url: str,
        *,
        width: int = 56,
        height: int = 56,
        electron_exe: str | None = None,
        app_root: str | None = None,
        api_handler: Callable[[str, list[Any]], Any] | None = None,
    ) -> None:
        super().__init__(
            url,
            width=width,
            height=height,
            electron_exe=electron_exe,
            app_root=app_root,
            api_handler=api_handler,
            event_names=("loaded", "closing", "moved", "resized",
                         "dragend", "clicked"),
            main_js_name="ball_main.js",
            args_log_tag="electron-ball-args",
        )

    # -- 球窗专属能力 --
    def capture(self) -> bytes:
        """抓 Electron 窗口自身渲染为 PNG（绕过桌面/离屏验证真实合成像素）。
        capturePage 是在渲染进程合成结果上截图，与屏幕显示无关（桌面全黑也能验证）。"""
        b64 = self._call("capture")
        if isinstance(b64, str) and b64:
            import base64
            return base64.b64decode(b64)
        return b""

    def set_topmost(self, topmost: bool) -> None:
        self._call("set_topmost", bool(topmost))

    def set_ignore(self, want_ignore: bool) -> None:
        """切换球窗「吞点击 / 穿透」。拖动/点击前先 set_ignore(False) 开吞。"""
        self._call("set_ignore", bool(want_ignore))

    def set_expanded(self, expanded: bool) -> None:
        """v0.184：展开态（球+侧栏 / 磁吸 dock）→ 停 hover 穿透循环 + 全窗口吞
        点击；收起态 → 恢复球帽 hover 判定（球内吞/球外穿）。"""
        self._call("set_expanded", bool(expanded))

    def reload(self) -> None:
        """v0.203：重载渲染层（webContents.reload，重新加载 ghost_panel.html）。

        不重建窗口 —— 保持同一个窗口对象（always_one_window 引用稳定），
        只是重新拉取 file:// URL，HTML/CSS 改动即时生效。
        """
        self._call("reload")

    def hwnd(self) -> int:
        """Electron 窗口原生 HWND（getNativeWindowHandle），供 Win32 抓窗验证。"""
        return int(self._call("get_hwnd"))
