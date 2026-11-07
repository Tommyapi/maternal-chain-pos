# -*- coding: utf-8 -*-
"""商品查询服务（收银台、APP、促销引擎共用）。Owner：模块①。

    find_sku(db, mid, code) -> {'product': dict, 'unit': '箱', 'rate': 6, 'barcode': '...'} 或 None
        按 商品编码 / 条码（含箱码）精确查找；箱码返回对应单位和换算率（POS-104 / 8.4）。
    sale_status(db, mid, store_code, product) -> '' 可售；否则不可售原因（停售 / 门店未铺货 / 门店停售）
    search(db, mid, store_code, kw, cat=None, limit=60) -> [product dict + 'stock' + 'units']   名称/简称/拼音首字母/货号/条码
    expand_bom(db, mid, product_id, base_qty) -> [{'product_id','base_qty'}]   组合装按 BOM 展开（combo_mode=components）
    ensure_store_warehouse(db, mid, store_code, name)   新建门店时建门店仓（编码 = 门店编码）
    units_of(db, mid, product) -> [{'unit','rate','for_sale','for_purchase','retail','member_price','is_base'}]
    unit_rate(db, mid, product_id, unit) -> int        交易单位 -> 换算率；未知单位报错
    pinyin_initials('奶粉一段') -> 'NFYD'               GB2312 一级字按拼音排序的区间表（无第三方依赖）
"""
from .util import BizError

# GB2312 一级汉字按拼音排序：区位码（高字节*256+低字节-65536）落在哪个区间就是哪个首字母
_PY_BOUNDS = [(-20319, 'A'), (-20283, 'B'), (-19775, 'C'), (-19218, 'D'), (-18710, 'E'), (-18526, 'F'), (-18239, 'G'),
              (-17922, 'H'), (-17417, 'J'), (-16474, 'K'), (-16212, 'L'), (-15640, 'M'), (-15165, 'N'), (-14922, 'O'),
              (-14914, 'P'), (-14630, 'Q'), (-14149, 'R'), (-14090, 'S'), (-13318, 'T'), (-12838, 'W'), (-12556, 'X'),
              (-11847, 'Y'), (-11055, 'Z')]
# 常用但属于 GB2312 二级字（按部首排序，区间表查不到）的母婴行业用字
_PY_EXTRA = {'婧': 'J', '璐': 'L', '妍': 'Y', '萌': 'M', '贝': 'B', '恩': 'E', '喆': 'Z', '赟': 'Y', '珂': 'K', '嫚': 'M'}


def _initial(ch):
    if ch.isascii():
        return ch.upper() if ch.isalnum() else ''
    if ch in _PY_EXTRA:
        return _PY_EXTRA[ch]
    try:
        b = ch.encode('gb2312')
    except UnicodeEncodeError:
        return ''
    if len(b) < 2:
        return ''
    code = b[0] * 256 + b[1] - 65536
    if code < -20319 or code > -10247:
        return ''
    letter = ''
    for bound, ltr in _PY_BOUNDS:
        if code >= bound:
            letter = ltr
        else:
            break
    return letter


def pinyin_initials(name):
    return ''.join(_initial(ch) for ch in (name or ''))[:60]


def find_sku(db, mid, code):
    code = (code or '').strip()
    if not code:
        return None
    p = db.execute('SELECT * FROM products WHERE merchant_id=? AND code=?', (mid, code)).fetchone()
    unit_id, barcode = None, ''
    if p is None:
        b = db.execute('SELECT product_id, unit_id, barcode FROM product_barcodes WHERE merchant_id=? AND barcode=?', (mid, code)).fetchone()
        if b is None:
            return None
        p = db.execute('SELECT * FROM products WHERE id=? AND merchant_id=?', (b['product_id'], mid)).fetchone()
        unit_id, barcode = b['unit_id'], b['barcode']
    if p is None:
        return None
    unit, rate = p['base_unit'] or p['unit'] or '', 1
    if unit_id:
        u = db.execute('SELECT unit, rate FROM product_units WHERE id=? AND product_id=?', (unit_id, p['id'])).fetchone()
        if u is not None:
            unit, rate = u['unit'], int(u['rate'])
    return {'product': dict(p), 'unit': unit, 'rate': rate, 'barcode': barcode}


def sale_status(db, mid, store_code, product):
    if product['status'] == '停售':
        return '商品已停售'
    r = db.execute('SELECT status FROM product_listings WHERE merchant_id=? AND store_code=? AND product_id=?',
                   (mid, store_code, product['id'])).fetchone()
    if r is not None and r['status'] != '在售':
        return '本门店已停售该商品'
    return ''


def product_row(db, mid, product_id):
    r = db.execute('SELECT * FROM products WHERE id=? AND merchant_id=?', (product_id, mid)).fetchone()
    if r is None:
        raise BizError('商品不存在', 404)
    return dict(r)


def base_unit_of(p):
    return p.get('base_unit') or p.get('unit') or ''


def units_of(db, mid, product):
    from .money import yuan
    p = product_row(db, mid, product) if isinstance(product, int) else dict(product)
    out = [{'id': None, 'unit': base_unit_of(p), 'rate': 1, 'for_sale': 1, 'for_purchase': 1, 'is_base': True,
            'retail': yuan(p.get('retail_cents')), 'member_price': yuan(p.get('member_price_cents'))}]
    for u in db.execute('SELECT * FROM product_units WHERE merchant_id=? AND product_id=? ORDER BY rate, id', (mid, p['id'])):
        if u['unit'] == out[0]['unit']:
            continue
        out.append({'id': u['id'], 'unit': u['unit'], 'rate': int(u['rate']), 'for_sale': u['for_sale'], 'for_purchase': u['for_purchase'],
                    'is_base': False, 'retail': yuan(u['retail_cents']), 'member_price': yuan(u['member_price_cents']),
                    'status': u['status']})
    return out


def unit_rate(db, mid, product_id, unit):
    """交易单位 -> 换算率（基本单位 = 1）。空单位按基本单位；未知单位报错（DATA-002「无效单位」）。"""
    p = product_row(db, mid, product_id)
    unit = (unit or '').strip()
    if not unit or unit == base_unit_of(p):
        return 1
    r = db.execute('SELECT rate FROM product_units WHERE merchant_id=? AND product_id=? AND unit=?', (mid, product_id, unit)).fetchone()
    if r is None:
        raise BizError('商品 %s 没有单位「%s」' % (p['name'], unit))
    return int(r[0])


def search(db, mid, store_code, kw, cat=None, limit=60):
    """名称/简称/拼音首字母/货号/条码 模糊搜索（收银台、APP、后台共用）。stock = 该门店可售数量。"""
    from . import inventory
    kw = (kw or '').strip()
    sql, args = 'SELECT p.* FROM products p WHERE p.merchant_id=?', [mid]
    if kw:
        like = '%' + kw + '%'
        sql += (' AND (p.name LIKE ? OR p.short_name LIKE ? OR p.code LIKE ? OR p.pinyin LIKE ? OR p.spu_code = ? '
                'OR p.id IN (SELECT product_id FROM product_barcodes WHERE merchant_id=? AND barcode LIKE ?))')
        args += [like, like, like, '%' + kw.upper() + '%', kw, mid, like]
    if cat:
        sql += ' AND (p.category=? OR p.category_id IN (SELECT id FROM categories WHERE merchant_id=? AND (code=? OR name=?)))'
        args += [cat, mid, cat, cat]
    sql += " ORDER BY CASE WHEN p.code=? THEN 0 WHEN p.status='在售' THEN 1 ELSE 2 END, p.code LIMIT ?"
    args += [kw, int(limit)]
    out = []
    for r in db.execute(sql, args).fetchall():
        d = dict(r)
        d['units'] = units_of(db, mid, d)
        d['barcodes'] = [x[0] for x in db.execute('SELECT barcode FROM product_barcodes WHERE merchant_id=? AND product_id=? ORDER BY id',
                                                  (mid, d['id']))]
        d['stock'] = inventory.available(db, mid, store_code, d['id']) if store_code else None
        d['sale_status'] = sale_status(db, mid, store_code, d) if store_code else ''
        out.append(d)
    return out


def expand_bom(db, mid, product_id, base_qty):
    """组合装：combo_mode=components 时按 BOM 展开为组件行；否则原样返回自身（self/none 扣自身库存）。"""
    p = product_row(db, mid, product_id)
    base_qty = int(base_qty)
    if p.get('combo_mode') != 'components':
        return [{'product_id': p['id'], 'base_qty': base_qty}]
    rows = db.execute('SELECT component_id, qty FROM product_bom WHERE merchant_id=? AND product_id=? ORDER BY id', (mid, p['id'])).fetchall()
    if not rows:
        raise BizError('组合装 %s 没有设置组件（BOM）' % p['name'])
    return [{'product_id': r['component_id'], 'base_qty': int(r['qty']) * base_qty} for r in rows]


def ensure_store_warehouse(db, mid, store_code, name):
    db.execute("INSERT OR IGNORE INTO warehouses(merchant_id, code, name, kind, store_code, sellable) VALUES(?,?,?,'store',?,1)",
               (mid, store_code, name or store_code, store_code))
    return store_code


# ================================================================ 模块①视图共用小工具（仓库数据范围 / 分页 / 入参）
def body():
    from flask import request
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def page_query(db, sql, args, page_size_args, order_by, conv=dict):
    """sql = 'FROM ... WHERE ...'；返回契约分页 {page,size,total,items}。"""
    from .util import inum
    page = max(1, inum(page_size_args.get('page'), 1))
    size = min(200, max(1, inum(page_size_args.get('size'), 20)))
    total = db.execute('SELECT COUNT(*) ' + sql, args).fetchone()[0]
    rows = db.execute('SELECT * ' + sql + ' ORDER BY ' + order_by + ' LIMIT ? OFFSET ?', list(args) + [size, (page - 1) * size]).fetchall()
    return {'page': page, 'size': size, 'total': total, 'items': [conv(r) for r in rows]}


def wh_scope(db, actor):
    """当前账号可访问的仓库编码：门店仓按门店数据范围；总部仓/无门店的残次仓只有全商户范围（data_scope=merchant）可见。"""
    from .core import scope_stores
    stores = set(scope_stores(db, actor))
    full = (actor.get('data_scope') == 'merchant')
    out = []
    for r in db.execute('SELECT code, kind, store_code FROM warehouses WHERE merchant_id=? ORDER BY kind, code', (actor['merchant_id'],)):
        if full or (r['store_code'] and r['store_code'] in stores):
            out.append(r['code'])
    return out


def check_wh(db, actor, code, label='仓库'):
    """仓库存在、属于本商户、在数据范围内；否则 404 / 403 SCOPE_DENIED。返回 warehouses 行 dict。"""
    code = (code or '').strip()
    r = db.execute('SELECT * FROM warehouses WHERE merchant_id=? AND code=?', (actor['merchant_id'], code)).fetchone()
    if r is None:
        raise BizError('%s不存在：%s' % (label, code or '（未填）'), 404)
    if code not in wh_scope(db, actor):
        raise BizError('无权操作%s：%s' % (label, code), 403, 'SCOPE_DENIED')
    return dict(r)


def resolve_product(db, mid, ref):
    """商品 id / 编码 / 条码 -> products 行 dict（跨商户一律当不存在）。"""
    if ref is None or ref == '':
        raise BizError('请选择商品')
    if isinstance(ref, int) and not isinstance(ref, bool):     # 整数 = 商品 id；字符串 = 编码/条码
        r = db.execute('SELECT * FROM products WHERE id=? AND merchant_id=?', (int(ref), mid)).fetchone()
        if r is not None:
            return dict(r)
    f = find_sku(db, mid, str(ref))
    if f is None:
        raise BizError('商品不存在：%s' % ref, 404)
    return f['product']
