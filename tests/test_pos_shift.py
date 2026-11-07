# -*- coding: utf-8 -*-
"""班次与现金：AC-014（销售+退款+现金支出勾稽）、同台唯一班次、强制交班、现金收支额度、交班幂等、补打班报、PIN 登录、收银台/设备检测。"""
from conftest import Client
from test_pos_helpers import body, set_pin


def test_ac014_cash_reconciliation(cashier, app):
    cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 2}], [{'method': 'cash', 'amount': 500}]))          # 现金 200（找零 300）
    d = cashier.post('/api/pos/checkout', body([{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 100}, {'method': 'wechat', 'amount': 300}])).get_json()
    line = d['order']['items'][0]['id']
    # 退货：先退微信，现金 0（默认顺序）；再用请求指定顺序把现金退出去
    r = cashier.post('/api/pos/refund', {'order_no': d['order_no'], 'items': [{'line_id': line, 'qty': 1}], 'refund_method_order': ['cash', 'wechat'],
                                         'auth_user': 'dianzhang', 'auth_pass': '123456'}).get_json()
    assert [(p['method'], p['amount']) for p in r['payments']] == [('cash', 100.0), ('wechat', 300.0)]
    # 现金收入 50；现金支出 30 超收银员额度 -> 要授权
    assert cashier.post('/api/pos/cash-moves', {'direction': 'in', 'amount': 50, 'reason': '找零备用金补充', 'client_id': 'cm-1'}).get_json()['ok']
    r = cashier.post('/api/pos/cash-moves', {'direction': 'out', 'amount': 30, 'reason': '买打印纸', 'client_id': 'cm-2'})
    assert r.status_code == 403 and r.get_json()['code'] == 'NEED_AUTH' and r.get_json()['data']['kind'] == 'cash_out'
    r = cashier.post('/api/pos/cash-moves', {'direction': 'out', 'amount': 30, 'reason': '买打印纸', 'client_id': 'cm-2',
                                             'auth_user': 'dianzhang', 'auth_pass': '123456'}).get_json()
    assert r['ok'] and r['auth_user'] == 'dianzhang'
    assert cashier.post('/api/pos/cash-moves', {'direction': 'out', 'amount': 30, 'reason': '买打印纸', 'client_id': 'cm-2'}).get_json()['duplicate']
    assert cashier.post('/api/pos/cash-moves', {'direction': 'out', 'amount': 30, 'reason': ''}).status_code == 400
    assert cashier.post('/api/pos/cash-moves', {'direction': 'out', 'amount': 99999, 'reason': 'x', 'auth_user': 'dianzhang',
                                                'auth_pass': '123456'}).status_code == 400          # 超过钱箱应有现金
    s = cashier.get('/api/pos/shift/current').get_json()['summary']
    # 备用金 100 + 现金销售 200 + 100 − 现金退款 100 + 收入 50 − 支出 30 = 320
    assert (s['opening_cash'], s['cash_pay'], s['cash_move_in'], s['cash_move_out'], s['expected_cash']) == (100.0, 200.0, 50.0, 30.0, 320.0)
    assert (s['sale_count'], s['sale_amount'], s['refund_count'], s['refund_amount']) == (2, 600.0, 1, -400.0)
    assert dict((x['method'], x['amount']) for x in s['by_method']) == {'cash': 200.0, 'wechat': 0.0}
    r = cashier.post('/api/pos/shift/close', {'counted_cash': 318, 'client_id': 'close-1'}).get_json()
    assert r['ok'] and r['shift']['expected_cash'] == 320.0 and r['shift']['counted_cash'] == 318.0 and r['shift']['diff'] == -2.0
    assert r['summary']['diff'] == -2.0
    again = cashier.post('/api/pos/shift/close', {'counted_cash': 999, 'client_id': 'close-1'}).get_json()     # 重复点交班
    assert again['duplicate'] and again['shift']['counted_cash'] == 318.0


def test_one_open_shift_per_terminal_and_force_close(app, cashier, dbc):
    mgr = Client(app, 'dianzhang')
    r = mgr.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00101'})            # 收银员占着 A00101
    assert r.status_code == 409 and '未交班' in r.get_json()['msg']
    rows = mgr.get('/api/pos/shifts?status=open').get_json()['rows']
    sid = [x['id'] for x in rows if x['terminal_no'] == 'A00101'][0]
    assert mgr.post('/api/pos/shift/force-close', {'shift_id': sid}).status_code == 400        # 原因必填
    assert cashier.post('/api/pos/shift/force-close', {'shift_id': sid, 'reason': 'x'}).status_code == 403   # 收银员无权
    r = mgr.post('/api/pos/shift/force-close', {'shift_id': sid, 'reason': '收银员临时离岗未交班', 'counted_cash': 100}).get_json()
    assert r['ok'] and r['shift']['forced'] == 1 and r['shift']['force_by'] == 'dianzhang' and r['shift']['diff'] == 0.0
    assert dbc.execute("SELECT level FROM audit_logs WHERE action='强制交班'").fetchone()[0] == 'high'
    assert dbc.execute("SELECT COUNT(*) FROM notifications WHERE user_id=(SELECT id FROM users WHERE username='shouyin01') "
                       "AND title LIKE '%强制交班%'").fetchone()[0] == 1
    assert mgr.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00101'}).get_json()['ok']
    # 收银员被强制交班后不能继续收银
    assert cashier.post('/api/pos/checkout', body([{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])).status_code == 400
    # 补打班报：标记重打
    rp = mgr.post('/api/pos/shift/%d/reprint' % sid).get_json()
    assert rp['ok'] and rp['shift']['reprint'] and rp['shift']['reprint_count'] == 1
    # 乙店店长不能强制交甲店班次
    assert Client(app, 'dianzhangB').post('/api/pos/shift/force-close', {'shift_id': sid, 'reason': 'x'}).status_code == 403


def test_terminal_belongs_to_store(app, cashier, dbc):
    b = Client(app, 'shouyinB')
    r = b.post('/api/pos/shift/open', {'opening_cash': 0, 'terminal_no': 'A00101'})
    assert r.status_code == 409 and '属于门店 A001' in r.get_json()['msg']
    t = cashier.get('/api/pos/terminals').get_json()['rows']
    assert [x['terminal_no'] for x in t] == ['A00101'] and t[0]['open_shift']
    assert cashier.post('/api/pos/terminals', {'terminal_no': 'A00109', 'store_code': 'A001'}).status_code == 403   # 维护收银台要 org.edit
    root = Client(app, 'root', 'rootpass1')
    r = root.post('/api/pos/terminals', {'terminal_no': 'A00109', 'store_code': 'A001', 'name': '2 号台', 'paper_width': 80}).get_json()
    assert r['ok'] and r['data']['paper_width'] == 80
    assert root.post('/api/pos/terminals', {'terminal_no': 'A00109', 'store_code': 'A001', 'paper_width': 70}).status_code == 400
    # 设备检测记录（POS-806）
    r = cashier.post('/api/pos/terminals/check', {'terminal_no': 'A00101', 'device': 'printer', 'model': 'XP-58IIH', 'driver': 'Xprinter 7.7',
                                                   'result': 'ok', 'detail': '打印测试页正常'}).get_json()
    assert r['ok'] and r['rows'][0]['model'] == 'XP-58IIH'
    assert cashier.post('/api/pos/terminals/check', {'device': 'printer', 'result': 'maybe'}).status_code == 400


def test_pin_login(app, dbc):
    set_pin(dbc, 'shouyin01', 'E001', '1234')
    c = app.test_client()
    r = c.post('/api/pos/login-pin', json={'store_code': 'A001', 'emp_no': 'E001', 'pin': '1234', 'terminal_no': 'A00101'})
    assert r.status_code == 200 and r.get_json()['data']['username'] == 'shouyin01'
    ctx = c.get('/api/pos/context').get_json()
    assert ctx['data']['store']['code'] == 'A001' and ctx['data']['merchant_code'] == 'DEMO'
    # 不属于 B001
    assert app.test_client().post('/api/pos/login-pin', json={'store_code': 'B001', 'emp_no': 'E001', 'pin': '1234'}).status_code == 403
    # 失败锁定
    c2 = app.test_client()
    for _ in range(5):
        assert c2.post('/api/pos/login-pin', json={'store_code': 'A001', 'emp_no': 'E001', 'pin': '0000'}).status_code == 401
    assert c2.post('/api/pos/login-pin', json={'store_code': 'A001', 'emp_no': 'E001', 'pin': '1234'}).status_code == 429
    # 自助改 PIN 要当前密码
    u = Client(app, 'dianzhang')
    assert u.post('/api/pos/pin', {'pin': '5678', 'password': 'bad'}).status_code == 403
    assert u.post('/api/pos/pin', {'pin': '12', 'password': '123456'}).status_code == 400
    assert u.post('/api/pos/pin', {'pin': '5678', 'password': '123456'}).get_json()['ok']


def test_auth_precheck(cashier, dbc):
    assert cashier.post('/api/pos/auth', {'kind': 'discount', 'value': '0.8', 'auth_user': 'dianzhang', 'auth_pass': '123456'}).get_json()['ok']
    r = cashier.post('/api/pos/auth', {'kind': 'discount', 'value': '0.6', 'auth_user': 'dianzhang', 'auth_pass': '123456'})
    assert r.status_code == 403 and r.get_json()['code'] == 'AUTH_FAILED'              # 超出店长额度 0.7
    assert cashier.post('/api/pos/auth', {'auth_user': 'shouyinB', 'auth_pass': '123456'}).status_code == 403   # 收银员不能授权
    assert dbc.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0          # 预校验不产生业务数据


def test_switch_store_scope(app, cashier):
    root = Client(app, 'root', 'rootpass1')
    ctx = root.get('/api/pos/context').get_json()['data']
    assert ctx['can_switch_store'] and [s['code'] for s in ctx['stores']] == ['A001', 'B001']
    assert root.post('/api/pos/store', {'store': 'B001'}).get_json()['ok']
    assert root.get('/api/pos/context').get_json()['data']['store']['code'] == 'B001'
    assert root.post('/api/pos/store', {'store': 'Z001'}).status_code == 403          # 别家商户门店
    assert cashier.get('/api/pos/context?store=B001').status_code == 403
