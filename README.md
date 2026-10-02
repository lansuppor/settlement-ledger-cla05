# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；以及退款单的受理、状态推进（完成/撤销）与冲正，内建请求级幂等与可退余额守恒。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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

### 退款单

租户均经请求头 `X-Tenant` 传入；写操作请求体携带 `request_id`（每次业务操作唯一），同一请求标识重放返回与首次完全一致的结果。

- `POST /orders/{order_id}/refunds`：受理退款单。请求字段 `refund_id`、`amount_cents`、`request_id`。以（订单标识，退款单标识）唯一；成功返回 201 与退款单（`status=pending`、金额、当前生效扣减 0）。订单不存在返回 404；同一订单同一 `refund_id` 重复受理、或金额超过当前可退余额返回 409，且不改动既有数据。受理即占用可退余额。
- `POST /orders/{order_id}/refunds/{refund_id}/complete`：推进为已完成。请求字段 `request_id`。按退款金额扣减订单 `paid_cents` 并同步 `outstanding_cents`，退款单 `effective_deduction_cents` 变为退款金额；成功 200。
- `POST /orders/{order_id}/refunds/{refund_id}/cancel`：推进为已撤销（终态）。释放占用、不扣减订单金额。
- `POST /orders/{order_id}/refunds/{refund_id}/reverse`：对已完成单发起一次冲正，金额加回订单已收，进入 `reversed` 终态，生效扣减清零；订单与退款单在同一事务内原子生效。重复冲正、对冲正单/待处理单冲正返回 409。
- `GET /orders/{order_id}/refunds/{refund_id}`：按标识读取退款单；跨租户或不存在返回 404。
- `GET /orders/{order_id}/refunds`：列出该订单全部退款单，含标识、状态、金额与当前生效扣减；订单跨租户/不存在返回 404。

状态机：`pending → completed → reversed`，`pending → cancelled`；三个终态均不可再推进，已完成不可撤销。

#### 调用示例

```bash
# 收款后受理退款
curl -X POST localhost:8000/orders/O-1/refunds -H 'X-Tenant: acme' -H 'Content-Type: application/json' \
  -d '{"refund_id":"R-1","amount_cents":300,"request_id":"q-accept-1"}'
# 完成：扣减已收
curl -X POST localhost:8000/orders/O-1/refunds/R-1/complete -H 'X-Tenant: acme' \
  -H 'Content-Type: application/json' -d '{"request_id":"q-complete-1"}'
# 冲正：加回已收
curl -X POST localhost:8000/orders/O-1/refunds/R-1/reverse -H 'X-Tenant: acme' \
  -H 'Content-Type: application/json' -d '{"request_id":"q-reverse-1"}'
# 列表与单读
curl localhost:8000/orders/O-1/refunds -H 'X-Tenant: acme'
curl localhost:8000/orders/O-1/refunds/R-1 -H 'X-Tenant: acme'
```

- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账；退款单支持受理、完成、撤销与冲正，尚不支持退款单的部分金额修改。
