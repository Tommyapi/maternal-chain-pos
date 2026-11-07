# -*- coding: utf-8 -*-
"""会员：档案/多宝宝/购买偏好/合并/导入导出/标签/黑名单/积分/储值。

Owner：模块②（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-501~507 ERP-601~602 ERP-605~606 POS-301~306
接口清单与入参出参：docs/API-CONTRACT.md §4.3；返回格式 {ok:true,data} / {ok:false,code,msg}；权限一律 @require_perm(...)。

数据范围：会员按「注册门店」归属；merchant 范围看全部，门店/区域范围看注册门店在范围内的会员（收银台识别会员走 member_svc.member_brief，
全商户通用，不受此限）。手机号按 member.phone.view 脱敏；储值余额按 member.stored.view；导出一律脱敏 + 安全审计。
"""
import json
from datetime import datetime

from flask import Blueprint, request

from . import approval, member_svc as ms, xlsx_io
from .core import get_db, tx, require_perm, current_actor, ok, audit, scope_sql, scope_stores, has_perm, mask_phone, check_limit_or_approval
from .money import to_cents, yuan, MoneyError
from .util import BizError, now_s

bp = Blueprint('member', __name__, url_prefix='/api/member')

EXPORT_MAX = 10000          # 单次导出上限（ERP-505 限制超范围批量获取）


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


def _scope_where(db, a, requested=None):
    """会员数据范围 SQL（按注册门店）。merchant 范围不限门店（含未登记门店的会员）。"""
    if a.get('data_scope') == 'merchant' and not requested:
        return ' AND m.merchant_id=?', [a['merchant_id']]
    return scope_sql(db, a, 'm.reg_store_code', requested, 'm.merchant_id')


def _member_in_scope(db, a, code):
    sql, args = _scope_where(db, a)
    m = db.execute('SELECT m.* FROM members m WHERE m.code=?' + sql, [code] + args).fetchone()
    if m is None:
        raise BizError('会员不存在', 404, 'NOT_FOUND')
    return m


def _list_query(db, a):
    sql, args = _scope_where(db, a, request.args.get('store'))
    where = ' WHERE 1=1' + sql
    kw = (request.args.get('kw') or '').strip()
    if kw:
        where += ' AND (m.code=? OR m.phone=? OR m.card_no=? OR m.name LIKE ? OR m.phone LIKE ?)'
        args += [kw, kw, kw, '%' + kw + '%', '%' + kw[-4:] if len(kw) >= 4 and kw.isdigit() else kw]
    if request.args.get('level'):
        where += ' AND m.level=?'
        args.append(request.args['level'])
    st = request.args.get('status')
    if st == 'blacklist':
        where += ' AND m.blacklisted=1'
    elif st == 'merged':
        where += " AND m.merged_into!=''"
    elif st:
        where += ' AND m.status=?'
        args.append(st)
    else:
        where += " AND m.merged_into=''"
    if request.args.get('tag'):
        where += ' AND m.id IN (SELECT member_id FROM member_tag_links l JOIN member_tags t ON t.id=l.tag_id WHERE t.merchant_id=? AND (t.name=? OR t.id=?))'
        args += [a['merchant_id'], request.args['tag'], request.args['tag']]
    return where, args


def _row_json(db, a, m):
    return {'code': m['code'], 'name': m['name'] or '', 'phone': mask_phone(m['phone'], a), 'level': m['level'] or '',
            'points': int(m['points'] or 0), 'stored_balance': yuan(m['balance_cents']) if has_perm(a, 'member.stored.view') else None,
            'status': m['status'] or '', 'blacklisted': int(m['blacklisted'] or 0), 'merged_into': m['merged_into'] or '',
            'reg_store_code': m['reg_store_code'] or '', 'reg_store_name': m['reg_store_name'] or '', 'reg_time': m['reg_time'] or '',
            'source': m['source'] or '', 'card_no': m['card_no'] or '',
            'tags': [r[0] for r in db.execute('SELECT t.name FROM member_tag_links l JOIN member_tags t ON t.id=l.tag_id WHERE l.member_id=? '
                                              'ORDER BY t.name', (m['id'],))]}


@bp.route('')
@require_perm('member.view')
def members_list():
    db, a = get_db(), current_actor()
    where, args = _list_query(db, a)
    page, size = _page()
    total = db.execute('SELECT COUNT(*) FROM members m' + where, args).fetchone()[0]
    rows = db.execute('SELECT m.* FROM members m' + where + ' ORDER BY m.id DESC LIMIT ? OFFSET ?', args + [size, (page - 1) * size]).fetchall()
    return ok({'page': page, 'size': size, 'total': total, 'items': [_row_json(db, a, r) for r in rows],
               'levels': ms.get_rules(db, a['merchant_id'])['member_levels']})


def preferences(db, mid, code, limit=8):
    """ERP-503：真实购买记录汇总（分类 → 品牌/规格，按购买件数），不采集额外敏感信息。"""
    rows = db.execute(
        "SELECT p.category, p.brand, p.spec, SUM(i.qty) q, COUNT(DISTINCT o.order_no) n, MAX(o.created_at) last FROM orders o "
        "JOIN order_items i ON i.order_no=o.order_no AND i.merchant_id=o.merchant_id JOIN products p ON p.id=i.product_id "
        "WHERE o.merchant_id=? AND o.member_code=? AND o.biz_type='sale' GROUP BY p.category, p.brand, p.spec "
        "ORDER BY q DESC, n DESC, p.category, p.brand LIMIT ?", (mid, code, limit)).fetchall()
    return [{'category': r['category'] or '', 'brand': r['brand'] or '', 'spec': r['spec'] or '', 'qty': r['q'], 'orders': r['n'],
             'last_time': r['last']} for r in rows]


@bp.route('/<code>')
@require_perm('member.view')
def member_get(code):
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    m = _member_in_scope(db, a, code)
    d = ms.member_card(db, a, m)
    d['phone_full'] = None
    d['babies'] = ms.babies_of(db, m['id'], include_hidden=True)
    d['note'] = m['note'] or ''
    d['disabled_reason'] = m['disabled_reason'] or ''
    d['tags'] = [dict(r) for r in db.execute('SELECT t.id, t.name, t.kind, l.source, l.created_by, l.created_at FROM member_tag_links l '
                                             'JOIN member_tags t ON t.id=l.tag_id WHERE l.member_id=? ORDER BY t.name', (m['id'],))]
    d['points_log'] = [dict(r) for r in db.execute('SELECT change, balance, type, order_no, orig_order_no, operator, note, store_code, expire_date, '
                                                   'approval_no, created_at FROM points_log WHERE merchant_id=? AND member_code=? ORDER BY id DESC LIMIT 50',
                                                   (mid, code))]
    if has_perm(a, 'member.stored.view'):
        d['stored_log'] = [dict(r, change=yuan(r['change_cents']), balance=yuan(r['balance_cents']), principal=yuan(r['principal_cents']),
                                gift=yuan(r['gift_cents'])) for r in db.execute(
            'SELECT change_cents, balance_cents, principal_cents, gift_cents, type, order_no, store_code, operator, note, approval_no, created_at '
            'FROM stored_log WHERE merchant_id=? AND member_code=? ORDER BY id DESC LIMIT 50', (mid, code))]
        d['stored_principal'] = yuan(m['stored_principal_cents'])
        d['stored_gift'] = yuan(m['stored_gift_cents'])
    d['all_coupons'] = [dict(r, face_value=yuan(r['face_value_cents'])) for r in db.execute(
        'SELECT coupon_no, template_name, face_value_cents, expire_date, status, order_no, used_at FROM coupons WHERE merchant_id=? AND member_code=? '
        'ORDER BY id DESC LIMIT 100', (mid, code))]
    d['recent_orders'] = [dict(r, payable=yuan(r['payable_cents'])) for r in db.execute(
        "SELECT order_no, biz_type, store_name, payable_cents, status, created_at FROM orders WHERE merchant_id=? AND member_code=? "
        "ORDER BY id DESC LIMIT 20", (mid, code))]
    d['preferences'] = preferences(db, mid, code)
    d['merges'] = [dict(r) for r in db.execute('SELECT from_code, to_code, operator, reason, created_at FROM member_merges WHERE merchant_id=? '
                                               'AND (from_code=? OR to_code=?) ORDER BY id', (mid, code, code))]
    return ok(d)


@bp.route('', methods=['POST'])
@require_perm('member.edit')
def member_save():
    """新增/修改会员档案。babies:[{id?, kind:'baby'|'pregnancy', nickname, gender, birthday, hidden, delete}]（删除=隐藏，不物理删）。"""
    db, a = get_db(), current_actor()
    d = _data()
    mid = a['merchant_id']
    code = (d.get('code') or '').strip()
    levels = ms.get_rules(db, mid)['member_levels']
    with tx(db):
        if not code:
            store = (d.get('reg_store_code') or a.get('store_code') or '').strip()
            if store:
                scope_stores(db, a, store)
            code = ms.quick_register(db, a, mid, store, dict(d, source=d.get('source') or 'admin'))
            m = db.execute('SELECT * FROM members WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
        else:
            m = _member_in_scope(db, a, code)
            before = {k: m[k] for k in ('name', 'level', 'note', 'card_no', 'phone')}
            phone = (d.get('phone') or '').strip()
            if not phone or '*' in phone:
                phone = m['phone']
            elif phone != m['phone']:
                if len(phone) != 11 or not phone.isdigit():
                    raise BizError('请输入 11 位手机号')
                if db.execute('SELECT 1 FROM members WHERE merchant_id=? AND phone=? AND code!=?', (mid, phone, code)).fetchone():
                    raise BizError('该手机号已被其他会员使用', 409, 'CONFLICT')
            level = (d.get('level') or m['level'] or levels[0]).strip()
            if level not in levels and level != m['level']:
                raise BizError('会员等级只能是：%s' % '、'.join(levels))
            card = (d.get('card_no') if d.get('card_no') is not None else m['card_no'] or '').strip()[:40]
            if card and db.execute('SELECT 1 FROM members WHERE merchant_id=? AND card_no=? AND code!=?', (mid, card, code)).fetchone():
                raise BizError('实体卡号已被其他会员使用', 409, 'CONFLICT')
            db.execute('UPDATE members SET phone=?, name=?, level=?, note=?, card_no=?, updated_at=? WHERE id=?',
                       (phone, (d.get('name') if d.get('name') is not None else m['name'] or '')[:20], level,
                        (d.get('note') if d.get('note') is not None else m['note'] or '')[:200], card, now_s(), m['id']))
            after = {'name': d.get('name'), 'level': level, 'card_no': card, 'phone_changed': phone != m['phone']}
            before['phone'] = mask_phone(before['phone'], {'perms': frozenset()})
            audit(db, a, 'member', '修改会员', code, before, after, store_code=m['reg_store_code'] or '')
            for b in d.get('babies') or []:
                if b.get('id'):
                    old = db.execute('SELECT * FROM babies WHERE id=? AND member_id=?', (b['id'], m['id'])).fetchone()
                    if old is None:
                        raise BizError('宝宝档案不存在', 404, 'NOT_FOUND')
                    hidden = 1 if (b.get('hidden') or b.get('delete')) else 0
                    bd = ms._valid_date(b.get('birthday') if b.get('birthday') is not None else old['birthday'], '宝宝生日/预产期')
                    db.execute('UPDATE babies SET kind=?, nickname=?, name=?, gender=?, birthday=?, hidden=?, note=? WHERE id=?',
                               (b.get('kind') or old['kind'] or 'baby', (b.get('nickname') if b.get('nickname') is not None else old['nickname'] or '')[:20],
                                (b.get('nickname') if b.get('nickname') is not None else old['name'] or '')[:20],
                                (b.get('gender') if b.get('gender') is not None else old['gender'] or '')[:4], bd, hidden,
                                (b.get('note') if b.get('note') is not None else old['note'] or '')[:100], old['id']))
                elif not b.get('delete'):
                    ms.add_baby(db, m['id'], mid, b)
    return member_get(code)


def _set_status(code, enable):
    db, a = get_db(), current_actor()
    d = _data()
    reason = (d.get('reason') or '').strip()
    if not reason:
        raise BizError('请填写原因')
    with tx(db):
        m = _member_in_scope(db, a, code)
        before = {'status': m['status'], 'blacklisted': int(m['blacklisted'] or 0)}
        if enable:
            db.execute("UPDATE members SET status='正常', blacklisted=0, disabled_reason='', updated_at=? WHERE id=?", (now_s(), m['id']))
        else:
            db.execute("UPDATE members SET status='停用', blacklisted=?, disabled_reason=?, updated_at=? WHERE id=?",
                       (1 if d.get('blacklist') else 0, reason[:200], now_s(), m['id']))
        after = {'status': '正常' if enable else '停用', 'blacklisted': 0 if enable else (1 if d.get('blacklist') else 0)}
        audit(db, a, 'member', '启用会员' if enable else ('拉黑会员' if d.get('blacklist') else '停用会员'), code, before, after, reason,
              level='high', store_code=m['reg_store_code'] or '')
    return member_get(code)


@bp.route('/<code>/disable', methods=['POST'])
@require_perm('member.blacklist')
def member_disable(code):
    return _set_status(code, False)


@bp.route('/<code>/enable', methods=['POST'])
@require_perm('member.blacklist')
def member_enable(code):
    return _set_status(code, True)


# ================================================================ 合并（ERP-504）
def _merge_summary(db, a, m):
    mid = a['merchant_id']
    c = ms.member_card(db, a, m)
    c['coupon_count'] = db.execute("SELECT COUNT(*) FROM coupons WHERE merchant_id=? AND member_code=? AND status='未使用'",
                                   (mid, m['code'])).fetchone()[0]
    c['stored_balance_cents'] = int(m['balance_cents'] or 0)
    c['babies'] = ms.babies_of(db, m['id'], include_hidden=True)
    return c


def _merge_pair(db, a, d):
    f, t = (d.get('from_code') or '').strip(), (d.get('to_code') or '').strip()
    if not f or not t or f == t:
        raise BizError('请选择两个不同的会员（被合并 from_code → 保留 to_code）')
    mf, mt = _member_in_scope(db, a, f), _member_in_scope(db, a, t)
    if mf['merged_into'] or mt['merged_into']:
        raise BizError('已合并过的会员不能再合并', 409, 'CONFLICT')
    if db.execute("SELECT 1 FROM coupons WHERE merchant_id=? AND member_code=? AND status='已锁定'", (a['merchant_id'], f)).fetchone():
        raise BizError('被合并会员有挂单占用的券，请先处理挂单', 409, 'CONFLICT')
    return mf, mt


@bp.route('/merge/preview', methods=['POST'])
@require_perm('member.merge')
def merge_preview():
    db, a = get_db(), current_actor()
    mf, mt = _merge_pair(db, a, _data())
    fs, ts = _merge_summary(db, a, mf), _merge_summary(db, a, mt)
    after = {'points': fs['points'] + ts['points'], 'coupon_count': fs['coupon_count'] + ts['coupon_count'],
             'babies': len(fs['babies']) + len(ts['babies']), 'orders': fs['orders'] + ts['orders'],
             'stored_balance': yuan(fs['stored_balance_cents'] + ts['stored_balance_cents']) if has_perm(a, 'member.stored.view') else None}
    for s in (fs, ts):
        s.pop('stored_balance_cents', None)
    return ok({'from': fs, 'to': ts, 'after': after, 'note': '被合并会员的历史订单保留原会员号；积分、储值、未使用券、宝宝档案、标签转入保留会员；'
                                                             '被合并会员标记为已合并，不可再使用'})


@bp.route('/merge', methods=['POST'])
@require_perm('member.merge')
def merge_do():
    from .promo_engine import coupon_log
    db, a = get_db(), current_actor()
    d = _data()
    reason = (d.get('reason') or '').strip()
    if not reason:
        raise BizError('请填写合并原因')
    mid = a['merchant_id']
    with tx(db):
        mf, mt = _merge_pair(db, a, d)
        snap = {'from': dict(mf), 'to': dict(mt),
                'coupons': [r[0] for r in db.execute("SELECT coupon_no FROM coupons WHERE merchant_id=? AND member_code=? AND status='未使用'",
                                                     (mid, mf['code']))],
                'babies': [dict(r) for r in db.execute('SELECT * FROM babies WHERE member_id=?', (mf['id'],))],
                'tags': [r[0] for r in db.execute('SELECT tag_id FROM member_tag_links WHERE member_id=?', (mf['id'],))]}
        snap['from']['phone'] = mask_phone(mf['phone'], {'perms': frozenset()})
        snap['to']['phone'] = mask_phone(mt['phone'], {'perms': frozenset()})
        ref = 'MERGE:%s>%s' % (mf['code'], mt['code'])
        pts = int(mf['points'] or 0)
        if pts:
            ms.points_change(db, a, mid, mf['code'], -pts, '合并转出', ref, note='转入 ' + mt['code'], allow_negative=True)
            ms.points_change(db, a, mid, mt['code'], pts, '合并转入', ref, note='来自 ' + mf['code'], allow_negative=True)
        debt = int(mf['points_debt'] or 0)
        if debt:
            db.execute('UPDATE members SET points_debt = points_debt + ? WHERE id=?', (debt, mt['id']))
            db.execute('UPDATE members SET points_debt = 0 WHERE id=?', (mf['id'],))
        bal = int(mf['balance_cents'] or 0)
        if bal:
            pf, gf = int(mf['stored_principal_cents'] or 0), int(mf['stored_gift_cents'] or 0)
            if pf + gf != bal:
                pf, gf = bal - gf if bal - gf >= 0 else bal, 0 if bal - gf < 0 else gf
            ms.stored_change(db, a, mid, mf['code'], -bal, '合并转出', ref, note='转入 ' + mt['code'], principal_cents=-pf, gift_cents=-gf)
            ms.stored_change(db, a, mid, mt['code'], bal, '合并转入', ref, note='来自 ' + mf['code'], principal_cents=pf, gift_cents=gf)
        for cno in snap['coupons']:
            db.execute("UPDATE coupons SET member_code=? WHERE merchant_id=? AND coupon_no=? AND status='未使用'", (mt['code'], mid, cno))
            coupon_log(db, mid, cno, '未使用', '未使用', ref, a['name'], '会员合并：%s → %s' % (mf['code'], mt['code']))
        db.execute('UPDATE babies SET member_id=? WHERE member_id=?', (mt['id'], mf['id']))
        for tid in snap['tags']:
            db.execute("INSERT OR IGNORE INTO member_tag_links(merchant_id, member_id, tag_id, source, created_by, created_at) VALUES(?,?,?,?,?,?)",
                       (mid, mt['id'], tid, 'merge', a['username'], now_s()))
        db.execute('DELETE FROM member_tag_links WHERE member_id=?', (mf['id'],))
        # 被合并会员：释放手机号/卡号唯一约束（原值留在快照里），标记合并去向
        db.execute("UPDATE members SET merged_into=?, status='已合并', phone=NULL, card_no='', note=?, updated_at=? WHERE id=?",
                   (mt['code'], ('原手机号 %s；%s' % (mask_phone(mf['phone'], {'perms': frozenset()}), mf['note'] or ''))[:200], now_s(), mf['id']))
        db.execute('INSERT INTO member_merges(merchant_id, from_code, to_code, snapshot, operator, reason, created_at) VALUES(?,?,?,?,?,?,?)',
                   (mid, mf['code'], mt['code'], json.dumps(snap, ensure_ascii=False, default=str), a['name'], reason[:200], now_s()))
        audit(db, a, 'member', '合并会员', mt['code'], {'from': mf['code'], 'points': pts, 'stored_cents': bal},
              {'to': mt['code'], 'coupons': len(snap['coupons'])}, reason, level='high', store_code=mt['reg_store_code'] or '')
    return member_get(mt['code'])


# ================================================================ 标签（ERP-506）
@bp.route('/tags')
@require_perm('member.view')
def tags_list():
    db, a = get_db(), current_actor()
    rows = db.execute('SELECT t.*, (SELECT COUNT(*) FROM member_tag_links l WHERE l.tag_id=t.id) cnt FROM member_tags t WHERE t.merchant_id=? '
                      'ORDER BY t.kind, t.name', (a['merchant_id'],)).fetchall()
    return ok([dict(r, rule=json.loads(r['rule'] or '{}')) for r in rows])


@bp.route('/tags', methods=['POST'])
@require_perm('member.tag')
def tag_save():
    """{id?, name, kind:'manual'|'auto', rule?:{days, min_amount, min_orders, category, brand}, status}。规则只基于消费行为，不做医疗/营养判断。"""
    db, a = get_db(), current_actor()
    d = _data()
    name = (d.get('name') or '').strip()[:20]
    if not name:
        raise BizError('请填写标签名称')
    kind = d.get('kind') or 'manual'
    if kind not in ('manual', 'auto'):
        raise BizError('标签类型只能是 manual/auto')
    rule = {}
    if kind == 'auto':
        r = d.get('rule') or {}
        try:
            rule = {'days': int(r.get('days') or 90), 'min_orders': int(r.get('min_orders') or 0),
                    'min_amount_cents': to_cents(r.get('min_amount') or 0), 'category': (r.get('category') or '')[:20], 'brand': (r.get('brand') or '')[:20]}
        except (ValueError, TypeError, MoneyError):
            raise BizError('自动标签规则格式不正确')
    with tx(db):
        if d.get('id'):
            c = db.execute('UPDATE member_tags SET name=?, kind=?, rule=?, status=? WHERE merchant_id=? AND id=?',
                           (name, kind, json.dumps(rule, ensure_ascii=False), d.get('status') or '启用', a['merchant_id'], d['id']))
            if c.rowcount != 1:
                raise BizError('标签不存在', 404, 'NOT_FOUND')
            tid = int(d['id'])
        else:
            if db.execute('SELECT 1 FROM member_tags WHERE merchant_id=? AND name=?', (a['merchant_id'], name)).fetchone():
                raise BizError('标签已存在', 409, 'CONFLICT')
            db.execute('INSERT INTO member_tags(merchant_id, name, kind, rule, status) VALUES(?,?,?,?,?)',
                       (a['merchant_id'], name, kind, json.dumps(rule, ensure_ascii=False), d.get('status') or '启用'))
            tid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        audit(db, a, 'member', '维护标签', str(tid), None, {'name': name, 'kind': kind, 'rule': rule}, store_code='')
    return ok(dict(db.execute('SELECT * FROM member_tags WHERE id=?', (tid,)).fetchone()))


@bp.route('/<code>/tags', methods=['POST'])
@require_perm('member.tag')
def member_tags(code):
    """{add:[tag_id], remove:[tag_id]}；手工打标记录来源和操作人。"""
    db, a = get_db(), current_actor()
    d = _data()
    with tx(db):
        m = _member_in_scope(db, a, code)
        for tid in d.get('add') or []:
            if not db.execute('SELECT 1 FROM member_tags WHERE merchant_id=? AND id=?', (a['merchant_id'], tid)).fetchone():
                raise BizError('标签不存在', 404, 'NOT_FOUND')
            db.execute("INSERT OR IGNORE INTO member_tag_links(merchant_id, member_id, tag_id, source, created_by, created_at) VALUES(?,?,?,'manual',?,?)",
                       (a['merchant_id'], m['id'], tid, a['username'], now_s()))
        for tid in d.get('remove') or []:
            db.execute('DELETE FROM member_tag_links WHERE member_id=? AND tag_id=?', (m['id'], tid))
        audit(db, a, 'member', '会员打标签', code, None, {'add': d.get('add') or [], 'remove': d.get('remove') or []}, store_code=m['reg_store_code'] or '')
    return member_get(code)


@bp.route('/tags/<int:tid>/refresh', methods=['POST'])
@require_perm('member.tag')
def tag_refresh(tid):
    """按消费行为重算自动标签（只动 source='auto' 的关联，手工打的不动）。"""
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    t = db.execute('SELECT * FROM member_tags WHERE merchant_id=? AND id=?', (mid, tid)).fetchone()
    if t is None:
        raise BizError('标签不存在', 404, 'NOT_FOUND')
    if t['kind'] != 'auto':
        raise BizError('手工标签不能自动计算')
    r = json.loads(t['rule'] or '{}')
    since = datetime.fromtimestamp(datetime.now().timestamp() - int(r.get('days') or 90) * 86400).strftime('%Y-%m-%d %H:%M:%S')
    sql = ("SELECT o.member_code, COUNT(DISTINCT o.order_no) n, COALESCE(SUM(i.amount_cents),0) amt FROM orders o JOIN order_items i "
           "ON i.order_no=o.order_no AND i.merchant_id=o.merchant_id JOIN products p ON p.id=i.product_id "
           "WHERE o.merchant_id=? AND o.biz_type='sale' AND o.member_code!='' AND o.created_at>=?")
    args = [mid, since]
    if r.get('category'):
        sql += ' AND p.category=?'
        args.append(r['category'])
    if r.get('brand'):
        sql += ' AND p.brand=?'
        args.append(r['brand'])
    sql += ' GROUP BY o.member_code HAVING n>=? AND amt>=?'
    args += [int(r.get('min_orders') or 0), int(r.get('min_amount_cents') or 0)]
    with tx(db):
        hits = [x['member_code'] for x in db.execute(sql, args)]
        db.execute("DELETE FROM member_tag_links WHERE tag_id=? AND source='auto'", (tid,))
        for c in hits:
            db.execute("INSERT OR IGNORE INTO member_tag_links(merchant_id, member_id, tag_id, source, created_by, created_at) "
                       "SELECT ?, id, ?, 'auto', 'rule', ? FROM members WHERE merchant_id=? AND code=? AND merged_into=''",
                       (mid, tid, now_s(), mid, c))
        audit(db, a, 'member', '刷新自动标签', str(tid), None, {'hits': len(hits)}, store_code='')
    return ok({'tag_id': tid, 'members': len(hits)})


# ================================================================ 积分 / 储值
@bp.route('/<code>/points', methods=['POST'])
@require_perm('member.points.adjust')
def points_adjust(code):
    """{delta, reason, auth_user?/auth_pass?/approval_no?}：超 points_adjust_max -> NEED_AUTH（店长授权或 POST /api/approvals kind=points_adjust）。"""
    db, a = get_db(), current_actor()
    d = _data()
    try:
        delta = int(d.get('delta'))
    except (TypeError, ValueError):
        raise BizError('调整积分必须是整数')
    reason = (d.get('reason') or '').strip()
    if not delta or not reason:
        raise BizError('请填写调整积分和原因')
    with tx(db):
        m = _member_in_scope(db, a, code)
        store = m['reg_store_code'] or a.get('store_code') or ''
        r = check_limit_or_approval(db, a, 'points_adjust', abs(delta), d, store, code, '积分调整 %+d' % delta)
        bal = ms.points_change(db, a, a['merchant_id'], code, delta, '人工调整', '', store, reason, r.get('approval_no', ''))
        audit(db, a, 'member', '积分调整', code, {'points': int(m['points'] or 0)}, {'points': bal, 'delta': delta, 'by': r['by'], 'mode': r['mode']},
              reason, level='high', store_code=store)
    return ok({'points': bal, 'auth': r})


@bp.route('/<code>/stored', methods=['POST'])
@require_perm('member.stored.adjust')
def stored_adjust(code):
    """储值调整/退款，必须审批（ERP-606）：第一次提交（无 approval_no）生成审批单 kind=stored_adjust；审批通过后带 approval_no 重提执行。
    {delta(元，正加负减), kind:'人工调整'|'储值退款', reason, request_key?, approval_no?, principal?, gift?}"""
    db, a = get_db(), current_actor()
    d = _data()
    try:
        delta = to_cents(d.get('delta'))
    except MoneyError as e:
        raise BizError('调整金额：%s' % e)
    kind = d.get('kind') or '人工调整'
    if kind not in ('人工调整', '储值退款'):
        raise BizError('类型只能是 人工调整/储值退款')
    if kind == '储值退款' and delta >= 0:
        raise BizError('储值退款金额应为负数')
    reason = (d.get('reason') or '').strip()
    if not delta or not reason:
        raise BizError('请填写调整金额和原因')
    with tx(db):
        m = _member_in_scope(db, a, code)
        store = m['reg_store_code'] or ''
        ano = (d.get('approval_no') or '').strip()
        if not ano:
            ap = approval.create(db, a, 'stored_adjust', store, '储值%s %s 元：%s' % (kind, '%.2f' % (delta / 100.0), code), value=delta,
                                 ref_no=code, ref_type='member', payload={'member_code': code, 'kind': kind, 'delta_cents': delta},
                                 reason=reason, need_perm='approval.member', request_key=(d.get('request_key') or None), amount_cents=abs(delta))
            return ok({'status': 'pending', 'approval_no': ap['approval_no'], 'msg': '已提交审批，审批通过后带 approval_no 重新提交'})
        ap = approval.consume(db, a, ano, 'stored_adjust', store, delta)
        if ap['payload'].get('member_code') != code or int(ap['payload'].get('delta_cents') or 0) != delta:
            raise BizError('审批单与本次调整不一致', 403, 'APPROVAL_MISMATCH')
        pc = to_cents(d['principal']) if d.get('principal') not in (None, '') else None
        gc = to_cents(d['gift']) if d.get('gift') not in (None, '') else None
        bal = ms.stored_change(db, a, a['merchant_id'], code, delta, kind, ano, store, reason, principal_cents=pc, gift_cents=gc, approval_no=ano)
        audit(db, a, 'member', '储值' + kind, code, {'balance_cents': int(m['balance_cents'] or 0)}, {'balance_cents': bal, 'approval_no': ano},
              reason, level='high', store_code=store)
    return ok({'status': 'done', 'stored_balance': yuan(bal), 'approval_no': ano})


@bp.route('/stored/daily')
@require_perm('member.stored.view')
def stored_daily():
    """储值日报（ERP-606）：按日期 × 类型汇总充值本金/赠送/消费/退款/调整 + 期末余额 + 异常检查。参数 start end store。"""
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    start = (request.args.get('start') or now_s()[:10])[:10]
    end = (request.args.get('end') or start)[:10]
    sql, args = 'SELECT substr(created_at,1,10) d, type, COUNT(*) n, SUM(change_cents) amt, SUM(COALESCE(principal_cents,0)) pr, ' \
                'SUM(COALESCE(gift_cents,0)) gf FROM stored_log WHERE merchant_id=? AND substr(created_at,1,10) BETWEEN ? AND ?', [mid, start, end]
    if request.args.get('store'):
        scope_stores(db, a, request.args['store'])
        sql += ' AND store_code=?'
        args.append(request.args['store'])
    elif a.get('data_scope') != 'merchant':
        stores = scope_stores(db, a)
        sql += ' AND store_code IN (%s)' % ','.join('?' * len(stores)) if stores else ' AND 1=0'
        args += stores
    days = {}
    for r in db.execute(sql + ' GROUP BY d, type ORDER BY d, type', args):
        day = days.setdefault(r['d'], {'date': r['d'], 'items': [], 'net': 0})
        day['items'].append({'type': r['type'], 'count': r['n'], 'amount': yuan(r['amt']), 'principal': yuan(r['pr']), 'gift': yuan(r['gf'])})
        day['net'] += int(r['amt'] or 0)
    for day in days.values():
        day['closing_balance'] = yuan(db.execute('SELECT COALESCE(SUM(change_cents),0) FROM stored_log WHERE merchant_id=? AND substr(created_at,1,10)<=?',
                                                 (mid, day['date'])).fetchone()[0])
        day['net'] = yuan(day['net'])
    total = db.execute('SELECT COALESCE(SUM(balance_cents),0), COALESCE(SUM(stored_principal_cents),0), COALESCE(SUM(stored_gift_cents),0) '
                       "FROM members WHERE merchant_id=?", (mid,)).fetchone()
    anomalies = [dict(code=r[0], balance=yuan(r[1]), principal=yuan(r[2]), gift=yuan(r[3]), log_sum=yuan(r[4])) for r in db.execute(
        'SELECT m.code, m.balance_cents, m.stored_principal_cents, m.stored_gift_cents, '
        '(SELECT COALESCE(SUM(change_cents),0) FROM stored_log s WHERE s.merchant_id=m.merchant_id AND s.member_code=m.code) ls '
        'FROM members m WHERE m.merchant_id=? AND (COALESCE(m.balance_cents,0) != m.stored_principal_cents + m.stored_gift_cents '
        'OR COALESCE(m.balance_cents,0) < 0) LIMIT 200', (mid,))]
    return ok({'start': start, 'end': end, 'days': [days[k] for k in sorted(days)],
               'balance': {'total': yuan(total[0]), 'principal': yuan(total[1]), 'gift': yuan(total[2])}, 'anomalies': anomalies,
               'caliber': '充值=本金+赠送；消费按本金:赠送余额比例分摊；期末余额=截至当日储值流水合计（v1 迁移前直接改余额的差额见异常检查）'})


@bp.route('/points-exceptions')
@require_perm('member.points.adjust', 'member.view')
def points_exceptions():
    db, a = get_db(), current_actor()
    st = request.args.get('status') or 'open'
    rows = db.execute('SELECT * FROM points_exceptions WHERE merchant_id=? AND status=? ORDER BY id DESC LIMIT 500', (a['merchant_id'], st)).fetchall()
    return ok([dict(r) for r in rows])


@bp.route('/points-exceptions/<int:eid>/resolve', methods=['POST'])
@require_perm('member.points.adjust')
def points_exception_resolve(eid):
    """{action:'deduct'(从当前积分扣，可形成负积分)|'waive'(豁免), reason}，必须留原因与流水。"""
    db, a = get_db(), current_actor()
    d = _data()
    act, reason = d.get('action'), (d.get('reason') or '').strip()
    if act not in ('deduct', 'waive') or not reason:
        raise BizError('请选择处理方式（deduct/waive）并填写原因')
    with tx(db):
        e = db.execute("SELECT * FROM points_exceptions WHERE merchant_id=? AND id=? AND status='open'", (a['merchant_id'], eid)).fetchone()
        if e is None:
            raise BizError('异常记录不存在或已处理', 409, 'CONFLICT')
        m = _member_in_scope(db, a, e['member_code'])
        dec = min(int(m['points_debt'] or 0), int(e['shortfall']))
        if act == 'deduct':
            r = check_limit_or_approval(db, a, 'points_adjust', dec, d, m['reg_store_code'] or '', e['ref_no'], '退货欠扣积分扣除')
            ms.points_change(db, a, a['merchant_id'], e['member_code'], -dec, '退货扣回', e['ref_no'], note='人工处理欠扣：' + reason,
                             allow_negative=True, orig_order_no=e['orig_order_no'], approval_no=r.get('approval_no', ''))
        else:
            db.execute("INSERT INTO points_log(merchant_id, member_code, member_name, change, balance, type, order_no, operator, note, orig_order_no, "
                       "created_at) VALUES(?,?,?,0,?,'欠扣豁免',?,?,?,?,?)", (a['merchant_id'], m['code'], m['name'], int(m['points'] or 0),
                                                                          e['ref_no'], a['name'], '豁免 %d：%s' % (dec, reason), e['orig_order_no'], now_s()))
        db.execute('UPDATE members SET points_debt = MAX(points_debt - ?, 0) WHERE id=?', (dec, m['id']))
        db.execute("UPDATE points_exceptions SET status='resolved', resolved_by=?, resolved_at=?, note=? WHERE id=?",
                   (a['name'], now_s(), (act + '：' + reason)[:200], eid))
        audit(db, a, 'member', '处理积分欠扣', e['ref_no'], {'debt': int(m['points_debt'] or 0)}, {'action': act, 'points': dec}, reason, level='high',
              store_code=m['reg_store_code'] or '')
    return ok({'id': eid, 'action': act, 'points': dec})


@bp.route('/points/expire', methods=['POST'])
@require_perm('member.points.adjust')
def points_expire():
    db, a = get_db(), current_actor()
    with tx(db):
        out = ms.expire_points(db, a, a['merchant_id'])
        audit(db, a, 'member', '积分过期处理', '', None, {'members': len(out), 'points': sum(out.values())}, store_code='')
    return ok({'members': len(out), 'points': sum(out.values())})


@bp.route('/rules')
@require_perm('member.view', 'promo.view')
def rules_get():
    from .core import get_config
    db, a = get_db(), current_actor()
    cfg = get_config(db, a['merchant_id'])
    r = ms.get_rules(db, a['merchant_id'])
    r['base'] = {k: cfg.get(k) for k in ('points_per_yuan', 'points_to_yuan', 'points_max_ratio', 'round_mode')}
    return ok(r)


@bp.route('/rules', methods=['POST'])
@require_perm('sys.config')
def rules_save():
    db, a = get_db(), current_actor()
    with tx(db):
        r = ms.save_rules(db, a, a['merchant_id'], _data())
    return ok(r)


# ================================================================ 导入导出（ERP-505 / DATA-001）
IMPORT_COLS = [
    xlsx_io.Col('phone', '手机号', required=True, max_len=11),
    xlsx_io.Col('name', '姓名', max_len=20),
    xlsx_io.Col('code', '会员编号', max_len=30, note='空=自动生成'),
    xlsx_io.Col('level', '会员等级', max_len=20),
    xlsx_io.Col('reg_store_code', '注册门店编码', max_len=20),
    xlsx_io.Col('card_no', '实体卡号', max_len=40),
    xlsx_io.Col('baby_birthday', '宝宝生日', kind='date'),
    xlsx_io.Col('due_date', '预产期', kind='date'),
    xlsx_io.Col('points', '期初积分', kind='int'),
    xlsx_io.Col('stored', '期初储值', kind='money', note='元；记为本金，写储值流水「期初导入」'),
    xlsx_io.Col('note', '备注', max_len=200),
]


@bp.route('/template')
@require_perm('member.import')
def import_template():
    return xlsx_io.send_template(IMPORT_COLS, '会员导入模板.xlsx',
                                 sample=[{'phone': '13800000000', 'name': '王妈妈', 'level': '普通会员', 'reg_store_code': '', 'baby_birthday': '2025-06-01'}])


@bp.route('/import', methods=['POST'])
@require_perm('member.import')
def import_members():
    """导入：手机号格式/文件内重复/库内重复（库内已存在 -> 跳过并报错，不覆盖）；期初积分/储值写流水。"""
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    rows, errors = xlsx_io.read_rows(request.files.get('file'), IMPORT_COLS)
    levels = ms.get_rules(db, mid)['member_levels']
    stores = set(scope_stores(db, a))
    seen, created = {}, 0
    with tx(db):
        for r in rows:
            d, errs = r['data'], []
            ph = str(d.get('phone') or '').strip()
            if len(ph) != 11 or not ph.isdigit():
                errs.append('手机号必须是 11 位数字')
            elif ph in seen:
                errs.append('与第 %d 行手机号重复' % seen[ph])
            elif db.execute('SELECT 1 FROM members WHERE merchant_id=? AND phone=?', (mid, ph)).fetchone():
                errs.append('手机号已是会员')
            if d.get('code') and db.execute('SELECT 1 FROM members WHERE merchant_id=? AND code=?', (mid, d['code'])).fetchone():
                errs.append('会员编号已存在')
            if d.get('level') and d['level'] not in levels:
                errs.append('会员等级只能是：%s' % '、'.join(levels))
            if d.get('reg_store_code') and d['reg_store_code'] not in stores:
                errs.append('注册门店不存在或不在你的范围内')
            if d.get('points') is not None and d['points'] < 0:
                errs.append('期初积分不能为负')
            if d.get('stored') is not None and d['stored'] < 0:
                errs.append('期初储值不能为负')
            if errs:
                errors.append({'row_no': r['row_no'], 'name': d.get('name') or ph, 'errors': errs})
                continue
            seen[ph] = r['row_no']
            code = ms.quick_register(db, a, mid, d.get('reg_store_code') or '', dict(d, phone=ph, source='import'))
            if d.get('code') and d['code'] != code:
                db.execute('UPDATE members SET code=? WHERE merchant_id=? AND code=?', (d['code'], mid, code))
                code = d['code']
            if d.get('points'):
                ms.points_change(db, a, mid, code, int(d['points']), '期初导入', 'IMPORT', note='会员导入')
            if d.get('stored'):
                ms.stored_change(db, a, mid, code, int(d['stored']), '期初导入', 'IMPORT', note='会员导入', principal_cents=int(d['stored']), gift_cents=0)
            created += 1
        audit(db, a, 'member', '导入会员', '', None, {'created': created, 'errors': len(errors)}, level='high', store_code='')
    return ok(xlsx_io.import_result(created, 0, errors))


@bp.route('/export')
@require_perm('member.export')
def export_members():
    """导出：独立权限 member.export、按数据范围、手机号一律脱敏、单次上限 EXPORT_MAX 行、安全审计。"""
    db, a = get_db(), current_actor()
    where, args = _list_query(db, a)
    total = db.execute('SELECT COUNT(*) FROM members m' + where, args).fetchone()[0]
    if total > EXPORT_MAX:
        raise BizError('一次最多导出 %d 个会员（当前 %d），请按门店/等级/标签缩小范围' % (EXPORT_MAX, total))
    stored_ok = has_perm(a, 'member.stored.view')
    rows = []
    nobody = {'perms': frozenset()}
    for m in db.execute('SELECT m.* FROM members m' + where + ' ORDER BY m.id', args):
        rows.append([m['code'], m['name'] or '', mask_phone(m['phone'], nobody), m['level'] or '', int(m['points'] or 0),
                     (yuan(m['balance_cents']) if stored_ok else ''), m['reg_store_code'] or '', m['reg_store_name'] or '', m['reg_time'] or '',
                     m['status'] or '', '是' if int(m['blacklisted'] or 0) else ''])
    audit(db, a, 'member', '导出会员', '', None, {'rows': len(rows), 'filters': dict(request.args)}, '会员导出', level='security', store_code='')
    db.commit()
    return xlsx_io.send_xlsx(['会员编号', '姓名', '手机号(脱敏)', '等级', '积分', '储值余额', '注册门店编码', '注册门店', '注册时间', '状态', '黑名单'],
                             rows, '会员导出.xlsx', '会员')
