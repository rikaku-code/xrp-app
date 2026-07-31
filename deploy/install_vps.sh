#!/usr/bin/env bash
# XRP 监控 VPS 一键部署（Ubuntu 22.04 / Debian 12）
# 用法：sudo bash install_vps.sh

set -euo pipefail

APP_DIR="/opt/xrp_monitor"
APP_USER="xrpmon"
SERVICE_NAME="xrp-monitor"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "请使用 root 运行：sudo bash install_vps.sh"
  exit 1
fi

echo "==> 安装系统依赖..."
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip rsync

echo "==> 创建用户 ${APP_USER}..."
id -u "${APP_USER}" &>/dev/null || useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"

echo "==> 同步程序文件到 ${APP_DIR}..."
mkdir -p "${APP_DIR}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rsync -a --exclude venv --exclude __pycache__ --exclude .git \
  "${SCRIPT_DIR}/xrp_monitor.py" \
  "${SCRIPT_DIR}/requirements.txt" \
  "${SCRIPT_DIR}/.env.example" \
  "${APP_DIR}/"

if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo ""
  echo "!!! 请先编辑 ${APP_DIR}/.env 填入 Lark Webhook 后再启动服务"
  echo ""
fi

echo "==> 创建 Python 虚拟环境..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install -U pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "==> 注册 systemd 服务..."
install -m 644 "${SCRIPT_DIR}/deploy/xrp-monitor.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo ""
echo "部署完成。"
echo ""
echo "下一步："
echo "  1. 编辑配置：  nano ${APP_DIR}/.env"
echo "  2. 启动服务：  systemctl start ${SERVICE_NAME}"
echo "  3. 查看状态：  systemctl status ${SERVICE_NAME}"
echo "  4. 查看日志：  journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "静默时段按 APP_TIMEZONE=Asia/Tokyo（日本时间 0:00-8:00）"
