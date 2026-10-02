# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额、收款回退（登记后发现收错或重复收款时反向退回），以及退款单的登记、读取、审核（同意/拒绝）与冲正（撤销）；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /payment-rollbacks`：收款回退。请求字段 `tenant`、`rollback_id`、`order_id`、`amount_cents`；`amount_cents` 为大于 0 的最小货币单位整数，回退以（租户, `rollback_id`）为业务身份。成功返回 201 与状态为 `completed` 的回退单；同一（租户, `rollback_id`）重复请求不新建记录、不重复退回金额，返回 200 与既有回退结果（业务身份与请求携带的金额/订单指纹无关）。回退后订单净已收按净额重算，未收 = 订单金额 − 已收。订单不存在或非本租户、金额非法、回退金额超过订单当前已收金额返回 400；回退会使退款占用额度（待审核 + 已生效未冲正）超过回退后已收金额、即超出剩余可回退额度，返回 409 且不改变任何数据。
- `GET /payment-rollbacks/{rollback_id}`：按回退标识读取收款回退单。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404（不泄漏对象是否存在）。
- `POST /orders/batch-accept`：批量受理订单。批次身份为（租户, `batch_id`）。支持两种提交形态：
  - `application/json`：请求字段 `tenant`、`batch_id`、`csv`（CSV 文本）。
  - 直接上传 CSV 文件（如 `Content-Type: text/csv`）：请求体即 CSV 字节，`tenant` 与 `batch_id` 通过查询参数传入（`?tenant=t1&batch_id=...`）。
  - CSV 表头必须恰为 `tenant,order_id,amount_cents,currency`，字段口径与单笔受理一致。首次处理返回 201 与批次结果；同一（租户, `batch_id`）重放返回 200 与既有结果，不重复受理任何订单。CSV 格式非法（缺表头、列数不符、编码非法、行结构损坏）整批拒绝返回 400 且不受理任何行；行业务校验失败只跳过该行并计入错误清单。进行中批次只接受同一份输入续跑，输入不一致返回 409。
- `GET /batches/{batch_id}`：按批次标识查询批次结果。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404（不泄漏批次是否存在）。
- `GET /health`：返回服务与数据库状态。

### 退款单

- `POST /refunds`：登记退款单。请求字段 `tenant`、`refund_id`、`order_id`、`amount_cents`、`reason`；`amount_cents` 为大于 0 的最小货币单位整数。成功返回 201 与状态为 `pending` 的退款单；订单不存在或非本租户、金额非法、原因缺失返回 400；待审核与已生效退款合计加本笔超过订单已收金额返回 409。同一租户下相同 `refund_id` 重复登记不新建单据，返回 200 与既有单据及当前状态。
- `GET /refunds/{refund_id}`：按退款标识读取退款单。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404。
- `POST /refunds/{refund_id}/review`：审核退款单。请求头 `X-Tenant` 必传；请求字段 `decision`，取值 `approve` 或 `reject`。仅 `approve` 对订单生效；若生效会使退款总额超过订单已收金额返回 409。审核结果不可覆盖，重复审核返回原结果（200）；退款单已冲正后再审核返回 409；不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/reverse`：冲正（撤销）已生效退款单。请求头 `X-Tenant` 必传；将该笔已生效退款全额反向退回订单并把单据置为 `reversed`。仅对已 `approved` 的单据有效，待审核/已拒绝/已冲正均返回 409；不存在或跨租户返回 404。冲正后不得再次审核。

退款单状态机：`pending`（待审核）→ `approved`（已同意并生效）/ `rejected`（已拒绝）；`approved` → `reversed`（已冲正，终态）。订单对外 `paid_cents` 为净已收（累计收款与收款回退轧差后的毛额，再扣减已批准未冲正退款），`outstanding_cents = amount_cents − paid_cents` 恒成立，`paid_cents` 始终落在 [0, 订单金额]。

### 收款回退

收款回退用于收款登记后发现收错或重复收款时，把该笔收款从订单已收金额中反向退回，与退款冲正共同构成账务闭环。

- 业务身份为（租户, `rollback_id`）：同一身份重复提交不新建回退记录、不重复退回金额，只返回既有回退结果（HTTP 200），与本次请求携带的 `order_id`、`amount_cents` 指纹无关；首次成功返回 201。
- 回退金额必须是大于 0 的最小货币单位整数，且不得超过订单当前已收（净）金额；回退后净已收 = 回退前净已收 − 回退金额，未收金额 = 订单金额 − 已收金额随之回升，已收金额始终在 0 与订单金额之间。
- 与退款额度口径相容：登记退款单时已把待审核与已生效未冲正退款合并计入占用额度，回退后该占用仍不得超过订单已收金额；否则返回 409（超出剩余可回退额度）且不改变任何数据。拒绝待审核退款会释放占用，冲正已生效退款会恢复已收，二者之后可回退空间相应变化。
- 回退不影响退款单后续按原规则审核与冲正；回退与审核、冲正任意交错后，恒有 订单金额 = 已收金额 + 未收金额。
- 回退到不存在或非本租户订单、金额非法、回退金额超过当前已收金额返回 400；占用超额返回 409；读取不存在或跨租户回退单返回 404；均区别于 500。任何失败都不留下半张回退单或半次金额调整——回退记录写入与订单已收金额调整在同一数据库事务内提交。
- 跨租户读取或执行回退一律按不存在/参数错误处理，不泄漏订单或回退单是否存在；回退记录与订单金额持久化落库，服务重启前后查询结论一致。

### 批量受理

批次结果字段：

- `batch_id`：批次标识；`status`：`completed`（全成功/空批次）、`completed_with_errors`（有失败行）、`in_progress`（处理中断，尚未跑完，只可能在中断后、续跑完成前查询到）。
- `total`、`succeeded`、`failed`：数据行总数与成功/失败计数，恒有 `succeeded + failed = total`（终态）。
- `errors`：失败行清单，每项含 `line_no`（CSV 物理行号，表头为第 1 行）、`order_id`、`error_code`（`invalid_parameter` 行参数错误；`order_conflict` 订单此前或批内已受理）与 `message`；失败行不受理。
- `accepted_order_ids`：本批次受理成功的订单标识，按行号排序；这些订单可按既有接口读取、登记收款。

业务规则：

- 批次身份是（租户, `batch_id`），与行内容无关：相同身份的重试不新建批次；终态后重放（即使 CSV 内容不同）只返回既有结果与计数，不再受理任何订单。
- CSV 格式问题整批拒绝（400，不产生批次、不受理任何行）；格式合法但个别行业务校验失败只跳过该行。行内 `tenant` 必须与请求声明租户一致，否则按 `invalid_parameter` 记入错误清单。
- 批次中出现已受理订单：不重复受理、不改变其数据，按 `order_conflict` 记入错误清单；同一行最多产生一张订单。
- 中断续跑：每行在独立事务内提交（订单写入与行结果同事务），服务停止或请求中断后已提交行不回滚、未处理行不留半张单据；用同一 `batch_id` 与**同一份输入**重新提交即从断点继续。进行中批次若用不同输入续跑返回 409。
- 跨租户查询批次一律 404，不泄漏批次是否存在。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，写事务以 `BEGIN IMMEDIATE` 串行执行，未做连接池与多实例扩展。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量受理为同步逐行提交，适合中小文件，未做异步任务队列。
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

```bash
# 收款回退（回退单以租户 + rollback_id 唯一；重复提交返回既有结果，不重复退回）
curl -s -X POST http://127.0.0.1:8000/payment-rollbacks \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","rollback_id":"rb-1","order_id":"demo-1","amount_cents":200}'

# 读取收款回退单（需 X-Tenant 头；跨租户一律 404）
curl -s http://127.0.0.1:8000/payment-rollbacks/rb-1 -H 'X-Tenant: t1'
```

```bash
# 批量受理（JSON 形态：csv 字段携带 CSV 文本）
curl -s -X POST http://127.0.0.1:8000/orders/batch-accept \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant":"t1",
    "batch_id":"batch-20260401",
    "csv":"tenant,order_id,amount_cents,currency\nt1,o-100,1200,CNY\nt1,o-101,800,CNY\n"
  }'

# 批量受理（直接上传 CSV 文件；中断后用相同 tenant/batch_id 与同一份文件重提即断点续跑）
curl -s -X POST 'http://127.0.0.1:8000/orders/batch-accept?tenant=t1&batch_id=batch-20260401' \
  -H 'Content-Type: text/csv' --data-binary @fixtures/orders.csv

# 查询批次结果（需 X-Tenant 头；跨租户一律 404）
curl -s http://127.0.0.1:8000/batches/batch-20260401 -H 'X-Tenant: t1'
```
