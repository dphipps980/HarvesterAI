# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static')],
    hiddenimports=[
        # uvicorn internals (commonly missed by PyInstaller)
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.off',
        'uvicorn.lifespan.on',
        # async backend
        'anyio',
        'anyio._backends._asyncio',
        # h11 HTTP parser used by uvicorn
        'h11',
        # starlette (FastAPI foundation)
        'starlette',
        'starlette.routing',
        'starlette.staticfiles',
        'starlette.responses',
        'starlette.middleware',
        'starlette.middleware.cors',
        # pydantic
        'pydantic',
        'pydantic.deprecated.decorator',
        # data / excel
        'openpyxl',
        'openpyxl.cell._writer',
        'pandas',
        'xlrd',
        # pdf
        'pdfplumber',
    ],
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
    name='HarvesterAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,        # keep console for log visibility
    icon='myicon.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
