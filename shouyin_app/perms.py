# -*- coding: utf-8 -*-
"""权限码注册表 + 默认角色模板 + 操作额度默认值。Owner：架构（冻结）；各模块新增权限码用 register_perm()，写在自己的文件里。

权限 = 功能权限码（本文件）+ 数据范围（roles.data_scope + user_scopes）+ 字段权限（也是权限码，如 goods.cost.view）
     + 操作额度（role_limits，见 LIMIT_KEYS）。服务端校验见 core.require_perm / scope_stores / mask_phone / check_limit_or_approval。
"""
from collections import OrderedDict

# code -> (名称, 模块)
PERMS = OrderedDict()


def register_perm(code, name, module=None, default_roles=()):
    """模块在自己的 views_*.py 顶部调用，新增权限码并（可选）加入默认角色。重复注册无副作用。"""
    PERMS[code] = (name, module or code.split('.')[0])
    for r in default_roles:
        if r in ROLE_TEMPLATES and code not in ROLE_TEMPLATES[r]['perms']:
            ROLE_TEMPLATES[r]['perms'].append(code)


_P = [
    # ---- 收银台（模块③）
    ('pos.login', '登录收银台'), ('pos.checkout', '收银结算'), ('pos.held', '挂单取单'), ('pos.shift', '开班交班'),
    ('pos.shift.force', '强制交班'), ('pos.cash_move', '现金收支'), ('pos.reprint', '补打小票'),
    ('pos.price.change', '收银改价'), ('pos.discount', '单品/整单折扣'), ('pos.refund', '原单退货'),
    ('pos.refund.no_receipt', '无单退货'), ('pos.exchange', '换货'), ('pos.member.register', '收银台注册会员'),
    ('pos.recharge', '储值充值'), ('pos.batch.choose', '手动指定批次'), ('pos.negative_stock', '负库存销售'),
    ('pos.authorize', '现场授权（店长输密码/PIN 放行）'), ('pos.device', '设备检测'),
    # ---- 商品 / 采购 / 库存（模块①）
    ('goods.view', '查看商品'), ('goods.edit', '新增修改商品'), ('goods.price.edit', '修改商品价格'),
    ('goods.cost.view', '查看成本价/毛利（字段权限）'), ('goods.import', '导入商品'), ('goods.export', '导出商品'),
    ('goods.listing', '铺货/门店停售'),
    ('supplier.view', '查看供应商'), ('supplier.edit', '维护供应商'), ('supplier.export', '导出供应商'),
    ('purchase.view', '查看采购'), ('purchase.edit', '新建采购单'), ('purchase.approve', '审核采购单'),
    ('purchase.receive', '采购收货'), ('purchase.return', '采购退货'),
    ('stock.view', '查看库存'), ('stock.request', '门店要货'), ('stock.transfer', '新建调拨'), ('stock.transfer.approve', '审核要货/调拨'),
    ('stock.ship', '调拨出库'), ('stock.receive', '调拨收货'), ('stock.take', '盘点录入'), ('stock.take.approve', '审核盘点'),
    ('stock.adjust', '报损报溢/库存调整申请'), ('stock.adjust.approve', '审核报损报溢/调整'),
    ('stock.batch.lock', '锁定批次'), ('stock.batch.unlock', '解锁批次'), ('stock.recall', '召回处理'), ('stock.export', '导出库存'),
    # ---- 会员 / 促销 / 积分券储值（模块②）
    ('member.view', '查看会员'), ('member.edit', '维护会员'), ('member.phone.view', '查看会员完整手机号（字段权限）'),
    ('member.import', '导入会员'), ('member.export', '导出会员'), ('member.merge', '合并会员'), ('member.tag', '会员标签'),
    ('member.blacklist', '会员停用/黑名单'), ('member.points.adjust', '积分调整'), ('member.stored.view', '查看储值余额（字段权限）'),
    ('member.stored.adjust', '储值调整/退款'),
    ('promo.view', '查看活动价格'), ('promo.edit', '维护活动'), ('promo.approve', '审核活动'),
    ('price.adjust', '调价单'), ('price.adjust.approve', '审核调价单'),
    ('coupon.edit', '维护券模板'), ('coupon.issue', '发券'), ('coupon.void', '作废券'),
    # ---- 组织 / 权限 / 报表 / 配置（模块④）
    ('org.view', '查看组织门店'), ('org.edit', '维护组织门店仓库收银台'), ('user.view', '查看员工'), ('user.edit', '维护员工账号'),
    ('role.edit', '维护角色权限'), ('sys.config', '系统参数'), ('sys.dict', '数据字典'), ('sys.print', '打印模板'),
    ('sys.init_check', '初始化检查'), ('audit.view', '查看操作审计'),
    ('report.sales', '销售报表'), ('report.goods', '商品分析'), ('report.profit', '毛利报表（字段权限）'), ('report.pay', '支付/班次报表'),
    ('report.stock', '库存报表'), ('report.member', '会员报表'), ('report.export', '导出报表'),
    ('approval.view', '查看审批单'),
    ('approval.pos', '审批：改价/折扣/退货/现金支出'), ('approval.stock', '审批：库存调整/报损/调拨'),
    ('approval.member', '审批：积分/储值调整'), ('approval.purchase', '审批：采购'),
    # ---- 手机端（模块⑤）
    ('app.dashboard', '经营看板'), ('app.store', '门店店务'), ('notice.publish', '发布公告'),
    # ---- 平台运营（模块④）
    ('platform.merchant.view', '查看商户（不含业务数据）'), ('platform.merchant.open', '开通商户'),
    ('platform.merchant.status', '商户状态'), ('platform.merchant.quota', '商户额度'),
    ('platform.merchant.reset_admin', '重置商户管理员'), ('platform.audit.view', '平台审计'), ('platform.ops', '运行状态/日志'),
]
for _c, _n in _P:
    PERMS[_c] = (_n, _c.split('.')[0])

ALL_MERCHANT = [c for c in PERMS if not c.startswith('platform.')]

# 操作额度键：值都存字符串
LIMIT_KEYS = OrderedDict([
    ('min_discount_rate', '最低折扣率（0~1，1=不能打折）'),
    ('price_change', '可直接改价（1/0）'),
    ('refund_daily_cents', '单日退款限额（分；-1 不限；0 每笔都要授权）'),
    ('no_receipt_refund', '可直接无单退货（1/0）'),
    ('cash_out_cents', '单笔现金支出免审额度（分；-1 不限）'),
    ('points_adjust_max', '单次积分调整免审额度（-1 不限）'),
])

_STRICT = {'min_discount_rate': '1', 'price_change': '0', 'refund_daily_cents': '0', 'no_receipt_refund': '0',
           'cash_out_cents': '0', 'points_adjust_max': '0'}

ROLE_TEMPLATES = OrderedDict([
    ('platform_admin', {'name': '平台运营管理员', 'platform': True, 'data_scope': 'platform', 'legacy': 'platform',
                        'perms': [c for c in PERMS if c.startswith('platform.')], 'limits': {}}),
    ('platform_ops', {'name': '平台运维', 'platform': True, 'data_scope': 'platform', 'legacy': 'platform',
                      'perms': ['platform.merchant.view', 'platform.ops', 'platform.audit.view'], 'limits': {}}),
    ('merchant_admin', {'name': '商户超级管理员', 'data_scope': 'merchant', 'legacy': 'admin', 'perms': list(ALL_MERCHANT),
                        'limits': {'min_discount_rate': '0', 'price_change': '1', 'refund_daily_cents': '-1', 'no_receipt_refund': '1',
                                   'cash_out_cents': '-1', 'points_adjust_max': '-1'}}),
    ('hq_goods', {'name': '总部商品/采购/仓管', 'data_scope': 'merchant', 'legacy': 'staff',
                  'perms': [c for c in ALL_MERCHANT if c.split('.')[0] in ('goods', 'supplier', 'purchase', 'stock')]
                  + ['report.goods', 'report.stock', 'approval.view', 'approval.stock', 'approval.purchase', 'org.view', 'promo.view',
                     'app.dashboard', 'app.store'],
                  'limits': dict(_STRICT)}),
    ('region_manager', {'name': '区域经理', 'data_scope': 'region', 'legacy': 'staff',
                        'perms': ['goods.view', 'stock.view', 'stock.transfer.approve', 'stock.take.approve', 'stock.adjust.approve',
                                  'member.view', 'promo.view', 'org.view', 'user.view', 'report.sales', 'report.goods', 'report.pay',
                                  'report.stock', 'report.member', 'report.export', 'approval.view', 'approval.pos', 'approval.stock',
                                  'approval.member', 'app.dashboard', 'app.store', 'pos.authorize'],
                        'limits': {'min_discount_rate': '0.5', 'price_change': '1', 'refund_daily_cents': '-1', 'no_receipt_refund': '1',
                                   'cash_out_cents': '-1', 'points_adjust_max': '1000'}}),
    ('store_manager', {'name': '店长', 'data_scope': 'store', 'legacy': 'manager',
                       'perms': [c for c in PERMS if c.startswith('pos.')]
                       + ['goods.view', 'stock.view', 'stock.request', 'stock.receive', 'stock.ship', 'stock.take', 'stock.take.approve',
                          'stock.adjust', 'stock.export', 'member.view', 'member.edit', 'member.tag', 'member.stored.view', 'promo.view',
                          'org.view', 'user.view', 'report.sales', 'report.goods', 'report.pay', 'report.stock', 'report.member',
                          'approval.view', 'approval.pos', 'app.dashboard', 'app.store'],
                       'limits': {'min_discount_rate': '0.7', 'price_change': '1', 'refund_daily_cents': '500000', 'no_receipt_refund': '1',
                                  'cash_out_cents': '100000', 'points_adjust_max': '0'}}),
    ('cashier', {'name': '收银员', 'data_scope': 'store', 'legacy': 'cashier',
                 'perms': ['pos.login', 'pos.checkout', 'pos.held', 'pos.shift', 'pos.cash_move', 'pos.reprint', 'pos.price.change',
                           'pos.discount', 'pos.refund', 'pos.exchange', 'pos.member.register', 'pos.recharge', 'pos.device',
                           'goods.view', 'stock.view', 'member.view', 'member.stored.view'],
                 'limits': {'min_discount_rate': '0.9', 'price_change': '0', 'refund_daily_cents': '0', 'no_receipt_refund': '0',
                            'cash_out_cents': '0', 'points_adjust_max': '0'}}),
    ('finance', {'name': '财务', 'data_scope': 'merchant', 'legacy': 'staff',
                 'perms': ['report.sales', 'report.goods', 'report.profit', 'report.pay', 'report.stock', 'report.member', 'report.export',
                           'goods.view', 'goods.cost.view', 'supplier.view', 'purchase.view', 'stock.view', 'member.view', 'member.stored.view',
                           'org.view', 'audit.view', 'approval.view', 'approval.member', 'app.dashboard'],
                 'limits': dict(_STRICT)}),
    ('member_ops', {'name': '会员运营', 'data_scope': 'merchant', 'legacy': 'staff',
                    'perms': ['member.view', 'member.edit', 'member.tag', 'member.merge', 'member.import', 'member.blacklist',
                              'member.points.adjust', 'promo.view', 'coupon.edit', 'coupon.issue', 'coupon.void', 'report.member',
                              'goods.view', 'org.view', 'app.dashboard'],
                    'limits': dict(_STRICT, points_adjust_max='200')}),
    ('readonly', {'name': '只读管理者', 'data_scope': 'merchant', 'legacy': 'staff',
                  'perms': ['report.sales', 'report.goods', 'report.pay', 'report.stock', 'report.member', 'goods.view', 'stock.view',
                            'member.view', 'promo.view', 'org.view', 'app.dashboard'],
                  'limits': dict(_STRICT)}),
])

LEGACY_ROLE_NAME = {'admin': '总部管理员', 'manager': '店长', 'cashier': '收银员', 'staff': '员工', 'platform': '平台运营'}


def ensure_default_roles(db, merchant_id):
    """幂等：为商户（0=平台）建系统角色；补齐模板里新增的权限码和额度键（不删除管理员自定义的）。"""
    import time
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    for code, t in ROLE_TEMPLATES.items():
        if bool(t.get('platform')) != (merchant_id == 0):
            continue
        db.execute('INSERT OR IGNORE INTO roles(merchant_id, code, name, data_scope, legacy_role, is_system, created_at, updated_at) '
                   'VALUES(?,?,?,?,?,1,?,?)', (merchant_id, code, t['name'], t['data_scope'], t['legacy'], ts, ts))
        rid = db.execute('SELECT id FROM roles WHERE merchant_id=? AND code=?', (merchant_id, code)).fetchone()[0]
        for p in t['perms']:
            db.execute('INSERT OR IGNORE INTO role_perms(role_id, perm) VALUES(?,?)', (rid, p))
        for k in LIMIT_KEYS:
            v = t['limits'].get(k, _STRICT[k]) if t['limits'] else None
            if v is not None:
                db.execute('INSERT OR IGNORE INTO role_limits(role_id, limit_key, value) VALUES(?,?,?)', (rid, k, v))


def role_id(db, merchant_id, code):
    r = db.execute('SELECT id FROM roles WHERE merchant_id=? AND code=?', (merchant_id, code)).fetchone()
    return r[0] if r else None
