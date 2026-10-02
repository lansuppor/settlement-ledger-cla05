import json

from app.store.db import connect

# 对账单总体状态：全部订单逐条衔接且末条余额等于当前对外已收、金额闭合为已核销；
# 任一订单不满足即为有差异。
RECONCILED = "reconciled"
DIFFERENCE_FOUND = "difference_found"

# 差异原因（逐张订单给出，可并存多个）。
BALANCE_CHAIN_BROKEN = "balance_chain_broken"        # 流水逐条余额衔接断链
FINAL_BALANCE_MISMATCH = "final_balance_mismatch"    # 末条流水余额与当前对外已收不一致
AMOUNT_NOT_CLOSED = "amount_not_closed"              # 订单金额 ≠ 已收 + 未收

# 净已收与未收口口径与订单读取/条件检索完全一致：
# 净已收 = 累计收款毛额 − 已批准未冲正退款；未收 = 订单金额 − 净已收。
NET_PAID_EXPR = "(o.paid_cents - COALESCE(r.approved_total,0))"
OUTSTANDING_EXPR = f"(o.amount_cents - {NET_PAID_EXPR})"


class ReconciliationError(Exception):
    pass


class EmptyScope(ReconciliationError):
    """对账范围合法但范围内没有任何订单，对外按参数错误处理。"""


def get(tenant: str, reconcile_id: str) -> dict | None:
    """按（租户, 对账标识）读取既有对账单；不存在或跨租户返回 None，由上层统一按 404 处理。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT result_json FROM reconciliations WHERE tenant=? AND reconcile_id=?",
            (tenant, reconcile_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else json.loads(row["result_json"])


def reconcile(
    tenant: str,
    reconcile_id: str,
    *,
    order_id: str | None = None,
    amount_min: int | None = None,
    amount_max: int | None = None,
    outstanding_min: int | None = None,
    outstanding_max: int | None = None,
) -> tuple[dict, bool]:
    """按范围核对并固化对账单，返回(对账单, 是否首次生成)。

    业务身份为（租户, reconcile_id）：身份已存在时绝不重新核对，直接返回既有对账单，
    与本次请求携带的范围指纹无关。首次核对在单个只读事务、同一份已提交快照内纯读取完成
    （不改变任何订单、收款、回退、退款与流水），核对结果随后作为一张不可变对账单整体落库。
    """
    # 先做一次廉价的既有单检查：命中则直接回放，连只读核对都不发起。
    existing = get(tenant, reconcile_id)
    if existing is not None:
        return existing, False

    result = _build_statement(
        tenant,
        reconcile_id,
        order_id=order_id,
        amount_min=amount_min,
        amount_max=amount_max,
        outstanding_min=outstanding_min,
        outstanding_max=outstanding_max,
    )
    payload = json.dumps(result, ensure_ascii=False)

    conn = connect()
    try:
        # IMMEDIATE 串行化并发首次提交：不同对账标识各自独立落库；同一标识并发时
        # 主键只放行一张，其余回滚并回放先提交的那张，结论均出自各自的一次快照核对。
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT result_json FROM reconciliations WHERE tenant=? AND reconcile_id=?",
            (tenant, reconcile_id),
        ).fetchone()
        if row is not None:
            conn.execute("COMMIT")
            return json.loads(row["result_json"]), False
        conn.execute(
            "INSERT INTO reconciliations(tenant, reconcile_id, result_json) VALUES(?,?,?)",
            (tenant, reconcile_id, payload),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        # 并发下同一身份的另一请求抢先提交：不把它当作失败，回放既有对账单即可。
        existing = get(tenant, reconcile_id)
        if existing is not None:
            return existing, False
        raise
    conn.close()
    return result, True


def _build_statement(
    tenant: str,
    reconcile_id: str,
    *,
    order_id: str | None,
    amount_min: int | None,
    amount_max: int | None,
    outstanding_min: int | None,
    outstanding_max: int | None,
) -> dict:
    """在一个只读事务、同一份已提交快照内完成范围内全部订单的核对。"""
    conn = connect()
    try:
        # 只读快照事务：核对期间提交的收款/回退/退款审核/冲正对本次核对不可见，
        # 保证订单当前金额与其流水结论出自同一份已提交快照，绝不出现口径撕裂。
        conn.execute("BEGIN")

        where = ["o.tenant = ?"]
        params: list = [tenant]
        if order_id is not None:
            where.append("o.order_id = ?")
            params.append(order_id)
        if amount_min is not None:
            where.append("o.amount_cents >= ?")
            params.append(amount_min)
        if amount_max is not None:
            where.append("o.amount_cents <= ?")
            params.append(amount_max)
        if outstanding_min is not None:
            where.append(f"{OUTSTANDING_EXPR} >= ?")
            params.append(outstanding_min)
        if outstanding_max is not None:
            where.append(f"{OUTSTANDING_EXPR} <= ?")
            params.append(outstanding_max)
        where_sql = " AND ".join(where)

        scope_select = (
            "FROM orders o LEFT JOIN ("
            "SELECT tenant, order_id, COALESCE(SUM(amount_cents),0) AS approved_total "
            "FROM refunds WHERE status='approved' GROUP BY tenant, order_id"
            ") r ON r.tenant=o.tenant AND r.order_id=o.order_id "
            f"WHERE {where_sql}"
        )
        rows = conn.execute(
            f"SELECT o.tenant, o.order_id, o.amount_cents, o.paid_cents, "
            f"COALESCE(r.approved_total,0) AS approved_total {scope_select} "
            "ORDER BY o.order_id ASC",
            params,
        ).fetchall()

        # 范围合法但范围内没有任何订单（含指定的单张订单不存在/非本租户）：参数错误，
        # 不留下半张对账单。
        if not rows:
            conn.execute("COMMIT")
            raise EmptyScope("no order matches the reconcile scope")

        scoped_ids = [row["order_id"] for row in rows]
        entries_by_order = _load_entries(conn, tenant, scoped_ids)
        # 核对时点取数据库时钟（UTC），与快照读取处于同一事务。
        checked_at = conn.execute("SELECT CURRENT_TIMESTAMP AS t").fetchone()["t"]
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()

    order_results = [
        _reconcile_order(row, entries_by_order.get(row["order_id"], [])) for row in rows
    ]
    differences = [
        {"order_id": item["order_id"], "reasons": item["difference_reasons"]}
        for item in order_results
        if item["difference_reasons"]
    ]
    return {
        "tenant": tenant,
        "reconcile_id": reconcile_id,
        "status": RECONCILED if not differences else DIFFERENCE_FOUND,
        "total_orders": len(order_results),
        "difference_count": len(differences),
        "differences": differences,
        "orders": order_results,
        "checked_at": checked_at,
    }


def _load_entries(conn, tenant: str, order_ids: list[str]) -> dict[str, list]:
    """一次性读取范围内全部订单的流水，按订单分组、组内按 seq 升序。

    范围订单数可能很多，故用 IN 分批而不是逐订单回表；全部读取与订单范围读取处于同一
    只读快照事务，订单金额与流水互为同一时点结论。
    """
    grouped: dict[str, list] = {oid: [] for oid in order_ids}
    batch = 400
    for start in range(0, len(order_ids), batch):
        ids = order_ids[start:start + batch]
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            "SELECT tenant, order_id, seq, action_type, ref_type, ref_id, change_cents, balance_cents "
            f"FROM order_account_entries WHERE tenant=? AND order_id IN ({placeholders}) "
            "ORDER BY order_id ASC, seq ASC",
            [tenant, *ids],
        ).fetchall()
        for row in rows:
            grouped[row["order_id"]].append(row)
    return grouped


def _reconcile_order(row, entries: list) -> dict:
    """生成单张订单的逐条流水核对结论；所有金额口径与订单读取保持一致。"""
    gross_paid = row["paid_cents"]
    paid_cents = gross_paid - row["approved_total"]
    amount_cents = row["amount_cents"]
    outstanding_cents = amount_cents - paid_cents

    # 应有余额从 0 起逐条累计：本条应有余额 = 上一条应有余额 + 本条变化额；
    # 衔接结论判定流水持久化的余额是否等于应有余额，任何一条不等即断链。
    entry_results = []
    running = 0
    chain_intact = True
    for entry in entries:
        expected = running + entry["change_cents"]
        chained = entry["balance_cents"] == expected
        chain_intact = chain_intact and chained
        entry_results.append({
            "seq": entry["seq"],
            "action_type": entry["action_type"],
            "ref_type": entry["ref_type"],
            "ref_id": entry["ref_id"],
            "change_cents": entry["change_cents"],
            "balance_cents": entry["balance_cents"],
            "expected_balance_cents": expected,
            "chained": chained,
        })
        running = expected

    # 末条流水持久化余额即账务流水给出的当前余额；尚无流水时账面起点为 0。
    last_balance = entries[-1]["balance_cents"] if entries else 0
    final_balance_matches = last_balance == paid_cents
    amount_closed = amount_cents == paid_cents + outstanding_cents

    reasons = []
    if not chain_intact:
        reasons.append(BALANCE_CHAIN_BROKEN)
    if not final_balance_matches:
        reasons.append(FINAL_BALANCE_MISMATCH)
    if not amount_closed:
        reasons.append(AMOUNT_NOT_CLOSED)

    return {
        "order_id": row["order_id"],
        "amount_cents": amount_cents,
        "paid_cents": paid_cents,
        "outstanding_cents": outstanding_cents,
        "chain_intact": chain_intact,
        "final_balance_matches": final_balance_matches,
        "amount_closed": amount_closed,
        "last_balance_cents": last_balance,
        "entries": entry_results,
        "difference_reasons": reasons,
    }
