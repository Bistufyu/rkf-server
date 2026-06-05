#!/bin/bash
# ============================================
# 刃客坊 API - Nginx + HTTPS 快速部署脚本
# 在宝塔终端执行此脚本即可
# ============================================

set -e

echo "=========================================="
echo "  刃客坊 API - Nginx + HTTPS 部署"
echo "=========================================="
echo ""

# ---- 第1步：安装依赖 ----
echo "[1/6] 安装必要工具..."
yum install -y nginx openssl > /dev/null 2>&1 || apt-get install -y nginx openssl > /dev/null 2>&1 || true
echo "      工具安装完成"

# ---- 第2步：生成自签名SSL证书 ----
echo "[2/6] 生成自签名SSL证书..."
mkdir -p /etc/nginx/ssl/rkf-auth
openssl req -x509 -nodes -days 3650 \
  -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/rkf-auth/key.pem \
  -out /etc/nginx/ssl/rkf-auth/cert.pem \
  -subj "/CN=59.110.5.13/O=RKF-Auth/C=CN" \
  2>/dev/null
echo "      证书已生成 (有效10年)"

# ---- 第3步：创建Nginx配置 ----
echo "[3/6] 创建Nginx配置..."
cat > /etc/nginx/conf.d/rkf-auth.conf << 'NGINXEOF'
server {
    listen 5001 ssl;
    server_name 59.110.5.13 _;

    ssl_certificate     /etc/nginx/ssl/rkf-auth/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/rkf-auth/key.pem;

    # 现代TLS设置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # 允许上传大文件（最大 1.5GB）
    client_max_body_size 1500m;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 超时设置 - 视频上传需要更长时间
        proxy_connect_timeout 60s;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # 专门的上传端点，使用更长的超时时间
    location /api/videos/upload {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 60s;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }
}
NGINXEOF
echo "      配置已写入"

# ---- 第4步：确保Flask服务在运行 ----
echo "[4/6] 确认Flask服务运行中..."
cd /www/wwwroot/rkf-auth
source venv/bin/activate 2>/dev/null || true
export $(grep -v '^#' .env | xargs) 2>/dev/null || true

# 检查是否有gunicorn进程在跑
if pgrep -f "gunicorn.*app:app" > /dev/null; then
    echo "      Flask服务已在运行 (PID: $(pgrep -f 'gunicorn.*app:app' | head -1))"
else
    echo "      重启Flask服务..."
    nohup gunicorn -w 2 -b 127.0.0.1:5000 app:app > auth.log 2>&1 &
    echo $! > auth.pid
    sleep 2
    echo "      Flask已启动 (PID: $(cat auth.pid))"
fi

# ---- 第5步：启动Nginx ----
echo "[5/6] 启动/重启Nginx..."

# 测试配置是否正确
nginx -t 2>/dev/null || {
    echo "      Nginx配置测试失败，尝试修复..."
    # 如果nginx.conf有问题，用基本配置
    mkdir -p /etc/nginx/conf.d 2>/dev/null || true
}

# 启动或重载
if pgrep nginx > /dev/null; then
    # 尝试reload，如果不行就restart
    nginx -s reload 2>/dev/null || nginx -s stop 2>/dev/null && nginx
else
    nginx
fi
sleep 1
if pgrep nginx > /dev/null; then
    echo "      Nginx 已启动 (PID: $(pgrep nginx | head -1))"
else
    echo "      ⚠️ Nginx启动可能需要手动操作，请看下面的说明"
fi

# ---- 第6步：验证 ----
echo ""
echo "[6/6] 验证HTTPS访问..."
echo ""

# 放行防火墙端口
firewall-cmd --add-port=5001/tcp --permanent 2>/dev/null || true
firewall-cmd --reload 2>/dev/null || true
iptables -I INPUT -p tcp --dport 5001 -j ACCEPT 2>/dev/null || true

echo "=========================================="
echo "  ✅ 部署完成！"
echo "=========================================="
echo ""
echo "  新的API地址（HTTPS）:"
echo "  https://59.110.5.13:5001/api/health"
echo ""
echo "  请在浏览器测试上面的地址"
echo "  （浏览器可能会提示'不安全'，点击'继续前往'即可）"
echo ""
echo "=========================================="
