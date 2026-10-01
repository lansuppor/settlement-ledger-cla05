import hashlib
import sqlite3

from app.store.db import connect

SUCCESS = "success"
FAILED = "failed"

IN_PROGRESS = "in_progress"
COMPLETED = "completed"
COMPLETED_WITH_ERRORS = "completed_with_errors"

INVALID_PARAMETER = "invalid_parameter"
ORDER_CONFLICT = "order_conflict"


class BatchError(Exception):
    pass


class InputMismatch(BatchError):
    """同一批次身份在进行中被换成另一份输入续跑。"""


class OrderAlreadyExists(BatchError):
    pass


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _header(conn, tenant: str, batch_id: str):
    return conn.execute(
        "SELECT tenant, batch_id, total, fingerprint, status FROM batches WHERE tenant=? AND batch_id=?",
        (tenant, batch_id),
    ).fetchone()


def _header_dict(row) -> dict:
    return {
        "tenant": row["tenant"],
        "batch_id": row["batch_id"],
        "total": row["total"],
        "fingerprint": row["fingerprint"],
        "status": row["status"],
    }


def ensure(tenant: str, batch_id: str, total: int, raw: bytes) -> tuple[dict, str]:
    """返回(批次头, 形态)，形态为 created / resume / terminal。

    批次身份只认（租户, batch_id）：头已存在时绝不新建。进行中的批次只接受同一份输入
    （原始字节指纹与行数都一致）续跑；终态批次对任何输入都只回放既有结果。
    """
    fingerprint = hashlib.sha256(raw).hexdigest()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _header(conn, tenant, batch_id)
        if row is None:
            conn.execute(
                "INSERT INTO batches(tenant, batch_id, total, fingerprint, status) VALUES(?,?,?,?,'in_progress')",
                (tenant, batch_id, total, fingerprint),
            )
            conn.execute("COMMIT")
            return _header_dict(conn.execute(
                "SELECT tenant, batch_id, total, fingerprint, status FROM batches WHERE tenant=? AND batch_id=?",
                (tenant, batch_id),
            ).fetchone()), "created"
        header = _header_dict(row)
        if header["status"] != IN_PROGRESS:
            conn.execute("COMMIT")
            return header, "terminal"
        if header["fingerprint"] != fingerprint or header["total"] != total:
            raise InputMismatch("batch is in progress with a different input; resubmit the original CSV")
        conn.execute("COMMIT")
        return header, "resume"
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def recorded_lines(tenant: str, batch_id: str) -> dict[int, dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT line_no, order_id, outcome, error_code, error_message, accepted_order_id "
            "FROM batch_orders WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    return {row["line_no"]: dict(row) for row in rows}


def record_failure(
    tenant: str, batch_id: str, line_no: int, order_id: str, error_code: str, error_message: str
) -> None:
    """失败行只记错误清单，不触碰订单表；行记录本身即提交点，崩溃后不重记。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM batch_orders WHERE tenant=? AND batch_id=? AND line_no=?",
            (tenant, batch_id, line_no),
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO batch_orders(tenant, batch_id, line_no, order_id, outcome, error_code, error_message, "
                "accepted_order_id) VALUES(?,?,?,?, 'failed', ?, ?, NULL)",
                (tenant, batch_id, line_no, order_id, error_code, error_message),
            )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def record_success(
    tenant: str, batch_id: str, line_no: int, order_id: str, amount_cents: int, currency: str
) -> None:
    """订单写入与行结果在同一事务提交：要么同时生效，要么同时不留痕，保证续跑结论一致。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        line = conn.execute(
            "SELECT 1 FROM batch_orders WHERE tenant=? AND batch_id=? AND line_no=?",
            (tenant, batch_id, line_no),
        ).fetchone()
        if line is not None:
            conn.execute("COMMIT")
            return
        existing = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if existing is not None:
            raise OrderAlreadyExists("order already accepted")
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
            "VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
        conn.execute(
            "INSERT INTO batch_orders(tenant, batch_id, line_no, order_id, outcome, error_code, error_message, "
            "accepted_order_id) VALUES(?,?,?,?, 'success', NULL, NULL, ?)",
            (tenant, batch_id, line_no, order_id, order_id),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as error:
        # 并发下另一请求已受理同一订单：按既有订单冲突处理，不留行记录，续跑时会重新得出冲突。
        if "UNIQUE" in str(error):
            raise OrderAlreadyExists("order already accepted")
        raise
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def finish(tenant: str, batch_id: str) -> dict:
    """全部行落库后按错误数置终态；终态化本身幂等。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        header = _header(conn, tenant, batch_id)
        counted = conn.execute(
            "SELECT COUNT(*) AS n FROM batch_orders WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()["n"]
        if header is not None and counted >= header["total"]:
            failed = conn.execute(
                "SELECT COUNT(*) AS n FROM batch_orders WHERE tenant=? AND batch_id=? AND outcome='failed'",
                (tenant, batch_id),
            ).fetchone()["n"]
            status = COMPLETED_WITH_ERRORS if failed > 0 else COMPLETED
            conn.execute(
                "UPDATE batches SET status=? WHERE tenant=? AND batch_id=? AND status='in_progress'",
                (status, tenant, batch_id),
            )
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    return get(tenant, batch_id)


def _summarize(header: dict, rows: list[dict]) -> dict:
    succeeded = [r for r in rows if r["outcome"] == SUCCESS]
    failed = [r for r in rows if r["outcome"] == FAILED]
    failed.sort(key=lambda r: r["line_no"])
    succeeded.sort(key=lambda r: r["line_no"])
    return {
        "tenant": header["tenant"],
        "batch_id": header["batch_id"],
        "status": header["status"],
        "total": header["total"],
        "succeeded": len(succeeded),
        "failed": len(failed),
        "errors": [
            {
                "line_no": r["line_no"],
                "order_id": r["order_id"],
                "error_code": r["error_code"],
                "message": r["error_message"],
            }
            for r in failed
        ],
        "accepted_order_ids": [r["accepted_order_id"] for r in succeeded],
    }


def get(tenant: str, batch_id: str) -> dict | None:
    conn = connect()
    try:
        header = _header(conn, tenant, batch_id)
        if header is None:
            return None
        rows = conn.execute(
            "SELECT line_no, order_id, outcome, error_code, error_message, accepted_order_id "
            "FROM batch_orders WHERE tenant=? AND batch_id=? ORDER BY line_no",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    return _summarize(_header_dict(header), [dict(r) for r in rows])
