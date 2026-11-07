# -*- coding: utf-8 -*-
"""退货验收：AC-004（多商品部分数量退，分摊/积分/回库）、AC-005（组合支付部分退，顺序与累计上限）、AC-006（打印失败可重打）、
POS-604 无单退货、POS-605 换货、POS-606 残次回库。"""
import pytest

from conftest import Client
from test_pos_helpers import add_coupon, qty, member, body, set_config


@pytest.fixture()
def mgr(app):
    c = Client(app, 'dianzhang')
    c.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00102'})
    return c


def refund(c, no, items, **kw):
    d = dict({'order_no': no, 'items': items}, **kw)
    return c.post('/api/pos/refund', d)


def cents_sum(dbc, sql, args):
    return dbc.execute(sql, args).fetchone()[0]


def test_ac004_partial_qty_refund_shares_points_stock(mgr, dbc):
    dbc.execute("UPDATE members SET points=1000 WHERE code='M001' AND merchant_id=1")
    cno = add_coupon(dbc, 'M001', 20, 100)
    d = mgr.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 3}, {'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 2000}],
                                           member_code='M001', coupon_nos=[cno], points_used=500, order_discount={'rate': '0.95'})).get_json()
    assert d['ok'], d
    o = d['order']
    assert o['payable'] == 1153.0            # (380×3 + 100) × 0.95 − 20 − 5
    p1 = [x for x in o['items'] if x['product_code'] == 'P1'][0]
    p2 = [x for x in o['items'] if x['product_code'] == 'P2'][0]
    assert abs(p1['amount'] + p2['amount'] - 1153.0) < 1e-9
    assert sum(x['amount'] for x in o['discounts']) == round(o['retail_total'] - o['payable'], 2)
    pts_after_sale = member(dbc, 'M001')[0]
    assert pts_after_sale == 1000 - 500 + 1153
    got = []
    for _ in range(3):                         # P1 逐罐退 3 次
        rd = refund(mgr, d['order_no'], [{'line_id': p1['id'], 'qty': 1}]).get_json()
        assert rd['ok'], rd
        got.append(rd['refund_amount'])
    p1_cents = dbc.execute('SELECT amount_cents FROM order_items WHERE id=?', (p1['id'],)).fetchone()[0]
    assert round(sum(got) * 100) == p1_cents                                     # 累计退款 = 行实付，一分不差
    assert refund(mgr, d['order_no'], [{'line_id': p1['id'], 'qty': 1}]).status_code == 400
    assert qty(dbc, 'P1') == 10
    rd = refund(mgr, d['order_no'], [{'line_id': p2['id'], 'qty': 1}]).get_json()
    assert rd['ok']
    total_ref = -cents_sum(dbc, "SELECT SUM(payable_cents) FROM orders WHERE orig_order_no=? AND biz_type='refund'", (d['order_no'],))
    assert total_ref == 115300
    assert mgr.get('/api/pos/orders/' + d['order_no']).get_json()['order']['status'] == '已退货'
    assert member(dbc, 'M001')[0] == 1000                                        # 消费积分全部扣回、抵扣积分全部返还
    logs = [r[0] for r in dbc.execute("SELECT type FROM points_log WHERE member_code='M001' AND type LIKE '退货%'")]
    assert '退货扣回' in logs and '退货返还抵扣积分' in logs


def test_ac005_combo_payment_partial_refund_order_and_cap(mgr, dbc):
    d = mgr.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 2}], [{'method': 'wechat', 'amount': 500}, {'method': 'alipay', 'amount': 200},
                                                                         {'method': 'cash', 'amount': 100}])).get_json()
    assert d['ok'] and d['payable'] == 800.0
    line = d['order']['items'][0]['id']
    # 可配置顺序：先现金、再支付宝、再微信
    rd = refund(mgr, d['order_no'], [{'line_id': line, 'qty': 1}], refund_method_order=['cash', 'alipay', 'wechat']).get_json()
    assert [(p['method'], p['amount']) for p in rd['payments']] == [('cash', 100.0), ('alipay', 200.0), ('wechat', 100.0)]
    # 剩下的按默认顺序（微信 -> 支付宝 -> ... 现金），每种方式不超过原实收剩余
    rd = refund(mgr, d['order_no'], [{'line_id': line, 'qty': 1}]).get_json()
    assert [(p['method'], p['amount']) for p in rd['payments']] == [('wechat', 400.0)]
    net = dict((r[0], r[1]) for r in dbc.execute('SELECT method, SUM(amount_cents) FROM payments WHERE order_no IN (SELECT order_no FROM orders '
                                                 'WHERE order_no=? OR orig_order_no=?) GROUP BY method', (d['order_no'], d['order_no'])))
    assert net == {'wechat': 0, 'alipay': 0, 'cash': 0}
    refs = dbc.execute("SELECT orig_pay_no FROM payments WHERE kind='refund' AND order_no IN (SELECT order_no FROM orders WHERE orig_order_no=?)",
                       (d['order_no'],)).fetchall()
    assert all(r[0].startswith('PY') for r in refs)                               # 每笔退款流水都指向原支付流水


def test_refund_defect_goes_to_defect_stock(mgr, dbc):
    d = mgr.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 2}], [{'method': 'cash', 'amount': 200}])).get_json()
    line = d['order']['items'][0]['id']
    rd = refund(mgr, d['order_no'], [{'line_id': line, 'qty': 1, 'quality': 'defect', 'reason_code': 'BROKEN'}], reason='包装破损').get_json()
    assert rd['ok']
    assert qty(dbc, 'P2') == 18 and qty(dbc, 'P2', quality='defect') == 1        # 残次不增加可售
    it = rd['order']['items'][0]
    assert it['quality'] == 'defect' and it['batch_alloc'][0]['quality'] == 'defect'
    assert mgr.get('/api/pos/goods/scan?code=P2').get_json()['goods']['stock'] == 18
    assert refund(mgr, d['order_no'], [{'line_id': line, 'qty': 1, 'quality': 'bad'}]).status_code == 400


def test_refund_idempotent_and_cashier_limit(cashier, mgr, dbc):
    d = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])).get_json()
    line = d['order']['items'][0]['id']
    req = {'order_no': d['order_no'], 'items': [{'line_id': line, 'qty': 1}], 'client_id': 'rf-1', 'auth_user': 'dianzhang', 'auth_pass': '123456'}
    a = cashier.post('/api/pos/refund', req).get_json()
    b = cashier.post('/api/pos/refund', req).get_json()
    assert a['ok'] and b['duplicate'] and a['order_no'] == b['order_no']
    assert qty(dbc, 'P2') == 20                                                  # 只回库一次


def test_ac006_print_fail_keeps_order_and_reprint(cashier, dbc):
    d = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}], client_id='pf-1')).get_json()
    no = d['order_no']
    r = cashier.post('/api/pos/orders/%s/print' % no, {'result': 'fail'}).get_json()
    assert r['ok'] and r['data']['status'] == '已完成'
    # 收银端重进：同 client_id 重提只拿回原单，不重复开单
    assert cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}], client_id='pf-1')).get_json()['order_no'] == no
    rp = cashier.post('/api/pos/orders/%s/reprint' % no).get_json()
    assert rp['ok'] and rp['order']['reprint'] and rp['order']['reprint_count'] == 1
    logs = dbc.execute('SELECT reprint, result FROM print_logs WHERE doc_no=? ORDER BY id', (no,)).fetchall()
    assert [tuple(x) for x in logs] == [(0, 'fail'), (1, 'sent')]
    assert dbc.execute("SELECT COUNT(*) FROM orders WHERE biz_type='sale'").fetchone()[0] == 1


def test_no_receipt_refund(cashier, mgr, dbc):
    req = {'items': [{'code': 'P2', 'qty': 1, 'price': 100, 'quality': 'pending'}], 'refund_method': 'cash', 'reason': '顾客无小票', 'client_id': 'nr-1'}
    assert cashier.post('/api/pos/refund/no-receipt', req).status_code == 403             # 收银员没有无单退货权限
    d = mgr.post('/api/pos/refund/no-receipt', req).get_json()
    assert d['ok'] and d['refund_amount'] == 100.0 and d['order']['no_receipt'] == 1
    assert qty(dbc, 'P2', quality='pending') == 1 and qty(dbc, 'P2') == 20
    assert mgr.post('/api/pos/refund/no-receipt', req).get_json()['duplicate']
    r = mgr.post('/api/pos/refund/no-receipt', dict(req, client_id='nr-2', items=[{'code': 'P2', 'qty': 1, 'price': 101}]))
    assert r.status_code == 400 and '不能高于零售价' in r.get_json()['msg']
    assert mgr.post('/api/pos/refund/no-receipt', dict(req, client_id='nr-3', reason='')).status_code == 400
    set_config(dbc, allow_no_receipt_refund=False)
    r = mgr.post('/api/pos/refund/no-receipt', dict(req, client_id='nr-4'))
    assert r.status_code == 403 and '禁用' in r.get_json()['msg']
    assert dbc.execute("SELECT COUNT(*) FROM audit_logs WHERE action='无单退货'").fetchone()[0] == 1


def test_exchange_pay_difference(mgr, dbc):
    d = mgr.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 2}], [{'method': 'wechat', 'amount': 200}])).get_json()
    line = d['order']['items'][0]['id']
    req = {'order_no': d['order_no'], 'back_items': [{'line_id': line, 'qty': 1}], 'new_items': [{'code': 'P1', 'qty': 1}],
           'payments': [{'method': 'cash', 'amount': 300}], 'client_id': 'ex-1', 'reason': '换大段奶粉'}
    x = mgr.post('/api/pos/exchange', req).get_json()
    assert x['ok'], x
    assert x['order_no'].startswith('X') and x['refund_no'].startswith('R') and x['sale_no'].startswith('S')
    assert (x['back_amount'], x['new_amount'], x['diff']) == (100.0, 400.0, 300.0)
    assert qty(dbc, 'P2') == 19 and qty(dbc, 'P1') == 9
    s = x['sale_order']
    assert sorted((p['method'], p['amount']) for p in s['payments']) == [('cash', 300.0), ('exchange', 100.0)] and s['exchange_no'] == x['order_no']
    assert [(p['method'], p['amount']) for p in x['refund_order']['payments']] == [('exchange', -100.0)]
    assert mgr.post('/api/pos/exchange', req).get_json()['duplicate']
    summ = mgr.get('/api/pos/shift/current').get_json()['summary']
    assert summ['exchange_count'] == 1 and summ['expected_cash'] == 300.0 and summ['sale_count'] == 2 and summ['refund_count'] == 1


def test_exchange_cheaper_refunds_difference(mgr, dbc):
    d = mgr.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 1}], [{'method': 'wechat', 'amount': 400}])).get_json()
    line = d['order']['items'][0]['id']
    x = mgr.post('/api/pos/exchange', {'order_no': d['order_no'], 'back_items': [{'line_id': line, 'qty': 1}],
                                       'new_items': [{'code': 'P2', 'qty': 1}], 'payments': [], 'client_id': 'ex-2'}).get_json()
    assert x['ok'] and x['diff'] == -300.0
    assert sorted((p['method'], p['amount']) for p in x['refund_order']['payments']) == [('exchange', -100.0), ('wechat', -300.0)]
    assert mgr.get('/api/pos/orders/' + d['order_no']).get_json()['order']['status'] == '已退货'


def test_ac016_history_uses_snapshot_after_price_change(mgr, dbc):
    """AC-016（收银侧）：成交后改商品价格/成本、加特价活动，历史订单展示与退款金额仍按成交快照。"""
    d = mgr.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 2}], [{'method': 'cash', 'amount': 760}], member_code='M001')).get_json()
    assert d['payable'] == 760.0
    dbc.execute("UPDATE products SET retail=999, member_price=888, cost=1 WHERE code='P1' AND merchant_id=1")
    dbc.execute("INSERT INTO promotions(merchant_id, name, product_code, promo_price, start_date, end_date, store_code, status, created_at) "
                "VALUES(1, '新特价', 'P1', 100, '2000-01-01', '2999-12-31', '', '启用', '2026-01-01')")
    o = mgr.get('/api/pos/orders/' + d['order_no']).get_json()['order']
    assert (o['items'][0]['price'], o['items'][0]['retail'], o['payable']) == (380.0, 400.0, 760.0)
    assert o['snapshot']['engine'] and 'at' in o['snapshot']
    rd = refund(mgr, d['order_no'], [{'line_id': o['items'][0]['id'], 'qty': 1}]).get_json()
    assert rd['refund_amount'] == 380.0
    assert dbc.execute('SELECT cost_cents FROM order_items WHERE order_no=?', (rd['order_no'],)).fetchone()[0] == -30000
