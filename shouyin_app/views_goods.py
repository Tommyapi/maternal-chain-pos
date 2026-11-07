# -*- coding: utf-8 -*-
"""商品中心：档案/规格/条码/多单位/组合装/铺货/批量调价/导入导出/分类品牌/变更记录。

Owner：模块①（只有该模块 agent 能改本文件，见 docs/ARCH.md §2）。
规格书：ERP-005 ERP-101~109 DATA-001~003、8.4
接口清单与入参出参：docs/API-CONTRACT.md §4.2；返回 {ok:true,data} / {ok:false,code,msg}；权限一律 @require_perm(...)。
"""
import json

from flask import Blueprint, request

from . import goods_svc, xlsx_io
from .core import get_db, tx, require_perm, current_actor, has_perm, scope_stores, audit, ok, cost_view
from .goods_svc import body, page_query, resolve_product, pinyin_initials, units_of
from .money import to_cents, yuan, MoneyError
from .util import BizError, now_s, inum

bp = Blueprint('goods', __name__, url_prefix='/api/goods')

STATUS = ('在售', '停售')
COMBO_MODES = {'none': '普通商品', 'self': '组合装（扣自身库存）', 'components': '组合装（按组件扣库存）'}
PRICE_KEYS = ('retail_cents', 'member_price_cents')
AUDIT_KEYS = ('code', 'name', 'short_name', 'spu_code', 'spec', 'spec_attrs', 'category', 'brand', 'base_unit', 'status',
              'retail_cents', 'member_price_cents', 'cost_cents', 'batch_managed', 'shelf_life_days', 'near_expiry_days',
              'allow_discount', 'allow_points', 'combo_mode', 'supplier_id', 'image')


def _cents(v, label):
    if v is None or v == '':
        return None
    try:
        c = to_cents(v)
    except MoneyError:
        raise BizError('%s金额格式不正确' % label)
    if c < 0:
        raise BizError('%s不能为负' % label)
    return c


def _flag(v, default):
    if v is None or v == '':
        return default
    if isinstance(v, str):
        return 0 if v.strip() in ('0', '否', 'false', 'False', 'N', 'n') else 1
    return 1 if v else 0


def _product_out(db, actor, p, store=None, detail=False):
    d = dict(p)
    cost = d.pop('cost_cents', None)
    d.pop('cost', None)
    d['cost'] = cost_view(actor, cost)                       # 字段权限：无 goods.cost.view 不返回明文
    d['retail'] = yuan(d.get('retail_cents'))
    d['member_price'] = yuan(d.get('member_price_cents'))
    try:
        d['spec_attrs'] = json.loads(d.get('spec_attrs') or '{}')
    except ValueError:
        d['spec_attrs'] = {}
    d['units'] = units_of(db, actor['merchant_id'], p)
    d['barcodes'] = [dict(barcode=r['barcode'], unit=(r['unit'] or d.get('base_unit') or d.get('unit') or ''))
                     for r in db.execute('SELECT b.barcode, u.unit FROM product_barcodes b LEFT JOIN product_units u ON u.id=b.unit_id '
                                         'WHERE b.merchant_id=? AND b.product_id=? ORDER BY b.id', (actor['merchant_id'], p['id']))]
    if store:
        from . import inventory
        d['stock'] = inventory.available(db, actor['merchant_id'], store, p['id'])
        d['sale_status'] = goods_svc.sale_status(db, actor['merchant_id'], store, p)
    if detail:
        mid = actor['merchant_id']
        d['bom'] = [dict(r) for r in db.execute('SELECT b.component_id, b.qty, p.code, p.name, p.base_unit FROM product_bom b '
                                                 'JOIN products p ON p.id=b.component_id WHERE b.merchant_id=? AND b.product_id=? ORDER BY b.id',
                                                 (mid, p['id']))]
        stores = scope_stores(db, actor)
        d['listings'] = [dict(r) for r in db.execute('SELECT store_code, status, updated_at FROM product_listings WHERE merchant_id=? AND product_id=? '
                                                      'ORDER BY store_code', (mid, p['id'])) if r['store_code'] in stores]
        d['history'] = [dict(created_at=r['created_at'], username=r['username'], action=r['action'],
                             before=_hide_cost(actor, r['before_json']), after=_hide_cost(actor, r['after_json']))
                        for r in db.execute("SELECT * FROM audit_logs WHERE merchant_id=? AND module='goods' AND ref_no=? ORDER BY id DESC LIMIT 50",
                                            (mid, p['code']))]
        if has_perm(actor, 'goods.cost.view'):
            d['purchase_prices'] = [dict(supplier=r['sname'], unit=r['unit'], rate=r['rate'], price=yuan(r['price_cents']), src_no=r['src_no'],
                                         created_at=r['created_at'])
                                    for r in db.execute('SELECT sp.*, s.name sname FROM supplier_prices sp LEFT JOIN suppliers s ON s.id=sp.supplier_id '
                                                        'WHERE sp.merchant_id=? AND sp.product_id=? ORDER BY sp.id DESC LIMIT 30', (mid, p['id']))]
    return d


def _hide_cost(actor, js):
    try:
        v = json.loads(js or '{}')
    except ValueError:
        return {}
    if isinstance(v, dict) and not has_perm(actor, 'goods.cost.view'):
        v.pop('cost_cents', None)
    return v


def _get(db, actor, pid):
    r = db.execute('SELECT * FROM products WHERE id=? AND merchant_id=?', (pid, actor['merchant_id'])).fetchone()
    if r is None:
        raise BizError('商品不存在', 404)
    return r


# ---------------------------------------------------------------- 分类 / 品牌
def ensure_category(db, mid, name):
    name = (name or '').strip()
    if not name:
        return None
    r = db.execute('SELECT id FROM categories WHERE merchant_id=? AND (name=? OR code=?)', (mid, name, name)).fetchone()
    if r:
        return r[0]
    n = db.execute('SELECT COUNT(*) FROM categories WHERE merchant_id=?', (mid,)).fetchone()[0] + 1
    code = 'C%03d' % n
    while db.execute('SELECT 1 FROM categories WHERE merchant_id=? AND code=?', (mid, code)).fetchone():
        n += 1
        code = 'C%03d' % n
    return db.execute('INSERT INTO categories(merchant_id, code, name) VALUES(?,?,?)', (mid, code, name)).lastrowid


def ensure_brand(db, mid, name):
    name = (name or '').strip()
    if not name:
        return None
    db.execute('INSERT OR IGNORE INTO brands(merchant_id, name) VALUES(?,?)', (mid, name))
    return db.execute('SELECT id FROM brands WHERE merchant_id=? AND name=?', (mid, name)).fetchone()[0]


@bp.route('/categories')
@require_perm('goods.view')
def categories():
    db, a = get_db(), current_actor()
    return ok([dict(r) for r in db.execute('SELECT * FROM categories WHERE merchant_id=? ORDER BY sort, code', (a['merchant_id'],))])


@bp.route('/categories', methods=['POST'])
@require_perm('goods.edit')
def categories_save():
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    name = (d.get('name') or '').strip()
    if not name:
        raise BizError('请填写分类名称')
    with tx(db):
        cid = inum(d.get('id'))
        if cid:
            old = db.execute('SELECT * FROM categories WHERE id=? AND merchant_id=?', (cid, mid)).fetchone()
            if old is None:
                raise BizError('分类不存在', 404)
            if db.execute('SELECT 1 FROM categories WHERE merchant_id=? AND name=? AND id!=?', (mid, name, cid)).fetchone():
                raise BizError('分类名称已存在')
            db.execute('UPDATE categories SET name=?, parent_id=?, near_expiry_days=?, safety_stock=?, status=?, sort=? WHERE id=?',
                       (name, inum(d.get('parent_id')) or None, inum(d.get('near_expiry_days')), inum(d.get('safety_stock')),
                        d.get('status') if d.get('status') in ('启用', '停用') else old['status'], inum(d.get('sort')), cid))
            db.execute('UPDATE products SET category=? WHERE merchant_id=? AND category_id=?', (name, mid, cid))
        else:
            if db.execute('SELECT 1 FROM categories WHERE merchant_id=? AND name=?', (mid, name)).fetchone():
                raise BizError('分类名称已存在')
            cid = ensure_category(db, mid, name)
            db.execute('UPDATE categories SET parent_id=?, near_expiry_days=?, safety_stock=? WHERE id=?',
                       (inum(d.get('parent_id')) or None, inum(d.get('near_expiry_days')), inum(d.get('safety_stock')), cid))
        audit(db, a, 'goods', '维护分类', name, None, d)
    return ok(dict(db.execute('SELECT * FROM categories WHERE id=?', (cid,)).fetchone()))


@bp.route('/brands')
@require_perm('goods.view')
def brands():
    db, a = get_db(), current_actor()
    return ok([dict(r) for r in db.execute('SELECT * FROM brands WHERE merchant_id=? ORDER BY name', (a['merchant_id'],))])


@bp.route('/brands', methods=['POST'])
@require_perm('goods.edit')
def brands_save():
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        bid = ensure_brand(db, a['merchant_id'], d.get('name'))
        if not bid:
            raise BizError('请填写品牌名称')
        if d.get('status') in ('启用', '停用'):
            db.execute('UPDATE brands SET status=? WHERE id=?', (d['status'], bid))
        audit(db, a, 'goods', '维护品牌', d.get('name'), None, d)
    return ok(dict(db.execute('SELECT * FROM brands WHERE id=?', (bid,)).fetchone()))


# ---------------------------------------------------------------- 列表 / 详情
@bp.route('')
@require_perm('goods.view')
def goods_list():
    db, a = get_db(), current_actor()
    mid, q = a['merchant_id'], request.args
    store = (q.get('store') or '').strip()
    if store:
        scope_stores(db, a, store)
    sql, args = 'FROM products p WHERE p.merchant_id=?', [mid]
    kw = (q.get('kw') or '').strip()
    if kw:
        like = '%' + kw + '%'
        sql += (' AND (p.name LIKE ? OR p.short_name LIKE ? OR p.code LIKE ? OR p.pinyin LIKE ? OR p.spu_code=? '
                'OR p.id IN (SELECT product_id FROM product_barcodes WHERE merchant_id=? AND barcode LIKE ?))')
        args += [like, like, like, '%' + kw.upper() + '%', kw, mid, like]
    if q.get('cat'):
        sql += ' AND (p.category=? OR p.category_id IN (SELECT id FROM categories WHERE merchant_id=? AND (code=? OR name=?)))'
        args += [q['cat'], mid, q['cat'], q['cat']]
    if q.get('brand'):
        sql += ' AND p.brand=?'
        args.append(q['brand'])
    if q.get('status') in STATUS:
        sql += ' AND p.status=?'
        args.append(q['status'])
    if q.get('no_price') == '1':
        sql += ' AND COALESCE(p.retail_cents,0) <= 0'
    if q.get('batch') == '1':
        sql += ' AND p.batch_managed=1'
    data = page_query(db, sql, args, q, 'p.code', conv=lambda r: _product_out(db, a, r, store))
    return ok(data)


@bp.route('/<int:pid>')
@require_perm('goods.view')
def goods_get(pid):
    db, a = get_db(), current_actor()
    store = (request.args.get('store') or '').strip()
    if store:
        scope_stores(db, a, store)
    return ok(_product_out(db, a, _get(db, a, pid), store, detail=True))


@bp.route('/search')
@require_perm('goods.view', 'pos.checkout')
def goods_search():
    """名称/简称/拼音首字母/货号/条码 模糊搜索（goods_svc.search；收银台、APP、后台选择商品共用）。"""
    db, a = get_db(), current_actor()
    store = (request.args.get('store') or a.get('store_code') or '').strip()
    if store:
        scope_stores(db, a, store)
    rows = goods_svc.search(db, a['merchant_id'], store, request.args.get('kw'), request.args.get('cat'), min(inum(request.args.get('limit'), 30), 100))
    out = []
    for r in rows:
        r.pop('cost', None)
        c = r.pop('cost_cents', None)
        r['cost'] = cost_view(a, c)
        r['retail'] = yuan(r.get('retail_cents'))
        r['member_price'] = yuan(r.get('member_price_cents'))
        out.append(r)
    return ok(out)


# ---------------------------------------------------------------- 保存（新增 / 修改）
def save_product(db, actor, d, pid=None):
    """新增/修改商品（含多单位、条码、BOM）。返回 product id。调用方在 tx 内。"""
    mid = actor['merchant_id']
    old = dict(_get(db, actor, pid)) if pid else None
    name = (d.get('name') if 'name' in d else (old or {}).get('name') or '')
    name = (name or '').strip()
    if not name:
        raise BizError('请填写商品名称')
    code = (d.get('code') or (old or {}).get('code') or '').strip()
    if not code:
        n = db.execute('SELECT COUNT(*) FROM products WHERE merchant_id=?', (mid,)).fetchone()[0] + 1
        code = 'SP%06d' % n
        while db.execute('SELECT 1 FROM products WHERE merchant_id=? AND code=?', (mid, code)).fetchone():
            n += 1
            code = 'SP%06d' % n
    dup = db.execute('SELECT id FROM products WHERE merchant_id=? AND code=? AND id IS NOT ?', (mid, code, pid)).fetchone()
    if dup:
        raise BizError('商品编码（货号）已存在：%s' % code)

    def pick(k, default=''):
        if k in d and d[k] is not None:
            return d[k]
        return (old or {}).get(k, default) if old else default

    base_unit = (pick('base_unit') or pick('unit') or '').strip() or '件'
    retail = _cents(d.get('retail'), '零售价') if 'retail' in d else (old or {}).get('retail_cents') or 0
    mp = _cents(d.get('member_price'), '会员价') if 'member_price' in d else (old or {}).get('member_price_cents')
    retail = retail or 0
    if not mp or mp > retail:
        mp = retail
    cost = (old or {}).get('cost_cents') or 0
    if d.get('cost') not in (None, ''):
        if not has_perm(actor, 'goods.cost.view'):
            raise BizError('没有成本价权限，不能修改成本价', 403)
        cost = _cents(d.get('cost'), '成本价')
    price_changed = (old is None and (retail or mp)) or (old is not None and ((old.get('retail_cents') or 0) != retail or
                                                                                  (old.get('member_price_cents') or 0) != mp))
    combo = pick('combo_mode', 'none') or 'none'
    if combo not in COMBO_MODES:
        raise BizError('组合装模式只能是：%s' % '、'.join(COMBO_MODES))
    attrs = pick('spec_attrs', '')
    if isinstance(attrs, dict):
        attrs = json.dumps(dict((str(k)[:20], str(v)[:40]) for k, v in attrs.items() if str(k).strip()), ensure_ascii=False)
    status = pick('status', '在售')
    if status not in STATUS:
        raise BizError('状态只能是 在售 / 停售')
    category = (pick('category') or '').strip()
    brand = (pick('brand') or '').strip()
    supplier_id = inum(pick('supplier_id', None)) or None
    if supplier_id and not db.execute('SELECT 1 FROM suppliers WHERE id=? AND merchant_id=?', (supplier_id, mid)).fetchone():
        raise BizError('供应商不存在')
    shelf, near = inum(pick('shelf_life_days', 0)), inum(pick('near_expiry_days', 0))
    if shelf < 0 or near < 0:
        raise BizError('保质期/临期天数不能为负')
    row = dict(code=code, name=name[:100], short_name=(pick('short_name') or '')[:40], spu_code=(pick('spu_code') or '').strip()[:40],
               spec=(pick('spec') or '').strip()[:60], spec_attrs=attrs or '', category=category, category_id=ensure_category(db, mid, category),
               brand=brand, brand_id=ensure_brand(db, mid, brand), image=(pick('image') or '')[:300], supplier_id=supplier_id,
               unit=base_unit, base_unit=base_unit, status=status, retail_cents=retail, member_price_cents=mp, cost_cents=cost,
               batch_managed=_flag(d.get('batch_managed'), (old or {}).get('batch_managed', 0)), shelf_life_days=shelf, near_expiry_days=near,
               allow_discount=_flag(d.get('allow_discount'), (old or {}).get('allow_discount', 1)),
               allow_points=_flag(d.get('allow_points'), (old or {}).get('allow_points', 1)), combo_mode=combo, pinyin=pinyin_initials(name),
               updated_at=now_s())
    if price_changed and not has_perm(actor, 'goods.price.edit'):
        raise BizError('没有修改商品价格的权限（goods.price.edit）', 403)
    if old is not None and old.get('batch_managed') and not row['batch_managed']:
        if db.execute('SELECT 1 FROM inv_stock WHERE merchant_id=? AND product_id=? AND batch_id>0 AND qty!=0', (mid, pid)).fetchone():
            raise BizError('该商品还有批次库存，不能关闭批次管理')
    cols = sorted(row)
    if old is None:
        row['created_at'] = now_s()
        cols = sorted(row)
        pid = db.execute('INSERT INTO products(merchant_id,%s) VALUES(?,%s)' % (','.join(cols), ','.join('?' * len(cols))),
                         [mid] + [row[c] for c in cols]).lastrowid
    else:
        db.execute('UPDATE products SET %s WHERE id=? AND merchant_id=?' % ','.join('%s=?' % c for c in cols), [row[c] for c in cols] + [pid, mid])
    _save_units_barcodes(db, actor, pid, base_unit, d)
    if 'bom' in d or combo == 'components' or (old is not None and old.get('combo_mode') != combo):
        _save_bom(db, mid, pid, combo, d.get('bom'))
    new = dict(_get(db, actor, pid))
    before = dict((k, old.get(k)) for k in AUDIT_KEYS) if old else None
    after = dict((k, new.get(k)) for k in AUDIT_KEYS)
    if old is None:
        action = '新增商品'
    elif price_changed:
        action = '商品调价'
    elif old.get('status') != new.get('status'):
        action = '商品状态变更'
    else:
        action = '修改商品'
    if before:
        changed = dict((k, v) for k, v in after.items() if before.get(k) != v)
        before = dict((k, before[k]) for k in changed)
        after = changed
    if old is None or after:
        audit(db, actor, 'goods', action, code, before, after, d.get('reason') or '')
    return pid


def _save_units_barcodes(db, actor, pid, base_unit, d):
    mid = actor['merchant_id']
    if 'units' in d:
        seen = {}
        for u in (d.get('units') or []):
            unit = (u.get('unit') or '').strip()
            if not unit or unit == base_unit:
                continue
            rate = inum(u.get('rate'))
            if rate < 2:
                raise BizError('单位「%s」的换算率必须是大于 1 的整数（1 %s = ? %s）' % (unit, unit, base_unit))
            if unit in seen:
                raise BizError('单位「%s」重复' % unit)
            seen[unit] = rate
            rc = _cents(u.get('retail'), '%s 零售价' % unit)
            mc = _cents(u.get('member_price'), '%s 会员价' % unit)
            oldu = db.execute('SELECT * FROM product_units WHERE product_id=? AND unit=?', (pid, unit)).fetchone()
            if oldu is not None and ((oldu['retail_cents'] or None) != rc or (oldu['member_price_cents'] or None) != mc) \
                    and not has_perm(actor, 'goods.price.edit'):
                raise BizError('没有修改商品价格的权限（goods.price.edit）', 403)
            if oldu is None:
                db.execute('INSERT INTO product_units(merchant_id, product_id, unit, rate, for_sale, for_purchase, retail_cents, member_price_cents) '
                           'VALUES(?,?,?,?,?,?,?,?)', (mid, pid, unit, rate, _flag(u.get('for_sale'), 1), _flag(u.get('for_purchase'), 1), rc, mc))
            else:
                if oldu['rate'] != rate:
                    audit(db, actor, 'goods', '换算率变更', db.execute('SELECT code FROM products WHERE id=?', (pid,)).fetchone()[0],
                          {'unit': unit, 'rate': oldu['rate']}, {'unit': unit, 'rate': rate}, '只影响新业务，历史单据保留原换算快照')
                db.execute('UPDATE product_units SET rate=?, for_sale=?, for_purchase=?, retail_cents=?, member_price_cents=?, status=? WHERE id=?',
                           (rate, _flag(u.get('for_sale'), 1), _flag(u.get('for_purchase'), 1), rc, mc,
                            u.get('status') if u.get('status') in ('启用', '停用') else '启用', oldu['id']))
        gone = [r['id'] for r in db.execute('SELECT id, unit FROM product_units WHERE product_id=?', (pid,)) if r['unit'] not in seen]
        for uid in gone:
            db.execute('UPDATE product_barcodes SET unit_id=NULL WHERE unit_id=?', (uid,))
            db.execute('DELETE FROM product_units WHERE id=?', (uid,))
    if 'barcodes' in d or 'barcode' in d:
        raw = d.get('barcodes')
        if raw is None:
            raw = [x for x in str(d.get('barcode') or '').replace('，', ',').replace(' ', ',').split(',')]
        items = []
        for b in raw:
            if isinstance(b, dict):
                items.append(((b.get('barcode') or '').strip(), (b.get('unit') or '').strip()))
            else:
                items.append((str(b).strip(), ''))
        items = [x for x in items if x[0]]
        codes = [x[0] for x in items]
        if len(set(codes)) != len(codes):
            raise BizError('条码重复填写')
        for bc, unit in items:
            dup = db.execute('SELECT p.code, p.name FROM product_barcodes b JOIN products p ON p.id=b.product_id '
                             'WHERE b.merchant_id=? AND b.barcode=? AND b.product_id!=?', (mid, bc, pid)).fetchone()
            if dup:
                raise BizError('条码 %s 已被商品 %s %s 使用，不能重复' % (bc, dup[0], dup[1]))
            dup = db.execute('SELECT code FROM products WHERE merchant_id=? AND code=? AND id!=?', (mid, bc, pid)).fetchone()
            if dup:
                raise BizError('条码 %s 与商品编码 %s 冲突' % (bc, dup[0]))
        db.execute('DELETE FROM product_barcodes WHERE merchant_id=? AND product_id=?', (mid, pid))
        for bc, unit in items:
            uid = None
            if unit and unit != base_unit:
                u = db.execute('SELECT id FROM product_units WHERE product_id=? AND unit=?', (pid, unit)).fetchone()
                if u is None:
                    raise BizError('条码 %s 对应的单位「%s」不存在，请先在多单位里添加' % (bc, unit))
                uid = u[0]
            db.execute('INSERT INTO product_barcodes(merchant_id, barcode, product_id, unit_id) VALUES(?,?,?,?)', (mid, bc, pid, uid))


def _save_bom(db, mid, pid, combo, bom):
    db.execute('DELETE FROM product_bom WHERE merchant_id=? AND product_id=?', (mid, pid))
    if combo == 'none':
        return                      # 普通商品不保留 BOM；self 模式的 BOM 只用于拆零/组装单，销售仍扣自身库存
    seen = set()
    for it in (bom or []):
        comp = resolve_product(db, mid, it.get('component_id') if it.get('component_id') else it.get('code'))
        if comp['id'] == pid:
            raise BizError('组合装不能包含自己')
        if comp.get('combo_mode') == 'components':
            raise BizError('组件 %s 本身是按组件扣库存的组合装，不能嵌套' % comp['name'])
        if comp['id'] in seen:
            raise BizError('组件 %s 重复' % comp['name'])
        q = inum(it.get('qty'))
        if q < 1:
            raise BizError('组件数量必须是正整数（基本单位）')
        seen.add(comp['id'])
        db.execute('INSERT INTO product_bom(merchant_id, product_id, component_id, qty) VALUES(?,?,?,?)', (mid, pid, comp['id'], q))
    if not seen and combo == 'components':
        raise BizError('按组件扣库存的组合装必须设置组件（BOM）')


@bp.route('', methods=['POST'])
@require_perm('goods.edit')
def goods_save():
    db, a = get_db(), current_actor()
    d = body()
    with tx(db):
        pid = save_product(db, a, d, inum(d.get('id')) or None)
    return ok(_product_out(db, a, _get(db, a, pid), detail=True))


@bp.route('/<int:pid>/disable', methods=['POST'])
@require_perm('goods.edit')
def goods_disable(pid):
    """停用（停售），不物理删除；enable=1 重新启用。"""
    db, a = get_db(), current_actor()
    d = body()
    to = '在售' if d.get('enable') else '停售'
    with tx(db):
        p = _get(db, a, pid)
        if p['status'] != to:
            db.execute('UPDATE products SET status=?, updated_at=? WHERE id=?', (to, now_s(), pid))
            audit(db, a, 'goods', '商品启用' if to == '在售' else '商品停用', p['code'], {'status': p['status']}, {'status': to}, d.get('reason') or '')
    return ok(_product_out(db, a, _get(db, a, pid)))


@bp.route('/listing', methods=['POST'])
@require_perm('goods.listing')
def goods_listing():
    """批量铺货 / 门店停售：{store_codes:[..], product_ids:[..], status:'在售'|'停售'|'跟随总部'}（ERP-005）。"""
    db, a = get_db(), current_actor()
    d, mid = body(), a['merchant_id']
    stores = scope_stores(db, a, ','.join(d.get('store_codes') or []) or None) if d.get('store_codes') else []
    if not stores:
        raise BizError('请选择门店')
    pids = [inum(x) for x in (d.get('product_ids') or []) if inum(x)]
    if not pids:
        raise BizError('请选择商品')
    st = d.get('status') or '在售'
    if st not in ('在售', '停售', '跟随总部'):
        raise BizError('状态只能是 在售 / 停售 / 跟随总部')
    n = 0
    with tx(db):
        for pid in pids:
            p = _get(db, a, pid)
            for s in stores:
                if st == '跟随总部':
                    db.execute('DELETE FROM product_listings WHERE merchant_id=? AND store_code=? AND product_id=?', (mid, s, pid))
                else:
                    db.execute('INSERT INTO product_listings(merchant_id, store_code, product_id, status, updated_at) VALUES(?,?,?,?,?) '
                               'ON CONFLICT(merchant_id, store_code, product_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at',
                               (mid, s, pid, st, now_s()))
                n += 1
            audit(db, a, 'goods', '门店铺货', p['code'], None, {'stores': stores, 'status': st})
    return ok({'updated': n})


@bp.route('/batch-price', methods=['POST'])
@require_perm('goods.price.edit')
def goods_batch_price():
    """批量调价：items=[{id, retail?, member_price?}] 或 {ids:[..], percent:'5'}（零售价、会员价同比例，元保留两位）。"""
    from .money import mul_rate
    from decimal import Decimal, InvalidOperation
    db, a = get_db(), current_actor()
    d = body()
    items = list(d.get('items') or [])
    if d.get('ids') and d.get('percent') not in (None, ''):
        try:
            rate = Decimal(1) + Decimal(str(d['percent'])) / Decimal(100)
        except InvalidOperation:
            raise BizError('调价比例格式不正确')
        if rate <= 0:
            raise BizError('调价比例不合理')
        for pid in d['ids']:
            p = _get(db, a, inum(pid))
            items.append({'id': p['id'], 'retail': yuan(mul_rate(p['retail_cents'] or 0, rate)),
                          'member_price': yuan(mul_rate(p['member_price_cents'] or 0, rate))})
    if not items:
        raise BizError('没有要调价的商品')
    n = 0
    with tx(db):
        for it in items:
            pid = inum(it.get('id'))
            p = _get(db, a, pid)
            upd = {'retail': it.get('retail', yuan(p['retail_cents'])), 'member_price': it.get('member_price', yuan(p['member_price_cents'])),
                   'reason': d.get('reason') or '批量调价'}
            save_product(db, a, upd, pid)
            n += 1
    return ok({'updated': n})


# ---------------------------------------------------------------- 导入 / 模板 / 导出（DATA-001/002、ERP-108）
IMPORT_COLS = [
    xlsx_io.Col('code', '商品编码', aliases=('货号',)), xlsx_io.Col('name', '商品名称', required=True), xlsx_io.Col('short_name', '简称'),
    xlsx_io.Col('barcodes', '条码', note='多个用英文逗号分隔', max_len=500), xlsx_io.Col('category', '分类'), xlsx_io.Col('brand', '品牌'),
    xlsx_io.Col('spu_code', 'SPU'), xlsx_io.Col('spec', '规格'), xlsx_io.Col('base_unit', '基本单位', aliases=('单位',)),
    xlsx_io.Col('box_unit', '大单位', note='如 箱'), xlsx_io.Col('box_rate', '大单位换算率', kind='int', note='1 大单位 = ? 基本单位'),
    xlsx_io.Col('box_barcode', '大单位条码', note='箱码'),
    xlsx_io.Col('retail', '零售价', kind='money'), xlsx_io.Col('member_price', '会员价', kind='money'), xlsx_io.Col('cost', '成本价', kind='money'),
    xlsx_io.Col('batch_managed', '批次管理', kind='bool'), xlsx_io.Col('shelf_life_days', '保质期天数', kind='int'),
    xlsx_io.Col('near_expiry_days', '临期预警天数', kind='int'), xlsx_io.Col('status', '状态', kind='enum', choices=list(STATUS)),
]


@bp.route('/template')
@require_perm('goods.import')
def goods_template():
    sample = [{'code': 'P0001', 'name': '示例奶粉一段 800g', 'barcodes': '6900000000001', 'category': '奶粉', 'brand': '示例品牌', 'spec': '800g',
               'base_unit': '罐', 'box_unit': '箱', 'box_rate': 6, 'box_barcode': '16900000000001', 'retail': 398, 'member_price': 388,
               'cost': 300, 'batch_managed': '是', 'shelf_life_days': 720, 'near_expiry_days': 90, 'status': '在售'}]
    return xlsx_io.send_template(IMPORT_COLS, '商品导入模板.xlsx', sample)


@bp.route('/import', methods=['POST'])
@require_perm('goods.import')
def goods_import():
    """逐行校验，部分成功：每行一个事务；结果清单含逐行错误（重复条码、无效单位等）。"""
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    rows, errors = xlsx_io.read_rows(request.files.get('file'), IMPORT_COLS)
    created = updated = 0
    seen_bc = {}
    for r in rows:
        x = r['data']
        if (x.get('name') or '').startswith('示例'):
            continue
        bcs = [b.strip() for b in (x.get('barcodes') or '').replace('，', ',').split(',') if b.strip()]
        errs = []
        for b in bcs + ([x['box_barcode']] if x.get('box_barcode') else []):
            if b in seen_bc:
                errs.append('条码 %s 与第 %d 行重复' % (b, seen_bc[b]))
            seen_bc.setdefault(b, r['row_no'])
        if x.get('box_unit') and (x.get('box_rate') or 0) < 2:
            errs.append('无效单位：大单位「%s」的换算率必须是大于 1 的整数' % x['box_unit'])
        if x.get('box_rate') and not x.get('box_unit'):
            errs.append('填写了换算率但没有大单位名称')
        if errs:
            errors.append({'row_no': r['row_no'], 'name': x.get('name') or '', 'errors': errs})
            continue
        d = dict((k, v) for k, v in x.items() if v is not None and k not in ('barcodes', 'box_unit', 'box_rate', 'box_barcode'))
        for k in ('retail', 'member_price', 'cost'):
            if k in d:
                d[k] = yuan(d[k])
        if 'cost' in d and not has_perm(a, 'goods.cost.view'):
            d.pop('cost')
        try:
            with tx(db):
                old = db.execute('SELECT * FROM products WHERE merchant_id=? AND code=?', (mid, x['code'])).fetchone() if x.get('code') else None
                if old is None and not x.get('code') and bcs:
                    f = goods_svc.find_sku(db, mid, bcs[0])
                    old = f['product'] if f else None
                pid = old['id'] if old else None
                if pid:
                    units = [dict(unit=u['unit'], rate=u['rate'], retail=u['retail'], member_price=u['member_price'])
                             for u in units_of(db, mid, pid) if not u['is_base']]
                    curbc = [dict(barcode=b['barcode'], unit=b['unit'] or '') for b in
                             db.execute('SELECT b.barcode, u.unit FROM product_barcodes b LEFT JOIN product_units u ON u.id=b.unit_id '
                                        'WHERE b.product_id=?', (pid,))]
                else:
                    units, curbc = [], []
                if x.get('box_unit'):
                    units = [u for u in units if u['unit'] != x['box_unit']] + [{'unit': x['box_unit'], 'rate': x['box_rate']}]
                    d['units'] = units
                if bcs or x.get('box_barcode'):
                    keep = [b for b in curbc if b['barcode'] not in bcs and b['barcode'] != x.get('box_barcode')]
                    if bcs:
                        keep = [b for b in keep if b['unit']]          # 新填的基本单位条码替换原基本单位条码
                    d['barcodes'] = keep + [{'barcode': b, 'unit': ''} for b in bcs] + \
                        ([{'barcode': x['box_barcode'], 'unit': x['box_unit'] or ''}] if x.get('box_barcode') else [])
                d['reason'] = '导入'
                save_product(db, a, d, pid)
            if pid:
                updated += 1
            else:
                created += 1
        except BizError as e:
            errors.append({'row_no': r['row_no'], 'name': x.get('name') or '', 'errors': [e.msg]})
    with tx(db):
        audit(db, a, 'goods', '导入商品', '', None, {'created': created, 'updated': updated, 'errors': len(errors)})
    return ok(xlsx_io.import_result(created, updated, errors))


@bp.route('/export')
@require_perm('goods.export')
def goods_export():
    db, a = get_db(), current_actor()
    mid = a['merchant_id']
    show_cost = has_perm(a, 'goods.cost.view')
    heads = ['商品编码', '商品名称', '简称', '条码', '分类', '品牌', 'SPU', '规格', '基本单位', '大单位', '大单位换算率', '零售价', '会员价'] + \
        (['成本价'] if show_cost else []) + ['批次管理', '保质期天数', '临期预警天数', '状态']
    rows = []
    for p in db.execute('SELECT * FROM products WHERE merchant_id=? ORDER BY code', (mid,)):
        us = [u for u in units_of(db, mid, dict(p)) if not u['is_base']]
        bcs = ','.join(r[0] for r in db.execute('SELECT barcode FROM product_barcodes WHERE merchant_id=? AND product_id=? AND unit_id IS NULL',
                                                (mid, p['id'])))
        rows.append([p['code'], p['name'], p['short_name'], bcs, p['category'], p['brand'], p['spu_code'], p['spec'], p['base_unit'],
                     us[0]['unit'] if us else '', us[0]['rate'] if us else '', yuan(p['retail_cents']), yuan(p['member_price_cents'])] +
                    ([yuan(p['cost_cents'])] if show_cost else []) +
                    ['是' if p['batch_managed'] else '否', p['shelf_life_days'], p['near_expiry_days'], p['status']])
    with tx(db):
        audit(db, a, 'goods', '导出商品', '', None, {'rows': len(rows), 'cost': show_cost}, level='security')
    return xlsx_io.send_xlsx(heads, rows, '商品档案.xlsx', '商品')
