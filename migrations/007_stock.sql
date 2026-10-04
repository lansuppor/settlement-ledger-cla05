-- 出入库单：以（租户, 单号）唯一；不预设商品目录与期初库存
CREATE TABLE IF NOT EXISTS stock_documents(
  tenant TEXT NOT NULL,
  doc_no TEXT NOT NULL,
  warehouse_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('in','out')),
  quantity INTEGER NOT NULL CHECK(quantity > 0),
  status TEXT NOT NULL CHECK(status IN ('pending','posted','cancelled','reversed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, doc_no)
);

-- 库存余额：按（租户, 仓库, 商品）维护账面数量与待入账出库占用；可用 = 账面 - 占用，三者不得为负
CREATE TABLE IF NOT EXISTS stock_balances(
  tenant TEXT NOT NULL,
  warehouse_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  book_qty INTEGER NOT NULL CHECK(book_qty >= 0),
  occupied_qty INTEGER NOT NULL CHECK(occupied_qty >= 0),
  PRIMARY KEY(tenant, warehouse_id, item_id)
);

-- 幂等记录：(租户, 请求标识) 唯一；重放返回首次结果（含业务拒绝），不重复占用或改动库存
CREATE TABLE IF NOT EXISTS stock_requests(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  op TEXT NOT NULL,
  doc_no TEXT NOT NULL,
  params_json TEXT NOT NULL,
  http_status INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);
