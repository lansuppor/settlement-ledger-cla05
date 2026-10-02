-- 订单对账核销：把一次对账核对的结论固化为不可变更的对账单。
--
-- 对账单以（租户, reconcile_id）为业务身份：同一身份重复提交不重新核对、不改变
-- 既有结论，只返回首张对账单，与请求携带的范围指纹无关。对账单在一次数据库只读
-- 事务、同一份已提交快照内完成核对后整体写入（单行即全部结论），因此任何失败都
-- 不会留下半张对账单；写入本身为纯结论落库，不改变任何订单、收款、回退、退款
-- 与流水数据。
--
-- scope_json：本次核对范围（单张订单/订单金额区间/未收金额区间，及其交集），
--   仅作留痕；幂等判定只看（tenant, reconcile_id），不与该指纹比较。
-- result_json：范围内每张订单的核对结果（金额、已收、未收、逐条累计余额与衔接
--   结论、末条余额是否等于当前已收）与差异清单，重读即得同一结论。
-- status：reconciled（已核销）/ discrepancy（有差异）。
CREATE TABLE IF NOT EXISTS reconciliations(
  tenant TEXT NOT NULL,
  reconcile_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('reconciled', 'discrepancy')),
  order_count INTEGER NOT NULL CHECK(order_count >= 0),
  discrepancy_count INTEGER NOT NULL CHECK(discrepancy_count >= 0),
  scope_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(tenant, reconcile_id)
);
