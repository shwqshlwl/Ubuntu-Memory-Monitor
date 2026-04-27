#!/bin/bash
echo "======================================"
echo "   Ubuntu 内存监控环境初始化脚本"
echo "======================================"

echo "[1/3] 更新 apt 索引并安装系统依赖 (python3-venv, python3-tk)..."
# 增加超时配置：限制连接超时时间为 5 秒，防止第三方源（如 elastic.co）网络不通导致卡死
sudo apt-get update -o Acquire::http::Timeout=5 -o Acquire::Retries=1 || echo "⚠️ 部分软件源更新超时，跳过更新直接尝试安装依赖..."
sudo apt-get install -y python3-venv python3-tk python3-pip

echo "[2/3] 创建 Python 虚拟环境..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "虚拟环境创建成功。"
else
    echo "虚拟环境已存在，跳过创建。"
fi

echo "[3/3] 安装 Python 依赖库 (psutil)..."
source venv/bin/activate
pip install -r requirements.txt

echo "======================================"
echo "环境配置完成！"
echo "您可以修改 config.json 来调整触发阈值和命令。"
echo "请使用以下命令启动监控服务："
echo "./venv/bin/python monitor.py"
echo "======================================"