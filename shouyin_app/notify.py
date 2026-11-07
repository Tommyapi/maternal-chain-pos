# -*- coding: utf-8 -*-
"""消息 / 待办。Owner：模块⑤（手机端）。任何模块都可以调用写入函数；读取/处理接口在 /api/notify。

写入（调用方在自己的 `with tx(db):` 里调用）：
    notify_users(db, mid, [user_id..], category, title, body, ref_type, ref_no, link, level, is_todo, store_code, dedup_key)
        点对点发送（如：审批结果通知申请人）。不受提醒规则过滤。
    notify_perm(db, mid, perm, store_code, category, title, ...)
        广播给「有该权限且门店在其数据范围内」的在职账号，再按提醒规则 notify_rules 过滤（APP-302）。
    risk_alert(db, mid, store_code, title, body='', ref_type='', ref_no='', perm='approval.pos', dedup_key=None)
        高风险事件告警（APP-304）：大额改价/折扣、频繁退款、负库存、支付异常……发给授权管理者，级别 urgent。
    refund_burst(db, mid, store_code, cashier_user, window_minutes=60, threshold=3)
        频繁退款检测：同一收银员窗口内退货单数 >= 阈值时发 risk_alert（同小时去重）。③在退货事务里调用。
    done_by_ref(db, mid, ref_type, ref_no)
        单据处理完：关闭相关待办（APP-303 闭环）。
dedup_key：同一接收人同一 key 只保留一条（如 'near_expiry:8007:2026-11-05'），防止定时任务刷屏。

隐私（APP-403）：标题/正文里的 11 位手机号一律自动脱敏为 138****0001；通知预览不放金额以外的敏感字段。
"""
import re

from flask import Blueprint, request

from .core import get_db, api, current_actor, ok, tx, _load_actor, scope_stores
from .util import BizError, now_s, inum

CATEGORIES = {
    'stock_low': '缺货预警', 'near_expiry': '临期预警', 'expired': '过期预警', 'transfer': '调拨', 'purchase': '采购',
    'approval': '审批', 'pay_error': '支付异常', 'recon_diff': '对账差异', 'risk': '高风险操作', 'notice': '公告', 'system': '系统',
}
LEVELS = ('normal', 'important', 'urgent')
LEVEL_NAME = {'normal': '普通', 'important': '重要', 'urgent': '紧急'}

_PHONE_RE = re.compile(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)')


def scrub(text):
    """通知预览脱敏：11 位手机号 -> 138****0001。"""
    return _PHONE_RE.sub(r'\1****\2', text or '')


def _level(level):
    return level if level in LEVELS else 'normal'


def notify_users(db, merchant_id, user_ids, category, title, body='', ref_type='', ref_no='', link='', level='normal',
                 is_todo=False, store_code='', dedup_key=None):
    """点对点写消息；只发给同商户的账号（防止业务方误传别家 user_id）。返回实际写入条数。"""
    ids = sorted(set(int(u) for u in user_ids if u))
    if not ids:
        return 0
    valid = set(r[0] for r in db.execute('SELECT id FROM users WHERE merchant_id=? AND id IN (%s)' % ','.join('?' * len(ids)),
                                         [merchant_id] + ids))
    n = 0
    for uid in ids:
        if uid not in valid:
            continue
        cur = db.execute('INSERT OR IGNORE INTO notifications(merchant_id, user_id, store_code, category, level, title, body, ref_type, ref_no, '
                         'link, is_todo, status, dedup_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (merchant_id, uid, store_code or '', category, _level(level), scrub(title)[:100], scrub(body)[:500],
                          ref_type or '', ref_no or '', link or '', 1 if is_todo else 0, 'unread', dedup_key, now_s()))
        n += cur.rowcount
    return n


def users_with_perm(db, merchant_id, perm, store_code=''):
    """有权限 perm 且（store_code 为空时：商户级范围；否则门店在其数据范围内）的在职账号 actor 列表。"""
    out = []
    for u in db.execute("SELECT * FROM users WHERE merchant_id=? AND status='启用'", (merchant_id,)):
        a = _load_actor(db, u)
        if perm not in a['perms']:
            continue
        if store_code:
            if store_code not in scope_stores(db, a):
                continue
        elif a['data_scope'] != 'merchant':
            continue
        out.append(a)
    return out


def rules_allow(db, merchant_id, category, actor, store_code, level):
    """提醒规则（APP-302）。某类别没有任何规则 = 默认全部有权人员接收；
    有规则时，接收人必须命中一条启用的规则（角色空=任意角色，门店空=任意门店，且消息级别 >= min_level）；
    命中停用规则（enabled=0）的直接排除（用于「某角色不收某类消息」）。"""
    rules = db.execute('SELECT * FROM notify_rules WHERE merchant_id=? AND category=?', (merchant_id, category)).fetchall()
    if not rules:
        return True
    lv = LEVELS.index(_level(level))
    hit, has_enabled = False, False
    for r in rules:
        has_enabled = has_enabled or bool(r['enabled'])
        if r['role_code'] and r['role_code'] != actor['role_code']:
            continue
        if r['store_code'] and r['store_code'] != (store_code or ''):
            continue
        if not r['enabled']:
            return False                  # 命中停用规则：明确不收
        if lv >= LEVELS.index(_level(r['min_level'])):
            hit = True
    return hit if has_enabled else True   # 只有停用规则时，其余人员照常接收


def notify_perm(db, merchant_id, perm, store_code, category, title, body='', exclude_user_id=None, **kw):
    level = kw.get('level', 'normal')
    uids = [a['id'] for a in users_with_perm(db, merchant_id, perm, store_code)
            if a['id'] != exclude_user_id and rules_allow(db, merchant_id, category, a, store_code, level)]
    return notify_users(db, merchant_id, uids, category, title, body, store_code=store_code, **kw)


def risk_alert(db, merchant_id, store_code, title, body='', ref_type='', ref_no='', perm='approval.pos', dedup_key=None, link=''):
    """高风险事件告警（APP-304）。业务模块在事务里调用；接收人 = 有 perm 且门店在范围内的管理者。"""
    return notify_perm(db, merchant_id, perm, store_code, 'risk', title, body, ref_type=ref_type, ref_no=ref_no,
                       link=link or ('m#order/' + ref_no if ref_type == 'order' and ref_no else ''), level='urgent',
                       dedup_key=dedup_key)


def refund_burst(db, merchant_id, store_code, cashier_user, window_minutes=60, threshold=3):
    """频繁退款检测：同一收银员 window_minutes 内退货单数 >= threshold -> 告警（同一小时只发一次）。返回窗口内单数。"""
    import time as _t
    since = _t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(_t.time() - window_minutes * 60))
    n = db.execute("SELECT COUNT(*) FROM orders WHERE merchant_id=? AND store_code=? AND cashier_user=? AND biz_type='refund' "
                   "AND created_at>=?", (merchant_id, store_code, cashier_user, since)).fetchone()[0]
    if n >= threshold:
        risk_alert(db, merchant_id, store_code, '频繁退款：%s %d 分钟内 %d 笔' % (cashier_user, window_minutes, n),
                   '门店 %s 收银员 %s 退款频繁，请核实' % (store_code, cashier_user),
                   dedup_key='refund_burst:%s:%s:%s' % (store_code, cashier_user, now_s()[:13]))
    return n


def done_by_ref(db, merchant_id, ref_type, ref_no, user_id=None):
    """关闭待办；user_id 不为空时只关该人的（如公告确认只关本人那条）。"""
    sql = "UPDATE notifications SET status='done', done_at=? WHERE merchant_id=? AND ref_type=? AND ref_no=? AND status != 'done'"
    args = [now_s(), merchant_id, ref_type, ref_no]
    if user_id is not None:
        sql += ' AND user_id=?'
        args.append(user_id)
    db.execute(sql, args)


def _row(r):
    d = dict(r)
    d['category_name'] = CATEGORIES.get(d['category'], d['category'])
    d['level_name'] = LEVEL_NAME.get(d['level'], d['level'])
    d.pop('dedup_key', None)
    return d


# ---------------------------------------------------------------- 接口 /api/notify（本人的消息）
bp = Blueprint('notify', __name__, url_prefix='/api/notify')


@bp.route('')
@api()
def my_list():
    """本人消息：status=unread|read|done|open(未处理=unread+read)、todo=1、category、page/size。data 多 unread / todo_open 计数。"""
    db = get_db()
    a = current_actor()
    page, size = max(1, inum(request.args.get('page'), 1)), min(100, max(1, inum(request.args.get('size'), 20)))
    clauses, args = ['merchant_id=? AND user_id=?'], [a['merchant_id'], a['id']]
    st = request.args.get('status') or ''
    if st in ('unread', 'read', 'done'):
        clauses.append('status=?')
        args.append(st)
    elif st == 'open':
        clauses.append("status != 'done'")
    if request.args.get('todo') == '1':
        clauses.append('is_todo=1')
    cat = request.args.get('category') or ''
    if cat:
        clauses.append('category=?')
        args.append(cat)
    where = ' WHERE ' + ' AND '.join(clauses)
    total = db.execute('SELECT COUNT(*) FROM notifications' + where, args).fetchone()[0]
    rows = db.execute('SELECT * FROM notifications' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?', args + [size, (page - 1) * size]).fetchall()
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE merchant_id=? AND user_id=? AND status='unread'", (a['merchant_id'], a['id'])).fetchone()[0]
    todo_open = db.execute("SELECT COUNT(*) FROM notifications WHERE merchant_id=? AND user_id=? AND is_todo=1 AND status != 'done'",
                           (a['merchant_id'], a['id'])).fetchone()[0]
    return ok({'page': page, 'size': size, 'total': total, 'unread': unread, 'todo_open': todo_open, 'items': [_row(r) for r in rows],
               'categories': CATEGORIES})


@bp.route('/read', methods=['POST'])
@api()
def mark_read():
    """{ids:[...]} 或 {all:true}：标记已读（只动本人、本商户的未读消息）。"""
    db = get_db()
    a = current_actor()
    d = request.get_json(silent=True) or {}
    with tx(db):
        if d.get('all'):
            cur = db.execute("UPDATE notifications SET status='read', read_at=? WHERE merchant_id=? AND user_id=? AND status='unread'",
                             (now_s(), a['merchant_id'], a['id']))
        else:
            ids = [inum(i) for i in (d.get('ids') or []) if inum(i)]
            if not ids:
                raise BizError('请选择消息')
            cur = db.execute("UPDATE notifications SET status='read', read_at=? WHERE merchant_id=? AND user_id=? AND status='unread' "
                             "AND id IN (%s)" % ','.join('?' * len(ids)), [now_s(), a['merchant_id'], a['id']] + ids)
    return ok({'updated': cur.rowcount})


@bp.route('/<int:nid>/done', methods=['POST'])
@api()
def mark_done(nid):
    """手动把本人一条提醒标记为已处理（如缺货已补、告警已核实）。审批类待办必须去审批，不能这里直接关。"""
    db = get_db()
    a = current_actor()
    r = db.execute('SELECT * FROM notifications WHERE id=? AND merchant_id=? AND user_id=?', (nid, a['merchant_id'], a['id'])).fetchone()
    if r is None:
        raise BizError('消息不存在', 404)
    if r['ref_type'] == 'approval' and r['is_todo'] and r['status'] != 'done':
        raise BizError('审批待办请在审批单里同意或驳回后自动关闭', 409, 'CONFLICT')
    with tx(db):
        db.execute("UPDATE notifications SET status='done', done_at=COALESCE(done_at, ?), read_at=COALESCE(read_at, ?) WHERE id=?",
                   (now_s(), now_s(), nid))
    return ok(_row(db.execute('SELECT * FROM notifications WHERE id=?', (nid,)).fetchone()))
