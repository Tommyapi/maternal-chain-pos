# -*- coding: utf-8 -*-
"""数据库升级（可重复执行）：备份 -> 迁移到各模块最新版本 -> 核对 -> 输出 JSON 报告。

用法（服务器上用 venv 的 python，先停服务或在低峰执行；迁移本身在一个事务里，失败自动回滚）：
    python scripts/migrate_db.py --db /opt/chainpos/data/shouyin.db
    python scripts/migrate_db.py --db data/shouyin.db --check      # 只核对不迁移
退出码：0 成功；1 核对不一致；2 参数/文件错误。
回滚：停服务，把报告里的 backup 文件拷回 --db 路径（同时删掉 -wal/-shm），再启动旧版本代码。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shouyin_app.db import connect, init_db, schema_versions   # noqa: E402

KEY_TABLES = ['stores', 'users', 'guides', 'products', 'product_barcodes', 'members', 'babies', 'stock', 'stock_moves',
              'orders', 'order_items', 'payments', 'shifts', 'stored_log', 'points_log', 'coupons', 'coupon_templates', 'promotions']


def counts(db):
    out = {}
    for t in KEY_TABLES:
        try:
            out[t] = db.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None
    return out


def backup(path):
    ts = time.strftime('%Y%m%d-%H%M%S')
    dst = '%s.bak-%s' % (path, ts)
    src = sqlite3.connect(path, timeout=30)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dst


def checks(db):
    """迁移后一致性核对。返回问题列表（空 = 通过）。"""
    problems = []
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        problems.append('integrity_check 失败')
    from shouyin_app.schema.base import MONEY_COLS, ADD_MERCHANT, REBUILD
    tables = ADD_MERCHANT + [x[0] for x in REBUILD]
    # 旧库（v1，未迁移）还没有 merchant_id / *_cents 列：--check 只报"待迁移"，不往下查（否则直接报错退出）
    pending = [t for t in tables if 'merchant_id' not in [r[1] for r in db.execute('PRAGMA table_info(%s)' % t)]]
    if pending:
        problems.append('待迁移：%d 张表还没有 merchant_id（%s 等），去掉 --check 运行迁移' % (len(pending), ', '.join(pending[:3])))
        return problems
    for t in tables:
        n = db.execute('SELECT COUNT(*) FROM %s WHERE merchant_id IS NULL' % t).fetchone()[0]
        if n:
            problems.append('%s 有 %d 行 merchant_id 为空' % (t, n))
    for t, cols in MONEY_COLS.items():
        for c in cols:
            n = db.execute('SELECT COUNT(*) FROM %s WHERE "%s" IS NOT NULL AND (%s_cents IS NULL OR %s_cents != CAST(ROUND("%s"*100) AS INTEGER))'
                           % (t, c, c, c, c)).fetchone()[0]
            if n:
                problems.append('%s.%s 有 %d 行分值与元值不一致' % (t, c, n))
    rows = db.execute("SELECT s.merchant_id, s.store_code, s.product_id, s.qty, COALESCE((SELECT SUM(i.qty) FROM inv_stock i WHERE i.merchant_id=s.merchant_id "
                      "AND i.wh_code=s.store_code AND i.product_id=s.product_id AND i.quality='ok'),0) inv FROM stock s").fetchall()
    bad = [tuple(r) for r in rows if abs(float(r['qty'] or 0) - float(r['inv'])) > 1e-9]
    if bad:
        problems.append('stock 与 inv_stock 不一致 %d 行，例：%r' % (len(bad), bad[:3]))
    n = db.execute('SELECT COUNT(*) FROM users WHERE role_id IS NULL').fetchone()[0]
    if n:
        problems.append('%d 个账号没有 role_id' % n)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--check', action='store_true', help='只核对，不迁移、不备份')
    ap.add_argument('--no-backup', action='store_true')
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(json.dumps({'ok': False, 'error': '数据库不存在：%s' % a.db}, ensure_ascii=False))
        return 2
    report = {'db': os.path.abspath(a.db), 'started': time.strftime('%Y-%m-%d %H:%M:%S')}
    db = connect(a.db)
    report['versions_before'] = schema_versions(db)
    report['counts_before'] = counts(db)
    db.close()
    if not a.check:
        report['backup'] = None if a.no_backup else backup(a.db)
        t0 = time.time()
        report['migrated'] = dict((k, list(v)) for k, v in init_db(a.db).items())
        report['seconds'] = round(time.time() - t0, 2)
    db = connect(a.db)
    report['versions_after'] = schema_versions(db)
    report['counts_after'] = counts(db)
    problems = checks(db)
    lost = [t for t in KEY_TABLES if report['counts_before'].get(t) is not None and report['counts_after'].get(t) != report['counts_before'].get(t)]
    if lost:
        problems.append('迁移前后行数变化：%s' % ', '.join('%s %s->%s' % (t, report['counts_before'][t], report['counts_after'][t]) for t in lost))
    db.close()
    report['problems'] = problems
    report['ok'] = not problems
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
