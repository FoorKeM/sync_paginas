# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


# Solo se usa playwright.async_api en todo el proyecto (nunca sync_api) —
# excluirlo reduce lo que hay que extraer/escanear en cada arranque del .exe.
datas = collect_data_files("playwright") + collect_data_files("certifi")
hiddenimports = collect_submodules("playwright.async_api") + collect_submodules("playwright._impl") + ["certifi"]


a = Analysis(
    ["menu.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["playwright.sync_api"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MercadohouseSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX comprime el .exe (mas chico en disco) pero obliga a descomprimirlo
    # entero en CPU cada vez que arranca — eso es lo que causa el "tranco"
    # al abrir el programa. Sin UPX el arranque es mas liviano para la CPU.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
