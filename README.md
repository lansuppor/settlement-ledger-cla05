# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额，以及退款单的登记、读取、审核与冲正；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`（启动时自动执行 `migrations/` 下全部迁移脚本）
- 仅执行数据库迁移：`python3 -m app.entry --migrate`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

### 订单与收款

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `GET /health`：返回服务与数据库状态。

### 退款单

- `POST /refunds`：登记退款单，进入待审核态（`pending`），此时不实际退款。请求体：
  ```json
  {"tenant": "t1", "refund_id": "r1", "order_id": "o1", "amount_cents": 200, "reason": "商品瑕疵"}
  ```
  - 成功新建返回 201；同一（租户, 退款标识）重复登记返回 200 与既有单当前状态，不新建、不重复退款，请求指纹（金额、原因等）不影响幂等判定。
  - 字段缺失/空白、金额不是大于 0 的整数、原因缺失返回 400；订单不存在或不属于该租户也返回 400。
  - 待审核退款会预留已收金额；本单与同订单其他待审核退款合计超过订单当前已收金额返回 409。
- `GET /refunds/{refund_id}`：读取退款单。必须带头 `X-Tenant`；不存在或跨租户一律 404。
- `POST /refunds/{refund_id}/review`：审核，必须带头 `X-Tenant`。请求体 `{"decision": "approve"}` 或 `{"decision": "reject"}`。
  - 同意：订单已收金额扣减、未收金额等额增加；若扣减会突破已收金额（与其他待审核预留冲突）返回 409。
  - 拒绝：单据置为 `rejected`，订单不变。
  - 审核结果不可覆盖：对已同意/已拒绝的单据重复审核返回原结果（200）；已冲正的单据再审核返回 409。
  - 单据不存在或属于其他租户返回 404，且不会对其他租户的单据产生任何作用。
- `POST /refunds/{refund_id}/reverse`：冲正（撤销），必须带头 `X-Tenant`，无请求体。
  - 仅对已同意（`approved`）的单据生效：已退款金额全额回补订单已收，单据置为 `reversed`。
  - 待审核、已拒绝、已冲正的单据冲正返回 409 且不改变任何数据；冲正后不得再次审核。
  - 不存在或跨租户返回 404。

退款单对象字段：`tenant`、`refund_id`、`order_id`、`amount_cents`、`reason`、`status`（`pending` / `approved` / `rejected` / `reversed`）。

全部金额调整在数据库事务内完成并加写锁：并发登记同一退款标识最终只有一张单据；审核与冲正交错后订单金额必然闭合（订单金额 = 已收 + 未收，且 0 ≤ 已收 ≤ 订单金额）；状态持久落库，重启后结论一致。详见 [docs/business-rules.md](docs/business-rules.md)。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优（已设置 SQLite `busy_timeout`，写事务串行执行）。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账。
