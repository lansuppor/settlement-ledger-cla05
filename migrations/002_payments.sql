CREATE TABLE IF NOT EXISTS payment_requests(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  PRIMARY KEY(tenant, order_id, request_id)
);
