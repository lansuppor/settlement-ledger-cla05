import csv
import io
import re
import sqlite3

from app.rules import order_rules
from app.store.db import connect

IN_PROGRESS = "in_progress"
COMPLETED = "completed"
COMPLETED_WITH_ERRORS = "completed_with_errors"

SUCCESS = "success"
INVALID_PARAM = "invalid_param"
ORDER_CONFLICT = "order_conflict"

CSV_HEADER = ["tenant", "order_id", "amount_cents", "currency"]
_AMOUNT_RE = re.compile(r"[0-9]+")


class BatchError(Exception):
    pass


class CSVFormatError(BatchError):
    """CSV 整批非法（缺表头、列数不符、编码/结构损坏），不受理任何行。"""


def _parse_csv(text: str) -> list[tuple[int, list[str]]]:
    """严格解析 CSV，返回(物理行号, 字段列表)的数据行；任何结构问题整批拒绝。"""
    text = text.removeprefix("﻿")
    try:
        records: list[tuple[int, list[str]]] = []
        reader = csv.reader(io.StringIO(text), strict=True)
        for row in reader:
            records.append((reader.line_num, row))
    except csv.Error as exc:
        raise CSVFormatError(f"malformed CSV structure: {exc}")
    if not records:
        raise CSVFormatError("missing CSV header")
    if records[0][1] != CSV_HEADER:
        raise CSVFormatError("CSV header must be exactly: tenant,order_id,amount_cents,currency")
    data_rows = records[1:]
    for line_no, row in data_rows:
        if len(row) != len(CSV_HEADER):
            raise CSVFormatError(f"line {line_no}: expected {len(CSV_HEADER)} columns, got {len(row)}")
    return data_rows


def _validate_row(tenant: str, fields: list[str]) -> tuple[str, str] | None:
    """逐行业务校验。返回(错误码, 错误信息)；通过返回 None。"""
    row_tenant, order_id, amount_raw, currency = fields
    if row_tenant != tenant:
        return INVALID_PARAM, "row tenant does not match request tenant"
    if not order_id:
        return INVALID_PARAM, "order_id is required"
    if not _AMOUNT_RE.fullmatch(amount_raw) or int(amount_raw) <= 0:
        return INVALID_PARAM, "amount_cents must be a positive integer"
    try:
        order_rules.assert_currency(currency)
    except ValueError:
        return INVALID_PARAM, "unsupported currency"
    return None


def _handle_row(conn: sqlite3.Connection, tenant: str, fields: list[str]) -> tuple[str, str, str]:
    """在调用方事务内处理一行，返回(结果码, order_id, 错误信息)。不提交。"""
    order_id = fields[1]
    error = _validate_row(tenant, fields)
    if error is not None:
        return error[0], order_id, error[1]
    amount_cents = int(fields[2])
    currency = fields[3]
    existing = conn.execute(
        "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if existing is not None:
        # 已受理订单（含批次内前序行或单笔受理）：不重复受理、不改变既有数据。
        return ORDER_CONFLICT, order_id, "order already accepted"
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
            "VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    except sqlite3.IntegrityError:
        # 并发写入下的防御性分支（写事务已以 BEGIN IMMEDIATE 串行，正常不会走到）。
        return ORDER_CONFLICT, order_id, "order already accepted"
    return SUCCESS, order_id, ""


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def process(
    tenant: str,
    batch_id: str,
    csv_text: str,
    crash_after: int | None = None,
) -> tuple[dict, bool]:
    """受理一个批次。返回(批次结果, 批次身份是否此前已存在)。

    业务身份仅为（租户, batch_id），与行内容、订单标识无关：
    - 身份不存在：CSV 格式非法抛 CSVFormatError，不创建批次、不受理任何行；
      否则以原始 CSV 存档建批，逐行受理。
    - 身份已存在且终态：直接返回既有处理结果与计数，绝不新建/修改任何订单，
      不重新校验、不重新受理（行内容不同也不新建批次）。
    - 身份已存在但中断于 in_progress：以存档 CSV 从断点继续，已提交行不重复处理，
      最终结果与一次连续处理完全一致。
    每行与订单写入在同一独立事务内提交，已提交行不回滚、不留半张单据。
    crash_after 仅用于测试中断续跑：成功提交指定行数后模拟崩溃。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT status, total, processed, input_text FROM batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if existing is not None:
            existed, resume = True, existing["status"] == IN_PROGRESS
            start_seq = existing["processed"] if resume else 0
            rows = _parse_csv(existing["input_text"]) if resume else []
        else:
            # 仅在身份不存在时才校验本次 CSV；解析失败则回滚，批次与订单都不落库。
            rows = _parse_csv(csv_text)
            conn.execute(
                "INSERT INTO batches(tenant, batch_id, status, total, processed, input_text) "
                "VALUES(?,?,?,?,0,?)",
                (tenant, batch_id, IN_PROGRESS, len(rows), csv_text),
            )
            existed, resume, start_seq = False, False, 0
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()

    if existed and not resume:
        return get_result(tenant, batch_id), True

    for seq, (line_no, fields) in enumerate(rows[start_seq:], start=start_seq):
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 断点检查在事务内完成，并发续跑下同一行只会有一个执行者真正处理。
            processed = conn.execute(
                "SELECT processed FROM batches WHERE tenant=? AND batch_id=?",
                (tenant, batch_id),
            ).fetchone()["processed"]
            if seq < processed:
                conn.execute("COMMIT")
                continue
            outcome, order_id, message = _handle_row(conn, tenant, fields)
            conn.execute(
                "INSERT INTO batch_lines(tenant, batch_id, seq, line_no, order_id, outcome, message) "
                "VALUES(?,?,?,?,?,?,?)",
                (tenant, batch_id, seq, line_no, order_id, outcome, message),
            )
            conn.execute(
                "UPDATE batches SET processed=processed+1 WHERE tenant=? AND batch_id=?",
                (tenant, batch_id),
            )
            conn.execute("COMMIT")
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()
        if crash_after is not None and seq + 1 >= crash_after:
            raise RuntimeError("simulated interruption after committed rows")

    _finalize(tenant, batch_id)
    return get_result(tenant, batch_id), existed


def _finalize(tenant: str, batch_id: str) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        failure_count = conn.execute(
            "SELECT COUNT(*) AS n FROM batch_lines WHERE tenant=? AND batch_id=? AND outcome!=?",
            (tenant, batch_id, SUCCESS),
        ).fetchone()["n"]
        status = COMPLETED_WITH_ERRORS if failure_count else COMPLETED
        conn.execute(
            "UPDATE batches SET status=? WHERE tenant=? AND batch_id=?",
            (status, tenant, batch_id),
        )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def get_result(tenant: str, batch_id: str) -> dict | None:
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT tenant, batch_id, status, total, processed FROM batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if batch is None:
            return None
        lines = conn.execute(
            "SELECT line_no, order_id, outcome, message FROM batch_lines "
            "WHERE tenant=? AND batch_id=? ORDER BY seq",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    errors = [
        {"line_no": line["line_no"], "code": line["outcome"], "message": line["message"]}
        for line in lines
        if line["outcome"] != SUCCESS
    ]
    accepted_order_ids = [line["order_id"] for line in lines if line["outcome"] == SUCCESS]
    return {
        "tenant": batch["tenant"],
        "batch_id": batch["batch_id"],
        "status": batch["status"],
        "total": batch["total"],
        "success_count": len(accepted_order_ids),
        "failure_count": len(errors),
        "errors": errors,
        "accepted_order_ids": accepted_order_ids,
    }
