from app.store.db import connect

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
REVERSED = "reversed"


class RefundError(Exception):
    pass


class ConflictError(RefundError):
    pass


class OrderNotFound(RefundError):
    pass


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _to_dict(row) -> dict:
    return {
        "tenant": row["tenant"],
        "refund_id": row["refund_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "reason": row["reason"],
        "status": row["status"],
    }


def get(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, refund_id, order_id, amount_cents, reason, status "
            "FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _to_dict(row)


def register(tenant: str, refund_id: str, order_id: str, amount_cents: int, reason: str) -> tuple[dict, bool]:
    """登记退款单。返回(退款单, 是否新建)；业务身份重复时返回既有单据及当前状态。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, refund_id, order_id, amount_cents, reason, status "
            "FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return _to_dict(existing), False

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            raise OrderNotFound("order not found")

        # 待审核 + 已批准未冲正的退款合计视为已占用额度，保证任意逐笔批准顺序下都不会超出已收。
        reserved = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status IN ('pending','approved') "
            "THEN amount_cents ELSE 0 END),0) AS reserved "
            "FROM refunds WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["reserved"]
        if reserved + amount_cents > order["paid_cents"]:
            raise ConflictError("refund amount exceeds paid amount")

        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, reason, status) "
            "VALUES(?,?,?,?,?,'pending')",
            (tenant, refund_id, order_id, amount_cents, reason),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    return get(tenant, refund_id), True


def review(tenant: str, refund_id: str, approve: bool) -> dict | None:
    """审核退款单：同意则对订单生效，拒绝则关闭。审核结果不可覆盖，重复审核返回原结果。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT tenant, refund_id, order_id, amount_cents, reason, status "
            "FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            _rollback(conn)
            return None
        if row["status"] in (APPROVED, REJECTED):
            conn.execute("COMMIT")
            return _to_dict(row)
        if row["status"] == REVERSED:
            raise ConflictError("refund already reversed")

        if approve:
            order = conn.execute(
                "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
                (tenant, row["order_id"]),
            ).fetchone()
            net_approved_total = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS total FROM refunds "
                "WHERE tenant=? AND order_id=? AND status='approved'",
                (tenant, row["order_id"]),
            ).fetchone()["total"]
            if order is None or net_approved_total + row["amount_cents"] > order["paid_cents"]:
                raise ConflictError("refund exceeds paid amount")
            # 退款生效后净已收严格小于订单金额，订单回到有待收余额的状态。
            conn.execute(
                "UPDATE orders SET status='accepted' WHERE tenant=? AND order_id=?",
                (tenant, row["order_id"]),
            )

        new_status = APPROVED if approve else REJECTED
        conn.execute(
            "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=?",
            (new_status, tenant, refund_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    return get(tenant, refund_id)


def reverse(tenant: str, refund_id: str) -> dict | None:
    """冲正已生效退款单：金额全额反向退回订单，单据置为已冲正，此后不可再审核。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT order_id, amount_cents, status FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            _rollback(conn)
            return None
        if row["status"] == REVERSED:
            raise ConflictError("refund already reversed")
        if row["status"] != APPROVED:
            raise ConflictError("refund not effective")

        # 防御性守恒检查：冲正会把该笔金额加回净已收，若已通过收款补回该额度，
        # 冲正后净已收将超过订单金额，此时拒绝冲正，保持账务闭合。
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, row["order_id"]),
        ).fetchone()
        approved_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM refunds "
            "WHERE tenant=? AND order_id=? AND status='approved'",
            (tenant, row["order_id"]),
        ).fetchone()["total"]
        if order is not None and order["paid_cents"] - approved_total + row["amount_cents"] > order["amount_cents"]:
            raise ConflictError("reverse would exceed order amount")

        conn.execute(
            "UPDATE refunds SET status='reversed' WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        )
        new_net_paid = order["paid_cents"] - approved_total + row["amount_cents"]
        new_status = "settled" if new_net_paid >= order["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET status=? WHERE tenant=? AND order_id=?",
            (new_status, tenant, row["order_id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    return get(tenant, refund_id)


def net_approved(tenant: str, order_id: str) -> int:
    """订单已批准且未冲正的退款总额，即已从已收金额中实际扣减的部分。"""
    conn = connect()
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM refunds "
            "WHERE tenant=? AND order_id=? AND status='approved'",
            (tenant, order_id),
        ).fetchone()["total"]
    finally:
        conn.close()
