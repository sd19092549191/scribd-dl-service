#!/usr/bin/env bash
# scribd-dl-service 一键安装脚本
# 用法:
#   1) 交互式:  bash install.sh
#   2) 非交互:  SCRIBD_COOKIE='_scribd_session=...' [SCRIBD_TOKEN=xxx] bash install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8090}"

echo "==> Scribd DL Service 安装程序"

if [ -z "${SCRIBD_COOKIE:-}" ]; then
  read -r -p "粘贴 _scribd_session cookie (形如 _scribd_session=xxxx): " SCRIBD_COOKIE
  [ -z "$SCRIBD_COOKIE" ] && { echo "✗ cookie 不能为空"; exit 1; }
fi
[ -z "${SCRIBD_TOKEN:-}" ] && read -r -p "[可选] 设置访问口令 SCRIBD_TOKEN (回车跳过): " SCRIBD_TOKEN || true

cat > "$REPO_DIR/.env" <<EOF
SCRIBD_COOKIE=$SCRIBD_COOKIE
SCRIBD_TOKEN=$SCRIBD_TOKEN
PORT=$PORT
EOF
chmod 600 "$REPO_DIR/.env"
echo "==> .env 已写入 (权限 600)"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "==> 检测到 Docker Compose, 使用容器部署"
  cd "$REPO_DIR"
  docker compose up -d --build
  echo "==> 等待启动..."
  sleep 3
  curl -fsS "http://127.0.0.1:${PORT}/healthz" && echo "" && echo "✓ Docker 部署完成: http://<服务器IP>:${PORT}/"
  exit 0
fi

echo "==> 未检测到 Docker Compose, 使用 systemd + venv 部署"
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

SERVICE_NAME="scribd-dl"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Scribd DL Service
After=network.target

[Service]
WorkingDirectory=$REPO_DIR
EnvironmentFile=$REPO_DIR/.env
ExecStart=$REPO_DIR/.venv/bin/uvicorn scribd_service:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
sleep 3
systemctl --no-pager -l status "$SERVICE_NAME" | head -5
curl -fsS "http://127.0.0.1:${PORT}/healthz" && echo "" && echo "✓ systemd 部署完成: http://<服务器IP>:${PORT}/  (服务名: $SERVICE_NAME)"
