# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额，以及退款单的登记、读取、审核（同意/拒绝）与冲正（撤销）；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `GET /health`：返回服务与数据库状态。

### 退款单

- `POST /refunds`：登记退款单。请求字段 `tenant`、`refund_id`、`order_id`、`amount_cents`、`reason`；`amount_cents` 为大于 0 的最小货币单位整数。成功返回 201 与状态为 `pending` 的退款单；订单不存在或非本租户、金额非法、原因缺失返回 400；待审核与已生效退款合计加本笔超过订单已收金额返回 409。同一租户下相同 `refund_id` 重复登记不新建单据，返回 200 与既有单据及当前状态。
- `GET /refunds/{refund_id}`：按退款标识读取退款单。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404。
- `POST /refunds/{refund_id}/review`：审核退款单。请求头 `X-Tenant` 必传；请求字段 `decision`，取值 `approve` 或 `reject`。仅 `approve` 对订单生效；若生效会使退款总额超过订单已收金额返回 409。审核结果不可覆盖，重复审核返回原结果（200）；退款单已冲正后再审核返回 409；不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/reverse`：冲正（撤销）已生效退款单。请求头 `X-Tenant` 必传；将该笔已生效退款全额反向退回订单并把单据置为 `reversed`。仅对已 `approved` 的单据有效，待审核/已拒绝/已冲正均返回 409；不存在或跨租户返回 404。冲正后不得再次审核。

退款单状态机：`pending`（待审核）→ `approved`（已同意并生效）/ `rejected`（已拒绝）；`approved` → `reversed`（已冲正，终态）。订单对外 `paid_cents` 为净已收（累计收款扣减已批准未冲正退款），`outstanding_cents = amount_cents − paid_cents` 恒成立。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，写事务以 `BEGIN IMMEDIATE` 串行执行，未做连接池与多实例扩展。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 退款支持逐笔登记、审核与冲正，未实现部分退款的分期审批流与对账报表。

## 调用示例

```bash
# 登记退款单（退款单以租户 + refund_id 唯一）
curl -s -X POST http://127.0.0.1:8000/refunds \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","refund_id":"rf-1","order_id":"demo-1","amount_cents":200,"reason":"商品破损"}'

# 读取退款单（读取、审核、冲正均需 X-Tenant 头）
curl -s http://127.0.0.1:8000/refunds/rf-1 -H 'X-Tenant: t1'

# 审核：同意（对订单生效）或拒绝
curl -s -X POST http://127.0.0.1:8000/refunds/rf-1/review \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' -d '{"decision":"approve"}'

# 冲正：把已生效退款全额退回订单
curl -s -X POST http://127.0.0.1:8000/refunds/rf-1/reverse -H 'X-Tenant: t1'
```
