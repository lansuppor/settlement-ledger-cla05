import sqlite3
from datetime import UTC, datetime

from app.store.db import connect

# 占用可退余额的状态：待处理（受理即占用）与已完成（实际扣减中）。
# 已撤销在撤销时释放；已冲正在冲正把款项加回已收时释放。
HOLDING_STATUSES = ("pending", "completed")
TERMINAL_STATUSES = ("completed", "cancelled", "reversed")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _snapshot(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> dict | None:
    row = conn.execute(
        "SELECT tenant, order_id, refund_id, amount_cents, status, effective_deduction_cents "
        "FROM refunds WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()
    return dict(row) if row is not None else None


def get(tenant: str, order_id: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, refund_id, amount_cents, status, effective_deduction_cents "
            "FROM refunds WHERE tenant=? AND order_id=? AND refund_id=?",
            (tenant, order_id, refund_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def list_by_order(tenant: str, order_id: str) -> list[dict] | None:
    """返回订单的全部退款单；订单不存在（含跨租户）时返回 None。"""
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            "SELECT tenant, order_id, refund_id, amount_cents, status, effective_deduction_cents "
            "FROM refunds WHERE tenant=? AND order_id=? ORDER BY rowid",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _idempotent(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    refund_id: str,
    action: str,
    request_id: str,
) -> dict | None:
    row = conn.execute(
        "SELECT response_status, response_amount, response_deduction "
        "FROM refund_requests WHERE tenant=? AND order_id=? AND refund_id=? AND action=? AND request_id=?",
        (tenant, order_id, refund_id, action, request_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "tenant": tenant,
        "order_id": order_id,
        "refund_id": refund_id,
        "amount_cents": row["response_amount"],
        "status": row["response_status"],
        "effective_deduction_cents": row["response_deduction"],
    }


def _record(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    refund_id: str,
    action: str,
    request_id: str,
    refund: dict,
) -> None:
    conn.execute(
        "INSERT INTO refund_requests(tenant, order_id, refund_id, action, request_id, "
        "response_status, response_amount, response_deduction, executed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            tenant,
            order_id,
            refund_id,
            action,
            request_id,
            refund["status"],
            refund["amount_cents"],
            refund["effective_deduction_cents"],
            _now(),
        ),
    )


def accept(
    tenant: str, order_id: str, refund_id: str, amount_cents: int, request_id: str
) -> dict | None:
    """受理退款单。订单不存在（含跨租户）返回 None；其余业务冲突抛 ValueError。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = _idempotent(conn, tenant, order_id, refund_id, "accept", request_id)
        if replay is not None:
            if replay["amount_cents"] != amount_cents:
                conn.execute("ROLLBACK")
                raise ValueError("request_id already used with a different payload")
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None

        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise ValueError("amount must be a positive integer")

        existing = conn.execute(
            "SELECT 1 FROM refunds WHERE tenant=? AND order_id=? AND refund_id=?",
            (tenant, order_id, refund_id),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            raise ValueError("refund already accepted")

        held = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS held FROM refunds "
            "WHERE tenant=? AND order_id=? AND status IN (?, ?)",
            (tenant, order_id, *HOLDING_STATUSES),
        ).fetchone()["held"]
        if held + amount_cents > order["paid_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("refund exceeds refundable balance")

        now = _now()
        conn.execute(
            "INSERT INTO refunds(tenant, order_id, refund_id, amount_cents, status, "
            "effective_deduction_cents, created_at, updated_at) VALUES(?,?,?,?,'pending',0,?,?)",
            (tenant, order_id, refund_id, amount_cents, now, now),
        )
        refund = _snapshot(conn, tenant, order_id, refund_id)
        _record(conn, tenant, order_id, refund_id, "accept", request_id, refund)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return refund


def advance(
    tenant: str, order_id: str, refund_id: str, action: str, request_id: str
) -> dict | None:
    """推进退款单：action 为 'complete' 或 'cancel'。不存在返回 None，冲突抛 ValueError。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = _idempotent(conn, tenant, order_id, refund_id, action, request_id)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        refund = _snapshot(conn, tenant, order_id, refund_id)
        if refund is None:
            conn.execute("ROLLBACK")
            return None

        if refund["status"] != "pending":
            conn.execute("ROLLBACK")
            raise ValueError(f"refund is {refund['status']} and cannot be {action}d")

        if action == "complete":
            # 原子扣减订单已收并同步未收；已收不足（理论上被受理占用约束拦住）时整单回滚。
            cur = conn.execute(
                "UPDATE orders SET paid_cents = paid_cents - ?, "
                "status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
                "WHERE tenant=? AND order_id=? AND paid_cents >= ?",
                (
                    refund["amount_cents"],
                    refund["amount_cents"],
                    tenant,
                    order_id,
                    refund["amount_cents"],
                ),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                raise ValueError("refund amount exceeds paid amount")
            conn.execute(
                "UPDATE refunds SET status='completed', effective_deduction_cents=?, updated_at=? "
                "WHERE tenant=? AND order_id=? AND refund_id=?",
                (refund["amount_cents"], _now(), tenant, order_id, refund_id),
            )
        else:  # cancel：释放受理时的占用，不动订单金额
            conn.execute(
                "UPDATE refunds SET status='cancelled', updated_at=? "
                "WHERE tenant=? AND order_id=? AND refund_id=?",
                (_now(), tenant, order_id, refund_id),
            )

        refund = _snapshot(conn, tenant, order_id, refund_id)
        _record(conn, tenant, order_id, refund_id, action, request_id, refund)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return refund


def reverse(tenant: str, order_id: str, refund_id: str, request_id: str) -> dict | None:
    """冲正已完成的退款单：款项加回订单已收，退款单进入已冲正终态。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replay = _idempotent(conn, tenant, order_id, refund_id, "reverse", request_id)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        refund = _snapshot(conn, tenant, order_id, refund_id)
        if refund is None:
            conn.execute("ROLLBACK")
            return None

        if refund["status"] != "completed":
            conn.execute("ROLLBACK")
            raise ValueError(f"only completed refund can be reversed, current status: {refund['status']}")

        # 订单与退款单在同一事务内更新：原子生效或都不生效。
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE status END "
            "WHERE tenant=? AND order_id=?",
            (refund["amount_cents"], refund["amount_cents"], tenant, order_id),
        )
        conn.execute(
            "UPDATE refunds SET status='reversed', effective_deduction_cents=0, updated_at=? "
            "WHERE tenant=? AND order_id=? AND refund_id=?",
            (_now(), tenant, order_id, refund_id),
        )
        refund = _snapshot(conn, tenant, order_id, refund_id)
        _record(conn, tenant, order_id, refund_id, "reverse", request_id, refund)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return refund
