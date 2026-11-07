# -*- coding: utf-8 -*-
"""金额：Python 侧 Decimal，库里整数「分」，接口 JSON 用「元」（两位小数的数字）。Owner：架构（冻结）。

    to_cents('12.30') -> 1230        # 入参（JSON/表单/Excel）一律先过这里；四舍五入到分（ROUND_HALF_UP）
    to_cents(12.3)    -> 1230        # float 先转 str 再转 Decimal，不做二进制浮点运算
    yuan(1230)        -> 12.3        # 出参：JSON 数字（float 只用于序列化，不再参与计算）
    yuan_str(1230)    -> '12.30'     # 小票/导出
    dec(1230)         -> Decimal('12.30')
    allocate(100, [3, 3, 3]) -> [34, 33, 33]   # 分摊，合计严格等于 total，每行不超过权重（见 allocate 文档）
    mul_rate(1999, '0.88') -> 1759   # 金额 × 折扣率，四舍五入到分
"""
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, InvalidOperation

CENT = Decimal('0.01')
MAX_CENTS = 10 ** 13           # 1000 亿元：防溢出/防乱填


class MoneyError(ValueError):
    pass


def dec_of(v):
    """任意输入 -> Decimal（元）。None/'' -> 0。非法 -> MoneyError。"""
    if v is None or v == '':
        return Decimal(0)
    if isinstance(v, bool):
        raise MoneyError('金额格式不正确')
    if isinstance(v, Decimal):
        d = v
    elif isinstance(v, int):
        d = Decimal(v)
    elif isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            raise MoneyError('金额格式不正确')
        d = Decimal(repr(v))
    else:
        s = str(v).strip().replace(',', '').replace('¥', '').replace('￥', '')
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise MoneyError('金额格式不正确：%s' % v)
    if not d.is_finite():
        raise MoneyError('金额格式不正确')
    return d


def to_cents(v):
    """元 -> 分（int）。"""
    c = int(dec_of(v).quantize(CENT, rounding=ROUND_HALF_UP) * 100)
    if abs(c) >= MAX_CENTS:
        raise MoneyError('金额超出范围')
    return c


def dec(cents):
    """分 -> Decimal 元（两位）。"""
    return (Decimal(int(cents or 0)) / 100).quantize(CENT)


def yuan(cents):
    """分 -> JSON 用的元（数字，两位小数）。None 原样返回（无权限字段用 None）。"""
    if cents is None:
        return None
    return float(dec(cents))


def yuan_str(cents):
    return '%s' % dec(cents)


def mul_rate(cents, rate, rounding=ROUND_HALF_UP):
    """金额（分）× 比率（如折扣 0.88 / 数量 3 / 比例 Decimal），结果四舍五入到分。"""
    r = dec_of(rate)
    return int((Decimal(int(cents)) * r).quantize(Decimal(1), rounding=rounding))


def floor_to(cents, step):
    """抹零：step=10 抹分，step=100 抹角（向下取整到 step 的整数倍）。负数同样向 0 方向。"""
    cents = int(cents)
    if step <= 1:
        return cents
    sign = -1 if cents < 0 else 1
    return sign * (abs(cents) // step * step)


def allocate(total_cents, weights, cap=True):
    """把 total_cents 按权重分摊到各行，返回与 weights 等长的 int 列表，合计 == total_cents。

    规则（规格书 9.3 尾差统一规则）：
    1. 每行先分 floor(total * w / sum(w))；
    2. 剩下的尾差（< 行数 分）按「小数部分从大到小，相同则行号靠前」每行 +1 分；
    3. cap=True 时要求 |total| <= sum(weights)，保证每行分摊额不超过该行权重（优惠不会超过行金额）；
    4. 权重全为 0：平均分，尾差同上。负数 total 按绝对值分再取负。权重不得为负。
    结果完全确定：同样输入永远同样输出。
    """
    weights = [int(w) for w in weights]
    n = len(weights)
    total = int(total_cents)
    if n == 0:
        if total:
            raise MoneyError('没有可分摊的行')
        return []
    if any(w < 0 for w in weights):
        raise MoneyError('分摊权重不能为负')
    sign = -1 if total < 0 else 1
    t = abs(total)
    wsum = sum(weights)
    if wsum == 0:
        weights = [1] * n
        wsum = n
    elif cap and t > wsum:
        raise MoneyError('分摊金额 %d 超过可分摊合计 %d' % (t, wsum))
    base = [t * w // wsum for w in weights]
    rem = t - sum(base)
    order = sorted(range(n), key=lambda i: (-((t * weights[i]) % wsum), i))
    for i in order[:rem]:
        base[i] += 1
    return [sign * x for x in base]


def ratio_cents(cents, num, den, rounding=ROUND_HALF_UP):
    """cents * num / den 四舍五入（部分退货按数量比例算金额时用；累计退款上限另行校验）。"""
    if not den:
        return 0
    return int((Decimal(int(cents)) * Decimal(num) / Decimal(den)).quantize(Decimal(1), rounding=rounding))


__all__ = ['to_cents', 'dec', 'dec_of', 'yuan', 'yuan_str', 'mul_rate', 'floor_to', 'allocate', 'ratio_cents', 'MoneyError',
           'ROUND_FLOOR', 'ROUND_HALF_UP']
