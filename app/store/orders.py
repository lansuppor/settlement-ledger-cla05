from app.store import order_ledger
from app.store.db import connect
from app.store.order_ledger import PAYMENT, REF_ORDER
from app.store.refunds import net_approved

# 检索支持的订单状态口径，与订单对象 status 字段取值一致。
SEARCHABLE_STATUSES = ("accepted", "settled")


class InvalidCursor(Exception):
    """游标指向的订单在本租户下不存在，对外按参数错误处理。"""


def _order_dict(row) -> dict:
    """把含净已收扣减信息的行组装为对外订单对象，金额口径与 get 完全一致。"""
    gross_paid = row["paid_cents"]
    approved_total = row["approved_total"]
    net_paid = gross_paid - approved_total
    return {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": net_paid,
        "outstanding_cents": row["amount_cents"] - net_paid,
        "currency": row["currency"],
        "status": row["status"],
    }


# 净已收 = 累计收款毛额 − 已批准未冲正退款；未收 = 订单金额 − 净已收。
_NET_PAID_EXPR = "(o.paid_cents - COALESCE(r.approved_total,0))"
_OUTSTANDING_EXPR = f"(o.amount_cents - {_NET_PAID_EXPR})"


def search(
    tenant: str,
    *,
    status: str | None = None,
    amount_min: int | None = None,
    amount_max: int | None = None,
    outstanding_min: int | None = None,
    outstanding_max: int | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[dict], int, str | None]:
    """按条件检索订单并做稳定游标分页。

    返回 (本页订单, 条件下总数, 下一页游标)；结果按 order_id 升序。
    一次只读连接、一个只读事务内完成总数统计与本页读取：
    二者对同一份已提交快照求值，互不重不漏，且不会读到写事务的中间状态。
    """
    conn = connect()
    try:
        # 只读快照事务：检索期间提交的收款/回退/退款审核/冲正对本次查询不可见，
        # 保证单次连续翻页的这一次读取内部不出现半张单据或口径撕裂。
        conn.execute("BEGIN")
        if cursor is not None:
            # 游标必须指向本租户真实存在的订单（与筛选条件无关），否则参数错误。
            anchor = conn.execute(
                "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
                (tenant, cursor),
            ).fetchone()
            if anchor is None:
                conn.execute("COMMIT")
                raise InvalidCursor("cursor does not point to an order of this tenant")

        where = ["o.tenant = ?"]
        params: list = [tenant]
        if status is not None:
            where.append("o.status = ?")
            params.append(status)
        if amount_min is not None:
            where.append("o.amount_cents >= ?")
            params.append(amount_min)
        if amount_max is not None:
            where.append("o.amount_cents <= ?")
            params.append(amount_max)
        if outstanding_min is not None:
            where.append(f"{_OUTSTANDING_EXPR} >= ?")
            params.append(outstanding_min)
        if outstanding_max is not None:
            where.append(f"{_OUTSTANDING_EXPR} <= ?")
            params.append(outstanding_max)
        where_sql = " AND ".join(where)

        base_select = (
            "FROM orders o LEFT JOIN ("
            "SELECT tenant, order_id, COALESCE(SUM(amount_cents),0) AS approved_total "
            "FROM refunds WHERE status='approved' GROUP BY tenant, order_id"
            ") r ON r.tenant=o.tenant AND r.order_id=o.order_id "
            f"WHERE {where_sql}"
        )

        # 总数统计只含筛选条件、不含游标：同一组条件下总数恒定，与翻到第几页、页大小无关。
        total = conn.execute(
            f"SELECT COUNT(*) AS n {base_select}", params
        ).fetchone()["n"]

        # 本页读取在筛选条件之上叠加游标条件（严格位于锚点之后），按 order_id 升序稳定前进。
        page_params = [*params]
        page_where = where_sql
        if cursor is not None:
            page_where = f"{where_sql} AND o.order_id > ?"
            page_params.append(cursor)
        page_select = (
            "FROM orders o LEFT JOIN ("
            "SELECT tenant, order_id, COALESCE(SUM(amount_cents),0) AS approved_total "
            "FROM refunds WHERE status='approved' GROUP BY tenant, order_id"
            ") r ON r.tenant=o.tenant AND r.order_id=o.order_id "
            f"WHERE {page_where}"
        )
        rows = conn.execute(
            f"SELECT o.tenant, o.order_id, o.amount_cents, o.paid_cents, o.currency, o.status, "
            f"COALESCE(r.approved_total,0) AS approved_total {page_select} "
            "ORDER BY o.order_id ASC LIMIT ?",
            [*page_params, limit + 1],
            # 多取 1 条用于判断是否还有下一页，避免再次回表统计。
        ).fetchall()
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()

    has_next = len(rows) > limit
    page = [_order_dict(row) for row in rows[:limit]]
    next_cursor = page[-1]["order_id"] if has_next and page else None
    return page, total, next_cursor


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
        # 收款不新建业务标识，按所属订单记入；金额调整与流水在同一事务内提交，同生同灭。
        order_ledger.append(
            conn,
            tenant=tenant,
            order_id=order_id,
            action_type=PAYMENT,
            ref_kind=REF_ORDER,
            ref_id=order_id,
            delta_cents=amount_cents,
            balance_after_cents=new_gross - refunded,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get(tenant, order_id)
