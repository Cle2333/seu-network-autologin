#!/bin/bash
# 把程序打包成 macOS 的 .app，双击就能用，不需要装 Python。
#
#   ./build_app.sh
#
# 产物：dist/东南大学校园网认证.app
#
# 依赖：PyInstaller（脚本会用 .venv-build 自己装，不污染系统）

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="东南大学校园网认证"
BUNDLE_ID="cn.edu.seu.net.autologin"
VENV=".venv-build"
PY="$VENV/bin/python"

echo "==> 准备打包环境"
if [ ! -x "$PY" ]; then
    python3 -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
fi
# 托盘需要 pyobjc，装进同一个环境才能一起打进 .app
"$PY" -m pip install --quiet --upgrade pyinstaller pyobjc-framework-Cocoa
"$PY" -c "import AppKit" || {
    echo "!! pyobjc 装不上，托盘不会被打包（窗口功能仍然可用）"
}

echo "==> 清理上次产物"
rm -rf build "dist/$APP_NAME.app" "dist/$APP_NAME" 2>/dev/null || true

echo "==> 打包（这一步要一两分钟）"
"$PY" -m PyInstaller \
    --noconfirm \
    --windowed \
    --name "$APP_NAME" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --hidden-import tkinter \
    --hidden-import tkinter.scrolledtext \
    --hidden-import tkinter.messagebox \
    --hidden-import seu_tray \
    --hidden-import AppKit \
    --hidden-import Foundation \
    --hidden-import objc \
    --collect-submodules tkinter \
    --collect-submodules objc \
    seu_net.py

if [ ! -d "dist/$APP_NAME.app" ]; then
    echo "!! 打包失败：没有生成 dist/$APP_NAME.app"
    exit 1
fi

echo
echo "==> 完成：dist/$APP_NAME.app"
echo
echo "用法："
echo "  双击打开          -> 图形界面"
echo "  拖进「应用程序」   -> 长期使用"
echo
echo "第一次打开提示「无法验证开发者」时：右键 -> 打开。"
echo
echo "配置文件位置（升级不会丢）："
echo "  ~/Library/Application Support/SEU-Net-AutoLogin/config.json"
