# -*- coding: utf-8 -*-
"""收银台接口 /api/pos/*（模块③）。契约见 docs/API-CONTRACT.md §4.1。

返回 {ok:true, data:{...}}；为兼容 v1 前端与旧测试，部分接口同时平铺旧字段（goods / member / rows / order / shift ...）。
业务全部在 trade.py；这里只做：参数、权限码、门店范围、事务边界。
"""
import json
from datetime import datetime

from flask import Blueprint, request, session

from . import trade
from .core import (get_db, tx, current_actor, require_perm, api, ok, get_config, scope_stores, store_row, has_perm, audit, mask_phone,
                   PAY_METHODS, new_code, within_limit, _load_actor, lock_remaining, record_attempt, start_session, merchant_row, client_ip,
                   LIMIT_KIND)
from .util import BizError, now_s, inum

bp = Blueprint('pos', __name__, url_prefix='/api/pos')

VIEW_ORDERS = ('pos.checkout', 'report.sales')


def _json():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _store(actor, requested=None):
    return trade.pos_store(get_db(), actor, requested, session.get('pos_store'))


# ---------------------------------------------------------------- 登录（员工编号 + PIN，POS-001）
@bp.route('/login-pin', methods=['POST'])
def login_pin():
    from flask import jsonify
    from werkzeug.security import check_password_hash
    db = get_db()
    d = _json()
    store_code = (d.get('store_code') or '').strip()
    emp_no = (d.get('emp_no') or '').strip()
    pin = str(d.get('pin') or '')
    term = (d.get('terminal_no') or '').strip()[:20]

    def fail(msg, status=400, code=None):
        body = {'ok': False, 'code': code or {401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 429: 'TOO_MANY'}.get(status, 'BIZ_ERROR'), 'msg': msg, 'error': msg}
        return jsonify(body), status

    if not store_code or not emp_no or not pin:
        return fail('请输入门店编码、员工编号和 PIN')
    mcode = (d.get('merchant_code') or '').strip()
    if mcode:
        mids = [r[0] for r in db.execute('SELECT id FROM merchants WHERE code=?', (mcode,))]
    else:
        mids = sorted(set(r[0] for r in db.execute('SELECT merchant_id FROM stores WHERE code=?', (store_code,))))
    if len(mids) != 1:
        return fail('无法确定商户：请先用账号密码登录一次，或在收银台设置里填写商户编码')
    mid = int(mids[0])
    key = 'pin:%d:%s' % (mid, emp_no)
    left = lock_remaining(db, key, client_ip())
    if left > 0:
        return fail('PIN 错误次数过多，请 %d 分钟后再试' % ((left + 59) // 60), 429)
    u = db.execute("SELECT * FROM users WHERE merchant_id=? AND emp_no=? AND emp_no != ''", (mid, emp_no)).fetchone()
    good = u is not None and u['status'] == '启用' and bool(u['pin_hash']) and check_password_hash(u['pin_hash'], pin)
    record_attempt(db, key, client_ip(), good)
    if not good:
        return fail('员工编号或 PIN 错误', 401)
    m = merchant_row(db, mid)
    if m is None or m['status'] == 'disabled':
        return fail('商户已停用，请联系平台', 403, 'MERCHANT_DISABLED')
    a = _load_actor(db, u)
    if 'pos.login' not in a['perms']:
        return fail('该员工没有收银台登录权限', 403)
    if store_code not in scope_stores(db, a):
        return fail('该员工不属于门店 %s' % store_code, 403, 'SCOPE_DENIED')
    start_session(db, u, 'pos', term or request.headers.get('User-Agent', '')[:100])
    session['pos_store'] = store_code
    db.execute('UPDATE users SET last_login=? WHERE id=?', (now_s(), u['id']))
    audit(db, a, 'pos', 'PIN登录', '', None, {'store': store_code, 'terminal': term}, store_code=store_code, terminal_no=term)
    info = {'username': u['username'], 'name': u['name'], 'merchant_id': mid, 'role_code': a['role_code'], 'is_platform': False}
    return ok(info, user=u['username'], name=u['name'], role=a['role'])


@bp.route('/pin', methods=['POST'])
@require_perm('pos.login')
def set_pin():
    """收银员自助设置 PIN（需当前登录密码）。员工编号由后台（模块④）分配。"""
    from werkzeug.security import check_password_hash, generate_password_hash
    db = get_db()
    actor = current_actor()
    d = _json()
    pin = str(d.get('pin') or '')
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        raise BizError('PIN 必须是 4~8 位数字')
    u = db.execute('SELECT * FROM users WHERE id=?', (actor['id'],)).fetchone()
    if not check_password_hash(u['password_hash'], d.get('password') or ''):
        raise BizError('登录密码不正确', 403)
    with tx(db):
        db.execute('UPDATE users SET pin_hash=? WHERE id=?', (generate_password_hash(pin), actor['id']))
        audit(db, actor, 'pos', '设置PIN', '', None, None, level='security')
    return ok({'emp_no': u['emp_no'] or ''})


# ---------------------------------------------------------------- 上下文 / 门店
def _print_template(db, mid, store_code, cfg):
    try:
        from . import sys_svc        # 模块④提供；没有时退回商户收银参数里的小票抬头/尾注
        t = sys_svc.get_print_template(db, mid, store_code, 'receipt')
        if t:
            return t
    except Exception:
        pass
    return {'width': int(cfg.get('receipt_width') or 58), 'copies': 1, 'version': 0,
            'content': {'title': cfg.get('receipt_title', ''), 'footer': cfg.get('receipt_footer', '')}}


@bp.route('/context')
@require_perm('pos.login')
def context():
    db = get_db()
    actor = current_actor()
    store_code = _store(actor, request.args.get('store'))
    session['pos_store'] = store_code
    mid = actor['merchant_id']
    st = store_row(db, store_code, mid)
    if st is None:
        raise BizError('还没有门店，请先到管理后台新建门店')
    codes = scope_stores(db, actor)
    stores = [dict(r) for r in db.execute("SELECT code, name FROM stores WHERE merchant_id=? AND status='营业' ORDER BY sort, code", (mid,))
              if r['code'] in codes]
    if not any(s['code'] == store_code for s in stores):
        stores.insert(0, {'code': st['code'], 'name': st['name']})
    sh = trade.current_shift(db, store_code, actor['username'], mid)
    guides = [{'emp_no': r['code'], 'guide_code': r['code'], 'emp_name': r['name'], 'role': '导购'} for r in db.execute(
        "SELECT code, name FROM guides WHERE merchant_id=? AND store_code=? AND status='在职' ORDER BY code", (mid, store_code))]
    cfg = get_config(db, mid)
    term = (request.args.get('terminal_no') or '').strip()
    terminal = db.execute('SELECT * FROM terminals WHERE merchant_id=? AND terminal_no=?', (mid, term)).fetchone() if term else None
    m = merchant_row(db, mid) or {}
    pos_perms = sorted(p for p in actor['perms'] if p.startswith('pos.') or p in ('member.stored.view', 'member.phone.view', 'goods.cost.view'))
    data = {'user': actor['username'], 'name': actor['name'], 'role': actor['role'], 'role_code': actor['role_code'], 'role_name': actor['role_name'],
            'merchant_id': mid, 'merchant_code': m.get('code', ''), 'merchant_name': m.get('name', ''),
            'store': st, 'stores': stores, 'can_switch_store': len(stores) > 1, 'shift': trade.shift_view(db, sh) if sh else None,
            'guides': guides, 'config': cfg, 'pay_methods': [{'code': c, 'name': n} for c, n in PAY_METHODS],
            'print_template': _print_template(db, mid, store_code, cfg), 'perms': pos_perms, 'limits': actor['limits'],
            'terminal': dict(terminal) if terminal else None, 'server_time': now_s()}
    return ok(data, **data)


@bp.route('/store', methods=['POST'])
@require_perm('pos.login')
def switch_store():
    db = get_db()
    actor = current_actor()
    code = (_json().get('store') or '').strip()
    scope_stores(db, actor, code or '-')
    if store_row(db, code, actor['merchant_id']) is None:
        raise BizError('门店不存在')
    sh = db.execute("SELECT 1 FROM shifts WHERE merchant_id=? AND cashier_user=? AND status='open' AND store_code != ?",
                    (actor['merchant_id'], actor['username'], code)).fetchone()
    if sh is not None:
        raise BizError('你在其他门店还有未交班的班次，请先交班再切换门店', 409, 'CONFLICT')
    session['pos_store'] = code
    return ok({'store': code})


# ---------------------------------------------------------------- 收银台 / 设备
@bp.route('/terminals', methods=['GET', 'POST'])
@require_perm('pos.login', 'org.edit')
def terminals():
    db = get_db()
    actor = current_actor()
    mid = actor['merchant_id']
    if request.method == 'POST':
        if not has_perm(actor, 'org.edit'):
            raise BizError('没有维护收银台的权限（需要 org.edit）', 403)
        d = _json()
        term = (d.get('terminal_no') or '').strip()[:20]
        if not term:
            raise BizError('请填写收银台编号')
        sc = (d.get('store_code') or '').strip()
        scope_stores(db, actor, sc or '-')
        width = inum(d.get('paper_width'), 58)
        if width not in (58, 80):
            raise BizError('小票纸宽只能是 58 或 80 毫米')
        status = d.get('status') or '启用'
        if status not in ('启用', '停用'):
            raise BizError('状态只能是 启用/停用')
        with tx(db):
            old = db.execute('SELECT * FROM terminals WHERE merchant_id=? AND terminal_no=?', (mid, term)).fetchone()
            vals = (sc, (d.get('name') or '')[:40], (d.get('printer') or '')[:60], width, 1 if d.get('cash_drawer', True) else 0,
                    1 if d.get('customer_display') else 0, status)
            if old is not None and old['store_code'] not in scope_stores(db, actor):
                raise BizError('收银台 %s 属于门店 %s，不在你的管理范围' % (term, old['store_code']), 403, 'SCOPE_DENIED')
            if old is None:
                db.execute('INSERT INTO terminals(store_code, name, printer, paper_width, cash_drawer, customer_display, status, merchant_id, '
                           'terminal_no, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)', vals + (mid, term, now_s()))
            else:
                if old['store_code'] != sc and db.execute("SELECT 1 FROM shifts WHERE merchant_id=? AND terminal_no=? AND status='open'",
                                                          (mid, term)).fetchone():
                    raise BizError('收银台还有未交班的班次，不能改门店', 409, 'CONFLICT')
                db.execute('UPDATE terminals SET store_code=?, name=?, printer=?, paper_width=?, cash_drawer=?, customer_display=?, status=? '
                           'WHERE merchant_id=? AND terminal_no=?', vals + (mid, term))
            audit(db, actor, 'pos', '维护收银台', term, dict(old) if old else None, dict(zip(
                ('store_code', 'name', 'printer', 'paper_width', 'cash_drawer', 'customer_display', 'status'), vals)), store_code=sc)
        return ok(dict(db.execute('SELECT * FROM terminals WHERE merchant_id=? AND terminal_no=?', (mid, term)).fetchone()))
    stores = scope_stores(db, actor)
    req = (request.args.get('store') or '').strip()
    if req:
        stores = scope_stores(db, actor, req)
    if not stores:
        return ok([], rows=[])
    rows = [dict(r) for r in db.execute('SELECT * FROM terminals WHERE merchant_id=? AND store_code IN (%s) ORDER BY store_code, terminal_no'
                                        % ','.join('?' * len(stores)), [mid] + stores)]
    for r in rows:
        o = db.execute("SELECT id, cashier_name, open_time FROM shifts WHERE merchant_id=? AND terminal_no=? AND status='open'", (mid, r['terminal_no'])).fetchone()
        r['open_shift'] = dict(o) if o else None
    return ok(rows, rows=rows)


@bp.route('/terminals/check', methods=['POST'])
@require_perm('pos.device')
def terminal_check():
    """设备检测记录（POS-806）：扫码枪/小票机/钱箱/客显的测试结果、型号、驱动。"""
    db = get_db()
    actor = current_actor()
    d = _json()
    store_code = _store(actor)
    device = (d.get('device') or '').strip()
    if device not in ('scanner', 'printer', 'cash_drawer', 'customer_display', 'other'):
        raise BizError('设备类型不正确')
    result = (d.get('result') or '').strip()
    if result not in ('ok', 'fail'):
        raise BizError('检测结果只能是 ok / fail')
    term = (d.get('terminal_no') or '').strip()[:20]
    with tx(db):
        db.execute('INSERT INTO device_checks(merchant_id, store_code, terminal_no, device, model, driver, result, detail, operator, created_at) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?)', (actor['merchant_id'], store_code, term, device, (d.get('model') or '')[:60],
                                                  (d.get('driver') or '')[:60], result, (d.get('detail') or '')[:300], actor['username'], now_s()))
    rows = [dict(r) for r in db.execute('SELECT * FROM device_checks WHERE merchant_id=? AND store_code=? ORDER BY id DESC LIMIT 20',
                                        (actor['merchant_id'], store_code))]
    return ok(rows, rows=rows)


# ---------------------------------------------------------------- 商品
@bp.route('/goods/scan')
@require_perm('pos.checkout')
def goods_scan():
    db = get_db()
    actor = current_actor()
    d = trade.scan_info(db, actor, _store(actor), request.args.get('code'))
    return ok(d, goods=d)


@bp.route('/goods/search')
@require_perm('pos.checkout')
def goods_search():
    db = get_db()
    actor = current_actor()
    rows, cats = trade.search_goods(db, actor, _store(actor), (request.args.get('kw') or '').strip(), (request.args.get('cat') or '').strip())
    return ok({'items': rows, 'cats': cats}, rows=rows, cats=cats)


# ---------------------------------------------------------------- 会员
@bp.route('/member/lookup')
@require_perm('pos.checkout')
def member_lookup():
    from . import member_svc
    db = get_db()
    actor = current_actor()
    mid = actor['merchant_id']
    kw = (request.args.get('kw') or '').strip()
    if not kw:
        raise BizError('请输入手机号或会员号')
    brief = getattr(member_svc, 'member_brief', None)
    if brief is not None:
        m = brief(db, actor, mid, kw)
        if m:
            return ok(m, member=m)
    rows = db.execute("SELECT code FROM members WHERE merchant_id=? AND (phone=? OR code=? OR (card_no=? AND card_no != '')) LIMIT 1",
                      (mid, kw, kw, kw)).fetchall()
    if not rows and len(kw) >= 4:
        rows = db.execute('SELECT code FROM members WHERE merchant_id=? AND (phone LIKE ? OR name LIKE ?) LIMIT 10',
                          (mid, '%' + kw, '%' + kw + '%')).fetchall()
    if not rows:
        raise BizError('未找到会员，可点「新会员」登记', 404)
    if len(rows) == 1:
        m = trade.member_card(db, actor, rows[0][0])
        return ok(m, member=m)
    cands = []
    for r in rows:
        x = dict(db.execute('SELECT code, phone, name, level FROM members WHERE merchant_id=? AND code=?', (mid, r[0])).fetchone())
        x['phone'] = mask_phone(x['phone'], actor)
        cands.append(x)
    return ok({'candidates': cands}, candidates=cands)


def create_member(db, data, store_code, actor):
    """收银台快速登记会员（POS-302）。模块②提供 member_svc.quick_register 后自动改用它。"""
    from . import member_svc
    qr = getattr(member_svc, 'quick_register', None)
    mid = int(actor['merchant_id'])
    if qr is not None:
        return qr(db, actor, mid, store_code, data)
    phone = (data.get('phone') or '').strip()
    name = (data.get('name') or '').strip()
    if len(phone) != 11 or not phone.isdigit():
        raise BizError('请输入 11 位手机号')
    if db.execute('SELECT 1 FROM members WHERE merchant_id=? AND phone=?', (mid, phone)).fetchone():
        raise BizError('该手机号已是会员')
    st = store_row(db, store_code, mid) or {}
    g = trade.guide_info(db, data.get('guide_code'), mid)
    code = new_code(db, 'members', 'code', 'XB', 8)
    db.execute('''INSERT INTO members(merchant_id,code,phone,name,level,birthday,points,balance,total_charged,reg_time,reg_store_code,reg_store_name,
        guide_code,guide_name,note,status,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (mid, code, phone, name or ('会员' + phone[-4:]), (data.get('level') or '普通会员'), data.get('birthday') or '', 0, 0, 0,
                now_s(), store_code, st.get('name', ''), g['code'], g['name'], (data.get('note') or '')[:200], '正常', '收银台'))
    member_id = db.execute('SELECT id FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone()[0]
    for key in ('baby_birthday', 'due_date'):
        bb = (data.get(key) or '').strip()
        if bb:
            try:
                datetime.strptime(bb, '%Y-%m-%d')
            except ValueError:
                raise BizError('日期格式应为 YYYY-MM-DD')
            db.execute('INSERT INTO babies(merchant_id,member_id,name,gender,birthday,note) VALUES(?,?,?,?,?,?)',
                       (mid, member_id, (data.get('baby_name') or '')[:20], (data.get('baby_gender') or '')[:4], bb,
                        '预产期' if key == 'due_date' else ''))
            break
    audit(db, actor, 'pos', '收银台登记会员', code, None, {'phone': mask_phone(phone, {'perms': frozenset()})}, store_code=store_code)
    return code


@bp.route('/member', methods=['POST'])
@require_perm('pos.member.register')
def member_create():
    db = get_db()
    actor = current_actor()
    store_code = _store(actor)
    with tx(db):
        code = create_member(db, _json(), store_code, actor)
    m = trade.member_card(db, actor, code)
    return ok(m, member=m)


def _recharge():
    db = get_db()
    actor = current_actor()
    store_code = _store(actor)
    d = _json()
    with tx(db):
        res = trade.do_recharge(db, d, store_code, actor)
    m = trade.member_card(db, actor, (d.get('member_code') or '').strip())
    res['member'] = m
    res['config'] = {'title': get_config(db, actor['merchant_id']).get('receipt_title')}
    return ok(res, **res)


@bp.route('/recharge', methods=['POST'])
@require_perm('pos.recharge')
def recharge():
    return _recharge()


@bp.route('/member/recharge', methods=['POST'])
@require_perm('pos.recharge')
def member_recharge():
    return _recharge()


# ---------------------------------------------------------------- 计价 / 结算
@bp.route('/price', methods=['POST'])
@require_perm('pos.checkout')
def price():
    db = get_db()
    actor = current_actor()
    d = trade.price_preview(db, actor, _store(actor), _json())
    return ok(d, **d)


def _order_out(db, actor, no, extra):
    o = trade.order_detail(db, no, actor)
    body = {'order': o}
    body.update(extra)
    return ok(body, **body)


@bp.route('/checkout', methods=['POST'])
@require_perm('pos.checkout')
def checkout():
    db = get_db()
    actor = current_actor()
    data = _json()
    store_code = _store(actor)
    with tx(db):
        res = trade.do_checkout(db, data, store_code, actor)
    o = trade.order_detail(db, res['order_no'], actor)
    extra = {'order_no': res['order_no'], 'duplicate': bool(res.get('duplicate'))}
    if res.get('duplicate'):
        extra.update(change=o['change_amount'], payable=o['payable'], paid_total=o['paid_total'])
    else:
        from .money import yuan
        extra.update(change=yuan(res['change']), payable=yuan(res['payable']), paid_total=yuan(res['paid_total']))
    return _order_out(db, actor, res['order_no'], extra)


@bp.route('/refund', methods=['POST'])
@require_perm('pos.refund')
def refund():
    from .money import yuan
    db = get_db()
    actor = current_actor()
    data = _json()
    with tx(db):
        res = trade.do_refund(db, data, actor, _store(actor))
    extra = {'order_no': res['order_no'], 'duplicate': bool(res.get('duplicate'))}
    if not res.get('duplicate'):
        extra.update(refund_amount=yuan(res['refund_amount']), payments=res['payments'], benefits=res.get('benefits'))
    return _order_out(db, actor, res['order_no'], extra)


@bp.route('/refund/no-receipt', methods=['POST'])
@require_perm('pos.refund.no_receipt')
def refund_no_receipt():
    from .money import yuan
    db = get_db()
    actor = current_actor()
    with tx(db):
        res = trade.do_refund_no_receipt(db, _json(), actor, _store(actor))
    extra = {'order_no': res['order_no'], 'duplicate': bool(res.get('duplicate'))}
    if not res.get('duplicate'):
        extra.update(refund_amount=yuan(res['refund_amount']), payments=res['payments'])
    return _order_out(db, actor, res['order_no'], extra)


@bp.route('/exchange', methods=['POST'])
@require_perm('pos.exchange')
def exchange():
    from .money import yuan
    db = get_db()
    actor = current_actor()
    with tx(db):
        res = trade.do_exchange(db, _json(), actor, _store(actor))
    if res.get('duplicate'):
        x = trade.order_detail(db, res['order_no'], actor)
        snap = x['snapshot'] or {}
        res = {'order_no': x['order_no'], 'refund_no': snap.get('refund_no'), 'sale_no': snap.get('sale_no'), 'duplicate': True}
    else:
        res = dict(res, back_amount=yuan(res['back_amount']), new_amount=yuan(res['new_amount']), diff=yuan(res['diff']), change=yuan(res['change']))
    res['sale_order'] = trade.order_detail(db, res['sale_no'], actor) if res.get('sale_no') else None
    res['refund_order'] = trade.order_detail(db, res['refund_no'], actor) if res.get('refund_no') else None
    return ok(res, **res)


@bp.route('/auth', methods=['POST'])
@require_perm('pos.login')
def auth():
    """现场授权预校验（不消耗、不写业务数据）：授权人账号+密码/PIN 正确、同商户、有 pos.authorize、门店在其范围、额度够。"""
    from werkzeug.security import check_password_hash
    from decimal import Decimal, InvalidOperation
    db = get_db()
    actor = current_actor()
    d = _json()
    store_code = _store(actor)
    kind = (d.get('kind') or '').strip() or None
    if kind and kind not in LIMIT_KIND:
        raise BizError('未知授权类型：%s' % kind)
    value = d.get('value')
    if kind == 'discount':
        try:
            value = str(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            value = '1'
    elif kind in ('refund', 'cash_out', 'points_adjust'):
        value = inum(value)
    au = (d.get('auth_user') or '').strip()
    if not au:
        raise BizError('请输入授权人账号')
    left = lock_remaining(db, 'auth:' + au, '')
    if left > 0:
        raise BizError('授权失败次数过多，请稍后再试', 429)
    row = db.execute('SELECT * FROM users WHERE username=? AND merchant_id=?', (au, actor['merchant_id'])).fetchone()
    good = False
    if row is not None and row['status'] == '启用':
        pw = (d.get('auth_pass') and check_password_hash(row['password_hash'], d.get('auth_pass'))) or \
             (d.get('auth_pin') and row['pin_hash'] and check_password_hash(row['pin_hash'], str(d.get('auth_pin'))))
        if pw:
            a = _load_actor(db, row)
            good = has_perm(a, 'pos.authorize') and store_code in scope_stores(db, a) and \
                (kind is None or within_limit(db, a, kind, value if value is not None else ('1' if kind == 'discount' else 0), store_code))
    with tx(db):
        record_attempt(db, 'auth:' + au, '', good)
    if not good:
        raise BizError('授权人账号/密码错误，或授权人权限、门店范围、额度不足', 403, 'AUTH_FAILED')
    return ok({'auth_user': au}, auth_user=au)


# ---------------------------------------------------------------- 订单
@bp.route('/orders')
@require_perm(*VIEW_ORDERS)
def orders():
    from flask import jsonify
    return jsonify(trade.order_query(get_db(), current_actor(), request.args))


@bp.route('/orders/<order_no>')
@require_perm(*VIEW_ORDERS)
def order_get(order_no):
    db = get_db()
    actor = current_actor()
    d = trade.order_detail(db, order_no, actor)
    if d is None:
        raise BizError('单据不存在', 404)
    trade.order_scope_check(db, actor, d)
    cfg = get_config(db, actor['merchant_id'])
    return ok(d, order=d, config=cfg)


@bp.route('/orders/<order_no>/print', methods=['POST'])
@require_perm('pos.checkout')
def order_print(order_no):
    """记录首次打印结果（POS-506 / AC-006：打印失败不影响已完成订单，可重打）。"""
    db = get_db()
    actor = current_actor()
    d = trade.order_detail(db, order_no, actor)
    if d is None:
        raise BizError('单据不存在', 404)
    trade.order_scope_check(db, actor, d)
    res = (_json().get('result') or 'sent').strip()
    if res not in ('sent', 'ok', 'fail'):
        raise BizError('打印结果只能是 sent/ok/fail')
    with tx(db):
        trade.log_print(db, actor, 'order', order_no, False, _json().get('terminal_no') or '', res)
    return ok({'order_no': order_no, 'result': res, 'status': d['status']})


@bp.route('/orders/<order_no>/reprint', methods=['POST'])
@require_perm('pos.reprint')
def order_reprint(order_no):
    db = get_db()
    actor = current_actor()
    d = trade.order_detail(db, order_no, actor)
    if d is None:
        raise BizError('单据不存在', 404)
    trade.order_scope_check(db, actor, d)
    with tx(db):
        db.execute('UPDATE orders SET reprint_count=COALESCE(reprint_count,0)+1 WHERE order_no=? AND merchant_id=?', (order_no, actor['merchant_id']))
        trade.log_print(db, actor, 'order', order_no, True, _json().get('terminal_no') or '')
        audit(db, actor, 'pos', '重打小票', order_no, None, None, store_code=d['store_code'])
    d = trade.order_detail(db, order_no, actor)
    d['reprint'] = True
    return ok(d, order=d, config=get_config(db, actor['merchant_id']))


# ---------------------------------------------------------------- 挂单（不占库存，POS-107）
@bp.route('/held', methods=['GET', 'POST'])
@require_perm('pos.held')
def held():
    db = get_db()
    actor = current_actor()
    mid = actor['merchant_id']
    store_code = _store(actor)
    if request.method == 'POST':
        data = _json()
        payload = data.get('payload') or {}
        if not isinstance(payload, dict) or not payload.get('items'):
            raise BizError('购物车为空，无需挂单')
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw) > 200000:
            raise BizError('挂单内容过大')
        with tx(db):
            if db.execute('SELECT COUNT(*) FROM held_orders WHERE merchant_id=? AND store_code=?', (mid, store_code)).fetchone()[0] >= 30:
                raise BizError('挂单已满 30 张，请先取单')
            db.execute('INSERT INTO held_orders(merchant_id,store_code,cashier_user,title,payload,created_at,terminal_no,remark) VALUES(?,?,?,?,?,?,?,?)',
                       (mid, store_code, actor['username'], (data.get('title') or '')[:40], raw, now_s(), (data.get('terminal_no') or '')[:20],
                        (data.get('remark') or '')[:100]))
        return ok({'saved': True})
    rows = []
    for r in db.execute('SELECT * FROM held_orders WHERE merchant_id=? AND store_code=? ORDER BY id DESC', (mid, store_code)):
        x = dict(r)
        x['payload'] = trade._json_load(r['payload'], {})
        rows.append(x)
    return ok(rows, rows=rows)


@bp.route('/held/<int:hid>', methods=['DELETE'])
@require_perm('pos.held')
def held_del(hid):
    db = get_db()
    actor = current_actor()
    with tx(db):
        cur = db.execute('DELETE FROM held_orders WHERE id=? AND merchant_id=? AND store_code=?', (hid, actor['merchant_id'], _store(actor)))
    if cur.rowcount != 1:
        raise BizError('挂单不存在或已被取走', 404)
    return ok({'deleted': hid})


# ---------------------------------------------------------------- 班次（POS-002/004/005）
@bp.route('/shift/open', methods=['POST'])
@require_perm('pos.shift')
def shift_open():
    db = get_db()
    actor = current_actor()
    with tx(db):
        sh = trade.open_shift(db, actor, _store(actor), _json())
    d = trade.shift_view(db, sh)
    return ok(d, shift=d)


@bp.route('/shift/current')
@require_perm('pos.shift', 'pos.checkout')
def shift_current():
    db = get_db()
    actor = current_actor()
    sh = trade.current_shift(db, _store(actor), actor['username'], actor['merchant_id'])
    if sh is None:
        return ok(None, shift=None)
    summ = trade.shift_summary(db, sh)
    moves = [dict(r) for r in db.execute('SELECT cm_no, direction, amount_cents, reason, operator, auth_user, approval_no, created_at FROM cash_moves '
                                         'WHERE shift_id=? ORDER BY id', (sh['id'],))]
    from .money import yuan
    for m in moves:
        m['amount'] = yuan(m.pop('amount_cents'))
    d = dict(sh)
    return ok({'shift': d, 'summary': summ, 'cash_moves': moves}, shift=d, summary=summ, cash_moves=moves)


@bp.route('/shift/close', methods=['POST'])
@require_perm('pos.shift')
def shift_close():
    db = get_db()
    actor = current_actor()
    with tx(db):
        sh, dup = trade.close_shift(db, actor, _store(actor), _json())
    d = dict(sh)
    summ = trade._json_load(d.get('summary'), {})
    return ok({'shift': d, 'summary': summ, 'duplicate': dup}, shift=d, summary=summ, duplicate=dup)


@bp.route('/shifts')
@require_perm('pos.shift.force', 'pos.reprint')
def shifts_list():
    """本店班次（店长看未交班/补打用）。status=open 只看未交班。"""
    db = get_db()
    actor = current_actor()
    store_code = _store(actor, request.args.get('store'))
    st = (request.args.get('status') or '').strip()
    sql = 'SELECT * FROM shifts WHERE merchant_id=? AND store_code=?'
    args = [actor['merchant_id'], store_code]
    if st in ('open', 'closed'):
        sql += ' AND status=?'
        args.append(st)
    rows = [trade.shift_view(db, r) for r in db.execute(sql + ' ORDER BY id DESC LIMIT 50', args)]
    return ok(rows, rows=rows)


@bp.route('/shift/force-close', methods=['POST'])
@require_perm('pos.shift.force')
def shift_force_close():
    db = get_db()
    actor = current_actor()
    with tx(db):
        sh = trade.force_close_shift(db, actor, _json())
    d = dict(sh)
    return ok({'shift': d, 'summary': trade._json_load(d.get('summary'), {})}, shift=d)


@bp.route('/shift/<int:sid>/reprint', methods=['POST'])
@require_perm('pos.reprint')
def shift_reprint(sid):
    db = get_db()
    actor = current_actor()
    with tx(db):
        sh = trade.shift_in_scope(db, actor, sid)
        db.execute('UPDATE shifts SET reprint_count=COALESCE(reprint_count,0)+1 WHERE id=?', (sid,))
        trade.log_print(db, actor, 'shift', sh['shift_no'] or str(sid), True)
        audit(db, actor, 'pos', '补打班报', sh['shift_no'] or str(sid), None, None, store_code=sh['store_code'])
    sh = db.execute('SELECT * FROM shifts WHERE id=?', (sid,)).fetchone()
    d = trade.shift_view(db, sh)
    d['reprint'] = True
    return ok(d, shift=d, summary=d['summary'])


# ---------------------------------------------------------------- 现金收支（POS-003）
@bp.route('/cash-moves', methods=['GET', 'POST'])
@require_perm('pos.cash_move')
def cash_moves():
    from .money import yuan
    db = get_db()
    actor = current_actor()
    store_code = _store(actor)
    if request.method == 'GET':
        sh = trade.current_shift(db, store_code, actor['username'], actor['merchant_id'])
        rows = []
        if sh is not None:
            rows = [dict(r) for r in db.execute('SELECT * FROM cash_moves WHERE shift_id=? ORDER BY id', (sh['id'],))]
            for r in rows:
                r['amount'] = yuan(r['amount_cents'])
        return ok(rows, rows=rows)
    with tx(db):
        row, dup = trade.cash_move(db, actor, store_code, _json())
    row['amount'] = yuan(row['amount_cents'])
    row['duplicate'] = dup
    return ok(row, **row)
