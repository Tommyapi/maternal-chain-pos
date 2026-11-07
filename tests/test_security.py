# -*- coding: utf-8 -*-
"""权限隔离、登录限制、停用账号、加前缀后的路由。"""
import re
import os

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.wrappers import Response
from conftest import checkout, Client

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'shouyin_app', 'static')


def test_requires_login(app):
    c = app.test_client()
    for url in ('/api/me', '/api/pos/context', '/api/goods', '/api/m/dashboard', '/api/pos/orders', '/api/report/sales', '/api/org/users'):
        r = c.get(url)
        assert r.status_code == 401, url


def test_cashier_cannot_use_backend(cashier):
    """收银员：后台报表/组织/参数/审计/看板一律 403（集成轮由 api/admin/* 改为新接口）。"""
    for url in ('/api/report/sales', '/api/report/goods', '/api/report/orders', '/api/report/shifts', '/api/org/users', '/api/org/roles',
                '/api/org/audit', '/api/org/stores', '/api/org/guides', '/api/sys/config', '/api/m/dashboard', '/api/purchase/orders',
                '/api/stock/adjusts'):
        assert cashier.get(url).status_code == 403, url
    assert cashier.post('/api/goods', {'name': 'x'}).status_code == 403
    assert cashier.post('/api/stock/adjusts', {'lines': []}).status_code == 403
    assert cashier.post('/api/sys/config', {'round_mode': 'yuan'}).status_code == 403
    assert cashier.post('/api/org/guides', {'code': 'G8', 'name': 'x', 'store_code': 'A001'}).status_code == 403


def test_cashier_only_sees_own_store(cashier, cashierB):
    d = checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}]).get_json()
    no = d['order_no']
    assert cashier.get('/api/pos/orders/' + no).status_code == 200
    assert cashierB.get('/api/pos/orders/' + no).status_code == 403          # 乙店看不到甲店的单
    assert cashierB.get('/api/pos/orders').get_json()['total'] == 0
    assert cashier.get('/api/pos/orders?store=B001').get_json()['rows'][0]['store_code'] == 'A001'   # 想传参数看别店也没用
    ctx = cashier.get('/api/pos/context').get_json()
    assert ctx['store']['code'] == 'A001' and [s['code'] for s in ctx['stores']] == ['A001'] and not ctx['can_switch_store']
    assert cashier.post('/api/pos/store', {'store': 'B001'}).status_code == 403
    # 收银库存只扣自己门店
    assert cashier.get('/api/pos/goods/scan?code=P1').get_json()['goods']['stock'] == 10
    assert cashierB.get('/api/pos/goods/scan?code=P1').get_json()['goods']['stock'] == 5


def test_manager_scoped_to_store(manager, cashier, cashierB):
    checkout(cashier, [{'code': 'P2', 'qty': 1}], [{'method': 'cash', 'amount': 100}])
    checkout(cashierB, [{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 400}])
    o = manager.get('/api/pos/orders?store=B001').get_json()                     # 范围外门店参数被忽略，只看本店
    assert o['total'] == 1 and o['rows'][0]['store_code'] == 'A001'
    assert manager.get('/api/report/sales').get_json()['data']['total']['sales'] == 100.0
    r = manager.get('/api/report/sales?store=B001')
    assert r.status_code == 403 and r.get_json()['code'] == 'SCOPE_DENIED'
    assert manager.get('/api/m/dashboard').get_json()['data']['current']['sales'] == 100.0
    assert [s['code'] for s in manager.get('/api/org/stores').get_json()['data']] == ['A001']
    # 店长不能改商品/账号/参数，也看不到成本和毛利
    assert manager.post('/api/goods', {'name': 'x', 'retail': 1}).status_code == 403
    assert manager.post('/api/org/users', {'username': 'abc', 'password': 'abcd1234'}).status_code == 403
    assert manager.post('/api/sys/config', {'round_mode': 'yuan'}).status_code == 403
    items = manager.get('/api/goods?kw=P1').get_json()['data']['items']
    assert items and all(x['cost'] is None for x in items)
    assert manager.get('/api/report/goods').get_json()['data']['total']['profit'] is None
    # 库存：看不到别店仓，也不能对别店仓建盘点/调整单
    assert manager.get('/api/stock?wh=B001').status_code == 403
    assert manager.post('/api/stock/adjusts', {'wh_code': 'B001', 'kind': 'loss', 'reason': 'x',
                                               'lines': [{'code': 'P1', 'qty': 1}]}).status_code == 403
    assert cashierB.get('/api/pos/goods/scan?code=P1').get_json()['goods']['stock'] == 4          # B001 (5-1) 未被动到


def test_login_lockout(app):
    c = app.test_client()
    for _ in range(5):
        r = c.post('/api/login', json={'username': 'shouyin01', 'password': 'bad'})
        assert r.status_code == 401
    r = c.post('/api/login', json={'username': 'shouyin01', 'password': '123456'})
    assert r.status_code == 429 and '次数过多' in r.get_json()['error']
    # 其他账号不受影响
    assert c.post('/api/login', json={'username': 'dianzhang', 'password': '123456'}).status_code == 200


def test_disabled_user_cut_off_immediately(app, admin):
    c = Client(app, 'shouyin01')
    assert c.get('/api/me').status_code == 200
    u = [u for u in admin.get('/api/org/users?size=200').get_json()['data']['items'] if u['username'] == 'shouyin01'][0]
    assert admin.post('/api/org/users', {'id': u['id'], 'role_id': u['role_id'], 'store_code': 'A001', 'status': '停用', 'reason': '离职'}).get_json()['ok']
    assert c.get('/api/me').status_code == 401
    assert app.test_client().post('/api/login', json={'username': 'shouyin01', 'password': '123456'}).status_code == 401


def test_password_stored_hashed(app, dbc):
    for r in dbc.execute('SELECT username, password_hash FROM users'):
        assert r['password_hash'].startswith(('scrypt:', 'pbkdf2:')) and '123456' not in r['password_hash']


def test_admin_password_file_perm(app):
    p = app.config['PW_FILE']
    txt = open(p, encoding='utf-8').read()
    assert 'admin' in txt and len(txt.split('密码: ')[1].strip()) >= 16
    if os.name != 'nt':
        assert oct(os.stat(p).st_mode & 0o777) == '0o600'


# ---------------------------------------------------------------- 路由前缀
def test_pages_use_only_relative_urls():
    """前端文件里不能出现以 / 开头的接口或资源路径，否则放到 /shouyin/ 下就会 404。"""
    for name in ('cashier.html', 'admin.html', 'm.html', 'admin.js'):
        txt = open(os.path.join(STATIC, name), encoding='utf-8').read()
        bad = re.findall(r"""(?:fetch\(|href=|src=|action=)\s*['"]\/(?!\/)[^'"]*""", txt)
        assert not bad, (name, bad)
        bad2 = re.findall(r"""['"`]\/api\/""", txt)
        assert not bad2, (name, bad2)


def test_works_under_prefix(app):
    """用 DispatcherMiddleware 模拟 nginx 的 /shouyin/ 反代（nginx 会去掉前缀；这里测应用挂在前缀下也能用）。"""
    wsgi = DispatcherMiddleware(Response('root', status=404), {'/shouyin': app.wsgi_app})
    from werkzeug.test import Client as WClient
    c = WClient(wsgi, Response)
    assert c.get('/shouyin/').status_code == 200
    assert 'static/favicon.png' in c.get('/shouyin/').get_data(as_text=True)
    assert c.get('/shouyin/admin').status_code == 200
    assert c.get('/shouyin/m').status_code == 200
    assert c.get('/shouyin/static/admin.js').status_code == 200
    r = c.post('/shouyin/api/login', json={'username': 'root', 'password': 'rootpass1'})
    assert r.status_code == 200
    # 相对路径 api/me 从 /shouyin/admin 解析后就是 /shouyin/api/me
    r = c.get('/shouyin/api/me')
    assert r.status_code == 200 and r.get_json()['role'] == 'admin'
    r = c.get('/shouyin/admin/')
    assert r.status_code == 302 and r.headers['Location'].endswith('../admin')


def test_root_pages_and_health(app):
    c = app.test_client()
    assert c.get('/').status_code == 200 and c.get('/admin').status_code == 200 and c.get('/m').status_code == 200
    assert c.get('/healthz').get_json()['ok']
    assert c.get('/api/nothing').status_code == 404
    r = c.get('/api/me')
    assert r.headers['Cache-Control'] == 'no-store'
    cookie = [h for h in c.post('/api/login', json={'username': 'root', 'password': 'rootpass1'}).headers.getlist('Set-Cookie')][0]
    assert cookie.startswith('shouyin_sid=') and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie


# ================================================================ v2 安全测试（模块④，NFR-SEC-002/005/007、AC-011/012/013）
# 越权测试按 app.url_map 自动遍历全部已注册蓝图：以后新增的蓝图/接口不用改这里就会被覆盖。
import hashlib                                  # noqa: E402
import json as _json                            # noqa: E402

# 不需要登录的公开接口（其余 /api/ 一律要求登录）
PUBLIC_API = {'/api/login', '/api/logout', '/api/me', '/api/m/version', '/api/pos/login-pin'}
# 会改变调用者自己会话的接口：遍历时跳过（单独有用例）
SELF_SESSION = {'/api/logout', '/api/password', '/api/login', '/api/pos/login-pin'}
# 1 号商户的敏感标记：任何商户 2 / 平台账号的响应里都不能出现
M1_MARKERS = ['13800000001', '13800000002', '张妈妈', '李妈妈', '奶粉一段800g', '纸尿裤L', '甲店', '乙店', 'shouyin01', 'dianzhang']
# 1 号商户业务表指纹（商户 2 越权遍历前后必须一致）；这些表是日志/计数类，允许变化
SKIP_TABLES = {'audit_logs', 'login_attempts', 'notifications', 'number_seq', 'user_sessions', 'op_logs', 'schema_version', 'export_jobs',
               'approval_logs', 'sqlite_sequence'}


def _api_rules(app):
    out = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith('/api/'):
            continue
        for m in sorted((rule.methods or set()) - {'HEAD', 'OPTIONS'}):
            out.append((rule, m))
    return out


def _fill(rule, value_for):
    url = rule.rule
    for arg in rule.arguments:
        conv = rule._converters[arg].__class__.__name__
        if conv == 'IntegerConverter':
            url = url.replace('<int:%s>' % arg, str(value_for(arg, 'int')))
        else:
            url = re.sub(r'<(?:[a-z]+:)?%s>' % arg, str(value_for(arg, 'str')), url)
    return url


def _m1_fingerprint(db):
    """{表名: 该表 1 号商户行的哈希}（失败时能直接看出哪张表被改）。"""
    out = {}
    for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        if t in SKIP_TABLES or t.startswith('_'):
            continue
        cols = [r[1] for r in db.execute('PRAGMA table_info(%s)' % t)]
        if t == 'merchants':
            rows = db.execute('SELECT * FROM merchants WHERE id=1').fetchall()
        elif 'merchant_id' in cols:
            rows = db.execute('SELECT * FROM %s WHERE merchant_id=1 ORDER BY rowid' % t).fetchall()
        elif t in ('role_perms', 'role_limits', 'user_scopes', 'announcement_reads'):
            continue          # 子表：按父表 merchant_id 隔离（父表已在指纹里）
        else:
            rows = db.execute('SELECT * FROM %s ORDER BY rowid' % t).fetchall()
        h = hashlib.sha256()
        for r in rows:
            h.update(_json.dumps([r[c] for c in cols], ensure_ascii=False, default=str).encode())
        out[t] = h.hexdigest()
    return out


def _m1_ids(app, dbc, cashier):
    """准备 1 号商户的真实标识（订单号、会员号、商品 id、员工 id、审批单号），用来替换 URL 参数。"""
    d = checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 400}], member_code='M001').get_json()
    ids = {'order_no': d['order_no'], 'member': 'M001', 'product_code': 'P1', 'store': 'A001',
           'product_id': dbc.execute("SELECT id FROM products WHERE merchant_id=1 AND code='P1'").fetchone()[0],
           'user_id': dbc.execute("SELECT id FROM users WHERE username='shouyin01'").fetchone()[0], 'coupon': 'QN000000000'}
    root = Client(app, 'root', 'rootpass1')
    ap = root.post('/api/approvals', {'kind': 'discount', 'store_code': 'A001', 'value': '0.5', 'reason': 't', 'request_key': 'sec-1'}).get_json()
    ids['approval_no'] = ap['data']['approval_no'] if ap.get('ok') else 'AP0010000000000000001'
    return ids


def _value_for(ids):
    def f(arg, conv):
        a = arg.lower()
        if conv == 'int':
            return ids['user_id'] if ('uid' in a or 'user' in a) else ids['product_id']
        if 'coupon' in a:
            return ids['coupon']
        if 'kind' in a:
            return 'sales'
        if 'code' in a or 'member' in a:
            return ids['member']
        if a == 'no' and False:
            return ids['approval_no']
        return ids['order_no']
    return f


def _body(ids):
    return {'store': 'A001', 'store_code': 'A001', 'code': 'P1', 'member_code': 'M001', 'order_no': ids['order_no'], 'id': ids['product_id'],
            'product_id': ids['product_id'], 'user_id': ids['user_id'],   # 不放 1 号商户手机号：写接口可能原样回显请求值，造成误报
            'items': [{'code': 'P1', 'qty': 1}], 'role_id': 1, 'approval_no': ids['approval_no'], 'name': '越权测试', 'reason': 'sec',
            'amount': 1, 'qty': 1}


def _resp_text(resp):
    """响应正文转文本：xlsx（导出/模板下载）读出全部单元格，其他按 UTF-8 解码。"""
    raw = resp.get_data()
    if raw[:2] == b'PK':
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True)
        return ' '.join(str(c) for ws in wb.worksheets for row in ws.iter_rows(values_only=True) for c in row if c is not None)
    return raw.decode('utf-8', errors='replace')


def _check_no_leak(resp, who, url):
    txt = _resp_text(resp)
    leak = [m for m in M1_MARKERS if m in txt]
    assert not leak, (who, url, resp.status_code, leak)


def test_every_api_requires_login(app):
    """NFR-SEC-002：除公开接口外，每个 /api/ 接口未登录都返回 401（不是 200/500）。"""
    c = app.test_client()
    ids = {'order_no': 'S0010000000000000001', 'member': 'M001', 'product_id': 1, 'user_id': 1, 'coupon': 'X', 'approval_no': 'X'}
    checked = 0
    for rule, m in _api_rules(app):
        if rule.rule in PUBLIC_API:
            continue
        url = _fill(rule, _value_for(ids))
        r = c.open(url, method=m, json={})
        assert r.status_code == 401, (m, url, r.status_code)
        checked += 1
    assert checked >= 100


def test_cross_merchant_traversal_all_blueprints(app, cashier, dbc):
    """AC-012：商户 2 管理员 / 收银员改参数访问 1 号商户的列表、详情、导出、APP 接口——遍历全部已注册蓝图：
    响应里不能出现 1 号商户的数据，1 号商户业务表在遍历前后完全一致（写接口也不能越权改到别家），且没有 500。"""
    ids = _m1_ids(app, dbc, cashier)
    before = _m1_fingerprint(dbc)
    vf = _value_for(ids)
    body = _body(ids)
    q = '?store=A001&kw=13800000001&code=P1&member_code=M001&order_no=%s&start=2000-01-01' % ids['order_no']
    total = 0
    for who in ('m2admin', 'm2cashier'):
        c = Client(app, who)
        for rule, m in _api_rules(app):
            if rule.rule in SELF_SESSION:
                continue
            for url in {_fill(rule, vf), _fill(rule, lambda a, cv: ids['approval_no'] if cv != 'int' else vf(a, cv))}:
                r = c.c.open(url + (q if m == 'GET' else ''), method=m, json=body)
                assert r.status_code < 500 or r.status_code == 501, (who, m, url, r.status_code, r.get_data(as_text=True)[:200])   # 501=占位接口
                _check_no_leak(r, who, url)
                total += 1
        assert c.get('/api/me').status_code == 200, '遍历过程中会话不应失效：%s' % who
    assert total >= 200
    after = _m1_fingerprint(dbc)
    changed = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
    assert not changed, '商户 2 的请求改动了 1 号商户的数据：%s' % changed


def test_platform_account_traversal(app, platform, cashier, dbc):
    """2.2：平台账号访问任何商户业务接口一律 403（不返回经营数据）；只能用 /api/platform/*。"""
    ids = _m1_ids(app, dbc, cashier)
    vf = _value_for(ids)
    for rule, m in _api_rules(app):
        if rule.rule in SELF_SESSION or rule.rule in PUBLIC_API:
            continue
        url = _fill(rule, vf)
        r = platform.c.open(url + ('?store=A001' if m == 'GET' else ''), method=m, json=_body(ids))
        if rule.rule.startswith('/api/platform/'):
            assert r.status_code in (200, 400, 404, 409, 501), (m, url, r.status_code)
        else:
            assert r.status_code == 403, (m, url, r.status_code)
        _check_no_leak(r, 'platform', url)


def test_store_scope_traversal_new_blueprints(app, cashier, dbc):
    """数据范围：乙店收银员/店长带 store=A001 访问全部蓝图的列表接口，不能拿到甲店数据（403 SCOPE_DENIED 或空）。
    集成轮：views_admin 已删除、legacy_single_tenant 标记全部去掉，遍历覆盖 app 上注册的全部 /api 蓝图（不再跳过任何蓝图）。"""
    _m1_ids(app, dbc, cashier)
    assert not [name for name, b in app.blueprints.items() if getattr(b, 'legacy_single_tenant', False)]
    n, seen = 0, set()
    for who in ('shouyinB', 'dianzhangB'):
        c = Client(app, who)
        for rule, m in _api_rules(app):
            if m != 'GET' or rule.arguments or rule.rule in PUBLIC_API:
                continue
            r = c.get(rule.rule + '?store=A001')
            assert r.status_code in (200, 400, 403, 404, 501), (who, rule.rule, r.status_code)
            if r.status_code == 200:
                txt = _resp_text(r)
                assert '甲店' not in txt and 'shouyin01' not in txt and '"A001"' not in txt, (who, rule.rule)
            seen.add(rule.endpoint.split('.')[0])
            n += 1
    assert n >= 20
    api_bps = {r.endpoint.split('.')[0] for r, _ in _api_rules(app) if '.' in r.endpoint}
    assert seen == api_bps, sorted(api_bps - seen)        # 每个 API 蓝图至少有一个列表接口被遍历到


def test_field_permissions_cost_phone(app, admin, manager, cashier):
    """AC-011（报表/组织侧）：无成本权限不返回成本/毛利；无手机号权限只返回脱敏手机号；收银员接口和导出全拒。"""
    checkout(cashier, [{'code': 'P1', 'qty': 1}], [{'method': 'cash', 'amount': 400}], member_code='M001')
    g = manager.get('/api/report/goods').get_json()['data']
    assert all(x['cost'] is None and x['profit'] is None for x in g['rows'])
    assert '300' not in _json.dumps(g['rows'])                                   # 成本价 300 不出现
    assert '13800000001' not in manager.get('/api/report/members').get_data(as_text=True)
    assert '13800000001' not in manager.get('/api/report/orders').get_data(as_text=True)
    assert '13800000001' in admin.get('/api/report/orders').get_data(as_text=True)
    for url in ('/api/report/goods', '/api/report/members/export', '/api/report/orders/export', '/api/org/users', '/api/sys/config'):
        assert cashier.get(url).status_code == 403, url


def test_sql_injection_params(admin, m2admin):
    """注入：查询参数一律参数化，恶意输入不报 500、不扩大结果、不泄露密码散列。"""
    evil = ["' OR 1=1 --", '" OR ""="', "A001' UNION SELECT username,password_hash FROM users --", '%', '_', '1; DROP TABLE users']
    for e in evil:
        for url in ('/api/org/users?kw=', '/api/org/audit?user=', '/api/org/audit?action=', '/api/report/goods?kw=', '/api/sys/dicts?type='):
            r = m2admin.get(url + e)
            assert r.status_code in (200, 400, 403), (url, e, r.status_code)
            assert 'scrypt:' not in r.get_data(as_text=True) and 'pbkdf2:' not in r.get_data(as_text=True)      # 不会查出密码散列
        assert m2admin.get('/api/report/sales?store=' + e).status_code == 403
    assert len(admin.get('/api/org/users?size=200&kw=' + "' OR 1=1 --").get_json()['data']['items']) == 0
    assert admin.get('/api/org/users?size=200&kw=%').get_json()['data']['total'] >= 10        # LIKE 通配符只是普通匹配


def test_duplicate_submissions(app, platform, admin, dbc):
    """重复提交：审批 request_key、员工账号、角色编码重复提交都不会产生第二条（开通请求号见 test_platform）。"""
    ap1 = admin.post('/api/approvals', {'kind': 'discount', 'store_code': 'A001', 'value': '0.5', 'request_key': 'dup-1'}).get_json()['data']
    ap2 = admin.post('/api/approvals', {'kind': 'discount', 'store_code': 'A001', 'value': '0.5', 'request_key': 'dup-1'}).get_json()['data']
    assert ap1['approval_no'] == ap2['approval_no']
    rid = [r for r in admin.get('/api/org/roles').get_json()['data'] if r['code'] == 'cashier'][0]['id']
    for _ in range(2):
        admin.post('/api/org/users', {'username': 'dupuser', 'password': 'abcd1234', 'role_id': rid, 'store_code': 'A001'})
    assert dbc.execute("SELECT COUNT(*) FROM users WHERE username='dupuser'").fetchone()[0] == 1
    for _ in range(2):
        admin.post('/api/org/roles', {'code': 'dup_role', 'name': 'x'})
    assert dbc.execute("SELECT COUNT(*) FROM roles WHERE merchant_id=1 AND code='dup_role'").fetchone()[0] == 1


def test_no_secrets_in_logs(app, admin, platform, dbc):
    """NFR-SEC-005：密码 / PIN / 一次性密码不进审计和操作日志。"""
    rid = [r for r in admin.get('/api/org/roles').get_json()['data'] if r['code'] == 'cashier'][0]['id']
    assert admin.post('/api/org/users', {'username': 'secuser', 'password': 'Secret#2026x', 'role_id': rid, 'store_code': 'A001',
                                         'pin': '864209'}).status_code == 200
    uid = dbc.execute("SELECT id FROM users WHERE username='secuser'").fetchone()[0]
    admin.post('/api/org/users', {'id': uid, 'role_id': rid, 'store_code': 'A001', 'password': 'Another#2026y', 'pin': '135791'})
    admin.post('/api/password', {'old': 'rootpass1', 'new': 'Legacy#2026z'})
    pw = platform.post('/api/platform/merchants/2/reset-admin', {'confirm_code': 'OTHER', 'reason': '核验'}).get_json()['data']['admin_password']
    blob = ' '.join(str(tuple(r)) for r in dbc.execute('SELECT * FROM audit_logs')) + ' '.join(str(tuple(r)) for r in dbc.execute('SELECT * FROM op_logs'))
    for secret in ('Secret#2026x', 'Another#2026y', 'Legacy#2026z', '864209', '135791', pw):
        assert secret not in blob, secret
    r = dbc.execute("SELECT pin_hash FROM users WHERE username='secuser'").fetchone()
    assert '135791' not in r[0] and r[0].startswith(('scrypt:', 'pbkdf2:'))


def test_v2_pages_relative_urls():
    """新前端文件同样不能出现以 / 开头的接口或资源路径（nginx 反代 /shouyin/）。"""
    for name in ('platform.html', 'admin-org.js', 'admin-report.js', 'admin-sys.js'):
        txt = open(os.path.join(STATIC, name), encoding='utf-8').read()
        bad = re.findall(r"""(?:fetch\(|href=|src=|action=)\s*['"]\/(?!\/)[^'"]*""", txt)
        assert not bad, (name, bad)
        assert not re.findall(r"""['"`]\/api\/""", txt), name
