# -*- coding: utf-8 -*-
"""模块①：供应商与采购（ERP-201~206）+ 一箱六罐入库出库（AC-010 库存侧）+ 越权。"""
from conftest import Client
from shouyin_app import inventory
from shouyin_app.db import connect
from shouyin_app.money import yuan


def pid_of(dbc, code, mid=1):
    return dbc.execute('SELECT id FROM products WHERE merchant_id=? AND code=?', (mid, code)).fetchone()[0]


def setup_box(admin, dbc):
    """P1 设为批次商品，加 1 箱 = 6 听；建供应商。"""
    p1 = pid_of(dbc, 'P1')
    r = admin.post('/api/goods', {'id': p1, 'batch_managed': 1, 'shelf_life_days': 720, 'units': [{'unit': '箱', 'rate': 6}]})
    assert r.status_code == 200, r.get_json()
    r = admin.post('/api/purchase/suppliers', {'name': '奶粉一级代理', 'contact': '王经理', 'phone': '13700000000', 'settle_info': '月结30天'})
    assert r.status_code == 200
    return p1, r.get_json()['data']['id']


def make_po(c, sid, p1, wh='A001', qty=3, submit=True):
    r = c.post('/api/purchase/orders', {'supplier_id': sid, 'wh_code': wh, 'expect_date': '2026-12-01', 'submit': submit,
                                        'lines': [{'product_id': p1, 'unit': '箱', 'qty': qty, 'price': 1800}]})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['data']


def avail(app, wh, pid):
    db = connect(app.config['DB_PATH'])
    try:
        return inventory.available(db, 1, wh, pid), inventory.qty_of(db, 1, wh, pid, 0, 'ok')
    finally:
        db.close()


def test_po_flow_partial_multi_receipt_and_box_units(app, admin, dbc):
    p1, sid = setup_box(admin, dbc)
    sp = Client(app, 'shangpin')
    po = make_po(sp, sid, p1)
    assert po['status'] == 'submitted' and po['total'] == 5400
    # 不能审核自己没权限的（店长没有 purchase.approve）
    assert Client(app, 'dianzhang').post('/api/purchase/orders/%s/approve' % po['po_no']).status_code == 403
    r = admin.post('/api/purchase/orders/%s/approve' % po['po_no'])
    assert r.get_json()['data']['status'] == 'approved'
    line = po['lines'][0]
    base_before = avail(app, 'A001', p1)[0]
    # 第一次：到 1 箱（批次必填）
    r = sp.post('/api/purchase/receipts', {'po_no': po['po_no'], 'lines': [{'po_line_id': line['id'], 'qty': 1}]})
    assert r.status_code == 400 and '批号' in r.get_json()['msg']
    r = sp.post('/api/purchase/receipts', {'po_no': po['po_no'], 'lines': [{'po_line_id': line['id'], 'qty': 1, 'batch_no': 'B1',
                                                                             'prod_date': '2030-01-01', 'expiry_date': '2031-01-01'}]})
    assert r.status_code == 400 and '晚于今天' in r.get_json()['msg']
    r = sp.post('/api/purchase/receipts', {'po_no': po['po_no'], 'lines': [{'po_line_id': line['id'], 'qty': 1, 'batch_no': 'B1',
                                                                             'prod_date': '2026-10-01'}]})
    assert r.status_code == 200, r.get_json()
    gr1 = r.get_json()['data']
    assert gr1['lines'][0]['base_qty'] == 6 and gr1['lines'][0]['expiry_date'] == '2028-09-20'      # 生产日期 + 保质期 720 天
    assert admin.get('/api/purchase/orders/' + po['po_no']).get_json()['data']['status'] == 'partial'
    # 第二次：到 1 箱、拒收 1 箱（必须写原因）
    r = sp.post('/api/purchase/receipts', {'po_no': po['po_no'], 'lines': [{'po_line_id': line['id'], 'qty': 1, 'rejected_qty': 1,
                                                                             'batch_no': 'B2', 'expiry_date': '2028-12-31'}]})
    assert r.status_code == 400 and '原因' in r.get_json()['msg']
    r = sp.post('/api/purchase/receipts', {'po_no': po['po_no'], 'lines': [{'po_line_id': line['id'], 'qty': 1, 'rejected_qty': 1,
                                                                             'diff_reason': '外箱破损', 'batch_no': 'B2', 'expiry_date': '2028-12-31'}]})
    assert r.status_code == 200, r.get_json()
    d = admin.get('/api/purchase/orders/' + po['po_no']).get_json()['data']
    assert d['status'] == 'received' and d['lines'][0]['received_base_qty'] == 12 and d['lines'][0]['rejected_base_qty'] == 6
    # 超收被拒
    r = sp.post('/api/purchase/receipts', {'po_no': po['po_no'], 'lines': [{'po_line_id': line['id'], 'qty': 1, 'batch_no': 'B3',
                                                                             'expiry_date': '2028-12-31'}]})
    assert r.status_code == 409
    assert avail(app, 'A001', p1)[0] == base_before + 12
    # AC-010 库存侧：整箱出 1 箱 = 6，单罐出 1 = 1；FEFO 先出 B1
    db = connect(app.config['DB_PATH'])
    try:
        db.execute('BEGIN IMMEDIATE')
        a1 = inventory.stock_out(db, {'username': 't'}, 1, 'A001', [
            {'product_id': p1, 'base_qty': inventory.to_base_qty(1, 6), 'unit': '箱', 'unit_qty': 1, 'rate': 6},
            {'product_id': p1, 'base_qty': inventory.to_base_qty(1, 1), 'unit': '听', 'unit_qty': 1, 'rate': 1}], 'sale', 'S-T1')
        db.execute('COMMIT')
        assert a1[0] == [{'batch_id': a1[0][0]['batch_id'], 'batch_no': 'B1', 'expiry_date': '2028-09-20', 'qty': 6}]
        assert a1[1][0]['batch_no'] == 'B2' and a1[1][0]['qty'] == 1
        mv = db.execute("SELECT unit, unit_qty, rate, change FROM stock_moves WHERE ref_no='S-T1' ORDER BY id").fetchall()
        assert [tuple(x) for x in mv] == [('箱', 1, 6, -6), ('听', 1, 1, -1)]
    finally:
        db.close()
    assert avail(app, 'A001', p1)[0] == base_before + 5
    # 采购价历史 + 最新成本（1800/6 = 300）
    prices = admin.get('/api/purchase/prices?product_id=%d' % p1).get_json()['data']['items']
    assert len(prices) == 2 and prices[0]['price'] == 1800 and prices[0]['rate'] == 6
    assert dbc.execute('SELECT cost_cents FROM products WHERE id=?', (p1,)).fetchone()[0] == 30000


def test_purchase_return_limited_by_receipt(app, admin, dbc):
    p1, sid = setup_box(admin, dbc)
    po = make_po(admin, sid, p1, qty=1)
    admin.post('/api/purchase/orders/%s/approve' % po['po_no'])
    Client(app, 'shangpin').post('/api/purchase/orders/%s/approve' % po['po_no'])
    gr = Client(app, 'shangpin').post('/api/purchase/receipts', {'po_no': po['po_no'], 'lines': [
        {'po_line_id': po['lines'][0]['id'], 'qty': 1, 'batch_no': 'RB', 'expiry_date': '2028-01-01'}]}).get_json()['data']
    bid = gr['lines'][0]['batch_id']
    r = admin.post('/api/purchase/returns', {'gr_no': gr['gr_no'], 'reason': '质量问题', 'lines': [{'product_id': p1, 'qty': 7, 'batch_id': bid}]})
    assert r.status_code == 400 and '超过原收货' in r.get_json()['msg']
    r = admin.post('/api/purchase/returns', {'gr_no': gr['gr_no'], 'reason': '质量问题', 'lines': [{'product_id': p1, 'qty': 2, 'batch_id': bid}]})
    assert r.status_code == 200, r.get_json()
    pr = r.get_json()['data']
    assert pr['status'] == 'submitted' and pr['total'] == 600
    before = avail(app, 'A001', p1)[0]
    r = admin.post('/api/purchase/returns/%s/approve' % pr['pr_no'])
    assert r.get_json()['data']['status'] == 'posted'
    assert avail(app, 'A001', p1)[0] == before - 2
    assert dbc.execute("SELECT COUNT(*) FROM stock_moves WHERE ref_type='purchase_return' AND ref_no=?", (pr['pr_no'],)).fetchone()[0] == 1
    assert admin.post('/api/purchase/returns/%s/approve' % pr['pr_no']).status_code == 409


def test_po_cancel_close_and_supplier_disable(app, admin, dbc):
    p1, sid = setup_box(admin, dbc)
    po = make_po(admin, sid, p1, submit=False)
    assert po['status'] == 'draft'
    r = admin.post('/api/purchase/orders', {'po_no': po['po_no'], 'supplier_id': sid, 'wh_code': 'A001',
                                            'lines': [{'product_id': p1, 'unit': '箱', 'qty': 2, 'price': '1750.5'}]})
    assert r.get_json()['data']['total'] == 3501
    assert admin.post('/api/purchase/orders/%s/cancel' % po['po_no']).get_json()['data']['status'] == 'cancelled'
    assert admin.post('/api/purchase/orders/%s/submit' % po['po_no']).status_code == 409
    po2 = make_po(admin, sid, p1, qty=2)
    Client(app, 'shangpin').post('/api/purchase/orders/%s/approve' % po2['po_no'])
    admin.post('/api/purchase/receipts', {'po_no': po2['po_no'], 'lines': [{'po_line_id': po2['lines'][0]['id'], 'qty': 1, 'batch_no': 'C1',
                                                                             'expiry_date': '2028-01-01'}]})
    assert admin.post('/api/purchase/orders/%s/close' % po2['po_no']).status_code == 400
    assert admin.post('/api/purchase/orders/%s/close' % po2['po_no'], {'reason': '供应商缺货'}).get_json()['data']['status'] == 'closed'
    # 供应商停用：历史保留，不能再下单
    admin.post('/api/purchase/suppliers', {'id': sid, 'name': '奶粉一级代理', 'status': '停用'})
    r = admin.post('/api/purchase/orders', {'supplier_id': sid, 'wh_code': 'A001', 'lines': [{'product_id': p1, 'qty': 1}]})
    assert r.status_code == 400
    assert admin.get('/api/purchase/orders?supplier_id=%d' % sid).get_json()['data']['total'] == 2


def test_purchase_perms_and_cross_merchant(app, admin, m2admin, dbc):
    p1, sid = setup_box(admin, dbc)
    po = make_po(admin, sid, p1)
    for u in ('shouyin01', 'dianzhang', 'zhidu'):
        c = Client(app, u)
        assert c.get('/api/purchase/prices').status_code == 403, u
        assert c.post('/api/purchase/orders', {}).status_code == 403, u
    assert Client(app, 'caiwu').get('/api/purchase/prices').status_code == 200
    assert m2admin.get('/api/purchase/orders/' + po['po_no']).status_code == 404
    assert m2admin.get('/api/purchase/orders').get_json()['data']['total'] == 0
    assert m2admin.post('/api/purchase/orders/%s/approve' % po['po_no']).status_code == 404
    assert m2admin.post('/api/purchase/orders', {'supplier_id': sid, 'wh_code': 'A001', 'lines': [{'product_id': p1, 'qty': 1}]}).status_code == 404
    # 店长（门店数据范围）不能往别的门店仓收货
    r = Client(app, 'quyu').get('/api/purchase/orders')
    assert r.status_code == 403
