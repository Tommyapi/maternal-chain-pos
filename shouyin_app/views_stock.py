# -*- coding: utf-8 -*-
"""库存：仓库/余额/流水/批次查询锁定追溯/召回/要货调拨在途/盘点/报损报溢调整拆零组装质量转移/预警/期初导入/导出。

Owner：模块①（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-004 ERP-301~309 BABY-001~004 BABY-101~103 DATA-003、AC-007 AC-008 AC-009 AC-018
状态机见 docs/ARCH.md §4：要货 draft→submitted→approved(生成调拨)|rejected；调拨 draft→submitted→approved→shipped→partial|received→closed；
盘点 counting→submitted→approved|rejected→counting；报损报溢 submitted(审批单)→posted（审批回调过账）|rejected。
所有库存变化只经 inventory.post_move/stock_out/stock_in，带来源单号。
"""
import json

from flask import Blueprint, request

from . import inventory, approval, notify, xlsx_io
from .core import get_db, tx, require_perm, current_actor, has_perm, scope_stores, audit, ok, mask_phone, cost_view
from .goods_svc import body, page_query, wh_scope, check_wh, resolve_product, unit_rate, base_unit_of
from .money import yuan
from .numbering import next_no
from .util import BizError, now_s, inum, today_s

bp = Blueprint('stock', __name__, url_prefix='/api/stock')

WH_KIND = {'hq': '总部仓', 'store': '门店仓', 'defect': '残次仓'}
TF_STATUS = {'draft': '草稿', 'submitted': '待审核', 'approved': '已审核', 'shipped': '已出库（在途）', 'partial': '部分收货', 'received': '已收货',
             'closed': '已完结', 'rejected': '已驳回', 'cancelled': '已取消'}
ST_STATUS = {'counting': '盘点中', 'submitted': '待审核', 'approved': '已审核', 'rejected': '已驳回', 'cancelled': '已取消'}
ADJ_KIND = {'loss': ('报损', 'LS', 'loss'), 'overflow': ('报溢', 'OV', 'overflow'), 'adjust': ('库存调整', 'AJ', 'stock_adjust'),
            'split': ('拆零', 'SP', 'stock_adjust'), 'assemble': ('组装', 'SP', 'stock_adjust'), 'quality': ('质量状态转移', 'AJ', 'stock_adjust')}
ADJ_STATUS = {'draft': '草稿', 'submitted': '待审批', 'approved': '已审批', 'posted': '已过账', 'rejected': '已驳回', 'cancelled': '已撤回'}


def _qty(ln, mid, db, pid, label='数量', allow_negative=False):
    """行数量 -> 基本单位整数：给 base_qty 直接用；否则 qty × 单位换算率。"""
    if ln.get('base_qty') not in (None, ''):
        q = inventory.to_base_qty(ln.get('base_qty'), 1)          # 不是整数直接报错
    else:
        unit = (ln.get('unit') or '').strip()
        q = inventory.to_base_qty(ln.get('qty') if ln.get('qty') not in (None, '') else 0, unit_rate(db, mid, pid, unit))
    if q == 0 or (q < 0 and not allow_negative):
        raise BizError('%s必须大于 0' % label)
    return q


def _wh_sql(db, a, col):
    whs = wh_scope(db, a)
    if not whs:
        return ' AND 1=0', []
    return ' AND %s IN (%s)' % (col, ','.join('?' * len(whs))), whs


# ================================================================ 仓库（ERP-004）
@bp.route('/warehouses')
@require_perm('stock.view')
def warehouses():
    db, a = get_db(), current_actor()
    whs = set(wh_scope(db, a))
    out = []
    for r in db.execute('SELECT * FROM warehouses WHERE merchant_id=? ORDER BY kind, code', (a['merchant_id'],)):
        if r['code'] in whs:
            x = dict(r)
            x['kind_name'] = WH_KIND.get(x['kind'], x['kind'])
            out.append(x)
    return ok(out)


@bp.route('/warehouses', methods=['POST'])
@require_perm('org.edit')
def warehouses_save():
    """{code, name, kind:'hq'|'store'|'defect', store_code?, sellable?, status?}；门店仓编码 = 门店编码；残次仓不可售。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    code, name, kind = (d.get('code') or '').strip(), (d.get('name') or '').strip(), d.get('kind') or 'hq'
    if not code or not name:
        raise BizError('请填写仓库编码和名称')
    if kind not in WH_KIND:
        raise BizError('仓库类型只能是：总部仓 / 门店仓 / 残次仓')
    store_code = (d.get('store_code') or '').strip()
    if kind == 'store':
        store_code = code
    if store_code:
        if not db.execute('SELECT 1 FROM stores WHERE merchant_id=? AND code=?', (mid, store_code)).fetchone():
            raise BizError('门店不存在：%s' % store_code)
        scope_stores(db, a, store_code)
    elif a.get('data_scope') != 'merchant':
        raise BizError('只有总部账号能维护总部仓', 403, 'SCOPE_DENIED')
    sellable = 0 if kind == 'defect' else (1 if d.get('sellable') in (None, '', 1, True, '1') else 0)
    status = d.get('status') if d.get('status') in ('启用', '停用') else '启用'
    with tx(db):
        old = db.execute('SELECT * FROM warehouses WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
        if old is None:
            db.execute('INSERT INTO warehouses(merchant_id, code, name, kind, store_code, sellable, status) VALUES(?,?,?,?,?,?,?)',
                       (mid, code, name, kind, store_code, sellable, status))
        else:
            if old['kind'] != kind and db.execute('SELECT 1 FROM inv_stock WHERE merchant_id=? AND wh_code=? AND qty!=0', (mid, code)).fetchone():
                raise BizError('仓库有库存，不能修改类型')
            db.execute('UPDATE warehouses SET name=?, kind=?, store_code=?, sellable=?, status=? WHERE id=?', (name, kind, store_code, sellable, status, old['id']))
        audit(db, a, 'stock', '维护仓库', code, dict(old) if old else None, {'name': name, 'kind': kind, 'store_code': store_code, 'sellable': sellable,
                                                                            'status': status})
    return ok(dict(db.execute('SELECT * FROM warehouses WHERE merchant_id=? AND code=?', (mid, code)).fetchone()))


# ================================================================ 余额 / 流水（ERP-301/302）
def _req_whs(db, a, raw):
    allowed = wh_scope(db, a)
    if not raw:
        return allowed
    req = [x.strip() for x in str(raw).split(',') if x.strip()]
    for w in req:
        check_wh(db, a, w)
    return req


@bp.route('')
@require_perm('stock.view')
def balance():
    """{wh?, kw?, cat?, product_id?, by_batch?} -> 账面/可售/锁定/在途/残次/过期；库存金额按成本权限。"""
    db, a = get_db(), current_actor()
    q, mid = request.args, a['merchant_id']
    whs = _req_whs(db, a, q.get('wh') or q.get('store'))
    pids = None
    if q.get('kw') or q.get('cat') or q.get('product_id'):
        sql, args = 'SELECT p.id FROM products p WHERE p.merchant_id=?', [mid]
        if q.get('product_id'):
            sql += ' AND p.id=?'
            args.append(inum(q['product_id']))
        if q.get('kw'):
            like = '%' + q['kw'] + '%'
            sql += (' AND (p.name LIKE ? OR p.code LIKE ? OR p.pinyin LIKE ? OR p.id IN '
                    '(SELECT product_id FROM product_barcodes WHERE merchant_id=? AND barcode LIKE ?))')
            args += [like, like, '%' + q['kw'].upper() + '%', mid, like]
        if q.get('cat'):
            sql += ' AND (p.category=? OR p.category_id IN (SELECT id FROM categories WHERE merchant_id=? AND (code=? OR name=?)))'
            args += [q['cat'], mid, q['cat'], q['cat']]
        pids = [r[0] for r in db.execute(sql, args)] or [-1]
    rows = inventory.balance_rows(db, mid, whs, pids, by_batch=q.get('by_batch') == '1')
    page, size = max(1, inum(q.get('page'), 1)), min(500, max(1, inum(q.get('size'), 50)))
    items = []
    for x in rows[(page - 1) * size: page * size]:
        p = db.execute('SELECT code, name, spec, base_unit, unit, cost_cents, category, brand FROM products WHERE id=?', (x['product_id'],)).fetchone()
        x.update(code=p['code'], name=p['name'], spec=p['spec'], unit=p['base_unit'] or p['unit'], category=p['category'], brand=p['brand'])
        x['cost'] = cost_view(a, p['cost_cents'])
        x['amount'] = cost_view(a, (p['cost_cents'] or 0) * x['book'])
        if x.get('batch_id'):
            b = inventory.batch_row(db, mid, x['batch_id'])
            x.update(batch_no=b['batch_no'], expiry_date=b['expiry_date'], batch_status=b['status'])
        items.append(x)
    return ok({'page': page, 'size': size, 'total': len(rows), 'items': items})


@bp.route('/moves')
@require_perm('stock.view')
def moves():
    db, a = get_db(), current_actor()
    q, mid = request.args, a['merchant_id']
    whs = _req_whs(db, a, q.get('wh') or q.get('store'))
    if not whs:
        return ok({'page': 1, 'size': 20, 'total': 0, 'items': []})
    sql, args = 'FROM stock_moves m WHERE m.merchant_id=? AND m.wh_code IN (%s)' % ','.join('?' * len(whs)), [mid] + whs
    for k in ('product_id', 'batch_id'):
        if q.get(k):
            sql += ' AND m.%s=?' % k
            args.append(inum(q[k]))
    for k in ('ref_no', 'ref_type', 'quality'):
        if q.get(k):
            sql += ' AND m.%s=?' % k
            args.append(q[k])
    if q.get('kw'):
        sql += ' AND m.product_id IN (SELECT id FROM products WHERE merchant_id=? AND (name LIKE ? OR code LIKE ?))'
        args += [mid, '%' + q['kw'] + '%', '%' + q['kw'] + '%']
    if q.get('start'):
        sql += ' AND m.created_at>=?'
        args.append(q['start'])
    if q.get('end'):
        sql += ' AND m.created_at<=?'
        args.append(q['end'] + ' 23:59:59')

    def conv(r):
        x = dict(r)
        x['cost'] = cost_view(a, x.pop('cost_cents', None))
        p = db.execute('SELECT code, name FROM products WHERE id=?', (x['product_id'],)).fetchone()
        x['code'], x['name'] = (p[0], p[1]) if p else ('', '')
        x['type_name'] = inventory.MOVE_TYPES.get(x.get('ref_type') or '', x.get('type'))
        return x
    return ok(page_query(db, sql, args, q, 'm.id DESC', conv=conv))


@bp.route('/export')
@require_perm('stock.export')
def export():
    db, a = get_db(), current_actor()
    whs = _req_whs(db, a, request.args.get('wh') or request.args.get('store'))
    rows = inventory.balance_rows(db, a['merchant_id'], whs, by_batch=True)
    show = has_perm(a, 'goods.cost.view')
    heads = ['仓库', '商品编码', '商品名称', '批号', '有效期', '账面', '可售', '锁定', '过期', '残次/待处理'] + (['成本价', '库存金额'] if show else [])
    out = []
    for x in rows:
        p = db.execute('SELECT code, name, cost_cents FROM products WHERE id=?', (x['product_id'],)).fetchone()
        b = inventory.batch_row(db, a['merchant_id'], x['batch_id']) or {}
        out.append([x['wh_code'], p['code'], p['name'], b.get('batch_no', ''), b.get('expiry_date', ''), x['book'], x['sellable'], x['locked'],
                    x['expired'], x['defect']] + ([yuan(p['cost_cents']), yuan((p['cost_cents'] or 0) * x['book'])] if show else []))
    with tx(db):
        audit(db, a, 'stock', '导出库存', '', None, {'rows': len(out), 'whs': whs, 'cost': show}, level='security')
    return xlsx_io.send_xlsx(heads, out, '库存.xlsx', '库存')


# ================================================================ 批次：定位 / 锁定 / 解锁 / 追溯（ERP-309、BABY-001~003）
def _batch(db, a, bid):
    b = inventory.batch_row(db, a['merchant_id'], bid)
    if b is None:
        raise BizError('批次不存在', 404)
    return b


def _batch_out(db, a, b, whs):
    b = dict(b)
    x = dict(b)
    p = db.execute('SELECT code, name, brand, base_unit FROM products WHERE id=?', (b['product_id'],)).fetchone()
    s = db.execute('SELECT name FROM suppliers WHERE id=?', (b['supplier_id'],)).fetchone() if b['supplier_id'] else None
    x.update(code=p['code'], name=p['name'], brand=p['brand'], unit=p['base_unit'], supplier_name=s[0] if s else '')
    x['sellable_reason'] = inventory.check_batch_sellable(b)
    dist = [dict(wh_code=r['wh_code'], quality=r['quality'], qty=r['qty'])
            for r in db.execute('SELECT wh_code, quality, qty FROM inv_stock WHERE merchant_id=? AND batch_id=? AND qty!=0 ORDER BY wh_code',
                                (a['merchant_id'], b['id'])) if r['wh_code'] in whs]
    x['distribution'] = dist
    x['qty'] = sum(d['qty'] for d in dist)
    return x


@bp.route('/batches')
@require_perm('stock.view')
def batches():
    """BABY-001：按供应商/品牌/商品/批号/有效期查询批次与库存分布。"""
    db, a = get_db(), current_actor()
    q, mid = request.args, a['merchant_id']
    whs = wh_scope(db, a)
    sql, args = 'FROM inv_batches b WHERE b.merchant_id=?', [mid]
    if q.get('supplier_id'):
        sql += ' AND b.supplier_id=?'
        args.append(inum(q['supplier_id']))
    if q.get('product_id'):
        sql += ' AND b.product_id=?'
        args.append(inum(q['product_id']))
    if q.get('brand') or q.get('kw'):
        sql += ' AND b.product_id IN (SELECT id FROM products WHERE merchant_id=?'
        args.append(mid)
        if q.get('brand'):
            sql += ' AND brand=?'
            args.append(q['brand'])
        if q.get('kw'):
            sql += ' AND (name LIKE ? OR code LIKE ?)'
            args += ['%' + q['kw'] + '%'] * 2
        sql += ')'
    if q.get('batch_no'):
        sql += ' AND b.batch_no LIKE ?'
        args.append('%' + q['batch_no'] + '%')
    if q.get('exp_from'):
        sql += ' AND b.expiry_date>=?'
        args.append(q['exp_from'])
    if q.get('exp_to'):
        sql += " AND b.expiry_date!='' AND b.expiry_date<=?"
        args.append(q['exp_to'])
    if q.get('status'):
        sql += ' AND b.status=?'
        args.append(q['status'])
    if q.get('in_stock') == '1' and whs:
        sql += ' AND b.id IN (SELECT batch_id FROM inv_stock WHERE merchant_id=? AND qty>0 AND wh_code IN (%s))' % ','.join('?' * len(whs))
        args += [mid] + whs
    return ok(page_query(db, sql, args, q, 'b.expiry_date, b.id', conv=lambda r: _batch_out(db, a, r, whs)))


def lock_batch(db, a, b, reason, status='locked', rc_no=''):
    if not (reason or '').strip():
        raise BizError('锁定批次必须填写原因')
    if b['status'] == 'recalled' and status == 'locked':
        return b
    db.execute('UPDATE inv_batches SET status=?, lock_reason=? WHERE id=?', (status, reason[:200], b['id']))
    audit(db, a, 'stock', '批次召回锁定' if status == 'recalled' else '批次锁定', b['batch_no'], {'status': b['status']}, {'status': status, 'batch_id': b['id']},
          reason, level='high', store_code='')
    stores = [r[0] for r in db.execute('SELECT DISTINCT w.store_code FROM inv_stock s JOIN warehouses w ON w.merchant_id=s.merchant_id AND w.code=s.wh_code '
                                       "WHERE s.merchant_id=? AND s.batch_id=? AND s.qty>0 AND w.store_code!=''", (a['merchant_id'], b['id']))]
    p = db.execute('SELECT name FROM products WHERE id=?', (b['product_id'],)).fetchone()
    for s in stores + ['']:
        notify.notify_perm(db, a['merchant_id'], 'stock.view', s, 'risk', '批次已锁定禁售：%s %s' % (p[0], b['batch_no']), reason,
                           ref_type='batch', ref_no=str(b['id']), link='m#batch/%d' % b['id'], level='important',
                           dedup_key='lock:%d:%s:%s' % (b['id'], s, now_s()[:16]))
    if rc_no:
        db.execute('INSERT INTO recall_events(merchant_id, rc_no, batch_id, action, detail, operator, created_at) VALUES(?,?,?,?,?,?,?)',
                   (a['merchant_id'], rc_no, b['id'], 'lock', reason[:200], a['username'], now_s()))
    return inventory.batch_row(db, a['merchant_id'], b['id'])


@bp.route('/batches/<int:bid>/lock', methods=['POST'])
@require_perm('stock.batch.lock')
def batch_lock(bid):
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        b = lock_batch(db, a, _batch(db, a, bid), d.get('reason'))
    return ok(_batch_out(db, a, b, wh_scope(db, a)))


def _unlock_handler(db, ap, approver):
    bid = inum(ap['payload'].get('batch_id'))
    b = inventory.batch_row(db, ap['merchant_id'], bid)
    if b is None:
        raise BizError('批次不存在', 404)
    if b['status'] != 'locked':
        raise BizError('批次当前不是锁定状态', 409, 'CONFLICT')
    db.execute("UPDATE inv_batches SET status='normal', lock_reason='' WHERE id=?", (bid,))
    audit(db, approver, 'stock', '批次解锁', b['batch_no'], {'status': 'locked'}, {'status': 'normal', 'batch_id': bid}, ap['apply_reason'], level='high',
          store_code='')


def _unlock_rejected(db, ap, actor, to):
    """解锁申请被驳回/撤回：批次保持锁定（不改库存状态），留一条审计记录。"""
    bid = inum(ap['payload'].get('batch_id'))
    b = inventory.batch_row(db, ap['merchant_id'], bid)
    audit(db, actor, 'stock', '批次解锁' + ('被驳回' if to == 'rejected' else '申请撤回'), b['batch_no'] if b else str(bid),
          None, {'status': b['status'] if b else '', 'batch_id': bid, 'approval_no': ap['approval_no']}, ap.get('decide_reason') or '', store_code='')


approval.register_handler('batch_unlock', _unlock_handler)
approval.register_reject_handler('batch_unlock', _unlock_rejected)


@bp.route('/batches/<int:bid>/unlock', methods=['POST'])
@require_perm('stock.batch.unlock')
def batch_unlock(bid):
    """解锁需授权和原因：生成审批单（batch_unlock，审批人需 approval.stock），通过后自动解锁。召回批次不能解锁。"""
    db, a = get_db(), current_actor()
    d = body()
    reason = (d.get('reason') or '').strip()
    if not reason:
        raise BizError('解锁必须填写原因')
    with tx(db):
        b = _batch(db, a, bid)
        if b['status'] == 'recalled':
            raise BizError('召回批次不能解锁，请在召回单里登记退回/报损处理')
        if b['status'] != 'locked':
            raise BizError('批次未锁定', 409, 'CONFLICT')
        ap = approval.create(db, a, 'batch_unlock', '', '解锁批次 %s' % b['batch_no'], ref_no=str(bid), ref_type='batch', payload={'batch_id': bid},
                             reason=reason, need_perm='approval.stock', request_key=d.get('request_key'))
    return ok({'approval_no': ap['approval_no'], 'status': ap['status']})


def batch_trace(db, a, b):
    mid = a['merchant_id']
    whs = wh_scope(db, a)
    stores = set(scope_stores(db, a))
    cost = has_perm(a, 'goods.cost.view')
    x = _batch_out(db, a, b, whs)
    x['purchases'] = [dict(gr_no=r['gr_no'], kind=r['kind'], po_no=r['po_no'], wh_code=r['wh_code'], supplier=r['sname'] or '', qty=r['base_qty'],
                           price=yuan(r['price_cents']) if cost else None, prod_date=r['prod_date'], posted_at=r['posted_at'])
                      for r in db.execute('SELECT l.*, g.kind, g.po_no, g.wh_code, g.posted_at, s.name sname FROM goods_receipt_lines l '
                                          'JOIN goods_receipts g ON g.gr_no=l.gr_no LEFT JOIN suppliers s ON s.id=g.supplier_id '
                                          'WHERE l.merchant_id=? AND l.batch_id=? ORDER BY l.id', (mid, b['id']))]
    sales = []
    for r in db.execute("SELECT ref_type, ref_no, wh_code, -SUM(change) qty, MIN(created_at) t FROM stock_moves WHERE merchant_id=? AND batch_id=? "
                        "AND ref_type IN ('sale','refund','exchange') GROUP BY ref_type, ref_no, wh_code ORDER BY t", (mid, b['id'])):
        o = db.execute('SELECT * FROM orders WHERE order_no=? AND merchant_id=?', (r['ref_no'], mid)).fetchone()
        store = o['store_code'] if o is not None else r['wh_code']
        if store not in stores:
            continue
        sales.append(dict(type=inventory.MOVE_TYPES.get(r['ref_type']), order_no=r['ref_no'], store_code=store, qty=r['qty'],
                          created_at=o['created_at'] if o is not None else r['t'],
                          member_code=o['member_code'] if o is not None else '', member_name=o['member_name'] if o is not None else '',
                          member_phone=mask_phone(o['member_phone'], a) if o is not None else ''))
    x['sales'] = sales
    x['sold_qty'] = sum(s['qty'] for s in sales)
    x['moves'] = [dict(r) for r in db.execute('SELECT id, wh_code, ref_type, ref_no, change, quality, qty_after, operator, created_at FROM stock_moves '
                                              'WHERE merchant_id=? AND batch_id=? ORDER BY id', (mid, b['id'])) if r['wh_code'] in whs]
    return x


@bp.route('/batches/<int:bid>/trace')
@require_perm('stock.view')
def batch_trace_api(bid):
    """BABY-003 / AC-009：采购来源、库存分布、已售订单（会员手机号按 member.phone.view 脱敏）。"""
    db, a = get_db(), current_actor()
    return ok(batch_trace(db, a, _batch(db, a, bid)))


# ================================================================ 召回（BABY-004）
RECALL_ACTIONS = {'locate': '定位', 'lock': '锁定', 'notify': '通知顾客/门店', 'return': '退回供应商', 'loss': '报损销毁', 'supplier': '供应商处理',
                  'other': '其他'}


def _rc(db, mid, no):
    r = db.execute('SELECT * FROM recall_cases WHERE rc_no=? AND merchant_id=?', (no, mid)).fetchone()
    if r is None:
        raise BizError('召回单不存在', 404)
    x = dict(r)
    x['criteria'] = json.loads(x['criteria'] or '{}')
    return x


def _rc_out(db, a, rc, detail=True):
    x = dict(rc)
    if detail:
        x['batches'] = [batch_trace(db, a, inventory.batch_row(db, a['merchant_id'], bid)) for bid in rc['criteria'].get('batch_ids', [])
                        if inventory.batch_row(db, a['merchant_id'], bid)]
        x['events'] = [dict(r, action_name=RECALL_ACTIONS.get(r['action'], r['action']))
                       for r in db.execute('SELECT * FROM recall_events WHERE merchant_id=? AND rc_no=? ORDER BY id', (a['merchant_id'], rc['rc_no']))]
    return x


@bp.route('/recalls')
@require_perm('stock.recall')
def recalls():
    db, a = get_db(), current_actor()
    return ok(page_query(db, 'FROM recall_cases WHERE merchant_id=?', [a['merchant_id']], request.args, 'id DESC',
                         conv=lambda r: dict(r, criteria=json.loads(r['criteria'] or '{}'))))


@bp.route('/recalls/<no>')
@require_perm('stock.recall')
def recall_get(no):
    db, a = get_db(), current_actor()
    return ok(_rc_out(db, a, _rc(db, a['merchant_id'], no)))


@bp.route('/recalls', methods=['POST'])
@require_perm('stock.recall')
def recall_create():
    """{title, batch_ids:[..]} 或 {title, product_id, batch_no?, supplier_id?} 定位批次 -> 召回锁定（销售、调出均被阻止）。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    title = (d.get('title') or '').strip()
    if not title:
        raise BizError('请填写召回标题/原因')
    if a.get('data_scope') != 'merchant':
        raise BizError('召回需要总部账号发起', 403, 'SCOPE_DENIED')
    with tx(db):
        ids = [inum(x) for x in (d.get('batch_ids') or []) if inum(x)]
        if not ids:
            sql, args = 'SELECT id FROM inv_batches WHERE merchant_id=?', [mid]
            if d.get('product_id'):
                sql += ' AND product_id=?'
                args.append(inum(d['product_id']))
            if d.get('batch_no'):
                sql += ' AND batch_no=?'
                args.append(d['batch_no'])
            if d.get('supplier_id'):
                sql += ' AND supplier_id=?'
                args.append(inum(d['supplier_id']))
            if len(args) == 1:
                raise BizError('请指定批次或定位条件（商品/批号/供应商）')
            ids = [r[0] for r in db.execute(sql, args)]
        if not ids:
            raise BizError('没有找到符合条件的批次')
        bs = [_batch(db, a, i) for i in ids]
        no = next_no(db, mid, 'RC', '')
        db.execute("INSERT INTO recall_cases(rc_no, merchant_id, title, criteria, status, created_by, created_at) VALUES(?,?,?,?,'open',?,?)",
                   (no, mid, title[:100], json.dumps({'batch_ids': ids, 'query': dict((k, d.get(k)) for k in ('product_id', 'batch_no', 'supplier_id'))},
                                                     ensure_ascii=False), a['username'], now_s()))
        db.execute('INSERT INTO recall_events(merchant_id, rc_no, batch_id, action, detail, operator, created_at) VALUES(?,?,?,?,?,?,?)',
                   (mid, no, None, 'locate', '定位批次 %d 个' % len(ids), a['username'], now_s()))
        for b in bs:
            lock_batch(db, a, b, '召回：' + title, status='recalled', rc_no=no)
        audit(db, a, 'stock', '新建召回单', no, None, {'batch_ids': ids}, title, level='high', store_code='')
    return ok(_rc_out(db, a, _rc(db, mid, no)))


@bp.route('/recalls/<no>/events', methods=['POST'])
@require_perm('stock.recall')
def recall_event(no):
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    act = d.get('action')
    if act not in RECALL_ACTIONS or act in ('locate', 'lock'):
        raise BizError('处理类型只能是：通知/退回供应商/报损销毁/供应商处理/其他')
    with tx(db):
        rc = _rc(db, mid, no)
        if rc['status'] != 'open':
            raise BizError('召回单已关闭', 409, 'CONFLICT')
        db.execute('INSERT INTO recall_events(merchant_id, rc_no, batch_id, action, detail, ref_no, operator, created_at) VALUES(?,?,?,?,?,?,?,?)',
                   (mid, no, inum(d.get('batch_id')) or None, act, (d.get('detail') or '')[:500], (d.get('ref_no') or '')[:40], a['username'], now_s()))
        audit(db, a, 'stock', '召回处理:' + RECALL_ACTIONS[act], no, None, d, d.get('detail') or '', level='high', store_code='')
    return ok(_rc_out(db, a, _rc(db, mid, no)))


@bp.route('/recalls/<no>/close', methods=['POST'])
@require_perm('stock.recall')
def recall_close(no):
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        rc = _rc(db, a['merchant_id'], no)
        if rc['status'] != 'open':
            raise BizError('召回单已关闭', 409, 'CONFLICT')
        db.execute("UPDATE recall_cases SET status='closed', closed_at=? WHERE id=?", (now_s(), rc['id']))
        audit(db, a, 'stock', '关闭召回单', no, {'status': 'open'}, {'status': 'closed'}, d.get('reason') or '', level='high', store_code='')
    return ok(_rc_out(db, a, _rc(db, a['merchant_id'], no)))


@bp.route('/recalls/<no>/export')
@require_perm('stock.recall')
def recall_export(no):
    db, a = get_db(), current_actor()
    x = _rc_out(db, a, _rc(db, a['merchant_id'], no))
    rows = []
    for b in x['batches']:
        for d in b['distribution']:
            rows.append(['库存', b['code'], b['name'], b['batch_no'], b['expiry_date'], d['wh_code'], d['qty'], '', '', ''])
        for s in b['sales']:
            rows.append(['已售', b['code'], b['name'], b['batch_no'], b['expiry_date'], s['store_code'], s['qty'], s['order_no'],
                         s['member_name'] or '', s['member_phone'] or ''])
    for e in x['events']:
        rows.append(['处理记录', '', '', '', '', e['operator'], '', e['ref_no'], e['action_name'], (e['detail'] or '') + ' ' + e['created_at']])
    with tx(db):
        audit(db, a, 'stock', '导出召回清单', no, None, {'rows': len(rows)}, level='security', store_code='')
    return xlsx_io.send_xlsx(['类型', '商品编码', '商品名称', '批号', '有效期', '门店/仓库/操作人', '数量', '单号', '会员/处理', '手机号/说明'], rows,
                             '召回处理清单-%s.xlsx' % no, '召回')


# ================================================================ 要货 / 调拨 / 在途（ERP-304/305，AC-007）
def _tf(db, mid, no):
    r = db.execute('SELECT * FROM transfer_orders WHERE tf_no=? AND merchant_id=?', (no, mid)).fetchone()
    if r is None:
        raise BizError('单据不存在', 404)
    return dict(r)


def _tf_out(db, a, t, lines=True):
    x = dict(t)
    x['status_name'] = TF_STATUS.get(x['status'], x['status'])
    if lines:
        x['lines'] = []
        for r in db.execute('SELECT l.*, p.code, p.name, p.base_unit, b.batch_no, b.expiry_date FROM transfer_lines l JOIN products p ON p.id=l.product_id '
                            'LEFT JOIN inv_batches b ON b.id=l.batch_id WHERE l.merchant_id=? AND l.tf_no=? ORDER BY l.id', (t['merchant_id'], t['tf_no'])):
            y = dict(r)
            y['req_base_qty'] = y['req_qty'] * y['rate']
            y['in_transit'] = y['out_base_qty'] - y['in_base_qty'] - y['diff_base_qty'] if t['status'] in ('shipped', 'partial') else 0
            x['lines'].append(y)
    return x


def _tf_lines(db, mid, lines):
    out = []
    for ln in lines or []:
        p = resolve_product(db, mid, ln.get('product_id') or ln.get('code'))
        unit = (ln.get('unit') or '').strip() or base_unit_of(p)
        rate = unit_rate(db, mid, p['id'], unit)
        q = ln.get('qty')
        try:
            qi = int(q)
        except (TypeError, ValueError):
            raise BizError('数量必须是整数（按所选单位）')
        if qi <= 0 or str(qi) != str(q).strip().split('.')[0]:
            raise BizError('数量必须大于 0')
        out.append(dict(product_id=p['id'], unit=unit, rate=rate, req_qty=qi, batch_id=inum(ln.get('batch_id'))))
    if not out:
        raise BizError('至少要有一行商品')
    return out


def _insert_tf(db, a, kind, from_wh, to_wh, lines, remark='', src_no='', status='draft'):
    mid = a['merchant_id']
    no = next_no(db, mid, 'RQ' if kind == 'request' else 'TF', to_wh if kind == 'request' else from_wh)
    db.execute('INSERT INTO transfer_orders(tf_no, merchant_id, kind, src_no, from_wh, to_wh, status, remark, created_by, created_at) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)', (no, mid, kind, src_no, from_wh, to_wh, status, (remark or '')[:200], a['username'], now_s()))
    for x in lines:
        db.execute('INSERT INTO transfer_lines(merchant_id, tf_no, product_id, unit, rate, req_qty, batch_id) VALUES(?,?,?,?,?,?,?)',
                   (mid, no, x['product_id'], x['unit'], x['rate'], x['req_qty'], x.get('batch_id') or 0))
    return no


def _tf_set(db, a, t, frm, to, action, reason='', **cols):
    if t['status'] not in frm:
        raise BizError('单据当前「%s」，不能%s' % (TF_STATUS[t['status']], action), 409, 'CONFLICT')
    sets = ['status=?'] + ['%s=?' % k for k in cols]
    cur = db.execute('UPDATE transfer_orders SET %s WHERE id=? AND status=?' % ','.join(sets), [to] + list(cols.values()) + [t['id'], t['status']])
    if cur.rowcount != 1:
        raise BizError('单据状态已变化，请刷新', 409, 'CONFLICT')
    st = db.execute('SELECT store_code FROM warehouses WHERE merchant_id=? AND code=?', (a['merchant_id'], t['to_wh'])).fetchone()
    audit(db, a, 'stock', action, t['tf_no'], {'status': t['status']}, dict({'status': to}, **cols), reason, store_code=st[0] if st else '')


def _list_tf(kind):
    db, a = get_db(), current_actor()
    q = request.args
    whs = wh_scope(db, a)
    if not whs:
        return ok({'page': 1, 'size': 20, 'total': 0, 'items': []})
    ph = ','.join('?' * len(whs))
    sql, args = 'FROM transfer_orders WHERE merchant_id=? AND kind=? AND (from_wh IN (%s) OR to_wh IN (%s))' % (ph, ph), [a['merchant_id'], kind] + whs + whs
    if q.get('status'):
        sql += ' AND status=?'
        args.append(q['status'])
    if q.get('wh'):
        sql += ' AND (from_wh=? OR to_wh=?)'
        args += [q['wh'], q['wh']]
    return ok(page_query(db, sql, args, q, 'id DESC', conv=lambda r: _tf_out(db, a, r, False)))


def _get_tf(no, kind):
    db, a = get_db(), current_actor()
    t = _tf(db, a['merchant_id'], no)
    whs = wh_scope(db, a)
    if t['kind'] != kind:
        raise BizError('单据不存在', 404)
    if t['from_wh'] not in whs and t['to_wh'] not in whs:
        raise BizError('无权查看该单据', 403, 'SCOPE_DENIED')
    return ok(_tf_out(db, a, t))


def _default_hq(db, mid):
    r = db.execute("SELECT code FROM warehouses WHERE merchant_id=? AND kind='hq' AND status='启用' ORDER BY id LIMIT 1", (mid,)).fetchone()
    if r is None:
        raise BizError('还没有总部仓，请先在仓库管理里新建总部仓，或指定供货仓库')
    return r[0]


@bp.route('/requests')
@require_perm('stock.request', 'stock.transfer.approve')
def requests_list():
    return _list_tf('request')


@bp.route('/requests/<no>')
@require_perm('stock.request', 'stock.transfer.approve')
def request_get(no):
    return _get_tf(no, 'request')


@bp.route('/requests', methods=['POST'])
@require_perm('stock.request')
def request_create():
    """门店要货：{to_wh(本店), from_wh?(默认总部仓), lines:[{product_id|code, unit, qty}], remark, submit?}"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        to = check_wh(db, a, d.get('to_wh') or a.get('store_code'), '要货门店仓')
        frm = (d.get('from_wh') or _default_hq(db, mid)).strip()
        if not db.execute('SELECT 1 FROM warehouses WHERE merchant_id=? AND code=?', (mid, frm)).fetchone() or frm == to['code']:
            raise BizError('供货仓库不正确')
        no = _insert_tf(db, a, 'request', frm, to['code'], _tf_lines(db, mid, d.get('lines')), d.get('remark'),
                        status='submitted' if d.get('submit') else 'draft')
        audit(db, a, 'stock', '新建要货单', no, None, {'to_wh': to['code'], 'from_wh': frm}, store_code=to['store_code'])
        if d.get('submit'):
            notify.notify_perm(db, mid, 'stock.transfer.approve', to['store_code'], 'transfer', '待审核要货：%s' % no, ref_type='transfer', ref_no=no,
                               link='m#transfer/' + no, is_todo=True, exclude_user_id=a['id'])
    return ok(_tf_out(db, a, _tf(db, mid, no)))


@bp.route('/requests/<no>/submit', methods=['POST'])
@require_perm('stock.request')
def request_submit(no):
    db, a = get_db(), current_actor()
    with tx(db):
        t = _tf(db, a['merchant_id'], no)
        check_wh(db, a, t['to_wh'])
        _tf_set(db, a, t, ('draft',), 'submitted', '提交要货单')
    return ok(_tf_out(db, a, _tf(db, a['merchant_id'], no)))


@bp.route('/requests/<no>/approve', methods=['POST'])
@require_perm('stock.transfer.approve')
def request_approve(no):
    """总部审核：可按建议数量修改 {lines:[{id, qty}], reason}；驳回 {reject:1, reason}。通过后生成调拨单（已审核，待出库）。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        t = _tf(db, mid, no)
        if t['kind'] != 'request':
            raise BizError('单据不存在', 404)
        check_wh(db, a, t['to_wh'])
        if d.get('reject'):
            if not (d.get('reason') or '').strip():
                raise BizError('驳回必须填写原因')
            _tf_set(db, a, t, ('submitted',), 'rejected', '驳回要货单', d.get('reason'), approved_by=a['username'], approved_at=now_s())
            notify.done_by_ref(db, mid, 'transfer', no)
            return ok(_tf_out(db, a, _tf(db, mid, no)))
        change = dict((inum(x.get('id')), x.get('qty')) for x in (d.get('lines') or []))
        lines = []
        for ln in db.execute('SELECT * FROM transfer_lines WHERE merchant_id=? AND tf_no=? ORDER BY id', (mid, no)).fetchall():
            q = ln['req_qty']
            if ln['id'] in change:
                q = inum(change[ln['id']])
                if q < 0:
                    raise BizError('审核数量不能为负')
                if q != ln['req_qty']:
                    db.execute('UPDATE transfer_lines SET diff_reason=? WHERE id=?', ('审核改为 %d（原 %d）%s' % (q, ln['req_qty'], d.get('reason') or ''), ln['id']))
            if q:
                lines.append(dict(product_id=ln['product_id'], unit=ln['unit'], rate=ln['rate'], req_qty=q, batch_id=ln['batch_id']))
        if not lines:
            raise BizError('审核后没有要配送的商品，请用驳回')
        tf = _insert_tf(db, a, 'transfer', t['from_wh'], t['to_wh'], lines, '要货单 %s 生成' % no, src_no=no, status='approved')
        db.execute('UPDATE transfer_orders SET approved_by=?, approved_at=? WHERE tf_no=?', (a['username'], now_s(), tf))
        _tf_set(db, a, t, ('submitted',), 'approved', '审核要货单', d.get('reason') or '', approved_by=a['username'], approved_at=now_s())
        notify.done_by_ref(db, mid, 'transfer', no)
    x = _tf_out(db, a, _tf(db, mid, no))
    x['transfer_no'] = tf
    return ok(x)


@bp.route('/transfers')
@require_perm('stock.transfer', 'stock.ship', 'stock.receive', 'stock.transfer.approve')
def transfers_list():
    return _list_tf('transfer')


@bp.route('/transfers/<no>')
@require_perm('stock.transfer', 'stock.ship', 'stock.receive', 'stock.transfer.approve', 'stock.view')
def transfer_get(no):
    return _get_tf(no, 'transfer')


@bp.route('/transfers', methods=['POST'])
@require_perm('stock.transfer')
def transfer_create():
    """{from_wh, to_wh, lines:[{product_id|code, unit, qty, batch_id?}], remark, submit?}"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        frm = check_wh(db, a, d.get('from_wh'), '调出仓库')
        to = (d.get('to_wh') or '').strip()
        if not db.execute('SELECT 1 FROM warehouses WHERE merchant_id=? AND code=?', (mid, to)).fetchone() or to == frm['code']:
            raise BizError('调入仓库不正确')
        no = _insert_tf(db, a, 'transfer', frm['code'], to, _tf_lines(db, mid, d.get('lines')), d.get('remark'),
                        status='submitted' if d.get('submit') else 'draft')
        audit(db, a, 'stock', '新建调拨单', no, None, {'from_wh': frm['code'], 'to_wh': to}, store_code=frm['store_code'])
    return ok(_tf_out(db, a, _tf(db, mid, no)))


@bp.route('/transfers/<no>/submit', methods=['POST'])
@require_perm('stock.transfer')
def transfer_submit(no):
    db, a = get_db(), current_actor()
    with tx(db):
        t = _tf(db, a['merchant_id'], no)
        check_wh(db, a, t['from_wh'])
        _tf_set(db, a, t, ('draft',), 'submitted', '提交调拨单')
    return ok(_tf_out(db, a, _tf(db, a['merchant_id'], no)))


@bp.route('/transfers/<no>/approve', methods=['POST'])
@require_perm('stock.transfer.approve')
def transfer_approve(no):
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        t = _tf(db, a['merchant_id'], no)
        check_wh(db, a, t['from_wh'])
        if d.get('reject'):
            if not (d.get('reason') or '').strip():
                raise BizError('驳回必须填写原因')
            _tf_set(db, a, t, ('submitted',), 'rejected', '驳回调拨单', d.get('reason'))
        else:
            _tf_set(db, a, t, ('submitted',), 'approved', '审核调拨单', d.get('reason') or '', approved_by=a['username'], approved_at=now_s())
    return ok(_tf_out(db, a, _tf(db, a['merchant_id'], no)))


@bp.route('/transfers/<no>/cancel', methods=['POST'])
@require_perm('stock.transfer')
def transfer_cancel(no):
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        t = _tf(db, a['merchant_id'], no)
        check_wh(db, a, t['from_wh'])
        _tf_set(db, a, t, ('draft', 'submitted', 'approved'), 'cancelled', '取消调拨单', d.get('reason') or '')
    return ok(_tf_out(db, a, _tf(db, a['merchant_id'], no)))


@bp.route('/transfers/<no>/ship', methods=['POST'])
@require_perm('stock.ship')
def transfer_ship(no):
    """调出方出库 -> 在途。批次商品按 FEFO 自动选批（可指定 batch_id）；锁定/召回/过期批次不能调出（ERP-309）。"""
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    with tx(db):
        t = _tf(db, mid, no)
        check_wh(db, a, t['from_wh'], '调出仓库')
        if t['kind'] != 'transfer':
            raise BizError('要货单不能直接出库，请先审核生成调拨单')
        _tf_set(db, a, t, ('approved',), 'shipped', '调拨出库', shipped_by=a['username'], shipped_at=now_s())
        for ln in db.execute('SELECT * FROM transfer_lines WHERE merchant_id=? AND tf_no=? ORDER BY id', (mid, no)).fetchall():
            base = ln['req_qty'] * ln['rate']
            if base <= 0:
                continue
            alloc = inventory.stock_out(db, a, mid, t['from_wh'], [dict(product_id=ln['product_id'], base_qty=base, batch_id=ln['batch_id'] or None,
                                                                         unit=ln['unit'], unit_qty=ln['req_qty'], rate=ln['rate'],
                                                                         note='调往 ' + t['to_wh'])], 'transfer_out', no)[0]
            first = alloc[0]
            db.execute('UPDATE transfer_lines SET batch_id=?, out_base_qty=? WHERE id=?', (first['batch_id'], first['qty'], ln['id']))
            for x in alloc[1:]:          # 跨批次：按批次拆行，收货/差异都按批次追踪
                db.execute('INSERT INTO transfer_lines(merchant_id, tf_no, product_id, unit, rate, req_qty, batch_id, out_base_qty) VALUES(?,?,?,?,?,0,?,?)',
                           (mid, no, ln['product_id'], ln['unit'], ln['rate'], x['batch_id'], x['qty']))
        st = db.execute('SELECT store_code FROM warehouses WHERE merchant_id=? AND code=?', (mid, t['to_wh'])).fetchone()
        notify.notify_perm(db, mid, 'stock.receive', st[0] if st else '', 'transfer', '调拨到货待收：%s（来自 %s）' % (no, t['from_wh']),
                           ref_type='transfer', ref_no=no, link='m#transfer/' + no, is_todo=True)
    return ok(_tf_out(db, a, _tf(db, mid, no)))


@bp.route('/transfers/<no>/receive', methods=['POST'])
@require_perm('stock.receive')
def transfer_receive(no):
    """调入方收货（可多次）：{lines:[{id, in_base_qty | in_qty(按行单位), diff_reason?, quality?}]}。
    填了 diff_reason 表示本行剩余未到数量记为差异（不再在途，不平账，等待差异处理）；没填则剩余继续在途（部分收货）。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        t = _tf(db, mid, no)
        check_wh(db, a, t['to_wh'], '调入仓库')
        if t['status'] not in ('shipped', 'partial'):
            raise BizError('单据当前「%s」，不能收货' % TF_STATUS[t['status']], 409, 'CONFLICT')
        for x in (d.get('lines') or []):
            ln = db.execute('SELECT * FROM transfer_lines WHERE id=? AND merchant_id=? AND tf_no=?', (inum(x.get('id')), mid, no)).fetchone()
            if ln is None:
                raise BizError('收货行不属于该调拨单')
            if x.get('in_base_qty') not in (None, ''):
                q = inum(x.get('in_base_qty'))
            else:
                q = inventory.to_base_qty(x.get('in_qty') or 0, ln['rate'])
            remain = ln['out_base_qty'] - ln['in_base_qty'] - ln['diff_base_qty']
            if q < 0 or q > remain:
                raise BizError('收货数量 %d 超过在途数量 %d' % (q, remain))
            if q:
                quality = x.get('quality') or 'ok'
                inventory.stock_in(db, a, mid, t['to_wh'], [dict(product_id=ln['product_id'], base_qty=q, batch_id=ln['batch_id'], quality=quality,
                                                                  unit=ln['unit'], rate=ln['rate'], note='来自 ' + t['from_wh'])], 'transfer_in', no)
            reason = (x.get('diff_reason') or '').strip()
            diff = remain - q if reason else 0
            db.execute('UPDATE transfer_lines SET in_base_qty=in_base_qty+?, diff_base_qty=diff_base_qty+?, diff_reason=CASE WHEN ?!=\'\' THEN ? '
                       'ELSE diff_reason END WHERE id=?', (q, diff, reason, reason[:200], ln['id']))
        left = db.execute('SELECT COALESCE(SUM(out_base_qty-in_base_qty-diff_base_qty),0) FROM transfer_lines WHERE merchant_id=? AND tf_no=?',
                          (mid, no)).fetchone()[0]
        to = 'partial' if left > 0 else 'received'
        _tf_set(db, a, t, ('shipped', 'partial'), to, '调拨收货', received_by=a['username'], received_at=now_s())
        if to == 'received':
            notify.done_by_ref(db, mid, 'transfer', no)
    return ok(_tf_out(db, a, _tf(db, mid, no)))


@bp.route('/transfers/<no>/close', methods=['POST'])
@require_perm('stock.transfer.approve')
def transfer_close(no):
    """差异处理完结：{lines:[{id, settle:'return'(退回调出方，生成回库流水)|'loss'(运输损耗，已在调出方出库，不再变动)|'claim'(索赔)}], reason}"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        t = _tf(db, mid, no)
        check_wh(db, a, t['from_wh'])
        if t['status'] != 'received':
            raise BizError('全部收货或登记差异后才能完结', 409, 'CONFLICT')
        settle = dict((inum(x.get('id')), x.get('settle')) for x in (d.get('lines') or []))
        for ln in db.execute('SELECT * FROM transfer_lines WHERE merchant_id=? AND tf_no=? AND diff_base_qty>0', (mid, no)).fetchall():
            s = settle.get(ln['id'])
            if s not in ('return', 'loss', 'claim'):
                raise BizError('差异行必须选择处理方式：退回调出方 / 运输损耗 / 索赔')
            if s == 'return':
                inventory.stock_in(db, a, mid, t['from_wh'], [dict(product_id=ln['product_id'], base_qty=ln['diff_base_qty'], batch_id=ln['batch_id'],
                                                                    note='调拨差异退回 ' + no)], 'transfer_in', no)
            db.execute('UPDATE transfer_lines SET diff_settle=? WHERE id=?', (s, ln['id']))
        _tf_set(db, a, t, ('received',), 'closed', '调拨差异处理完结', d.get('reason') or '')
    return ok(_tf_out(db, a, _tf(db, mid, no)))


# ================================================================ 盘点（ERP-306，AC-018）
def _st(db, mid, no):
    r = db.execute('SELECT * FROM stocktakes WHERE st_no=? AND merchant_id=?', (no, mid)).fetchone()
    if r is None:
        raise BizError('盘点单不存在', 404)
    return dict(r)


def _max_move(db):
    return db.execute('SELECT COALESCE(MAX(id),0) FROM stock_moves').fetchone()[0]


def _moved(db, mid, wh, pid, bid, after_id, upto_id):
    return int(db.execute("SELECT COALESCE(SUM(change),0) FROM stock_moves WHERE merchant_id=? AND wh_code=? AND product_id=? AND batch_id=? "
                          "AND quality='ok' AND id>? AND id<=?", (mid, wh, pid, bid, after_id, upto_id)).fetchone()[0])


def _st_out(db, a, st, lines=True):
    x = dict(st)
    x['status_name'] = ST_STATUS.get(x['status'], x['status'])
    if lines:
        x['lines'] = []
        for r in db.execute('SELECT l.*, p.code, p.name, p.base_unit, p.cost_cents, b.batch_no, b.expiry_date FROM stocktake_lines l '
                            'JOIN products p ON p.id=l.product_id LEFT JOIN inv_batches b ON b.id=l.batch_id WHERE l.merchant_id=? AND l.st_no=? '
                            'ORDER BY p.code, l.batch_id', (st['merchant_id'], st['st_no'])):
            y = dict(r)
            c = y.pop('cost_cents')
            y['diff_amount'] = cost_view(a, (c or 0) * y['diff_qty'])
            x['lines'].append(y)
        x['uncounted'] = sum(1 for y in x['lines'] if y['counted_qty'] is None)
    return x


@bp.route('/stocktakes')
@require_perm('stock.take', 'stock.take.approve')
def stocktakes():
    db, a = get_db(), current_actor()
    s, sa = _wh_sql(db, a, 'wh_code')
    q = request.args
    sql, args = 'FROM stocktakes WHERE merchant_id=?' + s, [a['merchant_id']] + sa
    if q.get('status'):
        sql += ' AND status=?'
        args.append(q['status'])
    return ok(page_query(db, sql, args, q, 'id DESC', conv=lambda r: _st_out(db, a, r, False)))


@bp.route('/stocktakes/<no>')
@require_perm('stock.take', 'stock.take.approve')
def stocktake_get(no):
    db, a = get_db(), current_actor()
    st = _st(db, a['merchant_id'], no)
    check_wh(db, a, st['wh_code'])
    return ok(_st_out(db, a, st))


@bp.route('/stocktakes', methods=['POST'])
@require_perm('stock.take')
def stocktake_create():
    """{wh_code, scope:'full'|'category'|'spot'|'dynamic', scope_value(分类编码/名称 或 商品编码逗号分隔), remark}。
    建单即按当前账面生成快照（snapshot_move_id）；盘点期间照常营业，审核时按 实盘 − (快照 + 期间流水) 算差异。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    scope = d.get('scope') or 'full'
    if scope not in ('full', 'category', 'spot', 'dynamic'):
        raise BizError('盘点范围只能是：全盘 / 分类 / 抽盘 / 动态盘点')
    sv = (d.get('scope_value') or '').strip()
    with tx(db):
        wh = check_wh(db, a, d.get('wh_code'))
        if db.execute("SELECT 1 FROM stocktakes WHERE merchant_id=? AND wh_code=? AND status IN ('counting','submitted') AND scope IN ('full','dynamic')",
                      (mid, wh['code'])).fetchone() and scope in ('full', 'dynamic'):
            raise BizError('该仓库已有进行中的全盘，请先完成或取消', 409, 'CONFLICT')
        sql, args = "SELECT s.product_id, s.batch_id, s.qty FROM inv_stock s JOIN products p ON p.id=s.product_id WHERE s.merchant_id=? AND s.wh_code=? " \
                    "AND s.quality='ok'", [mid, wh['code']]
        spot_ids = []
        if scope == 'category':
            if not sv:
                raise BizError('分类盘点请选择分类')
            sql += ' AND (p.category=? OR p.category_id IN (SELECT id FROM categories WHERE merchant_id=? AND (code=? OR name=?)))'
            args += [sv, mid, sv, sv]
        elif scope == 'spot':
            spot_ids = [resolve_product(db, mid, x.strip())['id'] for x in sv.replace('，', ',').split(',') if x.strip()]
            if not spot_ids:
                raise BizError('抽盘请填写商品')
            sql += ' AND s.product_id IN (%s)' % ','.join('?' * len(spot_ids))
            args += spot_ids
        no = next_no(db, mid, 'ST', wh['code'])
        snap = _max_move(db)
        db.execute("INSERT INTO stocktakes(st_no, merchant_id, wh_code, scope, scope_value, status, snapshot_at, snapshot_move_id, created_by, created_at, remark) "
                   "VALUES(?,?,?,?,?,'counting',?,?,?,?,?)", (no, mid, wh['code'], scope, sv, now_s(), snap, a['username'], now_s(), (d.get('remark') or '')[:200]))
        seen = set()
        for r in db.execute(sql, args).fetchall():
            if r['qty'] == 0 and r['batch_id']:
                continue
            seen.add(r['product_id'])
            db.execute('INSERT INTO stocktake_lines(merchant_id, st_no, product_id, batch_id, book_qty) VALUES(?,?,?,?,?)',
                       (mid, no, r['product_id'], r['batch_id'], r['qty']))
        for pid in spot_ids:
            if pid not in seen:
                db.execute('INSERT INTO stocktake_lines(merchant_id, st_no, product_id, batch_id, book_qty) VALUES(?,?,?,0,0)', (mid, no, pid))
        audit(db, a, 'stock', '新建盘点单', no, None, {'wh': wh['code'], 'scope': scope, 'scope_value': sv}, store_code=wh['store_code'])
    return ok(_st_out(db, a, _st(db, mid, no)))


@bp.route('/stocktakes/<no>/lines', methods=['POST'])
@require_perm('stock.take')
def stocktake_lines(no):
    """录入实盘（后台/APP 扫码同用）：{lines:[{product_id|code(条码/箱码), batch_id?, qty, unit?} 或 base_qty], mode:'set'|'add'}"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    mode = d.get('mode') or 'set'
    with tx(db):
        st = _st(db, mid, no)
        check_wh(db, a, st['wh_code'])
        if st['status'] != 'counting':
            raise BizError('盘点单当前「%s」，不能录入' % ST_STATUS[st['status']], 409, 'CONFLICT')
        cur_move = _max_move(db)
        for x in (d.get('lines') or []):
            ref = x.get('product_id') or x.get('code')
            p = resolve_product(db, mid, ref)
            if x.get('base_qty') not in (None, ''):
                q = inum(x['base_qty'])
            else:
                unit = (x.get('unit') or '').strip()
                if not unit and x.get('code'):
                    from .goods_svc import find_sku
                    f = find_sku(db, mid, str(x['code']))
                    unit = f['unit'] if f else ''
                q = inventory.to_base_qty(x.get('qty') or 0, unit_rate(db, mid, p['id'], unit))
            if q < 0:
                raise BizError('实盘数量不能为负')
            bid = inum(x.get('batch_id'))
            ln = db.execute('SELECT * FROM stocktake_lines WHERE merchant_id=? AND st_no=? AND product_id=? AND batch_id=?', (mid, no, p['id'], bid)).fetchone()
            if ln is None:
                if st['scope'] in ('category', 'spot') and not x.get('force'):
                    raise BizError('%s 不在本次盘点范围内' % p['name'])
                book = inventory.qty_of(db, mid, st['wh_code'], p['id'], bid, 'ok') - _moved(db, mid, st['wh_code'], p['id'], bid, st['snapshot_move_id'], cur_move)
                db.execute('INSERT INTO stocktake_lines(merchant_id, st_no, product_id, batch_id, book_qty) VALUES(?,?,?,?,?)', (mid, no, p['id'], bid, book))
                ln = db.execute('SELECT * FROM stocktake_lines WHERE merchant_id=? AND st_no=? AND product_id=? AND batch_id=?', (mid, no, p['id'], bid)).fetchone()
            newq = (ln['counted_qty'] or 0) + q if mode == 'add' else q
            db.execute('UPDATE stocktake_lines SET counted_qty=?, counted_by=?, counted_at=?, counted_move_id=? WHERE id=?',
                       (newq, a['username'], now_s(), cur_move, ln['id']))
    return ok(_st_out(db, a, _st(db, mid, no)))


def _calc_diffs(db, st):
    mid = st['merchant_id']
    for ln in db.execute('SELECT * FROM stocktake_lines WHERE merchant_id=? AND st_no=?', (mid, st['st_no'])).fetchall():
        if ln['counted_qty'] is None:
            db.execute('UPDATE stocktake_lines SET moved_qty=0, diff_qty=0 WHERE id=?', (ln['id'],))
            continue
        moved = _moved(db, mid, st['wh_code'], ln['product_id'], ln['batch_id'], st['snapshot_move_id'], ln['counted_move_id'])
        db.execute('UPDATE stocktake_lines SET moved_qty=?, diff_qty=? WHERE id=?', (moved, ln['counted_qty'] - (ln['book_qty'] + moved), ln['id']))


@bp.route('/stocktakes/<no>/submit', methods=['POST'])
@require_perm('stock.take')
def stocktake_submit(no):
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        st = _st(db, mid, no)
        check_wh(db, a, st['wh_code'])
        if st['status'] != 'counting':
            raise BizError('盘点单当前「%s」，不能提交' % ST_STATUS[st['status']], 409, 'CONFLICT')
        n = db.execute('SELECT COUNT(*) FROM stocktake_lines WHERE merchant_id=? AND st_no=? AND counted_qty IS NULL', (mid, no)).fetchone()[0]
        if n and not d.get('skip_uncounted'):
            raise BizError('还有 %d 行未录入实盘数量；确认不盘请勾选「未录入的不调整」' % n)
        _calc_diffs(db, st)
        db.execute("UPDATE stocktakes SET status='submitted' WHERE id=?", (st['id'],))
        audit(db, a, 'stock', '提交盘点单', no, {'status': 'counting'}, {'status': 'submitted', 'uncounted': n})
        wh = check_wh(db, a, st['wh_code'])
        notify.notify_perm(db, mid, 'stock.take.approve', wh['store_code'], 'approval', '待审核盘点：%s' % no, ref_type='stocktake', ref_no=no,
                           link='m#stocktake/' + no, is_todo=True, exclude_user_id=a['id'])
    return ok(_st_out(db, a, _st(db, mid, no)))


@bp.route('/stocktakes/<no>/approve', methods=['POST'])
@require_perm('stock.take.approve')
def stocktake_approve(no):
    """审核后才调整库存：按差异生成 'stocktake' 流水；驳回 {reject:1, reason} 回到盘点中。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        st = _st(db, mid, no)
        check_wh(db, a, st['wh_code'])
        if st['status'] != 'submitted':
            raise BizError('盘点单当前「%s」，不能审核' % ST_STATUS[st['status']], 409, 'CONFLICT')
        if d.get('reject'):
            if not (d.get('reason') or '').strip():
                raise BizError('驳回必须填写原因')
            db.execute("UPDATE stocktakes SET status='counting' WHERE id=?", (st['id'],))
            audit(db, a, 'stock', '驳回盘点单', no, {'status': 'submitted'}, {'status': 'counting'}, d['reason'])
        else:
            _calc_diffs(db, st)
            n = 0
            for ln in db.execute('SELECT * FROM stocktake_lines WHERE merchant_id=? AND st_no=? AND counted_qty IS NOT NULL AND diff_qty!=0',
                                 (mid, no)).fetchall():
                inventory.post_move(db, mid, st['wh_code'], ln['product_id'], ln['diff_qty'], 'stocktake', no, a['username'], batch_id=ln['batch_id'],
                                    note='盘盈' if ln['diff_qty'] > 0 else '盘亏')
                n += 1
            db.execute("UPDATE stocktakes SET status='approved', approved_by=?, approved_at=? WHERE id=?", (a['username'], now_s(), st['id']))
            audit(db, a, 'stock', '审核盘点单', no, {'status': 'submitted'}, {'status': 'approved', 'adjusted_lines': n}, d.get('reason') or '',
                  level='high')
        notify.done_by_ref(db, mid, 'stocktake', no)
    return ok(_st_out(db, a, _st(db, mid, no)))


@bp.route('/stocktakes/<no>/cancel', methods=['POST'])
@require_perm('stock.take')
def stocktake_cancel(no):
    db, a = get_db(), current_actor()
    with tx(db):
        st = _st(db, a['merchant_id'], no)
        check_wh(db, a, st['wh_code'])
        if st['status'] != 'counting':
            raise BizError('只有盘点中的单可以取消', 409, 'CONFLICT')
        db.execute("UPDATE stocktakes SET status='cancelled' WHERE id=?", (st['id'],))
        audit(db, a, 'stock', '取消盘点单', no, {'status': 'counting'}, {'status': 'cancelled'}, body().get('reason') or '')
    return ok(_st_out(db, a, _st(db, a['merchant_id'], no)))


# ================================================================ 报损 / 报溢 / 调整 / 拆零 / 组装 / 质量转移（ERP-307、ERP-107、8.4）
def _adj(db, mid, no):
    r = db.execute('SELECT * FROM stock_adjusts WHERE adj_no=? AND merchant_id=?', (no, mid)).fetchone()
    if r is None:
        raise BizError('单据不存在', 404)
    return dict(r)


def _adj_out(db, a, adj, lines=True):
    x = dict(adj)
    if x['status'] == 'submitted' and x['approval_no']:
        ap = db.execute('SELECT status FROM approvals WHERE approval_no=?', (x['approval_no'],)).fetchone()
        if ap is not None and ap[0] in ('rejected', 'cancelled'):
            x['status'] = ap[0]          # 审批驳回/撤回：单据视为驳回（不会过账）
    x['kind_name'] = ADJ_KIND[x['kind']][0]
    x['status_name'] = ADJ_STATUS.get(x['status'], x['status'])
    x['total'] = cost_view(a, x.pop('total_cents'))
    if lines:
        x['lines'] = []
        for r in db.execute('SELECT l.*, p.code, p.name, p.base_unit, b.batch_no, b.expiry_date FROM stock_adjust_lines l JOIN products p ON p.id=l.product_id '
                            'LEFT JOIN inv_batches b ON b.id=l.batch_id WHERE l.merchant_id=? AND l.adj_no=? ORDER BY l.id', (adj['merchant_id'], adj['adj_no'])):
            y = dict(r)
            y['cost'] = cost_view(a, y.pop('cost_cents'))
            x['lines'].append(y)
    return x


def post_adjust(db, ap, approver):
    """审批通过回调（与审批同一事务）：按单据类型过账库存。"""
    mid = ap['merchant_id']
    adj = _adj(db, mid, ap['ref_no'])
    if adj['status'] != 'submitted':
        raise BizError('单据状态已变化', 409, 'CONFLICT')
    no, wh, kind, op = adj['adj_no'], adj['wh_code'], adj['kind'], approver['username']
    for ln in db.execute('SELECT * FROM stock_adjust_lines WHERE merchant_id=? AND adj_no=? ORDER BY id', (mid, no)).fetchall():
        pid, bid, q, note = ln['product_id'], ln['batch_id'], ln['base_qty'], ln['reason'] or adj['reason']
        if kind == 'loss':
            inventory.post_move(db, mid, wh, pid, -q, 'loss', no, op, batch_id=bid, quality=ln['quality'], note=note, cost_cents=ln['cost_cents'])
        elif kind == 'overflow':
            inventory.post_move(db, mid, wh, pid, q, 'overflow', no, op, batch_id=bid, quality=ln['quality'], note=note, cost_cents=ln['cost_cents'])
        elif kind == 'adjust':
            inventory.post_move(db, mid, wh, pid, q, 'adjust', no, op, batch_id=bid, quality=ln['quality'], note=note)
        elif kind == 'quality':
            inventory.post_move(db, mid, wh, pid, -q, 'quality', no, op, batch_id=bid, quality=ln['quality'], note=note)
            inventory.post_move(db, mid, wh, pid, q, 'quality', no, op, batch_id=bid, quality=ln['to_quality'], note=note)
        elif kind in ('split', 'assemble'):
            sign = -1 if kind == 'split' else 1
            inventory.post_move(db, mid, wh, pid, sign * q, kind, no, op, batch_id=bid, note=note)
            for c in db.execute('SELECT component_id, qty FROM product_bom WHERE merchant_id=? AND product_id=? ORDER BY id', (mid, pid)).fetchall():
                inventory.post_move(db, mid, wh, c['component_id'], -sign * c['qty'] * q, kind, no, op, note=note)
    db.execute("UPDATE stock_adjusts SET status='posted', posted_at=?, approval_no=? WHERE id=?", (now_s(), ap['approval_no'], adj['id']))
    audit(db, approver, 'stock', ADJ_KIND[kind][0] + '过账', no, {'status': 'submitted'}, {'status': 'posted'}, ap.get('decide_reason') or '', level='high')


def reject_adjust(db, ap, actor, to):
    """审批驳回 / 申请人撤回回调（与驳回同一事务）：单据状态写回 rejected / cancelled，不过账。"""
    adj = _adj(db, ap['merchant_id'], ap['ref_no'])
    if adj['status'] != 'submitted':
        return
    db.execute("UPDATE stock_adjusts SET status=? WHERE id=? AND status='submitted'", (to, adj['id']))
    audit(db, actor, 'stock', ADJ_KIND[adj['kind']][0] + ('被驳回' if to == 'rejected' else '已撤回'), adj['adj_no'], {'status': 'submitted'},
          {'status': to, 'approval_no': ap['approval_no']}, ap.get('decide_reason') or '', store_code=ap.get('store_code') or '')


for _k in ('loss', 'overflow', 'stock_adjust'):
    approval.register_handler(_k, post_adjust)
    approval.register_reject_handler(_k, reject_adjust)


@bp.route('/adjusts')
@require_perm('stock.adjust', 'stock.adjust.approve')
def adjusts():
    db, a = get_db(), current_actor()
    s, sa = _wh_sql(db, a, 'wh_code')
    q = request.args
    sql, args = 'FROM stock_adjusts WHERE merchant_id=?' + s, [a['merchant_id']] + sa
    for k in ('kind', 'status'):
        if q.get(k):
            sql += ' AND %s=?' % k
            args.append(q[k])
    return ok(page_query(db, sql, args, q, 'id DESC', conv=lambda r: _adj_out(db, a, r, False)))


@bp.route('/adjusts/<no>')
@require_perm('stock.adjust', 'stock.adjust.approve')
def adjust_get(no):
    db, a = get_db(), current_actor()
    adj = _adj(db, a['merchant_id'], no)
    check_wh(db, a, adj['wh_code'])
    return ok(_adj_out(db, a, adj))


@bp.route('/adjusts', methods=['POST'])
@require_perm('stock.adjust')
def adjust_create():
    """{wh_code, kind, reason, attachment?, request_key?, lines:[{product_id|code, batch_id? | batch_no+expiry_date(报溢新批次), qty+unit | base_qty,
    quality?, to_quality?(质量转移), reason?}]} -> 提交并生成审批单（审批人需 approval.stock），审批通过后自动过账。
    adjust 调整的 base_qty 可为负；拆零 split / 组装 assemble 的商品须设置 BOM；报损可报过期/锁定批次。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    kind = d.get('kind')
    if kind not in ADJ_KIND:
        raise BizError('单据类型只能是：报损/报溢/调整/拆零/组装/质量转移')
    reason = (d.get('reason') or '').strip()
    if not reason:
        raise BizError('请填写原因')
    name, doc, ap_kind = ADJ_KIND[kind]
    with tx(db):
        wh = check_wh(db, a, d.get('wh_code'))
        if d.get('request_key'):
            r = db.execute('SELECT adj_no FROM stock_adjusts s JOIN approvals p ON p.approval_no=s.approval_no WHERE s.merchant_id=? AND p.request_key=?',
                           (mid, d['request_key'])).fetchone()
            if r:
                x = _adj_out(db, a, _adj(db, mid, r[0]))
                x['duplicate'] = True
                return ok(x)
        no = next_no(db, mid, doc, wh['code'])
        total, rows = 0, []
        for ln in (d.get('lines') or []):
            p = resolve_product(db, mid, ln.get('product_id') or ln.get('code'))
            q = _qty(ln, mid, db, p['id'], allow_negative=(kind == 'adjust'))
            bid = inum(ln.get('batch_id'))
            quality = ln.get('quality') or 'ok'
            to_q = ln.get('to_quality') or ''
            if quality not in inventory.QUALITY or (kind == 'quality' and (to_q not in inventory.QUALITY or to_q == quality)):
                raise BizError('质量状态不合法')
            if bid:
                b = inventory.batch_row(db, mid, bid)
                if b is None or b['product_id'] != p['id']:
                    raise BizError('批次不存在')
            elif kind == 'overflow' and ln.get('batch_no'):
                bid = inventory.get_or_create_batch(db, mid, p['id'], ln['batch_no'], ln.get('expiry_date', ''), ln.get('prod_date', ''), src_no=no)
            if p.get('batch_managed') and not bid and kind in ('loss', 'overflow', 'quality'):
                raise BizError('%s 是批次商品，请选择批次（报溢新批次填批号和有效期）' % p['name'])
            if kind in ('split', 'assemble'):
                if p.get('combo_mode') == 'components':
                    raise BizError('%s 按组件扣库存、没有自身库存，不需要拆零/组装' % p['name'])
                if not db.execute('SELECT 1 FROM product_bom WHERE merchant_id=? AND product_id=?', (mid, p['id'])).fetchone():
                    raise BizError('%s 没有设置组件（BOM），不能拆零/组装' % p['name'])
            if kind in ('loss', 'quality', 'split') or (kind == 'adjust' and q < 0):
                have = inventory.qty_of(db, mid, wh['code'], p['id'], bid, quality)
                if have < abs(q):
                    raise BizError('%s 库存不足（现有 %d，需要 %d）' % (p['name'], have, abs(q)), 409, 'STOCK_SHORT')
            cost = (p.get('cost_cents') or 0) * abs(q)
            total += cost
            rows.append((mid, no, p['id'], bid, quality, to_q, q, cost, (ln.get('reason') or '')[:100]))
        if not rows:
            raise BizError('至少要有一行商品')
        for r in rows:
            db.execute('INSERT INTO stock_adjust_lines(merchant_id, adj_no, product_id, batch_id, quality, to_quality, base_qty, cost_cents, reason) '
                       'VALUES(?,?,?,?,?,?,?,?,?)', r)
        db.execute("INSERT INTO stock_adjusts(adj_no, merchant_id, wh_code, kind, status, reason, attachment, total_cents, created_by, created_at) "
                   "VALUES(?,?,?,?,'submitted',?,?,?,?,?)", (no, mid, wh['code'], kind, reason[:200], (d.get('attachment') or '')[:300], total,
                                                            a['username'], now_s()))
        ap = approval.create(db, a, ap_kind, wh['store_code'], '%s %s（%d 行）' % (name, wh['name'], len(rows)), ref_no=no, ref_type='stock_adjust',
                             payload={'adj_no': no, 'kind': kind}, reason=reason, need_perm='approval.stock', request_key=d.get('request_key'),
                             amount_cents=total)
        db.execute('UPDATE stock_adjusts SET approval_no=? WHERE adj_no=?', (ap['approval_no'], no))
        audit(db, a, 'stock', '提交' + name, no, None, {'lines': len(rows), 'approval_no': ap['approval_no']}, reason, store_code=wh['store_code'])
    return ok(_adj_out(db, a, _adj(db, mid, no)))


# ================================================================ 预警（ERP-308、BABY-101/102）
@bp.route('/alerts')
@require_perm('stock.view')
def alerts_api():
    """{wh?, kind?} -> {out, low, slow, near_expiry(level 30/60/90…), expired}；阈值按 商品 > 分类 > 商户 规则（/api/stock/alert-rules）。"""
    db, a = get_db(), current_actor()
    whs = _req_whs(db, a, request.args.get('wh') or request.args.get('store'))
    kinds = tuple(k for k in (request.args.get('kind') or 'low,out,slow,near_expiry,expired').split(',') if k)
    data = inventory.alerts(db, a['merchant_id'], whs, kinds=kinds)
    data['counts'] = dict((k, len(v)) for k, v in data.items())
    return ok(data)


@bp.route('/alerts/scan', methods=['POST'])
@require_perm('stock.adjust.approve', 'stock.transfer.approve')
def alerts_scan():
    """把预警推送成消息（同一天同一条去重），也可由定时任务调用 scan_alerts()。"""
    db, a = get_db(), current_actor()
    with tx(db):
        n = scan_alerts(db, a['merchant_id'], wh_scope(db, a))
    return ok({'notified': n})


def scan_alerts(db, mid, whs=None):
    """定时任务入口：python -c "from shouyin_app.views_stock import scan_alerts" 或 cron 调用（调用方开事务）。"""
    if whs is None:
        whs = [r[0] for r in db.execute("SELECT code FROM warehouses WHERE merchant_id=? AND status='启用'", (mid,))]
    day = today_s()
    store_of = dict((r[0], r[1]) for r in db.execute('SELECT code, store_code FROM warehouses WHERE merchant_id=?', (mid,)))
    names = {'out': ('stock_low', '缺货'), 'low': ('stock_low', '低于安全库存'), 'near_expiry': ('near_expiry', '临期'), 'expired': ('expired', '已过期')}
    n = 0
    for kind, rows in inventory.alerts(db, mid, whs, kinds=tuple(names)).items():
        cat, label = names[kind]
        for r in rows:
            extra = (' %d 天内（剩 %d 天）批次 %s' % (r['level'], r['days_left'], r['batch_no'])) if kind == 'near_expiry' else \
                (' 批次 %s' % r['batch_no'] if kind == 'expired' else ' 可售 %s' % r['qty'])
            n += notify.notify_perm(db, mid, 'stock.view', store_of.get(r['wh_code'], ''), cat, '%s：%s %s' % (label, r['wh_code'], r['name']), extra.strip(),
                                    ref_type='stock_alert', ref_no='%s:%s' % (r['wh_code'], r['product_id']),
                                    dedup_key='%s:%s:%s:%s:%s' % (kind, r['wh_code'], r['product_id'], r.get('batch_id', 0), day),
                                    level='important' if kind in ('expired', 'out') else 'normal')
    return n


@bp.route('/alert-rules')
@require_perm('stock.view')
def alert_rules():
    db, a = get_db(), current_actor()
    return ok([dict(r) for r in db.execute('SELECT * FROM stock_alert_rules WHERE merchant_id=? ORDER BY target_type, target_id', (a['merchant_id'],))])


@bp.route('/alert-rules', methods=['POST'])
@require_perm('goods.edit')
def alert_rules_save():
    """{target_type:'merchant'|'category'|'product', target_id, safety_qty?, near_expiry_days:'30,60,90', slow_days?, receivers?}"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    tt = d.get('target_type') or 'merchant'
    if tt not in ('merchant', 'category', 'product'):
        raise BizError('规则对象只能是 商户/分类/商品')
    tid = 0 if tt == 'merchant' else inum(d.get('target_id'))
    if tt == 'category' and not db.execute('SELECT 1 FROM categories WHERE id=? AND merchant_id=?', (tid, mid)).fetchone():
        raise BizError('分类不存在', 404)
    if tt == 'product' and not db.execute('SELECT 1 FROM products WHERE id=? AND merchant_id=?', (tid, mid)).fetchone():
        raise BizError('商品不存在', 404)
    lv = ','.join(str(int(x)) for x in str(d.get('near_expiry_days') or '').replace('，', ',').split(',') if x.strip().isdigit())
    safety = d.get('safety_qty')
    with tx(db):
        db.execute('INSERT INTO stock_alert_rules(merchant_id, target_type, target_id, safety_qty, near_expiry_days, slow_days, receivers) VALUES(?,?,?,?,?,?,?) '
                   'ON CONFLICT(merchant_id, target_type, target_id) DO UPDATE SET safety_qty=excluded.safety_qty, near_expiry_days=excluded.near_expiry_days, '
                   'slow_days=excluded.slow_days, receivers=excluded.receivers',
                   (mid, tt, tid, inum(safety) if safety not in (None, '') else None, lv, inum(d.get('slow_days')) or None, (d.get('receivers') or '')[:200]))
        audit(db, a, 'stock', '预警规则', '%s:%s' % (tt, tid), None, d)
    return alert_rules.__wrapped__()


# ================================================================ 期初库存导入（DATA-003）：形成期初入库单，不直接改余额
OPENING_COLS = [
    xlsx_io.Col('wh_code', '仓库编码', required=True, note='门店仓编码 = 门店编码'), xlsx_io.Col('code', '商品编码或条码', required=True, aliases=('商品编码', '条码')),
    xlsx_io.Col('qty', '数量', kind='int', required=True), xlsx_io.Col('unit', '单位', note='空 = 基本单位；可填 箱 等'),
    xlsx_io.Col('batch_no', '批号'), xlsx_io.Col('prod_date', '生产日期', kind='date'), xlsx_io.Col('expiry_date', '有效期', kind='date'),
    xlsx_io.Col('cost', '成本价', kind='money', note='按所填单位'),
]


@bp.route('/opening/template')
@require_perm('stock.adjust')
def opening_template():
    return xlsx_io.send_template(OPENING_COLS, '期初库存导入模板.xlsx',
                                 [{'wh_code': 'A001', 'code': '6900000000011', 'qty': 12, 'unit': '', 'batch_no': 'B20260901', 'prod_date': '2026-09-01',
                                   'expiry_date': '2028-08-31', 'cost': 300}])


@bp.route('/opening/import', methods=['POST'])
@require_perm('stock.adjust')
def opening_import():
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    rows, errors = xlsx_io.read_rows(request.files.get('file'), OPENING_COLS)
    good = {}
    for r in rows:
        x, errs = r['data'], []
        try:
            check_wh(db, a, x['wh_code'])
            from .goods_svc import find_sku
            f = find_sku(db, mid, x['code'])
            if f is None:
                raise BizError('商品不存在：%s' % x['code'])
            p = f['product']
            unit = (x.get('unit') or '').strip() or (f['unit'] if f['barcode'] else base_unit_of(p))
            rate = unit_rate(db, mid, p['id'], unit)
            if x['qty'] <= 0:
                raise BizError('数量必须大于 0')
            if p['batch_managed'] and not (x.get('batch_no') and x.get('expiry_date')):
                raise BizError('%s 是批次商品，必须填写批号和有效期' % p['name'])
            if x.get('prod_date') and x.get('expiry_date') and x['prod_date'] >= x['expiry_date']:
                raise BizError('生产日期必须早于有效期')
            good.setdefault(x['wh_code'], []).append(dict(row_no=r['row_no'], product_id=p['id'], base_qty=x['qty'] * rate, unit=unit, unit_qty=x['qty'],
                                                          rate=rate, batch_no=x.get('batch_no') or '', prod_date=x.get('prod_date') or '',
                                                          expiry_date=x.get('expiry_date') or '', cost=x.get('cost')))
        except BizError as e:
            errs.append(e.msg)
        if errs:
            errors.append({'row_no': r['row_no'], 'name': x.get('code') or '', 'errors': errs})
    docs = []
    for wh, lines in good.items():
        with tx(db):
            no = next_no(db, mid, 'GR', wh)
            total = 0
            for ln in lines:
                alloc = inventory.stock_in(db, a, mid, wh, [dict(ln, cost_cents=((ln['cost'] or 0) + ln['rate'] // 2) // ln['rate'], note='期初导入')],
                                           'opening', no)
                db.execute('INSERT INTO goods_receipt_lines(merchant_id, gr_no, product_id, unit, rate, qty, base_qty, price_cents, batch_id, batch_no, '
                           'prod_date, expiry_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (mid, no, ln['product_id'], ln['unit'], ln['rate'], ln['unit_qty'], ln['base_qty'], ln['cost'] or 0, alloc[0][0]['batch_id'],
                            ln['batch_no'], ln['prod_date'], ln['expiry_date']))
                total += (ln['cost'] or 0) * ln['unit_qty']
            db.execute("INSERT INTO goods_receipts(gr_no, merchant_id, wh_code, kind, status, total_cents, remark, created_by, created_at, posted_at) "
                       "VALUES(?,?,?,'opening','posted',?,?,?,?,?)", (no, mid, wh, total, '期初库存导入', a['username'], now_s(), now_s()))
            audit(db, a, 'stock', '期初库存导入', no, None, {'wh': wh, 'lines': len(lines)}, level='high')
            docs.append(no)
    res = xlsx_io.import_result(sum(len(v) for v in good.values()), 0, errors)
    res['docs'] = docs
    return ok(res)
