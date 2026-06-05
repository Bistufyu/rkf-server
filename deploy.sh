#!/bin/bash
# ====== 刃客坊认证服务 - 阿里云部署脚本 ======
# 用法: bash deploy.sh
# 前提: 已安装 python3 + pip3

set -e

echo "========================================"
echo "  刃客坊认证服务 - 部署脚本"
echo "========================================"

# 检查Python
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] 未找到 python3，请先安装"
    exit 1
fi
echo "[OK] Python: $(python3 --version)"

# 创建虚拟环境（如果不存在）
if [ ! -d "venv" ]; then
    echo "[1/4] 创建虚拟环境..."
    python3 -m venv venv
else
    echo "[1/4] 虚拟环境已存在"
fi

# 安装依赖
echo "[2/4] 安装依赖..."
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 初始化数据库（会自动创建 data/users.db）
echo "[3/4] 初始化数据库..."
python3 -c "from app import init_db; init_db(); print('[OK] 数据库已就绪')"

# 检查环境变量
echo ""
echo "[4/4] 环境变量检查..."
if [ -z "$IHUDI_VOICE_ACCOUNT" ]; then
    echo "  ⚠️  IHUDI_VOICE_ACCOUNT 未设置!"
    echo "     请执行: export IHUDI_VOICE_ACCOUNT=你的账号ID"
    echo ""
else
    echo "  ✅ IHUDI_VOICE_ACCOUNT = ${IHUDI_VOICE_ACCOUNT:0:4}***"
fi

if [ -z "$IHUDI_VOICE_APIKEY" ]; then
    echo "  ⚠️  IHUDI_VOICE_APIKEY 未设置!"
    echo "     请执行: export IHUDI_VOICE_APIKEY=你的APIKey"
    echo ""
else
    echo "  ✅ IHUDI_VOICE_APIKEY = ${IHUDI_VOICE_APIKEY:0:4}***"
fi

echo ""
echo "========================================"
echo "  部署完成！启动服务:"
echo "========================================"
echo ""
echo "  方式A (开发测试):"
echo "    source venv/bin/activate"
echo "    python app.py"
echo ""
echo "  方式B (生产运行):"
echo "    source venv/bin/activate"
echo "    gunicorn -w 4 -b 0.0.0.0:5000 --timeout 600 app:app"
echo ""
echo "  方式C (后台守护进程):"
echo "    source venv/bin/activate"
echo "    nohup gunicorn -w 4 -b 0.0.0.0:5000 --timeout 600 app:app > auth.log 2>&1 &"
echo "    echo \$! > auth.pid"
echo ""
echo "  测试健康检查:"
echo "    curl http://localhost:5000/api/health"
echo ""

# 询问是否立即启动
read -p "是否现在启动服务? (y/N): " answer
if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    echo "正在启动..."
    exec gunicorn -w 4 -b 0.0.0.0:5000 --timeout 600 app:app
fi
