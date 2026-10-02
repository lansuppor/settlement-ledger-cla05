from app.store.db import connect


class RollbackError(Exception):
    pass


class ConflictError(RollbackError):
    pass


class OrderNotFound(RollbackError):
    pass


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _to_dict(row) -> dict:
    return {
        "tenant": row["tenant"],
        "rollback_id": row["rollback_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
    }


def get(tenant: str, rollback_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, rollback_id, order_id, amount_cents "
            "FROM payment_rollbacks WHERE tenant=? AND rollback_id=?",
            (tenant, rollback_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _to_dict(row)


def rollback(tenant: str, rollback_id: str, order_id: str, amount_cents: int) -> tuple[dict, bool]:
    """登记收款回退并在同一事务内调减订单累计收款。

    返回(回退单, 是否首次生效)；业务身份（租户 + rollback_id）重复时
    不新建记录、不重复退回金额，返回既有回退结果，与请求指纹无关。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, rollback_id, order_id, amount_cents "
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
            # 订单不存在或不属于本租户统一按参数错误处理，不泄漏订单是否存在。
            raise OrderNotFound("order not found")

        # 与退款登记口径一致：待审核与已生效未冲正退款合并计入退款占用额度。
        reserved = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status IN ('pending','approved') "
            "THEN amount_cents ELSE 0 END),0) AS reserved "
            "FROM refunds WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["reserved"]

        gross_paid = order["paid_cents"]

        # 当前对外已收（净已收）= 累计收款 − 已生效未冲正退款。
        approved_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM refunds "
            "WHERE tenant=? AND order_id=? AND status='approved'",
            (tenant, order_id),
        ).fetchone()["total"]
        net_paid = gross_paid - approved_total

        # 回退后已收不得为负：本笔超过当前已收金额属于参数错误。
        if amount_cents > net_paid:
            raise ValueError("rollback amount exceeds paid amount")

        # 本笔回退直接调减累计收款（已落库的历史回退已反映在 paid_cents 中）。
        new_gross_paid = gross_paid - amount_cents
        # 回退后退款占用额度（待审核 + 已生效未冲正）不得超过回退后的已收金额，
        # 即本笔不得超出「累计收款 − 退款占用」的剩余可回退额度。
        if reserved > new_gross_paid:
            raise ConflictError("rollback would make refund reservation exceed paid amount")

        conn.execute(
            "INSERT INTO payment_rollbacks(tenant, rollback_id, order_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, rollback_id, order_id, amount_cents),
        )
        new_status = "settled" if new_gross_paid - approved_total >= order["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_gross_paid, new_status, tenant, order_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    return get(tenant, rollback_id), True
