CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','reversed')),
  PRIMARY KEY(tenant, refund_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(tenant, order_id);
