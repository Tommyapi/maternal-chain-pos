# -*- coding: utf-8 -*-
"""会员权益记账服务（积分 / 储值）+ 收银台会员卡片 / 快速注册。Owner：模块②。
收银台、后台、APP 改积分和储值只能调这里（ERP-601/605、9.6）。所有写函数必须在调用方 `with tx(db):` 内调用。

    points_change(db, actor, mid, member_code, delta, kind, ref_no, store_code='', note='', approval_no='', allow_negative=False,
                  orig_order_no='', expire_date=None) -> 新积分余额
    stored_change(db, actor, mid, member_code, delta_cents, kind, ref_no, store_code='', note='', principal_cents=None, gift_cents=None,
                  approval_no='') -> 新储值余额（分）
    member_brief(db, actor, mid, code_or_phone) -> 收银台会员卡片（手机号按 member.phone.view 脱敏、储值按 member.stored.view）
    quick_register(db, actor, mid, store_code, data) -> member_code（POS-302：只要手机号，其余选填）
    get_rules(db, mid) / save_rules(db, actor, mid, data)     会员积分/券规则（ERP-601/602）
    expire_points(db, actor, mid, today=None) -> {member_code: 过期积分}   积分有效期（定时任务或后台手动触发）
kind（流水类型，中文，直接展示）：消费获得 / 积分抵扣 / 退货扣回 / 退货返还抵扣积分 / 人工调整 / 合并转入 / 合并转出 / 过期 / 欠扣抵减 ;
                                  充值 / 消费 / 退货退回 / 储值退款 / 人工调整 / 合并转入 / 合并转出
"""
import json
from datetime import datetime, timedelta

from .money import allocate, yuan
from .util import BizError, now_s, baby_axis

# 会员/积分/券规则默认值（商户级，存 promo_settings key='rules'）。
# 积分累计/抵扣比例、抹零沿用 core.get_config 的 points_per_yuan / points_to_yuan / points_max_ratio / round_mode（模块④维护）。
RULES_DEFAULT = {
    'points_refund_policy': 'negative',   # 退货扣回积分不足：negative 形成负积分 / limit 扣到 0 记欠扣并限制抵扣 / manual 扣到 0 转人工处理
    'points_expire_days': 0,              # 消费获得积分有效期（天，0=永久）
    'level_points_rate': {},              # 等级积分倍率，如 {"金卡会员": "1.5"}
    'coupon_max_per_order': 1,            # 每单最多用几张券
    'member_levels': ['普通会员', '银卡会员', '金卡会员'],
}
POLICIES = ('negative', 'limit', 'manual')
STATUS_OK = '正常'


def get_rules(db, mid):
    r = db.execute("SELECT value FROM promo_settings WHERE merchant_id=? AND key='rules'", (mid,)).fetchone()
    out = json.loads(json.dumps(RULES_DEFAULT))
    if r:
        try:
            out.update(json.loads(r[0]))
        except ValueError:
            pass
    return out


def save_rules(db, actor, mid, data):
    from decimal import Decimal, InvalidOperation
    cur = get_rules(db, mid)
    new = dict(cur)
    if 'points_refund_policy' in data:
        if data['points_refund_policy'] not in POLICIES:
            raise BizError('积分扣回不足策略只能是 negative/limit/manual')
        new['points_refund_policy'] = data['points_refund_policy']
    if 'points_expire_days' in data:
        try:
            new['points_expire_days'] = max(0, int(data['points_expire_days'] or 0))
        except (TypeError, ValueError):
            raise BizError('积分有效期天数格式不正确')
    if 'coupon_max_per_order' in data:
        try:
            new['coupon_max_per_order'] = max(1, min(10, int(data['coupon_max_per_order'])))
        except (TypeError, ValueError):
            raise BizError('每单用券张数格式不正确')
    if 'level_points_rate' in data:
        lr = {}
        for k, v in (data.get('level_points_rate') or {}).items():
            try:
                d = Decimal(str(v))
            except InvalidOperation:
                raise BizError('等级积分倍率格式不正确：%s' % k)
            if d < 0 or d > 100:
                raise BizError('等级积分倍率需在 0~100')
            lr[str(k)[:20]] = str(d)
        new['level_points_rate'] = lr
    if 'member_levels' in data:
        lv = [str(x).strip()[:20] for x in (data.get('member_levels') or []) if str(x).strip()]
        new['member_levels'] = lv or cur['member_levels']
    db.execute("INSERT INTO promo_settings(merchant_id, key, value, updated_by, updated_at) VALUES(?, 'rules', ?, ?, ?) "
               "ON CONFLICT(merchant_id, key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
               (mid, json.dumps(new, ensure_ascii=False), (actor or {}).get('username', ''), now_s()))
    from .core import audit
    audit(db, actor, 'member', '修改会员积分规则', '', cur, new, level='high')
    return new


def find_member(db, mid, code_or_phone, for_update=False):
    """按会员码 / 手机号 / 实体卡号查（商户内）。被合并的会员返回合并去向。"""
    k = (code_or_phone or '').strip()
    if not k:
        return None
    m = db.execute('SELECT * FROM members WHERE merchant_id=? AND code=?', (mid, k)).fetchone()
    if m is None:
        m = db.execute('SELECT * FROM members WHERE merchant_id=? AND phone=?', (mid, k)).fetchone()
    if m is None:
        m = db.execute("SELECT * FROM members WHERE merchant_id=? AND card_no=? AND card_no != ''", (mid, k)).fetchone()
    return m


def _member(db, mid, code):
    m = db.execute('SELECT * FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
    if m is None:
        raise BizError('会员不存在', 404, 'NOT_FOUND')
    return m


def benefits_block_reason(m):
    """会员权益不可用原因（ERP-507 停用/黑名单/已合并）；'' = 可用。"""
    if m is None:
        return '会员不存在'
    if m['merged_into']:
        return '会员已合并到 %s' % m['merged_into']
    if int(m['blacklisted'] or 0):
        return '会员在黑名单中，不能使用会员权益'
    if (m['status'] or STATUS_OK) != STATUS_OK:
        return '会员已停用，不能使用会员权益'
    return ''


def points_change(db, actor, mid, member_code, delta, kind, ref_no, store_code='', note='', approval_no='', allow_negative=False,
                  orig_order_no='', expire_date=None):
    delta = int(delta)
    m = _member(db, mid, member_code)
    cur = int(m['points'] or 0)
    if not delta:
        return cur
    bal = cur + delta
    if bal < 0 and delta < 0 and not allow_negative:
        raise BizError('积分不足（现有 %d）' % cur, 409, 'POINTS_SHORT')
    # 条件更新防并发：余额在读之后被别的事务改了就失败重来（同一 BEGIN IMMEDIATE 下其实不会发生，双保险）
    c = db.execute('UPDATE members SET points=? WHERE id=? AND points=?', (bal, m['id'], cur))
    if c.rowcount != 1:
        raise BizError('积分余额已变化，请重试', 409, 'CONFLICT')
    db.execute('INSERT INTO points_log(merchant_id, member_code, member_name, change, balance, type, order_no, operator, note, store_code, '
               'approval_no, expire_date, orig_order_no, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (mid, member_code, m['name'], delta, bal, kind, ref_no or '', (actor or {}).get('name', ''), (note or '')[:200], store_code or '',
                approval_no or '', expire_date or '', orig_order_no or '', now_s()))
    return bal


def earn_expire_date(db, mid, today=None):
    days = int(get_rules(db, mid).get('points_expire_days') or 0)
    if days <= 0:
        return ''
    d = datetime.strptime(today, '%Y-%m-%d') if today else datetime.now()
    return (d + timedelta(days=days)).strftime('%Y-%m-%d')


def stored_change(db, actor, mid, member_code, delta_cents, kind, ref_no, store_code='', note='', principal_cents=None, gift_cents=None,
                  approval_no=''):
    """储值记账，本金/赠送分账（ERP-605）。
    充值：principal_cents + gift_cents = delta（不给按全部本金）；消费/扣减：不给拆分时按当前本金:赠送余额比例分摊（allocate，尾差规则同 9.3）；
    退回/退款：不给拆分时按全部本金。"""
    delta_cents = int(delta_cents)
    m = _member(db, mid, member_code)
    cur = int(m['balance_cents'] or 0)
    pb, gb = int(m['stored_principal_cents'] or 0), int(m['stored_gift_cents'] or 0)
    if pb + gb != cur:                       # 旧数据（v1 直接改 balance）或手工修过：差额并入本金
        pb = cur - gb if cur - gb >= 0 else cur
        gb = cur - pb
    if cur + delta_cents < 0:
        raise BizError('储值余额不足（余额 %.2f）' % (cur / 100.0), 409, 'STORED_SHORT')
    if principal_cents is None and gift_cents is None:
        if delta_cents < 0:
            p_part, g_part = allocate(-delta_cents, [pb, gb])
            principal_cents, gift_cents = -p_part, -g_part
        else:
            principal_cents, gift_cents = delta_cents, 0
    else:
        principal_cents = int(principal_cents or 0) if principal_cents is not None else delta_cents - int(gift_cents or 0)
        gift_cents = int(gift_cents or 0) if gift_cents is not None else delta_cents - principal_cents
        if principal_cents + gift_cents != delta_cents:
            raise BizError('储值本金+赠送不等于变动金额')
    if pb + principal_cents < 0 or gb + gift_cents < 0:
        raise BizError('储值本金/赠送余额不足', 409, 'STORED_SHORT')
    bal = cur + delta_cents
    c = db.execute('UPDATE members SET balance_cents=?, stored_principal_cents=?, stored_gift_cents=? WHERE id=? AND balance_cents IS ?',
                   (bal, pb + principal_cents, gb + gift_cents, m['id'], m['balance_cents']))
    if c.rowcount != 1:
        raise BizError('储值余额已变化，请重试', 409, 'CONFLICT')
    if kind == '充值':
        db.execute('UPDATE members SET total_charged_cents = COALESCE(total_charged_cents, 0) + ? WHERE id=?', (int(principal_cents), m['id']))
    db.execute('INSERT INTO stored_log(merchant_id, member_code, member_name, change_cents, balance_cents, type, order_no, store_code, operator, '
               'note, principal_cents, gift_cents, approval_no, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (mid, member_code, m['name'], delta_cents, bal, kind, ref_no or '', store_code or '', (actor or {}).get('name', ''), (note or '')[:200],
                principal_cents, gift_cents, approval_no or '', now_s()))
    return bal


def member_coupons(db, mid, code, today=None):
    today = today or datetime.now().strftime('%Y-%m-%d')
    rows = db.execute(
        "SELECT c.coupon_no, c.template_id, c.template_name, c.face_value_cents, c.threshold_cents, c.expire_date, c.status, "
        "COALESCE(t.type, 'cash') type, t.discount_rate FROM coupons c LEFT JOIN coupon_templates t ON t.id=c.template_id "
        "WHERE c.merchant_id=? AND c.member_code=? AND c.status='未使用' AND (c.expire_date='' OR c.expire_date >= ?) "
        "ORDER BY c.face_value_cents DESC, c.coupon_no", (mid, code, today)).fetchall()
    return [{'coupon_no': r['coupon_no'], 'name': r['template_name'], 'type': r['type'], 'face_value': yuan(r['face_value_cents']),
             'threshold': yuan(r['threshold_cents']), 'discount_rate': r['discount_rate'], 'expire_date': r['expire_date']} for r in rows]


def babies_of(db, member_id, include_hidden=False):
    out = []
    sql = 'SELECT * FROM babies WHERE member_id=?' + ('' if include_hidden else ' AND hidden=0') + ' ORDER BY id'
    for b in db.execute(sql, (member_id,)):
        axis, months = baby_axis(b['birthday'])
        out.append({'id': b['id'], 'kind': b['kind'] or 'baby', 'nickname': b['nickname'] or b['name'] or '', 'name': b['name'] or '',
                    'gender': b['gender'] or '', 'birthday': b['birthday'] or '', 'axis': axis, 'month_age': months,
                    'hidden': int(b['hidden'] or 0), 'note': b['note'] or ''})
    return out


def member_card(db, actor, m):
    """会员行 -> 收银台/后台通用卡片（字段权限在这里统一处理）。"""
    return _with_first_baby(_member_card(db, actor, m))


def _member_card(db, actor, m):
    from .core import mask_phone, has_perm
    block = benefits_block_reason(m)
    stored = int(m['balance_cents'] or 0)
    agg = db.execute("SELECT COUNT(*), COALESCE(SUM(payable_cents),0), MAX(created_at) FROM orders WHERE merchant_id=? AND member_code=? "
                     "AND biz_type='sale'", (m['merchant_id'], m['code'])).fetchone()
    ref = db.execute("SELECT COALESCE(SUM(payable_cents),0) FROM orders WHERE merchant_id=? AND member_code=? AND biz_type='refund'",
                     (m['merchant_id'], m['code'])).fetchone()[0]
    return {
        'id': m['id'], 'code': m['code'], 'name': m['name'] or '', 'phone': mask_phone(m['phone'], actor), 'level': m['level'] or '普通会员',
        'card_no': m['card_no'] or '', 'points': int(m['points'] or 0), 'points_debt': int(m['points_debt'] or 0),
        'stored_balance': yuan(stored) if has_perm(actor, 'member.stored.view') else None,
        'status': m['status'] or STATUS_OK, 'blacklisted': int(m['blacklisted'] or 0), 'merged_into': m['merged_into'] or '',
        'benefits_ok': not block, 'benefits_block': block,
        'reg_store_code': m['reg_store_code'] or '', 'reg_store_name': m['reg_store_name'] or '', 'reg_time': m['reg_time'] or '',
        'guide_code': m['guide_code'] or '', 'guide_name': m['guide_name'] or '', 'source': m['source'] or '',
        'babies': babies_of(db, m['id']), 'coupons': member_coupons(db, m['merchant_id'], m['code']) if not block else [],
        'baby': None,   # 兼容 v1 member_full：第一个未隐藏的宝宝（下面回填）
        'orders': int(agg[0] or 0), 'amount': yuan(int(agg[1] or 0) + int(ref or 0)), 'last_time': agg[2] or '',
    }


def _with_first_baby(card):
    card['baby'] = card['babies'][0] if card['babies'] else None     # 兼容 v1 member_full 的 baby 字段
    return card


def member_brief(db, actor, mid, code_or_phone):
    m = find_member(db, mid, code_or_phone)
    if m is None:
        return None
    if m['merged_into']:
        to = db.execute('SELECT * FROM members WHERE merchant_id=? AND code=?', (mid, m['merged_into'])).fetchone()
        if to is not None:
            m = to
    return member_card(db, actor, m)


def next_member_code(db, mid, prefix='XB', width=8):
    n = 0
    for r in db.execute("SELECT code FROM members WHERE merchant_id=? AND code LIKE ? ORDER BY length(code) DESC, code DESC LIMIT 50",
                        (mid, prefix + '%')):
        tail = r[0][len(prefix):]
        if tail.isdigit():
            n = max(n, int(tail))
    return '%s%0*d' % (prefix, width, n + 1)


def _valid_date(s, label):
    s = (s or '').strip()
    if not s:
        return ''
    try:
        datetime.strptime(s, '%Y-%m-%d')
    except ValueError:
        raise BizError('%s格式应为 YYYY-MM-DD' % label)
    return s


def add_baby(db, member_id, mid, b):
    kind = b.get('kind') or ('pregnancy' if b.get('due_date') else 'baby')
    if kind not in ('baby', 'pregnancy'):
        raise BizError('宝宝档案类型只能是 baby/pregnancy')
    bd = _valid_date(b.get('birthday') or b.get('due_date'), '宝宝生日/预产期')
    db.execute('INSERT INTO babies(merchant_id, member_id, name, gender, birthday, note, kind, nickname, hidden) VALUES(?,?,?,?,?,?,?,?,0)',
               (mid, member_id, (b.get('name') or b.get('nickname') or '')[:20], (b.get('gender') or '')[:4], bd, (b.get('note') or '')[:100],
                kind, (b.get('nickname') or b.get('name') or '')[:20]))


def quick_register(db, actor, mid, store_code, data):
    """POS-302：收银台快速注册，只要手机号；宝宝生日/预产期选填。返回会员码。"""
    from .core import audit
    phone = (data.get('phone') or '').strip()
    if len(phone) != 11 or not phone.isdigit():
        raise BizError('请输入 11 位手机号')
    if db.execute('SELECT 1 FROM members WHERE merchant_id=? AND phone=?', (mid, phone)).fetchone():
        raise BizError('该手机号已是会员')
    st = db.execute('SELECT name FROM stores WHERE merchant_id=? AND code=?', (mid, store_code)).fetchone() if store_code else None
    gcode = (data.get('guide_code') or '').strip()
    g = db.execute('SELECT code, name FROM guides WHERE merchant_id=? AND code=?', (mid, gcode)).fetchone() if gcode else None
    code = next_member_code(db, mid)
    name = (data.get('name') or '').strip()[:20] or ('会员' + phone[-4:])
    levels = get_rules(db, mid)['member_levels']
    level = (data.get('level') or '').strip() or levels[0]
    db.execute('INSERT INTO members(merchant_id, code, phone, name, level, birthday, points, balance, total_charged, reg_time, reg_store_code, '
               'reg_store_name, guide_code, guide_name, note, status, source, card_no, updated_at) '
               'VALUES(?,?,?,?,?,?,0,0,0,?,?,?,?,?,?,?,?,?,?)',
               (mid, code, phone, name, level, '', now_s(), store_code or '', st[0] if st else '', g['code'] if g else '', g['name'] if g else '',
                (data.get('note') or '')[:200], STATUS_OK, (data.get('source') or 'pos')[:20], (data.get('card_no') or '')[:40], now_s()))
    mem_id = db.execute('SELECT id FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone()[0]
    if data.get('baby_birthday'):
        add_baby(db, mem_id, mid, {'kind': 'baby', 'birthday': data.get('baby_birthday'), 'name': data.get('baby_name'),
                                    'gender': data.get('baby_gender')})
    if data.get('due_date'):
        add_baby(db, mem_id, mid, {'kind': 'pregnancy', 'birthday': data.get('due_date')})
    for b in (data.get('babies') or [])[:10]:
        add_baby(db, mem_id, mid, b)
    audit(db, actor, 'member', '注册会员', code, None, {'code': code, 'store': store_code, 'source': data.get('source') or 'pos'},
          store_code=store_code or None)
    return code


def expire_points(db, actor, mid, today=None):
    """积分过期（先进先出近似）：已过期获得积分 − 历史全部扣减 = 仍未用掉的已过期积分，扣掉（不超过当前余额）。幂等。"""
    today = today or datetime.now().strftime('%Y-%m-%d')
    out = {}
    rows = db.execute("SELECT member_code, SUM(CASE WHEN change>0 AND expire_date!='' AND expire_date<? THEN change ELSE 0 END) e, "
                      "SUM(CASE WHEN change<0 THEN -change ELSE 0 END) d FROM points_log WHERE merchant_id=? GROUP BY member_code",
                      (today, mid)).fetchall()
    for r in rows:
        left = int(r['e'] or 0) - int(r['d'] or 0)
        if left <= 0:
            continue
        m = db.execute('SELECT points FROM members WHERE merchant_id=? AND code=?', (mid, r['member_code'])).fetchone()
        if m is None:
            continue
        n = min(left, int(m['points'] or 0))
        if n > 0:
            points_change(db, actor, mid, r['member_code'], -n, '过期', '', note='积分到期 %s' % today)
            out[r['member_code']] = n
    return out
