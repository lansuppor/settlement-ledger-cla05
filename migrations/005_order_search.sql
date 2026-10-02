-- 条件检索：按状态过滤并按 order_id 升序做游标分页时覆盖 (tenant, status, order_id)。
-- 无条件或仅金额区间条件的检索仍走主键 (tenant, order_id)。
CREATE INDEX IF NOT EXISTS idx_orders_tenant_status ON orders(tenant, status, order_id);
