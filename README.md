# Maternal Chain POS · 母婴连锁收银管理系统

> A lightweight, multi-store **retail POS** for **maternal & baby chain stores** — cashier desk + admin console + mobile assistant, built with **Flask + SQLite + PWA**. Multi-tenant, membership with **stored-value & points**, baby profiles, batch/expiry, fine-grained permissions. No Node build step.
>
> 面向**母婴连锁门店**的轻量级多门店**收银系统**：收银台 + 管理后台 + 手机店务助手，Flask + SQLite + 原生 JS PWA，一套搞定。多商户多门店、会员**储值/积分**、宝宝档案、批次效期、细粒度权限码，前端零构建。

![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![flask](https://img.shields.io/badge/Flask-3.x-black)
![tests](https://img.shields.io/badge/tests-230%20passed-brightgreen)

**Keywords / 关键词**: POS, point-of-sale, retail, chain store, multi-store, multi-tenant, membership, stored value, loyalty points, cashier, receipt printing, PWA, Flask, SQLite · 收银系统, 连锁门店, 母婴, 会员储值, 积分, 小票打印, 多门店, 多租户, 进销存, 开源收银

---

## Why this project / 为什么又造一个收银系统

GitHub 上的开源 POS 大多是**通用小超市/单店**场景（如 `money-pos` 偏 Java/Spring 重量级、`XiaoMu-PosSystem` 面向单店小超市）。母婴连锁有一批**垂直刚需**在通用收银里没有：

| 母婴连锁的真实需求 | 本项目 | 典型通用 POS |
|---|---|---|
| 多门店 + 总部统管（多租户） | ✅ 内建 `merchants` 多租户 + 门店/区域组织 | 多为单店 |
| 会员**储值余额 + 积分 + 等级价** | ✅ 储值/积分/会员价完整闭环 | 部分或无 |
| **宝宝档案**（生日→月龄营销） | ✅ 会员挂宝宝档案 | 无 |
| 奶粉**批次 / 保质期** | ✅ 批次效期字段 | 少见 |
| 细粒度**权限码**（财务/采购/只读老板/区域经理…） | ✅ 角色 + 权限码 + 数据范围 | 多为粗粒度 |
| 收银台 + 后台 + **手机店务助手** 三端 | ✅ 一套代码三端 | 多为两端 |
| 前端**零构建**（无需 Node/webpack） | ✅ 原生 JS + PWA，改完即生效 | 常需前端构建 |

> 不合适就 fork 二开 —— 本项目正是在这个思路下，把母婴连锁的垂直能力做成一套干净、可直接跑的开源底座。

## Features / 功能

- **收银台（Cashier, PWA）**：扫码/搜索开单、改价授权、会员价/储值/积分抵现、组合收款（现金 + 扫码登记）、挂单取单、退货、小票打印、开班/交班对账。
- **管理后台（Admin）**：商品与条码（一品多码）、库存与期初入库、采购收货、会员与储值积分、促销（满减/买赠/会员价）、门店与员工、角色权限码、经营报表与看板、打印模板、系统参数、操作日志。
- **手机店务助手（Mobile H5 / PWA）**：老板看板、查商品库存、会员查询、店务单据审批。
- **多租户**：一套系统服务多个商户，数据隔离，带跨商户越权测试。
- **审计**：关键操作留痕，敏感字段按权限打码。

## Tech stack / 技术栈

- Backend: **Flask 3** + **SQLite**（WAL），`gunicorn` 生产部署
- Frontend: 原生 JavaScript + **PWA**（`manifest.webmanifest`），无打包工具
- Tests: **pytest**，230 passed（含多租户越权、权限、迁移、收银交班、系统参数等）

## Quick start / 快速开始

```bash
git clone https://github.com/Tommyapi/maternal-chain-pos.git
cd maternal-chain-pos
pip install -r requirements.txt

# 生成演示数据库（虚构示例数据，一键可登录）
python -m scripts.init_demo

# 启动
flask --app wsgi run
# 打开 http://127.0.0.1:5000/
#   收银台  /static/cashier.html
#   管理后台 /static/admin.html
#   手机端  /static/m.html
```

### Demo accounts / 演示账号（密码均 `123456`）

| 账号 | 角色 |
|---|---|
| `dianzhang` | 甲店 A001 店长 |
| `shouyin01` | 甲店 A001 收银员 |
| `dianzhangB` / `shouyinB` | 乙店 B001 店长 / 收银 |
| `admin` | 商户超管（随机强密码，见 `data/ADMIN_PASSWORD.txt`） |

## Import your own data / 导入自有数据

生产初始化用 `shouyin_app.seed`，从一个 `seed_data.json` 批量导入门店、商品（含条码/零售价/会员价/成本）、期初库存、会员（含积分/储值/等级/宝宝）、导购。数据结构参考 `scripts/init_demo.py` 里的 `DATA` 示例：

```bash
python -m shouyin_app.seed --db data/shouyin.db --data seed_data.json \
       --admin-pass-file data/ADMIN_PASSWORD.txt
```

可重复运行，按编码 `INSERT OR IGNORE`，不覆盖后台已改内容、不产生重复数据。

## Tests / 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q      # 230 passed
```

## Deploy / 生产部署

`deploy/` 提供 `gunicorn` + `systemd`（`chainpos.service`）+ `nginx` 反代样例，以及备份（每小时）、健康探测（失败自动重启）、一键发布回滚脚本（`release.sh`：备份 → 迁移 → 切换 → healthz，失败自动回滚）。

## Roadmap

- [ ] 离线收银（断网可开单，恢复后同步）
- [ ] 聚合支付对接（主/被扫，自动到账与对账）
- [ ] 进销存 / 采购 / 调拨增强
- [ ] 报表与会员营销（月龄提醒、生日券）

## Support / 打赏

如果这个项目帮到你，欢迎请作者喝杯咖啡 ☕（按对应网络转对应币种）：

| 币种 / Network | 地址 / Address |
|---|---|
| USDT (TRC20) | `TNGdTwE6opH6rmUyoG1JUR7FSyEb4k273R` |
| USDT (ERC20 / BEP20 / Polygon) · ETH (Ethereum / Arbitrum / Base) | `0x59c43144a862b351fa1086ae842d1327c9d91613` |
| USDT (Solana) | `HZguEXfTtLChNdJTxAYCbcGscaqikz8JYrEpUhEyeVd9` |
| BTC | `1RS6tWXHregLfEsiPzVUCUBuhgDg5uTSV` |

## License

[MIT](./LICENSE) © 2026 Tommyapi
