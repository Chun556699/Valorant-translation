#!/usr/bin/env bash
# 裸机一键安装（Debian/Ubuntu/CentOS，需 root）：
#   tar xzf vtranslate-server.tar.gz && cd vtranslate-server && sudo bash deploy/install.sh
# 重复执行即为升级（保留 .env 与数据库）。
set -euo pipefail

APP=/opt/vtranslate
DATA=/var/lib/vtranslate
SRC="$(cd "$(dirname "$0")/.." && pwd)"

command -v python3 >/dev/null || { echo "需要 python3 (>=3.10)"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "python3 版本需 >= 3.10"; exit 1; }

id vtranslate >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$APP" vtranslate
mkdir -p "$APP/server" "$DATA"

# 代码：覆盖 .py / 词表 / 后台静态页，不动 .env
cp "$SRC"/server/*.py "$SRC"/server/game_terms.json "$APP/server/"
rm -rf "$APP/server/admin" && cp -r "$SRC/server/admin" "$APP/server/admin"
cp "$SRC/requirements-server.txt" "$APP/"

if [ ! -f "$APP/server/.env" ]; then
  cp "$SRC/server/.env.example" "$APP/server/.env"
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
  sed -i "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$TOKEN|" "$APP/server/.env"
  echo ">>> 已生成 $APP/server/.env，管理令牌 ADMIN_TOKEN=$TOKEN"
  echo ">>> 请编辑该文件填写 DASHSCOPE_API_KEY / ZPAY_* 后再执行：systemctl restart vtranslate"
fi
chmod 600 "$APP/server/.env"

[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements-server.txt"

chown -R vtranslate:vtranslate "$APP" "$DATA"
cp "$SRC/deploy/vtranslate.service" /etc/systemd/system/vtranslate.service
systemctl daemon-reload
systemctl enable --now vtranslate
systemctl restart vtranslate
sleep 2
curl -fsS http://127.0.0.1:8765/healthz && echo && echo ">>> 服务已启动：反代配置见 deploy/nginx.conf，后台地址 https://你的域名/admin/"
