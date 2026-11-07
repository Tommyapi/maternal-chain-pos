# -*- coding: utf-8 -*-
"""模块①表结构 goods_stock v2：v1 生产库能直接升级；已在 goods_stock v1 的库能补列、回填拼音；重复执行无副作用。"""
import sqlite3

from shouyin_app.db import connect, init_db
from shouyin_app.schema import columns, get_version, goods_stock

from test_migrate import make_v1


def test_v1_db_upgrades_to_goods_stock_v2(tmp_path):
    p = str(tmp_path / 'prod.db')
    make_v1(p)
    init_db(p)
    db = connect(p)
    try:
        assert get_version(db, 'goods_stock') == goods_stock.VERSION == 2
        for table, col, _ in goods_stock.ADD_COLS:
            assert col in columns(db, table), (table, col)
        assert db.execute("SELECT pinyin FROM products WHERE code='P1'").fetchone()[0] == 'NF'
        # 门店仓 + v1 库存搬进 inv_stock
        assert db.execute("SELECT qty FROM inv_stock WHERE wh_code='8007' AND product_id=1").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM warehouses WHERE kind='store'").fetchone()[0] == 2
    finally:
        db.close()


def test_goods_stock_v1_to_v2_idempotent(tmp_path):
    p = str(tmp_path / 'prod.db')
    make_v1(p)
    init_db(p)
    db = sqlite3.connect(p, isolation_level=None)
    db.execute("UPDATE schema_version SET version=1 WHERE module='goods_stock'")
    db.execute("UPDATE products SET pinyin=''")
    db.close()
    init_db(p)
    init_db(p)
    db = connect(p)
    try:
        assert get_version(db, 'goods_stock') == 2
        assert [r[0] for r in db.execute('SELECT pinyin FROM products ORDER BY id')] == ['NF', 'SJ']
        assert db.execute('SELECT COUNT(*) FROM inv_stock').fetchone()[0] == 3        # 没有重复搬库存
    finally:
        db.close()
