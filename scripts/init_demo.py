# -*- coding: utf-8 -*-
"""一键生成演示数据库，克隆后即可登录体验。

    python -m scripts.init_demo            # 生成 ./data/shouyin.db
    SHOUYIN_DB=/path/x.db python -m scripts.init_demo

生成：商户「萌贝母婴连锁」+ 甲店 A001 / 乙店 B001 + 3 个示例商品 + 2 个会员 + 导购；
账号（密码均 123456）：dianzhang=甲店店长、shouyin01=甲店收银、dianzhangB/shouyinB=乙店；
商户超管 admin 由系统生成随机强密码，写入 data/ADMIN_PASSWORD.txt。
全部为虚构示例数据，不含任何真实门店/会员信息。可重复运行，不会产生重复数据。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402
from shouyin_app.seed import seed, ensure_user          # noqa: E402
from shouyin_app.db import connect                       # noqa: E402

DATA = {
    'stores': [{'code': 'A001', 'name': '甲店', 'sort': 0}, {'code': 'B001', 'name': '乙店', 'sort': 1}],
    'products': [
        {'code': 'P1', 'name': '奶粉一段800g', 'category': '奶粉', 'brand': '示例品牌A', 'unit': '听', 'retail': 400,
         'member_price': 380, 'cost': 300, 'barcodes': ['6900000000011', '6900000000012']},
        {'code': 'P2', 'name': '纸尿裤L码', 'category': '尿裤', 'brand': '示例品牌B', 'unit': '包', 'retail': 100,
         'member_price': 100, 'cost': 60, 'barcodes': ['6900000000021']},
        {'code': 'P3', 'name': '婴儿湿巾80抽', 'category': '用品', 'brand': '', 'unit': '包', 'retail': 20,
         'member_price': 18, 'cost': 10, 'barcodes': ['6900000000031']},
    ],
    'stock': [{'store_code': 'A001', 'code': 'P1', 'qty': 10}, {'store_code': 'A001', 'code': 'P2', 'qty': 20},
              {'store_code': 'B001', 'code': 'P1', 'qty': 5}],
    'members': [
        {'code': 'M001', 'phone': '13800000001', 'name': '张妈妈', 'level': '普通会员', 'reg_store_code': 'A001',
         'reg_store_name': '甲店', 'babies': [{'birthday': '2025-05-01'}]},
        {'code': 'M002', 'phone': '13800000002', 'name': '李妈妈', 'level': '银卡会员'},
    ],
    'guides': [{'code': 'G1', 'name': '小王', 'store_code': 'A001'}],
}


def main():
    db_path = os.environ.get('SHOUYIN_DB') or os.path.join(os.getcwd(), 'data', 'shouyin.db')
    data_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(data_dir, exist_ok=True)
    seed_json = os.path.join(data_dir, '_demo_seed.json')
    pw_file = os.path.join(data_dir, 'ADMIN_PASSWORD.txt')
    with open(seed_json, 'w', encoding='utf-8') as f:
        json.dump(DATA, f, ensure_ascii=False)

    seed(db_path, seed_json, pw_file)

    db = connect(db_path)
    db.execute('BEGIN')
    ensure_user(db, 'dianzhang', '123456', '演示店长', 'manager', 'A001')
    ensure_user(db, 'shouyin01', '123456', '演示收银员', 'cashier', 'A001')
    ensure_user(db, 'dianzhangB', '123456', '乙店店长', 'manager', 'B001')
    ensure_user(db, 'shouyinB', '123456', '乙店收银', 'cashier', 'B001')
    db.execute('COMMIT')
    db.close()
    os.remove(seed_json)

    admin_pw = open(pw_file, encoding='utf-8').read().strip() if os.path.exists(pw_file) else '(见 %s)' % pw_file
    print('演示数据库已生成：', db_path)
    print('-' * 56)
    print('管理后台超管  admin        /  %s' % admin_pw)
    print('甲店店长      dianzhang    /  123456')
    print('甲店收银员    shouyin01    /  123456')
    print('乙店店长/收银 dianzhangB、shouyinB / 123456')
    print('-' * 56)
    print('启动：  flask --app wsgi run   然后访问 http://127.0.0.1:5000/')
    print('  收银台 /static/cashier.html  后台 /static/admin.html  手机端 /static/m.html')


if __name__ == '__main__':
    main()
