# 接口与模块契约（v2）

> 5 个模块 agent 之间的契约。HTTP 路径均为相对前缀（nginx 反代后为 `/shouyin/api/...`）。
> 「权限」列为 `@require_perm` 的码（多个 = 任一）；所有列表/详情/导出另外按数据范围过滤（`scope_sql`），按单号/ID 取数据时校验 merchant_id。
> 改契约（字段名、函数签名）必须先在报告里提出，总脑同步给相关模块后再改。

## 1. 通用约定

| 项 | 约定 |
|---|---|
| 成功 | `{"ok": true, "data": <对象或列表>}`（新接口）。v1 旧接口仍是平铺字段，模块重写时一并改前端 |
| 失败 | `{"ok": false, "code": "NEED_AUTH", "msg": "中文原因和下一步", "error": "同 msg（兼容旧前端）", "data": {...可选}}` + HTTP 状态码 |
| 金额 | 元，JSON 数字，两位小数（`money.yuan(cents)`）；入参接受数字或字符串，服务端 `to_cents()`；无字段权限返回 `null` |
| 数量 | 交易数量可带小数（如 0.5 箱不允许时报错）；库存数量 = 基本单位整数 |
| 折扣率 | 字符串 `"0.88"`（0 < rate ≤ 1） |
| 分页 | 入参 `page`(默认 1) `size`(默认 20，最大 200)；出参 `data = {"page","size","total","items":[...]}` |
| 时间 | `YYYY-MM-DD HH:MM:SS`；日期 `YYYY-MM-DD`；区间参数 `start` `end`（含当天） |
| 门店参数 | `store`（编码，可逗号分隔多个）；越权 → 403 `SCOPE_DENIED` |
| 幂等 | 写接口带 `client_id`（收银单据）或 `request_key`（申请类）；重复提交返回首次结果，`data.duplicate = true` |
| 导出 | `GET .../export?<同列表筛选>` 返回 xlsx；需单独导出权限码；写 audit(level='security')；超 5 万行 → 400，改用 `POST /api/report/exports` 异步任务 |
| 文件上传 | `multipart/form-data`，字段 `file`，.xlsx ≤ 20MB；导入结果 `{created, updated, skipped, error_count, errors:[{row_no, name, errors:[...]}]}` |

### 错误码

| code | HTTP | 含义 / 前端处理 |
|---|---|---|
| UNAUTHORIZED | 401 | 未登录/会话过期/被踢下线 → 跳登录 |
| FORBIDDEN | 403 | 无功能权限 |
| SCOPE_DENIED | 403 | 请求的门店不在数据范围 |
| NEED_AUTH | 403 | 超出操作额度：`data={kind,value,store_code}` → 弹「店长授权」或「提交审批」 |
| AUTH_FAILED | 403 | 现场授权失败（密码错/授权人权限、门店、额度不够） |
| APPROVAL_MISMATCH / APPROVAL_NOT_READY | 403 | 审批单与本次操作不符 / 未通过 |
| MERCHANT_DISABLED / MERCHANT_READONLY | 403 | 商户停用 / 到期只读 |
| LEGACY_SINGLE_TENANT | 403 | v1 旧接口暂只对 1 号商户开放 |
| NOT_FOUND | 404 | 不存在（含跨商户访问） |
| CONFLICT | 409 | 状态已变化/重复处理/券已被用 |
| STOCK_SHORT / BATCH_UNSELLABLE | 409 | 库存不足 / 批次过期锁定召回 |
| POINTS_SHORT / STORED_SHORT | 409 | 积分 / 储值不足 |
| TOO_MANY | 429 | 登录/授权失败过多 |
| BIZ_ERROR | 400 | 其他业务校验失败（msg 说清原因） |
| NOT_IMPLEMENTED | 501 | 占位接口 |

## 2. 公共接口（地基已实现）

| 路径 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `api/login` | POST | — | `{username, password, client?:'web'|'pos'|'m'}` → `data={username,name,merchant_id,role_code,is_platform}`；失败锁定 5 次/15 分钟；商户停用 403 |
| `api/logout` | POST | 登录 | 吊销当前会话 |
| `api/me` | GET | 登录 | `data={username,name,role,role_code,role_name,merchant_id,merchant_name,merchant_status,is_platform,data_scope,perms[],stores[],store_code,store_name}` —— 前端按 perms 显示菜单 |
| `api/password` | POST | 登录 | `{old,new}`；成功后其他终端全部下线 |
| `api/approvals` | GET | 登录 | `box=todo`(我能批的待办，需 approval.view) / `mine`(我发起的) / `all`；`status`；分页 |
| `api/approvals` | POST | 登录 + 对应基础权限 | 收银类超额审批申请 `{kind:'discount'|'price_change'|'refund'|'no_receipt_refund'|'cash_out'|'points_adjust', store_code, value, title, reason, request_key, ref_no?, payload?}` → 审批单 |
| `api/approvals/<no>` | GET | 申请人或 approval.view | 详情 + `logs` + `can_decide` |
| `api/approvals/<no>/approve` | POST | approval.*（且=单上 need_perm，门店在范围，不能批自己） | `{reason?}`；单据类审批同事务执行回调 |
| `api/approvals/<no>/reject` | POST | 同上 | `{reason}` 必填 |
| `api/approvals/<no>/cancel` | POST | 申请人 | 撤回 |
| `api/notify` | GET | 登录 | 本人消息：`status=unread|read|done`、`todo=1`；`data` 多一个 `unread` 计数 |
| `api/notify/read` | POST | 登录 | `{ids:[...]}` |

## 3. 模块间函数契约（Python，同进程调用，必须在调用方 `with tx(db):` 内）

### 3.1 库存服务 `shouyin_app/inventory.py`（①提供；③⑤②调用）

```python
to_base_qty(qty, rate) -> int                      # 交易数量×换算率，不整除抛 BizError
sellable_batches(db, mid, wh, product_id) -> [{'id','batch_no','expiry_date','status','qty',...}]   # FEFO 顺序，已剔除过期/锁定/召回
check_batch_sellable(batch_dict) -> '' | '批次 X 已过期（有效期至 …）'
available(db, mid, wh, product_id) -> int          # 可售数量
stock_out(db, actor, mid, wh, lines, ref_type, ref_no, allow_negative=False) -> [[{'batch_id','batch_no','expiry_date','qty'}], ...]
    lines = [{'product_id', 'base_qty', 'batch_id'?, 'unit', 'unit_qty', 'rate', 'cost_cents'?, 'note'?}]
stock_in(db, actor, mid, wh, lines, ref_type, ref_no) -> 同上
    lines = [{'product_id', 'base_qty', 'batch_id'? | ('batch_no','expiry_date','prod_date'), 'quality': 'ok'|'defect'|'pending', ...}]
post_move(db, mid, wh, product_id, delta, ref_type, ref_no, operator, batch_id=0, quality='ok', ...)   # 底层原语
ref_type ∈ sale refund exchange purchase purchase_return transfer_out transfer_in stocktake loss overflow adjust split assemble quality opening
```
- 收银：结算事务里 `stock_out(..., 'sale', order_no)`，把返回的批次分配存进 `order_items.batch_alloc`（POS-204 追溯）；退货 `stock_in(..., 'refund', refund_no)`，`quality` 来自退货行（POS-606）；组合装 `combo_mode=components` 时由③按 BOM 展开组件行再调用（①在 goods_svc 提供 `expand_bom(db, mid, product_id, base_qty)`，未提供前组合装按 self 处理）。
- 门店仓编码 = 门店编码（`wh = store_code`）。

### 3.2 商品服务 `shouyin_app/goods_svc.py`（①提供）

```python
find_sku(db, mid, code) -> {'product': dict, 'unit', 'rate', 'barcode'} | None   # 编码/条码/箱码精确匹配
sale_status(db, mid, store_code, product) -> '' | '不可售原因'
search(db, mid, store_code, kw, cat=None, limit=60) -> [product dict + 'stock' + 'units']   # ①实现：名称/简称/拼音首字母/货号/条码
expand_bom(db, mid, product_id, base_qty) -> [{'product_id','base_qty'}]               # ①实现
ensure_store_warehouse(db, mid, store_code, name)                                       # ①实现：④新建门店时调用
```

### 3.3 促销计价 `shouyin_app/promo_engine.py`（②提供；③调用）—— 收银台 ⇄ 促销引擎

```python
priced = price_cart(db, actor, store_code, cart)        # 纯计算，不写库；试算和结算都调它（服务端最终金额，POS-405）
apply_benefits(db, actor, order_no, store_code, priced) # 结算事务内：核销券(coupon_logs)、扣抵扣积分、发消费积分
reverse_benefits(db, actor, refund_no, orig_order, lines) -> {'points_back','points_restore','coupons_returned':[...]}
    lines = [{'orig_line_id','qty','amount_cents'}]       # 退货事务内：按原单快照；积分不足按商户配置（负积分/限制/人工）
```

**cart（输入）**
```json
{"items": [{"code": "6900000000011", "qty": "2", "unit": "罐", "price": null, "batch_id": null, "is_gift": false, "line_remark": ""}],
 "member_code": "XB00000001", "coupon_nos": ["CP0018007..."], "points_used": 200,
 "order_discount": {"rate": "0.95"} , "round_mode": "jiao"}
```
- `price` 有值 = 手工改价（元）；`order_discount` 二选一 `{"rate"}` / `{"amount"}`。

**priced（输出，金额全部为分）**
```json
{"lines": [{"line_no": 1, "product_id": 1, "code": "P1", "name": "...", "spec": "", "unit": "罐", "unit_rate": 1, "qty": 2, "base_qty": 2,
            "batch_id": null, "is_gift": false, "retail_cents": 40000, "base_price_cents": 38000, "price_cents": 38000,
            "line_amount_cents": 76000, "share_cents": 3800, "amount_cents": 72200, "cost_cents": 60000, "price_changed": false,
            "promos": [{"kind": "member_price|activity|item_discount", "source_no": "AC...", "name": "会员价", "amount_cents": 4000}]}],
 "member": {...member_full...} | null,
 "subtotal_cents": 76000, "retail_total_cents": 80000, "order_discount_cents": 3800,
 "coupons": [{"coupon_no": "...", "amount_cents": 2000}], "points_used": 200, "points_cents": 200, "round_off_cents": 0,
 "payable_cents": 72200, "points_earned": 722,
 "discounts": [{"kind": "member_price|price_change|item_discount|activity|order_discount|coupon|points|round_off|gift",
                "line_no": 1, "source_no": "", "source_name": "", "amount_cents": 3800}],
 "need_auth": [{"kind": "discount|price_change", "value": "0.95", "label": "整单折扣 0.95"}],
 "explain": ["会员价 380.00", "满 300 减 20（活动 AC…）", "券不可用：未达门槛"],
 "gifts": [{"product_id": 9, "qty": 1, "activity_no": "AC..."}],
 "snapshot": {"engine": "v2", "activities": [...], "coupons": [...], "rules": {...}, "at": "2026-11-05 15:00:00"}}
```
- 约束：`sum(lines.amount_cents) == payable_cents`；`discounts` 按 kind 汇总可还原 `retail_total − payable`；同输入同输出（POS-403）。
- `need_auth` 交给③用 `check_limit_or_approval` 逐条校验；`snapshot` 由③存进 `orders.snapshot`（POS-406，AC-016）。
- 不可用的券/活动写进 `explain` 说明原因（POS-303）。基线实现已可用（单券、v1 规则），②替换为完整引擎时输出格式不变。

### 3.4 会员记账 `shouyin_app/member_svc.py`（②提供；③④⑤调用）

```python
points_change(db, actor, mid, member_code, delta, kind, ref_no, store_code='', note='', approval_no='', allow_negative=False) -> 新积分
stored_change(db, actor, mid, member_code, delta_cents, kind, ref_no, store_code='', note='', principal_cents=None, gift_cents=None, approval_no='') -> 新余额(分)
member_brief(db, actor, mid, code_or_phone) -> 收银台用会员卡片（手机号按权限脱敏、储值按 member.stored.view）   # ②实现
quick_register(db, actor, mid, store_code, data) -> member_code                                                   # ②实现（POS-302）
```

### 3.5 审批 `shouyin_app/approval.py`（④提供；全部模块调用）

```python
create(db, actor, kind, store_code, title, value=None, ref_no='', ref_type='', payload=None, reason='', need_perm='approval.pos', request_key=None, amount_cents=0)
register_handler(kind, fn)    # 单据类：fn(db, approval_dict, approver_actor) 在审批同意的事务里执行（如过账报损单），抛 BizError 则审批失败
consume(db, actor, approval_no, kind, store_code, value)   # 收银类一次性使用（check_limit_or_approval 内部已调用）
```
单据类用法（①报损举例）：提交报损单 → `approval.create(db, actor, 'loss', wh, '报损 3 罐', ref_type='stock_adjust', ref_no=adj_no, need_perm='approval.stock')`，
单据状态置 `submitted`；模块加载时 `register_handler('loss', post_adjust)`，同意后过账、状态 `posted`。

### 3.6 消息 `shouyin_app/notify.py`（⑤提供；全部模块调用）

```python
notify_users(db, mid, user_ids, category, title, body='', ref_type='', ref_no='', link='', level='normal', is_todo=False, store_code='', dedup_key=None)
notify_perm(db, mid, perm, store_code, category, title, body='', exclude_user_id=None, **同上)
done_by_ref(db, mid, ref_type, ref_no)
```
category：stock_low near_expiry expired transfer purchase approval pay_error recon_diff risk notice system。
高风险事件（APP-304）由业务模块调用：大额改价/折扣（③）、频繁退款（③）、负库存（③）、批次锁定召回（①）。`link` 用 H5 hash 路由，如 `m#approval/AP...`、`m#transfer/TF...`。

### 3.7 系统配置（④提供）

```python
core.get_config(db, merchant_id=None) -> dict      # 收银参数（已存在，商户级）
sys_svc.get_print_template(db, mid, store_code, kind) -> {'width','copies','content':{...},'version'}   # ④在 views_sys.py 或新文件 sys_svc.py 实现
sys_svc.dict_items(db, mid, dict_type) -> [{'code','name'}]
```

## 4. HTTP 接口清单（各模块实现）

### 4.1 ③ 收银台 `api/pos/*`

| 路径 | 方法 | 权限 | 入参要点 | 出参要点 |
|---|---|---|---|---|
| `api/pos/login-pin` | POST | — | `{store_code, emp_no, pin, terminal_no}` | 同 login；失败锁定 |
| `api/pos/context` | GET | pos.login | — | 门店、可切换门店、当前班次、导购、收银参数、支付方式、打印模板、本人权限/额度 |
| `api/pos/terminals` | GET/POST | pos.login / org.edit | 收银台管理（ERP-003 收银台部分；表归③） |
| `api/pos/terminals/check` | POST | pos.device | `{terminal_no, device, model, driver, result, detail}` | 记录设备检测（POS-806） |
| `api/pos/shift/open` | POST | pos.shift | `{terminal_no, opening_cash}` | 同收银台已有 open 班次 → 409 |
| `api/pos/shift/current` | GET | pos.shift | — | 班次 + 汇总（销售/退款/各支付/现金收支/应有现金） |
| `api/pos/shift/close` | POST | pos.shift | `{counted_cash, client_id}` | 班报；长短款 |
| `api/pos/shift/force-close` | POST | pos.shift.force | `{shift_id, counted_cash?, reason}` | 审计 high |
| `api/pos/shift/<id>/reprint` | POST | pos.reprint | — | 重打标记 + print_logs |
| `api/pos/cash-moves` | POST | pos.cash_move | `{direction:'in'|'out', amount, reason, client_id, auth_user?/auth_pass?/approval_no?}` | 超 cash_out_cents → NEED_AUTH |
| `api/pos/goods/scan` | GET | pos.checkout | `code` | 商品 + 交易单位/换算率 + 可售批次(FEFO) + 不可售原因 + 临期提示 |
| `api/pos/goods/search` | GET | pos.checkout | `kw, cat` | 列表（规格、售价、可售状态） |
| `api/pos/member/lookup` | GET | pos.checkout | `kw`（手机号/会员码/卡号） | 会员卡片（脱敏）、可用券、积分、储值(按权限) |
| `api/pos/member` | POST | pos.member.register | `{phone, name?, baby_birthday?, due_date?, guide_code?}` | 会员卡片 |
| `api/pos/price` | POST | pos.checkout | cart（见 3.3） | priced（金额转元）+ need_auth |
| `api/pos/checkout` | POST | pos.checkout | `cart + {client_id, payments:[{method, amount, ref?}], guide_code?, remark?, terminal_no, auth_user?/auth_pass?/approval_no?}` | `data={order:{...快照...}, change, duplicate?}`；同事务：订单、支付流水、出库、权益、优惠明细 |
| `api/pos/held` | GET/POST | pos.held | 挂单 `{title, remark, payload}` | 列表（本店） |
| `api/pos/held/<id>` | DELETE | pos.held | — | 删除挂单（挂单不是单据，可删） |
| `api/pos/orders` | GET | pos.checkout | `start,end,type,kw,mine` | 收银员默认只看本人（data_scope=self） |
| `api/pos/orders/<no>` | GET | pos.checkout | — | 详情：行、批次、优惠明细、支付、已退情况、可退数量金额 |
| `api/pos/orders/<no>/reprint` | POST | pos.reprint | — | 重打（小票印「重打」） |
| `api/pos/refund` | POST | pos.refund | `{order_no, items:[{line_id, qty, quality:'ok'|'defect'|'pending', reason_code}], refund_method_order?, client_id, auth_user?/approval_no?}` | 退货单、退款流水、回库、权益处理 |
| `api/pos/refund/no-receipt` | POST | pos.refund.no_receipt | `{items:[{code, qty, price, quality}], refund_method, reason, client_id, ...授权}` | 商户可配置禁用 |
| `api/pos/exchange` | POST | pos.exchange | `{order_no, back_items:[...], new_items:[...], payments:[...], client_id}` | 换货单 + 退货单 + 销售单 |
| `api/pos/recharge` | POST | pos.recharge | `{member_code, amount, method, client_id}` | 储值充值（调用 member_svc） |
| `api/pos/auth` | POST | 登录 | `{kind, value, auth_user, auth_pass|auth_pin}` | 预校验授权（不消耗） |

### 4.2 ① 商品 `api/goods/*`、库存 `api/stock/*`、采购 `api/purchase/*`

| 路径 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `api/goods` | GET | goods.view | 列表：`kw,cat,brand,status,store,no_price`；成本字段按 goods.cost.view |
| `api/goods/<id>` | GET | goods.view | 详情：单位、条码、BOM、门店铺货、价格历史 |
| `api/goods` | POST | goods.edit | 新增/修改（含 units、barcodes、批次开关、保质期）；条码商户内冲突 → 400；改价字段需 goods.price.edit；写 audit（ERP-109） |
| `api/goods/<id>/disable` | POST | goods.edit | 停用（不物理删除） |
| `api/goods/listing` | POST | goods.listing | 批量铺货/门店停售 |
| `api/goods/batch-price` | POST | goods.price.edit | 批量调价（简单版；带审批的调价单在②） |
| `api/goods/import` `api/goods/template` `api/goods/export` | POST/GET/GET | goods.import / goods.import / goods.export | xlsx_io |
| `api/goods/categories` `api/goods/brands` | GET/POST | goods.view / goods.edit | |
| `api/stock/warehouses` | GET/POST | stock.view / org.edit | 仓库（总部仓/门店仓/残次仓，ERP-004）；表归① |
| `api/stock` | GET | stock.view | 余额：按仓/商品/批次；账面/可售/锁定/在途/残次/过期 |
| `api/stock/moves` | GET | stock.view | 流水：单号、批次、前后数量 |
| `api/stock/batches` | GET | stock.view | 批次查询（供应商/品牌/商品/批号/效期）（BABY-001） |
| `api/stock/batches/<id>/lock` `/unlock` | POST | stock.batch.lock / stock.batch.unlock | 原因必填；解锁走审批 batch_unlock |
| `api/stock/batches/<id>/trace` | GET | stock.view | 采购来源、库存分布、已售订单（会员手机号按权限）（BABY-003，AC-009） |
| `api/stock/recalls` … | GET/POST | stock.recall | 召回单与处理记录 |
| `api/stock/requests` | GET/POST | stock.request | 门店要货；`/<no>/submit` `/<no>/approve`(stock.transfer.approve) |
| `api/stock/transfers` | GET/POST | stock.transfer | 调拨；`/<no>/ship`(stock.ship) `/<no>/receive`(stock.receive，`{lines:[{id, in_qty, diff_reason}]}`) |
| `api/stock/stocktakes` | GET/POST | stock.take | 建盘点（scope full/category/spot/dynamic）；`/<no>/lines`(录入，APP 扫码同用) `/<no>/submit` `/<no>/approve`(stock.take.approve) |
| `api/stock/adjusts` | GET/POST | stock.adjust | 报损/报溢/调整/拆零/组装/质量转移；提交生成审批 |
| `api/stock/alerts` | GET | stock.view | 缺货/安全库存/滞销/临期(30/60/90 分级)/过期 |
| `api/stock/opening/import` | POST | stock.adjust | 期初库存导入 → 期初入库单（DATA-003） |
| `api/purchase/suppliers` | GET/POST | supplier.view / supplier.edit | |
| `api/purchase/orders` | GET/POST | purchase.view / purchase.edit | `/<no>/submit` `/<no>/approve`(purchase.approve) `/<no>/cancel` |
| `api/purchase/receipts` | POST | purchase.receive | 收货（部分/多次/拒收/差异；批次必填校验）→ 过账入库 |
| `api/purchase/returns` | GET/POST | purchase.return | 采购退货 |
| `api/purchase/prices` | GET | purchase.view + goods.cost.view | 历史采购价 |

### 4.3 ② 会员 `api/member/*`、价格促销券 `api/promo/*`

| 路径 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `api/member` | GET | member.view | 列表（手机号脱敏按 member.phone.view）：`kw,level,tag,store,status` |
| `api/member/<code>` | GET | member.view | 档案、宝宝、标签、积分/储值流水、券、购买偏好（ERP-503） |
| `api/member` | POST | member.edit | 新增/修改；宝宝增删隐藏 |
| `api/member/<code>/disable` `/enable` | POST | member.blacklist | 原因必填 |
| `api/member/merge/preview` `api/member/merge` | POST | member.merge | 预览 → 合并（日志、不可无痕） |
| `api/member/import` `template` `export` | POST/GET/GET | member.import / member.import / member.export | 导出脱敏 + 审计 |
| `api/member/tags` … | GET/POST | member.tag | |
| `api/member/<code>/points` | POST | member.points.adjust | 超 points_adjust_max → NEED_AUTH/审批 |
| `api/member/<code>/stored` | POST | member.stored.adjust | 储值调整/退款，必审批 |
| `api/member/stored/daily` | GET | member.stored.view | 储值日报（充值/赠送/消费/退款/余额） |
| `api/promo/prices` | GET/POST | promo.view / price.adjust | 区域价、门店价、等级价 |
| `api/promo/price-adjusts` | GET/POST | price.adjust | 调价单；`/<no>/submit` `/<no>/approve`(price.adjust.approve)；到点生效 |
| `api/promo/activities` | GET/POST | promo.view / promo.edit | `/<no>/submit` `/approve` `/pause` `/end` `/copy` |
| `api/promo/try` | POST | promo.view | 后台试算（与收银同一 price_cart）（ERP-404） |
| `api/promo/coupon-templates` | GET/POST | coupon.edit | |
| `api/promo/coupons/issue` | POST | coupon.issue | 发券（限领校验） |
| `api/promo/coupons` | GET | promo.view | 券实例 + 流水 |
| `api/promo/coupons/<no>/void` | POST | coupon.void | |

### 4.4 ④ 组织权限 `api/org/*`、平台 `api/platform/*`、报表 `api/report/*`、系统 `api/sys/*`

| 路径 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `api/org/merchant` | GET/POST | org.view / org.edit | 本商户资料、连锁政策（ERP-006） |
| `api/org/units` | GET/POST | org.view / org.edit | 总部/区域 |
| `api/org/stores` | GET/POST | org.view / org.edit | 门店（额度校验）；新建门店时调用①的 `goods_svc.ensure_store_warehouse(db, mid, code, name)` 建门店仓 |
| `api/org/users` | GET/POST | user.view / user.edit | 员工：角色、门店、区域范围、PIN、调店/离职（→ revoke_user_sessions） |
| `api/org/users/<id>/sessions` | GET/DELETE | user.edit | 设备会话、踢下线 |
| `api/org/roles` | GET/POST | role.edit | 自定义角色、复制、权限码勾选、额度；变更写审计 |
| `api/org/perms` | GET | role.edit | 权限码注册表（分组） |
| `api/org/audit` | GET | audit.view | 按人/单号/门店/时间/类型查 audit_logs |
| `api/report/sales` | GET | report.sales | 日周月/区域/门店：销售额、单数、退款、实收、客单价、件单价、连带率、优惠（口径说明随数据返回） |
| `api/report/goods` | GET | report.goods | 商品/分类/品牌/规格；成本毛利需 report.profit |
| `api/report/hourly` `api/report/staff` | GET | report.sales | 时段、收银员、收银台 |
| `api/report/pay` `api/report/shifts` | GET | report.pay | 支付、退款、现金收支、班次长短款 |
| `api/report/stock` | GET | report.stock | 库存金额(需成本权限)、出入库、在途、盘点差异、缺货、滞销、临期、批次 |
| `api/report/members` | GET | report.member | 新增、活跃、复购、贡献、等级分布、积分、券核销 |
| `api/report/exports` | POST/GET | report.export | 异步导出任务 + 下载（有效期） |
| `api/sys/config` | GET/POST | sys.config | 收银参数（商户级） |
| `api/sys/dicts` | GET/POST | sys.dict | 数据字典 |
| `api/sys/print-templates` | GET/POST | sys.print | 小票/班报模板（版本号） |
| `api/sys/alert-params` | GET/POST | sys.config | 预警参数、接收人 |
| `api/sys/init-check` | GET | sys.init_check | 初始化检查（商品/门店/仓库/员工/库存/支付/打印） |
| `api/sys/numbering` | GET | sys.config | 单号规则说明与示例 |
| `api/platform/merchants` | GET/POST | platform.merchant.view / platform.merchant.open | 开通：`{open_request_no, code, name, contact, phone, admin_username, max_stores, max_users, status}` 幂等；初始化管理员（一次性密码只返回一次）+ 默认角色 + 总部组织；**存在 legacy_single_tenant 蓝图时拒绝开通第 2 个商户** |
| `api/platform/merchants/<id>/status` | POST | platform.merchant.status | trial/active/expired/disabled + 原因 |
| `api/platform/merchants/<id>/quota` | POST | platform.merchant.quota | 门店/账号额度，返回已用 |
| `api/platform/merchants/<id>/reset-admin` | POST | platform.merchant.reset_admin | 二次确认（`confirm_code` = 商户编码）；生成一次性密码、must_change_pw=1、踢下线；审计 security |
| `api/platform/audit` | GET | platform.audit.view | 平台操作审计（merchant_id=0 的 audit_logs） |

### 4.5 ⑤ 手机端 `api/m/*`（只读汇总 + 调用其他模块接口）

| 路径 | 方法 | 权限 | 说明 |
|---|---|---|---|
| `api/m/dashboard` | GET | app.dashboard | `date, store, period=day|week|month`：销售额、单数、客单价、退款、较昨日/上周同期；数据更新时间与口径说明（APP-001） |
| `api/m/trend` `api/m/rank` | GET | app.dashboard | 趋势、门店/商品/分类排行（APP-002） |
| `api/m/profit` | GET | app.dashboard + report.profit | 毛利、折扣、异常改价（APP-003） |
| `api/m/overview-stock-member` | GET | app.dashboard | 缺货/临期/滞销/会员新增复购概况（APP-004） |
| `api/m/store` | GET | app.store | 本店销售/订单/退款/班次/收银员（APP-101） |
| `api/m/goods` | GET | stock.view | 条码/名称/货号查商品、价格、批次、库存（APP-102；摄像头扫码在前端） |
| `api/m/announcements` | GET/POST | 登录 / notice.publish | 公告、已读、确认（APP-105） |
| `api/m/announcements/<id>/read` `/confirm` | POST | 登录 | |
| `api/m/notify-rules` | GET/POST | sys.config | 提醒规则（APP-302） |
| `api/m/version` | GET | — | 当前 H5 版本、最低版本、是否强制刷新（APP-404） |
| 审批 / 待办 / 消息 | — | — | 直接用 `api/approvals*`、`api/notify*` |
| 要货 / 调拨收货 / 报损报溢 / 移动盘点 | — | — | 直接调用①的 `api/stock/*`（同一套权限与数据范围，APP-104/201 与后台实时一致） |
