-- 退款单批量导入：以（租户, 批次请求标识）唯一，记录提交批次的指纹与逐行结论
CREATE TABLE IF NOT EXISTS refund_imports(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  -- 提交内容的规范化指纹：同标识重放须一致，改作其他批次/对象 -> 409
  fingerprint TEXT NOT NULL,
  succeeded_count INTEGER NOT NULL,
  failed_count INTEGER NOT NULL,
  response_json TEXT NOT NULL DEFAULT '',
  -- 0=仍有未判行（断点）；1=全部行已判定，response_json 为首次完整结果
  completed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id)
);

-- 批次逐行结论：行号按提交顺序；已判行冻结，断点续跑只补判缺失行
CREATE TABLE IF NOT EXISTS refund_import_rows(
  tenant TEXT NOT NULL,
  request_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  -- 原样回显提交金额（非法值也能呈现），以 JSON 标量保存
  amount_raw TEXT NOT NULL,
  conclusion TEXT NOT NULL CHECK(conclusion IN ('accepted','rejected')),
  -- NULL 表示受理成功；否则为可区分的失败原因码
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, request_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_refund_import_rows
  ON refund_import_rows(tenant, request_id, line_no);
