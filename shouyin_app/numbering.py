# -*- coding: utf-8 -*-
"""单号规则（ERP-901 / 规格书 9.7）。Owner：架构（冻结）。

格式：<类型 1~2 位字母><商户 id 3 位><门店/仓 4 位><YYMMDD><当日流水 5 位>
例：S 001 8007 261105 00001 -> 'S001800726110500001'

- 商户 + 门店/仓 + 日期内自增，number_seq 表在调用方事务内 +1（必须在 BEGIN IMMEDIATE 事务里调用）。
- 已提交的单号永不回收：单据不物理删除；事务回滚时号也一起回滚，从未对外出现过，不算回收。
- 各单据表的单号列都有 UNIQUE 约束兜底。
- 门店编码不足 4 位左补 0，超过取后 4 位；没有门店（总部单据）用 0000。商户 id > 999 时自然变长，仍唯一。
"""
from datetime import datetime

DOC_TYPES = {
    'S': '销售单', 'R': '退货单', 'X': '换货单', 'CZ': '储值充值', 'CS': '现金收支', 'SH': '班次', 'PY': '支付流水', 'RF': '退款流水',
    'PO': '采购单', 'GR': '收货入库单', 'PR': '采购退货单', 'RQ': '要货单', 'TF': '调拨单', 'ST': '盘点单',
    'LS': '报损单', 'OV': '报溢单', 'AJ': '库存调整单', 'SP': '拆零/组装单', 'RC': '召回单',
    'PA': '调价单', 'AC': '促销活动', 'CP': '优惠券', 'AP': '审批单', 'EX': '导出任务', 'MC': '商户开通',
}


def _scope(store_code):
    s = ''.join(ch for ch in str(store_code or '') if ch.isalnum())
    if not s:
        return '0000'
    return s[-4:].rjust(4, '0')


def next_no(db, merchant_id, doc_type, store_code='', now=None):
    """取下一个单号。必须在调用方的写事务里调用（tx(db) 内）。"""
    if doc_type not in DOC_TYPES:
        raise ValueError('未登记的单据类型：%s' % doc_type)
    now = now or datetime.now()
    day = now.strftime('%y%m%d')
    scope = _scope(store_code)
    mid = int(merchant_id)
    db.execute('INSERT INTO number_seq(merchant_id, doc_type, scope, day, seq) VALUES(?,?,?,?,1) '
               'ON CONFLICT(merchant_id, doc_type, scope, day) DO UPDATE SET seq = seq + 1', (mid, doc_type, scope, day))
    seq = db.execute('SELECT seq FROM number_seq WHERE merchant_id=? AND doc_type=? AND scope=? AND day=?',
                     (mid, doc_type, scope, day)).fetchone()[0]
    return '%s%03d%s%s%05d' % (doc_type, mid, scope, day, seq)


def parse_no(no):
    """反解单号（报表/排障用）。返回 dict 或 None。"""
    import re
    m = re.match(r'^([A-Z]{1,2})(\d{3})(\w{4})(\d{6})(\d{5,})$', no or '')
    if not m or m.group(1) not in DOC_TYPES:
        return None
    return {'doc_type': m.group(1), 'merchant_id': int(m.group(2)), 'scope': m.group(3), 'day': m.group(4), 'seq': int(m.group(5))}
