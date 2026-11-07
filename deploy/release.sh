#!/bin/bash
# v3 起的升级发布脚本（在服务器上以 root 执行，可重复运行）。
# 用法：bash release.sh /root/shouyin-upload [--platform-pass-file /opt/chainpos/PLATFORM_PASSWORD.txt]
# 流程：备份库 → 解压到 app.new → 装依赖 → 在正式库上跑迁移（失败即中止，库已备份、旧代码未动）
#       → 切换 app 目录（保留上一版 app.prev 供回滚）→ 重启 → 本机 healthz → 装每小时备份与外部探测。
# 回滚：bash release.sh --rollback   （换回 app.prev，并提示用 backup/pre-release-*.db 恢复库）
set -euo pipefail
BASE=/opt/chainpos
VENV=$BASE/venv/bin
DB=$BASE/data/shouyin.db
STAMP=$(date +%Y%m%d-%H%M%S)
LOG=$BASE/backup/release.log
mkdir -p $BASE/backup
log() { echo "$(date '+%F %T') $*" | tee -a $LOG; }

if [ "${1:-}" = "--rollback" ]; then
  [ -d $BASE/app.prev ] || { log "没有 app.prev，无法回滚"; exit 1; }
  rm -rf $BASE/app.bad && mv $BASE/app $BASE/app.bad && mv $BASE/app.prev $BASE/app
  systemctl restart chainpos; sleep 2; systemctl is-active chainpos
  log "已回滚到上一版代码。库若需回退：systemctl stop chainpos && cp backup/pre-release-<时间>.db data/shouyin.db && chown shouyin:shouyin data/shouyin.db && systemctl start chainpos"
  exit 0
fi

SRC=${1:-/root/shouyin-upload}
PLATFORM_PASS=""
if [ "${2:-}" = "--platform-pass-file" ]; then PLATFORM_PASS=${3:?}; fi
[ -f $SRC/app.tar.gz ] || { log "缺 $SRC/app.tar.gz"; exit 1; }

# 1. 备份正式库（在线备份，不停服）
BK=$BASE/backup/pre-release-$STAMP.db
/usr/bin/sqlite3 "$DB" ".timeout 30000" ".backup '$BK'"
/usr/bin/sqlite3 "$BK" "PRAGMA integrity_check;" | grep -qx ok
log "备份 $BK ok ($(stat -c %s $BK) bytes)"

# 2. 解压新代码到 app.new
rm -rf $BASE/app.new && mkdir -p $BASE/app.new
tar -xzf $SRC/app.tar.gz -C $BASE/app.new
$VENV/pip install -q -r $BASE/app.new/requirements.txt
log "代码解压 + 依赖安装 ok"

# 3. 在正式库上跑迁移（库已备份；此时线上仍是旧代码 + 旧表，迁移脚本是幂等的）
cd $BASE/app.new
if ! $VENV/python scripts/migrate_db.py --db $DB --no-backup > $BASE/backup/migrate-$STAMP.json 2>&1; then
  log "迁移失败，已中止（旧代码未动）。报告：$BASE/backup/migrate-$STAMP.json"
  tail -20 $BASE/backup/migrate-$STAMP.json
  exit 1
fi
grep -q '"ok": true' $BASE/backup/migrate-$STAMP.json || { log "迁移报告不是 ok，已中止"; tail -20 $BASE/backup/migrate-$STAMP.json; exit 1; }
log "迁移 ok（报告 $BASE/backup/migrate-$STAMP.json）"
if [ -n "$PLATFORM_PASS" ]; then
  $VENV/python -m shouyin_app.seed --db $DB --platform-pass-file "$PLATFORM_PASS" >/dev/null
  chmod 600 "$PLATFORM_PASS" 2>/dev/null || true
  log "平台账号 platform 已确保存在（密码文件 $PLATFORM_PASS，只在首次创建时写入）"
fi

# 4. 切换代码目录并重启
rm -rf $BASE/app.prev
[ -d $BASE/app ] && mv $BASE/app $BASE/app.prev
mv $BASE/app.new $BASE/app
chown -R root:root $BASE/app
chown -R shouyin:shouyin $BASE/data $BASE/backup
install -m 644 $BASE/app/deploy/chainpos.service /etc/systemd/system/chainpos.service
systemctl daemon-reload
systemctl restart chainpos
sleep 3
systemctl is-active chainpos >/dev/null || { log "服务没起来，执行回滚"; bash $BASE/app.prev/deploy/release.sh --rollback || true; exit 1; }
curl -fsS --max-time 10 http://127.0.0.1:8110/healthz >/dev/null || { log "healthz 失败，执行回滚"; bash $BASE/app.prev/deploy/release.sh --rollback || true; exit 1; }
log "服务重启 ok，healthz ok"

# 5. 每小时备份（保留 7 天）+ 每 5 分钟外部探测（连续 3 次失败自动重启并记录）
install -m 755 $BASE/app/deploy/backup.sh $BASE/backup.sh
install -m 755 $BASE/app/deploy/probe.sh $BASE/probe.sh
cat > /etc/cron.d/chainpos-backup <<'CRON'
30 * * * * root /opt/chainpos/backup.sh >> /opt/chainpos/backup/backup.log 2>&1
*/5 * * * * root /opt/chainpos/probe.sh >> /opt/chainpos/backup/probe.log 2>&1
CRON
chmod 644 /etc/cron.d/chainpos-backup
log "发布完成：$(cd $BASE/app && cat VERSION 2>/dev/null || echo 未知版本)"
