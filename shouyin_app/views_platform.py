# -*- coding: utf-8 -*-
"""平台运营：商户开通/状态/额度/管理员重置/平台审计/运行状态。

Owner：模块④（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：SaaS-001~004 2.2 2.3
接口清单与入参出参：docs/API-CONTRACT.md §4.4；返回格式 {ok:true,data} / {ok:false,code,msg}；平台接口一律 @require_platform(...)。

平台与商户分权（2.2）：平台账号 merchant_id=0，只能看商户档案、状态、额度和已用数量，看不到任何经营数据（订单/会员/商品/库存）。
平台的每个写操作都写 audit_logs（merchant_id=0，ref_no=商户编码），重置密码类为 security 级，审计里不含任何密码。
"""
import re
import secrets
import string

from flask import Blueprint, jsonify, request, current_app
from werkzeug.security import generate_password_hash

from .core import get_db, tx, require_platform, current_actor, audit, ok, revoke_user_sessions
from .perms import ensure_default_roles, role_id
from .numbering import next_no
from .util import BizError, now_s, inum

bp = Blueprint('platform', __name__, url_prefix='/api/platform')

STATUS_NAME = {'trial': '试用', 'active': '正式', 'expired': '到期（只读）', 'disabled': '停用'}
# SaaS-002：各状态能力（与 core.api 的实际拦截一致）
STATUS_CAPS = {
    'trial': {'login': True, 'write': True, 'query': True, 'export': True},
    'active': {'login': True, 'write': True, 'query': True, 'export': True},
    'expired': {'login': True, 'write': False, 'query': True, 'export': True},
    'disabled': {'login': False, 'write': False, 'query': False, 'export': False},
}
def one_time_password(n=12):
    alphabet = string.ascii_letters + string.digits
    while True:
        p = ''.join(secrets.choice(alphabet) for _ in range(n))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def legacy_blueprints(app=None):
    """仍带 legacy_single_tenant 标记（v1 未按商户过滤）的蓝图名。非空时禁止开通第 2 个商户（ARCH §7）。"""
    app = app or current_app
    return sorted(name for name, b in app.blueprints.items() if getattr(b, 'legacy_single_tenant', False))


def _usage(db, mid):
    return {'stores_used': db.execute('SELECT COUNT(*) FROM stores WHERE merchant_id=?', (mid,)).fetchone()[0],
            'users_used': db.execute("SELECT COUNT(*) FROM users WHERE merchant_id=? AND status='启用'", (mid,)).fetchone()[0]}


def _merchant_out(db, r):
    """平台只看档案/状态/额度/已用数量（不含经营数据）。"""
    d = {k: r[k] for k in ('id', 'code', 'name', 'contact', 'phone', 'address', 'status', 'plan', 'expire_date', 'max_stores', 'max_users',
                           'open_request_no', 'created_at', 'updated_at')}
    d['status_name'] = STATUS_NAME.get(r['status'], r['status'])
    d['caps'] = STATUS_CAPS.get(r['status'], {})
    d.update(_usage(db, r['id']))
    admin = db.execute("SELECT u.username FROM users u JOIN roles ro ON ro.id=u.role_id WHERE u.merchant_id=? AND ro.code='merchant_admin' "
                       "ORDER BY u.id LIMIT 1", (r['id'],)).fetchone()
    d['admin_username'] = admin[0] if admin else ''
    return d


def _merchant(db, mid):
    r = db.execute('SELECT * FROM merchants WHERE id=?', (inum(mid),)).fetchone()
    if r is None:
        raise BizError('商户不存在', 404)
    return r


def _json():
    return request.get_json(silent=True) or {}


@bp.route('/merchants')
@require_platform('platform.merchant.view')
def merchants_list():
    db = get_db()
    q = request.args
    clauses, args = ['1=1'], []
    if (q.get('kw') or '').strip():
        clauses.append('(code LIKE ? OR name LIKE ? OR contact LIKE ?)')
        args += ['%' + q.get('kw').strip() + '%'] * 3
    if q.get('status') in STATUS_NAME:
        clauses.append('status=?')
        args.append(q.get('status'))
    rows = db.execute('SELECT * FROM merchants WHERE ' + ' AND '.join(clauses) + ' ORDER BY id', args).fetchall()
    return ok({'items': [_merchant_out(db, r) for r in rows], 'legacy_blueprints': legacy_blueprints(),
               'status_names': STATUS_NAME})


@bp.route('/merchants/<int:mid>')
@require_platform('platform.merchant.view')
def merchant_get(mid):
    db = get_db()
    return ok(_merchant_out(db, _merchant(db, mid)))


@bp.route('/merchants', methods=['POST'])
@require_platform('platform.merchant.open')
def merchant_open():
    """SaaS-001 开通：{open_request_no, code, name, contact, phone, admin_username, max_stores, max_users, status, expire_date}
    幂等：同一 open_request_no 重复提交返回已开通结果（duplicate=true，不再返回密码）；整个开通在一个事务里，失败全部回滚可直接重试。
    初始化：默认角色 + 总部组织 + 数据字典 + 商户管理员（一次性密码只返回一次，首次登录须改密）。"""
    from .schema.tenant_auth import DEFAULT_DICTS
    db = get_db()
    a = current_actor()
    d = _json()
    req = (d.get('open_request_no') or '').strip()
    if not re.match(r'^[A-Za-z0-9_-]{6,40}$', req):
        raise BizError('请提供开通请求号 open_request_no（6-40 位字母数字，用于防重复开通）')
    old = db.execute('SELECT * FROM merchants WHERE open_request_no=?', (req,)).fetchone()
    if old is not None:
        out = _merchant_out(db, old)
        out['duplicate'] = True
        return ok(out)
    legacy = legacy_blueprints()
    if legacy and db.execute('SELECT COUNT(*) FROM merchants').fetchone()[0] >= 1:
        raise BizError('系统还有未完成多商户隔离的旧接口（%s），暂不能开通新商户' % ','.join(legacy), 409, 'LEGACY_SINGLE_TENANT',
                       {'legacy_blueprints': legacy})
    code = (d.get('code') or '').strip().upper()
    name = (d.get('name') or '').strip()[:60]
    admin_username = (d.get('admin_username') or '').strip()
    status = d.get('status') if d.get('status') in ('trial', 'active') else 'trial'
    if not re.match(r'^[A-Z0-9]{2,12}$', code):
        raise BizError('商户编码为 2-12 位大写字母或数字')
    if not name:
        raise BizError('请填写商户名称')
    if not re.match(r'^[A-Za-z0-9_]{3,20}$', admin_username):
        raise BizError('管理员账号为 3-20 位字母、数字或下划线')
    max_stores, max_users = inum(d.get('max_stores')), inum(d.get('max_users'))
    if max_stores < 0 or max_users < 0:
        raise BizError('额度不能为负（0 = 不限）')
    expire = (d.get('expire_date') or '').strip()[:10]
    if expire and not re.match(r'^\d{4}-\d{2}-\d{2}$', expire):
        raise BizError('到期日格式应为 YYYY-MM-DD')
    pwd = one_time_password()
    ts = now_s()
    with tx(db):
        if db.execute('SELECT 1 FROM merchants WHERE code=?', (code,)).fetchone():
            raise BizError('商户编码已存在：%s' % code, 409, 'CONFLICT')
        if db.execute('SELECT 1 FROM users WHERE username=?', (admin_username,)).fetchone():
            raise BizError('管理员账号已被使用：%s（登录名全平台唯一）' % admin_username, 409, 'CONFLICT')
        mc_no = next_no(db, 0, 'MC')
        db.execute('INSERT INTO merchants(code, name, contact, phone, address, status, plan, expire_date, max_stores, max_users, open_request_no, '
                   'created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (code, name, (d.get('contact') or '')[:30], (d.get('phone') or '')[:30], (d.get('address') or '')[:200], status,
                    (d.get('plan') or '')[:30], expire, max_stores, max_users, req, ts, ts))
        mid = db.execute('SELECT id FROM merchants WHERE code=?', (code,)).fetchone()[0]
        ensure_default_roles(db, mid)
        db.execute("INSERT INTO org_units(merchant_id, kind, code, name, created_at) VALUES(?, 'hq', 'HQ', '总部', ?)", (mid, ts))
        for t, names in DEFAULT_DICTS.items():
            for i, n in enumerate(names):
                db.execute('INSERT OR IGNORE INTO dict_items(merchant_id, dict_type, code, name, sort) VALUES(?,?,?,?,?)',
                           (mid, t, 'D%02d' % (i + 1), n, i))
        db.execute('INSERT INTO users(merchant_id, username, password_hash, name, role, role_id, store_code, status, created_at, must_change_pw) '
                   "VALUES(?,?,?,?,'admin',?,'','启用',?,1)",
                   (mid, admin_username, generate_password_hash(pwd), (d.get('admin_name') or '商户管理员')[:20], role_id(db, mid, 'merchant_admin'), ts))
        audit(db, a, 'platform', '开通商户', code, None,
              {'mc_no': mc_no, 'merchant_id': mid, 'name': name, 'status': status, 'max_stores': max_stores, 'max_users': max_users,
               'admin_username': admin_username, 'open_request_no': req}, level='security', store_code='')
        out = _merchant_out(db, _merchant(db, mid))
    out.update({'duplicate': False, 'mc_no': mc_no, 'admin_username': admin_username, 'admin_password': pwd,
                'notice': '一次性初始密码只显示这一次，请当面或安全渠道交给商户，首次登录须修改密码'})
    return ok(out)


@bp.route('/merchants/<int:mid>/status', methods=['POST'])
@require_platform('platform.merchant.status')
def merchant_status(mid):
    """SaaS-002：trial/active/expired/disabled + 原因。停用 -> 该商户所有账号会话立即失效。"""
    db = get_db()
    a = current_actor()
    d = _json()
    st = d.get('status')
    reason = (d.get('reason') or '').strip()
    if st not in STATUS_NAME:
        raise BizError('状态只能是：%s' % '/'.join(STATUS_NAME))
    if not reason:
        raise BizError('请填写变更原因')
    with tx(db):
        m = _merchant(db, mid)
        if m['status'] == st:
            raise BizError('商户已经是「%s」' % STATUS_NAME[st], 409, 'CONFLICT')
        db.execute('UPDATE merchants SET status=?, expire_date=?, updated_at=? WHERE id=?',
                   (st, (d.get('expire_date') or m['expire_date'] or '')[:10], now_s(), mid))
        kicked = 0
        if st == 'disabled':
            for (uid,) in db.execute('SELECT id FROM users WHERE merchant_id=?', (mid,)).fetchall():
                revoke_user_sessions(db, uid, '商户停用')
                kicked += 1
        audit(db, a, 'platform', '商户状态', m['code'], {'status': m['status']}, {'status': st, 'kicked_users': kicked}, reason[:200],
              level='security', store_code='')
        out = _merchant_out(db, _merchant(db, mid))
    return ok(out)


@bp.route('/merchants/<int:mid>/quota', methods=['POST'])
@require_platform('platform.merchant.quota')
def merchant_quota(mid):
    """SaaS-003：门店/账号额度（0=不限）。不能低于已用数量；返回已用。"""
    db = get_db()
    a = current_actor()
    d = _json()
    with tx(db):
        m = _merchant(db, mid)
        ms = inum(d.get('max_stores'), m['max_stores'])
        mu = inum(d.get('max_users'), m['max_users'])
        if ms < 0 or mu < 0:
            raise BizError('额度不能为负（0 = 不限）')
        u = _usage(db, mid)
        if (ms and ms < u['stores_used']) or (mu and mu < u['users_used']):
            raise BizError('额度不能低于已用数量（门店已用 %d、启用账号 %d）' % (u['stores_used'], u['users_used']), 400, 'BIZ_ERROR', u)
        db.execute('UPDATE merchants SET max_stores=?, max_users=?, updated_at=? WHERE id=?', (ms, mu, now_s(), mid))
        audit(db, a, 'platform', '商户额度', m['code'], {'max_stores': m['max_stores'], 'max_users': m['max_users']},
              {'max_stores': ms, 'max_users': mu}, (d.get('reason') or '')[:200], level='high', store_code='')
        out = _merchant_out(db, _merchant(db, mid))
    return ok(out)


@bp.route('/merchants/<int:mid>/reset-admin', methods=['POST'])
@require_platform('platform.merchant.reset_admin')
def merchant_reset_admin(mid):
    """SaaS-004：二次确认（confirm_code 必须等于商户编码）+ 原因；生成一次性密码（不显示原密码）、must_change_pw=1、踢下线；security 审计。"""
    db = get_db()
    a = current_actor()
    d = _json()
    reason = (d.get('reason') or '').strip()
    with tx(db):
        m = _merchant(db, mid)
        if (d.get('confirm_code') or '').strip().upper() != m['code']:
            raise BizError('二次确认失败：请输入该商户编码 %s 确认操作' % m['code'], 400, 'CONFIRM_REQUIRED')
        if not reason:
            raise BizError('请填写重置原因（如：已电话核验商户负责人身份）')
        uname = (d.get('username') or '').strip()
        if uname:
            u = db.execute("SELECT u.* FROM users u JOIN roles ro ON ro.id=u.role_id WHERE u.merchant_id=? AND u.username=? "
                           "AND ro.code='merchant_admin'", (mid, uname)).fetchone()
        else:
            u = db.execute("SELECT u.* FROM users u JOIN roles ro ON ro.id=u.role_id WHERE u.merchant_id=? AND ro.code='merchant_admin' "
                           "ORDER BY u.id LIMIT 1", (mid,)).fetchone()
        if u is None:
            raise BizError('该商户没有这个管理员账号（只能重置商户超级管理员）', 404)
        pwd = one_time_password()
        db.execute("UPDATE users SET password_hash=?, must_change_pw=1, status='启用' WHERE id=?", (generate_password_hash(pwd), u['id']))
        revoke_user_sessions(db, u['id'], '平台重置管理员密码')
        audit(db, a, 'platform', '重置商户管理员', m['code'], None, {'username': u['username'], 'sessions_revoked': True}, reason[:200],
              level='security', store_code='')
    return ok({'merchant_id': mid, 'username': u['username'], 'admin_password': pwd,
               'notice': '一次性密码只显示这一次；对方首次登录须修改密码，原有登录已全部下线'})


@bp.route('/audit')
@require_platform('platform.audit.view')
def platform_audit():
    """平台操作审计（merchant_id=0 的 audit_logs）。"""
    db = get_db()
    q = request.args
    page, size = max(1, inum(q.get('page'), 1)), min(200, max(1, inum(q.get('size'), 30)))
    clauses, args = ['merchant_id=0'], []
    for k, col in (('ref_no', 'ref_no'), ('user', 'username'), ('level', 'level')):
        if (q.get(k) or '').strip():
            clauses.append('%s=?' % col)
            args.append(q.get(k).strip())
    if (q.get('action') or '').strip():
        clauses.append('action LIKE ?')
        args.append('%' + q.get('action').strip() + '%')
    where = ' WHERE ' + ' AND '.join(clauses)
    total = db.execute('SELECT COUNT(*) FROM audit_logs' + where, args).fetchone()[0]
    rows = db.execute('SELECT * FROM audit_logs' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?', args + [size, (page - 1) * size]).fetchall()
    return ok({'page': page, 'size': size, 'total': total, 'items': [dict(r) for r in rows]})


@bp.route('/ops')
@require_platform('platform.ops', 'platform.merchant.view')
def platform_ops():
    """运行状态（不含业务数据）：版本、库大小、商户状态分布、待处理导出任务、旧接口隔离进度。"""
    import os
    from . import __version__
    db = get_db()
    path = current_app.config['DB_PATH']
    by = dict((r[0], r[1]) for r in db.execute('SELECT status, COUNT(*) FROM merchants GROUP BY status'))
    return ok({'version': __version__, 'db_size_mb': round(os.path.getsize(path) / 1048576.0, 2) if os.path.exists(path) else 0,
               'merchants': by, 'legacy_blueprints': legacy_blueprints(),
               'export_jobs_running': db.execute("SELECT COUNT(*) FROM export_jobs WHERE status IN ('queued','running')").fetchone()[0],
               'time': now_s()})
