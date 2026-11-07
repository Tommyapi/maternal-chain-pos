# -*- coding: utf-8 -*-
"""库存服务：唯一能改库存的地方。Owner：模块①（商品/库存）。其他模块只调用这里的函数，不直接写 inv_stock / stock / stock_moves。

契约（docs/API-CONTRACT.md §库存服务）：
    post_move(db, mid, wh, product_id, delta, ref_type, ref_no, operator, ...)   一条流水（底层原语）
    stock_out(db, actor, mid, wh, lines, ref_type, ref_no)  -> [[{batch_id,batch_no,expiry_date,qty}], ...]   销售/调拨出库/报损（FEFO 自动选批）
    stock_in(db, actor, mid, wh, lines, ref_type, ref_no)   -> [[{batch_id,...,qty}], ...]                    采购入库/退货回库/调拨收货/报溢
    sellable_batches(db, mid, wh, product_id)  可售批次（先到期先出顺序）；check_batch_sellable() 返回不可售原因
    to_base_qty(qty, rate)  交易单位数量 -> 基本单位整数（不整除直接报错，禁止四舍五入吞库存）
数量一律是「基本单位」整数；必须在调用方写事务（tx(db)）里调用；失败抛 BizError，整单回滚。
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .util import BizError, now_s

QUALITY = {'ok': '合格', 'defect': '残次', 'pending': '待处理'}
MOVE_TYPES = {
    'sale': '销售', 'refund': '退货', 'exchange': '换货', 'purchase': '采购入库', 'purchase_return': '采购退货',
    'transfer_out': '调拨出库', 'transfer_in': '调拨入库', 'stocktake': '盘点', 'loss': '报损', 'overflow': '报溢',
    'adjust': '调整', 'split': '拆零', 'assemble': '组装', 'quality': '质量转移', 'opening': '期初', 'legacy': '旧版',
}
LEGACY_TYPE = dict((v, k) for k, v in MOVE_TYPES.items())
LEGACY_TYPE.update({'入库': 'purchase', '盘点': 'stocktake', '期初': 'opening'})


def to_base_qty(qty, rate=1):
    try:
        q = Decimal(str(qty)) * int(rate)
    except (InvalidOperation, ValueError, TypeError):
        raise BizError('数量格式不正确')
    if q != q.to_integral_value():
        raise BizError('数量换算成基本单位后必须是整数（%s × %s）' % (qty, rate))
    return int(q)


def today():
    return datetime.now().strftime('%Y-%m-%d')


def batch_row(db, mid, batch_id):
    if not batch_id:
        return None
    r = db.execute('SELECT * FROM inv_batches WHERE id=? AND merchant_id=?', (batch_id, mid)).fetchone()
    return dict(r) if r else None


def check_batch_sellable(b, on_day=None):
    """返回不可售原因（空串 = 可售）。b 为 inv_batches 行 dict 或 None（无批次=可售）。"""
    if not b:
        return ''
    if b['status'] == 'recalled':
        return '批次 %s 已召回' % b['batch_no']
    if b['status'] == 'locked':
        return '批次 %s 已锁定（%s）' % (b['batch_no'], b.get('lock_reason') or '')
    if b['expiry_date'] and b['expiry_date'] < (on_day or today()):
        return '批次 %s 已过期（有效期至 %s）' % (b['batch_no'], b['expiry_date'])
    return ''


def get_or_create_batch(db, mid, product_id, batch_no, expiry_date='', prod_date='', supplier_id=None, src_no=''):
    batch_no = (batch_no or '').strip()
    if not batch_no:
        raise BizError('批次商品必须填写批号')
    for d in (expiry_date, prod_date):
        if d:
            try:
                datetime.strptime(d, '%Y-%m-%d')
            except ValueError:
                raise BizError('日期格式应为 YYYY-MM-DD：%s' % d)
    if prod_date and expiry_date and prod_date > expiry_date:
        raise BizError('生产日期不能晚于有效期')
    r = db.execute('SELECT id FROM inv_batches WHERE merchant_id=? AND product_id=? AND batch_no=? AND expiry_date=?',
                   (mid, product_id, batch_no, expiry_date or '')).fetchone()
    if r:
        return r[0]
    cur = db.execute('INSERT INTO inv_batches(merchant_id, product_id, batch_no, prod_date, expiry_date, supplier_id, src_no, created_at) '
                     'VALUES(?,?,?,?,?,?,?,?)', (mid, product_id, batch_no, prod_date or '', expiry_date or '', supplier_id, src_no, now_s()))
    return cur.lastrowid


def qty_of(db, mid, wh, product_id, batch_id=0, quality='ok'):
    r = db.execute('SELECT qty FROM inv_stock WHERE merchant_id=? AND wh_code=? AND product_id=? AND batch_id=? AND quality=?',
                   (mid, wh, product_id, batch_id, quality)).fetchone()
    return int(r[0]) if r else 0


def post_move(db, mid, wh, product_id, delta, ref_type, ref_no, operator, batch_id=0, quality='ok', note='',
              unit='', unit_qty=None, rate=None, cost_cents=None, allow_negative=False):
    """库存原语：改一行 inv_stock + 写一条 stock_moves + 同步旧表 stock（合格品账面）。返回变动后的该行数量。"""
    delta = int(delta)
    if not delta:
        return qty_of(db, mid, wh, product_id, batch_id, quality)
    if ref_type not in MOVE_TYPES or not ref_no:
        raise BizError('库存变动必须有来源单据（ref_type/ref_no）', 500)
    if quality not in QUALITY:
        raise BizError('质量状态不合法：%s' % quality)
    ts = now_s()
    before = qty_of(db, mid, wh, product_id, batch_id, quality)
    after = before + delta
    if after < 0 and not allow_negative:
        name = db.execute('SELECT name FROM products WHERE id=?', (product_id,)).fetchone()
        raise BizError('库存不足：%s（%s 现有 %d，需要 %d）' % (name[0] if name else product_id, wh, before, -delta), 409, 'STOCK_SHORT')
    db.execute('INSERT INTO inv_stock(merchant_id, wh_code, product_id, batch_id, quality, qty, updated_at) VALUES(?,?,?,?,?,?,?) '
               'ON CONFLICT(merchant_id, wh_code, product_id, batch_id, quality) DO UPDATE SET qty = qty + excluded.qty, updated_at = excluded.updated_at',
               (mid, wh, product_id, batch_id, quality, delta, ts))
    legacy_bal = None
    if quality == 'ok':
        db.execute('INSERT OR IGNORE INTO stock(product_id, store_code, qty, updated_at, merchant_id) VALUES(?,?,0,?,?)', (product_id, wh, ts, mid))
        db.execute('UPDATE stock SET qty = qty + ?, updated_at=? WHERE product_id=? AND store_code=?', (delta, ts, product_id, wh))
        legacy_bal = db.execute('SELECT qty FROM stock WHERE product_id=? AND store_code=?', (product_id, wh)).fetchone()[0]
    b = batch_row(db, mid, batch_id)
    db.execute('INSERT INTO stock_moves(merchant_id, product_id, store_code, wh_code, change, balance, type, ref_type, ref_no, batch_id, batch_no, '
               'expiry_date, quality, qty_before, qty_after, unit, unit_qty, rate, cost_cents, operator, note, created_at) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (mid, product_id, wh, wh, delta, legacy_bal if legacy_bal is not None else after, MOVE_TYPES[ref_type], ref_type, ref_no,
                batch_id, b['batch_no'] if b else '', b['expiry_date'] if b else '', quality, before, after, unit, unit_qty, rate,
                cost_cents, operator or '', (note or '')[:200], ts))
    return after


def sellable_batches(db, mid, wh, product_id, on_day=None):
    """可售批次，先到期先出（有效期空的排最后，同有效期按批次 id）。只含 qty>0、合格、未锁定/召回、未过期。"""
    d = on_day or today()
    rows = db.execute("SELECT b.*, s.qty FROM inv_stock s JOIN inv_batches b ON b.id=s.batch_id "
                      "WHERE s.merchant_id=? AND s.wh_code=? AND s.product_id=? AND s.quality='ok' AND s.qty>0 AND s.batch_id>0 "
                      "ORDER BY CASE WHEN b.expiry_date='' THEN 1 ELSE 0 END, b.expiry_date, b.id", (mid, wh, product_id)).fetchall()
    return [dict(r) for r in rows if not check_batch_sellable(dict(r), d)]


def available(db, mid, wh, product_id):
    """可售数量 = 无批次合格品 + 可售批次合计。"""
    return max(0, qty_of(db, mid, wh, product_id, 0, 'ok')) + sum(b['qty'] for b in sellable_batches(db, mid, wh, product_id))


def _is_batch_managed(db, product_id):
    r = db.execute('SELECT batch_managed FROM products WHERE id=?', (product_id,)).fetchone()
    return bool(r and r[0])


def stock_out(db, actor, mid, wh, lines, ref_type, ref_no, allow_negative=False):
    """出库。lines: [{'product_id', 'base_qty'(>0), 'batch_id'(可选：指定批次), 'unit','unit_qty','rate','cost_cents','note'}]
    返回每行的批次分配 [{'batch_id','batch_no','expiry_date','qty'}]。
    - 指定批次：该批次必须可售（过期/锁定/召回直接拒绝，AC-008）。
    - 批次商品未指定：按 FEFO 自动分配；可售批次不够时，allow_negative=True 才把差额记到无批次行（负库存），否则报库存不足。
    - 非批次商品：扣无批次行。
    """
    op = (actor or {}).get('username', '')
    out = []
    for ln in lines:
        pid, need = int(ln['product_id']), int(ln['base_qty'])
        if need <= 0:
            raise BizError('出库数量必须大于 0')
        extra = dict(unit=ln.get('unit', ''), unit_qty=ln.get('unit_qty'), rate=ln.get('rate'), cost_cents=ln.get('cost_cents'), note=ln.get('note', ''))
        alloc = []
        if ln.get('batch_id'):
            b = batch_row(db, mid, int(ln['batch_id']))
            if b is None:
                raise BizError('批次不存在')
            why = check_batch_sellable(b)
            if why:
                raise BizError(why + '，不能出库', 409, 'BATCH_UNSELLABLE')
            post_move(db, mid, wh, pid, -need, ref_type, ref_no, op, batch_id=b['id'], allow_negative=allow_negative, **extra)
            alloc.append({'batch_id': b['id'], 'batch_no': b['batch_no'], 'expiry_date': b['expiry_date'], 'qty': need})
        elif _is_batch_managed(db, pid):
            left = need
            for b in sellable_batches(db, mid, wh, pid):
                if left <= 0:
                    break
                take = min(left, int(b['qty']))
                post_move(db, mid, wh, pid, -take, ref_type, ref_no, op, batch_id=b['id'], **extra)
                alloc.append({'batch_id': b['id'], 'batch_no': b['batch_no'], 'expiry_date': b['expiry_date'], 'qty': take})
                left -= take
            if left > 0:
                if not allow_negative and qty_of(db, mid, wh, pid, 0, 'ok') < left:
                    raise BizError('可售批次库存不足（过期/锁定批次不计入）', 409, 'STOCK_SHORT')
                post_move(db, mid, wh, pid, -left, ref_type, ref_no, op, batch_id=0, allow_negative=allow_negative, **extra)
                alloc.append({'batch_id': 0, 'batch_no': '', 'expiry_date': '', 'qty': left})
        else:
            post_move(db, mid, wh, pid, -need, ref_type, ref_no, op, batch_id=0, allow_negative=allow_negative, **extra)
            alloc.append({'batch_id': 0, 'batch_no': '', 'expiry_date': '', 'qty': need})
        out.append(alloc)
    return out


def stock_in(db, actor, mid, wh, lines, ref_type, ref_no):
    """入库。lines: [{'product_id','base_qty'(>0),'batch_id' 或 'batch_no'+'expiry_date'+'prod_date','quality'('ok'/'defect'/'pending'),
    'unit','unit_qty','rate','cost_cents','note'}]。退货回库按 quality 决定进可售还是残次/待处理（POS-606）。"""
    op = (actor or {}).get('username', '')
    out = []
    for ln in lines:
        pid, q = int(ln['product_id']), int(ln['base_qty'])
        if q <= 0:
            raise BizError('入库数量必须大于 0')
        bid = int(ln.get('batch_id') or 0)
        if not bid and ln.get('batch_no'):
            bid = get_or_create_batch(db, mid, pid, ln['batch_no'], ln.get('expiry_date', ''), ln.get('prod_date', ''),
                                      ln.get('supplier_id'), ref_no)
        if not bid and _is_batch_managed(db, pid) and ref_type in ('purchase', 'overflow', 'opening'):
            raise BizError('批次商品入库必须填写批号和有效期')
        quality = ln.get('quality') or 'ok'
        post_move(db, mid, wh, pid, q, ref_type, ref_no, op, batch_id=bid, quality=quality, unit=ln.get('unit', ''),
                  unit_qty=ln.get('unit_qty'), rate=ln.get('rate'), cost_cents=ln.get('cost_cents'), note=ln.get('note', ''))
        b = batch_row(db, mid, bid)
        out.append([{'batch_id': bid, 'batch_no': b['batch_no'] if b else '', 'expiry_date': b['expiry_date'] if b else '', 'qty': q}])
    return out


# ================================================================ 查询服务（模块①提供；报表④、APP⑤ 可直接调用，只读）
def in_transit(db, mid, wh, product_id=None):
    """调入方 wh 的在途数量 = 已出库未收货且未记差异（out − in − diff）。返回 {product_id: qty}。"""
    sql = ("SELECT l.product_id, SUM(l.out_base_qty - l.in_base_qty - l.diff_base_qty) FROM transfer_lines l JOIN transfer_orders t ON t.tf_no=l.tf_no "
           "WHERE t.merchant_id=? AND t.to_wh=? AND t.status IN ('shipped','partial')")
    args = [mid, wh]
    if product_id:
        sql += ' AND l.product_id=?'
        args.append(product_id)
    return dict((r[0], int(r[1] or 0)) for r in db.execute(sql + ' GROUP BY l.product_id', args) if r[1])


def balance_rows(db, mid, whs, product_ids=None, by_batch=False, on_day=None, include_zero=False):
    """库存余额分类（ERP-301）：book 账面（全部质量状态）/ sellable 可售 / locked 锁定召回 / defect 残次+待处理 / expired 过期 / in_transit 在途。
    whs: 仓库编码列表；返回 [{wh_code, product_id, batch_id?, book, ok, sellable, locked, defect, expired, in_transit}]。"""
    if not whs:
        return []
    d = on_day or today()
    grp = 's.wh_code, s.product_id' + (', s.batch_id' if by_batch else '')
    sql = ("SELECT %s, SUM(s.qty) book, SUM(CASE WHEN s.quality='ok' THEN s.qty ELSE 0 END) ok, "
           "SUM(CASE WHEN s.quality!='ok' THEN s.qty ELSE 0 END) defect, "
           "SUM(CASE WHEN s.quality='ok' AND b.status IN ('locked','recalled') THEN s.qty ELSE 0 END) locked, "
           "SUM(CASE WHEN s.quality='ok' AND b.status='normal' AND b.expiry_date!='' AND b.expiry_date<? THEN s.qty ELSE 0 END) expired "
           "FROM inv_stock s LEFT JOIN inv_batches b ON b.id=s.batch_id AND s.batch_id>0 "
           "WHERE s.merchant_id=? AND s.wh_code IN (%s)" % (grp, ','.join('?' * len(whs))))
    args = [d, mid] + list(whs)
    if product_ids:
        sql += ' AND s.product_id IN (%s)' % ','.join('?' * len(product_ids))
        args += list(product_ids)
    sql += ' GROUP BY %s%s ORDER BY s.wh_code, s.product_id' % (grp, '' if include_zero else ' HAVING SUM(CASE WHEN s.qty!=0 THEN 1 ELSE 0 END) > 0')
    sellable_wh = dict((r[0], r[1]) for r in db.execute('SELECT code, sellable FROM warehouses WHERE merchant_id=?', (mid,)))
    transit = {}
    out = []
    for r in db.execute(sql, args):
        x = dict(r)
        x['sellable'] = max(0, x['ok'] - x['locked'] - x['expired']) if sellable_wh.get(x['wh_code'], 1) else 0
        if x['wh_code'] not in transit:
            transit[x['wh_code']] = in_transit(db, mid, x['wh_code'])
        x['in_transit'] = 0 if by_batch else transit[x['wh_code']].get(x['product_id'], 0)
        out.append(x)
    return out


def _alert_rule(db, mid, p):
    """商品 > 分类 > 商户 的预警规则；返回 (safety_qty, [临期级别天数 升序], slow_days)。"""
    rules = {}
    for r in db.execute('SELECT * FROM stock_alert_rules WHERE merchant_id=?', (mid,)):
        rules[(r['target_type'], r['target_id'])] = dict(r)
    cat = db.execute('SELECT * FROM categories WHERE id=? AND merchant_id=?', (p.get('category_id'), mid)).fetchone() if p.get('category_id') else None
    chain = [rules.get(('product', p['id'])), rules.get(('category', p.get('category_id') or -1)), rules.get(('merchant', 0))]
    safety = next((c['safety_qty'] for c in chain if c and c['safety_qty'] is not None), None)
    if safety is None:
        safety = cat['safety_stock'] if cat is not None and cat['safety_stock'] else 0
    levels = next((c['near_expiry_days'] for c in chain if c and (c['near_expiry_days'] or '').strip()), '')
    lv = sorted(set(int(x) for x in str(levels).replace('，', ',').split(',') if x.strip().isdigit() and int(x) > 0))
    if not lv:
        single = p.get('near_expiry_days') or (cat['near_expiry_days'] if cat is not None else 0)
        lv = [int(single)] if single else [30, 60, 90]
    slow = next((c['slow_days'] for c in chain if c and c['slow_days']), None) or 60
    return int(safety or 0), lv, int(slow)


def alerts(db, mid, whs, on_day=None, kinds=('low', 'out', 'slow', 'near_expiry', 'expired')):
    """库存预警（ERP-308 / BABY-101）：缺货 out、低于安全库存 low、滞销 slow、临期 near_expiry（按 30/60/90 等级别）、过期 expired。
    返回 {kind: [ {wh_code, product_id, code, name, qty, ...} ]}。只读，可被 APP 概况、定时任务复用。"""
    from datetime import datetime as _dt, timedelta
    d = on_day or today()
    out = dict((k, []) for k in kinds)
    if not whs:
        return out
    prods = {}

    def prod(pid):
        if pid not in prods:
            r = db.execute('SELECT * FROM products WHERE id=? AND merchant_id=?', (pid, mid)).fetchone()
            prods[pid] = (dict(r), _alert_rule(db, mid, dict(r))) if r else (None, None)
        return prods[pid]
    for x in balance_rows(db, mid, whs, on_day=d, include_zero=True):     # 卖空的商品（余额 0 行）也要报缺货
        p, rule = prod(x['product_id'])
        if p is None or p['status'] != '在售':
            continue
        base = dict(wh_code=x['wh_code'], product_id=p['id'], code=p['code'], name=p['name'], unit=p['base_unit'] or p['unit'])
        safety, levels, slow_days = rule
        if 'out' in out and x['sellable'] <= 0:
            out['out'].append(dict(base, qty=x['sellable'], safety=safety))
        elif 'low' in out and safety and x['sellable'] < safety:
            out['low'].append(dict(base, qty=x['sellable'], safety=safety))
        if 'slow' in out and x['ok'] > 0:
            since = (_dt.strptime(d, '%Y-%m-%d') - timedelta(days=slow_days)).strftime('%Y-%m-%d')
            sold = db.execute("SELECT 1 FROM stock_moves WHERE merchant_id=? AND wh_code=? AND product_id=? AND ref_type='sale' AND created_at>=? LIMIT 1",
                              (mid, x['wh_code'], p['id'], since)).fetchone()
            first = db.execute('SELECT MIN(created_at) FROM stock_moves WHERE merchant_id=? AND wh_code=? AND product_id=?',
                               (mid, x['wh_code'], p['id'])).fetchone()[0]
            if not sold and first and first[:10] <= since:
                out['slow'].append(dict(base, qty=x['ok'], slow_days=slow_days))
    if 'near_expiry' in out or 'expired' in out:
        rows = db.execute("SELECT s.wh_code, s.product_id, s.qty, b.id batch_id, b.batch_no, b.expiry_date, b.status FROM inv_stock s "
                          "JOIN inv_batches b ON b.id=s.batch_id WHERE s.merchant_id=? AND s.quality='ok' AND s.qty>0 AND b.expiry_date!='' "
                          "AND s.wh_code IN (%s) ORDER BY b.expiry_date" % ','.join('?' * len(whs)), [mid] + list(whs)).fetchall()
        for r in rows:
            p, rule = prod(r['product_id'])
            if p is None:
                continue
            left = (_dt.strptime(r['expiry_date'], '%Y-%m-%d') - _dt.strptime(d, '%Y-%m-%d')).days
            base = dict(wh_code=r['wh_code'], product_id=p['id'], code=p['code'], name=p['name'], batch_id=r['batch_id'], batch_no=r['batch_no'],
                        expiry_date=r['expiry_date'], qty=r['qty'], days_left=left, batch_status=r['status'])
            if left < 0:
                if 'expired' in out:
                    out['expired'].append(base)
            elif 'near_expiry' in out:
                lv = next((x for x in rule[1] if left <= x), None)
                if lv is not None:
                    out['near_expiry'].append(dict(base, level=lv))
    return out
