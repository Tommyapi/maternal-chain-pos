# -*- coding: utf-8 -*-
"""收银业务核心（模块③）：班次 / 现金收支 / 扫码 / 结算 / 退货 / 无单退货 / 换货 / 储值充值 / 订单查询。

结算流程（docs/API-CONTRACT.md §3.3、§4.1）：
    promo_engine.price_cart（服务端最终金额）→ 改价/折扣逐条 check_limit_or_approval → 支付校验
    → 同一事务：orders + order_items（单位/换算率/基本单位数量/批次分配/赠品）+ order_discounts + payments
      + inventory.stock_out('sale') + member_svc 储值扣款 + promo_engine.apply_benefits；client_id 幂等。
金额一律整数「分」，接口出参用 money.yuan()。库存只通过 inventory.*，会员积分/储值只通过 member_svc.*，促销只通过 promo_engine.*。

下面几个旧函数签名保留给其他模块用（v1 兼容）：find_product / price_order（促销基线引擎在用）/ add_points / shift_summary /
do_recharge / current_shift / guide_info / first_barcode / order_detail。
"""
import json
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from . import inventory, goods_svc, member_svc, promo_engine
from .core import (PAY_NAME, _mid, get_config, has_perm, check_limit_or_approval, audit, store_row, scope_stores, mask_phone,
                   cost_view)
from .money import to_cents, yuan, ratio_cents, MoneyError
from .numbering import next_no
from .util import BizError, now_s, r2, fnum, inum, baby_axis

EXTRA_PAY = {'exchange': '换货抵扣'}
DEFAULT_REFUND_ORDER = ['stored', 'wechat', 'alipay', 'bankcard', 'other', 'cash']
QUALITY_NAME = {'ok': '合格', 'defect': '残次', 'pending': '待处理'}
DISCOUNT_KINDS = ('member_price', 'price_change', 'item_discount', 'activity', 'order_discount', 'coupon', 'points', 'round_off', 'gift')
BIG_DISCOUNT_CENTS = 10000          # 单笔改价/折扣让利 ≥ 100 元：发高风险提醒（APP-304）
BIG_DISCOUNT_RATE = Decimal('0.7')  # 整单折扣低于 7 折：发高风险提醒
REFUND_ALERT_COUNT = 5              # 同一收银员当天第 5 笔退货起：发频繁退款提醒


# ================================================================ 小工具
def cents_of(v, what='金额'):
    try:
        return to_cents(v)
    except (MoneyError, ValueError, TypeError):
        raise BizError('%s格式不正确：%s' % (what, v))


def dec_qty(v, what='数量'):
    try:
        q = Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise BizError('%s格式不正确：%s' % (what, v))
    if not q.is_finite():
        raise BizError('%s格式不正确：%s' % (what, v))
    return q


def qty_out(q):
    """Decimal/float 数量 -> JSON 数字（整数就输出整数）。"""
    d = Decimal(str(q or 0))
    return int(d) if d == d.to_integral_value() else float(d)


def pay_name(method, custom=''):
    if method == 'other' and custom:
        return str(custom).strip()[:20]
    return PAY_NAME.get(method) or EXTRA_PAY.get(method, method)


def _col(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _cents_col(row, col):
    """优先读 *_cents，旧数据没有分列时按元换算。"""
    v = _col(row, col + '_cents')
    if v is not None:
        return int(v)
    return to_cents(_col(row, col, 0) or 0)


def _json_load(s, default):
    try:
        return json.loads(s) if s else default
    except (ValueError, TypeError):
        return default


def pos_store(db, actor, requested=None, sticky=None):
    """收银门店：请求指定 > 会话记住的 > 账号主门店 > 范围内第一个。越权 -> 403 SCOPE_DENIED。"""
    stores = scope_stores(db, actor)
    if not stores:
        raise BizError('账号没有可收银的门店，请联系总部分配门店', 403, 'SCOPE_DENIED')
    req = (requested or '').strip()
    if req:
        if req not in stores:
            raise BizError('无权在门店 %s 收银' % req, 403, 'SCOPE_DENIED')
        return req
    for c in (sticky, actor.get('store_code')):
        if c and c in stores:
            return c
    return stores[0]


# ================================================================ 商品（v1 兼容 + 多商户）
def find_product(db, code, mid=None):
    """按商品编码/条码（含箱码）查商品（本商户）；找不到返回 None。返回 dict（含 products 全部列）。"""
    sku = goods_svc.find_sku(db, _mid(mid), code)
    return sku['product'] if sku else None


def first_barcode(db, pid):
    r = db.execute('SELECT barcode FROM product_barcodes WHERE product_id=? AND unit_id IS NULL ORDER BY barcode LIMIT 1', (pid,)).fetchone()
    if r is None:
        r = db.execute('SELECT barcode FROM product_barcodes WHERE product_id=? ORDER BY barcode LIMIT 1', (pid,)).fetchone()
    return r[0] if r else ''


def active_promo(db, product_code, store_code, mid=None):
    today = datetime.now().strftime('%Y-%m-%d')
    return db.execute("SELECT * FROM promotions WHERE merchant_id=? AND product_code=? AND status='启用' AND (start_date='' OR start_date IS NULL "
                      "OR start_date<=?) AND (end_date='' OR end_date IS NULL OR end_date>=?) AND (store_code='' OR store_code=?) "
                      "ORDER BY promo_price LIMIT 1", (_mid(mid), product_code, today, today, store_code)).fetchone()


def resolve_unit(db, mid, code, unit=None):
    """条码/编码 + 可选交易单位 -> {'product', 'unit', 'rate', 'barcode', 'unit_row'}。箱码自动带出箱单位和换算率（POS-104）。"""
    sku = goods_svc.find_sku(db, mid, code)
    if sku is None:
        raise BizError('未找到商品：%s（请核对条码，或按 F2 搜索）' % code, 404)
    p = sku['product']
    u, rate = sku['unit'], int(sku['rate'] or 1)
    want = (unit or '').strip()
    base_unit = p.get('base_unit') or p.get('unit') or ''
    if want and want != u:
        if want == base_unit:
            u, rate = base_unit, 1
        else:
            r = db.execute("SELECT unit, rate FROM product_units WHERE product_id=? AND unit=? AND status='启用'", (p['id'], want)).fetchone()
            if r is None:
                raise BizError('商品【%s】没有单位「%s」' % (p['name'], want))
            u, rate = r['unit'], int(r['rate'])
    urow = None
    if rate > 1:
        urow = db.execute('SELECT * FROM product_units WHERE product_id=? AND unit=?', (p['id'], u)).fetchone()
    return {'product': p, 'unit': u or base_unit, 'rate': rate, 'barcode': sku['barcode'], 'unit_row': dict(urow) if urow else None}


def unit_prices(res):
    """交易单位的零售价/会员价（分）：单位行有单独定价用单位价，否则 基本单位价 × 换算率。"""
    p, rate, ur = res['product'], res['rate'], res['unit_row']
    retail = int(p.get('retail_cents') if p.get('retail_cents') is not None else to_cents(p.get('retail') or 0))
    member = int(p.get('member_price_cents') if p.get('member_price_cents') is not None else to_cents(p.get('member_price') or 0))
    if rate > 1:
        retail = int(ur['retail_cents']) if ur and ur.get('retail_cents') is not None else retail * rate
        member = int(ur['member_price_cents']) if ur and ur.get('member_price_cents') is not None else member * rate
    return retail, member


def member_card(db, actor, code, mid=None):
    """收银台会员卡片（本商户）：手机号按 member.phone.view 脱敏，储值按 member.stored.view 返回。"""
    mid = _mid(mid if mid is not None else (actor or {}).get('merchant_id'))
    m = db.execute('SELECT * FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
    if m is None:
        return None
    d = dict(m)
    bal = _cents_col(m, 'balance')
    d['stored_balance_cents'] = bal
    d['stored_balance'] = yuan(bal) if (actor is None or has_perm(actor, 'member.stored.view')) else None
    d['points'] = int(m['points'] or 0)
    for k in ('balance', 'balance_cents', 'total_charged', 'total_charged_cents'):
        d.pop(k, None)
    if actor is not None:
        d['phone'] = mask_phone(m['phone'], actor)
    babies = []
    for b in db.execute('SELECT * FROM babies WHERE member_id=? ORDER BY id', (m['id'],)):
        if _col(b, 'hidden', 0):
            continue
        axis, months = baby_axis(b['birthday'])
        babies.append({'id': b['id'], 'baby_name': b['name'], 'baby_gender': b['gender'], 'birthday': b['birthday'],
                       'axis': axis, 'month_age': months, 'note': b['note']})
    d['babies'] = babies
    d['baby'] = babies[0] if babies else None
    today = datetime.now().strftime('%Y-%m-%d')
    d['coupons'] = [dict(x) for x in db.execute(
        "SELECT coupon_no, template_name, face_value, threshold, expire_date FROM coupons WHERE merchant_id=? AND member_code=? "
        "AND status='未使用' AND (expire_date='' OR expire_date IS NULL OR expire_date >= ?) ORDER BY face_value DESC", (mid, code, today))]
    agg = db.execute("SELECT COUNT(*), COALESCE(SUM(payable_cents),0), MAX(created_at) FROM orders WHERE merchant_id=? AND member_code=? "
                     "AND biz_type='sale'", (mid, code)).fetchone()
    ref = db.execute("SELECT COALESCE(SUM(payable_cents),0) FROM orders WHERE merchant_id=? AND member_code=? AND biz_type='refund'",
                     (mid, code)).fetchone()[0]
    d['orders'] = agg[0] or 0
    d['amount'] = yuan(int(agg[1] or 0) + int(ref or 0))
    d['last_time'] = agg[2] or ''
    d['usable'] = not (int(_col(m, 'blacklisted', 0) or 0) or (m['status'] or '正常') != '正常')
    if not d['usable']:
        d['warn'] = '会员已停用或在黑名单，不能使用积分/储值/券'
    return d


# ================================================================ 计价（促销基线引擎内部在用；模块②完整引擎上线后只剩兼容用途）
def price_order(db, data, store_code, actor):
    """v1 计价（元，float）——promo_engine 基线 price_cart 在调用。已按商户隔离、支持箱码/多单位单价。
    need_auth 只列出「发生了改价/整单折扣」，是否需要授权由 checkout 按角色额度判断。"""
    mid = int(actor['merchant_id'])
    cfg = get_config(db, mid)
    items_in = data.get('items') or []
    if not items_in or not isinstance(items_in, list):
        raise BizError('购物车为空')
    member = None
    mcode = (data.get('member_code') or '').strip()
    if mcode:
        member = member_card(db, None, mcode, mid)
        if member is None:
            raise BizError('会员不存在：%s' % mcode)
    need_auth = []
    lines = []
    for i, it in enumerate(items_in):
        res = resolve_unit(db, mid, it.get('code'), it.get('unit'))
        p = res['product']
        if p['status'] == '停售':
            raise BizError('商品已停售：%s' % p['name'])
        qty = fnum(it.get('qty'), 1)
        if qty <= 0 or qty > 99999:
            raise BizError('数量必须大于 0：%s' % p['name'])
        rc, mc = unit_prices(res)
        retail = rc / 100.0
        base = mc / 100.0 if (member and 0 < mc <= rc) else retail
        promo = active_promo(db, p['code'], store_code, mid) if res['rate'] == 1 else None
        promo_on = False
        if promo is not None and 0 <= fnum(promo['promo_price']) < base:
            base = fnum(promo['promo_price'])
            promo_on = True
        price = base
        manual = it.get('price') not in (None, '')
        if manual:
            pr_in = r2(fnum(it.get('price')))
            if pr_in < 0:
                raise BizError('价格不能为负')
            if abs(pr_in - base) > 0.001:
                price = pr_in
                if retail > 0:                         # 未定价商品允许现场开价
                    need_auth.append('改价：%s' % p['name'])
        elif retail <= 0:
            raise BizError('商品【%s】还没有设置售价，请改价后再卖，或到后台定价' % p['name'])
        if it.get('is_gift') and price > 0:       # 收银台手工标赠品 = 改价为 0，要走改价额度/授权（活动赠品由促销引擎给出）
            price = 0.0
            manual = True
            need_auth.append('赠送：%s' % p['name'])
        line_amount = r2(price * qty)
        cost = (int(p.get('cost_cents') or 0) * res['rate']) / 100.0
        lines.append({'line_no': i + 1, 'p': p, 'code': p['code'], 'qty': qty, 'retail': retail, 'price': price, 'unit': res['unit'],
                      'rate': res['rate'], 'base_price': base, 'line_amount': line_amount, 'cost': r2(cost * qty),
                      'price_changed': manual and abs(price - base) > 0.001, 'promo': promo_on})
    subtotal = r2(sum(x['line_amount'] for x in lines))
    retail_total = r2(sum(x['retail'] * x['qty'] for x in lines))

    od = data.get('order_discount') or {}
    order_discount = 0.0
    if od:
        if od.get('rate') not in (None, ''):
            rate = fnum(od.get('rate'))
            if not (0 < rate <= 1):
                raise BizError('整单折扣率需在 0~1 之间')
            order_discount = r2(subtotal * (1 - rate))
            if rate < 1:
                need_auth.append('整单折扣 %.2f' % rate)
        elif od.get('amount') not in (None, ''):
            order_discount = r2(fnum(od.get('amount')))
            if order_discount < 0 or order_discount > subtotal:
                raise BizError('整单优惠金额不合法')
            if order_discount > 0:
                need_auth.append('整单优惠 %.2f' % order_discount)
    after = r2(subtotal - order_discount)

    coupon_amount = 0.0
    coupon = None
    cno = (data.get('coupon_no') or '').strip()
    if cno:
        if not member:
            raise BizError('使用优惠券需先选择会员')
        c = db.execute('SELECT * FROM coupons WHERE coupon_no=? AND merchant_id=?', (cno, mid)).fetchone()
        if c is None or c['member_code'] != member['code']:
            raise BizError('优惠券不属于该会员')
        if c['status'] != '未使用':
            raise BizError('优惠券已使用或已失效')
        if c['expire_date'] and c['expire_date'] < datetime.now().strftime('%Y-%m-%d'):
            raise BizError('优惠券已过期')
        if after < fnum(c['threshold']):
            raise BizError('未达优惠券使用门槛（满 %.0f 可用）' % fnum(c['threshold']))
        coupon_amount = r2(min(fnum(c['face_value']), after))
        coupon = dict(c)
    after = r2(after - coupon_amount)

    points_used = inum(data.get('points_used'), 0)
    points_amount = 0.0
    if points_used:
        if not member:
            raise BizError('积分抵扣需先选择会员')
        if points_used < 0 or points_used > member['points']:
            raise BizError('积分不足（可用 %d）' % member['points'])
        p2y = max(1, inum(cfg.get('points_to_yuan'), 100))
        points_used = points_used - points_used % p2y
        points_amount = r2(points_used / float(p2y))
        cap = r2(after * fnum(cfg.get('points_max_ratio'), 0.5))
        if points_amount > cap:
            points_amount = float(int(cap))
            points_used = int(points_amount * p2y)
    after = r2(after - points_amount)

    mode = data.get('round_mode') or cfg.get('round_mode') or 'none'
    payable = after
    if mode == 'jiao':
        payable = int(after * 10 + 1e-6) / 10.0
    elif mode == 'yuan':
        payable = float(int(after + 1e-6))
    round_off = r2(after - payable)
    payable = r2(payable)

    share_total = r2(order_discount + coupon_amount + points_amount + round_off)
    acc = 0.0
    for idx, x in enumerate(lines):
        if idx == len(lines) - 1:
            sh = r2(share_total - acc)
        else:
            sh = r2(share_total * x['line_amount'] / subtotal) if subtotal > 0 else 0.0
            acc = r2(acc + sh)
        x['share_amount'] = sh
        x['amount'] = r2(x['line_amount'] - sh)

    earn = 0
    if member and member.get('usable', True):
        earn = int(payable * fnum(cfg.get('points_per_yuan'), 1) + 1e-6)
    return {
        'lines': lines, 'member': member, 'subtotal': subtotal, 'retail_total': retail_total,
        'order_discount': order_discount, 'coupon': coupon, 'coupon_amount': coupon_amount,
        'points_used': points_used, 'points_amount': points_amount, 'round_off': round_off,
        'payable': payable, 'points_earned': earn, 'need_auth': need_auth,
        'discount_total': r2(retail_total - payable),
    }


# ================================================================ 会员记账（v1 兼容入口，内部转 member_svc）
def add_points(db, m, change, mtype, order_no, operator, note=''):
    if not change:
        return
    member_svc.points_change(db, {'name': operator}, int(m.get('merchant_id') or _mid()), m['code'], int(change), mtype, order_no or '',
                             note=note or '', allow_negative=True)


def guide_info(db, code, mid=None):
    code = (code or '').strip()
    if not code:
        return {'code': '', 'name': ''}
    g = db.execute('SELECT code, name FROM guides WHERE merchant_id=? AND code=?', (_mid(mid), code)).fetchone()
    return {'code': g['code'], 'name': g['name']} if g else {'code': code, 'name': code}


# ================================================================ 班次
def current_shift(db, store_code, username, mid=None):
    return db.execute("SELECT * FROM shifts WHERE merchant_id=? AND cashier_user=? AND store_code=? AND status='open' ORDER BY id DESC LIMIT 1",
                      (_mid(mid), username, store_code)).fetchone()


def require_shift(db, store_code, actor):
    sh = current_shift(db, store_code, actor['username'], actor['merchant_id'])
    if sh is None:
        raise BizError('请先开班再收银（按 F8 开班）')
    return sh


def shift_summary(db, shift):
    """班次汇总（POS-004 / AC-014）：销售、退款、实收、各支付方式、充值、现金收支、应有现金。金额为元。"""
    sid = shift['id']
    pays = db.execute("SELECT method, MAX(method_name) name, SUM(COALESCE(amount_cents, CAST(ROUND(amount*100) AS INTEGER))) a, COUNT(*) n "
                      "FROM payments WHERE shift_id=? AND COALESCE(status,'success')='success' GROUP BY method ORDER BY method", (sid,)).fetchall()
    sales = db.execute("SELECT COUNT(*), COALESCE(SUM(payable_cents),0) FROM orders WHERE shift_id=? AND biz_type='sale'", (sid,)).fetchone()
    refunds = db.execute("SELECT COUNT(*), COALESCE(SUM(payable_cents),0) FROM orders WHERE shift_id=? AND biz_type='refund'", (sid,)).fetchone()
    exch = db.execute("SELECT COUNT(*) FROM orders WHERE shift_id=? AND biz_type='exchange'", (sid,)).fetchone()[0]
    rech = db.execute("SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM payments WHERE shift_id=? AND kind='recharge'", (sid,)).fetchone()
    cm = dict((r[0], int(r[1] or 0)) for r in db.execute('SELECT direction, SUM(amount_cents) FROM cash_moves WHERE shift_id=? GROUP BY direction', (sid,)))
    by = [{'method': p['method'], 'name': pay_name(p['method']) if p['method'] != 'other' else (p['name'] or '其他'),
           'amount': yuan(int(p['a'] or 0)), 'amount_cents': int(p['a'] or 0), 'count': p['n']} for p in pays]
    cash_pay = sum(x['amount_cents'] for x in by if x['method'] == 'cash')
    opening = _cents_col(shift, 'opening_cash')
    cin, cout = cm.get('in', 0), cm.get('out', 0)
    expected = opening + cash_pay + cin - cout
    net = sum(x['amount_cents'] for x in by if x['method'] != 'exchange')
    return {
        'sale_count': sales[0], 'sale_amount': yuan(int(sales[1] or 0)), 'refund_count': refunds[0], 'refund_amount': yuan(int(refunds[1] or 0)),
        'exchange_count': exch, 'recharge_count': rech[0], 'recharge_amount': yuan(int(rech[1] or 0)),
        'by_method': by, 'opening_cash': yuan(opening), 'cash_pay': yuan(cash_pay), 'cash_in': yuan(cash_pay),
        'cash_move_in': yuan(cin), 'cash_move_out': yuan(cout),
        'expected_cash': yuan(expected), 'expected_cash_cents': expected, 'net': yuan(net),
        'formula': '应有现金 = 备用金 + 现金收款净额（含退款、充值） + 现金收入 − 现金支出',
    }


def shift_view(db, sh):
    d = dict(sh)
    d['summary'] = _json_load(d.get('summary'), None) or (shift_summary(db, sh) if sh['status'] == 'open' else None)
    return d


def open_shift(db, actor, store_code, data):
    mid = actor['merchant_id']
    term = (data.get('terminal_no') or '').strip()[:20]
    opening = cents_of(data.get('opening_cash') or 0, '备用金')
    if opening < 0:
        raise BizError('备用金不能为负')
    sh = current_shift(db, store_code, actor['username'], mid)
    if sh is not None:
        if term and sh['terminal_no'] and sh['terminal_no'] != term:
            raise BizError('你已在收银台 %s 开班（%s），请先在那台交班' % (sh['terminal_no'], sh['shift_no'] or sh['id']), 409, 'CONFLICT')
        return sh
    st = store_row(db, store_code, mid) or {}
    if term:
        t = db.execute('SELECT * FROM terminals WHERE merchant_id=? AND terminal_no=?', (mid, term)).fetchone()
        if t is None:
            db.execute('INSERT INTO terminals(merchant_id, store_code, terminal_no, name, created_at) VALUES(?,?,?,?,?)',
                       (mid, store_code, term, '收银台 ' + term, now_s()))
            audit(db, actor, 'pos', '登记收银台', term, None, {'store_code': store_code}, '首次开班自动登记', store_code=store_code, terminal_no=term)
        elif t['store_code'] != store_code:
            raise BizError('收银台 %s 属于门店 %s，不能在本店开班' % (term, t['store_code']), 409, 'CONFLICT')
        elif t['status'] != '启用':
            raise BizError('收银台 %s 已停用' % term, 409, 'CONFLICT')
        o = db.execute("SELECT * FROM shifts WHERE merchant_id=? AND terminal_no=? AND status='open'", (mid, term)).fetchone()
        if o is not None:
            raise BizError('收银台 %s 还有未交班的班次（%s，%s 开班），请原收银员交班或店长强制交班' % (term, o['cashier_name'] or o['cashier_user'],
                                                                                           o['open_time']), 409, 'CONFLICT', {'shift_id': o['id']})
    no = next_no(db, mid, 'SH', store_code)
    try:
        db.execute('INSERT INTO shifts(merchant_id, shift_no, store_code, store_name, terminal_no, cashier_user, cashier_name, open_time, '
                   'opening_cash_cents, status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (mid, no, store_code, st.get('name', ''), term, actor['username'], actor['name'], now_s(), opening, 'open'))
    except sqlite3.IntegrityError:
        raise BizError('收银台 %s 已被其他班次占用，请刷新后重试' % term, 409, 'CONFLICT')
    audit(db, actor, 'pos', '开班', no, None, {'opening_cash': yuan(opening)}, store_code=store_code, terminal_no=term)
    return current_shift(db, store_code, actor['username'], mid)


def close_shift(db, actor, store_code, data):
    mid = actor['merchant_id']
    cid = (data.get('client_id') or '').strip()[:64] or None
    if cid:
        ex = db.execute('SELECT * FROM shifts WHERE merchant_id=? AND close_client_id=? AND cashier_user=?', (mid, cid, actor['username'])).fetchone()
        if ex is not None:
            return ex, True
    sh = current_shift(db, store_code, actor['username'], mid)
    if sh is None:
        raise BizError('当前没有进行中的班次')
    counted = cents_of(data.get('counted_cash') or 0, '实点现金')
    if counted < 0:
        raise BizError('实点现金不能为负')
    summ = shift_summary(db, sh)
    diff = counted - summ['expected_cash_cents']
    summ['counted_cash'] = yuan(counted)
    summ['diff'] = yuan(diff)
    db.execute("UPDATE shifts SET close_time=?, counted_cash_cents=?, expected_cash_cents=?, diff_cents=?, summary=?, status='closed', "
               "close_client_id=? WHERE id=? AND status='open'",
               (now_s(), counted, summ['expected_cash_cents'], diff, json.dumps(summ, ensure_ascii=False), cid, sh['id']))
    audit(db, actor, 'pos', '交班', sh['shift_no'] or str(sh['id']), {'status': 'open'},
          {'status': 'closed', 'expected': summ['expected_cash'], 'counted': yuan(counted), 'diff': yuan(diff)},
          store_code=store_code, terminal_no=sh['terminal_no'] or '')
    return db.execute('SELECT * FROM shifts WHERE id=?', (sh['id'],)).fetchone(), False


def shift_in_scope(db, actor, shift_id):
    sh = db.execute('SELECT * FROM shifts WHERE id=? AND merchant_id=?', (inum(shift_id), actor['merchant_id'])).fetchone()
    if sh is None:
        raise BizError('班次不存在', 404)
    if sh['store_code'] not in scope_stores(db, actor):
        raise BizError('无权处理其他门店的班次', 403, 'SCOPE_DENIED')
    return sh


def force_close_shift(db, actor, data):
    """店长强制交班（POS-005）：原因必填，审计 high，通知原收银员。"""
    from . import notify
    sh = shift_in_scope(db, actor, data.get('shift_id'))
    if sh['status'] != 'open':
        raise BizError('班次已交班，无需强制交班', 409, 'CONFLICT')
    reason = (data.get('reason') or '').strip()[:200]
    if not reason:
        raise BizError('强制交班必须填写原因')
    summ = shift_summary(db, sh)
    counted = None
    diff = None
    if data.get('counted_cash') not in (None, ''):
        counted = cents_of(data.get('counted_cash'), '实点现金')
        diff = counted - summ['expected_cash_cents']
        summ['counted_cash'] = yuan(counted)
        summ['diff'] = yuan(diff)
    summ['forced'] = True
    db.execute("UPDATE shifts SET close_time=?, counted_cash_cents=?, expected_cash_cents=?, diff_cents=?, summary=?, status='closed', forced=1, "
               "force_by=?, force_reason=? WHERE id=? AND status='open'",
               (now_s(), counted, summ['expected_cash_cents'], diff, json.dumps(summ, ensure_ascii=False), actor['username'], reason, sh['id']))
    audit(db, actor, 'pos', '强制交班', sh['shift_no'] or str(sh['id']), {'status': 'open', 'cashier': sh['cashier_user']},
          {'status': 'closed', 'forced': 1, 'counted': yuan(counted) if counted is not None else None}, reason, level='high',
          store_code=sh['store_code'], terminal_no=sh['terminal_no'] or '')
    u = db.execute('SELECT id FROM users WHERE merchant_id=? AND username=?', (actor['merchant_id'], sh['cashier_user'])).fetchone()
    if u is not None:
        notify.notify_users(db, actor['merchant_id'], [u[0]], 'risk', '你的班次被店长强制交班', reason, ref_type='shift',
                            ref_no=sh['shift_no'] or str(sh['id']), store_code=sh['store_code'])
    return db.execute('SELECT * FROM shifts WHERE id=?', (sh['id'],)).fetchone()


def log_print(db, actor, doc_type, doc_no, reprint, terminal_no='', result='sent'):
    db.execute('INSERT INTO print_logs(merchant_id, doc_type, doc_no, reprint, operator, terminal_no, result, created_at) VALUES(?,?,?,?,?,?,?,?)',
               (actor['merchant_id'], doc_type, doc_no, 1 if reprint else 0, actor['username'], (terminal_no or '')[:20], (result or 'sent')[:20], now_s()))


# ================================================================ 现金收支（POS-003）
def cash_move(db, actor, store_code, data):
    mid = actor['merchant_id']
    cid = (data.get('client_id') or '').strip()[:64] or None
    if cid:
        ex = db.execute('SELECT * FROM cash_moves WHERE client_id=?', (cid,)).fetchone()
        if ex is not None:
            if ex['merchant_id'] != mid or ex['store_code'] != store_code or ex['operator'] != actor['username']:
                raise BizError('请求号冲突，请重试', 409, 'CONFLICT')
            return dict(ex), True
    sh = require_shift(db, store_code, actor)
    direction = (data.get('direction') or '').strip()
    if direction not in ('in', 'out'):
        raise BizError('请选择现金收入或支出')
    amt = cents_of(data.get('amount'), '金额')
    if amt <= 0 or amt > 10000000:
        raise BizError('金额必须在 0.01 到 100000 之间')
    reason = (data.get('reason') or '').strip()[:100]
    if not reason:
        raise BizError('请填写现金%s原因' % ('收入' if direction == 'in' else '支出'))
    auth = {'mode': 'self', 'by': actor['username']}
    if direction == 'out':
        summ = shift_summary(db, sh)
        if amt > summ['expected_cash_cents']:
            raise BizError('钱箱应有现金只有 %s 元，不能支出 %s 元' % (summ['expected_cash'], yuan(amt)))
        auth = check_limit_or_approval(db, actor, 'cash_out', amt, data, store_code, '', '现金支出 %s 元：%s' % (yuan(amt), reason))
    no = next_no(db, mid, 'CS', store_code)
    db.execute('INSERT INTO cash_moves(cm_no, merchant_id, store_code, shift_id, direction, amount_cents, reason, approval_no, operator, client_id, '
               'created_at, auth_user, terminal_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (no, mid, store_code, sh['id'], direction, amt, reason, auth.get('approval_no', ''), actor['username'], cid, now_s(),
                auth['by'] if auth['mode'] != 'self' else '', sh['terminal_no'] or ''))
    audit(db, actor, 'pos', '现金' + ('收入' if direction == 'in' else '支出'), no, None,
          {'amount': yuan(amt), 'auth': auth['mode'], 'by': auth['by']}, reason, level='high' if direction == 'out' else 'info',
          store_code=store_code, terminal_no=sh['terminal_no'] or '')
    return dict(db.execute('SELECT * FROM cash_moves WHERE cm_no=?', (no,)).fetchone()), False


# ================================================================ 扫码 / 搜索（POS-101/102/104/201~203）
def _near_days(db, mid, p, cfg):
    d = int(p.get('near_expiry_days') or 0)
    if not d and p.get('category_id'):
        r = db.execute('SELECT near_expiry_days FROM categories WHERE id=? AND merchant_id=?', (p['category_id'], mid)).fetchone()
        d = int(r[0] or 0) if r else 0
    return d or int(cfg.get('near_expiry_days') or 30)


def batch_view(db, mid, wh, p, cfg):
    """可售批次（FEFO）+ 不可售批次及原因 + 临期提示。"""
    today = datetime.now().date()
    near = _near_days(db, mid, p, cfg)
    ok_list = []
    for b in inventory.sellable_batches(db, mid, wh, p['id']):
        left = None
        if b.get('expiry_date'):
            try:
                left = (datetime.strptime(b['expiry_date'], '%Y-%m-%d').date() - today).days
            except ValueError:
                left = None
        ok_list.append({'id': b['id'], 'batch_no': b['batch_no'], 'expiry_date': b['expiry_date'], 'qty': int(b['qty']),
                        'days_left': left, 'near': left is not None and left <= near})
    bad = []
    for r in db.execute("SELECT b.*, s.qty FROM inv_stock s JOIN inv_batches b ON b.id=s.batch_id WHERE s.merchant_id=? AND s.wh_code=? "
                        "AND s.product_id=? AND s.quality='ok' AND s.qty>0 AND s.batch_id>0 ORDER BY b.expiry_date, b.id", (mid, wh, p['id'])):
        why = inventory.check_batch_sellable(dict(r))
        if why:
            bad.append({'id': r['id'], 'batch_no': r['batch_no'], 'expiry_date': r['expiry_date'], 'qty': int(r['qty']), 'reason': why})
    return ok_list, bad


def scan_info(db, actor, store_code, code):
    mid = actor['merchant_id']
    code = (code or '').strip()
    if not code:
        raise BizError('请输入或扫描条码')
    res = resolve_unit(db, mid, code)
    p = res['product']
    why = goods_svc.sale_status(db, mid, store_code, p)
    if why:
        raise BizError('%s：%s' % (p['name'], why), 404)
    return goods_card(db, actor, store_code, res)


def goods_card(db, actor, store_code, res):
    mid = actor['merchant_id']
    cfg = get_config(db, mid)
    p = res['product']
    rc, mc = unit_prices(res)
    avail = inventory.available(db, mid, store_code, p['id'])
    d = dict(p)
    for k in ('cost', 'cost_cents'):
        d.pop(k, None)
    d['cost'] = cost_view(actor, int(p.get('cost_cents') or 0) * res['rate'])
    d['barcodes'] = [x[0] for x in db.execute('SELECT barcode FROM product_barcodes WHERE product_id=? ORDER BY barcode', (p['id'],))]
    d['barcode'] = res['barcode'] or (d['barcodes'][0] if d['barcodes'] else '')
    d['unit'] = res['unit']
    d['rate'] = res['rate']
    d['base_unit'] = p.get('base_unit') or p.get('unit') or ''
    d['retail'] = yuan(rc)
    d['member_price'] = yuan(mc)
    promo = active_promo(db, p['code'], store_code, mid) if res['rate'] == 1 else None
    d['promo_price'] = promo['promo_price'] if promo else None
    d['stock'] = avail // res['rate'] if res['rate'] > 1 else avail
    d['stock_base'] = avail
    d['units'] = [{'unit': d['base_unit'], 'rate': 1}] + [
        {'unit': u['unit'], 'rate': int(u['rate'])} for u in db.execute(
            "SELECT unit, rate FROM product_units WHERE product_id=? AND for_sale=1 AND status='启用' AND rate>1 ORDER BY rate", (p['id'],))]
    warn = []
    d['batches'], d['unsellable_batches'] = [], []
    d['sellable'], d['reason'] = True, ''
    if p.get('batch_managed'):
        ok_list, bad = batch_view(db, mid, store_code, p, cfg)
        d['batches'], d['unsellable_batches'] = ok_list, bad
        d['recommend_batch_id'] = ok_list[0]['id'] if ok_list else None
        if ok_list and ok_list[0]['near']:
            warn.append('临期：批次 %s 有效期至 %s（剩 %s 天）' % (ok_list[0]['batch_no'], ok_list[0]['expiry_date'], ok_list[0]['days_left']))
        if not ok_list and bad:
            d['sellable'] = False
            d['reason'] = '；'.join(b['reason'] for b in bad[:3]) + '，没有可售批次，不能销售'
    if d['sellable'] and avail < res['rate']:
        warn.append('可售库存不足（现有 %d%s）' % (avail, d['base_unit']))
    d['warn'] = '；'.join(warn)
    return d


def search_goods(db, actor, store_code, kw, cat=''):
    mid = actor['merchant_id']
    rows = None
    fn = getattr(goods_svc, 'search', None)
    if fn is not None:
        rows = fn(db, mid, store_code, kw, cat or None)
    else:
        clauses, args = ["p.merchant_id=?", "p.status != '停售'"], [mid]
        if kw:
            like = '%%%s%%' % kw
            clauses.append('(p.name LIKE ? OR p.code LIKE ? OR p.brand LIKE ? OR p.short_name LIKE ? OR p.pinyin LIKE ? OR EXISTS '
                           '(SELECT 1 FROM product_barcodes b WHERE b.product_id=p.id AND b.barcode LIKE ?))')
            args += [like] * 6
        if cat:
            clauses.append('p.category = ?')
            args.append(cat)
        rows = [dict(r) for r in db.execute('SELECT p.* FROM products p WHERE %s ORDER BY p.category, p.brand, p.name LIMIT 60'
                                            % ' AND '.join(clauses), args)]
    out = []
    for p in rows:
        res = {'product': p, 'unit': p.get('base_unit') or p.get('unit') or '', 'rate': 1, 'barcode': '', 'unit_row': None}
        d = goods_card(db, actor, store_code, res)
        why = goods_svc.sale_status(db, mid, store_code, p)
        if why:
            d['sellable'], d['reason'] = False, why
        d['brand_name'] = d.get('brand', '')
        out.append(d)
    cats = [r[0] for r in db.execute("SELECT DISTINCT category FROM products WHERE merchant_id=? AND category != '' ORDER BY 1", (mid,))]
    return out, cats


# ================================================================ 计价 / 结算
def normalize_cart(data):
    items = data.get('items')
    if not isinstance(items, list) or not items:
        raise BizError('购物车为空，请先扫码加商品')
    if len(items) > 200:
        raise BizError('一单最多 200 行商品')
    out = []
    for it in items:
        if not isinstance(it, dict):
            raise BizError('商品行格式不正确')
        code = str(it.get('code') or '').strip()
        if not code:
            raise BizError('商品行缺少条码/编码')
        q = dec_qty(it.get('qty', 1))
        if q <= 0 or q > 99999:
            raise BizError('数量必须大于 0：%s' % code)
        bid = it.get('batch_id')
        qs = str(int(q)) if q == q.to_integral_value() else format(q.normalize(), 'f')
        out.append({'code': code, 'qty': qs, 'unit': (it.get('unit') or None),
                    'price': it.get('price') if it.get('price') not in (None, '') else None,
                    'batch_id': inum(bid) if bid not in (None, '', 0, '0') else None, 'is_gift': bool(it.get('is_gift')),
                    'line_remark': str(it.get('line_remark') or '')[:60]})
    cnos = data.get('coupon_nos')
    if not isinstance(cnos, list):
        cnos = []
    if data.get('coupon_no'):
        cnos = cnos + [data.get('coupon_no')]
    cnos = [str(c).strip() for c in cnos if str(c or '').strip()]
    od = data.get('order_discount') or None
    if od and not (od.get('rate') not in (None, '') or od.get('amount') not in (None, '')):
        od = None
    cart = {'items': out, 'member_code': (data.get('member_code') or '').strip() or None, 'coupon_nos': list(dict.fromkeys(cnos)),
            'points_used': max(0, inum(data.get('points_used'), 0)), 'order_discount': od}
    if data.get('round_mode'):
        cart['round_mode'] = data.get('round_mode')
    return cart


def priced_lines(db, actor, store_code, cart, priced):
    """校对促销引擎输出的单位/换算率/基本单位数量（以条码解析结果为准），返回 [(line, resolved, src_item)]。"""
    mid = actor['merchant_id']
    items = cart['items']
    resolved = [resolve_unit(db, mid, it['code'], it.get('unit')) for it in items]
    out = []
    for ln in priced['lines']:
        idx = int(ln.get('line_no') or 0) - 1
        res, src = None, None
        if 0 <= idx < len(items) and resolved[idx]['product']['id'] == ln['product_id']:
            res, src = resolved[idx], items[idx]
        rate = int(ln.get('unit_rate') or 1)
        if res is not None and rate != res['rate']:
            if rate == 1:
                ln['unit'], ln['unit_rate'] = res['unit'], res['rate']
            else:
                raise BizError('计价引擎返回的单位换算（%s×%d）与条码不一致，请联系总部' % (ln.get('unit'), rate), 500)
        elif res is not None:
            ln['unit'] = res['unit']
        ln['base_qty'] = inventory.to_base_qty(ln['qty'], ln.get('unit_rate') or 1)
        if src is not None:
            ln['batch_id'] = src.get('batch_id')
            ln['is_gift'] = bool(ln.get('is_gift') or src.get('is_gift'))
            ln['line_remark'] = src.get('line_remark', '')
        out.append((ln, res, src))
    # 引擎只在 gifts 里给出、没有放进 lines 的赠品：补成 0 元赠品行（POS-108：出库、保留成本和活动来源）
    have = set((ln['product_id'], bool(ln.get('is_gift'))) for ln, _, _ in out)
    for g in priced.get('gifts') or []:
        if (g.get('product_id'), True) in have:
            continue
        p = db.execute('SELECT * FROM products WHERE id=? AND merchant_id=?', (g['product_id'], mid)).fetchone()
        if p is None:
            continue
        p = dict(p)
        q = int(g.get('qty') or 1)
        rc = int(p.get('retail_cents') or 0)
        ln = {'line_no': len(out) + 1, 'product_id': p['id'], 'code': p['code'], 'name': p['name'], 'spec': p.get('spec', ''),
              'unit': p.get('base_unit') or p.get('unit') or '', 'unit_rate': 1, 'qty': q, 'base_qty': q, 'batch_id': None, 'is_gift': True,
              'retail_cents': rc, 'base_price_cents': 0, 'price_cents': 0, 'line_amount_cents': 0, 'share_cents': 0, 'amount_cents': 0,
              'cost_cents': int(p.get('cost_cents') or 0) * q, 'price_changed': False,
              'promos': [{'kind': 'gift', 'source_no': g.get('activity_no', ''), 'name': '赠品', 'amount_cents': rc * q}]}
        priced['lines'].append(ln)
        out.append((ln, {'product': p, 'unit': ln['unit'], 'rate': 1, 'barcode': '', 'unit_row': None}, None))
    return out


def derive_need_auth(db, actor, cart, priced, plines):
    """改价/折扣是否超额以角色额度为准（不信任引擎的角色判断）：改价 -> price_change；整单折扣 -> discount（折扣率）。"""
    need = {}
    changed = [ln for ln, res, src in plines if ln.get('price_changed') and int(ln.get('retail_cents') or 0) > 0]
    for ln, res, src in plines:
        if ln.get('price_changed') and res is not None and not int(res['product'].get('allow_discount', 1) or 0) \
                and int(ln.get('price_cents') or 0) < int(ln.get('base_price_cents') or 0):
            raise BizError('商品【%s】不允许打折/降价' % ln['name'])
    if changed:
        need['price_change'] = {'kind': 'price_change', 'value': None, 'label': '改价：' + '、'.join(ln['name'] for ln in changed[:3])}
    od = cart.get('order_discount') or {}
    rate = None
    if od.get('rate') not in (None, ''):
        rate = dec_qty(od['rate'], '折扣率')
    elif od.get('amount') not in (None, ''):
        amt = cents_of(od['amount'], '整单优惠')
        sub = int(priced.get('subtotal_cents') or 0)
        if amt > 0 and sub > 0:
            rate = ((Decimal(sub) - Decimal(amt)) / Decimal(sub)).quantize(Decimal('0.0001'), rounding=ROUND_FLOOR)
    for n in priced.get('need_auth') or []:
        if n.get('kind') == 'discount':
            try:
                v = Decimal(str(n.get('value')))
            except (InvalidOperation, ValueError):
                continue
            if Decimal(0) < v < Decimal(1) and (rate is None or v < rate):
                rate = v
        elif n.get('kind') not in ('price_change', 'discount') and n.get('kind'):
            need.setdefault(n['kind'], {'kind': n['kind'], 'value': n.get('value'), 'label': n.get('label') or n['kind']})
    rank = {'none': 0, 'jiao': 1, 'yuan': 2}
    cfg_mode = get_config(db, actor['merchant_id']).get('round_mode') or 'none'
    ro = int(priced.get('round_off_cents') or 0)
    if cart.get('round_mode') and rank.get(cart['round_mode'], 0) > rank.get(cfg_mode, 0) and ro > 0:
        pay = int(priced['payable_cents'])
        rr = (Decimal(pay) / Decimal(pay + ro)).quantize(Decimal('0.0001'), rounding=ROUND_FLOOR)
        if rate is None or rr < rate:
            rate = rr
    if rate is not None and rate < 1:
        need['discount'] = {'kind': 'discount', 'value': str(rate), 'label': '整单折扣/抹零 %s' % rate}
    return list(need.values())


def _perm_for(kind):
    return {'price_change': 'pos.price.change', 'discount': 'pos.discount'}.get(kind)


def price_preview(db, actor, store_code, data):
    """试算（POS-105/405）：与结算同一个 price_cart；need_auth 标明本人额度是否够（self_ok）。"""
    from .core import within_limit
    cart = normalize_cart(data)
    priced = promo_engine.price_cart(db, actor, store_code, cart)
    plines = priced_lines(db, actor, store_code, cart, priced)
    need = derive_need_auth(db, actor, cart, priced, plines)
    for n in need:
        p = _perm_for(n['kind'])
        n['allowed'] = p is None or has_perm(actor, p)
        try:
            n['self_ok'] = n['allowed'] and within_limit(db, actor, n['kind'], n['value'], store_code)
        except (KeyError, InvalidOperation, ValueError, TypeError):
            n['self_ok'] = False
    return priced_view(db, actor, priced, plines, need)


def priced_view(db, actor, priced, plines, need):
    items = []
    for ln, res, src in plines:
        items.append({
            'line_no': ln['line_no'], 'product_id': ln['product_id'], 'code': ln['code'], 'name': ln['name'], 'spec': ln.get('spec', ''),
            'unit': ln.get('unit', ''), 'unit_rate': int(ln.get('unit_rate') or 1), 'qty': qty_out(ln['qty']), 'base_qty': ln['base_qty'],
            'batch_id': ln.get('batch_id'), 'is_gift': bool(ln.get('is_gift')), 'retail': yuan(ln['retail_cents']),
            'base_price': yuan(ln.get('base_price_cents', ln['price_cents'])), 'price': yuan(ln['price_cents']),
            'line_amount': yuan(ln['line_amount_cents']), 'share_amount': yuan(ln.get('share_cents', 0)), 'amount': yuan(ln['amount_cents']),
            'price_changed': bool(ln.get('price_changed')), 'promo': bool(ln.get('promos')),
            'promos': [{'kind': x.get('kind'), 'name': x.get('name', ''), 'source_no': x.get('source_no', ''), 'amount': yuan(x.get('amount_cents', 0))}
                       for x in (ln.get('promos') or [])]})
    coupons = [{'coupon_no': c['coupon_no'], 'amount': yuan(c['amount_cents'])} for c in priced.get('coupons') or []]
    m = priced.get('member')
    return {
        'items': items, 'subtotal': yuan(priced['subtotal_cents']), 'retail_total': yuan(priced['retail_total_cents']),
        'order_discount': yuan(priced.get('order_discount_cents', 0)), 'coupons': coupons,
        'coupon_amount': yuan(sum(c['amount_cents'] for c in priced.get('coupons') or [])), 'points_used': priced.get('points_used', 0),
        'points_amount': yuan(priced.get('points_cents', 0)), 'round_off': yuan(priced.get('round_off_cents', 0)),
        'payable': yuan(priced['payable_cents']), 'points_earned': priced.get('points_earned', 0),
        'discount_total': yuan(priced['retail_total_cents'] - priced['payable_cents']),
        'discounts': [{'kind': d['kind'], 'line_no': d.get('line_no', 0), 'source_no': d.get('source_no', ''), 'source_name': d.get('source_name', ''),
                       'amount': yuan(d['amount_cents'])} for d in priced.get('discounts') or []],
        'explain': priced.get('explain') or [], 'need_auth': need,
        'member': {'code': m['code'], 'name': m.get('name'), 'level': m.get('level')} if m else None,
    }


def _discount_rows(priced, plines):
    """order_discounts 行：引擎的 discounts + （引擎未覆盖时）按行补会员价/特价/改价/赠品，保证合计 = 零售合计 − 应收。"""
    rows = [dict(d) for d in priced.get('discounts') or []]
    gap = int(priced['retail_total_cents']) - int(priced['payable_cents']) - sum(int(d['amount_cents']) for d in rows)
    if gap:
        extra = []
        for ln, res, src in plines:
            retail_amt = int((Decimal(int(ln['retail_cents'])) * Decimal(str(ln['qty']))).to_integral_value())
            line_disc = retail_amt - int(ln['line_amount_cents'])
            if not line_disc:
                continue
            acc = 0
            for x in ln.get('promos') or []:
                k = x.get('kind') if x.get('kind') in DISCOUNT_KINDS else 'activity'
                extra.append({'kind': k, 'line_no': ln['line_no'], 'source_no': x.get('source_no', ''), 'source_name': x.get('name', ''),
                              'amount_cents': int(x.get('amount_cents') or 0)})
                acc += int(x.get('amount_cents') or 0)
            rest = line_disc - acc
            if rest:
                k = 'gift' if ln.get('is_gift') else ('price_change' if ln.get('price_changed') else 'member_price')
                extra.append({'kind': k, 'line_no': ln['line_no'], 'source_no': '', 'source_name': {'gift': '赠品', 'price_change': '收银改价',
                              'member_price': '会员价'}[k], 'amount_cents': rest})
        rows += extra
        left = gap - sum(e['amount_cents'] for e in extra)
        if left:
            rows.append({'kind': 'item_discount', 'line_no': 0, 'source_no': '', 'source_name': '其他优惠（引擎未细分）', 'amount_cents': left})
    return rows


def _alloc_and_out(db, actor, mid, store_code, ln, res, ref_type, ref_no, allow_negative):
    """一行商品出库：组合装按 BOM 扣组件（goods_svc.expand_bom 存在时），否则扣自身。返回批次分配（带 product_id）。"""
    p = res['product'] if res else db.execute('SELECT * FROM products WHERE id=?', (ln['product_id'],)).fetchone()
    stock_lines = [{'product_id': ln['product_id'], 'base_qty': ln['base_qty'], 'batch_id': ln.get('batch_id'),
                    'unit': ln.get('unit', ''), 'unit_qty': float(Decimal(str(ln['qty']))), 'rate': int(ln.get('unit_rate') or 1)}]
    expand = getattr(goods_svc, 'expand_bom', None)
    if expand is not None and (p['combo_mode'] if p is not None else '') == 'components':
        stock_lines = [{'product_id': c['product_id'], 'base_qty': int(c['base_qty']), 'note': '组合装 %s' % ln['code']}
                       for c in expand(db, mid, ln['product_id'], ln['base_qty'])]
    allocs = inventory.stock_out(db, actor, mid, store_code, stock_lines, ref_type, ref_no, allow_negative=allow_negative)
    out = []
    for sl, al in zip(stock_lines, allocs):
        for a in al:
            out.append({'product_id': sl['product_id'], 'batch_id': a['batch_id'], 'batch_no': a['batch_no'], 'expiry_date': a['expiry_date'],
                        'qty': int(a['qty']), 'ret': 0})
    return out


def _norm_payments(raw):
    pays = []
    for p in raw or []:
        if not isinstance(p, dict):
            raise BizError('支付行格式不正确')
        m = (p.get('method') or '').strip()
        if m not in PAY_NAME:
            raise BizError('不支持的支付方式：%s' % m)
        a = cents_of(p.get('amount'), '支付金额')
        if a < 0:
            raise BizError('支付金额不能为负')
        if a == 0:
            continue
        pays.append({'method': m, 'amount': a, 'ref': str(p.get('ref') or '')[:64], 'name': pay_name(m, p.get('name'))})
    return pays


def _settle_payments(pays, payable, credit=0):
    """校验组合支付并计算找零：只有现金能找零；非现金合计不能超过应收。返回 (记账支付行, 收到合计, 找零)。credit=换货抵扣（分）。"""
    need = payable - credit
    paid = sum(p['amount'] for p in pays)
    if need > 0 and not pays:
        raise BizError('请选择支付方式并输入收款金额')
    if paid < need:
        raise BizError('实收 %s 不足应收 %s，还差 %s 元' % (yuan(paid), yuan(need), yuan(need - paid)))
    noncash = sum(p['amount'] for p in pays if p['method'] != 'cash')
    if noncash > max(need, 0):
        raise BizError('只有现金可以找零：非现金合计 %s 超过应收 %s' % (yuan(noncash), yuan(max(need, 0))))
    change = paid - max(need, 0)
    out = [dict(p) for p in pays if p['method'] != 'cash']
    cash = sum(p['amount'] for p in pays if p['method'] == 'cash') - change
    if cash > 0:
        refs = [p['ref'] for p in pays if p['method'] == 'cash' and p['ref']]
        out.append({'method': 'cash', 'amount': cash, 'ref': refs[0] if refs else '', 'name': pay_name('cash')})
    if credit > 0:
        out.insert(0, {'method': 'exchange', 'amount': credit, 'ref': '换货抵扣', 'name': pay_name('exchange')})
    return out, paid, change


def _insert_payment(db, actor, mid, store_code, order_no, shift_id, kind, p, request_no=None, orig_pay_no=''):
    pno = next_no(db, mid, 'PY' if kind != 'refund' else 'RF', store_code)
    ts = now_s()
    db.execute('INSERT INTO payments(merchant_id, order_no, method, method_name, amount_cents, ref, shift_id, store_code, kind, created_at, '
               'pay_no, request_no, status, confirm_mode, paid_at, operator, orig_pay_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (mid, order_no, p['method'], p.get('name') or pay_name(p['method']), int(p['amount']), p.get('ref', ''), shift_id, store_code, kind,
                ts, pno, request_no, 'success', 'manual', ts, actor['username'], orig_pay_no or ''))
    return pno


_ORDER_COLS = ['merchant_id', 'order_no', 'client_id', 'biz_type', 'store_code', 'store_name', 'terminal_no', 'cashier_user', 'cashier_name',
               'guide_code', 'guide_name', 'member_code', 'member_phone', 'member_name', 'member_level', 'item_qty', 'retail_total_cents',
               'discount_total_cents', 'order_discount_cents', 'coupon_no', 'coupon_amount_cents', 'points_used', 'points_amount_cents',
               'round_off_cents', 'payable_cents', 'paid_total_cents', 'change_amount_cents', 'points_earned', 'status', 'orig_order_no',
               'refund_reason', 'remark', 'auth_user', 'shift_id', 'created_at', 'pay_status', 'exchange_no', 'approval_no', 'no_receipt', 'snapshot']


def insert_order(db, vals):
    row = dict((c, vals.get(c)) for c in _ORDER_COLS)
    for c in ('retail_total_cents', 'discount_total_cents', 'order_discount_cents', 'coupon_amount_cents', 'points_amount_cents',
              'round_off_cents', 'payable_cents', 'paid_total_cents', 'change_amount_cents', 'points_used', 'points_earned', 'no_receipt'):
        row[c] = int(row[c] or 0)
    for c in ('coupon_no', 'orig_order_no', 'refund_reason', 'remark', 'auth_user', 'exchange_no', 'approval_no', 'guide_code', 'guide_name',
              'member_code', 'member_phone', 'member_name', 'member_level', 'snapshot', 'terminal_no'):
        row[c] = row[c] or ''
    row['pay_status'] = row['pay_status'] or 'paid'
    db.execute('INSERT INTO orders(%s) VALUES(%s)' % (','.join(_ORDER_COLS), ','.join('?' * len(_ORDER_COLS))), [row[c] for c in _ORDER_COLS])


def _dup_order(db, mid, cid, biz_type=None):
    if not cid:
        return None
    ex = db.execute('SELECT order_no, merchant_id, biz_type FROM orders WHERE client_id=?', (cid,)).fetchone()
    if ex is None:
        return None
    if ex['merchant_id'] != mid or (biz_type and ex['biz_type'] != biz_type):
        raise BizError('请求号冲突，请重试', 409, 'CONFLICT')
    return ex


def _risk(db, actor, store_code, title, body, ref_no, dedup=None):
    from . import notify
    notify.notify_perm(db, actor['merchant_id'], 'approval.pos', store_code, 'risk', title, body, exclude_user_id=actor['id'], ref_type='order',
                       ref_no=ref_no, level='important', dedup_key=dedup)


def do_checkout(db, data, store_code, actor, exchange=None):
    """结算（POS-1xx/5xx，9.2~9.5）。调用方在 tx(db) 内。exchange={'no','credit'} 时是换货单里的新销售单（无 client_id）。"""
    mid = actor['merchant_id']
    cid = None if exchange else ((data.get('client_id') or '').strip()[:64] or None)
    ex = _dup_order(db, mid, cid, 'sale')
    if ex is not None:
        return {'order_no': ex['order_no'], 'duplicate': True}
    st = store_row(db, store_code, mid)
    if st is None:
        raise BizError('门店不存在')
    sh = require_shift(db, store_code, actor)
    cfg = get_config(db, mid)
    cart = normalize_cart(data)
    for it in cart['items']:
        if it.get('batch_id'):
            if not has_perm(actor, 'pos.batch.choose'):
                raise BizError('没有手动指定批次的权限（默认按先到期先出自动分配）', 403)
    priced = promo_engine.price_cart(db, actor, store_code, cart)
    plines = priced_lines(db, actor, store_code, cart, priced)
    for ln, res, src in plines:
        if res is not None:
            why = goods_svc.sale_status(db, mid, store_code, res['product'])
            if why:
                raise BizError('%s：%s' % (ln['name'], why))
        if ln.get('batch_id'):
            b = inventory.batch_row(db, mid, int(ln['batch_id']))
            if b is None or b['product_id'] != ln['product_id']:
                raise BizError('批次不属于商品【%s】' % ln['name'])
            why = inventory.check_batch_sellable(b)
            if why:
                raise BizError('%s：%s，不能销售' % (ln['name'], why), 409, 'BATCH_UNSELLABLE')
    # 改价/折扣额度：本人 -> 现场授权 -> 审批单（POS-106）
    auths = []
    for n in derive_need_auth(db, actor, cart, priced, plines):
        p = _perm_for(n['kind'])
        if p and not has_perm(actor, p):
            raise BizError('没有%s权限' % ('改价' if n['kind'] == 'price_change' else '折扣'), 403)
        r = check_limit_or_approval(db, actor, n['kind'], n['value'], data, store_code, '', n['label'])
        auths.append(dict(n, mode=r['mode'], by=r['by'], approval_no=r.get('approval_no', '')))
    payable = int(priced['payable_cents'])
    credit = min(int(exchange['credit']), payable) if exchange else 0
    pays, paid_total, change = _settle_payments(_norm_payments(data.get('payments')), payable, credit)
    member = priced.get('member')
    if member and member.get('usable') is False and (priced.get('points_used') or priced.get('coupons')):
        raise BizError('会员已停用或在黑名单，不能使用积分/券', 409, 'CONFLICT')
    stored = sum(p['amount'] for p in pays if p['method'] == 'stored')
    if stored:
        if not member:
            raise BizError('储值支付需先选择会员')
        mrow = db.execute('SELECT * FROM members WHERE merchant_id=? AND code=?', (mid, member['code'])).fetchone()
        if int(_col(mrow, 'blacklisted', 0) or 0) or (mrow['status'] or '正常') != '正常':
            raise BizError('会员已停用或在黑名单，不能使用储值', 409, 'CONFLICT')
        bal = _cents_col(mrow, 'balance')
        if stored > bal:
            raise BizError('储值余额不足（余额 %s 元）' % yuan(bal), 409, 'STORED_SHORT')

    ts = now_s()
    order_no = next_no(db, mid, 'S', store_code)
    neg_ok = bool(cfg.get('allow_negative_stock')) and has_perm(actor, 'pos.negative_stock')
    guide = guide_info(db, data.get('guide_code'), mid)
    terminal = (data.get('terminal_no') or sh['terminal_no'] or '').strip()[:20]
    touched = set()
    for ln, res, src in plines:
        p = res['product'] if res else dict(db.execute('SELECT * FROM products WHERE id=?', (ln['product_id'],)).fetchone())
        alloc = _alloc_and_out(db, actor, mid, store_code, ln, res, 'sale', order_no, neg_ok)
        touched.update(a['product_id'] for a in alloc)
        cost = int(p.get('cost_cents') or 0) * int(ln['base_qty'])
        db.execute('INSERT INTO order_items(merchant_id, order_no, line_no, product_id, product_code, barcode, name, spec, unit, brand, category, qty, '
                   'retail_cents, price_cents, line_amount_cents, share_amount_cents, amount_cents, cost_cents, unit_qty, unit_rate, base_qty, '
                   'batch_alloc, is_gift, promo_ref, line_remark, refunded_qty) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
                   (mid, order_no, ln['line_no'], ln['product_id'], ln['code'], (res or {}).get('barcode') or first_barcode(db, ln['product_id']),
                    ln['name'], ln.get('spec', ''), ln.get('unit', ''), p.get('brand', ''), p.get('category', ''), float(Decimal(str(ln['qty']))),
                    int(ln['retail_cents']), int(ln['price_cents']), int(ln['line_amount_cents']), int(ln.get('share_cents') or 0),
                    int(ln['amount_cents']), cost, float(Decimal(str(ln['qty']))), int(ln.get('unit_rate') or 1), int(ln['base_qty']),
                    json.dumps(alloc, ensure_ascii=False), 1 if ln.get('is_gift') else 0,
                    json.dumps(ln.get('promos') or [], ensure_ascii=False), ln.get('line_remark', '')))
    auth_of = dict((a['kind'], a) for a in auths)
    for d in _discount_rows(priced, plines):
        a = auth_of.get('price_change') if d['kind'] == 'price_change' else (
            auth_of.get('discount') if d['kind'] in ('order_discount', 'item_discount') else None)
        db.execute('INSERT INTO order_discounts(merchant_id, order_no, line_no, kind, source_no, source_name, amount_cents, auth_user, approval_no, '
                   'detail) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (mid, order_no, int(d.get('line_no') or 0), d['kind'] if d['kind'] in DISCOUNT_KINDS else 'item_discount',
                    d.get('source_no', ''), d.get('source_name', ''), int(d['amount_cents']),
                    a['by'] if a and a['mode'] != 'self' else '', a.get('approval_no', '') if a else '', '{}'))
    for i, p in enumerate(pays):
        _insert_payment(db, actor, mid, store_code, order_no, sh['id'], 'sale', p, ('%s#%d' % (cid, i)) if cid else None)
    if stored:
        member_svc.stored_change(db, actor, mid, member['code'], -stored, '消费', order_no, store_code)
    if member:
        promo_engine.apply_benefits(db, actor, order_no, store_code, priced)
    auth_user = ','.join(sorted(set(a['by'] for a in auths if a['mode'] != 'self')))
    approval_no = ','.join(a['approval_no'] for a in auths if a.get('approval_no'))
    snap = dict(priced.get('snapshot') or {})
    snap.update({'explain': priced.get('explain') or [], 'auth': [{'kind': a['kind'], 'value': a['value'], 'mode': a['mode'], 'by': a['by']}
                                                                 for a in auths],
                 'receipt': {'title': cfg.get('receipt_title', ''), 'footer': cfg.get('receipt_footer', '')}})
    insert_order(db, dict(
        merchant_id=mid, order_no=order_no, client_id=cid, biz_type='sale', store_code=store_code, store_name=st['name'], terminal_no=terminal,
        cashier_user=actor['username'], cashier_name=actor['name'], guide_code=guide['code'], guide_name=guide['name'],
        member_code=member['code'] if member else '',
        # 卡片里的 phone 已按当前账号权限脱敏，订单要存真实号码（报表/按手机号查单用）
        member_phone=((db.execute('SELECT phone FROM members WHERE merchant_id=? AND code=?', (mid, member['code'])).fetchone() or [''])[0] or '') if member else '',
        member_name=(member.get('name') or '') if member else '', member_level=(member.get('level') or '') if member else '',
        item_qty=float(sum(Decimal(str(ln['qty'])) for ln, _, _ in plines)), retail_total_cents=priced['retail_total_cents'],
        discount_total_cents=int(priced['retail_total_cents']) - payable, order_discount_cents=priced.get('order_discount_cents', 0),
        coupon_no=','.join(c['coupon_no'] for c in priced.get('coupons') or []),
        coupon_amount_cents=sum(int(c['amount_cents']) for c in priced.get('coupons') or []), points_used=priced.get('points_used', 0),
        points_amount_cents=priced.get('points_cents', 0), round_off_cents=priced.get('round_off_cents', 0), payable_cents=payable,
        paid_total_cents=paid_total + credit, change_amount_cents=change, points_earned=priced.get('points_earned', 0) if member else 0,
        status='已完成', remark=str(data.get('remark') or '')[:100], auth_user=auth_user, shift_id=sh['id'], created_at=ts,
        exchange_no=exchange['no'] if exchange else '', approval_no=approval_no, snapshot=json.dumps(snap, ensure_ascii=False, default=str)))
    neg = [pid for pid in touched if inventory.qty_of(db, mid, store_code, pid, 0, 'ok') < 0]
    if neg:
        names = [r[0] for r in db.execute('SELECT name FROM products WHERE id IN (%s)' % ','.join('?' * len(neg)), neg)]
        audit(db, actor, 'pos', '负库存销售', order_no, None, {'products': names}, '商户允许负库存', level='high', store_code=store_code)
        _risk(db, actor, store_code, '负库存销售：%s' % '、'.join(names[:3]), '单号 %s，请尽快补货或盘点' % order_no, order_no)
    big = [a for a in auths if a['mode'] != 'self']
    disc_cents = sum(int(d['amount_cents']) for d in _discount_rows(priced, plines) if d['kind'] in ('price_change', 'order_discount', 'item_discount'))
    if auths and (disc_cents >= BIG_DISCOUNT_CENTS or any(a['kind'] == 'discount' and Decimal(a['value']) < BIG_DISCOUNT_RATE for a in auths)):
        _risk(db, actor, store_code, '大额改价/折扣：%s 元' % yuan(disc_cents), '单号 %s，收银员 %s，授权 %s' % (order_no, actor['name'], auth_user or '本人'),
              order_no)
    audit(db, actor, 'pos', '收银结算' if not exchange else '换货销售', order_no, None,
          {'payable': yuan(payable), 'pays': [(p['method'], yuan(p['amount'])) for p in pays], 'auth': auth_user},
          level='high' if big else 'info', store_code=store_code, terminal_no=terminal)
    return {'order_no': order_no, 'change': change, 'payable': payable, 'paid_total': paid_total}


# ================================================================ 退货 / 无单退货 / 换货（POS-601~606）
def _orig_order(db, actor, order_no, store_code):
    o = db.execute("SELECT * FROM orders WHERE order_no=? AND merchant_id=? AND biz_type='sale'", ((order_no or '').strip(), actor['merchant_id'])).fetchone()
    if o is None:
        raise BizError('原销售单不存在：%s' % (order_no or ''), 404)
    if o['store_code'] not in scope_stores(db, actor):
        raise BizError('只能处理本人数据范围内门店的单据', 403, 'SCOPE_DENIED')
    if store_code and o['store_code'] != store_code:
        raise BizError('原单属于门店 %s，跨店退货暂不支持（P1）' % o['store_code'], 403)
    return o


def _refunded_cents(db, line_id):
    r = db.execute("SELECT COALESCE(-SUM(amount_cents),0), COALESCE(-SUM(share_amount_cents),0), COALESCE(-SUM(line_amount_cents),0), "
                   "COALESCE(-SUM(cost_cents),0) FROM order_items WHERE orig_line_id=?", (line_id,)).fetchone()
    return int(r[0] or 0), int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)


def _q4(v):
    return dec_qty(v).quantize(Decimal('0.0001'))


def refund_plan(db, o, req_items):
    """按原单快照算可退数量与应退金额（POS-602）：行实付 × 退数量 / 购买数量；退完该行时 = 行实付 − 已退，累计不超（AC-004/005）。"""
    lines = db.execute('SELECT * FROM order_items WHERE order_no=? AND merchant_id=? ORDER BY line_no', (o['order_no'], o['merchant_id'])).fetchall()
    by_id = dict((r['id'], r) for r in lines)
    req = []
    if not req_items:
        for r in lines:
            left = _q4(r['qty']) - _q4(r['refunded_qty'] or 0)
            if left > 0:
                req.append({'line_id': r['id'], 'qty': left, 'quality': 'ok', 'reason_code': ''})
    else:
        seen = set()
        for x in req_items:
            lid = inum(x.get('line_id'))
            if lid not in by_id:
                raise BizError('退货行不属于原单：%s' % lid)
            if lid in seen:
                raise BizError('同一商品行不要重复提交')
            seen.add(lid)
            q = dec_qty(x.get('qty')).quantize(Decimal('0.0001'))
            if q <= 0:
                continue
            qual = (x.get('quality') or 'ok').strip()
            if qual not in QUALITY_NAME:
                raise BizError('质量状态不正确：%s' % qual)
            req.append({'line_id': lid, 'qty': q, 'quality': qual, 'reason_code': str(x.get('reason_code') or '')[:20]})
    if not req:
        raise BizError('请选择要退的商品和数量')
    plan = []
    for x in req:
        r = by_id[x['line_id']]
        bought = _q4(r['qty'])
        left = bought - _q4(r['refunded_qty'] or 0)
        if x['qty'] > left:
            raise BizError('%s 可退数量只剩 %s' % (r['name'], qty_out(left)))
        rate = int(_col(r, 'unit_rate', 1) or 1)
        base = inventory.to_base_qty(x['qty'], rate)
        amt_all, share_all, la_all, cost_all = _cents_col(r, 'amount'), _cents_col(r, 'share_amount'), _cents_col(r, 'line_amount'), _cents_col(r, 'cost')
        ra, rs, rl, rc = _refunded_cents(db, r['id'])
        if x['qty'] == left:
            amt, share, la, cost = amt_all - ra, share_all - rs, la_all - rl, cost_all - rc
        else:
            amt = min(ratio_cents(amt_all, x['qty'], bought), amt_all - ra)
            share = min(ratio_cents(share_all, x['qty'], bought), share_all - rs)
            la = min(ratio_cents(la_all, x['qty'], bought), la_all - rl)
            cost = min(ratio_cents(cost_all, x['qty'], bought), cost_all - rc)
        plan.append(dict(x, row=r, base_qty=base, amount=amt, share=share, line_amount=la, cost=cost, completes=(x['qty'] == left)))
    return plan, lines


def _refund_split(db, o, total, order_pref=None, cfg=None):
    """原路退款拆分（POS-603/AC-005）：按顺序（请求指定 > 商户参数 refund_method_order > 默认 储值→微信→支付宝→银行卡→其他→现金）
    逐个退到原支付方式，每种方式累计不超过原单该方式实收；原单的换货抵扣部分按现金退。"""
    mid = o['merchant_id']
    paid = {}
    first_no = {}
    names = {}
    for p in db.execute("SELECT * FROM payments WHERE order_no=? AND merchant_id=? AND kind='sale' AND COALESCE(status,'success')='success' "
                        "ORDER BY id", (o['order_no'], mid)):
        m = 'cash' if p['method'] == 'exchange' else p['method']
        paid[m] = paid.get(m, 0) + _cents_col(p, 'amount')
        first_no.setdefault(m, _col(p, 'pay_no', '') or '')
        if m == 'other':
            names.setdefault(m, p['method_name'])
    for r in db.execute("SELECT p.method, SUM(p.amount_cents) FROM payments p JOIN orders o ON o.order_no=p.order_no "
                        "WHERE o.merchant_id=? AND o.orig_order_no=? AND o.biz_type='refund' AND p.kind='refund' GROUP BY p.method", (mid, o['order_no'])):
        m = 'cash' if r[0] == 'exchange' else r[0]
        paid[m] = paid.get(m, 0) + int(r[1] or 0)
    order = [m for m in (order_pref or (cfg or {}).get('refund_method_order') or DEFAULT_REFUND_ORDER) if m in PAY_NAME]
    order += [m for m in DEFAULT_REFUND_ORDER if m not in order]
    left = total
    out = []
    for m in order:
        if left <= 0:
            break
        avail = paid.get(m, 0)
        if avail <= 0:
            continue
        a = min(avail, left)
        out.append({'method': m, 'amount': a, 'orig_pay_no': first_no.get(m, ''), 'name': names.get(m) or pay_name(m)})
        left -= a
    if left > 0:
        raise BizError('退款金额超过原单剩余可退金额 %s 元' % yuan(left), 409, 'CONFLICT')
    return out


def _reverse_fallback(db, actor, rno, o, plan, store_code):
    """促销基线还没有 reverse_benefits 时的兜底：按累计退款比例扣回消费积分、返还抵扣积分（积分不足记负积分，留流水，AC-017）。券不退回。"""
    if not o['member_code'] or int(o['payable_cents'] or 0) <= 0:
        return {'points_back': 0, 'points_restore': 0, 'coupons_returned': [], 'note': ''}
    mid = o['merchant_id']
    payable = int(o['payable_cents'])
    prev = -int(db.execute("SELECT COALESCE(SUM(payable_cents),0) FROM orders WHERE merchant_id=? AND orig_order_no=? AND biz_type='refund' "
                           "AND order_no != ?", (mid, o['order_no'], rno)).fetchone()[0] or 0)
    now_total = sum(x['amount'] for x in plan)
    done = db.execute("SELECT type, COALESCE(SUM(change),0) FROM points_log WHERE merchant_id=? AND order_no IN (SELECT order_no FROM orders "
                      "WHERE merchant_id=? AND orig_order_no=? AND biz_type='refund' AND order_no != ?) GROUP BY type",
                      (mid, mid, o['order_no'], rno)).fetchall()
    done = dict((r[0], int(r[1] or 0)) for r in done)
    cum = min(payable, prev + now_total)
    back = int(Decimal(int(o['points_earned'] or 0)) * cum / payable) + done.get('退货扣回', 0)
    restore = int(Decimal(int(o['points_used'] or 0)) * cum / payable) - done.get('退货返还抵扣积分', 0)
    note = ''
    if back > 0:
        m = db.execute('SELECT points FROM members WHERE merchant_id=? AND code=?', (mid, o['member_code'])).fetchone()
        if m is not None and int(m[0] or 0) < back:
            note = '积分不足扣回，记为负积分'
        member_svc.points_change(db, actor, mid, o['member_code'], -back, '退货扣回', rno, store_code, '原单 %s %s' % (o['order_no'], note),
                                 allow_negative=True)
    if restore > 0:
        member_svc.points_change(db, actor, mid, o['member_code'], restore, '退货返还抵扣积分', rno, store_code, '原单 ' + o['order_no'])
    return {'points_back': max(back, 0), 'points_restore': max(restore, 0), 'coupons_returned': [], 'note': note or '券按基线规则不退回'}


def _return_stock(db, actor, mid, store_code, x, rno):
    """按原行批次分配回库（POS-606）：质量状态决定进合格/残次/待处理；组合装按组件比例回库。返回回库分配。"""
    r = x['row']
    alloc = _json_load(_col(r, 'batch_alloc', ''), None)
    line_base = int(_col(r, 'base_qty', 0) or 0) or inventory.to_base_qty(r['qty'], int(_col(r, 'unit_rate', 1) or 1))
    if not alloc:
        alloc = [{'product_id': r['product_id'], 'batch_id': 0, 'batch_no': '', 'expiry_date': '', 'qty': line_base, 'ret': 0}]
    pids = []
    for a in alloc:
        if a['product_id'] not in pids:
            pids.append(a['product_id'])
    lines, back = [], []
    for pid in pids:
        mine = [a for a in alloc if a['product_id'] == pid]
        total = sum(int(a['qty']) for a in mine)
        need_d = Decimal(total) * x['base_qty'] / Decimal(line_base)
        if need_d != need_d.to_integral_value():
            raise BizError('退货数量与组合装组件换算不整除')
        need = int(need_d)
        for a in reversed(mine):
            if need <= 0:
                break
            take = min(need, int(a['qty']) - int(a.get('ret') or 0))
            if take <= 0:
                continue
            a['ret'] = int(a.get('ret') or 0) + take
            need -= take
            lines.append({'product_id': pid, 'base_qty': take, 'batch_id': a['batch_id'], 'quality': x['quality'], 'note': '原单 ' + r['order_no']})
            back.append({'product_id': pid, 'batch_id': a['batch_id'], 'batch_no': a.get('batch_no', ''), 'expiry_date': a.get('expiry_date', ''),
                         'qty': take, 'quality': x['quality']})
        if need > 0:
            lines.append({'product_id': pid, 'base_qty': need, 'batch_id': 0, 'quality': x['quality'], 'note': '原单 ' + r['order_no']})
            back.append({'product_id': pid, 'batch_id': 0, 'batch_no': '', 'expiry_date': '', 'qty': need, 'quality': x['quality']})
    if lines and r['product_id']:
        inventory.stock_in(db, actor, mid, store_code, lines, 'refund', rno)
    return alloc, back


def do_refund(db, data, actor, store_code=None, exchange=None):
    """原单退货（POS-601~606）。调用方在 tx(db) 内。exchange={'no','credit'}：换货里的退货部分，credit 部分记「换货抵扣」不退钱。"""
    mid = actor['merchant_id']
    cid = None if exchange else ((data.get('client_id') or '').strip()[:64] or None)
    ex = _dup_order(db, mid, cid, 'refund')
    if ex is not None:
        return {'order_no': ex['order_no'], 'duplicate': True}
    o = _orig_order(db, actor, data.get('order_no'), store_code)
    sh = require_shift(db, o['store_code'], actor)
    cfg = get_config(db, mid)
    reason = (data.get('reason') or '').strip()[:100] or '顾客退货'
    plan, lines = refund_plan(db, o, data.get('items') if not exchange else data.get('back_items'))
    total = sum(x['amount'] for x in plan)
    credit = min(int(exchange['credit']), total) if exchange else 0
    rno = next_no(db, mid, 'R', o['store_code'])
    auth = {'mode': 'self', 'by': actor['username']}
    if total - credit > 0:
        auth = check_limit_or_approval(db, actor, 'refund', total - credit, data, o['store_code'], o['order_no'],
                                       '退货退款 %s 元（原单 %s）' % (yuan(total - credit), o['order_no']))
    ts = now_s()
    qty_total = Decimal(0)
    for i, x in enumerate(plan):
        r = x['row']
        alloc, back = _return_stock(db, actor, mid, o['store_code'], x, rno)
        db.execute('UPDATE order_items SET refunded_qty=COALESCE(refunded_qty,0)+?, batch_alloc=? WHERE id=?',
                   (float(x['qty']), json.dumps(alloc, ensure_ascii=False), r['id']))
        qty_total += x['qty']
        db.execute('INSERT INTO order_items(merchant_id, order_no, line_no, product_id, product_code, barcode, name, spec, unit, brand, category, qty, '
                   'retail_cents, price_cents, line_amount_cents, share_amount_cents, amount_cents, cost_cents, orig_line_id, unit_qty, unit_rate, '
                   'base_qty, batch_alloc, is_gift, quality, line_remark) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (mid, rno, i + 1, r['product_id'], r['product_code'], r['barcode'], r['name'], r['spec'], r['unit'], r['brand'], r['category'],
                    -float(x['qty']), _cents_col(r, 'retail'), _cents_col(r, 'price'), -x['line_amount'], -x['share'], -x['amount'], -x['cost'],
                    r['id'], -float(x['qty']), int(_col(r, 'unit_rate', 1) or 1), -x['base_qty'], json.dumps(back, ensure_ascii=False),
                    int(_col(r, 'is_gift', 0) or 0), x['quality'], (x.get('reason_code') or reason)[:60]))
    split = _refund_split(db, o, total - credit, data.get('refund_method_order') if isinstance(data.get('refund_method_order'), list) else None, cfg)
    if credit:
        split.insert(0, {'method': 'exchange', 'amount': credit, 'orig_pay_no': '', 'name': pay_name('exchange')})
    for p in split:
        _insert_payment(db, actor, mid, o['store_code'], rno, sh['id'], 'refund', dict(p, amount=-p['amount'], ref='退款 原单 ' + o['order_no']),
                        None, p.get('orig_pay_no', ''))
        if p['method'] == 'stored':
            if not o['member_code']:
                raise BizError('原单没有会员，无法退回储值', 409, 'CONFLICT')
            member_svc.stored_change(db, actor, mid, o['member_code'], p['amount'], '退货退回', rno, o['store_code'], '原单 ' + o['order_no'])
    blines = [{'orig_line_id': x['row']['id'], 'qty': str(x['qty']), 'amount_cents': x['amount']} for x in plan]
    try:
        ben = promo_engine.reverse_benefits(db, actor, rno, dict(o), blines)
    except NotImplementedError:
        ben = _reverse_fallback(db, actor, rno, o, plan, o['store_code'])
    ben = ben or {}
    left_qty = db.execute('SELECT COALESCE(SUM(qty-COALESCE(refunded_qty,0)),0) FROM order_items WHERE order_no=? AND merchant_id=? AND qty>0',
                          (o['order_no'], mid)).fetchone()[0]
    new_status = '已退货' if fnum(left_qty) <= 1e-9 else '部分退货'
    db.execute('UPDATE orders SET status=? WHERE order_no=? AND merchant_id=?', (new_status, o['order_no'], mid))
    auth_user = auth['by'] if auth['mode'] != 'self' else ''
    insert_order(db, dict(
        merchant_id=mid, order_no=rno, client_id=cid, biz_type='refund', store_code=o['store_code'], store_name=o['store_name'],
        terminal_no=(data.get('terminal_no') or sh['terminal_no'] or '')[:20], cashier_user=actor['username'], cashier_name=actor['name'],
        guide_code=o['guide_code'], guide_name=o['guide_name'], member_code=o['member_code'], member_phone=o['member_phone'],
        member_name=o['member_name'], member_level=o['member_level'], item_qty=-float(qty_total), payable_cents=-total, paid_total_cents=-total,
        points_earned=int(ben.get('points_restore') or 0) - int(ben.get('points_back') or 0), status='已完成', orig_order_no=o['order_no'],
        refund_reason=reason, auth_user=auth_user, shift_id=sh['id'], created_at=ts, exchange_no=exchange['no'] if exchange else '',
        approval_no=auth.get('approval_no', ''),
        snapshot=json.dumps({'refund_split': [{'method': p['method'], 'amount': p['amount']} for p in split], 'benefits': ben,
                             'quality': [(x['row']['name'], x['quality']) for x in plan]}, ensure_ascii=False, default=str)))
    audit(db, actor, 'pos', '退货' if not exchange else '换货退回', rno, {'orig_status': o['status']},
          {'orig_status': new_status, 'amount': yuan(total), 'split': [(p['method'], yuan(p['amount'])) for p in split], 'auth': auth['mode']},
          reason, level='high', store_code=o['store_code'])
    n = db.execute("SELECT COUNT(*) FROM orders WHERE merchant_id=? AND biz_type='refund' AND cashier_user=? AND substr(created_at,1,10)=?",
                   (mid, actor['username'], ts[:10])).fetchone()[0]
    if n >= REFUND_ALERT_COUNT:
        _risk(db, actor, o['store_code'], '频繁退款：%s 今日第 %d 笔退货' % (actor['name'], n), '最近一笔 %s 退 %s 元' % (rno, yuan(total)), rno,
              dedup='refund_freq:%s:%s:%d' % (actor['username'], ts[:10], n))
    return {'order_no': rno, 'refund_amount': total, 'credit': credit,
            'payments': [{'method': p['method'], 'name': p['name'], 'amount': yuan(p['amount'])} for p in split], 'benefits': ben}


def do_refund_no_receipt(db, data, actor, store_code):
    """无单退货（POS-604）：商户可禁用；默认需店长授权/审批（no_receipt_refund 额度）；同时受单日退款额度控制。"""
    mid = actor['merchant_id']
    cfg = get_config(db, mid)
    if not cfg.get('allow_no_receipt_refund', True):
        raise BizError('本商户已禁用无单退货', 403)
    cid = (data.get('client_id') or '').strip()[:64] or None
    ex = _dup_order(db, mid, cid, 'refund')
    if ex is not None:
        return {'order_no': ex['order_no'], 'duplicate': True}
    sh = require_shift(db, store_code, actor)
    reason = (data.get('reason') or '').strip()[:100]
    if not reason:
        raise BizError('无单退货必须填写原因')
    method = (data.get('refund_method') or 'cash').strip()
    if method not in PAY_NAME:
        raise BizError('退款方式不正确：%s' % method)
    mcode = (data.get('member_code') or '').strip()
    if method == 'stored' and not mcode:
        raise BizError('退回储值需要选择会员')
    if mcode and db.execute('SELECT 1 FROM members WHERE merchant_id=? AND code=?', (mid, mcode)).fetchone() is None:
        raise BizError('会员不存在')
    items = data.get('items') or []
    if not items:
        raise BizError('请添加要退的商品')
    rows = []
    for it in items:
        res = resolve_unit(db, mid, str(it.get('code') or '').strip(), it.get('unit'))
        q = dec_qty(it.get('qty', 1))
        if q <= 0:
            raise BizError('数量必须大于 0')
        price = cents_of(it.get('price'), '退货单价')
        if price < 0:
            raise BizError('退货单价不能为负')
        rc, mc = unit_prices(res)
        if rc and price > rc:
            raise BizError('%s 退货单价不能高于零售价 %s' % (res['product']['name'], yuan(rc)))
        qual = (it.get('quality') or 'ok').strip()
        if qual not in QUALITY_NAME:
            raise BizError('质量状态不正确：%s' % qual)
        amt = int((Decimal(price) * q).to_integral_value())
        rows.append({'res': res, 'qty': q, 'base': inventory.to_base_qty(q, res['rate']), 'price': price, 'amount': amt, 'quality': qual})
    total = sum(x['amount'] for x in rows)
    rno = next_no(db, mid, 'R', store_code)
    a1 = check_limit_or_approval(db, actor, 'no_receipt_refund', None, data, store_code, rno, '无单退货 %s 元：%s' % (yuan(total), reason))
    a2 = check_limit_or_approval(db, actor, 'refund', total, dict(data, approval_no='') if a1['mode'] == 'approval' else data, store_code, rno,
                                 '无单退货 %s 元' % yuan(total))
    st = store_row(db, store_code, mid) or {}
    for i, x in enumerate(rows):
        p = x['res']['product']
        inventory.stock_in(db, actor, mid, store_code, [{'product_id': p['id'], 'base_qty': x['base'], 'quality': x['quality'],
                                                         'unit': x['res']['unit'], 'unit_qty': float(x['qty']), 'rate': x['res']['rate'],
                                                         'note': '无单退货'}], 'refund', rno)
        db.execute('INSERT INTO order_items(merchant_id, order_no, line_no, product_id, product_code, barcode, name, spec, unit, brand, category, qty, '
                   'retail_cents, price_cents, line_amount_cents, share_amount_cents, amount_cents, cost_cents, unit_qty, unit_rate, base_qty, '
                   'batch_alloc, quality, line_remark) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (mid, rno, i + 1, p['id'], p['code'], x['res']['barcode'] or first_barcode(db, p['id']), p['name'], p.get('spec', ''),
                    x['res']['unit'], p.get('brand', ''), p.get('category', ''), -float(x['qty']), unit_prices(x['res'])[0], x['price'], -x['amount'], 0,
                    -x['amount'], -int(p.get('cost_cents') or 0) * x['base'], -float(x['qty']), x['res']['rate'], -x['base'],
                    json.dumps([{'product_id': p['id'], 'batch_id': 0, 'qty': x['base'], 'quality': x['quality']}]), x['quality'], reason[:60]))
    if total:
        _insert_payment(db, actor, mid, store_code, rno, sh['id'], 'refund', {'method': method, 'amount': -total, 'ref': '无单退货',
                                                                              'name': pay_name(method, data.get('refund_method_name'))})
        if method == 'stored':
            member_svc.stored_change(db, actor, mid, mcode, total, '退货退回', rno, store_code, '无单退货')
    by = ','.join(sorted(set(a['by'] for a in (a1, a2) if a['mode'] != 'self')))
    insert_order(db, dict(
        merchant_id=mid, order_no=rno, client_id=cid, biz_type='refund', store_code=store_code, store_name=st.get('name', ''),
        terminal_no=sh['terminal_no'] or '', cashier_user=actor['username'], cashier_name=actor['name'], member_code=mcode,
        item_qty=-float(sum(x['qty'] for x in rows)), payable_cents=-total, paid_total_cents=-total, status='已完成', refund_reason=reason,
        auth_user=by, shift_id=sh['id'], created_at=now_s(), no_receipt=1,
        approval_no=','.join(a.get('approval_no', '') for a in (a1, a2) if a.get('approval_no')),
        snapshot=json.dumps({'no_receipt': True, 'method': method}, ensure_ascii=False)))
    audit(db, actor, 'pos', '无单退货', rno, None, {'amount': yuan(total), 'method': method, 'auth': by or '本人'}, reason, level='high',
          store_code=store_code)
    _risk(db, actor, store_code, '无单退货 %s 元' % yuan(total), '%s，原因：%s，授权：%s' % (rno, reason, by or actor['name']), rno)
    return {'order_no': rno, 'refund_amount': total, 'payments': [{'method': method, 'name': pay_name(method), 'amount': yuan(total)}]}


def do_exchange(db, data, actor, store_code):
    """换货（POS-605）：一张换货单 X 关联退货单 R（换回）+ 销售单 S（换出），差价按支付补收或原路退回；库存与流水分别记录。"""
    mid = actor['merchant_id']
    cid = (data.get('client_id') or '').strip()[:64] or None
    ex = _dup_order(db, mid, cid, 'exchange')
    if ex is not None:
        return {'order_no': ex['order_no'], 'duplicate': True}
    o = _orig_order(db, actor, data.get('order_no'), store_code)
    require_shift(db, o['store_code'], actor)
    if not data.get('back_items'):
        raise BizError('请选择换回的商品')
    if not data.get('new_items'):
        raise BizError('请扫描换出的商品')
    plan, _ = refund_plan(db, o, data.get('back_items'))
    back_total = sum(x['amount'] for x in plan)
    xno = next_no(db, mid, 'X', o['store_code'])
    sale_in = dict(data, items=data.get('new_items'), member_code=data.get('member_code') or o['member_code'] or None)
    for k in ('coupon_nos', 'coupon_no', 'points_used'):
        sale_in.pop(k, None)
    s = do_checkout(db, sale_in, o['store_code'], actor, exchange={'no': xno, 'credit': back_total})
    new_total = int(s['payable'])
    credit = min(back_total, new_total)
    r = do_refund(db, dict(data, back_items=data.get('back_items')), actor, o['store_code'], exchange={'no': xno, 'credit': credit})
    st = store_row(db, o['store_code'], mid) or {}
    sh = require_shift(db, o['store_code'], actor)
    insert_order(db, dict(
        merchant_id=mid, order_no=xno, client_id=cid, biz_type='exchange', store_code=o['store_code'], store_name=st.get('name', ''),
        terminal_no=sh['terminal_no'] or '', cashier_user=actor['username'], cashier_name=actor['name'], member_code=o['member_code'],
        member_phone=o['member_phone'], member_name=o['member_name'], member_level=o['member_level'], status='已完成',
        orig_order_no=o['order_no'], refund_reason=(data.get('reason') or '换货')[:100], shift_id=sh['id'], created_at=now_s(), exchange_no=xno,
        snapshot=json.dumps({'refund_no': r['order_no'], 'sale_no': s['order_no'], 'back_cents': back_total, 'new_cents': new_total,
                             'diff_cents': new_total - back_total}, ensure_ascii=False)))
    audit(db, actor, 'pos', '换货', xno, None, {'orig': o['order_no'], 'refund_no': r['order_no'], 'sale_no': s['order_no'],
                                               'diff': yuan(new_total - back_total)}, level='high', store_code=o['store_code'])
    return {'order_no': xno, 'refund_no': r['order_no'], 'sale_no': s['order_no'], 'back_amount': back_total, 'new_amount': new_total,
            'diff': new_total - back_total, 'change': s['change'], 'refund_payments': r['payments']}


# ================================================================ 储值充值
def do_recharge(db, data, store_code, actor, need_shift=True):
    mid = int(actor['merchant_id'])
    cid = (data.get('client_id') or '').strip()[:64] or None
    code = (data.get('member_code') or '').strip()
    if cid:
        ex = db.execute("SELECT * FROM payments WHERE merchant_id=? AND request_no=? AND kind='recharge'", (mid, cid)).fetchone()
        if ex is not None:
            lg = db.execute('SELECT * FROM stored_log WHERE merchant_id=? AND order_no=? ORDER BY id LIMIT 1', (mid, ex['order_no'])).fetchone()
            return {'txn_no': ex['order_no'], 'amount': yuan(_cents_col(ex, 'amount')), 'gift': yuan(int(_col(lg, 'gift_cents', 0) or 0)) if lg else 0,
                    'balance': yuan(int(_col(lg, 'balance_cents', 0) or 0)) if lg else None, 'points': 0, 'duplicate': True}
    if db.execute('SELECT 1 FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone() is None:
        raise BizError('会员不存在')
    amount = cents_of(data.get('amount'), '充值金额')
    method = (data.get('method') or 'wechat').strip()
    if amount <= 0 or amount > 10000000:
        raise BizError('充值金额必须在 0.01 到 100000 之间')
    if method not in PAY_NAME or method in ('stored', 'other'):
        raise BizError('充值支付方式不合法')
    sh = require_shift(db, store_code, actor) if need_shift else None
    cfg = get_config(db, mid)
    gift = 0
    for r in sorted(cfg.get('recharge_rules') or [], key=lambda x: -fnum(x.get('threshold'))):
        if amount >= cents_of(r.get('threshold') or 0):
            gift = cents_of(r.get('gift') or 0)
            break
    no = next_no(db, mid, 'CZ', store_code)
    bal = member_svc.stored_change(db, actor, mid, code, amount + gift, '充值', no, store_code, '本金 %s 赠送 %s' % (yuan(amount), yuan(gift)),
                                   principal_cents=amount, gift_cents=gift)
    _insert_payment(db, actor, mid, store_code, no, sh['id'] if sh else None, 'recharge', {'method': method, 'amount': amount, 'ref': '储值充值'}, cid)
    audit(db, actor, 'pos', '储值充值', no, None, {'member': code, 'amount': yuan(amount), 'gift': yuan(gift), 'method': method}, store_code=store_code)
    return {'txn_no': no, 'amount': yuan(amount), 'gift': yuan(gift), 'balance': yuan(bal), 'points': 0}


# ================================================================ 查询
def order_scope_check(db, actor, o):
    if o['store_code'] not in scope_stores(db, actor):
        raise BizError('无权查看其他门店单据', 403, 'SCOPE_DENIED')
    if actor.get('data_scope') == 'self' and o['cashier_user'] != actor['username']:
        raise BizError('只能查看本人收银的单据', 403, 'SCOPE_DENIED')


def order_detail(db, order_no, actor=None, mid=None):
    """单据详情（含行、批次、优惠明细、支付、已退情况、可退数量金额）。actor 给出时做字段权限（成本、手机号）。"""
    mid = _mid(mid if mid is not None else (actor or {}).get('merchant_id'))
    o = db.execute('SELECT * FROM orders WHERE order_no=? AND merchant_id=?', (order_no, mid)).fetchone()
    if o is None:
        return None
    d = dict(o)
    for c in ('retail_total', 'discount_total', 'order_discount', 'coupon_amount', 'points_amount', 'round_off', 'payable', 'paid_total',
              'change_amount'):
        d[c] = yuan(_cents_col(o, c))
        d.pop(c + '_cents', None)
    d['snapshot'] = _json_load(d.get('snapshot'), {})
    if actor is not None:
        d['member_phone'] = mask_phone(o['member_phone'], actor)
    items = []
    for x in db.execute('SELECT * FROM order_items WHERE order_no=? AND merchant_id=? ORDER BY line_no', (order_no, mid)):
        it = dict(x)
        for c in ('retail', 'price', 'line_amount', 'share_amount', 'amount'):
            it[c] = yuan(_cents_col(x, c))
            it.pop(c + '_cents', None)
        cc = _cents_col(x, 'cost')
        it.pop('cost_cents', None)
        it['cost'] = cost_view(actor, cc) if actor is not None else yuan(cc)
        it['qty'] = qty_out(x['qty'])
        it['refunded_qty'] = qty_out(x['refunded_qty'] or 0)
        it['batch_alloc'] = _json_load(x['batch_alloc'] if 'batch_alloc' in x.keys() else '', [])
        it['promo_ref'] = _json_load(x['promo_ref'] if 'promo_ref' in x.keys() else '', [])
        if o['biz_type'] == 'sale' and float(x['qty'] or 0) > 0:
            ra = _refunded_cents(db, x['id'])[0]
            it['refundable_qty'] = qty_out(dec_qty(x['qty']) - dec_qty(x['refunded_qty'] or 0))
            it['refundable_amount'] = yuan(_cents_col(x, 'amount') - ra)
            it['refunded_amount'] = yuan(ra)
        items.append(it)
    d['items'] = items
    d['discounts'] = [dict(r, amount=yuan(r['amount_cents'])) for r in db.execute(
        'SELECT kind, line_no, source_no, source_name, amount_cents, auth_user, approval_no FROM order_discounts WHERE order_no=? AND merchant_id=? '
        'ORDER BY id', (order_no, mid))]
    d['payments'] = [{'pay_no': p['pay_no'] or '', 'method': p['method'], 'method_name': p['method_name'], 'amount': yuan(_cents_col(p, 'amount')),
                      'ref': p['ref'] or '', 'status': p['status'] or 'success', 'confirm_mode': p['confirm_mode'] or 'manual',
                      'channel_txn_no': p['channel_txn_no'] or '', 'paid_at': p['paid_at'] or p['created_at'], 'orig_pay_no': p['orig_pay_no'] or '',
                      'kind': p['kind']} for p in db.execute('SELECT * FROM payments WHERE order_no=? AND merchant_id=? ORDER BY id', (order_no, mid))]
    d['refunds'] = [dict(r, payable=yuan(r['payable_cents'])) for r in db.execute(
        "SELECT order_no, payable_cents, created_at, cashier_name, refund_reason, exchange_no FROM orders WHERE merchant_id=? AND orig_order_no=? "
        "AND biz_type='refund' ORDER BY created_at, id", (mid, order_no))]
    d['refundable_amount'] = yuan(sum(_cents_col(x, 'amount') - _refunded_cents(db, x['id'])[0] for x in db.execute(
        'SELECT * FROM order_items WHERE order_no=? AND merchant_id=? AND qty>0', (order_no, mid)))) if o['biz_type'] == 'sale' else 0
    d['print_count'] = db.execute('SELECT COUNT(*) FROM print_logs WHERE merchant_id=? AND doc_no=?', (mid, order_no)).fetchone()[0]
    return d


def order_query(db, actor, args):
    """订单列表（收银台/后台共用）。门店参数只在数据范围内生效（范围外的门店参数忽略，按本人范围查）；data_scope=self 只看本人。"""
    from .util import page_args, pages_of
    page, size = page_args(args, 20, 200)
    mid = actor['merchant_id']
    allowed = scope_stores(db, actor)
    req = [s.strip() for s in (args.get('store') or '').split(',') if s.strip()]
    stores = [s for s in req if s in allowed] or allowed
    if not stores:
        return {'ok': True, 'total': 0, 'page': page, 'pageSize': size, 'pages': 1, 'sale_amount': 0, 'refund_amount': 0, 'rows': [],
                'data': {'page': page, 'size': size, 'total': 0, 'items': []}}
    clauses, p = ['merchant_id=?', 'store_code IN (%s)' % ','.join('?' * len(stores))], [mid] + stores
    if actor.get('data_scope') == 'self' or (args.get('mine') or '') == '1':
        clauses.append('cashier_user=?')
        p.append(actor['username'])
    for k, c in (('start', 'substr(created_at,1,10) >= ?'), ('end', 'substr(created_at,1,10) <= ?')):
        v = (args.get(k) or '').strip()
        if v:
            clauses.append(c)
            p.append(v)
    t = (args.get('type') or '').strip()
    if t in ('sale', 'refund', 'exchange'):
        clauses.append('biz_type=?')
        p.append(t)
    kw = (args.get('kw') or '').strip()
    if kw:
        clauses.append('(order_no LIKE ? OR member_phone LIKE ? OR member_name LIKE ? OR cashier_name LIKE ? OR member_code = ?)')
        p += ['%%%s%%' % kw] * 4 + [kw]
    cashier = (args.get('cashier') or '').strip()
    if cashier:
        clauses.append('cashier_user=?')
        p.append(cashier)
    where = ' WHERE ' + ' AND '.join(clauses)
    total = db.execute('SELECT COUNT(*) FROM orders' + where, p).fetchone()[0]
    sums = db.execute("SELECT COALESCE(SUM(CASE WHEN biz_type='sale' THEN payable_cents END),0), "
                      "COALESCE(SUM(CASE WHEN biz_type='refund' THEN payable_cents END),0) FROM orders" + where, p).fetchone()
    rows = db.execute('SELECT * FROM orders' + where + ' ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?', p + [size, (page - 1) * size]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for c in ('retail_total', 'discount_total', 'order_discount', 'coupon_amount', 'points_amount', 'round_off', 'payable', 'paid_total',
                  'change_amount'):
            d[c] = yuan(_cents_col(r, c))
            d.pop(c + '_cents', None)
        d.pop('snapshot', None)
        d['member_phone'] = mask_phone(r['member_phone'], actor)
        d['pay_desc'] = ' '.join('%s%s' % (x[0], yuan(int(x[1] or 0))) for x in db.execute(
            'SELECT method_name, amount_cents FROM payments WHERE order_no=? AND merchant_id=? ORDER BY id', (r['order_no'], mid)))
        out.append(d)
    return {'ok': True, 'total': total, 'page': page, 'pageSize': size, 'pages': pages_of(total, size),
            'sale_amount': yuan(int(sums[0] or 0)), 'refund_amount': yuan(int(sums[1] or 0)), 'rows': out,
            'data': {'page': page, 'size': size, 'total': total, 'items': out}}
