#!/bin/bash
# One-shot, idempotent migration from the single-profile layout to profiles/<user>/ (owner "admin").
set -euo pipefail
BASE=/var/lib/fzu-checkin
install -d -m 0700 -o fzu-checkin -g fzu-checkin "$BASE/profiles" "$BASE/users"
if [[ -f "$BASE/config.yaml" && ! -e "$BASE/profiles/admin" ]]; then
    systemctl disable --now fzu-checkin.timer 2>/dev/null || true
    systemctl stop fzu-checkin.service 2>/dev/null || true
    install -d -m 0700 -o fzu-checkin -g fzu-checkin "$BASE/profiles/admin"
    mv "$BASE/config.yaml" "$BASE/profiles/admin/config.yaml"
    [[ -d "$BASE/state" ]] && mv "$BASE/state" "$BASE/profiles/admin/state" || install -d -m 0700 -o fzu-checkin -g fzu-checkin "$BASE/profiles/admin/state"
    [[ -d "$BASE/backups" ]] && mv "$BASE/backups" "$BASE/profiles/admin/backups" || install -d -m 0700 -o fzu-checkin -g fzu-checkin "$BASE/profiles/admin/backups"
    echo "moved single profile -> profiles/admin"
fi
if [[ ! -f "$BASE/users/users.json" ]]; then
    runuser -u fzu-checkin -- env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /opt/fzu-checkin/venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "/opt/fzu-checkin")
from src.admin_auth import read_users_file, write_users_file
users = read_users_file("/var/lib/fzu-checkin/admin/auth.json")   # legacy v1 -> owner "admin", same password
write_users_file("/var/lib/fzu-checkin/users/users.json", users)
print("users.json created for:", sorted(users))
PY
fi
install -m 0644 deploy/fzu-checkin@.service deploy/fzu-checkin@.timer deploy/fzu-checkin-preflight@.service deploy/fzu-checkin-control.service /etc/systemd/system/
# The web unit is rendered by deploy/install.sh (origin/base path/port); keep the existing one.
install -m 0755 -o root -g root deploy/fzu-checkinctl /usr/local/sbin/fzu-checkinctl
systemctl disable fzu-checkin.timer 2>/dev/null || true
rm -rf /etc/systemd/system/fzu-checkin.timer.d
rm -f /etc/systemd/system/fzu-checkin.timer /etc/systemd/system/fzu-checkin.service /etc/systemd/system/fzu-checkin-preflight.service
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
systemctl restart fzu-checkin-control.service fzu-checkin-web.service
echo "migration done"
