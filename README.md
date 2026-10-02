# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；退款单的受理、状态推进（完成/撤销）与冲正；以及针对退款单的工单受理、逐级状态推进（解决裁决扣减/撤销关闭），内建请求级幂等与可退余额守恒。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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

### 工单

工单针对已存在的退款单发起争议处理，以（订单标识，退款单标识，工单标识）唯一。租户均经请求头 `X-Tenant` 传入；写操作请求体携带 `request_id`，同一请求标识重放返回与首次完全一致的结果（含失败结果），同一标识改作不同操作或对象返回 409。

- `POST /orders/{order_id}/refunds/{refund_id}/workorders`：受理工单。请求字段 `workorder_id`、`claim_amount_cents`（处理请求金额，正整数）、`initiator`（发起人代号）、`reason`（事由说明，非空）、`request_id`。成功返回 201 与工单（`status=accepted`）。退款单不存在返回 404；退款单已完成/已撤销/已冲正、同一 `workorder_id` 重复受理、或退款单已被进行中工单标记时返回 409，且不改动既有数据。受理即对退款单施加「处理中」标记（一张退款单最多一张进行中工单），不改变订单与退款单的可退余额约束。
- `POST /orders/{order_id}/refunds/{refund_id}/workorders/{workorder_id}/advance`：状态推进。请求字段 `request_id`、`to_status`（`processing`/`pending_review`/`resolved`/`cancelled`）；推进到 `resolved` 时必须携带 `award_cents`（裁决金额，正整数，且不超过处理请求金额与退款单金额，超限拒绝且不改数据）。解决成功按裁决金额扣减退款单金额并同步其生效扣减，订单金额不变；推进到 `cancelled` 释放处理中标记、不扣减金额。
- `POST /orders/{order_id}/refunds/{refund_id}/workorders/{workorder_id}/cancel`：撤销关闭。仅已解决工单可撤销一次：把裁决扣减加回退款单并恢复其生效扣减，工单与退款单两侧同事务原子生效，工单进入 `cancelled` 终态。请求字段 `request_id`。
- `GET /orders/{order_id}/refunds/{refund_id}/workorders/{workorder_id}`：按标识读取工单，含状态、处理请求金额、发起人代号与当前生效扣减；跨租户或不存在返回 404。
- `GET /orders/{order_id}/refunds/{refund_id}/workorders`：列出该退款单全部工单；退款单跨租户/不存在返回 404。

状态机：`accepted → processing → pending_review → resolved`，`pending_review → processing` 可回退；进行中状态（`accepted`/`processing`/`pending_review`）可推进到 `cancelled`（释放标记）；`resolved` 与 `cancelled` 为终态。工单进行中期间，被标记的退款单不得完成、撤销或冲正（一律 409）。

#### 调用示例

```bash
# 受理工单（对退款单 R-1 发起 300 的争议）
curl -X POST localhost:8000/orders/O-1/refunds/R-1/workorders -H 'X-Tenant: acme' -H 'Content-Type: application/json' \
  -d '{"workorder_id":"W-1","claim_amount_cents":300,"initiator":"agent-7","reason":"customer disputes amount","request_id":"q-wo-1"}'
# 推进：处理中 -> 待复核 -> 已解决（裁决扣减 250）
curl -X POST localhost:8000/orders/O-1/refunds/R-1/workorders/W-1/advance -H 'X-Tenant: acme' \
  -H 'Content-Type: application/json' -d '{"request_id":"q-wo-2","to_status":"processing"}'
curl -X POST localhost:8000/orders/O-1/refunds/R-1/workorders/W-1/advance -H 'X-Tenant: acme' \
  -H 'Content-Type: application/json' -d '{"request_id":"q-wo-3","to_status":"resolved","award_cents":250}'
# 撤销关闭：扣减加回退款单
curl -X POST localhost:8000/orders/O-1/refunds/R-1/workorders/W-1/cancel -H 'X-Tenant: acme' \
  -H 'Content-Type: application/json' -d '{"request_id":"q-wo-4"}'
# 列表与单读
curl localhost:8000/orders/O-1/refunds/R-1/workorders -H 'X-Tenant: acme'
curl localhost:8000/orders/O-1/refunds/R-1/workorders/W-1 -H 'X-Tenant: acme'
```

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
