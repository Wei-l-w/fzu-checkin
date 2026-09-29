#!/bin/bash
# 通用安装脚本：任何带 systemd 的 Linux 服务器（Ubuntu 22.04/24.04、Debian 12 已验证思路）。
# 用法：sudo deploy/install.sh --origin https://your.domain [--base-path /fzu] [--port 18779] [--render-only DIR]
set -euo pipefail
ORIGIN=""; BASE_PATH="/fzu"; PORT="18779"; RENDER_ONLY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --origin) ORIGIN="$2"; shift 2 ;;
        --base-path) BASE_PATH="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --render-only) RENDER_ONLY="$2"; shift 2 ;;
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done
[[ "$ORIGIN" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || { echo '--origin 必须是 https://域名（不含路径），例如 https://checkin.example.com' >&2; exit 2; }
[[ "$BASE_PATH" =~ ^/[A-Za-z0-9_-]{1,48}$ ]] || { echo '--base-path 必须是单段路径，例如 /fzu' >&2; exit 2; }
[[ "$PORT" =~ ^[0-9]{4,5}$ ]] || { echo '--port 必须是 1024–65535 的端口' >&2; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
render_units() {
    local out="$1"; mkdir -p "$out"
    sed -e "s|@ORIGIN@|$ORIGIN|" -e "s|@BASE_PATH@|$BASE_PATH|" -e "s|@PORT@|$PORT|" "$SRC/deploy/fzu-checkin-web.service" > "$out/fzu-checkin-web.service"
    cp "$SRC/deploy/fzu-checkin@.service" "$SRC/deploy/fzu-checkin@.timer" "$SRC/deploy/fzu-checkin-preflight@.service" "$SRC/deploy/fzu-checkin-control.service" "$out/"
}
if [[ -n "$RENDER_ONLY" ]]; then render_units "$RENDER_ONLY"; echo "已渲染到 $RENDER_ONLY（未安装）"; exit 0; fi
[[ $EUID -eq 0 ]] || { echo '请用 sudo 运行' >&2; exit 1; }
command -v systemctl >/dev/null || { echo '需要 systemd' >&2; exit 1; }
PY=$(command -v python3 || true); [[ -n "$PY" ]] || { echo '需要 python3（3.11+）' >&2; exit 1; }
"$PY" - <<'PY' || { echo '需要 Python 3.11 或更高，并安装 python3-venv' >&2; exit 1; }
import sys, venv; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
APP=/opt/fzu-checkin; DATA=/var/lib/fzu-checkin; SVC=fzu-checkin
id -u "$SVC" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC"
if [[ "$SRC" != "$APP" ]]; then
    mkdir -p "$APP"
    tar -C "$SRC" --exclude=.git --exclude=venv --exclude=rollback --exclude='__pycache__' --exclude='*.pyc' -cf - . | tar -C "$APP" -xf -
fi
chown -R root:root "$APP"; find "$APP" -type d -exec chmod 755 {} +; find "$APP" -type f -exec chmod 644 {} +
[[ -x "$APP/venv/bin/python" ]] || "$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.lock" || "$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"
install -d -m 0700 -o "$SVC" -g "$SVC" "$DATA" "$DATA/profiles" "$DATA/users" "$DATA/admin"
TMP=$(mktemp -d); render_units "$TMP"; install -m 0644 "$TMP"/* /etc/systemd/system/; rm -rf "$TMP"
install -m 0755 -o root -g root "$APP/deploy/fzu-checkinctl" /usr/local/sbin/fzu-checkinctl
systemctl daemon-reload
systemctl enable --now fzu-checkin-control.service
systemctl restart fzu-checkin-control.service
if [[ ! -f "$DATA/users/users.json" ]]; then
    if [[ -t 0 ]]; then
        runuser -u "$SVC" -- env -i PATH=/usr/bin:/bin LANG=C.UTF-8 FZU_ADMIN_USERS_FILE="$DATA/users/users.json" "$APP/venv/bin/python" "$APP/admin_server.py" init-users
    else
        echo "尚未创建管理员账户：稍后在终端执行  sudo -u $SVC FZU_ADMIN_USERS_FILE=$DATA/users/users.json $APP/venv/bin/python $APP/admin_server.py init-users"
    fi
fi
systemctl enable --now fzu-checkin-web.service
systemctl restart fzu-checkin-web.service
cat <<EOF

安装完成。管理页监听 127.0.0.1:$PORT，路径前缀 $BASE_PATH ，请在你的 HTTPS 反向代理里加一条转发（示例）：

  Caddy（写在该域名的站点块里）:
    handle $BASE_PATH* {
        reverse_proxy 127.0.0.1:$PORT
    }

  Nginx:
    location $BASE_PATH/ { proxy_pass http://127.0.0.1:$PORT; proxy_set_header Host \$host; }

然后打开 $ORIGIN$BASE_PATH/ ，用管理员账户登录，在“成员管理”里为同学创建账户。
常用命令：sudo fzu-checkinctl status|preflight|pause|resume [用户名]
EOF
