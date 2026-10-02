# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；以及退款单的受理、状态推进（完成/撤销）与冲正，内建请求级幂等与可退余额守恒；另支持针对退款单的工单争议链路：受理、逐级推进、裁决扣减与撤销关闭；并支持针对订单的结算单对账核销链路：受理占用未收余额、核销计入已收、撤销释放与冲正加回，含条件检索；以及针对退款单的对账单对账核销链路：受理占用未核销余额、核销计入已核销、撤销释放与冲正减回，含条件检索且全程不改动订单与退款单金额；另支持退款单批量导入：逐行校验、部分成功、错误清单与断点续跑。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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

### 退款单批量导入

租户经请求头 `X-Tenant` 传入；整批以请求体 `request_id`（批次请求标识）幂等。导入是提交一批**待受理**退款单，每行给出 `order_id`、`refund_id`、`amount_cents`，以（订单标识，退款单标识）唯一确定一张退款单。

- 逐行独立判定，一行失败不影响其他行：成功行按单笔受理规则真正落库（`status=pending`、占用可退余额），失败行不留下任何退款单写入。行内规则与单笔受理完全一致：金额须为正整数（`invalid_amount`），`order_id`/`refund_id` 须为非空字符串（`invalid_line`），订单须存在且属于当前租户（`order_not_found`），（订单，退款单）不得重复受理（`duplicate_refund`），金额不得超过当前可退余额（`exceeds_refundable_balance`；该订单上待处理退款单金额之和——含本批此前已生效行——不得超过已收金额）。
- `POST /refund-imports`：提交/续跑批次。请求字段 `request_id`、`lines`（非空数组，每项含 `order_id`、`refund_id`、`amount_cents`；非法字段类型不作为整请求 422，而是该行失败原因）。始终返回 200 与批次结果（部分成功亦 200）：`request_id`、`status`（`in_progress`/`completed`）、`total`、`accepted_count`、`rejected_count`、`rows`。每个 `rows[i]` 给出 `position`（从 0 起、按提交顺序稳定）、`order_id`、`refund_id`、`amount_cents`、`outcome`（`accepted`/`rejected`），成功行附 `refund` 对象，失败行附 `error_code` 与 `reason`。
- 幂等与冲突：同一 `request_id` 重放（请求体一致）返回与首次完全一致的结果（含各行成败与原因），不重复受理任何行；同一 `request_id` 提交不同批次内容返回 409；批量导入与单笔退款受理共享 `request_id` 命名空间，同标识在两类操作间混用返回 409。
- 断点续跑：中断后用**同一 `request_id` 与同一批数据**重新 `POST` 即可续跑；已生效行不重复受理，已判失败行原因不变，最终成功行集合与错误清单和一次跑完逐行一致。中断期间也可读取检查点进度。
- `GET /refund-imports/{request_id}`：按批次请求标识查询结果（含中断态 `in_progress`），按提交顺序排列；跨租户或不存在返回 404。
- 导入生成的退款单沿用现有入口：`GET /orders/{order_id}/refunds/{refund_id}` 单读、`GET /orders/{order_id}/refunds` 按订单列出，并可正常完成/撤销/冲正。

#### 调用示例

```bash
# 提交一批：部分成功（始终 200，逐行看 outcome）
curl -X POST localhost:8000/refund-imports -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{
  "request_id": "batch-20261003-01",
  "lines": [
    {"order_id":"O-1","refund_id":"R-1","amount_cents":300},
    {"order_id":"O-1","refund_id":"R-2","amount_cents":0},
    {"order_id":"O-2","refund_id":"R-3","amount_cents":50}
  ]
}'
# 重放 / 中断后续跑：同一 request_id 与同一批数据，结果逐行一致
curl -X POST localhost:8000/refund-imports -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{
  "request_id": "batch-20261003-01",
  "lines": [
    {"order_id":"O-1","refund_id":"R-1","amount_cents":300},
    {"order_id":"O-1","refund_id":"R-2","amount_cents":0},
    {"order_id":"O-2","refund_id":"R-3","amount_cents":50}
  ]
}'
# 查询批次结果
curl localhost:8000/refund-imports/batch-20261003-01 -H 'X-Tenant: acme'
# 导入生成的退款单走既有读取/推进入口
curl localhost:8000/orders/O-1/refunds/R-1 -H 'X-Tenant: acme'
```

- `GET /health`：返回服务与数据库状态。

## 对账单（退款单对账核销）

租户均经请求头 `X-Tenant` 传入；写操作请求体携带 `request_id`（每次业务操作唯一），同一请求标识重放返回与首次完全一致的结果（含失败结果），不重复占用、扣减或释放；同一标识改作不同操作或对象返回 409。

对账单针对已存在的退款单发起，以（订单标识，退款单标识，对账单标识）唯一。未核销金额 = 退款单金额 − 全部已生效（已核销）核销金额之和；待核销对账单受理即占用同一池余额（守恒式：已核销合计 + 待核销占用合计 ≤ 退款单当前金额），核销把占用转为已核销，撤销释放占用，冲正把已核销金额减回。任何步骤不得超过当时未核销余额，超限拒绝且不留部分写入。对账核销全程不调整订单已收/未收，也不改变退款单金额与生效扣减；退款单金额被已解决工单裁决扣减或其撤销加回后，未核销余额按上式重算（裁决扣减后仍须 ≥ 对账单已核销与占用合计）。

状态机：`pending → reconciled → reversed`，`pending → cancelled`；三个终态均不可再推进，已核销不可撤销，冲正仅一次。

- `POST /orders/{order_id}/refunds/{refund_id}/reconciliations`：受理对账单。请求字段 `reconciliation_id`、`amount_cents`（>0）、`reason`（非空）、`request_id`。成功返回 201 与对账单（`status=pending`、`effective_deduction_cents=0`）。退款单不存在返回 404；同一退款单同一 `reconciliation_id` 重复受理、或金额超过当前未核销余额返回 409，且不改动既有数据。
- `POST /orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}/reconcile`：推进为已核销。请求字段 `request_id`。核销金额计入该退款单已核销金额，对账单状态原子生效（不动订单/退款单金额）；成功 200。
- `POST /orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}/cancel`：推进为已撤销（终态）。释放占用、不改已核销金额。
- `POST /orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}/reverse`：对已核销单发起一次冲正，已核销金额减回未核销余额，进入 `reversed` 终态。重复冲正、对冲正单/待核销单/已撤销单冲正返回 409。
- `GET /orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}`：按标识读取对账单；跨租户或不存在返回 404。
- `GET /orders/{order_id}/refunds/{refund_id}/reconciliations`：列出该退款单全部对账单（按受理先后）；退款单跨租户/不存在返回 404。
- `GET /reconciliations?status=&min_amount_cents=&max_amount_cents=`：按状态与核销金额范围检索当前租户对账单（参数均可选，按受理先后稳定排序）；状态非法或区间倒置返回 400。

单读、列表与检索的每个对账单对象给出：`order_id`、`refund_id`、`reconciliation_id`、`status`、`amount_cents`、`reason`、`effective_deduction_cents`（已核销为核销金额，其余为 0）。

### 调用示例

```bash
B=/orders/O-1/refunds/R-1/reconciliations
# 受理对账单（核销金额 300，占用 R-1 的未核销余额；R-1 须为已存在退款单）
curl -X POST localhost:8000$B -H 'X-Tenant: acme' -H 'Content-Type: application/json' \
  -d '{"reconciliation_id":"X-1","amount_cents":300,"reason":"退款对账核销","request_id":"x-acc-1"}'
# 核销：占用转为已核销（不动订单/退款单金额）
curl -X POST localhost:8000$B/X-1/reconcile -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{"request_id":"x-rc-1"}'
# 冲正：已核销金额减回未核销余额，进入已冲正终态
curl -X POST localhost:8000$B/X-1/reverse -H 'X-Tenant: acme' -H 'Content-Type: application/json' -d '{"request_id":"x-rv-1"}'
# 单读、列表与条件检索
curl localhost:8000$B/X-1 -H 'X-Tenant: acme'
curl localhost:8000$B -H 'X-Tenant: acme'
curl 'localhost:8000/reconciliations?status=pending&min_amount_cents=100&max_amount_cents=500' -H 'X-Tenant: acme'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；退款单批量导入为小样本同步方式（逐行独立事务提交，支持中断续跑），未做异步分片。
- 收款只支持整单登记，未实现分期；结算单支持受理、核销、撤销与冲正，收款与结算单共同受未收余额守恒约束；退款单支持受理、完成、撤销与冲正，退款单金额仅可经由已解决工单的裁决扣减与其撤销加回调整；对账单支持受理、核销、撤销与冲正，只在退款单未核销余额内占用/核销/减回，不改动订单与退款单金额。
