# -*- coding: utf-8 -*-
"""金额（分/Decimal）、分摊尾差、单号规则。"""
from decimal import Decimal
import random

import pytest

from shouyin_app.money import to_cents, yuan, yuan_str, dec, allocate, mul_rate, floor_to, ratio_cents, MoneyError
from shouyin_app.numbering import next_no, parse_no
from shouyin_app.db import connect, init_db


def test_to_cents_inputs():
    assert to_cents('12.30') == 1230 and to_cents(12.3) == 1230 and to_cents(Decimal('0.005')) == 1
    assert to_cents('1,234.565') == 123457 and to_cents('¥8') == 800 and to_cents('') == 0 and to_cents(None) == 0
    assert to_cents(0.1 + 0.2) == 30                       # float 走 repr，不出 0.30000000000000004 的问题
    assert to_cents('-3.335') == -334                      # ROUND_HALF_UP 远离 0
    for bad in ('abc', float('nan'), True, '1e20'):
        with pytest.raises(MoneyError):
            to_cents(bad)


def test_yuan_and_dec():
    assert yuan(1230) == 12.3 and yuan(None) is None and yuan_str(5) == '0.05' and dec(-101) == Decimal('-1.01')


def test_mul_rate_floor_ratio():
    assert mul_rate(1999, '0.88') == 1759 and mul_rate(1000, 3) == 3000
    assert floor_to(1999, 10) == 1990 and floor_to(1999, 100) == 1900 and floor_to(-1999, 100) == -1900 and floor_to(5, 1) == 5
    assert ratio_cents(1000, 1, 3) == 333 and ratio_cents(1000, 2, 3) == 667 and ratio_cents(5, 1, 0) == 0


def test_allocate_exact_and_capped():
    assert allocate(100, [100, 100, 100]) == [34, 33, 33]
    assert allocate(2, [1, 1, 1]) == [1, 1, 0]               # 每行不超过权重
    assert allocate(0, [5, 6]) == [0, 0]
    assert allocate(-100, [100, 100, 100]) == [-34, -33, -33]
    assert allocate(7, [0, 0]) == [4, 3]                      # 权重全 0 平均分
    assert allocate(1000, [1000]) == [1000]
    with pytest.raises(MoneyError):
        allocate(11, [5, 5])                                  # 优惠超过行金额合计
    with pytest.raises(MoneyError):
        allocate(1, [])
    assert allocate(11, [5, 5], cap=False) == [6, 5]


def test_allocate_property_random():
    rnd = random.Random(20261105)
    for _ in range(500):
        w = [rnd.randint(0, 50000) for _ in range(rnd.randint(1, 12))]
        if sum(w) == 0:
            continue
        t = rnd.randint(0, sum(w))
        a = allocate(t, w)
        assert sum(a) == t and all(0 <= x <= wi for x, wi in zip(a, w))
        assert a == allocate(t, list(w))                      # 确定性


def test_numbering_unique_and_format(tmp_path):
    p = str(tmp_path / 'n.db')
    init_db(p)
    db = connect(p)
    db.execute('BEGIN IMMEDIATE')
    a = next_no(db, 1, 'S', '8007')
    b = next_no(db, 1, 'S', '8007')
    c = next_no(db, 1, 'S', 'A001')
    d = next_no(db, 2, 'S', '8007')
    e = next_no(db, 1, 'PO', '')
    db.execute('COMMIT')
    assert a.startswith('S0018007') and a.endswith('00001') and b.endswith('00002') and len(a) == 19
    assert c[4:8] == 'A001' and c.endswith('00001') and d[1:4] == '002' and d.endswith('00001')
    assert e.startswith('PO0010000')
    assert parse_no(b) == {'doc_type': 'S', 'merchant_id': 1, 'scope': '8007', 'day': b[8:14], 'seq': 2}
    assert parse_no(e)['doc_type'] == 'PO' and parse_no('hello') is None
    # 回滚的号不算发出；提交过的号不会再出现
    db.execute('BEGIN IMMEDIATE')
    next_no(db, 1, 'S', '8007')
    db.execute('ROLLBACK')
    db.execute('BEGIN IMMEDIATE')
    f = next_no(db, 1, 'S', '8007')
    db.execute('COMMIT')
    assert f.endswith('00003') and f not in (a, b)
    with pytest.raises(ValueError):
        next_no(db, 1, 'ZZ', '')
    db.close()
