from app.store.db import connect
from app.store.refunds import net_approved


def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    # paid_cents 为累计收款毛额；对外已收金额需扣除已生效（批准且未冲正）的退款。
    gross_paid = row["paid_cents"]
    net_paid = gross_paid - net_approved(tenant, order_id)
    return {**dict(row), "paid_cents": net_paid, "outstanding_cents": row["amount_cents"] - net_paid}

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        refunded = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM refunds "
            "WHERE tenant=? AND order_id=? AND status='approved'",
            (tenant, order_id),
        ).fetchone()["total"]
        net_paid = row["paid_cents"] - refunded
        if amount_cents <= 0 or net_paid + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        new_gross = row["paid_cents"] + amount_cents
        new_status = "settled" if new_gross - refunded >= row["amount_cents"] else "accepted"
        conn.execute(
            "UPDATE orders SET paid_cents=?, status=? WHERE tenant=? AND order_id=?",
            (new_gross, new_status, tenant, order_id),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get(tenant, order_id)
