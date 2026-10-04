import json
import sqlite3

from app.store.db import connect

# 出入库单状态机：
#   pending --post---> posted --reverse--> reversed
#   pending --cancel---> cancelled
# posted 只能冲正一次；cancelled/reversed 为终态，均不可再推进
PENDING = "pending"
POSTED = "posted"
CANCELLED = "cancelled"
REVERSED = "reversed"

STATUSES = (PENDING, POSTED, CANCELLED, REVERSED)

INBOUND = "inbound"
OUTBOUND = "outbound"
DIRECTIONS = (INBOUND, OUTBOUND)

OP_ACCEPT = "accept"
OP_POST = "post"
OP_CANCEL = "cancel"
OP_REVERSE = "reverse"


class InventoryConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_doc(row: sqlite3.Row) -> dict:
    return {
        "doc_id": row["doc_id"],
        "warehouse_id": row["warehouse_id"],
        "item_id": row["item_id"],
        "direction": row["direction"],
        "quantity": row["quantity"],
        "status": row["status"],
    }


def _load_idempotent(conn: sqlite3.Connection, tenant: str, request_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT op, doc_id, params_json, http_status, response_json FROM inventory_requests"
        " WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    return (
        row["op"],
        row["doc_id"],
        json.loads(row["params_json"]),
        row["http_status"],
        json.loads(row["response_json"]),
    )


def _save_idempotent(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    doc_id: str,
    params: dict,
    http_status: int,
    response: dict,
) -> None:
    conn.execute(
        "INSERT INTO inventory_requests(tenant, request_id, op, doc_id, params_json,"
        " http_status, response_json, created_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, doc_id, json.dumps(params, ensure_ascii=False, sort_keys=True),
         http_status, json.dumps(response, ensure_ascii=False)),
    )


def _replay_or_none(
    conn: sqlite3.Connection, tenant: str, request_id: str, op: str, doc_id: str, params: dict
) -> tuple[int, dict] | None:
    """命中幂等记录则返回首次结果（含业务拒绝）；同 request_id 指向不同操作/对象/参数则冲突。"""
    recorded = _load_idempotent(conn, tenant, request_id)
    if recorded is None:
        return None
    saved_op, saved_doc, saved_params, http_status, response = recorded
    if (saved_op, saved_doc, saved_params) != (op, doc_id, params):
        raise InventoryConflict("request_id was already used for a different operation")
    return http_status, response


def _reject(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    doc_id: str,
    params: dict,
    http_status: int,
    reason: str,
    detail: str,
) -> tuple[int, dict]:
    """业务拒绝：只落幂等结果（可重放），不改动单据与余额。"""
    response = {"detail": detail, "reason": reason}
    _save_idempotent(conn, tenant, request_id, op, doc_id, params, http_status, response)
    conn.execute("COMMIT")
    return http_status, response


def _get_doc_conn(conn: sqlite3.Connection, tenant: str, doc_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT doc_id, warehouse_id, item_id, direction, quantity, status FROM inventory_docs"
        " WHERE tenant=? AND doc_id=?",
        (tenant, doc_id),
    ).fetchone()


def _balance_conn(conn: sqlite3.Connection, tenant: str, warehouse_id: str, item_id: str) -> dict:
    """余额由单据流水推导：账面=已入账入库-已入账出库；占用=待入账出库；可用=账面-占用。"""
    row = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN status='posted' AND direction='inbound' THEN quantity"
        "                   WHEN status='posted' AND direction='outbound' THEN -quantity"
        "                   ELSE 0 END),0) AS book,"
        " COALESCE(SUM(CASE WHEN status='pending' AND direction='outbound' THEN quantity"
        "                   ELSE 0 END),0) AS reserved"
        " FROM inventory_docs WHERE tenant=? AND warehouse_id=? AND item_id=?",
        (tenant, warehouse_id, item_id),
    ).fetchone()
    book = row["book"]
    reserved = row["reserved"]
    return {
        "warehouse_id": warehouse_id,
        "item_id": item_id,
        "book_quantity": book,
        "reserved_quantity": reserved,
        "available_quantity": book - reserved,
    }


def accept(
    tenant: str,
    doc_id: str,
    warehouse_id: str,
    item_id: str,
    direction: str,
    quantity: int,
    request_id: str,
) -> tuple[int, dict]:
    params = {"warehouse_id": warehouse_id, "item_id": item_id,
              "direction": direction, "quantity": quantity}
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ACCEPT, doc_id, params)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                return _reject(conn, tenant, request_id, OP_ACCEPT, doc_id, params, 409,
                               "invalid_quantity", "quantity must be a positive integer")
            if direction not in DIRECTIONS:
                return _reject(conn, tenant, request_id, OP_ACCEPT, doc_id, params, 409,
                               "invalid_direction", "direction must be inbound or outbound")
            if not warehouse_id.strip() or not item_id.strip():
                return _reject(conn, tenant, request_id, OP_ACCEPT, doc_id, params, 409,
                               "invalid_identifier", "warehouse_id and item_id must be non-empty")

            if _get_doc_conn(conn, tenant, doc_id) is not None:
                return _reject(conn, tenant, request_id, OP_ACCEPT, doc_id, params, 409,
                               "duplicate_doc", "document already accepted")

            if direction == OUTBOUND:
                # 待入账出库单立即占用可用数量；超额受理拒绝，绝不出现负可用
                balance = _balance_conn(conn, tenant, warehouse_id, item_id)
                if quantity > balance["available_quantity"]:
                    return _reject(conn, tenant, request_id, OP_ACCEPT, doc_id, params, 409,
                                   "insufficient_available", "outbound exceeds available quantity")

            conn.execute(
                "INSERT INTO inventory_docs(tenant, doc_id, warehouse_id, item_id, direction,"
                " quantity, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,'pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (tenant, doc_id, warehouse_id, item_id, direction, quantity),
            )
            result = _row_to_doc(_get_doc_conn(conn, tenant, doc_id))
            result["balance"] = _balance_conn(conn, tenant, warehouse_id, item_id)
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, doc_id, params, 201, result)
            conn.execute("COMMIT")
            return 201, result
        except InventoryConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _advance(
    tenant: str, doc_id: str, request_id: str, op: str, target_status: str
) -> tuple[int, dict]:
    params: dict = {}
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, op, doc_id, params)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            doc = _get_doc_conn(conn, tenant, doc_id)
            if doc is None:
                return _reject(conn, tenant, request_id, op, doc_id, params, 404,
                               "doc_not_found", "document not found")
            if doc["status"] == target_status:
                return _reject(conn, tenant, request_id, op, doc_id, params, 409,
                               "invalid_state", f"document already {target_status}")
            if doc["status"] != PENDING:
                return _reject(conn, tenant, request_id, op, doc_id, params, 409,
                               "invalid_state", f"cannot {op} document in status {doc['status']}")

            # 入账：入库增加账面，出库把占用转为账面扣减；撤销：释放出库占用，不动账面
            # （余额由流水推导，状态翻转即生效；不变量 账面>=占用 在各分支均保持）
            conn.execute(
                "UPDATE inventory_docs SET status=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND doc_id=?",
                (target_status, tenant, doc_id),
            )
            updated = _get_doc_conn(conn, tenant, doc_id)
            result = _row_to_doc(updated)
            result["balance"] = _balance_conn(
                conn, tenant, updated["warehouse_id"], updated["item_id"])
            _save_idempotent(conn, tenant, request_id, op, doc_id, params, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except InventoryConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def post(tenant: str, doc_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, doc_id, request_id, OP_POST, POSTED)


def cancel(tenant: str, doc_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, doc_id, request_id, OP_CANCEL, CANCELLED)


def reverse(tenant: str, doc_id: str, request_id: str) -> tuple[int, dict]:
    params: dict = {}
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_REVERSE, doc_id, params)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            doc = _get_doc_conn(conn, tenant, doc_id)
            if doc is None:
                return _reject(conn, tenant, request_id, OP_REVERSE, doc_id, params, 404,
                               "doc_not_found", "document not found")
            if doc["status"] == REVERSED:
                return _reject(conn, tenant, request_id, OP_REVERSE, doc_id, params, 409,
                               "invalid_state", "document already reversed")
            if doc["status"] != POSTED:
                return _reject(conn, tenant, request_id, OP_REVERSE, doc_id, params, 409,
                               "invalid_state", f"cannot reverse document in status {doc['status']}")

            if doc["direction"] == INBOUND:
                # 冲正入库会减账面：不得使账面低于待入账出库占用，否则拒绝且保持原状态
                balance = _balance_conn(conn, tenant, doc["warehouse_id"], doc["item_id"])
                if balance["book_quantity"] - doc["quantity"] < balance["reserved_quantity"]:
                    return _reject(conn, tenant, request_id, OP_REVERSE, doc_id, params, 409,
                                   "insufficient_book",
                                   "reversal would drop book quantity below reserved quantity")
            # 冲正出库把数量加回账面；冲正入库从账面减回（已校验不低于占用）
            conn.execute(
                "UPDATE inventory_docs SET status='reversed', updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND doc_id=?",
                (tenant, doc_id),
            )
            result = _row_to_doc(_get_doc_conn(conn, tenant, doc_id))
            result["balance"] = _balance_conn(conn, tenant, doc["warehouse_id"], doc["item_id"])
            _save_idempotent(conn, tenant, request_id, OP_REVERSE, doc_id, params, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except InventoryConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get(tenant: str, doc_id: str) -> dict | None:
    conn = connect()
    try:
        row = _get_doc_conn(conn, tenant, doc_id)
    finally:
        conn.close()
    return _row_to_doc(row) if row is not None else None


def get_balance(tenant: str, warehouse_id: str, item_id: str) -> dict:
    """按仓库与商品读取余额；未发生任何单据时三项均为零。"""
    conn = connect()
    try:
        return _balance_conn(conn, tenant, warehouse_id, item_id)
    finally:
        conn.close()
