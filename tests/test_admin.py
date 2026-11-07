# -*- coding: utf-8 -*-
"""后台（集成轮）：v1 旧后台 api/admin/* 已拆除，后台场景改走新接口；种子脚本可重复运行。

商品/库存/会员/报表/组织/参数的细节用例分别在 test_goods/test_stock/test_member_api/test_report/test_tenant/test_sys，
这里只保留：旧接口确实下线、v1 后台常用场景在新接口上能走通、种子幂等。
"""
import io
import json

from openpyxl import load_workbook
from conftest import checkout, DATA
from shouyin_app.seed import seed
from shouyin_app.db import connect


def j(r):
    return r.get_json()


def test_v1_admin_api_removed(admin, manager, dbc):
    pid = dbc.execute("SELECT id FROM products WHERE merchant_id=1 AND code='P1'").fetchone()[0]
    for url in ('/api/admin/overview', '/api/admin/products', '/api/admin/stock', '/api/admin/members', '/api/admin/orders',
                '/api/admin/users', '/api/admin/config', '/api/admin/stores', '/api/admin/reports', '/api/admin/logs'):
        assert admin.get(url).status_code == 404, url
        assert manager.get(url).status_code == 404, url
    r = admin.c.delete('/api/admin/products/%d' % pid)                      # v1 会物理删除商品
    assert r.status_code == 404 and j(r)['code'] == 'NOT_FOUND'
    assert dbc.execute('SELECT COUNT(*) FROM products WHERE id=?', (pid,)).fetchone()[0] == 1
    for url in ('/api/m/overview', '/api/m/stock?kw=P1', '/api/m/member?kw=13800000001', '/api/expiry'):    # ⑤ 的 v1 接口同样下线
        assert admin.get(url).status_code == 404, url


def test_v1_backend_scenarios_on_new_api(admin, cashier, dbc):
    """v1 后台的「概览/报表/导出/参数/日志/门店/导购」在新接口上走一遍。"""
    checkout(cashier, [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 2}], [{'method': 'cash', 'amount': 600}])
    d = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'wechat', 'amount': 100}]).get_json()
    assert cashier.post('/api/pos/refund', {'order_no': d['order_no'], 'auth_user': 'dianzhang', 'auth_pass': '123456'}).status_code == 200
    # 报表（替代 api/admin/reports）
    t = j(admin.get('/api/report/sales'))['data']['total']
    assert (t['orders'], t['sales'], t['refund'], t['net']) == (2, 700.0, 100.0, 600.0)
    goods = dict((x['key'], x) for x in j(admin.get('/api/report/goods'))['data']['rows'])
    assert goods['P2']['qty'] == 2 and goods['P1']['qty'] == 1                 # 3 件卖出 - 1 件退回
    x = admin.get('/api/report/sales/export?dim=store')
    assert x.status_code == 200 and load_workbook(io.BytesIO(x.data)).active.max_row >= 2
    # 概览（替代 api/admin/overview）
    c = j(admin.get('/api/m/dashboard'))['data']['current']
    assert c['sales'] == 700.0 and c['net'] == 600.0
    # 订单列表（替代 api/admin/orders）
    o = j(admin.get('/api/report/orders'))['data']
    assert len(o['rows']) == 3
    # 收银参数（替代 api/admin/config），保存写审计
    cfg = j(admin.post('/api/sys/config', {'round_mode': 'jiao', 'receipt_title': '测试抬头', 'receipt_width': 80, 'points_to_yuan': 50,
                                           'reason': '集成轮'}))['data']['config']
    assert cfg['round_mode'] == 'jiao' and cfg['receipt_width'] == 80 and cfg['points_to_yuan'] == 50
    assert j(cashier.get('/api/pos/context'))['data']['config']['receipt_title'] == '测试抬头'
    # 日志（替代 api/admin/logs）
    assert j(admin.get('/api/org/audit?action=修改收银参数'))['data']['total'] >= 1
    # 门店 + 导购（替代 api/admin/stores、api/admin/guides）
    assert j(admin.post('/api/org/stores', {'code': 'C001', 'name': '丙店'}))['ok']
    assert j(admin.post('/api/org/guides', {'store_code': 'C001', 'code': 'G9', 'name': '小李'}))['ok']
    assert [g['code'] for g in j(admin.get('/api/org/guides?store=C001'))['data']] == ['G9']


def test_seed_is_idempotent(tmp_path):
    p = tmp_path / 'seed.json'
    p.write_text(json.dumps(dict(DATA, stores=DATA['stores'] + [{'code': '8007', 'name': '新妇幼店', 'sort': 5}]), ensure_ascii=False), encoding='utf-8')
    dbp = str(tmp_path / 'x.db')
    pw = str(tmp_path / 'ADMIN_PASSWORD.txt')
    s1 = seed(dbp, str(p), pw)
    first_pw = open(pw, encoding='utf-8').read()
    s2 = seed(dbp, str(p), pw)
    assert s1['stores'] == 3 and s1['products'] == 3 and s1['stock'] == 3 and s1['members'] == 2 and s1['users'] == 3
    assert all(s2[k] == 0 for k in ('stores', 'products', 'barcodes', 'stock', 'members', 'babies', 'guides', 'users'))
    assert open(pw, encoding='utf-8').read() == first_pw          # 第二次不会改密码文件
    db = connect(dbp)
    counts = dict((t, db.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]) for t in ('stores', 'products', 'stock', 'stock_moves', 'members', 'babies', 'users', 'orders'))
    assert counts == {'stores': 3, 'products': 3, 'stock': 3, 'stock_moves': 3, 'members': 2, 'babies': 1, 'users': 3, 'orders': 0}
    # 后台改过的数据不会被种子覆盖
    db.execute("UPDATE products SET retail=999 WHERE code='P1'")
    seed(dbp, str(p), pw)
    assert db.execute("SELECT retail FROM products WHERE code='P1'").fetchone()[0] == 999
    db.close()
