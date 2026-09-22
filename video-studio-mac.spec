# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（macOS）：视频模型测试广场 → 单个 .app

用法（在 macOS 上、已安装依赖的虚拟环境中）：
    pyinstaller video-studio-mac.spec --noconfirm

产物：dist/VideoStudio.app
说明：
- static/ 与 ffmpeg 二进制打进应用包；videos/、uploads/ 运行时落在
  ~/Library/Application Support/VideoStudio（见 main.py::_app_dir）
- 必须在 macOS 上构建：PyInstaller 不支持跨平台交叉编译
- Apple Silicon 的 Mac 上构建即为原生 arm64 版；Intel Mac 上构建为 x86_64 版
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
    "uvicorn.loops.uvloop",
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

# ffmpeg：随包分发到 ffmpeg/ 目录，providers 在冻结环境下优先使用它。
# imageio-ffmpeg 会按当前平台给出对应二进制（Apple Silicon 为 aarch64 原生版）。
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
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# console=False：.app 双击后不弹终端窗口，由程序自动打开浏览器
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
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
    name="VideoStudio",
)

app = BUNDLE(
    coll,
    name="VideoStudio.app",
    icon=None,
    bundle_identifier="com.local.videostudio",
)
