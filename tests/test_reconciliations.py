import os
import sqlite3
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_reconciliations.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "rc"
H = {"X-Tenant": T}


def _refund(order_id: str, refund_id: str, amount: int = 1000) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount},
                       headers=H).status_code == 200
    assert client.post(f"/orders/{order_id}/refunds",
                       json={"refund_id": refund_id, "amount_cents": amount,
                             "request_id": f"rf-{order_id}-{refund_id}"},
                       headers=H).status_code == 201


def _accept(order_id: str, refund_id: str, recon_id: str, amount: int, request_id: str,
            reason: str = "退款对账", headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/reconciliations",
                       json={"reconciliation_id": recon_id, "amount_cents": amount,
                             "reason": reason, "request_id": request_id},
                       headers=headers)


def _action(order_id: str, refund_id: str, recon_id: str, action: str, request_id: str, headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/reconciliations/{recon_id}/{action}",
                       json={"request_id": request_id}, headers=headers)


def _get(order_id: str, refund_id: str, recon_id: str, headers=H):
    return client.get(f"/orders/{order_id}/refunds/{refund_id}/reconciliations/{recon_id}", headers=headers)


def test_accept_occupies_and_settle_counts_reconciled() -> None:
    _refund("ro1", "rr1", 1000)
    r = _accept("ro1", "rr1", "rc1", 300, "req-a1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["amount_cents"] == 300
    assert body["effective_deduction_cents"] == 0 and body["reason"] == "退款对账"
    # 受理即占用：未核销余额 1000 中已占用 300，再受理 701 超限
    assert _accept("ro1", "rr1", "rc2", 701, "req-a2").status_code == 409
    assert _accept("ro1", "rr1", "rc2", 700, "req-a3").status_code == 201

    done = _action("ro1", "rr1", "rc1", "settle", "req-a4")
    assert done.status_code == 200
    assert done.json()["status"] == "settled"
    assert done.json()["effective_deduction_cents"] == 300
    assert done.json()["refund_amount_cents"] == 1000
    assert done.json()["unreconciled_cents"] == 0  # 1000 - 300 已核销 - 700 占用
    # 对账核销不触碰订单账面
    order = client.get("/orders/ro1", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


def test_duplicate_reconciliation_id_refused_without_mutation() -> None:
    _refund("ro2", "rr1", 500)
    assert _accept("ro2", "rr1", "rc1", 100, "req-b1").status_code == 201
    dup = _accept("ro2", "rr1", "rc1", 200, "req-b2")
    assert dup.status_code == 409
    got = _get("ro2", "rr1", "rc1").json()
    assert got["amount_cents"] == 100 and got["status"] == "pending"


def test_accept_refund_not_found() -> None:
    assert _accept("missing", "rr1", "rc1", 1, "req-m1").status_code == 404
    _refund("ro3", "rr1", 100)
    assert _accept("ro3", "missing", "rc1", 1, "req-m2").status_code == 404


def test_accept_exceeds_unreconciled_balance() -> None:
    _refund("ro4", "rr1", 1000)
    assert _accept("ro4", "rr1", "rc1", 1001, "req-e1").status_code == 409
    assert _accept("ro4", "rr1", "rc1", 1000, "req-e2").status_code == 201
    # 退款单账面未被失败请求改变
    refund = client.get("/orders/ro4/refunds/rr1", headers=H).json()
    assert refund["amount_cents"] == 1000 and refund["status"] == "pending"


def test_cancel_releases_occupation() -> None:
    _refund("ro5", "rr1", 500)
    assert _accept("ro5", "rr1", "rc1", 500, "req-f1").status_code == 201
    assert _accept("ro5", "rr1", "rc2", 1, "req-f2").status_code == 409
    r = _action("ro5", "rr1", "rc1", "cancel", "req-f3")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["effective_deduction_cents"] == 0
    # 撤销释放占用后可再受理
    assert _accept("ro5", "rr1", "rc2", 500, "req-f4").status_code == 201


def test_terminal_transitions_rejected() -> None:
    _refund("ro6", "rr1", 300)
    _accept("ro6", "rr1", "rc1", 100, "req-g1")
    assert _action("ro6", "rr1", "rc1", "settle", "req-g2").status_code == 200
    # 重复核销
    assert _action("ro6", "rr1", "rc1", "settle", "req-g3").status_code == 409
    # 已核销不可撤销
    assert _action("ro6", "rr1", "rc1", "cancel", "req-g4").status_code == 409

    _accept("ro6", "rr1", "rc2", 100, "req-g5")
    assert _action("ro6", "rr1", "rc2", "cancel", "req-g6").status_code == 200
    assert _action("ro6", "rr1", "rc2", "cancel", "req-g7").status_code == 409
    assert _action("ro6", "rr1", "rc2", "settle", "req-g8").status_code == 409


def test_reverse_deducts_back_once_and_is_terminal() -> None:
    _refund("ro7", "rr1", 400)
    _accept("ro7", "rr1", "rc1", 400, "req-h1")
    _action("ro7", "rr1", "rc1", "settle", "req-h2")
    rev = _action("ro7", "rr1", "rc1", "reverse", "req-h3")
    assert rev.status_code == 200
    assert rev.json()["status"] == "reversed"
    assert rev.json()["effective_deduction_cents"] == 0
    assert rev.json()["unreconciled_cents"] == 400
    # 重复冲正与冲正后撤销都被拒绝
    assert _action("ro7", "rr1", "rc1", "reverse", "req-h4").status_code == 409
    assert _action("ro7", "rr1", "rc1", "cancel", "req-h5").status_code == 409
    # 待核销单不能冲正
    _accept("ro7", "rr1", "rc2", 10, "req-h6")
    assert _action("ro7", "rr1", "rc2", "reverse", "req-h7").status_code == 409
    # 冲正释放后余额可再被占用
    assert _accept("ro7", "rr1", "rc3", 390, "req-h8").status_code == 201


def test_settle_and_reverse_do_not_touch_order_or_refund_amounts() -> None:
    _refund("ro8", "rr1", 200)
    _accept("ro8", "rr1", "rc1", 200, "req-i1")
    assert _action("ro8", "rr1", "rc1", "settle", "req-i2").status_code == 200
    raw = sqlite3.connect(db_path())
    paid, ostatus = raw.execute(
        "SELECT paid_cents, status FROM orders WHERE tenant='rc' AND order_id='ro8'").fetchone()
    ramount, rstatus = raw.execute(
        "SELECT amount_cents, status FROM refunds WHERE tenant='rc' AND order_id='ro8' AND refund_id='rr1'"
    ).fetchone()
    cstatus = raw.execute(
        "SELECT status FROM refund_reconciliations"
        " WHERE tenant='rc' AND order_id='ro8' AND refund_id='rr1' AND reconciliation_id='rc1'").fetchone()[0]
    assert (paid, ostatus) == (200, "settled")
    assert (ramount, rstatus) == (200, "pending")
    assert cstatus == "settled"
    assert _action("ro8", "rr1", "rc1", "reverse", "req-i3").status_code == 200
    paid = raw.execute(
        "SELECT paid_cents FROM orders WHERE tenant='rc' AND order_id='ro8'").fetchone()[0]
    cstatus = raw.execute(
        "SELECT status FROM refund_reconciliations"
        " WHERE tenant='rc' AND order_id='ro8' AND refund_id='rr1' AND reconciliation_id='rc1'").fetchone()[0]
    raw.close()
    assert paid == 200 and cstatus == "reversed"


def test_idempotent_replays() -> None:
    _refund("ro9", "rr1", 600)
    first = _accept("ro9", "rr1", "rc1", 100, "req-j1")
    replay = _accept("ro9", "rr1", "rc1", 100, "req-j1")
    assert replay.status_code == first.status_code == 201
    assert replay.json() == first.json()
    rows = client.get("/orders/ro9/refunds/rr1/reconciliations", headers=H).json()["reconciliations"]
    assert len(rows) == 1

    s1 = _action("ro9", "rr1", "rc1", "settle", "req-j2")
    s2 = _action("ro9", "rr1", "rc1", "settle", "req-j2")
    assert s1.status_code == s2.status_code == 200 and s1.json() == s2.json()

    v1 = _action("ro9", "rr1", "rc1", "reverse", "req-j3")
    v2 = _action("ro9", "rr1", "rc1", "reverse", "req-j3")
    assert v1.status_code == v2.status_code == 200 and v1.json() == v2.json()

    # 同一 request_id 改作其他操作/对象 -> 409
    assert _action("ro9", "rr1", "rc1", "cancel", "req-j1").status_code == 409
    assert _accept("ro9", "rr1", "rc9", 100, "req-j1").status_code == 409
    # 失败响应同样可重放
    f1 = _accept("ro9", "rr1", "rc9", 10000, "req-j4")
    f2 = _accept("ro9", "rr1", "rc9", 10000, "req-j4")
    assert f1.status_code == f2.status_code == 409 and f1.json() == f2.json()


def test_concurrent_accepts_never_exceed_balance() -> None:
    _refund("ro10", "rr1", 1000)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_accept("ro10", "rr1", f"cc{i}", 300, f"req-k{i}").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 3
    assert sorted(results).count(409) == 5
    # 全部核销后守恒：已核销合计 900，未核销余额 100
    for i in range(8):
        if _get("ro10", "rr1", f"cc{i}").status_code == 200:
            _action("ro10", "rr1", f"cc{i}", "settle", f"req-ks{i}")
    settled = [c for c in client.get("/orders/ro10/refunds/rr1/reconciliations", headers=H)
               .json()["reconciliations"] if c["status"] == "settled"]
    assert sum(c["amount_cents"] for c in settled) == 900
    assert _accept("ro10", "rr1", "ccx", 101, "req-kx").status_code == 409
    assert _accept("ro10", "rr1", "ccx", 100, "req-ky").status_code == 201


def test_concurrent_same_reconciliation_id_single_winner() -> None:
    _refund("ro11", "rr1", 500)
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(rid: str) -> None:
        barrier.wait()
        results.append(_accept("ro11", "rr1", "same", 100, rid).status_code)

    threads = [threading.Thread(target=worker, args=(f"req-l{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]


def test_ticket_award_shrinks_unreconciled_balance() -> None:
    _refund("ro12", "rr1", 1000)
    _accept("ro12", "rr1", "rc1", 400, "req-t1")
    # 工单裁决扣减退款单金额 300：未核销余额重算为 1000-300-400占用 = 300
    assert client.post("/orders/ro12/refunds/rr1/tickets",
                       json={"ticket_id": "tk1", "request_amount_cents": 300, "initiator": "alice",
                             "reason": "争议", "request_id": "req-t2"}, headers=H).status_code == 201
    assert client.post("/orders/ro12/refunds/rr1/tickets/tk1/process",
                       json={"request_id": "req-t3"}, headers=H).status_code == 200
    assert client.post("/orders/ro12/refunds/rr1/tickets/tk1/resolve",
                       json={"award_cents": 300, "request_id": "req-t4"}, headers=H).status_code == 200
    assert _accept("ro12", "rr1", "rc2", 301, "req-t5").status_code == 409
    assert _accept("ro12", "rr1", "rc2", 300, "req-t6").status_code == 201
    # 已解决工单撤销把裁决额加回，余额恢复
    assert client.post("/orders/ro12/refunds/rr1/tickets/tk1/revoke",
                       json={"request_id": "req-t7"}, headers=H).status_code == 200
    assert _accept("ro12", "rr1", "rc3", 300, "req-t8").status_code == 201


def test_ticket_award_cannot_undercut_settled_sum() -> None:
    _refund("ro13", "rr1", 1000)
    _accept("ro13", "rr1", "rc1", 600, "req-u1")
    assert _action("ro13", "rr1", "rc1", "settle", "req-u2").status_code == 200
    # 已核销 600，裁决扣减最多 400；501 会把退款单金额压到已核销合计之下
    assert client.post("/orders/ro13/refunds/rr1/tickets",
                       json={"ticket_id": "tk1", "request_amount_cents": 501, "initiator": "bob",
                             "reason": "争议", "request_id": "req-u3"}, headers=H).status_code == 201
    assert client.post("/orders/ro13/refunds/rr1/tickets/tk1/process",
                       json={"request_id": "req-u4"}, headers=H).status_code == 200
    assert client.post("/orders/ro13/refunds/rr1/tickets/tk1/resolve",
                       json={"award_cents": 401, "request_id": "req-u5"}, headers=H).status_code == 409
    assert client.post("/orders/ro13/refunds/rr1/tickets/tk1/resolve",
                       json={"award_cents": 400, "request_id": "req-u6"}, headers=H).status_code == 200
    refund = client.get("/orders/ro13/refunds/rr1", headers=H).json()
    assert refund["amount_cents"] == 600


def test_cross_tenant_is_not_found_everywhere() -> None:
    _refund("ro14", "rr1", 100)
    _accept("ro14", "rr1", "rc1", 100, "req-o1")
    other = {"X-Tenant": "rc-other"}
    assert _get("ro14", "rr1", "rc1", headers=other).status_code == 404
    assert client.get("/orders/ro14/refunds/rr1/reconciliations", headers=other).status_code == 404
    assert _accept("ro14", "rr1", "rc1", 100, "req-o2", headers=other).status_code == 404
    assert _action("ro14", "rr1", "rc1", "settle", "req-o3", headers=other).status_code == 404
    assert _action("ro14", "rr1", "rc1", "reverse", "req-o4", headers=other).status_code == 404
    # 跨租户检索看不到本租户对账单
    assert client.get("/reconciliations", headers=other).json()["reconciliations"] == []
    # 跨租户操作未影响本租户状态
    assert _get("ro14", "rr1", "rc1").json()["status"] == "pending"


def test_list_shape_and_ordering() -> None:
    _refund("ro15", "rr1", 900)
    _accept("ro15", "rr1", "a", 100, "req-p1")
    _accept("ro15", "rr1", "b", 200, "req-p2")
    _action("ro15", "rr1", "a", "settle", "req-p3")
    _accept("ro15", "rr1", "c", 300, "req-p4")
    _action("ro15", "rr1", "c", "cancel", "req-p5")
    listed = client.get("/orders/ro15/refunds/rr1/reconciliations", headers=H)
    assert listed.status_code == 200
    items = listed.json()["reconciliations"]
    assert [c["reconciliation_id"] for c in items] == ["a", "b", "c"]
    by_id = {c["reconciliation_id"]: c for c in items}
    assert by_id["a"]["status"] == "settled" and by_id["a"]["effective_deduction_cents"] == 100
    assert by_id["b"]["status"] == "pending" and by_id["b"]["effective_deduction_cents"] == 0
    assert by_id["c"]["status"] == "cancelled" and by_id["c"]["effective_deduction_cents"] == 0
    for c in items:
        assert set(c) == {"order_id", "refund_id", "reconciliation_id", "amount_cents", "reason",
                          "status", "effective_deduction_cents"}


def test_search_by_status_and_amount_range() -> None:
    _refund("ro16", "rr1", 2000)
    _accept("ro16", "rr1", "a", 100, "req-q1")
    _accept("ro16", "rr1", "b", 200, "req-q2")
    _accept("ro16", "rr1", "c", 300, "req-q3")
    _action("ro16", "rr1", "a", "settle", "req-q4")
    _action("ro16", "rr1", "b", "cancel", "req-q5")

    all_items = client.get("/reconciliations", headers=H).json()["reconciliations"]
    mine = [c for c in all_items if c["order_id"] == "ro16"]
    assert [c["reconciliation_id"] for c in mine] == ["a", "b", "c"]

    settled = client.get("/reconciliations?status=settled", headers=H).json()["reconciliations"]
    mine_settled = [c for c in settled if c["order_id"] == "ro16"]
    assert [c["reconciliation_id"] for c in mine_settled] == ["a"]
    assert mine_settled[0]["effective_deduction_cents"] == 100

    ranged = client.get("/reconciliations?min_amount_cents=150&max_amount_cents=300",
                        headers=H).json()["reconciliations"]
    assert [c["reconciliation_id"] for c in ranged if c["order_id"] == "ro16"] == ["b", "c"]

    combo = client.get("/reconciliations?status=pending&min_amount_cents=250",
                       headers=H).json()["reconciliations"]
    assert [c["reconciliation_id"] for c in combo if c["order_id"] == "ro16"] == ["c"]

    assert client.get("/reconciliations?status=bogus", headers=H).status_code == 400
    assert client.get("/reconciliations?min_amount_cents=300&max_amount_cents=100", headers=H).status_code == 400


def test_tenant_header_required() -> None:
    assert client.post("/orders/ro1/refunds/rr1/reconciliations",
                       json={"reconciliation_id": "x", "amount_cents": 1, "reason": "r",
                             "request_id": "z"}).status_code == 400
    assert client.get("/reconciliations").status_code == 400


def test_invalid_params_rejected_by_validation() -> None:
    _refund("ro17", "rr1", 100)
    assert _accept("ro17", "rr1", "rc1", 0, "req-r1").status_code == 422
    assert _accept("ro17", "rr1", "rc1", -5, "req-r2").status_code == 422
    assert _accept("ro17", "rr1", "rc1", 10, "req-r3", reason="").status_code == 422


def test_state_survives_restart_and_replay() -> None:
    _refund("ro18", "rr1", 800)
    _accept("ro18", "rr1", "rc1", 300, "req-s1")
    _action("ro18", "rr1", "rc1", "settle", "req-s2")
    _action("ro18", "rr1", "rc1", "reverse", "req-s3")
    _accept("ro18", "rr1", "rc2", 200, "req-s4")
    _action("ro18", "rr1", "rc2", "cancel", "req-s5")
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    by_id = {c["reconciliation_id"]: c
             for c in client.get("/orders/ro18/refunds/rr1/reconciliations", headers=H)
             .json()["reconciliations"]}
    assert by_id["rc1"]["status"] == "reversed" and by_id["rc1"]["effective_deduction_cents"] == 0
    assert by_id["rc2"]["status"] == "cancelled"
    # 重启后重放旧请求，结果一致且不重复生效
    replay = _action("ro18", "rr1", "rc1", "reverse", "req-s3")
    assert replay.status_code == 200 and replay.json()["status"] == "reversed"
    assert replay.json()["unreconciled_cents"] == 800
