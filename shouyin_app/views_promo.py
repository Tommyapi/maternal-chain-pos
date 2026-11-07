# -*- coding: utf-8 -*-
"""价格与促销：价格体系/调价单/活动/叠加规则/试算/优惠券生命周期。

Owner：模块②（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-401~404 ERP-603~604 POS-401~406
接口清单与入参出参：docs/API-CONTRACT.md §4.3；返回格式 {ok:true,data} / {ok:false,code,msg}；权限一律 @require_perm(...)。

活动 rule / scope JSON（金额可写元 "price" 或分 "price_cents"）：
    scope: {"stores":[], "regions":[], "product_codes":[], "categories":[], "brands":[], "levels":[], "member_only":false, "exclude_codes":[]}
    special_price {"items":[{"code":"P1","price":350}]}        item_discount {"rate":"0.9"}
    nth_discount  {"n":2,"rate":"0.5"}                         bundle_price  {"codes":["P1","P2"],"price":450}
    redeem        {"code":"P3","threshold":200,"price":9.9,"max_qty":1}
    order_discount {"threshold":0,"rate":"0.95"}                full_reduce {"tiers":[{"threshold":300,"reduce":20}],"repeat":false}
    full_gift     {"threshold":300,"gift_code":"P2","gift_qty":1,"repeat":false}
状态：draft -> submitted(审批中) -> approved(生效中，按时间窗) -> paused <-> approved -> ended；draft/submitted -> cancelled；驳回回到 draft。
"""
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import Blueprint, request

from . import approval
from .core import get_db, tx, require_perm, current_actor, ok, audit, scope_stores, has_perm
from .money import to_cents, yuan, MoneyError
from .numbering import next_no
from .util import BizError, now_s

bp = Blueprint('promo', __name__, url_prefix='/api/promo')

ACT_STATUS = {'draft': '草稿', 'submitted': '审批中', 'approved': '已审核', 'paused': '已暂停', 'ended': '已终止', 'cancelled': '已作废'}
PA_STATUS = {'draft': '草稿', 'submitted': '审批中', 'approved': '待生效', 'effective': '已生效', 'rejected': '已驳回', 'cancelled': '已作废'}
SCOPE_KEYS = ('stores', 'regions', 'product_codes', 'categories', 'brands', 'levels', 'exclude_codes')


# ================================================================ 小工具
def _data():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _page():
    try:
        page = max(1, int(request.args.get('page') or 1))
        size = min(200, max(1, int(request.args.get('size') or request.args.get('pageSize') or 20)))
    except ValueError:
        raise BizError('分页参数不正确')
    return page, size


def _paged(db, sql, args, page, size, conv):
    total = db.execute('SELECT COUNT(*) FROM (%s)' % sql, args).fetchone()[0]
    rows = db.execute(sql + ' LIMIT ? OFFSET ?', list(args) + [size, (page - 1) * size]).fetchall()
    return {'page': page, 'size': size, 'total': total, 'items': [conv(r) for r in rows]}


def _cents(v, label, allow_zero=True):
    try:
        c = to_cents(v)
    except MoneyError as e:
        raise BizError('%s：%s' % (label, e))
    if c < 0 or (c == 0 and not allow_zero):
        raise BizError('%s必须大于 0' % label)
    return c


def _rate(v, label):
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise BizError('%s格式不正确' % label)
    if not (Decimal(0) < d <= Decimal(1)):
        raise BizError('%s需在 0~1 之间（如 0.88）' % label)
    return str(d)


def _dt(v, label, end=False):
    s = (v or '').strip()
    if not s:
        raise BizError('请填写%s' % label)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            d = datetime.strptime(s, fmt)
            if fmt == '%Y-%m-%d' and end:
                d = d.replace(hour=23, minute=59, second=59)
            return d.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
    raise BizError('%s格式应为 YYYY-MM-DD HH:MM:SS' % label)


def _scope_in(sc):
    sc = sc if isinstance(sc, dict) else {}
    out = {}
    for k in SCOPE_KEYS:
        v = sc.get(k) or []
        if isinstance(v, str):
            v = [x.strip() for x in v.replace('，', ',').split(',') if x.strip()]
        if not isinstance(v, list):
            raise BizError('范围 %s 应为列表' % k)
        if v:
            out[k] = [str(x)[:40] for x in v][:2000]
    if sc.get('member_only'):
        out['member_only'] = True
    return out


def _check_products(db, mid, codes):
    from . import goods_svc
    bad = [c for c in codes if goods_svc.find_sku(db, mid, c) is None]
    if bad:
        raise BizError('商品不存在：%s' % '、'.join(bad[:10]))


def _rule_in(db, mid, typ, r):
    """校验并规范活动规则（金额统一成 *_cents）。"""
    r = r if isinstance(r, dict) else {}
    if typ == 'special_price':
        items = []
        for x in r.get('items') or []:
            code = (x.get('code') or '').strip()
            if not code:
                raise BizError('特价商品编码不能为空')
            items.append({'code': code, 'price_cents': int(x['price_cents']) if x.get('price_cents') is not None else _cents(x.get('price'), '特价')})
        if not items:
            raise BizError('特价活动至少要一个商品')
        _check_products(db, mid, [x['code'] for x in items])
        return {'items': items}
    if typ == 'item_discount':
        return {'rate': _rate(r.get('rate'), '折扣率')}
    if typ == 'nth_discount':
        n = int(r.get('n') or 2)
        if n < 2 or n > 20:
            raise BizError('第 N 件的 N 需在 2~20')
        return {'n': n, 'rate': _rate(r.get('rate'), '第N件折扣率')}
    if typ == 'bundle_price':
        codes = [str(c).strip() for c in (r.get('codes') or []) if str(c).strip()]
        if len(codes) < 2 or len(set(codes)) != len(codes):
            raise BizError('套餐至少两个不同商品')
        _check_products(db, mid, codes)
        return {'codes': codes, 'price_cents': int(r['price_cents']) if r.get('price_cents') is not None else _cents(r.get('price'), '套餐价', False)}
    if typ == 'redeem':
        code = (r.get('code') or '').strip()
        _check_products(db, mid, [code])
        return {'code': code, 'threshold_cents': _cents(r.get('threshold') if r.get('threshold_cents') is None else Decimal(r['threshold_cents']) / 100, '换购门槛'),
                'price_cents': _cents(r.get('price') if r.get('price_cents') is None else Decimal(r['price_cents']) / 100, '换购价'),
                'max_qty': max(1, int(r.get('max_qty') or 1))}
    if typ == 'order_discount':
        return {'threshold_cents': _cents(r.get('threshold') or 0 if r.get('threshold_cents') is None else Decimal(r['threshold_cents']) / 100, '门槛'),
                'rate': _rate(r.get('rate'), '整单折扣率')}
    if typ == 'full_reduce':
        tiers = []
        for t in r.get('tiers') or []:
            tc = int(t['threshold_cents']) if t.get('threshold_cents') is not None else _cents(t.get('threshold'), '满减门槛', False)
            rc = int(t['reduce_cents']) if t.get('reduce_cents') is not None else _cents(t.get('reduce'), '满减金额', False)
            if rc >= tc:
                raise BizError('满减金额必须小于门槛')
            tiers.append({'threshold_cents': tc, 'reduce_cents': rc})
        if not tiers:
            raise BizError('满减至少一档')
        if r.get('repeat') and len(tiers) > 1:
            raise BizError('「每满减」只能设一档')
        return {'tiers': sorted(tiers, key=lambda t: t['threshold_cents']), 'repeat': bool(r.get('repeat'))}
    if typ == 'full_gift':
        code = (r.get('gift_code') or '').strip()
        _check_products(db, mid, [code])
        return {'threshold_cents': _cents(r.get('threshold') if r.get('threshold_cents') is None else Decimal(r['threshold_cents']) / 100, '满赠门槛', False),
                'gift_code': code, 'gift_qty': max(1, int(r.get('gift_qty') or 1)), 'repeat': bool(r.get('repeat')),
                'max_gift_qty': int(r.get('max_gift_qty') or 0) or None}
    raise BizError('不支持的活动类型：%s' % typ)


def _act(db, mid, no):
    r = db.execute('SELECT * FROM promo_activities WHERE merchant_id=? AND act_no=?', (mid, no)).fetchone()
    if r is None:
        raise BizError('活动不存在', 404, 'NOT_FOUND')
    return r


def _act_json(r):
    from .promo_engine import TYPE_NAME
    d = dict(r)
    d['scope'] = json.loads(d['scope'] or '{}')
    d['rule'] = json.loads(d['rule'] or '{}')
    d['type_name'] = TYPE_NAME.get(d['type'], d['type'])
    d['status_name'] = ACT_STATUS.get(d['status'], d['status'])
    d['explain'] = _act_explain(d)
    return d


def _act_explain(d):
    """ERP-404：规则说明（中文）。"""
    r, t = d['rule'], d['type']
    y = lambda c: '%.2f' % (int(c or 0) / 100.0)  # noqa: E731
    if t == 'special_price':
        s = '特价：' + '，'.join('%s %s 元' % (x['code'], y(x['price_cents'])) for x in r.get('items') or [])
    elif t == 'item_discount':
        s = '单品 %s 折' % r.get('rate')
    elif t == 'nth_discount':
        s = '同一商品第 %s 件 %s 折' % (r.get('n'), r.get('rate'))
    elif t == 'bundle_price':
        s = '%s 组合套餐价 %s 元' % ('+'.join(r.get('codes') or []), y(r.get('price_cents')))
    elif t == 'redeem':
        s = '其它商品满 %s 元，%s 换购价 %s 元（限 %s 件）' % (y(r.get('threshold_cents')), r.get('code'), y(r.get('price_cents')), r.get('max_qty'))
    elif t == 'order_discount':
        s = '满 %s 元整单 %s 折' % (y(r.get('threshold_cents')), r.get('rate'))
    elif t == 'full_reduce':
        s = '；'.join('%s %s 减 %s' % ('每满' if r.get('repeat') else '满', y(x['threshold_cents']), y(x['reduce_cents'])) for x in r.get('tiers') or [])
    elif t == 'full_gift':
        s = '满 %s 元送 %s × %s' % (y(r.get('threshold_cents')), r.get('gift_code'), r.get('gift_qty'))
    else:
        s = t
    s += '；优先级 %s' % d.get('priority')
    if d.get('exclusive_group'):
        s += '；互斥组「%s」（同组一单只享一个）' % d['exclusive_group']
    s += '；%s与会员价叠加；%s与优惠券同享' % ('可' if int(d.get('stack_member_price') or 0) else '不', '可' if int(d.get('stack_coupon') or 0) else '不')
    return s


def _activity_handler(db, ap, approver):
    """审批同意：活动 submitted -> approved。"""
    c = db.execute("UPDATE promo_activities SET status='approved', approved_by=?, approved_at=?, updated_at=? WHERE merchant_id=? AND act_no=? "
                   "AND status='submitted'", (approver['name'], now_s(), now_s(), ap['merchant_id'], ap['ref_no']))
    if c.rowcount != 1:
        raise BizError('活动状态已变化，不能审批', 409, 'CONFLICT')


def _price_adjust_handler(db, ap, approver):
    pa = db.execute("SELECT * FROM price_adjusts WHERE merchant_id=? AND pa_no=? AND status='submitted'", (ap['merchant_id'], ap['ref_no'])).fetchone()
    if pa is None:
        raise BizError('调价单状态已变化，不能审批', 409, 'CONFLICT')
    db.execute("UPDATE price_adjusts SET status='approved', approved_by=?, approved_at=? WHERE id=?", (approver['name'], now_s(), pa['id']))
    _write_prices(db, pa, approver)


approval.register_handler('promo', _activity_handler)
approval.register_handler('price_adjust', _price_adjust_handler)


def _decide_ref(db, actor, ref_type, ref_no, approve, reason):
    ap = db.execute("SELECT approval_no FROM approvals WHERE merchant_id=? AND ref_type=? AND ref_no=? AND status='pending' ORDER BY id DESC LIMIT 1",
                    (actor['merchant_id'], ref_type, ref_no)).fetchone()
    if ap is None:
        raise BizError('没有待审批的申请', 409, 'CONFLICT')
    return approval.decide(db, actor, ap[0], approve, reason)


# ================================================================ 活动
@bp.route('/activities')
@require_perm('promo.view')
def activities_list():
    db, a = get_db(), current_actor()
    sql, args = 'SELECT * FROM promo_activities WHERE merchant_id=?', [a['merchant_id']]
    for k in ('status', 'type'):
        if request.args.get(k):
            sql += ' AND %s=?' % k
            args.append(request.args[k])
    kw = (request.args.get('kw') or '').strip()
    if kw:
        sql += ' AND (act_no LIKE ? OR name LIKE ?)'
        args += ['%' + kw + '%'] * 2
    if request.args.get('active') == '1':
        sql += " AND status='approved' AND start_at<=? AND end_at>=?"
        args += [now_s(), now_s()]
    page, size = _page()
    return ok(_paged(db, sql + ' ORDER BY id DESC', args, page, size, _act_json))


@bp.route('/activities/<no>')
@require_perm('promo.view')
def activity_get(no):
    db, a = get_db(), current_actor()
    d = _act_json(_act(db, a['merchant_id'], no))
    d['approvals'] = [dict(r) for r in db.execute("SELECT approval_no, status, applicant_name, approver_name, decide_reason, created_at, decided_at "
                                                  "FROM approvals WHERE merchant_id=? AND ref_type='promo' AND ref_no=? ORDER BY id",
                                                  (a['merchant_id'], no))]
    d['logs'] = [dict(r) for r in db.execute("SELECT username, action, reason, created_at FROM audit_logs WHERE merchant_id=? AND module='promo' "
                                             "AND ref_no=? ORDER BY id", (a['merchant_id'], no))]
    return ok(d)


@bp.route('/activities', methods=['POST'])
@require_perm('promo.edit')
def activity_save():
    """新建/修改活动（只能改草稿；已审核的活动要先暂停/复制）。"""
    from .promo_engine import TYPE_NAME
    db, a = get_db(), current_actor()
    d = _data()
    mid = a['merchant_id']
    typ = d.get('type')
    if typ not in TYPE_NAME:
        raise BizError('活动类型只能是：%s' % '、'.join(TYPE_NAME))
    name = (d.get('name') or '').strip()[:60]
    if not name:
        raise BizError('请填写活动名称')
    start, end = _dt(d.get('start_at'), '开始时间'), _dt(d.get('end_at'), '结束时间', end=True)
    if end <= start:
        raise BizError('结束时间必须晚于开始时间')
    try:
        priority = int(d.get('priority') if d.get('priority') not in (None, '') else 100)
    except (TypeError, ValueError):
        raise BizError('优先级必须是整数')
    pr_rate = ''
    if d.get('points_rate') not in (None, ''):
        try:
            pr_rate = str(Decimal(str(d['points_rate'])))
        except InvalidOperation:
            raise BizError('积分倍率格式不正确')
    scope = _scope_in(d.get('scope'))
    if scope.get('stores'):
        bad = [s for s in scope['stores'] if not db.execute('SELECT 1 FROM stores WHERE merchant_id=? AND code=?', (mid, s)).fetchone()]
        if bad:
            raise BizError('门店不存在：%s' % '、'.join(bad))
    with tx(db):
        rule = _rule_in(db, mid, typ, d.get('rule'))
        vals = (name, typ, priority, (d.get('exclusive_group') or '').strip()[:40], 1 if d.get('stack_member_price') else 0,
                0 if d.get('stack_coupon') in (False, 0, '0') else 1, json.dumps(scope, ensure_ascii=False), json.dumps(rule, ensure_ascii=False),
                start, end, pr_rate, now_s())
        no = (d.get('act_no') or '').strip()
        if no:
            old = _act(db, mid, no)
            if old['status'] != 'draft':
                raise BizError('只有草稿可以修改（当前：%s）；已审核的活动请暂停后复制新建' % ACT_STATUS.get(old['status']), 409, 'CONFLICT')
            db.execute('UPDATE promo_activities SET name=?, type=?, priority=?, exclusive_group=?, stack_member_price=?, stack_coupon=?, scope=?, '
                       'rule=?, start_at=?, end_at=?, points_rate=?, updated_at=? WHERE id=?', vals + (old['id'],))
            audit(db, a, 'promo', '修改活动', no, _act_json(old), {'rule': rule, 'scope': scope}, store_code='')
        else:
            no = next_no(db, mid, 'AC', '')
            db.execute('INSERT INTO promo_activities(name, type, priority, exclusive_group, stack_member_price, stack_coupon, scope, rule, start_at, '
                       'end_at, points_rate, updated_at, act_no, merchant_id, status, created_by, created_at) '
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?)", vals + (no, mid, a['name'], now_s()))
            audit(db, a, 'promo', '新建活动', no, None, {'name': name, 'type': typ}, store_code='')
    return ok(_act_json(_act(db, mid, no)))


def _act_transition(no, frm, to, action, need_reason=False):
    db, a = get_db(), current_actor()
    reason = (_data().get('reason') or '').strip()
    if need_reason and not reason:
        raise BizError('请填写原因')
    with tx(db):
        r = _act(db, a['merchant_id'], no)
        if r['status'] not in frm:
            raise BizError('活动当前「%s」，不能%s' % (ACT_STATUS.get(r['status']), action), 409, 'CONFLICT')
        db.execute('UPDATE promo_activities SET status=?, updated_at=? WHERE id=? AND status=?', (to, now_s(), r['id'], r['status']))
        audit(db, a, 'promo', action + '活动', no, {'status': r['status']}, {'status': to}, reason, level='high', store_code='')
    return ok(_act_json(_act(db, a['merchant_id'], no)))


@bp.route('/activities/<no>/submit', methods=['POST'])
@require_perm('promo.edit')
def activity_submit(no):
    db, a = get_db(), current_actor()
    with tx(db):
        r = _act(db, a['merchant_id'], no)
        if r['status'] != 'draft':
            raise BizError('只有草稿可以提交审核', 409, 'CONFLICT')
        db.execute("UPDATE promo_activities SET status='submitted', updated_at=? WHERE id=?", (now_s(), r['id']))
        ap = approval.create(db, a, 'promo', '', '促销活动：%s' % r['name'], ref_no=no, ref_type='promo', need_perm='promo.approve',
                             reason=(_data().get('reason') or '')[:200])
        audit(db, a, 'promo', '提交活动审核', no, {'status': 'draft'}, {'status': 'submitted', 'approval_no': ap['approval_no']}, store_code='')
    d = _act_json(_act(db, a['merchant_id'], no))
    d['approval_no'] = ap['approval_no']
    return ok(d)


@bp.route('/activities/<no>/approve', methods=['POST'])
@require_perm('promo.approve')
def activity_approve(no):
    db, a = get_db(), current_actor()
    with tx(db):
        _decide_ref(db, a, 'promo', no, True, _data().get('reason') or '')
    return ok(_act_json(_act(db, a['merchant_id'], no)))


@bp.route('/activities/<no>/reject', methods=['POST'])
@require_perm('promo.approve')
def activity_reject(no):
    db, a = get_db(), current_actor()
    reason = (_data().get('reason') or '').strip()
    with tx(db):
        _decide_ref(db, a, 'promo', no, False, reason)
        db.execute("UPDATE promo_activities SET status='draft', updated_at=? WHERE merchant_id=? AND act_no=? AND status='submitted'",
                   (now_s(), a['merchant_id'], no))
    return ok(_act_json(_act(db, a['merchant_id'], no)))


@bp.route('/activities/<no>/pause', methods=['POST'])
@require_perm('promo.edit')
def activity_pause(no):
    return _act_transition(no, ('approved',), 'paused', '暂停', need_reason=True)


@bp.route('/activities/<no>/resume', methods=['POST'])
@require_perm('promo.approve')
def activity_resume(no):
    return _act_transition(no, ('paused',), 'approved', '恢复')


@bp.route('/activities/<no>/end', methods=['POST'])
@require_perm('promo.edit')
def activity_end(no):
    return _act_transition(no, ('approved', 'paused'), 'ended', '终止', need_reason=True)


@bp.route('/activities/<no>/cancel', methods=['POST'])
@require_perm('promo.edit')
def activity_cancel(no):
    db, a = get_db(), current_actor()
    with tx(db):
        for r in db.execute("SELECT approval_no FROM approvals WHERE merchant_id=? AND ref_type='promo' AND ref_no=? AND status='pending'",
                            (a['merchant_id'], no)).fetchall():
            db.execute("UPDATE approvals SET status='cancelled', decided_at=? WHERE merchant_id=? AND approval_no=?", (now_s(), a['merchant_id'], r[0]))
    return _act_transition(no, ('draft', 'submitted'), 'cancelled', '作废')


@bp.route('/activities/<no>/copy', methods=['POST'])
@require_perm('promo.edit')
def activity_copy(no):
    db, a = get_db(), current_actor()
    with tx(db):
        r = _act(db, a['merchant_id'], no)
        new = next_no(db, a['merchant_id'], 'AC', '')
        db.execute('INSERT INTO promo_activities(act_no, merchant_id, name, type, priority, exclusive_group, stack_member_price, stack_coupon, scope, '
                   "rule, start_at, end_at, status, created_by, created_at, updated_at, points_rate) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?,?)",
                   (new, a['merchant_id'], (r['name'] + '(复制)')[:60], r['type'], r['priority'], r['exclusive_group'], r['stack_member_price'],
                    r['stack_coupon'], r['scope'], r['rule'], r['start_at'], r['end_at'], a['name'], now_s(), now_s(), r['points_rate'] or ''))
        audit(db, a, 'promo', '复制活动', new, None, {'from': no}, store_code='')
    return ok(_act_json(_act(db, a['merchant_id'], new)))


# ================================================================ 试算（ERP-404，与收银同一 price_cart）
def priced_yuan(pr):
    """priced（分）-> 接口 JSON（元）。收银台（③）可复用。"""
    def conv(d):
        out = {}
        for k, v in d.items():
            if k.endswith('_cents') and isinstance(v, int):
                out[k[:-6]] = yuan(v)
            elif isinstance(v, list):
                out[k] = [conv(x) if isinstance(x, dict) else x for x in v]
            else:
                out[k] = v
        return out
    res = conv(dict((k, v) for k, v in pr.items() if k not in ('snapshot', 'member')))
    res['member'] = pr.get('member')
    res['snapshot'] = pr.get('snapshot')
    return res


@bp.route('/try', methods=['POST'])
@require_perm('promo.view')
def try_price():
    from .promo_engine import price_cart, coupons_for_cart
    db, a = get_db(), current_actor()
    d = _data()
    store = (d.get('store_code') or a.get('store_code') or '').strip()
    if not store:
        stores = scope_stores(db, a)
        store = stores[0] if stores else ''
    scope_stores(db, a, store)
    at = None
    if d.get('at'):
        at = _dt(d.get('at'), '试算时间')
    pr = price_cart(db, a, store, d, now=at)
    res = priced_yuan(pr)
    if d.get('member_code'):
        res['member_coupons'] = [dict(x, amount=yuan(x.pop('amount_cents'))) for x in coupons_for_cart(db, a, store, d, now=at)]
    if not has_perm(a, 'goods.cost.view'):
        for l in res['lines']:
            l['cost'] = None
    return ok(res)


# ================================================================ 价格体系 / 调价单
@bp.route('/prices')
@require_perm('promo.view')
def prices_list():
    db, a = get_db(), current_actor()
    sql = ('SELECT pp.*, p.code product_code, p.name product_name FROM product_prices pp JOIN products p ON p.id=pp.product_id '
           'WHERE pp.merchant_id=? AND p.merchant_id=?')
    args = [a['merchant_id'], a['merchant_id']]
    for k, col in (('scope_type', 'pp.scope_type'), ('scope_ref', 'pp.scope_ref'), ('code', 'p.code')):
        if request.args.get(k):
            sql += ' AND %s=?' % col
            args.append(request.args[k])
    page, size = _page()

    def conv(r):
        d = dict(r)
        d['price'] = yuan(d.pop('price_cents'))
        return d
    return ok(_paged(db, sql + ' ORDER BY p.code, pp.scope_type, pp.scope_ref, pp.start_at', args, page, size, conv))


def _write_prices(db, pa, actor):
    """调价单审批通过：写 product_prices（start_at=生效时间，到点自动生效，不改历史订单）。"""
    mid = pa['merchant_id']
    refs = [x for x in (pa['scope_refs'] or '').split(',') if x] or ['*']
    for ln in db.execute('SELECT * FROM price_adjust_lines WHERE merchant_id=? AND pa_no=? ORDER BY id', (mid, pa['pa_no'])).fetchall():
        if ln['field'] == 'level_price':
            targets = [('level', ln['level'])]
        elif pa['scope_type'] == 'hq':
            targets = [('member' if ln['field'] == 'member_price' else 'hq', '*')]
        elif ln['field'] == 'member_price':
            raise BizError('区域/门店调价单只能调零售价，会员价请用总部调价或等级价')
        else:
            targets = [(pa['scope_type'], r) for r in refs]
        for st, ref in targets:
            db.execute('INSERT INTO product_prices(merchant_id, product_id, unit, scope_type, scope_ref, price_cents, start_at, end_at, src_no, updated_at) '
                       "VALUES(?,?,'',?,?,?,?,'',?,?) ON CONFLICT(merchant_id, product_id, unit, scope_type, scope_ref, start_at) "
                       'DO UPDATE SET price_cents=excluded.price_cents, src_no=excluded.src_no, updated_at=excluded.updated_at',
                       (mid, ln['product_id'], st, ref, ln['new_cents'], pa['effective_at'], pa['pa_no'], now_s()))
    if pa['effective_at'] <= now_s():
        db.execute("UPDATE price_adjusts SET status='effective', effected_at=? WHERE id=?", (now_s(), pa['id']))
    audit(db, actor, 'promo', '调价单生效登记', pa['pa_no'], None, {'effective_at': pa['effective_at']}, level='high', store_code='')


def mark_due_price_adjusts(db, mid):
    """到点的已审核调价单标记为已生效（价格本身由 start_at 自动生效，这里只更新单据状态）。"""
    db.execute("UPDATE price_adjusts SET status='effective', effected_at=? WHERE merchant_id=? AND status='approved' AND effective_at<=?",
               (now_s(), mid, now_s()))


def _pa_json(db, r):
    d = dict(r)
    d['status_name'] = PA_STATUS.get(d['status'], d['status'])
    d['lines'] = []
    for ln in db.execute('SELECT l.*, p.code, p.name FROM price_adjust_lines l JOIN products p ON p.id=l.product_id WHERE l.merchant_id=? AND l.pa_no=? '
                         'ORDER BY l.id', (r['merchant_id'], r['pa_no'])):
        x = dict(ln)
        x['old_price'], x['new_price'] = yuan(x.pop('old_cents')), yuan(x.pop('new_cents'))
        d['lines'].append(x)
    return d


@bp.route('/price-adjusts')
@require_perm('price.adjust', 'price.adjust.approve')
def price_adjusts_list():
    db, a = get_db(), current_actor()
    with tx(db):
        mark_due_price_adjusts(db, a['merchant_id'])
    sql, args = 'SELECT * FROM price_adjusts WHERE merchant_id=?', [a['merchant_id']]
    if request.args.get('status'):
        sql += ' AND status=?'
        args.append(request.args['status'])
    page, size = _page()
    return ok(_paged(db, sql + ' ORDER BY id DESC', args, page, size, lambda r: _pa_json(db, r)))


@bp.route('/price-adjusts/<no>')
@require_perm('price.adjust', 'price.adjust.approve')
def price_adjust_get(no):
    db, a = get_db(), current_actor()
    r = db.execute('SELECT * FROM price_adjusts WHERE merchant_id=? AND pa_no=?', (a['merchant_id'], no)).fetchone()
    if r is None:
        raise BizError('调价单不存在', 404, 'NOT_FOUND')
    return ok(_pa_json(db, r))


@bp.route('/price-adjusts', methods=['POST'])
@require_perm('price.adjust')
def price_adjust_create():
    """{scope_type:'hq'|'region'|'store', scope_refs:['A001'], effective_at, reason,
        lines:[{code, field:'retail'|'member_price'|'level_price', level?, new_price}]} -> 草稿调价单（保存调前价）。"""
    from . import goods_svc
    from .promo_engine import _price_list, _store_region
    db, a = get_db(), current_actor()
    d = _data()
    mid = a['merchant_id']
    st = d.get('scope_type') or 'hq'
    if st not in ('hq', 'region', 'store'):
        raise BizError('调价范围只能是 hq/region/store')
    refs = [str(x).strip() for x in (d.get('scope_refs') or []) if str(x).strip()]
    if st != 'hq' and not refs:
        raise BizError('请选择调价的区域/门店')
    if st == 'store':
        scope_stores(db, a, ','.join(refs))
    if st == 'region':
        for r in refs:
            if not db.execute("SELECT 1 FROM org_units WHERE merchant_id=? AND code=?", (mid, r)).fetchone():
                raise BizError('区域不存在：%s' % r)
    eff = _dt(d.get('effective_at') or now_s(), '生效时间')
    lines = d.get('lines') or []
    if not lines or len(lines) > 5000:
        raise BizError('调价明细 1~5000 行')
    with tx(db):
        no = next_no(db, mid, 'PA', '')
        db.execute("INSERT INTO price_adjusts(pa_no, merchant_id, scope_type, scope_refs, effective_at, status, reason, created_by, created_at) "
                   "VALUES(?,?,?,?,?,'draft',?,?,?)", (no, mid, st, ','.join(refs), eff, (d.get('reason') or '')[:200], a['name'], now_s()))
        for i, ln in enumerate(lines):
            sku = goods_svc.find_sku(db, mid, ln.get('code'))
            if sku is None:
                raise BizError('第 %d 行商品不存在：%s' % (i + 1, ln.get('code')))
            p = sku['product']
            field = ln.get('field') or 'retail'
            if field not in ('retail', 'member_price', 'level_price'):
                raise BizError('第 %d 行调价字段只能是 retail/member_price/level_price' % (i + 1))
            level = (ln.get('level') or '').strip()
            if field == 'level_price' and not level:
                raise BizError('第 %d 行等级价要填会员等级' % (i + 1))
            new = _cents(ln.get('new_price'), '第 %d 行新价' % (i + 1), False)
            # 调前价：按单据范围的第一个对象取当前生效价
            ref0 = refs[0] if refs else '*'
            if field == 'level_price':
                o = _price_list(db, mid, p['id'], '', 'level', level, eff)
                old = o['price_cents'] if o else int(p['member_price_cents'] or 0)
            elif field == 'member_price':
                o = _price_list(db, mid, p['id'], '', 'member', '*', eff)
                old = o['price_cents'] if o else int(p['member_price_cents'] or 0)
            else:
                o = None
                if st == 'store':
                    o = _price_list(db, mid, p['id'], '', 'store', ref0, eff) or _price_list(db, mid, p['id'], '', 'region',
                                                                                             _store_region(db, mid, ref0), eff)
                elif st == 'region':
                    o = _price_list(db, mid, p['id'], '', 'region', ref0, eff)
                o = o or _price_list(db, mid, p['id'], '', 'hq', '*', eff)
                old = o['price_cents'] if o else int(p['retail_cents'] or 0)
            db.execute('INSERT INTO price_adjust_lines(merchant_id, pa_no, product_id, field, level, old_cents, new_cents) VALUES(?,?,?,?,?,?,?)',
                       (mid, no, p['id'], field, level, old, new))
        audit(db, a, 'promo', '新建调价单', no, None, {'lines': len(lines), 'effective_at': eff}, store_code='')
    return price_adjust_get(no)


@bp.route('/price-adjusts/<no>/submit', methods=['POST'])
@require_perm('price.adjust')
def price_adjust_submit(no):
    db, a = get_db(), current_actor()
    with tx(db):
        r = db.execute('SELECT * FROM price_adjusts WHERE merchant_id=? AND pa_no=?', (a['merchant_id'], no)).fetchone()
        if r is None:
            raise BizError('调价单不存在', 404, 'NOT_FOUND')
        if r['status'] not in ('draft', 'rejected'):
            raise BizError('只有草稿可以提交', 409, 'CONFLICT')
        db.execute("UPDATE price_adjusts SET status='submitted' WHERE id=?", (r['id'],))
        approval.create(db, a, 'price_adjust', '', '调价单 %s' % no, ref_no=no, ref_type='price_adjust', need_perm='price.adjust.approve',
                        reason=r['reason'] or '')
    return price_adjust_get(no)


@bp.route('/price-adjusts/<no>/approve', methods=['POST'])
@require_perm('price.adjust.approve')
def price_adjust_approve(no):
    db, a = get_db(), current_actor()
    with tx(db):
        _decide_ref(db, a, 'price_adjust', no, True, _data().get('reason') or '')
    return price_adjust_get(no)


@bp.route('/price-adjusts/<no>/reject', methods=['POST'])
@require_perm('price.adjust.approve')
def price_adjust_reject(no):
    db, a = get_db(), current_actor()
    with tx(db):
        _decide_ref(db, a, 'price_adjust', no, False, _data().get('reason') or '')
        db.execute("UPDATE price_adjusts SET status='rejected' WHERE merchant_id=? AND pa_no=? AND status='submitted'", (a['merchant_id'], no))
    return price_adjust_get(no)


# ================================================================ 优惠券
def _tpl_json(r):
    from .promo_engine import COUPON_TYPE_NAME
    d = dict(r)
    d['scope'] = json.loads(d.get('scope') or '{}')
    d['face_value'] = yuan(d.get('face_value_cents'))
    d['threshold'] = yuan(d.get('threshold_cents'))
    d['max_discount'] = yuan(d.get('max_discount_cents'))
    d['type_name'] = COUPON_TYPE_NAME.get(d.get('type'), d.get('type'))
    for k in ('face_value_cents', 'threshold_cents', 'max_discount_cents'):
        d.pop(k, None)
    return d


@bp.route('/coupon-templates')
@require_perm('coupon.edit', 'coupon.issue', 'promo.view')
def coupon_templates():
    db, a = get_db(), current_actor()
    sql, args = 'SELECT * FROM coupon_templates WHERE merchant_id=?', [a['merchant_id']]
    if request.args.get('status'):
        sql += ' AND status=?'
        args.append(request.args['status'])
    page, size = _page()
    return ok(_paged(db, sql + ' ORDER BY id DESC', args, page, size, _tpl_json))


@bp.route('/coupon-templates', methods=['POST'])
@require_perm('coupon.edit')
def coupon_template_save():
    db, a = get_db(), current_actor()
    d = _data()
    mid = a['merchant_id']
    typ = d.get('type') or 'cash'
    if typ not in ('cash', 'discount', 'free', 'product'):
        raise BizError('券类型只能是 cash 满减 / discount 折扣 / free 无门槛 / product 商品券')
    name = (d.get('name') or '').strip()[:40]
    if not name:
        raise BizError('请填写券名称')
    face = _cents(d.get('face_value') or 0, '面额')
    thr = _cents(d.get('threshold') or 0, '使用门槛')
    rate = None
    if typ == 'discount':
        rate = float(_rate(d.get('discount_rate'), '折扣率'))
    elif face <= 0:
        raise BizError('面额必须大于 0')
    if typ == 'free':
        thr = 0
    if typ == 'cash' and thr and face >= thr:
        raise BizError('满减券面额必须小于门槛')
    scope = _scope_in(d.get('scope'))
    if typ == 'product' and not (scope.get('product_codes') or scope.get('categories') or scope.get('brands')):
        raise BizError('商品券必须指定适用商品/分类/品牌')
    try:
        valid_days = int(d.get('valid_days') or 30)
        per_limit, total_limit = int(d.get('per_member_limit') or 0), int(d.get('total_limit') or 0)
    except (TypeError, ValueError):
        raise BizError('有效天数/限领数量必须是整数')
    sd = (d.get('start_date') or '')[:10]
    ed = (d.get('end_date') or '')[:10]
    status = d.get('status') or '启用'
    if status not in ('启用', '停用'):
        raise BizError('状态只能是 启用/停用')
    vals = (name, face / 100.0, face, thr / 100.0, thr, valid_days, status, typ, rate, json.dumps(scope, ensure_ascii=False), per_limit, total_limit,
            0 if d.get('return_on_refund') in (False, 0, '0') else 1, sd, ed, _cents(d.get('max_discount') or 0, '封顶金额'))
    with tx(db):
        if d.get('id'):
            old = db.execute('SELECT * FROM coupon_templates WHERE merchant_id=? AND id=?', (mid, d['id'])).fetchone()
            if old is None:
                raise BizError('券模板不存在', 404, 'NOT_FOUND')
            db.execute('UPDATE coupon_templates SET name=?, face_value=?, face_value_cents=?, threshold=?, threshold_cents=?, valid_days=?, status=?, '
                       'type=?, discount_rate=?, scope=?, per_member_limit=?, total_limit=?, return_on_refund=?, start_date=?, end_date=?, '
                       'max_discount_cents=? WHERE id=?', vals + (old['id'],))
            tid = old['id']
            audit(db, a, 'coupon', '修改券模板', str(tid), _tpl_json(old), {'name': name, 'type': typ}, store_code='')
        else:
            db.execute('INSERT INTO coupon_templates(name, face_value, face_value_cents, threshold, threshold_cents, valid_days, status, type, '
                       'discount_rate, scope, per_member_limit, total_limit, return_on_refund, start_date, end_date, max_discount_cents, merchant_id, '
                       'created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', vals + (mid, now_s()))
            tid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            audit(db, a, 'coupon', '新建券模板', str(tid), None, {'name': name, 'type': typ}, store_code='')
    return ok(_tpl_json(db.execute('SELECT * FROM coupon_templates WHERE id=?', (tid,)).fetchone()))


def issue_coupons(db, actor, template_id, member_codes, ref='', note='发放'):
    """发券（ERP-603 限领 / 总量；ERP-604 每张写流水）。在调用方事务内。返回 {issued:[...], skipped:[{member_code, reason}]}。"""
    from .promo_engine import coupon_log
    from .member_svc import benefits_block_reason
    mid = actor['merchant_id']
    t = db.execute('SELECT * FROM coupon_templates WHERE merchant_id=? AND id=?', (mid, template_id)).fetchone()
    if t is None:
        raise BizError('券模板不存在', 404, 'NOT_FOUND')
    if t['status'] != '启用':
        raise BizError('券模板已停用')
    today = datetime.now().strftime('%Y-%m-%d')
    if t['end_date'] and t['end_date'] < today:
        raise BizError('券模板已过发放期')
    exp = t['end_date'] or (datetime.now() + timedelta(days=int(t['valid_days'] or 30))).strftime('%Y-%m-%d')
    if t['end_date'] and int(t['valid_days'] or 0) > 0:
        exp = min(t['end_date'], (datetime.now() + timedelta(days=int(t['valid_days']))).strftime('%Y-%m-%d'))
    issued, skipped = [], []
    for code in member_codes:
        m = db.execute('SELECT * FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
        if m is None:
            skipped.append({'member_code': code, 'reason': '会员不存在'})
            continue
        why = benefits_block_reason(m)
        if why:
            skipped.append({'member_code': code, 'reason': why})
            continue
        if int(t['per_member_limit'] or 0) > 0:
            n = db.execute('SELECT COUNT(*) FROM coupons WHERE merchant_id=? AND template_id=? AND member_code=?', (mid, t['id'], code)).fetchone()[0]
            if n >= int(t['per_member_limit']):
                skipped.append({'member_code': code, 'reason': '已达每人限领 %d 张' % int(t['per_member_limit'])})
                continue
        c = db.execute('UPDATE coupon_templates SET issued_count = issued_count + 1 WHERE id=? AND (total_limit=0 OR issued_count < total_limit)',
                       (t['id'],))
        if c.rowcount != 1:
            skipped.append({'member_code': code, 'reason': '已达发放总量 %d 张' % int(t['total_limit'])})
            continue
        cno = next_no(db, mid, 'CP', m['reg_store_code'] or '')
        db.execute("INSERT INTO coupons(merchant_id, coupon_no, template_id, template_name, member_code, face_value, face_value_cents, threshold, "
                   "threshold_cents, expire_date, status, created_at, issue_ref) VALUES(?,?,?,?,?,?,?,?,?,?,'未使用',?,?)",
                   (mid, cno, t['id'], t['name'], code, (t['face_value_cents'] or 0) / 100.0, t['face_value_cents'] or 0,
                    (t['threshold_cents'] or 0) / 100.0, t['threshold_cents'] or 0, exp, now_s(), ref or ''))
        coupon_log(db, mid, cno, None, '未使用', ref, actor.get('name', ''), note)
        issued.append(cno)
    return {'issued': issued, 'skipped': skipped}


@bp.route('/coupons/issue', methods=['POST'])
@require_perm('coupon.issue')
def coupons_issue():
    """{template_id, member_codes:[...] | member_code, request_key?, note?}"""
    db, a = get_db(), current_actor()
    d = _data()
    codes = d.get('member_codes') or ([d['member_code']] if d.get('member_code') else [])
    codes = [str(c).strip() for c in codes if str(c).strip()]
    if not codes or len(codes) > 5000:
        raise BizError('请选择 1~5000 个会员')
    rk = (d.get('request_key') or '').strip()[:64]
    with tx(db):
        if rk and db.execute('SELECT 1 FROM coupons WHERE merchant_id=? AND issue_ref=? LIMIT 1', (a['merchant_id'], 'RK:' + rk)).fetchone():
            issued = [r[0] for r in db.execute('SELECT coupon_no FROM coupons WHERE merchant_id=? AND issue_ref=? ORDER BY id',
                                               (a['merchant_id'], 'RK:' + rk))]
            return ok({'issued': issued, 'skipped': [], 'duplicate': True})
        res = issue_coupons(db, a, d.get('template_id'), codes, ref='RK:' + rk if rk else '', note=(d.get('note') or '发放')[:100])
        audit(db, a, 'coupon', '发券', str(d.get('template_id')), None, {'issued': len(res['issued']), 'skipped': len(res['skipped'])},
              store_code='')
    return ok(res)


def _coupon_json(r):
    d = dict(r)
    d['face_value'] = yuan(d.get('face_value_cents'))
    d['threshold'] = yuan(d.get('threshold_cents'))
    for k in ('face_value_cents', 'threshold_cents'):
        d.pop(k, None)
    return d


@bp.route('/coupons')
@require_perm('promo.view', 'coupon.issue', 'coupon.void')
def coupons_list():
    db, a = get_db(), current_actor()
    sql = ('SELECT coupon_no, template_id, template_name, member_code, face_value_cents, threshold_cents, expire_date, status, order_no, used_at, '
           'store_code, locked_order_no, created_at FROM coupons WHERE merchant_id=?')
    args = [a['merchant_id']]
    for k in ('member_code', 'status', 'template_id', 'order_no'):
        if request.args.get(k):
            sql += ' AND %s=?' % k
            args.append(request.args[k])
    page, size = _page()
    return ok(_paged(db, sql + ' ORDER BY id DESC', args, page, size, _coupon_json))


@bp.route('/coupons/<no>')
@require_perm('promo.view', 'coupon.issue', 'coupon.void')
def coupon_get(no):
    db, a = get_db(), current_actor()
    r = db.execute('SELECT * FROM coupons WHERE merchant_id=? AND coupon_no=?', (a['merchant_id'], no)).fetchone()
    if r is None:
        raise BizError('优惠券不存在', 404, 'NOT_FOUND')
    d = _coupon_json(r)
    d['logs'] = [dict(x) for x in db.execute('SELECT from_status, to_status, ref_no, operator, note, created_at FROM coupon_logs '
                                             'WHERE merchant_id=? AND coupon_no=? ORDER BY id', (a['merchant_id'], no))]
    return ok(d)


@bp.route('/coupons/<no>/void', methods=['POST'])
@require_perm('coupon.void')
def coupon_void(no):
    from .promo_engine import coupon_log
    db, a = get_db(), current_actor()
    reason = (_data().get('reason') or '').strip()
    if not reason:
        raise BizError('作废必须填写原因')
    with tx(db):
        c = db.execute("UPDATE coupons SET status='已作废', void_reason=? WHERE merchant_id=? AND coupon_no=? AND status='未使用'",
                       (reason[:100], a['merchant_id'], no))
        if c.rowcount != 1:
            if not db.execute('SELECT 1 FROM coupons WHERE merchant_id=? AND coupon_no=?', (a['merchant_id'], no)).fetchone():
                raise BizError('优惠券不存在', 404, 'NOT_FOUND')
            raise BizError('只有未使用的券可以作废', 409, 'CONFLICT')
        coupon_log(db, a['merchant_id'], no, '未使用', '已作废', '', a['name'], reason)
        audit(db, a, 'coupon', '作废优惠券', no, {'status': '未使用'}, {'status': '已作废'}, reason, level='high', store_code='')
    return coupon_get(no)


@bp.route('/coupons/expire', methods=['POST'])
@require_perm('coupon.void')
def coupons_expire():
    """把过期未使用的券标为已过期（定时任务或手动），每张写流水。"""
    from .promo_engine import coupon_log
    db, a = get_db(), current_actor()
    today = datetime.now().strftime('%Y-%m-%d')
    with tx(db):
        rows = db.execute("SELECT coupon_no FROM coupons WHERE merchant_id=? AND status='未使用' AND expire_date!='' AND expire_date<?",
                          (a['merchant_id'], today)).fetchall()
        for r in rows:
            db.execute("UPDATE coupons SET status='已过期' WHERE merchant_id=? AND coupon_no=? AND status='未使用'", (a['merchant_id'], r[0]))
            coupon_log(db, a['merchant_id'], r[0], '未使用', '已过期', '', a['name'], '到期')
    return ok({'expired': len(rows)})
