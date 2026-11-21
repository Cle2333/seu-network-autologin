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

if ! cargo tauri --version 2>/dev/null | grep -q "tauri-cli 2\."; then
    echo "==> 安装 Tauri CLI 2.x（第一次要几分钟）"
    # --force 覆盖可能存在的 1.x，否则会用它去构建 2.x 项目然后报错
    cargo install tauri-cli --version "^2" --locked --force
fi

echo "==> 打包（目标平台：$(uname -s)）"
cd src-tauri
cargo tauri build
cd ..

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
    MINGW*|MSYS*|CYGWIN*)
        echo "  src-tauri/target/release/bundle/msi/*.msi"
        ;;
    *)
        echo "  src-tauri/target/release/bundle/"
        ;;
esac
echo
echo "认证逻辑用的是同目录的 seu_net.py，需要系统里有 python3。"
