# -*- coding: utf-8 -*-
"""促销计价引擎 + 会员权益记账（收银台 ⇄ 促销 的契约）。Owner：模块②。收银台（模块③）只调用这里，不自己算价。

契约（完整字段见 docs/API-CONTRACT.md §3.3，输出格式不变）：
    price_cart(db, actor, store_code, cart, now=None) -> priced     纯计算，不写库；/api/pos/price 与 checkout 都调它（服务端最终金额）
    apply_benefits(db, actor, order_no, store_code, priced)         结算事务内：核销券(coupon_logs)、扣抵扣积分、发消费积分
    reverse_benefits(db, actor, refund_no, orig_order, lines)       退货事务内：按原单快照退券/返还抵扣积分/扣回消费积分（不足按配置）
    lock_coupons(db, actor, ref_no, coupon_nos) / release_coupons(db, actor, ref_no)   挂单/待支付占用与释放（POS-304、9.6）

计价层级（ARCH §5、规格 9.3）：
  单品层：价格体系（门店价 > 区域价 > 总部统一价）→ 会员价（等级价优先）→ 单品活动（特价/单品折扣/第二件/套餐/换购，每行最多一个）
          → 手工改价/单品折扣/手工赠送（need_auth）
  整单层（逐层对上一层后的行金额分摊，money.allocate，尾差按小数部分从大到小）：
          整单类活动（满减/整单折扣，按优先级） → 收银整单折扣 → 满赠（追加赠品行）→ 优惠券 → 积分抵扣 → 抹零
  活动：status=approved 且 start_at <= now <= end_at；范围 scope（门店/区域/商品/分类/品牌/会员等级）；优先级 priority 小的先算，
        同优先级按活动号；exclusive_group 相同的活动一单只用一个；stack_member_price=0 时会员价更优则不用活动；stack_coupon=0 的活动覆盖的行不能用券。
  结果确定：所有查询带 ORDER BY，同输入同输出（snapshot.at 除外）。
"""
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

from .money import to_cents, allocate, mul_rate, floor_to, MoneyError
from .util import BizError, now_s

ITEM_TYPES = ('special_price', 'item_discount', 'nth_discount', 'bundle_price', 'redeem')
ORDER_TYPES = ('order_discount', 'full_reduce')
TYPE_NAME = {'special_price': '特价', 'item_discount': '单品折扣', 'order_discount': '整单折扣', 'full_reduce': '满减',
             'full_gift': '满赠', 'nth_discount': '第N件折扣', 'bundle_price': '套餐价', 'redeem': '换购'}
COUPON_TYPE_NAME = {'cash': '满减券', 'discount': '折扣券', 'free': '无门槛券', 'product': '商品券'}


# ================================================================ 小工具
def _dec(v, label='数值'):
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise BizError('%s格式不正确：%s' % (label, v))
    if not d.is_finite():
        raise BizError('%s格式不正确：%s' % (label, v))
    return d


def _rate(v, label='折扣率'):
    d = _dec(v, label)
    if not (Decimal(0) < d <= Decimal(1)):
        raise BizError('%s需在 0~1 之间（如 0.88）' % label)
    return d


def _cents_of(v, label='金额'):
    try:
        c = to_cents(v)
    except MoneyError as e:
        raise BizError('%s：%s' % (label, e))
    if c < 0:
        raise BizError('%s不能为负' % label)
    return c


def _qty_num(q):
    """JSON 输出用数量：整数给 int，否则 float（数量不是金额）。"""
    return int(q) if q == q.to_integral_value() else float(q)


def _yuan_s(c):
    return '%.2f' % (int(c) / 100.0)


def _jl(s, default):
    if isinstance(s, (dict, list)):
        return s
    try:
        v = json.loads(s or '')
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


def _config(db, mid):
    from .core import get_config
    from .member_svc import get_rules
    cfg = get_config(db, mid)
    rules = get_rules(db, mid)
    p2y = int(Decimal(str(cfg.get('points_to_yuan') or 100)))
    return {
        'points_per_yuan': str(cfg.get('points_per_yuan', 1)), 'points_to_yuan': max(1, p2y),
        'points_max_ratio': str(cfg.get('points_max_ratio', 0.5)), 'round_mode': cfg.get('round_mode') or 'none',
        'level_points_rate': rules.get('level_points_rate') or {}, 'coupon_max_per_order': int(rules.get('coupon_max_per_order') or 1),
        'points_refund_policy': rules.get('points_refund_policy') or 'negative',
    }


def _store_region(db, mid, store_code):
    r = db.execute('SELECT o.code FROM stores s JOIN org_units o ON o.id=s.region_id WHERE s.merchant_id=? AND s.code=?',
                   (mid, store_code)).fetchone()
    return r[0] if r else ''


def _scope_ok(scope, store_code, region, member_level, product=None):
    """活动/券范围：空列表 = 不限。product=None 只判门店/会员层。"""
    if scope.get('stores') and store_code not in scope['stores']:
        return False
    if scope.get('regions') and region not in scope['regions']:
        return False
    if scope.get('levels') and (member_level or '') not in scope['levels']:
        return False
    if scope.get('member_only') and not member_level:
        return False
    if product is None:
        return True
    code = product['code']
    if code in (scope.get('exclude_codes') or []):
        return False
    pc, cats, brands = scope.get('product_codes') or [], scope.get('categories') or [], scope.get('brands') or []
    if not (pc or cats or brands):
        return True
    return code in pc or (product['category'] or '') in cats or (product['brand'] or '') in brands


def _price_list(db, mid, product_id, unit, scope_type, scope_ref, now):
    """product_prices 里当前生效的价（分）；unit='' 表示按基本单位定价。"""
    if not scope_ref:
        return None
    r = db.execute("SELECT price_cents, unit FROM product_prices WHERE merchant_id=? AND product_id=? AND scope_type=? AND scope_ref=? "
                   "AND unit IN (?, '') AND (start_at='' OR start_at<=?) AND (end_at='' OR end_at>=?) "
                   "ORDER BY CASE WHEN unit=? THEN 0 ELSE 1 END, start_at DESC, id DESC LIMIT 1",
                   (mid, product_id, scope_type, scope_ref, unit, now, now, unit)).fetchone()
    return r


def _active_activities(db, mid, now):
    acts = []
    for r in db.execute("SELECT * FROM promo_activities WHERE merchant_id=? AND status='approved' AND start_at<=? AND end_at>=? "
                        "ORDER BY priority, act_no", (mid, now, now)):
        d = dict(r)
        d['scope'] = _jl(d['scope'], {})
        d['rule'] = _jl(d['rule'], {})
        acts.append(d)
    # v1 promotions（迁移后新增、尚未转成活动的单品特价）：视作特价活动，优先级 100
    day = now[:10]
    for r in db.execute("SELECT * FROM promotions WHERE merchant_id=? AND status='启用' AND (start_date='' OR start_date IS NULL OR start_date<=?) "
                        "AND (end_date='' OR end_date IS NULL OR end_date>=?) AND id NOT IN (SELECT src_promotion_id FROM promo_activities "
                        "WHERE merchant_id=? AND src_promotion_id IS NOT NULL) ORDER BY id", (mid, day, day, mid)):
        scope = {'product_codes': [r['product_code']]}
        if r['store_code']:
            scope['stores'] = [r['store_code']]
        acts.append({'act_no': 'ACV1%07d' % r['id'], 'name': r['name'] or '单品特价', 'type': 'special_price', 'priority': 100,
                     'exclusive_group': '', 'stack_member_price': 0, 'stack_coupon': 1, 'scope': scope, 'points_rate': '',
                     'rule': {'items': [{'code': r['product_code'], 'price_cents': int(r['promo_price_cents'] or 0)}]},
                     'start_at': '', 'end_at': ''})
    acts.sort(key=lambda a: (int(a['priority']), a['act_no']))
    return acts


def _act_brief(a):
    return {'act_no': a['act_no'], 'name': a['name'], 'type': a['type'], 'priority': a['priority'], 'exclusive_group': a.get('exclusive_group') or '',
            'stack_member_price': int(a.get('stack_member_price') or 0), 'stack_coupon': int(a.get('stack_coupon') if a.get('stack_coupon') is not None else 1),
            'scope': a['scope'], 'rule': a['rule'], 'points_rate': a.get('points_rate') or '', 'start_at': a.get('start_at', ''), 'end_at': a.get('end_at', '')}


# ================================================================ 计价
def price_cart(db, actor, store_code, cart, now=None):
    """cart = {'items':[{'code','qty','unit'?,'price'?(元，手工改价),'rate'?(单品手工折扣),'batch_id'?,'is_gift'?(手工赠送),'line_remark'?}],
               'member_code'?, 'coupon_nos'?:[..], 'points_used'?, 'order_discount'?:{'rate'|'amount'}, 'round_mode'?}
    返回 priced（全部金额为分，见 API-CONTRACT §3.3）。纯计算，不写库。"""
    from . import goods_svc, inventory
    from .member_svc import find_member, benefits_block_reason, member_card
    mid = int(actor['merchant_id'])
    now = now or now_s()
    cfg = _config(db, mid)
    items_in = (cart or {}).get('items') or []
    if not isinstance(items_in, list) or not items_in:
        raise BizError('购物车为空')
    if len(items_in) > 500:
        raise BizError('一单最多 500 行商品')
    region = _store_region(db, mid, store_code)
    explain, need_auth, discounts = [], [], []

    # ---------------- 会员
    member, mrow, mlevel = None, None, ''
    mcode = ((cart or {}).get('member_code') or '').strip()
    benefits = False
    if mcode:
        mrow = find_member(db, mid, mcode)
        if mrow is None:
            raise BizError('会员不存在')
        if mrow['merged_into']:
            raise BizError('该会员已合并到 %s，请使用新会员号' % mrow['merged_into'])
        member = member_card(db, actor, mrow)
        block = benefits_block_reason(mrow)
        benefits = not block
        if block:
            explain.append(block + '：不享受会员价、积分和优惠券')
        else:
            mlevel = mrow['level'] or '普通会员'

    # ---------------- 单品层：取商品、价格体系、会员价
    lines = []
    for i, it in enumerate(items_in):
        if not isinstance(it, dict):
            raise BizError('购物车第 %d 行格式不正确' % (i + 1))
        sku = goods_svc.find_sku(db, mid, it.get('code'))
        if sku is None:
            raise BizError('商品不存在：%s' % (it.get('code'),))
        p = sku['product']
        why = goods_svc.sale_status(db, mid, store_code, p)
        if why:
            raise BizError('%s：%s' % (p['name'], why))
        unit, rate = sku['unit'], int(sku['rate'] or 1)
        want_unit = (it.get('unit') or '').strip()
        urow = None
        if want_unit and want_unit != unit:
            if want_unit == (p['base_unit'] or p['unit'] or ''):
                unit, rate = want_unit, 1
            else:
                urow = db.execute("SELECT * FROM product_units WHERE merchant_id=? AND product_id=? AND unit=? AND status='启用'",
                                  (mid, p['id'], want_unit)).fetchone()
                if urow is None:
                    raise BizError('%s 没有销售单位「%s」' % (p['name'], want_unit))
                unit, rate = urow['unit'], int(urow['rate'])
        elif rate > 1:
            urow = db.execute('SELECT * FROM product_units WHERE merchant_id=? AND product_id=? AND unit=?', (mid, p['id'], unit)).fetchone()
        qty = _dec(it.get('qty') if it.get('qty') not in (None, '') else 1, '数量')
        if qty <= 0 or qty > 99999:
            raise BizError('数量必须大于 0：%s' % p['name'])
        base_qty = inventory.to_base_qty(qty, rate)
        # 价格体系（ERP-401）：门店价 > 区域价 > 总部调价 > 商品档案零售价；按单位定价优先，否则基本单位价 × 换算率
        hq = (urow['retail_cents'] if urow is not None and urow['retail_cents'] else int(p['retail_cents'] or 0) * rate)
        list_cents, list_src = hq, 'hq'
        for st, ref in (('store', store_code), ('region', region), ('hq', '*')):
            r = _price_list(db, mid, p['id'], unit, st, ref, now)
            if r is not None:
                list_cents = int(r['price_cents']) * (1 if r['unit'] == unit and unit else rate)
                list_src = st
                break
        member_cents = None
        if benefits:
            r = _price_list(db, mid, p['id'], unit, 'level', mlevel, now) or _price_list(db, mid, p['id'], unit, 'member', '*', now)
            if r is not None:
                member_cents = int(r['price_cents']) * (1 if r['unit'] == unit and unit else rate)
            else:
                mp = urow['member_price_cents'] if urow is not None and urow['member_price_cents'] else int(p['member_price_cents'] or 0) * rate
                member_cents = mp if mp > 0 else None
            if member_cents is not None and member_cents >= list_cents:
                member_cents = None
        manual_price = it.get('price') not in (None, '')
        if list_cents <= 0 and not manual_price and not it.get('is_gift'):
            raise BizError('商品【%s】还没有设置售价，请改价后再卖，或到后台定价' % p['name'])
        lines.append({
            'line_no': i + 1, 'p': p, 'unit': unit, 'unit_rate': rate, 'qty': qty, 'base_qty': base_qty, 'in': it,
            'batch_id': it.get('batch_id'), 'is_gift': bool(it.get('is_gift')), 'auto_gift': False,
            'list_cents': list_cents, 'list_src': list_src, 'member_cents': member_cents,
            'unit_cents': member_cents if member_cents is not None else list_cents,     # 当前单价（活动前）
            'act': None, 'act_cut': 0, 'promos': [], 'manual': manual_price or it.get('rate') not in (None, '') or bool(it.get('is_gift')),
            'no_coupon': False, 'allow_discount': int(p['allow_discount'] if p['allow_discount'] is not None else 1),
            'allow_points': int(p['allow_points'] if p['allow_points'] is not None else 1),
            'cost_cents': int(p['cost_cents'] or 0) * base_qty, 'remark': (it.get('line_remark') or '')[:100],
        })

    acts = _active_activities(db, mid, now)
    used_groups, applied = set(), {}

    def group_free(a):
        g = a.get('exclusive_group') or ''
        return not g or g not in used_groups

    def mark(a):
        if a.get('exclusive_group'):
            used_groups.add(a['exclusive_group'])
        applied[a['act_no']] = a

    def act_lines(a, pool):
        return [L for L in pool if _scope_ok(a['scope'], store_code, region, mlevel, L['p'])]

    store_ok = [a for a in acts if _scope_ok(a['scope'], store_code, region, mlevel)]
    plain = lambda: [L for L in lines if not L['is_gift'] and not L['manual'] and L['act'] is None and L['allow_discount']]  # noqa: E731

    # ---------------- 单品活动：特价 / 单品折扣
    for a in store_ok:
        if a['type'] not in ('special_price', 'item_discount') or not group_free(a):
            continue
        hit = False
        stack = int(a.get('stack_member_price') or 0)
        prices = {}
        if a['type'] == 'special_price':
            for x in a['rule'].get('items') or []:
                if x.get('code'):
                    prices[x['code']] = int(x['price_cents']) if x.get('price_cents') is not None else _cents_of(x.get('price'), '特价')
        for L in act_lines(a, plain()):
            start = L['member_cents'] if (stack and L['member_cents'] is not None) else L['list_cents']
            if a['type'] == 'special_price':
                if L['p']['code'] not in prices:
                    continue
                actp = prices[L['p']['code']] * L['unit_rate']
            else:
                actp = mul_rate(start, _rate(a['rule'].get('rate'), '活动折扣率'))
            if actp >= start:
                continue
            if not stack and L['member_cents'] is not None and L['member_cents'] <= actp:
                explain.append('%s：会员价更优，未使用活动「%s」' % (L['p']['name'], a['name']))
                continue
            if not stack:
                L['member_cents'] = None          # 活动不与会员价叠加：按原价起算，不记会员价优惠
            L['unit_cents'] = actp
            L['act'] = a
            hit = True
        if hit:
            mark(a)

    # 会员价 / 活动价 落成行金额（单价 × 数量，四舍五入到分；逐级差额记优惠，合计严格可还原）
    for L in lines:
        q = L['qty']
        L['retail_line'] = mul_rate(L['list_cents'], q)
        L['line'] = L['retail_line']
        if L['member_cents'] is not None:
            v = mul_rate(L['member_cents'], q)
            L['promos'].append({'kind': 'member_price', 'source_no': mlevel, 'name': '会员价', 'amount_cents': L['line'] - v})
            L['line'] = v
        if L['act'] is not None:
            v = mul_rate(L['unit_cents'], q)
            L['promos'].append({'kind': 'activity', 'source_no': L['act']['act_no'], 'name': L['act']['name'], 'amount_cents': L['line'] - v})
            L['line'] = v

    # ---------------- 第 N 件折扣（同一行内按基本单位计件）
    for a in store_ok:
        if a['type'] != 'nth_discount' or not group_free(a):
            continue
        n = int(a['rule'].get('n') or 2)
        r = _rate(a['rule'].get('rate'), '第N件折扣率')
        hit = False
        for L in act_lines(a, plain()):
            k = L['base_qty'] // max(n, 1)
            if k <= 0:
                continue
            per_base = Decimal(L['line']) / Decimal(L['base_qty'])
            cut = int((per_base * k * (Decimal(1) - r)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            if cut <= 0:
                continue
            L['act'] = a
            L['promos'].append({'kind': 'activity', 'source_no': a['act_no'], 'name': a['name'], 'amount_cents': cut})
            L['line'] -= cut
            hit = True
        if hit:
            mark(a)
        elif act_lines(a, plain()):
            explain.append('「%s」：第 %d 件起享 %s 折，数量不足' % (a['name'], n, r))

    # ---------------- 套餐价（每种商品各 1 件成套）
    for a in store_ok:
        if a['type'] != 'bundle_price' or not group_free(a):
            continue
        codes = [c for c in (a['rule'].get('codes') or []) if c]
        bprice = int(a['rule']['price_cents']) if a['rule'].get('price_cents') is not None else _cents_of(a['rule'].get('price'), '套餐价')
        pool = dict((L['p']['code'], L) for L in plain() if L['unit_rate'] == 1)
        if not codes or any(c not in pool for c in codes):
            continue
        sets = min(pool[c]['base_qty'] for c in codes)
        if sets <= 0:
            continue
        unit_vals = [Decimal(pool[c]['line']) / Decimal(pool[c]['base_qty']) for c in codes]
        set_val = int((sum(unit_vals) * sets).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        cut = set_val - bprice * sets
        if cut <= 0:
            continue
        ws = [int((u * sets).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for u in unit_vals]
        for c, part in zip(codes, allocate(cut, ws)):
            L = pool[c]
            L['act'] = a
            L['promos'].append({'kind': 'activity', 'source_no': a['act_no'], 'name': a['name'], 'amount_cents': part})
            L['line'] -= part
        mark(a)

    # ---------------- 换购（其它商品满额后，换购商品按换购价）
    for a in store_ok:
        if a['type'] != 'redeem' or not group_free(a):
            continue
        code = a['rule'].get('code')
        thr = int(a['rule']['threshold_cents']) if a['rule'].get('threshold_cents') is not None else _cents_of(a['rule'].get('threshold') or 0, '换购门槛')
        rp = int(a['rule']['price_cents']) if a['rule'].get('price_cents') is not None else _cents_of(a['rule'].get('price'), '换购价')
        maxq = int(a['rule'].get('max_qty') or 1)
        tgt = [L for L in plain() if L['p']['code'] == code and L['unit_rate'] == 1]
        if not tgt:
            continue
        L = tgt[0]
        others = sum(x['line'] for x in act_lines(a, [y for y in lines if y is not L and not y['is_gift']]))
        if others < thr:
            explain.append('换购「%s」：其它商品满 %s 元可换购，还差 %s 元' % (a['name'], _yuan_s(thr), _yuan_s(thr - others)))
            continue
        k = min(L['base_qty'], maxq)
        per = Decimal(L['line']) / Decimal(L['base_qty'])
        cut = int(((per - rp) * k).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if cut <= 0:
            continue
        L['act'] = a
        L['promos'].append({'kind': 'activity', 'source_no': a['act_no'], 'name': a['name'], 'amount_cents': cut})
        L['line'] -= cut
        mark(a)

    for L in lines:
        L['base_price'] = L['unit_cents']      # 系统价（活动/会员价后、手工改价前）的单价

    # ---------------- 手工：改价 / 单品折扣 / 赠送（need_auth 交给③校验）
    for L in lines:
        it = L['in']
        if L['is_gift']:
            L['promos'].append({'kind': 'gift', 'source_no': '', 'name': '手工赠送', 'amount_cents': L['line']})
            L['line'] = 0
            L['price'] = 0
            L['price_changed'] = True          # 手工赠送 = 改价为 0，走改价额度（与收银台约定）；优惠明细记 gift
            need_auth.append({'kind': 'price_change', 'value': None, 'label': '赠送：%s' % L['p']['name']})
            continue
        if it.get('price') not in (None, ''):
            pc = _cents_of(it.get('price'), '价格')
            v = mul_rate(pc, L['qty'])
            if v != L['line'] or pc != L['unit_cents']:
                L['promos'].append({'kind': 'price_change', 'source_no': '', 'name': '改价', 'amount_cents': L['line'] - v})
                L['line'] = v
                L['price_changed'] = True
                if L['list_cents'] > 0:          # 未定价商品允许现场开价
                    need_auth.append({'kind': 'price_change', 'value': None, 'label': '改价：%s %s→%s' % (
                        L['p']['name'], _yuan_s(L['unit_cents']), _yuan_s(pc))})
            L['price'] = pc
        elif it.get('rate') not in (None, ''):
            if not L['allow_discount']:
                raise BizError('商品【%s】不参与折扣' % L['p']['name'])
            r = _rate(it.get('rate'), '单品折扣率')
            v = mul_rate(L['line'], r)
            if v != L['line']:
                L['promos'].append({'kind': 'item_discount', 'source_no': '', 'name': '单品折扣 %s' % r, 'amount_cents': L['line'] - v})
                need_auth.append({'kind': 'discount', 'value': str(r), 'label': '单品折扣 %s：%s' % (r, L['p']['name'])})
                L['line'] = v
            L['price'] = mul_rate(L['unit_cents'], r)
        else:
            L['price'] = L['unit_cents']
        L.setdefault('price_changed', False)

    subtotal = sum(L['line'] for L in lines)
    retail_total = sum(L['retail_line'] for L in lines)
    for L in lines:
        L['cur'] = L['line']
        L['shares'] = {}

    def share(kind, src_no, src_name, pool, total):
        """整单层：把 total 按当前行金额分摊到 pool，记优惠明细。"""
        if total <= 0:
            return
        parts = allocate(total, [L['cur'] for L in pool])
        for L, part in zip(pool, parts):
            if part:
                L['cur'] -= part
                key = kind if kind != 'coupon' else 'coupon:' + src_no
                L['shares'][key] = L['shares'].get(key, 0) + part
                discounts.append({'kind': kind, 'line_no': L['line_no'], 'source_no': src_no, 'source_name': src_name, 'amount_cents': part})

    disc_pool = lambda: [L for L in lines if not L['is_gift'] and L['allow_discount'] and L['cur'] > 0]  # noqa: E731
    order_disc = 0

    # ---------------- 整单类活动：满减 / 整单折扣
    for a in store_ok:
        if a['type'] not in ORDER_TYPES or not group_free(a):
            continue
        pool = act_lines(a, disc_pool())
        base = sum(L['cur'] for L in pool)
        rule = a['rule']
        cut = 0
        if a['type'] == 'full_reduce':
            tiers = []
            for t in rule.get('tiers') or []:
                tc = int(t['threshold_cents']) if t.get('threshold_cents') is not None else _cents_of(t.get('threshold'), '满减门槛')
                rc = int(t['reduce_cents']) if t.get('reduce_cents') is not None else _cents_of(t.get('reduce'), '满减金额')
                tiers.append((tc, rc))
            tiers.sort(reverse=True)
            for tc, rc in tiers:
                if base >= tc and tc > 0:
                    cut = rc * (base // tc) if rule.get('repeat') else rc
                    break
            if not cut and tiers and pool:
                tc, rc = tiers[-1]
                explain.append('「%s」满 %s 减 %s，还差 %s 元' % (a['name'], _yuan_s(tc), _yuan_s(rc), _yuan_s(tc - base)))
        else:
            thr = int(rule['threshold_cents']) if rule.get('threshold_cents') is not None else _cents_of(rule.get('threshold') or 0, '门槛')
            if pool and base >= thr:
                cut = base - mul_rate(base, _rate(rule.get('rate'), '整单折扣率'))
            elif pool:
                explain.append('「%s」满 %s 元可用，还差 %s 元' % (a['name'], _yuan_s(thr), _yuan_s(thr - base)))
        cut = min(cut, base)
        if cut > 0:
            share('activity', a['act_no'], a['name'], pool, cut)
            order_disc += cut
            for L in pool:
                L['promos'].append({'kind': 'activity', 'source_no': a['act_no'], 'name': a['name'], 'amount_cents': L['shares'].get('activity', 0)})
                if not int(a.get('stack_coupon') if a.get('stack_coupon') is not None else 1):
                    L['no_coupon'] = True
            mark(a)
    # 单品活动不与券同享
    for L in lines:
        if L['act'] is not None and not int(L['act'].get('stack_coupon') if L['act'].get('stack_coupon') is not None else 1):
            L['no_coupon'] = True
    # promos 里整单活动按行汇总（上面循环可能多次追加，这里合并同源）
    for L in lines:
        merged = []
        for pmo in L['promos']:
            if pmo['kind'] == 'activity' and merged and merged[-1]['source_no'] == pmo['source_no'] and pmo['source_no'] in applied \
                    and applied[pmo['source_no']]['type'] in ORDER_TYPES:
                continue
            merged.append(pmo)
        L['promos'] = merged
    # 修正：整单活动行分摊额以 discounts 为准
    for L in lines:
        for pmo in L['promos']:
            if pmo['kind'] == 'activity' and pmo['source_no'] in applied and applied[pmo['source_no']]['type'] in ORDER_TYPES:
                pmo['amount_cents'] = sum(d['amount_cents'] for d in discounts if d['line_no'] == L['line_no'] and d['source_no'] == pmo['source_no'])

    # ---------------- 收银整单折扣（手工）
    od = (cart or {}).get('order_discount') or {}
    if od and (od.get('rate') not in (None, '') or od.get('amount') not in (None, '')):
        pool = disc_pool()
        base = sum(L['cur'] for L in pool)
        if not pool or base <= 0:
            raise BizError('没有可打折的商品（商品设置了不参与折扣或已是赠品）')
        if od.get('rate') not in (None, ''):
            r = _rate(od.get('rate'), '整单折扣率')
            cut = base - mul_rate(base, r)
            eff = r
        else:
            cut = _cents_of(od.get('amount'), '整单优惠金额')
            if cut > base:
                raise BizError('整单优惠金额不能超过可打折金额 %s' % _yuan_s(base))
            eff = (Decimal(base - cut) / Decimal(base)).quantize(Decimal('0.0001'), rounding=ROUND_FLOOR)
        if cut > 0:
            share('order_discount', '', '整单折扣', pool, cut)
            order_disc += cut
            need_auth.append({'kind': 'discount', 'value': str(eff), 'label': '整单折扣 %s（优惠 %s）' % (eff, _yuan_s(cut))})

    # ---------------- 满赠（追加赠品行，金额 0，保留成本与活动来源）
    gifts = []
    for a in store_ok:
        if a['type'] != 'full_gift' or not group_free(a):
            continue
        rule = a['rule']
        pool = act_lines(a, [L for L in lines if not L['is_gift']])
        base = sum(L['cur'] for L in pool)
        thr = int(rule['threshold_cents']) if rule.get('threshold_cents') is not None else _cents_of(rule.get('threshold') or 0, '满赠门槛')
        if not pool:
            continue
        if base < thr or thr <= 0:
            explain.append('「%s」满 %s 元送赠品，还差 %s 元' % (a['name'], _yuan_s(thr), _yuan_s(max(thr - base, 0))))
            continue
        sku = goods_svc.find_sku(db, mid, rule.get('gift_code'))
        if sku is None or goods_svc.sale_status(db, mid, store_code, sku['product']):
            explain.append('「%s」赠品 %s 不可用（商品不存在或已停售）' % (a['name'], rule.get('gift_code')))
            continue
        gp = sku['product']
        gq = int(rule.get('gift_qty') or 1) * ((base // thr) if rule.get('repeat') else 1)
        gq = min(gq, int(rule.get('max_gift_qty') or gq))
        lc = int(gp['retail_cents'] or 0) * int(sku['rate'] or 1)
        L = {'line_no': len(lines) + 1, 'p': gp, 'unit': sku['unit'], 'unit_rate': int(sku['rate'] or 1), 'qty': Decimal(gq),
             'base_qty': gq * int(sku['rate'] or 1), 'in': {}, 'batch_id': None, 'is_gift': True, 'auto_gift': True,
             'list_cents': lc, 'list_src': 'hq', 'member_cents': None, 'unit_cents': lc, 'base_price': lc, 'price': 0, 'act': a,
             'promos': [{'kind': 'gift', 'source_no': a['act_no'], 'name': a['name'], 'amount_cents': lc * gq}], 'manual': False,
             'no_coupon': True, 'allow_discount': 0, 'allow_points': 0, 'cost_cents': int(gp['cost_cents'] or 0) * gq * int(sku['rate'] or 1),
             'remark': '满赠', 'retail_line': lc * gq, 'line': 0, 'cur': 0, 'shares': {}, 'price_changed': False}
        lines.append(L)
        retail_total += lc * gq
        gifts.append({'product_id': gp['id'], 'code': gp['code'], 'name': gp['name'], 'qty': gq, 'activity_no': a['act_no'], 'line_no': L['line_no']})
        mark(a)

    # ---------------- 优惠券
    coupons_out, coupon_snap = [], []
    cnos = [str(c).strip() for c in ((cart or {}).get('coupon_nos') or []) if str(c).strip()]
    if (cart or {}).get('coupon_no'):
        cnos.append(str(cart['coupon_no']).strip())
    if len(set(cnos)) != len(cnos):
        raise BizError('同一张优惠券不能重复使用', 409, 'CONFLICT')
    if cnos:
        if not member:
            raise BizError('使用优惠券需先选择会员')
        if not benefits:
            raise BizError(member['benefits_block'] or '会员不能使用优惠券', 409, 'CONFLICT')
        if len(cnos) > cfg['coupon_max_per_order']:
            raise BizError('每单最多使用 %d 张优惠券' % cfg['coupon_max_per_order'])
    today = now[:10]
    seen_tpl = set()
    for cno in cnos:
        c = db.execute('SELECT c.*, t.type t_type, t.discount_rate t_rate, t.scope t_scope, t.return_on_refund t_ret, '
                       't.max_discount_cents t_max FROM coupons c LEFT JOIN coupon_templates t ON t.id=c.template_id '
                       'WHERE c.merchant_id=? AND c.coupon_no=?', (mid, cno)).fetchone()
        if c is None or c['member_code'] != mrow['code']:
            raise BizError('优惠券 %s 不属于该会员' % cno)
        if c['status'] != '未使用':
            raise BizError('优惠券 %s 已使用或不可用（当前「%s」）' % (cno, c['status']), 409, 'CONFLICT')
        if c['expire_date'] and c['expire_date'] < today:
            raise BizError('优惠券 %s 已过期（%s）' % (cno, c['expire_date']))
        ctype = c['t_type'] or 'cash'
        cscope = _jl(c['t_scope'], {})
        if not _scope_ok(cscope, store_code, region, mlevel):
            raise BizError('优惠券 %s 不适用于本门店' % cno)
        if c['template_id'] and c['template_id'] in seen_tpl:
            raise BizError('同一种优惠券每单只能用一张')
        seen_tpl.add(c['template_id'])
        pool = [L for L in disc_pool() if not L['no_coupon'] and _scope_ok(cscope, store_code, region, mlevel, L['p'])]
        base = sum(L['cur'] for L in pool)
        thr, face = int(c['threshold_cents'] or 0), int(c['face_value_cents'] or 0)
        if not pool:
            raise BizError('优惠券 %s 没有适用商品（或商品活动不与券同享）' % cno)
        if ctype in ('cash', 'product') and base < thr:
            raise BizError('未达优惠券使用门槛（满 %s 可用，当前适用金额 %s）' % (_yuan_s(thr), _yuan_s(base)))
        if ctype == 'discount':
            if base < thr:
                raise BizError('未达优惠券使用门槛（满 %s 可用）' % _yuan_s(thr))
            cut = base - mul_rate(base, _rate(c['t_rate'], '折扣券折扣率'))
            if int(c['t_max'] or 0) > 0:
                cut = min(cut, int(c['t_max']))
        else:
            cut = min(face, base)
        if cut <= 0:
            continue
        share('coupon', cno, c['template_name'] or COUPON_TYPE_NAME.get(ctype, '优惠券'), pool, cut)
        coupons_out.append({'coupon_no': cno, 'amount_cents': cut})
        coupon_snap.append({'coupon_no': cno, 'template_id': c['template_id'], 'name': c['template_name'], 'type': ctype,
                            'face_value_cents': face, 'threshold_cents': thr, 'discount_rate': c['t_rate'], 'expire_date': c['expire_date'],
                            'return_on_refund': int(c['t_ret'] if c['t_ret'] is not None else 1), 'scope': cscope, 'amount_cents': cut})

    # ---------------- 积分抵扣
    points_used = 0
    points_cents = 0
    pu = (cart or {}).get('points_used')
    if pu not in (None, '', 0, '0'):
        try:
            want = int(_dec(pu, '抵扣积分'))
        except BizError:
            raise
        if want < 0:
            raise BizError('抵扣积分不能为负')
        if want:
            if not member:
                raise BizError('积分抵扣需先选择会员')
            if not benefits:
                raise BizError(member['benefits_block'] or '会员不能使用积分', 409, 'CONFLICT')
            if int(mrow['points_debt'] or 0) > 0 and cfg['points_refund_policy'] == 'limit':
                raise BizError('会员有 %d 积分未扣回（退货），暂不能用积分抵扣' % int(mrow['points_debt']), 409, 'POINTS_SHORT')
            have = int(mrow['points'] or 0)
            if want > have:
                raise BizError('积分不足（可用 %d）' % max(have, 0), 409, 'POINTS_SHORT')
            p2y = cfg['points_to_yuan']
            g = _gcd(p2y, 100)
            step_pts, step_cents = p2y // g, 100 // g
            pool = [L for L in lines if not L['is_gift'] and L['allow_points'] and L['cur'] > 0]
            base = sum(L['cur'] for L in pool)
            cap = int((Decimal(base) * _dec(cfg['points_max_ratio'], '积分抵扣比例')).quantize(Decimal(1), rounding=ROUND_FLOOR))
            nsteps = min(want // step_pts, cap // step_cents)
            points_used, points_cents = nsteps * step_pts, nsteps * step_cents
            if points_used < want:
                explain.append('积分抵扣按 %d 积分抵 1 元、最多抵 %s%%，实际使用 %d 积分' % (
                    p2y, (_dec(cfg['points_max_ratio'], '') * 100).normalize(), points_used))
            if points_cents > 0:
                share('points', '', '积分抵扣 %d' % points_used, pool, points_cents)

    # ---------------- 抹零
    mode = (cart or {}).get('round_mode') or cfg['round_mode']
    if mode not in ('none', 'jiao', 'yuan'):
        raise BizError('抹零方式只能是 none/jiao/yuan')
    after = sum(L['cur'] for L in lines)
    payable = floor_to(after, {'jiao': 10, 'yuan': 100}.get(mode, 1))
    round_off = after - payable
    if round_off > 0:
        share('round_off', '', '抹零', [L for L in lines if L['cur'] > 0], round_off)

    # ---------------- 消费积分（按商品可积分行金额 × 每元积分 × 等级倍率 × 活动倍率）
    points_earned, line_earn = 0, {}
    if member and benefits:
        ppy = _dec(cfg['points_per_yuan'], '每元积分')
        lvl_rate = _dec(cfg['level_points_rate'].get(mlevel, '1') or '1', '等级积分倍率')
        tot = Decimal(0)
        weights = []
        for L in lines:
            w = Decimal(0)
            if not L['is_gift'] and L['allow_points'] and L['cur'] > 0:
                ar = Decimal(1)
                if L['act'] is not None and L['act'].get('points_rate'):
                    ar = _dec(L['act']['points_rate'], '活动积分倍率')
                w = Decimal(L['cur']) * ar
            weights.append(w)
            tot += w
        points_earned = int((tot / 100 * ppy * lvl_rate).quantize(Decimal(1), rounding=ROUND_FLOOR))
        if points_earned > 0:
            ws = [int(w.quantize(Decimal(1), rounding=ROUND_HALF_UP)) for w in weights]
            for L, part in zip(lines, allocate(points_earned, ws, cap=False)):
                line_earn[L['line_no']] = part

    # ---------------- 汇总输出
    payable = sum(L['cur'] for L in lines)
    for L in lines:                               # 单品层优惠进 discounts（整单层已在 share 里写入）
        for pmo in L['promos']:
            if pmo['kind'] in ('member_price', 'price_change', 'item_discount', 'gift') or (
                    pmo['kind'] == 'activity' and not (pmo['source_no'] in applied and applied[pmo['source_no']]['type'] in ORDER_TYPES)):
                if pmo['amount_cents']:
                    discounts.append({'kind': pmo['kind'], 'line_no': L['line_no'], 'source_no': pmo['source_no'],
                                      'source_name': pmo['name'], 'amount_cents': pmo['amount_cents']})
    kind_order = {'member_price': 0, 'activity': 1, 'item_discount': 2, 'price_change': 3, 'gift': 4, 'order_discount': 5, 'coupon': 6,
                  'points': 7, 'round_off': 8}
    discounts.sort(key=lambda d: (kind_order.get(d['kind'], 9), d['line_no'], d['source_no']))
    pts_by_line = {}
    if points_used:
        pl = [L for L in lines if L['shares'].get('points')]
        for L, part in zip(pl, allocate(points_used, [L['shares']['points'] for L in pl], cap=False)):
            pts_by_line[L['line_no']] = part
    out_lines, snap_lines = [], []
    for L in lines:
        p = L['p']
        sh = L['line'] - L['cur']
        out_lines.append({
            'line_no': L['line_no'], 'product_id': p['id'], 'code': p['code'], 'name': p['name'], 'spec': p['spec'] or '',
            'unit': L['unit'], 'unit_rate': L['unit_rate'], 'qty': _qty_num(L['qty']), 'base_qty': L['base_qty'],
            'batch_id': L['batch_id'], 'is_gift': L['is_gift'], 'auto_gift': L['auto_gift'],
            'retail_cents': L['list_cents'], 'base_price_cents': L['base_price'], 'price_cents': L['price'],
            'line_amount_cents': L['line'], 'share_cents': sh, 'amount_cents': L['cur'], 'cost_cents': L['cost_cents'],
            'price_changed': bool(L['price_changed']), 'activity_no': L['act']['act_no'] if L['act'] is not None else '',
            'promos': [x for x in L['promos'] if x['amount_cents']], 'points_earned': line_earn.get(L['line_no'], 0),
            'line_remark': L['remark'],
        })
        snap_lines.append({'line_no': L['line_no'], 'product_id': p['id'], 'code': p['code'], 'qty': str(L['qty']), 'base_qty': L['base_qty'],
                           'unit': L['unit'], 'unit_rate': L['unit_rate'], 'list_cents': L['list_cents'], 'list_src': L['list_src'],
                           'member_cents': L['member_cents'], 'price_cents': L['price'], 'line_amount_cents': L['line'],
                           'amount_cents': L['cur'], 'shares': L['shares'], 'activity_no': L['act']['act_no'] if L['act'] is not None else '',
                           'points_earned': line_earn.get(L['line_no'], 0), 'points_used': pts_by_line.get(L['line_no'], 0),
                           'is_gift': L['is_gift'], 'allow_points': L['allow_points']})
    assert sum(x['amount_cents'] for x in out_lines) == payable
    assert retail_total - payable == sum(d['amount_cents'] for d in discounts), (retail_total, payable, discounts)
    return {
        'lines': out_lines, 'member': member,
        'subtotal_cents': subtotal, 'retail_total_cents': retail_total, 'order_discount_cents': order_disc,
        'coupons': coupons_out, 'points_used': points_used, 'points_cents': points_cents, 'round_off_cents': round_off,
        'payable_cents': payable, 'points_earned': points_earned, 'discounts': discounts, 'need_auth': need_auth,
        'explain': explain, 'gifts': gifts,
        'snapshot': {
            'engine': 'v2', 'at': now, 'store_code': store_code, 'region': region,
            'member': {'code': mrow['code'], 'level': mlevel, 'benefits': benefits} if mrow is not None else None,
            'activities': [_act_brief(a) for a in sorted(applied.values(), key=lambda a: (int(a['priority']), a['act_no']))],
            'coupons': coupon_snap, 'lines': snap_lines,
            'rules': {'points_per_yuan': cfg['points_per_yuan'], 'points_to_yuan': cfg['points_to_yuan'],
                      'points_max_ratio': cfg['points_max_ratio'], 'round_mode': mode,
                      'level_points_rate': cfg['level_points_rate'].get(mlevel, '1') if mlevel else '1',
                      'points_refund_policy': cfg['points_refund_policy']},
        },
    }


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def coupons_for_cart(db, actor, store_code, cart, now=None):
    """POS-303：会员每张可用券对当前购物车是否可用、不可用原因、预计优惠（逐张单独试算）。"""
    from .member_svc import find_member
    mid = int(actor['merchant_id'])
    m = find_member(db, mid, (cart or {}).get('member_code'))
    if m is None:
        return []
    out = []
    base_cart = dict(cart or {})
    base_cart.pop('coupon_no', None)
    for c in db.execute("SELECT coupon_no, template_name FROM coupons WHERE merchant_id=? AND member_code=? AND status='未使用' ORDER BY coupon_no",
                        (mid, m['code'])):
        t = dict(base_cart, coupon_nos=[c['coupon_no']], points_used=0)
        try:
            pr = price_cart(db, actor, store_code, t, now)
            amt = sum(x['amount_cents'] for x in pr['coupons'])
            out.append({'coupon_no': c['coupon_no'], 'name': c['template_name'], 'usable': amt > 0, 'amount_cents': amt,
                        'reason': '' if amt > 0 else '优惠为 0'})
        except BizError as e:
            out.append({'coupon_no': c['coupon_no'], 'name': c['template_name'], 'usable': False, 'amount_cents': 0, 'reason': e.msg})
    return out


# ================================================================ 券状态流水
def coupon_log(db, mid, coupon_no, frm, to, ref_no='', operator='', note=''):
    db.execute('INSERT INTO coupon_logs(merchant_id, coupon_no, from_status, to_status, ref_no, operator, note, created_at) VALUES(?,?,?,?,?,?,?,?)',
               (mid, coupon_no, frm, to, ref_no or '', operator or '', (note or '')[:200], now_s()))


def lock_coupons(db, actor, ref_no, coupon_nos):
    """挂单/待支付占用券：未使用 -> 已锁定（locked_order_no=ref_no）。同一 ref 重复锁定幂等。"""
    mid = int(actor['merchant_id'])
    for cno in coupon_nos or []:
        c = db.execute("UPDATE coupons SET status='已锁定', locked_order_no=?, locked_at=? WHERE merchant_id=? AND coupon_no=? AND status='未使用'",
                       (ref_no, now_s(), mid, cno))
        if c.rowcount == 1:
            coupon_log(db, mid, cno, '未使用', '已锁定', ref_no, actor.get('name', ''), '挂单/待支付占用')
            continue
        r = db.execute('SELECT status, locked_order_no FROM coupons WHERE merchant_id=? AND coupon_no=?', (mid, cno)).fetchone()
        if r is None or not (r['status'] == '已锁定' and r['locked_order_no'] == ref_no):
            raise BizError('优惠券 %s 已被使用或占用' % cno, 409, 'CONFLICT')


def release_coupons(db, actor, ref_no, note='取消/释放'):
    """取消挂单/未支付订单：释放 ref_no 锁定的券（已锁定 -> 未使用）。返回释放的券号。"""
    mid = int(actor['merchant_id'])
    out = []
    for r in db.execute("SELECT coupon_no FROM coupons WHERE merchant_id=? AND status='已锁定' AND locked_order_no=? ORDER BY coupon_no",
                        (mid, ref_no)).fetchall():
        db.execute("UPDATE coupons SET status='未使用', locked_order_no='', locked_at='' WHERE merchant_id=? AND coupon_no=? AND status='已锁定'",
                   (mid, r[0]))
        coupon_log(db, mid, r[0], '已锁定', '未使用', ref_no, actor.get('name', ''), note)
        out.append(r[0])
    return out


# ================================================================ 结算 / 退货 权益
def apply_benefits(db, actor, order_no, store_code, priced):
    """结算事务内调用（订单已完成）：核销券（防重复核销，第二次 409）+ 扣抵扣积分 + 发消费积分（限制策略下先抵欠扣）。"""
    from .member_svc import points_change, earn_expire_date, _member
    mid = int(actor['merchant_id'])
    m = priced.get('member')
    out = {'coupons_used': [], 'points_used': 0, 'points_earned': 0, 'debt_offset': 0}
    if not m:
        if priced.get('coupons') or priced.get('points_used'):
            raise BizError('使用券/积分必须有会员')
        return out
    code = m['code']
    ts = now_s()
    who = actor.get('name', '')
    for c in priced.get('coupons') or []:
        cur = db.execute("UPDATE coupons SET status='已核销', used_at=?, order_no=?, store_code=?, locked_order_no='' "
                         "WHERE merchant_id=? AND coupon_no=? AND member_code=? AND (status='未使用' OR (status='已锁定' AND locked_order_no=?))",
                         (ts, order_no, store_code, mid, c['coupon_no'], code, order_no))
        if cur.rowcount != 1:
            raise BizError('优惠券 %s 已被使用' % c['coupon_no'], 409, 'CONFLICT')
        coupon_log(db, mid, c['coupon_no'], '未使用', '已核销', order_no, who, '核销 %s 元' % _yuan_s(c['amount_cents']))
        out['coupons_used'].append(c['coupon_no'])
    if db.execute("SELECT 1 FROM points_log WHERE merchant_id=? AND order_no=? AND type IN ('积分抵扣','消费获得') LIMIT 1",
                  (mid, order_no)).fetchone():
        raise BizError('该订单积分已处理过', 409, 'CONFLICT')
    if priced.get('points_used'):
        points_change(db, actor, mid, code, -int(priced['points_used']), '积分抵扣', order_no, store_code,
                      note='抵扣 %s 元' % _yuan_s(priced.get('points_cents') or 0))
        out['points_used'] = int(priced['points_used'])
    earn = int(priced.get('points_earned') or 0)
    if earn:
        row = _member(db, mid, code)
        debt = int(row['points_debt'] or 0)
        off = min(debt, earn) if debt > 0 else 0
        points_change(db, actor, mid, code, earn, '消费获得', order_no, store_code, expire_date=earn_expire_date(db, mid))
        if off:
            points_change(db, actor, mid, code, -off, '欠扣抵减', order_no, store_code, note='抵减退货未扣回积分')
            db.execute('UPDATE members SET points_debt = points_debt - ? WHERE id=?', (off, row['id']))
            out['debt_offset'] = off
        out['points_earned'] = earn
    return out


def _snapshot_of(orig_order):
    s = orig_order.get('snapshot') if isinstance(orig_order, dict) else orig_order['snapshot']
    return _jl(s, {})


def reverse_benefits(db, actor, refund_no, orig_order, lines):
    """退货事务内调用。lines=[{'orig_line_id','qty','amount_cents'}]（qty=退货交易数量）。
    按原单快照（snapshot.lines 的 points_earned/points_used、snapshot.coupons、snapshot.rules）处理：
      1. 返还该行抵扣用掉的积分（按退货数量比例，最后一次退完该行=剩余全部）；
      2. 扣回该行发放的消费积分（同上）；余额不足按商户配置 negative/limit/manual（ERP-602、AC-017），差额记 points_exceptions，不静默丢失；
      3. 原单全部商品都退完时，券按模板「整单退回」规则退回（未过期 -> 未使用；已过期不退并记流水）。
    返回 {'points_back','points_restore','coupons_returned':[...],'points_shortfall','policy'}。"""
    from .member_svc import points_change, get_rules, _member
    mid = int(actor['merchant_id'])
    o = dict(orig_order) if not isinstance(orig_order, dict) else orig_order
    ono = o['order_no']
    if int(o.get('merchant_id') or mid) != mid:
        raise BizError('原单不存在', 404, 'NOT_FOUND')
    snap = _snapshot_of(o)
    out = {'points_back': 0, 'points_restore': 0, 'coupons_returned': [], 'points_shortfall': 0, 'policy': '', 'lines': []}
    if db.execute('SELECT 1 FROM benefit_reversals WHERE merchant_id=? AND refund_no=? LIMIT 1', (mid, refund_no)).fetchone():
        raise BizError('该退货单权益已处理过', 409, 'CONFLICT')
    snap_lines = dict((int(x['line_no']), x) for x in (snap.get('lines') or []))
    mcode = o.get('member_code') or ((snap.get('member') or {}).get('code') if snap.get('member') else '')
    store = o.get('store_code') or ''
    ts = now_s()
    back_total, restore_total = 0, 0
    for ln in lines or []:
        it = db.execute('SELECT * FROM order_items WHERE id=? AND order_no=?', (ln['orig_line_id'], ono)).fetchone()
        if it is None:
            raise BizError('原单行不存在：%s' % ln.get('orig_line_id'), 404, 'NOT_FOUND')
        line_no = int(it['line_no'])
        sl = snap_lines.get(line_no) or {}
        orig_qty = _dec(sl.get('qty') if sl.get('qty') is not None else it['qty'], '原单数量')
        q = _dec(ln.get('qty'), '退货数量')
        if q <= 0:
            continue
        done = db.execute('SELECT qty, points_back, points_restore FROM benefit_reversals WHERE merchant_id=? AND orig_order_no=? AND line_no=?',
                          (mid, ono, line_no)).fetchall()
        done_q = sum((_dec(r['qty']) for r in done), Decimal(0))
        done_back = sum(int(r['points_back']) for r in done)
        done_rest = sum(int(r['points_restore']) for r in done)
        if done_q + q > orig_qty:
            raise BizError('第 %d 行累计退货数量超过购买数量' % line_no, 409, 'CONFLICT')
        earned, used = int(sl.get('points_earned') or 0), int(sl.get('points_used') or 0)
        if done_q + q == orig_qty:
            back, rest = earned - done_back, used - done_rest
        else:
            back = int((Decimal(earned) * q / orig_qty).quantize(Decimal(1), rounding=ROUND_FLOOR))
            rest = int((Decimal(used) * q / orig_qty).quantize(Decimal(1), rounding=ROUND_FLOOR))
            back, rest = min(back, earned - done_back), min(rest, used - done_rest)
        db.execute('INSERT INTO benefit_reversals(merchant_id, orig_order_no, refund_no, line_no, qty, points_back, points_restore, created_at) '
                   'VALUES(?,?,?,?,?,?,?,?)', (mid, ono, refund_no, line_no, str(q), back, rest, ts))
        back_total += back
        restore_total += rest
        out['lines'].append({'line_no': line_no, 'qty': str(q), 'points_back': back, 'points_restore': rest})
    if mcode and db.execute('SELECT 1 FROM members WHERE merchant_id=? AND code=?', (mid, mcode)).fetchone():
        policy = (snap.get('rules') or {}).get('points_refund_policy') or get_rules(db, mid)['points_refund_policy']
        policy = get_rules(db, mid)['points_refund_policy'] or policy        # 以当前商户配置为准（规格：按配置）
        out['policy'] = policy
        if restore_total:
            points_change(db, actor, mid, mcode, restore_total, '退货返还抵扣积分', refund_no, store, orig_order_no=ono)
        if back_total:
            row = _member(db, mid, mcode)
            have = max(int(row['points'] or 0), 0)
            if have >= back_total or policy == 'negative':
                points_change(db, actor, mid, mcode, -back_total, '退货扣回', refund_no, store, allow_negative=True, orig_order_no=ono,
                              note='' if have >= back_total else '余额不足，形成负积分 %d' % (have - back_total))
                if have < back_total:
                    short = back_total - have
                    out['points_shortfall'] = short
                    db.execute('INSERT INTO points_exceptions(merchant_id, member_code, ref_no, orig_order_no, shortfall, policy, status, note, '
                               "created_at) VALUES(?,?,?,?,?,?,'resolved',?,?)",
                               (mid, mcode, refund_no, ono, short, policy, '已形成负积分', ts))
            else:
                short = back_total - have
                if have:
                    points_change(db, actor, mid, mcode, -have, '退货扣回', refund_no, store, orig_order_no=ono,
                                  note='应扣 %d，余额不足，差额 %d %s' % (back_total, short, '记欠扣并限制抵扣' if policy == 'limit' else '转人工处理'))
                else:
                    db.execute('INSERT INTO points_log(merchant_id, member_code, member_name, change, balance, type, order_no, operator, note, '
                               'store_code, orig_order_no, created_at) VALUES(?,?,?,0,?,?,?,?,?,?,?,?)',
                               (mid, mcode, row['name'], int(row['points'] or 0), '退货扣回', refund_no, actor.get('name', ''),
                                '应扣 %d，余额 0，差额 %d %s' % (back_total, short, '记欠扣并限制抵扣' if policy == 'limit' else '转人工处理'),
                                store, ono, ts))
                db.execute('UPDATE members SET points_debt = points_debt + ? WHERE id=?', (short, row['id']))
                db.execute('INSERT INTO points_exceptions(merchant_id, member_code, ref_no, orig_order_no, shortfall, policy, status, note, created_at) '
                           "VALUES(?,?,?,?,?,?,'open',?,?)", (mid, mcode, refund_no, ono, short, policy,
                                                             '限制抵扣：后续消费积分先抵扣' if policy == 'limit' else '待人工处理', ts))
                out['points_shortfall'] = short
                if policy == 'manual':
                    from . import notify
                    notify.notify_perm(db, mid, 'member.points.adjust', '', 'risk', '退货积分扣回不足待处理',
                                       '会员 %s 退货单 %s 应扣积分差 %d' % (mcode, refund_no, short), ref_type='points_exception',
                                       ref_no=refund_no, is_todo=True, level='important')
        out['points_back'], out['points_restore'] = back_total, restore_total
    # 券：原单所有非赠品行都退完才退券
    coupons = snap.get('coupons') or []
    if coupons:
        all_done = True
        for sl in snap.get('lines') or []:
            if sl.get('is_gift'):
                continue
            dq = sum((_dec(r['qty']) for r in db.execute('SELECT qty FROM benefit_reversals WHERE merchant_id=? AND orig_order_no=? AND line_no=?',
                                                          (mid, ono, int(sl['line_no'])))), Decimal(0))
            if dq < _dec(sl['qty']):
                all_done = False
                break
        if all_done:
            today = ts[:10]
            for c in coupons:
                cno = c['coupon_no']
                row = db.execute('SELECT status, expire_date FROM coupons WHERE merchant_id=? AND coupon_no=?', (mid, cno)).fetchone()
                if row is None or row['status'] != '已核销':
                    continue
                if not int(c.get('return_on_refund', 1)):
                    coupon_log(db, mid, cno, '已核销', '已核销', refund_no, actor.get('name', ''), '整单退货：该券规则为不退回')
                    continue
                if row['expire_date'] and row['expire_date'] < today:
                    coupon_log(db, mid, cno, '已核销', '已核销', refund_no, actor.get('name', ''), '整单退货：券已过期不退回')
                    continue
                db.execute("UPDATE coupons SET status='未使用', order_no='', used_at='', store_code='' WHERE merchant_id=? AND coupon_no=? "
                           "AND status='已核销'", (mid, cno))
                coupon_log(db, mid, cno, '已核销', '未使用', refund_no, actor.get('name', ''), '整单退货退回')
                out['coupons_returned'].append(cno)
    return out
