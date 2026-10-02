import json
import sqlite3

from app.store.db import connect

# 对账单状态机：
#   pending --settle---> settled --reverse--> reversed
#   pending --cancel---> cancelled
# settled/cancelled/reversed 均为终态（reversed 后亦不可再推进）
PENDING = "pending"
SETTLED = "settled"
CANCELLED = "cancelled"
REVERSED = "reversed"

STATUSES = (PENDING, SETTLED, CANCELLED, REVERSED)

OP_ACCEPT = "accept"
OP_SETTLE = "settle"
OP_CANCEL = "cancel"
OP_REVERSE = "reverse"


class ReconciliationConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_reconciliation(row: sqlite3.Row) -> dict:
    # 当前生效扣减：仅已核销按核销金额生效，其余状态为 0
    effective = row["amount_cents"] if row["status"] == SETTLED else 0
    return {
        "order_id": row["order_id"],
        "refund_id": row["refund_id"],
        "reconciliation_id": row["reconciliation_id"],
        "amount_cents": row["amount_cents"],
        "reason": row["reason"],
        "status": row["status"],
        "effective_deduction_cents": effective,
    }


def _load_idempotent(conn: sqlite3.Connection, tenant: str, request_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT op, order_id, refund_id, reconciliation_id, http_status, response_json"
        " FROM reconciliation_requests WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    return (
        row["op"],
        row["order_id"],
        row["refund_id"],
        row["reconciliation_id"],
        row["http_status"],
        json.loads(row["response_json"]),
    )


def _save_idempotent(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    order_id: str,
    refund_id: str,
    reconciliation_id: str,
    http_status: int,
    response: dict,
) -> None:
    conn.execute(
        "INSERT INTO reconciliation_requests(tenant, request_id, op, order_id, refund_id,"
        " reconciliation_id, http_status, response_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, order_id, refund_id, reconciliation_id,
         http_status, json.dumps(response, ensure_ascii=False)),
    )


def _replay_or_none(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    order_id: str,
    refund_id: str,
    reconciliation_id: str,
) -> tuple[int, dict] | None:
    """命中幂等记录则返回首次结果；同 request_id 指向不同操作/对象则冲突。"""
    recorded = _load_idempotent(conn, tenant, request_id)
    if recorded is None:
        return None
    saved_op, saved_order, saved_refund, saved_recon, http_status, response = recorded
    if (saved_op, saved_order, saved_refund, saved_recon) != (op, order_id, refund_id, reconciliation_id):
        raise ReconciliationConflict("request_id was already used for a different operation")
    return http_status, response


def _get_reconciliation_conn(
    conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str, reconciliation_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT order_id, refund_id, reconciliation_id, amount_cents, reason, status"
        " FROM refund_reconciliations"
        " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?",
        (tenant, order_id, refund_id, reconciliation_id),
    ).fetchone()


def _get_refund_conn(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT amount_cents, status FROM refunds WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()


def settled_sum_conn(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> int:
    """已生效（已核销）对账单核销金额之和。供工单裁决同事务核对账面守恒。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS settled_sum FROM refund_reconciliations"
        " WHERE tenant=? AND order_id=? AND refund_id=? AND status='settled'",
        (tenant, order_id, refund_id),
    ).fetchone()
    return row["settled_sum"]


def _pending_sum_conn(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> int:
    """进行中（待核销）对账单核销金额之和，即当前占用的未核销余额。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS pending_sum FROM refund_reconciliations"
        " WHERE tenant=? AND order_id=? AND refund_id=? AND status='pending'",
        (tenant, order_id, refund_id),
    ).fetchone()
    return row["pending_sum"]


def _refund_view(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> dict:
    refund = _get_refund_conn(conn, tenant, order_id, refund_id)
    unreconciled = (
        refund["amount_cents"]
        - settled_sum_conn(conn, tenant, order_id, refund_id)
        - _pending_sum_conn(conn, tenant, order_id, refund_id)
    )
    return {"refund_amount_cents": refund["amount_cents"], "unreconciled_cents": unreconciled}


def accept(
    tenant: str, order_id: str, refund_id: str, reconciliation_id: str,
    amount_cents: int, reason: str, request_id: str,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, reconciliation_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            refund = _get_refund_conn(conn, tenant, order_id, refund_id)
            if refund is None:
                response = {"detail": "refund not found"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, reconciliation_id,
                                 404, response)
                conn.execute("COMMIT")
                return 404, response

            existing = _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id)
            if existing is not None:
                response = {"detail": "reconciliation already accepted"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, reconciliation_id,
                                 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 守恒不变量：待核销占用 + 已核销合计 <= 退款单当前金额；受理即占用未核销余额
            occupied = (
                _pending_sum_conn(conn, tenant, order_id, refund_id)
                + settled_sum_conn(conn, tenant, order_id, refund_id)
            )
            if amount_cents <= 0 or occupied + amount_cents > refund["amount_cents"]:
                response = {"detail": "reconciliation exceeds unreconciled balance"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, reconciliation_id,
                                 409, response)
                conn.execute("COMMIT")
                return 409, response

            conn.execute(
                "INSERT INTO refund_reconciliations(tenant, order_id, refund_id, reconciliation_id,"
                " amount_cents, reason, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,'pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (tenant, order_id, refund_id, reconciliation_id, amount_cents, reason),
            )
            reconciliation = _row_to_reconciliation(
                _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id))
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, reconciliation_id,
                             201, reconciliation)
            conn.execute("COMMIT")
            return 201, reconciliation
        except ReconciliationConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _advance(
    tenant: str, order_id: str, refund_id: str, reconciliation_id: str,
    request_id: str, op: str, target_status: str,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, op, order_id, refund_id, reconciliation_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            reconciliation = _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id)
            if reconciliation is None:
                response = {"detail": "reconciliation not found"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, reconciliation_id,
                                 404, response)
                conn.execute("COMMIT")
                return 404, response

            if reconciliation["status"] == target_status:
                response = {"detail": f"reconciliation already {target_status}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, reconciliation_id,
                                 409, response)
                conn.execute("COMMIT")
                return 409, response
            if reconciliation["status"] != PENDING:
                response = {"detail": f"cannot {op} reconciliation in status {reconciliation['status']}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, reconciliation_id,
                                 409, response)
                conn.execute("COMMIT")
                return 409, response

            if op == OP_SETTLE:
                # 核销完成把核销金额计入已核销金额；退款单金额可能已被工单裁决调减，
                # 核销后已核销合计不得超过退款单当前金额，超限拒绝且不留部分写入
                refund = _get_refund_conn(conn, tenant, order_id, refund_id)
                settled_sum = settled_sum_conn(conn, tenant, order_id, refund_id)
                if settled_sum + reconciliation["amount_cents"] > refund["amount_cents"]:
                    response = {"detail": "reconciliation exceeds unreconciled balance"}
                    _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, reconciliation_id,
                                     409, response)
                    conn.execute("COMMIT")
                    return 409, response
                conn.execute(
                    "UPDATE refund_reconciliations SET status='settled', updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?",
                    (tenant, order_id, refund_id, reconciliation_id),
                )
            else:  # cancel：释放占用，不改已核销金额
                conn.execute(
                    "UPDATE refund_reconciliations SET status='cancelled', updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?",
                    (tenant, order_id, refund_id, reconciliation_id),
                )

            result = _row_to_reconciliation(
                _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id))
            result.update(_refund_view(conn, tenant, order_id, refund_id))
            _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, reconciliation_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except ReconciliationConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def settle(tenant: str, order_id: str, refund_id: str, reconciliation_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, reconciliation_id, request_id, OP_SETTLE, SETTLED)


def cancel(tenant: str, order_id: str, refund_id: str, reconciliation_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, reconciliation_id, request_id, OP_CANCEL, CANCELLED)


def reverse(tenant: str, order_id: str, refund_id: str, reconciliation_id: str, request_id: str) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, reconciliation_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            reconciliation = _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id)
            if reconciliation is None:
                response = {"detail": "reconciliation not found"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, reconciliation_id,
                                 404, response)
                conn.execute("COMMIT")
                return 404, response

            if reconciliation["status"] == REVERSED:
                response = {"detail": "reconciliation already reversed"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, reconciliation_id,
                                 409, response)
                conn.execute("COMMIT")
                return 409, response
            if reconciliation["status"] != SETTLED:
                response = {"detail": f"cannot reverse reconciliation in status {reconciliation['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, reconciliation_id,
                                 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 冲正把已核销金额减回（已核销合计同步下降），对账单进入已冲正终态；
            # 订单与退款单金额均不变，两侧在同一事务内原子生效
            conn.execute(
                "UPDATE refund_reconciliations SET status='reversed', updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?",
                (tenant, order_id, refund_id, reconciliation_id),
            )
            result = _row_to_reconciliation(
                _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id))
            result.update(_refund_view(conn, tenant, order_id, refund_id))
            _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id, reconciliation_id,
                             200, result)
            conn.execute("COMMIT")
            return 200, result
        except ReconciliationConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get(tenant: str, order_id: str, refund_id: str, reconciliation_id: str) -> dict | None:
    conn = connect()
    try:
        row = _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id)
    finally:
        conn.close()
    return _row_to_reconciliation(row) if row is not None else None


def list_for_refund(tenant: str, order_id: str, refund_id: str) -> list[dict] | None:
    conn = connect()
    try:
        refund = conn.execute(
            "SELECT 1 FROM refunds WHERE tenant=? AND order_id=? AND refund_id=?",
            (tenant, order_id, refund_id),
        ).fetchone()
        if refund is None:
            return None
        rows = conn.execute(
            "SELECT order_id, refund_id, reconciliation_id, amount_cents, reason, status"
            " FROM refund_reconciliations"
            " WHERE tenant=? AND order_id=? AND refund_id=? ORDER BY rowid",
            (tenant, order_id, refund_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_reconciliation(row) for row in rows]


def search(
    tenant: str,
    status: str | None = None,
    min_amount_cents: int | None = None,
    max_amount_cents: int | None = None,
) -> list[dict]:
    """按状态与核销金额范围检索当前租户对账单，按受理先后稳定排序。"""
    sql = ("SELECT order_id, refund_id, reconciliation_id, amount_cents, reason, status"
           " FROM refund_reconciliations WHERE tenant=?")
    params: list = [tenant]
    if status is not None:
        sql += " AND status=?"
        params.append(status)
    if min_amount_cents is not None:
        sql += " AND amount_cents>=?"
        params.append(min_amount_cents)
    if max_amount_cents is not None:
        sql += " AND amount_cents<=?"
        params.append(max_amount_cents)
    sql += " ORDER BY rowid"
    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_reconciliation(row) for row in rows]
