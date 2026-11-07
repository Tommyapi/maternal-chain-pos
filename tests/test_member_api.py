# -*- coding: utf-8 -*-
"""模块② 会员 HTTP 接口：档案/宝宝/偏好/合并/标签/黑名单/积分调整/储值审批/日报/导入导出/越权与脱敏。"""
import io

from openpyxl import Workbook, load_workbook

from conftest import Client
from shouyin_app.db import connect, init_db


def grant(app, username, perms):
    db = connect(app.config['DB_PATH'])
    rid = db.execute('SELECT role_id FROM users WHERE username=?', (username,)).fetchone()[0]
    for p in perms:
        db.execute('INSERT OR IGNORE INTO role_perms(role_id, perm) VALUES(?,?)', (rid, p))
    db.commit()
    db.close()


def test_list_detail_masking_and_scope(app, admin):
    r = admin.get('/api/member').get_json()['data']
    assert r['total'] == 2 and r['items'][0]['phone'] in ('13800000002', '13800000001')    # 超管有 member.phone.view
    mgr = Client(app, 'dianzhang')                         # A001 店长：只看注册门店在范围内的会员，手机号脱敏
    r = mgr.get('/api/member').get_json()['data']
    assert [x['code'] for x in r['items']] == ['M001'] and r['items'][0]['phone'] == '138****0001'
    assert mgr.get('/api/member/M002').status_code == 404
    d = mgr.get('/api/member/M001').get_json()['data']
    assert d['phone'] == '138****0001' and d['babies'] and 'preferences' in d and d['stored_balance'] == 0.0
    zd = Client(app, 'zhidu')                              # 只读：无储值字段权限
    assert zd.get('/api/member/M001').get_json()['data']['stored_balance'] is None
    assert admin.get('/api/member?kw=0002').get_json()['data']['items'][0]['code'] == 'M002'
    assert admin.get('/api/member?store=Z001').status_code == 403


def test_cross_merchant_denied(app, m2admin):
    assert m2admin.get('/api/member/M001').status_code == 404
    codes = [x['code'] for x in m2admin.get('/api/member').get_json()['data']['items']]
    assert codes == ['Z0001']
    assert m2admin.post('/api/member', {'code': 'M001', 'name': 'hack'}).status_code == 404
    assert m2admin.post('/api/member/M001/points', {'delta': 10, 'reason': 'x'}).status_code == 404
    assert m2admin.post('/api/member/merge/preview', {'from_code': 'M001', 'to_code': 'Z0001'}).status_code == 404
    m2c = Client(app, 'm2cashier')
    assert m2c.get('/api/member/M001').status_code == 404
    plat = Client(app, 'platform', 'platpass1')
    assert plat.get('/api/member').status_code == 403


def test_create_update_babies(admin):
    r = admin.post('/api/member', {'phone': '13711112222', 'name': '赵妈妈', 'reg_store_code': 'A001',
                                   'babies': [{'kind': 'baby', 'nickname': '大宝', 'birthday': '2024-01-01'}], 'due_date': '2027-03-01'})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()['data']
    code = d['code']
    assert code.startswith('XB') and len(d['babies']) == 2
    bid = [b for b in d['babies'] if b['kind'] == 'baby'][0]['id']
    d = admin.post('/api/member', {'code': code, 'level': '银卡会员', 'babies': [{'id': bid, 'delete': True}, {'nickname': '二宝', 'birthday': '2026-01-01'}]}).get_json()['data']
    assert d['level'] == '银卡会员'
    assert [b['hidden'] for b in d['babies'] if b['id'] == bid] == [1] and len(d['babies']) == 3
    assert admin.post('/api/member', {'phone': '13711112222'}).status_code == 400
    assert admin.post('/api/member', {'code': code, 'phone': '13800000001'}).status_code == 409
    assert admin.post('/api/member', {'code': code, 'level': '钻石会员'}).status_code == 400


def test_disable_blacklist_blocks_benefits(app, admin):
    assert admin.post('/api/member/M001/disable', {}).status_code == 400
    d = admin.post('/api/member/M001/disable', {'reason': '恶意刷券', 'blacklist': True}).get_json()['data']
    assert d['blacklisted'] == 1 and not d['benefits_ok']
    r = admin.post('/api/promo/try', {'store_code': 'A001', 'items': [{'code': 'P1', 'qty': 1}], 'member_code': 'M001'}).get_json()['data']
    assert r['payable'] == 400.0 and r['points_earned'] == 0
    tid = admin.post('/api/promo/coupon-templates', {'name': '券', 'type': 'free', 'face_value': 5}).get_json()['data']['id']
    r = admin.post('/api/promo/coupons/issue', {'template_id': tid, 'member_codes': ['M001']}).get_json()['data']
    assert r['issued'] == [] and r['skipped']
    assert admin.post('/api/member/M001/enable', {'reason': '误判'}).get_json()['data']['benefits_ok']
    cashier = Client(app, 'shouyin01')
    assert cashier.post('/api/member/M001/disable', {'reason': 'x'}).status_code == 403


def test_merge_preview_and_merge(app, admin):
    db = connect(app.config['DB_PATH'])
    db.execute("UPDATE members SET points=100, balance=50, balance_cents=5000, stored_principal_cents=5000 WHERE code='M002'")
    db.execute("UPDATE members SET points=30 WHERE code='M001'")
    db.commit()
    db.close()
    tid = admin.post('/api/promo/coupon-templates', {'name': '券', 'type': 'free', 'face_value': 5}).get_json()['data']['id']
    cno = admin.post('/api/promo/coupons/issue', {'template_id': tid, 'member_codes': ['M002']}).get_json()['data']['issued'][0]
    p = admin.post('/api/member/merge/preview', {'from_code': 'M002', 'to_code': 'M001'}).get_json()['data']
    assert p['after']['points'] == 130 and p['after']['coupon_count'] == 1 and p['after']['stored_balance'] == 50.0
    assert admin.post('/api/member/merge', {'from_code': 'M002', 'to_code': 'M001'}).status_code == 400     # 原因必填
    d = admin.post('/api/member/merge', {'from_code': 'M002', 'to_code': 'M001', 'reason': '同一人两个号'}).get_json()['data']
    assert d['points'] == 130 and d['stored_balance'] == 50.0 and d['merges'][0]['from_code'] == 'M002'
    assert admin.get('/api/promo/coupons/%s' % cno).get_json()['data']['member_code'] == 'M001'
    old = admin.get('/api/member?status=merged').get_json()['data']['items'][0]
    assert old['code'] == 'M002' and old['merged_into'] == 'M001'
    assert admin.post('/api/member/merge', {'from_code': 'M002', 'to_code': 'M001', 'reason': 'again'}).status_code == 409
    db = connect(app.config['DB_PATH'])
    assert db.execute("SELECT COUNT(*) FROM member_merges WHERE from_code='M002'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM points_log WHERE type IN ('合并转出','合并转入')").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM stored_log WHERE type IN ('合并转出','合并转入')").fetchone()[0] == 2
    db.close()
    # 手机号释放后可重新注册
    assert admin.post('/api/member', {'phone': '13800000002'}).status_code == 200


def test_tags_manual_and_auto(app, admin):
    t = admin.post('/api/member/tags', {'name': '高价值', 'kind': 'manual'}).get_json()['data']
    d = admin.post('/api/member/M001/tags', {'add': [t['id']]}).get_json()['data']
    assert d['tags'][0]['name'] == '高价值' and d['tags'][0]['source'] == 'manual' and d['tags'][0]['created_by'] == 'root'
    assert admin.get('/api/member?tag=高价值').get_json()['data']['total'] == 1
    auto = admin.post('/api/member/tags', {'name': '奶粉客', 'kind': 'auto', 'rule': {'days': 30, 'category': '奶粉', 'min_orders': 1}}).get_json()['data']
    db = connect(app.config['DB_PATH'])
    db.execute("INSERT INTO orders(merchant_id, order_no, biz_type, store_code, member_code, status, created_at) "
               "VALUES(1,'SX1','sale','A001','M002','已完成',datetime('now','localtime'))")
    db.execute("INSERT INTO order_items(merchant_id, order_no, line_no, product_id, product_code, qty, amount_cents) "
               "SELECT 1,'SX1',1,id,code,2,76000 FROM products WHERE code='P1' AND merchant_id=1")
    db.commit()
    db.close()
    assert admin.post('/api/member/tags/%d/refresh' % auto['id']).get_json()['data']['members'] == 1
    d = admin.get('/api/member/M002').get_json()['data']
    assert d['tags'][0]['source'] == 'auto' and d['preferences'][0]['category'] == '奶粉'


def test_points_adjust_limit_and_auth(app, admin):
    hy = Client(app, 'huiyuan')                     # 会员运营：免审 200 分
    assert hy.post('/api/member/M001/points', {'delta': 100, 'reason': '活动补偿'}).get_json()['data']['points'] == 100
    r = hy.post('/api/member/M001/points', {'delta': 500, 'reason': '大额'})
    assert r.status_code == 403 and r.get_json()['code'] == 'NEED_AUTH'
    # 审批：会员运营申请，财务（approval.member）同意，带 approval_no 重提
    ap = hy.post('/api/approvals', {'kind': 'points_adjust', 'store_code': 'A001', 'value': 500, 'title': '补积分', 'reason': '客诉',
                                   'request_key': 'pa1'})
    assert ap.status_code == 200, ap.get_json()
    no = ap.get_json()['data']['approval_no']
    fin = Client(app, 'caiwu')
    assert fin.post('/api/approvals/%s/approve' % no, {}).status_code == 200
    r = hy.post('/api/member/M001/points', {'delta': 500, 'reason': '大额', 'approval_no': no}).get_json()['data']
    assert r['points'] == 600 and r['auth']['mode'] == 'approval'
    cashier = Client(app, 'shouyin01')
    assert cashier.post('/api/member/M001/points', {'delta': 1, 'reason': 'x'}).status_code == 403


def test_stored_adjust_requires_approval_and_daily(app, admin):
    grant(app, 'caiwu', ['member.stored.adjust'])
    r = admin.post('/api/member/M001/stored', {'delta': 100, 'reason': '期初补录'}).get_json()['data']
    assert r['status'] == 'pending'
    no = r['approval_no']
    assert admin.post('/api/member/M001/stored', {'delta': 100, 'reason': '期初补录', 'approval_no': no}).status_code == 403   # 未审批
    fin = Client(app, 'caiwu')
    assert fin.post('/api/approvals/%s/approve' % no, {}).status_code == 200
    assert admin.post('/api/member/M001/stored', {'delta': 99, 'reason': 'x', 'approval_no': no}).status_code == 403          # 金额不符
    r = admin.post('/api/member/M001/stored', {'delta': 100, 'reason': '期初补录', 'approval_no': no}).get_json()['data']
    assert r['status'] == 'done' and r['stored_balance'] == 100.0
    assert admin.post('/api/member/M001/stored', {'delta': 100, 'reason': '期初补录', 'approval_no': no}).status_code in (403, 409)  # 一次性
    rep = admin.get('/api/member/stored/daily').get_json()['data']
    assert rep['balance']['total'] == 100.0 and rep['days'][0]['items'][0]['type'] == '人工调整' and rep['anomalies'] == []
    zd = Client(app, 'zhidu')
    assert zd.get('/api/member/stored/daily').status_code == 403


def test_import_and_export(app, admin):
    wb = Workbook()
    ws = wb.active
    ws.append(['手机号*', '姓名', '会员等级', '注册门店编码', '宝宝生日', '期初积分', '期初储值'])
    ws.append(['13755550001', '甲', '普通会员', 'A001', '2025-01-01', 100, 20])
    ws.append(['13755550001', '重复', '', '', '', None, None])
    ws.append(['13800000001', '库内重复', '', '', '', None, None])
    ws.append(['1375555', '短号', '', '', '', None, None])
    ws.append(['13755550002', '乙', '钻石', '', '', None, None])
    b = io.BytesIO()
    wb.save(b)
    b.seek(0)
    r = admin.c.post('/api/member/import', data={'file': (b, 'm.xlsx')}, content_type='multipart/form-data').get_json()['data']
    assert r['created'] == 1 and r['error_count'] == 4
    d = admin.get('/api/member?kw=13755550001').get_json()['data']['items'][0]
    assert d['points'] == 100 and d['stored_balance'] == 20.0
    # 导出：会员运营没有 member.export -> 403；超管有权限也脱敏 + 安全审计
    hy = Client(app, 'huiyuan')
    assert hy.get('/api/member/export').status_code == 403
    r = admin.get('/api/member/export')
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.data)).active
    phones = [row[2] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert phones and all('****' in p for p in phones)
    db = connect(app.config['DB_PATH'])
    assert db.execute("SELECT level FROM audit_logs WHERE action='导出会员'").fetchone()[0] == 'security'
    db.close()
    assert admin.get('/api/member/template').status_code == 200


def test_rules_and_points_exceptions(app, admin):
    assert admin.post('/api/member/rules', {'points_refund_policy': 'bogus'}).status_code == 400
    r = admin.post('/api/member/rules', {'points_refund_policy': 'manual', 'level_points_rate': {'金卡会员': '2'}}).get_json()['data']
    assert r['points_refund_policy'] == 'manual'
    g = admin.get('/api/member/rules').get_json()['data']
    assert g['level_points_rate'] == {'金卡会员': '2'} and 'points_per_yuan' in g['base']
    db = connect(app.config['DB_PATH'])
    db.execute("INSERT INTO points_exceptions(merchant_id, member_code, ref_no, shortfall, policy, created_at) VALUES(1,'M001','R1',50,'manual','x')")
    db.execute("UPDATE members SET points_debt=50 WHERE code='M001'")
    db.commit()
    eid = db.execute('SELECT id FROM points_exceptions').fetchone()[0]
    db.close()
    lst = admin.get('/api/member/points-exceptions').get_json()['data']
    assert lst[0]['shortfall'] == 50
    r = admin.post('/api/member/points-exceptions/%d/resolve' % eid, {'action': 'deduct', 'reason': '与顾客确认'}).get_json()['data']
    assert r['points'] == 50
    d = admin.get('/api/member/M001').get_json()['data']
    assert d['points'] == -50 and d['points_debt'] == 0
    assert admin.post('/api/member/points-exceptions/%d/resolve' % eid, {'action': 'waive', 'reason': 'x'}).status_code == 409


def test_schema_v1_to_v2_member_promo(tmp_path):
    """member_promo v1 -> v2：product_prices CHECK 重建、v1 promotions 迁成特价活动，幂等。"""
    p = str(tmp_path / 'm.db')
    init_db(p)
    db = connect(p)
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('DROP TABLE product_prices')
    db.execute("CREATE TABLE product_prices (id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, product_id INTEGER NOT NULL, "
               "unit TEXT NOT NULL DEFAULT '', scope_type TEXT NOT NULL CHECK (scope_type IN ('region','store','level')), scope_ref TEXT NOT NULL, "
               "price_cents INTEGER NOT NULL, start_at TEXT DEFAULT '', end_at TEXT DEFAULT '', src_no TEXT DEFAULT '', updated_at TEXT, "
               "UNIQUE (merchant_id, product_id, unit, scope_type, scope_ref, start_at))")
    db.execute("INSERT INTO product_prices(merchant_id, product_id, scope_type, scope_ref, price_cents) VALUES(1, 1, 'store', 'A001', 100)")
    db.execute("INSERT INTO promotions(merchant_id, name, product_code, promo_price, start_date, end_date, store_code, status) "
               "VALUES(1, '老特价', 'P1', 9.9, '2026-01-01', '', 'A001', '启用')")
    db.execute("UPDATE schema_version SET version=1 WHERE module='member_promo'")
    db.commit()
    db.close()
    init_db(p)
    init_db(p)
    db = connect(p)
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='product_prices'").fetchone()[0]
    assert "'hq'" in sql
    assert db.execute('SELECT COUNT(*) FROM product_prices').fetchone()[0] == 1
    acts = db.execute("SELECT act_no, type, status, rule FROM promo_activities WHERE src_promotion_id IS NOT NULL").fetchall()
    assert len(acts) == 1 and acts[0]['type'] == 'special_price' and acts[0]['status'] == 'approved' and '990' in acts[0]['rule']
    db.close()
