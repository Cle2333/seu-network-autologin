#!/bin/bash
# 双击即可打开图形界面。
#
# 注意：macOS 26 以后，系统自带 Python 3（/usr/bin/python3）里的 Tk 8.5 渲染不出 Label / Entry /
# 输入框这些控件（只有 Button 和 Radiobutton 能画），窗口会是一大片空白。所以这里会挑一个
# Tk 版本够新的 Python（≥ 8.6），挑不到就明确告诉你怎么装，而不是开一个空白窗口。

cd "$(dirname "$0")" || exit 1

find_python() {
    # 依次尝试候选解释器，挑第一个 Tk >= 8.6 的
    for candidate in \
        "$(command -v python3.13)" \
        "$(command -v python3.12)" \
        "$(command -v python3.11)" \
        "$(command -v python3)" \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        /usr/bin/python3
    do
        [ -n "$candidate" ] && [ -x "$candidate" ] || continue
        if "$candidate" - <<'PY' 2>/dev/null
import sys
try:
    import tkinter
except Exception:
    sys.exit(1)
sys.exit(0 if float(tkinter.TkVersion) >= 8.6 else 1)
PY
        then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python)

if [ -z "$PYTHON" ]; then
    echo "没找到能用的图形界面环境。"
    echo
    echo "原因：macOS 自带的 Python 3 使用 Tk 8.5，在新版 macOS 上画不出输入框和文字"
    echo "（窗口会一片空白），所以不能用它开界面。"
    echo
    echo "装一个带新版 Tk 的 Python 就行（任选一种）："
    echo "  · 命令行：brew install python-tk@3.12"
    echo "  · 或者到 https://www.python.org/downloads/ 下载安装 Python 3.12+"
    echo
    echo "不想装也可以先用命令行，功能一样："
    echo "  python3 seu_net.py init      # 配置账号"
    echo "  python3 seu_net.py status    # 看状态"
    echo "  python3 seu_net.py           # 认证"
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
