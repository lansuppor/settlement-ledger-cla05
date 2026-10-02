-- 退款单批量导入：一次提交多条退款单受理记录，逐行判定、部分成功、可断点续跑。
-- 批次以（租户, 批次请求标识）唯一；fingerprint 为提交行内容的规范摘要，
-- 同一请求标识改作其他批次/对象（行内容不一致）返回 409。
CREATE TABLE IF NOT EXISTS refund_import_batches(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('in_progress','completed')),
  http_status INTEGER,
  response_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);

-- 逐行检查点：每行独立事务落库，成功行已真正受理（占用可退余额），
-- 失败行只记录原因、不产生任何退款单写入；中断后续跑据此跳过已判定行。
CREATE TABLE IF NOT EXISTS refund_import_rows(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  order_id TEXT,
  refund_id TEXT,
  amount_cents INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('accepted','rejected')),
  reason TEXT,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id, position)
);
