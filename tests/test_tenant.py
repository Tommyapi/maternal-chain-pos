# -*- coding: utf-8 -*-
"""模块④：组织/门店/员工/角色/数据范围/会话/审计（ERP-001~003、ERP-006、ERP-701~705、AC-013）。"""
from conftest import Client


def j(r):
    return r.get_json()


def role_id(admin, code):
    return [r for r in j(admin.get('/api/org/roles'))['data'] if r['code'] == code][0]['id']


def user_by(admin, username):
    return [u for u in j(admin.get('/api/org/users?size=200'))['data']['items'] if u['username'] == username][0]


def test_merchant_profile_and_policy(admin, manager, m2admin, dbc):
    d = j(admin.get('/api/org/merchant'))['data']
    assert d['id'] == 1 and d['policy']['cross_store_member'] == 'all' and 'stores_used' in d
    r = admin.post('/api/org/merchant', {'contact': '王总', 'policy': {'cross_store_stored': 'none'}, 'reason': '储值只在开卡门店用'})
    assert j(r)['data']['contact'] == '王总' and j(r)['data']['policy']['cross_store_stored'] == 'none'
    assert admin.post('/api/org/merchant', {'policy': {'cross_store_stored': 'bad'}}).status_code == 400
    assert admin.post('/api/org/merchant', {'name': ''}).status_code == 400
    assert manager.post('/api/org/merchant', {'contact': 'x'}).status_code == 403          # 店长无 org.edit
    assert j(m2admin.get('/api/org/merchant'))['data']['id'] == 2                            # 只能看到自己商户
    acts = [r['action'] for r in dbc.execute("SELECT action FROM audit_logs WHERE merchant_id=1 AND module='org'")]
    assert '修改连锁政策' in acts and '修改商户资料' in acts


def test_units_and_stores(admin, manager, m2admin, dbc):
    assert j(admin.post('/api/org/units', {'kind': 'region', 'code': 'R2', 'name': '二区'}))['ok']
    assert admin.post('/api/org/units', {'kind': 'xx', 'code': 'R3', 'name': 'x'}).status_code == 400
    r = j(admin.post('/api/org/stores', {'code': 'C001', 'name': '丙店', 'region_code': 'R2', 'manager_name': '赵店长', 'open_hours': '9:00-21:00'}))
    assert r['ok'] and r['data']['region_id']
    assert admin.post('/api/org/stores', {'code': 'C002', 'name': 'x', 'region_code': 'NOPE'}).status_code == 400
    units = j(admin.get('/api/org/units'))['data']
    assert [u for u in units if u['code'] == 'R2'][0]['stores'] == ['C001']
    # 店长只看到本店；不能新建门店
    assert [s['code'] for s in j(manager.get('/api/org/stores'))['data']] == ['A001']
    assert manager.post('/api/org/stores', {'code': 'D001', 'name': 'x'}).status_code == 403
    # 同编码在别的商户可以存在，互不影响
    assert j(m2admin.post('/api/org/stores', {'code': 'C001', 'name': '别家丙店'}))['ok']
    assert dbc.execute("SELECT name FROM stores WHERE merchant_id=1 AND code='C001'").fetchone()[0] == '丙店'
    assert [s['code'] for s in j(m2admin.get('/api/org/stores'))['data']] == ['C001', 'Z001']


def test_store_and_user_quota(admin, dbc):
    dbc.execute('UPDATE merchants SET max_stores=3, max_users=0 WHERE id=1')
    assert j(admin.post('/api/org/stores', {'code': 'C001', 'name': '丙店'}))['ok']           # 2 -> 3
    r = admin.post('/api/org/stores', {'code': 'C002', 'name': '丁店'})
    assert r.status_code == 403 and j(r)['code'] == 'QUOTA_EXCEEDED' and j(r)['data']['max'] == 3
    assert j(admin.post('/api/org/stores', {'code': 'C001', 'name': '丙店改名'}))['ok']                    # 修改已有门店不受额度影响
    used = dbc.execute("SELECT COUNT(*) FROM users WHERE merchant_id=1 AND status='启用'").fetchone()[0]
    dbc.execute('UPDATE merchants SET max_users=? WHERE id=1', (used,))
    r = admin.post('/api/org/users', {'username': 'over1', 'password': 'abcd1234', 'role_id': role_id(admin, 'cashier'), 'store_code': 'A001'})
    assert r.status_code == 403 and j(r)['code'] == 'QUOTA_EXCEEDED'


def test_user_crud_pin_and_validation(app, admin, manager):
    cid = role_id(admin, 'cashier')
    r = admin.post('/api/org/users', {'username': 'newbie', 'password': 'abcd1234', 'name': '新人', 'role_id': cid, 'store_code': 'A001',
                                      'emp_no': 'E01', 'pin': '1234', 'phone': '13700000000'})
    u = j(r)['data']
    assert u['role_code'] == 'cashier' and u['has_pin'] and u['must_change_pw'] == 1
    assert admin.post('/api/org/users', {'username': 'newbie', 'password': 'abcd1234', 'role_id': cid, 'store_code': 'A001'}).status_code == 400
    assert admin.post('/api/org/users', {'username': 'x1', 'password': 'short', 'role_id': cid, 'store_code': 'A001'}).status_code == 400
    assert admin.post('/api/org/users', {'username': 'x2', 'password': 'abcd1234', 'role_id': cid}).status_code == 400          # 收银员要绑门店
    assert admin.post('/api/org/users', {'username': 'x3', 'password': 'abcd1234', 'role_id': cid, 'store_code': 'A001', 'emp_no': 'E01'}).status_code == 400
    assert admin.post('/api/org/users', {'username': 'x4', 'password': 'abcd1234', 'role_id': cid, 'store_code': 'A001', 'pin': '12'}).status_code == 400
    assert admin.post('/api/org/users', {'username': 'x5', 'password': 'abcd1234', 'role_id': 99999, 'store_code': 'A001'}).status_code == 400
    # 新账号能登录
    assert Client(app, 'newbie', 'abcd1234').get('/api/me').status_code == 200
    # 店长：只能管本店员工，不能把人建到别的门店，不能分配超出自己权限的角色
    assert manager.post('/api/org/users', {'username': 'x6', 'password': 'abcd1234', 'role_id': cid, 'store_code': 'B001'}).status_code == 403


def test_manager_cannot_escalate(admin, app):
    # 给店长 user.edit，验证不能分配「商户超管」角色
    mgr_role = role_id(admin, 'store_manager')
    admin.post('/api/org/roles', {'copy_from': mgr_role, 'code': 'mgr_plus', 'name': '店长+员工管理'})
    rid = role_id(admin, 'mgr_plus')
    perms = [p for p in [r for r in j(admin.get('/api/org/roles'))['data'] if r['id'] == rid][0]['perms']] + ['user.edit', 'role.edit']
    assert j(admin.post('/api/org/roles', {'id': rid, 'perms': perms}))['ok']
    uid = user_by(admin, 'dianzhang')['id']
    assert j(admin.post('/api/org/users', {'id': uid, 'role_id': rid, 'store_code': 'A001'}))['ok']
    m = Client(app, 'dianzhang')
    r = m.post('/api/org/users', {'username': 'boss2', 'password': 'abcd1234', 'role_id': role_id(admin, 'merchant_admin'), 'store_code': 'A001'})
    assert r.status_code == 403
    r = m.post('/api/org/roles', {'code': 'hack', 'name': 'x', 'perms': ['goods.cost.view', 'report.profit']})
    assert r.status_code == 403
    r = m.post('/api/org/users', {'username': 'okcash', 'password': 'abcd1234', 'role_id': role_id(admin, 'cashier'), 'store_code': 'A001'})
    assert r.status_code == 200


def test_disable_and_transfer_revoke_sessions(app, admin, dbc):
    """AC-013：离职（停用）/调店后旧会话立即 401，重新登录后数据范围立即是新门店。"""
    c = Client(app, 'shouyin01')
    assert c.get('/api/pos/context').status_code == 200
    u = user_by(admin, 'shouyin01')
    # 调店 A001 -> B001
    assert j(admin.post('/api/org/users', {'id': u['id'], 'role_id': u['role_id'], 'store_code': 'B001', 'reason': '调店'}))['ok']
    assert c.get('/api/me').status_code == 401
    c2 = Client(app, 'shouyin01')
    assert j(c2.get('/api/me'))['data']['stores'] == ['B001']
    # 离职（停用）
    assert j(admin.post('/api/org/users', {'id': u['id'], 'role_id': u['role_id'], 'store_code': 'B001', 'status': '停用'}))['ok']
    assert c2.get('/api/me').status_code == 401
    assert app.test_client().post('/api/login', json={'username': 'shouyin01', 'password': '123456'}).status_code == 401
    acts = [r[0] for r in dbc.execute("SELECT action FROM audit_logs WHERE ref_no='shouyin01' ORDER BY id")]
    assert acts == ['员工调店', '员工离职/停用']
    # 不能停用自己
    me = user_by(admin, 'root')
    assert admin.post('/api/org/users', {'id': me['id'], 'role_id': me['role_id'], 'status': '停用'}).status_code == 400


def test_extra_store_scope_takes_effect(app, admin):
    """数据范围：给店长额外授权 B001，立即能看 B001（重新登录）。"""
    u = user_by(admin, 'dianzhang')
    assert j(admin.post('/api/org/users', {'id': u['id'], 'role_id': u['role_id'], 'store_code': 'A001', 'scopes': [{'type': 'store', 'code': 'B001'}]}))['ok']
    m = Client(app, 'dianzhang')
    assert j(m.get('/api/me'))['data']['stores'] == ['A001', 'B001']
    assert admin.post('/api/org/users', {'id': u['id'], 'role_id': u['role_id'], 'store_code': 'A001',
                                         'scopes': [{'type': 'store', 'code': 'Z001'}]}).status_code == 400          # 别家门店


def test_sessions_list_and_kick(app, admin, dbc):
    c = Client(app, 'dianzhang')
    uid = user_by(admin, 'dianzhang')['id']
    s = j(admin.get('/api/org/users/%d/sessions' % uid))['data']
    assert s and s[0]['active'] and s[0]['sid'].endswith('…')
    r = j(admin.c.delete('/api/org/users/%d/sessions' % uid))
    assert r['data']['revoked'] >= 1
    assert c.get('/api/me').status_code == 401
    assert dbc.execute("SELECT level FROM audit_logs WHERE action='踢下线'").fetchone()[0] == 'security'


def test_roles_custom_copy_and_limits(app, admin, dbc):
    reg = j(admin.get('/api/org/perms'))['data']
    codes = [p['code'] for g in reg['groups'] for p in g['perms']]
    assert 'goods.cost.view' in codes and not any(c.startswith('platform.') for c in codes)
    # 系统角色：权限码不能直接改，额度可以改
    cid = role_id(admin, 'cashier')
    assert admin.post('/api/org/roles', {'id': cid, 'perms': ['pos.login']}).status_code == 400
    r = j(admin.post('/api/org/roles', {'id': cid, 'limits': {'min_discount_rate': '0.85'}, 'reason': '放宽到 85 折'}))
    assert r['data']['limits']['min_discount_rate'] == '0.85'
    assert admin.post('/api/org/roles', {'id': cid, 'limits': {'min_discount_rate': '2'}}).status_code == 400
    assert admin.post('/api/org/roles', {'id': cid, 'limits': {'nope': '1'}}).status_code == 400
    # 复制 + 自定义
    r = j(admin.post('/api/org/roles', {'copy_from': cid, 'code': 'cashier_lead', 'name': '收银组长'}))['data']
    assert r['is_system'] == 0 and 'pos.checkout' in r['perms'] and r['limits']['min_discount_rate'] == '0.85'
    r = j(admin.post('/api/org/roles', {'id': r['id'], 'perms': r['perms'] + ['pos.authorize'], 'data_scope': 'store'}))['data']
    assert 'pos.authorize' in r['perms']
    assert admin.post('/api/org/roles', {'id': r['id'], 'perms': ['platform.merchant.open']}).status_code == 400
    assert admin.post('/api/org/roles', {'code': 'cashier', 'name': 'x'}).status_code == 400
    assert admin.post('/api/org/roles', {'code': 'Bad Code', 'name': 'x'}).status_code == 400
    # 权限变化立即生效（不需要重新登录）
    lead = j(admin.post('/api/org/users', {'username': 'lead1', 'password': 'abcd1234', 'role_id': r['id'], 'store_code': 'A001'}))['data']
    c = Client(app, 'lead1', 'abcd1234')
    assert 'pos.authorize' in j(c.get('/api/me'))['data']['perms']
    admin.post('/api/org/roles', {'id': r['id'], 'perms': [p for p in r['perms'] if p != 'pos.authorize']})
    assert 'pos.authorize' not in j(c.get('/api/me'))['data']['perms']
    assert lead['role_code'] == 'cashier_lead'
    n = dbc.execute("SELECT COUNT(*) FROM audit_logs WHERE module='role' AND level='high'").fetchone()[0]
    assert n >= 4


def test_audit_query_scoped(app, admin, manager, m2admin):
    admin.post('/api/org/stores', {'code': 'B001', 'name': '乙店改'})
    admin.post('/api/org/stores', {'code': 'A001', 'name': '甲店改'})
    a = j(admin.get('/api/org/audit?module=org'))['data']
    assert a['total'] >= 2 and {x['store_code'] for x in a['items']} >= {'A001', 'B001'}
    assert j(admin.get('/api/org/audit?ref_no=A001'))['data']['items'][0]['ref_no'] == 'A001'
    assert manager.get('/api/org/audit').status_code == 403            # 店长没有 audit.view
    caiwu = Client(app, 'caiwu')
    assert j(caiwu.get('/api/org/audit?store=A001'))['data']['total'] >= 1
    assert j(m2admin.get('/api/org/audit'))['data']['total'] == 0       # 别家看不到
    assert m2admin.get('/api/org/audit?store=A001').status_code == 403


def test_users_list_scope_and_masking(app, admin, manager, m2admin):
    names = {u['username'] for u in j(manager.get('/api/org/users?size=200'))['data']['items']}
    assert 'shouyin01' in names and 'shouyinB' not in names and 'root' not in names
    assert manager.get('/api/org/users?store=B001').status_code == 403
    names2 = {u['username'] for u in j(m2admin.get('/api/org/users?size=200'))['data']['items']}
    assert names2 == {'m2admin', 'm2cashier'}
    assert m2admin.post('/api/org/users', {'id': user_by(admin, 'shouyin01')['id'], 'role_id': role_id(m2admin, 'cashier'),
                                           'store_code': 'Z001'}).status_code == 404


def test_guides_crud_scope_and_pos(app, admin, manager, m2admin, dbc):
    """导购（集成轮从 views_admin 迁到 api/org/guides）：增改停用 + 审计 + 数据范围 + 收银台只列在职导购。"""
    lst = j(admin.get('/api/org/guides'))['data']
    assert [g['code'] for g in lst] == ['G1'] and lst[0]['store_name'] == '甲店'
    assert j(admin.post('/api/org/guides', {'code': 'G2', 'name': '小李', 'store_code': 'B001'}))['data']['status'] == '在职'
    assert admin.post('/api/org/guides', {'code': 'G3', 'name': '', 'store_code': 'A001'}).status_code == 400
    assert admin.post('/api/org/guides', {'code': 'G3', 'name': 'x', 'store_code': 'NOPE'}).status_code == 400
    assert manager.post('/api/org/guides', {'code': 'G9', 'name': 'x', 'store_code': 'A001'}).status_code == 403    # 店长无 org.edit
    assert [g['code'] for g in j(manager.get('/api/org/guides'))['data']] == ['G1']                                  # 店长只看本店
    assert j(m2admin.get('/api/org/guides'))['data'] == []                                                           # 别家商户看不到
    assert j(m2admin.post('/api/org/guides', {'code': 'G1', 'name': '别家导购', 'store_code': 'Z001'}))['ok']      # 编号商户内唯一
    assert dbc.execute("SELECT name FROM guides WHERE merchant_id=1 AND code='G1'").fetchone()[0] == '小王'
    # 停用：收银台导购列表不再出现
    assert j(admin.post('/api/org/guides', {'code': 'G1', 'name': '小王', 'store_code': 'A001', 'status': '离职', 'reason': '离职'}))['ok']
    cashier = Client(app, 'shouyin01')
    ctx = j(cashier.get('/api/pos/context'))
    guides = (ctx.get('data') or ctx).get('guides')
    assert guides is not None and all(g['guide_code'] != 'G1' for g in guides)
    acts = [r[0] for r in dbc.execute("SELECT action FROM audit_logs WHERE merchant_id=1 AND module='org' AND ref_no IN ('G1','G2')")]
    assert '新增导购' in acts and '停用导购' in acts


def test_session_last_seen_touched_every_5_minutes(app, dbc):
    """REPORT-4 §5-4：current_actor 每 5 分钟最多更新一次 user_sessions.last_seen（设备列表的最后活跃）。"""
    c = Client(app, 'dianzhang')
    sid = dbc.execute("SELECT s.sid FROM user_sessions s JOIN users u ON u.id=s.user_id WHERE u.username='dianzhang' "
                      "AND s.revoked_at IS NULL ORDER BY s.created_at DESC").fetchone()[0]
    dbc.execute("UPDATE user_sessions SET last_seen='2026-01-01 00:00:00' WHERE sid=?", (sid,))
    assert c.get('/api/me').status_code == 200
    first = dbc.execute('SELECT last_seen FROM user_sessions WHERE sid=?', (sid,)).fetchone()[0]
    assert first > '2026-01-01 00:00:00'
    recent = first[:17] + '00'                                    # 5 分钟内：不再写库
    dbc.execute('UPDATE user_sessions SET last_seen=? WHERE sid=?', (recent, sid))
    assert c.get('/api/me').status_code == 200
    assert dbc.execute('SELECT last_seen FROM user_sessions WHERE sid=?', (sid,)).fetchone()[0] == recent
