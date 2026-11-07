# -*- coding: utf-8 -*-
"""报表中心：销售/商品毛利/时段/人员/支付/班次/库存/会员/订单明细 + 同步导出 + 异步导出任务（export_jobs）。

Owner：模块④（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-801~806 ERP-808 NFR-SEC-004 AC-011 AC-019
接口：GET api/report/<kind>（kind 见 REPORTS）、GET api/report/<kind>/export、POST/GET api/report/exports、GET api/report/exports/<no>/download

规则：
- 报表只读（不写任何业务表）；所有查询带 merchant_id + 数据范围（scope_sql）；口径说明随数据返回（caliber）。
- 金额：库内 *_cents，出参元（money.yuan）。成本/毛利/库存金额需要 report.profit（字段权限），无权限返回 null。
- 导出：需要 report.export + 该报表权限；被拒也写 security 审计（AC-011）；同步导出超过 xlsx_io.MAX_SYNC_EXPORT_ROWS 行 -> 400，
  改用异步任务：后台线程独立连接只读查询（SQLite WAL 读不阻塞收银写），文件 24 小时有效，只能本人下载（AC-019 / NFR-SEC-004）。
"""
import json
import os
import threading
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from flask import Blueprint, request, current_app, send_file

from .core import (get_db, tx, require_perm, api, current_actor, scope_sql, scope_stores, has_perm, audit, ok, mask_phone)
from .money import yuan
from .numbering import next_no
from .util import BizError, now_s, inum
from . import xlsx_io

bp = Blueprint('report', __name__, url_prefix='/api/report')

EXPORT_TTL_HOURS = 24
MAX_DAYS = 400


# ---------------------------------------------------------------- 小工具
def _cents(v):
    return int(v or 0)


def _avg(a_cents, n):
    """金额(分) ÷ 数量 -> 元（ROUND_HALF_UP 到分）。"""
    if not n:
        return 0.0
    return yuan(int((Decimal(int(a_cents or 0)) / Decimal(str(n))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))


def _ratio(a, b, nd=2):
    if not b:
        return 0.0
    return float((Decimal(str(a or 0)) / Decimal(str(b))).quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP))


def _qty(v):
    v = float(v or 0)
    return int(v) if abs(v - round(v)) < 1e-9 else round(v, 3)


def _range(args):
    today = datetime.now().strftime('%Y-%m-%d')
    start = (args.get('start') or '').strip()[:10] or (datetime.now() - timedelta(days=29)).strftime('%Y-%m-%d')
    end = (args.get('end') or '').strip()[:10] or today
    try:
        d0, d1 = datetime.strptime(start, '%Y-%m-%d'), datetime.strptime(end, '%Y-%m-%d')
    except ValueError:
        raise BizError('日期格式应为 YYYY-MM-DD')
    if d0 > d1:
        raise BizError('开始日期不能晚于结束日期')
    if (d1 - d0).days > MAX_DAYS:
        raise BizError('查询区间最长 %d 天' % MAX_DAYS)
    return start, end


def _base(db, actor, args, alias='o'):
    """orders 的公共条件：商户 + 数据范围 + 日期。返回 (where_sql, params, start, end)。"""
    start, end = _range(args)
    sql, p = scope_sql(db, actor, alias + '.store_code', args.get('store'), alias + '.merchant_id')
    return (' WHERE 1=1' + sql + ' AND substr(%s.created_at,1,10) BETWEEN ? AND ?' % alias), p + [start, end], start, end


def _result(columns, rows, total=None, caliber=None, **extra):
    d = {'columns': [{'key': k, 'title': t} for k, t in columns], 'rows': rows, 'total': total or {}, 'caliber': caliber or []}
    d.update(extra)
    return d


SALES_CALIBER = [
    '销售额 = 销售单应收合计（已扣除会员价、改价、活动、整单折扣、券、积分抵扣、抹零）；储值支付计入销售额',
    '退款 = 退货单退款金额；实收（净额）= 销售额 − 退款',
    '客单价 = 销售额 ÷ 销售单数；件单价 = 销售额 ÷ 销售件数；连带率 = 销售件数 ÷ 销售单数',
    '优惠 = 销售单优惠合计；时间按单据创建时间，门店按当前账号数据范围',
]


# ================================================================ ERP-801 销售汇总
def rpt_sales(db, actor, args):
    dim = args.get('dim') or 'day'
    keys = {'day': ('substr(o.created_at,1,10)', '日期'), 'week': ("strftime('%Y-W%W', o.created_at)", '周'),
            'month': ('substr(o.created_at,1,7)', '月份'), 'store': ('o.store_code', '门店'), 'region': ("COALESCE(ou.code,'')", '区域')}
    if dim not in keys:
        raise BizError('不支持的统计维度：%s' % dim)
    key, title = keys[dim]
    label = {'store': 'MAX(o.store_name)', 'region': "COALESCE(MAX(ou.name),'未分区')"}.get(dim, key)
    where, p, start, end = _base(db, actor, args)
    join = (' LEFT JOIN stores s ON s.merchant_id=o.merchant_id AND s.code=o.store_code LEFT JOIN org_units ou ON ou.id=s.region_id'
            if dim == 'region' else '')
    rows = []
    t = {'orders': 0, 'sales_c': 0, 'refund_orders': 0, 'refund_c': 0, 'qty': 0, 'disc_c': 0}
    for r in db.execute(
            "SELECT %s k, %s lb, COUNT(CASE WHEN o.biz_type='sale' THEN 1 END), "
            "COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.payable_cents END),0), COUNT(CASE WHEN o.biz_type='refund' THEN 1 END), "
            "COALESCE(-SUM(CASE WHEN o.biz_type='refund' THEN o.payable_cents END),0), COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.item_qty END),0), "
            "COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.discount_total_cents END),0) FROM orders o%s%s GROUP BY k ORDER BY k"
            % (key, label, join, where), p):
        x = {'key': r[0], 'name': r[1] or r[0], 'orders': r[2], 'sales': yuan(r[3]), 'refund_orders': r[4], 'refund': yuan(r[5]),
             'net': yuan(r[3] - r[5]), 'qty': _qty(r[6]), 'avg_order': _avg(r[3], r[2]), 'avg_item': _avg(r[3], r[6]),
             'attach_rate': _ratio(r[6], r[2]), 'discount': yuan(r[7])}
        rows.append(x)
        for k, v in (('orders', r[2]), ('sales_c', r[3]), ('refund_orders', r[4]), ('refund_c', r[5]), ('qty', r[6]), ('disc_c', r[7])):
            t[k] += v or 0
    if dim in ('day', 'week', 'month'):
        rows.reverse()
    else:
        rows.sort(key=lambda x: -x['sales'])
    total = {'name': '合计', 'orders': t['orders'], 'sales': yuan(t['sales_c']), 'refund_orders': t['refund_orders'], 'refund': yuan(t['refund_c']),
             'net': yuan(t['sales_c'] - t['refund_c']), 'qty': _qty(t['qty']), 'avg_order': _avg(t['sales_c'], t['orders']),
             'avg_item': _avg(t['sales_c'], t['qty']), 'attach_rate': _ratio(t['qty'], t['orders']), 'discount': yuan(t['disc_c'])}
    cols = [('name', title), ('orders', '销售单数'), ('sales', '销售额'), ('refund_orders', '退货单数'), ('refund', '退款'), ('net', '实收净额'),
            ('qty', '销售件数'), ('avg_order', '客单价'), ('avg_item', '件单价'), ('attach_rate', '连带率'), ('discount', '优惠')]
    return _result(cols, rows, total, SALES_CALIBER, dim=dim, start=start, end=end)


# ================================================================ ERP-802 商品分析（成本毛利单独授权）
def rpt_goods(db, actor, args):
    dim = args.get('dim') or 'product'
    keys = {'product': ('i.product_code', "MAX(i.name || CASE WHEN COALESCE(i.spec,'')!='' THEN ' ' || i.spec ELSE '' END)", '商品'),
            'category': ("COALESCE(NULLIF(i.category,''),'未分类')", None, '分类'),
            'brand': ("COALESCE(NULLIF(i.brand,''),'无品牌')", None, '品牌'),
            'spec': ("COALESCE(NULLIF(i.spec,''),'无规格')", None, '规格')}
    if dim not in keys:
        raise BizError('不支持的统计维度：%s' % dim)
    key, label, title = keys[dim]
    where, p, start, end = _base(db, actor, args)
    extra, ep = '', []
    if (args.get('category') or '').strip():
        extra += ' AND i.category=?'
        ep.append(args.get('category').strip())
    if (args.get('kw') or '').strip():
        extra += ' AND (i.name LIKE ? OR i.product_code LIKE ? OR i.barcode LIKE ?)'
        ep += ['%' + args.get('kw').strip() + '%'] * 3
    show = has_perm(actor, 'report.profit')
    rows = []
    t = {'qty': 0, 'sales': 0, 'cost': 0}
    for r in db.execute(
            "SELECT %s k, %s lb, COALESCE(SUM(COALESCE(i.base_qty, i.qty)),0), COALESCE(SUM(i.amount_cents),0), COALESCE(SUM(i.cost_cents),0), "
            "COUNT(DISTINCT CASE WHEN o.biz_type='sale' THEN o.order_no END) "
            "FROM order_items i JOIN orders o ON o.order_no=i.order_no AND o.merchant_id=i.merchant_id%s%s GROUP BY k ORDER BY 4 DESC"
            % (key, label or key, where, extra), p + ep):
        profit = r[3] - r[4]
        rows.append({'key': r[0], 'name': r[1] or r[0], 'orders': r[5], 'qty': _qty(r[2]), 'sales': yuan(r[3]),
                     'cost': yuan(r[4]) if show else None, 'profit': yuan(profit) if show else None,
                     'margin': (_ratio(profit * 100, r[3]) if show else None)})
        t['qty'] += r[2] or 0
        t['sales'] += r[3]
        t['cost'] += r[4]
    total = {'name': '合计', 'qty': _qty(t['qty']), 'sales': yuan(t['sales']), 'cost': yuan(t['cost']) if show else None,
             'profit': yuan(t['sales'] - t['cost']) if show else None, 'margin': _ratio((t['sales'] - t['cost']) * 100, t['sales']) if show else None}
    cols = [('name', title), ('orders', '销售单数'), ('qty', '净销量（基本单位）'), ('sales', '净销售额')]
    if show:
        cols += [('cost', '成本'), ('profit', '毛利'), ('margin', '毛利率%')]
    cal = ['净销量/净销售额 = 销售 − 退货（按成交快照：行实付金额，已含分摊优惠）',
           '销量按基本单位（order_items.base_qty，如 1 箱 = 6 听记 6）；v1 旧单没有该列时按交易数量',
           '成本 = 成交时的商品成本快照；毛利 = 净销售额 − 成本；毛利率 = 毛利 ÷ 净销售额' if show else '成本、毛利需要「毛利报表」权限，当前账号不显示']
    return _result(cols, rows, total, cal, dim=dim, start=start, end=end, profit_visible=show)


# ================================================================ ERP-803 时段 / 人员 / 收银台
def rpt_hourly(db, actor, args):
    where, p, start, end = _base(db, actor, args)
    m = dict((int(r[0]), r) for r in db.execute(
        "SELECT CAST(substr(o.created_at,12,2) AS INTEGER) h, COUNT(CASE WHEN o.biz_type='sale' THEN 1 END), "
        "COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.payable_cents END),0), COALESCE(-SUM(CASE WHEN o.biz_type='refund' THEN o.payable_cents END),0) "
        "FROM orders o%s GROUP BY h" % where, p))
    rows = []
    for h in range(24):
        r = m.get(h)
        o, s, rf = (r[1], r[2], r[3]) if r else (0, 0, 0)
        rows.append({'hour': h, 'name': '%02d:00-%02d:59' % (h, h), 'orders': o, 'sales': yuan(s), 'refund': yuan(rf), 'avg_order': _avg(s, o)})
    return _result([('name', '时段'), ('orders', '销售单数'), ('sales', '销售额'), ('refund', '退款'), ('avg_order', '客单价')], rows, None,
                   ['按单据创建时间的小时统计，区间内各天合计'], start=start, end=end)


def rpt_staff(db, actor, args):
    dim = args.get('dim') or 'cashier'
    key = {'cashier': 'o.cashier_user', 'terminal': "COALESCE(NULLIF(o.terminal_no,''),'未登记')", 'guide': "COALESCE(NULLIF(o.guide_code,''),'无导购')"}.get(dim)
    if key is None:
        raise BizError('不支持的统计维度：%s' % dim)
    label = {'cashier': "MAX(o.cashier_name) || '(' || o.cashier_user || ')'", 'guide': "MAX(o.guide_name)"}.get(dim, key)
    where, p, start, end = _base(db, actor, args)
    if actor.get('data_scope') == 'self':
        where += ' AND o.cashier_user=?'
        p = p + [actor['username']]
    rows = []
    for r in db.execute(
            "SELECT %s k, %s lb, COUNT(CASE WHEN o.biz_type='sale' THEN 1 END), COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.payable_cents END),0), "
            "COUNT(CASE WHEN o.biz_type='refund' THEN 1 END), COALESCE(-SUM(CASE WHEN o.biz_type='refund' THEN o.payable_cents END),0), "
            "COALESCE(SUM(CASE WHEN o.biz_type='sale' THEN o.item_qty END),0) FROM orders o%s GROUP BY k ORDER BY 4 DESC" % (key, label, where), p):
        rows.append({'key': r[0], 'name': r[1] or r[0], 'orders': r[2], 'sales': yuan(r[3]), 'refund_orders': r[4], 'refund': yuan(r[5]),
                     'net': yuan(r[3] - r[5]), 'qty': _qty(r[6]), 'avg_order': _avg(r[3], r[2])})
    title = {'cashier': '收银员', 'terminal': '收银台', 'guide': '导购'}[dim]
    cols = [('name', title), ('orders', '销售单数'), ('sales', '销售额'), ('refund_orders', '退货单数'), ('refund', '经手退款'), ('net', '业绩（扣退款）'),
            ('qty', '件数'), ('avg_order', '客单价')]
    return _result(cols, rows, None, ['员工业绩口径：业绩 = 本人收银的销售额 − 本人经手的退款（扣除退款）；按收银账号归属，不按原单收银员回冲'],
                   dim=dim, start=start, end=end)


# ================================================================ ERP-804 支付 / 现金收支 / 班次 / 支付差异
KIND_NAME = {'sale': '销售收款', 'refund': '退款', 'recharge': '储值充值'}


def rpt_pay(db, actor, args):
    start, end = _range(args)
    sql, p = scope_sql(db, actor, 'p.store_code', args.get('store'), 'p.merchant_id')
    rows = []
    by_kind = {}
    for r in db.execute("SELECT p.kind, p.method, MAX(p.method_name), COUNT(*), COALESCE(SUM(p.amount_cents),0) FROM payments p "
                        "WHERE substr(p.created_at,1,10) BETWEEN ? AND ?" + sql + " GROUP BY p.kind, p.method ORDER BY p.kind, 5 DESC",
                        [start, end] + p):
        rows.append({'kind': r[0], 'kind_name': KIND_NAME.get(r[0], r[0]), 'method': r[1], 'name': r[2], 'count': r[3], 'amount': yuan(r[4])})
        by_kind[r[0]] = by_kind.get(r[0], 0) + r[4]
    csql, cp = scope_sql(db, actor, 'c.store_code', args.get('store'), 'c.merchant_id')
    cm = dict((r[0], (r[1], r[2])) for r in db.execute(
        "SELECT c.direction, COUNT(*), COALESCE(SUM(c.amount_cents),0) FROM cash_moves c WHERE substr(c.created_at,1,10) BETWEEN ? AND ?" + csql +
        " GROUP BY c.direction", [start, end] + cp))
    osql, op = scope_sql(db, actor, 'o.store_code', args.get('store'), 'o.merchant_id')
    diffs = [{'order_no': r[0], 'payable': yuan(r[1]), 'paid': yuan(r[2]), 'diff': yuan(r[2] - r[1]), 'created_at': r[3]} for r in db.execute(
        "SELECT o.order_no, o.payable_cents, COALESCE((SELECT SUM(x.amount_cents) FROM payments x WHERE x.merchant_id=o.merchant_id "
        "AND x.order_no=o.order_no),0) paid, o.created_at FROM orders o WHERE substr(o.created_at,1,10) BETWEEN ? AND ?" + osql +
        " AND o.pay_status='paid' AND paid != o.payable_cents ORDER BY o.id DESC LIMIT 100", [start, end] + op)]
    total = {'cash_in': yuan(cm.get('in', (0, 0))[1]), 'cash_out': yuan(cm.get('out', (0, 0))[1])}
    for k in KIND_NAME:
        total[k] = yuan(by_kind.get(k, 0))
    cols = [('kind_name', '类型'), ('name', '支付方式'), ('count', '笔数'), ('amount', '金额')]
    return _result(cols, rows, total, ['支付流水按收款时间；现金按净额（收到现金 − 找零）', '退款为负数；现金收支来自收银台现金收入/支出登记',
                                       '支付差异：订单应收 ≠ 支付流水合计的已支付订单（正常应为空）'],
                   cash_moves={'in_count': cm.get('in', (0, 0))[0], 'out_count': cm.get('out', (0, 0))[0]}, pay_diffs=diffs, start=start, end=end)


def rpt_shifts(db, actor, args):
    start, end = _range(args)
    sql, p = scope_sql(db, actor, 's.store_code', args.get('store'), 's.merchant_id')
    rows = []
    t = {'short': 0, 'over': 0, 'diff': 0}
    for r in db.execute("SELECT s.* FROM shifts s WHERE substr(s.open_time,1,10) BETWEEN ? AND ?" + sql + " ORDER BY s.id DESC LIMIT 5000",
                        [start, end] + p):
        diff = r['diff_cents'] if r['status'] == 'closed' else None
        rows.append({'id': r['id'], 'store_code': r['store_code'], 'name': r['store_name'], 'terminal_no': r['terminal_no'] or '',
                     'cashier': '%s(%s)' % (r['cashier_name'] or '', r['cashier_user'] or ''), 'open_time': r['open_time'], 'close_time': r['close_time'] or '',
                     'opening_cash': yuan(r['opening_cash_cents']), 'expected_cash': yuan(r['expected_cash_cents']) if r['expected_cash_cents'] is not None else None,
                     'counted_cash': yuan(r['counted_cash_cents']) if r['counted_cash_cents'] is not None else None,
                     'diff': yuan(diff) if diff is not None else None, 'diff_type': ('' if not diff else ('长款' if diff > 0 else '短款')),
                     'status': '已交班' if r['status'] == 'closed' else '未交班'})
        if diff:
            t['diff'] += diff
            t['over' if diff > 0 else 'short'] += 1
    cols = [('name', '门店'), ('terminal_no', '收银台'), ('cashier', '收银员'), ('open_time', '开班'), ('close_time', '交班'), ('opening_cash', '备用金'),
            ('expected_cash', '应有现金'), ('counted_cash', '实点现金'), ('diff', '长短款'), ('diff_type', '类型'), ('status', '状态')]
    return _result(cols, rows, {'shifts': len(rows), 'over_count': t['over'], 'short_count': t['short'], 'diff': yuan(t['diff'])},
                   ['应有现金 = 备用金 + 现金净收款 + 现金收入 − 现金支出 − 现金退款；长短款 = 实点 − 应有（正数长款，负数短款）'], start=start, end=end)


# ================================================================ ERP-805 库存报表
def rpt_stock(db, actor, args):
    stores = scope_stores(db, actor, args.get('store'))
    mid = actor['merchant_id']
    show = has_perm(actor, 'report.profit')
    if not stores:
        return _result([], [], {}, [])
    ph = ','.join('?' * len(stores))
    today = datetime.now().strftime('%Y-%m-%d')
    near = (datetime.now() + timedelta(days=inum(args.get('near_days'), 30) or 30)).strftime('%Y-%m-%d')
    names = dict((r[0], r[1]) for r in db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)))
    if args.get('detail') == '1':
        rows = []
        for r in db.execute("SELECT s.wh_code, p.code, p.name, p.spec, p.category, SUM(CASE WHEN s.quality='ok' THEN s.qty ELSE 0 END), "
                            "SUM(CASE WHEN s.quality!='ok' THEN s.qty ELSE 0 END), MAX(p.cost_cents) FROM inv_stock s JOIN products p ON p.id=s.product_id "
                            "AND p.merchant_id=s.merchant_id WHERE s.merchant_id=? AND s.wh_code IN (%s) GROUP BY s.wh_code, s.product_id "
                            "ORDER BY s.wh_code, p.category, p.name LIMIT 100000" % ph, [mid] + stores):
            rows.append({'store': names.get(r[0], r[0]), 'code': r[1], 'name': r[2], 'spec': r[3] or '', 'category': r[4] or '', 'qty': r[5],
                         'defect_qty': r[6], 'amount': yuan(r[5] * (r[7] or 0)) if show else None})
        cols = [('store', '门店'), ('code', '商品编码'), ('name', '商品'), ('spec', '规格'), ('category', '分类'), ('qty', '合格品数量'), ('defect_qty', '残次/待处理')]
        if show:
            cols.append(('amount', '库存金额（成本）'))
        return _result(cols, rows, None, ['数量为基本单位；库存金额 = 合格品数量 × 当前成本价'])
    rows = []
    for wh in stores:
        agg = db.execute("SELECT COUNT(DISTINCT CASE WHEN s.quality='ok' AND s.qty>0 THEN s.product_id END), "
                         "COALESCE(SUM(CASE WHEN s.quality='ok' THEN s.qty END),0), COALESCE(SUM(CASE WHEN s.quality!='ok' THEN s.qty END),0), "
                         "COALESCE(SUM(CASE WHEN s.quality='ok' AND s.qty>0 THEN s.qty * COALESCE(p.cost_cents,0) END),0) "
                         "FROM inv_stock s JOIN products p ON p.id=s.product_id AND p.merchant_id=s.merchant_id WHERE s.merchant_id=? AND s.wh_code=?",
                         (mid, wh)).fetchone()
        per = db.execute("SELECT COUNT(*) FROM (SELECT s.product_id, SUM(s.qty) q FROM inv_stock s WHERE s.merchant_id=? AND s.wh_code=? "
                         "AND s.quality='ok' GROUP BY s.product_id) WHERE q < 0", (mid, wh)).fetchone()[0]
        out_of = db.execute("SELECT COUNT(*) FROM (SELECT s.product_id, SUM(s.qty) q FROM inv_stock s WHERE s.merchant_id=? AND s.wh_code=? "
                            "AND s.quality='ok' GROUP BY s.product_id) WHERE q = 0", (mid, wh)).fetchone()[0]
        nearc = db.execute("SELECT COUNT(*) FROM inv_stock s JOIN inv_batches b ON b.id=s.batch_id WHERE s.merchant_id=? AND s.wh_code=? AND s.qty>0 "
                           "AND b.expiry_date != '' AND b.expiry_date >= ? AND b.expiry_date <= ?", (mid, wh, today, near)).fetchone()[0]
        expc = db.execute("SELECT COUNT(*) FROM inv_stock s JOIN inv_batches b ON b.id=s.batch_id WHERE s.merchant_id=? AND s.wh_code=? AND s.qty>0 "
                          "AND b.expiry_date != '' AND b.expiry_date < ?", (mid, wh, today)).fetchone()[0]
        transit = db.execute("SELECT COALESCE(SUM(l.out_base_qty - l.in_base_qty - l.diff_base_qty),0) FROM transfer_lines l JOIN transfer_orders t "
                             "ON t.tf_no=l.tf_no AND t.merchant_id=l.merchant_id WHERE t.merchant_id=? AND t.to_wh=? AND t.status IN ('shipped','partial')",
                             (mid, wh)).fetchone()[0]
        takediff = db.execute("SELECT COALESCE(SUM(l.diff_qty),0) FROM stocktake_lines l JOIN stocktakes t ON t.st_no=l.st_no AND t.merchant_id=l.merchant_id "
                              "WHERE t.merchant_id=? AND t.wh_code=? AND t.status='approved'", (mid, wh)).fetchone()[0]
        rows.append({'store_code': wh, 'name': names.get(wh, wh), 'sku': agg[0], 'qty': agg[1], 'defect_qty': agg[2],
                     'amount': yuan(agg[3]) if show else None, 'negative_sku': per, 'out_of_stock_sku': out_of, 'near_expiry_batches': nearc,
                     'expired_batches': expc, 'in_transit': transit, 'stocktake_diff': takediff})
    start, end = _range(args)
    mv = [{'type': r[0] or r[1] or '', 'in_qty': r[2], 'out_qty': r[3], 'count': r[4]} for r in db.execute(
        "SELECT m.ref_type, m.type, COALESCE(SUM(CASE WHEN m.change>0 THEN m.change END),0), COALESCE(-SUM(CASE WHEN m.change<0 THEN m.change END),0), "
        "COUNT(*) FROM stock_moves m WHERE m.merchant_id=? AND m.store_code IN (%s) AND substr(m.created_at,1,10) BETWEEN ? AND ? "
        "GROUP BY m.ref_type, m.type ORDER BY 5 DESC" % ph, [mid] + stores + [start, end])]
    slow_days = inum(args.get('slow_days'), 60) or 60
    since = (datetime.now() - timedelta(days=slow_days)).strftime('%Y-%m-%d')
    slow = db.execute("SELECT COUNT(DISTINCT s.product_id) FROM inv_stock s WHERE s.merchant_id=? AND s.wh_code IN (%s) AND s.qty>0 AND s.quality='ok' "
                      "AND NOT EXISTS (SELECT 1 FROM order_items i JOIN orders o ON o.order_no=i.order_no WHERE i.merchant_id=s.merchant_id "
                      "AND i.product_id=s.product_id AND o.store_code=s.wh_code AND o.biz_type='sale' AND substr(o.created_at,1,10) >= ?)" % ph,
                      [mid] + stores + [since]).fetchone()[0]
    cols = [('name', '门店'), ('sku', '有货SKU'), ('qty', '合格品数量'), ('defect_qty', '残次/待处理'), ('negative_sku', '负库存SKU'),
            ('out_of_stock_sku', '缺货SKU'), ('near_expiry_batches', '临期批次'), ('expired_batches', '过期批次'), ('in_transit', '在途'),
            ('stocktake_diff', '盘点差异')]
    if show:
        cols.insert(4, ('amount', '库存金额（成本）'))
    return _result(cols, rows, {'slow_moving_sku': slow, 'slow_days': slow_days}, [
        '数量为基本单位；在途 = 已出库未收货调拨数量（调入方）；盘点差异 = 已审核盘点的差异合计',
        '临期 = 有效期在 %s 天内；滞销 = 有库存但近 %d 天无销售的 SKU（全部门店合计）' % (inum(args.get('near_days'), 30) or 30, slow_days),
        '库存金额需要「毛利报表」权限；批次追溯见 库存 > 批次追溯（模块①）'], moves=mv, start=start, end=end)


# ================================================================ ERP-806 会员报表
def rpt_members(db, actor, args):
    mid = actor['merchant_id']
    stores = scope_stores(db, actor, args.get('store'))
    start, end = _range(args)
    if not stores:
        return _result([], [], {}, [])
    ph = ','.join('?' * len(stores))
    merchant_wide = actor.get('data_scope') == 'merchant' and not args.get('store')
    reg_cond = '' if merchant_wide else " AND reg_store_code IN (%s)" % ph
    reg_p = [] if merchant_wide else list(stores)
    new = dict((r[0] or '', r[1]) for r in db.execute(
        "SELECT reg_store_code, COUNT(*) FROM members WHERE merchant_id=? AND substr(reg_time,1,10) BETWEEN ? AND ?" + reg_cond +
        " GROUP BY reg_store_code", [mid, start, end] + reg_p))
    osql = " AND o.merchant_id=? AND o.store_code IN (%s) AND substr(o.created_at,1,10) BETWEEN ? AND ?" % ph
    op = [mid] + stores + [start, end]
    per_store = dict((r[0], r) for r in db.execute(
        "SELECT o.store_code, COUNT(DISTINCT CASE WHEN o.member_code!='' THEN o.member_code END), "
        "COALESCE(SUM(CASE WHEN o.member_code!='' THEN o.payable_cents END),0), COALESCE(SUM(o.payable_cents),0) "
        "FROM orders o WHERE o.biz_type IN ('sale','refund')" + osql + " GROUP BY o.store_code", op))
    names = dict((r[0], r[1]) for r in db.execute('SELECT code, name FROM stores WHERE merchant_id=?', (mid,)))
    rows = []
    sum_m = sum_all = 0
    for s in stores:
        r = per_store.get(s)
        active, msales, allsales = (r[1], r[2], r[3]) if r else (0, 0, 0)
        sum_m += msales
        sum_all += allsales
        rows.append({'store_code': s, 'name': names.get(s, s), 'new_members': new.get(s, 0), 'active_members': active, 'member_sales': yuan(msales),
                     'total_sales': yuan(allsales), 'contribution': _ratio(msales * 100, allsales)})
    act = db.execute("SELECT COUNT(DISTINCT o.member_code) FROM orders o WHERE o.biz_type='sale' AND o.member_code!=''" + osql, op).fetchone()[0]
    rep = db.execute("SELECT COUNT(*) FROM (SELECT o.member_code FROM orders o WHERE o.biz_type='sale' AND o.member_code!=''" + osql +
                     " GROUP BY o.member_code HAVING COUNT(*) >= 2)", op).fetchone()[0]
    levels = [{'level': r[0] or '未设置', 'count': r[1]} for r in db.execute(
        "SELECT level, COUNT(*) FROM members WHERE merchant_id=? AND status='正常'" + reg_cond + " GROUP BY level ORDER BY 2 DESC", [mid] + reg_p)]
    pts = db.execute("SELECT COALESCE(SUM(CASE WHEN change>0 THEN change END),0), COALESCE(-SUM(CASE WHEN change<0 THEN change END),0) FROM points_log "
                     "WHERE merchant_id=? AND substr(created_at,1,10) BETWEEN ? AND ? AND (store_code IN (%s)%s)" % (ph, " OR store_code=''" if merchant_wide else ''),
                     [mid, start, end] + stores).fetchone()
    cps = db.execute("SELECT COUNT(*), COALESCE(SUM(face_value_cents),0) FROM coupons WHERE merchant_id=? AND status='已核销' "
                     "AND substr(used_at,1,10) BETWEEN ? AND ? AND store_code IN (%s)" % ph, [mid, start, end] + stores).fetchone()
    top = []
    for r in db.execute("SELECT o.member_code, MAX(o.member_name), MAX(o.member_phone), COUNT(CASE WHEN o.biz_type='sale' THEN 1 END), "
                        "COALESCE(SUM(o.payable_cents),0) s FROM orders o WHERE o.member_code!=''" + osql + " GROUP BY o.member_code ORDER BY s DESC LIMIT 20", op):
        top.append({'member_code': r[0], 'name': r[1] or '', 'phone': mask_phone(r[2], actor), 'orders': r[3], 'sales': yuan(r[4])})
    total = {'new_members': sum(new.get(s, 0) for s in stores) + (new.get('', 0) if merchant_wide else 0), 'active_members': act, 'repeat_members': rep,
             'repeat_rate': _ratio(rep * 100, act), 'points_issued': pts[0], 'points_used': pts[1], 'coupons_used': cps[0], 'coupon_amount': yuan(cps[1]),
             'member_sales': yuan(sum_m), 'total_sales': yuan(sum_all), 'contribution': _ratio(sum_m * 100, sum_all)}
    cols = [('name', '门店'), ('new_members', '新增会员'), ('active_members', '活跃会员'), ('member_sales', '会员销售额'), ('total_sales', '总销售额（净）'),
            ('contribution', '会员贡献%')]
    return _result(cols, rows, total, [
        '新增 = 区间内注册（按注册门店）；活跃 = 区间内有销售单的会员；复购 = 区间内销售单 ≥ 2 笔的会员；复购率 = 复购 ÷ 活跃',
        '会员贡献 = 会员净销售额 ÷ 门店净销售额（含退货冲减）；积分发放/使用按积分流水；券核销按核销时间与核销门店',
        '手机号按「查看会员完整手机号」权限脱敏'], levels=levels, top_members=top, start=start, end=end)


# ================================================================ 订单明细（导出主力，大数据量走异步）
def rpt_orders(db, actor, args):
    where, p, start, end = _base(db, actor, args)
    if actor.get('data_scope') == 'self':
        where += ' AND o.cashier_user=?'
        p = p + [actor['username']]
    if args.get('type') in ('sale', 'refund'):
        where += ' AND o.biz_type=?'
        p = p + [args.get('type')]
    rows = []
    for r in db.execute("SELECT o.created_at, o.order_no, o.biz_type, o.store_name, o.terminal_no, o.cashier_name, o.member_name, o.member_phone, "
                        "o.item_qty, o.discount_total_cents, o.payable_cents, o.status, o.orig_order_no FROM orders o%s ORDER BY o.id LIMIT 300000" % where, p):
        rows.append({'created_at': r[0], 'order_no': r[1], 'type': '销售' if r[2] == 'sale' else ('退货' if r[2] == 'refund' else r[2]),
                     'store': r[3], 'terminal_no': r[4] or '', 'cashier': r[5], 'member': r[6] or '', 'phone': mask_phone(r[7], actor),
                     'qty': _qty(r[8]), 'discount': yuan(r[9]), 'payable': yuan(r[10]), 'status': r[11], 'orig_order_no': r[12] or ''})
    cols = [('created_at', '时间'), ('order_no', '单号'), ('type', '类型'), ('store', '门店'), ('terminal_no', '收银台'), ('cashier', '收银员'),
            ('member', '会员'), ('phone', '手机'), ('qty', '件数'), ('discount', '优惠'), ('payable', '金额'), ('status', '状态'), ('orig_order_no', '原单号')]
    return _result(cols, rows, {'count': len(rows)}, ['退货金额为负数；手机号按权限脱敏'], start=start, end=end)


REPORTS = {
    # kind: (权限码, 函数, 中文名)
    'sales': ('report.sales', rpt_sales, '销售汇总'),
    'goods': ('report.goods', rpt_goods, '商品分析'),
    'hourly': ('report.sales', rpt_hourly, '时段分析'),
    'staff': ('report.sales', rpt_staff, '人员/收银台'),
    'pay': ('report.pay', rpt_pay, '支付与现金收支'),
    'shifts': ('report.pay', rpt_shifts, '班次长短款'),
    'stock': ('report.stock', rpt_stock, '库存报表'),
    'members': ('report.member', rpt_members, '会员报表'),
    'orders': ('report.sales', rpt_orders, '订单明细'),
}


def _make_view(kind):
    perm, fn, _ = REPORTS[kind]

    def view():
        db = get_db()
        return ok(fn(db, current_actor(), request.args))
    view.__name__ = 'report_' + kind
    return require_perm(perm)(view)


for _k in REPORTS:
    bp.add_url_rule('/' + _k, 'report_' + _k, _make_view(_k))


# ================================================================ 导出（ERP-808 / NFR-SEC-004）
def _export_check(db, actor, kind, params):
    """导出权限：report.export + 该报表权限。被拒写 security 审计（AC-011）。"""
    if kind not in REPORTS:
        raise BizError('不支持的报表：%s' % kind, 404)
    need = [REPORTS[kind][0], 'report.export']
    miss = [p for p in need if not has_perm(actor, p)]
    if miss:
        audit(db, actor, 'report', '导出被拒', kind, None, {'params': params, 'missing': miss}, '无导出权限', level='security')
        raise BizError('没有导出权限（需要：%s）' % '、'.join(miss), 403)


def _xlsx_rows(res):
    keys = [c['key'] for c in res['columns']]
    rows = [[r.get(k) for k in keys] for r in res['rows']]
    if res.get('total') and any(k in res['total'] for k in keys[1:]):
        rows.append([res['total'].get(k, '合计' if i == 0 else '') for i, k in enumerate(keys)])
    return [c['title'] for c in res['columns']], rows


@bp.route('/<kind>/export')
@api()
def export_sync(kind):
    db = get_db()
    actor = current_actor()
    params = dict(request.args)
    _export_check(db, actor, kind, params)
    res = REPORTS[kind][1](db, actor, request.args)
    head, rows = _xlsx_rows(res)
    if len(rows) > xlsx_io.MAX_SYNC_EXPORT_ROWS:
        raise BizError('数据超过 %d 行，请改用「异步导出」' % xlsx_io.MAX_SYNC_EXPORT_ROWS, 400, 'USE_ASYNC_EXPORT')
    audit(db, actor, 'report', '导出报表', kind, None, {'params': params, 'rows': len(rows)}, '', level='security')
    return xlsx_io.send_xlsx(head, rows, '%s-%s.xlsx' % (REPORTS[kind][2], datetime.now().strftime('%Y%m%d%H%M')), REPORTS[kind][2])


def export_dir(app=None):
    app = app or current_app
    d = app.config.get('EXPORT_DIR') or os.path.join(os.path.dirname(os.path.abspath(app.config['DB_PATH'])), 'exports')
    os.makedirs(d, exist_ok=True)
    return d


def run_export_job(db_path, out_dir, job_id):
    """后台线程：独立连接，按任务创建人的「当前」权限与数据范围重新取数（权限被收回则失败），只读业务表。"""
    from .db import connect
    from .core import _load_actor
    from openpyxl import Workbook
    db = connect(db_path)
    try:
        job = db.execute('SELECT * FROM export_jobs WHERE id=?', (job_id,)).fetchone()
        db.execute("UPDATE export_jobs SET status='running' WHERE id=? AND status='queued'", (job_id,))
        try:
            u = db.execute('SELECT * FROM users WHERE id=? AND merchant_id=?', (job['user_id'], job['merchant_id'])).fetchone()
            m = db.execute('SELECT status FROM merchants WHERE id=?', (job['merchant_id'],)).fetchone()
            if u is None or u['status'] != '启用' or m is None or m['status'] == 'disabled':
                raise BizError('账号或商户已停用')
            actor = _load_actor(db, u)
            perm = REPORTS[job['kind']][0]
            if not (has_perm(actor, perm) and has_perm(actor, 'report.export')):
                raise BizError('导出权限已被收回')
            res = REPORTS[job['kind']][1](db, actor, json.loads(job['params'] or '{}'))
            head, rows = _xlsx_rows(res)
            wb = Workbook(write_only=True)
            ws = wb.create_sheet(REPORTS[job['kind']][2])
            ws.append(head)
            for r in rows:
                ws.append(r)
            path = os.path.join(out_dir, job['job_no'] + '.xlsx')
            wb.save(path)
            exp = (datetime.now() + timedelta(hours=EXPORT_TTL_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
            db.execute("UPDATE export_jobs SET status='done', row_count=?, file_path=?, finished_at=?, expire_at=? WHERE id=?",
                       (len(rows), path, now_s(), exp, job_id))
        except Exception as e:     # noqa: BLE001 —— 任务失败要落库，不能让线程静默死掉
            msg = e.msg if isinstance(e, BizError) else '导出失败：%s' % type(e).__name__
            db.execute("UPDATE export_jobs SET status='failed', error=?, finished_at=? WHERE id=?", (msg[:200], now_s(), job_id))
    finally:
        db.close()


def _job_out(r):
    d = {k: r[k] for k in ('job_no', 'kind', 'status', 'row_count', 'error', 'created_at', 'finished_at', 'expire_at')}
    d['params'] = json.loads(r['params'] or '{}')
    d['kind_name'] = REPORTS.get(r['kind'], ('', None, r['kind']))[2]
    if d['status'] == 'done' and r['expire_at'] and r['expire_at'] < now_s():
        d['status'] = 'expired'
    d['download'] = ('api/report/exports/%s/download' % r['job_no']) if d['status'] == 'done' else ''
    return d


@bp.route('/exports', methods=['POST'])
@api()
def export_create():
    """异步导出：{kind, params:{start,end,store,dim,...}} -> 任务（EX 单号）。同一人同时最多 3 个进行中的任务。"""
    db = get_db()
    actor = current_actor()
    d = request.get_json(silent=True) or {}
    kind = d.get('kind') or ''
    params = dict((str(k), str(v)) for k, v in (d.get('params') or {}).items() if v is not None)
    _export_check(db, actor, kind, params)
    _range(params)              # 参数先做一次轻校验（日期/门店范围），不合法当场返回，不进队列
    if params.get('store'):
        scope_stores(db, actor, params.get('store'))
    with tx(db):
        busy = db.execute("SELECT COUNT(*) FROM export_jobs WHERE merchant_id=? AND user_id=? AND status IN ('queued','running')",
                          (actor['merchant_id'], actor['id'])).fetchone()[0]
        if busy >= 3:
            raise BizError('已有 %d 个导出任务在进行中，请稍后再试' % busy, 429, 'TOO_MANY')
        no = next_no(db, actor['merchant_id'], 'EX', actor.get('store_code') or '')
        db.execute("INSERT INTO export_jobs(job_no, merchant_id, user_id, kind, params, status, created_at) VALUES(?,?,?,?,?,'queued',?)",
                   (no, actor['merchant_id'], actor['id'], kind, json.dumps(params, ensure_ascii=False), now_s()))
        jid = db.execute('SELECT id FROM export_jobs WHERE job_no=?', (no,)).fetchone()[0]
        audit(db, actor, 'report', '创建导出任务', no, None, {'kind': kind, 'params': params}, '', level='security')
    args = (current_app.config['DB_PATH'], export_dir(), jid)
    if current_app.config.get('EXPORT_INLINE'):
        run_export_job(*args)
    else:
        threading.Thread(target=run_export_job, args=args, name='export-' + no, daemon=True).start()
    return ok(_job_out(db.execute('SELECT * FROM export_jobs WHERE id=?', (jid,)).fetchone()))


@bp.route('/exports')
@api()
def export_list():
    db = get_db()
    a = current_actor()
    rows = db.execute('SELECT * FROM export_jobs WHERE merchant_id=? AND user_id=? ORDER BY id DESC LIMIT 50', (a['merchant_id'], a['id'])).fetchall()
    return ok([_job_out(r) for r in rows])


def _my_job(db, a, no):
    r = db.execute('SELECT * FROM export_jobs WHERE job_no=? AND merchant_id=? AND user_id=?', (no, a['merchant_id'], a['id'])).fetchone()
    if r is None:
        raise BizError('导出任务不存在', 404)
    return r


@bp.route('/exports/<no>')
@api()
def export_get(no):
    db = get_db()
    return ok(_job_out(_my_job(db, current_actor(), no)))


@bp.route('/exports/<no>/download')
@api()
def export_download(no):
    db = get_db()
    a = current_actor()
    r = _my_job(db, a, no)
    j = _job_out(r)
    if j['status'] == 'expired':
        raise BizError('导出文件已过期（有效期 %d 小时），请重新导出' % EXPORT_TTL_HOURS, 410, 'EXPIRED')
    if j['status'] != 'done':
        raise BizError('导出任务状态为 %s，还不能下载' % j['status'], 409, 'CONFLICT')
    if not has_perm(a, 'report.export'):
        audit(db, a, 'report', '下载导出被拒', no, None, None, '导出权限已被收回', level='security')
        raise BizError('导出权限已被收回', 403)
    path = r['file_path']
    if not path or not os.path.abspath(path).startswith(os.path.abspath(export_dir())) or not os.path.exists(path):
        raise BizError('导出文件不存在，请重新导出', 404)
    audit(db, a, 'report', '下载导出文件', no, None, {'rows': r['row_count']}, '', level='security')
    return send_file(path, mimetype=xlsx_io.XLSX_MIME, as_attachment=True, download_name='%s-%s.xlsx' % (j['kind_name'], no))
