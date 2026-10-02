# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；以及退款单的受理、状态推进（完成/撤销）与冲正，内建请求级幂等与可退余额守恒；另支持针对退款单的工单争议链路：受理、逐级推进、裁决扣减与撤销关闭；并支持针对订单的结算单对账核销链路：受理占用未收余额、核销计入已收、撤销释放与冲正加回，含条件检索。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额（须扣除进行中结算单占用的余额）返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。

### 结算单

租户均经请求头 `X-Tenant` 传入；写操作请求体携带 `request_id`（每次业务操作唯一），同一请求标识重放返回与首次完全一致的结果（含失败结果），不重复占用、扣减或释放；同一标识改作不同操作或对象返回 409。

结算单针对已存在的订单发起，以（订单标识，结算单标识）唯一。未收金额 = 订单金额 − 已收金额 − 进行中（待核销）结算单核销金额之和；受理即占用未收余额，核销计入订单已收，撤销释放占用，冲正把核销金额从已收减回并加回未收。任何步骤不得超过当时未收金额，超限拒绝且不留部分写入。

状态机：`pending → settled → reversed`，`pending → cancelled`；三个终态均不可再推进，已核销不可撤销，冲正仅一次。

- `POST /orders/{order_id}/settlements`：受理结算单。请求字段 `settlement_id`、`amount_cents`（>0）、`reason`（非空）、`request_id`。成功返回 201 与结算单（`status=pending`、`effective_deduction_cents=0`）。订单不存在返回 404；同一订单同一 `settlement_id` 重复受理、或金额超过当前未收余额返回 409，且不改动既有数据。
- `POST /orders/{order_id}/settlements/{settlement_id}/settle`：推进为已核销。请求字段 `request_id`。核销金额计入订单 `paid_cents` 并同步未收金额，订单与结算单同事务原子生效；成功 200。
- `POST /orders/{order_id}/settlements/{settlement_id}/cancel`：推进为已撤销（终态）。释放占用、不动订单金额。
- `POST /orders/{order_id}/settlements/{settlement_id}/reverse`：对已核销单发起一次冲正，核销金额从订单已收减回、未收同步加回，进入 `reversed` 终态；订单与结算单两侧原子生效。重复冲正、对冲正单/待核销单冲正返回 409。
- `GET /orders/{order_id}/settlements/{settlement_id}`：按标识读取结算单；跨租户或不存在返回 404。
- `GET /orders/{order_id}/settlements`：列出该订单全部结算单（按受理先后）；订单跨租户/不存在返回 404。
- `GET /settlements?status=&min_amount_cents=&max_amount_cents=`：按状态与核销金额范围检索当前租户结算单（参数均可选，按受理先后稳定排序）；状态非法或区间倒置返回 400。

单读、列表与检索的每个结算单对象给出：`order_id`、`settlement_id`、`status`、`amount_cents`、`reason`、`effective_deduction_cents`（已核销为核销金额，其余为 0）。

#### 调用示例

```bash
B=/orders/O-1/settlements
# 受理结算单（核销金额 300，占用未收余额）
curl -X POST localhost:8000$B -H 'X-Tenant: acme' -H 'Content-Type: application/json' \
  -d '{"settlement_id":"S-1","amount_cents":300,"reason":"月度对账","request_id":"s-acc-1"}'
# 核销：计入订单已收
curl -X POST localhost:8000$B/S-1/settle -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{"request_id":"s-st-1"}'
# 冲正：从已收减回，进入已冲正终态
curl -X POST localhost:8000$B/S-1/reverse -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{"request_id":"s-rv-1"}'
# 单读、列表与条件检索
curl localhost:8000$B/S-1 -H 'X-Tenant: acme'
curl localhost:8000$B -H 'X-Tenant: acme'
curl 'localhost:8000/settlements?status=pending&min_amount_cents=100&max_amount_cents=500' -H 'X-Tenant: acme'
```

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

租户均经请求头 `X-Tenant` 传入；写操作请求体携带 `request_id`（每次业务操作唯一），同一请求标识重放返回与首次完全一致的结果（含失败结果），不重复扣减或释放。

工单针对已存在且未终结（非 `cancelled`/`reversed`）的退款单发起，以（订单标识，退款单标识，工单标识）唯一。受理即对退款单施加「处理中」标记：一张退款单最多被一张进行中工单锁定；工单进行中（`accepted`/`processing`/`review`）期间，退款单的完成、撤销、冲正一律 409。工单受理不改变订单与退款单金额。

状态机：`accepted → processing → review`（`review` 可回退 `processing`）；`processing|review → resolved`（落裁决金额，释放标记）；任意进行中状态或 `resolved` 可 `revoke` 到 `revoked`（释放标记；已解决撤销把裁决额加回退款单）。`resolved`/`revoked` 为终态。

- `POST /orders/{order_id}/refunds/{refund_id}/tickets`：受理工单。请求字段 `ticket_id`、`request_amount_cents`（>0）、`initiator`、`reason`（非空）、`request_id`。成功返回 201 与工单对象（`ticket_id`、`status=accepted`、`request_amount_cents`、`initiator`、`effective_deduction_cents=0`）。退款单不存在/跨租户返回 404；同工单标识重复受理、退款单已撤销/已冲正、已有进行中工单返回 409，且不改任何数据。
- `POST /orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/process`：已受理→处理中。请求字段 `request_id`。
- `POST /orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/review`：处理中→待复核。
- `POST /orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/reprocess`：待复核→处理中（回退）。
- `POST /orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/resolve`：处理中/待复核→已解决（终态）。请求字段 `award_cents`（>0）、`request_id`。裁决金额须 ≤ 处理请求金额且 ≤ 退款单当前金额，否则 409 且不改数据。成功后退款单金额按裁决额扣减；已完成退款单的生效扣减同步为扣减后金额（待处理单仍为 0，完成时才生效）；订单金额不变。
- `POST /orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/revoke`：撤销关闭。进行中撤销只释放标记、不动金额；已解决撤销把裁决金额加回退款单并恢复生效扣减，进入 `revoked` 终态（每张工单只能从已解决撤销一次）。
- `GET /orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}`：按标识读取工单；跨租户或不存在返回 404。
- `GET /orders/{order_id}/refunds/{refund_id}/tickets`：按退款单列出其全部工单（按受理先后），返回 `tickets` 数组；退款单跨租户/不存在返回 404。

单读与列表的每个工单对象给出：`ticket_id`、`status`、`request_amount_cents`、`initiator`、`effective_deduction_cents`（已解决为裁决金额，其余为 0）。

#### 调用示例

```bash
B=/orders/O-1/refunds/R-1/tickets
# 受理工单（R-1 须为 pending/completed 的退款单）
curl -X POST localhost:8000$B -H 'X-Tenant: acme' -H 'Content-Type: application/json' \
  -d '{"ticket_id":"T-1","request_amount_cents":300,"initiator":"alice","reason":"货不对版","request_id":"w-acc-1"}'
# 逐级推进
curl -X POST localhost:8000$B/T-1/process -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{"request_id":"w-p-1"}'
curl -X POST localhost:8000$B/T-1/review  -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{"request_id":"w-rv-1"}'
# 裁决 120：扣减退款单金额（≤ 请求金额 300、≤ 退款金额）
curl -X POST localhost:8000$B/T-1/resolve -H 'X-Tenant: acme' -H 'Content-Type: application/json' \
  -d '{"award_cents":120,"request_id":"w-rs-1"}'
# 已解决可撤销一次：金额加回退款单
curl -X POST localhost:8000$B/T-1/revoke  -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{"request_id":"w-rk-1"}'
# 单读与列表
curl localhost:8000$B/T-1 -H 'X-Tenant: acme'
curl localhost:8000$B -H 'X-Tenant: acme'
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
- 收款只支持整单登记，未实现分期；结算单支持受理、核销、撤销与冲正，收款与结算单共同受未收余额守恒约束；退款单支持受理、完成、撤销与冲正，退款单金额仅可经由已解决工单的裁决扣减与其撤销加回调整。
