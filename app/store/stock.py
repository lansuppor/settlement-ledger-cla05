import json
import sqlite3

from app.store.db import connect

# 出入库单状态机：
#   pending --post-----> posted --reverse--> reversed
#   pending --cancel---> cancelled
# posted/cancelled/reversed 均不可再推进；冲正仅一次
PENDING = "pending"
POSTED = "posted"
CANCELLED = "cancelled"
REVERSED = "reversed"

STATUSES = (PENDING, POSTED, CANCELLED, REVERSED)

DIRECTION_IN = "in"
DIRECTION_OUT = "out"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)

OP_ACCEPT = "accept"
OP_POST = "post"
OP_CANCEL = "cancel"
OP_REVERSE = "reverse"


class StockConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_document(row: sqlite3.Row) -> dict:
    return {
        "doc_no": row["doc_no"],
        "warehouse_id": row["warehouse_id"],
        "item_id": row["item_id"],
        "direction": row["direction"],
        "quantity": row["quantity"],
        "status": row["status"],
    }


def _balance_view(row: sqlite3.Row | None, warehouse_id: str, item_id: str) -> dict:
    book = row["book_qty"] if row is not None else 0
    occupied = row["occupied_qty"] if row is not None else 0
    return {
        "warehouse_id": warehouse_id,
        "item_id": item_id,
        "book_qty": book,
        "occupied_qty": occupied,
        "available_qty": book - occupied,
    }


def _get_document_conn(conn: sqlite3.Connection, tenant: str, doc_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT doc_no, warehouse_id, item_id, direction, quantity, status FROM stock_documents"
        " WHERE tenant=? AND doc_no=?",
        (tenant, doc_no),
    ).fetchone()


def _get_balance_conn(conn: sqlite3.Connection, tenant: str, warehouse_id: str, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT book_qty, occupied_qty FROM stock_balances"
        " WHERE tenant=? AND warehouse_id=? AND item_id=?",
        (tenant, warehouse_id, item_id),
    ).fetchone()


def _ensure_balance_conn(conn: sqlite3.Connection, tenant: str, warehouse_id: str, item_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO stock_balances(tenant, warehouse_id, item_id, book_qty, occupied_qty)"
        " VALUES(?,?,?,0,0)",
        (tenant, warehouse_id, item_id),
    )


def _load_idempotent(conn: sqlite3.Connection, tenant: str, request_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT op, doc_no, params_json, http_status, response_json FROM stock_requests"
        " WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    return (row["op"], row["doc_no"], json.loads(row["params_json"]),
            row["http_status"], json.loads(row["response_json"]))


def _save_idempotent(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    doc_no: str,
    params: dict,
    http_status: int,
    response: dict,
) -> None:
    conn.execute(
        "INSERT INTO stock_requests(tenant, request_id, op, doc_no, params_json, http_status, response_json,"
        " created_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, doc_no, json.dumps(params, ensure_ascii=False, sort_keys=True),
         http_status, json.dumps(response, ensure_ascii=False)),
    )


def _replay_or_none(
    conn: sqlite3.Connection, tenant: str, request_id: str, op: str, doc_no: str, params: dict
) -> tuple[int, dict] | None:
    """命中幂等记录则返回首次结果；同 request_id 指向不同操作/对象/参数则冲突。"""
    recorded = _load_idempotent(conn, tenant, request_id)
    if recorded is None:
        return None
    saved_op, saved_doc_no, saved_params, http_status, response = recorded
    if (saved_op, saved_doc_no, saved_params) != (op, doc_no, params):
        raise StockConflict("request_id was already used for a different operation")
    return http_status, response


def accept(
    tenant: str,
    doc_no: str,
    warehouse_id: str,
    item_id: str,
    direction: str,
    quantity: int,
    request_id: str,
) -> tuple[int, dict]:
    params = {"warehouse_id": warehouse_id, "item_id": item_id, "direction": direction, "quantity": quantity}
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ACCEPT, doc_no, params)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            if _get_document_conn(conn, tenant, doc_no) is not None:
                response = {"detail": "stock document already accepted"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, doc_no, params, 409, response)
                conn.execute("COMMIT")
                return 409, response

            if direction == DIRECTION_OUT:
                # 待入账出库单受理即占用可用数量；可用 = 账面 - 占用，超额受理拒绝
                balance = _get_balance_conn(conn, tenant, warehouse_id, item_id)
                book = balance["book_qty"] if balance is not None else 0
                occupied = balance["occupied_qty"] if balance is not None else 0
                if quantity > book - occupied:
                    response = {"detail": "insufficient available stock"}
                    _save_idempotent(conn, tenant, request_id, OP_ACCEPT, doc_no, params, 409, response)
                    conn.execute("COMMIT")
                    return 409, response
                _ensure_balance_conn(conn, tenant, warehouse_id, item_id)
                conn.execute(
                    "UPDATE stock_balances SET occupied_qty = occupied_qty + ?"
                    " WHERE tenant=? AND warehouse_id=? AND item_id=?",
                    (quantity, tenant, warehouse_id, item_id),
                )

            conn.execute(
                "INSERT INTO stock_documents(tenant, doc_no, warehouse_id, item_id, direction, quantity, status,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,'pending',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (tenant, doc_no, warehouse_id, item_id, direction, quantity),
            )
            result = _row_to_document(_get_document_conn(conn, tenant, doc_no))
            result["balance"] = _balance_view(
                _get_balance_conn(conn, tenant, warehouse_id, item_id), warehouse_id, item_id)
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, doc_no, params, 201, result)
            conn.execute("COMMIT")
            return 201, result
        except StockConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _load_pending_or_reject(
    conn: sqlite3.Connection, tenant: str, doc_no: str, request_id: str, op: str, target_status: str
) -> tuple[sqlite3.Row | None, tuple[int, dict] | None]:
    """读取单据并按状态机校验；返回 (单据, None) 或 (None, 应直接返回的响应)。"""
    document = _get_document_conn(conn, tenant, doc_no)
    if document is None:
        response = {"detail": "stock document not found"}
        _save_idempotent(conn, tenant, request_id, op, doc_no, {}, 404, response)
        return None, (404, response)
    if document["status"] == target_status:
        response = {"detail": f"stock document already {target_status}"}
        _save_idempotent(conn, tenant, request_id, op, doc_no, {}, 409, response)
        return None, (409, response)
    if document["status"] != PENDING:
        response = {"detail": f"cannot {op} stock document in status {document['status']}"}
        _save_idempotent(conn, tenant, request_id, op, doc_no, {}, 409, response)
        return None, (409, response)
    return document, None


def _finish(
    conn: sqlite3.Connection, tenant: str, doc_no: str, request_id: str, op: str
) -> tuple[int, dict]:
    document = _get_document_conn(conn, tenant, doc_no)
    result = _row_to_document(document)
    result["balance"] = _balance_view(
        _get_balance_conn(conn, tenant, document["warehouse_id"], document["item_id"]),
        document["warehouse_id"], document["item_id"])
    _save_idempotent(conn, tenant, request_id, op, doc_no, {}, 200, result)
    conn.execute("COMMIT")
    return 200, result


def post(tenant: str, doc_no: str, request_id: str) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_POST, doc_no, {})
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            document, rejection = _load_pending_or_reject(conn, tenant, doc_no, request_id, OP_POST, POSTED)
            if rejection is not None:
                conn.execute("COMMIT")
                return rejection

            warehouse_id, item_id = document["warehouse_id"], document["item_id"]
            quantity = document["quantity"]
            _ensure_balance_conn(conn, tenant, warehouse_id, item_id)
            if document["direction"] == DIRECTION_IN:
                # 入库入账：增加账面数量
                conn.execute(
                    "UPDATE stock_balances SET book_qty = book_qty + ?"
                    " WHERE tenant=? AND warehouse_id=? AND item_id=?",
                    (quantity, tenant, warehouse_id, item_id),
                )
            else:
                # 出库入账：占用转为账面扣减；条件更新兜底并发，绝不出现负账面或负占用
                conn.execute(
                    "UPDATE stock_balances SET book_qty = book_qty - ?, occupied_qty = occupied_qty - ?"
                    " WHERE tenant=? AND warehouse_id=? AND item_id=? AND book_qty >= ? AND occupied_qty >= ?",
                    (quantity, quantity, tenant, warehouse_id, item_id, quantity, quantity),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise StockConflict("insufficient book stock to post outbound document")
            conn.execute(
                "UPDATE stock_documents SET status='posted', updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND doc_no=?",
                (tenant, doc_no),
            )
            return _finish(conn, tenant, doc_no, request_id, OP_POST)
        except StockConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def cancel(tenant: str, doc_no: str, request_id: str) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_CANCEL, doc_no, {})
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            document, rejection = _load_pending_or_reject(conn, tenant, doc_no, request_id, OP_CANCEL, CANCELLED)
            if rejection is not None:
                conn.execute("COMMIT")
                return rejection

            if document["direction"] == DIRECTION_OUT:
                # 撤销待入账出库单：释放占用，不改账面数量
                conn.execute(
                    "UPDATE stock_balances SET occupied_qty = occupied_qty - ?"
                    " WHERE tenant=? AND warehouse_id=? AND item_id=? AND occupied_qty >= ?",
                    (document["quantity"], tenant, document["warehouse_id"], document["item_id"],
                     document["quantity"]),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise StockConflict("occupied stock is insufficient to release")
            conn.execute(
                "UPDATE stock_documents SET status='cancelled', updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND doc_no=?",
                (tenant, doc_no),
            )
            return _finish(conn, tenant, doc_no, request_id, OP_CANCEL)
        except StockConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def reverse(tenant: str, doc_no: str, request_id: str) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_REVERSE, doc_no, {})
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            document = _get_document_conn(conn, tenant, doc_no)
            if document is None:
                response = {"detail": "stock document not found"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, doc_no, {}, 404, response)
                conn.execute("COMMIT")
                return 404, response
            if document["status"] == REVERSED:
                response = {"detail": "stock document already reversed"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, doc_no, {}, 409, response)
                conn.execute("COMMIT")
                return 409, response
            if document["status"] != POSTED:
                response = {"detail": f"cannot reverse stock document in status {document['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_REVERSE, doc_no, {}, 409, response)
                conn.execute("COMMIT")
                return 409, response

            warehouse_id, item_id = document["warehouse_id"], document["item_id"]
            quantity = document["quantity"]
            if document["direction"] == DIRECTION_IN:
                # 冲正入库单：账面扣回；若会使账面低于待入账出库占用则拒绝且保持原状态
                conn.execute(
                    "UPDATE stock_balances SET book_qty = book_qty - ?"
                    " WHERE tenant=? AND warehouse_id=? AND item_id=? AND book_qty - ? >= occupied_qty",
                    (quantity, tenant, warehouse_id, item_id, quantity),
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    response = {"detail": "reversal would drive book stock below occupied quantity"}
                    _save_idempotent(conn, tenant, request_id, OP_REVERSE, doc_no, {}, 409, response)
                    conn.execute("COMMIT")
                    return 409, response
            else:
                # 冲正出库单：数量加回账面
                conn.execute(
                    "UPDATE stock_balances SET book_qty = book_qty + ?"
                    " WHERE tenant=? AND warehouse_id=? AND item_id=?",
                    (quantity, tenant, warehouse_id, item_id),
                )
            conn.execute(
                "UPDATE stock_documents SET status='reversed', updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND doc_no=?",
                (tenant, doc_no),
            )
            return _finish(conn, tenant, doc_no, request_id, OP_REVERSE)
        except StockConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get(tenant: str, doc_no: str) -> dict | None:
    conn = connect()
    try:
        row = _get_document_conn(conn, tenant, doc_no)
    finally:
        conn.close()
    return _row_to_document(row) if row is not None else None


def balance(tenant: str, warehouse_id: str, item_id: str) -> dict:
    """按仓库与商品读取余额；未发生入库的余额为零。"""
    conn = connect()
    try:
        row = _get_balance_conn(conn, tenant, warehouse_id, item_id)
    finally:
        conn.close()
    return _balance_view(row, warehouse_id, item_id)
