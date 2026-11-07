# -*- coding: utf-8 -*-
"""手机端 H5 接口 /api/m/*。Owner：模块⑤。规格：APP-001~005、APP-101~105、APP-201、APP-301~304、APP-401~405、AC-013（APP 侧）。

口径与约定：
- 一律按 merchant_id + 数据范围（scope_stores）过滤；门店切换 store=编码(可逗号多个) / region=区域编码；越权 403 SCOPE_DENIED（APP-005）。
- 金额 = 元（money.yuan）；毛利/成本只在 /api/m/profit（需 report.profit），看板/排行/门店接口不返回任何成本相关字段（APP-003）。
- 会员手机号按 member.phone.view 脱敏；储值余额按 member.stored.view（APP-403）。
- 库存类操作（要货/调拨收货/报损报溢/盘点录入）前端直接调模块①的 api/stock/*，本文件不写库存。
- 旧 v1 接口（/m/overview /m/stock /m/member /expiry）已在集成轮删除（看板 /m/dashboard、商品 /m/goods、会员走②的 api/member/*）。
"""
import hashlib
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, session

from .core import (api, require_perm, require_platform, get_db, current_actor,
                   scope_stores, has_perm, mask_phone, cost_view, ok, tx, audit, _load_actor)
from .money import yuan
from .util import BizError, fnum, inum, now_s
from . import notify

bp = Blueprint('mobile', __name__, url_prefix='/api')

M_VERSION = '3.0.0'          # 与 static/m-app.js 里的 APP_VERSION 保持一致
CALIBER = [
    '销售额 = 已支付销售单应收合计（含储值支付，不含充值）；退款 = 退货单退款合计；净额 = 销售额 − 退款。',
    '订单数 = 销售单数（不含退货单）；客单价 = 销售额 ÷ 订单数。',
    '日期按下单时间（服务器北京时间）；周 = 周一至周日；月 = 自然月；本期未结束时与上期相同天数对比。',
]


# ================================================================ 公共：数据范围、日期区间、销售汇总
def _region_stores(db, mid, region_code):
    r = db.execute("SELECT id, name FROM org_units WHERE merchant_id=? AND code=?", (mid, region_code)).fetchone()
    if r is None:
        raise BizError('区域不存在', 404, 'NOT_FOUND')
    return r['name'], [x[0] for x in db.execute('SELECT code FROM stores WHERE merchant_id=? AND region_id=? ORDER BY sort, code', (mid, r['id']))]


def resolve_scope(db, actor, store=None, region=None):
    """APP-005：返回 (门店编码列表, 范围名称)。store/region 都空 = 本人全部可见门店（总部=全商户汇总）。"""
    store = (store if store is not None else request.args.get('store') or '').strip()
    region = (region if region is not None else request.args.get('region') or '').strip()
    if store:
        codes = scope_stores(db, actor, store)
        names = [r[0] for r in db.execute('SELECT name FROM stores WHERE merchant_id=? AND code IN (%s) ORDER BY sort, code'
                                          % ','.join('?' * len(codes)), [actor['merchant_id']] + codes)]
        return codes, '、'.join(names) or store
    allowed = scope_stores(db, actor)
    if region:
        name, codes = _region_stores(db, actor['merchant_id'], region)
        bad = [c for c in codes if c not in allowed]
        if bad or not codes:
            raise BizError('无权查看该区域（或区域下没有门店）', 403, 'SCOPE_DENIED')
        return codes, name
    if actor['data_scope'] == 'merchant':
        return allowed, '全部门店（总部汇总）'
    if len(allowed) == 1:
        return allowed, _store_name(db, actor['merchant_id'], allowed[0])
    return allowed, '我的门店（%d 家）' % len(allowed)


def _store_name(db, mid, code):
    r = db.execute('SELECT name FROM stores WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
    return r[0] if r else code


def _in(codes):
    return ','.join('?' * len(codes)) if codes else "''"


def _d(s, default=None):
    s = (s or '').strip()[:10]
    if not s:
        return default or datetime.now().date()
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        raise BizError('日期格式应为 YYYY-MM-DD')


def period_range(day, period):
    """(start, end) 含首尾的 date。week = 周一~周日；month = 自然月。"""
    if period == 'week':
        s = day - timedelta(days=day.weekday())
        return s, s + timedelta(days=6)
    if period == 'month':
        s = day.replace(day=1)
        nxt = (s.replace(day=28) + timedelta(days=4)).replace(day=1)
        return s, nxt - timedelta(days=1)
    return day, day


def compare_ranges(day, period):
    """本期 + 对比期。日：昨日、上周同日；周：上周；月：上月。本期未结束时截到今天，并与上期相同天数对比。"""
    s, e = period_range(day, period)
    today = datetime.now().date()
    e_eff = min(e, max(today, s))
    n = (e_eff - s).days
    out = {'current': (s, e_eff)}
    if period == 'day':
        out['prev'] = (s - timedelta(days=1), s - timedelta(days=1))
        out['same'] = (s - timedelta(days=7), s - timedelta(days=7))
    elif period == 'week':
        out['prev'] = (s - timedelta(days=7), s - timedelta(days=7) + timedelta(days=n))
    else:
        ps = (s - timedelta(days=1)).replace(day=1)
        pe_full = s - timedelta(days=1)
        out['prev'] = (ps, min(pe_full, ps + timedelta(days=n)))
    return out


COMPARE_LABEL = {'day': {'prev': '较昨日', 'same': '较上周同期'}, 'week': {'prev': '较上周同期'}, 'month': {'prev': '较上月同期'}}


def _ts(d, end=False):
    return (d + timedelta(days=1)).strftime('%Y-%m-%d') + ' 00:00:00' if end else d.strftime('%Y-%m-%d') + ' 00:00:00'


PAID = " AND COALESCE(o.pay_status,'paid')='paid'"


def sales_stats(db, mid, codes, start, end):
    """[start, end] 两端含。返回分 + 计数（不含成本）。"""
    if not codes:
        return {'sales': 0.0, 'orders': 0, 'avg': 0.0, 'refund': 0.0, 'refund_orders': 0, 'net': 0.0, 'items': 0, 'members': 0}
    r = db.execute("SELECT COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.payable_cents END),0), COUNT(CASE WHEN o.biz_type='sale' THEN 1 END), "
                   "COALESCE(-SUM(CASE WHEN o.biz_type='refund' THEN o.payable_cents END),0), COUNT(CASE WHEN o.biz_type='refund' THEN 1 END), "
                   "COALESCE(SUM(o.payable_cents),0), COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.item_qty END),0), "
                   "COUNT(DISTINCT CASE WHEN o.biz_type='sale' AND COALESCE(o.member_code,'')!='' THEN o.member_code END) "
                   "FROM orders o WHERE o.merchant_id=? AND o.store_code IN (%s) AND o.created_at>=? AND o.created_at<?" % _in(codes) + PAID,
                   [mid] + codes + [_ts(start), _ts(end, True)]).fetchone()
    sale, cnt = int(r[0] or 0), int(r[1] or 0)
    return {'sales': yuan(sale), 'orders': cnt, 'avg': yuan((sale * 2 + cnt) // (2 * cnt)) if cnt else 0.0,
            'refund': yuan(int(r[2] or 0)), 'refund_orders': int(r[3] or 0), 'net': yuan(int(r[4] or 0)),
            'items': round(fnum(r[5]), 3), 'members': int(r[6] or 0)}


def _pct(cur, base):
    if not base:
        return None
    return round((cur - base) * 100.0 / abs(base), 1)


def _latest(db, mid, codes):
    if not codes:
        return ''
    return db.execute('SELECT MAX(created_at) FROM orders WHERE merchant_id=? AND store_code IN (%s)' % _in(codes), [mid] + codes).fetchone()[0] or ''


def _period():
    p = request.args.get('period') or 'day'
    if p not in ('day', 'week', 'month'):
        raise BizError('period 只能是 day / week / month')
    return p


# ================================================================ APP-001 经营概览
@bp.route('/m/dashboard')
@require_perm('app.dashboard')
def dashboard():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    codes, label = resolve_scope(db, a)
    period = _period()
    day = _d(request.args.get('date'))
    rg = compare_ranges(day, period)
    data = {'scope': {'label': label, 'stores': codes, 'store_count': len(codes)}, 'period': period, 'date': day.isoformat(),
            'range': {'start': rg['current'][0].isoformat(), 'end': rg['current'][1].isoformat()}, 'compare': {}}
    cur = sales_stats(db, mid, codes, *rg['current'])
    data['current'] = cur
    for k in ('prev', 'same'):
        if k in rg:
            st = sales_stats(db, mid, codes, *rg[k])
            data['compare'][k] = {'label': COMPARE_LABEL[period][k], 'start': rg[k][0].isoformat(), 'end': rg[k][1].isoformat(), 'stats': st,
                                  'pct': dict((f, _pct(cur[f], st[f])) for f in ('sales', 'orders', 'avg', 'refund', 'net'))}
    data['updated_at'] = now_s()
    data['latest_order_at'] = _latest(db, mid, codes)
    data['caliber'] = CALIBER
    data['can_profit'] = has_perm(a, 'report.profit')
    return ok(data)


# ================================================================ APP-002 趋势与排行
@bp.route('/m/trend')
@require_perm('app.dashboard')
def trend():
    """period=day：近 14 天每日 + 所选日 24 小时；week：近 12 周；month：近 12 个月。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    codes, label = resolve_scope(db, a)
    period = _period()
    day = _d(request.args.get('date'))
    buckets = []
    if period == 'day':
        for i in range(13, -1, -1):
            d = day - timedelta(days=i)
            buckets.append((d.strftime('%m-%d'), d, d))
    elif period == 'week':
        s0, _ = period_range(day, 'week')
        for i in range(11, -1, -1):
            s = s0 - timedelta(days=7 * i)
            buckets.append((s.strftime('%m-%d') + '周', s, s + timedelta(days=6)))
    else:
        s = day.replace(day=1)
        months = []
        for _ in range(12):
            months.append(s)
            s = (s - timedelta(days=1)).replace(day=1)
        for m in reversed(months):
            buckets.append((m.strftime('%Y-%m'), m, period_range(m, 'month')[1]))
    lo, hi = buckets[0][1], buckets[-1][2]
    daily = {}
    if codes:
        for r in db.execute("SELECT substr(o.created_at,1,10) d, COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.payable_cents END),0), "
                            "COUNT(CASE WHEN o.biz_type='sale' THEN 1 END), COALESCE(-SUM(CASE WHEN o.biz_type='refund' THEN o.payable_cents END),0) "
                            "FROM orders o WHERE o.merchant_id=? AND o.store_code IN (%s) AND o.created_at>=? AND o.created_at<?" % _in(codes)
                            + PAID + " GROUP BY d", [mid] + codes + [_ts(lo), _ts(hi, True)]):
            daily[r[0]] = (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0))
    points = []
    for name, s, e in buckets:
        tot = [0, 0, 0]
        for dk, v in daily.items():
            if s.isoformat() <= dk <= e.isoformat():
                tot = [tot[0] + v[0], tot[1] + v[1], tot[2] + v[2]]
        points.append({'label': name, 'start': s.isoformat(), 'end': e.isoformat(), 'sales': yuan(tot[0]), 'orders': tot[1], 'refund': yuan(tot[2])})
    out = {'scope': {'label': label, 'stores': codes}, 'period': period, 'points': points}
    if period == 'day':
        hours = [0] * 24
        if codes:
            for r in db.execute("SELECT CAST(substr(o.created_at,12,2) AS INTEGER) h, COALESCE(SUM(o.payable_cents),0) FROM orders o "
                                "WHERE o.merchant_id=? AND o.store_code IN (%s) AND o.created_at>=? AND o.created_at<?" % _in(codes) + PAID
                                + " GROUP BY h", [mid] + codes + [_ts(day), _ts(day, True)]):
                if r[0] is not None and 0 <= r[0] < 24:
                    hours[r[0]] = yuan(int(r[1] or 0))
        out['hours'] = hours
    out['updated_at'] = now_s()
    return ok(out)


@bp.route('/m/rank')
@require_perm('app.dashboard')
def rank():
    """dim=store|product|category|brand；按净销售额（销售 − 退货）排行，附占比。不返回成本/毛利。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    codes, label = resolve_scope(db, a)
    dim = request.args.get('dim') or 'store'
    if dim not in ('store', 'product', 'category', 'brand'):
        raise BizError('dim 只能是 store / product / category / brand')
    period = _period()
    s, e = compare_ranges(_d(request.args.get('date')), period)['current']
    limit = min(100, max(1, inum(request.args.get('limit'), 20)))
    items = []
    if codes:
        args = [mid] + codes + [_ts(s), _ts(e, True)]
        if dim == 'store':
            rows = db.execute("SELECT o.store_code k, o.store_code, COALESCE(SUM(o.payable_cents),0) amt, "
                              "COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.item_qty END),0) q, COUNT(CASE WHEN o.biz_type='sale' THEN 1 END) n "
                              "FROM orders o WHERE o.merchant_id=? AND o.store_code IN (%s) AND o.created_at>=? AND o.created_at<?" % _in(codes)
                              + PAID + " GROUP BY o.store_code ORDER BY amt DESC LIMIT ?", args + [limit]).fetchall()
            names = dict(db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)).fetchall())
            items = [{'key': r[0], 'name': names.get(r[0], r[0]), 'sales': int(r[2] or 0), 'qty': round(fnum(r[3]), 3), 'orders': r[4]} for r in rows]
        else:
            col = {'product': 'i.product_code', 'category': "COALESCE(NULLIF(i.category,''),'其他')", 'brand': "COALESCE(NULLIF(i.brand,''),'其他')"}[dim]
            name = 'MAX(i.name)' if dim == 'product' else col
            rows = db.execute("SELECT %s k, %s nm, COALESCE(SUM(i.amount_cents),0) amt, COALESCE(SUM(i.qty),0) q, COUNT(DISTINCT CASE WHEN o.biz_type='sale' "
                              "THEN o.order_no END) n FROM order_items i JOIN orders o ON o.order_no=i.order_no AND o.merchant_id=i.merchant_id "
                              "WHERE o.merchant_id=? AND o.store_code IN (%s) AND o.created_at>=? AND o.created_at<?" % (col, name, _in(codes))
                              + PAID + " GROUP BY k ORDER BY amt DESC LIMIT ?", args + [limit]).fetchall()
            items = [{'key': r[0], 'name': r[1] or r[0], 'sales': int(r[2] or 0), 'qty': round(fnum(r[3]), 3), 'orders': r[4]} for r in rows]
    total = sum(x['sales'] for x in items) or 0
    for x in items:
        x['share'] = round(x['sales'] * 100.0 / total, 1) if total > 0 else 0.0
        x['sales'] = yuan(x['sales'])
    return ok({'scope': {'label': label, 'stores': codes}, 'dim': dim, 'period': period, 'range': {'start': s.isoformat(), 'end': e.isoformat()},
               'items': items, 'updated_at': now_s()})


# ================================================================ APP-003 毛利与异常（需 report.profit）
@bp.route('/m/profit')
@require_perm('app.dashboard')
def profit():
    db = get_db()
    a = current_actor()
    if not has_perm(a, 'report.profit'):
        raise BizError('没有毛利查看权限（需要：report.profit）', 403, 'FORBIDDEN')
    mid = a['merchant_id']
    codes, label = resolve_scope(db, a)
    period = _period()
    s, e = compare_ranges(_d(request.args.get('date')), period)['current']
    try:
        threshold = float(request.args.get('threshold') or 0.8)
    except ValueError:
        raise BizError('threshold 应为 0~1 的小数')
    threshold = min(1.0, max(0.1, threshold))
    out = {'scope': {'label': label, 'stores': codes}, 'period': period, 'range': {'start': s.isoformat(), 'end': e.isoformat()},
           'sales': 0.0, 'cost': 0.0, 'gross': 0.0, 'margin': None, 'refund': 0.0, 'discount': 0.0, 'discounts': [], 'abnormal': []}
    if codes:
        args = [mid] + codes + [_ts(s), _ts(e, True)]
        where = " WHERE o.merchant_id=? AND o.store_code IN (%s) AND o.created_at>=? AND o.created_at<?" % _in(codes) + PAID
        r = db.execute("SELECT COALESCE(SUM(i.amount_cents),0), COALESCE(SUM(i.cost_cents),0) FROM order_items i "
                       "JOIN orders o ON o.order_no=i.order_no AND o.merchant_id=i.merchant_id" + where, args).fetchone()
        amt, cost = int(r[0] or 0), int(r[1] or 0)
        out.update(sales=yuan(amt), cost=cost_view(a, cost) if has_perm(a, 'goods.cost.view') else None, gross=yuan(amt - cost),
                   margin=round((amt - cost) * 100.0 / amt, 1) if amt > 0 else None)
        rf = db.execute("SELECT COALESCE(-SUM(o.payable_cents),0), COUNT(*) FROM orders o" + where + " AND o.biz_type='refund'", args).fetchone()
        out['refund'], out['refund_orders'] = yuan(int(rf[0] or 0)), rf[1]
        dsum = db.execute("SELECT COALESCE(SUM(o.discount_total_cents),0) FROM orders o" + where + " AND o.biz_type='sale'", args).fetchone()[0]
        out['discount'] = yuan(int(dsum or 0))
        out['discounts'] = [{'kind': x[0], 'amount': yuan(int(x[1] or 0)), 'count': x[2]} for x in db.execute(
            "SELECT d.kind, SUM(d.amount_cents), COUNT(*) FROM order_discounts d JOIN orders o ON o.order_no=d.order_no AND o.merchant_id=d.merchant_id"
            + where + " GROUP BY d.kind ORDER BY 2 DESC", args)]
        # 异常改价：成交价低于零售价 threshold 倍（含会员价以外的手工改价/大额单品折扣）
        out['abnormal'] = [{'order_no': x[0], 'store_code': x[1], 'cashier': x[2], 'created_at': x[3], 'name': x[4], 'qty': x[5],
                            'retail': yuan(x[6]), 'price': yuan(x[7]), 'rate': round(x[7] * 1.0 / x[6], 2) if x[6] else None, 'auth_user': x[8] or ''}
                           for x in db.execute(
            "SELECT o.order_no, o.store_code, o.cashier_name, o.created_at, i.name, i.qty, i.retail_cents, i.price_cents, o.auth_user "
            "FROM order_items i JOIN orders o ON o.order_no=i.order_no AND o.merchant_id=i.merchant_id" + where +
            " AND o.biz_type='sale' AND i.qty>0 AND COALESCE(i.is_gift,0)=0 AND i.retail_cents>0 AND i.price_cents < i.retail_cents * ? "
            "ORDER BY (i.retail_cents - i.price_cents) * i.qty DESC LIMIT 30", args + [threshold])]
    out['threshold'] = threshold
    out['updated_at'] = now_s()
    return ok(out)


# ================================================================ APP-004 库存与会员概览
def _wh_alerts(db, mid, codes, kind, days=60, slow_days=30, limit=100):
    """kind: stock_low / near_expiry / expired / slow。读 inv_stock / inv_batches / stock_alert_rules（只读）。"""
    if not codes:
        return []
    today = datetime.now().date().isoformat()
    names = dict(db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)).fetchall())
    args = [mid] + codes
    if kind in ('near_expiry', 'expired'):
        cond = "b.expiry_date != '' AND b.expiry_date < ?" if kind == 'expired' else "b.expiry_date >= ? AND b.expiry_date <= date(?, '+%d day')" % int(days)
        cargs = [today] if kind == 'expired' else [today, today]
        rows = db.execute("SELECT s.wh_code, p.code, p.name, b.batch_no, b.expiry_date, b.status, SUM(s.qty) q, "
                          "CAST(julianday(b.expiry_date) - julianday(?) AS INTEGER) left_days FROM inv_stock s "
                          "JOIN inv_batches b ON b.id=s.batch_id AND b.merchant_id=s.merchant_id JOIN products p ON p.id=s.product_id "
                          "WHERE s.merchant_id=? AND s.wh_code IN (%s) AND s.qty>0 AND s.quality='ok' AND " % _in(codes) + cond +
                          " GROUP BY s.wh_code, s.product_id, s.batch_id ORDER BY b.expiry_date LIMIT ?",
                          [today] + args + cargs + [limit]).fetchall()
        return [{'store_code': r[0], 'store_name': names.get(r[0], r[0]), 'code': r[1], 'name': r[2], 'batch_no': r[3], 'expiry_date': r[4],
                 'batch_status': r[5], 'qty': r[6], 'days_left': r[7]} for r in rows]
    if kind == 'stock_low':
        rule = db.execute("SELECT safety_qty FROM stock_alert_rules WHERE merchant_id=? AND target_type='merchant'", (mid,)).fetchone()
        base = int(rule[0] or 0) if rule and rule[0] is not None else 0
        rows = db.execute("SELECT s.wh_code, p.id, p.code, p.name, SUM(CASE WHEN s.quality='ok' THEN s.qty ELSE 0 END) q, "
                          "COALESCE((SELECT r.safety_qty FROM stock_alert_rules r WHERE r.merchant_id=p.merchant_id AND r.target_type='product' "
                          "AND r.target_id=p.id), ?) safety FROM inv_stock s JOIN products p ON p.id=s.product_id AND p.merchant_id=s.merchant_id "
                          "WHERE s.merchant_id=? AND s.wh_code IN (%s) AND COALESCE(p.status,'在售') != '停售' "
                          "GROUP BY s.wh_code, p.id HAVING q <= safety ORDER BY q, p.code LIMIT ?" % _in(codes), [base] + args + [limit]).fetchall()
        return [{'store_code': r[0], 'store_name': names.get(r[0], r[0]), 'code': r[2], 'name': r[3], 'qty': r[4], 'safety_qty': r[5]} for r in rows]
    since = (datetime.now() - timedelta(days=int(slow_days))).strftime('%Y-%m-%d %H:%M:%S')
    rows = db.execute("SELECT s.wh_code, p.code, p.name, SUM(s.qty) q FROM inv_stock s JOIN products p ON p.id=s.product_id AND p.merchant_id=s.merchant_id "
                      "WHERE s.merchant_id=? AND s.wh_code IN (%s) AND s.quality='ok' GROUP BY s.wh_code, p.id HAVING q > 0 AND NOT EXISTS ("
                      "SELECT 1 FROM order_items i JOIN orders o ON o.order_no=i.order_no WHERE o.merchant_id=? AND o.store_code=s.wh_code "
                      "AND i.product_id=p.id AND o.biz_type='sale' AND o.created_at>=?) ORDER BY q DESC LIMIT ?" % _in(codes),
                      args + [mid, since, limit]).fetchall()
    return [{'store_code': r[0], 'store_name': names.get(r[0], r[0]), 'code': r[1], 'name': r[2], 'qty': r[3], 'no_sale_days': int(slow_days)} for r in rows]


def _member_overview(db, actor, codes, s, e):
    mid = actor['merchant_id']
    if not codes:
        return {'new': 0, 'buyers': 0, 'repeat': 0, 'repeat_rate': None}
    extra = " OR COALESCE(reg_store_code,'')=''" if actor['data_scope'] == 'merchant' and not request.args.get('store') and not request.args.get('region') else ''
    new = db.execute("SELECT COUNT(*) FROM members WHERE merchant_id=? AND (reg_store_code IN (%s)%s) AND reg_time>=? AND reg_time<?" % (_in(codes), extra),
                     [mid] + codes + [_ts(s), _ts(e, True)]).fetchone()[0]
    r = db.execute("SELECT COUNT(*), COUNT(CASE WHEN n>=2 THEN 1 END) FROM (SELECT o.member_code, COUNT(*) n FROM orders o WHERE o.merchant_id=? "
                   "AND o.store_code IN (%s) AND o.biz_type='sale' AND COALESCE(o.member_code,'')!='' AND o.created_at>=? AND o.created_at<?" % _in(codes)
                   + PAID + " GROUP BY o.member_code)", [mid] + codes + [_ts(s), _ts(e, True)]).fetchone()
    buyers, rep = r[0] or 0, r[1] or 0
    return {'new': new, 'buyers': buyers, 'repeat': rep, 'repeat_rate': round(rep * 100.0 / buyers, 1) if buyers else None}


@bp.route('/m/overview-stock-member')
@require_perm('app.dashboard')
def overview_stock_member():
    """计数 + 每类前 5 条；detail=stock_low|near_expiry|expired|slow 返回该类明细（最多 200 条）。days=临期天数，slow_days=滞销天数。"""
    db = get_db()
    a = current_actor()
    codes, label = resolve_scope(db, a)
    period = _period()
    s, e = compare_ranges(_d(request.args.get('date')), period)['current']
    days = min(365, max(1, inum(request.args.get('days'), 60)))
    slow_days = min(365, max(7, inum(request.args.get('slow_days'), 30)))
    detail = request.args.get('detail') or ''
    if detail:
        if detail not in ('stock_low', 'near_expiry', 'expired', 'slow'):
            raise BizError('detail 只能是 stock_low / near_expiry / expired / slow')
        return ok({'scope': {'label': label, 'stores': codes}, 'kind': detail,
                   'items': _wh_alerts(db, a['merchant_id'], codes, detail, days, slow_days, 200)})
    stock = {}
    for k in ('stock_low', 'near_expiry', 'expired', 'slow'):
        rows = _wh_alerts(db, a['merchant_id'], codes, k, days, slow_days, 500)
        stock[k] = {'count': len(rows), 'top': rows[:5]}
    return ok({'scope': {'label': label, 'stores': codes}, 'period': period, 'range': {'start': s.isoformat(), 'end': e.isoformat()},
               'days': days, 'slow_days': slow_days, 'stock': stock, 'member': _member_overview(db, a, codes, s, e), 'updated_at': now_s()})


@bp.route('/m/scopes')
@require_perm('app.dashboard', 'app.store')
def scopes():
    """门店切换器（APP-005）：可选的总部汇总 / 区域 / 门店（只列本人范围内的）。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    allowed = scope_stores(db, a)
    stores = [{'code': r[0], 'name': r[1], 'region_id': r[2]} for r in db.execute(
        'SELECT code, name, region_id FROM stores WHERE merchant_id=? ORDER BY sort, code', (mid,)) if r[0] in allowed]
    regions = []
    for r in db.execute("SELECT id, code, name FROM org_units WHERE merchant_id=? AND kind='region' ORDER BY code", (mid,)):
        rs = [s['code'] for s in db.execute('SELECT code FROM stores WHERE merchant_id=? AND region_id=?', (mid, r['id']))]
        if rs and all(c in allowed for c in rs):
            regions.append({'code': r['code'], 'name': r['name'], 'stores': rs})
    for s in stores:
        s.pop('region_id', None)
    return ok({'all': a['data_scope'] == 'merchant', 'regions': regions, 'stores': stores, 'default_store': a['store_code']})


# ================================================================ APP-101 门店经营
@bp.route('/m/store')
@require_perm('app.store')
def store_ops():
    """本店销售/订单/退款/班次/收银员。store 单店（默认本人门店）；start/end 日期（默认今天）。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    req = (request.args.get('store') or a['store_code'] or '').strip()
    if not req:
        allowed = scope_stores(db, a)
        if not allowed:
            raise BizError('账号没有可查看的门店', 403, 'SCOPE_DENIED')
        req = allowed[0]
    if ',' in req:
        raise BizError('门店经营一次只能看一家门店')
    code = scope_stores(db, a, req)[0]
    s = _d(request.args.get('start'))
    e = _d(request.args.get('end'), s)
    if e < s or (e - s).days > 92:
        raise BizError('时间范围最长 93 天，且结束日期不能早于开始日期')
    args = [mid, code, _ts(s), _ts(e, True)]
    where = ' WHERE o.merchant_id=? AND o.store_code=? AND o.created_at>=? AND o.created_at<?'
    stats = sales_stats(db, mid, [code], s, e)
    cashiers = [{'user': r[0], 'name': r[1], 'orders': r[2], 'sales': yuan(int(r[3] or 0)), 'refund_orders': r[4], 'refund': yuan(int(r[5] or 0))}
                for r in db.execute("SELECT o.cashier_user, MAX(o.cashier_name), COUNT(CASE WHEN o.biz_type='sale' THEN 1 END), "
                                    "COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.payable_cents END),0), COUNT(CASE WHEN o.biz_type='refund' THEN 1 END), "
                                    "COALESCE(-SUM(CASE WHEN o.biz_type='refund' THEN o.payable_cents END),0) FROM orders o" + where + PAID +
                                    " GROUP BY o.cashier_user ORDER BY 4 DESC", args)]
    pays = [{'method': r[0], 'name': r[1], 'amount': yuan(int(r[2] or 0))} for r in db.execute(
        "SELECT p.method, MAX(p.method_name), SUM(p.amount_cents) FROM payments p WHERE p.merchant_id=? AND p.store_code=? AND p.created_at>=? "
        "AND p.created_at<? AND COALESCE(p.status,'success')='success' GROUP BY p.method ORDER BY 3 DESC", args)]
    shifts = [{'id': r[0], 'terminal_no': r[1] or '', 'cashier': r[2] or '', 'open_time': r[3], 'close_time': r[4] or '', 'status': r[5] or '',
               'opening_cash': yuan(r[6]), 'expected_cash': yuan(r[7]), 'counted_cash': yuan(r[8]), 'diff': yuan(r[9])}
              for r in db.execute("SELECT id, terminal_no, cashier_name, open_time, close_time, status, opening_cash_cents, expected_cash_cents, "
                                  "counted_cash_cents, diff_cents FROM shifts WHERE merchant_id=? AND store_code=? AND ((open_time>=? AND open_time<?) "
                                  "OR status IN ('open','营业中','进行中')) ORDER BY id DESC LIMIT 30", args)]
    recent = [{'order_no': r[0], 'biz_type': r[1], 'created_at': r[2], 'cashier': r[3], 'member': (r[4] or '') and ('%s %s' % (r[4], mask_phone(r[5], a))).strip(),
               'payable': yuan(r[6]), 'status': r[7], 'items': round(fnum(r[8]), 3)}
              for r in db.execute('SELECT o.order_no, o.biz_type, o.created_at, o.cashier_name, o.member_name, o.member_phone, o.payable_cents, o.status, '
                                  'o.item_qty FROM orders o' + where + ' ORDER BY o.id DESC LIMIT 30', args)]
    return ok({'store': {'code': code, 'name': _store_name(db, mid, code)}, 'start': s.isoformat(), 'end': e.isoformat(), 'stats': stats,
               'cashiers': cashiers, 'pays': pays, 'shifts': shifts, 'recent': recent, 'updated_at': now_s(), 'caliber': CALIBER})


# ================================================================ APP-102 商品库存查询
def _goods_payload(db, a, p, codes):
    from . import inventory
    mid = a['merchant_id']
    units = [{'unit': u[0], 'rate': u[1], 'retail': yuan(u[2]) if u[2] is not None else None} for u in db.execute(
        "SELECT unit, rate, retail_cents FROM product_units WHERE merchant_id=? AND product_id=? AND COALESCE(status,'启用')='启用' ORDER BY rate",
        (mid, p['id']))]
    barcodes = [r[0] for r in db.execute('SELECT barcode FROM product_barcodes WHERE merchant_id=? AND product_id=? ORDER BY barcode', (mid, p['id']))]
    by_store = []
    for c in codes:
        by_store.append({'store_code': c, 'available': inventory.available(db, mid, c, p['id']),
                         'book': int(db.execute("SELECT COALESCE(SUM(qty),0) FROM inv_stock WHERE merchant_id=? AND wh_code=? AND product_id=? AND quality='ok'",
                                                (mid, c, p['id'])).fetchone()[0] or 0),
                         'defect': int(db.execute("SELECT COALESCE(SUM(qty),0) FROM inv_stock WHERE merchant_id=? AND wh_code=? AND product_id=? AND quality!='ok'",
                                                  (mid, c, p['id'])).fetchone()[0] or 0)})
    batches = []
    if codes:
        today = datetime.now().date().isoformat()
        for r in db.execute("SELECT s.wh_code, b.id, b.batch_no, b.expiry_date, b.status, b.lock_reason, SUM(s.qty) q FROM inv_stock s "
                            "JOIN inv_batches b ON b.id=s.batch_id AND b.merchant_id=s.merchant_id WHERE s.merchant_id=? AND s.product_id=? "
                            "AND s.wh_code IN (%s) AND s.qty>0 GROUP BY s.wh_code, b.id ORDER BY b.expiry_date, b.id LIMIT 60" % _in(codes),
                            [mid, p['id']] + codes):
            b = {'batch_no': r[2], 'expiry_date': r[3], 'status': r[4], 'lock_reason': r[5] or ''}
            left = None
            if r[3]:
                try:
                    left = (datetime.strptime(r[3], '%Y-%m-%d').date() - datetime.strptime(today, '%Y-%m-%d').date()).days
                except ValueError:
                    left = None
            batches.append({'store_code': r[0], 'batch_no': r[2], 'expiry_date': r[3] or '', 'days_left': left, 'qty': r[6],
                            'unsellable': inventory.check_batch_sellable(b)})
    names = dict(db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)).fetchall())
    for x in by_store:
        x['store_name'] = names.get(x['store_code'], x['store_code'])
    for x in batches:
        x['store_name'] = names.get(x['store_code'], x['store_code'])
    d = {'id': p['id'], 'code': p['code'], 'name': p['name'], 'spec': p['spec'] or '', 'brand': p['brand'] or '', 'category': p['category'] or '',
         'unit': p['base_unit'] or p['unit'] or '', 'status': p['status'] or '', 'retail': yuan(p['retail_cents']),
         'member_price': yuan(p['member_price_cents']), 'barcodes': barcodes, 'units': units, 'batch_managed': bool(p['batch_managed']),
         'stock': by_store, 'total_available': sum(x['available'] for x in by_store), 'batches': batches}
    if has_perm(a, 'goods.cost.view'):
        d['cost'] = cost_view(a, p['cost_cents'])
    return d


@bp.route('/m/goods')
@require_perm('stock.view')
def m_goods():
    """kw = 条码/箱码/货号（精确）或名称/简称/拼音首字母（模糊）；store 限定门店（默认本人可见全部）。"""
    from . import goods_svc
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    kw = (request.args.get('kw') or request.args.get('code') or '').strip()
    if not kw:
        raise BizError('请输入或扫描条码、货号、商品名称')
    codes, label = resolve_scope(db, a)
    hit = goods_svc.find_sku(db, mid, kw)
    if hit:
        rows = [db.execute('SELECT * FROM products WHERE id=? AND merchant_id=?', (hit['product']['id'], mid)).fetchone()]
        scan = {'unit': hit['unit'], 'rate': hit['rate'], 'barcode': hit['barcode']}
    else:
        like = '%' + kw.replace('%', '').replace('_', '') + '%'
        rows = db.execute("SELECT * FROM products WHERE merchant_id=? AND (name LIKE ? OR short_name LIKE ? OR code LIKE ? OR pinyin LIKE ? "
                          "OR brand LIKE ?) ORDER BY CASE WHEN status='在售' THEN 0 ELSE 1 END, code LIMIT 30",
                          (mid, like, like, like, like.upper(), like)).fetchall()
        scan = None
    return ok({'scope': {'label': label, 'stores': codes}, 'scan': scan, 'items': [_goods_payload(db, a, r, codes) for r in rows if r is not None]})


# ================================================================ APP-105 总部公告
def _ann_visible(db, actor, row, my_stores=None):
    if row['merchant_id'] != actor['merchant_id'] or row['status'] != 'published':
        return False
    if row['target_roles']:
        if actor['role_code'] not in [x for x in row['target_roles'].split(',') if x]:
            return False
    if row['target_stores']:
        mine = my_stores if my_stores is not None else scope_stores(db, actor)
        if not set(x for x in row['target_stores'].split(',') if x) & set(mine):
            return False
    return True


def _ann_dict(db, actor, row, with_body=False):
    rd = db.execute('SELECT read_at, confirmed_at FROM announcement_reads WHERE announcement_id=? AND user_id=?', (row['id'], actor['id'])).fetchone()
    d = {'id': row['id'], 'title': row['title'], 'level': row['level'], 'need_confirm': bool(row['need_confirm']), 'status': row['status'],
         'created_by': row['created_by'] or '', 'created_at': row['created_at'], 'read_at': rd[0] if rd else None,
         'confirmed_at': rd[1] if rd else None, 'target_stores': [x for x in row['target_stores'].split(',') if x],
         'target_roles': [x for x in row['target_roles'].split(',') if x]}
    d['summary'] = (row['body'] or '')[:60]
    if with_body:
        d['body'] = row['body'] or ''
    return d


def _ann_targets(db, mid, target_stores, target_roles):
    out = []
    for u in db.execute("SELECT * FROM users WHERE merchant_id=? AND status='启用'", (mid,)):
        a = _load_actor(db, u)
        if target_roles and a['role_code'] not in target_roles:
            continue
        if target_stores and not set(target_stores) & set(scope_stores(db, a)):
            continue
        out.append(a)
    return out


def _ann_row(db, actor, aid):
    r = db.execute('SELECT * FROM announcements WHERE id=? AND merchant_id=?', (aid, actor['merchant_id'])).fetchone()
    if r is None:
        raise BizError('公告不存在', 404, 'NOT_FOUND')
    return r


@bp.route('/m/announcements')
@api()
def ann_list():
    """本人可见的公告（box=mine 且有 notice.publish：我发布的，含撤回的）。unread_only=1 只看未读。"""
    db = get_db()
    a = current_actor()
    if a['is_platform']:
        raise BizError('平台账号没有商户公告', 403)
    page, size = max(1, inum(request.args.get('page'), 1)), min(50, max(1, inum(request.args.get('size'), 20)))
    if request.args.get('box') == 'mine' and has_perm(a, 'notice.publish'):
        rows = db.execute('SELECT * FROM announcements WHERE merchant_id=? AND created_by=? ORDER BY id DESC LIMIT 200',
                          (a['merchant_id'], a['username'])).fetchall()
        items = [_ann_dict(db, a, r) for r in rows]
    else:
        mine = scope_stores(db, a)
        rows = db.execute("SELECT * FROM announcements WHERE merchant_id=? AND status='published' ORDER BY id DESC LIMIT 500", (a['merchant_id'],)).fetchall()
        items = [_ann_dict(db, a, r) for r in rows if _ann_visible(db, a, r, mine)]
        if request.args.get('unread_only') == '1':
            items = [x for x in items if not x['read_at'] or (x['need_confirm'] and not x['confirmed_at'])]
    total = len(items)
    unread = sum(1 for x in items if not x['read_at'])
    unconfirmed = sum(1 for x in items if x['need_confirm'] and not x['confirmed_at'])
    return ok({'page': page, 'size': size, 'total': total, 'unread': unread, 'unconfirmed': unconfirmed,
               'items': items[(page - 1) * size: page * size]})


@bp.route('/m/announcements', methods=['POST'])
@require_perm('notice.publish')
def ann_publish():
    """{title, body, level:'normal'|'important', need_confirm, target_stores:[...]|'A,B', target_roles:[role_code..]}"""
    db = get_db()
    a = current_actor()
    d = request.get_json(silent=True) or {}
    title = (d.get('title') or '').strip()
    if not title or len(title) > 100:
        raise BizError('标题必填，且不超过 100 字')
    body = (d.get('body') or '').strip()
    if len(body) > 5000:
        raise BizError('正文不超过 5000 字')
    level = d.get('level') or 'normal'
    if level not in ('normal', 'important'):
        raise BizError('level 只能是 normal / important')
    ts = d.get('target_stores') or []
    ts = [x.strip() for x in (ts.split(',') if isinstance(ts, str) else ts) if x and x.strip()]
    if ts:
        scope_stores(db, a, ','.join(ts))
    elif a['data_scope'] != 'merchant':
        ts = scope_stores(db, a)          # 非总部账号只能发给自己范围内的门店
        if not ts:
            raise BizError('账号没有可发布的门店范围', 403, 'SCOPE_DENIED')
    tr = d.get('target_roles') or []
    tr = [x.strip() for x in (tr.split(',') if isinstance(tr, str) else tr) if x and x.strip()]
    valid_roles = set(r[0] for r in db.execute('SELECT code FROM roles WHERE merchant_id=?', (a['merchant_id'],)))
    bad = [x for x in tr if x not in valid_roles]
    if bad:
        raise BizError('角色不存在：%s' % ','.join(bad))
    need_confirm = 1 if d.get('need_confirm') else 0
    with tx(db):
        cur = db.execute('INSERT INTO announcements(merchant_id, title, body, level, need_confirm, target_stores, target_roles, status, created_by, created_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?)', (a['merchant_id'], title, body, level, need_confirm, ','.join(ts), ','.join(tr), 'published',
                                                         a['username'], now_s()))
        aid = cur.lastrowid
        targets = [t['id'] for t in _ann_targets(db, a['merchant_id'], ts, tr) if t['id'] != a['id']]
        n = notify.notify_users(db, a['merchant_id'], targets, 'notice', ('【需确认】' if need_confirm else '') + '公告：' + title,
                                '', ref_type='notice', ref_no=str(aid), link='m#notice/%d' % aid,
                                level='important' if level == 'important' else 'normal', is_todo=bool(need_confirm))
        audit(db, a, 'mobile', '发布公告', str(aid), None, {'title': title, 'stores': ts, 'roles': tr, 'need_confirm': need_confirm, 'notified': n},
              store_code='')
    return ok({'id': aid, 'notified': n})


@bp.route('/m/announcements/<int:aid>')
@api()
def ann_get(aid):
    db = get_db()
    a = current_actor()
    r = _ann_row(db, a, aid)
    is_pub = has_perm(a, 'notice.publish') and r['created_by'] == a['username']
    if not is_pub and not _ann_visible(db, a, r):
        raise BizError('公告不存在或不在你的接收范围', 404, 'NOT_FOUND')
    d = _ann_dict(db, a, r, True)
    if is_pub:
        targets = _ann_targets(db, a['merchant_id'], d['target_stores'], d['target_roles'])
        reads = dict((x[0], (x[1], x[2])) for x in db.execute('SELECT user_id, read_at, confirmed_at FROM announcement_reads WHERE announcement_id=?', (aid,)))
        d['stats'] = {'targets': len(targets), 'read': sum(1 for t in targets if t['id'] in reads),
                      'confirmed': sum(1 for t in targets if reads.get(t['id'], (None, None))[1]),
                      'unread_names': [t['name'] for t in targets if t['id'] not in reads][:50]}
    return ok(d)


@bp.route('/m/announcements/<int:aid>/read', methods=['POST'])
@api()
def ann_read(aid):
    db = get_db()
    a = current_actor()
    r = _ann_row(db, a, aid)
    if not _ann_visible(db, a, r):
        raise BizError('公告不存在或不在你的接收范围', 404, 'NOT_FOUND')
    with tx(db):
        db.execute('INSERT OR IGNORE INTO announcement_reads(announcement_id, user_id, read_at) VALUES(?,?,?)', (aid, a['id'], now_s()))
        db.execute("UPDATE notifications SET status='read', read_at=? WHERE merchant_id=? AND user_id=? AND ref_type='notice' AND ref_no=? "
                   "AND status='unread'", (now_s(), a['merchant_id'], a['id'], str(aid)))
        if not r['need_confirm']:
            notify.done_by_ref(db, a['merchant_id'], 'notice', str(aid), a['id'])
    return ok(_ann_dict(db, a, r, True))


@bp.route('/m/announcements/<int:aid>/confirm', methods=['POST'])
@api()
def ann_confirm(aid):
    db = get_db()
    a = current_actor()
    r = _ann_row(db, a, aid)
    if not _ann_visible(db, a, r):
        raise BizError('公告不存在或不在你的接收范围', 404, 'NOT_FOUND')
    if not r['need_confirm']:
        raise BizError('该公告不需要确认')
    with tx(db):
        db.execute('INSERT OR IGNORE INTO announcement_reads(announcement_id, user_id, read_at) VALUES(?,?,?)', (aid, a['id'], now_s()))
        db.execute('UPDATE announcement_reads SET confirmed_at=COALESCE(confirmed_at, ?) WHERE announcement_id=? AND user_id=?', (now_s(), aid, a['id']))
        notify.done_by_ref(db, a['merchant_id'], 'notice', str(aid), a['id'])
    return ok(_ann_dict(db, a, r, True))


@bp.route('/m/announcements/<int:aid>/withdraw', methods=['POST'])
@require_perm('notice.publish')
def ann_withdraw(aid):
    db = get_db()
    a = current_actor()
    r = _ann_row(db, a, aid)
    if r['created_by'] != a['username'] and a['data_scope'] != 'merchant':
        raise BizError('只能撤回自己发布的公告', 403)
    if r['status'] != 'published':
        raise BizError('公告已撤回', 409, 'CONFLICT')
    reason = ((request.get_json(silent=True) or {}).get('reason') or '').strip()
    with tx(db):
        db.execute("UPDATE announcements SET status='withdrawn' WHERE id=?", (aid,))
        notify.done_by_ref(db, a['merchant_id'], 'notice', str(aid))
        audit(db, a, 'mobile', '撤回公告', str(aid), {'status': 'published'}, {'status': 'withdrawn'}, reason, store_code='')
    return ok({'id': aid, 'status': 'withdrawn'})


# ================================================================ APP-302 提醒规则
def _rule_dict(r):
    d = dict(r)
    d['category_name'] = notify.CATEGORIES.get(d['category'], d['category'])
    d['enabled'] = bool(d['enabled'])
    return d


@bp.route('/m/notify-rules')
@require_perm('sys.config')
def rules_list():
    db = get_db()
    a = current_actor()
    rows = db.execute('SELECT * FROM notify_rules WHERE merchant_id=? ORDER BY category, role_code, store_code', (a['merchant_id'],)).fetchall()
    roles = [{'code': r[0], 'name': r[1]} for r in db.execute("SELECT code, name FROM roles WHERE merchant_id=? AND status='启用' ORDER BY id",
                                                             (a['merchant_id'],))]
    return ok({'items': [_rule_dict(r) for r in rows], 'categories': notify.CATEGORIES, 'levels': notify.LEVEL_NAME, 'roles': roles,
               'note': '某类消息没有任何规则时，所有有权限的人员都会收到；配置后只有命中启用规则的人员收到，命中停用规则的人员不收。'
                       '审批结果、公告等点对点消息不受规则影响。'})


@bp.route('/m/notify-rules', methods=['POST'])
@require_perm('sys.config')
def rules_save():
    """新增/修改：{category, role_code, store_code, min_level, enabled}；删除：{id, delete:true}。"""
    db = get_db()
    a = current_actor()
    d = request.get_json(silent=True) or {}
    mid = a['merchant_id']
    with tx(db):
        if d.get('delete'):
            r = db.execute('SELECT * FROM notify_rules WHERE id=? AND merchant_id=?', (inum(d.get('id')), mid)).fetchone()
            if r is None:
                raise BizError('规则不存在', 404, 'NOT_FOUND')
            db.execute('DELETE FROM notify_rules WHERE id=?', (r['id'],))
            audit(db, a, 'mobile', '删除提醒规则', str(r['id']), dict(r), None, store_code='')
            return ok({'deleted': r['id']})
        cat = d.get('category') or ''
        if cat not in notify.CATEGORIES:
            raise BizError('消息类别不正确')
        role = (d.get('role_code') or '').strip()
        if role and db.execute('SELECT 1 FROM roles WHERE merchant_id=? AND code=?', (mid, role)).fetchone() is None:
            raise BizError('角色不存在')
        store = (d.get('store_code') or '').strip()
        if store:
            scope_stores(db, a, store)
        lv = d.get('min_level') or 'normal'
        if lv not in notify.LEVELS:
            raise BizError('min_level 只能是 normal / important / urgent')
        en = 0 if d.get('enabled') in (False, 0, '0') else 1
        old = db.execute('SELECT * FROM notify_rules WHERE merchant_id=? AND category=? AND role_code=? AND store_code=?', (mid, cat, role, store)).fetchone()
        db.execute('INSERT INTO notify_rules(merchant_id, category, role_code, store_code, min_level, enabled) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(merchant_id, category, role_code, store_code) DO UPDATE SET min_level=excluded.min_level, enabled=excluded.enabled',
                   (mid, cat, role, store, lv, en))
        r = db.execute('SELECT * FROM notify_rules WHERE merchant_id=? AND category=? AND role_code=? AND store_code=?', (mid, cat, role, store)).fetchone()
        audit(db, a, 'mobile', '保存提醒规则', str(r['id']), dict(old) if old else None, dict(r), store_code='')
    return ok(_rule_dict(r))


# ================================================================ APP-404 版本检查
def _ver_tuple(v):
    out = []
    for x in str(v or '0').split('.'):
        try:
            out.append(int(x))
        except ValueError:
            out.append(0)
    return tuple(out)


@bp.route('/m/version')
def m_version():
    """无需登录。client 当前版本 v（前端传）低于 min_version 且 force=1 时，前端清缓存强制刷新。"""
    db = get_db()
    r = db.execute("SELECT * FROM app_versions WHERE client='m' ORDER BY id DESC LIMIT 1").fetchone()
    latest = r['version'] if r and _ver_tuple(r['version']) >= _ver_tuple(M_VERSION) else M_VERSION
    min_v = (r['min_version'] if r else '') or ''
    client_v = (request.args.get('v') or '').strip()
    data = {'version': latest, 'server_version': M_VERSION, 'min_version': min_v, 'force': bool(r['force']) if r else False,
            'notes': (r['notes'] if r else '') or '', 'server_time': now_s()}
    if client_v:
        data['outdated'] = _ver_tuple(client_v) < _ver_tuple(latest)
        data['must_upgrade'] = bool(min_v) and _ver_tuple(client_v) < _ver_tuple(min_v) and data['force']
    return jsonify({'ok': True, 'data': data})


@bp.route('/m/version', methods=['POST'])
@require_platform('platform.ops')
def m_version_publish():
    """平台发布 H5 版本：{version, min_version, force, notes}（全平台生效，表无 merchant_id）。"""
    db = get_db()
    a = current_actor()
    d = request.get_json(silent=True) or {}
    v = (d.get('version') or '').strip()
    if not v or not all(x.isdigit() for x in v.split('.')):
        raise BizError('版本号格式应为 3.0.1')
    with tx(db):
        db.execute('INSERT INTO app_versions(client, version, min_version, force, notes, created_at) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(client, version) DO UPDATE SET min_version=excluded.min_version, force=excluded.force, notes=excluded.notes',
                   ('m', v, (d.get('min_version') or '').strip(), 1 if d.get('force') else 0, (d.get('notes') or '')[:500], now_s()))
        audit(db, a, 'platform', '发布手机端版本', v, None, d, level='high', store_code='')
    return ok({'version': v})


# ================================================================ APP-402 / APP-405 我的登录设备（设备丢失可远程退出）
def _sess_key(sid):
    return hashlib.sha256(('m-sess:' + sid).encode('utf-8')).hexdigest()[:16]


@bp.route('/m/sessions')
@api()
def my_sessions():
    db = get_db()
    a = current_actor()
    cur = session.get('sid') or ''
    rows = db.execute('SELECT * FROM user_sessions WHERE user_id=? AND merchant_id=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 50',
                      (a['id'], a['merchant_id'])).fetchall()
    return ok({'items': [{'key': _sess_key(r['sid']), 'client': r['client'], 'device': r['device'] or '', 'ip': r['ip'] or '',
                          'created_at': r['created_at'], 'last_seen': r['last_seen'] or '', 'current': r['sid'] == cur} for r in rows]})


@bp.route('/m/sessions/revoke', methods=['POST'])
@api()
def revoke_session():
    """{key} 退出某台设备；{others:true} 退出除本机外全部设备。"""
    db = get_db()
    a = current_actor()
    d = request.get_json(silent=True) or {}
    cur = session.get('sid') or ''
    rows = db.execute('SELECT sid FROM user_sessions WHERE user_id=? AND merchant_id=? AND revoked_at IS NULL', (a['id'], a['merchant_id'])).fetchall()
    if d.get('others'):
        sids = [r[0] for r in rows if r[0] != cur]
    else:
        sids = [r[0] for r in rows if _sess_key(r[0]) == (d.get('key') or '')]
        if not sids:
            raise BizError('设备会话不存在或已退出', 404, 'NOT_FOUND')
    with tx(db):
        for s in sids:
            db.execute("UPDATE user_sessions SET revoked_at=?, revoke_reason=? WHERE sid=? AND revoked_at IS NULL", (now_s(), '本人远程退出', s))
        audit(db, a, 'mobile', '远程退出设备', '', None, {'count': len(sids)}, level='security', store_code='')
    return ok({'revoked': len(sids)})


# ================================================================ 首页聚合（减少手机端请求数）
@bp.route('/m/home')
@api()
def m_home():
    db = get_db()
    a = current_actor()
    if a['is_platform']:
        raise BizError('平台账号请使用平台运营后台', 403)
    mid = a['merchant_id']
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE merchant_id=? AND user_id=? AND status='unread'", (mid, a['id'])).fetchone()[0]
    todo = db.execute("SELECT COUNT(*) FROM notifications WHERE merchant_id=? AND user_id=? AND is_todo=1 AND status!='done'", (mid, a['id'])).fetchone()[0]
    risk = db.execute("SELECT COUNT(*) FROM notifications WHERE merchant_id=? AND user_id=? AND category='risk' AND status='unread'", (mid, a['id'])).fetchone()[0]
    p = a['perms']
    return ok({'name': a['name'], 'role_name': a['role_name'], 'store_code': a['store_code'], 'data_scope': a['data_scope'],
               'unread': unread, 'todo_open': todo, 'risk_unread': risk, 'server_version': M_VERSION,
               'features': {'dashboard': 'app.dashboard' in p, 'profit': 'report.profit' in p and 'app.dashboard' in p, 'store': 'app.store' in p,
                            'goods': 'stock.view' in p, 'approval': any(x.startswith('approval.') for x in p),
                            'request': 'stock.request' in p, 'receive': 'stock.receive' in p, 'adjust': 'stock.adjust' in p,
                            'take': 'stock.take' in p, 'notice_publish': 'notice.publish' in p, 'notify_rules': 'sys.config' in p}})


# ================================================================ PWA：允许 static/sw.js 控制整个应用作用域（/shouyin/）
@bp.after_app_request
def _sw_scope(resp):
    if request.path.endswith('/static/sw.js'):
        resp.headers['Service-Worker-Allowed'] = '../'
        resp.headers['Cache-Control'] = 'no-cache'
    return resp
