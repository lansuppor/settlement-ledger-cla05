import os
import sqlite3
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_settlements.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "st"
H = {"X-Tenant": T}


def _order(order_id: str, amount: int = 1000, paid: int = 0) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _accept(order_id: str, settlement_id: str, amount: int, request_id: str,
            reason: str = "月度对账", headers=H):
    return client.post(f"/orders/{order_id}/settlements",
                       json={"settlement_id": settlement_id, "amount_cents": amount,
                             "reason": reason, "request_id": request_id},
                       headers=headers)


def _action(order_id: str, settlement_id: str, action: str, request_id: str, headers=H):
    return client.post(f"/orders/{order_id}/settlements/{settlement_id}/{action}",
                       json={"request_id": request_id}, headers=headers)


def test_accept_occupies_and_settle_counts_paid() -> None:
    _order("so1", 1000, 400)
    r = _accept("so1", "ss1", 300, "req-a1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["amount_cents"] == 300
    assert body["effective_deduction_cents"] == 0 and body["reason"] == "月度对账"
    # 受理即占用：未收余额 600 中已占用 300，再受理 301 超限
    assert _accept("so1", "ss2", 301, "req-a2").status_code == 409
    assert _accept("so1", "ss2", 300, "req-a3").status_code == 201
    # 占用不影响订单已收
    order = client.get("/orders/so1", headers=H).json()
    assert order["paid_cents"] == 400

    done = _action("so1", "ss1", "settle", "req-a4")
    assert done.status_code == 200
    assert done.json()["status"] == "settled"
    assert done.json()["effective_deduction_cents"] == 300
    order = client.get("/orders/so1", headers=H).json()
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300


def test_duplicate_settlement_id_refused_without_mutation() -> None:
    _order("so2", 500)
    assert _accept("so2", "ss1", 100, "req-b1").status_code == 201
    dup = _accept("so2", "ss1", 200, "req-b2")
    assert dup.status_code == 409
    got = client.get("/orders/so2/settlements/ss1", headers=H).json()
    assert got["amount_cents"] == 100 and got["status"] == "pending"


def test_accept_order_not_found() -> None:
    assert _accept("missing", "ss1", 1, "req-m1").status_code == 404


def test_accept_exceeds_outstanding_balance() -> None:
    _order("so3", 1000, 400)
    assert _accept("so3", "ss1", 601, "req-e1").status_code == 409
    assert _accept("so3", "ss1", 600, "req-e2").status_code == 201
    # 订单账面未被失败请求改变
    assert client.get("/orders/so3", headers=H).json()["paid_cents"] == 400


def test_cancel_releases_occupation() -> None:
    _order("so4", 500)
    assert _accept("so4", "ss1", 500, "req-f1").status_code == 201
    assert _accept("so4", "ss2", 1, "req-f2").status_code == 409
    r = _action("so4", "ss1", "cancel", "req-f3")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # 撤销释放占用、不动订单金额
    order = client.get("/orders/so4", headers=H).json()
    assert order["paid_cents"] == 0
    assert _accept("so4", "ss2", 500, "req-f4").status_code == 201


def test_terminal_transitions_rejected() -> None:
    _order("so5", 300)
    _accept("so5", "ss1", 100, "req-g1")
    assert _action("so5", "ss1", "settle", "req-g2").status_code == 200
    # 重复核销
    assert _action("so5", "ss1", "settle", "req-g3").status_code == 409
    # 已核销不可撤销
    assert _action("so5", "ss1", "cancel", "req-g4").status_code == 409

    _accept("so5", "ss2", 100, "req-g5")
    assert _action("so5", "ss2", "cancel", "req-g6").status_code == 200
    assert _action("so5", "ss2", "cancel", "req-g7").status_code == 409
    assert _action("so5", "ss2", "settle", "req-g8").status_code == 409


def test_reverse_deducts_back_once_and_is_terminal() -> None:
    _order("so6", 400)
    _accept("so6", "ss1", 400, "req-h1")
    _action("so6", "ss1", "settle", "req-h2")
    assert client.get("/orders/so6", headers=H).json()["paid_cents"] == 400
    rev = _action("so6", "ss1", "reverse", "req-h3")
    assert rev.status_code == 200
    assert rev.json()["status"] == "reversed"
    assert rev.json()["effective_deduction_cents"] == 0
    order = client.get("/orders/so6", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 400
    # 重复冲正与冲正后撤销都被拒绝
    assert _action("so6", "ss1", "reverse", "req-h4").status_code == 409
    assert _action("so6", "ss1", "cancel", "req-h5").status_code == 409
    # 待核销单不能冲正
    _accept("so6", "ss2", 10, "req-h6")
    assert _action("so6", "ss2", "reverse", "req-h7").status_code == 409
    # 冲正释放后余额可再被占用
    assert _accept("so6", "ss3", 390, "req-h8").status_code == 201


def test_settle_and_reverse_are_atomic_on_both_sides() -> None:
    _order("so7", 200)
    _accept("so7", "ss1", 200, "req-i1")
    assert _action("so7", "ss1", "settle", "req-i2").status_code == 200
    raw = sqlite3.connect(db_path())
    paid, ostatus = raw.execute(
        "SELECT paid_cents, status FROM orders WHERE tenant='st' AND order_id='so7'").fetchone()
    sstatus = raw.execute(
        "SELECT status FROM settlements WHERE tenant='st' AND order_id='so7' AND settlement_id='ss1'").fetchone()[0]
    assert (paid, ostatus) == (200, "settled")
    assert sstatus == "settled"
    assert _action("so7", "ss1", "reverse", "req-i3").status_code == 200
    paid, ostatus = raw.execute(
        "SELECT paid_cents, status FROM orders WHERE tenant='st' AND order_id='so7'").fetchone()
    sstatus = raw.execute(
        "SELECT status FROM settlements WHERE tenant='st' AND order_id='so7' AND settlement_id='ss1'").fetchone()[0]
    raw.close()
    assert (paid, ostatus) == (0, "accepted")
    assert sstatus == "reversed"


def test_idempotent_replays() -> None:
    _order("so8", 600)
    first = _accept("so8", "ss1", 100, "req-j1")
    replay = _accept("so8", "ss1", 100, "req-j1")
    assert replay.status_code == first.status_code == 201
    assert replay.json() == first.json()
    rows = client.get("/orders/so8/settlements", headers=H).json()["settlements"]
    assert len(rows) == 1

    s1 = _action("so8", "ss1", "settle", "req-j2")
    s2 = _action("so8", "ss1", "settle", "req-j2")
    assert s1.status_code == s2.status_code == 200 and s1.json() == s2.json()
    assert client.get("/orders/so8", headers=H).json()["paid_cents"] == 100

    v1 = _action("so8", "ss1", "reverse", "req-j3")
    v2 = _action("so8", "ss1", "reverse", "req-j3")
    assert v1.status_code == v2.status_code == 200 and v1.json() == v2.json()
    assert client.get("/orders/so8", headers=H).json()["paid_cents"] == 0

    # 同一 request_id 改作其他操作/对象 -> 409
    assert _action("so8", "ss1", "cancel", "req-j1").status_code == 409
    assert _accept("so8", "ss9", 100, "req-j1").status_code == 409
    # 失败响应同样可重放
    f1 = _accept("so8", "ss9", 10000, "req-j4")
    f2 = _accept("so8", "ss9", 10000, "req-j4")
    assert f1.status_code == f2.status_code == 409 and f1.json() == f2.json()


def test_concurrent_accepts_never_exceed_balance() -> None:
    _order("so9", 1000)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_accept("so9", f"cs{i}", 300, f"req-k{i}").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 3
    assert sorted(results).count(409) == 5
    # 全部核销后守恒：已收 900，未收 100
    for i in range(8):
        if client.get(f"/orders/so9/settlements/cs{i}", headers=H).status_code == 200:
            _action("so9", f"cs{i}", "settle", f"req-ks{i}")
    order = client.get("/orders/so9", headers=H).json()
    assert order["paid_cents"] == 900 and order["outstanding_cents"] == 100


def test_concurrent_same_settlement_id_single_winner() -> None:
    _order("so10", 500)
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(rid: str) -> None:
        barrier.wait()
        results.append(_accept("so10", "same", 100, rid).status_code)

    threads = [threading.Thread(target=worker, args=(f"req-l{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]


def test_payment_cannot_consume_occupied_balance() -> None:
    _order("so11", 1000, 400)
    assert _accept("so11", "ss1", 500, "req-n1").status_code == 201
    # 未收 600 中 500 已被结算单占用，收款最多再登记 100
    assert client.post("/orders/so11/payments", json={"amount_cents": 101}, headers=H).status_code == 409
    assert client.post("/orders/so11/payments", json={"amount_cents": 100}, headers=H).status_code == 200
    # 核销完成后占用计入已收，账面闭合
    assert _action("so11", "ss1", "settle", "req-n2").status_code == 200
    order = client.get("/orders/so11", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


def test_cross_tenant_is_not_found_everywhere() -> None:
    _order("so12", 100)
    _accept("so12", "ss1", 100, "req-o1")
    other = {"X-Tenant": "st-other"}
    assert client.get("/orders/so12/settlements/ss1", headers=other).status_code == 404
    assert client.get("/orders/so12/settlements", headers=other).status_code == 404
    assert _accept("so12", "ss1", 100, "req-o2", headers=other).status_code == 404
    assert _action("so12", "ss1", "settle", "req-o3", headers=other).status_code == 404
    assert _action("so12", "ss1", "reverse", "req-o4", headers=other).status_code == 404
    # 跨租户检索看不到本租户结算单
    assert client.get("/settlements", headers=other).json()["settlements"] == []
    # 跨租户操作未影响本租户状态
    assert client.get("/orders/so12/settlements/ss1", headers=H).json()["status"] == "pending"


def test_list_shape_and_ordering() -> None:
    _order("so13", 900)
    _accept("so13", "a", 100, "req-p1")
    _accept("so13", "b", 200, "req-p2")
    _action("so13", "a", "settle", "req-p3")
    _accept("so13", "c", 300, "req-p4")
    _action("so13", "c", "cancel", "req-p5")
    listed = client.get("/orders/so13/settlements", headers=H)
    assert listed.status_code == 200
    items = listed.json()["settlements"]
    assert [s["settlement_id"] for s in items] == ["a", "b", "c"]
    by_id = {s["settlement_id"]: s for s in items}
    assert by_id["a"]["status"] == "settled" and by_id["a"]["effective_deduction_cents"] == 100
    assert by_id["b"]["status"] == "pending" and by_id["b"]["effective_deduction_cents"] == 0
    assert by_id["c"]["status"] == "cancelled" and by_id["c"]["effective_deduction_cents"] == 0
    for s in items:
        assert set(s) == {"order_id", "settlement_id", "amount_cents", "reason", "status",
                          "effective_deduction_cents"}


def test_search_by_status_and_amount_range() -> None:
    _order("so14", 2000)
    _accept("so14", "a", 100, "req-q1")
    _accept("so14", "b", 200, "req-q2")
    _accept("so14", "c", 300, "req-q3")
    _action("so14", "a", "settle", "req-q4")
    _action("so14", "b", "cancel", "req-q5")

    all_items = client.get("/settlements", headers=H).json()["settlements"]
    mine = [s for s in all_items if s["order_id"] == "so14"]
    assert [s["settlement_id"] for s in mine] == ["a", "b", "c"]

    settled = client.get("/settlements?status=settled", headers=H).json()["settlements"]
    mine_settled = [s for s in settled if s["order_id"] == "so14"]
    assert [s["settlement_id"] for s in mine_settled] == ["a"]
    assert mine_settled[0]["effective_deduction_cents"] == 100

    ranged = client.get("/settlements?min_amount_cents=150&max_amount_cents=300", headers=H).json()["settlements"]
    assert [s["settlement_id"] for s in ranged if s["order_id"] == "so14"] == ["b", "c"]

    combo = client.get("/settlements?status=pending&min_amount_cents=250", headers=H).json()["settlements"]
    assert [s["settlement_id"] for s in combo if s["order_id"] == "so14"] == ["c"]

    assert client.get("/settlements?status=bogus", headers=H).status_code == 400
    assert client.get("/settlements?min_amount_cents=300&max_amount_cents=100", headers=H).status_code == 400


def test_tenant_header_required() -> None:
    assert client.post("/orders/so1/settlements",
                       json={"settlement_id": "x", "amount_cents": 1, "reason": "r",
                             "request_id": "z"}).status_code == 400
    assert client.get("/settlements").status_code == 400


def test_invalid_params_rejected_by_validation() -> None:
    _order("so15", 100)
    assert _accept("so15", "ss1", 0, "req-r1").status_code == 422
    assert _accept("so15", "ss1", -5, "req-r2").status_code == 422
    assert _accept("so15", "ss1", 10, "req-r3", reason="").status_code == 422


def test_state_survives_restart_and_replay() -> None:
    _order("so16", 800)
    _accept("so16", "ss1", 300, "req-s1")
    _action("so16", "ss1", "settle", "req-s2")
    _action("so16", "ss1", "reverse", "req-s3")
    _accept("so16", "ss2", 200, "req-s4")
    _action("so16", "ss2", "cancel", "req-s5")
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    order = client.get("/orders/so16", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 800
    by_id = {s["settlement_id"]: s
             for s in client.get("/orders/so16/settlements", headers=H).json()["settlements"]}
    assert by_id["ss1"]["status"] == "reversed" and by_id["ss1"]["effective_deduction_cents"] == 0
    assert by_id["ss2"]["status"] == "cancelled"
    # 重启后重放旧请求，结果一致且不重复生效
    replay = _action("so16", "ss1", "reverse", "req-s3")
    assert replay.status_code == 200 and replay.json()["status"] == "reversed"
    assert client.get("/orders/so16", headers=H).json()["paid_cents"] == 0
