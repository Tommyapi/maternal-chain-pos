# -*- coding: utf-8 -*-
"""权限地基：权限码注册表、默认角色、数据范围、字段权限、操作额度/现场授权/审批单、平台与商户隔离、会话吊销、审计不可改。"""
import sqlite3
from decimal import Decimal

import pytest

from conftest import Client
from shouyin_app.perms import PERMS, ROLE_TEMPLATES, LIMIT_KEYS
from shouyin_app.core import _load_actor, scope_stores, scope_sql, mask_phone, cost_view, check_limit_or_approval, audit, revoke_user_sessions
from shouyin_app.util import BizError
from shouyin_app.db import connect


def actor_of(db, username):
    return _load_actor(db, db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone())


def test_registry_consistent():
    for code, t in ROLE_TEMPLATES.items():
        for p in t['perms']:
            assert p in PERMS, (code, p)
        for k in t['limits']:
            assert k in LIMIT_KEYS
    assert all(not p.startswith('platform.') for p in ROLE_TEMPLATES['merchant_admin']['perms'])
    assert 'goods.cost.view' not in ROLE_TEMPLATES['cashier']['perms'] and 'member.export' not in ROLE_TEMPLATES['cashier']['perms']
    assert 'member.export' not in ROLE_TEMPLATES['member_ops']['perms']          # 导出单独授权


def test_me_exposes_role_perms_and_scope(app):
    me = Client(app, 'shouyin01').get('/api/me').get_json()
    assert me['ok'] and me['data']['role_code'] == 'cashier' and me['data']['stores'] == ['A001']
    assert 'pos.checkout' in me['data']['perms'] and 'goods.cost.view' not in me['data']['perms']
    root = Client(app, 'root', 'rootpass1').get('/api/me').get_json()['data']
    assert root['role_code'] == 'merchant_admin' and root['stores'] == ['A001', 'B001'] and root['merchant_name'] == '萌贝母婴连锁'
    assert Client(app, 'm2admin').get('/api/me').get_json()['data']['stores'] == ['Z001']
    assert Client(app, 'quyu').get('/api/me').get_json()['data']['stores'] == ['A001']


def test_scope_and_field_helpers(app, dbc):
    with app.test_request_context():
        cashier, root, quyu, m2 = (actor_of(dbc, u) for u in ('shouyin01', 'root', 'quyu', 'm2admin'))
        assert scope_stores(dbc, cashier) == ['A001'] and scope_stores(dbc, root, 'B001') == ['B001']
        assert scope_stores(dbc, root, 'A001,B001') == ['A001', 'B001']
        for a, req in ((cashier, 'B001'), (quyu, 'B001'), (m2, 'A001'), (root, 'Z001')):
            with pytest.raises(BizError) as e:
                scope_stores(dbc, a, req)
            assert e.value.status == 403 and e.value.code == 'SCOPE_DENIED'
        sql, args = scope_sql(dbc, cashier, 'o.store_code', None, 'o.merchant_id')
        assert sql == ' AND o.merchant_id=? AND o.store_code IN (?)' and args == [1, 'A001']
        assert mask_phone('13800000001', cashier) == '138****0001' and mask_phone('13800000001', root) == '13800000001'
        assert cost_view(cashier, 12345) is None and cost_view(root, 12345) == 123.45
        assert cost_view(actor_of(dbc, 'caiwu'), 100) == 1.0


def test_platform_and_merchant_isolation(app, platform, m2admin, admin):
    assert platform.get('/api/platform/merchants').status_code == 200             # 平台账号可进平台接口
    for url in ('/api/pos/context', '/api/goods', '/api/report/sales', '/api/org/users', '/api/approvals'):
        r = platform.get(url)
        assert r.status_code == 403, url                                          # 平台账号默认看不到商户业务数据
    assert admin.get('/api/platform/merchants').status_code == 403                # 商户超管进不了平台
    # 商户 2：v1 旧接口已全部删除（集成轮），看板走 api/m/dashboard，只返回自己商户的数据
    r = m2admin.get('/api/m/dashboard')
    assert r.status_code == 200 and r.get_json()['data']['scope']['stores'] == ['Z001']
    r = m2admin.get('/api/goods')
    assert r.status_code == 200 and 'P1' not in [i.get('code') for i in r.get_json()['data']['items']]   # 新接口按商户隔离
    assert m2admin.get('/api/pos/context').get_json()['data']['store']['code'] == 'Z001'               # 收银台已按商户隔离


def test_error_contract(app, cashier):
    r = cashier.get('/api/org/users')                                             # 收银员无 user.view
    d = r.get_json()
    assert r.status_code == 403 and d['ok'] is False and d['code'] == 'FORBIDDEN' and d['msg'] == d['error']
    d = app.test_client().get('/api/me').get_json()
    assert d['code'] == 'UNAUTHORIZED'
    assert app.test_client().get('/api/nothing').get_json()['code'] == 'NOT_FOUND'


def test_limit_auth_and_approval_flow(app, dbc):
    from shouyin_app.core import tx
    with app.test_request_context():
        cashier = actor_of(dbc, 'shouyin01')
        with tx(dbc):
            assert check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.95'), {}, 'A001')['mode'] == 'self'
        with pytest.raises(BizError) as e:
            check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.8'), {}, 'A001')
        assert e.value.code == 'NEED_AUTH'
        with tx(dbc):
            r = check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.8'), {'auth_user': 'dianzhang', 'auth_pass': '123456'}, 'A001', 'S1')
        assert r == {'mode': 'auth', 'by': 'dianzhang'}
        for bad in ({'auth_user': 'dianzhangB', 'auth_pass': '123456'},      # 别店店长
                    {'auth_user': 'dianzhang', 'auth_pass': 'wrong'},
                    {'auth_user': 'm2admin', 'auth_pass': '123456'}):         # 别的商户
            with pytest.raises(BizError) as e:
                with tx(dbc):
                    check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.8'), bad, 'A001')
            assert e.value.code == 'AUTH_FAILED'
        with pytest.raises(BizError) as e:
            with tx(dbc):
                check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.6'), {'auth_user': 'dianzhang', 'auth_pass': '123456'}, 'A001')
        assert e.value.code == 'AUTH_FAILED'                                   # 店长自己额度 0.7，也批不了 0.6
        with pytest.raises(BizError) as e:
            check_limit_or_approval(dbc, cashier, 'refund', 100, {}, 'A001')
        assert e.value.code == 'NEED_AUTH' and e.value.data['kind'] == 'refund'        # 收银员退款一律要授权
        assert check_limit_or_approval(dbc, actor_of(dbc, 'dianzhang'), 'refund', 100, {}, 'A001')['mode'] == 'self'


def test_approval_api_end_to_end(app, dbc):
    from shouyin_app.core import tx
    cas0 = Client(app, 'shouyin01')
    body = {'kind': 'discount', 'store_code': 'A001', 'value': '0.8', 'title': '整单 8 折', 'reason': '老顾客', 'request_key': 'k2'}
    ap = cas0.post('/api/approvals', body).get_json()['data']
    assert cas0.post('/api/approvals', body).get_json()['data']['approval_no'] == ap['approval_no']      # 幂等
    assert cas0.post('/api/approvals', dict(body, store_code='B001', request_key='k3')).status_code == 403  # 不能替别店申请
    assert cas0.post('/api/approvals', dict(body, kind='stock_adjust')).status_code == 400
    no = ap['approval_no']
    assert no.startswith('AP001A001')
    mgr, mgrB, cas = Client(app, 'dianzhang'), Client(app, 'dianzhangB'), Client(app, 'shouyin01')
    todo = mgr.get('/api/notify?todo=1').get_json()['data']
    assert todo['unread'] >= 1 and todo['items'][0]['ref_no'] == no
    assert mgrB.get('/api/notify').get_json()['data']['total'] == 0                 # 别店店长收不到
    assert [x['approval_no'] for x in mgr.get('/api/approvals?box=todo').get_json()['data']['items']] == [no]
    assert mgrB.get('/api/approvals?box=todo').get_json()['data']['total'] == 0
    assert mgrB.post('/api/approvals/%s/approve' % no).status_code == 403
    assert cas.post('/api/approvals/%s/approve' % no).status_code == 403              # 收银员没有审批权
    assert mgr.post('/api/approvals/%s/reject' % no, {}).status_code == 400          # 驳回要原因
    d = mgr.post('/api/approvals/%s/approve' % no, {'reason': '同意'}).get_json()
    assert d['ok'] and d['data']['status'] == 'approved'
    assert mgr.post('/api/approvals/%s/approve' % no).status_code == 409
    assert mgr.get('/api/notify?todo=1&status=unread').get_json()['data']['total'] == 0   # 待办已闭环
    assert cas.get('/api/approvals?box=mine').get_json()['data']['items'][0]['status'] == 'approved'
    with app.test_request_context():
        cashier = actor_of(dbc, 'shouyin01')
        with pytest.raises(BizError):
            with tx(dbc):
                check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.75'), {'approval_no': no}, 'A001')   # 超出批准范围
        with tx(dbc):
            r = check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.85'), {'approval_no': no}, 'A001')
        assert r['mode'] == 'approval' and r['by'] == '演示店长'
        with pytest.raises(BizError):
            with tx(dbc):
                check_limit_or_approval(dbc, cashier, 'discount', Decimal('0.85'), {'approval_no': no}, 'A001')   # 只能用一次
    logs = dbc.execute('SELECT to_status FROM approval_logs WHERE approval_no=? ORDER BY id', (no,)).fetchall()
    assert [r[0] for r in logs] == ['pending', 'approved', 'consumed']


def test_audit_is_append_only(app, dbc):
    with app.test_request_context():
        a = actor_of(dbc, 'root')
        audit(dbc, a, 'goods', '改价', 'P1', {'retail': 400}, {'retail': 410}, '调价', level='high')
    row = dbc.execute("SELECT * FROM audit_logs WHERE ref_no='P1'").fetchone()
    assert row['merchant_id'] == 1 and row['username'] == 'root' and '410' in row['after_json']
    with pytest.raises(sqlite3.DatabaseError):
        dbc.execute("UPDATE audit_logs SET reason='x'")
    with pytest.raises(sqlite3.DatabaseError):
        dbc.execute('DELETE FROM audit_logs')


def test_session_revoke_and_merchant_status(app, dbc):
    c = Client(app, 'shouyin01')
    assert c.get('/api/me').status_code == 200
    uid = dbc.execute("SELECT id FROM users WHERE username='shouyin01'").fetchone()[0]
    revoke_user_sessions(dbc, uid, '调店')
    assert c.get('/api/me').status_code == 401                                       # 立即失效（APP-405）
    assert Client(app, 'shouyin01').get('/api/me').status_code == 200                # 重新登录可用
    dbc.execute("UPDATE merchants SET status='expired' WHERE id=2")
    m2 = Client(app, 'm2admin')
    assert m2.get('/api/goods').status_code == 200                                   # 到期：可查
    r = m2.post('/api/approvals/X/cancel')
    assert r.status_code == 403 and r.get_json()['code'] == 'MERCHANT_READONLY'      # 到期：不能写
    dbc.execute("UPDATE merchants SET status='disabled' WHERE id=2")
    r = app.test_client().post('/api/login', json={'username': 'm2admin', 'password': '123456'})
    assert r.status_code == 403 and r.get_json()['code'] == 'MERCHANT_DISABLED'
    assert m2.get('/api/me').status_code == 200 and m2.get('/api/goods').status_code == 403


def test_logout_revokes_session(app):
    c = Client(app, 'dianzhang')
    cookie_client = c.c
    assert cookie_client.post('/api/logout').status_code == 200
    assert c.get('/api/me').status_code == 401
