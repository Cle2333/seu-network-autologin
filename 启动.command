#!/bin/bash
# 双击即可打开图形界面。
# 如果双击后终端闪一下就消失，说明出错了——把终端里的红字截图下来即可。

cd "$(dirname "$0")" || exit 1

# 优先用系统自带的 Python（稳定路径，不会因为工具链升级而失效）
PYTHON=""
for candidate in /usr/bin/python3 "$(command -v python3)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "没找到 Python 3。"
    echo "macOS 上可以运行 xcode-select --install 装 Command Line Tools（自带 python3）。"
    echo
    echo "按回车键关闭..."
    read -r _
    exit 1
fi

"$PYTHON" seu_net.py gui
code=$?

# 只有出错时才停住，正常关闭窗口直接退出
if [ $code -ne 0 ]; then
    echo
    echo "程序退出码 $code。可以运行下面这条看详细自检："
    echo "  $PYTHON seu_net.py doctor"
    echo
    echo "按回车键关闭..."
    read -r _
fi
exit $code
