# -*- coding: utf-8 -*-
"""v1 生产库 -> v2 迁移：数据不丢、全部归 1 号商户、金额分列一致、库存搬进 inv_stock、可重复执行、迁移后旧代码照常收银。"""
import json
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

from shouyin_app.schema._legacy_v1 import LEGACY_V1
from shouyin_app.db import connect, init_db, schema_versions
from shouyin_app.schema import MODULES


def make_v1(path):
    """按 v1 结构造一个“生产库”（含已有订单/流水，比当前生产更苛刻）。"""
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript(LEGACY_V1)
    ts = '2026-11-05 10:00:00'
    db.execute("INSERT INTO stores(code,name,status,sort,created_at) VALUES('8007','新妇幼店','营业',0,?)", (ts,))
    db.execute("INSERT INTO stores(code,name,status,sort,created_at) VALUES('3000','总店','营业',1,?)", (ts,))
    for u, role, st in (('admin', 'admin', ''), ('dianzhang', 'manager', '8007'), ('shouyin01', 'cashier', '8007')):
        db.execute('INSERT INTO users(username,password_hash,name,role,store_code,status,created_at) VALUES(?,?,?,?,?,?,?)',
                   (u, generate_password_hash('123456'), u, role, st, '启用', ts))
    db.execute("INSERT INTO products(code,name,unit,retail,member_price,cost,status,created_at,updated_at) VALUES('P1','奶粉','罐',268.8,258,199.99,'在售',?,?)", (ts, ts))
    db.execute("INSERT INTO products(code,name,unit,retail,member_price,cost,status,created_at,updated_at) VALUES('P2','湿巾','包',0,0,0,'在售',?,?)", (ts, ts))
    db.execute("INSERT INTO product_barcodes(barcode,product_id) VALUES('6900000000011',1),('6900000000012',1),('6900000000031',2)")
    db.execute("INSERT INTO stock(product_id,store_code,qty,updated_at) VALUES(1,'8007',12,?),(2,'8007',5,?),(1,'3000',3,?)", (ts, ts, ts))
    db.execute("INSERT INTO stock_moves(product_id,store_code,change,balance,type,ref_no,operator,created_at) VALUES(1,'8007',12,12,'期初','INIT','seed',?)", (ts,))
    db.execute("INSERT INTO members(code,phone,name,level,points,balance,total_charged,reg_time) VALUES('XB00000001','13800000001','张妈妈','普通会员',120,88.8,100,?)", (ts,))
    db.execute("INSERT INTO members(code,phone,name,reg_time) VALUES('XB00000002',NULL,'无手机',?)", (ts,))
    db.execute("INSERT INTO babies(member_id,name,birthday) VALUES(1,'宝宝','2025-05-01')")
    db.execute("INSERT INTO guides(code,name,store_code) VALUES('G1','小王','8007')")
    db.execute("INSERT INTO orders(order_no,biz_type,store_code,payable,paid_total,retail_total,discount_total,status,created_at,shift_id) "
               "VALUES('S2611058007100000001','sale','8007',268.8,300,268.8,0,'已完成',?,1)", (ts,))
    db.execute("INSERT INTO order_items(order_no,line_no,product_id,product_code,qty,retail,price,line_amount,share_amount,amount,cost) "
               "VALUES('S2611058007100000001',1,1,'P1',1,268.8,268.8,268.8,0,268.8,199.99)")
    db.execute("INSERT INTO payments(order_no,method,method_name,amount,shift_id,store_code,kind,created_at) VALUES('S2611058007100000001','cash','现金',268.8,1,'8007','sale',?)", (ts,))
    db.execute("INSERT INTO shifts(store_code,cashier_user,open_time,opening_cash,status) VALUES('8007','shouyin01',?,100,'open')", (ts,))
    db.execute("INSERT INTO settings(key,value) VALUES('config', ?)", (json.dumps({'round_mode': 'jiao'}),))
    db.execute("INSERT INTO coupons(coupon_no,member_code,face_value,threshold,status,created_at) VALUES('C1','XB00000001',20,100,'未使用',?)", (ts,))
    db.close()


@pytest.fixture()
def v1db(tmp_path):
    p = str(tmp_path / 'prod.db')
    make_v1(p)
    return p


def test_migrate_v1_preserves_everything(v1db):
    before = sqlite3.connect(v1db)
    cnt = lambda c, t: c.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
    tables = ['stores', 'users', 'products', 'product_barcodes', 'stock', 'stock_moves', 'members', 'babies', 'guides', 'orders',
              'order_items', 'payments', 'shifts', 'coupons', 'settings']
    n0 = dict((t, cnt(before, t)) for t in tables)
    before.close()
    done = init_db(v1db)
    assert set(done) == set(m.MODULE for m in MODULES)
    db = connect(v1db)
    assert dict((t, cnt(db, t)) for t in tables) == n0
    assert schema_versions(db) == dict((m.MODULE, m.VERSION) for m in MODULES)
    assert db.execute("SELECT name, status FROM merchants WHERE id=1").fetchone()[0] == '萌贝母婴连锁'
    for t in tables:
        assert db.execute('SELECT COUNT(*) FROM %s WHERE merchant_id != 1' % t).fetchone()[0] == 0, t
    p = db.execute("SELECT * FROM products WHERE code='P1'").fetchone()
    assert (p['retail_cents'], p['member_price_cents'], p['cost_cents'], p['id'], p['base_unit']) == (26880, 25800, 19999, 1, '罐')
    m = db.execute("SELECT * FROM members WHERE code='XB00000001'").fetchone()
    assert m['balance_cents'] == 8880 and m['points'] == 120 and m['id'] == 1
    assert db.execute("SELECT payable_cents, paid_total_cents FROM orders").fetchone()[:] == (26880, 30000)
    assert db.execute("SELECT amount_cents FROM payments").fetchone()[0] == 26880
    # 账号映射到默认角色
    roles = dict(db.execute('SELECT u.username, r.code FROM users u JOIN roles r ON r.id=u.role_id').fetchall())
    assert roles == {'admin': 'merchant_admin', 'dianzhang': 'store_manager', 'shouyin01': 'cashier'}
    # 库存搬进 inv_stock，门店仓已建
    inv = dict(((r[0], r[1]), r[2]) for r in db.execute("SELECT wh_code, product_id, qty FROM inv_stock WHERE quality='ok' AND batch_id=0"))
    assert inv == {('8007', 1): 12, ('8007', 2): 5, ('3000', 1): 3}
    assert db.execute("SELECT COUNT(*) FROM warehouses WHERE kind='store'").fetchone()[0] == 2
    assert json.loads(db.execute("SELECT value FROM settings WHERE merchant_id=1 AND key='config'").fetchone()[0]) == {'round_mode': 'jiao'}
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' and not db.execute('PRAGMA foreign_key_check').fetchall()
    db.close()
    # 可重复执行：第二次什么都不做
    assert init_db(v1db) == {}


def test_money_sync_triggers(v1db):
    init_db(v1db)
    db = connect(v1db)
    db.execute("UPDATE products SET retail=299.9 WHERE code='P1'")                     # 旧代码写元
    assert db.execute("SELECT retail_cents FROM products WHERE code='P1'").fetchone()[0] == 29990
    db.execute("UPDATE products SET retail_cents=12345 WHERE code='P1'")               # 新代码写分
    assert db.execute("SELECT retail FROM products WHERE code='P1'").fetchone()[0] == 123.45
    db.execute("UPDATE members SET balance=balance-10.5 WHERE code='XB00000001'")
    assert db.execute("SELECT balance_cents FROM members WHERE code='XB00000001'").fetchone()[0] == 7830
    db.execute("UPDATE members SET balance_cents=balance_cents+170 WHERE code='XB00000001'")
    assert db.execute("SELECT balance FROM members WHERE code='XB00000001'").fetchone()[0] == 80.0
    db.execute("INSERT INTO products(code,name,retail) VALUES('P9','旧写法',9.99)")
    assert db.execute("SELECT retail_cents, member_price_cents FROM products WHERE code='P9'").fetchone()[:] == (999, 0)
    db.execute("INSERT INTO products(merchant_id,code,name,retail_cents) VALUES(1,'P8','新写法',1500)")
    assert db.execute("SELECT retail FROM products WHERE code='P8'").fetchone()[0] == 15.0
    db.execute("INSERT INTO payments(order_no,method,amount_cents) VALUES('X','cash',-501)")
    assert db.execute("SELECT amount FROM payments WHERE order_no='X'").fetchone()[0] == -5.01
    db.close()


def test_unique_per_merchant(v1db):
    init_db(v1db)
    db = connect(v1db)
    db.execute("INSERT INTO merchants(id, code, name) VALUES(2, 'M2', '别家')")
    db.execute("INSERT INTO stores(merchant_id, code, name) VALUES(2, '8007', '别家8007')")          # 门店编码商户内唯一
    db.execute("INSERT INTO products(merchant_id, code, name) VALUES(2, 'P1', '别家P1')")
    pid = db.execute("SELECT id FROM products WHERE merchant_id=2 AND code='P1'").fetchone()[0]
    db.execute("INSERT INTO product_barcodes(merchant_id, barcode, product_id) VALUES(2, '6900000000011', ?)", (pid,))
    db.execute("INSERT INTO members(merchant_id, code, phone) VALUES(2, 'XB00000001', '13800000001')")
    for sql in ("INSERT INTO products(merchant_id, code, name) VALUES(1, 'P1', '重复')",
                "INSERT INTO product_barcodes(merchant_id, barcode, product_id) VALUES(1, '6900000000011', 2)",
                "INSERT INTO members(merchant_id, code, phone) VALUES(1, 'XB9', '13800000001')",
                "INSERT INTO users(merchant_id, username, password_hash) VALUES(2, 'admin', 'x')"):         # 登录名全局唯一
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    db.close()


def test_old_code_still_sells_after_migration(v1db):
    """迁移后不改业务代码，v1 收银流程照常：扣库存同时更新 inv_stock，流水带来源单号。"""
    init_db(v1db)
    from shouyin_app import create_app
    app = create_app({'DB_PATH': v1db, 'SECRET_KEY': 't', 'TESTING': True})
    c = app.test_client()
    assert c.post('/api/login', json={'username': 'shouyin01', 'password': '123456'}).status_code == 200
    assert c.get('/api/pos/shift/current').get_json()['shift']['id'] == 1
    r = c.post('/api/pos/checkout', json={'items': [{'code': '6900000000012', 'qty': 2}], 'payments': [{'method': 'cash', 'amount': 600}]}).get_json()
    assert r['ok'] and r['change'] == 62.4
    db = connect(v1db)
    assert db.execute("SELECT qty FROM stock WHERE product_id=1 AND store_code='8007'").fetchone()[0] == 10
    assert db.execute("SELECT qty FROM inv_stock WHERE product_id=1 AND wh_code='8007' AND batch_id=0").fetchone()[0] == 10
    mv = db.execute("SELECT * FROM stock_moves WHERE ref_no=?", (r['order_no'],)).fetchone()
    assert (mv['type'], mv['ref_type'], mv['change'], mv['qty_before'], mv['qty_after'], mv['merchant_id']) == ('销售', 'sale', -2, 12, 10, 1)
    o = db.execute("SELECT payable, payable_cents, merchant_id FROM orders WHERE order_no=?", (r['order_no'],)).fetchone()
    assert o[:] == (537.6, 53760, 1)
    db.close()


def test_migrate_script_reports_ok(v1db, capsys):
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
    import migrate_db
    assert migrate_db.main(['--db', v1db]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep['ok'] and rep['problems'] == [] and os.path.exists(rep['backup'])
    assert rep['counts_before'] == rep['counts_after']
    assert migrate_db.main(['--db', v1db, '--check']) == 0
