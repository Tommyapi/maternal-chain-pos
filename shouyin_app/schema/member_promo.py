# -*- coding: utf-8 -*-
"""member_promo：价格体系 / 调价单 / 促销活动 / 优惠券生命周期 / 积分 / 会员标签合并 / 储值。Owner：模块②。
旧表 members / babies / points_log / stored_log / coupon_templates / coupons / promotions 也归②。
"""
MODULE = 'member_promo'
VERSION = 2

SCHEMA = """
-- 价格体系（ERP-401）：零售 门店价 > 区域价 > 总部调价(hq, scope_ref='*') > products.retail；
-- 会员 等级价(level) > 总部会员价调价(member, '*') > products.member_price。start_at 到点自动生效（调价单写入）
CREATE TABLE IF NOT EXISTS product_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, product_id INTEGER NOT NULL, unit TEXT NOT NULL DEFAULT '',
    scope_type TEXT NOT NULL CHECK (scope_type IN ('hq','member','region','store','level')), scope_ref TEXT NOT NULL,
    price_cents INTEGER NOT NULL, start_at TEXT DEFAULT '', end_at TEXT DEFAULT '', src_no TEXT DEFAULT '', updated_at TEXT,
    UNIQUE (merchant_id, product_id, unit, scope_type, scope_ref, start_at));
CREATE INDEX IF NOT EXISTS idx_prices_p ON product_prices(merchant_id, product_id);

-- 调价单（ERP-402）
CREATE TABLE IF NOT EXISTS price_adjusts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pa_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'hq', scope_refs TEXT NOT NULL DEFAULT '', effective_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','effective','rejected','cancelled')),
    reason TEXT DEFAULT '', created_by TEXT, created_at TEXT NOT NULL, approved_by TEXT DEFAULT '', approved_at TEXT, effected_at TEXT);
CREATE TABLE IF NOT EXISTS price_adjust_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, pa_no TEXT NOT NULL, product_id INTEGER NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('retail','member_price','cost','level_price')), level TEXT DEFAULT '',
    old_cents INTEGER, new_cents INTEGER NOT NULL);

-- 促销活动（POS-401~406 / ERP-403~404）。rule / scope 用 JSON，结构见 docs/API-CONTRACT.md §促销
CREATE TABLE IF NOT EXISTS promo_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT, act_no TEXT NOT NULL UNIQUE, merchant_id INTEGER NOT NULL, name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('special_price','item_discount','order_discount','full_reduce','full_gift','nth_discount','bundle_price','redeem')),
    priority INTEGER NOT NULL DEFAULT 100, exclusive_group TEXT DEFAULT '', stack_member_price INTEGER NOT NULL DEFAULT 0,
    stack_coupon INTEGER NOT NULL DEFAULT 1, scope TEXT NOT NULL DEFAULT '{}', rule TEXT NOT NULL DEFAULT '{}',
    start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','paused','ended','cancelled')),
    created_by TEXT, created_at TEXT NOT NULL, approved_by TEXT DEFAULT '', approved_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_promo_m ON promo_activities(merchant_id, status, start_at, end_at);

-- 优惠券流水（ERP-604：每次状态变化一行）
CREATE TABLE IF NOT EXISTS coupon_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, coupon_no TEXT NOT NULL,
    from_status TEXT, to_status TEXT NOT NULL, ref_no TEXT DEFAULT '', operator TEXT DEFAULT '', note TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_coupon_logs ON coupon_logs(merchant_id, coupon_no);

CREATE TABLE IF NOT EXISTS member_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'manual',
    rule TEXT DEFAULT '', status TEXT DEFAULT '启用', UNIQUE (merchant_id, name));
CREATE TABLE IF NOT EXISTS member_tag_links (
    merchant_id INTEGER NOT NULL, member_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
    created_by TEXT DEFAULT '', created_at TEXT, PRIMARY KEY (member_id, tag_id));
-- 会员/积分/券的商户级规则（ERP-601/602/603）：key='rules' 的 JSON，见 member_svc.RULES_DEFAULT
CREATE TABLE IF NOT EXISTS promo_settings (
    merchant_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '{}', updated_by TEXT DEFAULT '', updated_at TEXT,
    PRIMARY KEY (merchant_id, key));

-- 退货权益处理台账（POS-306/AC-017）：按原单+行累计已退数量和已扣回/返还积分，保证多次部分退累计不超
CREATE TABLE IF NOT EXISTS benefit_reversals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, orig_order_no TEXT NOT NULL, refund_no TEXT NOT NULL,
    line_no INTEGER NOT NULL, qty TEXT NOT NULL DEFAULT '0', points_back INTEGER NOT NULL DEFAULT 0, points_restore INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, UNIQUE (merchant_id, refund_no, line_no));
CREATE INDEX IF NOT EXISTS idx_benefit_rev ON benefit_reversals(merchant_id, orig_order_no);

-- 积分扣回不足的异常记录（ERP-602：负积分 / 限制抵扣 / 人工处理，不得静默丢失差额）
CREATE TABLE IF NOT EXISTS points_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, member_code TEXT NOT NULL, ref_no TEXT NOT NULL,
    orig_order_no TEXT DEFAULT '', shortfall INTEGER NOT NULL, policy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')), note TEXT DEFAULT '',
    created_at TEXT NOT NULL, resolved_by TEXT DEFAULT '', resolved_at TEXT);
CREATE INDEX IF NOT EXISTS idx_points_exc ON points_exceptions(merchant_id, status);

CREATE TABLE IF NOT EXISTS member_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id INTEGER NOT NULL, from_code TEXT NOT NULL, to_code TEXT NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '{}', operator TEXT, reason TEXT DEFAULT '', created_at TEXT NOT NULL);
"""

# 旧表补列
EXTRA = [
    ('babies', 'kind', "TEXT NOT NULL DEFAULT 'baby'"),          # baby / pregnancy（birthday 存预产期）
    ('babies', 'hidden', 'INTEGER NOT NULL DEFAULT 0'),
    ('babies', 'nickname', "TEXT DEFAULT ''"),
    ('coupon_templates', 'type', "TEXT NOT NULL DEFAULT 'cash'"),  # cash 满减 / discount 折扣 / free 无门槛 / product 商品券
    ('coupon_templates', 'discount_rate', 'REAL'),
    ('coupon_templates', 'scope', "TEXT NOT NULL DEFAULT '{}'"),
    ('coupon_templates', 'per_member_limit', 'INTEGER NOT NULL DEFAULT 0'),
    ('coupon_templates', 'total_limit', 'INTEGER NOT NULL DEFAULT 0'),
    ('coupon_templates', 'issued_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('coupons', 'locked_order_no', "TEXT DEFAULT ''"),
    ('coupons', 'issue_ref', "TEXT DEFAULT ''"),
    ('points_log', 'store_code', "TEXT DEFAULT ''"),
    ('points_log', 'expire_date', "TEXT DEFAULT ''"),
    ('points_log', 'approval_no', "TEXT DEFAULT ''"),
    ('stored_log', 'principal_cents', 'INTEGER'),              # 本金部分
    ('stored_log', 'gift_cents', 'INTEGER'),                   # 赠送部分
    ('stored_log', 'approval_no', "TEXT DEFAULT ''"),
    # v2
    ('members', 'points_debt', 'INTEGER NOT NULL DEFAULT 0'),          # 「限制抵扣/人工」策略下未扣回的积分（ERP-602）
    ('members', 'stored_principal_cents', 'INTEGER NOT NULL DEFAULT 0'),  # 储值本金余额
    ('members', 'stored_gift_cents', 'INTEGER NOT NULL DEFAULT 0'),       # 储值赠送余额
    ('members', 'disabled_reason', "TEXT DEFAULT ''"),
    ('members', 'updated_at', "TEXT DEFAULT ''"),
    ('points_log', 'orig_order_no', "TEXT DEFAULT ''"),
    ('coupon_templates', 'merchant_code', "TEXT DEFAULT ''"),
    ('coupon_templates', 'return_on_refund', 'INTEGER NOT NULL DEFAULT 1'),   # 整单退货时券是否退回
    ('coupon_templates', 'start_date', "TEXT DEFAULT ''"),
    ('coupon_templates', 'end_date', "TEXT DEFAULT ''"),
    ('coupon_templates', 'max_discount_cents', 'INTEGER NOT NULL DEFAULT 0'),  # 折扣券封顶（0=不封顶）
    ('coupons', 'locked_at', "TEXT DEFAULT ''"),
    ('coupons', 'void_reason', "TEXT DEFAULT ''"),
    ('promo_activities', 'points_rate', "TEXT DEFAULT ''"),        # 活动积分倍率（空=不加倍）
    ('promo_activities', 'src_promotion_id', 'INTEGER'),         # 由 v1 promotions 迁移而来
]


def migrate(db, from_version):
    from . import add_column
    for t, c, d in EXTRA:
        add_column(db, t, c, d)
    db.execute('CREATE INDEX IF NOT EXISTS idx_points_m ON points_log(merchant_id, member_code)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_stored_m ON stored_log(merchant_id, member_code)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_members_card ON members(merchant_id, card_no)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_points_orig ON points_log(merchant_id, orig_order_no)')
    # 储值本金/赠送分账：旧余额全部视为本金（只回填从未分账的会员）
    db.execute('UPDATE members SET stored_principal_cents = COALESCE(balance_cents, 0) '
               'WHERE stored_principal_cents = 0 AND stored_gift_cents = 0 AND COALESCE(balance_cents, 0) > 0')
    _rebuild_prices_check(db)
    migrate_v1_promotions(db)


def _rebuild_prices_check(db):
    """v1 的 product_prices CHECK 不含 hq/member：重建（只在开发库出现过 v1，表为空或很小）。"""
    r = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='product_prices'").fetchone()
    if r is None or "'hq'" in r[0]:
        return
    db.execute('ALTER TABLE product_prices RENAME TO _old_product_prices')
    db.execute('DROP INDEX IF EXISTS idx_prices_p')
    from . import run_script
    start = SCHEMA.index('CREATE TABLE IF NOT EXISTS product_prices')
    end = SCHEMA.index('-- 调价单')
    run_script(db, SCHEMA[start:end])
    db.execute('INSERT INTO product_prices SELECT * FROM _old_product_prices')
    db.execute('DROP TABLE _old_product_prices')


def migrate_v1_promotions(db):
    """v1 单品特价 promotions -> promo_activities(type=special_price)。幂等：按 src_promotion_id 去重。"""
    import json
    from ..util import now_s
    ts = now_s()
    rows = db.execute('SELECT * FROM promotions WHERE id NOT IN (SELECT src_promotion_id FROM promo_activities '
                      'WHERE src_promotion_id IS NOT NULL) ORDER BY id').fetchall()
    for r in rows:
        price = r['promo_price_cents'] if 'promo_price_cents' in r.keys() and r['promo_price_cents'] is not None else int(round((r['promo_price'] or 0) * 100))
        scope = {'product_codes': [r['product_code']]}
        if r['store_code']:
            scope['stores'] = [r['store_code']]
        db.execute('INSERT INTO promo_activities(act_no, merchant_id, name, type, priority, scope, rule, start_at, end_at, status, created_by, '
                   'created_at, updated_at, src_promotion_id, stack_member_price) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
                   ('ACV1%07d' % r['id'], r['merchant_id'] or 1, r['name'] or '单品特价', 'special_price', 100,
                    json.dumps(scope, ensure_ascii=False),
                    json.dumps({'items': [{'code': r['product_code'], 'price_cents': int(price)}]}, ensure_ascii=False),
                    (r['start_date'] or '2000-01-01')[:10] + ' 00:00:00', (r['end_date'] or '2099-12-31')[:10] + ' 23:59:59',
                    'approved' if r['status'] == '启用' else 'ended', 'migrate', ts, ts, r['id']))
