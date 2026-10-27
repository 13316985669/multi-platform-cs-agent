#!/usr/bin/env bash
# RPA Worker 一键安装（Linux/macOS）
set -e

echo "==> 创建 Python 虚拟环境..."
python3 -m venv .venv
source .venv/bin/activate

echo "==> 安装依赖..."
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "==> 安装 Chromium 浏览器..."
playwright install chromium

if [ ! -f .env.local ]; then
    cp .env.example .env.local
    KEY=$(python -c "import secrets;print(secrets.token_hex(16))")
    sed -i "s/^RPA_API_KEY=$/RPA_API_KEY=$KEY/" .env.local
    echo "==> 已生成 .env.local，RPA_API_KEY 随机生成：$KEY"
    echo "    请把同一个 key 填到后端 .env 的 RPA_API_KEY，并修改 .env.local 的 BACKEND_URL / ACCOUNT"
else
    echo "==> .env.local 已存在，跳过生成"
fi

echo ""
echo "安装完成！接下来的步骤："
echo "  1. 编辑 .env.local（BACKEND_URL / ACCOUNT / PLATFORM）"
echo "  2. python login.py        # 首次登录引导（扫码）"
echo "  3. python doctor.py       # 自检全绿后"
echo "  4. python douyin_feige_worker.py   # 或 xhs_ark_worker.py"
