# -*- coding: utf-8 -*-
"""base：跨模块公共表 + v1 旧库升级（多租户 merchant_id、金额分列、唯一约束改为商户内唯一）。

Owner：架构（冻结）。其他 agent 需要改这里，在报告里提出，由总脑合并。

v1 -> v2 升级做了什么（scripts/migrate_db.py 会先备份再执行）：
1. 建 merchants，写入 1 号商户「萌贝母婴连锁」，所有旧数据归 merchant_id=1。
2. 重建 users / stores / guides / products / product_barcodes / members / settings：
   去掉 users.role 的 CHECK；编码、条码、手机号、配置键改为「商户内唯一」；主键/自增 id 原样保留。
3. 其余旧业务表 ALTER ADD merchant_id（默认 1）。
4. 旧表金额列（REAL 元）旁新增 <列>_cents（INTEGER 分）并回填；装同步触发器，
   旧代码写 REAL、新代码写 *_cents，两边自动一致。新代码一律读写 *_cents（见 docs/ARCH.md §5）。
"""
from ._legacy_v1 import LEGACY_V1  # noqa: F401  (db.init_db 用它建 v1 底表，再统一走迁移)

MODULE = 'base'
VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (module TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT);

CREATE TABLE IF NOT EXISTS merchants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, contact TEXT DEFAULT '', phone TEXT DEFAULT '', address TEXT DEFAULT '', biz_info TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('trial','active','expired','disabled')),
    plan TEXT DEFAULT '', expire_date TEXT DEFAULT '',
    max_stores INTEGER NOT NULL DEFAULT 0, max_users INTEGER NOT NULL DEFAULT 0,
    open_request_no TEXT UNIQUE,
    created_at TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, store_code TEXT DEFAULT '', terminal_no TEXT DEFAULT '',
    user_id INTEGER, username TEXT DEFAULT '', module TEXT NOT NULL, action TEXT NOT NULL, ref_no TEXT DEFAULT '',
    before_json TEXT DEFAULT '', after_json TEXT DEFAULT '', reason TEXT DEFAULT '', ip TEXT DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info','high','security')), created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(merchant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_ref ON audit_logs(merchant_id, ref_no);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(merchant_id, username, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_act ON audit_logs(merchant_id, module, action, created_at);
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON audit_logs BEGIN SELECT RAISE(ABORT, '审计日志不可修改'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON audit_logs BEGIN SELECT RAISE(ABORT, '审计日志不可删除'); END;

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, approval_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL,
    store_code TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, ref_type TEXT DEFAULT '', ref_no TEXT DEFAULT '',
    title TEXT DEFAULT '', payload TEXT NOT NULL DEFAULT '{}', amount_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled','consumed','expired')),
    need_perm TEXT NOT NULL,
    applicant_id INTEGER, applicant_name TEXT DEFAULT '', apply_reason TEXT DEFAULT '',
    approver_id INTEGER, approver_name TEXT DEFAULT '', decide_reason TEXT DEFAULT '',
    request_key TEXT,
    created_at TEXT NOT NULL, decided_at TEXT, consumed_at TEXT, expire_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_reqkey ON approvals(merchant_id, request_key) WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(merchant_id, status, store_code, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_ref ON approvals(merchant_id, ref_type, ref_no);
CREATE TABLE IF NOT EXISTS approval_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, approval_no TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
    user_id INTEGER, username TEXT DEFAULT '', reason TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_approval_logs_no ON approval_logs(approval_no);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, user_id INTEGER NOT NULL, store_code TEXT DEFAULT '',
    category TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'normal' CHECK (level IN ('normal','important','urgent')),
    title TEXT NOT NULL, body TEXT DEFAULT '', ref_type TEXT DEFAULT '', ref_no TEXT DEFAULT '', link TEXT DEFAULT '',
    is_todo INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'unread' CHECK (status IN ('unread','read','done')),
    dedup_key TEXT, created_at TEXT NOT NULL, read_at TEXT, done_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notify_dedup ON notifications(user_id, dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notify_user ON notifications(merchant_id, user_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_ref ON notifications(merchant_id, ref_type, ref_no);

CREATE TABLE IF NOT EXISTS number_seq (
    merchant_id INTEGER NOT NULL, doc_type TEXT NOT NULL, scope TEXT NOT NULL, day TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (merchant_id, doc_type, scope, day));
"""

# ---------------------------------------------------------------- v1 -> v2：重建的表（新形态）
REBUILD = [
    ('users', """CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merchant_id INTEGER NOT NULL DEFAULT 1,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, name TEXT,
        role TEXT NOT NULL DEFAULT 'staff',
        role_id INTEGER, store_code TEXT DEFAULT '', emp_no TEXT DEFAULT '', pin_hash TEXT DEFAULT '',
        phone TEXT DEFAULT '', status TEXT DEFAULT '启用', created_at TEXT, last_login TEXT,
        must_change_pw INTEGER NOT NULL DEFAULT 0, token_version INTEGER NOT NULL DEFAULT 0)"""),
    ('stores', """CREATE TABLE stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL DEFAULT 1,
        code TEXT NOT NULL, name TEXT NOT NULL, address TEXT DEFAULT '', phone TEXT DEFAULT '',
        status TEXT DEFAULT '营业', sort INTEGER DEFAULT 0, created_at TEXT,
        region_id INTEGER, store_type TEXT DEFAULT '直营', manager_name TEXT DEFAULT '', open_hours TEXT DEFAULT '',
        UNIQUE (merchant_id, code))"""),
    ('guides', """CREATE TABLE guides (
        id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL DEFAULT 1,
        code TEXT NOT NULL, name TEXT, store_code TEXT, status TEXT DEFAULT '在职',
        UNIQUE (merchant_id, code))"""),
    ('products', """CREATE TABLE products (
        id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL DEFAULT 1,
        code TEXT NOT NULL, name TEXT NOT NULL, short_name TEXT DEFAULT '',
        category TEXT DEFAULT '', brand TEXT DEFAULT '', spec TEXT DEFAULT '', unit TEXT DEFAULT '',
        retail REAL DEFAULT 0, member_price REAL DEFAULT 0, cost REAL DEFAULT 0,
        status TEXT DEFAULT '在售', created_at TEXT, updated_at TEXT,
        spu_code TEXT DEFAULT '', pinyin TEXT DEFAULT '', category_id INTEGER, brand_id INTEGER, supplier_id INTEGER,
        base_unit TEXT DEFAULT '',
        batch_managed INTEGER NOT NULL DEFAULT 0, shelf_life_days INTEGER NOT NULL DEFAULT 0, near_expiry_days INTEGER NOT NULL DEFAULT 0,
        allow_discount INTEGER NOT NULL DEFAULT 1, allow_points INTEGER NOT NULL DEFAULT 1,
        combo_mode TEXT NOT NULL DEFAULT 'none',
        UNIQUE (merchant_id, code))"""),
    ('product_barcodes', """CREATE TABLE product_barcodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL DEFAULT 1,
        barcode TEXT NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        unit_id INTEGER,
        UNIQUE (merchant_id, barcode))"""),
    ('members', """CREATE TABLE members (
        id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL DEFAULT 1,
        code TEXT NOT NULL, phone TEXT, name TEXT, level TEXT DEFAULT '普通会员',
        birthday TEXT DEFAULT '', points INTEGER DEFAULT 0, balance REAL DEFAULT 0, total_charged REAL DEFAULT 0,
        reg_time TEXT, reg_store_code TEXT DEFAULT '', reg_store_name TEXT DEFAULT '', guide_code TEXT DEFAULT '', guide_name TEXT DEFAULT '',
        note TEXT DEFAULT '', status TEXT DEFAULT '正常',
        source TEXT DEFAULT '', card_no TEXT DEFAULT '', blacklisted INTEGER NOT NULL DEFAULT 0, merged_into TEXT DEFAULT '',
        UNIQUE (merchant_id, code), UNIQUE (merchant_id, phone))"""),
    ('settings', """CREATE TABLE settings (
        merchant_id INTEGER NOT NULL DEFAULT 1, key TEXT NOT NULL, value TEXT,
        PRIMARY KEY (merchant_id, key))"""),
]

# 其余旧表：只加 merchant_id
ADD_MERCHANT = ['stock', 'stock_moves', 'babies', 'orders', 'order_items', 'payments', 'shifts', 'held_orders',
                'stored_log', 'points_log', 'coupon_templates', 'coupons', 'promotions', 'op_logs']

# 旧表金额列（REAL 元）-> 新增 <列>_cents（INTEGER 分）+ 双向同步触发器
MONEY_COLS = {
    'products': ['retail', 'member_price', 'cost'],
    'members': ['balance', 'total_charged'],
    'orders': ['retail_total', 'discount_total', 'order_discount', 'coupon_amount', 'points_amount', 'round_off',
               'payable', 'paid_total', 'change_amount'],
    'order_items': ['retail', 'price', 'line_amount', 'share_amount', 'amount', 'cost'],
    'payments': ['amount'],
    'shifts': ['opening_cash', 'counted_cash', 'expected_cash', 'diff'],
    'stored_log': ['change', 'balance'],
    'coupon_templates': ['face_value', 'threshold'],
    'coupons': ['face_value', 'threshold'],
    'promotions': ['promo_price'],
}

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_products_name ON products(merchant_id, name);
CREATE INDEX IF NOT EXISTS idx_products_cat ON products(merchant_id, category);
CREATE INDEX IF NOT EXISTS idx_barcodes_pid ON product_barcodes(product_id);
CREATE INDEX IF NOT EXISTS idx_members_name ON members(merchant_id, name);
CREATE INDEX IF NOT EXISTS idx_users_merchant ON users(merchant_id, role_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_empno ON users(merchant_id, emp_no) WHERE emp_no != '';
CREATE INDEX IF NOT EXISTS idx_stock_m ON stock(merchant_id, store_code);
CREATE INDEX IF NOT EXISTS idx_moves_m ON stock_moves(merchant_id, store_code, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_m ON orders(merchant_id, store_code, created_at);
CREATE INDEX IF NOT EXISTS idx_items_m ON order_items(merchant_id, order_no);
CREATE INDEX IF NOT EXISTS idx_pay_m ON payments(merchant_id, store_code, created_at);
CREATE INDEX IF NOT EXISTS idx_shifts_m ON shifts(merchant_id, store_code, open_time);
CREATE INDEX IF NOT EXISTS idx_coupons_m ON coupons(merchant_id, member_code, status);
CREATE INDEX IF NOT EXISTS idx_oplogs_m ON op_logs(merchant_id, created_at);
"""


def _cents_expr(v):
    return 'CAST(ROUND(%s * 100) AS INTEGER)' % v


def money_trigger_sql(table, cols):
    """生成 REAL 元 <-> INTEGER 分 的同步触发器（WHEN 守卫保证不会互相无限触发）。

    - INSERT：给了 *_cents 就以分为准回填元；没给就由元算分。
    - UPDATE 只改了元：重算分；只改了分：重算元；同一语句两个都改：信任调用方（不同步）。
    """
    out = []
    sets = []
    for c in cols:
        cc = c + '_cents'
        sets.append('%s = CASE WHEN NEW.%s IS NOT NULL THEN NEW.%s ELSE %s END' % (cc, cc, cc, _cents_expr('NEW."%s"' % c)))
        sets.append('"%s" = CASE WHEN NEW.%s IS NOT NULL THEN NEW.%s / 100.0 ELSE NEW."%s" END' % (c, cc, cc, c))
    out.append('CREATE TRIGGER IF NOT EXISTS trg_%s_money_ins AFTER INSERT ON %s BEGIN UPDATE %s SET %s WHERE rowid = NEW.rowid; END'
               % (table, table, table, ', '.join(sets)))
    for c in cols:
        cc = c + '_cents'
        out.append('CREATE TRIGGER IF NOT EXISTS trg_%s_%s_y2c AFTER UPDATE OF "%s" ON %s '
                   'WHEN NEW."%s" IS NOT OLD."%s" AND NEW.%s IS OLD.%s '
                   'BEGIN UPDATE %s SET %s = %s WHERE rowid = NEW.rowid; END'
                   % (table, c, c, table, c, c, cc, cc, table, cc, _cents_expr('NEW."%s"' % c)))
        out.append('CREATE TRIGGER IF NOT EXISTS trg_%s_%s_c2y AFTER UPDATE OF %s ON %s '
                   'WHEN NEW.%s IS NOT OLD.%s AND NEW."%s" IS OLD."%s" '
                   'BEGIN UPDATE %s SET "%s" = NEW.%s / 100.0 WHERE rowid = NEW.rowid; END'
                   % (table, c, cc, table, cc, cc, c, c, table, c, cc))
    return out


def _rebuild(db, table, ddl):
    """SQLite 官方推荐的 12 步重建简化版（调用方已关外键、在事务里）。保留全部列和 id。"""
    from . import columns
    old = columns(db, table)
    db.execute(ddl.replace('CREATE TABLE %s (' % table, 'CREATE TABLE _new_%s (' % table, 1))
    new = columns(db, '_new_' + table)
    common = [c for c in old if c in new]
    db.execute('INSERT INTO _new_%s(%s) SELECT %s FROM %s' % (table, ','.join(common), ','.join(common), table))
    db.execute('DROP TABLE %s' % table)
    db.execute('ALTER TABLE _new_%s RENAME TO %s' % (table, table))


def migrate(db, from_version):
    from . import add_column, columns, run_script
    from ..util import now_s
    ts = now_s()
    db.execute("INSERT OR IGNORE INTO merchants(id, code, name, status, created_at, updated_at) "
               "VALUES(1, 'DEMO', '萌贝母婴连锁', 'active', ?, ?)", (ts, ts))
    if from_version < 2:
        for table, ddl in REBUILD:
            if 'merchant_id' not in columns(db, table):
                _rebuild(db, table, ddl)
        for t in ADD_MERCHANT:
            add_column(db, t, 'merchant_id', 'INTEGER NOT NULL DEFAULT 1')
        for t, cols in MONEY_COLS.items():
            for c in cols:
                if add_column(db, t, c + '_cents', 'INTEGER'):
                    db.execute('UPDATE %s SET %s_cents = %s WHERE "%s" IS NOT NULL' % (t, c, _cents_expr('"%s"' % c), c))
    for t, cols in MONEY_COLS.items():
        for sql in money_trigger_sql(t, cols):
            db.execute(sql)
    run_script(db, INDEXES)
