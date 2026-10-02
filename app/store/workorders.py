import json
import sqlite3

from app.store.db import connect

# 工单状态机：
#   accepted --advance--> processing
#   processing --advance--> pending_review | resolved | cancelled
#   pending_review --advance--> processing | resolved | cancelled
#   accepted --advance--> cancelled
#   resolved --cancel---> cancelled（撤销关闭：把裁决扣减加回退款单）
# resolved/cancelled 均为终态，终态不可再推进
ACCEPTED = "accepted"
PROCESSING = "processing"
PENDING_REVIEW = "pending_review"
RESOLVED = "resolved"
CANCELLED = "cancelled"

# 进行中状态：期间退款单被施加「处理中」标记，不得完成/撤销/冲正
IN_PROGRESS = (ACCEPTED, PROCESSING, PENDING_REVIEW)

OP_ACCEPT = "accept"
OP_ADVANCE = "advance"
OP_CANCEL = "cancel"

_ADVANCE_TARGETS = {
    ACCEPTED: {PROCESSING, CANCELLED},
    PROCESSING: {PENDING_REVIEW, RESOLVED, CANCELLED},
    PENDING_REVIEW: {PROCESSING, RESOLVED, CANCELLED},
}


class WorkorderConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_workorder(row: sqlite3.Row) -> dict:
    return {
        "order_id": row["order_id"],
        "refund_id": row["refund_id"],
        "workorder_id": row["workorder_id"],
        "status": row["status"],
        "claim_amount_cents": row["claim_amount_cents"],
        "initiator": row["initiator"],
        "reason": row["reason"],
        "award_cents": row["award_cents"],
        "effective_deduction_cents": row["effective_deduction_cents"],
    }


def _load_idempotent(conn: sqlite3.Connection, tenant: str, request_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT op, order_id, refund_id, workorder_id, http_status, response_json FROM workorder_requests"
        " WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    return (
        row["op"], row["order_id"], row["refund_id"], row["workorder_id"],
        row["http_status"], json.loads(row["response_json"]),
    )


def _save_idempotent(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    order_id: str,
    refund_id: str,
    workorder_id: str,
    http_status: int,
    response: dict,
) -> None:
    conn.execute(
        "INSERT INTO workorder_requests(tenant, request_id, op, order_id, refund_id, workorder_id,"
        " http_status, response_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, order_id, refund_id, workorder_id, http_status,
         json.dumps(response, ensure_ascii=False)),
    )


def _replay_or_none(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    order_id: str,
    refund_id: str,
    workorder_id: str,
) -> tuple[int, dict] | None:
    """命中幂等记录则返回首次结果；同 request_id 指向不同操作/对象则冲突。"""
    recorded = _load_idempotent(conn, tenant, request_id)
    if recorded is None:
        return None
    saved_op, saved_order, saved_refund, saved_workorder, http_status, response = recorded
    if (saved_op, saved_order, saved_refund, saved_workorder) != (op, order_id, refund_id, workorder_id):
        raise WorkorderConflict("request_id was already used for a different operation")
    return http_status, response


def _get_workorder_conn(
    conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str, workorder_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT order_id, refund_id, workorder_id, status, claim_amount_cents, initiator, reason,"
        " award_cents, effective_deduction_cents FROM workorders"
        " WHERE tenant=? AND order_id=? AND refund_id=? AND workorder_id=?",
        (tenant, order_id, refund_id, workorder_id),
    ).fetchone()


def _get_refund_conn(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT order_id, refund_id, amount_cents, status, effective_deduction_cents FROM refunds"
        " WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()


def has_active(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> bool:
    """退款单是否被进行中工单施加「处理中」标记（调用方须持有写事务）。"""
    row = conn.execute(
        "SELECT 1 FROM workorders WHERE tenant=? AND order_id=? AND refund_id=?"
        " AND status IN ('accepted','processing','pending_review') LIMIT 1",
        (tenant, order_id, refund_id),
    ).fetchone()
    return row is not None


def _refund_view(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> dict:
    row = _get_refund_conn(conn, tenant, order_id, refund_id)
    return {
        "refund_amount_cents": row["amount_cents"],
        "refund_effective_deduction_cents": row["effective_deduction_cents"],
    }


def accept(
    tenant: str,
    order_id: str,
    refund_id: str,
    workorder_id: str,
    claim_amount_cents: int,
    initiator: str,
    reason: str,
    request_id: str,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, workorder_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            refund = _get_refund_conn(conn, tenant, order_id, refund_id)
            if refund is None:
                response = {"detail": "refund not found"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, workorder_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            existing = _get_workorder_conn(conn, tenant, order_id, refund_id, workorder_id)
            if existing is not None:
                response = {"detail": "workorder already accepted"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, workorder_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            if refund["status"] != "pending":
                response = {"detail": f"cannot accept workorder on refund in status {refund['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, workorder_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 一张退款单最多被一张进行中工单标记
            if has_active(conn, tenant, order_id, refund_id):
                response = {"detail": "refund already marked by an in-progress workorder"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, workorder_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 受理只落工单并施加标记，不改变订单与退款单金额（不动可退余额约束）
            conn.execute(
                "INSERT INTO workorders(tenant, order_id, refund_id, workorder_id, claim_amount_cents,"
                " initiator, reason, status, award_cents, effective_deduction_cents, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,'accepted',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (tenant, order_id, refund_id, workorder_id, claim_amount_cents, initiator, reason),
            )
            workorder = _row_to_workorder(_get_workorder_conn(conn, tenant, order_id, refund_id, workorder_id))
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, workorder_id, 201, workorder)
            conn.execute("COMMIT")
            return 201, workorder
        except WorkorderConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def advance(
    tenant: str,
    order_id: str,
    refund_id: str,
    workorder_id: str,
    to_status: str,
    award_cents: int | None,
    request_id: str,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ADVANCE, order_id, refund_id, workorder_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            workorder = _get_workorder_conn(conn, tenant, order_id, refund_id, workorder_id)
            if workorder is None:
                response = {"detail": "workorder not found"}
                _save_idempotent(conn, tenant, request_id, OP_ADVANCE, order_id, refund_id, workorder_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            current = workorder["status"]
            if current in (RESOLVED, CANCELLED):
                response = {"detail": f"cannot advance workorder in terminal status {current}"}
                _save_idempotent(conn, tenant, request_id, OP_ADVANCE, order_id, refund_id, workorder_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if to_status not in _ADVANCE_TARGETS[current]:
                response = {"detail": f"cannot advance workorder from {current} to {to_status}"}
                _save_idempotent(conn, tenant, request_id, OP_ADVANCE, order_id, refund_id, workorder_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            if to_status == RESOLVED:
                refund = _get_refund_conn(conn, tenant, order_id, refund_id)
                award = award_cents if award_cents is not None else 0
                # 裁决金额须为正整数，且不超过处理请求金额与退款单金额
                if award <= 0 or award > workorder["claim_amount_cents"] or award > refund["amount_cents"]:
                    response = {"detail": "award exceeds claim amount or refund amount"}
                    _save_idempotent(conn, tenant, request_id, OP_ADVANCE, order_id, refund_id, workorder_id, 409, response)
                    conn.execute("COMMIT")
                    return 409, response
                # 扣减退款单金额并同步生效扣减；订单金额不变
                conn.execute(
                    "UPDATE refunds SET amount_cents = amount_cents - ?,"
                    " effective_deduction_cents = effective_deduction_cents + ?, updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=?",
                    (award, award, tenant, order_id, refund_id),
                )
                conn.execute(
                    "UPDATE workorders SET status='resolved', award_cents=?, effective_deduction_cents=?,"
                    " updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND workorder_id=?",
                    (award, award, tenant, order_id, refund_id, workorder_id),
                )
            else:
                # 推进到 processing/pending_review/cancelled；到已撤销即释放处理中标记、不扣减金额
                conn.execute(
                    "UPDATE workorders SET status=?, updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND workorder_id=?",
                    (to_status, tenant, order_id, refund_id, workorder_id),
                )

            result = _row_to_workorder(_get_workorder_conn(conn, tenant, order_id, refund_id, workorder_id))
            if to_status == RESOLVED:
                result.update(_refund_view(conn, tenant, order_id, refund_id))
            _save_idempotent(conn, tenant, request_id, OP_ADVANCE, order_id, refund_id, workorder_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except WorkorderConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def cancel(
    tenant: str, order_id: str, refund_id: str, workorder_id: str, request_id: str
) -> tuple[int, dict]:
    """撤销关闭：仅已解决工单可撤销一次，把裁决扣减加回退款单并恢复其生效扣减，两侧原子生效。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_CANCEL, order_id, refund_id, workorder_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            workorder = _get_workorder_conn(conn, tenant, order_id, refund_id, workorder_id)
            if workorder is None:
                response = {"detail": "workorder not found"}
                _save_idempotent(conn, tenant, request_id, OP_CANCEL, order_id, refund_id, workorder_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            if workorder["status"] == CANCELLED:
                response = {"detail": "workorder already cancelled"}
                _save_idempotent(conn, tenant, request_id, OP_CANCEL, order_id, refund_id, workorder_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if workorder["status"] != RESOLVED:
                response = {"detail": f"cannot cancel workorder in status {workorder['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_CANCEL, order_id, refund_id, workorder_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            award = workorder["award_cents"]
            # 两侧原子更新：扣减加回退款单并恢复其生效扣减，工单进入已撤销终态
            conn.execute(
                "UPDATE refunds SET amount_cents = amount_cents + ?,"
                " effective_deduction_cents = MAX(effective_deduction_cents - ?, 0), updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND order_id=? AND refund_id=?",
                (award, award, tenant, order_id, refund_id),
            )
            conn.execute(
                "UPDATE workorders SET status='cancelled', effective_deduction_cents=0, updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND order_id=? AND refund_id=? AND workorder_id=?",
                (tenant, order_id, refund_id, workorder_id),
            )
            result = _row_to_workorder(_get_workorder_conn(conn, tenant, order_id, refund_id, workorder_id))
            result.update(_refund_view(conn, tenant, order_id, refund_id))
            _save_idempotent(conn, tenant, request_id, OP_CANCEL, order_id, refund_id, workorder_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except WorkorderConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get(tenant: str, order_id: str, refund_id: str, workorder_id: str) -> dict | None:
    conn = connect()
    try:
        row = _get_workorder_conn(conn, tenant, order_id, refund_id, workorder_id)
    finally:
        conn.close()
    return _row_to_workorder(row) if row is not None else None


def list_for_refund(tenant: str, order_id: str, refund_id: str) -> list[dict] | None:
    conn = connect()
    try:
        refund = _get_refund_conn(conn, tenant, order_id, refund_id)
        if refund is None:
            return None
        rows = conn.execute(
            "SELECT order_id, refund_id, workorder_id, status, claim_amount_cents, initiator, reason,"
            " award_cents, effective_deduction_cents FROM workorders"
            " WHERE tenant=? AND order_id=? AND refund_id=? ORDER BY rowid",
            (tenant, order_id, refund_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_workorder(row) for row in rows]
