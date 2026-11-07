#!/bin/bash
# 每小时备份一次收银数据库（SQLite 在线备份，不影响收银），保留 7 天。
# cron: /etc/cron.d/chainpos-backup 每小时 :30 运行（release.sh 安装）
set -euo pipefail
DB=/opt/chainpos/data/shouyin.db
DIR=/opt/chainpos/backup
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DIR/shouyin-$STAMP.db"
mkdir -p "$DIR"
/usr/bin/sqlite3 "$DB" ".timeout 30000" ".backup '$OUT'"
/usr/bin/sqlite3 "$OUT" "PRAGMA integrity_check;" | grep -qx ok
gzip -f "$OUT"
find "$DIR" -maxdepth 1 -name 'shouyin-*.db.gz' -mtime +7 -delete
echo "$(date '+%F %T') backup ok: $OUT.gz"
