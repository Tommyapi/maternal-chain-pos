# -*- coding: utf-8 -*-
"""收银台越权（AC-012 收银侧、3.3）：跨商户、跨门店、无权限码、字段权限。

views_pos 蓝图仍带 legacy_single_tenant 标记（冻结测试 test_perms.py:63 依赖它），所以跨商户用例先 monkeypatch 去掉标记，
证明「去掉标记后」模块③自身的查询也全部按 merchant_id 隔离——总脑删标记时这些用例就是依据。
"""
import pytest

from conftest import Client
from test_pos_helpers import body, m2cashier_client, add_coupon
from shouyin_app import views_pos


@pytest.fixture()
def m2(app, dbc):
    c = m2cashier_client(app, dbc)
    assert c.post('/api/pos/shift/open', {'opening_cash': 10, 'terminal_no': 'Z00101'}).get_json()['ok']
    return c


def test_other_merchant_fully_isolated(m2, cashier, dbc):
    cno = add_coupon(dbc, 'M001', 20, 0)
    a = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}], client_id='m1-order')).get_json()
    no1 = a['order_no']
    cashier.post('/api/pos/held', {'title': 'm1', 'payload': {'items': [{'code': 'P1', 'qty': 1}]}})
    held1 = dbc.execute('SELECT id FROM held_orders WHERE merchant_id=1').fetchone()[0]
    shift1 = dbc.execute("SELECT id FROM shifts WHERE merchant_id=1 AND status='open'").fetchone()[0]

    ctx = m2.get('/api/pos/context').get_json()['data']
    assert ctx['store']['code'] == 'Z001' and [s['code'] for s in ctx['stores']] == ['Z001'] and ctx['merchant_id'] == 2
    assert m2.get('/api/pos/context?store=A001').status_code == 403
    assert m2.get('/api/pos/goods/scan?code=P1').status_code == 404                      # 1 号商户商品编码
    assert m2.get('/api/pos/goods/scan?code=6900000000011').status_code == 404
    assert m2.get('/api/pos/goods/search?kw=奶粉').get_json()['rows'][0]['code'] == 'Q1'
    assert m2.get('/api/pos/member/lookup?kw=13800000001').status_code == 404
    assert m2.get('/api/pos/member/lookup?kw=13900000009').get_json()['member']['code'] == 'Z0001'
    d = m2.post('/api/pos/checkout', body([{'code': 'Q1', 'qty': 1}], [{'method': 'cash', 'amount': 300}])).get_json()
    assert d['ok'] and d['order_no'].startswith('S002Z001')
    # 详情 / 列表 / 退货 / 重打：1 号商户单据一律查不到（404，不泄露存在性）
    assert m2.get('/api/pos/orders/' + no1).status_code == 404
    assert m2.post('/api/pos/orders/%s/reprint' % no1).status_code == 404
    assert m2.post('/api/pos/refund', {'order_no': no1}).status_code == 404
    assert m2.post('/api/pos/exchange', {'order_no': no1, 'back_items': [{'line_id': 1, 'qty': 1}], 'new_items': [{'code': 'Q1', 'qty': 1}]}).status_code == 404
    lst = m2.get('/api/pos/orders?store=A001').get_json()
    assert lst['total'] == 1 and lst['rows'][0]['store_code'] == 'Z001'
    # 别家 client_id 冲突不返回别家单据
    r = m2.post('/api/pos/checkout', body([{'code': 'Q1', 'qty': 1}], [{'method': 'cash', 'amount': 300}], client_id='m1-order'))
    assert r.status_code == 409 and no1 not in r.get_data(as_text=True)
    # 1 号商户的券、挂单、班次、店长授权都用不了
    r = m2.post('/api/pos/checkout', body([{'code': 'Q1', 'qty': 1}], [{'method': 'cash', 'amount': 300}], member_code='Z0001', coupon_nos=[cno]))
    assert r.status_code == 400
    assert m2.c.delete('/api/pos/held/%d' % held1).status_code == 404
    assert m2.get('/api/pos/held').get_json()['rows'] == []
    assert m2.post('/api/pos/auth', {'auth_user': 'dianzhang', 'auth_pass': '123456'}).status_code == 403
    r = m2.post('/api/pos/checkout', body([{'code': 'Q1', 'qty': 1}], [{'method': 'cash', 'amount': 240}], order_discount={'rate': '0.8'},
                                          auth_user='dianzhang', auth_pass='123456'))
    assert r.status_code == 403 and r.get_json()['code'] == 'AUTH_FAILED'
    assert [t['terminal_no'] for t in m2.get('/api/pos/terminals').get_json()['rows']] == ['Z00101']
    m2admin = Client(m2.c.application, 'm2admin')
    assert m2admin.post('/api/pos/shift/force-close', {'shift_id': shift1, 'reason': 'x'}).status_code == 404
    assert m2admin.post('/api/pos/shift/%d/reprint' % shift1).status_code == 404
    # 1 号商户这边也看不到 2 号的单
    assert cashier.get('/api/pos/orders/' + d['order_no']).status_code == 404
    assert dbc.execute("SELECT COUNT(*) FROM orders WHERE merchant_id=2").fetchone()[0] == 1
    assert dbc.execute("SELECT qty FROM inv_stock WHERE merchant_id=2 AND wh_code='Z001'").fetchone()[0] == 4


def test_function_perms(app, cashier):
    assert Client(app, 'huiyuan').get('/api/pos/context').status_code == 403            # 会员运营没有 pos.login
    zhidu = Client(app, 'zhidu')
    assert zhidu.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])).status_code == 403
    assert zhidu.post('/api/pos/shift/open', {'opening_cash': 0}).status_code == 403
    assert Client(app, 'platform', 'platpass1').get('/api/pos/orders').status_code == 403
    assert cashier.post('/api/pos/refund/no-receipt', {}).status_code == 403
    assert cashier.post('/api/pos/shift/force-close', {'shift_id': 1, 'reason': 'x'}).status_code == 403


def test_field_perms(app, cashier, dbc):
    d = cashier.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 380}], member_code='M001')).get_json()
    o = cashier.get('/api/pos/orders/' + d['order_no']).get_json()['order']
    assert o['items'][0]['cost'] is None and o['member_phone'] == '138****0001'
    assert cashier.get('/api/pos/orders').get_json()['rows'][0]['member_phone'] == '138****0001'
    caiwu = Client(app, 'caiwu')                                                        # 财务有成本 + 报表查看
    o = caiwu.get('/api/pos/orders/' + d['order_no']).get_json()['order']
    assert o['items'][0]['cost'] == 300.0
    root = Client(app, 'root', 'rootpass1')
    assert root.get('/api/pos/orders/' + d['order_no']).get_json()['order']['member_phone'] == '13800000001'


def test_store_scope_on_details(app, cashier, cashierB):
    d = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])).get_json()
    assert cashierB.get('/api/pos/orders/' + d['order_no']).status_code == 403
    assert cashierB.post('/api/pos/orders/%s/reprint' % d['order_no']).status_code == 403
    assert cashierB.post('/api/pos/exchange', {'order_no': d['order_no'], 'back_items': [{'line_id': 1, 'qty': 1}],
                                               'new_items': [{'code': 'P1', 'qty': 1}]}).status_code == 403
    quyu = Client(app, 'quyu')                                                          # 区域经理只管 R1（A001）
    assert quyu.get('/api/pos/orders/' + d['order_no']).status_code == 200
    assert quyu.get('/api/pos/orders?store=B001').get_json()['total'] == 1             # 范围外门店参数被忽略，仍只看 A001
