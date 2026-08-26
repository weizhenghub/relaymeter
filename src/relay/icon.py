# -*- coding: utf-8 -*-
"""v0.111 应用图标 —— 运行时合成，不依赖任何图片资源文件。

设计：深色圆角渐变底（顶亮底暗）+ 琥珀色「用量仪表盘」弧环 + 中心白色
粗体 T（Token）。琥珀 = GUI 深色主题里 --platform-anthropic / --accent 的
品牌色，T = Token。弧环 ~68% 留一个缺口，暗示"用量/配额"而不是完整闭环。

同一份设计服务两处：
  * 系统托盘图标（pystray）—— build_tray_image()，64px
  * 窗口/任务栏图标（Win32 LoadImageW + WM_SETICON）—— write_ico()
    生成多尺寸 .ico 文件

Tray 旧的"双向箭头"实现（tray.py _build_icon_image）被此模块取代。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# ---------------------------------------------------------------------------
# 品牌色（与 styles-20260817.css 深色主题一致）
# ---------------------------------------------------------------------------
_TILE_TOP = (50, 50, 58)     # 圆角底顶部
_TILE_BOT = (15, 15, 21)     # 圆角底底部（暗）
_SHEEN = (255, 255, 255)     # 顶部柔光
_AMBER = (245, 158, 11)      # 弧环主色（--platform-anthropic 深色）
_AMBER_HI = (252, 211, 77)   # 弧环高光端
_RING = (255, 255, 255)      # 背景虚环
_RING_A = 34                 # 背景虚环 alpha
_T = (250, 250, 252)         # 中心 T

# 弧环覆盖角度：约 68%（从正上方向右顺时针）。
_ARC_DEG = 245


def build_app_icon(size: int = 256) -> Image.Image:
    """渲染主图标（RGBA，透明圆角底）。``size`` 为最终边长。

    全部在 4× 超采样空间绘制，最后一次性 LANCZOS 降采样 —— 环/弧/T 的
    线宽单位保持一致，不出现"某些元素在低分辨率空间被放大"的混叠。
    """
    ss = 4  # 超采样倍率
    S = size * ss
    radius = size * 0.22
    r = int(radius * ss)

    # ---- 圆角渐变底 + 顶部柔光 ----
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    for y in range(S):
        t = y / (S - 1)
        col = tuple(int(_TILE_TOP[i] + (_TILE_BOT[i] - _TILE_TOP[i]) * t)
                    for i in range(3))
        d.rounded_rectangle([0, y, S, y + 1], radius=r, fill=col + (255,))
    sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sheen)
    band = int(S * 0.40)
    for y in range(band):
        a = int(32 * (1 - y / band))
        if a <= 0:
            break
        ds.rounded_rectangle([0, y, S, y + 1], radius=r, fill=_SHEEN + (a,))
    tile = Image.alpha_composite(tile, sheen)

    # ---- 背景虚环 + 琥珀进度弧（带辉光）----
    cx = cy = S / 2
    rr = size * 0.315 * ss
    w = size * 0.085 * ss
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
              outline=_RING + (_RING_A,), width=int(w))

    arc = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    da = ImageDraw.Draw(arc)
    start, end = -90.0, -90.0 + _ARC_DEG
    da.arc([cx - rr, cy - rr, cx + rr, cy + rr], start, end,
           fill=_AMBER + (255,), width=int(w))
    cap_r = w / 2
    for a in (start, end):
        rad = math.radians(a)
        ex, ey = cx + rr * math.cos(rad), cy + rr * math.sin(rad)
        da.ellipse([ex - cap_r, ey - cap_r, ex + cap_r, ey + cap_r],
                   fill=_AMBER_HI + (255,))
    glow = arc.filter(ImageFilter.GaussianBlur(size * 0.02 * ss))
    arc = Image.alpha_composite(glow, arc)
    tile = Image.alpha_composite(tile, arc)

    # ---- 中心白色粗体 T（Token）----
    ft = size * 0.52 * ss
    bar_h = ft * 0.22
    stem_w = ft * 0.20
    x0 = (S - ft) / 2
    y0 = (S - ft) / 2 + ft * 0.06
    cr = size * 0.035 * ss
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle([x0, y0, x0 + ft, y0 + bar_h], radius=cr, fill=_T + (255,))
    td.rounded_rectangle([x0 + ft / 2 - stem_w / 2, y0,
                          x0 + ft / 2 + stem_w / 2, y0 + ft], radius=cr,
                         fill=_T + (255,))

    return tile.resize((size, size), Image.LANCZOS)


def build_tray_image() -> Image.Image:
    """系统托盘图标（pystray 用，64px）。"""
    return build_app_icon(64)


_ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def write_ico(path: str | Path) -> Path:
    """把多尺寸 .ico 写到 ``path``，返回该路径（供 Win32 LoadImageW 用）。"""
    path = Path(path)
    master = build_app_icon(256)
    master.save(path, format="ICO", sizes=[(s, s) for s in _ICO_SIZES])
    return path
