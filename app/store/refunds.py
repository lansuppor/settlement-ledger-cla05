import json
import sqlite3

from app.store import workorders
from app.store.db import connect

# 退款单状态机：
#   pending --complete--> completed --reverse--> reversed
#   pending --cancel----> cancelled
# completed/cancelled/reversed 均为终态（reversed 后亦不可再推进）
PENDING = "pending"
COMPLETED = "completed"
CANCELLED = "cancelled"
REVERSED = "reversed"

OP_ACCEPT = "accept"
OP_COMPLETE = "complete"
OP_CANCEL = "cancel"
OP_REVERSE = "reverse"


class RefundConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_refund(row: sqlite3.Row) -> dict:
    return {
        "order_id": row["order_id"],
        "refund_id": row["refund_id"],
        "amount_cents": row["amount_cents"],
        "status": row["status"],
        "effective_deduction_cents": row["effective_deduction_cents"],
    }


def _load_idempotent(conn: sqlite3.Connection, tenant: str, request_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT op, order_id, refund_id, http_status, response_json FROM refund_requests"
        " WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    return row["op"], row["order_id"], row["refund_id"], row["http_status"], json.loads(row["response_json"])


def _save_idempotent(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    order_id: str,
    refund_id: str,
    http_status: int,
    response: dict,
) -> None:
    conn.execute(
        "INSERT INTO refund_requests(tenant, request_id, op, order_id, refund_id, http_status, response_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, order_id, refund_id, http_status, json.dumps(response, ensure_ascii=False)),
    )


def _replay_or_none(
    conn: sqlite3.Connection, tenant: str, request_id: str, op: str, order_id: str, refund_id: str
) -> tuple[int, dict] | None:
    """命中幂等记录则返回首次结果；同 request_id 指向不同操作/对象则冲突。"""
    recorded = _load_idempotent(conn, tenant, request_id)
    if recorded is None:
        return None
    saved_op, saved_order, saved_refund, http_status, response = recorded
    if (saved_op, saved_order, saved_refund) != (op, order_id, refund_id):
        raise RefundConflict("request_id was already used for a different operation")
    return http_status, response


def _get_refund_conn(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT order_id, refund_id, amount_cents, status, effective_deduction_cents FROM refunds"
        " WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()


def _order_view(conn: sqlite3.Connection, tenant: str, order_id: str) -> dict:
    row = conn.execute(
        "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    return {"paid_cents": row["paid_cents"], "outstanding_cents": row["amount_cents"] - row["paid_cents"]}


def accept(tenant: str, order_id: str, refund_id: str, amount_cents: int, request_id: str) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            order = conn.execute(
                "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
                (tenant, order_id),
            ).fetchone()
            if order is None:
                response = {"detail": "order not found"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            existing = _get_refund_conn(conn, tenant, order_id, refund_id)
            if existing is not None:
                response = {"detail": "refund already accepted"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 守恒不变量：待处理占用 + 已生效扣减（已体现在 paid_cents 的扣减中）<= 累计已收
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS pending_sum FROM refunds"
                " WHERE tenant=? AND order_id=? AND status='pending'",
                (tenant, order_id),
            ).fetchone()
            if amount_cents <= 0 or row["pending_sum"] + amount_cents > order["paid_cents"]:
                response = {"detail": "refund exceeds refundable balance"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            conn.execute(
                "INSERT INTO refunds(tenant, order_id, refund_id, amount_cents, status,"
                " effective_deduction_cents, created_at, updated_at)"
                " VALUES(?,?,?,?,'pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (tenant, order_id, refund_id, amount_cents),
            )
            refund = _row_to_refund(_get_refund_conn(conn, tenant, order_id, refund_id))
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, 201, refund)
            conn.execute("COMMIT")
            return 201, refund
        except RefundConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _advance(
    tenant: str, order_id: str, refund_id: str, request_id: str, op: str, target_status: str
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, op, order_id, refund_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            refund = _get_refund_conn(conn, tenant, order_id, refund_id)
            if refund is None:
                response = {"detail": "refund not found"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            # 被进行中工单标记的退款单不得完成/撤销
            if workorders.has_active(conn, tenant, order_id, refund_id):
                response = {"detail": "refund is marked by an in-progress workorder"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            if refund["status"] == target_status:
                response = {"detail": f"refund already {target_status}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if refund["status"] != PENDING:
                response = {"detail": f"cannot {op} refund in status {refund['status']}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            amount = refund["amount_cents"]
            if op == OP_COMPLETE:
                # 扣减订单已收；不变量保证 paid_cents >= amount
                conn.execute(
                    "UPDATE orders SET paid_cents = paid_cents - ?,"
                    " status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
                    " WHERE tenant=? AND order_id=? AND paid_cents >= ?",
                    (amount, amount, tenant, order_id, amount),
                )
                changed = conn.execute("SELECT changes()").fetchone()[0]
                if changed == 0:
                    raise RefundConflict("refund exceeds refundable balance")
                conn.execute(
                    "UPDATE refunds SET status='completed', effective_deduction_cents=?, updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=?",
                    (amount, tenant, order_id, refund_id),
                )
            else:  # cancel：释放占用，不动订单金额
                conn.execute(
                    "UPDATE refunds SET status='cancelled', updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=?",
                    (tenant, order_id, refund_id),
                )

            result = _row_to_refund(_get_refund_conn(conn, tenant, order_id, refund_id))
            result.update(_order_view(conn, tenant, order_id))
            _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except RefundConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def complete(tenant: str, order_id: str, refund_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, request_id, OP_COMPLETE, COMPLETED)


def cancel(tenant: str, order_id: str, refund_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, request_id, OP_CANCEL, CANCELLED)


def reverse(tenant: str, order_id: str, refund_id: str, request_id: str) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_REVERSE, order_id, refund_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            refund = _get_refund_conn(conn, tenant, order_id, refund_id)
            if refund is None:
                response = {"detail": "refund not found"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            # 被进行中工单标记的退款单不得冲正
            if workorders.has_active(conn, tenant, order_id, refund_id):
                response = {"detail": "refund is marked by an in-progress workorder"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            if refund["status"] == REVERSED:
                response = {"detail": "refund already reversed"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if refund["status"] != COMPLETED:
                response = {"detail": f"cannot reverse refund in status {refund['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            amount = refund["amount_cents"]
            # 两侧原子更新：加回订单已收，退款单进入已冲正终态，当前生效扣减清零
            conn.execute(
                "UPDATE orders SET paid_cents = paid_cents + ?,"
                " status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
                " WHERE tenant=? AND order_id=?",
                (amount, amount, tenant, order_id),
            )
            conn.execute(
                "UPDATE refunds SET status='reversed', effective_deduction_cents=0, updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND order_id=? AND refund_id=?",
                (tenant, order_id, refund_id),
            )
            result = _row_to_refund(_get_refund_conn(conn, tenant, order_id, refund_id))
            result.update(_order_view(conn, tenant, order_id))
            _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except RefundConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get(tenant: str, order_id: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = _get_refund_conn(conn, tenant, order_id, refund_id)
    finally:
        conn.close()
    return _row_to_refund(row) if row is not None else None


def list_for_order(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            "SELECT order_id, refund_id, amount_cents, status, effective_deduction_cents FROM refunds"
            " WHERE tenant=? AND order_id=? ORDER BY rowid",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_refund(row) for row in rows]
