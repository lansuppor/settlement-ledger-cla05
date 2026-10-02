-- 对账单：针对已存在退款单发起的对账核销单，以（租户, 订单标识, 退款单标识, 对账单标识）唯一
CREATE TABLE IF NOT EXISTS refund_reconciliations(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  reconciliation_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','settled','cancelled','reversed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, refund_id, reconciliation_id),
  FOREIGN KEY(tenant, order_id, refund_id) REFERENCES refunds(tenant, order_id, refund_id)
);

-- 幂等记录：(租户, 请求标识) 唯一；重放返回首次结果，不重复占用/扣减/释放
CREATE TABLE IF NOT EXISTS reconciliation_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  op TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  reconciliation_id TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);
