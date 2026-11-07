# -*- coding: utf-8 -*-
"""模块④：报表中心（ERP-801~806、ERP-808、AC-011 报表侧、AC-019 异步导出不阻塞收银）。"""
import io
import time

from openpyxl import load_workbook
from conftest import checkout, Client


def j(r):
    return r.get_json()


def make_sales(cashier):
    checkout(cashier, [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 2}], [{'method': 'cash', 'amount': 600}], member_code='M001')
    d = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'wechat', 'amount': 100}]).get_json()
    r = cashier.post('/api/pos/refund', {'order_no': d['order_no'], 'auth_user': 'dianzhang', 'auth_pass': '123456'})
    assert r.status_code == 200, r.get_json()
    return d['order_no']


def test_sales_summary_and_dims(admin, cashier):
    make_sales(cashier)
    d = j(admin.get('/api/report/sales'))['data']
    t = d['total']
    assert (t['orders'], t['sales'], t['refund'], t['net'], t['qty']) == (2, 680.0, 100.0, 580.0, 4)   # P1 走会员价 380
    assert t['avg_order'] == 340.0 and t['avg_item'] == 170.0 and t['attach_rate'] == 2.0
    assert d['caliber'] and [c['key'] for c in d['columns']][:3] == ['name', 'orders', 'sales']
    for dim in ('week', 'month', 'store', 'region'):
        dd = j(admin.get('/api/report/sales?dim=' + dim))['data']
        assert dd['total']['sales'] == 680.0, dim
    reg = j(admin.get('/api/report/sales?dim=region'))['data']['rows']
    assert reg[0]['name'] in ('一区', '未分区')
    assert admin.get('/api/report/sales?dim=nope').status_code == 400
    assert admin.get('/api/report/sales?start=2026-13-01').status_code == 400
    assert admin.get('/api/report/sales?start=2026-02-01&end=2026-01-01').status_code == 400


def test_goods_profit_field_permission(app, admin, manager, cashier):
    make_sales(cashier)
    d = j(admin.get('/api/report/goods'))['data']
    p2 = [x for x in d['rows'] if x['key'] == 'P2'][0]
    assert p2['qty'] == 2 and p2['sales'] == 200.0 and p2['profit'] == 80.0 and p2['margin'] == 40.0
    assert d['total']['profit'] == 160.0 and d['profit_visible']
    m = j(manager.get('/api/report/goods'))['data']
    assert not m['profit_visible'] and 'cost' not in [c['key'] for c in m['columns']]
    assert all(x['cost'] is None and x['profit'] is None and x['margin'] is None for x in m['rows'])
    assert m['total']['profit'] is None
    cw = j(Client(app, 'caiwu').get('/api/report/goods?dim=category'))['data']
    assert cw['profit_visible'] and cw['total']['profit'] == 160.0
    for dim in ('brand', 'spec'):
        assert j(admin.get('/api/report/goods?dim=' + dim))['ok']


def test_scope_and_cross_merchant(app, admin, manager, cashier, cashierB, m2admin):
    make_sales(cashier)
    checkout(cashierB, [{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 400}])
    assert j(admin.get('/api/report/sales?dim=store'))['data']['total']['sales'] == 1080.0
    assert j(manager.get('/api/report/sales'))['data']['total']['sales'] == 680.0          # 店长只看 A001
    r = manager.get('/api/report/sales?store=B001')
    assert r.status_code == 403 and j(r)['code'] == 'SCOPE_DENIED'
    quyu = Client(app, 'quyu')
    assert j(quyu.get('/api/report/sales'))['data']['total']['sales'] == 680.0             # 区域 R1 只含 A001
    for kind in ('sales', 'goods', 'hourly', 'staff', 'pay', 'shifts', 'stock', 'members', 'orders'):
        d = j(m2admin.get('/api/report/' + kind))
        assert d['ok'], kind
        body = str(d)
        assert '甲店' not in body and 'A001' not in body and '13800000001' not in body and '奶粉一段' not in body, kind
        assert m2admin.get('/api/report/%s?store=A001' % kind).status_code == 403, kind


def test_cashier_denied_and_export_denied_audited(admin, cashier, manager, dbc):
    for kind in ('sales', 'goods', 'stock', 'members', 'orders'):
        assert cashier.get('/api/report/' + kind).status_code == 403
    r = cashier.get('/api/report/members/export')
    assert r.status_code == 403
    r = manager.get('/api/report/sales/export')                  # 店长有 report.sales 但没有 report.export
    assert r.status_code == 403
    assert cashier.post('/api/report/exports', {'kind': 'orders'}).status_code == 403
    rows = dbc.execute("SELECT username, level, ref_no FROM audit_logs WHERE action='导出被拒' ORDER BY id").fetchall()
    assert [(x[0], x[1], x[2]) for x in rows] == [('shouyin01', 'security', 'members'), ('dianzhang', 'security', 'sales'),
                                                  ('shouyin01', 'security', 'orders')]
    assert admin.get('/api/report/nope/export').status_code == 404


def test_hourly_staff_pay_shifts(admin, cashier):
    make_sales(cashier)
    h = j(admin.get('/api/report/hourly'))['data']['rows']
    assert len(h) == 24 and sum(x['orders'] for x in h) == 2
    s = j(admin.get('/api/report/staff'))['data']['rows']
    assert s[0]['name'].endswith('(shouyin01)') and s[0]['net'] == 580.0
    assert j(admin.get('/api/report/staff?dim=terminal'))['ok'] and j(admin.get('/api/report/staff?dim=guide'))['ok']
    p = j(admin.get('/api/report/pay'))['data']
    assert p['total']['sale'] == 680.0 and p['total']['refund'] == -100.0 and p['pay_diffs'] == []
    sh = j(admin.get('/api/report/shifts'))['data']
    assert sh['rows'] and sh['rows'][0]['status'] == '未交班'


def test_stock_and_members_reports(admin, manager, cashier):
    make_sales(cashier)
    st = j(admin.get('/api/report/stock'))['data']
    a = [x for x in st['rows'] if x['store_code'] == 'A001'][0]
    assert a['qty'] == 10 - 1 + 20 - 2 and a['amount'] is not None and st['moves']
    sm = j(manager.get('/api/report/stock'))['data']
    assert [x['store_code'] for x in sm['rows']] == ['A001'] and sm['rows'][0]['amount'] is None
    det = j(admin.get('/api/report/stock?detail=1&store=A001'))['data']
    assert {x['code'] for x in det['rows']} >= {'P1', 'P2'}
    mb = j(admin.get('/api/report/members'))['data']
    assert mb['total']['active_members'] == 1 and mb['levels'] and mb['top_members'][0]['phone'] == '13800000001'
    mm = j(manager.get('/api/report/members'))['data']
    assert mm['top_members'][0]['phone'] == '138****0001'                       # 店长无完整手机号权限
    od = j(manager.get('/api/report/orders'))['data']
    assert any(x['phone'] == '138****0001' for x in od['rows'])


def test_sync_export_audited(admin, cashier, dbc):
    make_sales(cashier)
    r = admin.get('/api/report/sales/export?dim=store')
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.data)).active
    assert ws.cell(row=1, column=1).value == '门店' and ws.max_row == 3        # 表头 + 1 店 + 合计
    a = dbc.execute("SELECT level, ref_no FROM audit_logs WHERE action='导出报表'").fetchone()
    assert tuple(a) == ('security', 'sales')


def _wait(c, no, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = j(c.get('/api/report/exports/' + no))['data']
        if d['status'] in ('done', 'failed'):
            return d
        time.sleep(0.1)
    raise AssertionError('export job timeout')


def test_async_export_does_not_block_pos(app, admin, cashier, m2admin, dbc):
    """AC-019：异步导出执行期间收银照常；任务只能本人下载；文件有有效期。"""
    make_sales(cashier)
    r = j(admin.post('/api/report/exports', {'kind': 'orders', 'params': {'start': '2026-01-01'}}))
    assert r['ok'] and r['data']['job_no'].startswith('EX001') and r['data']['status'] in ('queued', 'running', 'done')
    no = r['data']['job_no']
    t0 = time.time()
    rr = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])
    assert rr.status_code == 200 and time.time() - t0 < 5
    d = _wait(admin, no)
    assert d['status'] == 'done' and d['row_count'] >= 3 and d['download']
    f = admin.get('/api/report/exports/%s/download' % no)
    assert f.status_code == 200
    ws = load_workbook(io.BytesIO(f.data)).active
    assert ws.cell(row=1, column=2).value == '单号'
    assert j(admin.get('/api/report/exports'))['data'][0]['job_no'] == no
    # 别人（同商户、跨商户）都拿不到
    assert Client(app, 'caiwu').get('/api/report/exports/%s/download' % no).status_code == 404
    assert m2admin.get('/api/report/exports/%s/download' % no).status_code == 404
    assert m2admin.get('/api/report/exports/' + no).status_code == 404
    # 过期
    dbc.execute("UPDATE export_jobs SET expire_at='2000-01-01 00:00:00' WHERE job_no=?", (no,))
    assert admin.get('/api/report/exports/%s/download' % no).status_code == 410
    acts = [x[0] for x in dbc.execute("SELECT action FROM audit_logs WHERE ref_no=? ORDER BY id", (no,))]
    assert acts == ['创建导出任务', '下载导出文件']
    # 参数越权当场拒绝，不进队列
    assert admin.post('/api/report/exports', {'kind': 'orders', 'params': {'store': 'Z001'}}).status_code == 403
    assert admin.post('/api/report/exports', {'kind': 'orders', 'params': {'start': 'bad'}}).status_code == 400


def test_async_export_rechecks_permission(app, admin, cashier, dbc):
    """任务执行时按创建人「当前」权限取数：权限被收回则任务失败。"""
    app.config['EXPORT_INLINE'] = True
    try:
        caiwu = Client(app, 'caiwu')
        rid = dbc.execute("SELECT id FROM roles WHERE merchant_id=1 AND code='finance'").fetchone()[0]
        dbc.execute("DELETE FROM role_perms WHERE role_id=? AND perm='report.export'", (rid,))
        assert caiwu.post('/api/report/exports', {'kind': 'sales'}).status_code == 403
        dbc.execute("INSERT INTO role_perms(role_id, perm) VALUES(?, 'report.export')", (rid,))
        d = j(caiwu.post('/api/report/exports', {'kind': 'sales'}))['data']
        assert d['status'] == 'done'
        uid = dbc.execute("SELECT id FROM users WHERE username='caiwu'").fetchone()[0]
        dbc.execute("INSERT INTO export_jobs(job_no, merchant_id, user_id, kind, params, status, created_at) "
                    "VALUES('EXTEST1', 1, ?, 'sales', '{}', 'queued', '2026-11-05 00:00:00')", (uid,))
        dbc.execute("DELETE FROM role_perms WHERE role_id=? AND perm='report.export'", (rid,))
        from shouyin_app.views_report import run_export_job, export_dir
        with app.app_context():
            run_export_job(app.config['DB_PATH'], export_dir(app), dbc.execute("SELECT id FROM export_jobs WHERE job_no='EXTEST1'").fetchone()[0])
        row = dbc.execute("SELECT status, error FROM export_jobs WHERE job_no='EXTEST1'").fetchone()
        assert row['status'] == 'failed' and '收回' in row['error']
    finally:
        app.config['EXPORT_INLINE'] = False


def test_goods_qty_uses_base_units(admin, cashier, dbc):
    """REPORT-4 §7-1：多单位商品的商品分析销量按基本单位（order_items.base_qty）：1 箱(6 罐) + 2 罐 − 退 1 罐 = 7 罐（按交易数量会算成 2）。"""
    from test_pos_helpers import add_product, add_unit, stock_in
    pid = add_product(dbc, 'P5', '奶粉罐装', 100, cost=60, barcodes=['6900000000051'])
    add_unit(dbc, pid, '箱', 6, barcode='6900000000059')
    stock_in(dbc, pid, 'A001', 12)
    d = checkout(cashier, [{'code': '6900000000059', 'qty': 1}, {'code': '6900000000051', 'qty': 2}], [{'method': 'cash', 'amount': 800}]).get_json()
    assert d['ok'], d
    can = [x for x in d['order']['items'] if x['unit'] != '箱'][0]
    r = cashier.post('/api/pos/refund', {'order_no': d['order_no'], 'items': [{'line_id': can['id'], 'qty': 1}],
                                         'auth_user': 'dianzhang', 'auth_pass': '123456'})
    assert r.status_code == 200, r.get_json()
    g = j(admin.get('/api/report/goods?kw=P5'))['data']
    p5 = [x for x in g['rows'] if x['key'] == 'P5'][0]
    assert p5['qty'] == 7 and p5['sales'] == 700.0 and p5['cost'] == 420.0          # 金额/成本均来自 *_cents 列
    assert any('基本单位' in c for c in g['caliber'])


def test_restart_marks_unfinished_export_jobs_failed(app, dbc):
    """REPORT-4 §7-5：进程重启（init_db）把 queued/running 的导出任务标成失败，done 的不动。"""
    from shouyin_app.db import init_db
    for no, st in (('EXQ', 'queued'), ('EXR', 'running'), ('EXD', 'done')):
        dbc.execute("INSERT INTO export_jobs(job_no, merchant_id, user_id, kind, status, created_at) VALUES(?,1,1,'orders',?,'2026-11-05 10:00:00')",
                    (no, st))
    init_db(app.config['DB_PATH'])
    got = dict((r[0], (r[1], r[2])) for r in dbc.execute("SELECT job_no, status, error FROM export_jobs WHERE job_no LIKE 'EX%'"))
    assert got['EXQ'] == ('failed', '服务重启') and got['EXR'] == ('failed', '服务重启') and got['EXD'][0] == 'done'
