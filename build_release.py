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
# The Electron runtime lives under the verify clone (npm ci + proxy download).
VERIFY_ELECTRON = Path(
    r"C:\relaymeter-verify\relaymeter\src\relay\electron_app"
)
DIST = ROOT / "dist" / "RelayMeter"


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
    app_dst = assets / "electron_app"
    app_dst.mkdir(parents=True, exist_ok=True)
    for f in electron_src.glob("*.js"):
        shutil.copy2(f, app_dst / f.name)
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
    electron_src = VERIFY_ELECTRON
    if not (electron_src / "node_modules" / "electron" / "dist" / "electron.exe").exists():
        print("!! Electron runtime not found under", electron_src)
        print("   run:  cd src/relay/electron_app && npm ci")
        sys.exit(1)
    run_pyinstaller()
    assemble_portable(electron_src)
    print("\n== done ->", DIST.as_posix())


if __name__ == "__main__":
    main()
