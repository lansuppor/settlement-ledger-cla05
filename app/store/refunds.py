import sqlite3

from app.store.db import connect

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
REVERSED = "reversed"

# 已同意是唯一允许冲正的状态；待审核、已拒绝、已冲正均不可冲正。
REVERSIBLE_STATUSES = (APPROVED,)


class RefundConflict(Exception):
    """业务冲突（409）：金额守恒或状态机被违反。"""


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "refund_id": row["refund_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "reason": row["reason"],
        "status": row["status"],
    }


_REFUND_COLUMNS = "tenant, refund_id, order_id, amount_cents, reason, status"


def get(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_REFUND_COLUMNS} FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_dict(row)


def register(
    tenant: str, refund_id: str, order_id: str, amount_cents: int, reason: str
) -> tuple[dict | None, bool]:
    """登记退款单（业务身份幂等）。

    订单不存在或非本租户返回 (None, False)；占用超额抛 RefundConflict。
    成功返回 (退款单, 是否新建)；重复登记返回 (既有单当前状态, False)，不看请求指纹。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            f"SELECT {_REFUND_COLUMNS} FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            return _row_to_dict(existing), False

        order = conn.execute(
            "SELECT paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None, False

        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS occupied FROM refunds "
            "WHERE tenant=? AND order_id=? AND status=?",
            (tenant, order_id, PENDING),
        ).fetchone()["occupied"]
        # 待审核即预留金额：全部待审核退款之和不得超过当前已收。
        # 已同意退款已从 paid 扣减，不再占用当前已收。
        if order["paid_cents"] - occupied < amount_cents:
            conn.execute("ROLLBACK")
            raise RefundConflict("refund amount exceeds paid amount")

        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, reason, status) "
            "VALUES(?,?,?,?,?,?)",
            (tenant, refund_id, order_id, amount_cents, reason, PENDING),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, refund_id), True


def review(tenant: str, refund_id: str, approve: bool) -> dict | None:
    """审核退款单。终态单重复审核返回原结果、不改数据；同意会突破已收金额则冲突。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT r.{', r.'.join(_REFUND_COLUMNS.split(', '))} "
            "FROM refunds r WHERE r.tenant=? AND r.refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None

        if row["status"] != PENDING:
            # 已冲正的单据不得再次审核；其余终态（同意/拒绝）重复审核返回原结果。
            if row["status"] == REVERSED:
                conn.execute("ROLLBACK")
                raise RefundConflict("refund already reversed")
            conn.execute("ROLLBACK")
            return _row_to_dict(row)

        if not approve:
            conn.execute(
                "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=?",
                (REJECTED, tenant, refund_id),
            )
            conn.execute("COMMIT")
            return get(tenant, refund_id)

        other_pending = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS occupied FROM refunds "
            "WHERE tenant=? AND order_id=? AND status=? AND refund_id<>?",
            (tenant, row["order_id"], PENDING, refund_id),
        ).fetchone()["occupied"]
        # 守卫更新：已生效退款已体现在 paid 中，扣减后只需仍能覆盖其余待审核预留；
        # 订单状态按剩余已收回算。
        cur = conn.execute(
            "UPDATE orders SET paid_cents = paid_cents - ?, "
            "status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=? AND paid_cents - ? >= ?",
            (
                row["amount_cents"],
                row["amount_cents"],
                tenant,
                row["order_id"],
                row["amount_cents"],
                other_pending,
            ),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise RefundConflict("approved refund exceeds paid amount")
        conn.execute(
            "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=?",
            (APPROVED, tenant, refund_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, refund_id)


def reverse(tenant: str, refund_id: str) -> dict | None:
    """冲正已生效退款：全额回补订单已收，退款单置为已冲正。非已同意状态冲突。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_REFUND_COLUMNS} FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["status"] not in REVERSIBLE_STATUSES:
            conn.execute("ROLLBACK")
            raise RefundConflict("refund is not effective")

        # 守卫更新：回补后已收不得超过订单金额，保证 0 <= paid <= amount 闭合。
        cur = conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=? AND paid_cents + ? <= amount_cents",
            (
                row["amount_cents"],
                row["amount_cents"],
                tenant,
                row["order_id"],
                row["amount_cents"],
            ),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise RefundConflict("reversal would exceed order amount")
        conn.execute(
            "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=?",
            (REVERSED, tenant, refund_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, refund_id)
