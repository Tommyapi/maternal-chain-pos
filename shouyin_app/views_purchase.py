# -*- coding: utf-8 -*-
"""供应商与采购：供应商档案/采购单/收货入库/采购退货/历史采购价。

Owner：模块①（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-201~206（ERP-207 应付结算为 P1，不做；保留采购金额与导出数据）
状态机：采购单 draft -> submitted -> approved -> partial -> received -> closed；draft/submitted（或已审核未收货）-> cancelled
        收货单 过账即 posted（stock_in 'purchase'）；采购退货 submitted -> posted（purchase.approve 审核过账）| cancelled
"""
from datetime import datetime, timedelta

from flask import Blueprint, request

from . import inventory
from .core import get_db, tx, require_perm, current_actor, has_perm, audit, ok
from .goods_svc import body, page_query, wh_scope, check_wh, resolve_product, unit_rate, base_unit_of
from .money import to_cents, yuan, MoneyError
from .numbering import next_no
from .util import BizError, now_s, inum, today_s

bp = Blueprint('purchase', __name__, url_prefix='/api/purchase')

PO_STATUS = {'draft': '草稿', 'submitted': '待审核', 'approved': '已审核', 'partial': '部分到货', 'received': '已到货', 'closed': '已关闭',
             'cancelled': '已取消'}
PR_STATUS = {'draft': '草稿', 'submitted': '待审核', 'approved': '已审核', 'posted': '已出库', 'cancelled': '已取消'}


def _int_qty(v, label='数量', allow_zero=False):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise BizError('%s格式不正确' % label)
    if f != int(f):
        raise BizError('%s必须是整数（按所选单位）' % label)
    q = int(f)
    if q < 0 or (q == 0 and not allow_zero):
        raise BizError('%s必须大于 0' % label)
    return q


def _price(v, label='采购价'):
    if v is None or v == '':
        return 0
    try:
        c = to_cents(v)
    except MoneyError:
        raise BizError('%s格式不正确' % label)
    if c < 0:
        raise BizError('%s不能为负' % label)
    return c


def _wh_in_scope_sql(db, actor, col):
    whs = wh_scope(db, actor)
    if not whs:
        return ' AND 1=0', []
    return ' AND %s IN (%s)' % (col, ','.join('?' * len(whs))), whs


# ================================================================ 供应商（ERP-201）
def _supplier(db, mid, sid):
    r = db.execute('SELECT * FROM suppliers WHERE id=? AND merchant_id=?', (inum(sid), mid)).fetchone()
    if r is None:
        raise BizError('供应商不存在', 404)
    return dict(r)


@bp.route('/suppliers')
@require_perm('supplier.view', 'purchase.view')
def suppliers():
    db, a = get_db(), current_actor()
    q = request.args
    sql, args = 'FROM suppliers WHERE merchant_id=?', [a['merchant_id']]
    if q.get('kw'):
        sql += ' AND (name LIKE ? OR code LIKE ? OR contact LIKE ?)'
        args += ['%' + q['kw'] + '%'] * 3
    if q.get('status'):
        sql += ' AND status=?'
        args.append(q['status'])
    return ok(page_query(db, sql, args, q, 'code'))


@bp.route('/suppliers', methods=['POST'])
@require_perm('supplier.edit')
def suppliers_save():
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    name = (d.get('name') or '').strip()
    if not name:
        raise BizError('请填写供应商名称')
    status = d.get('status') if d.get('status') in ('启用', '停用') else None
    with tx(db):
        sid = inum(d.get('id'))
        fields = dict(name=name[:100], contact=(d.get('contact') or '')[:40], phone=(d.get('phone') or '')[:40],
                      address=(d.get('address') or '')[:200], settle_info=(d.get('settle_info') or '')[:500])
        if sid:
            old = _supplier(db, mid, sid)
            if status:
                fields['status'] = status
            db.execute('UPDATE suppliers SET %s WHERE id=?' % ','.join('%s=?' % k for k in fields), list(fields.values()) + [sid])
            audit(db, a, 'purchase', '修改供应商', old['code'], dict((k, old.get(k)) for k in fields), fields)
        else:
            code = (d.get('code') or '').strip()
            if not code:
                n = db.execute('SELECT COUNT(*) FROM suppliers WHERE merchant_id=?', (mid,)).fetchone()[0] + 1
                code = 'GYS%04d' % n
                while db.execute('SELECT 1 FROM suppliers WHERE merchant_id=? AND code=?', (mid, code)).fetchone():
                    n += 1
                    code = 'GYS%04d' % n
            if db.execute('SELECT 1 FROM suppliers WHERE merchant_id=? AND code=?', (mid, code)).fetchone():
                raise BizError('供应商编码已存在：%s' % code)
            sid = db.execute('INSERT INTO suppliers(merchant_id, code, status, created_at, %s) VALUES(?,?,?,?,%s)'
                             % (','.join(fields), ','.join('?' * len(fields))), [mid, code, status or '启用', now_s()] + list(fields.values())).lastrowid
            audit(db, a, 'purchase', '新增供应商', code, None, fields)
    return ok(_supplier(db, mid, sid))


# ================================================================ 采购单（ERP-202）
def _po(db, mid, no):
    r = db.execute('SELECT * FROM purchase_orders WHERE po_no=? AND merchant_id=?', (no, mid)).fetchone()
    if r is None:
        raise BizError('采购单不存在', 404)
    return dict(r)


def _po_out(db, actor, po, lines=True):
    d = dict(po)
    d['status_name'] = PO_STATUS.get(d['status'], d['status'])
    d['total'] = yuan(d.pop('total_cents'))
    s = db.execute('SELECT name FROM suppliers WHERE id=?', (d['supplier_id'],)).fetchone()
    d['supplier_name'] = s[0] if s else ''
    if lines:
        d['lines'] = []
        for r in db.execute('SELECT l.*, p.code, p.name, p.base_unit FROM purchase_lines l JOIN products p ON p.id=l.product_id '
                            'WHERE l.merchant_id=? AND l.po_no=? ORDER BY l.line_no', (po['merchant_id'], po['po_no'])):
            x = dict(r)
            x['price'] = yuan(x.pop('price_cents'))
            x['amount'] = yuan(x.pop('amount_cents'))
            x['order_base_qty'] = x['qty'] * x['rate']
            x['remain_base_qty'] = max(0, x['order_base_qty'] - x['received_base_qty'] - x['rejected_base_qty'])
            d['lines'].append(x)
        d['receipts'] = [r[0] for r in db.execute('SELECT gr_no FROM goods_receipts WHERE merchant_id=? AND po_no=? ORDER BY id',
                                                   (po['merchant_id'], po['po_no']))]
    return d


def _po_lines(db, mid, lines):
    out = []
    for i, ln in enumerate(lines or [], start=1):
        p = resolve_product(db, mid, ln.get('product_id') or ln.get('code'))
        unit = (ln.get('unit') or '').strip() or base_unit_of(p)
        rate = unit_rate(db, mid, p['id'], unit)
        qty = _int_qty(ln.get('qty'))
        price = _price(ln.get('price'))
        out.append(dict(line_no=i, product_id=p['id'], unit=unit, rate=rate, qty=qty, price_cents=price, amount_cents=price * qty,
                        remark=(ln.get('remark') or '')[:100]))
    if not out:
        raise BizError('采购单至少要有一行商品')
    return out


@bp.route('/orders')
@require_perm('purchase.view')
def orders():
    db, a = get_db(), current_actor()
    q = request.args
    sql, args = 'FROM purchase_orders WHERE merchant_id=?', [a['merchant_id']]
    s, sa = _wh_in_scope_sql(db, a, 'wh_code')
    sql, args = sql + s, args + sa
    for k in ('status', 'supplier_id', 'wh_code'):
        if q.get(k):
            sql += ' AND %s=?' % k
            args.append(q[k])
    if q.get('kw'):
        sql += ' AND po_no LIKE ?'
        args.append('%' + q['kw'] + '%')
    if q.get('start'):
        sql += ' AND created_at >= ?'
        args.append(q['start'])
    if q.get('end'):
        sql += ' AND created_at <= ?'
        args.append(q['end'] + ' 23:59:59')
    return ok(page_query(db, sql, args, q, 'id DESC', conv=lambda r: _po_out(db, a, r, False)))


@bp.route('/orders/<no>')
@require_perm('purchase.view')
def order_get(no):
    db, a = get_db(), current_actor()
    po = _po(db, a['merchant_id'], no)
    check_wh(db, a, po['wh_code'], '收货仓库')
    return ok(_po_out(db, a, po))


@bp.route('/orders', methods=['POST'])
@require_perm('purchase.edit')
def orders_save():
    """新建 / 修改草稿：{po_no?, supplier_id, wh_code, expect_date, remark, lines:[{product_id|code, unit, qty, price}], submit?}"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        sup = _supplier(db, mid, d.get('supplier_id'))
        if sup['status'] != '启用':
            raise BizError('供应商已停用，不能新建采购单')
        wh = check_wh(db, a, d.get('wh_code'), '收货仓库')
        lines = _po_lines(db, mid, d.get('lines'))
        total = sum(x['amount_cents'] for x in lines)
        no = (d.get('po_no') or '').strip()
        if no:
            po = _po(db, mid, no)
            if po['status'] != 'draft':
                raise BizError('只有草稿可以修改（当前：%s）' % PO_STATUS[po['status']], 409, 'CONFLICT')
            db.execute('UPDATE purchase_orders SET supplier_id=?, wh_code=?, expect_date=?, remark=?, total_cents=?, updated_at=? WHERE id=?',
                       (sup['id'], wh['code'], (d.get('expect_date') or '')[:10], (d.get('remark') or '')[:200], total, now_s(), po['id']))
            db.execute('DELETE FROM purchase_lines WHERE merchant_id=? AND po_no=?', (mid, no))
            act = '修改采购单'
        else:
            no = next_no(db, mid, 'PO', wh['code'])
            db.execute('INSERT INTO purchase_orders(po_no, merchant_id, supplier_id, wh_code, expect_date, status, total_cents, remark, created_by, '
                       'created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (no, mid, sup['id'], wh['code'], (d.get('expect_date') or '')[:10], 'draft', total, (d.get('remark') or '')[:200],
                        a['username'], now_s(), now_s()))
            act = '新建采购单'
        for x in lines:
            db.execute('INSERT INTO purchase_lines(merchant_id, po_no, line_no, product_id, unit, rate, qty, price_cents, amount_cents, remark) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?)', (mid, no, x['line_no'], x['product_id'], x['unit'], x['rate'], x['qty'], x['price_cents'],
                                                       x['amount_cents'], x['remark']))
        audit(db, a, 'purchase', act, no, None, {'total_cents': total, 'lines': len(lines)}, store_code=wh['store_code'])
        if d.get('submit'):
            _po_transit(db, a, no, ('draft',), 'submitted', '提交采购单')
    return ok(_po_out(db, a, _po(db, mid, no)))


def _po_transit(db, a, no, frm, to, action, reason=''):
    po = _po(db, a['merchant_id'], no)
    check_wh(db, a, po['wh_code'], '收货仓库')
    if po['status'] not in frm:
        raise BizError('采购单当前「%s」，不能%s' % (PO_STATUS[po['status']], action), 409, 'CONFLICT')
    extra = ', approved_by=?, approved_at=?' if to == 'approved' else ''
    args = [to, now_s()] + ([a['username'], now_s()] if to == 'approved' else []) + [po['id'], po['status']]
    cur = db.execute('UPDATE purchase_orders SET status=?, updated_at=?%s WHERE id=? AND status=?' % extra, args)
    if cur.rowcount != 1:
        raise BizError('采购单状态已变化，请刷新', 409, 'CONFLICT')
    audit(db, a, 'purchase', action, no, {'status': po['status']}, {'status': to}, reason, store_code=None)
    return po


@bp.route('/orders/<no>/submit', methods=['POST'])
@require_perm('purchase.edit')
def order_submit(no):
    db, a = get_db(), current_actor()
    with tx(db):
        _po_transit(db, a, no, ('draft',), 'submitted', '提交采购单')
    return ok(_po_out(db, a, _po(db, a['merchant_id'], no)))


@bp.route('/orders/<no>/approve', methods=['POST'])
@require_perm('purchase.approve')
def order_approve(no):
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        if d.get('reject'):
            if not (d.get('reason') or '').strip():
                raise BizError('驳回必须填写原因')
            _po_transit(db, a, no, ('submitted',), 'draft', '驳回采购单', d.get('reason'))
        else:
            _po_transit(db, a, no, ('submitted',), 'approved', '审核采购单', d.get('reason') or '')
    return ok(_po_out(db, a, _po(db, a['merchant_id'], no)))


@bp.route('/orders/<no>/cancel', methods=['POST'])
@require_perm('purchase.edit')
def order_cancel(no):
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        po = _po(db, a['merchant_id'], no)
        if po['status'] == 'approved' and db.execute('SELECT 1 FROM goods_receipts WHERE merchant_id=? AND po_no=?', (a['merchant_id'], no)).fetchone():
            raise BizError('已有收货记录，不能取消，请用「关闭」')
        _po_transit(db, a, no, ('draft', 'submitted', 'approved'), 'cancelled', '取消采购单', d.get('reason') or '')
    return ok(_po_out(db, a, _po(db, a['merchant_id'], no)))


@bp.route('/orders/<no>/close', methods=['POST'])
@require_perm('purchase.edit', 'purchase.approve')
def order_close(no):
    """部分到货后不再等剩余：关闭（剩余数量作废，记录原因）。"""
    db, a = get_db(), current_actor()
    d = body()
    if not (d.get('reason') or '').strip():
        raise BizError('关闭采购单必须填写原因')
    with tx(db):
        _po_transit(db, a, no, ('partial', 'approved'), 'closed', '关闭采购单', d.get('reason'))
    return ok(_po_out(db, a, _po(db, a['merchant_id'], no)))


# ================================================================ 收货入库（ERP-203/204）
def _batch_dates(p, ln):
    """批次商品：批号、有效期必填；生产日期不晚于今天、早于有效期；有保质期且只填生产日期时自动算有效期。"""
    bno = (ln.get('batch_no') or '').strip()
    prod = (ln.get('prod_date') or '').strip()[:10]
    exp = (ln.get('expiry_date') or '').strip()[:10]
    for x, label in ((prod, '生产日期'), (exp, '有效期')):
        if x:
            try:
                datetime.strptime(x, '%Y-%m-%d')
            except ValueError:
                raise BizError('%s格式应为 YYYY-MM-DD：%s' % (label, x))
    if not p.get('batch_managed'):
        return bno, prod, exp
    if not bno:
        raise BizError('%s 是批次商品，收货必须填写生产批号' % p['name'])
    if not exp and prod and p.get('shelf_life_days'):
        exp = (datetime.strptime(prod, '%Y-%m-%d') + timedelta(days=int(p['shelf_life_days']))).strftime('%Y-%m-%d')
    if not exp:
        raise BizError('%s 是批次商品，收货必须填写有效期（或生产日期 + 商品保质期）' % p['name'])
    if prod and prod > today_s():
        raise BizError('%s 生产日期 %s 晚于今天' % (p['name'], prod))
    if prod and prod >= exp:
        raise BizError('%s 生产日期必须早于有效期' % p['name'])
    if exp < today_s():
        raise BizError('%s 批次 %s 已过期（有效期 %s），不能入库' % (p['name'], bno, exp))
    return bno, prod, exp


@bp.route('/receipts')
@require_perm('purchase.view', 'purchase.receive')
def receipts():
    db, a = get_db(), current_actor()
    q = request.args
    sql, args = 'FROM goods_receipts WHERE merchant_id=?', [a['merchant_id']]
    s, sa = _wh_in_scope_sql(db, a, 'wh_code')
    sql, args = sql + s, args + sa
    for k in ('po_no', 'kind', 'wh_code'):
        if q.get(k):
            sql += ' AND %s=?' % k
            args.append(q[k])

    def conv(r):
        x = dict(r)
        x['total'] = yuan(x.pop('total_cents')) if has_perm(a, 'goods.cost.view') else None
        return x
    return ok(page_query(db, sql, args, q, 'id DESC', conv=conv))


@bp.route('/receipts/<no>')
@require_perm('purchase.view', 'purchase.receive')
def receipt_get(no):
    db, a = get_db(), current_actor()
    r = db.execute('SELECT * FROM goods_receipts WHERE gr_no=? AND merchant_id=?', (no, a['merchant_id'])).fetchone()
    if r is None:
        raise BizError('收货单不存在', 404)
    check_wh(db, a, r['wh_code'])
    d = dict(r)
    cost = has_perm(a, 'goods.cost.view')
    d['total'] = yuan(d.pop('total_cents')) if cost else None
    d['lines'] = []
    for x in db.execute('SELECT l.*, p.code, p.name FROM goods_receipt_lines l JOIN products p ON p.id=l.product_id WHERE l.merchant_id=? AND l.gr_no=? '
                        'ORDER BY l.id', (a['merchant_id'], no)):
        x = dict(x)
        pc = x.pop('price_cents')
        x['price'] = yuan(pc) if cost else None
        d['lines'].append(x)
    return ok(d)


@bp.route('/receipts', methods=['POST'])
@require_perm('purchase.receive')
def receipts_create():
    """收货并过账入库：{po_no?, supplier_id?, wh_code?, remark, lines:[{po_line_id | product_id, unit, qty, rejected_qty, price?,
    batch_no, prod_date, expiry_date, diff_reason}]}。部分到货/多次收货/拒收/差异原因；批次商品校验批号与日期。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        po = None
        if d.get('po_no'):
            po = _po(db, mid, d['po_no'])
            if po['status'] not in ('approved', 'partial'):
                raise BizError('采购单当前「%s」，不能收货（须已审核）' % PO_STATUS[po['status']], 409, 'CONFLICT')
            wh = check_wh(db, a, po['wh_code'], '收货仓库')
            sup = _supplier(db, mid, po['supplier_id'])
        else:
            wh = check_wh(db, a, d.get('wh_code'), '收货仓库')
            sup = _supplier(db, mid, d.get('supplier_id'))
        gr_no = next_no(db, mid, 'GR', wh['code'])
        rows, total, n_lines = [], 0, 0
        for ln in (d.get('lines') or []):
            pl = None
            if po is not None:
                pl = db.execute('SELECT * FROM purchase_lines WHERE id=? AND merchant_id=? AND po_no=?',
                                (inum(ln.get('po_line_id')), mid, po['po_no'])).fetchone()
                if pl is None:
                    raise BizError('收货行不属于该采购单')
                p = resolve_product(db, mid, pl['product_id'])
                unit, rate, price = pl['unit'], pl['rate'], pl['price_cents']
            else:
                p = resolve_product(db, mid, ln.get('product_id') or ln.get('code'))
                unit = (ln.get('unit') or '').strip() or base_unit_of(p)
                rate = unit_rate(db, mid, p['id'], unit)
                price = _price(ln.get('price'))
            qty = _int_qty(ln.get('qty') or 0, '实收数量', allow_zero=True)
            rej = _int_qty(ln.get('rejected_qty') or 0, '拒收数量', allow_zero=True)
            if qty == 0 and rej == 0:
                continue
            reason = (ln.get('diff_reason') or '').strip()
            if rej and not reason:
                raise BizError('%s 有拒收，必须填写拒收/差异原因' % p['name'])
            base, rej_base = inventory.to_base_qty(qty, rate), inventory.to_base_qty(rej, rate)
            if pl is not None:
                remain = pl['qty'] * pl['rate'] - pl['received_base_qty'] - pl['rejected_base_qty']
                if base + rej_base > remain:
                    raise BizError('%s 本次收货 %d + 拒收 %d 超过未到货数量 %d（基本单位）' % (p['name'], base, rej_base, remain))
                db.execute('UPDATE purchase_lines SET received_base_qty=received_base_qty+?, rejected_base_qty=rejected_base_qty+? WHERE id=?',
                           (base, rej_base, pl['id']))
            bno, prod, exp = _batch_dates(p, ln) if qty else ('', '', '')
            bid = 0
            if qty:
                alloc = inventory.stock_in(db, a, mid, wh['code'], [dict(
                    product_id=p['id'], base_qty=base, batch_no=bno if p.get('batch_managed') or bno else '', expiry_date=exp, prod_date=prod,
                    supplier_id=sup['id'], unit=unit, unit_qty=qty, rate=rate, cost_cents=(price + rate // 2) // rate,
                    note=reason)], 'purchase', gr_no)
                bid = alloc[0][0]['batch_id']
                db.execute('INSERT INTO supplier_prices(merchant_id, supplier_id, product_id, unit, rate, price_cents, src_no, created_at) '
                           'VALUES(?,?,?,?,?,?,?,?)', (mid, sup['id'], p['id'], unit, rate, price, gr_no, now_s()))
                new_cost = (price + rate // 2) // rate
                if price and new_cost != (p.get('cost_cents') or 0):
                    # 最新采购成本（基本单位）；历史销售订单保存的是成交时成本快照，不回写（ERP-206）
                    db.execute('UPDATE products SET cost_cents=?, updated_at=? WHERE id=?', (new_cost, now_s(), p['id']))
                    audit(db, a, 'goods', '采购成本更新', p['code'], {'cost_cents': p.get('cost_cents')}, {'cost_cents': new_cost}, gr_no)
            db.execute('INSERT INTO goods_receipt_lines(merchant_id, gr_no, po_line_id, product_id, unit, rate, qty, rejected_qty, base_qty, price_cents, '
                       'batch_id, batch_no, prod_date, expiry_date, diff_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (mid, gr_no, pl['id'] if pl is not None else None, p['id'], unit, rate, qty, rej, base, price, bid, bno, prod, exp, reason))
            total += price * qty
            n_lines += 1
        if not n_lines:
            raise BizError('没有填写收货数量')
        db.execute("INSERT INTO goods_receipts(gr_no, merchant_id, po_no, supplier_id, wh_code, kind, status, total_cents, remark, created_by, created_at, "
                   "posted_at) VALUES(?,?,?,?,?,'purchase','posted',?,?,?,?,?)",
                   (gr_no, mid, po['po_no'] if po else '', sup['id'], wh['code'], total, (d.get('remark') or '')[:200], a['username'], now_s(), now_s()))
        if po is not None:
            left = db.execute('SELECT COUNT(*) FROM purchase_lines WHERE merchant_id=? AND po_no=? AND qty*rate > received_base_qty + rejected_base_qty',
                              (mid, po['po_no'])).fetchone()[0]
            to = 'partial' if left else 'received'
            db.execute('UPDATE purchase_orders SET status=?, updated_at=? WHERE id=?', (to, now_s(), po['id']))
            if to != po['status']:
                audit(db, a, 'purchase', '采购到货', po['po_no'], {'status': po['status']}, {'status': to}, gr_no)
        audit(db, a, 'purchase', '采购收货过账', gr_no, None, {'po_no': po['po_no'] if po else '', 'lines': n_lines}, store_code=wh['store_code'])
    return receipt_get.__wrapped__(gr_no)


# ================================================================ 采购退货（ERP-205）
def _pr(db, mid, no):
    r = db.execute('SELECT * FROM purchase_returns WHERE pr_no=? AND merchant_id=?', (no, mid)).fetchone()
    if r is None:
        raise BizError('采购退货单不存在', 404)
    return dict(r)


def _pr_out(db, a, pr):
    d = dict(pr)
    d['status_name'] = PR_STATUS.get(d['status'], d['status'])
    d['total'] = yuan(d.pop('total_cents'))
    d['lines'] = []
    for x in db.execute('SELECT l.*, p.code, p.name, b.batch_no, b.expiry_date FROM purchase_return_lines l JOIN products p ON p.id=l.product_id '
                        'LEFT JOIN inv_batches b ON b.id=l.batch_id WHERE l.merchant_id=? AND l.pr_no=? ORDER BY l.id', (pr['merchant_id'], pr['pr_no'])):
        x = dict(x)
        x['price'] = yuan(x.pop('price_cents'))
        d['lines'].append(x)
    return d


@bp.route('/returns')
@require_perm('purchase.return', 'purchase.view')
def returns():
    db, a = get_db(), current_actor()
    q = request.args
    sql, args = 'FROM purchase_returns WHERE merchant_id=?', [a['merchant_id']]
    s, sa = _wh_in_scope_sql(db, a, 'wh_code')
    sql, args = sql + s, args + sa
    if q.get('status'):
        sql += ' AND status=?'
        args.append(q['status'])
    return ok(page_query(db, sql, args, q, 'id DESC', conv=lambda r: _pr_out(db, a, r)))


@bp.route('/returns', methods=['POST'])
@require_perm('purchase.return')
def returns_create():
    """{gr_no?|po_no?, supplier_id?, wh_code?, reason, lines:[{product_id|code, unit, qty, price?, batch_id?, quality}]} -> 待审核。
    关联原收货单时：同商品（同批次）累计退货不超过该单实收。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    reason = (d.get('reason') or '').strip()
    if not reason:
        raise BizError('请填写退货原因')
    with tx(db):
        gr = None
        if d.get('gr_no'):
            gr = db.execute('SELECT * FROM goods_receipts WHERE gr_no=? AND merchant_id=?', (d['gr_no'], mid)).fetchone()
            if gr is None:
                raise BizError('收货单不存在', 404)
            wh = check_wh(db, a, gr['wh_code'])
            sup = _supplier(db, mid, gr['supplier_id'])
        else:
            wh = check_wh(db, a, d.get('wh_code'))
            sup = _supplier(db, mid, d.get('supplier_id'))
        no = next_no(db, mid, 'PR', wh['code'])
        total, n = 0, 0
        for ln in (d.get('lines') or []):
            p = resolve_product(db, mid, ln.get('product_id') or ln.get('code'))
            unit = (ln.get('unit') or '').strip() or base_unit_of(p)
            rate = unit_rate(db, mid, p['id'], unit)
            qty = _int_qty(ln.get('qty'))
            base = inventory.to_base_qty(qty, rate)
            bid = inum(ln.get('batch_id'))
            if bid and inventory.batch_row(db, mid, bid) is None:
                raise BizError('批次不存在')
            quality = ln.get('quality') or 'ok'
            if quality not in inventory.QUALITY:
                raise BizError('质量状态不合法')
            price = _price(ln.get('price')) if ln.get('price') not in (None, '') else None
            if gr is not None:
                cond, args = 'merchant_id=? AND gr_no=? AND product_id=?', [mid, gr['gr_no'], p['id']]
                if bid:
                    cond += ' AND batch_id=?'
                    args.append(bid)
                got = db.execute('SELECT COALESCE(SUM(base_qty),0), MAX(price_cents), MAX(rate) FROM goods_receipt_lines WHERE ' + cond, args).fetchone()
                if not got[0]:
                    raise BizError('%s 不在原收货单 %s 里' % (p['name'], gr['gr_no']))
                done = db.execute('SELECT COALESCE(SUM(l.base_qty),0) FROM purchase_return_lines l JOIN purchase_returns r ON r.pr_no=l.pr_no '
                                  "WHERE r.merchant_id=? AND r.gr_no=? AND r.status!='cancelled' AND l.product_id=?" + (' AND l.batch_id=?' if bid else ''),
                                  [mid, gr['gr_no'], p['id']] + ([bid] if bid else [])).fetchone()[0]
                if done + base > got[0]:
                    raise BizError('%s 累计退货 %d 超过原收货 %d（基本单位）' % (p['name'], done + base, got[0]))
                if price is None:
                    price = (got[1] * rate + (got[2] or 1) // 2) // (got[2] or 1)     # 原收货单价折算到本次单位
            price = price or 0
            db.execute('INSERT INTO purchase_return_lines(merchant_id, pr_no, product_id, unit, rate, qty, base_qty, price_cents, batch_id, quality) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?)', (mid, no, p['id'], unit, rate, qty, base, price, bid, quality))
            total += price * qty
            n += 1
        if not n:
            raise BizError('采购退货至少要有一行商品')
        db.execute("INSERT INTO purchase_returns(pr_no, merchant_id, gr_no, po_no, supplier_id, wh_code, status, total_cents, reason, created_by, created_at) "
                   "VALUES(?,?,?,?,?,?,'submitted',?,?,?,?)", (no, mid, gr['gr_no'] if gr else '', (gr['po_no'] if gr else d.get('po_no')) or '',
                                                              sup['id'], wh['code'], total, reason[:200], a['username'], now_s()))
        audit(db, a, 'purchase', '提交采购退货', no, None, {'lines': n, 'total_cents': total}, reason, store_code=wh['store_code'])
    return ok(_pr_out(db, a, _pr(db, mid, no)))


@bp.route('/returns/<no>/approve', methods=['POST'])
@require_perm('purchase.approve')
def return_approve(no):
    """审核并出库（stock 'purchase_return'）。指定批次时可退已锁定/召回/过期批次（退回供应商是召回处理的正常路径）。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    with tx(db):
        pr = _pr(db, mid, no)
        check_wh(db, a, pr['wh_code'])
        if pr['status'] != 'submitted':
            raise BizError('退货单当前「%s」，不能审核' % PR_STATUS[pr['status']], 409, 'CONFLICT')
        if d.get('reject'):
            db.execute("UPDATE purchase_returns SET status='cancelled' WHERE id=?", (pr['id'],))
            audit(db, a, 'purchase', '驳回采购退货', no, {'status': 'submitted'}, {'status': 'cancelled'}, d.get('reason') or '')
            return ok(_pr_out(db, a, _pr(db, mid, no)))
        for ln in db.execute('SELECT * FROM purchase_return_lines WHERE merchant_id=? AND pr_no=? ORDER BY id', (mid, no)).fetchall():
            extra = dict(unit=ln['unit'], unit_qty=ln['qty'], rate=ln['rate'], note=pr['reason'])
            if ln['batch_id'] or ln['quality'] != 'ok':
                inventory.post_move(db, mid, pr['wh_code'], ln['product_id'], -ln['base_qty'], 'purchase_return', no, a['username'],
                                    batch_id=ln['batch_id'], quality=ln['quality'], **extra)
            else:
                alloc = inventory.stock_out(db, a, mid, pr['wh_code'], [dict(product_id=ln['product_id'], base_qty=ln['base_qty'], **extra)],
                                            'purchase_return', no)
                if len(alloc[0]) == 1:
                    db.execute('UPDATE purchase_return_lines SET batch_id=? WHERE id=?', (alloc[0][0]['batch_id'], ln['id']))
        db.execute("UPDATE purchase_returns SET status='posted', posted_at=? WHERE id=?", (now_s(), pr['id']))
        audit(db, a, 'purchase', '采购退货出库', no, {'status': 'submitted'}, {'status': 'posted'}, d.get('reason') or '')
    return ok(_pr_out(db, a, _pr(db, mid, no)))


# ================================================================ 采购价（ERP-206）
@bp.route('/prices')
@require_perm('purchase.view')
def prices():
    db, a = get_db(), current_actor()
    if not has_perm(a, 'goods.cost.view'):
        raise BizError('查看采购价需要成本价权限（goods.cost.view）', 403)
    q = request.args
    sql, args = 'FROM supplier_prices sp WHERE sp.merchant_id=?', [a['merchant_id']]
    if q.get('product_id'):
        sql += ' AND sp.product_id=?'
        args.append(inum(q['product_id']))
    if q.get('supplier_id'):
        sql += ' AND sp.supplier_id=?'
        args.append(inum(q['supplier_id']))

    def conv(r):
        x = dict(r)
        x['price'] = yuan(x.pop('price_cents'))
        s = db.execute('SELECT name FROM suppliers WHERE id=?', (x['supplier_id'],)).fetchone()
        p = db.execute('SELECT code, name FROM products WHERE id=?', (x['product_id'],)).fetchone()
        x['supplier_name'] = s[0] if s else ''
        x['code'], x['name'] = (p[0], p[1]) if p else ('', '')
        return x
    return ok(page_query(db, sql, args, q, 'sp.id DESC', conv=conv))
