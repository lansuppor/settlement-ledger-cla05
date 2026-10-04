-- 出入库单：按租户隔离的库存单据，以（租户, 单号）唯一；不预设商品目录与期初库存
CREATE TABLE IF NOT EXISTS inventory_docs(
  tenant TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  warehouse_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
  quantity INTEGER NOT NULL CHECK(quantity > 0),
  status TEXT NOT NULL CHECK(status IN ('pending','posted','cancelled','reversed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, doc_id)
);

-- 余额按单据流水推导，另建索引加速按（租户, 仓库, 商品）汇总
CREATE INDEX IF NOT EXISTS idx_inventory_docs_balance
  ON inventory_docs(tenant, warehouse_id, item_id, status, direction);

-- 幂等记录：(租户, 请求标识) 唯一；重放返回首次结果（含业务拒绝），不重复占用或改动库存；
-- params_json 记录操作参数指纹，同一标识改作其他操作/对象/参数须报冲突
CREATE TABLE IF NOT EXISTS inventory_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  op TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  params_json TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);
