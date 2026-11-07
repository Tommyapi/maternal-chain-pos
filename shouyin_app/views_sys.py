# -*- coding: utf-8 -*-
"""系统配置：收银参数/编号规则/小票与班报模板/预警参数/数据字典/初始化检查/导入模板下载中心/门店导入。

Owner：模块④（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-901~905 DATA-001/002 NFR-SEC-007（上传校验）
接口清单与入参出参：docs/API-CONTRACT.md §4.4；返回格式 {ok:true,data} / {ok:false,code,msg}；权限一律 @require_perm(...)。
服务函数在 sys_svc.py（dict_items / get_print_template / alert_params），供③④⑤同进程调用。
"""
import json
import re
from datetime import datetime

from flask import Blueprint, request

from .core import (get_db, tx, require_perm, api, current_actor, scope_stores, has_perm, audit, ok, get_config, save_config, DEFAULT_CONFIG,
                   PAY_METHODS)
from .numbering import DOC_TYPES
from .sys_svc import DICT_TYPES, PRINT_KINDS, PRINT_FIELDS, ALERT_DEFAULT, dict_items, get_print_template, alert_params
from .util import BizError, now_s, inum
from . import xlsx_io

bp = Blueprint('sys', __name__, url_prefix='/api/sys')

CONFIG_LABELS = {
    'points_per_yuan': '每实付 1 元积几分', 'points_to_yuan': '多少积分抵 1 元', 'points_max_ratio': '积分最多抵扣订单金额比例',
    'round_mode': '抹零（none/jiao/yuan）', 'cashier_can_change_price': '收银员可直接改价（v1 兼容，v2 以角色额度为准）',
    'cashier_max_discount': '收银员最低折扣（v1 兼容，v2 以角色额度为准）', 'receipt_title': '小票抬头', 'receipt_footer': '小票底部文字',
    'receipt_width': '小票纸宽 58/80', 'allow_negative_stock': '允许负库存销售（还需角色有 pos.negative_stock）',
    'recharge_rules': '储值赠送规则', 'member_levels': '会员等级',
}


def _json():
    return request.get_json(silent=True) or {}


# ================================================================ 收银参数（商户级）
@bp.route('/config')
@require_perm('sys.config')
def config_get():
    db = get_db()
    a = current_actor()
    return ok({'config': get_config(db, a['merchant_id']), 'labels': CONFIG_LABELS, 'pay_methods': [{'code': c, 'name': n} for c, n in PAY_METHODS]})


@bp.route('/config', methods=['POST'])
@require_perm('sys.config')
def config_save():
    db = get_db()
    a = current_actor()
    d = _json()
    unknown = [k for k in d if k not in DEFAULT_CONFIG and k != 'reason']
    if unknown:
        raise BizError('未知参数：%s' % ','.join(unknown[:5]))
    with tx(db):
        before = get_config(db, a['merchant_id'])
        cfg = save_config(db, d, a['merchant_id'])
        changed = dict((k, cfg[k]) for k in cfg if before.get(k) != cfg[k])
        if changed:
            audit(db, a, 'sys', '修改收银参数', '', dict((k, before.get(k)) for k in changed), changed, (d.get('reason') or '')[:200], level='high')
    return ok({'config': cfg, 'changed': sorted(changed)})


# ================================================================ 编号规则（ERP-901）
@bp.route('/numbering')
@require_perm('sys.config')
def numbering_info():
    db = get_db()
    a = current_actor()
    st = db.execute('SELECT code FROM stores WHERE merchant_id=? ORDER BY sort, code LIMIT 1', (a['merchant_id'],)).fetchone()
    scope = ''.join(ch for ch in (st[0] if st else '') if ch.isalnum())[-4:].rjust(4, '0') if st else '0000'
    day = datetime.now().strftime('%y%m%d')
    return ok({'rule': '<类型><商户号 3 位><门店/仓 4 位><年月日 6 位><当日流水 5 位>',
               'notes': ['单号含商户和门店信息，全局唯一（数据库唯一约束兜底）', '提交后的单号永不回收；作废单据保留原号，不物理删除',
                         '离线收银（P1）将使用「终端号 + 本地序号」，不占用本序列'],
               'types': [{'code': k, 'name': v, 'example': '%s%03d%s%s%05d' % (k, a['merchant_id'], scope, day, 1)} for k, v in DOC_TYPES.items()]})


# ================================================================ 数据字典（ERP-904）
@bp.route('/dicts')
@api()
def dicts_get():
    """所有登录账号可读（收银台/APP 要用退货原因、单位等）；有 sys.dict 的人能看到已停用项。"""
    db = get_db()
    a = current_actor()
    t = request.args.get('type') or ''
    full = has_perm(a, 'sys.dict')
    types = [t] if t else list(DICT_TYPES)
    for x in types:
        if x not in DICT_TYPES:
            raise BizError('未知字典类型：%s' % x)
    return ok({'types': [{'code': k, 'name': v} for k, v in DICT_TYPES.items()],
               'items': dict((x, dict_items(db, a['merchant_id'], x, include_disabled=full)) for x in types)})


@bp.route('/dicts', methods=['POST'])
@require_perm('sys.dict')
def dicts_save():
    """{dict_type, code?, name, sort?, status?}：code 为空新增（自动编码）；停用不影响历史数据（历史单据存的是文字快照）。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    d = _json()
    t = d.get('dict_type') or ''
    if t not in DICT_TYPES:
        raise BizError('未知字典类型：%s' % t)
    name = (d.get('name') or '').strip()[:30]
    if not name:
        raise BizError('请填写名称')
    status = d.get('status') if d.get('status') in ('启用', '停用') else '启用'
    code = (d.get('code') or '').strip()
    with tx(db):
        dup = db.execute('SELECT code FROM dict_items WHERE merchant_id=? AND dict_type=? AND name=? AND code != ?', (mid, t, name, code)).fetchone()
        if dup:
            raise BizError('名称重复：%s' % name)
        if code:
            old = db.execute('SELECT * FROM dict_items WHERE merchant_id=? AND dict_type=? AND code=?', (mid, t, code)).fetchone()
            if old is None:
                raise BizError('字典项不存在', 404)
            db.execute('UPDATE dict_items SET name=?, sort=?, status=? WHERE id=?', (name, inum(d.get('sort'), old['sort']), status, old['id']))
            audit(db, a, 'sys', '修改字典', '%s:%s' % (t, code), {'name': old['name'], 'status': old['status']}, {'name': name, 'status': status})
        else:
            n = db.execute('SELECT COUNT(*) FROM dict_items WHERE merchant_id=? AND dict_type=?', (mid, t)).fetchone()[0]
            code = 'D%02d' % (n + 1)
            while db.execute('SELECT 1 FROM dict_items WHERE merchant_id=? AND dict_type=? AND code=?', (mid, t, code)).fetchone():
                n += 1
                code = 'D%02d' % (n + 1)
            db.execute('INSERT INTO dict_items(merchant_id, dict_type, code, name, sort, status) VALUES(?,?,?,?,?,?)',
                       (mid, t, code, name, inum(d.get('sort'), n), status))
            audit(db, a, 'sys', '新增字典', '%s:%s' % (t, code), None, {'name': name})
    return ok({'dict_type': t, 'items': dict_items(db, mid, t, include_disabled=True)})


# ================================================================ 小票 / 班报 / 价签模板（ERP-902）
@bp.route('/print-templates')
@require_perm('sys.print')
def print_list():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    allowed = set(scope_stores(db, a))
    rows = []
    for r in db.execute('SELECT * FROM print_templates WHERE merchant_id=? ORDER BY store_code, kind', (mid,)):
        if r['store_code'] and r['store_code'] not in allowed:
            continue
        d = dict(r)
        d['content'] = json.loads(r['content'] or '{}')
        d['kind_name'] = PRINT_KINDS.get(r['kind'], r['kind'])
        rows.append(d)
    store = (request.args.get('store') or '').strip()
    if store:
        scope_stores(db, a, store)
    effective = dict((k, get_print_template(db, mid, store, k)) for k in PRINT_KINDS)
    return ok({'items': rows, 'kinds': PRINT_KINDS, 'fields': PRINT_FIELDS, 'effective': effective,
               'note': '历史订单补打按成交快照打印，修改模板只影响之后的打印版式'})


@bp.route('/print-templates', methods=['POST'])
@require_perm('sys.print')
def print_save():
    """{store_code?（空=商户默认）, kind, width 58/80, copies 1~3, content:{title, footer, fields:[...], address?, phone?, after_sale?}}。保存版本号 +1。"""
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    d = _json()
    kind = d.get('kind') or ''
    if kind not in PRINT_KINDS:
        raise BizError('模板类型只能是：%s' % '/'.join(PRINT_KINDS))
    store = (d.get('store_code') or '').strip()
    if store:
        if db.execute('SELECT 1 FROM stores WHERE merchant_id=? AND code=?', (mid, store)).fetchone() is None:
            raise BizError('门店不存在：%s' % store)
        scope_stores(db, a, store)
    elif a['data_scope'] != 'merchant':
        raise BizError('只有总部账号可以修改商户默认模板', 403)
    width = inum(d.get('width'), 58)
    copies = inum(d.get('copies'), 1)
    if width not in (58, 80):
        raise BizError('纸宽只能是 58 或 80 毫米')
    if not 1 <= copies <= 3:
        raise BizError('打印份数 1~3')
    c = d.get('content') or {}
    if not isinstance(c, dict):
        raise BizError('content 格式错误')
    fields = [f for f in (c.get('fields') or []) if f in PRINT_FIELDS]
    content = {'title': str(c.get('title') or '')[:40], 'footer': str(c.get('footer') or '')[:300], 'fields': fields,
               'address': str(c.get('address') or '')[:100], 'phone': str(c.get('phone') or '')[:30], 'after_sale': str(c.get('after_sale') or '')[:300]}
    if kind == 'label':
        content['size'] = c.get('size') if c.get('size') in ('40x30', '50x30', '60x40') else '40x30'
    status = d.get('status') if d.get('status') in ('启用', '停用') else '启用'
    with tx(db):
        old = db.execute('SELECT * FROM print_templates WHERE merchant_id=? AND store_code=? AND kind=?', (mid, store, kind)).fetchone()
        js = json.dumps(content, ensure_ascii=False)
        if old is None:
            db.execute('INSERT INTO print_templates(merchant_id, store_code, kind, width, copies, content, version, status, updated_at) '
                       'VALUES(?,?,?,?,?,?,1,?,?)', (mid, store, kind, width, copies, js, status, now_s()))
            ver = 1
        else:
            ver = old['version'] + 1
            db.execute('UPDATE print_templates SET width=?, copies=?, content=?, version=?, status=?, updated_at=? WHERE id=?',
                       (width, copies, js, ver, status, now_s(), old['id']))
        audit(db, a, 'sys', '修改打印模板', '%s:%s' % (store or '商户默认', kind),
              old and {'width': old['width'], 'copies': old['copies'], 'content': json.loads(old['content'] or '{}'), 'version': old['version']},
              {'width': width, 'copies': copies, 'content': content, 'version': ver}, store_code=store)
    return ok(get_print_template(db, mid, store, kind))


# ================================================================ 预警参数（ERP-903）
@bp.route('/alert-params')
@require_perm('sys.config')
def alert_get():
    db = get_db()
    return ok(alert_params(db, current_actor()['merchant_id']))


@bp.route('/alert-params', methods=['POST'])
@require_perm('sys.config')
def alert_save():
    from .perms import PERMS
    db = get_db()
    a = current_actor()
    d = _json()
    with tx(db):
        before = alert_params(db, a['merchant_id'])
        p = dict(before)
        if 'near_expiry_days' in d:
            days = sorted(set(inum(x) for x in (d.get('near_expiry_days') or [])))
            if not days or any(x <= 0 or x > 720 for x in days) or len(days) > 5:
                raise BizError('临期天数为 1~720 的整数，最多 5 级')
            p['near_expiry_days'] = days
        for k, lo, hi in (('safety_stock_default', 0, 1000000), ('slow_moving_days', 7, 720)):
            if k in d:
                v = inum(d.get(k), -1)
                if not lo <= v <= hi:
                    raise BizError('%s 应在 %d~%d 之间' % (k, lo, hi))
                p[k] = v
        if 'notify_time' in d:
            if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', str(d.get('notify_time') or '')):
                raise BizError('推送时间格式应为 HH:MM')
            p['notify_time'] = d['notify_time']
        if 'receivers' in d:
            rc = [x for x in (d.get('receivers') or []) if x in PERMS and not x.startswith('platform.')]
            if not rc:
                raise BizError('请至少选择一个接收权限（如 stock.view）')
            p['receivers'] = rc
        if 'overrides' in d:
            ov = []
            for o in (d.get('overrides') or [])[:500]:
                if o.get('scope') not in ('category', 'product') or not str(o.get('value') or '').strip():
                    raise BizError('覆盖规则需要 scope=category/product 和 value')
                item = {'scope': o['scope'], 'value': str(o['value']).strip()[:40]}
                if o.get('safety_stock') not in (None, ''):
                    item['safety_stock'] = max(0, inum(o.get('safety_stock')))
                if o.get('near_expiry_days') not in (None, ''):
                    item['near_expiry_days'] = max(1, inum(o.get('near_expiry_days')))
                ov.append(item)
            p['overrides'] = ov
        p = dict((k, p[k]) for k in ALERT_DEFAULT)
        db.execute("INSERT OR REPLACE INTO settings(merchant_id, key, value) VALUES(?, 'alert_params', ?)", (a['merchant_id'], json.dumps(p, ensure_ascii=False)))
        if p != before:
            audit(db, a, 'sys', '修改预警参数', '', before, p)
    return ok(p)


# ================================================================ 初始化检查（ERP-905）
# 旧账号 role_id 可能为空：与 core._load_actor 一致，按 v1 粗角色映射默认角色
_ROLE_OF_USER = ("COALESCE(u.role_id, (SELECT r2.id FROM roles r2 WHERE r2.merchant_id=u.merchant_id AND r2.code = CASE u.role "
                 "WHEN 'admin' THEN 'merchant_admin' WHEN 'manager' THEN 'store_manager' WHEN 'cashier' THEN 'cashier' END))")
@bp.route('/init-check')
@require_perm('sys.init_check')
def init_check():
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    one = lambda sql, *p: db.execute(sql, p).fetchone()[0]          # noqa: E731
    stores = [r[0] for r in db.execute("SELECT code FROM stores WHERE merchant_id=? AND status='营业'", (mid,))]
    items = []

    def add(key, name, ok_, detail, link=''):
        items.append({'key': key, 'name': name, 'ok': bool(ok_), 'detail': detail, 'link': link})
    add('stores', '门店', stores, '营业门店 %d 家' % len(stores), 'org-stores')
    no_wh = [s for s in stores if not one('SELECT COUNT(*) FROM warehouses WHERE merchant_id=? AND code=?', mid, s)]
    add('warehouses', '门店仓库', not no_wh, '缺少门店仓：%s' % ','.join(no_wh) if no_wh else '每个门店都有门店仓')
    n_goods = one("SELECT COUNT(*) FROM products WHERE merchant_id=? AND status='在售'", mid)
    add('goods', '商品', n_goods, '在售商品 %d 个' % n_goods)
    no_price = one("SELECT COUNT(*) FROM products WHERE merchant_id=? AND status='在售' AND COALESCE(retail_cents,0) <= 0", mid)
    add('goods_price', '商品售价', not no_price, '%d 个在售商品没有售价' % no_price if no_price else '在售商品都有售价')
    no_bc = one("SELECT COUNT(*) FROM products p WHERE p.merchant_id=? AND p.status='在售' AND NOT EXISTS "
                "(SELECT 1 FROM product_barcodes b WHERE b.merchant_id=p.merchant_id AND b.product_id=p.id)", mid)
    add('goods_barcode', '商品条码', not no_bc, '%d 个在售商品没有条码（只能按编码/名称收银）' % no_bc if no_bc else '在售商品都有条码')
    no_cost = one("SELECT COUNT(*) FROM products WHERE merchant_id=? AND status='在售' AND COALESCE(cost_cents,0) <= 0", mid)
    add('goods_cost', '商品成本', not no_cost, '%d 个在售商品未维护成本（毛利报表会失真）' % no_cost if no_cost else '成本已维护')
    no_cashier = [s for s in stores if not one("SELECT COUNT(*) FROM users u JOIN roles r ON r.id=" + _ROLE_OF_USER + " WHERE u.merchant_id=? AND u.store_code=? "
                                                "AND u.status='启用' AND EXISTS (SELECT 1 FROM role_perms rp WHERE rp.role_id=r.id AND rp.perm='pos.checkout')",
                                                mid, s)]
    add('staff', '收银员账号', not no_cashier, '以下门店没有可收银的账号：%s' % ','.join(no_cashier) if no_cashier else '每个门店都有收银账号', 'org-users')
    no_mgr = [s for s in stores if not one("SELECT COUNT(*) FROM users u JOIN roles r ON r.id=" + _ROLE_OF_USER + " WHERE u.merchant_id=? AND u.store_code=? "
                                            "AND u.status='启用' AND EXISTS (SELECT 1 FROM role_perms rp WHERE rp.role_id=r.id AND rp.perm='pos.authorize')",
                                            mid, s)]
    add('authorizer', '店长授权人', not no_mgr, '以下门店没有能现场授权的账号：%s' % ','.join(no_mgr) if no_mgr else '每个门店都有授权人', 'org-users')
    empty = [s for s in stores if not one('SELECT COUNT(*) FROM inv_stock WHERE merchant_id=? AND wh_code=? AND qty != 0', mid, s)]
    add('stock', '期初库存', not empty, '以下门店没有任何库存：%s' % ','.join(empty) if empty else '各门店已有库存')
    cfg = get_config(db, mid)
    add('payment', '支付方式', True, '收银员确认到账模式：%s；聚合支付直连需商户号（未接入）' % '、'.join(n for _, n in PAY_METHODS))
    tpl = one("SELECT COUNT(*) FROM print_templates WHERE merchant_id=? AND kind='sale' AND status='启用'", mid)
    add('print', '小票模板', True, '已配置 %d 个销售小票模板' % tpl if tpl else '使用内置模板（抬头：%s，%d 毫米）' % (cfg.get('receipt_title'), cfg.get('receipt_width')),
        'sys-print')
    add('negative_stock', '负库存策略', not cfg.get('allow_negative_stock'), '当前允许负库存销售（规格书要求默认禁止）' if cfg.get('allow_negative_stock')
        else '负库存销售已禁止', 'sys-config')
    m = db.execute('SELECT * FROM merchants WHERE id=?', (mid,)).fetchone()
    add('merchant', '商户状态', m['status'] in ('trial', 'active'), '状态：%s%s' % (m['status'], ('，到期日 ' + m['expire_date']) if m['expire_date'] else ''))
    return ok({'items': items, 'passed': sum(1 for x in items if x['ok']), 'total': len(items), 'checked_at': now_s()})


# ================================================================ 导入模板下载中心（DATA-001）
@bp.route('/import-templates')
@api()
def import_templates():
    a = current_actor()
    out = []
    for k, (title, perm, cols, _s, endpoint) in xlsx_io.TEMPLATES.items():
        if has_perm(a, perm):
            out.append({'kind': k, 'title': title, 'download': 'api/sys/templates/' + k, 'import_api': endpoint,
                        'columns': [{'title': c.title, 'required': c.required, 'note': c.note} for c in cols]})
    return ok(out)


@bp.route('/templates/<kind>')
@api()
def template_download(kind):
    a = current_actor()
    t = xlsx_io.TEMPLATES.get(kind)
    if t is None:
        raise BizError('没有这个模板', 404)
    if not has_perm(a, t[1]):
        raise BizError('没有权限（需要：%s）' % t[1], 403)
    return xlsx_io.send_template(t[2], t[0] + '.xlsx', t[3])


# ================================================================ 门店导入（DATA-001/002：逐行错误、重复提示、部分成功清单）
@bp.route('/import/stores', methods=['POST'])
@require_perm('org.edit')
def import_stores():
    from .views_tenant import check_quota, after_store_created
    db = get_db()
    a = current_actor()
    mid = a['merchant_id']
    f = request.files.get('file')
    if f is None or not (f.filename or '').lower().endswith('.xlsx'):
        raise BizError('请上传 .xlsx 文件（可先下载门店导入模板）')
    rows, errors = xlsx_io.read_rows(f, xlsx_io.TEMPLATES['stores'][2], max_rows=2000)
    created = updated = 0
    seen = {}
    regions = dict((r[0], r[1]) for r in db.execute("SELECT code, id FROM org_units WHERE merchant_id=? AND kind='region'", (mid,)))
    for r in rows:
        d, n = r['data'], r['row_no']
        errs = []
        if not re.match(r'^[A-Za-z0-9_-]{2,12}$', d['code'] or ''):
            errs.append('门店编码为 2-12 位字母或数字')
        if d['code'] in seen:
            errs.append('与第 %d 行门店编码重复' % seen[d['code']])
        if d.get('region_code') and d['region_code'] not in regions:
            errs.append('区域不存在：%s' % d['region_code'])
        if errs:
            errors.append({'row_no': n, 'name': d.get('name') or '', 'errors': errs})
            continue
        seen[d['code']] = n
        try:
            with tx(db):
                old = db.execute('SELECT id FROM stores WHERE merchant_id=? AND code=?', (mid, d['code'])).fetchone()
                vals = (d['name'], d.get('address') or '', d.get('phone') or '', d.get('status') or '营业', regions.get(d.get('region_code')),
                        d.get('store_type') or '直营', d.get('manager_name') or '', d.get('open_hours') or '')
                if old is None:
                    check_quota(db, mid, 'stores')
                    db.execute('INSERT INTO stores(merchant_id, code, name, address, phone, status, region_id, store_type, manager_name, open_hours, created_at) '
                               'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (mid, d['code']) + vals + (now_s(),))
                    after_store_created(db, mid, d['code'], d['name'])
                    created += 1
                else:
                    db.execute('UPDATE stores SET name=?, address=?, phone=?, status=?, region_id=?, store_type=?, manager_name=?, open_hours=? '
                               'WHERE id=?', vals + (old[0],))
                    updated += 1
        except BizError as e:
            errors.append({'row_no': n, 'name': d.get('name') or '', 'errors': [e.msg]})
    res = xlsx_io.import_result(created, updated, errors)
    with tx(db):
        audit(db, a, 'org', '导入门店', '', None, {'created': created, 'updated': updated, 'errors': len(errors)}, level='high')
    return ok(res)
