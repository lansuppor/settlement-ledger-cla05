-- 订单账务流水：每笔真正生效的账务动作（收款、收款回退、退款登记/审核/冲正）
-- 在其数据库事务内追加一条不可变流水。动作被拒绝或失败时不写流水，因此
-- 拒绝/失败路径不存在对应的 action_type，下面的取值即全部合法动作。
--
-- seq 为表级 AUTOINCREMENT 单调递增、不随删除改变的发生顺序；同一订单的流水
-- 按 seq 从早到晚排列并据此翻页。
--
-- 动作的业务标识分两类：
--   1. 收款不携带独立业务标识，ref_type='payment'，ref_id 存订单标识，按所属订单记入；
--   2. 收款回退 ref_type='rollback'、退款（登记/同意/拒绝/冲正）ref_type='refund'，
--      ref_id 分别为回退标识、退款标识。
--
-- 下列部分唯一索引把“每次动作恰好一条、不重不漏”下沉到存储层：
--   - 每个回退标识至多一条回退流水（重复提交同一业务身份不追加）；
--   - 每个退款标识的登记至多一条；
--   - 同一退款单同意、拒绝各至多一条（重复审核不产生新动作，不追加）；
--   - 每个退款单冲正至多一条（对已冲正单据重复冲正不追加）。
-- 收款按订单记入且无独立标识，多笔收款天然各对应一条。
CREATE TABLE IF NOT EXISTS order_account_entries(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  action_type TEXT NOT NULL CHECK(action_type IN
    ('payment_received', 'payment_rolled_back', 'refund_registered',
     'refund_approved', 'refund_rejected', 'refund_reversed')),
  ref_type TEXT NOT NULL CHECK(ref_type IN ('payment', 'rollback', 'refund')),
  ref_id TEXT NOT NULL,
  change_cents INTEGER NOT NULL,
  balance_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

-- 同一订单按发生顺序（seq 升序）翻页读取的主路径。
CREATE INDEX IF NOT EXISTS idx_account_entries_order ON order_account_entries(tenant, order_id, seq);

-- 收款回退：每个（租户, 回退标识）至多一条回退流水。
CREATE UNIQUE INDEX IF NOT EXISTS uq_account_entries_rollback
  ON order_account_entries(tenant, ref_id)
  WHERE ref_type='rollback';

-- 退款登记：每个（租户, 退款标识）至多一条登记流水。
CREATE UNIQUE INDEX IF NOT EXISTS uq_account_entries_refund_register
  ON order_account_entries(tenant, ref_id)
  WHERE action_type='refund_registered';

-- 退款审核：同一退款单同意、拒绝各至多一条。
CREATE UNIQUE INDEX IF NOT EXISTS uq_account_entries_refund_approve
  ON order_account_entries(tenant, ref_id)
  WHERE action_type='refund_approved';

CREATE UNIQUE INDEX IF NOT EXISTS uq_account_entries_refund_reject
  ON order_account_entries(tenant, ref_id)
  WHERE action_type='refund_rejected';

-- 退款冲正：每个退款单至多一条。
CREATE UNIQUE INDEX IF NOT EXISTS uq_account_entries_refund_reverse
  ON order_account_entries(tenant, ref_id)
  WHERE action_type='refund_reversed';
