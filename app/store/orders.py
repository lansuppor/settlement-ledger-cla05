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

def search(
    tenant: str,
    *,
    status: str | None = None,
    amount_min: int | None = None,
    amount_max: int | None = None,
    outstanding_min: int | None = None,
    outstanding_max: int | None = None,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[dict], int, bool]:
    """按条件检索订单：状态与金额区间取交集，按订单标识升序游标分页。

    返回(本页订单, 该条件下的订单总数, 是否还有下一页)。金额口径与 get() 一致：
    已收为净额（累计收款毛额扣减已批准未冲正退款），未收 = 订单金额 − 净已收。
    总数与本页在同一读事务快照内取数，并发写入下二者互相一致、不出现半张单据。
    """
    inner = (
        "SELECT o.tenant AS tenant, o.order_id AS order_id, o.amount_cents AS amount_cents, "
        "o.currency AS currency, o.status AS status, "
        "o.paid_cents - COALESCE(r.approved, 0) AS net_paid, "
        "o.amount_cents - o.paid_cents + COALESCE(r.approved, 0) AS outstanding "
        "FROM orders o LEFT JOIN ("
        "SELECT tenant, order_id, SUM(amount_cents) AS approved FROM refunds "
        "WHERE status='approved' GROUP BY tenant, order_id"
        ") r ON r.tenant = o.tenant AND r.order_id = o.order_id"
    )
    conditions = ["tenant = ?"]
    params: list = [tenant]
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if amount_min is not None:
        conditions.append("amount_cents >= ?")
        params.append(amount_min)
    if amount_max is not None:
        conditions.append("amount_cents <= ?")
        params.append(amount_max)
    if outstanding_min is not None:
        conditions.append("outstanding >= ?")
        params.append(outstanding_min)
    if outstanding_max is not None:
        conditions.append("outstanding <= ?")
        params.append(outstanding_max)
    where = " AND ".join(conditions)

    conn = connect()
    try:
        conn.execute("BEGIN")
        # 总数只与检索条件有关，与游标、页大小无关。
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({inner}) WHERE {where}", params
        ).fetchone()["n"]
        page_where = where
        page_params = params
        if cursor is not None:
            page_where += " AND order_id > ?"
            page_params = [*params, cursor]
        # 多取一条用于判断是否还有下一页。
        rows = conn.execute(
            f"SELECT * FROM ({inner}) WHERE {page_where} ORDER BY order_id ASC LIMIT ?",
            [*page_params, limit + 1],
        ).fetchall()
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    has_next = len(rows) > limit
    items = [
        {
            "tenant": row["tenant"],
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "paid_cents": row["net_paid"],
            "currency": row["currency"],
            "status": row["status"],
            "outstanding_cents": row["outstanding"],
        }
        for row in rows[:limit]
    ]
    return items, total, has_next

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
