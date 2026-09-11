# -*- mode: python ; coding: utf-8 -*-
"""Build the RelayMeter release bundle.

Produces a portable ``dist/RelayMeter/`` folder containing:

    RelayMeter.exe          the frozen GUI (WebView2 dashboard + embedded
                            ``serve`` HTTP relay, see pyproject/deps)
    relay_assets/           Electron runtime + ball/panel renderers that
                            cannot be frozen into the exe
        electron/electron.exe
        electron_app/*.js  (ball_main.js, ball_preload.js, panel_*.js, ...)
        web/                static frontend (so _WEB_DIR resolves under the
                            attachment directory too)
    plugins/                example + volc_agent plugin .py files

The GUI discovers ``relay_assets`` next to the exe at runtime and sets
RELAY_ELECTRON_EXE / RELAY_ELECTRON_APP_ROOT / RELAY_PLUGINS_DIR so the
floating ball + live panel work in the portable layout.

Usage (from repo root, after .venv-pack deps are installed):
    .venv-pack/Scripts/python.exe build_release.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "relay-gui.spec"
PYI = ROOT / ".venv-pack" / "Scripts" / "pyinstaller.exe"
# Electron 资产的来源 = 本仓库自己的 src/relay/electron_app（含 node_modules/
# electron/dist，npm ci 装好后常驻）。原实现指向 C:\relaymeter-verify 的
# 独立克隆，那份会随仓库推进而变旧 —— 曾导致打包出的 relay_assets 里
# ball_main.js 等 JS 停留在旧版本（新版刚修的拖拽冻结逻辑没进包）。
ELECTRON_APP = ROOT / "src" / "relay" / "electron_app"
DIST = ROOT / "dist" / "RelayMeter"

# 开发期诊断脚本 —— 只在本机手动跑，不被任何入口 require，不进发布包。
# 命名无统一约定（探针 / 像素分析 / 查色各一套），故用显式集合。
DEV_PROBE_JS = {
    "band_check.js", "calib_probe.js", "cap_probe.js", "diag_ball.js",
    "dump_px.js", "find_blue.js", "hl_probe.js", "hl_px2.js",
    "panel_tour_probe.js", "pixel_analyze.js", "tour_probe.js",
    "visual_tour_probe.js",
}


def run_pyinstaller() -> None:
    cmd = [str(PYI), str(SPEC), "--noconfirm", "--clean"]
    print("==> pyinstaller:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def copy_web(src_root: Path, dst_root: Path) -> None:
    """Copy the static frontend tree that _WEB_DIR points at."""
    shutil.copytree(src_root, dst_root, dirs_exist_ok=True)


def assemble_portable(electron_src: Path) -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True, exist_ok=True)

    # 1) The frozen exe (dist/RelayMeter.exe from PyInstaller onefile).
    onefile = ROOT / "dist" / "RelayMeter.exe"
    if onefile.exists():
        shutil.copy2(onefile, DIST / "RelayMeter.exe")
        print("   exe:", (DIST / "RelayMeter.exe").as_posix())

    # 2) relay_assets/electron/electron.exe + the Electron runtime that a
    #    bare electron.exe needs (its node_modules/electron/dist tree).
    assets = DIST / "relay_assets"
    elec_dist = assets / "electron"
    elec_src = electron_src / "node_modules" / "electron" / "dist"
    if elec_src.exists():
        shutil.copytree(elec_src, elec_dist, dirs_exist_ok=True)
        print("   electron dist:", elec_dist.as_posix())
    else:
        print("   !! electron dist missing:", elec_src)

    # 3) relay_assets/electron_app/*.js + package.json (ball/panel renderers).
    #    跳过开发期诊断脚本（DEV_PROBE_JS）—— 它们不被任何入口 require，
    #    留在发布包里只会让用户看到一堆源码里没有的杂项（源码侧也未提交）。
    app_dst = assets / "electron_app"
    app_dst.mkdir(parents=True, exist_ok=True)
    skipped = []
    for f in electron_src.glob("*.js"):
        if f.name in DEV_PROBE_JS:
            skipped.append(f.name)
            continue
        shutil.copy2(f, app_dst / f.name)
    if skipped:
        print("   skipped dev probes:", ", ".join(sorted(skipped)))
    (electron_src / "package.json").exists() and shutil.copy2(
        electron_src / "package.json", app_dst / "package.json"
    )
    print("   electron_app:", app_dst.as_posix())

    # 4) relay_assets/web/ — mirrors _WEB_DIR ($MEIPASS/relay/web) so the
    #    portable folder also serves the frontend if the exe can't unpack.
    web_src = ROOT / "src" / "relay" / "web"
    copy_web(web_src, assets / "web")
    print("   web:", (assets / "web").as_posix())

    # 5) plugins/ for the frozen plugin loader (exe-dir/plugins fallback).
    plug_src = ROOT / "plugins"
    plug_dst = DIST / "plugins"
    if plug_src.exists():
        shutil.copytree(plug_src, plug_dst, dirs_exist_ok=True)
        print("   plugins:", plug_dst.as_posix())

    # 6) Seed configs (relay.env.dist / relay.upstreams.dist.json).
    pkg_dst = DIST / "packaging"
    pkg_src = ROOT / "src" / "relay" / "packaging"
    if pkg_src.exists():
        shutil.copytree(pkg_src, pkg_dst, dirs_exist_ok=True)
        print("   packaging:", pkg_dst.as_posix())


def main() -> None:
    electron_src = ELECTRON_APP
    if not (electron_src / "node_modules" / "electron" / "dist" / "electron.exe").exists():
        print("!! Electron runtime not found under", electron_src)
        print("   run:  cd src/relay/electron_app && npm ci")
        sys.exit(1)
    run_pyinstaller()
    assemble_portable(electron_src)
    print("\n== done ->", DIST.as_posix())


if __name__ == "__main__":
    main()
