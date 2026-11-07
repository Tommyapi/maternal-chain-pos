# -*- coding: utf-8 -*-
"""测试夹具（v2 多租户模型）。

商户 1（萌贝）：门店 A001 甲店 / B001 乙店；账号（密码都是 123456，root 是 rootpass1）：
    root=商户超管  dianzhang=A001 店长  shouyin01=A001 收银员  dianzhangB / shouyinB = B001 店长/收银员
    caiwu=财务  huiyuan=会员运营  zhidu=只读管理者  quyu=区域经理（区域 R1 含 A001）  shangpin=总部商品采购仓管
商户 2（别家）：门店 Z001；账号 m2admin（商户超管）、m2cashier；商品 Q1；会员 13900000009 —— 用来测跨商户越权。
平台：platform（平台运营管理员，merchant_id=0）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shouyin_app import create_app          # noqa: E402
from shouyin_app.seed import seed            # noqa: E402
from shouyin_app.db import connect           # noqa: E402
from werkzeug.security import generate_password_hash   # noqa: E402

DATA = {
    'stores': [{'code': 'A001', 'name': '甲店', 'sort': 0}, {'code': 'B001', 'name': '乙店', 'sort': 1}],
    'products': [
        {'code': 'P1', 'name': '奶粉一段800g', 'category': '奶粉', 'brand': '金领冠', 'unit': '听', 'retail': 400, 'member_price': 380,
         'cost': 300, 'barcodes': ['6900000000011', '6900000000012']},
        {'code': 'P2', 'name': '纸尿裤L', 'category': '尿裤', 'brand': '帮宝适', 'unit': '包', 'retail': 100, 'member_price': 100, 'cost': 60,
         'barcodes': ['6900000000021']},
        {'code': 'P3', 'name': '婴儿湿巾', 'category': '用品', 'brand': '', 'unit': '包', 'retail': 0, 'member_price': 0, 'cost': 0,
         'barcodes': ['6900000000031']},
    ],
    'stock': [{'store_code': 'A001', 'code': 'P1', 'qty': 10}, {'store_code': 'A001', 'code': 'P2', 'qty': 20},
              {'store_code': 'B001', 'code': 'P1', 'qty': 5}],
    'members': [
        {'code': 'M001', 'phone': '13800000001', 'name': '张妈妈', 'level': '普通会员', 'reg_store_code': 'A001', 'reg_store_name': '甲店',
         'babies': [{'birthday': '2025-05-01'}]},
        {'code': 'M002', 'phone': '13800000002', 'name': '李妈妈', 'level': '银卡会员'},
    ],
    'guides': [{'code': 'G1', 'name': '小王', 'store_code': 'A001'}],
}


@pytest.fixture()
def app(tmp_path):
    p = tmp_path / 'seed.json'
    p.write_text(json.dumps(DATA, ensure_ascii=False), encoding='utf-8')
    db_path = str(tmp_path / 'test.db')
    pw_file = str(tmp_path / 'ADMIN_PASSWORD.txt')
    seed(db_path, str(p), pw_file)
    # 把测试账号绑定到 A001，另建乙店店长
    db = connect(db_path)
    from shouyin_app.seed import ensure_user
    db.execute('BEGIN')
    ensure_user(db, 'dianzhang', '123456', '演示店长', 'manager', 'A001')
    ensure_user(db, 'shouyin01', '123456', '演示收银员', 'cashier', 'A001')
    db.execute('COMMIT')
    db.execute("INSERT INTO users(username,password_hash,name,role,store_code,status,created_at) VALUES(?,?,?,?,?,?,?)",
               ('dianzhangB', generate_password_hash('123456'), '乙店店长', 'manager', 'B001', '启用', '2026-01-01 00:00:00'))
    db.execute("INSERT INTO users(username,password_hash,name,role,store_code,status,created_at) VALUES(?,?,?,?,?,?,?)",
               ('shouyinB', generate_password_hash('123456'), '乙店收银', 'cashier', 'B001', '启用', '2026-01-01 00:00:00'))
    db.execute("INSERT INTO users(username,password_hash,name,role,store_code,status,created_at) VALUES(?,?,?,?,?,?,?)",
               ('root', generate_password_hash('rootpass1'), '测试总部', 'admin', '', '启用', '2026-01-01 00:00:00'))
    _more_accounts(db)
    db.close()
    a = create_app({'DB_PATH': db_path, 'SECRET_KEY': 'test', 'TESTING': True})
    a.config['PW_FILE'] = pw_file
    return a


def _more_accounts(db):
    """v2：各默认角色账号 + 第二个商户 + 平台账号。"""
    from shouyin_app.seed import ensure_user
    from shouyin_app.perms import ensure_default_roles
    db.execute('BEGIN')
    ensure_user(db, 'caiwu', '123456', '财务', 'staff', '', 1, 'finance')
    ensure_user(db, 'huiyuan', '123456', '会员运营', 'staff', '', 1, 'member_ops')
    ensure_user(db, 'zhidu', '123456', '只读老板', 'staff', '', 1, 'readonly')
    ensure_user(db, 'shangpin', '123456', '商品采购', 'staff', '', 1, 'hq_goods')
    ensure_user(db, 'quyu', '123456', '区域经理', 'staff', '', 1, 'region_manager')
    db.execute("INSERT INTO org_units(merchant_id, kind, code, name) VALUES(1, 'region', 'R1', '一区')")
    db.execute("UPDATE stores SET region_id=(SELECT id FROM org_units WHERE code='R1') WHERE merchant_id=1 AND code='A001'")
    db.execute("INSERT INTO user_scopes(user_id, scope_type, ref_code) SELECT id, 'region', 'R1' FROM users WHERE username='quyu'")
    # 商户 2
    db.execute("INSERT INTO merchants(id, code, name, status, created_at) VALUES(2, 'OTHER', '别家母婴', 'active', '2026-01-01 00:00:00')")
    ensure_default_roles(db, 2)
    db.execute("INSERT INTO stores(merchant_id, code, name) VALUES(2, 'Z001', '别家店')")
    db.execute("INSERT INTO products(merchant_id, code, name, retail, member_price, cost) VALUES(2, 'Q1', '别家奶粉', 300, 300, 200)")
    db.execute("INSERT INTO members(merchant_id, code, phone, name) VALUES(2, 'Z0001', '13900000009', '别家会员')")
    ensure_user(db, 'm2admin', '123456', '别家老板', 'admin', '', 2, 'merchant_admin')
    ensure_user(db, 'm2cashier', '123456', '别家收银', 'cashier', 'Z001', 2, 'cashier')
    ensure_user(db, 'platform', 'platpass1', '平台管理员', 'platform', '', 0, 'platform_admin')
    db.execute('COMMIT')


class Client(object):
    def __init__(self, app, user=None, password='123456'):
        self.c = app.test_client()
        if user:
            r = self.c.post('/api/login', json={'username': user, 'password': password})
            assert r.status_code == 200, r.get_json()

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, body=None, **kw):
        return self.c.post(url, json=body if body is not None else {}, **kw)

    def j(self, resp):
        return resp.get_json()


@pytest.fixture()
def admin(app):
    return Client(app, 'root', 'rootpass1')


@pytest.fixture()
def manager(app):
    return Client(app, 'dianzhang')


@pytest.fixture()
def cashier(app):
    c = Client(app, 'shouyin01')
    c.post('/api/pos/shift/open', {'opening_cash': 100, 'terminal_no': 'A00101'})
    return c


@pytest.fixture()
def cashierB(app):
    c = Client(app, 'shouyinB')
    c.post('/api/pos/shift/open', {'opening_cash': 50})
    return c


@pytest.fixture()
def platform(app):
    return Client(app, 'platform', 'platpass1')


@pytest.fixture()
def m2admin(app):
    return Client(app, 'm2admin')


@pytest.fixture()
def dbc(app):
    db = connect(app.config['DB_PATH'])
    yield db
    db.close()


def checkout(c, items, payments, **extra):
    body = {'items': items, 'payments': payments}
    body.update(extra)
    return c.post('/api/pos/checkout', body)
