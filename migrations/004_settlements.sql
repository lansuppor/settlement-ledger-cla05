-- 结算单：针对已存在订单发起的对账核销单，以（租户, 订单标识, 结算单标识）唯一
CREATE TABLE IF NOT EXISTS settlements(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','written_off','cancelled','reversed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, settlement_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 幂等记录：(租户, 请求标识) 唯一；重放返回首次结果，不重复占用、扣减或释放
CREATE TABLE IF NOT EXISTS settlement_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  op TEXT NOT NULL,
  order_id TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);
