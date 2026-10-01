CREATE TABLE IF NOT EXISTS batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  total INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('in_progress','completed','completed_with_errors')),
  PRIMARY KEY(tenant, batch_id)
);

CREATE TABLE IF NOT EXISTS batch_orders(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('success','failed')),
  error_code TEXT,
  error_message TEXT,
  accepted_order_id TEXT,
  PRIMARY KEY(tenant, batch_id, line_no),
  FOREIGN KEY(tenant, batch_id) REFERENCES batches(tenant, batch_id)
);

CREATE INDEX IF NOT EXISTS idx_batch_orders_batch ON batch_orders(tenant, batch_id);
