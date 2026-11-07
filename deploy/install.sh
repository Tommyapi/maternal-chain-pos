#!/bin/bash
# 在服务器上执行（root）。可重复运行：创建用户/目录/venv、装依赖、初始化数据、装服务和每日备份。
# 用法：bash install.sh /root/shouyin-upload   （上传目录里要有 app.tar.gz、seed_data.json）
set -euo pipefail
SRC=${1:-/root/shouyin-upload}
BASE=/opt/chainpos

id shouyin >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin shouyin
mkdir -p $BASE/app $BASE/data $BASE/backup
tar -xzf $SRC/app.tar.gz -C $BASE/app
if [ ! -d $BASE/venv ]; then
  python3 -m venv $BASE/venv
fi
$BASE/venv/bin/pip install -q --upgrade pip
$BASE/venv/bin/pip install -q -r $BASE/app/requirements.txt

# 初始数据（可重复运行；admin 随机密码只在第一次生成）
cd $BASE/app
$BASE/venv/bin/python -m shouyin_app.seed --db $BASE/data/shouyin.db --data $SRC/seed_data.json --admin-pass-file $BASE/ADMIN_PASSWORD.txt
chmod 600 $BASE/ADMIN_PASSWORD.txt 2>/dev/null || true
chown -R shouyin:shouyin $BASE/data $BASE/backup
chown -R root:root $BASE/app $BASE/venv
chmod 700 $BASE
chmod 755 $BASE/app $BASE/venv 2>/dev/null || true
chmod 711 $BASE

install -m 644 $BASE/app/deploy/chainpos.service /etc/systemd/system/chainpos.service
install -m 755 $BASE/app/deploy/backup.sh $BASE/backup.sh
cat > /etc/cron.d/chainpos-backup <<'CRON'
30 3 * * * root /opt/chainpos/backup.sh >> /opt/chainpos/backup/backup.log 2>&1
CRON
chmod 644 /etc/cron.d/chainpos-backup
systemctl daemon-reload
systemctl enable chainpos >/dev/null 2>&1
systemctl restart chainpos
sleep 2
systemctl is-active chainpos
curl -s --max-time 10 http://127.0.0.1:8110/healthz; echo
