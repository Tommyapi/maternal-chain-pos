# -*- coding: utf-8 -*-
"""基础设施：数据库连接、配置、登录态、权限、审计、通用查询。Owner：架构（冻结；改动在报告里提，由总脑合并）。

新代码用法（详见 docs/ARCH.md §8）：
    @bp.route('/x') + @require_perm('goods.view')         登录 + 商户状态 + 功能权限（多个码 = 任一即可）
    actor = current_actor()                               dict：id/username/name/merchant_id/role/role_code/perms/limits/data_scope/store_code
    stores = scope_stores(db, actor, request.args.get('store'))   数据范围内门店编码列表（越权 -> 403）
    sql, args = scope_sql(db, actor, 'o.store_code', requested, 'o.merchant_id')   拼到 WHERE 1=1 后面
    mask_phone(phone, actor) / cost_view(actor, cents)    字段权限
    check_limit_or_approval(db, actor, 'discount', Decimal('0.85'), data, store_code)   操作额度 -> 本人 / 现场授权 / 审批单
    audit(db, actor, 'pos', '改价', order_no, before, after, reason, level='high')
"""
import json
import sqlite3
import time
from functools import wraps

from flask import g, session, request, current_app, jsonify
from werkzeug.security import check_password_hash

from .db import connect
from .util import BizError, now_s, r2, fnum, baby_axis

DEFAULT_CONFIG = {
    'points_per_yuan': 1.0,        # 每实付 1 元积几分
    'points_to_yuan': 100,         # 多少积分抵 1 元
    'points_max_ratio': 0.5,       # 积分最多抵扣订单金额比例
    'round_mode': 'none',          # none 不抹零 / jiao 抹分 / yuan 抹角
    'cashier_can_change_price': False,   # 收银员可否直接改价（否则需店长授权）
    'cashier_max_discount': 0.9,   # 收银员可直接给的最低整单折扣（低于需店长授权）
    'receipt_title': '萌贝母婴连锁',
    'receipt_footer': '谢谢惠顾，欢迎再次光临！\n商品如有质量问题请凭小票7天内退换',
    'receipt_width': 58,           # 58 / 80 毫米
    'allow_negative_stock': False, # 库存不足时是否允许继续开单（POS-109：默认禁止；开启后还需角色有 pos.negative_stock）
    'allow_no_receipt_refund': True,   # 无单退货开关（POS-604，可整体禁用）
    'refund_method_order': ['stored', 'wechat', 'alipay', 'bankcard', 'other', 'cash'],   # 原路退款拆分顺序（POS-603）
    'near_expiry_days': 30,       # 收银临期提示默认天数（商品/分类未设置时，POS-203）
    'recharge_rules': [],          # 充值赠送规则，如 [{"threshold":500,"gift":50}]
    'member_levels': ['普通会员', '银卡会员', '金卡会员'],
}

PAY_METHODS = [
    ('cash', '现金'), ('wechat', '微信'), ('alipay', '支付宝'),
    ('bankcard', '银行卡'), ('stored', '储值'), ('other', '其他'),
]
PAY_NAME = dict(PAY_METHODS)

LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60


def get_db():
    if 'db' not in g:
        g.db = connect(current_app.config['DB_PATH'])
    return g.db


def close_db(exc=None):
    db = g.pop('db', None)
    if db is not None:
        try:
            if db.in_transaction:
                db.execute('ROLLBACK')
        except Exception:
            pass
        db.close()


class tx(object):
    """with tx(db): ... 一个 BEGIN IMMEDIATE 事务；异常则回滚。"""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute('BEGIN IMMEDIATE')
        return self.db

    def __exit__(self, et, ev, tb):
        if et is None:
            self.db.execute('COMMIT')
        else:
            try:
                self.db.execute('ROLLBACK')
            except Exception:
                pass
        return False


# ---------------------------------------------------------------- 配置
def _mid(merchant_id=None):
    if merchant_id is not None:
        return int(merchant_id)
    a = g.get('actor') if g else None
    return int(a['merchant_id']) if a and a.get('merchant_id') else 1


def get_config(db, merchant_id=None):
    row = db.execute("SELECT value FROM settings WHERE merchant_id=? AND key='config'", (_mid(merchant_id),)).fetchone()
    cfg = dict(DEFAULT_CONFIG)
    if row:
        try:
            cfg.update(json.loads(row[0]))
        except Exception:
            pass
    return cfg


def save_config(db, data, merchant_id=None):
    cfg = get_config(db, merchant_id)
    for k, v in data.items():
        if k not in DEFAULT_CONFIG:
            continue
        dv = DEFAULT_CONFIG[k]
        if isinstance(dv, bool):
            cfg[k] = bool(v)
        elif isinstance(dv, (int, float)):
            cfg[k] = fnum(v, dv)
        elif isinstance(dv, list):
            if k == 'recharge_rules':
                rules = []
                for x in (v or []):
                    t, gft = fnum(x.get('threshold')), fnum(x.get('gift'))
                    if t > 0 and gft >= 0:
                        rules.append({'threshold': t, 'gift': gft})
                cfg[k] = sorted(rules, key=lambda x: -x['threshold'])
            else:
                cfg[k] = [str(i)[:20] for i in (v or []) if str(i).strip()] or dv
        else:
            cfg[k] = str(v)[:500]
    if cfg['round_mode'] not in ('none', 'jiao', 'yuan'):
        cfg['round_mode'] = 'none'
    if cfg['receipt_width'] not in (58, 80):
        cfg['receipt_width'] = 58
    db.execute("INSERT OR REPLACE INTO settings(merchant_id,key,value) VALUES(?,'config',?)",
               (_mid(merchant_id), json.dumps(cfg, ensure_ascii=False)))
    return cfg


# ---------------------------------------------------------------- 登录态
def client_ip():
    return (request.headers.get('X-Real-IP') or request.remote_addr or '')[:45]


def log_op(action, module, detail='', user=None):
    db = get_db()
    u = user if user is not None else (session.get('username') or '')
    db.execute('INSERT INTO op_logs(merchant_id,user,action,module,detail,ip,created_at) VALUES(?,?,?,?,?,?,?)',
               (_mid(), u, action, module, (detail or '')[:500], client_ip(), now_s()))


def _load_actor(db, u):
    """users 行 -> actor dict（含角色、权限码集合、额度、数据范围级别）。"""
    from .perms import LEGACY_ROLE_NAME
    role = None
    if u['role_id']:
        role = db.execute('SELECT * FROM roles WHERE id=? AND merchant_id=?', (u['role_id'], u['merchant_id'])).fetchone()
    if role is None:      # 旧账号/种子账号还没分配 role_id：按旧粗角色映射默认角色
        code = {'admin': 'merchant_admin', 'manager': 'store_manager', 'cashier': 'cashier', 'platform': 'platform_admin'}.get(u['role'])
        if code:
            role = db.execute('SELECT * FROM roles WHERE merchant_id=? AND code=?', (u['merchant_id'], code)).fetchone()
    perms, limits = frozenset(), {}
    if role is not None and role['status'] == '启用':
        perms = frozenset(r[0] for r in db.execute('SELECT perm FROM role_perms WHERE role_id=?', (role['id'],)))
        limits = dict((r[0], r[1]) for r in db.execute('SELECT limit_key, value FROM role_limits WHERE role_id=?', (role['id'],)))
    legacy = u['role'] if u['role'] in LEGACY_ROLE_NAME else (role['legacy_role'] if role is not None else 'staff')
    return {'id': u['id'], 'username': u['username'], 'name': u['name'] or u['username'], 'merchant_id': int(u['merchant_id']),
            'is_platform': int(u['merchant_id']) == 0, 'role': legacy, 'role_id': role['id'] if role is not None else None,
            'role_code': role['code'] if role is not None else '',
            'role_name': role['name'] if role is not None else LEGACY_ROLE_NAME.get(legacy, ''),
            'data_scope': role['data_scope'] if role is not None else 'self', 'perms': perms, 'limits': limits,
            'store_code': u['store_code'] or ''}


def current_actor(db=None):
    """当前登录账号（每次请求实时查库：停用、改角色、改权限、踢下线立即生效）。"""
    if 'actor' in g:
        return g.actor
    uid = session.get('uid')
    actor = None
    if uid:
        db = db or get_db()
        u = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        ok_ = u is not None and u['status'] == '启用' and int(session.get('tv') or 0) == int(u['token_version'] or 0)
        sid = session.get('sid')
        if ok_ and sid:
            srow = db.execute('SELECT revoked_at, last_seen FROM user_sessions WHERE sid=? AND user_id=?', (sid, uid)).fetchone()
            ok_ = srow is not None and not srow['revoked_at']
            if ok_:
                _touch_session(db, sid, srow['last_seen'])
        if ok_:
            actor = _load_actor(db, u)
    g.actor = actor
    return actor


SESSION_TOUCH_SECONDS = 300


def _touch_session(db, sid, last_seen):
    """设备会话「最后活跃」：距上次记录超过 5 分钟才写一次库（REPORT-4 §5-4），写失败（库忙）不影响本次请求。"""
    from datetime import datetime
    try:
        if last_seen and (datetime.now() - datetime.strptime(last_seen[:19], '%Y-%m-%d %H:%M:%S')).total_seconds() < SESSION_TOUCH_SECONDS:
            return
    except ValueError:
        pass
    try:
        db.execute('UPDATE user_sessions SET last_seen=? WHERE sid=?', (now_s(), sid))
    except sqlite3.OperationalError:
        pass


def start_session(db, u, client='web', device=''):
    """登录成功后调用：写 Flask session + user_sessions（设备管理/踢下线用）。"""
    import secrets as _secrets
    sid = _secrets.token_hex(16)
    session.clear()
    session['uid'] = u['id']
    session['username'] = u['username']
    session['tv'] = int(u['token_version'] or 0)
    session['sid'] = sid
    session.permanent = True
    db.execute('INSERT INTO user_sessions(sid, merchant_id, user_id, client, device, ip, created_at, last_seen) VALUES(?,?,?,?,?,?,?,?)',
               (sid, u['merchant_id'], u['id'], (client or 'web')[:10], (device or '')[:100], client_ip(), now_s(), now_s()))
    g.pop('actor', None)
    return sid


def revoke_user_sessions(db, user_id, reason=''):
    """停用/调店/改权限/改密码：该账号所有终端立即失效（APP-405 / ERP-701）。"""
    db.execute('UPDATE users SET token_version = token_version + 1 WHERE id=?', (user_id,))
    db.execute('UPDATE user_sessions SET revoked_at=?, revoke_reason=? WHERE user_id=? AND revoked_at IS NULL',
               (now_s(), (reason or '')[:100], user_id))


def merchant_row(db, merchant_id):
    key = 'merchant_%s' % merchant_id
    if key not in g:
        r = db.execute('SELECT * FROM merchants WHERE id=?', (merchant_id,)).fetchone()
        setattr(g, key, dict(r) if r else None)
    return getattr(g, key)


ROLE_NAME = {'admin': '总部管理员', 'manager': '店长', 'cashier': '收银员', 'staff': '员工', 'platform': '平台运营'}


def err_response(msg, status, code=None, data=None):
    """统一失败返回 {ok:false, code, msg, error}（error 与 msg 相同，兼容旧前端）。"""
    body = {'ok': False, 'code': code or BizError(msg, status).code, 'msg': msg, 'error': msg}
    if data is not None:
        body['data'] = data
    return jsonify(body), status


def api(roles=None, perm=None, platform=False):
    """接口装饰器：登录 + 商户状态 + 平台/商户隔离 + 角色或权限码 + 统一业务错误。

    roles：旧代码的粗角色元组（新代码别用）；perm：权限码或元组（任一即可）；platform=True：只给平台账号。
    """
    perms_need = (perm,) if isinstance(perm, str) else tuple(perm or ())

    def deco(f):
        @wraps(f)
        def w(*a, **k):
            db = get_db()
            actor = current_actor(db)
            if actor is None:
                return err_response('未登录或会话已过期', 401)
            if bool(platform) != actor['is_platform']:
                return err_response('平台账号与商户账号不能互访' if actor['is_platform'] else '没有权限执行此操作', 403)
            if not actor['is_platform']:
                m = merchant_row(db, actor['merchant_id'])
                if m is None or m['status'] == 'disabled':
                    return err_response('商户已停用，请联系平台', 403, 'MERCHANT_DISABLED')
                if m['status'] == 'expired' and request.method not in ('GET', 'HEAD'):
                    return err_response('商户已到期，当前只能查询和导出', 403, 'MERCHANT_READONLY')
            bpo = current_app.blueprints.get(request.blueprint) if request.blueprint else None
            if bpo is not None and getattr(bpo, 'legacy_single_tenant', False) and actor['merchant_id'] != 1:
                # v1 旧接口没有按商户过滤：模块完成商户隔离前，只对 1 号商户开放（防跨商户读到数据）
                return err_response('该功能正在升级多商户支持，暂只对 1 号商户开放', 403, 'LEGACY_SINGLE_TENANT')
            if roles and actor['role'] not in roles:
                return err_response('没有权限执行此操作', 403)
            if perms_need and not any(p in actor['perms'] for p in perms_need):
                # AC-011：越权导出也要留安全审计（①②的 */export 走装饰器拒绝时此前没有记录）
                if 'export' in request.path or any(p.endswith('.export') for p in perms_need):
                    try:
                        with tx(db):
                            audit(db, actor, 'security', '越权导出被拒', request.path, None, {'need': list(perms_need)}, '', level='security')
                    except Exception:
                        pass
                return err_response('没有权限执行此操作（需要：%s）' % '/'.join(perms_need), 403)
            try:
                return f(*a, **k)
            except BizError as e:
                try:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                except Exception:
                    pass
                return err_response(e.msg, e.status, e.code, e.data)
        return w
    return deco


def require_perm(*codes):
    """@require_perm('goods.view') —— 新代码统一用它（等价 api(perm=codes)）。"""
    return api(perm=codes)


def require_platform(*codes):
    """平台运营后台接口：只允许平台账号（merchant_id=0）。"""
    return api(perm=codes, platform=True)


def has_perm(actor, code):
    return bool(actor) and code in actor['perms']


def ok(data=None, **extra):
    """新接口统一成功返回 {ok:true, data}。extra 只给兼容旧前端用，新接口不要用。"""
    body = {'ok': True, 'data': data}
    body.update(extra)
    return jsonify(body)


def scope_store(actor, requested=None):
    """查询范围门店：店长/收银员强制为自己绑定门店；总部可指定（空=全部）。"""
    if actor['role'] in ('manager', 'cashier'):
        return actor['store_code']
    return (requested or '').strip()


def ctx_store(db, actor, requested=None):
    """收银台当前门店。"""
    if actor['role'] in ('manager', 'cashier'):
        if not actor['store_code']:
            raise BizError('账号未绑定门店，请联系总部', 403)
        return actor['store_code']
    code = (requested or session.get('pos_store') or '').strip()
    if code:
        return code
    r = db.execute("SELECT code FROM stores WHERE status='营业' ORDER BY sort, code LIMIT 1").fetchone()
    return r[0] if r else ''


def store_row(db, code, merchant_id=None):
    r = db.execute('SELECT * FROM stores WHERE merchant_id=? AND code=?', (_mid(merchant_id), code)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- 登录失败限制
def lock_remaining(db, username, ip):
    """返回需要等待的秒数（0=未锁定）。同账号 15 分钟内连续失败 5 次锁定。"""
    since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - LOGIN_LOCK_SECONDS))
    rows = db.execute('SELECT ok, created_at FROM login_attempts WHERE username=? AND created_at>=? ORDER BY id DESC LIMIT ?',
                      (username, since, LOGIN_MAX_FAILS)).fetchall()
    if len(rows) >= LOGIN_MAX_FAILS and all(r['ok'] == 0 for r in rows):
        last = time.mktime(time.strptime(rows[0]['created_at'], '%Y-%m-%d %H:%M:%S'))
        left = int(last + LOGIN_LOCK_SECONDS - time.time())
        return max(0, left)
    # 同一 IP 短时间内大量失败也限制
    ipfails = db.execute('SELECT COUNT(*) FROM login_attempts WHERE ip=? AND ok=0 AND created_at>=?', (ip, since)).fetchone()[0]
    if ipfails >= LOGIN_MAX_FAILS * 4:
        return 60
    return 0


def record_attempt(db, username, ip, ok):
    db.execute('INSERT INTO login_attempts(username,ip,ok,created_at) VALUES(?,?,?,?)', (username, ip, 1 if ok else 0, now_s()))
    db.execute("DELETE FROM login_attempts WHERE created_at < datetime('now','localtime','-3 day')")


def verify_user(db, username, password, ip=''):
    """校验账号密码（含失败次数限制）。成功返回用户行，失败抛 BizError。"""
    username = (username or '').strip()
    left = lock_remaining(db, username, ip)
    if left > 0:
        raise BizError('登录失败次数过多，请 %d 分钟后再试' % ((left + 59) // 60), 429)
    u = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    ok = u is not None and u['status'] == '启用' and check_password_hash(u['password_hash'], password or '')
    record_attempt(db, username, ip, ok)
    if not ok:
        raise BizError('账号或密码错误', 401)
    return u


def check_auth(db, data, actor, need_roles=('manager', 'admin')):
    """店长授权：当前账号本身是店长/总部直接通过；否则校验 auth_user/auth_pass（店长须同门店）。返回授权人用户名。"""
    if actor['role'] in need_roles:
        return actor['username']
    au = (data.get('auth_user') or '').strip()
    ap = data.get('auth_pass') or ''
    if not au or not ap:
        raise BizError('该操作需要店长授权', 403)
    left = lock_remaining(db, 'auth:' + au, '')
    if left > 0:
        raise BizError('授权失败次数过多，请稍后再试', 429)
    row = db.execute('SELECT * FROM users WHERE username=?', (au,)).fetchone()
    ok = (row is not None and row['status'] == '启用' and row['role'] in need_roles
          and check_password_hash(row['password_hash'], ap)
          and (row['role'] == 'admin' or (row['store_code'] or '') == actor['store_code']))
    record_attempt(db, 'auth:' + au, '', ok)
    if not ok:
        raise BizError('授权账号或密码错误（须为本店店长）', 403)
    return au


# ---------------------------------------------------------------- 编号
def new_code(db, table, col, prefix, width):
    row = db.execute('SELECT MAX(%s) FROM %s WHERE %s LIKE ?' % (col, table, col), (prefix + '%',)).fetchone()[0]
    n = 0
    if row:
        try:
            n = int(str(row)[len(prefix):])
        except Exception:
            n = db.execute('SELECT COUNT(*) FROM %s' % table).fetchone()[0]
    return prefix + str(n + 1).zfill(width)


# ---------------------------------------------------------------- 库存
def stock_qty(db, pid, store_code):
    r = db.execute('SELECT qty FROM stock WHERE product_id=? AND store_code=?', (pid, store_code)).fetchone()
    return r[0] if r else 0.0


def stock_change(db, pid, store_code, delta, mtype, ref_no='', operator='', note='', batch_no='', expiry_date=''):
    """旧代码的库存入口（v1 签名不变）。v2 起转给 inventory.post_move（无批次、合格品），保证 inv_stock 与旧表 stock 一致。
    新代码不要用它：直接调 inventory.stock_out / stock_in。"""
    from . import inventory
    d = float(delta or 0)
    if abs(d - round(d)) > 1e-9:
        raise BizError('数量必须是整数（按基本单位）')
    rtype = inventory.LEGACY_TYPE.get(mtype, 'legacy')
    inventory.post_move(db, _mid(), store_code, pid, int(round(d)), rtype, ref_no or 'LEGACY', operator, note=note, allow_negative=True)
    # 保持 v1 流水口径（type='入库' + 批号/效期文本），旧的临期查询依赖它
    db.execute('UPDATE stock_moves SET type=?, batch_no=?, expiry_date=? WHERE id=last_insert_rowid()', (mtype, batch_no or '', expiry_date or ''))
    return stock_qty(db, pid, store_code)


def fmt_qty(v):
    v = float(v or 0)
    return int(v) if abs(v - round(v)) < 1e-9 else round(v, 3)


# ---------------------------------------------------------------- 商品 / 会员
def product_dict(db, r, store_code=None):
    d = dict(r)
    d['barcodes'] = [x[0] for x in db.execute('SELECT barcode FROM product_barcodes WHERE product_id=? ORDER BY barcode', (r['id'],))]
    d['barcode'] = d['barcodes'][0] if d['barcodes'] else ''
    if store_code is not None:
        d['stock'] = fmt_qty(stock_qty(db, r['id'], store_code)) if store_code else fmt_qty(
            db.execute('SELECT COALESCE(SUM(qty),0) FROM stock WHERE product_id=?', (r['id'],)).fetchone()[0])
    exp = db.execute("SELECT MIN(expiry_date) FROM stock_moves WHERE product_id=? AND type='入库' AND expiry_date != '' "
                     "AND expiry_date >= date('now','localtime')", (r['id'],)).fetchone()[0]
    d['near_expiry'] = exp or ''
    return d


def member_full(db, code):
    m = db.execute('SELECT * FROM members WHERE code=?', (code,)).fetchone()
    if m is None:
        return None
    d = dict(m)
    d['stored_balance'] = r2(m['balance'])
    d['points'] = int(m['points'] or 0)
    babies = []
    for b in db.execute('SELECT * FROM babies WHERE member_id=? ORDER BY id', (m['id'],)):
        axis, months = baby_axis(b['birthday'])
        babies.append({'id': b['id'], 'baby_name': b['name'], 'baby_gender': b['gender'], 'birthday': b['birthday'],
                       'axis': axis, 'month_age': months, 'note': b['note']})
    d['babies'] = babies
    d['baby'] = babies[0] if babies else None
    today = time.strftime('%Y-%m-%d')
    d['coupons'] = [dict(x) for x in db.execute(
        "SELECT coupon_no, template_name, face_value, threshold, expire_date FROM coupons "
        "WHERE member_code=? AND status='未使用' AND (expire_date='' OR expire_date >= ?) ORDER BY face_value DESC", (code, today))]
    agg = db.execute("SELECT COUNT(*), COALESCE(SUM(payable),0), MAX(created_at) FROM orders WHERE member_code=? AND biz_type='sale'",
                     (code,)).fetchone()
    ref = db.execute("SELECT COALESCE(SUM(payable),0) FROM orders WHERE member_code=? AND biz_type='refund'", (code,)).fetchone()[0]
    d['orders'] = agg[0] or 0
    d['amount'] = r2(agg[1] + ref)
    d['last_time'] = agg[2] or ''
    d.pop('balance', None)
    return d


# ================================================================ v2 新增：数据范围 / 字段权限 / 操作额度 / 审计
def scope_stores(db, actor, requested=None):
    """当前账号数据范围内的门店编码列表（只含本商户）。

    requested 为空：返回全部可见门店；不为空（编码或逗号分隔多个）：全部在范围内才返回，否则 BizError 403。
    data_scope：merchant=全商户；region=user_scopes 里的区域下门店 + 直接授权门店；store/self=绑定门店 + 直接授权门店。
    """
    mid = actor['merchant_id']
    ds = actor.get('data_scope') or 'self'
    if ds == 'merchant':
        allowed = [r[0] for r in db.execute('SELECT code FROM stores WHERE merchant_id=? ORDER BY sort, code', (mid,))]
    else:
        codes = set()
        if actor.get('store_code'):
            codes.add(actor['store_code'])
        for r in db.execute("SELECT scope_type, ref_code FROM user_scopes WHERE user_id=?", (actor['id'],)):
            if r['scope_type'] == 'store':
                codes.add(r['ref_code'])
            elif ds == 'region':
                for s in db.execute('SELECT s.code FROM stores s JOIN org_units o ON o.id=s.region_id '
                                    'WHERE s.merchant_id=? AND o.merchant_id=? AND o.code=?', (mid, mid, r['ref_code'])):
                    codes.add(s[0])
        allowed = [r[0] for r in db.execute('SELECT code FROM stores WHERE merchant_id=? ORDER BY sort, code', (mid,)) if r[0] in codes]
    if requested:
        req = [x.strip() for x in str(requested).split(',') if x.strip()]
        bad = [x for x in req if x not in allowed]
        if bad:
            raise BizError('无权访问门店：%s' % ','.join(bad), 403, 'SCOPE_DENIED')
        return req
    return allowed


def scope_sql(db, actor, store_col='store_code', requested=None, merchant_col='merchant_id'):
    """返回 (' AND merchant_id=? AND store_code IN (?,..)', params)。范围为空时返回恒假条件。"""
    stores = scope_stores(db, actor, requested)
    if not stores:
        return ' AND 1=0', []
    return (' AND %s=? AND %s IN (%s)' % (merchant_col, store_col, ','.join('?' * len(stores))), [actor['merchant_id']] + stores)


def mask_phone(phone, actor):
    """字段权限：没有 member.phone.view 时返回 138****0001。"""
    p = phone or ''
    if not p or has_perm(actor, 'member.phone.view'):
        return p
    return p[:3] + '****' + p[-4:] if len(p) >= 7 else '****'


def cost_view(actor, cents):
    """字段权限：没有 goods.cost.view 时返回 None（接口不返回明文，也不要返回能推导成本的毛利）。"""
    from .money import yuan
    return yuan(cents) if has_perm(actor, 'goods.cost.view') else None


LIMIT_KIND = {
    # kind: (role_limits 键, 审批权限码, 说明)
    'discount': ('min_discount_rate', 'approval.pos', '折扣'),
    'price_change': ('price_change', 'approval.pos', '改价'),
    'refund': ('refund_daily_cents', 'approval.pos', '退款'),
    'no_receipt_refund': ('no_receipt_refund', 'approval.pos', '无单退货'),
    'cash_out': ('cash_out_cents', 'approval.pos', '现金支出'),
    'points_adjust': ('points_adjust_max', 'approval.member', '积分调整'),
}


def within_limit(db, actor, kind, value, store_code=''):
    """actor 自己的额度是否覆盖本次操作。value：discount=折扣率(Decimal/str)；refund/cash_out=分；points_adjust=积分；其余忽略。"""
    from decimal import Decimal
    key = LIMIT_KIND[kind][0]
    raw = (actor.get('limits') or {}).get(key)
    if raw is None:
        return False
    if kind == 'discount':
        return Decimal(str(value)) >= Decimal(raw)
    if kind in ('price_change', 'no_receipt_refund'):
        return raw == '1'
    lim = int(raw)
    if lim < 0:
        return True
    if kind == 'refund':
        used = db.execute("SELECT COALESCE(-SUM(payable_cents),0) FROM orders WHERE merchant_id=? AND biz_type='refund' "
                          "AND cashier_user=? AND substr(created_at,1,10)=?", (actor['merchant_id'], actor['username'], now_s()[:10])).fetchone()[0]
        return int(used or 0) + int(value) <= lim
    return abs(int(value)) <= lim


def check_limit_or_approval(db, actor, kind, value, data, store_code, ref_no='', title='', payload=None):
    """操作额度校验（POS-106/604、ERP-703、3.3）。调用方在写事务里调用。

    依次尝试：① actor 自己额度够 -> {'mode':'self','by':username}
             ② data 带 auth_user + auth_pass（或 auth_pin）：现场授权人须同商户、有 pos.authorize、门店在其范围、额度够
                -> {'mode':'auth','by':授权人}（写 high 级审计）
             ③ data 带 approval_no：已批准、同类型、同门店、批准额度 >= 本次、未用过 -> 标记 consumed -> {'mode':'approval','by':审批人}
             ④ 都没有：抛 BizError(code='NEED_AUTH', data={kind,value,store_code})。前端二选一：弹店长授权框（带 auth_user/auth_pass 重提），
                或 POST /api/approvals 提交审批单（独立事务），审批通过后带 approval_no 重提。
    """
    from werkzeug.security import check_password_hash
    from . import approval as _approval
    if within_limit(db, actor, kind, value, store_code):
        return {'mode': 'self', 'by': actor['username']}
    data = data or {}
    label = LIMIT_KIND[kind][2]
    au = (data.get('auth_user') or '').strip()
    if au:
        left = lock_remaining(db, 'auth:' + au, '')
        if left > 0:
            raise BizError('授权失败次数过多，请稍后再试', 429)
        row = db.execute('SELECT * FROM users WHERE username=? AND merchant_id=?', (au, actor['merchant_id'])).fetchone()
        okk = False
        if row is not None and row['status'] == '启用':
            pw_ok = (data.get('auth_pass') and check_password_hash(row['password_hash'], data.get('auth_pass'))) or \
                    (data.get('auth_pin') and row['pin_hash'] and check_password_hash(row['pin_hash'], data.get('auth_pin')))
            if pw_ok:
                auth_actor = _load_actor(db, row)
                in_scope = store_code in scope_stores(db, auth_actor)
                okk = has_perm(auth_actor, 'pos.authorize') and in_scope and within_limit(db, auth_actor, kind, value, store_code)
        record_attempt(db, 'auth:' + au, '', okk)
        if not okk:
            raise BizError('授权人账号/密码错误，或授权人权限、门店范围、额度不足', 403, 'AUTH_FAILED')
        audit(db, actor, 'auth', '现场授权:' + label, ref_no, None, {'value': str(value), 'by': au}, title, level='high', store_code=store_code)
        return {'mode': 'auth', 'by': au}
    ano = (data.get('approval_no') or '').strip()
    if ano:
        ap = _approval.consume(db, actor, ano, kind, store_code, value)
        return {'mode': 'approval', 'by': ap['approver_name'], 'approval_no': ano}
    raise BizError('%s超出本人权限，需要店长现场授权或提交审批' % label, 403, 'NEED_AUTH',
                   {'kind': kind, 'value': str(value) if value is not None else None, 'store_code': store_code})


def audit(db, actor, module, action, ref_no='', before=None, after=None, reason='', level='info', store_code=None, terminal_no=''):
    """写审计日志（只能插入）。before/after 传 dict/list/str；不要传密码、密钥、完整证件号。"""
    def js(v):
        if v is None:
            return ''
        if isinstance(v, str):
            return v[:4000]
        return json.dumps(v, ensure_ascii=False, default=str)[:4000]
    a = actor or {}
    try:
        ip = client_ip()
    except RuntimeError:
        ip = ''
    db.execute('INSERT INTO audit_logs(merchant_id, store_code, terminal_no, user_id, username, module, action, ref_no, before_json, '
               'after_json, reason, ip, level, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (a.get('merchant_id', 0), store_code if store_code is not None else a.get('store_code', ''), terminal_no or '',
                a.get('id'), a.get('username', ''), module, action, ref_no or '', js(before), js(after), (reason or '')[:500], ip, level, now_s()))
