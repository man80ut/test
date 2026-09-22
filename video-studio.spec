# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：视频模型测试广场 → 单个 exe。

用法（在已安装依赖的虚拟环境中）：
    pyinstaller video-studio.spec --noconfirm

产物：dist/VideoStudio.exe
说明：static/ 与 ffmpeg 二进制打进 exe；videos/、uploads/ 运行时在 exe 同级目录生成。
"""
from PyInstaller.utils.hooks import collect_all

import os

import imageio_ffmpeg as _iio

datas = [
    ("static", "static"),          # 前端页面
]
binaries = []
hiddenimports = [
    # uvicorn 的 loop / protocol / lifespan 均为运行时动态导入
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    # FastAPI 表单/文件上传依赖 python-multipart
    "multipart",
    "python_multipart",
    "email.mime.multipart",
    # 业务模块
    "main",
    "providers",
]

# certifi：httpx 校验证书需要 cacert.pem
d, b, h = collect_all("certifi")
datas += d
binaries += b
hiddenimports += h

# ffmpeg：随包分发到 ffmpeg/ 目录，providers 在冻结环境下优先使用它
_ffmpeg_src = _iio.get_ffmpeg_exe()
if not os.path.isfile(_ffmpeg_src):
    raise SystemExit("未找到 imageio_ffmpeg 自带的 ffmpeg 二进制，无法打包")
binaries.append((_ffmpeg_src, "ffmpeg"))

excludes = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
]

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name="VideoStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 保留控制台：能看到服务地址，关窗口即停服务
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
