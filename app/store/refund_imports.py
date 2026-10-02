"""退款单批量导入：逐行受理、部分成功、幂等重放与断点续跑。

每行在独立的 BEGIN IMMEDIATE 事务内判定并落检查点：成功行按单笔受理规则写入
refunds（真正占用可退余额），失败行只在 refund_import_rows 记录原因、不留任何
退款单写入。成功行本身即 refunds 中的 pending 行，故续跑时可退余额的重算
（待处理占用之和）天然包含此前已生效行，无需另设批次内累计。
"""
import hashlib
import json
import sqlite3

from app.store.db import connect

OP_IMPORT = "refund_import"

ACCEPTED = "accepted"
REJECTED = "rejected"

# 可区分的逐行失败原因
ERR_INVALID_LINE = "invalid_line"          # 行结构/标识非法
ERR_INVALID_AMOUNT = "invalid_amount"      # 金额非正整数
ERR_ORDER_NOT_FOUND = "order_not_found"    # 订单不存在或跨租户
ERR_DUPLICATE = "duplicate_refund"         # （订单，退款单）已受理
ERR_EXCEEDS_BALANCE = "exceeds_refundable_balance"  # 超过当前可退余额


class RefundImportConflict(Exception):
    """批次请求标识冲突（同标识改作其他批次/对象），对应 HTTP 409。"""


def _fingerprint(rows: list) -> str:
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _is_int(value) -> bool:
    # bool 是 int 的子类，金额字段明确拒绝
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _echo_line(position: int, line: dict) -> dict:
    return {
        "position": position,
        "order_id": line.get("order_id"),
        "refund_id": line.get("refund_id"),
        "amount_cents": line.get("amount_cents"),
    }


def _result_row(row: sqlite3.Row) -> dict:
    return json.loads(row["result_json"])


def _snapshot(conn: sqlite3.Connection, tenant: str, request_id: str, status: str,
              fingerprint: str | None = None, payload_json: str | None = None) -> dict:
    rows = conn.execute(
        "SELECT result_json FROM refund_import_rows WHERE tenant=? AND request_id=? ORDER BY position",
        (tenant, request_id),
    ).fetchall()
    items = [_result_row(r) for r in rows]
    accepted = sum(1 for r in items if r["outcome"] == ACCEPTED)
    total = len(json.loads(payload_json)) if payload_json is not None else len(items)
    return {
        "request_id": request_id,
        "status": status,
        "total": total,
        "accepted_count": accepted,
        "rejected_count": len(items) - accepted,
        "rows": items,
    }


def _record_row(
    conn: sqlite3.Connection, tenant: str, request_id: str, position: int,
    line: dict, outcome: str, error_code: str | None, reason: str | None,
    refund: dict | None, order_id: str | None, refund_id: str | None, amount,
) -> dict:
    result = _echo_line(position, line)
    result["outcome"] = outcome
    if refund is not None:
        result["refund"] = refund
    if error_code is not None:
        result["error_code"] = error_code
        result["reason"] = reason
    conn.execute(
        "INSERT INTO refund_import_rows(tenant, request_id, position, order_id, refund_id,"
        " amount_cents, outcome, reason, result_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        (tenant, request_id, position, order_id, refund_id,
         amount if _is_int(amount) else None, outcome, reason,
         json.dumps(result, ensure_ascii=False)),
    )
    return result


def _process_line(conn: sqlite3.Connection, tenant: str, request_id: str,
                  position: int, line: dict) -> None:
    """在已开启的写事务内判定一行；业务拒绝落 rejected 检查点，绝不向外抛业务错误。"""
    order_id = line.get("order_id") if isinstance(line, dict) else None
    refund_id = line.get("refund_id") if isinstance(line, dict) else None
    amount = line.get("amount_cents") if isinstance(line, dict) else None

    if not _is_text(order_id) or not _is_text(refund_id):
        _record_row(conn, tenant, request_id, position, line, REJECTED,
                    ERR_INVALID_LINE, "order_id and refund_id are required non-empty strings",
                    None, None, None, amount)
        return
    if not _is_int(amount) or amount <= 0:
        _record_row(conn, tenant, request_id, position, line, REJECTED,
                    ERR_INVALID_AMOUNT, "amount_cents must be a positive integer",
                    None, order_id, refund_id, amount)
        return

    order = conn.execute(
        "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if order is None:
        _record_row(conn, tenant, request_id, position, line, REJECTED,
                    ERR_ORDER_NOT_FOUND, "order not found",
                    None, order_id, refund_id, amount)
        return

    existing = conn.execute(
        "SELECT 1 FROM refunds WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()
    if existing is not None:
        _record_row(conn, tenant, request_id, position, line, REJECTED,
                    ERR_DUPLICATE, "refund already accepted",
                    None, order_id, refund_id, amount)
        return

    # 与单笔受理完全一致的守恒式：待处理占用之和（含本批此前已生效行）+ 本行 <= 已收
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS pending_sum FROM refunds"
        " WHERE tenant=? AND order_id=? AND status='pending'",
        (tenant, order_id),
    ).fetchone()
    if row["pending_sum"] + amount > order["paid_cents"]:
        _record_row(conn, tenant, request_id, position, line, REJECTED,
                    ERR_EXCEEDS_BALANCE, "refund exceeds refundable balance",
                    None, order_id, refund_id, amount)
        return

    conn.execute(
        "INSERT INTO refunds(tenant, order_id, refund_id, amount_cents, status,"
        " effective_deduction_cents, created_at, updated_at)"
        " VALUES(?,?,?,?,'pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (tenant, order_id, refund_id, amount),
    )
    saved = conn.execute(
        "SELECT order_id, refund_id, amount_cents, status, effective_deduction_cents FROM refunds"
        " WHERE tenant=? AND order_id=? AND refund_id=?",
        (tenant, order_id, refund_id),
    ).fetchone()
    refund = {
        "order_id": saved["order_id"],
        "refund_id": saved["refund_id"],
        "amount_cents": saved["amount_cents"],
        "status": saved["status"],
        "effective_deduction_cents": saved["effective_deduction_cents"],
    }
    _record_row(conn, tenant, request_id, position, line, ACCEPTED, None, None,
                refund, order_id, refund_id, amount)


def submit(tenant: str, request_id: str, rows: list[dict]) -> tuple[int, dict]:
    """提交（或续跑）一个导入批次。返回 (http_status, response)。"""
    fingerprint = _fingerprint(rows)
    payload_json = json.dumps(rows, ensure_ascii=False)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            batch = conn.execute(
                "SELECT fingerprint, status, http_status, response_json FROM refund_import_batches"
                " WHERE tenant=? AND request_id=?",
                (tenant, request_id),
            ).fetchone()
            if batch is not None:
                if batch["fingerprint"] != fingerprint:
                    raise RefundImportConflict(
                        "request_id was already used for a different import batch")
                if batch["status"] == "completed":
                    conn.execute("ROLLBACK")
                    return batch["http_status"], json.loads(batch["response_json"])
            else:
                # 与单笔退款受理共享 request_id 命名空间：同标识改作单笔操作冲突
                clash = conn.execute(
                    "SELECT 1 FROM refund_requests WHERE tenant=? AND request_id=?",
                    (tenant, request_id),
                ).fetchone()
                if clash is not None:
                    raise RefundImportConflict(
                        "request_id was already used for a different operation")
                conn.execute(
                    "INSERT INTO refund_import_batches(tenant, request_id, fingerprint, payload_json,"
                    " status, created_at, updated_at) VALUES(?,?,?,?,'in_progress',"
                    "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                    (tenant, request_id, fingerprint, payload_json),
                )
            conn.execute("COMMIT")
        except RefundImportConflict:
            conn.execute("ROLLBACK")
            raise

        # 逐行独立事务：已判定行（含中断前）从检查点跳过，未判定行才受理
        for position, line in enumerate(rows):
            conn.execute("BEGIN IMMEDIATE")
            try:
                already = conn.execute(
                    "SELECT 1 FROM refund_import_rows WHERE tenant=? AND request_id=? AND position=?",
                    (tenant, request_id, position),
                ).fetchone()
                if already is None:
                    _process_line(conn, tenant, request_id, position, line)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        # 全部行判定完毕：汇总结果并终结批次（同事务原子生效）
        conn.execute("BEGIN IMMEDIATE")
        try:
            response = _snapshot(conn, tenant, request_id, "completed",
                                 payload_json=payload_json)
            conn.execute(
                "UPDATE refund_import_batches SET status='completed', http_status=200,"
                " response_json=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE tenant=? AND request_id=?",
                (json.dumps(response, ensure_ascii=False), tenant, request_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return 200, response
    finally:
        conn.close()


def get(tenant: str, request_id: str) -> dict | None:
    """按批次请求标识读取结果；跨租户/不存在按 None（404）处理。"""
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT status, payload_json, http_status, response_json FROM refund_import_batches"
            " WHERE tenant=? AND request_id=?",
            (tenant, request_id),
        ).fetchone()
        if batch is None:
            return None
        if batch["status"] == "completed":
            return json.loads(batch["response_json"])
        # 中断态：返回当前检查点快照（已判定行按提交顺序排列）
        return _snapshot(conn, tenant, request_id, "in_progress",
                         payload_json=batch["payload_json"])
    finally:
        conn.close()


def request_exists(conn: sqlite3.Connection, tenant: str, request_id: str) -> sqlite3.Row | None:
    """供单笔退款链路反查：request_id 是否已被批量导入占用。"""
    return conn.execute(
        "SELECT 1 FROM refund_import_batches WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()
