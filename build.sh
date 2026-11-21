#!/bin/bash
# 打包成 macOS 应用（.app + .dmg）
#
#   ./build.sh
#
# 产物：
#   src-tauri/target/release/bundle/macos/东南大学校园网.app
#   src-tauri/target/release/bundle/dmg/东南大学校园网_3.0.0_aarch64.dmg
#
# Windows 上同样的命令会出 .msi / .exe（需要在 Windows 机器上构建）。

set -euo pipefail
cd "$(dirname "$0")"

export PATH="$HOME/.cargo/bin:$PATH"

if ! command -v cargo >/dev/null 2>&1; then
    echo "没找到 cargo。先装 Rust：https://rustup.rs"
    exit 1
fi

if ! cargo tauri --version >/dev/null 2>&1; then
    echo "==> 安装 Tauri CLI（第一次要几分钟）"
    cargo install tauri-cli --version "^2" --locked
fi

echo "==> 打包"
cd src-tauri
cargo tauri build

echo
echo "==> 完成"
echo
case "$(uname -s)" in
    Darwin)
        echo "  src-tauri/target/release/bundle/macos/东南大学校园网.app"
        echo "  src-tauri/target/release/bundle/dmg/*.dmg"
        echo
        echo "第一次打开提示「无法验证开发者」时：右键 -> 打开。"
        ;;
    *)
        echo "  src-tauri/target/release/bundle/"
        ;;
esac
echo
echo "认证逻辑用的是同目录的 seu_net.py，需要系统里有 python3。"
