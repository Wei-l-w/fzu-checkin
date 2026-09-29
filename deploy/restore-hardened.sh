#!/bin/bash
# 恢复本次加固快照；不修改私人配置/登录态/提交意图，不自动重新启用。
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo '请通过sudo执行此回滚脚本' >&2; exit 1; }
[[ -d /opt/fzu-checkin && ! -L /opt/fzu-checkin ]] || exit 1
[[ ! -L /opt/fzu-checkin/venv ]] || { echo '拒绝恢复符号链接venv，请人工核查目标。' >&2; exit 1; }
[[ -f /opt/fzu-checkin/rollback/hardened-release.tar.gz ]] || exit 1
cd /opt/fzu-checkin/rollback
sha256sum -c hardened-release.sha256
# A later UI must not remain exposed while reverting its supporting code.
for unit in fzu-checkin-web.service fzu-checkin-control.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then
        systemctl disable --now "$unit"
    fi
done
if ! /usr/local/sbin/fzu-checkinctl pause; then
    echo '当前应用暂停命令失败；先独立停用单元，修复后重新落暂停标记。' >&2
fi
systemctl disable --now fzu-checkin.timer
systemctl stop fzu-checkin.service fzu-checkin-preflight.service
tar -xzf /opt/fzu-checkin/rollback/hardened-release.tar.gz -C /opt/fzu-checkin
if [[ -d /opt/fzu-checkin/venv && ! -L /opt/fzu-checkin/venv ]]; then
    restore_backup_dir=$(mktemp -d /opt/fzu-checkin/rollback/venv-before-rollback-XXXXXXXX)
    mv /opt/fzu-checkin/venv "$restore_backup_dir/venv"
fi
python3 -m venv /opt/fzu-checkin/venv
/opt/fzu-checkin/venv/bin/python -m pip install --disable-pip-version-check \
    --no-index --find-links=/opt/fzu-checkin/rollback/wheels \
    -r /opt/fzu-checkin/requirements.lock
/opt/fzu-checkin/venv/bin/python -m pip check
install -m 0644 /opt/fzu-checkin/deploy/fzu-checkin.service /etc/systemd/system/fzu-checkin.service
install -m 0644 /opt/fzu-checkin/deploy/fzu-checkin-preflight.service /etc/systemd/system/fzu-checkin-preflight.service
install -m 0644 /opt/fzu-checkin/deploy/fzu-checkin.timer /etc/systemd/system/fzu-checkin.timer
install -m 0755 /opt/fzu-checkin/deploy/fzu-checkinctl /usr/local/sbin/fzu-checkinctl
systemctl daemon-reload
/usr/local/sbin/fzu-checkinctl pause
echo '本项目加固快照已恢复，私有配置和签到状态保留；timer继续停用。请先做只读预检，再明确恢复。'
