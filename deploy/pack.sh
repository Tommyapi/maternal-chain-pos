#!/bin/bash
# 本机打包：从 git HEAD 导出干净代码（不含 tests/qa/docs/.git/工作区杂物），生成 deploy/out/app.tar.gz + VERSION。
# 用法：bash deploy/pack.sh   （在 shouyin 仓库根目录执行）
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=deploy/out
rm -rf $OUT && mkdir -p $OUT
VER="$(date +%Y%m%d-%H%M)-$(git rev-parse --short HEAD)"
echo "$VER" > VERSION
git archive --format=tar HEAD shouyin_app wsgi.py requirements.txt scripts/migrate_db.py deploy > $OUT/app.tar
tar -rf $OUT/app.tar VERSION
# seed_data.json 不入库（客户数据），从工作区带上（只在首装/建平台账号时用）
[ -f seed/seed_data.json ] && tar -rf $OUT/app.tar seed/seed_data.json
gzip -f $OUT/app.tar
rm -f VERSION
echo "打包完成：$OUT/app.tar.gz 版本 $VER ($(stat -c %s $OUT/app.tar.gz) bytes)"
