# -*- coding: utf-8 -*-
"""模块①：商品中心（ERP-101~109、ERP-005、8.4、DATA-001/002）+ 越权。"""
import io

from openpyxl import Workbook, load_workbook

from conftest import Client
from shouyin_app import goods_svc


def pid_of(dbc, code, mid=1):
    return dbc.execute('SELECT id FROM products WHERE merchant_id=? AND code=?', (mid, code)).fetchone()[0]


def new_box_product(admin, **kw):
    body = {'code': 'NF6', 'name': '奶粉二段800g', 'category': '奶粉', 'brand': '金领冠', 'base_unit': '罐', 'retail': 300, 'member_price': 290,
            'cost': 200, 'batch_managed': 1, 'shelf_life_days': 720, 'near_expiry_days': 90, 'spec_attrs': {'段位': '2段'},
            'units': [{'unit': '箱', 'rate': 6, 'retail': 1700}],
            'barcodes': [{'barcode': '6911111111111'}, {'barcode': '16911111111111', 'unit': '箱'}]}
    body.update(kw)
    r = admin.post('/api/goods', body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()['data']


def test_create_product_units_barcodes_box_code(admin, dbc):
    d = new_box_product(admin)
    assert d['retail'] == 300 and d['cost'] == 200 and d['spec_attrs'] == {'段位': '2段'}
    assert [(u['unit'], u['rate']) for u in d['units']] == [('罐', 1), ('箱', 6)]
    f = goods_svc.find_sku(dbc, 1, '16911111111111')
    assert f['unit'] == '箱' and f['rate'] == 6 and f['product']['code'] == 'NF6'
    assert goods_svc.unit_rate(dbc, 1, d['id'], '箱') == 6
    # 拼音首字母搜索
    r = admin.get('/api/goods/search?kw=NFED').get_json()['data']
    assert [x['code'] for x in r] == ['NF6']
    r = admin.get('/api/goods?kw=nfed').get_json()['data']
    assert r['total'] == 1 and r['items'][0]['code'] == 'NF6'
    # 变更记录（ERP-109）
    det = admin.get('/api/goods/%d' % d['id']).get_json()['data']
    assert det['history'][0]['action'] == '新增商品'


def test_barcode_conflict_blocked(admin):
    r = admin.post('/api/goods', {'name': '冲突商品', 'base_unit': '包', 'barcodes': ['6900000000011']})
    assert r.status_code == 400 and '已被商品' in r.get_json()['msg']
    r = admin.post('/api/goods', {'name': '冲突商品', 'base_unit': '包', 'barcodes': ['X1', 'X1']})
    assert r.status_code == 400


def test_invalid_unit_rate(admin):
    r = admin.post('/api/goods', {'name': '坏单位', 'base_unit': '罐', 'units': [{'unit': '箱', 'rate': 1}]})
    assert r.status_code == 400 and '换算率' in r.get_json()['msg']


def test_price_change_audited_and_rate_change_keeps_history(admin, dbc):
    d = new_box_product(admin)
    r = admin.post('/api/goods', {'id': d['id'], 'retail': 320, 'units': [{'unit': '箱', 'rate': 8}]})
    assert r.status_code == 200, r.get_json()
    acts = [x[0] for x in dbc.execute("SELECT action FROM audit_logs WHERE module='goods' AND ref_no='NF6' ORDER BY id")]
    assert '商品调价' in acts and '换算率变更' in acts


def test_bom_combo(admin, dbc):
    p1, p2 = pid_of(dbc, 'P1'), pid_of(dbc, 'P2')
    r = admin.post('/api/goods', {'code': 'GIFT1', 'name': '礼盒', 'base_unit': '盒', 'retail': 450, 'combo_mode': 'components',
                                  'bom': [{'component_id': p1, 'qty': 1}, {'code': 'P2', 'qty': 2}]})
    assert r.status_code == 200, r.get_json()
    gid = r.get_json()['data']['id']
    assert sorted((x['product_id'], x['base_qty']) for x in goods_svc.expand_bom(dbc, 1, gid, 3)) == sorted([(p1, 3), (p2, 6)])
    assert goods_svc.expand_bom(dbc, 1, p1, 2) == [{'product_id': p1, 'base_qty': 2}]
    r = admin.post('/api/goods', {'code': 'GIFT2', 'name': '空礼盒', 'base_unit': '盒', 'combo_mode': 'components', 'bom': []})
    assert r.status_code == 400


def test_disable_not_delete(admin, dbc):
    p1 = pid_of(dbc, 'P1')
    r = admin.post('/api/goods/%d/disable' % p1, {'reason': '厂家停产'})
    assert r.get_json()['data']['status'] == '停售'
    assert dbc.execute('SELECT COUNT(*) FROM products WHERE id=?', (p1,)).fetchone()[0] == 1
    admin.post('/api/goods/%d/disable' % p1, {'enable': 1})
    assert dbc.execute('SELECT status FROM products WHERE id=?', (p1,)).fetchone()[0] == '在售'


def test_listing_store_stop(admin, dbc):
    p1 = pid_of(dbc, 'P1')
    r = admin.post('/api/goods/listing', {'store_codes': ['B001'], 'product_ids': [p1], 'status': '停售'})
    assert r.status_code == 200
    p = dict(dbc.execute('SELECT * FROM products WHERE id=?', (p1,)).fetchone())
    assert goods_svc.sale_status(dbc, 1, 'B001', p) and not goods_svc.sale_status(dbc, 1, 'A001', p)


def test_batch_price_percent(admin, dbc):
    p2 = pid_of(dbc, 'P2')
    r = admin.post('/api/goods/batch-price', {'ids': [p2], 'percent': '10'})
    assert r.status_code == 200
    assert dbc.execute('SELECT retail_cents FROM products WHERE id=?', (p2,)).fetchone()[0] == 11000


def test_cashier_no_cost_no_export_no_edit(app, dbc):
    c = Client(app, 'shouyin01')
    items = c.get('/api/goods?kw=P1').get_json()['data']['items']
    assert items and all(x['cost'] is None for x in items)
    det = c.get('/api/goods/%d' % pid_of(dbc, 'P1')).get_json()['data']
    assert det['cost'] is None and 'purchase_prices' not in det
    assert c.get('/api/goods/export').status_code == 403
    assert c.post('/api/goods', {'name': 'x'}).status_code == 403
    m = Client(app, 'dianzhang')
    assert m.post('/api/goods/batch-price', {'ids': [1], 'percent': '1'}).status_code == 403


def test_price_edit_needs_perm(app, dbc):
    # 把总部商品角色的改价权限去掉：能改名称，不能改价格
    dbc.execute("DELETE FROM role_perms WHERE perm='goods.price.edit' AND role_id=(SELECT id FROM roles WHERE merchant_id=1 AND code='hq_goods')")
    dbc.commit()
    c = Client(app, 'shangpin')
    p1 = pid_of(dbc, 'P1')
    assert c.post('/api/goods', {'id': p1, 'short_name': '一段'}).status_code == 200
    r = c.post('/api/goods', {'id': p1, 'retail': 1})
    assert r.status_code == 403


def test_cross_merchant(app, m2admin, dbc):
    p1 = pid_of(dbc, 'P1')
    assert m2admin.get('/api/goods/%d' % p1).status_code == 404
    items = m2admin.get('/api/goods').get_json()['data']['items']
    assert [x['code'] for x in items] == ['Q1']
    assert m2admin.post('/api/goods', {'id': p1, 'name': '黑'}).status_code == 404
    assert m2admin.post('/api/goods/%d/disable' % p1).status_code == 404
    assert m2admin.get('/api/goods?store=A001').status_code == 403
    # 别家条码可以和 1 号商户重复（商户内唯一）
    r = m2admin.post('/api/goods', {'name': '别家二', 'base_unit': '罐', 'barcodes': ['6900000000011']})
    assert r.status_code == 200


def test_import_partial_success_and_export(admin, dbc):
    wb = Workbook()
    ws = wb.active
    ws.append(['商品编码', '商品名称*', '条码', '基本单位', '大单位', '大单位换算率', '大单位条码', '零售价', '成本价', '批次管理'])
    ws.append(['N1', '新品一', '6922222222221', '罐', '箱', 6, '16922222222221', 99, 50, '是'])
    ws.append(['N2', '新品二', '6922222222221', '罐', '', '', '', 10, '', ''])         # 与上一行条码重复
    ws.append(['N3', '新品三', '6922222222223', '罐', '箱', 1, '', 10, '', ''])         # 无效单位
    ws.append(['N4', '新品四', '6900000000021', '包', '', '', '', 10, '', ''])          # 与已有商品条码冲突
    ws.append(['P2', '纸尿裤L改名', '', '', '', '', '', '', '', ''])                     # 更新已有
    b = io.BytesIO()
    wb.save(b)
    b.seek(0)
    r = admin.c.post('/api/goods/import', data={'file': (b, 'g.xlsx')}, content_type='multipart/form-data')
    d = r.get_json()['data']
    assert d['created'] == 1 and d['updated'] == 1 and d['error_count'] == 3, d
    assert {e['row_no'] for e in d['errors']} == {3, 4, 5}
    assert goods_svc.find_sku(dbc, 1, '16922222222221')['rate'] == 6
    assert dbc.execute("SELECT name, retail_cents FROM products WHERE merchant_id=1 AND code='P2'").fetchone()[:] == ('纸尿裤L改名', 10000)
    r = admin.get('/api/goods/export')
    assert r.status_code == 200
    rows = list(load_workbook(io.BytesIO(r.data)).worksheets[0].iter_rows(values_only=True))
    assert '成本价' in rows[0] and any(x[0] == 'N1' for x in rows)
    assert dbc.execute("SELECT level FROM audit_logs WHERE action='导出商品'").fetchone()[0] == 'security'
    assert admin.get('/api/goods/template').status_code == 200


def test_categories_brands(admin):
    r = admin.post('/api/goods/categories', {'name': '辅食', 'near_expiry_days': 30, 'safety_stock': 5})
    assert r.status_code == 200 and r.get_json()['data']['code'].startswith('C')
    assert any(x['name'] == '辅食' for x in admin.get('/api/goods/categories').get_json()['data'])
    assert admin.post('/api/goods/brands', {'name': '美赞臣'}).status_code == 200
    assert any(x['name'] == '美赞臣' for x in admin.get('/api/goods/brands').get_json()['data'])
