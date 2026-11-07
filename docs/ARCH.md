# 收银系统 v2 架构（地基版，2026-11-05）

> 依据：`docs/SCOPE-DECISIONS.md`（最高优先级）+ 规格书 `spec_v1.txt` 第 2~10 章、附录 A。
> 本文是 5 个模块 agent 的共同约束；与本文冲突时先在报告里提出，由总脑裁决，不要各改各的。
> 配套：`docs/API-CONTRACT.md`（接口与模块间函数契约）、`docs/BUILD-PLAN.md`（5 张任务卡）。

## 1. 总体

- 技术栈不变：Flask 3 + SQLite(WAL) + 原生 JS，无构建；服务器 Python 3.9（禁 3.10+ 语法），本机 3.14 两边都要过。
- 一个进程、一个库（`data/shouyin.db`），nginx 反代 `/shouyin/`，前端全部相对路径。
- 页面：`/` 收银台（cashier.html）、`/admin` 商户后台（admin.html + admin-*.js）、`/m` 手机端 H5（m.html）、`/platform` 平台运营后台（platform.html）。
- 分层：`views_*.py`（HTTP：参数解析、权限装饰器、事务边界）→ `*_svc.py / inventory.py / promo_engine.py / approval.py / notify.py`（业务服务，可被别的模块调用）→ SQLite。
- **写别人的表只能调对方的服务函数**；读任何表都可以（报表、APP 直接 SQL 读）。

## 2. 模块划分与文件归属表（两两不相交）

| 模块 | 范围（规格书编号） | 独占文件（只有该 agent 能改） |
|---|---|---|
| ① 商品库存采购 | ERP-004/005、ERP-101~109、ERP-201~206、ERP-301~309、BABY-001~004、BABY-101~103、8.4、DATA-001~003(商品/期初库存部分) | `shouyin_app/schema/goods_stock.py` `shouyin_app/inventory.py` `shouyin_app/goods_svc.py` `shouyin_app/views_goods.py` `shouyin_app/views_stock.py` `shouyin_app/views_purchase.py` `shouyin_app/static/admin-goods.js` `shouyin_app/static/admin-stock.js` `shouyin_app/static/admin-purchase.js` `tests/test_goods*.py` `tests/test_stock*.py` `tests/test_purchase*.py` |
| ② 会员促销 | ERP-401~404、ERP-501~507、ERP-601~606、POS-301~306、POS-401~406、DATA-001(会员部分) | `shouyin_app/schema/member_promo.py` `shouyin_app/promo_engine.py` `shouyin_app/member_svc.py` `shouyin_app/views_member.py` `shouyin_app/views_promo.py` `shouyin_app/static/admin-member.js` `shouyin_app/static/admin-promo.js` `tests/test_member*.py` `tests/test_promo*.py` |
| ③ 收银台 | POS-001~005、POS-101~110、POS-201~205、POS-501~507、POS-601~606、POS-801~803/806、9.2~9.5、AC-001/004~006/008/010/014/016/017 | `shouyin_app/schema/pos.py` `shouyin_app/trade.py` `shouyin_app/views_pos.py` `shouyin_app/static/cashier.html` `shouyin_app/static/pos-*.js`(如拆分) `tests/test_trade.py` `tests/test_pos*.py` |
| ④ 多租户权限报表配置平台 | 2.2/2.3、3.2~3.4、ERP-001~003/006、ERP-701~705、ERP-801~806/808、ERP-901~905、SaaS-001~004、NFR-SEC-001~007、AC-011~013/019 | `shouyin_app/schema/tenant_auth.py` `shouyin_app/approval.py` `shouyin_app/xlsx_io.py` `shouyin_app/views_admin.py`(v1 遗留，负责拆除) `shouyin_app/views_tenant.py` `shouyin_app/views_platform.py` `shouyin_app/views_report.py` `shouyin_app/views_sys.py` `shouyin_app/static/admin.html` `shouyin_app/static/admin.js` `shouyin_app/static/admin-org.js` `shouyin_app/static/admin-report.js` `shouyin_app/static/admin-sys.js` `shouyin_app/static/platform.html` `tests/test_admin.py` `tests/test_security.py` `tests/test_tenant*.py` `tests/test_platform*.py` `tests/test_report*.py` `tests/test_sys*.py` |
| ⑤ 手机端 H5 | APP-001~005、APP-101~105、APP-201、APP-301~304、APP-401~405（H5/PWA 口径） | `shouyin_app/schema/mobile.py` `shouyin_app/notify.py` `shouyin_app/views_m.py` `shouyin_app/static/m.html` `shouyin_app/static/m-*.js`(如拆分) `shouyin_app/static/manifest.webmanifest` `shouyin_app/static/sw.js` `tests/test_mobile*.py` `tests/test_notify*.py` |
| 冻结（架构/总脑） | 地基 | `shouyin_app/__init__.py` `db.py` `core.py` `util.py` `money.py` `numbering.py` `perms.py` `seed.py` `schema/__init__.py` `schema/base.py` `schema/_legacy_v1.py` `tests/conftest.py` `tests/test_money.py` `tests/test_perms.py` `tests/test_migrate.py` `scripts/*` `deploy/*` `requirements*.txt` `docs/*.md` `qa/*` |

规则：
1. 需要改冻结文件或别人的文件：在报告里写「文件 + 建议 diff + 原因」，由总脑合并；不要直接改。
2. 新增权限码：在自己的 views 文件顶部 `register_perm('goods.label.print', '打印价签', default_roles=['store_manager'])`，不用改 perms.py。
3. 新增表/列：只在自己模块的 `schema/<模块>.py` 里加；改表结构必须把该文件的 `VERSION` +1，`migrate()` 保持幂等（用 `schema.add_column`）。
4. 新增蓝图：放在自己的 views 文件里（文件内可以有多个 Blueprint，但只能注册已列出的 `bp`；需要另一个 bp 请提给总脑加到 `__init__.py`）。
5. 测试夹具：`tests/conftest.py` 冻结；需要额外数据就在自己的测试文件里写 fixture / helper。
6. 后台前端：admin.html/admin.js 是④的壳；①②④的页面写在各自 `static/admin-<模块>.js`，用 `AdminShell.register({...})` 注册（admin.js 末尾有说明），按权限码显示菜单。

## 3. 数据模型总览（每张表一句话）

所有业务表都有 `merchant_id`（平台账号 = 0）。旧表（v1）金额列保留 REAL 元并新增 `<列>_cents`，由触发器双向同步；**新表只有 `*_cents`**。状态值：v1 旧表沿用中文（`启用/营业/在售/未使用/已完成`），v2 新单据表用英文代码 + 各模块 `STATUS_NAME` 显示。

**base（冻结）**
- `schema_version` 各模块表结构版本。
- `merchants` 商户（租户）：状态 trial/active/expired/disabled、门店/账号额度、开通请求号（幂等）。
- `audit_logs` 审计日志（只能插入，触发器禁止改删）：人/门店/终端/模块/动作/单号/前后值/原因/级别。
- `approvals` / `approval_logs` 审批单及其状态变化流水。
- `notifications` 消息/待办（每个接收人一行，dedup_key 防刷屏）。
- `number_seq` 单号流水计数（商户+类型+门店+日期）。

**tenant_auth（④）**
- `users`（v1 重建）账号：merchant_id、role_id、store_code（主门店）、emp_no+pin_hash（收银 PIN 登录）、token_version（踢下线）。
- `stores`（v1 重建）门店：商户内编码唯一、region_id、类型直营/加盟、负责人、营业时间。
- `org_units` 总部/区域层级；`user_scopes` 账号额外授权的门店/区域（数据范围）。
- `roles` / `role_perms` / `role_limits` 角色、权限码、操作额度。
- `user_sessions` 登录会话（设备、IP、吊销）。
- `dict_items` 数据字典（退货原因/报损原因/现金收支原因/单位…）。
- `print_templates` 小票/班报/价签模板（门店级，带版本号；订单打印内容按成交快照，不受模板修改影响）。
- `export_jobs` 异步导出任务。
- `settings`（v1 重建）商户级参数 JSON（收银参数等）；`op_logs`（v1）简单操作日志，逐步被 audit_logs 取代。

**goods_stock（①）**
- `products`（v1 重建，=SKU）：商户内编码唯一，base_unit、批次开关、保质期/临期天数、可折扣/可积分、组合装模式、spu_code。
- `product_barcodes`（v1 重建）：商户内条码唯一；unit_id 指向箱码等非基本单位。
- `product_units` 多单位及换算率（1 箱 = 6 罐，整数）；`product_bom` 组合装组件；`product_listings` 门店铺货/停售。
- `categories` / `brands` 分类品牌；`suppliers` / `supplier_prices` 供应商与历史采购价。
- `purchase_orders`/`purchase_lines` 采购单；`goods_receipts`/`goods_receipt_lines` 收货入库单（含期初单 kind=opening）；`purchase_returns`/`purchase_return_lines` 采购退货。
- `warehouses` 仓库（门店仓编码 = 门店编码；总部仓、残次仓）。
- `inv_batches` 批次（批号/生产日期/有效期/供应商/锁定召回状态）。
- `inv_stock` **权威库存余额**：(仓, 商品, 批次, 质量状态) → 基本单位整数。
- `stock`（v1）门店合格品账面合计（兼容视图，由 inventory.post_move 同步）；`stock_moves`（v1 扩列）库存流水：来源单类型/单号、批次、质量、前后数量、交易单位与换算率快照。
- `transfer_orders`/`transfer_lines` 要货与调拨（出库→在途→收货→差异）；`stocktakes`/`stocktake_lines` 盘点；`stock_adjusts`/`stock_adjust_lines` 报损/报溢/调整/拆零组装/质量转移；`stock_alert_rules` 预警阈值；`recall_cases`/`recall_events` 召回处理记录。

**member_promo（②）**
- `members`（v1 重建）会员：商户内编码/手机号唯一、积分、储值余额、来源、实体卡号、黑名单、合并去向。
- `babies`（v1 扩列）宝宝/孕期档案（kind、hidden、昵称）。
- `member_tags`/`member_tag_links` 标签；`member_merges` 合并日志（含合并前快照）。
- `points_log`（v1 扩列）积分流水；`stored_log`（v1 扩列）储值流水（本金/赠送分账）。
- `coupon_templates`（v1 扩列）券模板（满减/折扣/无门槛/商品券、范围、限领）；`coupons`（v1）券实例；`coupon_logs` 券状态流水。
- `product_prices` 区域价/门店价/会员等级价；`price_adjusts`/`price_adjust_lines` 调价单；`promo_activities` 促销活动（类型、优先级、互斥组、范围 JSON、规则 JSON）；`promotions`（v1 单品特价，逐步并入活动）。

**pos（③）**
- `orders`（v1 扩列）销售单/退货单/换货单（biz_type 区分，各自单号），含成交快照、无单退货标记、审批单号。
- `order_items`（v1 扩列）商品行快照：名称规格条码、交易单位/换算率/基本单位数量、批次分配、赠品、分摊、成本、退货质量状态。
- `order_discounts` 优惠明细（会员价/改价/活动/整单/券/积分/抹零/赠品分别记录，可按行）。
- `payments`（v1 扩列）支付与退款流水：pay_no、request_no(幂等)、状态、人工确认/渠道确认、渠道商户号与渠道单号（预留聚合支付）。
- `shifts`（v1 扩列）班次（强制交班、重打次数）；`cash_moves` 现金收支；`held_orders`（v1）挂单；`terminals` 收银台；`print_logs` 打印/重打记录；`device_checks` 设备检测记录。

**mobile（⑤）**
- `announcements`/`announcement_reads` 总部公告与已读/确认；`notify_rules` 提醒接收规则；`app_versions` H5 版本与强制升级。

## 4. 关键状态机

**销售单 orders(biz_type=sale)**：`status` 中文沿用 v1。首版收银全部为「收银员确认到账」支付，提交即完成：
`(挂单 held_orders，不占库存) → 已完成 → 部分退货 → 已退货`；`pay_status`：`paid`（默认）。
预留聚合支付：`unpaid → paying → paid | closed`，订单 status 在 paid 前为「待支付」，**只有 paid 后才出库、记权益**（9.4/9.6）。已完成单不改不删，售后只能生成退货/换货单。

**退货单 orders(biz_type=refund)**：提交时校验可退数量/金额 → （超额度：授权或审批）→ `已完成`（同事务里写退款流水、回库、权益处理）。原单状态随之变为部分退货/已退货。
**换货**：一张换货单号 X… 关联一张退货单 + 一张销售单（`exchange_no` 字段互相关联），差价走支付或退款流水。

**支付流水 payments**：`pending → success | failed | closed`；人工确认方式直接 success。退款流水 kind=refund、`orig_pay_no` 指向原支付。

**采购单 purchase_orders**：`draft → submitted → approved → partial → received → closed`；`draft/submitted → cancelled`。收货单 `draft → posted`（过账才入库）。采购退货 `draft → submitted → approved → posted`。

**调拨/要货 transfer_orders**：要货 `kind=request`：`draft → submitted → approved(生成 transfer) | rejected`。
调拨 `kind=transfer`：`draft → submitted → approved → shipped(调出方出库，形成在途) → partial | received(调入方收货) → closed(差异处理完)`；`draft/submitted → cancelled`。差异数量写 `diff_base_qty + diff_reason`，不直接平账（AC-007）。

**盘点 stocktakes**：`counting → submitted → approved(按差异生成库存流水) | rejected → counting`；`counting → cancelled`。
动态盘点规则：账面以 `snapshot_at` 时刻为准，盘点期间的流水累加到 `moved_qty`，差异 = 实盘 − (账面快照 + moved_qty)（AC-018）。

**报损报溢/调整 stock_adjusts**：`draft → submitted → approved → posted | rejected`；审批走 approvals(kind=loss/overflow/stock_adjust)，通过回调过账。

**优惠券 coupons**（v1 中文状态）：`未使用 → 已锁定(挂单/待支付占用) → 已核销`；`已锁定 → 未使用`(释放)；`已核销 → 未使用`(按券规则退回)；`未使用 → 已过期 | 已作废`。每次变化写 `coupon_logs`。

**审批单 approvals**：`pending → approved → consumed`（收银类，一次性使用）；`pending → rejected`（必须填原因）；`pending → cancelled`（申请人撤回）；单据类审批 approved 时同事务执行回调。

**班次 shifts**：`open → closed`；店长强制交班 `forced=1` + 原因 + 审计。同一收银台同一时间只能有一个 open（部分唯一索引）。

## 5. 金额规则

- Python 侧一律 `Decimal` 或整数分；**禁止 float 参与金额计算**。入参先 `money.to_cents()`，出参 `money.yuan()`。
- 库内：新表只有 `*_cents INTEGER`；v1 旧表 REAL 元 + `*_cents` 双列，触发器同步（写哪个都行，**新代码一律读写 `*_cents`**）。
- 接口 JSON：金额为「元」数字，两位小数（`yuan(cents)`）；无字段权限时为 `null`。
- 折扣率：字符串或 Decimal（如 `"0.88"`），金额 × 折扣用 `money.mul_rate`（ROUND_HALF_UP 到分）。
- 抹零：`money.floor_to(cents, 10|100)`，抹掉的金额作为 `round_off` 优惠记入 `order_discounts` 并分摊。
- **优惠分摊与尾差（9.3）**：`money.allocate(total_cents, weights)`，权重 = 各行分摊前的行金额（分）。
  先按比例向下取整，剩余尾差按「小数部分从大到小、同则行号靠前」每行 +1 分 → 合计严格相等、每行分摊不超过行金额、结果确定。
  分摊顺序：单品层（会员价/特价/改价/单品折扣/第二件/套餐）直接落在行 → 整单层按顺序「整单折扣/满减 → 券 → 积分 → 抹零」逐层对「上一层后的行金额」分摊。
  赠品行金额 0，不参与分摊，但保留成本和活动来源。
- 部分退货金额：`可退 = 行实付 × 退数量 / 购买数量`（`ratio_cents`），最后一次退完该行时 = 行实付 − 已退金额，保证累计不超（AC-004/005）。
- 储值、积分抵扣、券、改价、活动分别记录（`order_discounts.kind`），不得合并成一个总折扣。

## 6. 库存、多单位、批次

- **基本单位**：每个商品一个 `base_unit`，所有库存数量 = 基本单位整数。交易单位与换算率（整数）在 `product_units`；行上保存 `unit / unit_rate / unit_qty / base_qty` 快照，换算率变更不影响历史单（8.4）。不整除的数量直接报错（`inventory.to_base_qty`），禁止四舍五入吞库存。
- **唯一改库存入口** `inventory.post_move()`（及其上层 `stock_out / stock_in`）：必须带来源单据（ref_type + ref_no），写 `stock_moves`（前后数量、批次、质量、单位快照），并同步旧表 `stock`。业务代码禁止直接 `UPDATE inv_stock/stock`。
- **扣减时点（9.4）**：销售在「支付成功（首版=提交结算）」的同一事务里出库；挂单不占库存；退货在退货单完成事务里回库。
- **批次**：`batch_id=0` = 无批次。批次商品出库默认 FEFO（先到期先出，`sellable_batches`），可指定批次（需 `pos.batch.choose`）；过期 / 锁定 / 召回批次不可售，`check_batch_sellable` 给出原因（AC-008）。
- **质量状态**：`ok` 合格（可售）/ `defect` 残次 / `pending` 待处理；退货入库按质量状态进不同行（POS-606），残次不增加可售库存。
- **可售库存** = 无批次合格品 + 可售批次合计（`inventory.available`）；账面 / 锁定 / 在途 / 残次 / 过期分别可查（ERP-301）。在途 = 调拨单 `out_base_qty − in_base_qty − diff`。
- **组合装**：`combo_mode=self` 扣组合装自身库存；`components` 按 BOM 扣组件库存，二选一，不重复扣。拆零/组装用 `stock_adjusts(kind=split/assemble)` 单据。
- 负库存：规格要求默认禁止（POS-109）。v1 的 `DEFAULT_CONFIG['allow_negative_stock']` 仍是 True（核心冻结，避免打断现有数据）——模块③在新结算流程里按「商户参数允许 **且** 角色有 `pos.negative_stock`」判断，商户参数默认值改为 False 的 diff 由③在报告中提出；开启时发预警消息。

## 7. 多租户模型

- `merchants` 表；1 号商户 = 萌贝母婴连锁（迁移自动创建，现有全部数据归它）。平台账号 `users.merchant_id = 0`。
- 所有业务表带 `merchant_id`；编码/条码/手机号/配置键「商户内唯一」；单号含商户 id，全局唯一；登录名全局唯一。
- 服务端每个查询都必须带 `merchant_id = actor.merchant_id` 条件（`scope_sql()` 已包含）；按 id/单号取详情时也要校验 merchant_id，不匹配一律 404/403（AC-012）。
- 平台与商户分权：`api(platform=True)` 只给平台账号；平台账号访问任何商户接口一律 403（2.2：平台默认看不到商户经营数据）。
- 商户状态：`disabled` 禁止登录和所有接口；`expired` 只读（GET 可用、写操作 403 MERCHANT_READONLY）；`trial/active` 正常。额度 `max_stores/max_users`（0 = 不限）由④在新建门店/账号时校验。
- **v1 旧接口保护**：`views_pos / views_m / views_admin` 的蓝图标记 `legacy_single_tenant = True`，非 1 号商户访问一律 403 `LEGACY_SINGLE_TENANT`。
  模块完成「全部查询带 merchant_id + 越权测试」后删除该行。🔴 **在所有标记删除之前，平台不得开通第 2 个真实商户**（v1 查询不按商户过滤，1 号商户会看到别家的数据）——④的商户开通接口要检查此条件并拒绝。

## 8. 权限模型与 helper

权限 = **功能权限码** + **数据范围** + **字段权限** + **操作额度**，全部在服务端校验（3.3）。

- 权限码注册表 `perms.PERMS`（按模块前缀：`pos.* goods.* supplier.* purchase.* stock.* member.* promo.* price.* coupon.* org.* user.* role.* sys.* audit.* report.* approval.* app.* notice.* platform.*`）。模块新增用 `register_perm()`。
- 默认角色模板 `perms.ROLE_TEMPLATES`：平台运营管理员、平台运维、商户超级管理员、总部商品/采购/仓管、区域经理、店长、收银员、财务、会员运营、只读管理者。每个商户一套（`ensure_default_roles`），可在后台复制/自定义（④）。
- 数据范围 `roles.data_scope`：`merchant` 全商户 / `region` 授权区域下门店 + 直接授权门店 / `store` 主门店 + 直接授权门店 / `self`（收银员只看本人：列表接口自行加 `cashier_user = actor.username`）。
- 字段权限即权限码：`goods.cost.view`（成本、毛利、毛利率）、`member.phone.view`（完整手机号）、`member.stored.view`（储值余额）、`report.profit`。无权限时**接口不返回明文**（返回 null 或脱敏），也不能返回可推导成本的字段（APP-003）。
- 操作额度 `role_limits`：`min_discount_rate`、`price_change`、`refund_daily_cents`、`no_receipt_refund`、`cash_out_cents`、`points_adjust_max`。

Helper（`shouyin_app/core.py`）：

```python
@bp.route('/goods')
@require_perm('goods.view')                 # 登录 + 商户状态 + 平台隔离 + 权限码（多个码=任一）
def goods_list(): ...

@require_platform('platform.merchant.open')  # 平台接口

actor = current_actor()   # {'id','username','name','merchant_id','is_platform','role'(v1 粗角色),'role_id','role_code','role_name',
                          #  'data_scope','perms':frozenset,'limits':dict,'store_code'}
has_perm(actor, 'goods.cost.view') -> bool
scope_stores(db, actor, requested=None) -> ['A001', ...]        # requested 越权 -> BizError 403 SCOPE_DENIED
sql, args = scope_sql(db, actor, 'o.store_code', request.args.get('store'), 'o.merchant_id')
rows = db.execute('SELECT ... FROM orders o WHERE 1=1' + sql, args)
mask_phone(phone, actor) -> '138****0001' | 明文
cost_view(actor, cost_cents) -> 元 | None
r = check_limit_or_approval(db, actor, 'discount', Decimal('0.85'), data, store_code, ref_no, title)
    # -> {'mode':'self'|'auth'|'approval', 'by':..., 'approval_no'?}；不够 -> BizError 403 NEED_AUTH(data={kind,value,store_code})
    # kind: discount / price_change / refund / no_receipt_refund / cash_out / points_adjust
audit(db, actor, module, action, ref_no, before, after, reason, level='info'|'high'|'security', store_code=None, terminal_no='')
start_session(db, user_row, client, device) / revoke_user_sessions(db, user_id, reason)   # 改密码/停用/调店/改角色后必须调用
ok(data) -> {'ok':true,'data':...}；raise BizError(msg, status, code, data) -> {'ok':false,'code','msg','error','data'?}
```

现场授权：data 带 `auth_user + auth_pass`（或 `auth_pin`），授权人须同商户、有 `pos.authorize`、门店在其范围、自身额度够；写 high 级审计。
审批：前端收到 `NEED_AUTH` 后可 `POST /api/approvals` 提交审批（独立事务），审批通过后带 `approval_no` 重提，`approval.consume()` 一次性核销。

## 9. 单号规则（ERP-901 / 9.7）

`numbering.next_no(db, merchant_id, doc_type, store_code)` → `<类型><商户id 3位><门店/仓 4位><YYMMDD><当日流水 5位>`，例 `S001800726110500001`。
在写事务内取号（`number_seq` +1），全局唯一（各单号列 UNIQUE 兜底），提交后不回收；类型码见 `numbering.DOC_TYPES`（S 销售 / R 退货 / X 换货 / CZ 充值 / CS 现金收支 / PO / GR / PR / RQ / TF / ST / LS / OV / AJ / SP / RC / PA / AC / CP / AP / EX / MC / PY / RF / SH）。
v1 已有单号（`S` + 日期 + 门店 + 时间 + 随机）保留不改，新代码一律用 next_no。离线收银（P1）将来用「终端号 + 本地序号」作 client_id，不占用此序列。

## 10. 时间、幂等、事务

- 时间：服务器本地时间（Asia/Shanghai）字符串 `YYYY-MM-DD HH:MM:SS`（`util.now_s()`）；日期 `YYYY-MM-DD`。
- 写操作一律 `with tx(db):`（BEGIN IMMEDIATE），异常整单回滚；外部可见的副作用（打印）不放事务里。
- 幂等：结算/退货/充值/现金收支/审批申请带 `client_id` 或 `request_key`（唯一约束兜底），重复提交返回第一次的结果（POS-505、12.3）。
- 单据不物理删除（v1 遗留的「无销售记录商品可删除」由①改为停用）；状态变化写 `audit_logs`（3.4）。

## 11. 迁移方案（v1 生产库 → v2）

- `db.init_db()` 启动时自动执行；正式升级用 `scripts/migrate_db.py --db <库>`：先在线备份（sqlite backup API）→ 单事务迁移 → 一致性核对（integrity、merchant_id 非空、元/分一致、stock 与 inv_stock 一致、账号都有 role_id、关键表行数不变）→ JSON 报告。失败自动回滚；回滚到旧版本 = 停服务、拷回备份。
- 本机实测（2026-11-05 15:40）：用 v1 代码 + `seed/seed_data.json` 生成与生产同规模的库（14 店 / 530 商品 / 503 条码 / 529 库存 / 194 会员 / 148 宝宝 / 14 导购），迁移 0.12 秒，核对 0 问题；第二次执行无变化；迁移后 v1 收银流程照常（结算后 stock 与 inv_stock 一致）。
- 部署顺序（总脑执行）：停服务 → 备份 → `migrate_db.py` → 看报告 ok → 启动新代码 → 冒烟。

## 12. 前端约定

- 全部相对路径（`api/...`、`static/...`），中文 UI；不得引入构建工具和外部 CDN（门店网络差）。
- 后台：`AdminShell.register({key, title, icon, group, perm, render: async box => {}})`，复用 admin.js 里的 `api/modal/fld/inp/sel/v/esc/money/qs/toast/pager/bindPager`。
- 新接口返回 `{ok,data}`；旧前端代码读的是平铺字段，模块改接口时同步改自己的前端。
