#!/bin/bash
# macOS 打包脚本：生成 dist/VideoStudio.app
# 用法：在 Mac 上打开终端，cd 到本目录，执行  bash build_mac.sh
set -e
cd "$(dirname "$0")"

echo "[1/4] 准备虚拟环境..."
if [ ! -x ".venv/bin/python3" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller

echo "[2/4] 清理旧产物..."
rm -rf build dist

echo "[3/4] 打包中（约 1-3 分钟）..."
pyinstaller video-studio-mac.spec --noconfirm

echo "[4/4] 签名（ad-hoc，缓解 Gatekeeper 拦截）..."
codesign --force --deep --sign - "dist/VideoStudio.app" 2>/dev/null || true

echo
echo "完成：dist/VideoStudio.app"
echo
echo "首次打开前先解除隔离属性（否则 macOS 会提示“已损坏”）："
echo "    xattr -cr \"dist/VideoStudio.app\""
echo
echo "视频输出目录：~/Library/Application Support/VideoStudio/videos"
echo "停止服务：活动监视器搜索 VideoStudio，退出即可"
