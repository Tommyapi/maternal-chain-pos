# -*- coding: utf-8 -*-
"""收银台测试用的造数工具（直接写测试库，不依赖其他模块的后台接口，避免它们改版后连带失败）。本文件不含测试用例。"""
import json
from datetime import datetime, timedelta

from conftest import Client


def set_config(dbc, mid=1, **kv):
    r = dbc.execute("SELECT value FROM settings WHERE merchant_id=? AND key='config'", (mid,)).fetchone()
    cfg = json.loads(r[0]) if r else {}
    cfg.update(kv)
    dbc.execute("INSERT OR REPLACE INTO settings(merchant_id, key, value) VALUES(?, 'config', ?)", (mid, json.dumps(cfg, ensure_ascii=False)))
    dbc.commit()


def add_coupon(dbc, member, face, threshold=0, no=None, mid=1):
    no = no or 'CPT%s%d' % (member, face)
    dbc.execute("INSERT INTO coupons(merchant_id, coupon_no, template_name, member_code, face_value, threshold, expire_date, status, created_at) "
                "VALUES(?,?,?,?,?,?,?, '未使用', ?)", (mid, no, '测试券', member, face, threshold, '2999-12-31', '2026-01-01 00:00:00'))
    dbc.commit()
    return no


def day(n):
    return (datetime.now() + timedelta(days=n)).strftime('%Y-%m-%d')


def add_product(dbc, code, name, retail, cost=0, member_price=None, unit='罐', barcodes=(), batch_managed=0, mid=1, **extra):
    cols = dict(merchant_id=mid, code=code, name=name, unit=unit, base_unit=unit, retail=retail, member_price=member_price or retail, cost=cost,
                status='在售', batch_managed=batch_managed)
    cols.update(extra)
    dbc.execute('INSERT INTO products(%s) VALUES(%s)' % (','.join(cols), ','.join('?' * len(cols))), list(cols.values()))
    pid = dbc.execute('SELECT id FROM products WHERE merchant_id=? AND code=?', (mid, code)).fetchone()[0]
    for b in barcodes:
        dbc.execute('INSERT INTO product_barcodes(merchant_id, barcode, product_id) VALUES(?,?,?)', (mid, b, pid))
    dbc.commit()
    return pid


def add_unit(dbc, pid, unit, rate, barcode=None, retail_cents=None, mid=1):
    dbc.execute('INSERT INTO product_units(merchant_id, product_id, unit, rate, retail_cents) VALUES(?,?,?,?,?)', (mid, pid, unit, rate, retail_cents))
    uid = dbc.execute('SELECT id FROM product_units WHERE product_id=? AND unit=?', (pid, unit)).fetchone()[0]
    if barcode:
        dbc.execute('INSERT INTO product_barcodes(merchant_id, barcode, product_id, unit_id) VALUES(?,?,?,?)', (mid, barcode, pid, uid))
    dbc.commit()
    return uid


def stock_in(dbc, pid, wh, qty, batch_no=None, expiry=None, mid=1, ref='GRTEST'):
    from shouyin_app import inventory
    dbc.execute('BEGIN IMMEDIATE')
    line = {'product_id': pid, 'base_qty': qty}
    if batch_no:
        line.update(batch_no=batch_no, expiry_date=expiry or '')
    res = inventory.stock_in(dbc, {'username': 'test'}, mid, wh, [line], 'purchase', ref)
    dbc.execute('COMMIT')
    return res[0][0]['batch_id']


def qty(dbc, code, wh='A001', quality='ok', mid=1):
    r = dbc.execute('SELECT COALESCE(SUM(s.qty),0) FROM inv_stock s JOIN products p ON p.id=s.product_id WHERE p.merchant_id=? AND p.code=? '
                    'AND s.wh_code=? AND s.quality=?', (mid, code, wh, quality)).fetchone()
    return r[0]


def legacy_stock(dbc, code, wh='A001'):
    r = dbc.execute('SELECT s.qty FROM stock s JOIN products p ON p.id=s.product_id WHERE p.code=? AND p.merchant_id=1 AND s.store_code=?',
                    (code, wh)).fetchone()
    return r[0] if r else None


def member(dbc, code, mid=1):
    return dbc.execute('SELECT points, balance_cents FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone()


def set_pin(dbc, username, emp_no, pin):
    from werkzeug.security import generate_password_hash
    dbc.execute('UPDATE users SET emp_no=?, pin_hash=? WHERE username=?', (emp_no, generate_password_hash(pin), username))
    dbc.commit()


def body(items, payments, **extra):
    d = {'items': items, 'payments': payments}
    d.update(extra)
    return d


def m2cashier_client(app, dbc):
    """商户 2 的收银员（带开班）。用于跨商户越权测试。"""
    stock_in(dbc, dbc.execute("SELECT id FROM products WHERE merchant_id=2 AND code='Q1'").fetchone()[0], 'Z001', 5, mid=2, ref='GRM2')
    c = Client(app, 'm2cashier')
    return c
