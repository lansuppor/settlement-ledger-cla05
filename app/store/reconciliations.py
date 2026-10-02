import json
import sqlite3

from app.store.db import connect

# 对账单状态：全部订单逐条衔接、末条余额等于当前已收且金额闭合为已核销；任一不满足为有差异。
RECONCILED = "reconciled"
DISCREPANCY = "discrepancy"

# 差异原因（逐张订单给出，可并存）：
REASON_BROKEN_CHAIN = "balance_chain_broken"        # 余额衔接断链
REASON_FINAL_MISMATCH = "final_balance_mismatch"    # 末条流水余额与当前对外已收不一致
REASON_AMOUNT_NOT_CLOSED = "amount_not_closed"      # 订单金额 ≠ 已收 + 未收

# 净已收 = 累计收款毛额 − 已批准未冲正退款；口径与订单读取、条件检索完全一致。
_NET_PAID_EXPR = "(o.paid_cents - COALESCE(r.approved_total,0))"


class ReconciliationError(Exception):
    pass


class InvalidScope(ReconciliationError):
    """对账范围非法（端点非法/下限大于上限）或范围下没有任何订单，对外按参数错误处理。"""


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _row_to_dict(row) -> dict:
    # 结论整体以 JSON 固化：重读得到的逐订单结果、差异清单与生成时逐字节同构。
    result = json.loads(row["result_json"])
    return {
        "tenant": row["tenant"],
        "reconcile_id": row["reconcile_id"],
        "status": row["status"],
        "order_count": row["order_count"],
        "discrepancy_count": row["discrepancy_count"],
        "scope": json.loads(row["scope_json"]),
        "reconciled_at": row["created_at"],
        "orders": result["orders"],
        "discrepancies": result["discrepancies"],
    }


def get(tenant: str, reconcile_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, reconcile_id, status, order_count, discrepancy_count, scope_json, "
            "result_json, created_at FROM reconciliations WHERE tenant=? AND reconcile_id=?",
            (tenant, reconcile_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_dict(row)


def _query_orders(conn, tenant: str, scope: dict) -> list:
    """在调用方事务内按范围（单张订单/金额区间/未收区间取交集）取出订单，按订单标识升序。"""
    where = ["o.tenant = ?"]
    params: list = [tenant]
    if scope.get("order_id") is not None:
        where.append("o.order_id = ?")
        params.append(scope["order_id"])
    if scope.get("amount_min") is not None:
        where.append("o.amount_cents >= ?")
        params.append(scope["amount_min"])
    if scope.get("amount_max") is not None:
        where.append("o.amount_cents <= ?")
        params.append(scope["amount_max"])
    if scope.get("outstanding_min") is not None:
        where.append(f"(o.amount_cents - {_NET_PAID_EXPR}) >= ?")
        params.append(scope["outstanding_min"])
    if scope.get("outstanding_max") is not None:
        where.append(f"(o.amount_cents - {_NET_PAID_EXPR}) <= ?")
        params.append(scope["outstanding_max"])
    return conn.execute(
        "SELECT o.tenant, o.order_id, o.amount_cents, o.paid_cents, "
        "COALESCE(r.approved_total,0) AS approved_total "
        "FROM orders o LEFT JOIN ("
        "SELECT tenant, order_id, COALESCE(SUM(amount_cents),0) AS approved_total "
        "FROM refunds WHERE status='approved' GROUP BY tenant, order_id"
        ") r ON r.tenant=o.tenant AND r.order_id=o.order_id "
        f"WHERE {' AND '.join(where)} ORDER BY o.order_id ASC",
        params,
    ).fetchall()


def _check_order(order_row, entry_rows) -> tuple[dict, dict | None]:
    """核对单张订单：逐条累计应有余额与衔接结论、末条余额对账、金额闭合。"""
    amount = order_row["amount_cents"]
    paid = order_row["paid_cents"] - order_row["approved_total"]
    outstanding = amount - paid

    # 应有余额从 0 起按逐条变化额累计，与该条落库余额比较；不以前一条落库余额递推，
    # 这样断链点之后的条目不会被连带误判，差异能定位到具体哪一笔。
    entries: list[dict] = []
    running = 0
    broken_seqs: list[int] = []
    for row in entry_rows:
        running += row["change_cents"]
        chained = row["balance_cents"] == running
        if not chained:
            broken_seqs.append(row["seq"])
        entries.append({
            "seq": row["seq"],
            "action_type": row["action_type"],
            "ref_type": row["ref_type"],
            "ref_id": row["ref_id"],
            "change_cents": row["change_cents"],
            "balance_cents": row["balance_cents"],
            "expected_balance_cents": running,
            "chained": chained,
        })

    chain_intact = not broken_seqs
    last_balance = entry_rows[-1]["balance_cents"] if entry_rows else 0
    final_matches = last_balance == paid
    amount_closed = paid >= 0 and paid <= amount and amount == paid + outstanding

    reasons: list[str] = []
    if not chain_intact:
        reasons.append(REASON_BROKEN_CHAIN)
    if not final_matches:
        reasons.append(REASON_FINAL_MISMATCH)
    if not amount_closed:
        reasons.append(REASON_AMOUNT_NOT_CLOSED)

    item = {
        "order_id": order_row["order_id"],
        "amount_cents": amount,
        "paid_cents": paid,
        "outstanding_cents": outstanding,
        "entries": entries,
        "chain_intact": chain_intact,
        "final_balance_matches_paid": final_matches,
        "amount_closed": amount_closed,
        "reconciled": not reasons,
    }
    discrepancy = None
    if reasons:
        discrepancy = {
            "order_id": order_row["order_id"],
            "reasons": reasons,
            "broken_entry_seqs": broken_seqs,
            "last_balance_cents": last_balance,
            "current_paid_cents": paid,
        }
    return item, discrepancy


def reconcile(tenant: str, reconcile_id: str, scope: dict) -> tuple[dict, bool]:
    """发起对账，返回(对账单, 是否首次生成)。

    整个核对（范围内订单、每张订单的全部流水、差异判定与结论落库）在同一个数据库
    事务、同一份已提交快照内完成：对业务数据（订单、收款、回退、退款、流水）纯读取，
    唯一写入是对账单本身这一行结论。业务身份为（租户, reconcile_id）：身份已存在时
    不重新核对、不改变既有结论，直接返回既有对账单，与本次请求携带的范围指纹无关。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT tenant, reconcile_id, status, order_count, discrepancy_count, scope_json, "
            "result_json, created_at FROM reconciliations WHERE tenant=? AND reconcile_id=?",
            (tenant, reconcile_id),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return _row_to_dict(existing), False

        order_rows = _query_orders(conn, tenant, scope)
        if not order_rows:
            # 范围下没有任何订单（含单张订单不存在）：参数错误，与内部错误区分，不留对账单。
            raise InvalidScope("reconciliation scope matches no orders")

        orders: list[dict] = []
        discrepancies: list[dict] = []
        for order_row in order_rows:
            entry_rows = conn.execute(
                "SELECT seq, action_type, ref_type, ref_id, change_cents, balance_cents "
                "FROM order_account_entries WHERE tenant=? AND order_id=? ORDER BY seq ASC",
                (tenant, order_row["order_id"]),
            ).fetchall()
            item, discrepancy = _check_order(order_row, entry_rows)
            orders.append(item)
            if discrepancy is not None:
                discrepancies.append(discrepancy)

        status = RECONCILED if not discrepancies else DISCREPANCY
        payload = {"orders": orders, "discrepancies": discrepancies}
        conn.execute(
            "INSERT INTO reconciliations(tenant, reconcile_id, status, order_count, discrepancy_count, "
            "scope_json, result_json) VALUES(?,?,?,?,?,?,?)",
            (
                tenant,
                reconcile_id,
                status,
                len(orders),
                len(discrepancies),
                json.dumps(scope, ensure_ascii=False, sort_keys=True),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as error:
        # 并发下另一请求已用同一（租户, reconcile_id）落库：回放既有对账单，不重新核对。
        _rollback(conn)
        if "UNIQUE" not in str(error):
            raise
        existing = get(tenant, reconcile_id)
        return existing, False
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    return get(tenant, reconcile_id), True
