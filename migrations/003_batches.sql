-- 批量受理：批次以（租户, batch_id）为业务身份，与行内容、订单标识无关。
-- input_text 保存首次提交的 CSV 原文：中断后用同一 batch_id 续跑时以存档为准，
-- processed 为已提交行数断点；每行与订单写入在同一事务内提交，不留半张单据。
CREATE TABLE IF NOT EXISTS batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('in_progress','completed','completed_with_errors')),
  total INTEGER NOT NULL,
  processed INTEGER NOT NULL DEFAULT 0,
  input_text TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, batch_id)
);

CREATE TABLE IF NOT EXISTS batch_lines(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  line_no INTEGER NOT NULL,
  order_id TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL CHECK(outcome IN ('success','invalid_param','order_conflict')),
  message TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(tenant, batch_id, seq),
  FOREIGN KEY(tenant, batch_id) REFERENCES batches(tenant, batch_id)
);
