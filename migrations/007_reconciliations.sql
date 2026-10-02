-- 订单对账核销：对账以（租户, 对账标识 reconcile_id）为业务身份，每次对账恰好生成一张
-- 不可变更的对账单。对账单把“核对时点 + 范围内每张订单的逐条流水核对结果 + 总体状态
-- （已核销/有差异）”整体固化为 JSON：重复提交同一身份只返回既有单据，不重新核对、不改变
-- 结论；即使后来订单/流水发生变动，重读仍返回生成时那份结论。
--
-- 对账单的写入是对账过程唯一发生的持久化：核对本身在一个只读事务、同一份已提交快照内
-- 纯读取完成，不改变任何订单、收款、回退、退款与流水数据。
CREATE TABLE IF NOT EXISTS reconciliations(
  tenant TEXT NOT NULL,
  reconcile_id TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(tenant, reconcile_id)
);
