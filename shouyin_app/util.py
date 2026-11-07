# -*- coding: utf-8 -*-
"""通用小工具。兼容 Python 3.9。"""
import math
from datetime import datetime


class BizError(Exception):
    """业务错误：返回给前端的中文提示。

    code：机器可读错误码（docs/API-CONTRACT.md §错误码），默认按 status 推断；data：附加信息（如待审批单号）。
    """

    def __init__(self, msg, status=400, code=None, data=None):
        Exception.__init__(self, msg)
        self.msg = msg
        self.status = status
        self.code = code or {401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 409: 'CONFLICT', 429: 'TOO_MANY'}.get(status, 'BIZ_ERROR')
        self.data = data


def now_s():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def today_s():
    return datetime.now().strftime('%Y-%m-%d')


def r2(x):
    x = float(x or 0)
    return round(x + (1e-9 if x >= 0 else -1e-9), 2)


def fnum(v, default=0.0):
    try:
        if v is None or v == '':
            return default
        if isinstance(v, str):
            v = v.strip().replace('%', '').replace(',', '')
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')):
            return default
        return f
    except Exception:
        return default


def inum(v, default=0):
    try:
        return int(fnum(v, default))
    except Exception:
        return default


def page_args(args, default_size=20, max_size=500):
    page = max(1, inum(args.get('page'), 1))
    size = min(max_size, max(1, inum(args.get('pageSize'), default_size)))
    return page, size


def pages_of(total, size):
    return max(1, int(math.ceil(total / float(size))))


def baby_axis(birthday):
    """根据宝宝生日（或预产期）算年龄段，返回 (axis, month_age)。"""
    try:
        bd = datetime.strptime((birthday or '')[:10], '%Y-%m-%d')
    except ValueError:
        return '', None
    months = round((datetime.now() - bd).days / 30.44, 1)
    if months < 0:
        return '孕期', months
    if months < 6:
        return '宝宝 0-6个月', months
    if months < 12:
        return '宝宝 6-12个月', months
    if months < 24:
        return '宝宝 1-2岁', months
    if months < 36:
        return '宝宝 2-3岁', months
    return '宝宝 3岁以上', months
