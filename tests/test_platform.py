# -*- coding: utf-8 -*-
"""模块④：平台运营（SaaS-001~004、2.2 平台与商户分权）。"""
from conftest import Client


def j(r):
    return r.get_json()


OPEN = {'open_request_no': 'REQ-20261105-01', 'code': 'NEWM', 'name': '新开母婴', 'contact': '钱老板', 'phone': '13600000000',
        'admin_username': 'newm_admin', 'max_stores': 2, 'max_users': 5, 'status': 'trial'}


def test_open_allowed_after_legacy_blueprints_removed(app, platform, dbc):
    """集成轮已删除 views_admin 并去掉全部 legacy_single_tenant 标记：开通第 2 个以上商户放行。
    views_platform 里「存在 legacy 蓝图时拒绝开通」的保护保留（标记没了自然放行）。"""
    assert not [n for n, b in app.blueprints.items() if getattr(b, 'legacy_single_tenant', False)]
    assert j(platform.get('/api/platform/merchants'))['data']['legacy_blueprints'] == []
    r = platform.post('/api/platform/merchants', OPEN)
    assert r.status_code == 200 and j(r)['ok'] and j(r)['data']['code'] == 'NEWM', j(r)
    assert dbc.execute("SELECT COUNT(*) FROM merchants WHERE code='NEWM'").fetchone()[0] == 1


def test_open_still_refused_if_a_legacy_blueprint_reappears(app, platform):
    bp = app.blueprints['mobile']
    bp.legacy_single_tenant = True
    try:
        r = platform.post('/api/platform/merchants', OPEN)
        assert r.status_code == 409 and j(r)['code'] == 'LEGACY_SINGLE_TENANT' and j(r)['data']['legacy_blueprints'] == ['mobile']
    finally:
        bp.legacy_single_tenant = False


def test_open_merchant_idempotent_and_initialized(app, platform, dbc):
    r = j(platform.post('/api/platform/merchants', OPEN))
    d = r['data']
    assert r['ok'] and d['duplicate'] is False and d['code'] == 'NEWM' and d['status'] == 'trial' and len(d['admin_password']) == 12
    assert d['mc_no'].startswith('MC000') and d['stores_used'] == 0 and d['max_stores'] == 2
    mid = d['id']
    # 重复提交同一请求号：不产生第二个商户，也不再返回密码
    r2 = j(platform.post('/api/platform/merchants', dict(OPEN, name='改了名字也一样')))['data']
    assert r2['duplicate'] is True and r2['id'] == mid and 'admin_password' not in r2
    assert dbc.execute("SELECT COUNT(*) FROM merchants WHERE code='NEWM'").fetchone()[0] == 1
    # 初始化：默认角色、总部组织、字典、管理员
    assert dbc.execute('SELECT COUNT(*) FROM roles WHERE merchant_id=?', (mid,)).fetchone()[0] >= 8
    assert dbc.execute("SELECT name FROM org_units WHERE merchant_id=? AND kind='hq'", (mid,)).fetchone()[0] == '总部'
    assert dbc.execute("SELECT COUNT(*) FROM dict_items WHERE merchant_id=? AND dict_type='refund_reason'", (mid,)).fetchone()[0] >= 3
    u = dbc.execute("SELECT * FROM users WHERE username='newm_admin'").fetchone()
    assert u['merchant_id'] == mid and u['must_change_pw'] == 1 and d['admin_password'] not in u['password_hash']
    # 管理员能登录且只看到自己的商户
    c = Client(app, 'newm_admin', d['admin_password'])
    me = j(c.get('/api/me'))['data']
    assert me['merchant_id'] == mid and me['role_code'] == 'merchant_admin' and me['stores'] == []
    # 审计不含密码
    a = dbc.execute("SELECT * FROM audit_logs WHERE merchant_id=0 AND action='开通商户'").fetchone()
    assert a['level'] == 'security' and d['admin_password'] not in (a['after_json'] + a['before_json'])
    # 编码 / 管理员账号冲突
    assert platform.post('/api/platform/merchants', dict(OPEN, open_request_no='REQ-OTHER-0001')).status_code == 409
    assert platform.post('/api/platform/merchants', dict(OPEN, open_request_no='REQ-OTHER-0002', code='NEW2',
                                                          admin_username='root')).status_code == 409
    assert platform.post('/api/platform/merchants', dict(OPEN, open_request_no='x')).status_code == 400
    # 失败的开通整单回滚，可直接用新请求号重试
    assert dbc.execute("SELECT COUNT(*) FROM merchants WHERE code='NEW2'").fetchone()[0] == 0
    ok2 = j(platform.post('/api/platform/merchants', dict(OPEN, open_request_no='REQ-OTHER-0002', code='NEW2', admin_username='new2_admin')))
    assert ok2['ok']


def test_status_expired_readonly_and_disabled(app, platform, dbc):
    m2 = Client(app, 'm2admin')
    assert m2.get('/api/org/stores').status_code == 200
    assert platform.post('/api/platform/merchants/2/status', {'status': 'expired'}).status_code == 400      # 原因必填
    assert j(platform.post('/api/platform/merchants/2/status', {'status': 'expired', 'reason': '到期未续费'}))['data']['status'] == 'expired'
    # 到期：查询可以，写操作 403 MERCHANT_READONLY
    assert m2.get('/api/org/stores').status_code == 200
    r = m2.post('/api/org/stores', {'code': 'Z002', 'name': 'x'})
    assert r.status_code == 403 and j(r)['code'] == 'MERCHANT_READONLY'
    # 停用：会话立即失效、不能登录
    assert j(platform.post('/api/platform/merchants/2/status', {'status': 'disabled', 'reason': '违约停用'}))['ok']
    assert m2.get('/api/org/stores').status_code == 401
    r = app.test_client().post('/api/login', json={'username': 'm2admin', 'password': '123456'})
    assert r.status_code == 403 and j(r)['code'] == 'MERCHANT_DISABLED'
    # 恢复正式
    assert j(platform.post('/api/platform/merchants/2/status', {'status': 'active', 'reason': '已续费'}))['data']['caps']['write'] is True
    assert Client(app, 'm2admin').get('/api/org/stores').status_code == 200
    assert platform.post('/api/platform/merchants/2/status', {'status': 'active', 'reason': 'x'}).status_code == 409
    assert platform.post('/api/platform/merchants/2/status', {'status': 'bogus', 'reason': 'x'}).status_code == 400
    assert platform.post('/api/platform/merchants/999/status', {'status': 'active', 'reason': 'x'}).status_code == 404
    lv = [r[0] for r in dbc.execute("SELECT level FROM audit_logs WHERE merchant_id=0 AND action='商户状态'")]
    assert lv == ['security'] * 3


def test_quota(platform, app):
    d = j(platform.post('/api/platform/merchants/2/quota', {'max_stores': 1, 'max_users': 5}))['data']
    assert d['max_stores'] == 1 and d['stores_used'] == 1 and d['users_used'] == 2
    r = platform.post('/api/platform/merchants/2/quota', {'max_users': 1})
    assert r.status_code == 400 and j(r)['data']['users_used'] == 2
    assert platform.post('/api/platform/merchants/2/quota', {'max_stores': -1}).status_code == 400
    m2 = Client(app, 'm2admin')
    r = m2.post('/api/org/stores', {'code': 'Z002', 'name': '二店'})
    assert r.status_code == 403 and j(r)['code'] == 'QUOTA_EXCEEDED'
    assert j(platform.post('/api/platform/merchants/2/quota', {'max_stores': 0}))['data']['max_stores'] == 0       # 0=不限
    assert j(m2.post('/api/org/stores', {'code': 'Z002', 'name': '二店'}))['ok']


def test_reset_admin(app, platform, dbc):
    old = Client(app, 'm2admin')
    assert platform.post('/api/platform/merchants/2/reset-admin', {'reason': '电话核验'}).status_code == 400          # 缺二次确认
    assert platform.post('/api/platform/merchants/2/reset-admin', {'confirm_code': 'DEMO', 'reason': '电话核验'}).status_code == 400
    assert platform.post('/api/platform/merchants/2/reset-admin', {'confirm_code': 'OTHER'}).status_code == 400       # 缺原因
    assert platform.post('/api/platform/merchants/2/reset-admin', {'confirm_code': 'OTHER', 'reason': 'x', 'username': 'm2cashier'}).status_code == 404
    d = j(platform.post('/api/platform/merchants/2/reset-admin', {'confirm_code': 'other', 'reason': '已电话核验负责人'}))['data']
    assert d['username'] == 'm2admin' and len(d['admin_password']) == 12
    assert old.get('/api/me').status_code == 401                                  # 原会话下线
    assert app.test_client().post('/api/login', json={'username': 'm2admin', 'password': '123456'}).status_code == 401
    c = Client(app, 'm2admin', d['admin_password'])
    assert j(c.get('/api/me'))['data']['merchant_id'] == 2
    assert dbc.execute("SELECT must_change_pw FROM users WHERE username='m2admin'").fetchone()[0] == 1
    a = dbc.execute("SELECT * FROM audit_logs WHERE merchant_id=0 AND action='重置商户管理员'").fetchone()
    assert a['level'] == 'security' and d['admin_password'] not in a['after_json'] and a['ref_no'] == 'OTHER'
    assert j(platform.get('/api/platform/audit?ref_no=OTHER'))['data']['total'] == 1


def test_platform_sees_no_business_data(app, platform, admin, m2admin, cashier):
    lst = j(platform.get('/api/platform/merchants'))['data']['items']
    demo = [m for m in lst if m['code'] == 'DEMO'][0]
    assert demo['stores_used'] == 2 and demo['admin_username']
    for k in ('orders', 'sales', 'members', 'products'):
        assert k not in demo
    for url in ('/api/org/stores', '/api/org/users', '/api/report/sales', '/api/sys/config', '/api/report/orders', '/api/pos/orders'):
        assert platform.get(url).status_code == 403, url
    # 商户账号进不了平台接口
    for c in (admin, m2admin, cashier):
        for url in ('/api/platform/merchants', '/api/platform/audit', '/api/platform/ops'):
            assert c.get(url).status_code == 403
        assert c.post('/api/platform/merchants/1/status', {'status': 'disabled', 'reason': 'x'}).status_code == 403
    ops = j(platform.get('/api/platform/ops'))['data']
    assert ops['merchants']['active'] == 2 and ops['legacy_blueprints'] == []
    # 平台运维（只读角色）不能开通/重置
    from shouyin_app.db import connect
    from shouyin_app.seed import ensure_user
    db = connect(app.config['DB_PATH'])
    db.execute('BEGIN')
    ensure_user(db, 'pops', 'popspass1', '运维', 'platform', '', 0, 'platform_ops')
    db.execute('COMMIT')
    db.close()
    ops_c = Client(app, 'pops', 'popspass1')
    assert ops_c.get('/api/platform/merchants').status_code == 200
    assert ops_c.post('/api/platform/merchants/2/reset-admin', {'confirm_code': 'OTHER', 'reason': 'x'}).status_code == 403
    assert ops_c.post('/api/platform/merchants', OPEN).status_code == 403


def test_platform_page_served(app):
    r = app.test_client().get('/platform')
    assert r.status_code == 200 and '平台运营' in r.get_data(as_text=True)
