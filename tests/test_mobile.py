# -*- coding: utf-8 -*-
"""卡⑤ 手机端 H5：看板/趋势/排行/毛利、门店切换与数据范围、门店经营、商品库存、审批待办闭环、公告、提醒规则、版本、设备会话、
停用/调店会话失效（AC-013）、跨商户越权（AC-012 APP 侧）、前端相对路径。"""
import json
import os
import re
from datetime import datetime, timedelta

import pytest

from conftest import Client

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'shouyin_app', 'static')
TODAY = datetime.now().date()
SEQ = [0]


def ts(day, hm='10:00:00'):
    return day.strftime('%Y-%m-%d') + ' ' + hm


def add_order(db, store, payable, when, biz='sale', mid=1, member='', cashier='shouyin01', items=None, member_phone=''):
    """直接写订单（不依赖③的结算实现，便于构造任意日期的数据）。items: [(code, name, category, qty, retail, price, amount, cost)] 元。"""
    SEQ[0] += 1
    no = '%s%05d' % ('T' if biz == 'sale' else 'TR', SEQ[0])
    qty = sum(i[3] for i in (items or [])) or (1 if biz == 'sale' else -1)
    db.execute("INSERT INTO orders(merchant_id, order_no, biz_type, store_code, store_name, cashier_user, cashier_name, member_code, member_name, "
               "member_phone, item_qty, payable_cents, discount_total_cents, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (mid, no, biz, store, store, cashier, cashier, member, '会员' if member else '', member_phone, qty, int(round(payable * 100)), 0,
                '已完成', when))
    for n, it in enumerate(items or [], 1):
        code, name, cat, q, retail, price, amount, cost = it
        pid = db.execute('SELECT id FROM products WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
        db.execute("INSERT INTO order_items(merchant_id, order_no, line_no, product_id, product_code, name, category, brand, qty, retail_cents, "
                   "price_cents, line_amount_cents, amount_cents, cost_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (mid, no, n, pid[0] if pid else None, code, name, cat, '', q, int(retail * 100), int(price * 100), int(amount * 100),
                    int(amount * 100), int(cost * 100)))
    return no


def keys_of(obj):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= keys_of(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= keys_of(v)
    return out


@pytest.fixture()
def sales(dbc):
    y, lw = TODAY - timedelta(days=1), TODAY - timedelta(days=7)
    add_order(dbc, 'A001', 100, ts(TODAY, '09:10:00'), member='M001', items=[('P2', '纸尿裤L', '尿裤', 1, 100, 100, 100, 60)])
    add_order(dbc, 'A001', 100, ts(TODAY, '11:20:00'), member='M001', items=[('P1', '奶粉一段800g', '奶粉', 1, 400, 100, 100, 300)])
    add_order(dbc, 'A001', -30, ts(TODAY, '12:00:00'), biz='refund', items=[('P2', '纸尿裤L', '尿裤', -0.3, 100, 100, -30, -18)])
    add_order(dbc, 'B001', 200, ts(TODAY, '15:00:00'), cashier='shouyinB', items=[('P2', '纸尿裤L', '尿裤', 2, 100, 100, 200, 120)])
    add_order(dbc, 'A001', 50, ts(y), items=[('P2', '纸尿裤L', '尿裤', 1, 100, 50, 50, 60)])
    add_order(dbc, 'A001', 80, ts(lw))
    add_order(dbc, 'Z001', 999, ts(TODAY), mid=2)               # 别家商户
    return True


# ---------------------------------------------------------------- APP-001 / APP-005 看板与门店切换
def test_dashboard_totals_and_compare(admin, sales):
    d = admin.get('/api/m/dashboard').get_json()['data']
    c = d['current']
    assert c['sales'] == 400.0 and c['orders'] == 3 and c['refund'] == 30.0 and c['refund_orders'] == 1 and c['net'] == 370.0
    assert c['avg'] == 133.33 and c['members'] == 1
    assert d['scope']['stores'] == ['A001', 'B001'] and '总部' in d['scope']['label']
    assert d['compare']['prev']['stats']['sales'] == 50.0 and d['compare']['prev']['pct']['sales'] == 700.0
    assert d['compare']['same']['stats']['sales'] == 80.0 and d['compare']['same']['label'] == '较上周同期'
    assert d['updated_at'] and d['latest_order_at'].startswith(TODAY.isoformat()) and d['caliber']
    # 单店 / 区域切换后所有指标用同一范围
    a = admin.get('/api/m/dashboard?store=A001').get_json()['data']
    assert a['current']['sales'] == 200.0 and a['scope']['stores'] == ['A001']
    r = admin.get('/api/m/dashboard?region=R1').get_json()['data']
    assert r['scope']['stores'] == ['A001'] and r['current']['sales'] == 200.0 and r['scope']['label'] == '一区'
    assert admin.get('/api/m/dashboard?region=NOPE').status_code == 404
    # 周/月
    w = admin.get('/api/m/dashboard?period=week').get_json()['data']
    assert w['current']['sales'] >= 400.0 and 'prev' in w['compare'] and 'same' not in w['compare']
    assert admin.get('/api/m/dashboard?period=year').status_code == 400
    assert admin.get('/api/m/dashboard?date=2026/1/1').status_code == 400


def test_dashboard_scope_enforced(app, sales):
    mgr = Client(app, 'dianzhang')
    d = mgr.get('/api/m/dashboard').get_json()['data']
    assert d['scope']['stores'] == ['A001'] and d['current']['sales'] == 200.0                 # 店长只看本店
    r = mgr.get('/api/m/dashboard?store=B001')
    assert r.status_code == 403 and r.get_json()['code'] == 'SCOPE_DENIED'                      # 切换越权被拒
    assert mgr.get('/api/m/dashboard?store=A001,B001').status_code == 403
    q = Client(app, 'quyu')                                                                      # 区域经理：R1 = A001
    assert q.get('/api/m/dashboard').get_json()['data']['scope']['stores'] == ['A001']
    assert q.get('/api/m/dashboard?region=R1').status_code == 200
    assert q.get('/api/m/dashboard?store=B001').get_json()['code'] == 'SCOPE_DENIED'
    sc = q.get('/api/m/scopes').get_json()['data']
    assert not sc['all'] and [s['code'] for s in sc['stores']] == ['A001'] and [x['code'] for x in sc['regions']] == ['R1']
    assert Client(app, 'shouyin01').get('/api/m/dashboard').status_code == 403                  # 收银员无看板权限


def test_no_profit_fields_without_permission(app, admin, sales):
    banned = {'profit', 'cost', 'gross', 'margin', 'cost_cents', 'profit_cents'}
    mgr = Client(app, 'dianzhang')
    for c in (mgr, admin):
        for url in ('/api/m/dashboard', '/api/m/trend', '/api/m/rank?dim=product', '/api/m/rank?dim=category', '/api/m/rank',
                    '/api/m/overview-stock-member'):
            body = c.get(url).get_json()
            assert body['ok'], url
            assert not (keys_of(body) & banned), (url, keys_of(body) & banned)
    r = mgr.get('/api/m/profit')
    assert r.status_code == 403                                                                  # 店长无 report.profit
    assert mgr.get('/api/m/dashboard').get_json()['data']['can_profit'] is False
    assert Client(app, 'quyu').get('/api/m/profit').status_code == 403
    # 有 goods.cost.view 的商品查询才返回成本
    goods = mgr.get('/api/m/goods?kw=P1').get_json()['data']['items'][0]
    assert 'cost' not in goods
    assert admin.get('/api/m/goods?kw=P1').get_json()['data']['items'][0]['cost'] == 300.0


def test_profit_and_abnormal(app, sales):
    fin = Client(app, 'caiwu')                       # 财务：report.profit + goods.cost.view
    d = fin.get('/api/m/profit').get_json()['data']
    assert d['sales'] == 370.0 and d['cost'] == 462.0 and d['gross'] == -92.0 and d['refund'] == 30.0
    ab = d['abnormal']
    assert len(ab) == 1 and ab[0]['name'] == '奶粉一段800g' and ab[0]['rate'] == 0.25          # 400 卖 100
    assert fin.get('/api/m/profit?store=A001').get_json()['data']['sales'] == 170.0
    assert fin.get('/api/m/profit?threshold=x').status_code == 400


def test_trend_and_rank(admin, sales):
    t = admin.get('/api/m/trend').get_json()['data']
    assert len(t['points']) == 14 and t['points'][-1]['sales'] == 400.0 and t['points'][-2]['sales'] == 50.0
    assert t['hours'][15] == 200.0 and t['hours'][12] == -30.0
    assert len(admin.get('/api/m/trend?period=week').get_json()['data']['points']) == 12
    assert len(admin.get('/api/m/trend?period=month').get_json()['data']['points']) == 12
    st = admin.get('/api/m/rank?dim=store').get_json()['data']['items']
    assert [x['key'] for x in st] == ['B001', 'A001'] and st[0]['sales'] == 200.0 and st[1]['sales'] == 170.0
    assert st[0]['name'] == '乙店' and abs(st[0]['share'] + st[1]['share'] - 100) < 0.2
    pr = admin.get('/api/m/rank?dim=product').get_json()['data']['items']
    assert pr[0]['key'] == 'P2' and pr[0]['sales'] == 270.0
    cat = admin.get('/api/m/rank?dim=category').get_json()['data']['items']
    assert {x['key'] for x in cat} == {'尿裤', '奶粉'}
    assert admin.get('/api/m/rank?dim=nope').status_code == 400


# ---------------------------------------------------------------- AC-012：跨商户（APP 接口）
def test_other_merchant_cannot_see_merchant1(app, sales, admin):
    m2 = Client(app, 'm2admin')
    d = m2.get('/api/m/dashboard').get_json()['data']
    assert d['scope']['stores'] == ['Z001'] and d['current']['sales'] == 999.0                 # 只看到自己的
    for url in ('/api/m/dashboard?store=A001', '/api/m/rank?store=A001', '/api/m/store?store=A001', '/api/m/goods?kw=P1&store=A001',
                '/api/m/overview-stock-member?store=A001', '/api/m/trend?store=A001'):
        r = m2.get(url)
        assert r.status_code == 403 and r.get_json()['code'] == 'SCOPE_DENIED', url
    assert m2.get('/api/m/dashboard?region=R1').status_code == 404
    assert m2.get('/api/m/goods?kw=P1').get_json()['data']['items'] == []                      # 1 号商户商品查不到
    assert m2.get('/api/m/goods?kw=6900000000011').get_json()['data']['items'] == []
    ranks = m2.get('/api/m/rank?dim=product').get_json()['data']['items']
    assert all(x['key'] != 'P1' and x['key'] != 'P2' for x in ranks)
    for url in ('/api/m/overview', '/api/m/stock?kw=P1', '/api/m/member?kw=13800000001', '/api/expiry'):     # v1 旧接口已删除
        assert m2.get(url).status_code == 404, url
    # 公告 / 消息 / 审批 按 id 访问别家：404
    aid = admin.post('/api/m/announcements', {'title': '总部通知', 'body': '内容'}).get_json()['data']['id']
    assert m2.get('/api/m/announcements/%d' % aid).status_code == 404
    assert m2.post('/api/m/announcements/%d/read' % aid).status_code == 404
    assert m2.get('/api/m/announcements').get_json()['data']['total'] == 0
    nid = Client(app, 'dianzhang').get('/api/notify').get_json()['data']['items'][0]['id']
    assert m2.post('/api/notify/%d/done' % nid).status_code == 404
    assert m2.post('/api/notify/read', {'ids': [nid]}).get_json()['data']['updated'] == 0
    # 平台账号看不到商户经营数据
    p = Client(app, 'platform', 'platpass1')
    assert p.get('/api/m/dashboard').status_code == 403 and p.get('/api/m/home').status_code == 403


# ---------------------------------------------------------------- AC-013：停用 / 调店后 APP 会话失效
def test_disabled_or_moved_user_cut_off(app, dbc, sales):
    from shouyin_app.core import revoke_user_sessions
    mgr = Client(app, 'dianzhang')
    assert mgr.get('/api/m/home').status_code == 200
    dbc.execute("UPDATE users SET status='停用' WHERE username='dianzhang'")
    for url in ('/api/m/home', '/api/m/dashboard', '/api/m/store', '/api/notify', '/api/m/announcements'):
        r = mgr.get(url)
        assert r.status_code == 401 and r.get_json()['code'] == 'UNAUTHORIZED', url
    dbc.execute("UPDATE users SET status='启用' WHERE username='dianzhang'")
    mgr2 = Client(app, 'dianzhang')
    assert mgr2.get('/api/m/dashboard').get_json()['data']['scope']['stores'] == ['A001']
    uid = dbc.execute("SELECT id FROM users WHERE username='dianzhang'").fetchone()[0]
    dbc.execute("UPDATE users SET store_code='B001' WHERE id=?", (uid,))
    revoke_user_sessions(dbc, uid, '调店')                                                      # ④调店时调用
    assert mgr2.get('/api/m/dashboard').status_code == 401
    mgr3 = Client(app, 'dianzhang')
    d = mgr3.get('/api/m/dashboard').get_json()['data']
    assert d['scope']['stores'] == ['B001'] and d['current']['sales'] == 200.0                 # 新范围立即生效
    assert mgr3.get('/api/m/store?store=A001').status_code == 403


# ---------------------------------------------------------------- APP-101 门店经营
def test_store_ops(app, dbc, sales):
    add_order(dbc, 'A001', 10, ts(TODAY, '16:00:00'), member='M001', member_phone='13800000001')
    mgr = Client(app, 'dianzhang')
    d = mgr.get('/api/m/store').get_json()['data']
    assert d['store']['code'] == 'A001' and d['stats']['sales'] == 210.0 and d['stats']['refund'] == 30.0
    assert d['cashiers'][0]['user'] == 'shouyin01' and d['cashiers'][0]['orders'] == 3
    assert any('138****0001' in x['member'] for x in d['recent'])
    assert '13800000001' not in json.dumps(d, ensure_ascii=False)                                 # 店长无 member.phone.view
    y = (TODAY - timedelta(days=1)).isoformat()
    assert mgr.get('/api/m/store?start=%s&end=%s' % (y, y)).get_json()['data']['stats']['sales'] == 50.0
    assert mgr.get('/api/m/store?store=B001').status_code == 403
    assert mgr.get('/api/m/store?start=2026-01-01&end=2025-01-01').status_code == 400
    assert Client(app, 'caiwu').get('/api/m/store').status_code == 403                          # 财务无 app.store


# ---------------------------------------------------------------- APP-102 商品库存查询（扫码）
def test_goods_lookup_with_batches(app, dbc):
    from shouyin_app import inventory
    pid = dbc.execute("SELECT id FROM products WHERE merchant_id=1 AND code='P2'").fetchone()[0]
    soon = (TODAY + timedelta(days=20)).isoformat()
    old = (TODAY - timedelta(days=3)).isoformat()
    dbc.execute('BEGIN IMMEDIATE')
    b1 = inventory.get_or_create_batch(dbc, 1, pid, 'L001', soon)
    b2 = inventory.get_or_create_batch(dbc, 1, pid, 'L000', old)
    inventory.post_move(dbc, 1, 'A001', pid, 4, 'purchase', 'GRTEST1', 'test', batch_id=b1)
    inventory.post_move(dbc, 1, 'A001', pid, 2, 'purchase', 'GRTEST1', 'test', batch_id=b2)
    dbc.execute('COMMIT')
    mgr = Client(app, 'dianzhang')
    it = mgr.get('/api/m/goods?kw=6900000000021').get_json()['data']
    assert it['scan']['barcode'] == '6900000000021'
    g = it['items'][0]
    assert g['code'] == 'P2' and g['retail'] == 100.0 and [s['store_code'] for s in g['stock']] == ['A001']
    assert g['stock'][0]['available'] == 24                                                       # 20 无批次 + 4 可售批次（过期 2 不算）
    bt = dict((b['batch_no'], b) for b in g['batches'])
    assert bt['L001']['days_left'] == 20 and bt['L001']['unsellable'] == ''
    assert '过期' in bt['L000']['unsellable']
    assert mgr.get('/api/m/goods?kw=纸尿').get_json()['data']['items'][0]['code'] == 'P2'
    assert mgr.get('/api/m/goods').status_code == 400
    ov = mgr.get('/api/m/overview-stock-member').get_json()['data']
    assert ov['stock']['near_expiry']['count'] == 1 and ov['stock']['expired']['count'] == 1
    det = mgr.get('/api/m/overview-stock-member?detail=expired').get_json()['data']['items']
    assert det[0]['batch_no'] == 'L000' and det[0]['qty'] == 2
    assert mgr.get('/api/m/overview-stock-member?detail=bad').status_code == 400
    low = mgr.get('/api/m/overview-stock-member?detail=stock_low').get_json()['data']['items']
    assert all(x['store_code'] == 'A001' for x in low)


def test_member_overview(admin, dbc, sales):
    dbc.execute("UPDATE members SET reg_time='2025-01-01 10:00:00'")
    dbc.execute("UPDATE members SET reg_time=? WHERE code='M001'", (ts(TODAY),))
    add_order(dbc, 'A001', 5, ts(TODAY, '17:00:00'), member='M002')
    m = admin.get('/api/m/overview-stock-member').get_json()['data']['member']
    assert m['new'] == 1 and m['buyers'] == 2 and m['repeat'] == 1 and m['repeat_rate'] == 50.0


# ---------------------------------------------------------------- APP-103 / APP-303 审批待办闭环
def test_approval_todo_closed_loop(app):
    cas = Client(app, 'shouyin01')
    ap = cas.post('/api/approvals', {'kind': 'discount', 'store_code': 'A001', 'value': '0.8', 'title': '整单 8 折', 'reason': '老顾客',
                                     'request_key': 'mk1'}).get_json()['data']
    mgr = Client(app, 'dianzhang')
    h = mgr.get('/api/m/home').get_json()['data']
    assert h['todo_open'] == 1 and h['unread'] == 1 and h['features']['approval'] and not h['features']['profit']
    todo = mgr.get('/api/notify?todo=1&status=open').get_json()['data']['items']
    assert todo[0]['link'] == 'm#approval/' + ap['approval_no'] and todo[0]['category_name'] == '审批'
    r = mgr.post('/api/notify/%d/done' % todo[0]['id'])
    assert r.status_code == 409                                                                   # 审批待办不能手动关
    detail = mgr.get('/api/approvals/' + ap['approval_no']).get_json()['data']
    assert detail['can_decide'] and detail['apply_reason'] == '老顾客' and detail['logs'][0]['to_status'] == 'pending'
    assert mgr.post('/api/approvals/%s/approve' % ap['approval_no'], {'reason': '同意'}).get_json()['ok']
    h = mgr.get('/api/m/home').get_json()['data']
    assert h['todo_open'] == 0
    done = mgr.get('/api/notify?status=done').get_json()['data']['items']
    assert done[0]['ref_no'] == ap['approval_no']                                                 # 已处理事项保留记录
    mine = cas.get('/api/notify').get_json()['data']['items']
    assert '审批通过' in mine[0]['title']


def test_notify_read_all_and_done(app, dbc):
    from shouyin_app import notify
    uid = dbc.execute("SELECT id FROM users WHERE username='dianzhang'").fetchone()[0]
    dbc.execute('BEGIN IMMEDIATE')
    n = notify.notify_users(dbc, 1, [uid], 'stock_low', '缺货：会员 13800000001 预订的奶粉', '联系 13912345678', is_todo=True, ref_type='x', ref_no='1')
    assert notify.notify_users(dbc, 2, [uid], 'system', '别家发来的') == 0                         # 跨商户 user_id 被忽略
    dbc.execute('COMMIT')
    assert n == 1
    mgr = Client(app, 'dianzhang')
    it = mgr.get('/api/notify').get_json()['data']['items'][0]
    assert it['title'] == '缺货：会员 138****0001 预订的奶粉' and it['body'] == '联系 139****5678'     # APP-403 通知预览脱敏
    assert mgr.post('/api/notify/read', {'all': True}).get_json()['data']['updated'] == 1
    assert mgr.post('/api/notify/%d/done' % it['id']).get_json()['data']['status'] == 'done'
    assert mgr.get('/api/m/home').get_json()['data']['todo_open'] == 0
    assert mgr.post('/api/notify/read', {}).status_code == 400


# ---------------------------------------------------------------- APP-302 / APP-304 提醒规则与高风险告警
def test_notify_rules_filter_risk_alerts(app, admin, dbc):
    from shouyin_app import notify

    def risk(tag):
        dbc.execute('BEGIN IMMEDIATE')
        notify.risk_alert(dbc, 1, 'A001', '大额改价 ' + tag, '收银员把奶粉改到 1 元', ref_type='order', ref_no='S' + tag)
        dbc.execute('COMMIT')

    def got(user):
        return [x['ref_no'] for x in Client(app, user).get('/api/notify?category=risk').get_json()['data']['items']]

    risk('1')
    assert got('dianzhang') == ['S1'] and got('quyu') == ['S1'] and got('dianzhangB') == [] and got('shouyin01') == []
    it = Client(app, 'dianzhang').get('/api/notify?category=risk').get_json()['data']['items'][0]
    assert it['level'] == 'urgent' and it['link'] == 'm#order/S1'
    # 店长不收风险告警
    r = admin.post('/api/m/notify-rules', {'category': 'risk', 'role_code': 'store_manager', 'enabled': False}).get_json()
    assert r['ok'] and r['data']['enabled'] is False
    risk('2')
    assert got('dianzhang') == ['S1'] and got('quyu') == ['S2', 'S1']
    rules = admin.get('/api/m/notify-rules').get_json()['data']
    assert len(rules['items']) == 1 and 'risk' in rules['categories']
    assert Client(app, 'dianzhang').post('/api/m/notify-rules', {'category': 'risk'}).status_code == 403
    assert admin.post('/api/m/notify-rules', {'category': 'nope'}).status_code == 400
    assert admin.post('/api/m/notify-rules', {'category': 'risk', 'role_code': 'ghost'}).status_code == 400
    assert admin.post('/api/m/notify-rules', {'category': 'risk', 'store_code': 'Z001'}).status_code == 403
    assert admin.post('/api/m/notify-rules', {'delete': True, 'id': r['data']['id']}).get_json()['ok']
    assert admin.post('/api/m/notify-rules', {'delete': True, 'id': 99999}).status_code == 404
    # 白名单：只有区域经理收（级别 >= urgent）
    assert admin.post('/api/m/notify-rules', {'category': 'risk', 'role_code': 'region_manager', 'min_level': 'urgent'}).get_json()['ok']
    risk('3')
    assert got('dianzhang') == ['S1'] and got('quyu') == ['S3', 'S2', 'S1']
    for x in admin.get('/api/m/notify-rules').get_json()['data']['items']:
        admin.post('/api/m/notify-rules', {'delete': True, 'id': x['id']})
    # 频繁退款
    for _ in range(3):
        add_order(dbc, 'A001', -10, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), biz='refund', cashier='shouyin01')
    dbc.execute('BEGIN IMMEDIATE')
    assert notify.refund_burst(dbc, 1, 'A001', 'shouyin01') == 3
    notify.refund_burst(dbc, 1, 'A001', 'shouyin01')                                             # 同小时去重
    dbc.execute('COMMIT')
    burst = [x for x in Client(app, 'dianzhang').get('/api/notify?category=risk').get_json()['data']['items'] if '频繁退款' in x['title']]
    assert len(burst) == 1


# ---------------------------------------------------------------- APP-105 公告
def test_announcements_targeting_read_confirm(app, admin):
    r = admin.post('/api/m/announcements', {'title': '盘点通知 联系 13800000001', 'body': '周五闭店盘点', 'level': 'important',
                                            'need_confirm': True, 'target_stores': ['A001']}).get_json()
    assert r['ok'] and r['data']['notified'] >= 2
    aid = r['data']['id']
    mgr, mgrB = Client(app, 'dianzhang'), Client(app, 'dianzhangB')
    lst = mgr.get('/api/m/announcements').get_json()['data']
    assert lst['unread'] == 1 and lst['unconfirmed'] == 1 and lst['items'][0]['need_confirm']
    assert mgrB.get('/api/m/announcements').get_json()['data']['total'] == 0                      # 别店收不到
    assert mgrB.get('/api/m/announcements/%d' % aid).status_code == 404
    n = mgr.get('/api/notify?todo=1&status=open').get_json()['data']['items'][0]
    assert n['ref_type'] == 'notice' and '138****0001' in n['title'] and n['link'] == 'm#notice/%d' % aid
    assert mgr.post('/api/m/announcements/%d/read' % aid).get_json()['data']['read_at']
    assert mgr.get('/api/m/home').get_json()['data']['todo_open'] == 1                            # 需确认：读了还没完
    assert mgr.post('/api/m/announcements/%d/confirm' % aid).get_json()['data']['confirmed_at']
    assert mgr.get('/api/m/home').get_json()['data']['todo_open'] == 0
    st = admin.get('/api/m/announcements/%d' % aid).get_json()['data']['stats']
    assert st['read'] == 1 and st['confirmed'] == 1 and st['targets'] >= 2 and '演示收银员' in st['unread_names']
    # 店长没有发布权限；不需确认的公告读完即闭环；撤回后收不到
    assert mgr.post('/api/m/announcements', {'title': 'x'}).status_code == 403
    aid2 = admin.post('/api/m/announcements', {'title': '新品到店', 'target_roles': ['cashier']}).get_json()['data']['id']
    cas = Client(app, 'shouyin01')
    assert mgr.get('/api/m/announcements/%d' % aid2).status_code == 404                          # 只发收银员
    assert cas.post('/api/m/announcements/%d/confirm' % aid2).status_code == 400
    cas.post('/api/m/announcements/%d/read' % aid2)
    assert [x for x in cas.get('/api/notify?status=done').get_json()['data']['items'] if x['ref_no'] == str(aid2)]
    assert admin.post('/api/m/announcements/%d/withdraw' % aid2).get_json()['ok']
    assert cas.get('/api/m/announcements/%d' % aid2).status_code == 404
    assert admin.post('/api/m/announcements/%d/withdraw' % aid2).status_code == 409
    for bad in ({'title': ''}, {'title': 'x', 'level': 'urgent'}, {'title': 'x', 'target_roles': ['ghost']}, {'title': 'x', 'target_stores': ['Z001']}):
        assert admin.post('/api/m/announcements', bad).status_code in (400, 403), bad
    assert admin.get('/api/m/announcements?box=mine').get_json()['data']['total'] == 2


# ---------------------------------------------------------------- APP-404 版本 / APP-402 设备会话
def test_version_check_and_force(app, admin):
    c = app.test_client()
    d = c.get('/api/m/version?v=3.0.0').get_json()['data']
    assert d['version'] == '3.0.0' and d['outdated'] is False and d['must_upgrade'] is False
    p = Client(app, 'platform', 'platpass1')
    assert admin.post('/api/m/version', {'version': '3.1.0'}).status_code == 403                 # 商户账号不能发版本
    assert p.post('/api/m/version', {'version': 'abc'}).status_code == 400
    assert p.post('/api/m/version', {'version': '3.1.0', 'min_version': '3.1.0', 'force': True, 'notes': '安全修复'}).get_json()['ok']
    d = c.get('/api/m/version?v=3.0.0').get_json()['data']
    assert d['version'] == '3.1.0' and d['outdated'] and d['must_upgrade'] and d['notes'] == '安全修复'
    assert c.get('/api/m/version?v=3.1.0').get_json()['data']['must_upgrade'] is False


def test_remote_session_logout(app):
    phone, pc = Client(app, 'dianzhang'), Client(app, 'dianzhang')
    items = phone.get('/api/m/sessions').get_json()['data']['items']
    assert len(items) == 2 and sum(1 for x in items if x['current']) == 1 and 'sid' not in items[0]
    lost = [x for x in items if not x['current']][0]
    assert phone.post('/api/m/sessions/revoke', {'key': lost['key']}).get_json()['data']['revoked'] == 1
    assert pc.get('/api/m/home').status_code == 401 and phone.get('/api/m/home').status_code == 200
    other = Client(app, 'dianzhang')
    assert phone.post('/api/m/sessions/revoke', {'others': True}).get_json()['data']['revoked'] == 1
    assert other.get('/api/m/home').status_code == 401
    assert phone.post('/api/m/sessions/revoke', {'key': 'nope'}).status_code == 404
    assert Client(app, 'dianzhangB').post('/api/m/sessions/revoke', {'key': lost['key']}).status_code == 404   # 不能退别人的


def test_member_lookup_masks_phone(app):
    # v1 /api/m/member 已删除（集成轮），手机端查会员走②的 api/member；店长无 member.phone.view 只看到脱敏号
    d = Client(app, 'dianzhang').get('/api/member?kw=13800000001').get_json()['data']['items'][0]
    assert d['phone'] == '138****0001' and d['name'] == '张妈妈'


# ---------------------------------------------------------------- 前端：相对路径、PWA 文件
def _front_files():
    names = ['m.html', 'sw.js', 'manifest.webmanifest']
    names += [n for n in os.listdir(STATIC) if n.startswith('m-') and n.endswith('.js')]
    return names


def test_mobile_pages_use_only_relative_urls(app):
    for name in _front_files():
        txt = open(os.path.join(STATIC, name), encoding='utf-8').read()
        bad = re.findall(r"""(?:fetch\(|href=|src=|action=|register\(|importScripts\()\s*['"`]\/(?!\/)[^'"`]*""", txt)
        assert not bad, (name, bad)
        assert not re.findall(r"""['"`]\/(?:api|static)\/""", txt), name
        assert not re.findall(r"""https?://""", txt.replace('http://www.w3.org/2000/svg', '')), name          # 不引外部 CDN
    m = json.load(open(os.path.join(STATIC, 'manifest.webmanifest'), encoding='utf-8'))
    assert m['start_url'] == '../m' and m['display'] == 'standalone' and m['icons']
    sw = open(os.path.join(STATIC, 'sw.js'), encoding='utf-8').read()
    assert "'api/'" in sw or '"api/"' in sw or '/api/' in sw.replace("'/api/'", '')                                # sw 明确跳过接口
    c = app.test_client()
    r = c.get('/static/sw.js')
    assert r.status_code == 200 and r.headers.get('Service-Worker-Allowed') == '../'
    assert c.get('/static/manifest.webmanifest').status_code == 200
    html = c.get('/m').get_data(as_text=True)
    assert 'manifest.webmanifest' in html and 'm-app.js' in html
    from shouyin_app.views_m import M_VERSION
    assert ("APP_VERSION = '%s'" % M_VERSION) in open(os.path.join(STATIC, 'm-app.js'), encoding='utf-8').read()
