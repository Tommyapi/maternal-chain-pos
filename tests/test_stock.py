# -*- coding: utf-8 -*-
"""模块①：库存/批次/调拨/盘点/报损报溢/预警/召回/期初（ERP-004、ERP-301~309、BABY-001~004、BABY-101~103、DATA-003、
AC-007、AC-008、AC-009、AC-018）+ 数据范围/跨商户/字段权限。"""
import io
from datetime import date, timedelta

import pytest
from openpyxl import Workbook, load_workbook

from conftest import Client
from shouyin_app import inventory
from shouyin_app.db import connect
from shouyin_app.util import BizError

TODAY = date.today()


def d(n):
    return (TODAY + timedelta(days=n)).strftime('%Y-%m-%d')


def pid_of(dbc, code, mid=1):
    return dbc.execute('SELECT id FROM products WHERE merchant_id=? AND code=?', (mid, code)).fetchone()[0]


class DB(object):
    """测试里直接调用库存服务（模拟收银出库），自带事务。"""

    def __init__(self, app):
        self.db = connect(app.config['DB_PATH'])

    def __enter__(self):
        self.db.execute('BEGIN IMMEDIATE')
        return self.db

    def __exit__(self, et, ev, tb):
        self.db.execute('COMMIT' if et is None else 'ROLLBACK')
        self.db.close()
        return False


def qty(app, wh, pid, batch_id=None, quality='ok'):
    db = connect(app.config['DB_PATH'])
    try:
        if batch_id is None:
            return db.execute('SELECT COALESCE(SUM(qty),0) FROM inv_stock WHERE merchant_id=1 AND wh_code=? AND product_id=? AND quality=?',
                              (wh, pid, quality)).fetchone()[0]
        return inventory.qty_of(db, 1, wh, pid, batch_id, quality)
    finally:
        db.close()


def ledger_ok(app, wh, pid):
    """库存余额 = 流水合计（每个批次/质量状态都成立）。"""
    db = connect(app.config['DB_PATH'])
    try:
        for r in db.execute('SELECT batch_id, quality, qty FROM inv_stock WHERE merchant_id=1 AND wh_code=? AND product_id=?', (wh, pid)):
            s = db.execute('SELECT COALESCE(SUM(change),0) FROM stock_moves WHERE merchant_id=1 AND wh_code=? AND product_id=? AND batch_id=? AND quality=?',
                           (wh, pid, r['batch_id'], r['quality'])).fetchone()[0]
            assert s == r['qty'], (r['batch_id'], r['quality'], s, r['qty'])
    finally:
        db.close()


@pytest.fixture()
def batchy(admin, dbc):
    """P1 设为批次商品（1 箱 = 6 听）+ 供应商 + A001 收货两个批次。返回 (p1, sid, {batch_no: batch_id})。"""
    p1 = pid_of(dbc, 'P1')
    assert admin.post('/api/goods', {'id': p1, 'batch_managed': 1, 'units': [{'unit': '箱', 'rate': 6}]}).status_code == 200
    sid = admin.post('/api/purchase/suppliers', {'name': '供应商甲'}).get_json()['data']['id']
    ids = {}
    for bno, exp in (('BA', d(20)), ('BB', d(400))):
        r = admin.post('/api/purchase/receipts', {'supplier_id': sid, 'wh_code': 'A001', 'lines': [
            {'product_id': p1, 'unit': '箱', 'qty': 1, 'price': 1800, 'batch_no': bno, 'expiry_date': exp}]})
        assert r.status_code == 200, r.get_json()
        ids[bno] = r.get_json()['data']['lines'][0]['batch_id']
    return p1, sid, ids


def test_balance_categories_and_cost_field(app, admin, batchy, dbc):
    p1, sid, ids = batchy
    rows = admin.get('/api/stock?wh=A001&product_id=%d' % p1).get_json()['data']['items']
    assert rows[0]['book'] == 22 and rows[0]['sellable'] == 22 and rows[0]['cost'] is not None
    assert admin.post('/api/stock/batches/%d/lock' % ids['BA'], {'reason': '质检'}).status_code == 200
    r = admin.get('/api/stock?wh=A001&product_id=%d' % p1).get_json()['data']['items'][0]
    assert (r['book'], r['locked'], r['sellable']) == (22, 6, 16)
    byb = admin.get('/api/stock?wh=A001&product_id=%d&by_batch=1' % p1).get_json()['data']['items']
    assert {x.get('batch_no', '') for x in byb} == {'', 'BA', 'BB'}
    c = Client(app, 'shouyin01')
    r = c.get('/api/stock?wh=A001&product_id=%d' % p1).get_json()['data']['items'][0]
    assert r['cost'] is None and r['amount'] is None
    assert c.get('/api/stock/export').status_code == 403
    mv = c.get('/api/stock/moves?wh=A001&product_id=%d' % p1).get_json()['data']['items']
    assert mv and all(x['cost'] is None for x in mv) and {'qty_before', 'qty_after', 'ref_no', 'batch_no', 'operator'} <= set(mv[0])
    r = admin.get('/api/stock/export')
    assert r.status_code == 200 and '成本价' in list(load_workbook(io.BytesIO(r.data)).worksheets[0].iter_rows(values_only=True))[0]


def test_ac008_expired_locked_batch_cannot_stock_out(app, admin, batchy, dbc):
    p1, sid, ids = batchy
    with DB(app) as db:
        inventory.stock_in(db, {'username': 't'}, 1, 'A001', [{'product_id': p1, 'base_qty': 3, 'batch_no': 'OLD', 'expiry_date': d(-1)}], 'opening', 'T-OLD')
        old = db.execute("SELECT id FROM inv_batches WHERE batch_no='OLD'").fetchone()[0]
    admin.post('/api/stock/batches/%d/lock' % ids['BA'], {'reason': '疑似问题'})
    with DB(app) as db:
        for bid, word in ((old, '已过期'), (ids['BA'], '已锁定')):
            with pytest.raises(BizError) as e:
                inventory.stock_out(db, {'username': 't'}, 1, 'A001', [{'product_id': p1, 'base_qty': 1, 'batch_id': bid}], 'sale', 'S-X')
            assert e.value.code == 'BATCH_UNSELLABLE' and word in e.value.msg
        # 不指定批次：FEFO 跳过过期/锁定批次，直接用 BB
        a = inventory.stock_out(db, {'username': 't'}, 1, 'A001', [{'product_id': p1, 'base_qty': 2}], 'sale', 'S-Y')
        assert [x['batch_no'] for x in a[0]] == ['BB']
        assert [b['batch_no'] for b in inventory.sellable_batches(db, 1, 'A001', p1)] == ['BB']
    # 锁定批次也不能调出（ERP-309）
    admin.post('/api/stock/warehouses', {'code': 'HQ01', 'name': '总部仓', 'kind': 'hq'})
    r = admin.post('/api/stock/transfers', {'from_wh': 'A001', 'to_wh': 'HQ01', 'submit': 1, 'lines': [{'product_id': p1, 'qty': 1, 'batch_id': ids['BA']}]})
    no = r.get_json()['data']['tf_no']
    Client(app, 'shangpin').post('/api/stock/transfers/%s/approve' % no)
    r = admin.post('/api/stock/transfers/%s/ship' % no)
    assert r.status_code == 409 and r.get_json()['code'] == 'BATCH_UNSELLABLE'


def test_batch_unlock_needs_approval(app, admin, batchy):
    p1, sid, ids = batchy
    sp = Client(app, 'shangpin')
    assert sp.post('/api/stock/batches/%d/lock' % ids['BB']).status_code == 400            # 原因必填
    assert sp.post('/api/stock/batches/%d/lock' % ids['BB'], {'reason': '客诉'}).status_code == 200
    assert Client(app, 'dianzhang').post('/api/stock/batches/%d/unlock' % ids['BB'], {'reason': 'x'}).status_code == 403
    r = sp.post('/api/stock/batches/%d/unlock' % ids['BB'], {'reason': '检测合格'})
    apno = r.get_json()['data']['approval_no']
    assert sp.get('/api/stock/batches?batch_no=BB').get_json()['data']['items'][0]['status'] == 'locked'
    assert sp.post('/api/approvals/%s/approve' % apno).status_code == 403                  # 不能批自己的
    assert admin.post('/api/approvals/%s/approve' % apno).status_code == 200
    assert sp.get('/api/stock/batches?batch_no=BB').get_json()['data']['items'][0]['status'] == 'normal'


def test_ac009_batch_trace_purchase_and_sales_masked(app, admin, batchy, dbc):
    p1, sid, ids = batchy
    with DB(app) as db:
        inventory.stock_out(db, {'username': 'shouyin01'}, 1, 'A001', [{'product_id': p1, 'base_qty': 2, 'batch_id': ids['BB']}], 'sale', 'S-TRACE-1')
        db.execute("INSERT INTO orders(merchant_id, order_no, biz_type, store_code, member_code, member_phone, member_name, status, created_at) "
                   "VALUES(1, 'S-TRACE-1', 'sale', 'A001', 'M001', '13800000001', '张妈妈', '已完成', '2026-11-05 10:00:00')")
    t = admin.get('/api/stock/batches/%d/trace' % ids['BB']).get_json()['data']
    assert t['purchases'][0]['supplier'] == '供应商甲' and t['purchases'][0]['qty'] == 6
    assert t['sales'][0]['order_no'] == 'S-TRACE-1' and t['sales'][0]['qty'] == 2 and t['sales'][0]['member_phone'] == '13800000001'
    assert t['distribution'] == [{'wh_code': 'A001', 'quality': 'ok', 'qty': 4}]
    t = Client(app, 'shangpin').get('/api/stock/batches/%d/trace' % ids['BB']).get_json()['data']
    assert t['sales'][0]['member_phone'] == '138****0001' and t['purchases'][0]['price'] == 1800
    t = Client(app, 'dianzhang').get('/api/stock/batches/%d/trace' % ids['BB']).get_json()['data']
    assert t['sales'][0]['member_phone'] == '138****0001' and t['purchases'][0]['price'] is None
    t = Client(app, 'dianzhangB').get('/api/stock/batches/%d/trace' % ids['BB']).get_json()['data']
    assert t['sales'] == [] and t['distribution'] == []                                    # 乙店店长看不到甲店去向
    # BABY-001 批次定位
    r = admin.get('/api/stock/batches?supplier_id=%d&in_stock=1' % sid).get_json()['data']
    assert {x['batch_no'] for x in r['items']} == {'BA', 'BB'}


def test_recall_flow(app, admin, batchy, dbc):
    p1, sid, ids = batchy
    assert Client(app, 'dianzhang').post('/api/stock/recalls', {'title': 'x', 'batch_ids': [ids['BA']]}).status_code == 403
    r = admin.post('/api/stock/recalls', {'title': '厂家召回 BA', 'product_id': p1, 'batch_no': 'BA'})
    assert r.status_code == 200, r.get_json()
    rc = r.get_json()['data']
    assert rc['batches'][0]['status'] == 'recalled' and [e['action'] for e in rc['events']] == ['locate', 'lock']
    with DB(app) as db:
        assert inventory.available(db, 1, 'A001', p1) == 6 + (qty(app, 'A001', p1, 0))
    assert admin.post('/api/stock/batches/%d/unlock' % ids['BA'], {'reason': 'x'}).status_code == 400
    r = admin.post('/api/stock/recalls/%s/events' % rc['rc_no'], {'action': 'return', 'detail': '退回供应商 6 听', 'batch_id': ids['BA']})
    assert r.get_json()['data']['events'][-1]['action'] == 'return'
    # 召回批次可以退回供应商（采购退货指定批次）
    pr = admin.post('/api/purchase/returns', {'supplier_id': sid, 'wh_code': 'A001', 'reason': '召回', 'lines': [
        {'product_id': p1, 'qty': 6, 'batch_id': ids['BA']}]}).get_json()['data']
    assert admin.post('/api/purchase/returns/%s/approve' % pr['pr_no']).get_json()['data']['status'] == 'posted'
    assert qty(app, 'A001', p1, ids['BA']) == 0
    r = admin.get('/api/stock/recalls/%s/export' % rc['rc_no'])
    assert r.status_code == 200
    assert admin.post('/api/stock/recalls/%s/close' % rc['rc_no']).get_json()['data']['status'] == 'closed'
    assert dbc.execute("SELECT COUNT(*) FROM audit_logs WHERE action='导出召回清单' AND level='security'").fetchone()[0] == 1


def test_ac007_request_transfer_partial_receive_diff(app, admin, dbc):
    p2 = pid_of(dbc, 'P2')
    assert admin.post('/api/stock/warehouses', {'code': 'HQ01', 'name': '总部仓', 'kind': 'hq'}).status_code == 200
    sid = admin.post('/api/purchase/suppliers', {'name': '尿裤厂'}).get_json()['data']['id']
    admin.post('/api/purchase/receipts', {'supplier_id': sid, 'wh_code': 'HQ01', 'lines': [{'product_id': p2, 'qty': 50, 'price': 60}]})
    dz = Client(app, 'dianzhang')
    assert dz.get('/api/stock/warehouses').get_json()['data'][0]['code'] == 'A001'          # 店长看不到总部仓
    r = dz.post('/api/stock/requests', {'lines': [{'product_id': p2, 'qty': 12}], 'submit': 1})
    assert r.status_code == 200, r.get_json()
    rq = r.get_json()['data']
    assert rq['from_wh'] == 'HQ01' and rq['to_wh'] == 'A001' and rq['status'] == 'submitted'
    assert dz.post('/api/stock/requests/%s/approve' % rq['tf_no']).status_code == 403
    sp = Client(app, 'shangpin')
    r = sp.post('/api/stock/requests/%s/approve' % rq['tf_no'], {'lines': [{'id': rq['lines'][0]['id'], 'qty': 10}], 'reason': '总部库存紧'})
    tf = r.get_json()['data']['transfer_no']
    t = sp.get('/api/stock/transfers/' + tf).get_json()['data']
    assert t['status'] == 'approved' and t['lines'][0]['req_qty'] == 10 and t['src_no'] == rq['tf_no']
    a_before = qty(app, 'A001', p2)
    assert sp.post('/api/stock/transfers/%s/ship' % tf).get_json()['data']['status'] == 'shipped'
    assert qty(app, 'HQ01', p2) == 40
    bal = dz.get('/api/stock?wh=A001&product_id=%d' % p2).get_json()['data']['items'][0]
    assert bal['in_transit'] == 10 and bal['book'] == a_before
    # 乙店店长不能收甲店的货
    lid = t['lines'][0]['id']
    assert Client(app, 'dianzhangB').post('/api/stock/transfers/%s/receive' % tf, {'lines': [{'id': lid, 'in_base_qty': 1}]}).status_code == 403
    r = dz.post('/api/stock/transfers/%s/receive' % tf, {'lines': [{'id': lid, 'in_base_qty': 7}]})
    t = r.get_json()['data']
    assert t['status'] == 'partial' and t['lines'][0]['in_transit'] == 3
    r = dz.post('/api/stock/transfers/%s/receive' % tf, {'lines': [{'id': lid, 'in_base_qty': 0, 'diff_reason': '少到 3 包'}]})
    t = r.get_json()['data']
    ln = t['lines'][0]
    assert t['status'] == 'received' and (ln['out_base_qty'], ln['in_base_qty'], ln['diff_base_qty']) == (10, 7, 3) and ln['diff_reason'] == '少到 3 包'
    assert qty(app, 'A001', p2) == a_before + 7 and qty(app, 'HQ01', p2) == 40              # 差异不平账
    assert dz.get('/api/stock?wh=A001&product_id=%d' % p2).get_json()['data']['items'][0]['in_transit'] == 0
    assert sp.post('/api/stock/transfers/%s/close' % tf, {'lines': []}).status_code == 400  # 差异必须选处理方式
    r = sp.post('/api/stock/transfers/%s/close' % tf, {'lines': [{'id': lid, 'settle': 'return'}], 'reason': '查明在总部未装车'})
    assert r.get_json()['data']['status'] == 'closed' and qty(app, 'HQ01', p2) == 43
    for wh in ('A001', 'HQ01'):
        ledger_ok(app, wh, p2)


def test_ac018_dynamic_stocktake_with_sales(app, admin, dbc):
    p1, p2 = pid_of(dbc, 'P1'), pid_of(dbc, 'P2')
    dz = Client(app, 'dianzhang')
    r = dz.post('/api/stock/stocktakes', {'wh_code': 'A001', 'scope': 'dynamic'})
    assert r.status_code == 200, r.get_json()
    st = r.get_json()['data']
    book = dict((x['product_id'], x['book_qty']) for x in st['lines'])
    assert book[p1] == 10 and book[p2] == 20
    with DB(app) as db:                                           # 盘点期间照常卖：P1 卖 2
        inventory.stock_out(db, {'username': 'shouyin01'}, 1, 'A001', [{'product_id': p1, 'base_qty': 2}], 'sale', 'S-ST-1')
    # 实盘：P1 数到 8（= 10 − 2，无差异），P2 数到 19（真实盘亏 1）
    dz.post('/api/stock/stocktakes/%s/lines' % st['st_no'], {'lines': [{'code': '6900000000011', 'qty': 8}, {'product_id': p2, 'qty': 19}]})
    with DB(app) as db:                                           # 数完之后又卖：P1 卖 1、P2 卖 5
        inventory.stock_out(db, {'username': 'shouyin01'}, 1, 'A001', [{'product_id': p1, 'base_qty': 1}, {'product_id': p2, 'base_qty': 5}], 'sale', 'S-ST-2')
    assert dz.post('/api/stock/stocktakes/%s/submit' % st['st_no']).status_code == 200
    assert qty(app, 'A001', p1) == 7                              # 审核前不调整
    # 店长有 stock.take.approve，但不能审核时库存仍以流水为准
    r = admin.post('/api/stock/stocktakes/%s/approve' % st['st_no'])
    st = r.get_json()['data']
    ln = dict((x['product_id'], x) for x in st['lines'])
    assert (ln[p1]['moved_qty'], ln[p1]['diff_qty']) == (-2, 0)
    assert (ln[p2]['moved_qty'], ln[p2]['diff_qty']) == (0, -1)
    assert qty(app, 'A001', p1) == 7 and qty(app, 'A001', p2) == 14
    for p in (p1, p2):
        ledger_ok(app, 'A001', p)
    assert dbc.execute("SELECT change FROM stock_moves WHERE ref_type='stocktake' AND ref_no=?", (st['st_no'],)).fetchall()[0][0] == -1


def test_stocktake_spot_reject_and_uncounted(app, admin, dbc):
    p1 = pid_of(dbc, 'P1')
    dz = Client(app, 'dianzhang')
    st = dz.post('/api/stock/stocktakes', {'wh_code': 'A001', 'scope': 'spot', 'scope_value': 'P1'}).get_json()['data']
    assert [x['product_id'] for x in st['lines']] == [p1]
    assert dz.post('/api/stock/stocktakes/%s/lines' % st['st_no'], {'lines': [{'code': 'P2', 'qty': 1}]}).status_code == 400
    assert dz.post('/api/stock/stocktakes/%s/submit' % st['st_no']).status_code == 400
    dz.post('/api/stock/stocktakes/%s/lines' % st['st_no'], {'lines': [{'code': 'P1', 'qty': 12}]})
    dz.post('/api/stock/stocktakes/%s/submit' % st['st_no'])
    assert admin.post('/api/stock/stocktakes/%s/approve' % st['st_no'], {'reject': 1}).status_code == 400
    assert admin.post('/api/stock/stocktakes/%s/approve' % st['st_no'], {'reject': 1, 'reason': '复盘'}).get_json()['data']['status'] == 'counting'
    assert qty(app, 'A001', p1) == 10
    assert Client(app, 'dianzhangB').get('/api/stock/stocktakes/' + st['st_no']).status_code == 403


def test_loss_overflow_via_approval_and_split_assemble_quality(app, admin, dbc):
    p1, p2 = pid_of(dbc, 'P1'), pid_of(dbc, 'P2')
    sp = Client(app, 'shangpin')
    r = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'loss', 'reason': '破损', 'lines': [{'product_id': p2, 'qty': 2}]})
    assert r.status_code == 200, r.get_json()
    adj = r.get_json()['data']
    assert adj['status'] == 'submitted' and adj['approval_no'] and qty(app, 'A001', p2) == 20
    assert admin.post('/api/approvals/%s/approve' % adj['approval_no']).status_code == 200
    assert sp.get('/api/stock/adjusts/' + adj['adj_no']).get_json()['data']['status'] == 'posted'
    assert qty(app, 'A001', p2) == 18
    # 驳回：不过账
    adj = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'overflow', 'reason': '盘点外多出', 'lines': [{'product_id': p2, 'qty': 1}]}).get_json()['data']
    admin.post('/api/approvals/%s/reject' % adj['approval_no'], {'reason': '先复核'})
    assert sp.get('/api/stock/adjusts/' + adj['adj_no']).get_json()['data']['status'] == 'rejected' and qty(app, 'A001', p2) == 18
    # 组装 / 拆零（礼盒 self 模式 + BOM：1 盒 = 1 听 P1 + 2 包 P2）
    r = admin.post('/api/goods', {'code': 'GIFT', 'name': '礼盒', 'base_unit': '盒', 'combo_mode': 'self',
                                  'bom': [{'component_id': p1, 'qty': 1}, {'component_id': p2, 'qty': 2}]})
    assert r.status_code == 200, r.get_json()
    g = r.get_json()['data']
    assert g['combo_mode'] == 'self'
    adj = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'assemble', 'reason': '做礼盒', 'lines': [{'product_id': g['id'], 'qty': 3}]}).get_json()['data']
    admin.post('/api/approvals/%s/approve' % adj['approval_no'])
    assert (qty(app, 'A001', g['id']), qty(app, 'A001', p1), qty(app, 'A001', p2)) == (3, 7, 12)
    adj = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'split', 'reason': '拆散卖', 'lines': [{'product_id': g['id'], 'qty': 1}]}).get_json()['data']
    admin.post('/api/approvals/%s/approve' % adj['approval_no'])
    assert (qty(app, 'A001', g['id']), qty(app, 'A001', p1), qty(app, 'A001', p2)) == (2, 8, 14)
    # 质量转移：合格 -> 残次（残次不计可售）
    adj = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'quality', 'reason': '包装破', 'lines': [
        {'product_id': p2, 'qty': 1, 'quality': 'ok', 'to_quality': 'defect'}]}).get_json()['data']
    admin.post('/api/approvals/%s/approve' % adj['approval_no'])
    assert qty(app, 'A001', p2) == 13 and qty(app, 'A001', p2, quality='defect') == 1
    bal = admin.get('/api/stock?wh=A001&product_id=%d' % p2).get_json()['data']['items'][0]
    assert (bal['book'], bal['sellable'], bal['defect']) == (14, 13, 1)
    for p in (p1, p2, g['id']):
        ledger_ok(app, 'A001', p)
    # 报损超库存拒绝
    r = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'loss', 'reason': 'x', 'lines': [{'product_id': p2, 'qty': 999}]})
    assert r.status_code == 409


def test_alerts_levels(app, admin, batchy, dbc):
    p1, sid, ids = batchy
    p2 = pid_of(dbc, 'P2')
    with DB(app) as db:
        inventory.stock_in(db, {'username': 't'}, 1, 'A001', [{'product_id': p1, 'base_qty': 2, 'batch_no': 'EXP', 'expiry_date': d(-3)}], 'opening', 'T-E')
        inventory.stock_out(db, {'username': 't'}, 1, 'A001', [{'product_id': p2, 'base_qty': 20}], 'sale', 'S-AL')
    admin.post('/api/stock/alert-rules', {'target_type': 'merchant', 'near_expiry_days': '30,60,90', 'safety_qty': 5})
    a = admin.get('/api/stock/alerts?wh=A001').get_json()['data']
    ne = [x for x in a['near_expiry'] if x['batch_no'] == 'BA']
    assert ne and ne[0]['level'] == 30 and ne[0]['days_left'] == 20
    assert any(x['batch_no'] == 'EXP' for x in a['expired'])
    assert any(x['product_id'] == p2 for x in a['out'])
    with DB(app) as db:
        from shouyin_app.views_stock import scan_alerts
        n = scan_alerts(db, 1)
        assert n > 0 and scan_alerts(db, 1) == 0                  # 同一天去重


def test_opening_import(app, admin, dbc):
    p1 = pid_of(dbc, 'P1')
    admin.post('/api/goods', {'id': p1, 'units': [{'unit': '箱', 'rate': 6}]})
    wb = Workbook()
    ws = wb.active
    ws.append(['仓库编码*', '商品编码或条码*', '数量*', '单位', '批号', '生产日期', '有效期', '成本价'])
    ws.append(['A001', 'P1', 2, '箱', '', '', '', 1800])
    ws.append(['A001', 'NOPE', 1, '', '', '', '', ''])
    ws.append(['Z001', 'P1', 1, '', '', '', '', ''])
    ws.append(['A001', 'P2', 1, '盒', '', '', '', ''])
    b = io.BytesIO()
    wb.save(b)
    b.seek(0)
    before = qty(app, 'A001', p1)
    r = admin.c.post('/api/stock/opening/import', data={'file': (b, 'o.xlsx')}, content_type='multipart/form-data')
    d_ = r.get_json()['data']
    assert d_['created'] == 1 and d_['error_count'] == 3 and len(d_['docs']) == 1, d_
    assert qty(app, 'A001', p1) == before + 12
    gr = dbc.execute('SELECT kind, status FROM goods_receipts WHERE gr_no=?', (d_['docs'][0],)).fetchone()
    assert tuple(gr) == ('opening', 'posted')
    assert dbc.execute("SELECT COUNT(*) FROM stock_moves WHERE ref_type='opening' AND ref_no=?", (d_['docs'][0],)).fetchone()[0] == 1
    assert admin.get('/api/stock/opening/template').status_code == 200


def test_scope_and_cross_merchant(app, admin, m2admin, batchy, dbc):
    p1, sid, ids = batchy
    quyu = Client(app, 'quyu')
    assert quyu.get('/api/stock?wh=A001').status_code == 200
    assert quyu.get('/api/stock?wh=B001').status_code == 403
    assert {x['wh_code'] for x in quyu.get('/api/stock').get_json()['data']['items']} == {'A001'}
    assert Client(app, 'dianzhangB').get('/api/stock/moves?wh=A001').status_code == 403
    # 别家商户：仓库/批次/追溯/单据都拿不到
    assert m2admin.get('/api/stock?wh=A001').status_code == 404
    assert m2admin.get('/api/stock/batches/%d/trace' % ids['BA']).status_code == 404
    assert m2admin.post('/api/stock/batches/%d/lock' % ids['BA'], {'reason': 'x'}).status_code == 404
    assert m2admin.get('/api/stock/batches').get_json()['data']['total'] == 0
    assert m2admin.get('/api/stock').get_json()['data']['items'] == []
    st = admin.post('/api/stock/stocktakes', {'wh_code': 'A001', 'scope': 'full'}).get_json()['data']
    assert m2admin.get('/api/stock/stocktakes/' + st['st_no']).status_code == 404
    assert m2admin.post('/api/stock/stocktakes/%s/lines' % st['st_no'], {'lines': []}).status_code == 404
    assert m2admin.post('/api/stock/transfers', {'from_wh': 'A001', 'to_wh': 'B001', 'lines': [{'product_id': p1, 'qty': 1}]}).status_code == 404
    assert m2admin.get('/api/stock/warehouses').get_json()['data'] == []
    # 无权限码
    assert Client(app, 'shouyin01').post('/api/stock/adjusts', {}).status_code == 403
    assert Client(app, 'zhidu').post('/api/stock/stocktakes', {}).status_code == 403


def test_reject_and_cancel_write_back_document_status(app, admin, dbc, batchy):
    """REPORT-1 §7-3（集成轮）：审批驳回/撤回在同一事务里回调，单据状态写回库（不再只是读取时推导）。"""
    p2 = pid_of(dbc, 'P2')
    sp = Client(app, 'shangpin')
    st = lambda no: dbc.execute('SELECT status FROM stock_adjusts WHERE adj_no=?', (no,)).fetchone()[0]
    # 驳回：库里 rejected，不过账
    adj = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'loss', 'reason': '破损', 'lines': [{'product_id': p2, 'qty': 1}]}).get_json()['data']
    assert admin.post('/api/approvals/%s/reject' % adj['approval_no'], {'reason': '先复核'}).status_code == 200
    assert st(adj['adj_no']) == 'rejected' and qty(app, 'A001', p2) == 20
    # 撤回（申请人自己）：库里 cancelled
    adj = sp.post('/api/stock/adjusts', {'wh_code': 'A001', 'kind': 'adjust', 'reason': '账实不符', 'lines': [{'product_id': p2, 'qty': 1}]}).get_json()['data']
    assert sp.post('/api/approvals/%s/cancel' % adj['approval_no']).status_code == 200
    assert st(adj['adj_no']) == 'cancelled' and sp.get('/api/stock/adjusts/' + adj['adj_no']).get_json()['data']['status'] == 'cancelled'
    # 撤回后不能再批（审批单已撤回 409），库存不动
    assert admin.post('/api/approvals/%s/approve' % adj['approval_no']).status_code == 409
    assert qty(app, 'A001', p2) == 20
    # 批次解锁被驳回：批次保持锁定，留审计
    p1, sid, ids = batchy
    assert sp.post('/api/stock/batches/%d/lock' % ids['BB'], {'reason': '客诉'}).status_code == 200
    apno = sp.post('/api/stock/batches/%d/unlock' % ids['BB'], {'reason': '检测合格'}).get_json()['data']['approval_no']
    assert admin.post('/api/approvals/%s/reject' % apno, {'reason': '报告未到'}).status_code == 200
    assert sp.get('/api/stock/batches?batch_no=BB').get_json()['data']['items'][0]['status'] == 'locked'
    acts = [r[0] for r in dbc.execute("SELECT action FROM audit_logs WHERE module='stock' ORDER BY id")]
    assert '报损被驳回' in acts and '库存调整已撤回' in acts and '批次解锁被驳回' in acts
