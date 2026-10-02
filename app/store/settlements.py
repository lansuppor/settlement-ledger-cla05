import json
import sqlite3

from app.store.db import connect

# 结算单状态机：
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


class SettlementConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_settlement(row: sqlite3.Row) -> dict:
    # 当前生效扣减：仅已核销按核销金额生效，其余状态为 0
    effective = row["amount_cents"] if row["status"] == SETTLED else 0
    return {
        "order_id": row["order_id"],
        "settlement_id": row["settlement_id"],
        "amount_cents": row["amount_cents"],
        "reason": row["reason"],
        "status": row["status"],
        "effective_deduction_cents": effective,
    }


def _load_idempotent(conn: sqlite3.Connection, tenant: str, request_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT op, order_id, settlement_id, http_status, response_json FROM settlement_requests"
        " WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    return row["op"], row["order_id"], row["settlement_id"], row["http_status"], json.loads(row["response_json"])


def _save_idempotent(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    order_id: str,
    settlement_id: str,
    http_status: int,
    response: dict,
) -> None:
    conn.execute(
        "INSERT INTO settlement_requests(tenant, request_id, op, order_id, settlement_id,"
        " http_status, response_json, created_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, order_id, settlement_id, http_status, json.dumps(response, ensure_ascii=False)),
    )


def _replay_or_none(
    conn: sqlite3.Connection, tenant: str, request_id: str, op: str, order_id: str, settlement_id: str
) -> tuple[int, dict] | None:
    """命中幂等记录则返回首次结果；同 request_id 指向不同操作/对象则冲突。"""
    recorded = _load_idempotent(conn, tenant, request_id)
    if recorded is None:
        return None
    saved_op, saved_order, saved_settlement, http_status, response = recorded
    if (saved_op, saved_order, saved_settlement) != (op, order_id, settlement_id):
        raise SettlementConflict("request_id was already used for a different operation")
    return http_status, response


def _get_settlement_conn(
    conn: sqlite3.Connection, tenant: str, order_id: str, settlement_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT order_id, settlement_id, amount_cents, reason, status FROM settlements"
        " WHERE tenant=? AND order_id=? AND settlement_id=?",
        (tenant, order_id, settlement_id),
    ).fetchone()


def pending_sum_conn(conn: sqlite3.Connection, tenant: str, order_id: str) -> int:
    """进行中（待核销）结算单核销金额之和，即当前占用的未收余额。供订单收款同事务核对。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS pending_sum FROM settlements"
        " WHERE tenant=? AND order_id=? AND status='pending'",
        (tenant, order_id),
    ).fetchone()
    return row["pending_sum"]


def _order_view(conn: sqlite3.Connection, tenant: str, order_id: str) -> dict:
    row = conn.execute(
        "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    outstanding = row["amount_cents"] - row["paid_cents"] - pending_sum_conn(conn, tenant, order_id)
    return {"paid_cents": row["paid_cents"], "outstanding_cents": outstanding}


def accept(
    tenant: str, order_id: str, settlement_id: str, amount_cents: int, reason: str, request_id: str
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ACCEPT, order_id, settlement_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            order = conn.execute(
                "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
                (tenant, order_id),
            ).fetchone()
            if order is None:
                response = {"detail": "order not found"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, settlement_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            existing = _get_settlement_conn(conn, tenant, order_id, settlement_id)
            if existing is not None:
                response = {"detail": "settlement already accepted"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, settlement_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 守恒不变量：待核销占用之和 <= 订单金额 - 已收；受理即占用未收余额
            pending_sum = pending_sum_conn(conn, tenant, order_id)
            if amount_cents <= 0 or pending_sum + amount_cents > order["amount_cents"] - order["paid_cents"]:
                response = {"detail": "settlement exceeds outstanding amount"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, settlement_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            conn.execute(
                "INSERT INTO settlements(tenant, order_id, settlement_id, amount_cents, reason, status,"
                " created_at, updated_at) VALUES(?,?,?,?,?,'pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (tenant, order_id, settlement_id, amount_cents, reason),
            )
            settlement = _row_to_settlement(_get_settlement_conn(conn, tenant, order_id, settlement_id))
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, settlement_id, 201, settlement)
            conn.execute("COMMIT")
            return 201, settlement
        except SettlementConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _advance(
    tenant: str, order_id: str, settlement_id: str, request_id: str, op: str, target_status: str
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, op, order_id, settlement_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            settlement = _get_settlement_conn(conn, tenant, order_id, settlement_id)
            if settlement is None:
                response = {"detail": "settlement not found"}
                _save_idempotent(conn, tenant, request_id, op, order_id, settlement_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            if settlement["status"] == target_status:
                response = {"detail": f"settlement already {target_status}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, settlement_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if settlement["status"] != PENDING:
                response = {"detail": f"cannot {op} settlement in status {settlement['status']}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, settlement_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            amount = settlement["amount_cents"]
            if op == OP_SETTLE:
                # 核销金额计入订单已收；条件更新兜底并发，绝不超额核销
                conn.execute(
                    "UPDATE orders SET paid_cents = paid_cents + ?,"
                    " status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
                    " WHERE tenant=? AND order_id=? AND paid_cents + ? <= amount_cents",
                    (amount, amount, tenant, order_id, amount),
                )
                changed = conn.execute("SELECT changes()").fetchone()[0]
                if changed == 0:
                    raise SettlementConflict("settlement exceeds outstanding amount")
                conn.execute(
                    "UPDATE settlements SET status='settled', updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND settlement_id=?",
                    (tenant, order_id, settlement_id),
                )
            else:  # cancel：释放占用，不动订单金额
                conn.execute(
                    "UPDATE settlements SET status='cancelled', updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND settlement_id=?",
                    (tenant, order_id, settlement_id),
                )

            result = _row_to_settlement(_get_settlement_conn(conn, tenant, order_id, settlement_id))
            result.update(_order_view(conn, tenant, order_id))
            _save_idempotent(conn, tenant, request_id, op, order_id, settlement_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except SettlementConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def settle(tenant: str, order_id: str, settlement_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, settlement_id, request_id, OP_SETTLE, SETTLED)


def cancel(tenant: str, order_id: str, settlement_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, settlement_id, request_id, OP_CANCEL, CANCELLED)


def reverse(tenant: str, order_id: str, settlement_id: str, request_id: str) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_REVERSE, order_id, settlement_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            settlement = _get_settlement_conn(conn, tenant, order_id, settlement_id)
            if settlement is None:
                response = {"detail": "settlement not found"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, settlement_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            if settlement["status"] == REVERSED:
                response = {"detail": "settlement already reversed"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, settlement_id, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if settlement["status"] != SETTLED:
                response = {"detail": f"cannot reverse settlement in status {settlement['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, settlement_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            amount = settlement["amount_cents"]
            # 两侧原子更新：核销金额从订单已收减回（未收同步加回），结算单进入已冲正终态；
            # 条件更新兜底并发（如已收已被退款扣减），绝不出现负已收
            conn.execute(
                "UPDATE orders SET paid_cents = paid_cents - ?,"
                " status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END"
                " WHERE tenant=? AND order_id=? AND paid_cents >= ?",
                (amount, amount, tenant, order_id, amount),
            )
            changed = conn.execute("SELECT changes()").fetchone()[0]
            if changed == 0:
                raise SettlementConflict("order paid amount is insufficient for reversal")
            conn.execute(
                "UPDATE settlements SET status='reversed', updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND order_id=? AND settlement_id=?",
                (tenant, order_id, settlement_id),
            )
            result = _row_to_settlement(_get_settlement_conn(conn, tenant, order_id, settlement_id))
            result.update(_order_view(conn, tenant, order_id))
            _save_idempotent(conn, tenant, request_id, OP_REVERSE, order_id, settlement_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except SettlementConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get(tenant: str, order_id: str, settlement_id: str) -> dict | None:
    conn = connect()
    try:
        row = _get_settlement_conn(conn, tenant, order_id, settlement_id)
    finally:
        conn.close()
    return _row_to_settlement(row) if row is not None else None


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
            "SELECT order_id, settlement_id, amount_cents, reason, status FROM settlements"
            " WHERE tenant=? AND order_id=? ORDER BY rowid",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_settlement(row) for row in rows]


def search(
    tenant: str,
    status: str | None = None,
    min_amount_cents: int | None = None,
    max_amount_cents: int | None = None,
) -> list[dict]:
    """按状态与核销金额范围检索当前租户结算单，按受理先后稳定排序。"""
    sql = ("SELECT order_id, settlement_id, amount_cents, reason, status FROM settlements"
           " WHERE tenant=?")
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
    return [_row_to_settlement(row) for row in rows]
