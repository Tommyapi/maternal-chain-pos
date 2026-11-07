# -*- coding: utf-8 -*-
"""模块② 价格促销券 HTTP 接口：活动审核状态机、试算、快照不受活动修改影响、调价单、券生命周期、越权。"""
import json

from conftest import Client
from shouyin_app.db import connect


def grant(app, username, perms):
    db = connect(app.config['DB_PATH'])
    rid = db.execute('SELECT role_id FROM users WHERE username=?', (username,)).fetchone()[0]
    for p in perms:
        db.execute('INSERT OR IGNORE INTO role_perms(role_id, perm) VALUES(?,?)', (rid, p))
    db.commit()
    db.close()


def new_act(c, **kw):
    body = {'name': '满300减20', 'type': 'full_reduce', 'rule': {'tiers': [{'threshold': 300, 'reduce': 20}]},
            'start_at': '2026-01-01', 'end_at': '2099-12-31', 'priority': 10}
    body.update(kw)
    r = c.post('/api/promo/activities', body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()['data']


def approve_flow(admin, approver, no):
    r = admin.post('/api/promo/activities/%s/submit' % no)
    assert r.get_json()['data']['status'] == 'submitted'
    r = admin.post('/api/promo/activities/%s/approve' % no)
    assert r.status_code == 403                    # 不能审批自己的申请
    r = approver.post('/api/promo/activities/%s/approve' % no)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['data']['status'] == 'approved'


def test_activity_lifecycle_and_try(app, admin):
    grant(app, 'caiwu', ['promo.approve', 'promo.view', 'promo.edit'])
    fin = Client(app, 'caiwu')
    a = new_act(admin)
    no = a['act_no']
    assert no.startswith('AC001') and a['status'] == 'draft' and '满' in a['explain']
    cart = {'store_code': 'A001', 'items': [{'code': 'P1', 'qty': 1}]}
    r = admin.post('/api/promo/try', cart).get_json()['data']
    assert r['payable'] == 400.0                    # 草稿不生效
    approve_flow(admin, fin, no)
    r = admin.post('/api/promo/try', cart).get_json()['data']
    assert r['payable'] == 380.0 and r['discounts'][0]['amount'] == 20.0 and r['lines'][0]['cost'] == 300.0
    # 已审核不能直接改
    assert admin.post('/api/promo/activities', {'act_no': no, 'name': 'x', 'type': 'full_reduce', 'rule': {'tiers': [{'threshold': 300, 'reduce': 50}]},
                                                'start_at': '2026-01-01', 'end_at': '2099-12-31'}).status_code == 409
    assert admin.post('/api/promo/activities/%s/pause' % no, {}).status_code == 400      # 需原因
    assert admin.post('/api/promo/activities/%s/pause' % no, {'reason': '调整'}).get_json()['data']['status'] == 'paused'
    assert admin.post('/api/promo/try', cart).get_json()['data']['payable'] == 400.0
    assert fin.post('/api/promo/activities/%s/resume' % no).get_json()['data']['status'] == 'approved'
    cp = admin.post('/api/promo/activities/%s/copy' % no).get_json()['data']
    assert cp['status'] == 'draft' and cp['rule'] == a['rule']
    assert admin.post('/api/promo/activities/%s/end' % no, {'reason': '结束'}).get_json()['data']['status'] == 'ended'
    assert admin.post('/api/promo/try', cart).get_json()['data']['payable'] == 400.0
    d = admin.get('/api/promo/activities/%s' % no).get_json()['data']
    assert len(d['logs']) >= 4 and d['approvals'][0]['status'] == 'approved'
    lst = admin.get('/api/promo/activities?status=ended').get_json()['data']
    assert lst['total'] == 1


def test_activity_validation(admin):
    assert admin.post('/api/promo/activities', {'name': 'x', 'type': 'bogus', 'start_at': '2026-01-01', 'end_at': '2026-02-01'}).status_code == 400
    assert admin.post('/api/promo/activities', {'name': 'x', 'type': 'special_price', 'rule': {'items': [{'code': 'NOPE', 'price': 1}]},
                                                'start_at': '2026-01-01', 'end_at': '2026-02-01'}).status_code == 400
    assert admin.post('/api/promo/activities', {'name': 'x', 'type': 'full_reduce', 'rule': {'tiers': [{'threshold': 10, 'reduce': 20}]},
                                                'start_at': '2026-01-01', 'end_at': '2026-02-01'}).status_code == 400
    assert admin.post('/api/promo/activities', {'name': 'x', 'type': 'item_discount', 'rule': {'rate': '1.2'},
                                                'start_at': '2026-01-01', 'end_at': '2026-02-01'}).status_code == 400
    assert admin.post('/api/promo/activities', {'name': 'x', 'type': 'item_discount', 'rule': {'rate': '0.9'},
                                                'start_at': '2026-03-01', 'end_at': '2026-02-01'}).status_code == 400


def test_snapshot_unchanged_after_activity_change(app, admin):
    """AC-016：改活动后，已保存的快照（③存进 orders.snapshot）不变；新试算按新规则。"""
    grant(app, 'caiwu', ['promo.approve'])
    fin = Client(app, 'caiwu')
    no = new_act(admin, type='special_price', name='奶粉特价', rule={'items': [{'code': 'P1', 'price': 350}]})['act_no']
    approve_flow(admin, fin, no)
    r1 = admin.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P1', 'qty': 1}]}).get_json()['data']
    snap = json.dumps(r1['snapshot'], sort_keys=True)
    assert r1['payable'] == 350.0 and r1['snapshot']['activities'][0]['rule']['items'][0]['price_cents'] == 35000
    admin.post('/api/promo/activities/%s/end' % no, {'reason': '改价'})
    no2 = new_act(admin, type='special_price', name='奶粉特价2', rule={'items': [{'code': 'P1', 'price': 330}]})['act_no']
    approve_flow(admin, fin, no2)
    r2 = admin.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P1', 'qty': 1}]}).get_json()['data']
    assert r2['payable'] == 330.0
    assert json.dumps(r1['snapshot'], sort_keys=True) == snap and r1['snapshot']['activities'][0]['act_no'] == no


def test_price_adjust_future_effective(app, admin):
    grant(app, 'caiwu', ['price.adjust.approve'])
    fin = Client(app, 'caiwu')
    r = admin.post('/api/promo/price-adjusts', {'scope_type': 'store', 'scope_refs': ['A001'], 'effective_at': '2026-01-01 00:00:00',
                                                 'reason': '门店价', 'lines': [{'code': 'P2', 'field': 'retail', 'new_price': 88}]})
    assert r.status_code == 200, r.get_json()
    pa = r.get_json()['data']
    assert pa['status'] == 'draft' and pa['lines'][0]['old_price'] == 100.0 and pa['lines'][0]['new_price'] == 88.0
    admin.post('/api/promo/price-adjusts/%s/submit' % pa['pa_no'])
    assert admin.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P2', 'qty': 1}]}).get_json()['data']['payable'] == 100.0
    r = fin.post('/api/promo/price-adjusts/%s/approve' % pa['pa_no'])
    assert r.get_json()['data']['status'] == 'effective'
    assert admin.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P2', 'qty': 1}]}).get_json()['data']['payable'] == 88.0
    assert admin.post('/api/promo/try', {'store_code': 'B001', 'items': [{'code': 'P2', 'qty': 1}]}).get_json()['data']['payable'] == 100.0
    # 未来生效的总部调价：审核后待生效，到点自动生效（试算指定时间）
    r = admin.post('/api/promo/price-adjusts', {'scope_type': 'hq', 'effective_at': '2099-01-01 00:00:00',
                                                 'lines': [{'code': 'P1', 'field': 'retail', 'new_price': 420}]}).get_json()['data']
    admin.post('/api/promo/price-adjusts/%s/submit' % r['pa_no'])
    assert fin.post('/api/promo/price-adjusts/%s/approve' % r['pa_no']).get_json()['data']['status'] == 'approved'
    assert admin.post('/api/promo/try', {'store_code': 'B001', 'items': [{'code': 'P1', 'qty': 1}]}).get_json()['data']['payable'] == 400.0
    assert admin.post('/api/promo/try', {'store_code': 'B001', 'at': '2099-02-01', 'items': [{'code': 'P1', 'qty': 1}]}).get_json()['data']['payable'] == 420.0
    prices = admin.get('/api/promo/prices?code=P1').get_json()['data']
    assert prices['items'][0]['scope_type'] == 'hq' and prices['items'][0]['price'] == 420.0


def test_coupon_lifecycle(app, admin):
    r = admin.post('/api/promo/coupon-templates', {'name': '满200减20', 'type': 'cash', 'face_value': 20, 'threshold': 200, 'valid_days': 30,
                                                    'per_member_limit': 1, 'total_limit': 2})
    assert r.status_code == 200, r.get_json()
    tid = r.get_json()['data']['id']
    r = admin.post('/api/promo/coupons/issue', {'template_id': tid, 'member_codes': ['M001', 'M001', 'M002', 'NOPE'], 'request_key': 'k1'}).get_json()['data']
    assert len(r['issued']) == 2 and len(r['skipped']) == 2
    again = admin.post('/api/promo/coupons/issue', {'template_id': tid, 'member_codes': ['M001'], 'request_key': 'k1'}).get_json()['data']
    assert again['duplicate'] and again['issued'] == r['issued']
    m3 = admin.post('/api/promo/coupons/issue', {'template_id': tid, 'member_codes': ['M002']}).get_json()['data']
    assert m3['issued'] == [] and '限领' in m3['skipped'][0]['reason']
    cno = r['issued'][0]
    d = admin.get('/api/promo/coupons/%s' % cno).get_json()['data']
    assert d['status'] == '未使用' and d['logs'][0]['to_status'] == '未使用'
    assert admin.post('/api/promo/coupons/%s/void' % cno, {}).status_code == 400
    assert admin.post('/api/promo/coupons/%s/void' % cno, {'reason': '发错'}).get_json()['data']['status'] == '已作废'
    assert admin.post('/api/promo/coupons/%s/void' % cno, {'reason': '再来'}).status_code == 409
    lst = admin.get('/api/promo/coupons?member_code=M001').get_json()['data']
    assert lst['total'] == 1
    # 收银员不能发券/作废
    cashier = Client(app, 'shouyin01')
    assert cashier.post('/api/promo/coupons/issue', {'template_id': tid, 'member_codes': ['M002']}).status_code == 403
    assert cashier.post('/api/promo/coupons/%s/void' % r['issued'][1], {'reason': 'x'}).status_code == 403


def test_try_shows_member_coupons_and_reasons(admin):
    tid = admin.post('/api/promo/coupon-templates', {'name': '满1000减100', 'type': 'cash', 'face_value': 100, 'threshold': 1000}).get_json()['data']['id']
    admin.post('/api/promo/coupons/issue', {'template_id': tid, 'member_codes': ['M001']})
    r = admin.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001'}).get_json()['data']
    mc = r['member_coupons']
    assert len(mc) == 1 and not mc[0]['usable'] and '门槛' in mc[0]['reason']


def test_promo_cross_merchant_and_perms(app, admin, m2admin):
    no = new_act(admin)['act_no']
    assert m2admin.get('/api/promo/activities/%s' % no).status_code == 404
    assert m2admin.get('/api/promo/activities').get_json()['data']['total'] == 0
    assert m2admin.post('/api/promo/activities/%s/submit' % no).status_code == 404
    assert m2admin.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P1', 'qty': 1}]}).status_code == 403   # 门店不在范围
    assert m2admin.post('/api/promo/try', {'store_code': 'Z001', 'items': [{'code': 'P1', 'qty': 1}]}).status_code == 400   # 商品不是他家的
    cashier = Client(app, 'shouyin01')
    assert cashier.post('/api/promo/activities', {'name': 'x'}).status_code == 403
    assert cashier.get('/api/promo/activities').status_code == 403
    mgr = Client(app, 'dianzhang')
    assert mgr.post('/api/promo/try', {'store_code': 'B001', 'items': [{'code': 'P1', 'qty': 1}]}).status_code == 403
    r = mgr.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P1', 'qty': 1}]}).get_json()['data']
    assert r['lines'][0]['cost'] is None                     # 店长无成本权限
    plat = Client(app, 'platform', 'platpass1')
    assert plat.get('/api/promo/activities').status_code == 403
