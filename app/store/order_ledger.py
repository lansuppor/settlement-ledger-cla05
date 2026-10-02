from app.store.db import connect

# 账务动作类型：与各单据状态迁移一一对应，动作成功落库时才追加流水。
PAYMENT = "payment"
PAYMENT_ROLLBACK = "payment_rollback"
REFUND_REGISTERED = "refund_registered"
REFUND_APPROVED = "refund_approved"
REFUND_REJECTED = "refund_rejected"
REFUND_REVERSED = "refund_reversed"

# 动作业务标识的归属口径：
# 收款不新建标识，按所属订单记入；收款回退用回退标识；退款的登记/审核/冲正用退款标识。
REF_ORDER = "order"
REF_ROLLBACK = "rollback"
REF_REFUND = "refund"


class InvalidCursor(Exception):
    """顺序位置不指向本租户该订单的流水，对外按参数错误处理。"""


def append(
    conn,
    *,
    tenant: str,
    order_id: str,
    action_type: str,
    ref_kind: str,
    ref_id: str,
    delta_cents: int,
    balance_after_cents: int,
) -> int:
    """在调用方已开启的写事务内追加一条不可变流水，返回其在该订单流水内的顺序号。

    必须由账务动作本身的事务调用：单据写入、状态迁移、金额调整与本流水要么一起提交，
    要么一起回滚，不允许动作成功而流水缺失、或流水存在而动作未生效。
    写事务以 BEGIN IMMEDIATE 串行执行，MAX(seq)+1 在并发下不重号。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS max_seq FROM order_ledger WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    seq = row["max_seq"] + 1
    conn.execute(
        "INSERT INTO order_ledger("
        "tenant, order_id, seq, action_type, ref_kind, ref_id, delta_cents, balance_after_cents"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (tenant, order_id, seq, action_type, ref_kind, ref_id, delta_cents, balance_after_cents),
    )
    return seq


def _to_dict(row) -> dict:
    return {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "seq": row["seq"],
        "action_type": row["action_type"],
        "ref_kind": row["ref_kind"],
        "ref_id": row["ref_id"],
        "delta_cents": row["delta_cents"],
        "balance_after_cents": row["balance_after_cents"],
    }


def list_for_order(
    tenant: str, order_id: str, *, cursor: int | None = None, limit: int = 50
) -> tuple[list[dict], int | None]:
    """按发生顺序从早到晚读取一张订单的账务流水并做顺序位置分页。

    返回(本页流水, 下一页游标 seq)。整页读取在同一个只读事务、同一份已提交快照上完成：
    并发提交的动作要么整笔可见（单据、金额与流水一起），要么整笔不可见，绝不读到半条记录。
    游标必须指向本租户该订单的一条真实流水，否则抛 InvalidCursor（对外 400）；
    订单是否属于本租户由入口先行判定，跨租户一律按订单不存在（404）处理。
    """
    conn = connect()
    try:
        conn.execute("BEGIN")
        if cursor is not None:
            anchor = conn.execute(
                "SELECT 1 FROM order_ledger WHERE tenant=? AND order_id=? AND seq=?",
                (tenant, order_id, cursor),
            ).fetchone()
            if anchor is None:
                conn.execute("COMMIT")
                raise InvalidCursor("cursor does not point to a ledger entry of this tenant and order")

        after_seq = cursor or 0
        rows = conn.execute(
            "SELECT tenant, order_id, seq, action_type, ref_kind, ref_id, delta_cents, balance_after_cents "
            "FROM order_ledger WHERE tenant=? AND order_id=? AND seq > ? ORDER BY seq ASC LIMIT ?",
            (tenant, order_id, after_seq, limit + 1),
            # 多取 1 条用于判断是否还有下一页。
        ).fetchall()
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()

    has_next = len(rows) > limit
    page = [_to_dict(row) for row in rows[:limit]]
    next_cursor = page[-1]["seq"] if has_next and page else None
    return page, next_cursor
