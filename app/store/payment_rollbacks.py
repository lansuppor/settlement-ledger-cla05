from app.store.db import connect

COMPLETED = "completed"


class RollbackError(Exception):
    pass


class ConflictError(RollbackError):
    pass


class OrderNotFound(RollbackError):
    pass


class InvalidAmountError(RollbackError):
    """回退金额非法（如超过订单当前已收金额），对外按参数错误处理。"""


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _to_dict(row) -> dict:
    return {
        "tenant": row["tenant"],
        "rollback_id": row["rollback_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "status": row["status"],
    }


def get(tenant: str, rollback_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, rollback_id, order_id, amount_cents, status "
            "FROM payment_rollbacks WHERE tenant=? AND rollback_id=?",
            (tenant, rollback_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _to_dict(row)


def register(tenant: str, rollback_id: str, order_id: str, amount_cents: int) -> tuple[dict, bool]:
    """登记收款回退，返回(回退单, 是否新建)。

    业务身份为（租户, rollback_id）：重复请求不新建记录、不重复退回金额，直接返回既有回退结果，
    与本次请求携带的订单/金额指纹无关。新建时回退记录与订单已收金额调整在同一事务内提交。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, rollback_id, order_id, amount_cents, status "
            "FROM payment_rollbacks WHERE tenant=? AND rollback_id=?",
            (tenant, rollback_id),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return _to_dict(existing), False

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 订单不存在或不属于本租户：按参数错误处理，不泄漏订单是否存在。
            raise OrderNotFound("order not found")

        # 已批准未冲正退款：已从累计收款毛额中实际扣减的部分；对外净已收 = 毛额 − 已生效退款。
        approved_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM refunds "
            "WHERE tenant=? AND order_id=? AND status='approved'",
            (tenant, order_id),
        ).fetchone()["total"]
        # 退款占用额度：待审核与已生效未冲正退款合并计入（与退款登记口径完全一致）。
        reserved = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status IN ('pending','approved') "
            "THEN amount_cents ELSE 0 END),0) AS reserved "
            "FROM refunds WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["reserved"]

        net_paid = order["paid_cents"] - approved_total
        if amount_cents > net_paid:
            # 回退金额超过订单当前已收（净）金额：参数错误。
            raise InvalidAmountError("rollback amount exceeds paid amount")
        new_gross = order["paid_cents"] - amount_cents
        if reserved > new_gross:
            # 退款登记以累计收款毛额为占用上限；回退后毛额必须仍覆盖待审核与已生效退款占用，
            # 否则待审核退款将失去收款支撑。等价于回退金额超过“净已收 − 待审核占用”的剩余可回退额度。
            raise ConflictError("rollback exceeds remaining rollback quota")

        # 回退即反向扣减累计收款毛额；订单对外净已收随之重算，未收 = 订单金额 − 净已收。
        new_net_paid = new_gross - approved_total
        new_status = "settled" if new_net_paid >= order["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_gross, new_status, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO payment_rollbacks(tenant, rollback_id, order_id, amount_cents, status) "
            "VALUES(?,?,?,?,'completed')",
            (tenant, rollback_id, order_id, amount_cents),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    return get(tenant, rollback_id), True
