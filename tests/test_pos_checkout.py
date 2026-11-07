# -*- coding: utf-8 -*-
"""收银结算验收：AC-001（批次+会员价+券+组合支付只生效一次）、AC-008（过期/锁定批次禁售）、AC-010（箱/罐）、
超额折扣 NEED_AUTH -> 审批 -> 成功、试算=结算、赠品、优惠明细可还原。"""
import pytest

from conftest import Client
from test_pos_helpers import (add_product, add_unit, add_coupon, stock_in, qty, member, day, body)


@pytest.fixture()
def mgr(app):
    c = Client(app, 'dianzhang')
    c.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00102'})
    return c


@pytest.fixture()
def milk(dbc):
    """批次奶粉 P4：临期批次 2 罐、远期 5 罐、过期 4 罐。"""
    pid = add_product(dbc, 'P4', '奶粉二段', 300, cost=200, member_price=280, barcodes=['6900000000041'], batch_managed=1, spec='2段 800g')
    b = {'near': stock_in(dbc, pid, 'A001', 2, 'B-NEAR', day(10)), 'far': stock_in(dbc, pid, 'A001', 5, 'B-FAR', day(400)),
         'exp': stock_in(dbc, pid, 'A001', 4, 'B-EXP', day(-1))}
    b['pid'] = pid
    return b


def batch_qty(dbc, bid):
    return dbc.execute("SELECT COALESCE(SUM(qty),0) FROM inv_stock WHERE batch_id=? AND quality='ok'", (bid,)).fetchone()[0]


def test_scan_shows_fefo_batches_and_reasons(cashier, milk):
    g = cashier.get('/api/pos/goods/scan?code=6900000000041').get_json()['data']
    assert [b['batch_no'] for b in g['batches']] == ['B-NEAR', 'B-FAR'] and g['recommend_batch_id'] == milk['near']
    assert g['batches'][0]['near'] and '临期' in g['warn']
    assert g['unsellable_batches'][0]['batch_no'] == 'B-EXP' and '已过期' in g['unsellable_batches'][0]['reason']
    assert g['stock'] == 7 and g['sellable']                        # 过期批次不计入可售


def test_ac001_batch_member_coupon_combo_pay_once(mgr, dbc, milk):
    cno = add_coupon(dbc, 'M001', 20, 100)
    req = body([{'code': '6900000000041', 'qty': 2, 'batch_id': milk['far']}],
               [{'method': 'wechat', 'amount': 300, 'ref': 'WX-0001'}, {'method': 'cash', 'amount': 300}],
               member_code='M001', coupon_nos=[cno], client_id='ac001-1', guide_code='G1')
    pv = mgr.post('/api/pos/price', req).get_json()['data']
    r = mgr.post('/api/pos/checkout', req)
    d = r.get_json()
    assert r.status_code == 200, d
    assert pv['payable'] == d['payable'] == 540.0 and d['change'] == 60.0          # 会员价 280×2 − 券 20；试算 = 结算
    o = d['data']['order']
    assert sorted((p['method'], p['amount']) for p in o['payments']) == [('cash', 240.0), ('wechat', 300.0)]
    assert all(p['status'] == 'success' and p['confirm_mode'] == 'manual' and p['pay_no'] for p in o['payments'])
    assert o['items'][0]['batch_alloc'][0]['batch_no'] == 'B-FAR' and o['items'][0]['base_qty'] == 2
    assert batch_qty(dbc, milk['far']) == 3 and batch_qty(dbc, milk['near']) == 2
    kinds = dict((x['kind'], x['amount']) for x in o['discounts'])
    assert kinds == {'member_price': 40.0, 'coupon': 20.0}                           # 优惠明细可还原 零售 600 − 实付 540
    assert sum(x['amount'] for x in o['discounts']) == o['retail_total'] - o['payable']
    pts, bal = member(dbc, 'M001')
    assert pts == 540
    assert dbc.execute('SELECT status, order_no FROM coupons WHERE coupon_no=?', (cno,)).fetchone()[:] == ('已核销', d['order_no'])
    # 网络重试：同一 client_id 再提交 -> 返回同一单，支付/库存/积分/券都不重复
    d2 = mgr.post('/api/pos/checkout', req).get_json()
    assert d2['order_no'] == d['order_no'] and d2['duplicate']
    assert batch_qty(dbc, milk['far']) == 3 and member(dbc, 'M001')[0] == 540
    assert dbc.execute('SELECT COUNT(*) FROM payments WHERE order_no=?', (d['order_no'],)).fetchone()[0] == 2
    assert dbc.execute("SELECT COUNT(*) FROM points_log WHERE order_no=?", (d['order_no'],)).fetchone()[0] == 1
    # 换个 client_id 再用同一张券 -> 拒绝
    req['client_id'] = 'ac001-2'
    assert mgr.post('/api/pos/checkout', req).status_code in (400, 409)
    # 批次追溯：按批次能反查到销售单（POS-204）
    rows = dbc.execute("SELECT order_no FROM order_items WHERE batch_alloc LIKE ?", ('%"batch_id": ' + str(milk['far']) + ',%',)).fetchall()
    assert [r[0] for r in rows] == [d['order_no']]


def test_cashier_fefo_and_cannot_choose_batch(cashier, dbc, milk):
    d = cashier.post('/api/pos/checkout', body([{'code': 'P4', 'qty': 3}], [{'method': 'cash', 'amount': 900}])).get_json()
    assert d['ok'] and [(a['batch_no'], a['qty']) for a in d['order']['items'][0]['batch_alloc']] == [('B-NEAR', 2), ('B-FAR', 1)]
    r = cashier.post('/api/pos/checkout', body([{'code': 'P4', 'qty': 1, 'batch_id': milk['far']}], [{'method': 'cash', 'amount': 300}]))
    assert r.status_code == 403 and '批次' in r.get_json()['msg']


def test_ac008_expired_or_locked_batch_rejected(mgr, cashier, dbc, milk):
    r = mgr.post('/api/pos/checkout', body([{'code': 'P4', 'qty': 1, 'batch_id': milk['exp']}], [{'method': 'cash', 'amount': 300}]))
    assert r.status_code == 409 and r.get_json()['code'] == 'BATCH_UNSELLABLE' and '已过期' in r.get_json()['msg']
    dbc.execute("UPDATE inv_batches SET status='locked', lock_reason='质检不合格' WHERE id=?", (milk['far'],))
    r = mgr.post('/api/pos/checkout', body([{'code': 'P4', 'qty': 1, 'batch_id': milk['far']}], [{'method': 'cash', 'amount': 300}]))
    assert r.status_code == 409 and '已锁定' in r.get_json()['msg']
    # 只剩临期批次 2 罐可售：买 3 罐 -> 库存不足（过期/锁定不计入）
    r = cashier.post('/api/pos/checkout', body([{'code': 'P4', 'qty': 3}], [{'method': 'cash', 'amount': 900}]))
    assert r.status_code == 409 and r.get_json()['code'] == 'STOCK_SHORT'
    dbc.execute("UPDATE inv_batches SET status='recalled' WHERE id=?", (milk['near'],))
    g = cashier.get('/api/pos/goods/scan?code=P4').get_json()['data']
    assert not g['sellable'] and '召回' in g['reason'] and '过期' in g['reason']
    assert qty(dbc, 'P4') == 11                                                       # 全部被拒，库存一件没动


def test_ac010_box_and_can_sale_and_refund(mgr, dbc):
    pid = add_product(dbc, 'P5', '奶粉罐装', 100, cost=60, barcodes=['6900000000051'])
    add_unit(dbc, pid, '箱', 6, barcode='6900000000059')
    stock_in(dbc, pid, 'A001', 12)
    g = mgr.get('/api/pos/goods/scan?code=6900000000059').get_json()['data']
    assert (g['unit'], g['rate'], g['retail'], g['stock'], g['stock_base']) == ('箱', 6, 600.0, 2, 12)
    d = mgr.post('/api/pos/checkout', body([{'code': '6900000000059', 'qty': 1}, {'code': '6900000000051', 'qty': 2}],
                                           [{'method': 'cash', 'amount': 800}])).get_json()
    assert d['ok'] and d['payable'] == 800.0
    it = dict((x['unit'], x) for x in d['order']['items'])
    assert (it['箱']['qty'], it['箱']['unit_rate'], it['箱']['base_qty'], it['箱']['amount']) == (1, 6, 6, 600.0)
    assert (it['罐']['qty'], it['罐']['unit_rate'], it['罐']['base_qty']) == (2, 1, 2)
    assert it['箱']['cost'] is None                                                   # 店长没有成本字段权限
    assert dbc.execute('SELECT cost_cents FROM order_items WHERE id=?', (it['箱']['id'],)).fetchone()[0] == 36000   # 库里成本快照 6 罐 × 60
    assert qty(dbc, 'P5') == 4
    mv = dbc.execute("SELECT unit, unit_qty, rate, change FROM stock_moves WHERE ref_no=? ORDER BY id", (d['order_no'],)).fetchall()
    assert [tuple(x) for x in mv] == [('箱', 1.0, 6, -6), ('罐', 2.0, 1, -2)]
    # 退货：整箱退 1 箱、单罐退 1 罐，基本单位回库正确
    r = mgr.post('/api/pos/refund', {'order_no': d['order_no'], 'items': [{'line_id': it['箱']['id'], 'qty': 1}, {'line_id': it['罐']['id'], 'qty': 1}]})
    rd = r.get_json()
    assert rd['ok'] and rd['refund_amount'] == 700.0 and qty(dbc, 'P5') == 11
    # 基本单位不能是小数
    r = mgr.post('/api/pos/checkout', body([{'code': '6900000000051', 'qty': 0.5}], [{'method': 'cash', 'amount': 50}]))
    assert r.status_code == 400 and '整数' in r.get_json()['msg']


def test_need_auth_then_approval_then_success(app, cashier, dbc):
    req = body([{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 320}], order_discount={'rate': '0.8'})
    r = cashier.post('/api/pos/checkout', req)
    assert r.status_code == 403 and r.get_json()['code'] == 'NEED_AUTH'
    ap = cashier.post('/api/approvals', {'kind': 'discount', 'store_code': 'A001', 'value': '0.8', 'title': '整单 8 折', 'reason': '老顾客',
                                         'request_key': 'disc-1'}).get_json()['data']
    r = cashier.post('/api/pos/checkout', dict(req, approval_no=ap['approval_no']))
    assert r.status_code == 403 and r.get_json()['code'] == 'APPROVAL_NOT_READY'
    mgr = Client(app, 'dianzhang')
    assert mgr.post('/api/approvals/%s/approve' % ap['approval_no'], {'reason': '同意'}).get_json()['ok']
    d = cashier.post('/api/pos/checkout', dict(req, approval_no=ap['approval_no'])).get_json()
    assert d['ok'] and d['payable'] == 320.0 and d['order']['approval_no'] == ap['approval_no']
    # 审批单只能用一次
    r = cashier.post('/api/pos/checkout', dict(req, approval_no=ap['approval_no']))
    assert r.status_code in (403, 409)
    # 店长本人额度内（0.8 >= 0.7）直接通过；低于 0.7 照样要授权
    mgr.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00102'})
    assert mgr.post('/api/pos/checkout', req).get_json()['ok']
    r = mgr.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 240}], order_discount={'rate': '0.6'}))
    assert r.get_json()['code'] == 'NEED_AUTH'
    # 店长 PIN 现场授权：区域经理（额度 0.5）可授权 0.6
    from test_pos_helpers import set_pin
    set_pin(dbc, 'quyu', 'E900', '8888')
    d = cashier.post('/api/pos/checkout', dict(body([{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 240}], order_discount={'rate': '0.6'}),
                                               auth_user='quyu', auth_pin='8888')).get_json()
    assert d['ok'] and d['order']['auth_user'] == 'quyu'
    assert dbc.execute("SELECT COUNT(*) FROM audit_logs WHERE action LIKE '现场授权%' AND level='high'").fetchone()[0] >= 1
    assert dbc.execute("SELECT COUNT(*) FROM notifications WHERE category='risk' AND title LIKE '大额改价%'").fetchone()[0] >= 1


def test_price_change_needs_auth_and_gift_is_price_change(cashier, mgr, dbc):
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1, 'price': 90}], [{'method': 'cash', 'amount': 90}]))
    assert r.status_code == 403 and r.get_json()['data']['kind'] == 'price_change'
    d = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1, 'price': 90}], [{'method': 'cash', 'amount': 90}],
                                               auth_user='dianzhang', auth_pass='123456')).get_json()
    assert d['ok'] and [(x['kind'], x['amount'], x['auth_user']) for x in d['order']['discounts']] == [('price_change', 10.0, 'dianzhang')]
    # 收银员手工标赠品 = 改价为 0，同样要授权；店长可以直接送，赠品出库且保留成本
    r = cashier.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 1, 'is_gift': True}], [{'method': 'cash', 'amount': 400}]))
    assert r.status_code == 403 and r.get_json()['code'] == 'NEED_AUTH'
    d = mgr.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 1, 'is_gift': True}], [{'method': 'cash', 'amount': 400}])).get_json()
    g = [x for x in d['order']['items'] if x['is_gift']][0]
    assert d['payable'] == 400.0 and g['amount'] == 0 and g['cost'] is None
    assert dbc.execute('SELECT cost_cents FROM order_items WHERE id=?', (g['id'],)).fetchone()[0] == 6000
    assert [x['kind'] for x in d['order']['discounts']] == ['gift'] and qty(dbc, 'P2') == 18


def test_item_not_discountable(mgr, dbc):
    dbc.execute("UPDATE products SET allow_discount=0 WHERE code='P2' AND merchant_id=1")
    r = mgr.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1, 'price': 50}], [{'method': 'cash', 'amount': 50}]))
    assert r.status_code == 400 and '不允许' in r.get_json()['msg']


def test_stopped_in_store_cannot_sell(cashier, dbc):
    dbc.execute("INSERT INTO product_listings(merchant_id, store_code, product_id, status) SELECT 1, 'A001', id, '停售' FROM products WHERE code='P2' AND merchant_id=1")
    r = cashier.get('/api/pos/goods/scan?code=P2')
    assert r.status_code == 404 and '停售' in r.get_json()['msg']
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}]))
    assert r.status_code == 400 and '停售' in r.get_json()['msg']


def test_validation_messages(cashier):
    assert '购物车为空' in cashier.post('/api/pos/checkout', {'items': [], 'payments': []}).get_json()['msg']
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 'abc'}], [{'method': 'cash', 'amount': 100}]))
    assert r.status_code == 400 and '数量' in r.get_json()['msg']
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'bitcoin', 'amount': 100}]))
    assert r.status_code == 400 and '不支持的支付方式' in r.get_json()['msg']
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 'x'}]))
    assert r.status_code == 400 and '金额' in r.get_json()['msg']
    r = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], []))
    assert r.status_code == 400 and '支付方式' in r.get_json()['msg']
    # 自定义支付方式名称
    d = cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'other', 'name': '美团券', 'amount': 100}])).get_json()
    assert d['order']['payments'][0]['method_name'] == '美团券'
