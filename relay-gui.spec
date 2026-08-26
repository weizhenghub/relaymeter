# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for RelayMeter desktop GUI (relay-gui).
#
# Produces a single-file ``RelayMeter.exe`` that bundles:
#   * the GUI (relay.gui:run -> pywebview / WebView2 dashboard)
#   * an embedded ``serve`` subcommand (frozen GUI spawns the HTTP relay
#     via ``sys.executable serve ...``, see relay/server.py)
#   * the static frontend assets (src/relay/web/* -> relay/web)
#
# The Electron floating ball CANNOT be frozen into the exe (it needs the
# full Electron runtime). A portable ``relay_assets`` folder is produced
# next to the exe by build_release.py; the GUI picks it up via
# RELAY_ELECTRON_EXE + RELAY_ELECTRON_APP_ROOT (see electron_ball_window).
#
# Build (from repo root):
#   .venv-pack\Scripts\pyinstaller.exe relay-gui.spec --noconfirm --clean

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules


block_cipher = None

# Frontend assets the GUI loads through file:// (relay/gui.py:_WEB_DIR).
# PyInstaller onefile unpacks <datas> into sys._MEIPASS, and _WEB_DIR is
# ``Path(__file__).parent / "web"``, so each asset must land at
# ``relay/web/...`` inside the bundle.
datas = [
    (os.path.join("src", "relay", "web"), "relay/web"),
    # Seed configs referenced by package-data (relay/packaging/).
    (os.path.join("src", "relay", "packaging"), "relay/packaging"),
]

# GUI + in-process uvicorn (serve) load a lot of modules dynamically
# (pydantic-settings, starlette, uvicorn, pywebview backends, pystray,
# aiosqlite, httpx, textual, martian-linguafranca). Collect submodules so
# the frozen ``serve`` subcommand and the GUI bridge both resolve.
hiddenimports = []
for pkg in (
    "relay",
    "uvicorn",
    "fastapi",
    "starlette",
    "aiosqlite",
    "httpx",
    "pydantic_settings",
    "textual",
):
    hiddenimports += collect_submodules(pkg)

# Non-python data inside the package (e.g. martian-linguafranca, textual
# theme files) that don't ship as a single importable module.
for pkg in ("textual", "martian_linguafranca"):
    datas += collect_data_files(pkg)

a = Analysis(
    ["src/relay_launcher.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="RelayMeter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    icon="relay_app.ico",   # brand icon (relay.icon.write_ico output)
    console=False,          # windowed GUI; serve relay runs silent
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
