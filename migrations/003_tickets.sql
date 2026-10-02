-- 工单：针对已存在退款单发起的争议处理单，以（租户, 订单标识, 退款单标识, 工单标识）唯一
CREATE TABLE IF NOT EXISTS refund_tickets(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  ticket_id TEXT NOT NULL,
  request_amount_cents INTEGER NOT NULL CHECK(request_amount_cents > 0),
  initiator TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('accepted','processing','review','resolved','revoked')),
  -- 推进到已解决时落定的裁决金额；撤销后清零（加回退款单）
  award_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, refund_id, ticket_id),
  FOREIGN KEY(tenant, order_id, refund_id) REFERENCES refunds(tenant, order_id, refund_id)
);

-- 处理中标记：一张退款单最多被一张进行中（已受理/处理中/待复核）工单标记。
-- 终态（已解决/已撤销）行不占用标记，故同一退款单可在工单终结后再次受理新工单。
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_ticket_per_refund
  ON refund_tickets(tenant, order_id, refund_id)
  WHERE status IN ('accepted','processing','review');

-- 幂等记录：(租户, 请求标识) 唯一；重放返回首次结果，不重复扣减或释放
CREATE TABLE IF NOT EXISTS ticket_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  op TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  ticket_id TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);
