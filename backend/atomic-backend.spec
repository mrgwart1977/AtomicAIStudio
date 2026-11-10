# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Atomic AI Studio backend sidecar.

Produces an onedir build: dist/atomic-backend/atomic-backend.exe plus
_internal dependencies. Alembic migration scripts are bundled as data
under backend_res/alembic so `run_migrations` can locate them via
sys._MEIPASS when frozen.
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + collect_submodules("app")
    + [
        "email_validator",
        "httptools",
        "watchfiles",
    ]
)

datas = [
    ("app/db/alembic", "backend_res/alembic"),
]

a = Analysis(
    ["sidecar_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 'av' (PyAV) ships GPL-licensed x264/x265 DLLs in its av.libs wheel
    # payload — excluded from the distributed artifact pending a licensing
    # decision (media_probe falls back to container-signature checks).
    excludes=["pytest", "pytest_asyncio", "ruff", "tkinter", "av"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="atomic-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="atomic-backend",
)
