# -*- coding: utf-8 -*-
"""pos：收银台 / 班次 / 现金收支 / 销售单 / 退换货 / 支付流水 / 优惠明细 / 打印。Owner：模块③。
旧表 orders / order_items / payments / shifts / held_orders 也归③。

销售单与退货单都在 orders（biz_type = sale / refund / exchange），各自独立单号；
退款金额以 order_items 的分摊结果为准；支付/退款流水在 payments（kind = sale / refund / recharge）。
"""
MODULE = 'pos'
VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS terminals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, store_code TEXT NOT NULL, terminal_no TEXT NOT NULL,
    name TEXT DEFAULT '', printer TEXT DEFAULT '', paper_width INTEGER NOT NULL DEFAULT 58, cash_drawer INTEGER NOT NULL DEFAULT 1,
    customer_display INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '启用', created_at TEXT,
    UNIQUE (merchant_id, terminal_no));

-- 现金收支（POS-003）：非销售现金收入/支出，计入班次现金
CREATE TABLE IF NOT EXISTS cash_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cm_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, store_code TEXT NOT NULL,
    shift_id INTEGER NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('in','out')), amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason TEXT NOT NULL, approval_no TEXT DEFAULT '', operator TEXT, client_id TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cash_moves_shift ON cash_moves(shift_id);

-- 优惠明细（9.3：改价、活动、券、积分、抹零分别记录）
CREATE TABLE IF NOT EXISTS order_discounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, order_no TEXT NOT NULL, line_no INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL CHECK (kind IN ('member_price','price_change','item_discount','activity','order_discount','coupon','points','round_off','gift')),
    source_no TEXT DEFAULT '', source_name TEXT DEFAULT '', amount_cents INTEGER NOT NULL, auth_user TEXT DEFAULT '', approval_no TEXT DEFAULT '',
    detail TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_order_disc ON order_discounts(merchant_id, order_no);

-- 打印记录（补打标记“重打”）
CREATE TABLE IF NOT EXISTS print_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, doc_type TEXT NOT NULL, doc_no TEXT NOT NULL,
    reprint INTEGER NOT NULL DEFAULT 0, operator TEXT, terminal_no TEXT DEFAULT '', result TEXT DEFAULT 'sent', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_print_doc ON print_logs(merchant_id, doc_no);

-- 设备检测记录（POS-806）
CREATE TABLE IF NOT EXISTS device_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, store_code TEXT NOT NULL, terminal_no TEXT DEFAULT '',
    device TEXT NOT NULL, model TEXT DEFAULT '', driver TEXT DEFAULT '', result TEXT NOT NULL, detail TEXT DEFAULT '', operator TEXT, created_at TEXT NOT NULL);
"""

EXTRA = [
    # orders：状态/退换关联/快照
    ('orders', 'pay_status', "TEXT NOT NULL DEFAULT 'paid'"),          # unpaid / paying / paid / closed
    ('orders', 'exchange_no', "TEXT DEFAULT ''"),
    ('orders', 'approval_no', "TEXT DEFAULT ''"),
    ('orders', 'no_receipt', 'INTEGER NOT NULL DEFAULT 0'),            # 无单退货
    ('orders', 'reprint_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('orders', 'snapshot', "TEXT DEFAULT ''"),                         # 成交时活动/券/规则快照 JSON
    # order_items：多单位、批次、赠品、质量状态快照
    ('order_items', 'unit_qty', 'REAL'),                               # 交易单位数量（如 1 箱）
    ('order_items', 'unit_rate', 'INTEGER NOT NULL DEFAULT 1'),        # 换算率快照
    ('order_items', 'base_qty', 'INTEGER'),                            # 基本单位数量 = unit_qty * unit_rate
    ('order_items', 'batch_alloc', "TEXT DEFAULT ''"),                 # [{"batch_id":..,"batch_no":..,"expiry_date":..,"qty":..}]
    ('order_items', 'is_gift', 'INTEGER NOT NULL DEFAULT 0'),
    ('order_items', 'promo_ref', "TEXT DEFAULT ''"),
    ('order_items', 'quality', "TEXT DEFAULT ''"),                     # 退货行：ok / defect / pending
    ('order_items', 'line_remark', "TEXT DEFAULT ''"),
    ('order_items', 'tax_cents', 'INTEGER NOT NULL DEFAULT 0'),        # 预留
    # payments：支付流水（9.5）
    ('payments', 'pay_no', 'TEXT'),
    ('payments', 'request_no', 'TEXT'),
    ('payments', 'status', "TEXT NOT NULL DEFAULT 'success'"),         # pending / success / failed / closed
    ('payments', 'confirm_mode', "TEXT NOT NULL DEFAULT 'manual'"),    # manual 收银员确认 / auto 渠道确认
    ('payments', 'channel', "TEXT DEFAULT ''"),
    ('payments', 'channel_mch_id', "TEXT DEFAULT ''"),
    ('payments', 'channel_txn_no', "TEXT DEFAULT ''"),
    ('payments', 'orig_pay_no', "TEXT DEFAULT ''"),
    ('payments', 'paid_at', 'TEXT'),
    ('payments', 'operator', "TEXT DEFAULT ''"),
    # shifts
    ('shifts', 'forced', 'INTEGER NOT NULL DEFAULT 0'),
    ('shifts', 'force_by', "TEXT DEFAULT ''"),
    ('shifts', 'force_reason', "TEXT DEFAULT ''"),
    ('shifts', 'reprint_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('shifts', 'shift_no', 'TEXT'),
    # held_orders
    ('held_orders', 'terminal_no', "TEXT DEFAULT ''"),
    ('held_orders', 'remark', "TEXT DEFAULT ''"),
    # v2（模块③ 2026-11-05）：交班幂等、现金收支授权人/收银台
    ('shifts', 'close_client_id', 'TEXT'),
    ('cash_moves', 'auth_user', "TEXT DEFAULT ''"),
    ('cash_moves', 'terminal_no', "TEXT DEFAULT ''"),
]


def migrate(db, from_version):
    from . import add_column
    for t, c, d in EXTRA:
        add_column(db, t, c, d)
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_no ON payments(pay_no) WHERE pay_no IS NOT NULL')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_req ON payments(merchant_id, request_no) WHERE request_no IS NOT NULL')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_no ON shifts(shift_no) WHERE shift_no IS NOT NULL')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_terminal_open ON shifts(merchant_id, terminal_no) "
               "WHERE status='open' AND terminal_no IS NOT NULL AND terminal_no != ''")
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_orig ON orders(orig_order_no)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_close_cid ON shifts(merchant_id, close_client_id) WHERE close_client_id IS NOT NULL')
    db.execute('CREATE INDEX IF NOT EXISTS idx_items_orig_line ON order_items(orig_line_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_exchange ON orders(exchange_no)')
