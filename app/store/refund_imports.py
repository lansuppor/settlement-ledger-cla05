import hashlib
import json
import sqlite3

from app.store import refunds
from app.store.db import connect

# 批量导入：一批待受理退款单，逐行独立判定、部分成功。
# 成功行按 refunds.accept 同一规则落库（占用可退余额）；失败行不写退款单。
OP_IMPORT = "import"

ACCEPTED = "accepted"
REJECTED = "rejected"

# 行内失败原因码：与单笔受理规则一一对应，另含批量特有的金额非法/行格式错/行内重复
INVALID_LINE = "invalid_line"
INVALID_AMOUNT = "invalid_amount"
ORDER_NOT_FOUND = "order_not_found"
DUPLICATE_ACCEPTANCE = "duplicate_acceptance"
EXCEEDS_REFUNDABLE = "exceeds_refundable_balance"
DUPLICATE_LINE = "duplicate_line"

REASON_DETAIL = {
    INVALID_LINE: "order_id and refund_id are required",
    INVALID_AMOUNT: "amount_cents must be a positive integer",
    ORDER_NOT_FOUND: "order not found",
    DUPLICATE_ACCEPTANCE: "refund already accepted",
    EXCEEDS_REFUNDABLE: "refund exceeds refundable balance",
    DUPLICATE_LINE: "duplicate (order_id, refund_id) line within the same batch",
}


class RefundImportConflict(Exception):
    """批次请求标识冲突（同标识提交了不同批次内容），对应 HTTP 409。"""


def _fingerprint(lines: list[dict]) -> str:
    # 规范化为 [order_id, refund_id, amount_json]，金额保持提交时的 JSON 形态（非法值也参与）
    payload = json.dumps(
        [[line.get("order_id"), line.get("refund_id"), line.get("amount_cents")] for line in lines],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_to_line(row: sqlite3.Row) -> dict:
    return {
        "line_no": row["line_no"],
        "order_id": row["order_id"],
        "refund_id": row["refund_id"],
        # 原样回显提交金额：合法整数回显整数，非法值（字符串/小数/None 等）保持提交形态
        "amount_cents": json.loads(row["amount_raw"]),
        "conclusion": row["conclusion"],
        **({"reason": row["reason"], "detail": REASON_DETAIL[row["reason"]]} if row["reason"] else {}),
    }


def _load_batch(conn: sqlite3.Connection, tenant: str, request_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT request_id, fingerprint, succeeded_count, failed_count, response_json, completed"
        " FROM refund_imports WHERE tenant=? AND request_id=?",
        (tenant, request_id),
    ).fetchone()


def _load_rows(conn: sqlite3.Connection, tenant: str, request_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT line_no, order_id, refund_id, amount_raw, conclusion, reason"
        " FROM refund_import_rows WHERE tenant=? AND request_id=? ORDER BY line_no",
        (tenant, request_id),
    ).fetchall()


def _response(rows: list[dict], request_id: str) -> dict:
    succeeded = sum(1 for line in rows if line["conclusion"] == ACCEPTED)
    return {
        "request_id": request_id,
        "succeeded_count": succeeded,
        "failed_count": len(rows) - succeeded,
        "lines": rows,
    }


def submit(tenant: str, request_id: str, raw_lines: list[dict]) -> tuple[int, dict]:
    """提交（或续跑）一批退款单受理，全程一个写事务。

    每行独立判定；成功行落 refunds（占用可退余额），失败行仅记录结论。
    同一 request_id 重放：内容指纹一致 -> 返回首次完整结果；内容不一致 -> 409。
    断点续跑：只补判尚无结论的行；已生效行不重复受理，已判失败行原因不变。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            batch = _load_batch(conn, tenant, request_id)
            fingerprint = _fingerprint(raw_lines)
            if batch is not None and batch["fingerprint"] != fingerprint:
                raise RefundImportConflict(
                    "request_id was already used for a different refund import batch"
                )
            if batch is None:
                conn.execute(
                    "INSERT INTO refund_imports(tenant, request_id, fingerprint, succeeded_count,"
                    " failed_count, response_json, completed, created_at)"
                    " VALUES(?,?,?,0,0,'',0,CURRENT_TIMESTAMP)",
                    (tenant, request_id, fingerprint),
                )

            decided = _load_rows(conn, tenant, request_id)
            decided_lines = {row["line_no"]: row for row in decided}

            # 行内（订单标识，退款单标识）重复：以提交顺序，首个通过格式校验的行按正常规则判定
            # （无论受理成败都占位），其后判 duplicate_line。断点续跑时从已判行重建同一集合。
            non_occupying = {DUPLICATE_LINE, INVALID_AMOUNT, INVALID_LINE}
            seen: dict[tuple[str, str], int] = {}
            for row in decided:
                if row["reason"] not in non_occupying:
                    seen.setdefault((row["order_id"], row["refund_id"]), row["line_no"])

            def record(
                line_no: int, order_id: str, refund_id: str, amount: object,
                conclusion: str, reason: str | None,
            ) -> None:
                conn.execute(
                    "INSERT INTO refund_import_rows(tenant, request_id, line_no, order_id, refund_id,"
                    " amount_raw, conclusion, reason, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (tenant, request_id, line_no, order_id, refund_id,
                     json.dumps(amount, ensure_ascii=False), conclusion, reason),
                )

            for idx, raw in enumerate(raw_lines, start=1):
                if idx in decided_lines:
                    # 断点续跑：已判行原样保留，不重新判定
                    continue
                order_value = raw.get("order_id")
                refund_value = raw.get("refund_id")
                amount = raw.get("amount_cents")
                if not isinstance(order_value, str) or not order_value or not isinstance(refund_value, str) \
                        or not refund_value:
                    record(idx, str(order_value or ""), str(refund_value or ""), amount, REJECTED, INVALID_LINE)
                    continue
                order_id, refund_id = order_value, refund_value
                key = (order_id, refund_id)

                if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                    record(idx, order_id, refund_id, amount, REJECTED, INVALID_AMOUNT)
                    continue
                if key in seen:
                    record(idx, order_id, refund_id, amount, REJECTED, DUPLICATE_LINE)
                    continue
                seen[key] = idx

                reason, _refund = refunds._accept_conn(conn, tenant, order_id, refund_id, amount)
                if reason == "ok":
                    record(idx, order_id, refund_id, amount, ACCEPTED, None)
                else:
                    code = {
                        "order_not_found": ORDER_NOT_FOUND,
                        "duplicate_refund": DUPLICATE_ACCEPTANCE,
                        "exceeds_refundable_balance": EXCEEDS_REFUNDABLE,
                    }[reason]
                    record(idx, order_id, refund_id, amount, REJECTED, code)

            all_rows = [_row_to_line(row) for row in _load_rows(conn, tenant, request_id)]
            response = _response(all_rows, request_id)
            conn.execute(
                "UPDATE refund_imports SET succeeded_count=?, failed_count=?, response_json=?, completed=1"
                " WHERE tenant=? AND request_id=?",
                (response["succeeded_count"], response["failed_count"],
                 json.dumps(response, ensure_ascii=False), tenant, request_id),
            )
            conn.execute("COMMIT")
            return 200, response
        except RefundImportConflict:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def get(tenant: str, request_id: str) -> dict | None:
    conn = connect()
    try:
        batch = _load_batch(conn, tenant, request_id)
        if batch is None or not batch["completed"]:
            return None
        return json.loads(batch["response_json"])
    finally:
        conn.close()
