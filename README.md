# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`，可选 `request_id`（收款请求标识）；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。携带 `request_id` 时响应额外包含 `payment`（`request_id`、`amount_cents`、`result`、`duplicate`）：同一（租户, 订单, `request_id`）重复提交不二次累加，返回首次登记的处理结果且 `duplicate=true`；同一 `request_id` 金额不同返回 409；失败（超限、订单不存在、跨租户）不占用该标识，修正后可重用。不带 `request_id` 的调用行为不变。
- `GET /health`：返回服务与数据库状态。

## 调用示例

```bash
python3 -m app.entry --port 8000

# 受理订单
curl -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","order_id":"o1","amount_cents":500,"currency":"CNY"}'

# 登记收款（幂等）：同一 request_id 重复提交只入账一次
curl -X POST localhost:8000/orders/o1/payments -H 'X-Tenant: t1' \
  -H 'Content-Type: application/json' \
  -d '{"amount_cents":200,"request_id":"pay-req-1"}'
# => 200 {"tenant":"t1",...,"paid_cents":200,"outstanding_cents":300,
#         "payment":{"request_id":"pay-req-1","amount_cents":200,"result":"applied","duplicate":false}}
# 再次提交同一请求 => 200，paid_cents 仍为 200，payment.duplicate=true
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期、退款与对账。
