# -*- coding: utf-8 -*-
"""表结构与迁移（按模块拆分）。

每个模块文件导出：
    MODULE   模块名（schema_version 表里的键）
    VERSION  当前版本号（整数，模块 owner 自己递增）
    SCHEMA   CREATE TABLE/INDEX IF NOT EXISTS ...（最新形态，可重复执行）
    migrate(db, from_version)  把旧库从 from_version 升到 VERSION；必须幂等（可重复执行不出错、不重复写数据）

执行顺序见 MODULES：base 永远第一个（它把 v1 旧表升级为多租户 + 分存储）。
db.init_db() 对每个模块：执行 SCHEMA → migrate(db, 已记录版本) → 记录 VERSION。
"""
from . import base, tenant_auth, goods_stock, member_promo, pos, mobile

MODULES = [base, tenant_auth, goods_stock, member_promo, pos, mobile]


# ---------------------------------------------------------------- 迁移小工具（各模块 migrate 里用）
def run_script(db, sql):
    """逐条执行 SQL 脚本（不用 executescript：它会先 COMMIT，破坏迁移事务）。支持 TRIGGER ... BEGIN ... END。"""
    import sqlite3
    buf = ''
    for line in sql.splitlines(True):
        if not buf and (not line.strip() or line.strip().startswith('--')):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            db.execute(buf)
            buf = ''
    if buf.strip():
        db.execute(buf)


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def columns(db, table):
    return [r[1] for r in db.execute('PRAGMA table_info(%s)' % table)]


def add_column(db, table, col, decl):
    """幂等加列：列已存在则跳过。decl 例：'INTEGER NOT NULL DEFAULT 0'。"""
    if col not in columns(db, table):
        db.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, decl))
        return True
    return False


def get_version(db, module):
    r = db.execute('SELECT version FROM schema_version WHERE module=?', (module,)).fetchone()
    return int(r[0]) if r else 0


def set_version(db, module, version):
    db.execute("INSERT INTO schema_version(module, version, updated_at) VALUES(?,?,datetime('now','localtime')) "
               "ON CONFLICT(module) DO UPDATE SET version=excluded.version, updated_at=excluded.updated_at", (module, version))
