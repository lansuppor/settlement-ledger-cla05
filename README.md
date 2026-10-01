# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额，订单的 CSV 批量受理（逐行校验、部分成功、重放幂等与中断续跑），以及退款单的登记、读取、审核（同意/拒绝）与冲正（撤销）；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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

### 批量受理

- `POST /batches`：批量受理订单，以（租户, `batch_id`）为批次业务身份。请求体支持两种形式：
  - JSON：`{"tenant":..., "batch_id":..., "csv":"<CSV 文本>"}`（`Content-Type: application/json`）；
  - CSV 原文：`Content-Type: text/csv`，`tenant`、`batch_id` 通过查询参数传入，如 `POST /batches?tenant=t1&batch_id=B1`。

  CSV 首行必须为表头 `tenant,order_id,amount_cents,currency`，其后每行为一笔订单，字段含义与 `POST /orders` 一致。成功首次受理返回 201；同一（租户, `batch_id`）重放不新建批次、不重复受理任何订单，返回既有结果（200），行内容不同也不新建批次。
- `GET /batches/{batch_id}`：按批次标识查询批次结果。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404（不泄漏批次是否存在）。

批次结果字段：

- `batch_id`、`status`、`total`（数据行总数）、`success_count`、`failure_count`、`errors`（错误清单）、`accepted_order_ids`（本批次成功受理的订单标识列表）。
- `status` 取值：`in_progress`（处理中/中断）、`completed`（全部成功）、`completed_with_errors`（有失败行但已处理完）。恒有 `success_count + failure_count = total`。
- `errors` 每项含 `line_no`（从 1 计、含表头行，故首条数据行为 2）、`code`、`message`。`code` 区分参数错误 `invalid_param` 与订单冲突 `order_conflict`。

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

# 批量受理（JSON 内嵌 CSV 文本；同一 batch_id 重放返回既有结果，不重复受理）
curl -s -X POST http://127.0.0.1:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","batch_id":"b-20261001","csv":"tenant,order_id,amount_cents,currency\nt1,b1,1200,CNY\nt1,b2,800,CNY\n"}'

# 批量受理（直接上传 CSV 文件；fixtures/orders.csv 内 t2 行会按租户不一致计入错误清单）
curl -s -X POST 'http://127.0.0.1:8000/batches?tenant=t1&batch_id=b-file' \
  -H 'Content-Type: text/csv' --data-binary @fixtures/orders.csv

# 查询批次结果（查询需 X-Tenant 头；跨租户返回 404）
curl -s http://127.0.0.1:8000/batches/b-20261001 -H 'X-Tenant: t1'
```
