# -*- coding: utf-8 -*-
"""goods_stock：商品中心 / 多单位 / 组合装 / 供应商采购 / 仓库 / 批次 / 库存 / 要货调拨 / 盘点 / 报损报溢 / 预警 / 召回。
Owner：模块①。旧表 products / product_barcodes / stock / stock_moves 也归①。

库存模型（docs/ARCH.md §6）：
- inv_stock 是权威余额：(仓, 商品, 批次, 质量状态) 一行，数量为「基本单位」整数。
- 门店仓编码 = 门店编码；旧表 stock(product_id, store_code) 只是「门店合格品账面合计」的兼容视图，由 inventory.post_move 同步维护。
- 只有 inventory.post_move 能改库存，且必须带来源单据（ref_type + ref_no），同时写 stock_moves 流水。
"""
MODULE = 'goods_stock'
VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, parent_id INTEGER, code TEXT NOT NULL, name TEXT NOT NULL,
    near_expiry_days INTEGER NOT NULL DEFAULT 0, safety_stock INTEGER NOT NULL DEFAULT 0, status TEXT DEFAULT '启用', sort INTEGER DEFAULT 0,
    UNIQUE (merchant_id, code));
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, name TEXT NOT NULL, status TEXT DEFAULT '启用',
    UNIQUE (merchant_id, name));

-- 多单位：rate = 1 个该单位 = 多少个基本单位（整数）。销售/采购行保存 unit + rate 快照。
CREATE TABLE IF NOT EXISTS product_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id),
    unit TEXT NOT NULL, rate INTEGER NOT NULL CHECK (rate >= 1),
    for_sale INTEGER NOT NULL DEFAULT 1, for_purchase INTEGER NOT NULL DEFAULT 1,
    retail_cents INTEGER, member_price_cents INTEGER, status TEXT DEFAULT '启用',
    UNIQUE (product_id, unit));

-- 组合装 / 礼盒（BOM）：combo_mode=components 时卖组合装扣组件库存
CREATE TABLE IF NOT EXISTS product_bom (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
    component_id INTEGER NOT NULL, qty INTEGER NOT NULL CHECK (qty >= 1),
    UNIQUE (product_id, component_id));

-- 铺货 / 门店停售（ERP-005）。没有记录 = 跟随总部
CREATE TABLE IF NOT EXISTS product_listings (
    merchant_id INTEGER NOT NULL, store_code TEXT NOT NULL, product_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '在售', updated_at TEXT,
    PRIMARY KEY (merchant_id, store_code, product_id));

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
    contact TEXT DEFAULT '', phone TEXT DEFAULT '', address TEXT DEFAULT '', settle_info TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT '启用', created_at TEXT,
    UNIQUE (merchant_id, code));
CREATE TABLE IF NOT EXISTS supplier_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
    unit TEXT DEFAULT '', rate INTEGER NOT NULL DEFAULT 1, price_cents INTEGER NOT NULL, src_no TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_supplier_prices ON supplier_prices(merchant_id, product_id, created_at);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'store' CHECK (kind IN ('hq','store','defect')),
    store_code TEXT DEFAULT '', sellable INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '启用',
    UNIQUE (merchant_id, code));

-- 采购：PO（采购单）-> GR（收货入库单，可多次）-> PR（采购退货）
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, po_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL,
    supplier_id INTEGER NOT NULL, wh_code TEXT NOT NULL, expect_date TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','partial','received','closed','cancelled')),
    total_cents INTEGER NOT NULL DEFAULT 0, remark TEXT DEFAULT '',
    created_by TEXT, created_at TEXT NOT NULL, approved_by TEXT DEFAULT '', approved_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_po_m ON purchase_orders(merchant_id, status, created_at);
CREATE TABLE IF NOT EXISTS purchase_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, po_no TEXT NOT NULL, line_no INTEGER NOT NULL,
    product_id INTEGER NOT NULL, unit TEXT NOT NULL, rate INTEGER NOT NULL DEFAULT 1,
    qty INTEGER NOT NULL, price_cents INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
    received_base_qty INTEGER NOT NULL DEFAULT 0, rejected_base_qty INTEGER NOT NULL DEFAULT 0, remark TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_pl_po ON purchase_lines(po_no);
CREATE TABLE IF NOT EXISTS goods_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, gr_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, po_no TEXT DEFAULT '',
    supplier_id INTEGER, wh_code TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'purchase' CHECK (kind IN ('purchase','opening','other')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted','cancelled')),
    total_cents INTEGER NOT NULL DEFAULT 0, remark TEXT DEFAULT '', created_by TEXT, created_at TEXT NOT NULL, posted_at TEXT);
CREATE TABLE IF NOT EXISTS goods_receipt_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, gr_no TEXT NOT NULL, po_line_id INTEGER,
    product_id INTEGER NOT NULL, unit TEXT NOT NULL, rate INTEGER NOT NULL DEFAULT 1, qty INTEGER NOT NULL, rejected_qty INTEGER NOT NULL DEFAULT 0,
    base_qty INTEGER NOT NULL, price_cents INTEGER NOT NULL DEFAULT 0, batch_id INTEGER NOT NULL DEFAULT 0,
    batch_no TEXT DEFAULT '', prod_date TEXT DEFAULT '', expiry_date TEXT DEFAULT '', diff_reason TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_grl_gr ON goods_receipt_lines(gr_no);
CREATE TABLE IF NOT EXISTS purchase_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pr_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, gr_no TEXT DEFAULT '', po_no TEXT DEFAULT '',
    supplier_id INTEGER NOT NULL, wh_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','posted','cancelled')),
    total_cents INTEGER NOT NULL DEFAULT 0, reason TEXT DEFAULT '', created_by TEXT, created_at TEXT NOT NULL, posted_at TEXT);
CREATE TABLE IF NOT EXISTS purchase_return_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, pr_no TEXT NOT NULL, product_id INTEGER NOT NULL,
    unit TEXT NOT NULL, rate INTEGER NOT NULL DEFAULT 1, qty INTEGER NOT NULL, base_qty INTEGER NOT NULL,
    price_cents INTEGER NOT NULL DEFAULT 0, batch_id INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'ok');

-- 批次：batch_id=0 表示「无批次」（非批次商品）
CREATE TABLE IF NOT EXISTS inv_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
    batch_no TEXT NOT NULL, prod_date TEXT DEFAULT '', expiry_date TEXT DEFAULT '', supplier_id INTEGER, src_no TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'normal' CHECK (status IN ('normal','locked','recalled')),
    lock_reason TEXT DEFAULT '', created_at TEXT NOT NULL,
    UNIQUE (merchant_id, product_id, batch_no, expiry_date));
CREATE INDEX IF NOT EXISTS idx_batches_exp ON inv_batches(merchant_id, expiry_date);

-- 权威库存余额（基本单位整数）
CREATE TABLE IF NOT EXISTS inv_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, wh_code TEXT NOT NULL, product_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'ok' CHECK (quality IN ('ok','defect','pending')),
    qty INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
    UNIQUE (merchant_id, wh_code, product_id, batch_id, quality));
CREATE INDEX IF NOT EXISTS idx_inv_stock_p ON inv_stock(merchant_id, product_id);

-- 要货 / 调拨：kind=request 门店要货（审核后生成 transfer）；kind=transfer 调拨（出库 -> 在途 -> 收货）
CREATE TABLE IF NOT EXISTS transfer_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tf_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('request','transfer')), src_no TEXT DEFAULT '',
    from_wh TEXT NOT NULL, to_wh TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','shipped','partial','received','closed','rejected','cancelled')),
    remark TEXT DEFAULT '', created_by TEXT, created_at TEXT NOT NULL, approved_by TEXT DEFAULT '', approved_at TEXT,
    shipped_by TEXT DEFAULT '', shipped_at TEXT, received_by TEXT DEFAULT '', received_at TEXT);
CREATE INDEX IF NOT EXISTS idx_tf_m ON transfer_orders(merchant_id, status, created_at);
CREATE TABLE IF NOT EXISTS transfer_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, tf_no TEXT NOT NULL, product_id INTEGER NOT NULL,
    unit TEXT NOT NULL, rate INTEGER NOT NULL DEFAULT 1, req_qty INTEGER NOT NULL DEFAULT 0,
    batch_id INTEGER NOT NULL DEFAULT 0, out_base_qty INTEGER NOT NULL DEFAULT 0, in_base_qty INTEGER NOT NULL DEFAULT 0,
    diff_base_qty INTEGER NOT NULL DEFAULT 0, diff_reason TEXT DEFAULT '', diff_settle TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_tfl_no ON transfer_lines(tf_no);

-- 盘点
CREATE TABLE IF NOT EXISTS stocktakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, st_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, wh_code TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'full' CHECK (scope IN ('full','category','spot','dynamic')), scope_value TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'counting' CHECK (status IN ('counting','submitted','approved','rejected','cancelled')),
    snapshot_at TEXT NOT NULL, created_by TEXT, created_at TEXT NOT NULL, approved_by TEXT DEFAULT '', approved_at TEXT, remark TEXT DEFAULT '',
    snapshot_move_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stocktake_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, st_no TEXT NOT NULL, product_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL DEFAULT 0, book_qty INTEGER NOT NULL DEFAULT 0, counted_qty INTEGER, moved_qty INTEGER NOT NULL DEFAULT 0,
    diff_qty INTEGER NOT NULL DEFAULT 0, counted_by TEXT DEFAULT '', counted_at TEXT, counted_move_id INTEGER NOT NULL DEFAULT 0,
    UNIQUE (st_no, product_id, batch_id));

-- 报损 / 报溢 / 库存调整 / 拆零组装 / 质量状态转移
CREATE TABLE IF NOT EXISTS stock_adjusts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, adj_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, wh_code TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('loss','overflow','adjust','split','assemble','quality')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','posted','rejected','cancelled')),
    reason TEXT DEFAULT '', attachment TEXT DEFAULT '', approval_no TEXT DEFAULT '', total_cents INTEGER NOT NULL DEFAULT 0,
    created_by TEXT, created_at TEXT NOT NULL, posted_at TEXT);
CREATE TABLE IF NOT EXISTS stock_adjust_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, adj_no TEXT NOT NULL, product_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'ok', to_quality TEXT DEFAULT '',
    base_qty INTEGER NOT NULL, cost_cents INTEGER NOT NULL DEFAULT 0, reason TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS stock_alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('merchant','category','product')), target_id INTEGER NOT NULL DEFAULT 0,
    safety_qty INTEGER, near_expiry_days TEXT DEFAULT '', slow_days INTEGER, receivers TEXT DEFAULT '',
    UNIQUE (merchant_id, target_type, target_id));

-- 召回（BABY-001~004）
CREATE TABLE IF NOT EXISTS recall_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, rc_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, title TEXT NOT NULL,
    criteria TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    created_by TEXT, created_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS recall_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, rc_no TEXT NOT NULL, batch_id INTEGER,
    action TEXT NOT NULL, detail TEXT DEFAULT '', ref_no TEXT DEFAULT '', operator TEXT, created_at TEXT NOT NULL);
"""

# v2（模块①）：旧表/已建表补列（新库由 SCHEMA 直接建出；旧库在 migrate 里幂等补）
ADD_COLS = [
    ('products', 'image', "TEXT DEFAULT ''"), ('products', 'spec_attrs', "TEXT DEFAULT ''"),
    ('stocktakes', 'snapshot_move_id', 'INTEGER NOT NULL DEFAULT 0'),
    ('stocktake_lines', 'counted_move_id', 'INTEGER NOT NULL DEFAULT 0'),
    ('transfer_lines', 'diff_settle', "TEXT DEFAULT ''"),
    ('stock_adjust_lines', 'reason', "TEXT DEFAULT ''"),
]

# stock_moves 补列（v1 已有 product_id, store_code, change, balance, type, ref_no, batch_no, expiry_date, operator, note, created_at）
MOVE_COLS = [
    ('wh_code', "TEXT DEFAULT ''"), ('batch_id', 'INTEGER NOT NULL DEFAULT 0'), ('quality', "TEXT NOT NULL DEFAULT 'ok'"),
    ('ref_type', "TEXT DEFAULT ''"), ('qty_before', 'INTEGER'), ('qty_after', 'INTEGER'),
    ('unit', "TEXT DEFAULT ''"), ('unit_qty', 'INTEGER'), ('rate', 'INTEGER'), ('cost_cents', 'INTEGER'),
]


def migrate(db, from_version):
    from . import add_column
    from ..util import now_s
    for col, decl in MOVE_COLS:
        add_column(db, 'stock_moves', col, decl)
    db.execute("UPDATE stock_moves SET wh_code=store_code WHERE wh_code='' OR wh_code IS NULL")
    db.execute('CREATE INDEX IF NOT EXISTS idx_moves_ref ON stock_moves(merchant_id, ref_no)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_moves_batch ON stock_moves(merchant_id, batch_id)')
    # 每个门店一个门店仓（编码 = 门店编码）
    db.execute("INSERT OR IGNORE INTO warehouses(merchant_id, code, name, kind, store_code, sellable) "
               "SELECT merchant_id, code, name, 'store', code, 1 FROM stores")
    # v1 旧库存 -> inv_stock（无批次、合格品）。只在 inv_stock 还没有该仓+商品记录时搬一次。
    ts = now_s()
    db.execute("INSERT OR IGNORE INTO inv_stock(merchant_id, wh_code, product_id, batch_id, quality, qty, updated_at) "
               "SELECT s.merchant_id, s.store_code, s.product_id, 0, 'ok', CAST(ROUND(s.qty) AS INTEGER), ? FROM stock s "
               "WHERE NOT EXISTS (SELECT 1 FROM inv_stock i WHERE i.merchant_id=s.merchant_id AND i.wh_code=s.store_code AND i.product_id=s.product_id)",
               (ts,))
    db.execute("UPDATE products SET base_unit=unit WHERE base_unit='' OR base_unit IS NULL")
    # v2：补列 + 拼音首字母（收银/后台按拼音搜索）
    for table, col, decl in ADD_COLS:
        add_column(db, table, col, decl)
    db.execute('CREATE INDEX IF NOT EXISTS idx_moves_wh ON stock_moves(merchant_id, wh_code, product_id, id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_grl_batch ON goods_receipt_lines(merchant_id, batch_id)')
    from ..goods_svc import pinyin_initials
    for r in db.execute("SELECT id, name FROM products WHERE pinyin='' OR pinyin IS NULL").fetchall():
        db.execute('UPDATE products SET pinyin=? WHERE id=?', (pinyin_initials(r[1]), r[0]))
