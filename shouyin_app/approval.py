# -*- coding: utf-8 -*-
"""审批单：创建 / 审批 / 驳回 / 撤回 / 一次性核销。Owner：模块④（权限）。APP（模块⑤）通过 /api/approvals 处理待办。

业务方两种用法：
1. 收银类（改价/折扣/退货/无单退/现金支出）：core.check_limit_or_approval() 自动走「本人额度 → 现场授权 → 审批单」；
   审批通过后收银台带 approval_no 重新提交，consume() 一次性核销。
2. 单据类（库存调整/报损/调拨/盘点差异/采购/调价/活动）：业务模块 create() 后，把单据状态置为 submitted；
   用 register_handler(kind, fn) 注册回调，审批通过时在同一事务里 fn(db, approval, actor) 执行单据（如过账库存）；
   用 register_reject_handler(kind, fn) 注册驳回/撤回回调，在同一事务里 fn(db, approval, actor, to_status) 把单据状态写回
   （to_status = 'rejected' 驳回 / 'cancelled' 申请人撤回）。
状态机：pending -> approved -> consumed（收银类） | pending -> rejected | pending -> cancelled（申请人撤回）
"""
import json

from flask import Blueprint, request

from .core import (get_db, tx, require_perm, api, current_actor, has_perm, scope_stores, audit, ok)
from .numbering import next_no
from .util import BizError, now_s, inum

KINDS = {
    'discount': '折扣', 'price_change': '改价', 'refund': '退货退款', 'no_receipt_refund': '无单退货', 'cash_out': '现金支出',
    'points_adjust': '积分调整', 'stored_adjust': '储值调整', 'stock_adjust': '库存调整', 'loss': '报损', 'overflow': '报溢',
    'transfer': '调拨/要货', 'stocktake': '盘点差异', 'purchase': '采购单', 'price_adjust': '调价单', 'promo': '促销活动',
    'batch_unlock': '批次解锁', 'shift_force': '强制交班',
}
STATUS_NAME = {'pending': '待审批', 'approved': '已同意', 'rejected': '已驳回', 'cancelled': '已撤回', 'consumed': '已使用', 'expired': '已过期'}

_handlers = {}
_reject_handlers = {}


def register_handler(kind, fn):
    """审批通过时回调 fn(db, approval_dict, approver_actor)，与审批在同一事务；抛 BizError 则审批失败回滚。"""
    _handlers[kind] = fn


def register_reject_handler(kind, fn):
    """审批驳回 / 申请人撤回时回调 fn(db, approval_dict, actor, to_status)，与驳回/撤回在同一事务；抛 BizError 则整体回滚。"""
    _reject_handlers[kind] = fn


def _row(db, mid, no):
    r = db.execute('SELECT * FROM approvals WHERE approval_no=? AND merchant_id=?', (no, mid)).fetchone()
    if r is None:
        raise BizError('审批单不存在', 404)
    d = dict(r)
    d['payload'] = json.loads(d['payload'] or '{}')
    d['kind_name'] = KINDS.get(d['kind'], d['kind'])
    d['status_name'] = STATUS_NAME.get(d['status'], d['status'])
    return d


def _log(db, ap, frm, to, actor, reason=''):
    db.execute('INSERT INTO approval_logs(merchant_id, approval_no, from_status, to_status, user_id, username, reason, created_at) '
               'VALUES(?,?,?,?,?,?,?,?)', (ap['merchant_id'], ap['approval_no'], frm, to, actor['id'], actor['username'], reason, now_s()))


def create(db, actor, kind, store_code, title, value=None, ref_no='', ref_type='', payload=None, reason='', need_perm='approval.pos',
           request_key=None, amount_cents=0):
    """创建审批单（幂等：同商户同 request_key 返回已有单）。通知有审批权限且门店在范围内的人（待办）。"""
    from . import notify
    mid = actor['merchant_id']
    if kind not in KINDS:
        raise BizError('未知审批类型：%s' % kind)
    if request_key:
        r = db.execute('SELECT approval_no FROM approvals WHERE merchant_id=? AND request_key=?', (mid, request_key)).fetchone()
        if r:
            return _row(db, mid, r[0])
    pl = dict(payload or {})
    if value is not None:
        pl['_value'] = str(value)
    no = next_no(db, mid, 'AP', store_code)
    db.execute('INSERT INTO approvals(approval_no, merchant_id, store_code, kind, ref_type, ref_no, title, payload, amount_cents, status, need_perm, '
               'applicant_id, applicant_name, apply_reason, request_key, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (no, mid, store_code or '', kind, ref_type, ref_no, title[:100], json.dumps(pl, ensure_ascii=False, default=str), int(amount_cents or 0),
                'pending', need_perm, actor['id'], actor['name'], reason[:200], request_key, now_s()))
    ap = _row(db, mid, no)
    _log(db, ap, None, 'pending', actor, reason)
    notify.notify_perm(db, mid, need_perm, store_code, 'approval', '待审批：%s' % title, reason, ref_type='approval', ref_no=no,
                       link='m#approval/' + no, is_todo=True, exclude_user_id=actor['id'], level='important')
    return ap


def can_decide(db, actor, ap):
    if ap['merchant_id'] != actor['merchant_id'] or not has_perm(actor, ap['need_perm']):
        return False
    if ap['applicant_id'] == actor['id']:
        return False                         # 不能自己批自己
    if ap['store_code']:
        return ap['store_code'] in scope_stores(db, actor)
    return actor['data_scope'] == 'merchant'


def decide(db, actor, no, approve, reason=''):
    from . import notify
    ap = _row(db, actor['merchant_id'], no)
    if ap['status'] != 'pending':
        raise BizError('审批单已是「%s」，不能重复处理' % ap['status_name'], 409, 'CONFLICT')
    if not can_decide(db, actor, ap):
        raise BizError('无权审批该单（需要权限 %s 且门店在你的范围内，且不能审批自己的申请）' % ap['need_perm'], 403)
    to = 'approved' if approve else 'rejected'
    if not approve and not (reason or '').strip():
        raise BizError('驳回必须填写原因')
    db.execute('UPDATE approvals SET status=?, approver_id=?, approver_name=?, decide_reason=?, decided_at=? WHERE id=? AND status=?',
               (to, actor['id'], actor['name'], (reason or '')[:200], now_s(), ap['id'], 'pending'))
    _log(db, ap, 'pending', to, actor, reason)
    ap = _row(db, actor['merchant_id'], no)
    if approve and ap['kind'] in _handlers:
        _handlers[ap['kind']](db, ap, actor)
    elif not approve and ap['kind'] in _reject_handlers:
        _reject_handlers[ap['kind']](db, ap, actor, 'rejected')
    audit(db, actor, 'approval', '审批' + ('同意' if approve else '驳回'), no, {'status': 'pending'}, {'status': to}, reason,
          level='high', store_code=ap['store_code'])
    notify.done_by_ref(db, actor['merchant_id'], 'approval', no)
    notify.notify_users(db, actor['merchant_id'], [ap['applicant_id']], 'approval',
                        '审批%s：%s' % ('通过' if approve else '被驳回', ap['title']), reason, ref_type='approval', ref_no=no,
                        link='m#approval/' + no)
    return ap


def cancel(db, actor, no):
    from . import notify
    ap = _row(db, actor['merchant_id'], no)
    if ap['applicant_id'] != actor['id']:
        raise BizError('只能撤回自己的申请', 403)
    if ap['status'] != 'pending':
        raise BizError('只有待审批的单可以撤回', 409, 'CONFLICT')
    db.execute("UPDATE approvals SET status='cancelled', decided_at=? WHERE id=?", (now_s(), ap['id']))
    _log(db, ap, 'pending', 'cancelled', actor)
    ap = _row(db, actor['merchant_id'], no)
    if ap['kind'] in _reject_handlers:
        _reject_handlers[ap['kind']](db, ap, actor, 'cancelled')
    notify.done_by_ref(db, actor['merchant_id'], 'approval', no)
    return ap


def consume(db, actor, no, kind, store_code, value=None):
    """收银类审批一次性使用：已同意、同类型、同门店、额度覆盖本次；成功后置 consumed。"""
    from decimal import Decimal
    ap = _row(db, actor['merchant_id'], no)
    if ap['kind'] != kind or (ap['store_code'] or '') != (store_code or ''):
        raise BizError('审批单与本次操作不匹配', 403, 'APPROVAL_MISMATCH')
    if ap['status'] != 'approved':
        raise BizError('审批单状态为「%s」，不能使用' % ap['status_name'], 403, 'APPROVAL_NOT_READY')
    approved = ap['payload'].get('_value')
    if value is not None and approved is not None:
        if kind == 'discount':
            okv = Decimal(str(value)) >= Decimal(approved)       # 批准到 0.8 折，本次 0.85 可用
        elif kind in ('price_change', 'no_receipt_refund'):
            okv = True
        else:
            okv = abs(int(Decimal(str(value)))) <= abs(int(Decimal(approved)))
        if not okv:
            raise BizError('本次金额/折扣超出审批批准的范围', 403, 'APPROVAL_MISMATCH')
    cur = db.execute("UPDATE approvals SET status='consumed', consumed_at=? WHERE id=? AND status='approved'", (now_s(), ap['id']))
    if cur.rowcount != 1:
        raise BizError('审批单已被使用', 409, 'CONFLICT')
    _log(db, ap, 'approved', 'consumed', actor)
    return ap


def list_for(db, actor, box='todo', status='', page=1, size=20):
    """box=todo 我能审批的待办；box=mine 我发起的；box=all 范围内全部（需 approval.view）。"""
    mid = actor['merchant_id']
    clauses, args = ['merchant_id=?'], [mid]
    if box == 'mine':
        clauses.append('applicant_id=?')
        args.append(actor['id'])
    else:
        if not has_perm(actor, 'approval.view'):
            raise BizError('没有权限查看审批单', 403)
        stores = scope_stores(db, actor)
        if actor['data_scope'] == 'merchant':
            clauses.append("(store_code='' OR store_code IN (%s))" % ','.join('?' * len(stores)) if stores else "store_code=''")
        else:
            clauses.append('store_code IN (%s)' % ','.join('?' * len(stores)) if stores else '1=0')
        args += stores
        if box == 'todo':
            clauses.append("status='pending' AND applicant_id != ?")
            args.append(actor['id'])
            mine = [p for p in actor['perms'] if p.startswith('approval.')]
            clauses.append('need_perm IN (%s)' % ','.join('?' * len(mine)) if mine else '1=0')
            args += mine
    if status:
        clauses.append('status=?')
        args.append(status)
    where = ' WHERE ' + ' AND '.join(clauses)
    total = db.execute('SELECT COUNT(*) FROM approvals' + where, args).fetchone()[0]
    rows = db.execute('SELECT approval_no FROM approvals' + where + ' ORDER BY id DESC LIMIT ? OFFSET ?', args + [size, (page - 1) * size]).fetchall()
    return {'page': page, 'size': size, 'total': total, 'items': [_row(db, mid, r[0]) for r in rows]}


# ---------------------------------------------------------------- 接口 /api/approvals
bp = Blueprint('approval', __name__, url_prefix='/api/approvals')


@bp.route('')
@api()
def approvals_list():
    db = get_db()
    a = request.args
    page, size = max(1, inum(a.get('page'), 1)), min(100, max(1, inum(a.get('size'), 20)))
    return ok(list_for(db, current_actor(), a.get('box') or 'todo', a.get('status') or '', page, size))


# 收银类审批：申请人自己必须有对应的基础操作权限，才能为它申请超额审批
REQUEST_KINDS = {'discount': 'pos.discount', 'price_change': 'pos.price.change', 'refund': 'pos.refund',
                 'no_receipt_refund': 'pos.refund', 'cash_out': 'pos.cash_move', 'points_adjust': 'member.points.adjust'}


@bp.route('', methods=['POST'])
@api()
def approvals_create():
    """收银台/APP 提交「超额度」审批单：{kind, store_code, value, title, reason, request_key, ref_no?, payload?}。
    单据类审批（库存/采购/调价…）由各模块在自己的提交接口里调用 approval.create()，不走这里。"""
    from .core import LIMIT_KIND
    db = get_db()
    actor = current_actor()
    d = request.get_json(silent=True) or {}
    kind = d.get('kind') or ''
    if kind not in REQUEST_KINDS:
        raise BizError('该类型不能在这里申请')
    if not has_perm(actor, REQUEST_KINDS[kind]):
        raise BizError('你没有该操作的基础权限', 403)
    store = (d.get('store_code') or actor['store_code'] or '').strip()
    scope_stores(db, actor, store)
    if d.get('value') in (None, ''):
        raise BizError('缺少申请的折扣率/金额（value）')
    with tx(db):
        ap = create(db, actor, kind, store, (d.get('title') or KINDS[kind] + '申请')[:100], d.get('value'), ref_no=(d.get('ref_no') or '')[:40],
                    payload=d.get('payload') or {}, reason=(d.get('reason') or '')[:200], need_perm=LIMIT_KIND[kind][1],
                    request_key=(d.get('request_key') or None))
    return ok(ap)


@bp.route('/<no>')
@api()
def approvals_get(no):
    db = get_db()
    actor = current_actor()
    ap = _row(db, actor['merchant_id'], no)
    if ap['applicant_id'] != actor['id'] and not has_perm(actor, 'approval.view'):
        raise BizError('没有权限查看该审批单', 403)
    if ap['store_code'] and ap['applicant_id'] != actor['id']:
        scope_stores(db, actor, ap['store_code'])
    ap['logs'] = [dict(r) for r in db.execute('SELECT * FROM approval_logs WHERE approval_no=? ORDER BY id', (no,))]
    ap['can_decide'] = ap['status'] == 'pending' and can_decide(db, actor, ap)
    return ok(ap)


@bp.route('/<no>/approve', methods=['POST'])
@require_perm('approval.pos', 'approval.stock', 'approval.member', 'approval.purchase')
def approvals_approve(no):
    db = get_db()
    with tx(db):
        ap = decide(db, current_actor(), no, True, (request.get_json(silent=True) or {}).get('reason') or '')
    return ok(ap)


@bp.route('/<no>/reject', methods=['POST'])
@require_perm('approval.pos', 'approval.stock', 'approval.member', 'approval.purchase')
def approvals_reject(no):
    db = get_db()
    with tx(db):
        ap = decide(db, current_actor(), no, False, (request.get_json(silent=True) or {}).get('reason') or '')
    return ok(ap)


@bp.route('/<no>/cancel', methods=['POST'])
@api()
def approvals_cancel(no):
    db = get_db()
    with tx(db):
        ap = cancel(db, current_actor(), no)
    return ok(ap)
