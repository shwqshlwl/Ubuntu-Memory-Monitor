#!/bin/bash

# 获取当前项目的绝对路径
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$APP_DIR/venv/bin/python"
GUI_SCRIPT="$APP_DIR/gui.py"
ICON="utilities-system-monitor" # Ubuntu 内置的系统监控图标

# 目标桌面文件路径
APPS_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APPS_DIR/memory-monitor.desktop"

# 确保应用程序目录存在
mkdir -p "$APPS_DIR"

echo "正在生成桌面快捷方式配置文件..."

cat <<EOF > "$DESKTOP_FILE"
[Desktop Entry]
Name=Ubuntu 内存监控器
Comment=轻量级内存监控与自动清理工具
Exec=$VENV_PYTHON $GUI_SCRIPT
Icon=$ICON
Terminal=false
Type=Application
Categories=System;Utility;
EOF

chmod +x "$DESKTOP_FILE"
echo "已添加到应用程序列表：$DESKTOP_FILE"

# 尝试将其放置在桌面上，使用 xdg-user-dir 兼容中文路径 (如 ~/桌面)
USER_DESKTOP=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
if [ -d "$USER_DESKTOP" ]; then
    cp "$DESKTOP_FILE" "$USER_DESKTOP/"
    chmod +x "$USER_DESKTOP/memory-monitor.desktop"
    
    # 针对 Ubuntu GNOME 桌面环境，尝试赋予桌面图标信任权限
    gio set "$USER_DESKTOP/memory-monitor.desktop" metadata::trusted true 2>/dev/null || true
    echo "已复制到桌面：$USER_DESKTOP/memory-monitor.desktop"
else
    echo "未能找到桌面目录 ($USER_DESKTOP)，跳过桌面图标创建。"
fi

echo "================================================="
echo "✅ 快捷方式安装成功！"
echo "现在您可以通过以下两种方式启动程序："
echo "1. 在桌面上双击 'Ubuntu 内存监控器' (如果显示为 memory-monitor.desktop，请右键选择 '允许运行' / 'Allow Launching')"
echo "2. 按下 Super(Win) 键打开应用抽屉，搜索 '内存监控' 或 'Memory Monitor'"
echo "================================================="