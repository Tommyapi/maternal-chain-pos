# -*- coding: utf-8 -*-
"""系统配置服务（同进程调用，docs/API-CONTRACT.md §3.7）。Owner：模块④。

    dict_items(db, mid, dict_type, include_disabled=False) -> [{'code','name','status'}]      数据字典（退货/报损/现金收支原因、单位…）
    get_print_template(db, mid, store_code, kind) -> {'width','copies','content':{...},'version','source'}
        kind: sale / refund / shift / label；门店模板 > 商户默认模板 > 内置默认（取收银参数里的抬头/底部文字/纸宽）。
        🔴 打印历史订单一律用订单成交快照（③ orders.snapshot / 订单字段），模板只决定版式，修改模板不影响历史订单内容（ERP-902）。
    alert_params(db, mid) -> {...}                                                               预警参数（ERP-903），①的预警任务读取
"""
import json

DICT_TYPES = {
    'refund_reason': '退货原因', 'loss_reason': '报损原因', 'overflow_reason': '报溢原因', 'cash_reason': '现金收支原因',
    'unit': '计量单位', 'pay_custom': '自定义支付方式',
}

PRINT_KINDS = {'sale': '销售小票', 'refund': '退货小票', 'shift': '交班报表', 'label': '价签'}
PRINT_FIELDS = {
    # 可勾选的打印字段（content.fields）
    'store_name': '门店名称', 'store_address': '门店地址', 'store_phone': '门店电话', 'order_no': '单号', 'cashier': '收银员',
    'member': '会员（手机号脱敏）', 'points': '本单积分/积分余额', 'discount_detail': '优惠明细', 'pay_detail': '支付明细',
    'batch_no': '批号/效期', 'barcode': '单号条码', 'guide': '导购',
}

ALERT_DEFAULT = {
    'near_expiry_days': [30, 60, 90],     # 临期分级（天）
    'safety_stock_default': 0,             # 默认安全库存（基本单位，0=不预警）
    'slow_moving_days': 60,                # 滞销：有库存且 N 天无销售
    'notify_time': '09:00',                # 每日预警消息推送时间（由总脑挂 cron 调①的预警任务）
    'receivers': ['stock.view'],           # 接收人：拥有这些权限码且门店在范围内的人（notify.notify_perm）
    'overrides': [],                       # 按分类/商品覆盖：[{'scope':'category'|'product','value':'奶粉'|'P1','safety_stock':10,'near_expiry_days':60}]
}


def dict_items(db, mid, dict_type, include_disabled=False):
    sql = 'SELECT code, name, status, sort FROM dict_items WHERE merchant_id=? AND dict_type=?'
    if not include_disabled:
        sql += " AND status='启用'"
    return [{'code': r['code'], 'name': r['name'], 'status': r['status'], 'sort': r['sort']}
            for r in db.execute(sql + ' ORDER BY sort, code', (mid, dict_type))]


def default_content(cfg, kind):
    base = {'title': cfg.get('receipt_title') or '', 'footer': cfg.get('receipt_footer') or '',
            'fields': ['store_name', 'store_address', 'store_phone', 'order_no', 'cashier', 'member', 'points', 'discount_detail', 'pay_detail']}
    if kind == 'shift':
        base['fields'] = ['store_name', 'cashier']
        base['footer'] = ''
    elif kind == 'label':
        base = {'title': '', 'footer': '', 'fields': ['barcode'], 'size': '40x30'}
    elif kind == 'refund':
        base['title'] = (cfg.get('receipt_title') or '') + ' 退货单'
    return base


def get_print_template(db, mid, store_code, kind):
    from .core import get_config
    for sc, src in (((store_code or ''), 'store'), ('', 'merchant')):
        if src == 'store' and not sc:
            continue
        r = db.execute("SELECT * FROM print_templates WHERE merchant_id=? AND store_code=? AND kind=? AND status='启用'", (mid, sc, kind)).fetchone()
        if r is not None:
            try:
                content = json.loads(r['content'] or '{}')
            except ValueError:
                content = {}
            return {'width': r['width'], 'copies': r['copies'], 'content': content, 'version': r['version'], 'source': src,
                    'store_code': sc, 'kind': kind}
    cfg = get_config(db, mid)
    return {'width': int(cfg.get('receipt_width') or 58), 'copies': 1, 'content': default_content(cfg, kind), 'version': 0, 'source': 'builtin',
            'store_code': '', 'kind': kind}


def alert_params(db, mid):
    r = db.execute("SELECT value FROM settings WHERE merchant_id=? AND key='alert_params'", (mid,)).fetchone()
    p = json.loads(json.dumps(ALERT_DEFAULT))
    if r:
        try:
            p.update(json.loads(r[0]))
        except ValueError:
            pass
    return p
