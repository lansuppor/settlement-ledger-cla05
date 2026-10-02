import json
import sqlite3

from app.store.db import connect

# 对账单（退款单对账核销）状态机：
#   pending --reconcile--> reconciled --reverse--> reversed
#   pending --cancel----> cancelled
# reconciled/cancelled/reversed 均为终态（reversed 后亦不可再推进）
PENDING = "pending"
RECONCILED = "reconciled"
CANCELLED = "cancelled"
REVERSED = "reversed"

STATUSES = (PENDING, RECONCILED, CANCELLED, REVERSED)

OP_ACCEPT = "accept"
OP_RECONCILE = "reconcile"
OP_CANCEL = "cancel"
OP_REVERSE = "reverse"


class ReconciliationConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_reconciliation(row: sqlite3.Row) -> dict:
    # 当前生效扣减：仅已核销按核销金额生效，其余状态（含已冲正）为 0
    effective = row["amount_cents"] if row["status"] == RECONCILED else 0
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
        "INSERT INTO reconciliation_requests(tenant, request_id, op, order_id, refund_id, reconciliation_id,"
        " http_status, response_json, created_at) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, order_id, refund_id, reconciliation_id, http_status,
         json.dumps(response, ensure_ascii=False)),
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
        "SELECT order_id, refund_id, reconciliation_id, amount_cents, reason, status FROM reconciliations"
        " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?",
        (tenant, order_id, refund_id, reconciliation_id),
    ).fetchone()


def _get_refund_amount_conn(
    conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str
) -> int | None:
    row = conn.execute(
        "SELECT amount_cents FROM refunds WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()
    return None if row is None else row["amount_cents"]


def _sum_by_status_conn(
    conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str, status: str
) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS total FROM reconciliations"
        " WHERE tenant=? AND order_id=? AND refund_id=? AND status=?",
        (tenant, order_id, refund_id, status),
    ).fetchone()
    return row["total"]


def occupied_sum_conn(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> int:
    """已核销合计 + 待核销占用合计；即退款单金额中已被对账单锁定的部分。

    未核销余额 = 退款单金额 − 已生效核销金额之和；待核销单在受理时即占用同一池余额，
    故余额守恒不变量为 已核销合计 + 待核销合计 <= 退款单当前金额。
    供工单裁决扣减退款单金额时在同一事务内核对。
    """
    return (_sum_by_status_conn(conn, tenant, order_id, refund_id, RECONCILED)
            + _sum_by_status_conn(conn, tenant, order_id, refund_id, PENDING))


def accept(
    tenant: str,
    order_id: str,
    refund_id: str,
    reconciliation_id: str,
    amount_cents: int,
    reason: str,
    request_id: str,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(
                conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, reconciliation_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            refund_amount = _get_refund_amount_conn(conn, tenant, order_id, refund_id)
            if refund_amount is None:
                response = {"detail": "refund not found"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id,
                                 reconciliation_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            existing = _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id)
            if existing is not None:
                response = {"detail": "reconciliation already accepted"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id,
                                 reconciliation_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 守恒不变量：已核销合计 + 待核销占用 + 本单 <= 退款单当前金额；受理即占用未核销余额
            occupied = occupied_sum_conn(conn, tenant, order_id, refund_id)
            if amount_cents <= 0 or occupied + amount_cents > refund_amount:
                response = {"detail": "reconciliation exceeds unwritten-off amount"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id,
                                 reconciliation_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            conn.execute(
                "INSERT INTO reconciliations(tenant, order_id, refund_id, reconciliation_id, amount_cents,"
                " reason, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,'pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (tenant, order_id, refund_id, reconciliation_id, amount_cents, reason),
            )
            reconciliation = _row_to_reconciliation(
                _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id))
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id,
                             reconciliation_id, 201, reconciliation)
            conn.execute("COMMIT")
            return 201, reconciliation
        except ReconciliationConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _advance(
    tenant: str,
    order_id: str,
    refund_id: str,
    reconciliation_id: str,
    request_id: str,
    op: str,
    target_status: str,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(
                conn, tenant, request_id, op, order_id, refund_id, reconciliation_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            reconciliation = _get_reconciliation_conn(
                conn, tenant, order_id, refund_id, reconciliation_id)
            if reconciliation is None:
                response = {"detail": "reconciliation not found"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id,
                                 reconciliation_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            if reconciliation["status"] == target_status:
                response = {"detail": f"reconciliation already {target_status}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id,
                                 reconciliation_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if reconciliation["status"] != PENDING:
                response = {"detail": f"cannot {op} reconciliation in status {reconciliation['status']}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id,
                                 reconciliation_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            if op == OP_RECONCILE:
                # 占用转为已核销：不调整订单与退款单金额，仅状态推进；
                # 受理时已校验占用合计 <= 退款单金额，推进只是把同额在两类合计间转移，守恒天然保持。
                # 条件更新兜底并发，绝不重复核销。
                conn.execute(
                    "UPDATE reconciliations SET status='reconciled', updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?"
                    " AND status='pending'",
                    (tenant, order_id, refund_id, reconciliation_id),
                )
            else:  # cancel：释放占用，不改已核销金额，不动订单/退款单金额
                conn.execute(
                    "UPDATE reconciliations SET status='cancelled', updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?"
                    " AND status='pending'",
                    (tenant, order_id, refund_id, reconciliation_id),
                )
            changed = conn.execute("SELECT changes()").fetchone()[0]
            if changed == 0:
                raise ReconciliationConflict(f"cannot {op} reconciliation in status {reconciliation['status']}")

            result = _row_to_reconciliation(
                _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id))
            _save_idempotent(conn, tenant, request_id, op, order_id, refund_id,
                             reconciliation_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except ReconciliationConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def reconcile(
    tenant: str, order_id: str, refund_id: str, reconciliation_id: str, request_id: str
) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, reconciliation_id, request_id,
                    OP_RECONCILE, RECONCILED)


def cancel(
    tenant: str, order_id: str, refund_id: str, reconciliation_id: str, request_id: str
) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, reconciliation_id, request_id,
                    OP_CANCEL, CANCELLED)


def reverse(
    tenant: str, order_id: str, refund_id: str, reconciliation_id: str, request_id: str
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(
                conn, tenant, request_id, OP_REVERSE, order_id, refund_id, reconciliation_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            reconciliation = _get_reconciliation_conn(
                conn, tenant, order_id, refund_id, reconciliation_id)
            if reconciliation is None:
                response = {"detail": "reconciliation not found"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id,
                                 reconciliation_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            if reconciliation["status"] == REVERSED:
                response = {"detail": "reconciliation already reversed"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id,
                                 reconciliation_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if reconciliation["status"] != RECONCILED:
                response = {"detail": f"cannot reverse reconciliation in status {reconciliation['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id,
                                 reconciliation_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 冲正把已核销金额减回（该额重新进入未核销余额），进入已冲正终态；
            # 不改订单与退款单金额。条件更新兜底并发，冲正仅一次。
            conn.execute(
                "UPDATE reconciliations SET status='reversed', updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND order_id=? AND refund_id=? AND reconciliation_id=?"
                " AND status='reconciled'",
                (tenant, order_id, refund_id, reconciliation_id),
            )
            changed = conn.execute("SELECT changes()").fetchone()[0]
            if changed == 0:
                raise ReconciliationConflict("cannot reverse reconciliation that is not reconciled")

            result = _row_to_reconciliation(
                _get_reconciliation_conn(conn, tenant, order_id, refund_id, reconciliation_id))
            _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, refund_id,
                             reconciliation_id, 200, result)
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
            " FROM reconciliations WHERE tenant=? AND order_id=? AND refund_id=? ORDER BY rowid",
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
           " FROM reconciliations WHERE tenant=?")
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
