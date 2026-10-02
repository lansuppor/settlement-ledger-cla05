CREATE TABLE IF NOT EXISTS payment_rollbacks(
  tenant TEXT NOT NULL,
  rollback_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  PRIMARY KEY(tenant, rollback_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_rollbacks_order ON payment_rollbacks(tenant, order_id);
