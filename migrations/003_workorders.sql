-- 工单：针对退款单发起的争议处理单，以（租户, 订单标识, 退款单标识, 工单标识）唯一
CREATE TABLE IF NOT EXISTS workorders(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  workorder_id TEXT NOT NULL,
  claim_amount_cents INTEGER NOT NULL CHECK(claim_amount_cents > 0),
  initiator TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('accepted','processing','pending_review','resolved','cancelled')),
  award_cents INTEGER NOT NULL DEFAULT 0,
  effective_deduction_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, refund_id, workorder_id),
  FOREIGN KEY(tenant, order_id, refund_id) REFERENCES refunds(tenant, order_id, refund_id)
);

-- 幂等记录：(租户, 请求标识) 唯一；重放返回首次结果，不重复扣减/释放
CREATE TABLE IF NOT EXISTS workorder_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  op TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  workorder_id TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);
