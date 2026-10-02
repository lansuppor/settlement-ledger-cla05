import json
import sqlite3

from app.store.db import connect

# 工单状态机：
#   accepted --process--> processing --review--> review
#   review --reprocess--> processing
#   processing|review --resolve--> resolved（终态，落裁决金额）
#   accepted|processing|review|resolved --revoke--> revoked（终态）
ACCEPTED = "accepted"
PROCESSING = "processing"
REVIEW = "review"
RESOLVED = "resolved"
REVOKED = "revoked"

ACTIVE_STATUSES = (ACCEPTED, PROCESSING, REVIEW)
TERMINAL_STATUSES = (RESOLVED, REVOKED)

OP_ACCEPT = "accept"
OP_PROCESS = "process"
OP_REVIEW = "review"
OP_REPROCESS = "reprocess"
OP_RESOLVE = "resolve"
OP_REVOKE = "revoke"

# 各推进操作允许的来源状态
_ALLOWED_FROM = {
    OP_PROCESS: (ACCEPTED,),
    OP_REVIEW: (PROCESSING,),
    OP_REPROCESS: (REVIEW,),
    OP_RESOLVE: (PROCESSING, REVIEW),
    OP_REVOKE: (ACCEPTED, PROCESSING, REVIEW, RESOLVED),
}


class TicketConflict(Exception):
    """业务规则冲突，对应 HTTP 409。"""


def _row_to_ticket(row: sqlite3.Row) -> dict:
    # 当前生效扣减：仅已解决工单按裁决金额生效；撤销后清零
    effective = row["award_cents"] if row["status"] == RESOLVED else 0
    return {
        "ticket_id": row["ticket_id"],
        "status": row["status"],
        "request_amount_cents": row["request_amount_cents"],
        "initiator": row["initiator"],
        "effective_deduction_cents": effective,
    }


def _load_idempotent(conn: sqlite3.Connection, tenant: str, request_id: str) -> tuple | None:
    row = conn.execute(
        "SELECT op, order_id, refund_id, ticket_id, http_status, response_json FROM ticket_requests"
        " WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
    if row is None:
        return None
    return (
        row["op"],
        row["order_id"],
        row["refund_id"],
        row["ticket_id"],
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
    ticket_id: str,
    http_status: int,
    response: dict,
) -> None:
    conn.execute(
        "INSERT INTO ticket_requests(tenant, request_id, op, order_id, refund_id, ticket_id,"
        " http_status, response_json, created_at) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, op, order_id, refund_id, ticket_id, http_status, json.dumps(response, ensure_ascii=False)),
    )


def _replay_or_none(
    conn: sqlite3.Connection,
    tenant: str,
    request_id: str,
    op: str,
    order_id: str,
    refund_id: str,
    ticket_id: str,
) -> tuple[int, dict] | None:
    """命中幂等记录则返回首次结果；同 request_id 指向不同操作/对象则冲突。"""
    recorded = _load_idempotent(conn, tenant, request_id)
    if recorded is None:
        return None
    saved_op, saved_order, saved_refund, saved_ticket, http_status, response = recorded
    if (saved_op, saved_order, saved_refund, saved_ticket) != (op, order_id, refund_id, ticket_id):
        raise TicketConflict("request_id was already used for a different operation")
    return http_status, response


def _get_ticket_conn(
    conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str, ticket_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ticket_id, request_amount_cents, initiator, status, award_cents FROM refund_tickets"
        " WHERE tenant=? AND order_id=? AND refund_id=? AND ticket_id=?",
        (tenant, order_id, refund_id, ticket_id),
    ).fetchone()


def _get_refund_conn(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT amount_cents, status, effective_deduction_cents FROM refunds"
        " WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()


def _active_ticket_exists_conn(
    conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM refund_tickets WHERE tenant=? AND order_id=? AND refund_id=?"
        " AND status IN ('accepted','processing','review') LIMIT 1",
        (tenant, order_id, refund_id),
    ).fetchone()
    return row is not None


def active_ticket_exists(conn: sqlite3.Connection, tenant: str, order_id: str, refund_id: str) -> bool:
    """供退款单链路在同一事务内判断处理中标记。"""
    return _active_ticket_exists_conn(conn, tenant, order_id, refund_id)


def accept(
    tenant: str,
    order_id: str,
    refund_id: str,
    ticket_id: str,
    request_amount_cents: int,
    initiator: str,
    reason: str,
    request_id: str,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, ticket_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            refund = _get_refund_conn(conn, tenant, order_id, refund_id)
            if refund is None:
                response = {"detail": "refund not found"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, ticket_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            existing = _get_ticket_conn(conn, tenant, order_id, refund_id, ticket_id)
            if existing is not None:
                response = {"detail": "ticket already accepted"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, ticket_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 已撤销/已冲正的退款单不可再被工单受理（已完成与待处理可受理）
            if refund["status"] in ("cancelled", "reversed"):
                response = {"detail": f"cannot open ticket for refund in status {refund['status']}"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, ticket_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            # 一张退款单最多一张进行中工单；先做显式检查，部分唯一索引为并发兜底
            if _active_ticket_exists_conn(conn, tenant, order_id, refund_id):
                response = {"detail": "refund already has an in-progress ticket"}
                _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, ticket_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            try:
                conn.execute(
                    "INSERT INTO refund_tickets(tenant, order_id, refund_id, ticket_id, request_amount_cents,"
                    " initiator, reason, status, award_cents, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,'accepted',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                    (tenant, order_id, refund_id, ticket_id, request_amount_cents, initiator, reason),
                )
            except sqlite3.IntegrityError as error:
                if "UNIQUE" in str(error):
                    raise TicketConflict("refund already has an in-progress ticket")
                raise

            ticket = _row_to_ticket(_get_ticket_conn(conn, tenant, order_id, refund_id, ticket_id))
            _save_idempotent(conn, tenant, request_id, OP_ACCEPT, order_id, refund_id, ticket_id, 201, ticket)
            conn.execute("COMMIT")
            return 201, ticket
        except TicketConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _advance(
    tenant: str,
    order_id: str,
    refund_id: str,
    ticket_id: str,
    request_id: str,
    op: str,
    award_cents: int | None = None,
) -> tuple[int, dict]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay_or_none(conn, tenant, request_id, op, order_id, refund_id, ticket_id)
            if replay is not None:
                conn.execute("ROLLBACK")
                return replay

            ticket = _get_ticket_conn(conn, tenant, order_id, refund_id, ticket_id)
            if ticket is None:
                response = {"detail": "ticket not found"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, ticket_id, 404, response)
                conn.execute("COMMIT")
                return 404, response

            if ticket["status"] not in _ALLOWED_FROM[op]:
                response = {"detail": f"cannot {op} ticket in status {ticket['status']}"}
                _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, ticket_id, 409, response)
                conn.execute("COMMIT")
                return 409, response

            if op == OP_RESOLVE:
                refund = _get_refund_conn(conn, tenant, order_id, refund_id)
                award = award_cents or 0
                # 裁决金额为正整数，且不超过处理请求金额与退款单当前金额
                if award <= 0 or award > ticket["request_amount_cents"] or award > refund["amount_cents"]:
                    response = {"detail": "award amount is invalid or exceeds allowed amount"}
                    _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, ticket_id, 409, response)
                    conn.execute("COMMIT")
                    return 409, response
                # 扣减退款单金额；已完成单的生效扣减同步为扣减后金额，订单金额不变
                conn.execute(
                    "UPDATE refunds SET amount_cents = amount_cents - ?,"
                    " effective_deduction_cents = CASE WHEN status='completed' THEN amount_cents - ? ELSE 0 END,"
                    " updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=?",
                    (award, award, tenant, order_id, refund_id),
                )
                conn.execute(
                    "UPDATE refund_tickets SET status='resolved', award_cents=?, updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND ticket_id=?",
                    (award, tenant, order_id, refund_id, ticket_id),
                )
            elif op == OP_REVOKE:
                if ticket["status"] == RESOLVED:
                    award = ticket["award_cents"]
                    # 已解决撤销一次：裁决金额加回退款单，生效扣减同步恢复，两侧同事务原子生效
                    conn.execute(
                        "UPDATE refunds SET amount_cents = amount_cents + ?,"
                        " effective_deduction_cents = CASE WHEN status='completed' THEN amount_cents + ? ELSE 0 END,"
                        " updated_at=CURRENT_TIMESTAMP"
                        " WHERE tenant=? AND order_id=? AND refund_id=?",
                        (award, award, tenant, order_id, refund_id),
                    )
                # 进行中撤销只释放处理中标记，不扣减金额
                conn.execute(
                    "UPDATE refund_tickets SET status='revoked', award_cents=0, updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND ticket_id=?",
                    (tenant, order_id, refund_id, ticket_id),
                )
            else:
                target = {OP_PROCESS: PROCESSING, OP_REVIEW: REVIEW, OP_REPROCESS: PROCESSING}[op]
                conn.execute(
                    "UPDATE refund_tickets SET status=?, updated_at=CURRENT_TIMESTAMP"
                    " WHERE tenant=? AND order_id=? AND refund_id=? AND ticket_id=?",
                    (target, tenant, order_id, refund_id, ticket_id),
                )

            result = _row_to_ticket(_get_ticket_conn(conn, tenant, order_id, refund_id, ticket_id))
            _save_idempotent(conn, tenant, request_id, op, order_id, refund_id, ticket_id, 200, result)
            conn.execute("COMMIT")
            return 200, result
        except TicketConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def process(tenant: str, order_id: str, refund_id: str, ticket_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, ticket_id, request_id, OP_PROCESS)


def review(tenant: str, order_id: str, refund_id: str, ticket_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, ticket_id, request_id, OP_REVIEW)


def reprocess(tenant: str, order_id: str, refund_id: str, ticket_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, ticket_id, request_id, OP_REPROCESS)


def resolve(
    tenant: str, order_id: str, refund_id: str, ticket_id: str, award_cents: int, request_id: str
) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, ticket_id, request_id, OP_RESOLVE, award_cents)


def revoke(tenant: str, order_id: str, refund_id: str, ticket_id: str, request_id: str) -> tuple[int, dict]:
    return _advance(tenant, order_id, refund_id, ticket_id, request_id, OP_REVOKE)


def get(tenant: str, order_id: str, refund_id: str, ticket_id: str) -> dict | None:
    conn = connect()
    try:
        row = _get_ticket_conn(conn, tenant, order_id, refund_id, ticket_id)
    finally:
        conn.close()
    return _row_to_ticket(row) if row is not None else None


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
            "SELECT ticket_id, request_amount_cents, initiator, status, award_cents FROM refund_tickets"
            " WHERE tenant=? AND order_id=? AND refund_id=? ORDER BY rowid",
            (tenant, order_id, refund_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_ticket(row) for row in rows]
