# -*- coding: utf-8 -*-
"""评审回归（2026-11-05 代码评审发现的 4 个问题）：自选抹零绕过折扣额度、小数数量累计退货、黑名单会员用积分/券、跨门店改收银台。"""
from conftest import Client
from test_pos_helpers import add_product, add_unit, stock_in, add_coupon, body


def test_client_round_mode_cannot_bypass_discount_limit(cashier, dbc):
    stock_in(dbc, dbc.execute("SELECT id FROM products WHERE code='P3' AND merchant_id=1").fetchone()[0], 'A001', 5)
    req = body([{'code': 'P3', 'qty': 1, 'price': 1.99}], [{'method': 'cash', 'amount': 1}], round_mode='yuan')
    r = cashier.post('/api/pos/checkout', req)
    assert r.status_code == 403 and r.get_json()['code'] == 'NEED_AUTH' and r.get_json()['data']['kind'] == 'discount'
    r = cashier.post('/api/pos/checkout', dict(req, auth_user='dianzhang', auth_pass='123456'))
    assert r.get_json()['code'] == 'AUTH_FAILED'                                  # 实际 5 折，超过店长 7 折额度
    assert cashier.post('/api/pos/checkout', dict(req, auth_user='root', auth_pass='rootpass1')).get_json()['ok']


def test_fractional_box_refunds_sum_exactly(app, dbc):
    mgr = Client(app, 'dianzhang')
    mgr.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00102'})
    pid = add_product(dbc, 'P6', '湿巾单包', 10, cost=5, unit='包', barcodes=['6900000000061'])
    add_unit(dbc, pid, '箱', 10, barcode='6900000000069')
    stock_in(dbc, pid, 'A001', 10)
    d = mgr.post('/api/pos/checkout', body([{'code': '6900000000069', 'qty': 1}], [{'method': 'cash', 'amount': 100}])).get_json()
    line = d['order']['items'][0]['id']
    got = []
    for q in (0.1, 0.2, 0.7):                       # 0.1+0.2 浮点累计 = 0.30000000000000004
        r = mgr.post('/api/pos/refund', {'order_no': d['order_no'], 'items': [{'line_id': line, 'qty': q}]}).get_json()
        assert r['ok'], r
        got.append(r['refund_amount'])
    assert round(sum(got), 2) == 100.0
    assert mgr.get('/api/pos/orders/' + d['order_no']).get_json()['order']['status'] == '已退货'


def test_blacklisted_member_cannot_use_points_or_coupon(cashier, dbc):
    dbc.execute("UPDATE members SET points=1000, blacklisted=1 WHERE code='M001' AND merchant_id=1")
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 95}], member_code='M001', points_used=500))
    assert r.status_code == 409 and '黑名单' in r.get_json()['msg']
    cno = add_coupon(dbc, 'M001', 10, 0)
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 90}], member_code='M001', coupon_nos=[cno]))
    assert r.status_code == 409
    assert cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}], member_code='M001')).get_json()['ok']


def test_cannot_move_other_store_terminal(app, cashier, dbc):
    dbc.execute("INSERT INTO role_perms(role_id, perm) SELECT id, 'org.edit' FROM roles WHERE merchant_id=1 AND code='store_manager'")
    b = Client(app, 'dianzhangB')
    r = b.post('/api/pos/terminals', {'terminal_no': 'A00101', 'store_code': 'B001'})
    assert r.status_code == 403 and r.get_json()['code'] == 'SCOPE_DENIED'
    assert b.post('/api/pos/terminals', {'terminal_no': 'B00109', 'store_code': 'B001'}).get_json()['ok']


def test_client_id_replay_on_other_doc_type_conflicts(cashier):
    a = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}], client_id='dup-x')).get_json()
    r = cashier.post('/api/pos/refund', {'order_no': a['order_no'], 'client_id': 'dup-x', 'auth_user': 'dianzhang', 'auth_pass': '123456'})
    assert r.status_code == 409
