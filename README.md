# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额，以及退款单的受理、状态推进（完成/撤销）与冲正；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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

## 退款单接口

租户均通过请求头 `X-Tenant` 传入；写操作均须携带 `request_id` 保证幂等。退款单状态：`pending` 待处理、`completed` 已完成、`cancelled` 已撤销、`reversed` 已冲正。响应字段含 `refund_id`、`amount_cents`、`status`、`effective_deduction_cents`（当前生效扣减）。

- `POST /orders/{order_id}/refunds`：受理退款单。请求字段 `refund_id`、`amount_cents`（正整数）、`request_id`。成功返回 201 与退款单；订单不存在（含跨租户）返回 404；同标识重复受理或金额超过可退余额返回 409，无部分写入。
- `GET /orders/{order_id}/refunds/{refund_id}`：按标识读取退款单；不存在或跨租户返回 404。
- `GET /orders/{order_id}/refunds`：列出该订单全部退款单，含标识、状态、金额与当前生效扣减；订单不存在（含跨租户）返回 404。
- `POST /orders/{order_id}/refunds/{refund_id}/advance`：状态推进。请求字段 `action`（`complete` 或 `cancel`）、`request_id`。`complete` 按金额扣减订单已收；`cancel` 释放占用且不扣减。退款单不存在返回 404；非待处理状态重复/非法推进返回 409。
- `POST /orders/{order_id}/refunds/{refund_id}/reverse`：冲正已完成的退款单。请求字段 `request_id`。把已扣减金额加回订单已收并进入 `reversed` 终态，两侧原子生效。退款单不存在返回 404；非已完成状态或重复冲正返回 409。

调用示例：

```bash
# 受理（待处理，占用可退余额 200）
curl -s -X POST localhost:8000/orders/o1/refunds -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"refund_id":"rf1","amount_cents":200,"request_id":"accept-1"}'
# 推进为已完成（订单已收扣减 200）
curl -s -X POST localhost:8000/orders/o1/refunds/rf1/advance -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"action":"complete","request_id":"complete-1"}'
# 冲正（200 加回订单已收）
curl -s -X POST localhost:8000/orders/o1/refunds/rf1/reverse -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"request_id":"reverse-1"}'
# 读取 / 列出
curl -s localhost:8000/orders/o1/refunds/rf1 -H 'X-Tenant: t1'
curl -s localhost:8000/orders/o1/refunds -H 'X-Tenant: t1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账；退款单不支持部分金额冲正。
