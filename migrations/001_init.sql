CREATE TABLE IF NOT EXISTS orders(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id)
);

CREATE TABLE IF NOT EXISTS payment_requests(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  result TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id, request_id)
);
