#!/usr/bin/env bash
# Debian 12 安装脚本。
#
# 只做「装依赖 + 建 venv + 建数据目录」，不改系统服务、不写 .env、不跑迁移 ——
# 那三件事都需要部署者自己决定内容（尤其是 .env 里的凭据）。
# 脚本必须可重复执行：venv 已存在时 python3 -m venv 会复用而不是报错。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip build-essential libpq-dev postgresql-client

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel "setuptools>=83"
pip install -r requirements.txt

mkdir -p data/sessions data/backups

echo "Install complete."
echo "Next:"
echo "  cp .env.example .env"
echo "  edit .env"
echo "  source .venv/bin/activate && alembic upgrade head"
echo "  python -m app.main"
