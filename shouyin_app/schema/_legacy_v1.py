# -*- coding: utf-8 -*-
"""v1（2026-11-05 13:50 上线版）原样表结构。只读快照，不要改。"""
LEGACY_V1 = """
CREATE TABLE IF NOT EXISTS stores (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT DEFAULT '', phone TEXT DEFAULT '',
    status TEXT DEFAULT '营业', sort INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
    name TEXT, role TEXT NOT NULL CHECK (role IN ('admin','manager','cashier')),
    store_code TEXT, phone TEXT DEFAULT '', status TEXT DEFAULT '启用', created_at TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS guides (
    code TEXT PRIMARY KEY, name TEXT, store_code TEXT, status TEXT DEFAULT '在职');
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, short_name TEXT DEFAULT '',
    category TEXT DEFAULT '', brand TEXT DEFAULT '', spec TEXT DEFAULT '', unit TEXT DEFAULT '',
    retail REAL DEFAULT 0, member_price REAL DEFAULT 0, cost REAL DEFAULT 0,
    status TEXT DEFAULT '在售', created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_products_cat ON products(category);
CREATE TABLE IF NOT EXISTS product_barcodes (
    barcode TEXT PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_barcodes_pid ON product_barcodes(product_id);
CREATE TABLE IF NOT EXISTS stock (
    product_id INTEGER NOT NULL, store_code TEXT NOT NULL, qty REAL DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (product_id, store_code));
CREATE TABLE IF NOT EXISTS stock_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, store_code TEXT, change REAL, balance REAL,
    type TEXT, ref_no TEXT, batch_no TEXT DEFAULT '', expiry_date TEXT DEFAULT '', operator TEXT, note TEXT DEFAULT '', created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_moves_pid ON stock_moves(product_id, store_code);
CREATE INDEX IF NOT EXISTS idx_moves_exp ON stock_moves(expiry_date);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, phone TEXT UNIQUE, name TEXT, level TEXT DEFAULT '普通会员',
    birthday TEXT DEFAULT '', points INTEGER DEFAULT 0, balance REAL DEFAULT 0, total_charged REAL DEFAULT 0,
    reg_time TEXT, reg_store_code TEXT DEFAULT '', reg_store_name TEXT DEFAULT '', guide_code TEXT DEFAULT '', guide_name TEXT DEFAULT '',
    note TEXT DEFAULT '', status TEXT DEFAULT '正常');
CREATE INDEX IF NOT EXISTS idx_members_name ON members(name);
CREATE TABLE IF NOT EXISTS babies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    name TEXT DEFAULT '', gender TEXT DEFAULT '', birthday TEXT DEFAULT '', note TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_babies_member ON babies(member_id);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT UNIQUE NOT NULL, client_id TEXT, biz_type TEXT NOT NULL,
    store_code TEXT, store_name TEXT, terminal_no TEXT, cashier_user TEXT, cashier_name TEXT, guide_code TEXT, guide_name TEXT,
    member_code TEXT, member_phone TEXT, member_name TEXT, member_level TEXT,
    item_qty REAL, retail_total REAL, discount_total REAL, order_discount REAL,
    coupon_no TEXT, coupon_amount REAL, points_used INTEGER, points_amount REAL,
    round_off REAL, payable REAL, paid_total REAL, change_amount REAL, points_earned INTEGER,
    status TEXT, orig_order_no TEXT, refund_reason TEXT, remark TEXT, auth_user TEXT, shift_id INTEGER, created_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client ON orders(client_id);
CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_store ON orders(store_code, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_member ON orders(member_code);
CREATE INDEX IF NOT EXISTS idx_orders_shift ON orders(shift_id);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT NOT NULL, line_no INTEGER, product_id INTEGER, product_code TEXT,
    barcode TEXT, name TEXT, spec TEXT, unit TEXT, brand TEXT, category TEXT,
    qty REAL, retail REAL, price REAL, line_amount REAL, share_amount REAL, amount REAL, cost REAL,
    refunded_qty REAL DEFAULT 0, orig_line_id INTEGER);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_no);
CREATE INDEX IF NOT EXISTS idx_items_product ON order_items(product_code);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT, method TEXT, method_name TEXT, amount REAL, ref TEXT,
    shift_id INTEGER, store_code TEXT, kind TEXT DEFAULT 'sale', created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_pay_order ON payments(order_no);
CREATE INDEX IF NOT EXISTS idx_pay_shift ON payments(shift_id);
CREATE INDEX IF NOT EXISTS idx_pay_time ON payments(created_at);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, store_code TEXT, store_name TEXT, terminal_no TEXT, cashier_user TEXT, cashier_name TEXT,
    open_time TEXT, close_time TEXT, opening_cash REAL, counted_cash REAL, expected_cash REAL, diff REAL, summary TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS held_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, store_code TEXT, cashier_user TEXT, title TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS stored_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_code TEXT, member_name TEXT, change REAL, balance REAL, type TEXT,
    order_no TEXT, store_code TEXT, operator TEXT, note TEXT DEFAULT '', created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_stored_member ON stored_log(member_code);
CREATE TABLE IF NOT EXISTS points_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_code TEXT, member_name TEXT, change INTEGER, balance INTEGER, type TEXT,
    order_no TEXT, operator TEXT, note TEXT DEFAULT '', created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_points_member ON points_log(member_code);
CREATE TABLE IF NOT EXISTS coupon_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, face_value REAL NOT NULL, threshold REAL DEFAULT 0,
    valid_days INTEGER DEFAULT 30, status TEXT DEFAULT '启用', created_at TEXT);
CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, coupon_no TEXT UNIQUE NOT NULL, template_id INTEGER, template_name TEXT,
    member_code TEXT, face_value REAL, threshold REAL, expire_date TEXT DEFAULT '', status TEXT DEFAULT '未使用',
    order_no TEXT DEFAULT '', used_at TEXT DEFAULT '', store_code TEXT DEFAULT '', created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_coupons_member ON coupons(member_code, status);
CREATE TABLE IF NOT EXISTS promotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, product_code TEXT NOT NULL, promo_price REAL NOT NULL,
    start_date TEXT, end_date TEXT, store_code TEXT DEFAULT '', status TEXT DEFAULT '启用', created_at TEXT);
CREATE TABLE IF NOT EXISTS op_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, action TEXT, module TEXT, detail TEXT, ip TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, ip TEXT, ok INTEGER, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_attempts ON login_attempts(username, created_at);
"""
