# -*- coding: utf-8 -*-
"""组织与权限：商户资料/连锁政策/区域/门店/员工/角色权限/数据范围/会话/审计查询。

Owner：模块④（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-001~003 ERP-006 ERP-701~705 3.2~3.4 NFR-SEC-005/006
接口清单与入参出参：docs/API-CONTRACT.md §4.4；返回格式 {ok:true,data} / {ok:false,code,msg}；权限一律 @require_perm(...)。
仓库 CRUD 归①（api/stock/warehouses），收银台 CRUD 归③（api/pos/terminals）。

供其他文件调用：
    check_quota(db, mid, 'stores'|'users')            新建门店/账号前校验平台授权额度（SaaS-003），超额 403 QUOTA_EXCEEDED
    after_store_created(db, mid, code, name)          新门店建门店仓（调①的 goods_svc.ensure_store_warehouse）
"""
import json
import re

from flask import Blueprint, request
from werkzeug.security import generate_password_hash

from .core import (get_db, tx, require_perm, current_actor, scope_stores, has_perm, audit, ok, revoke_user_sessions, mask_phone)
from .perms import PERMS, LIMIT_KEYS, ROLE_TEMPLATES
from .util import BizError, now_s, inum

bp = Blueprint('tenant', __name__, url_prefix='/api/org')

USER_STATUS = ('启用', '停用')
STORE_STATUS = ('营业', '停业')
DATA_SCOPES = {'merchant': '全商户', 'region': '授权区域', 'store': '本店（+授权门店）', 'self': '仅本人'}
POLICY_KEYS = {
    # ERP-006 连锁政策：每项 = (说明, 可选值)
    'cross_store_member': ('跨店会员（任意门店可识别同一会员）', ['all', 'region', 'none']),
    'cross_store_points': ('跨店积分累计与抵扣', ['all', 'region', 'none']),
    'cross_store_stored': ('跨店储值消费', ['all', 'region', 'none']),
    'cross_store_refund': ('跨店退货（P1，首版仅记录配置）', ['none', 'region', 'all']),
    'store_price_mode': ('门店自主定价', ['hq_only', 'store_allowed']),
}
POLICY_DEFAULT = {'cross_store_member': 'all', 'cross_store_points': 'all', 'cross_store_stored': 'all', 'cross_store_refund': 'none',
                  'store_price_mode': 'hq_only'}


def _json():
    return request.get_json(silent=True) or {}


def _page():
    a = request.args
    return max(1, inum(a.get('page'), 1)), min(200, max(1, inum(a.get('size') or a.get('pageSize'), 20)))


def _paged(db, sql_from, args, select, order, page, size):
    total = db.execute('SELECT COUNT(*) ' + sql_from, args).fetchone()[0]
    rows = db.execute('SELECT ' + select + ' ' + sql_from + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', list(args) + [size, (page - 1) * size]).fetchall()
    return total, rows


# ================================================================ 额度 / 新门店（供本文件、门店导入调用）
def check_quota(db, mid, kind):
    m = db.execute('SELECT max_stores, max_users FROM merchants WHERE id=?', (mid,)).fetchone()
    if m is None:
        raise BizError('商户不存在', 404)
    if kind == 'stores':
        lim, used, label = int(m['max_stores'] or 0), db.execute('SELECT COUNT(*) FROM stores WHERE merchant_id=?', (mid,)).fetchone()[0], '门店'
    else:
        lim, used, label = int(m['max_users'] or 0), db.execute("SELECT COUNT(*) FROM users WHERE merchant_id=? AND status='启用'",
                                                                (mid,)).fetchone()[0], '启用账号'
    if lim > 0 and used >= lim:
        raise BizError('%s数量已达平台授权上限（%d/%d），请联系平台增加额度' % (label, used, lim), 403, 'QUOTA_EXCEEDED',
                       {'kind': kind, 'used': used, 'max': lim})
    return used, lim


def quota_usage(db, mid):
    m = db.execute('SELECT max_stores, max_users FROM merchants WHERE id=?', (mid,)).fetchone()
    return {'stores_used': db.execute('SELECT COUNT(*) FROM stores WHERE merchant_id=?', (mid,)).fetchone()[0],
            'users_used': db.execute("SELECT COUNT(*) FROM users WHERE merchant_id=? AND status='启用'", (mid,)).fetchone()[0],
            'max_stores': int(m['max_stores'] or 0) if m else 0, 'max_users': int(m['max_users'] or 0) if m else 0}


def after_store_created(db, mid, code, name):
    """新门店：建门店仓（仓库表归①，只能调①的服务函数；基线未提供时由①的迁移脚本按门店补建）。"""
    from . import goods_svc
    fn = getattr(goods_svc, 'ensure_store_warehouse', None)
    if fn is not None:
        fn(db, mid, code, name)


# ================================================================ 商户资料 + 连锁政策（ERP-001 / ERP-006）
def get_policy(db, mid):
    r = db.execute("SELECT value FROM settings WHERE merchant_id=? AND key='chain_policy'", (mid,)).fetchone()
    pol = dict(POLICY_DEFAULT)
    if r:
        try:
            pol.update(json.loads(r[0]))
        except Exception:
            pass
    return pol


@bp.route('/merchant')
@require_perm('org.view')
def merchant_get():
    db = get_db()
    a = current_actor()
    m = dict(db.execute('SELECT id, code, name, contact, phone, address, biz_info, status, plan, expire_date, max_stores, max_users, created_at '
                        'FROM merchants WHERE id=?', (a['merchant_id'],)).fetchone())
    m['policy'] = get_policy(db, a['merchant_id'])
    m['policy_options'] = dict((k, {'label': v[0], 'choices': v[1]}) for k, v in POLICY_KEYS.items())
    m.update(quota_usage(db, a['merchant_id']))
    return ok(m)


@bp.route('/merchant', methods=['POST'])
@require_perm('org.edit')
def merchant_save():
    """商户可改：名称/联系人/电话/地址/经营信息 + 连锁政策。状态与额度只能由平台改。"""
    db = get_db()
    a = current_actor()
    d = _json()
    mid = a['merchant_id']
    with tx(db):
        old = dict(db.execute('SELECT name, contact, phone, address, biz_info FROM merchants WHERE id=?', (mid,)).fetchone())
        new = dict(old)
        for k, n in (('name', 60), ('contact', 30), ('phone', 30), ('address', 200), ('biz_info', 500)):
            if k in d:
                new[k] = str(d.get(k) or '').strip()[:n]
        if not new['name']:
            raise BizError('商户名称不能为空')
        db.execute('UPDATE merchants SET name=?, contact=?, phone=?, address=?, biz_info=?, updated_at=? WHERE id=?',
                   (new['name'], new['contact'], new['phone'], new['address'], new['biz_info'], now_s(), mid))
        if new != old:
            audit(db, a, 'org', '修改商户资料', '', old, new)
        if isinstance(d.get('policy'), dict):
            pol_old = get_policy(db, mid)
            pol = dict(pol_old)
            for k, v in d['policy'].items():
                if k not in POLICY_KEYS:
                    continue
                if v not in POLICY_KEYS[k][1]:
                    raise BizError('%s 只能是：%s' % (POLICY_KEYS[k][0], '/'.join(POLICY_KEYS[k][1])))
                pol[k] = v
            db.execute("INSERT OR REPLACE INTO settings(merchant_id, key, value) VALUES(?, 'chain_policy', ?)", (mid, json.dumps(pol)))
            if pol != pol_old:
                audit(db, a, 'org', '修改连锁政策', '', pol_old, pol, (d.get('reason') or '')[:200], level='high')
    return merchant_get()


# ================================================================ 组织：总部 / 区域（ERP-002）
@bp.route('/units')
@require_perm('org.view')
def units_list():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    allowed = set(scope_stores(db, a))
    rows = []
    for r in db.execute('SELECT * FROM org_units WHERE merchant_id=? ORDER BY kind, sort, code', (mid,)):
        d = dict(r)
        d['stores'] = [x[0] for x in db.execute('SELECT code FROM stores WHERE merchant_id=? AND region_id=? ORDER BY sort, code', (mid, r['id']))
                       if x[0] in allowed]                 # 只列数据范围内的门店
        rows.append(d)
    return ok(rows)


@bp.route('/units', methods=['POST'])
@require_perm('org.edit')
def units_save():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    d = _json()
    code = (d.get('code') or '').strip()
    name = (d.get('name') or '').strip()
    kind = d.get('kind') or 'region'
    status = d.get('status') if d.get('status') in USER_STATUS else '启用'
    if kind not in ('hq', 'region'):
        raise BizError('组织类型只能是 hq（总部）或 region（区域）')
    if not re.match(r'^[A-Za-z0-9_-]{1,20}$', code) or not name:
        raise BizError('请填写组织编码（1-20 位字母数字）和名称')
    parent_id = None
    if d.get('parent_code'):
        p = db.execute('SELECT id FROM org_units WHERE merchant_id=? AND code=?', (mid, d['parent_code'])).fetchone()
        if p is None:
            raise BizError('上级组织不存在')
        parent_id = p[0]
    with tx(db):
        old = db.execute('SELECT * FROM org_units WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
        if old is None:
            db.execute('INSERT INTO org_units(merchant_id, parent_id, kind, code, name, status, sort, created_at) VALUES(?,?,?,?,?,?,?,?)',
                       (mid, parent_id, kind, code, name, status, inum(d.get('sort')), now_s()))
            audit(db, a, 'org', '新增组织', code, None, {'kind': kind, 'name': name, 'parent': d.get('parent_code') or ''})
        else:
            db.execute('UPDATE org_units SET name=?, parent_id=?, status=?, sort=? WHERE id=?', (name, parent_id, status, inum(d.get('sort')), old['id']))
            audit(db, a, 'org', '调整组织', code, {'name': old['name'], 'status': old['status'], 'parent_id': old['parent_id']},
                  {'name': name, 'status': status, 'parent_id': parent_id}, (d.get('reason') or '')[:200])
    return units_list()


# ================================================================ 门店（ERP-003）
STORE_FIELDS = (('name', 40), ('address', 100), ('phone', 30), ('manager_name', 20), ('open_hours', 40))


@bp.route('/stores')
@require_perm('org.view')
def stores_list():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    allowed = scope_stores(db, a)
    regions = dict((r['id'], (r['code'], r['name'])) for r in db.execute('SELECT id, code, name FROM org_units WHERE merchant_id=?', (mid,)))
    rows = []
    for r in db.execute('SELECT * FROM stores WHERE merchant_id=? ORDER BY sort, code', (mid,)):
        if r['code'] not in allowed:
            continue
        d = dict(r)
        rg = regions.get(r['region_id'])
        d['region_code'], d['region_name'] = (rg if rg else ('', ''))
        d['users'] = db.execute("SELECT COUNT(*) FROM users WHERE merchant_id=? AND store_code=? AND status='启用'", (mid, r['code'])).fetchone()[0]
        rows.append(d)
    return ok(rows)


@bp.route('/stores', methods=['POST'])
@require_perm('org.edit')
def stores_save():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    d = _json()
    code = (d.get('code') or '').strip()
    if not re.match(r'^[A-Za-z0-9_-]{2,12}$', code):
        raise BizError('门店编码为 2-12 位字母或数字')
    vals = dict((k, str(d.get(k) or '').strip()[:n]) for k, n in STORE_FIELDS)
    if not vals['name']:
        raise BizError('请填写门店名称')
    status = d.get('status') if d.get('status') in STORE_STATUS else '营业'
    store_type = d.get('store_type') if d.get('store_type') in ('直营', '加盟') else '直营'
    region_id = None
    if d.get('region_code'):
        rg = db.execute("SELECT id FROM org_units WHERE merchant_id=? AND code=? AND kind='region'", (mid, d['region_code'])).fetchone()
        if rg is None:
            raise BizError('区域不存在：%s' % d['region_code'])
        region_id = rg[0]
    with tx(db):
        old = db.execute('SELECT * FROM stores WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
        if old is None:
            check_quota(db, mid, 'stores')
            db.execute('INSERT INTO stores(merchant_id, code, name, address, phone, status, sort, created_at, region_id, store_type, manager_name, open_hours) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (mid, code, vals['name'], vals['address'], vals['phone'], status, inum(d.get('sort')),
                                                         now_s(), region_id, store_type, vals['manager_name'], vals['open_hours']))
            after_store_created(db, mid, code, vals['name'])
            audit(db, a, 'org', '新增门店', code, None, dict(vals, status=status, region_id=region_id), store_code=code)
        else:
            if a['data_scope'] != 'merchant':
                scope_stores(db, a, code)
            db.execute('UPDATE stores SET name=?, address=?, phone=?, status=?, sort=?, region_id=?, store_type=?, manager_name=?, open_hours=? '
                       'WHERE id=?', (vals['name'], vals['address'], vals['phone'], status, inum(d.get('sort')), region_id, store_type,
                                      vals['manager_name'], vals['open_hours'], old['id']))
            before = dict((k, old[k]) for k in ('name', 'address', 'phone', 'status', 'region_id', 'store_type', 'manager_name', 'open_hours'))
            after = dict(vals, status=status, region_id=region_id, store_type=store_type)
            audit(db, a, 'org', '修改门店', code, before, after, (d.get('reason') or '')[:200], store_code=code)
    return ok(dict(db.execute('SELECT * FROM stores WHERE merchant_id=? AND code=?', (mid, code)).fetchone()))


# ================================================================ 导购（v1 guides 表；收银台选导购、会员归属导购用）
GUIDE_STATUS = ('在职', '离职')


@bp.route('/guides')
@require_perm('org.view')
def guides_list():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    allowed = scope_stores(db, a)
    store = (request.args.get('store') or '').strip()
    names = dict((r[0], r[1]) for r in db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)))
    rows = []
    for r in db.execute('SELECT id, code, name, store_code, status FROM guides WHERE merchant_id=? ORDER BY store_code, code', (mid,)):
        if r['store_code'] not in allowed or (store and r['store_code'] != store):
            continue
        d = dict(r)
        d['store_name'] = names.get(r['store_code'], '')
        rows.append(d)
    return ok(rows)


@bp.route('/guides', methods=['POST'])
@require_perm('org.edit')
def guides_save():
    """新增/修改/停用导购：{code, name, store_code, status:在职|离职, reason}。编号商户内唯一；只能动数据范围内门店的导购。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    d = _json()
    code = (d.get('code') or '').strip()
    name = (d.get('name') or '').strip()[:30]
    store = (d.get('store_code') or '').strip()
    if not re.match(r'^[A-Za-z0-9_-]{1,20}$', code):
        raise BizError('导购编号为 1-20 位字母或数字')
    if not name:
        raise BizError('请填写导购姓名')
    if db.execute('SELECT 1 FROM stores WHERE merchant_id=? AND code=?', (mid, store)).fetchone() is None:
        raise BizError('门店不存在：%s' % store)
    scope_stores(db, a, store)                       # 目标门店不在范围 → 403
    status = d.get('status') if d.get('status') in GUIDE_STATUS else '在职'
    with tx(db):
        old = db.execute('SELECT * FROM guides WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
        if old is None:
            db.execute('INSERT INTO guides(merchant_id, code, name, store_code, status) VALUES(?,?,?,?,?)', (mid, code, name, store, status))
            audit(db, a, 'org', '新增导购', code, None, {'name': name, 'store_code': store, 'status': status}, (d.get('reason') or '')[:200],
                  store_code=store)
        else:
            if old['store_code'] and old['store_code'] != store:
                scope_stores(db, a, old['store_code'])   # 从别的门店调过来：原门店也必须在范围内
            db.execute('UPDATE guides SET name=?, store_code=?, status=? WHERE id=?', (name, store, status, old['id']))
            before = {'name': old['name'], 'store_code': old['store_code'], 'status': old['status']}
            after = {'name': name, 'store_code': store, 'status': status}
            if before != after:
                audit(db, a, 'org', '停用导购' if status == '离职' and old['status'] != '离职' else '修改导购', code, before, after,
                      (d.get('reason') or '')[:200], store_code=store)
    return ok(dict(db.execute('SELECT id, code, name, store_code, status FROM guides WHERE merchant_id=? AND code=?', (mid, code)).fetchone()))


# ================================================================ 员工（ERP-701）
def _role(db, mid, role_id):
    r = db.execute('SELECT * FROM roles WHERE merchant_id=? AND id=?', (mid, inum(role_id))).fetchone()
    if r is None:
        raise BizError('角色不存在')
    return r


def _role_perms(db, rid):
    return set(x[0] for x in db.execute('SELECT perm FROM role_perms WHERE role_id=?', (rid,)))


def _user_out(db, a, r, roles, stores):
    d = {k: r[k] for k in ('id', 'username', 'name', 'emp_no', 'role_id', 'store_code', 'status', 'created_at', 'last_login', 'must_change_pw')}
    rr = roles.get(r['role_id'])
    d['role_code'], d['role_name'], d['data_scope'] = (rr['code'], rr['name'], rr['data_scope']) if rr else ('', r['role'], '')
    d['store_name'] = stores.get(r['store_code'] or '', '')
    d['phone'] = r['phone'] if has_perm(a, 'user.edit') else mask_phone(r['phone'], a)
    d['has_pin'] = bool(r['pin_hash'])
    d['scopes'] = [{'type': s['scope_type'], 'code': s['ref_code']} for s in db.execute(
        'SELECT scope_type, ref_code FROM user_scopes WHERE user_id=? ORDER BY scope_type, ref_code', (r['id'],))]
    d['online'] = db.execute('SELECT COUNT(*) FROM user_sessions WHERE user_id=? AND revoked_at IS NULL', (r['id'],)).fetchone()[0]
    return d


@bp.route('/users')
@require_perm('user.view')
def users_list():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    page, size = _page()
    clauses, args = ['merchant_id=?'], [mid]
    if a['data_scope'] != 'merchant':
        allowed = scope_stores(db, a)
        clauses.append("store_code IN (%s)" % ','.join('?' * len(allowed)) if allowed else '1=0')
        args += allowed
    st = (request.args.get('store') or '').strip()
    if st:
        scope_stores(db, a, st)
        clauses.append('store_code=?')
        args.append(st)
    kw = (request.args.get('kw') or '').strip()
    if kw:
        clauses.append('(username LIKE ? OR name LIKE ? OR emp_no LIKE ?)')
        args += ['%' + kw + '%'] * 3
    if request.args.get('status') in USER_STATUS:
        clauses.append('status=?')
        args.append(request.args.get('status'))
    total, rows = _paged(db, 'FROM users WHERE ' + ' AND '.join(clauses), args, '*', 'id', page, size)
    roles = dict((r['id'], r) for r in db.execute('SELECT * FROM roles WHERE merchant_id=?', (mid,)))
    stores = dict((r['code'], r['name']) for r in db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)))
    return ok({'page': page, 'size': size, 'total': total, 'items': [_user_out(db, a, r, roles, stores) for r in rows]})


@bp.route('/users', methods=['POST'])
@require_perm('user.edit')
def users_save():
    """新增/修改员工：角色、主门店、额外授权门店/区域、员工号+PIN、状态（停用=离职）。
    改角色/调店/停用/改密码/改 PIN/改授权范围 -> revoke_user_sessions（所有终端立即失效，AC-013）。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    d = _json()
    uid = inum(d.get('id'))
    with tx(db):
        role = _role(db, mid, d.get('role_id'))
        if role['status'] != '启用':
            raise BizError('角色已停用')
        if a['data_scope'] != 'merchant' and not _role_perms(db, role['id']) <= set(a['perms']):
            raise BizError('不能分配比自己权限更大的角色', 403)
        store = (d.get('store_code') or '').strip()
        if store:
            if db.execute('SELECT 1 FROM stores WHERE merchant_id=? AND code=?', (mid, store)).fetchone() is None:
                raise BizError('门店不存在：%s' % store)
            scope_stores(db, a, store)
        elif role['data_scope'] in ('store', 'self'):
            raise BizError('该角色（%s）必须绑定一个门店' % role['name'])
        elif a['data_scope'] != 'merchant':
            raise BizError('只有总部账号可以创建不绑定门店的员工', 403)
        scopes = []
        for s in (d.get('scopes') or []):
            t, c = (s.get('type') or ''), (s.get('code') or '').strip()
            if t == 'store':
                if db.execute('SELECT 1 FROM stores WHERE merchant_id=? AND code=?', (mid, c)).fetchone() is None:
                    raise BizError('授权门店不存在：%s' % c)
                scope_stores(db, a, c)
            elif t == 'region':
                if db.execute("SELECT 1 FROM org_units WHERE merchant_id=? AND code=? AND kind='region'", (mid, c)).fetchone() is None:
                    raise BizError('授权区域不存在：%s' % c)
                if a['data_scope'] != 'merchant':
                    raise BizError('只有总部账号可以授权区域', 403)
            else:
                raise BizError('授权范围类型只能是 store / region')
            scopes.append((t, c))
        scopes = sorted(set(scopes))
        status = d.get('status') if d.get('status') in USER_STATUS else '启用'
        emp_no = (d.get('emp_no') or '').strip()[:20]
        if emp_no and not re.match(r'^[A-Za-z0-9_-]{1,20}$', emp_no):
            raise BizError('员工编号只能是字母数字')
        pin = str(d.get('pin') or '').strip()
        if pin and not re.match(r'^\d{4,8}$', pin):
            raise BizError('收银 PIN 为 4-8 位数字')
        pwd = d.get('password') or ''
        if pwd and len(pwd) < 8:
            raise BizError('密码至少 8 位')
        name = (d.get('name') or '').strip()[:20]
        phone = (d.get('phone') or '').strip()[:20]
        if emp_no:
            dup = db.execute("SELECT id FROM users WHERE merchant_id=? AND emp_no=? AND id != ?", (mid, emp_no, uid)).fetchone()
            if dup:
                raise BizError('员工编号已被使用：%s' % emp_no)
        if uid:
            u = db.execute('SELECT * FROM users WHERE merchant_id=? AND id=?', (mid, uid)).fetchone()
            if u is None:
                raise BizError('员工不存在', 404)
            if a['data_scope'] != 'merchant':
                if not u['store_code']:
                    raise BizError('无权修改总部员工', 403)
                scope_stores(db, a, u['store_code'])
            if u['id'] == a['id'] and (status != '启用' or role['id'] != u['role_id']):
                raise BizError('不能停用自己或修改自己的角色')
            if status == '启用' and u['status'] != '启用':
                check_quota(db, mid, 'users')
            old_scopes = sorted((s[0], s[1]) for s in db.execute('SELECT scope_type, ref_code FROM user_scopes WHERE user_id=?', (uid,)))
            before = {'role_id': u['role_id'], 'store_code': u['store_code'], 'status': u['status'], 'emp_no': u['emp_no'],
                      'scopes': old_scopes, 'name': u['name']}
            db.execute('UPDATE users SET name=?, role=?, role_id=?, store_code=?, phone=?, status=?, emp_no=? WHERE id=?',
                       (name or u['name'], role['legacy_role'], role['id'], store, phone, status, emp_no, uid))
            if pwd:
                db.execute('UPDATE users SET password_hash=?, must_change_pw=1 WHERE id=?', (generate_password_hash(pwd), uid))
            if pin:
                db.execute('UPDATE users SET pin_hash=? WHERE id=?', (generate_password_hash(pin), uid))
            db.execute('DELETE FROM user_scopes WHERE user_id=?', (uid,))
            after = {'role_id': role['id'], 'store_code': store, 'status': status, 'emp_no': emp_no, 'scopes': scopes, 'name': name or u['name']}
            changed = (before['role_id'] != role['id'] or (before['store_code'] or '') != store or before['status'] != status
                       or old_scopes != scopes or bool(pwd) or bool(pin))
            action = '员工离职/停用' if (status == '停用' and u['status'] != '停用') else ('员工调店' if (u['store_code'] or '') != store else '修改员工')
            if changed:
                revoke_user_sessions(db, uid, action)
            audit(db, a, 'user', action, u['username'], before, dict(after, password_changed=bool(pwd), pin_changed=bool(pin)),
                  (d.get('reason') or '')[:200], level='high' if changed else 'info', store_code=store)
        else:
            username = (d.get('username') or '').strip()
            if not re.match(r'^[A-Za-z0-9_]{3,20}$', username):
                raise BizError('登录账号为 3-20 位字母、数字或下划线')
            if len(pwd) < 8:
                raise BizError('初始密码至少 8 位')
            if db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                raise BizError('登录账号已存在（全平台唯一）')
            check_quota(db, mid, 'users')
            db.execute('INSERT INTO users(merchant_id, username, password_hash, name, role, role_id, store_code, emp_no, pin_hash, phone, status, '
                       'created_at, must_change_pw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)',
                       (mid, username, generate_password_hash(pwd), name or username, role['legacy_role'], role['id'], store, emp_no,
                        generate_password_hash(pin) if pin else '', phone, status, now_s()))
            uid = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()[0]
            audit(db, a, 'user', '新增员工', username, None, {'role_id': role['id'], 'store_code': store, 'emp_no': emp_no, 'scopes': scopes},
                  level='high', store_code=store)
        for t, c in scopes:
            db.execute('INSERT OR IGNORE INTO user_scopes(user_id, scope_type, ref_code) VALUES(?,?,?)', (uid, t, c))
    roles = dict((r['id'], r) for r in db.execute('SELECT * FROM roles WHERE merchant_id=?', (mid,)))
    stores = dict((r['code'], r['name']) for r in db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)))
    return ok(_user_out(db, a, db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone(), roles, stores))


def _target_user(db, a, uid):
    u = db.execute('SELECT * FROM users WHERE merchant_id=? AND id=?', (a['merchant_id'], uid)).fetchone()
    if u is None:
        raise BizError('员工不存在', 404)
    if a['data_scope'] != 'merchant':
        if not u['store_code']:
            raise BizError('无权管理总部员工', 403)
        scope_stores(db, a, u['store_code'])
    return u


# ================================================================ 会话（ERP-704 / NFR-SEC-006）
@bp.route('/users/<int:uid>/sessions')
@require_perm('user.edit')
def sessions_list(uid):
    db = get_db()
    a = current_actor()
    _target_user(db, a, uid)
    rows = [dict(r) for r in db.execute('SELECT sid, client, device, ip, created_at, last_seen, revoked_at, revoke_reason FROM user_sessions '
                                        'WHERE merchant_id=? AND user_id=? ORDER BY created_at DESC LIMIT 50', (a['merchant_id'], uid))]
    for r in rows:
        r['sid'] = r['sid'][:8] + '…'          # 会话 id 不完整返回
        r['active'] = not r['revoked_at']
    return ok(rows)


@bp.route('/users/<int:uid>/sessions', methods=['DELETE'])
@require_perm('user.edit')
def sessions_kick(uid):
    """踢下线：该员工所有终端立即失效（会话吊销 + token_version+1）。"""
    db = get_db()
    a = current_actor()
    with tx(db):
        u = _target_user(db, a, uid)
        n = db.execute('SELECT COUNT(*) FROM user_sessions WHERE user_id=? AND revoked_at IS NULL', (uid,)).fetchone()[0]
        revoke_user_sessions(db, uid, '管理员踢下线')
        audit(db, a, 'user', '踢下线', u['username'], {'active_sessions': n}, {'active_sessions': 0},
              (request.args.get('reason') or '')[:200], level='security', store_code=u['store_code'] or '')
    return ok({'revoked': n})


# ================================================================ 角色权限（ERP-702 / 3.3）
def _role_out(db, r):
    d = dict(r)
    d['perms'] = sorted(_role_perms(db, r['id']))
    d['limits'] = dict((x[0], x[1]) for x in db.execute('SELECT limit_key, value FROM role_limits WHERE role_id=?', (r['id'],)))
    d['users'] = db.execute("SELECT COUNT(*) FROM users WHERE role_id=? AND status='启用'", (r['id'],)).fetchone()[0]
    d['data_scope_name'] = DATA_SCOPES.get(r['data_scope'], r['data_scope'])
    return d


@bp.route('/roles')
@require_perm('role.edit', 'user.edit')
def roles_list():
    db = get_db()
    mid = current_actor()['merchant_id']
    return ok([_role_out(db, r) for r in db.execute('SELECT * FROM roles WHERE merchant_id=? ORDER BY is_system DESC, id', (mid,))])


def _check_limit_value(k, v):
    v = str(v).strip()
    if k == 'min_discount_rate':
        try:
            from decimal import Decimal
            x = Decimal(v)
        except Exception:
            raise BizError('最低折扣率应为 0~1 的小数')
        if x < 0 or x > 1:
            raise BizError('最低折扣率应为 0~1 的小数')
    elif k in ('price_change', 'no_receipt_refund'):
        if v not in ('0', '1'):
            raise BizError('%s 只能是 0 或 1' % LIMIT_KEYS[k])
    else:
        if not re.match(r'^-?\d+$', v) or int(v) < -1:
            raise BizError('%s 应为整数（-1 表示不限）' % LIMIT_KEYS[k])
    return v


@bp.route('/roles', methods=['POST'])
@require_perm('role.edit')
def roles_save():
    """新增 / 修改 / 复制角色：{id?, copy_from?, code, name, data_scope, perms[], limits{}, status, note, reason}。
    系统角色：权限码由模板维护（升级会补齐），只允许改名称和操作额度；要改权限请先复制再自定义。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    d = _json()
    rid = inum(d.get('id'))
    with tx(db):
        src = None
        if d.get('copy_from'):
            src = _role(db, mid, d.get('copy_from'))
        if rid:
            r = _role(db, mid, rid)
            before = _role_out(db, r)
            name = (d.get('name') or r['name']).strip()[:30]
            status = d.get('status') if d.get('status') in USER_STATUS else r['status']
            if r['is_system']:
                if 'perms' in d and sorted(set(d.get('perms') or [])) != before['perms']:
                    raise BizError('系统角色的权限码不能直接修改，请复制后自定义')
                if d.get('data_scope') and d['data_scope'] != r['data_scope']:
                    raise BizError('系统角色的数据范围不能修改，请复制后自定义')
                if status != '启用':
                    raise BizError('系统角色不能停用')
                scope = r['data_scope']
            else:
                scope = d.get('data_scope') or r['data_scope']
            db.execute('UPDATE roles SET name=?, data_scope=?, status=?, note=?, updated_at=? WHERE id=?',
                       (name, scope, status, (d.get('note') or r['note'] or '')[:200], now_s(), rid))
        else:
            code = (d.get('code') or '').strip()
            if not re.match(r'^[a-z][a-z0-9_]{1,30}$', code):
                raise BizError('角色编码为小写字母开头的 2-31 位字母数字下划线')
            if code in ROLE_TEMPLATES or db.execute('SELECT 1 FROM roles WHERE merchant_id=? AND code=?', (mid, code)).fetchone():
                raise BizError('角色编码已存在：%s' % code)
            name = (d.get('name') or '').strip()[:30]
            if not name:
                raise BizError('请填写角色名称')
            scope = d.get('data_scope') or (src['data_scope'] if src is not None else 'store')
            legacy = src['legacy_role'] if src is not None else 'staff'
            if legacy in ('admin', 'platform'):
                legacy = 'staff'          # 自定义角色不继承 v1「总部管理员」粗角色（v1 旧接口按粗角色放行）
            db.execute('INSERT INTO roles(merchant_id, code, name, data_scope, legacy_role, is_system, status, note, created_at, updated_at) '
                       'VALUES(?,?,?,?,?,0,?,?,?,?)', (mid, code, name, scope, legacy, '启用', (d.get('note') or '')[:200], now_s(), now_s()))
            rid = db.execute('SELECT id FROM roles WHERE merchant_id=? AND code=?', (mid, code)).fetchone()[0]
            before = None
            if src is not None:
                for p in _role_perms(db, src['id']):
                    db.execute('INSERT INTO role_perms(role_id, perm) VALUES(?,?)', (rid, p))
                for k, v in db.execute('SELECT limit_key, value FROM role_limits WHERE role_id=?', (src['id'],)).fetchall():
                    db.execute('INSERT INTO role_limits(role_id, limit_key, value) VALUES(?,?,?)', (rid, k, v))
        if scope not in DATA_SCOPES:
            raise BizError('数据范围只能是：%s' % '/'.join(DATA_SCOPES))
        r = _role(db, mid, rid)
        if 'perms' in d and not r['is_system']:
            perms = sorted(set(d.get('perms') or []))
            bad = [p for p in perms if p not in PERMS or p.startswith('platform.')]
            if bad:
                raise BizError('未知或不允许的权限码：%s' % ','.join(bad[:5]))
            if a['data_scope'] != 'merchant' and not set(perms) <= set(a['perms']):
                raise BizError('不能授予自己没有的权限', 403)
            db.execute('DELETE FROM role_perms WHERE role_id=?', (rid,))
            for p in perms:
                db.execute('INSERT INTO role_perms(role_id, perm) VALUES(?,?)', (rid, p))
        if isinstance(d.get('limits'), dict):
            for k, v in d['limits'].items():
                if k not in LIMIT_KEYS:
                    raise BizError('未知额度项：%s' % k)
                db.execute('INSERT OR REPLACE INTO role_limits(role_id, limit_key, value) VALUES(?,?,?)', (rid, k, _check_limit_value(k, v)))
        after = _role_out(db, r)
        audit(db, a, 'role', '复制角色' if src is not None and before is None else ('新增角色' if before is None else '修改角色'), r['code'],
              before and {k: before[k] for k in ('name', 'data_scope', 'status', 'perms', 'limits')},
              {k: after[k] for k in ('name', 'data_scope', 'status', 'perms', 'limits')}, (d.get('reason') or '')[:200], level='high')
    return ok(after)


@bp.route('/perms')
@require_perm('role.edit', 'user.edit')
def perms_registry():
    groups = {}
    names = {'pos': '收银台', 'goods': '商品', 'supplier': '供应商', 'purchase': '采购', 'stock': '库存', 'member': '会员', 'promo': '促销',
             'price': '价格', 'coupon': '优惠券', 'org': '组织', 'user': '员工', 'role': '角色', 'sys': '系统配置', 'audit': '审计',
             'report': '报表', 'approval': '审批', 'app': '手机端', 'notice': '公告'}
    for code, (name, mod) in PERMS.items():
        if code.startswith('platform.'):
            continue
        groups.setdefault(mod, {'module': mod, 'name': names.get(mod, mod), 'perms': []})['perms'].append({'code': code, 'name': name})
    return ok({'groups': list(groups.values()), 'limits': [{'key': k, 'name': v} for k, v in LIMIT_KEYS.items()],
               'data_scopes': [{'code': k, 'name': v} for k, v in DATA_SCOPES.items()]})


# ================================================================ 审计查询（ERP-705 / 3.4）
@bp.route('/audit')
@require_perm('audit.view')
def audit_list():
    db = get_db()
    a = current_actor()
    page, size = _page()
    q = request.args
    clauses, args = ['merchant_id=?'], [a['merchant_id']]
    if a['data_scope'] != 'merchant':
        allowed = scope_stores(db, a)
        clauses.append('store_code IN (%s)' % ','.join('?' * len(allowed)) if allowed else '1=0')
        args += allowed
    if q.get('store'):
        clauses.append('store_code IN (%s)' % ','.join('?' * len(scope_stores(db, a, q.get('store')))))
        args += scope_stores(db, a, q.get('store'))
    for k, col in (('user', 'username'), ('ref_no', 'ref_no'), ('module', 'module'), ('level', 'level')):
        v = (q.get(k) or '').strip()
        if v:
            clauses.append('%s=?' % col)
            args.append(v)
    if (q.get('action') or '').strip():
        clauses.append('action LIKE ?')
        args.append('%' + q.get('action').strip() + '%')
    if (q.get('start') or '').strip():
        clauses.append('created_at >= ?')
        args.append(q.get('start').strip()[:10])
    if (q.get('end') or '').strip():
        clauses.append('created_at <= ?')
        args.append(q.get('end').strip()[:10] + ' 23:59:59')
    total, rows = _paged(db, 'FROM audit_logs WHERE ' + ' AND '.join(clauses), args, '*', 'id DESC', page, size)
    return ok({'page': page, 'size': size, 'total': total, 'items': [dict(r) for r in rows]})
