# -*- coding: utf-8 -*-
"""SQLite（WAL）连接 + 按模块的表结构/迁移执行器。

表结构不再写在这里：见 shouyin_app/schema/*.py（每个模块一个文件，各自维护 VERSION）。
init_db() 在一个 BEGIN IMMEDIATE 事务里依次执行各模块 SCHEMA + migrate，失败整体回滚；
多个 gunicorn worker 同时启动也只会有一个真正执行迁移。
"""
import os
import sqlite3


def connect(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)  # 自动提交，事务显式 BEGIN IMMEDIATE
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    return db


def schema_versions(db):
    from .schema import MODULES, table_exists, get_version
    if not table_exists(db, 'schema_version'):
        return dict((m.MODULE, 0) for m in MODULES)
    return dict((m.MODULE, get_version(db, m.MODULE)) for m in MODULES)


def migrate_all(db):
    """把库升级到所有模块的最新版本。返回 {module: (from, to)}（只含实际升级的模块）。"""
    from .schema import MODULES, base, run_script, get_version, set_version
    done = {}
    db.execute('PRAGMA foreign_keys=OFF')          # 重建表需要；必须在事务外设置
    db.execute('BEGIN IMMEDIATE')
    try:
        run_script(db, base.SCHEMA)
        if all(get_version(db, m.MODULE) >= m.VERSION for m in MODULES):
            db.execute('COMMIT')
            return done
        if get_version(db, base.MODULE) < base.VERSION:
            run_script(db, base.LEGACY_V1)        # 新库：先建 v1 底表，再和旧库走同一条迁移路径
        for m in MODULES:
            cur = get_version(db, m.MODULE)
            run_script(db, m.SCHEMA)
            if cur < m.VERSION:
                m.migrate(db, cur)
                set_version(db, m.MODULE, m.VERSION)
                done[m.MODULE] = (cur, m.VERSION)
        bad = db.execute('PRAGMA foreign_key_check').fetchall()
        if bad:
            raise RuntimeError('迁移后外键检查失败：%r' % [tuple(r) for r in bad[:5]])
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')
    return done


def cleanup_export_jobs(db):
    """进程启动时：异步导出跑在进程内线程，进程重启后 queued/running 的任务不会再有人处理，标记失败（REPORT-4 §7-5）。
    多 worker 时某个 worker 重启也会把别的 worker 正在跑的任务标成失败；那个任务真正跑完时会无条件写回 done，不会丢结果。"""
    from .schema import table_exists
    if not table_exists(db, 'export_jobs'):
        return 0
    cur = db.execute("UPDATE export_jobs SET status='failed', error='服务重启', finished_at=datetime('now','localtime') "
                     "WHERE status IN ('queued','running')")
    return cur.rowcount


def init_db(path):
    db = connect(path)
    try:
        done = migrate_all(db)
        cleanup_export_jobs(db)
        return done
    finally:
        db.close()
