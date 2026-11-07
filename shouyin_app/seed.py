# -*- coding: utf-8 -*-
"""初始化数据：可重复运行，不会产生重复数据。

python -m shouyin_app.seed --db /opt/chainpos/data/shouyin.db --data seed_data.json \
        --admin-pass-file /opt/chainpos/ADMIN_PASSWORD.txt

- 门店、商品、条码、会员、导购：按编码 INSERT OR IGNORE（不覆盖后台已改的内容）
- 期初库存：仅在该商品+门店还没有库存记录时写入一次，并记一条“期初”流水
- admin：不存在才创建，随机强密码写入 --admin-pass-file（权限 600）；已存在则不动
- 演示店长 dianzhang / 演示收银员 shouyin01：密码 123456，绑定新妇幼店；已存在则不动
- 不创建任何订单、流水
- v2：数据全部归 1 号商户（迁移时自动建「萌贝母婴连锁」+ 默认角色）；账号写 role_id；
  期初库存按门店生成一张「期初入库单」（goods_receipts kind=opening）并经 inventory.post_move 记流水（DATA-003）。
- --platform-pass-file：额外创建平台运营管理员 platform（merchant_id=0），只在不存在时创建。
"""
import argparse
import json
import os
import secrets
import string

from werkzeug.security import generate_password_hash

from .db import connect, init_db
from .util import now_s, baby_axis
from .perms import role_id as _role_id

MERCHANT_ID = 1
LEGACY_ROLE_CODE = {'admin': 'merchant_admin', 'manager': 'store_manager', 'cashier': 'cashier', 'platform': 'platform_admin'}

DEMO_STORE = '8007'
DEMO_PASSWORD = '123456'


def gen_password(n=16):
    alphabet = string.ascii_letters + string.digits
    while True:
        p = ''.join(secrets.choice(alphabet) for _ in range(n))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def ensure_user(db, username, password, name, role, store_code='', merchant_id=None, role_code=None):
    """建账号（已存在则不动）。role=旧粗角色（admin/manager/cashier/staff/platform）；role_code=默认角色编码，不给则按 role 映射。"""
    if db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
        return False
    mid = (0 if role == 'platform' else MERCHANT_ID) if merchant_id is None else merchant_id
    rid = _role_id(db, mid, role_code or LEGACY_ROLE_CODE.get(role, ''))
    db.execute('INSERT INTO users(merchant_id,username,password_hash,name,role,role_id,store_code,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
               (mid, username, generate_password_hash(password), name, role, rid, store_code, '启用', now_s()))
    return True


def _opening_doc(db, store_code, cache):
    """每个门店一张期初入库单（同一次播种内复用）。"""
    from .numbering import next_no
    if store_code not in cache:
        no = next_no(db, MERCHANT_ID, 'GR', store_code)
        db.execute("INSERT INTO goods_receipts(gr_no, merchant_id, wh_code, kind, status, remark, created_by, created_at, posted_at) "
                   "VALUES(?,?,?,'opening','posted','期初库存（仓库库存汇总 2026-10-26）','seed',?,?)", (no, MERCHANT_ID, store_code, now_s(), now_s()))
        cache[store_code] = no
    return cache[store_code]


def seed(db_path, data_path=None, admin_pass_file=None, platform_pass_file=None):
    init_db(db_path)
    db = connect(db_path)
    ts = now_s()
    stat = {'stores': 0, 'products': 0, 'barcodes': 0, 'stock': 0, 'members': 0, 'babies': 0, 'guides': 0, 'users': 0}
    db.execute('BEGIN IMMEDIATE')
    try:
        data = {}
        if data_path:
            with open(data_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        for s in data.get('stores', []):
            cur = db.execute('INSERT OR IGNORE INTO stores(code,name,status,sort,created_at) VALUES(?,?,?,?,?)',
                             (s['code'], s['name'], '营业', s.get('sort', 0), ts))
            stat['stores'] += cur.rowcount
        for p in data.get('products', []):
            cur = db.execute('''INSERT OR IGNORE INTO products(code,name,short_name,category,brand,spec,unit,retail,member_price,cost,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (p['code'], p['name'], p.get('short_name', ''), p.get('category', ''), p.get('brand', ''),
                                                     p.get('spec', ''), p.get('unit', ''), p.get('retail', 0), p.get('member_price', 0),
                                                     p.get('cost', 0), '在售', ts, ts))
            stat['products'] += cur.rowcount
            pid = db.execute('SELECT id FROM products WHERE code=?', (p['code'],)).fetchone()[0]
            for b in p.get('barcodes', []):
                stat['barcodes'] += db.execute('INSERT OR IGNORE INTO product_barcodes(barcode,product_id) VALUES(?,?)', (b, pid)).rowcount
        from .inventory import post_move
        docs = {}
        for st in data.get('stock', []):
            r = db.execute('SELECT id FROM products WHERE merchant_id=? AND code=?', (MERCHANT_ID, st['code'])).fetchone()
            if r is None:
                continue
            if db.execute('SELECT 1 FROM stock WHERE product_id=? AND store_code=?', (r[0], st['store_code'])).fetchone():
                continue
            qty = int(round(float(st['qty'] or 0)))
            gr_no = _opening_doc(db, st['store_code'], docs)
            db.execute("INSERT INTO goods_receipt_lines(merchant_id, gr_no, product_id, unit, rate, qty, base_qty) VALUES(?,?,?,'',1,?,?)",
                       (MERCHANT_ID, gr_no, r[0], qty, qty))
            if qty:
                post_move(db, MERCHANT_ID, st['store_code'], r[0], qty, 'opening', gr_no, 'seed', note='仓库库存汇总 2026-10-26', allow_negative=True)
            else:
                db.execute('INSERT INTO stock(merchant_id,product_id,store_code,qty,updated_at) VALUES(?,?,?,0,?)', (MERCHANT_ID, r[0], st['store_code'], ts))
            stat['stock'] += 1
        for g in data.get('guides', []):
            stat['guides'] += db.execute('INSERT OR IGNORE INTO guides(code,name,store_code,status) VALUES(?,?,?,?)',
                                         (g['code'], g['name'], g.get('store_code', ''), '在职')).rowcount
        for m in data.get('members', []):
            phone = m.get('phone') or None
            if phone and db.execute('SELECT 1 FROM members WHERE phone=?', (phone,)).fetchone():
                phone = None
            cur = db.execute('''INSERT OR IGNORE INTO members(code,phone,name,level,birthday,points,balance,total_charged,reg_time,reg_store_code,
                reg_store_name,guide_code,guide_name,note,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                             (m['code'], phone, m.get('name') or '', m.get('level') or '普通会员', m.get('birthday') or '', 0, 0, 0,
                              m.get('reg_time') or ts, m.get('reg_store_code', ''), m.get('reg_store_name', ''), m.get('guide_code', ''),
                              m.get('guide_name', ''), '', '正常'))
            if cur.rowcount:
                stat['members'] += 1
                mid = db.execute('SELECT id FROM members WHERE code=?', (m['code'],)).fetchone()[0]
                for b in m.get('babies', []):
                    db.execute('INSERT INTO babies(member_id,name,gender,birthday) VALUES(?,?,?,?)',
                               (mid, b.get('name', ''), b.get('gender', ''), b.get('birthday', '')))
                    stat['babies'] += 1
        # 账号
        pw_written = False
        if db.execute("SELECT 1 FROM users WHERE username='admin'").fetchone() is None:
            pw = gen_password()
            ensure_user(db, 'admin', pw, '管理员', 'admin')
            stat['users'] += 1
            if admin_pass_file:
                os.makedirs(os.path.dirname(os.path.abspath(admin_pass_file)), exist_ok=True)
                with open(admin_pass_file, 'w') as f:
                    f.write('用户名: admin\n密码: %s\n' % pw)
                try:
                    os.chmod(admin_pass_file, 0o600)
                except Exception:
                    pass
                pw_written = True
        if platform_pass_file and db.execute("SELECT 1 FROM users WHERE username='platform'").fetchone() is None:
            ppw = gen_password()
            ensure_user(db, 'platform', ppw, '平台管理员', 'platform', '', 0, 'platform_admin')
            stat['users'] += 1
            with open(platform_pass_file, 'w') as f:
                f.write('用户名: platform\n密码: %s\n' % ppw)
            try:
                os.chmod(platform_pass_file, 0o600)
            except Exception:
                pass
        if db.execute('SELECT 1 FROM stores WHERE code=?', (DEMO_STORE,)).fetchone():
            stat['users'] += ensure_user(db, 'dianzhang', DEMO_PASSWORD, '演示店长', 'manager', DEMO_STORE)
            stat['users'] += ensure_user(db, 'shouyin01', DEMO_PASSWORD, '演示收银员', 'cashier', DEMO_STORE)
        db.execute("INSERT OR IGNORE INTO settings(merchant_id,key,value) VALUES(?, 'config', ?)", (MERCHANT_ID, json.dumps({}, ensure_ascii=False)))
        db.execute("INSERT OR IGNORE INTO warehouses(merchant_id, code, name, kind, store_code, sellable) "
                   "SELECT merchant_id, code, name, 'store', code, 1 FROM stores")
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    stat['admin_password_written'] = pw_written
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--data')
    ap.add_argument('--admin-pass-file')
    ap.add_argument('--platform-pass-file')
    a = ap.parse_args()
    print(json.dumps(seed(a.db, a.data, a.admin_pass_file, a.platform_pass_file), ensure_ascii=False))


if __name__ == '__main__':
    main()
