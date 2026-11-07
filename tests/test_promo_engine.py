# -*- coding: utf-8 -*-
"""模块② 促销引擎 price_cart / apply_benefits / reverse_benefits（API-CONTRACT §3.3）。"""
import json

import pytest

from shouyin_app.core import _load_actor, tx
from shouyin_app.util import BizError
from shouyin_app import promo_engine as pe
from shouyin_app import member_svc as ms

NOW = '2026-11-05 12:00:00'


def actor_of(db, username):
    return _load_actor(db, db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone())


def add_act(db, act_no, typ, rule, scope=None, priority=100, status='approved', start='2026-01-01 00:00:00', end='2026-12-31 23:59:59',
            group='', stack_member=0, stack_coupon=1, points_rate='', mid=1):
    db.execute('INSERT INTO promo_activities(act_no, merchant_id, name, type, priority, exclusive_group, stack_member_price, stack_coupon, '
               'scope, rule, start_at, end_at, status, created_at, points_rate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (act_no, mid, act_no + '活动', typ, priority, group, stack_member, stack_coupon, json.dumps(scope or {}), json.dumps(rule),
                start, end, status, NOW, points_rate))


def add_coupon(db, cno, member='M001', face=2000, thr=0, typ='cash', rate=None, scope=None, expire='', ret=1, mid=1):
    db.execute('INSERT INTO coupon_templates(merchant_id, name, face_value, face_value_cents, threshold_cents, valid_days, status, created_at, type, '
               'discount_rate, scope, return_on_refund) VALUES(?,?,?,?,?,30,?,?,?,?,?,?)',
               (mid, cno + '模板', face / 100.0, face, thr, '启用', NOW, typ, rate, json.dumps(scope or {}), ret))
    tid = db.execute('SELECT max(id) FROM coupon_templates').fetchone()[0]
    db.execute("INSERT INTO coupons(merchant_id, coupon_no, template_id, template_name, member_code, face_value_cents, threshold_cents, "
               "expire_date, status, created_at) VALUES(?,?,?,?,?,?,?,?,'未使用',?)", (mid, cno, tid, cno + '模板', member, face, thr, expire, NOW))


def check_invariants(pr):
    assert sum(l['amount_cents'] for l in pr['lines']) == pr['payable_cents']
    assert pr['retail_total_cents'] - pr['payable_cents'] == sum(d['amount_cents'] for d in pr['discounts'])
    for l in pr['lines']:
        assert 0 <= l['share_cents'] <= l['line_amount_cents'] or l['line_amount_cents'] == 0
        assert l['amount_cents'] == l['line_amount_cents'] - l['share_cents']
        assert l['amount_cents'] >= 0


def price(db, user, cart, store='A001'):
    return pe.price_cart(db, actor_of(db, user), store, cart, now=NOW)


def test_contract_shape_and_member_price(dbc):
    pr = price(dbc, 'shouyin01', {'items': [{'code': '6900000000011', 'qty': '2'}], 'member_code': 'M001'})
    for k in ('lines', 'member', 'subtotal_cents', 'retail_total_cents', 'order_discount_cents', 'coupons', 'points_used', 'points_cents',
              'round_off_cents', 'payable_cents', 'points_earned', 'discounts', 'need_auth', 'explain', 'gifts', 'snapshot'):
        assert k in pr
    l = pr['lines'][0]
    for k in ('line_no', 'product_id', 'code', 'name', 'spec', 'unit', 'unit_rate', 'qty', 'base_qty', 'batch_id', 'is_gift', 'retail_cents',
              'base_price_cents', 'price_cents', 'line_amount_cents', 'share_cents', 'amount_cents', 'cost_cents', 'price_changed', 'promos'):
        assert k in l
    assert l['retail_cents'] == 40000 and l['price_cents'] == 38000 and l['line_amount_cents'] == 76000
    assert pr['payable_cents'] == 76000 and pr['points_earned'] == 760
    assert pr['discounts'] == [{'kind': 'member_price', 'line_no': 1, 'source_no': '普通会员', 'source_name': '会员价', 'amount_cents': 4000}]
    assert pr['member']['phone'] == '138****0001'           # 收银员无 member.phone.view
    assert pr['member']['stored_balance'] == 0.0             # 收银员可见储值余额（总脑定）
    check_invariants(pr)
    # 非会员：原价，无积分
    pr2 = price(dbc, 'shouyin01', {'items': [{'code': 'P1', 'qty': 1}]})
    assert pr2['payable_cents'] == 40000 and pr2['points_earned'] == 0 and pr2['member'] is None


def test_same_input_same_output(dbc):
    add_act(dbc, 'AC1', 'full_reduce', {'tiers': [{'threshold': 300, 'reduce': 20}]})
    add_coupon(dbc, 'CPX1', face=1000, thr=10000)
    dbc.commit()
    cart = {'items': [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 3}], 'member_code': 'M001', 'coupon_nos': ['CPX1'], 'points_used': 333,
            'round_mode': 'jiao', 'order_discount': {'rate': '0.95'}}
    dbc.execute("UPDATE members SET points=1000 WHERE code='M001'")
    a = price(dbc, 'root', cart)
    for _ in range(3):
        b = price(dbc, 'root', cart)
        assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    check_invariants(a)


def test_layers_full_reduce_coupon_points_roundoff(dbc):
    add_act(dbc, 'AC1', 'full_reduce', {'tiers': [{'threshold': 300, 'reduce': 20}, {'threshold': 600, 'reduce': 50}]})
    add_coupon(dbc, 'CP1', face=1000, thr=10000)
    dbc.execute("UPDATE members SET points=5000 WHERE code='M001'")
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 3}], 'member_code': 'M001', 'coupon_nos': ['CP1'],
                             'points_used': 777, 'round_mode': 'jiao'})
    # 380 + 300 = 680 ≥ 600 → 减 50；券 10；积分 7.77；抹零到角
    assert pr['subtotal_cents'] == 68000
    assert pr['order_discount_cents'] == 5000
    assert pr['coupons'] == [{'coupon_no': 'CP1', 'amount_cents': 1000}]
    assert pr['points_used'] == 777 and pr['points_cents'] == 777
    after = 68000 - 5000 - 1000 - 777
    assert pr['payable_cents'] == after - after % 10 and pr['round_off_cents'] == after % 10
    kinds = set(d['kind'] for d in pr['discounts'])
    assert {'member_price', 'activity', 'coupon', 'points', 'round_off'} <= kinds
    check_invariants(pr)
    snap = pr['snapshot']
    assert snap['engine'] == 'v2' and snap['activities'][0]['act_no'] == 'AC1' and snap['coupons'][0]['coupon_no'] == 'CP1'
    assert sum(x['points_used'] for x in snap['lines']) == 777
    assert sum(x['points_earned'] for x in snap['lines']) == pr['points_earned']


def test_activity_status_and_time_window(dbc):
    add_act(dbc, 'ACP', 'special_price', {'items': [{'code': 'P1', 'price': 300}]}, status='paused')
    add_act(dbc, 'ACF', 'special_price', {'items': [{'code': 'P1', 'price': 300}]}, start='2026-12-01 00:00:00')
    add_act(dbc, 'ACE', 'special_price', {'items': [{'code': 'P1', 'price': 300}]}, end='2026-11-01 00:00:00')
    add_act(dbc, 'ACD', 'special_price', {'items': [{'code': 'P1', 'price': 300}]}, status='draft')
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}]})
    assert pr['payable_cents'] == 40000 and not pr['snapshot']['activities']
    add_act(dbc, 'ACOK', 'special_price', {'items': [{'code': 'P1', 'price': 300}]})
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}]})
    assert pr['payable_cents'] == 30000 and pr['lines'][0]['activity_no'] == 'ACOK'


def test_scope_store_level_and_exclusive_group(dbc):
    add_act(dbc, 'ACB', 'item_discount', {'rate': '0.5'}, scope={'stores': ['B001']})
    add_act(dbc, 'ACL', 'item_discount', {'rate': '0.8'}, scope={'levels': ['银卡会员']})
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}], 'member_code': 'M001'})
    assert pr['payable_cents'] == 10000
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}], 'member_code': 'M002'})
    assert pr['payable_cents'] == 8000
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}]}, store='B001')
    assert pr['payable_cents'] == 5000
    # 互斥组：同组只用优先级高的
    add_act(dbc, 'ACG1', 'full_reduce', {'tiers': [{'threshold': 100, 'reduce': 10}]}, group='G', priority=1)
    add_act(dbc, 'ACG2', 'order_discount', {'rate': '0.5'}, group='G', priority=2)
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 2}]})
    assert pr['order_discount_cents'] == 1000
    check_invariants(pr)


def test_member_price_vs_activity_stack(dbc):
    add_act(dbc, 'ACS', 'special_price', {'items': [{'code': 'P1', 'price': 390}]}, stack_member=0)
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001'})
    assert pr['payable_cents'] == 38000 and any('会员价更优' in e for e in pr['explain'])
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}]})
    assert pr['payable_cents'] == 39000
    add_act(dbc, 'ACK', 'item_discount', {'rate': '0.9'}, stack_member=1, priority=1, scope={'product_codes': ['P1']})
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001'})
    assert pr['payable_cents'] == 34200     # 380 × 0.9
    check_invariants(pr)


def test_nth_bundle_redeem_full_gift(dbc):
    add_act(dbc, 'ACN', 'nth_discount', {'n': 2, 'rate': '0.5'}, scope={'product_codes': ['P2']})
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 3}]})
    assert pr['payable_cents'] == 25000
    check_invariants(pr)
    dbc.execute("UPDATE promo_activities SET status='ended' WHERE act_no='ACN'")
    add_act(dbc, 'ACBD', 'bundle_price', {'codes': ['P1', 'P2'], 'price': 450})
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 2}]})
    assert pr['payable_cents'] == 45000 + 10000
    check_invariants(pr)
    dbc.execute("UPDATE promo_activities SET status='ended' WHERE act_no='ACBD'")
    dbc.execute("UPDATE products SET retail=20, retail_cents=2000 WHERE code='P3'")
    add_act(dbc, 'ACR', 'redeem', {'code': 'P3', 'threshold': 300, 'price': 1, 'max_qty': 1})
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}, {'code': 'P3', 'qty': 2}]})
    assert pr['payable_cents'] == 40000 + 100 + 2000
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}, {'code': 'P3', 'qty': 1}]})
    assert pr['payable_cents'] == 12000 and any('换购' in e for e in pr['explain'])
    add_act(dbc, 'ACFG', 'full_gift', {'threshold': 500, 'gift_code': 'P2', 'gift_qty': 1})
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 2}]})
    g = [l for l in pr['lines'] if l['is_gift']]
    assert len(g) == 1 and g[0]['code'] == 'P2' and g[0]['amount_cents'] == 0 and g[0]['auto_gift'] and g[0]['cost_cents'] == 6000
    assert pr['gifts'][0]['activity_no'] == 'ACFG' and pr['payable_cents'] == 80000
    check_invariants(pr)


def test_price_system_store_region_level_and_units(dbc):
    pid = dbc.execute("SELECT id FROM products WHERE code='P2'").fetchone()[0]
    dbc.execute("INSERT INTO product_prices(merchant_id, product_id, unit, scope_type, scope_ref, price_cents) VALUES(1,?,'','region','R1',9000)", (pid,))
    assert price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}]})['payable_cents'] == 9000
    dbc.execute("INSERT INTO product_prices(merchant_id, product_id, unit, scope_type, scope_ref, price_cents) VALUES(1,?,'','store','A001',8800)", (pid,))
    assert price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}]})['payable_cents'] == 8800
    assert price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}]}, store='B001')['payable_cents'] == 10000
    dbc.execute("INSERT INTO product_prices(merchant_id, product_id, unit, scope_type, scope_ref, price_cents) VALUES(1,?,'','level','银卡会员',8000)", (pid,))
    assert price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}], 'member_code': 'M002'})['payable_cents'] == 8000
    # 多单位：1 箱 = 6 包，箱码
    dbc.execute("INSERT INTO product_units(merchant_id, product_id, unit, rate) VALUES(1,?,'箱',6)", (pid,))
    uid = dbc.execute('SELECT max(id) FROM product_units').fetchone()[0]
    dbc.execute("INSERT INTO product_barcodes(merchant_id, barcode, product_id, unit_id) VALUES(1,'BOX2',?,?)", (pid, uid))
    pr = price(dbc, 'root', {'items': [{'code': 'BOX2', 'qty': 1}]}, store='B001')
    l = pr['lines'][0]
    assert l['unit'] == '箱' and l['unit_rate'] == 6 and l['base_qty'] == 6 and pr['payable_cents'] == 60000
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 2, 'unit': '箱'}]}, store='B001')
    assert pr['lines'][0]['base_qty'] == 12


def test_manual_price_discount_need_auth_and_errors(dbc):
    pr = price(dbc, 'shouyin01', {'items': [{'code': 'P1', 'qty': 1, 'price': 350}, {'code': 'P2', 'qty': 1, 'rate': '0.8'}],
                                  'order_discount': {'amount': 10}})
    kinds = [n['kind'] for n in pr['need_auth']]
    assert kinds.count('price_change') == 1 and kinds.count('discount') == 2
    assert pr['payable_cents'] == 35000 + 8000 - 1000
    check_invariants(pr)
    with pytest.raises(BizError):
        price(dbc, 'shouyin01', {'items': [{'code': 'P3', 'qty': 1}]})       # 未定价
    with pytest.raises(BizError):
        price(dbc, 'shouyin01', {'items': [{'code': 'Q1', 'qty': 1}]})       # 别家商品
    with pytest.raises(BizError):
        price(dbc, 'shouyin01', {'items': []})
    with pytest.raises(BizError):
        price(dbc, 'shouyin01', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'Z0001'})   # 别家会员


def test_coupon_rules(dbc):
    add_coupon(dbc, 'CPT', face=5000, thr=50000)
    with pytest.raises(BizError) as e:
        price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPT']})
    assert '门槛' in e.value.msg
    add_coupon(dbc, 'CPD', typ='discount', rate=0.9, face=0, thr=0)
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPD']})
    assert pr['coupons'][0]['amount_cents'] == 1000
    add_coupon(dbc, 'CPP', typ='product', face=3000, scope={'product_codes': ['P2']})
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPP']})
    cd = [d for d in pr['discounts'] if d['kind'] == 'coupon']
    assert len(cd) == 1 and cd[0]['line_no'] == 2 and cd[0]['amount_cents'] == 3000
    add_coupon(dbc, 'CPO', member='M002', face=100)
    with pytest.raises(BizError):
        price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPO']})
    add_coupon(dbc, 'CPX', face=100, expire='2026-01-01')
    with pytest.raises(BizError):
        price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPX']})
    with pytest.raises(BizError):
        price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPD', 'CPP']})   # 每单 1 张
    lst = pe.coupons_for_cart(dbc, actor_of(dbc, 'root'), 'A001', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001'}, now=NOW)
    byno = dict((x['coupon_no'], x) for x in lst)
    assert byno['CPD']['usable'] and not byno['CPT']['usable'] and '门槛' in byno['CPT']['reason']


def test_disabled_member_no_benefits(dbc):
    add_coupon(dbc, 'CPB', face=100)
    dbc.execute("UPDATE members SET blacklisted=1 WHERE code='M001'")
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001'})
    assert pr['payable_cents'] == 40000 and pr['points_earned'] == 0 and pr['explain']
    with pytest.raises(BizError):
        price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPB']})


def _fake_order(db, order_no, pr, member='M001'):
    db.execute("INSERT INTO orders(merchant_id, order_no, biz_type, store_code, member_code, status, snapshot, created_at) "
               "VALUES(1,?,'sale','A001',?,'已完成',?,?)", (order_no, member, json.dumps(pr['snapshot']), NOW))
    ids = {}
    for l in pr['lines']:
        db.execute('INSERT INTO order_items(merchant_id, order_no, line_no, product_id, product_code, qty, amount_cents) VALUES(1,?,?,?,?,?,?)',
                   (order_no, l['line_no'], l['product_id'], l['code'], l['qty'], l['amount_cents']))
        ids[l['line_no']] = db.execute('SELECT max(id) FROM order_items').fetchone()[0]
    return dict(db.execute('SELECT * FROM orders WHERE order_no=?', (order_no,)).fetchone()), ids


def test_apply_benefits_coupon_once_and_points(dbc):
    add_coupon(dbc, 'CPA', face=1000)
    dbc.execute("UPDATE members SET points=500 WHERE code='M001'")
    dbc.commit()
    act = actor_of(dbc, 'shouyin01')
    pr = price(dbc, 'shouyin01', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPA'], 'points_used': 200})
    with tx(dbc):
        r = pe.apply_benefits(dbc, act, 'S1', 'A001', pr)
    assert r['coupons_used'] == ['CPA'] and r['points_used'] == 200
    pts = dbc.execute("SELECT points FROM members WHERE code='M001'").fetchone()[0]
    assert pts == 500 - 200 + pr['points_earned']
    assert dbc.execute("SELECT status, order_no FROM coupons WHERE coupon_no='CPA'").fetchone()[:] == ('已核销', 'S1')
    assert dbc.execute("SELECT to_status FROM coupon_logs WHERE coupon_no='CPA'").fetchone()[0] == '已核销'
    with pytest.raises(BizError) as e:
        with tx(dbc):
            pe.apply_benefits(dbc, act, 'S2', 'A001', pr)        # 同一券第二次核销
    assert e.value.status == 409
    assert dbc.execute("SELECT points FROM members WHERE code='M001'").fetchone()[0] == pts   # 回滚，积分不变


def test_lock_release_coupons(dbc):
    add_coupon(dbc, 'CPL', face=1000)
    act = actor_of(dbc, 'shouyin01')
    with tx(dbc):
        pe.lock_coupons(dbc, act, 'H1', ['CPL'])
        pe.lock_coupons(dbc, act, 'H1', ['CPL'])         # 幂等
    with pytest.raises(BizError):
        with tx(dbc):
            pe.lock_coupons(dbc, act, 'H2', ['CPL'])
    with tx(dbc):
        assert pe.release_coupons(dbc, act, 'H1') == ['CPL']
    assert dbc.execute("SELECT status FROM coupons WHERE coupon_no='CPL'").fetchone()[0] == '未使用'
    assert [r[0] for r in dbc.execute("SELECT to_status FROM coupon_logs WHERE coupon_no='CPL' ORDER BY id")] == ['已锁定', '未使用']


@pytest.mark.parametrize('policy', ['negative', 'limit', 'manual'])
def test_reverse_benefits_points_shortfall_policies(dbc, policy):
    """AC-017：部分退货积分扣回不足按配置处理，留流水，不静默丢失。"""
    act = actor_of(dbc, 'root')
    with tx(dbc):
        ms.save_rules(dbc, act, 1, {'points_refund_policy': policy})
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 2}, {'code': 'P2', 'qty': 1}], 'member_code': 'M001'})
    with tx(dbc):
        pe.apply_benefits(dbc, act, 'S9', 'A001', pr)
        order, ids = _fake_order(dbc, 'S9', pr)
    earned = pr['points_earned']               # 860
    with tx(dbc):
        dbc.execute("UPDATE members SET points=100 WHERE code='M001'")     # 会员把积分用掉了
    with tx(dbc):
        r = pe.reverse_benefits(dbc, act, 'R9', order, [{'orig_line_id': ids[1], 'qty': 1, 'amount_cents': 38000}])
    line1 = [x for x in pr['snapshot']['lines'] if x['line_no'] == 1][0]['points_earned']
    assert r['points_back'] == line1 // 2 and r['policy'] == policy
    short = r['points_back'] - 100
    assert r['points_shortfall'] == short
    m = dbc.execute("SELECT points, points_debt FROM members WHERE code='M001'").fetchone()
    exc = dbc.execute("SELECT shortfall, policy, status FROM points_exceptions WHERE ref_no='R9'").fetchone()
    assert exc['shortfall'] == short and exc['policy'] == policy
    logs = dbc.execute("SELECT change, note FROM points_log WHERE order_no='R9'").fetchall()
    assert logs, '必须有流水'
    if policy == 'negative':
        assert m['points'] == 100 - r['points_back'] and m['points_debt'] == 0
    else:
        assert m['points'] == 0 and m['points_debt'] == short and exc['status'] == 'open'
        assert '差额' in logs[0]['note']
    if policy == 'limit':
        with pytest.raises(BizError):
            dbc.execute("UPDATE members SET points=1000 WHERE code='M001'")
            price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}], 'member_code': 'M001', 'points_used': 100})
        dbc.commit()
    # 再退剩下的 → 累计扣回 = 该行全部发放积分；重复同一退货单 409
    with tx(dbc):
        r2 = pe.reverse_benefits(dbc, act, 'R10', order, [{'orig_line_id': ids[1], 'qty': 1, 'amount_cents': 38000}])
    assert r['points_back'] + r2['points_back'] == line1
    with pytest.raises(BizError):
        with tx(dbc):
            pe.reverse_benefits(dbc, act, 'R10', order, [{'orig_line_id': ids[2], 'qty': 1, 'amount_cents': 1}])
    with pytest.raises(BizError):
        with tx(dbc):
            pe.reverse_benefits(dbc, act, 'R11', order, [{'orig_line_id': ids[1], 'qty': 1, 'amount_cents': 1}])   # 超退
    assert earned >= line1


def test_reverse_benefits_points_restore_and_coupon_return(dbc):
    add_coupon(dbc, 'CPR', face=1000)
    dbc.execute("UPDATE members SET points=1000 WHERE code='M001'")
    dbc.commit()
    act = actor_of(dbc, 'root')
    pr = price(dbc, 'root', {'items': [{'code': 'P1', 'qty': 1}, {'code': 'P2', 'qty': 2}], 'member_code': 'M001', 'coupon_nos': ['CPR'],
                             'points_used': 300})
    with tx(dbc):
        pe.apply_benefits(dbc, act, 'S5', 'A001', pr)
        order, ids = _fake_order(dbc, 'S5', pr)
    with tx(dbc):
        r1 = pe.reverse_benefits(dbc, act, 'R5', order, [{'orig_line_id': ids[2], 'qty': 1, 'amount_cents': 1}])
    assert r1['coupons_returned'] == []                       # 部分退不退券
    with tx(dbc):
        r2 = pe.reverse_benefits(dbc, act, 'R6', order, [{'orig_line_id': ids[1], 'qty': 1, 'amount_cents': 1},
                                                         {'orig_line_id': ids[2], 'qty': 1, 'amount_cents': 1}])
    assert r2['coupons_returned'] == ['CPR']
    assert r1['points_restore'] + r2['points_restore'] == 300
    assert r1['points_back'] + r2['points_back'] == pr['points_earned']
    assert dbc.execute("SELECT points FROM members WHERE code='M001'").fetchone()[0] == 1000
    assert dbc.execute("SELECT status FROM coupons WHERE coupon_no='CPR'").fetchone()[0] == '未使用'


def test_stored_split_and_quick_register(dbc):
    act = actor_of(dbc, 'shouyin01')
    with tx(dbc):
        ms.stored_change(dbc, act, 1, 'M001', 11000, '充值', 'CZ1', 'A001', principal_cents=10000, gift_cents=1000)
        ms.stored_change(dbc, act, 1, 'M001', -5500, '消费', 'S1', 'A001')
    m = dbc.execute("SELECT balance_cents, stored_principal_cents, stored_gift_cents FROM members WHERE code='M001'").fetchone()
    assert tuple(m) == (5500, 5000, 500)
    with pytest.raises(BizError):
        with tx(dbc):
            ms.stored_change(dbc, act, 1, 'M001', -6000, '消费', 'S2', 'A001')
    with tx(dbc):
        code = ms.quick_register(dbc, act, 1, 'A001', {'phone': '13700000077', 'due_date': '2027-01-01'})
    card = ms.member_brief(dbc, act, 1, '13700000077')
    assert card['code'] == code and card['phone'] == '137****0077' and card['babies'][0]['kind'] == 'pregnancy'
    with pytest.raises(BizError):
        with tx(dbc):
            ms.quick_register(dbc, act, 1, 'A001', {'phone': '13700000077'})
    assert ms.member_brief(dbc, act, 1, '13900000009') is None           # 别家会员查不到


def test_points_expire(dbc):
    act = actor_of(dbc, 'root')
    with tx(dbc):
        ms.points_change(dbc, act, 1, 'M001', 100, '消费获得', 'S1', expire_date='2026-01-01')
        ms.points_change(dbc, act, 1, 'M001', 50, '消费获得', 'S2', expire_date='2027-01-01')
        ms.points_change(dbc, act, 1, 'M001', -30, '积分抵扣', 'S3')
        out = ms.expire_points(dbc, act, 1, today='2026-06-01')
        again = ms.expire_points(dbc, act, 1, today='2026-06-01')
    assert out == {'M001': 70} and again == {}
    assert dbc.execute("SELECT points FROM members WHERE code='M001'").fetchone()[0] == 50


def test_coupon_concurrent_redeem_only_once(app, dbc):
    """券只能核销一次：两个连接并发结算同一张券，恰好一个成功，另一个 409（条件更新 + BEGIN IMMEDIATE）。"""
    import threading
    from shouyin_app.db import connect
    add_coupon(dbc, 'CPC', face=1000)
    dbc.commit()
    pr = price(dbc, 'shouyin01', {'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001', 'coupon_nos': ['CPC']})
    results, barrier = [], threading.Barrier(2)

    def run(no):
        db = connect(app.config['DB_PATH'])
        try:
            act = actor_of(db, 'shouyin01')
            barrier.wait()
            with tx(db):
                pe.apply_benefits(db, act, no, 'A001', pr)
            results.append('ok')
        except BizError as e:
            results.append(e.status)
        finally:
            db.close()
    ts = [threading.Thread(target=run, args=(n,)) for n in ('SA', 'SB')]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(results, key=str) == sorted(['ok', 409], key=str)
    assert dbc.execute("SELECT COUNT(*) FROM coupon_logs WHERE coupon_no='CPC' AND to_status='已核销'").fetchone()[0] == 1


def test_zero_payable_orders(dbc):
    """9.3：全赠 / 全积分抵扣订单应付 0，合计仍可还原。"""
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1, 'is_gift': True}]})
    assert pr['payable_cents'] == 0 and pr['need_auth'][0]['kind'] == 'price_change' and pr['lines'][0]['price_changed']
    assert [d['kind'] for d in pr['discounts']] == ['gift']
    check_invariants(pr)
    dbc.execute("INSERT OR REPLACE INTO settings(merchant_id, key, value) VALUES(1, 'config', '{\"points_max_ratio\": 1}')")
    dbc.execute("UPDATE members SET points=100000 WHERE code='M001'")
    pr = price(dbc, 'root', {'items': [{'code': 'P2', 'qty': 1}], 'member_code': 'M001', 'points_used': 10000})
    assert pr['payable_cents'] == 0 and pr['points_used'] == 10000 and pr['points_earned'] == 0
    check_invariants(pr)
    assert pr['member']['baby'] is None or 'birthday' in pr['member']['baby']
