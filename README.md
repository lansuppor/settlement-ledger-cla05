# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额、收款回退（登记后发现收错或重复收款时反向退回），退款单的登记、读取、审核（同意/拒绝）与冲正（撤销），以及订单对账核销（把账务流水与订单当前金额的核对结论固化为不可变更、可查询的对账单）；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `GET /orders/{order_id}/account-entries`：按订单查询账务流水（见“订单账务流水”）。租户通过请求头 `X-Tenant` 传入；订单不存在或跨租户一律返回 404（不泄漏对象是否存在）。查询参数 `page_size`（必传，正整数）、`cursor`（可选，上一页最后一条流水的顺序位置 `seq`，返回其之后的流水）。
- `GET /orders`：按条件检索订单（订单标识之外的检索入口）。租户通过请求头 `X-Tenant` 传入，结果只含本租户订单；跨租户检索一律得到空结果，不泄漏其他租户是否有匹配订单。查询参数：
  - `page_size`（必传）：每页条数，正整数；`cursor`（可选）：上一页最后一条订单标识，返回其**之后**的订单。
  - `status`（可选）：`accepted`（受理中）或 `settled`（已结清），与订单对象 `status` 字段口径一致。
  - 金额区间（可选，含边界，可只给上限或下限，非负整数）：`amount_min`/`amount_max` 按订单金额过滤；`outstanding_min`/`outstanding_max` 按未收金额过滤（金额口径与订单读取一致，未收 = 订单金额 − 净已收）。
  - 多条件同时给出取交集；区间端点非法（负数/非整数/下限大于上限）、状态取值不支持、游标不指向本租户订单均返回 400，与 500 区分。
  - 返回 `orders`（按订单标识升序）、`total`（该条件下订单总数，与页大小/页码无关）、`page_size`、`next_cursor`、`has_next`。同一组条件下顺序稳定；无写入时游标逐页取完与一次取全得到完全相同的订单集合，不重不漏。翻页期间新提交的收款/回退/退款审核/冲正只影响后续页所见状态，单次读取不会出现半张单据，连续翻页中每张订单只出现一次。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /payment-rollbacks`：收款回退。请求字段 `tenant`、`rollback_id`、`order_id`、`amount_cents`；`amount_cents` 为大于 0 的最小货币单位整数，回退以（租户, `rollback_id`）为业务身份。成功返回 201 与状态为 `completed` 的回退单；同一（租户, `rollback_id`）重复请求不新建记录、不重复退回金额，返回 200 与既有回退结果（业务身份与请求携带的金额/订单指纹无关）。回退后订单净已收按净额重算，未收 = 订单金额 − 已收。订单不存在或非本租户、金额非法、回退金额超过订单当前已收金额返回 400；回退会使退款占用额度（待审核 + 已生效未冲正）超过回退后已收金额、即超出剩余可回退额度，返回 409 且不改变任何数据。
- `GET /payment-rollbacks/{rollback_id}`：按回退标识读取收款回退单。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404（不泄漏对象是否存在）。
- `POST /orders/batch-accept`：批量受理订单。批次身份为（租户, `batch_id`）。支持两种提交形态：
  - `application/json`：请求字段 `tenant`、`batch_id`、`csv`（CSV 文本）。
  - 直接上传 CSV 文件（如 `Content-Type: text/csv`）：请求体即 CSV 字节，`tenant` 与 `batch_id` 通过查询参数传入（`?tenant=t1&batch_id=...`）。
  - CSV 表头必须恰为 `tenant,order_id,amount_cents,currency`，字段口径与单笔受理一致。首次处理返回 201 与批次结果；同一（租户, `batch_id`）重放返回 200 与既有结果，不重复受理任何订单。CSV 格式非法（缺表头、列数不符、编码非法、行结构损坏）整批拒绝返回 400 且不受理任何行；行业务校验失败只跳过该行并计入错误清单。进行中批次只接受同一份输入续跑，输入不一致返回 409。
- `GET /batches/{batch_id}`：按批次标识查询批次结果。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404（不泄漏批次是否存在）。
- `POST /reconciliations`：发起订单对账核销（见“订单对账核销”）。请求字段 `tenant`、`reconcile_id` 与对账范围；首次成功返回 201 与对账单，同一（租户, `reconcile_id`）重复提交返回 200 与既有对账单，不重新核对、不改变既有结论（与本次携带的范围指纹无关）。范围非法或范围下没有任何订单返回 400。
- `GET /reconciliations/{reconcile_id}`：按标识读取对账单。租户通过请求头 `X-Tenant` 传入；不存在或跨租户一律返回 404（不泄漏对账单是否存在）。
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

### 订单账务流水

账务流水把一张订单上收款、收款回退、退款单登记/审核同意/审核拒绝/冲正的每一次账务变动按发生顺序持久留痕，用于回答单据为何处于当前状态、失败或重试后哪些操作已经生效。

- 每笔账务动作成功落库时，在与单据写入、状态迁移、金额调整**同一个数据库事务**内追加一条不可变流水；动作被拒绝或失败时不写流水。不存在动作生效而流水缺失、或流水存在而动作未生效的情况；服务崩溃后已提交的动作与流水一起保留、未提交的一起丢弃。
- 每次动作恰好一条记录，不重不漏：重复提交同一业务身份的退款登记或收款回退、对同一退款单重复审核、对已冲正单据重复冲正等不产生新动作的请求，均不追加流水（数据库部分唯一索引兜底）。
- 反向动作（收款回退、退款冲正）以**新流水**体现，不改写历史；流水不可修改或删除。

流水字段：

- `seq`：发生顺序位置，表级单调递增；同一订单的流水严格按 `seq` 从早到晚排列。
- `action_type`：变动类型，取值 `payment_received`（收款）、`payment_rolled_back`（收款回退）、`refund_registered`（退款登记）、`refund_approved`（审核同意）、`refund_rejected`（审核拒绝）、`refund_reversed`（退款冲正）。
- 业务标识：收款不新建标识，`ref_type='payment'`、`ref_id` 记所属订单标识；收款回退 `ref_type='rollback'`、`ref_id` 为回退标识；退款登记/同意/拒绝/冲正 `ref_type='refund'`、`ref_id` 为退款标识。每条流水都经 `tenant`、`order_id` 唯一回指对应订单。
- `change_cents`：该订单对外已收金额（净已收，口径与订单对象 `paid_cents` 一致）的变化额：收款与退款冲正为正，收款回退与审核同意为负；登记、审核拒绝等不改变金额的动作记 `0`。
- `balance_cents`：变化后余额。逐条满足 本条余额 = 上一条余额 + 本条变化额（首条从 0 起）；任意时点余额落在 0 与订单金额之间；最后一条余额等于该订单当前对外已收金额。
- `created_at`：落库时间（UTC，由数据库生成）。

查询与翻页：

- `GET /orders/{order_id}/account-entries`，租户由请求头 `X-Tenant` 传入；缺少租户头返回 400；订单不存在或跨租户一律 404，不泄漏对象是否存在。
- `page_size` 必传且为正整数；`cursor` 可选，为上一页最后一条流水的 `seq`，返回其之后的流水，每页最多 `page_size` 条。
- `cursor` 必须指向本租户该订单的一条真实流水，否则返回 400（属于其他租户或其他订单的顺序位置同样按参数错误处理，不泄漏对象是否存在）。
- 返回 `entries`（按 `seq` 升序）、`page_size`、`next_cursor`、`has_next`；末页 `has_next` 为假且 `next_cursor` 为空。无新动作时每次读取顺序与内容一致；查询在单个只读事务、同一份已提交快照内完成，并发动作落库后后续查询看到其完整流水，绝不出现半条记录。
- 订单受理不是账务动作，不产生流水；刚受理、尚无账务动作的订单返回空流水页。

### 订单对账核销

对账把账务流水与订单当前金额的核对结论固化为一张可查询、不可变更的对账单，回答某个时点账面是否闭合、差异出在哪一笔。对账以（租户, `reconcile_id`）为业务身份。

- 发起：`POST /reconciliations`，请求字段 `tenant`、`reconcile_id` 与对账范围。范围可三选一并取交集（至少声明一种）：
  - `order_id`：单张订单（非空字符串）；
  - 订单金额区间：`amount_min`/`amount_max`；
  - 未收金额区间：`outstanding_min`/`outstanding_max`（金额口径与订单读取一致，未收 = 订单金额 − 净已收）。
  - 区间端点含边界、可只给一侧，必须是非负整数；端点非法、下限大于上限、未声明任何范围形态、或范围下没有任何订单（含单张订单不存在/非本租户）均返回 400，与 500 区分，且不留下半张对账单。
- 核对在一次数据库事务、同一份已提交快照内完成，对范围内每张订单给出：订单金额、对外已收金额（净已收，口径与订单对象一致）、未收金额、逐条流水（含逐条累计的 `expected_balance_cents` 与衔接结论 `chained`）、`chain_intact`（余额衔接是否完整）、`final_balance_matches_paid`（末条流水余额是否等于当前已收；无流水时末余额按 0 计）、`amount_closed`（订单金额 = 已收 + 未收且已收落在 [0, 订单金额]）。
- 对账单状态：每张订单都“逐条衔接、末条余额等于当前已收、金额闭合”时为 `reconciled`（已核销）；任一订单不满足即为 `discrepancy`（有差异）。差异清单 `discrepancies` 逐张订单给出 `order_id`、`reasons`（`balance_chain_broken` 余额衔接断链 / `final_balance_mismatch` 末条余额与当前已收不一致 / `amount_not_closed` 金额不闭合，可并存）、断链流水 `broken_entry_seqs`、末条流水余额 `last_balance_cents` 与当前已收 `current_paid_cents`。
- 不可变与幂等：同一（租户, `reconcile_id`）重复提交不重新核对、不改变既有结论，返回首张对账单（HTTP 200），即使本次携带完全不同的范围、或此后订单数据已经变化；不同 `reconcile_id` 各自得到独立的一张，核对时点即首次提交的已提交快照。并发下多个客户端用不同标识同时核对同一范围，各自得到独立且自洽的结论；并发提交同一标识恰好生成一张（一张 201，其余 200）。
- 对账过程为纯读取：不改变任何订单、收款、收款回退、退款与流水数据，唯一写入是对账单结论本身。对账单持久化落库，服务重启后按标识重读结论一致。
- 读取：`GET /reconciliations/{reconcile_id}`，租户由请求头 `X-Tenant` 传入，缺失租户头返回 400；不存在或跨租户一律 404，不泄漏对账单是否存在。
- 返回字段：`reconcile_id`、`status`（`reconciled`/`discrepancy`）、`order_count`（核对订单总数）、`discrepancy_count`（差异订单数）、`orders`（逐订单核对结果，按订单标识升序）、`discrepancies`（差异清单）、`scope`（首次提交的范围留痕）、`reconciled_at`（核对时点，UTC，由数据库生成）。
- 对账能力不改变既有订单受理、读取、收款、退款单登记/审核/冲正、收款回退、批量受理、条件检索与账务流水的任何行为。

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
- 退款支持逐笔登记、审核与冲正，未实现部分退款的分期审批流。

## 调用示例

```bash
# 条件检索（X-Tenant 限定租户；可组合状态、订单金额/未收金额区间，取交集）
curl -s 'http://127.0.0.1:8000/orders?page_size=20&status=accepted&outstanding_min=100' -H 'X-Tenant: t1'

# 用上一页返回的 next_cursor 继续翻页（游标之后的订单，每页最多 page_size 条）
curl -s 'http://127.0.0.1:8000/orders?page_size=20&status=accepted&cursor=o-101' -H 'X-Tenant: t1'
```

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
# 查询订单账务流水首页（按发生顺序 seq 升序，每页最多 page_size 条）
curl -s 'http://127.0.0.1:8000/orders/demo-1/account-entries?page_size=20' -H 'X-Tenant: t1'

# 用上一页返回的 next_cursor（末条流水的 seq）继续翻页
curl -s 'http://127.0.0.1:8000/orders/demo-1/account-entries?page_size=20&cursor=8' -H 'X-Tenant: t1'
```

```bash
# 发起对账（单张订单；首次 201，同一 tenant+reconcile_id 重放 200 且结论不变）
curl -s -X POST http://127.0.0.1:8000/reconciliations \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","reconcile_id":"rec-20261002","order_id":"demo-1"}'

# 发起对账（订单金额与未收金额区间取交集，端点含边界，可只给一侧）
curl -s -X POST http://127.0.0.1:8000/reconciliations \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"t1","reconcile_id":"rec-range","amount_min":100,"outstanding_min":0,"outstanding_max":500}'

# 读取对账单（需 X-Tenant 头；不存在或跨租户一律 404）
curl -s http://127.0.0.1:8000/reconciliations/rec-20261002 -H 'X-Tenant: t1'
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
