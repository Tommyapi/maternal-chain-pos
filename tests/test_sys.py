# -*- coding: utf-8 -*-
"""模块④：系统配置（ERP-901~905、DATA-001/002、上传校验）。"""
import io

from openpyxl import Workbook, load_workbook
from conftest import Client


def j(r):
    return r.get_json()


def xlsx(head, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(head)
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    b.seek(0)
    return b


def test_config_per_merchant_and_audited(admin, m2admin, cashier, manager, dbc):
    d = j(admin.get('/api/sys/config'))['data']
    assert 'round_mode' in d['config'] and d['labels']['round_mode']
    r = j(admin.post('/api/sys/config', {'round_mode': 'yuan', 'allow_negative_stock': True, 'reason': '临时允许店长超卖'}))['data']   # 默认已禁止（POS-109）
    assert r['config']['round_mode'] == 'yuan' and set(r['changed']) == {'round_mode', 'allow_negative_stock'}
    assert admin.post('/api/sys/config', {'hack': 1}).status_code == 400
    # 商户 2 的配置互不影响
    assert j(m2admin.get('/api/sys/config'))['data']['config']['round_mode'] == 'none'
    j(m2admin.post('/api/sys/config', {'round_mode': 'jiao'}))
    assert j(admin.get('/api/sys/config'))['data']['config']['round_mode'] == 'yuan'
    for c in (cashier, manager):
        assert c.get('/api/sys/config').status_code == 403 and c.post('/api/sys/config', {'round_mode': 'none'}).status_code == 403
    a = dbc.execute("SELECT * FROM audit_logs WHERE merchant_id=1 AND action='修改收银参数'").fetchone()
    assert a['level'] == 'high' and '"round_mode": "none"' in a['before_json']


def test_numbering_rule(admin):
    d = j(admin.get('/api/sys/numbering'))['data']
    s = [t for t in d['types'] if t['code'] == 'S'][0]
    assert s['example'].startswith('S001A001') and len(s['example']) == 1 + 3 + 4 + 6 + 5


def test_dicts(admin, cashier, m2admin):
    d = j(cashier.get('/api/sys/dicts?type=refund_reason'))['data']          # 收银台可读
    assert [x['name'] for x in d['items']['refund_reason']][:2] == ['质量问题', '顾客不想要']
    assert cashier.post('/api/sys/dicts', {'dict_type': 'refund_reason', 'name': 'x'}).status_code == 403
    r = j(admin.post('/api/sys/dicts', {'dict_type': 'refund_reason', 'name': '尺码不合适'}))['data']['items']
    new = [x for x in r if x['name'] == '尺码不合适'][0]
    assert admin.post('/api/sys/dicts', {'dict_type': 'refund_reason', 'name': '尺码不合适'}).status_code == 400      # 重名
    j(admin.post('/api/sys/dicts', {'dict_type': 'refund_reason', 'code': new['code'], 'name': '尺码不合适', 'status': '停用'}))
    assert '尺码不合适' not in [x['name'] for x in j(cashier.get('/api/sys/dicts?type=refund_reason'))['data']['items']['refund_reason']]
    assert '尺码不合适' in [x['name'] for x in j(admin.get('/api/sys/dicts?type=refund_reason'))['data']['items']['refund_reason']]
    assert admin.post('/api/sys/dicts', {'dict_type': 'nope', 'name': 'x'}).status_code == 400
    assert admin.get('/api/sys/dicts?type=nope').status_code == 400
    assert '尺码不合适' not in str(j(m2admin.get('/api/sys/dicts'))['data'])
    from shouyin_app.sys_svc import dict_items
    from shouyin_app.db import connect
    db = connect(admin.c.application.config['DB_PATH'])
    assert '尺码不合适' not in [x['name'] for x in dict_items(db, 1, 'refund_reason')]
    db.close()


def test_print_templates_versioned_and_resolved(app, admin, manager, dbc):
    d = j(admin.get('/api/sys/print-templates'))['data']
    assert d['effective']['sale']['source'] == 'builtin' and d['effective']['sale']['content']['title'] == '萌贝母婴连锁'
    r = j(admin.post('/api/sys/print-templates', {'kind': 'sale', 'width': 80, 'content': {'title': '萌贝（总部）', 'footer': '7 天无理由',
                                                                                        'fields': ['store_name', 'order_no', 'hack']}}))['data']
    assert r['version'] == 1 and r['source'] == 'merchant' and r['content']['fields'] == ['store_name', 'order_no']
    r = j(admin.post('/api/sys/print-templates', {'kind': 'sale', 'width': 80, 'content': {'title': '萌贝（总部）v2'}}))['data']
    assert r['version'] == 2
    j(admin.post('/api/sys/print-templates', {'kind': 'sale', 'store_code': 'A001', 'width': 58, 'content': {'title': '甲店专用'}}))
    from shouyin_app.sys_svc import get_print_template
    assert get_print_template(dbc, 1, 'A001', 'sale')['content']['title'] == '甲店专用'
    assert get_print_template(dbc, 1, 'B001', 'sale')['content']['title'] == '萌贝（总部）v2'
    assert get_print_template(dbc, 2, 'Z001', 'sale')['source'] == 'builtin'
    assert admin.post('/api/sys/print-templates', {'kind': 'sale', 'width': 70}).status_code == 400
    assert admin.post('/api/sys/print-templates', {'kind': 'xx'}).status_code == 400
    assert admin.post('/api/sys/print-templates', {'kind': 'sale', 'store_code': 'Z001'}).status_code == 400
    assert manager.get('/api/sys/print-templates').status_code == 403
    assert dbc.execute("SELECT COUNT(*) FROM audit_logs WHERE action='修改打印模板'").fetchone()[0] == 3


def test_alert_params(admin):
    d = j(admin.get('/api/sys/alert-params'))['data']
    assert d['near_expiry_days'] == [30, 60, 90]
    r = j(admin.post('/api/sys/alert-params', {'near_expiry_days': [90, 30], 'slow_moving_days': 45, 'notify_time': '08:30',
                                              'receivers': ['stock.view', 'app.store'],
                                              'overrides': [{'scope': 'category', 'value': '奶粉', 'safety_stock': 6, 'near_expiry_days': 60}]}))['data']
    assert r['near_expiry_days'] == [30, 90] and r['overrides'][0]['safety_stock'] == 6
    for bad in ({'near_expiry_days': [0]}, {'slow_moving_days': 1}, {'notify_time': '25:00'}, {'receivers': ['platform.ops']},
                {'overrides': [{'scope': 'x', 'value': 'y'}]}):
        assert admin.post('/api/sys/alert-params', bad).status_code == 400, bad


def test_init_check(admin, m2admin):
    d = j(admin.get('/api/sys/init-check'))['data']
    keys = {x['key']: x for x in d['items']}
    for k in ('stores', 'warehouses', 'goods', 'goods_price', 'goods_barcode', 'staff', 'authorizer', 'stock', 'payment', 'print', 'negative_stock'):
        assert k in keys, k
    assert keys['stores']['ok'] and not keys['goods_price']['ok']           # P3 售价为 0
    assert keys['staff']['ok'] and keys['negative_stock']['ok'] is True           # core 默认已禁止负库存（POS-109）
    admin.post('/api/sys/config', {'allow_negative_stock': True})
    assert {x['key']: x for x in j(admin.get('/api/sys/init-check'))['data']['items']}['negative_stock']['ok'] is False
    m = {x['key']: x for x in j(m2admin.get('/api/sys/init-check'))['data']['items']}
    assert not m['stock']['ok']


def test_import_template_center(admin, manager, cashier):
    lst = j(admin.get('/api/sys/import-templates'))['data']
    assert {x['kind'] for x in lst} == {'goods', 'barcode', 'category', 'member', 'opening_stock', 'stores'}
    assert {x['kind'] for x in j(cashier.get('/api/sys/import-templates'))['data']} == set()
    r = admin.get('/api/sys/templates/goods')
    wb = load_workbook(io.BytesIO(r.data))
    assert wb.sheetnames == ['导入模板', '字段说明'] and wb['导入模板'].cell(row=1, column=3).value == '商品名称*'
    assert manager.get('/api/sys/templates/goods').status_code == 403
    assert admin.get('/api/sys/templates/nope').status_code == 404


def test_import_stores_partial_success(admin, dbc):
    head = ['门店编码*', '门店名称*', '区域编码', '类型', '地址', '电话', '负责人', '营业时间', '状态']
    f = xlsx(head, [['C001', '丙店', 'R1', '直营', '某路 1 号', '', '赵', '9-21', '营业'],
                    ['A001', '甲店（改名）', '', '', '', '', '', '', ''],
                    ['C001', '丙店重复', '', '', '', '', '', '', ''],
                    ['??', '坏编码', '', '', '', '', '', '', ''],
                    ['C002', '', '', '', '', '', '', '', ''],
                    ['C003', '戊店', 'NOPE', '', '', '', '', '', ''],
                    ['C004', '己店', '', '连锁', '', '', '', '', '']])
    r = j(admin.c.post('/api/sys/import/stores', data={'file': (f, 's.xlsx')}, content_type='multipart/form-data'))['data']
    assert (r['created'], r['updated'], r['error_count']) == (1, 1, 5)
    assert [e['row_no'] for e in r['errors']] == [4, 5, 6, 7, 8]
    assert '重复' in r['errors'][0]['errors'][0]
    assert dbc.execute("SELECT name FROM stores WHERE merchant_id=1 AND code='A001'").fetchone()[0] == '甲店（改名）'


def test_upload_type_and_size(app, admin):
    r = admin.c.post('/api/sys/import/stores', data={'file': (io.BytesIO(b'MZ\x90\x00'), 'evil.exe')}, content_type='multipart/form-data')
    assert r.status_code == 400
    r = admin.c.post('/api/sys/import/stores', data={'file': (io.BytesIO(b'not a zip'), 'fake.xlsx')}, content_type='multipart/form-data')
    assert r.status_code == 400 and '有效' in j(r)['msg']
    big = io.BytesIO(b'0' * (21 * 1024 * 1024))
    r = admin.c.post('/api/sys/import/stores', data={'file': (big, 'big.xlsx')}, content_type='multipart/form-data')
    assert r.status_code == 413
    assert Client(app, 'shouyin01').c.post('/api/sys/import/stores', data={'file': (io.BytesIO(b''), 'a.xlsx')},
                                           content_type='multipart/form-data').status_code == 403
