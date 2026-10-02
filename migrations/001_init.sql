CREATE TABLE IF NOT EXISTS orders(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id)
);

-- 退款单：同一（租户, 订单, 退款单标识）唯一
CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  -- pending 待处理 / completed 已完成 / cancelled 已撤销 / reversed 已冲正
  status TEXT NOT NULL DEFAULT 'pending',
  -- 当前实际生效扣减：completed=金额，reversed=0，pending/cancelled=0
  effective_deduction_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, refund_id)
);

CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(tenant, order_id);

-- 幂等记录：每个（退款单, 动作, 请求标识）只执行一次
CREATE TABLE IF NOT EXISTS refund_requests(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  action TEXT NOT NULL,          -- accept / complete / cancel / reverse
  request_id TEXT NOT NULL,
  response_status TEXT NOT NULL, -- 完成时退款单状态
  response_amount INTEGER NOT NULL,
  response_deduction INTEGER NOT NULL,
  executed_at TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, refund_id, action, request_id)
);
