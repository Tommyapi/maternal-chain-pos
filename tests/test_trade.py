# -*- coding: utf-8 -*-
"""结算、组合支付、储值与积分、券、退货授权、交班、库存扣减（v1 用例按 v2 模型改写，逐条说明见 REPORT-3.md「v1 test_trade 改动」）。

改写原则：断言的业务含义不变；只改 ① 错误码按契约（库存/储值不足 409、NEED_AUTH/AUTH_FAILED）；
② 造数不再走 /api/admin/*（模块④会拆掉 views_admin），改为直接写测试库；③ 负库存默认禁止后，无库存商品先入库。
"""
from conftest import checkout, Client
from test_pos_helpers import set_config, add_coupon, stock_in, legacy_stock


def stock(dbc, code, store):
    return legacy_stock(dbc, code, store)


def recharge(c, member, amount, method='cash'):
    r = c.post('/api/pos/member/recharge', {'member_code': member, 'amount': amount, 'method': method})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_scan_by_any_barcode_and_code(cashier):
    for code in ('6900000000011', '6900000000012', 'P1'):
        d = cashier.get('/api/pos/goods/scan?code=' + code).get_json()
        assert d['ok'] and d['goods']['code'] == 'P1' and d['goods']['stock'] == 10
        assert d['data']['cost'] is None                    # 收银员看不到成本（字段权限）
    assert cashier.get('/api/pos/goods/scan?code=nope').status_code == 404


def test_checkout_cash_change_and_stock(cashier, dbc):
    r = checkout(cashier, [{'code': '6900000000011', 'qty': 2}, {'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 1000}])
    d = r.get_json()
    assert r.status_code == 200 and d['ok']
    assert d['payable'] == 900.0 and d['paid_total'] == 1000.0 and d['change'] == 100.0
    o = d['order']
    assert o['status'] == '已完成' and o['store_code'] == 'A001' and len(o['items']) == 2
    assert o['order_no'].startswith('S001A001')            # 统一单号规则 next_no
    # 现金记账为净额（收 1000 找 100）
    assert o['payments'][0]['amount'] == 900.0 and o['payments'][0]['pay_no'].startswith('PY')
    assert stock(dbc, 'P1', 'A001') == 8 and stock(dbc, 'P2', 'A001') == 19
    assert stock(dbc, 'P1', 'B001') == 5          # 别的门店不受影响


def test_underpay_rejected(cashier):
    r = checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 100}])
    assert r.status_code == 400 and '不足' in r.get_json()['error']


def test_combo_payment(cashier):
    r = checkout(cashier, [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 1}],
                 [{'method': 'wechat', 'amount': 200}, {'method': 'alipay', 'amount': 100}, {'method': 'cash', 'amount': 100},
                  {'method': 'bankcard', 'amount': 100}])
    d = r.get_json()
    assert d['ok'] and d['change'] == 0
    assert sorted(p['method'] for p in d['order']['payments']) == ['alipay', 'bankcard', 'cash', 'wechat']
    assert sum(p['amount'] for p in d['order']['payments']) == 500.0


def test_noncash_overpay_rejected(cashier):
    r = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'wechat', 'amount': 150}])
    assert r.status_code == 400 and '只有现金可以找零' in r.get_json()['error']


def test_idempotent_client_id(cashier, dbc):
    body = {'items': [{'code': 'P2', 'qty': 1}], 'payments': [{'method': 'cash', 'amount': 100}], 'client_id': 'abc123'}
    a = cashier.post('/api/pos/checkout', body).get_json()
    b = cashier.post('/api/pos/checkout', body).get_json()
    assert a['order_no'] == b['order_no'] and b.get('duplicate') and b['data']['duplicate']
    assert stock(dbc, 'P2', 'A001') == 19            # 只扣一次
    assert dbc.execute('SELECT COUNT(*) FROM payments WHERE order_no=?', (a['order_no'],)).fetchone()[0] == 1


def test_member_price_and_points_earned(cashier):
    r = checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 380}], member_code='M001')
    d = r.get_json()
    assert d['payable'] == 380.0 and d['order']['points_earned'] == 380
    m = cashier.get('/api/pos/member/lookup?kw=13800000001').get_json()['member']
    assert m['points'] == 380 and m['orders'] == 1 and m['amount'] == 380.0
    assert m['phone'] == '138****0001'               # 收银员手机号脱敏


def test_stored_value_and_points_redeem(cashier):
    d = recharge(cashier, 'M001', 1000)
    assert d['balance'] == 1000.0
    checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'stored', 'amount': 380}], member_code='M001')
    m = cashier.get('/api/pos/member/lookup?kw=M001').get_json()['member']
    assert m['stored_balance'] == 620.0 and m['points'] == 380
    # 用 300 积分 = 抵 3 元（100 分抵 1 元）
    r = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 97}], member_code='M001', points_used=300)
    d = r.get_json()
    assert d['ok'] and d['payable'] == 97.0 and d['order']['points_amount'] == 3.0
    m = cashier.get('/api/pos/member/lookup?kw=M001').get_json()['member']
    assert m['points'] == 380 - 300 + 97


def test_stored_value_insufficient_and_needs_member(cashier):
    r = checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'stored', 'amount': 400}])
    assert r.status_code == 400 and '会员' in r.get_json()['error']
    r = checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'stored', 'amount': 380}], member_code='M001')
    assert r.status_code == 409 and r.get_json()['code'] == 'STORED_SHORT' and '余额不足' in r.get_json()['error']


def test_recharge_gift_rule(cashier, dbc):
    set_config(dbc, recharge_rules=[{'threshold': 1000, 'gift': 150}, {'threshold': 500, 'gift': 50}])
    d = recharge(cashier, 'M002', 1000)
    assert d['gift'] == 150.0 and d['balance'] == 1150.0
    d = recharge(cashier, 'M002', 600)
    assert d['gift'] == 50.0 and d['balance'] == 1800.0
    log = dbc.execute("SELECT principal_cents, gift_cents FROM stored_log WHERE member_code='M002' ORDER BY id").fetchall()
    assert [tuple(x) for x in log] == [(100000, 15000), (60000, 5000)]          # 本金/赠送分账
    # 充值幂等
    a = cashier.post('/api/pos/recharge', {'member_code': 'M002', 'amount': 100, 'method': 'cash', 'client_id': 'rc-1'}).get_json()
    b = cashier.post('/api/pos/recharge', {'member_code': 'M002', 'amount': 100, 'method': 'cash', 'client_id': 'rc-1'}).get_json()
    assert a['txn_no'] == b['txn_no'] and b['duplicate'] and a['balance'] == 1900.0


def test_coupon_use_once(cashier, dbc):
    cno = add_coupon(dbc, 'M001', 20, 100)
    m = cashier.get('/api/pos/member/lookup?kw=M001').get_json()['member']
    assert m['coupons'][0]['coupon_no'] == cno
    # 别人的会员
    assert checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}], member_code='M002', coupon_no=cno).status_code == 400
    r = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 80}], member_code='M001', coupon_no=cno)
    d = r.get_json()
    assert d['ok'] and d['payable'] == 80.0 and d['order']['coupon_amount'] == 20.0
    assert [x['kind'] for x in d['order']['discounts']] == ['coupon']
    # 同一张券不能再用
    r = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 80}], member_code='M001', coupon_no=cno)
    assert r.status_code == 409 and '已使用' in r.get_json()['error']


def test_round_off(cashier):
    r = cashier.post('/api/pos/price', {'items': [{'code': 'P2', 'qty': 1, 'price': 99.57}], 'round_mode': 'yuan'})
    d = r.get_json()
    # 收银员改价需授权，但计价接口返回 need_auth 而不是拒绝
    assert d['payable'] == 99.0 and d['round_off'] == 0.57
    # 改价超收银员额度；收银台自选抹角（商户参数为不抹零）按折扣算，0.57/99.57 在收银员 9 折额度内
    assert [(n['kind'], n['self_ok']) for n in d['need_auth']] == [('price_change', False), ('discount', True)]


def test_cashier_discount_requires_manager_auth(cashier):
    body = {'items': [{'code': 'P1', 'qty': 1}], 'payments': [{'method': 'cash', 'amount': 400}], 'order_discount': {'rate': 0.8}}
    r = cashier.post('/api/pos/checkout', body)
    assert r.status_code == 403 and r.get_json()['code'] == 'NEED_AUTH' and r.get_json()['data']['kind'] == 'discount'
    body.update(auth_user='dianzhang', auth_pass='wrong')
    r = cashier.post('/api/pos/checkout', body)
    assert r.status_code == 403 and r.get_json()['code'] == 'AUTH_FAILED'
    body['auth_pass'] = '123456'
    body['payments'] = [{'method': 'cash', 'amount': 320}]
    d = cashier.post('/api/pos/checkout', body).get_json()
    assert d['ok'] and d['payable'] == 320.0 and d['order']['auth_user'] == 'dianzhang'
    od = [x for x in d['order']['discounts'] if x['kind'] == 'order_discount']
    assert sum(x['amount'] for x in od) == 80.0 and od[0]['auth_user'] == 'dianzhang'


def test_other_store_manager_cannot_authorize(cashier):
    body = {'items': [{'code': 'P1', 'qty': 1}], 'payments': [{'method': 'cash', 'amount': 320}], 'order_discount': {'rate': 0.8},
            'auth_user': 'dianzhangB', 'auth_pass': '123456'}
    r = cashier.post('/api/pos/checkout', body)
    assert r.status_code == 403 and r.get_json()['code'] == 'AUTH_FAILED'


def test_open_price_for_unpriced_product(cashier, dbc):
    stock_in(dbc, dbc.execute("SELECT id FROM products WHERE code='P3' AND merchant_id=1").fetchone()[0], 'A001', 5)
    r = checkout(cashier, [{'code': 'P3', 'qty': 1}], [{'method': 'cash', 'amount': 10}])
    assert r.status_code == 400 and '没有设置售价' in r.get_json()['error']
    r = checkout(cashier, [{'code': 'P3', 'qty': 1, 'price': 12.5}], [{'method': 'cash', 'amount': 12.5}])
    assert r.get_json()['ok'] and r.get_json()['payable'] == 12.5         # 未定价商品现场开价不需要授权


def test_promotion_price(cashier, dbc):
    dbc.execute("INSERT INTO promotions(merchant_id, name, product_code, promo_price, start_date, end_date, store_code, status, created_at) "
                "VALUES(1, '特价', 'P1', 350, '2000-01-01', '2999-12-31', '', '启用', '2026-01-01')")
    d = cashier.post('/api/pos/price', {'items': [{'code': 'P1', 'qty': 1}]}).get_json()
    assert d['payable'] == 350.0 and d['items'][0]['promo']
    # 过期促销不生效
    dbc.execute("INSERT INTO promotions(merchant_id, name, product_code, promo_price, start_date, end_date, store_code, status, created_at) "
                "VALUES(1, '过期', 'P2', 1, '2000-01-01', '2000-01-02', '', '启用', '2026-01-01')")
    assert cashier.post('/api/pos/price', {'items': [{'code': 'P2', 'qty': 1}]}).get_json()['payable'] == 100.0


def test_negative_stock_default_forbidden(app, cashier, dbc):
    """POS-109：默认禁止负库存；商户开启后也只有带 pos.negative_stock 的角色能超卖，并发高风险提醒。"""
    r = checkout(cashier, [{'code': 'P1', 'qty': 11}], [{'method': 'cash', 'amount': 5000}])
    assert r.status_code == 409 and r.get_json()['code'] == 'STOCK_SHORT' and '库存不足' in r.get_json()['error']
    set_config(dbc, allow_negative_stock=True)
    assert checkout(cashier, [{'code': 'P1', 'qty': 11}], [{'method': 'cash', 'amount': 5000}]).status_code == 409   # 收银员角色无权超卖
    mgr = Client(app, 'dianzhang')
    mgr.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00102'})
    d = checkout(mgr, [{'code': 'P1', 'qty': 11}], [{'method': 'cash', 'amount': 5000}]).get_json()
    assert d['ok'] and stock(dbc, 'P1', 'A001') == -1
    assert dbc.execute("SELECT COUNT(*) FROM notifications WHERE category='risk' AND title LIKE '负库存%'").fetchone()[0] >= 1
    set_config(dbc, allow_negative_stock=False)
    assert checkout(mgr, [{'code': 'P2', 'qty': 21}], [{'method': 'cash', 'amount': 5000}]).status_code == 409


def test_checkout_requires_open_shift(app):
    c = Client(app, 'shouyin01')
    r = checkout(c, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])
    assert r.status_code == 400 and '开班' in r.get_json()['error']


# ---------------------------------------------------------------- 退货
def test_refund_needs_manager_and_restores_everything(cashier, manager, dbc):
    recharge(cashier, 'M001', 1000)
    d = checkout(cashier, [{'code': 'P1', 'qty': 2}], [{'method': 'stored', 'amount': 500}, {'method': 'cash', 'amount': 260}],
                 member_code='M001').get_json()
    assert d['ok'] and d['payable'] == 760.0
    no = d['order_no']
    line = d['order']['items'][0]['id']
    # 收银员不带授权 -> 403 NEED_AUTH
    r = cashier.post('/api/pos/refund', {'order_no': no, 'items': [{'line_id': line, 'qty': 1}]})
    assert r.status_code == 403 and r.get_json()['code'] == 'NEED_AUTH'
    # 错误密码
    r = cashier.post('/api/pos/refund', {'order_no': no, 'items': [{'line_id': line, 'qty': 1}], 'auth_user': 'dianzhang', 'auth_pass': 'x'})
    assert r.status_code == 403 and r.get_json()['code'] == 'AUTH_FAILED'
    r = cashier.post('/api/pos/refund', {'order_no': no, 'items': [{'line_id': line, 'qty': 1}], 'reason': '质量问题',
                                         'auth_user': 'dianzhang', 'auth_pass': '123456'})
    rd = r.get_json()
    assert rd['ok'] and rd['refund_amount'] == 380.0 and rd['order_no'].startswith('R001A001')
    assert stock(dbc, 'P1', 'A001') == 9           # 10 - 2 + 1
    assert sum(p['amount'] for p in rd['order']['payments']) == -380.0
    assert [p['method'] for p in rd['payments']] == ['stored']          # 默认顺序：先退储值
    o = cashier.get('/api/pos/orders/' + no).get_json()['order']
    assert o['status'] == '部分退货' and o['items'][0]['refunded_qty'] == 1 and o['items'][0]['refundable_amount'] == 380.0
    # 超退
    r = cashier.post('/api/pos/refund', {'order_no': no, 'items': [{'line_id': line, 'qty': 2}], 'auth_user': 'dianzhang', 'auth_pass': '123456'})
    assert r.status_code == 400 and '可退数量' in r.get_json()['error']
    # 店长自己退剩余（额度内，不用授权）
    mgr = manager
    mgr.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00102'})
    rd = mgr.post('/api/pos/refund', {'order_no': no, 'items': []}).get_json()
    assert rd['ok'] and rd['refund_amount'] == 380.0
    assert sorted((p['method'], p['amount']) for p in rd['payments']) == [('cash', 260.0), ('stored', 120.0)]
    assert cashier.get('/api/pos/orders/' + no).get_json()['order']['status'] == '已退货'
    assert stock(dbc, 'P1', 'A001') == 10
    # 积分按比例扣回为 0，储值全部退回
    m = cashier.get('/api/pos/member/lookup?kw=M001').get_json()['member']
    assert m['points'] == 0 and m['stored_balance'] == 1000.0


def test_refund_other_store_forbidden(cashier, cashierB):
    d = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}]).get_json()
    r = cashierB.post('/api/pos/refund', {'order_no': d['order_no'], 'auth_user': 'dianzhangB', 'auth_pass': '123456'})
    assert r.status_code == 403


# ---------------------------------------------------------------- 交班
def test_shift_close_totals(cashier, manager):
    checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])
    checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'wechat', 'amount': 400}])
    recharge(cashier, 'M001', 200, 'cash')
    cur = cashier.get('/api/pos/shift/current').get_json()
    s = cur['summary']
    assert s['sale_count'] == 2 and s['sale_amount'] == 500.0 and s['recharge_amount'] == 200.0
    assert s['expected_cash'] == 100 + 100 + 200      # 备用金 + 销售现金 + 充值现金
    r = cashier.post('/api/pos/shift/close', {'counted_cash': 395})
    d = r.get_json()
    assert d['ok'] and d['shift']['diff'] == -5.0 and d['shift']['status'] == 'closed'
    assert cashier.get('/api/pos/shift/current').get_json()['shift'] is None
    # 交班后不能继续收银；店长能看到本店交班记录
    assert checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}]).status_code == 400
    rows = manager.get('/api/pos/shifts?status=closed').get_json()['rows']
    assert rows[0]['diff'] == -5.0 and rows[0]['summary']['by_method']


def test_held_orders(cashier):
    assert cashier.post('/api/pos/held', {'title': 'x', 'payload': {'items': [{'code': 'P1', 'qty': 1}]}}).get_json()['ok']
    rows = cashier.get('/api/pos/held').get_json()['rows']
    assert len(rows) == 1 and rows[0]['payload']['items'][0]['code'] == 'P1'
    assert cashier.c.delete('/api/pos/held/%d' % rows[0]['id']).status_code == 200
    assert cashier.get('/api/pos/held').get_json()['rows'] == []
    assert cashier.c.delete('/api/pos/held/%d' % rows[0]['id']).status_code == 404
