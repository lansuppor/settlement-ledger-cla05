import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_reconciliations.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "rc"
H = {"X-Tenant": T}


def _order(order_id: str, amount: int = 1000, paid: int = 0) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _refund(order_id: str, refund_id: str, amount: int, request_id: str, complete: bool = False) -> None:
    assert client.post(f"/orders/{order_id}/refunds",
                       json={"refund_id": refund_id, "amount_cents": amount,
                             "request_id": request_id},
                       headers=H).status_code == 201
    if complete:
        assert client.post(f"/orders/{order_id}/refunds/{refund_id}/complete",
                           json={"request_id": request_id + "-c"}, headers=H).status_code == 200


def _accept(order_id: str, refund_id: str, reconciliation_id: str, amount: int, request_id: str,
            reason: str = "退款对账", headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/reconciliations",
                       json={"reconciliation_id": reconciliation_id, "amount_cents": amount,
                             "reason": reason, "request_id": request_id},
                       headers=headers)


def _action(order_id: str, refund_id: str, reconciliation_id: str, action: str, request_id: str,
            headers=H):
    return client.post(
        f"/orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}/{action}",
        json={"request_id": request_id}, headers=headers)


def test_accept_occupies_and_reconcile_counts_written_off() -> None:
    _order("ro1", 1000, 1000)
    _refund("ro1", "rf1", 1000, "req-rf1")
    r = _accept("ro1", "rf1", "c1", 300, "req-a1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["amount_cents"] == 300
    assert body["effective_deduction_cents"] == 0 and body["reason"] == "退款对账"
    # 受理即占用未核销余额：1000 中占 300，再占 701 超限
    assert _accept("ro1", "rf1", "c2", 701, "req-a2").status_code == 409
    assert _accept("ro1", "rf1", "c2", 700, "req-a3").status_code == 201
    # 占用不改变订单与退款单金额
    assert client.get("/orders/ro1", headers=H).json()["paid_cents"] == 1000
    assert client.get("/orders/ro1/refunds/rf1", headers=H).json()["amount_cents"] == 1000

    done = _action("ro1", "rf1", "c1", "reconcile", "req-a4")
    assert done.status_code == 200
    assert done.json()["status"] == "reconciled"
    assert done.json()["effective_deduction_cents"] == 300
    # 核销只作用于退款单未核销金额，订单与退款单金额均不变
    assert client.get("/orders/ro1", headers=H).json()["paid_cents"] == 1000
    assert client.get("/orders/ro1/refunds/rf1", headers=H).json()["amount_cents"] == 1000
    # 已核销合计仍占用余额：已核销 300 + 待核销 700 = 1000，再受理 1 超限
    assert _accept("ro1", "rf1", "c3", 1, "req-a5").status_code == 409


def test_duplicate_reconciliation_id_refused_without_mutation() -> None:
    _order("ro2", 500, 500)
    _refund("ro2", "rf1", 500, "req-b0")
    assert _accept("ro2", "rf1", "c1", 100, "req-b1").status_code == 201
    dup = _accept("ro2", "rf1", "c1", 200, "req-b2")
    assert dup.status_code == 409
    got = client.get("/orders/ro2/refunds/rf1/reconciliations/c1", headers=H).json()
    assert got["amount_cents"] == 100 and got["status"] == "pending"


def test_accept_refund_not_found() -> None:
    _order("ro3", 100, 100)
    assert _accept("ro3", "missing", "c1", 1, "req-m1").status_code == 404
    assert _accept("missing-order", "rf1", "c1", 1, "req-m2").status_code == 404


def test_accept_exceeds_unwritten_off_amount() -> None:
    _order("ro4", 1000, 1000)
    _refund("ro4", "rf1", 1000, "req-e0")
    assert _accept("ro4", "rf1", "c1", 1001, "req-e1").status_code == 409
    assert _accept("ro4", "rf1", "c1", 1000, "req-e2").status_code == 201
    # 失败请求不留部分写入：退款单与订单金额不变
    assert client.get("/orders/ro4/refunds/rf1", headers=H).json()["amount_cents"] == 1000


def test_cancel_releases_occupation() -> None:
    _order("ro5", 500, 500)
    _refund("ro5", "rf1", 500, "req-f0")
    assert _accept("ro5", "rf1", "c1", 500, "req-f1").status_code == 201
    assert _accept("ro5", "rf1", "c2", 1, "req-f2").status_code == 409
    r = _action("ro5", "rf1", "c1", "cancel", "req-f3")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["effective_deduction_cents"] == 0
    # 撤销释放占用、不改已核销金额与订单/退款单金额
    assert client.get("/orders/ro5", headers=H).json()["paid_cents"] == 500
    assert client.get("/orders/ro5/refunds/rf1", headers=H).json()["amount_cents"] == 500
    assert _accept("ro5", "rf1", "c2", 500, "req-f4").status_code == 201


def test_terminal_transitions_rejected() -> None:
    _order("ro6", 300, 300)
    _refund("ro6", "rf1", 300, "req-g0")
    _accept("ro6", "rf1", "c1", 100, "req-g1")
    assert _action("ro6", "rf1", "c1", "reconcile", "req-g2").status_code == 200
    # 重复核销、已核销不可撤销
    assert _action("ro6", "rf1", "c1", "reconcile", "req-g3").status_code == 409
    assert _action("ro6", "rf1", "c1", "cancel", "req-g4").status_code == 409
    # 待核销单不能冲正
    _accept("ro6", "rf1", "c2", 100, "req-g5")
    assert _action("ro6", "rf1", "c2", "reverse", "req-g6").status_code == 409

    assert _action("ro6", "rf1", "c2", "cancel", "req-g7").status_code == 200
    assert _action("ro6", "rf1", "c2", "cancel", "req-g8").status_code == 409
    assert _action("ro6", "rf1", "c2", "reconcile", "req-g9").status_code == 409


def test_reverse_writes_back_once_and_is_terminal() -> None:
    _order("ro7", 400, 400)
    _refund("ro7", "rf1", 400, "req-h0")
    _accept("ro7", "rf1", "c1", 400, "req-h1")
    _action("ro7", "rf1", "c1", "reconcile", "req-h2")
    rev = _action("ro7", "rf1", "c1", "reverse", "req-h3")
    assert rev.status_code == 200
    assert rev.json()["status"] == "reversed"
    assert rev.json()["effective_deduction_cents"] == 0
    # 冲正把已核销金额减回未核销余额：可再受理全额对账单
    assert _accept("ro7", "rf1", "c2", 400, "req-h4").status_code == 201
    # 订单与退款单金额始终不变
    assert client.get("/orders/ro7", headers=H).json()["paid_cents"] == 400
    assert client.get("/orders/ro7/refunds/rf1", headers=H).json()["amount_cents"] == 400
    # 重复冲正与冲正后撤销都被拒绝
    assert _action("ro7", "rf1", "c1", "reverse", "req-h5").status_code == 409
    assert _action("ro7", "rf1", "c1", "cancel", "req-h6").status_code == 409
    # 已撤销单不能冲正
    _action("ro7", "rf1", "c2", "cancel", "req-h7")
    assert _action("ro7", "rf1", "c2", "reverse", "req-h8").status_code == 409


def test_works_on_completed_refund_without_moving_order_money() -> None:
    _order("ro8", 1000, 1000)
    _refund("ro8", "rf1", 400, "req-i0", complete=True)
    # 退款完成已扣减订单已收 400
    assert client.get("/orders/ro8", headers=H).json()["paid_cents"] == 600
    assert _accept("ro8", "rf1", "c1", 400, "req-i1").status_code == 201
    assert _action("ro8", "rf1", "c1", "reconcile", "req-i2").status_code == 200
    assert _action("ro8", "rf1", "c1", "reverse", "req-i3").status_code == 200
    # 对账核销链路全程不触碰订单已收
    assert client.get("/orders/ro8", headers=H).json()["paid_cents"] == 600
    refund = client.get("/orders/ro8/refunds/rf1", headers=H).json()
    assert refund["amount_cents"] == 400 and refund["effective_deduction_cents"] == 400


def test_action_on_missing_reconciliation_is_404_and_replayable() -> None:
    _order("ro9", 100, 100)
    _refund("ro9", "rf1", 100, "req-n0")
    r1 = _action("ro9", "rf1", "ghost", "reconcile", "req-n1")
    r2 = _action("ro9", "rf1", "ghost", "reconcile", "req-n1")
    assert r1.status_code == r2.status_code == 404
    assert r1.json() == r2.json()


def test_idempotent_replays() -> None:
    _order("ro10", 600, 600)
    _refund("ro10", "rf1", 600, "req-j0")
    first = _accept("ro10", "rf1", "c1", 100, "req-j1")
    replay = _accept("ro10", "rf1", "c1", 100, "req-j1")
    assert replay.status_code == first.status_code == 201
    assert replay.json() == first.json()
    rows = client.get("/orders/ro10/refunds/rf1/reconciliations", headers=H).json()["reconciliations"]
    assert len(rows) == 1

    s1 = _action("ro10", "rf1", "c1", "reconcile", "req-j2")
    s2 = _action("ro10", "rf1", "c1", "reconcile", "req-j2")
    assert s1.status_code == s2.status_code == 200 and s1.json() == s2.json()

    v1 = _action("ro10", "rf1", "c1", "reverse", "req-j3")
    v2 = _action("ro10", "rf1", "c1", "reverse", "req-j3")
    assert v1.status_code == v2.status_code == 200 and v1.json() == v2.json()

    c1 = _accept("ro10", "rf1", "c2", 100, "req-j5")
    c2 = _action("ro10", "rf1", "c2", "cancel", "req-j6")
    assert c1.status_code == 201 and c2.status_code == 200
    assert _action("ro10", "rf1", "c2", "cancel", "req-j6").json() == c2.json()

    # 同一 request_id 改作其他操作/对象 -> 409 且不改数据
    assert _action("ro10", "rf1", "c1", "cancel", "req-j1").status_code == 409
    assert _accept("ro10", "rf1", "c9", 100, "req-j1").status_code == 409
    # 失败响应同样可重放
    f1 = _accept("ro10", "rf1", "c9", 10000, "req-j4")
    f2 = _accept("ro10", "rf1", "c9", 10000, "req-j4")
    assert f1.status_code == f2.status_code == 409 and f1.json() == f2.json()
    assert client.get("/orders/ro10/refunds/rf1/reconciliations/c9", headers=H).status_code == 404


def test_concurrent_accepts_never_exceed_balance() -> None:
    _order("ro11", 1000, 1000)
    _refund("ro11", "rf1", 1000, "req-k0")
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_accept("ro11", "rf1", f"cc{i}", 300, f"req-k{i+1}").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 3
    assert sorted(results).count(409) == 5
    # 全部核销后账面闭合：已核销 900，未核销余额 100
    for i in range(8):
        if client.get(f"/orders/ro11/refunds/rf1/reconciliations/cc{i}", headers=H).status_code == 200:
            _action("ro11", "rf1", f"cc{i}", "reconcile", f"req-kr{i}")
    assert _accept("ro11", "rf1", "tail", 101, "req-kt1").status_code == 409
    assert _accept("ro11", "rf1", "tail", 100, "req-kt2").status_code == 201


def test_concurrent_same_reconciliation_id_single_winner() -> None:
    _order("ro12", 500, 500)
    _refund("ro12", "rf1", 500, "req-l0")
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(rid: str) -> None:
        barrier.wait()
        results.append(_accept("ro12", "rf1", "same", 100, rid).status_code)

    threads = [threading.Thread(target=worker, args=(f"req-l{i+1}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]


def test_cross_tenant_is_not_found_everywhere() -> None:
    _order("ro13", 100, 100)
    _refund("ro13", "rf1", 100, "req-o0")
    _accept("ro13", "rf1", "c1", 100, "req-o1")
    other = {"X-Tenant": "rc-other"}
    assert client.get("/orders/ro13/refunds/rf1/reconciliations/c1", headers=other).status_code == 404
    assert client.get("/orders/ro13/refunds/rf1/reconciliations", headers=other).status_code == 404
    assert _accept("ro13", "rf1", "c1", 100, "req-o2", headers=other).status_code == 404
    assert _action("ro13", "rf1", "c1", "reconcile", "req-o3", headers=other).status_code == 404
    assert _action("ro13", "rf1", "c1", "reverse", "req-o4", headers=other).status_code == 404
    # 跨租户检索看不到本租户对账单
    assert client.get("/reconciliations", headers=other).json()["reconciliations"] == []
    # 跨租户操作未影响本租户状态
    assert client.get("/orders/ro13/refunds/rf1/reconciliations/c1",
                      headers=H).json()["status"] == "pending"


def test_list_shape_and_ordering() -> None:
    _order("ro14", 900, 900)
    _refund("ro14", "rf1", 900, "req-p0")
    _accept("ro14", "rf1", "a", 100, "req-p1")
    _accept("ro14", "rf1", "b", 200, "req-p2")
    _action("ro14", "rf1", "a", "reconcile", "req-p3")
    _accept("ro14", "rf1", "c", 300, "req-p4")
    _action("ro14", "rf1", "c", "cancel", "req-p5")
    listed = client.get("/orders/ro14/refunds/rf1/reconciliations", headers=H)
    assert listed.status_code == 200
    items = listed.json()["reconciliations"]
    assert [s["reconciliation_id"] for s in items] == ["a", "b", "c"]
    by_id = {s["reconciliation_id"]: s for s in items}
    assert by_id["a"]["status"] == "reconciled" and by_id["a"]["effective_deduction_cents"] == 100
    assert by_id["b"]["status"] == "pending" and by_id["b"]["effective_deduction_cents"] == 0
    assert by_id["c"]["status"] == "cancelled" and by_id["c"]["effective_deduction_cents"] == 0
    for s in items:
        assert set(s) == {"order_id", "refund_id", "reconciliation_id", "amount_cents", "reason",
                          "status", "effective_deduction_cents"}


def test_search_by_status_and_amount_range() -> None:
    _order("ro15", 2000, 2000)
    _refund("ro15", "rf1", 2000, "req-q0")
    _accept("ro15", "rf1", "a", 100, "req-q1")
    _accept("ro15", "rf1", "b", 200, "req-q2")
    _accept("ro15", "rf1", "c", 300, "req-q3")
    _action("ro15", "rf1", "a", "reconcile", "req-q4")
    _action("ro15", "rf1", "b", "cancel", "req-q5")

    all_items = client.get("/reconciliations", headers=H).json()["reconciliations"]
    mine = [s for s in all_items if s["order_id"] == "ro15" and s["refund_id"] == "rf1"]
    assert [s["reconciliation_id"] for s in mine] == ["a", "b", "c"]

    reconciled = client.get("/reconciliations?status=reconciled", headers=H).json()["reconciliations"]
    mine_reconciled = [s for s in reconciled if s["order_id"] == "ro15"]
    assert [s["reconciliation_id"] for s in mine_reconciled] == ["a"]
    assert mine_reconciled[0]["effective_deduction_cents"] == 100

    ranged = client.get("/reconciliations?min_amount_cents=150&max_amount_cents=300",
                        headers=H).json()["reconciliations"]
    assert [s["reconciliation_id"] for s in ranged if s["order_id"] == "ro15"] == ["b", "c"]

    combo = client.get("/reconciliations?status=pending&min_amount_cents=250",
                       headers=H).json()["reconciliations"]
    assert [s["reconciliation_id"] for s in combo if s["order_id"] == "ro15"] == ["c"]

    assert client.get("/reconciliations?status=bogus", headers=H).status_code == 400
    assert client.get("/reconciliations?min_amount_cents=300&max_amount_cents=100",
                      headers=H).status_code == 400


def test_tenant_header_required() -> None:
    assert client.post("/orders/ro1/refunds/rf1/reconciliations",
                       json={"reconciliation_id": "x", "amount_cents": 1, "reason": "r",
                             "request_id": "z"}).status_code == 400
    assert client.get("/reconciliations").status_code == 400


def test_invalid_params_rejected_by_validation() -> None:
    _order("ro16", 100, 100)
    _refund("ro16", "rf1", 100, "req-r0")
    assert _accept("ro16", "rf1", "c1", 0, "req-r1").status_code == 422
    assert _accept("ro16", "rf1", "c1", -5, "req-r2").status_code == 422
    assert _accept("ro16", "rf1", "c1", 10, "req-r3", reason="").status_code == 422


def test_ticket_award_respects_reconciliation_closure() -> None:
    _order("ro17", 1000, 1000)
    _refund("ro17", "rf1", 1000, "req-u0")
    # 待核销对账单占用 600，工单裁决最多把退款单金额扣到 600（裁决 ≤ 400）
    assert _accept("ro17", "rf1", "c1", 600, "req-u1").status_code == 201
    base = "/orders/ro17/refunds/rf1/tickets"
    assert client.post(base, json={"ticket_id": "t1", "request_amount_cents": 1000,
                                   "initiator": "alice", "reason": "货不对版",
                                   "request_id": "req-u2"}, headers=H).status_code == 201
    assert client.post(f"{base}/t1/process", json={"request_id": "req-u3"},
                       headers=H).status_code == 200
    # 裁决 401 会使已占用合计 600 超过退款单新金额 599 -> 409，且不扣减
    assert client.post(f"{base}/t1/resolve", json={"award_cents": 401, "request_id": "req-u4"},
                       headers=H).status_code == 409
    assert client.get("/orders/ro17/refunds/rf1", headers=H).json()["amount_cents"] == 1000
    # 裁决 400：退款单金额降到 600，恰好等于对账单占用，账面闭合
    assert client.post(f"{base}/t1/resolve", json={"award_cents": 400, "request_id": "req-u5"},
                       headers=H).status_code == 200
    assert client.get("/orders/ro17/refunds/rf1", headers=H).json()["amount_cents"] == 600
    assert _action("ro17", "rf1", "c1", "reconcile", "req-u6").status_code == 200
    # 已核销 600 = 退款单金额 600，再受理任意金额均超限
    assert _accept("ro17", "rf1", "c2", 1, "req-u7").status_code == 409
    # 撤销已解决工单把 400 加回退款单，余额重新开放
    assert client.post(f"{base}/t1/revoke", json={"request_id": "req-u8"},
                       headers=H).status_code == 200
    assert client.get("/orders/ro17/refunds/rf1", headers=H).json()["amount_cents"] == 1000
    assert _accept("ro17", "rf1", "c2", 400, "req-u9").status_code == 201
    # 全程订单金额不变
    assert client.get("/orders/ro17", headers=H).json()["paid_cents"] == 1000


def test_state_survives_restart_and_replay() -> None:
    _order("ro18", 800, 800)
    _refund("ro18", "rf1", 800, "req-s0")
    _accept("ro18", "rf1", "c1", 300, "req-s1")
    _action("ro18", "rf1", "c1", "reconcile", "req-s2")
    _action("ro18", "rf1", "c1", "reverse", "req-s3")
    _accept("ro18", "rf1", "c2", 200, "req-s4")
    _action("ro18", "rf1", "c2", "cancel", "req-s5")
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    by_id = {s["reconciliation_id"]: s
             for s in client.get("/orders/ro18/refunds/rf1/reconciliations",
                                 headers=H).json()["reconciliations"]}
    assert by_id["c1"]["status"] == "reversed" and by_id["c1"]["effective_deduction_cents"] == 0
    assert by_id["c2"]["status"] == "cancelled"
    # 订单与退款单金额从未被对账链路改变
    assert client.get("/orders/ro18", headers=H).json()["paid_cents"] == 800
    assert client.get("/orders/ro18/refunds/rf1", headers=H).json()["amount_cents"] == 800
    # 重启后重放旧请求，结果一致且不重复生效
    replay = _action("ro18", "rf1", "c1", "reverse", "req-s3")
    assert replay.status_code == 200 and replay.json()["status"] == "reversed"
    # 冲正已减回，全额 800 重新可占用（c2 已撤销不占）
    assert _accept("ro18", "rf1", "c3", 800, "req-s6").status_code == 201
