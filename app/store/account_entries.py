from app.store.db import connect

# 账务动作类型：动作成功落库时在同一事务内追加一条对应流水；被拒绝或失败的动作不写流水，
# 故这里只有“真正生效的动作”，不存在拒绝/失败对应的类型。
PAYMENT_RECEIVED = "payment_received"      # 收款登记成功，对外已收金额增加
PAYMENT_ROLLED_BACK = "payment_rolled_back"  # 收款回退生效，对外已收金额扣减
REFUND_REGISTERED = "refund_registered"    # 退款单登记（待审核，不改金额，变化额记 0）
REFUND_APPROVED = "refund_approved"        # 退款审核同意并生效，对外已收金额扣减
REFUND_REJECTED = "refund_rejected"        # 退款审核拒绝（不改金额，变化额记 0）
REFUND_REVERSED = "refund_reversed"        # 已生效退款冲正，对外已收金额恢复

# 动作回指的单据类型：收款无独立标识，按所属订单记入；回退/退款分别回指回退单、退款单。
REF_PAYMENT = "payment"
REF_ROLLBACK = "rollback"
REF_REFUND = "refund"


class InvalidCursor(Exception):
    """顺序位置（seq）不指向本租户该订单的流水，对外按参数错误处理。"""


def append(
    conn,
    *,
    tenant: str,
    order_id: str,
    action_type: str,
    ref_type: str,
    ref_id: str,
    change_cents: int,
    balance_cents: int,
) -> None:
    """在调用方已开启的写事务内追加一条不可变流水。

    必须与单据写入、状态迁移、金额调整使用同一个连接/事务：调用方提交时二者一起生效，
    回滚时一起丢弃，绝不出现动作生效而流水缺失、或流水存在而动作未生效。
    """
    conn.execute(
        "INSERT INTO order_account_entries(tenant, order_id, action_type, ref_type, ref_id, "
        "change_cents, balance_cents) VALUES(?,?,?,?,?,?,?)",
        (tenant, order_id, action_type, ref_type, ref_id, change_cents, balance_cents),
    )


def _to_dict(row) -> dict:
    return {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "seq": row["seq"],
        "action_type": row["action_type"],
        "ref_type": row["ref_type"],
        "ref_id": row["ref_id"],
        "change_cents": row["change_cents"],
        "balance_cents": row["balance_cents"],
        "created_at": row["created_at"],
    }


def list_for_order(
    tenant: str, order_id: str, *, cursor: int | None = None, limit: int = 50
) -> tuple[list[dict], int | None] | None:
    """按订单读取账务流水并以 seq 做稳定游标分页。

    返回 (本页流水(按 seq 升序), 下一页游标)；订单不属于本租户或不存在时返回 None，
    由上层统一按 404 处理，不泄漏订单是否存在。整个读取在一个只读事务、同一份已提交
    快照内完成：并发动作要么整体可见（含其流水）、要么整体不可见，绝不读到半条记录。
    """
    conn = connect()
    try:
        conn.execute("BEGIN")
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("COMMIT")
            return None
        if cursor is not None:
            # 顺序位置必须指向本租户该订单的一条真实流水，否则参数错误；
            # 属于其他租户/其他订单的 seq 同样校验不过，不泄漏对象是否存在。
            anchor = conn.execute(
                "SELECT 1 FROM order_account_entries WHERE tenant=? AND order_id=? AND seq=?",
                (tenant, order_id, cursor),
            ).fetchone()
            if anchor is None:
                conn.execute("COMMIT")
                raise InvalidCursor("cursor does not point to an account entry of this order")

        rows = conn.execute(
            "SELECT tenant, order_id, seq, action_type, ref_type, ref_id, change_cents, "
            "balance_cents, created_at FROM order_account_entries "
            "WHERE tenant=? AND order_id=? AND (? IS NULL OR seq > ?) "
            "ORDER BY seq ASC LIMIT ?",
            (tenant, order_id, cursor, cursor, limit + 1),
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
