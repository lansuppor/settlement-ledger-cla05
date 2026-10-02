-- 订单账务流水：每张订单上每个成功落库的账务动作恰好在其所在事务内追加一条不可变记录。
-- 发生顺序按 (租户, 订单) 内单调递增的 seq 编排；主键即顺序索引，只追加、不修改、不删除。
CREATE TABLE IF NOT EXISTS order_ledger(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK(seq > 0),
  action_type TEXT NOT NULL CHECK(action_type IN (
    'payment',
    'payment_rollback',
    'refund_registered',
    'refund_approved',
    'refund_rejected',
    'refund_reversed'
  )),
  -- 动作业务标识的归属：收款不新建标识（按所属订单记入），回退用回退标识，退款各动作均用退款标识。
  ref_kind TEXT NOT NULL CHECK(ref_kind IN ('order', 'rollback', 'refund')),
  ref_id TEXT NOT NULL,
  -- 该订单对外已收（净）金额的变化额；不改变金额的动作（登记/拒绝）记 0；反向动作以负值/正值新流水体现。
  delta_cents INTEGER NOT NULL,
  -- 变化后余额，与订单对象 paid_cents 同口径；非负，且业务上恒在 0 与订单金额之间。
  balance_after_cents INTEGER NOT NULL CHECK(balance_after_cents >= 0),
  PRIMARY KEY(tenant, order_id, seq),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);
