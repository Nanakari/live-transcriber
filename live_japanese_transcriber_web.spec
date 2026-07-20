# -*- mode: python ; coding: utf-8 -*-

import os
import sys

python_runtime_binaries = []
for dll_name in (
    'expat.dll',
    'libexpat.dll',
    'libssl-3-x64.dll',
    'libcrypto-3-x64.dll',
    'ffi.dll',
    'sqlite3.dll',
    'libbz2.dll',
    'liblzma.dll',
    'zlib.dll',
):
    dll_path = os.path.join(sys.base_prefix, 'Library', 'bin', dll_name)
    if os.path.exists(dll_path):
        python_runtime_binaries.append((dll_path, '.'))

a = Analysis(
    ['main.py'],
    pathex=[],
    # CUDA runtime DLLs are loaded from the local .venv paths in config.yaml.
    # Bundling every NVIDIA DLL makes the launcher multiple gigabytes large.
    binaries=python_runtime_binaries,
    datas=[('app\\web\\templates', 'app\\web\\templates'), ('app\\web\\static', 'app\\web\\static'), ('.venv\\Lib\\site-packages\\faster_whisper\\assets', 'faster_whisper\\assets')],
    hiddenimports=['fastapi', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'jinja2', 'starlette', 'pydantic', 'yaml', 'rich', 'app.web.server', 'app.web.routes', 'app.web.jobs'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='多语言影音研析',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)





