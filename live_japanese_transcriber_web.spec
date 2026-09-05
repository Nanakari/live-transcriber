# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
stage = root / 'build' / 'release-assets'
if not (stage / 'tools' / 'ffmpeg.exe').exists():
    raise RuntimeError('Run scripts/release_assets.py before building.')
runtime = []
for name in ('expat.dll', 'libexpat.dll', 'libssl-3-x64.dll', 'libcrypto-3-x64.dll',
             'ffi.dll', 'sqlite3.dll', 'libbz2.dll', 'liblzma.dll', 'zlib.dll'):
    path = Path(sys.base_prefix) / 'Library' / 'bin' / name
    if path.exists():
        runtime.append((str(path), '.'))
datas = [
    (str(root / 'app/web/templates/workbench.html'), 'app/web/templates'),
    (str(root / 'app/web/static/workbench.css'), 'app/web/static'),
    (str(root / 'app/web/static/workbench.js'), 'app/web/static'),
    (str(root / 'app/assets/neutral_cover.png'), 'app/assets'),
    (str(root / 'dictionaries'), 'dictionaries'),
    (str(stage / 'licenses'), 'licenses'),
]
datas += collect_data_files('faster_whisper')
for package in ('faster-whisper', 'yt-dlp', 'huggingface-hub', 'tqdm', 'requests'):
    datas += copy_metadata(package)
hidden = ['app.web.server', 'app.web.routes', 'app.web.jobs', 'uvicorn.logging',
          'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
          'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
hidden += collect_submodules('yt_dlp')
a = Analysis([str(root / 'main.py')], pathex=[str(root)],
    binaries=runtime + [(str(stage / 'tools/ffmpeg.exe'), 'tools'), (str(stage / 'tools/node.exe'), 'tools')],
    datas=datas, hiddenimports=hidden, excludes=['nvidia', 'torch', 'pytest', 'tkinter'],
    noarchive=False, optimize=0)
pyz = PYZ(a.pure)
# A console-capable worker is essential for capturing task output. start.bat hides the UI launcher.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='LiveTranscriber',
          debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='LiveTranscriber')
