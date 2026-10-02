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


def _order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _accept(order_id: str, settlement_id: str, amount: int, request_id: str,
            reason: str = "对账核销", headers=H):
    return client.post(f"/orders/{order_id}/settlements",
                       json={"settlement_id": settlement_id, "amount_cents": amount,
                             "reason": reason, "request_id": request_id},
                       headers=headers)


def _action(order_id: str, settlement_id: str, action: str, request_id: str, headers=H):
    return client.post(f"/orders/{order_id}/settlements/{settlement_id}/{action}",
                       json={"request_id": request_id}, headers=headers)


def test_accept_occupies_and_writeoff_adds_to_paid() -> None:
    _order("so1", 1000)
    r = _accept("so1", "s1", 300, "req-a1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["amount_cents"] == 300
    assert body["effective_deduction_cents"] == 0 and body["reason"] == "对账核销"
    # 受理即占用：未收余额剩 700，再受理 800 超限
    assert _accept("so1", "s2", 800, "req-a2").status_code == 409
    # 既有数据未被失败请求改变
    order = client.get("/orders/so1", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000

    done = _action("so1", "s1", "writeoff", "req-a3")
    assert done.status_code == 200
    assert done.json()["status"] == "written_off"
    assert done.json()["effective_deduction_cents"] == 300
    order = client.get("/orders/so1", headers=H).json()
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 700
    # 核销后占用转化为已收，未收余额仍为 700
    assert _accept("so1", "s3", 700, "req-a4").status_code == 201
    assert _accept("so1", "s4", 1, "req-a5").status_code == 409


def test_duplicate_settlement_id_refused_without_mutation() -> None:
    _order("so2", 500)
    assert _accept("so2", "s1", 100, "req-b1").status_code == 201
    dup = _accept("so2", "s1", 200, "req-b2")
    assert dup.status_code == 409
    got = client.get("/orders/so2/settlements/s1", headers=H).json()
    assert got["amount_cents"] == 100 and got["status"] == "pending"


def test_accept_order_not_found() -> None:
    assert _accept("missing", "s1", 1, "req-m1").status_code == 404


def test_accept_exceeds_outstanding_amount() -> None:
    _order("so3", 1000, paid=400)
    assert _accept("so3", "s1", 601, "req-e1").status_code == 409
    assert _accept("so3", "s1", 600, "req-e2").status_code == 201


def test_cancel_releases_occupation() -> None:
    _order("so4", 500)
    assert _accept("so4", "s1", 500, "req-f1").status_code == 201
    assert _accept("so4", "s2", 1, "req-f2").status_code == 409
    assert _action("so4", "s1", "cancel", "req-f3").status_code == 200
    # 撤销释放占用，且不改动订单金额
    order = client.get("/orders/so4", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 500
    assert _accept("so4", "s2", 500, "req-f4").status_code == 201


def test_terminal_transitions_rejected() -> None:
    _order("so5", 300)
    _accept("so5", "s1", 100, "req-g1")
    assert _action("so5", "s1", "writeoff", "req-g2").status_code == 200
    # 重复核销
    assert _action("so5", "s1", "writeoff", "req-g3").status_code == 409
    # 已核销不可撤销
    assert _action("so5", "s1", "cancel", "req-g4").status_code == 409
    order = client.get("/orders/so5", headers=H).json()
    assert order["paid_cents"] == 100

    _accept("so5", "s2", 100, "req-g5")
    assert _action("so5", "s2", "cancel", "req-g6").status_code == 200
    assert _action("so5", "s2", "cancel", "req-g7").status_code == 409
    assert _action("so5", "s2", "writeoff", "req-g8").status_code == 409


def test_reverse_deducts_back_once_and_is_terminal() -> None:
    _order("so6", 400)
    _accept("so6", "s1", 400, "req-h1")
    _action("so6", "s1", "writeoff", "req-h2")
    rev = _action("so6", "s1", "reverse", "req-h3")
    assert rev.status_code == 200
    assert rev.json()["status"] == "reversed"
    assert rev.json()["effective_deduction_cents"] == 0
    order = client.get("/orders/so6", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 400
    # 重复冲正与冲正后撤销都被拒绝
    assert _action("so6", "s1", "reverse", "req-h4").status_code == 409
    assert _action("so6", "s1", "cancel", "req-h5").status_code == 409
    # 待核销单不能冲正
    _accept("so6", "s2", 10, "req-h6")
    assert _action("so6", "s2", "reverse", "req-h7").status_code == 409


def test_reverse_is_atomic_on_both_sides() -> None:
    _order("so7", 200)
    _accept("so7", "s1", 200, "req-i1")
    _action("so7", "s1", "writeoff", "req-i2")
    assert client.get("/orders/so7", headers=H).json()["status"] == "settled"
    assert _action("so7", "s1", "reverse", "req-i3").status_code == 200
    raw = sqlite3.connect(db_path())
    paid, status = raw.execute(
        "SELECT paid_cents, status FROM orders WHERE tenant='st' AND order_id='so7'").fetchone()
    sstatus = raw.execute(
        "SELECT status FROM settlements"
        " WHERE tenant='st' AND order_id='so7' AND settlement_id='s1'").fetchone()[0]
    raw.close()
    assert (paid, status) == (0, "accepted")
    assert sstatus == "reversed"


def test_idempotent_replays() -> None:
    _order("so8", 600)
    first = _accept("so8", "s1", 100, "req-j1")
    replay = _accept("so8", "s1", 100, "req-j1")
    assert replay.status_code == first.status_code == 201
    assert replay.json() == first.json()
    # 重放不产生重复单据
    rows = client.get("/orders/so8/settlements", headers=H).json()["settlements"]
    assert len(rows) == 1

    c1 = _action("so8", "s1", "writeoff", "req-j2")
    c2 = _action("so8", "s1", "writeoff", "req-j2")
    assert c1.status_code == c2.status_code == 200 and c1.json() == c2.json()
    assert client.get("/orders/so8", headers=H).json()["paid_cents"] == 100

    v1 = _action("so8", "s1", "reverse", "req-j3")
    v2 = _action("so8", "s1", "reverse", "req-j3")
    assert v1.status_code == v2.status_code == 200 and v1.json() == v2.json()
    assert client.get("/orders/so8", headers=H).json()["paid_cents"] == 0

    # 同一 request_id 改作其他操作 -> 冲突
    assert _action("so8", "s1", "cancel", "req-j1").status_code == 409
    # 失败响应同样可重放
    f1 = _accept("so8", "s9", 10000, "req-j4")
    f2 = _accept("so8", "s9", 10000, "req-j4")
    assert f1.status_code == f2.status_code == 409 and f1.json() == f2.json()


def test_concurrent_accepts_never_exceed_outstanding() -> None:
    _order("so9", 1000)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        r = _accept("so9", f"cs{i}", 300, f"req-k{i}")
        results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 3
    assert sorted(results).count(409) == 5
    order = client.get("/orders/so9", headers=H).json()
    assert order["paid_cents"] == 0  # 占用不影响已收
    # 全部核销后守恒：已收总额 900，绝不超额
    for i in range(8):
        if client.get(f"/orders/so9/settlements/cs{i}", headers=H).status_code == 200:
            _action("so9", f"cs{i}", "writeoff", f"req-kw{i}")
    assert client.get("/orders/so9", headers=H).json()["paid_cents"] == 900


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


def test_cross_tenant_is_not_found_everywhere() -> None:
    _order("so11", 100)
    _accept("so11", "s1", 100, "req-n1")
    other = {"X-Tenant": "st-other"}
    assert client.get("/orders/so11/settlements/s1", headers=other).status_code == 404
    assert client.get("/orders/so11/settlements", headers=other).status_code == 404
    assert _accept("so11", "s1", 100, "req-n2", headers=other).status_code == 404
    assert _action("so11", "s1", "writeoff", "req-n3", headers=other).status_code == 404
    # 跨租户检索看不到本租户单据
    assert client.get("/settlements", headers=other).json()["settlements"] == []
    # 跨租户操作未影响本租户状态
    assert client.get("/orders/so11/settlements/s1", headers=H).json()["status"] == "pending"


def test_list_shape_and_ordering() -> None:
    _order("so12", 900)
    _accept("so12", "a", 100, "req-o1")
    _accept("so12", "b", 200, "req-o2")
    _action("so12", "a", "writeoff", "req-o3")
    _accept("so12", "c", 300, "req-o4")
    _action("so12", "c", "cancel", "req-o5")
    listed = client.get("/orders/so12/settlements", headers=H)
    assert listed.status_code == 200
    items = listed.json()["settlements"]
    assert [s["settlement_id"] for s in items] == ["a", "b", "c"]
    by_id = {s["settlement_id"]: s for s in items}
    assert by_id["a"]["status"] == "written_off" and by_id["a"]["effective_deduction_cents"] == 100
    assert by_id["b"]["status"] == "pending" and by_id["b"]["effective_deduction_cents"] == 0
    assert by_id["c"]["status"] == "cancelled"
    for s in items:
        assert set(s) == {"order_id", "settlement_id", "amount_cents", "reason",
                          "status", "effective_deduction_cents"}


def test_search_by_status_and_amount_range() -> None:
    _order("so13", 2000)
    _accept("so13", "s1", 100, "req-r1")
    _accept("so13", "s2", 200, "req-r2")
    _accept("so13", "s3", 300, "req-r3")
    _action("so13", "s1", "writeoff", "req-r4")
    _action("so13", "s3", "cancel", "req-r5")

    all_items = client.get("/settlements", headers=H).json()["settlements"]
    mine = [s for s in all_items if s["order_id"] == "so13"]
    assert [s["settlement_id"] for s in mine] == ["s1", "s2", "s3"]

    pending = client.get("/settlements?status=pending", headers=H).json()["settlements"]
    assert [s["settlement_id"] for s in pending if s["order_id"] == "so13"] == ["s2"]

    written = client.get("/settlements?status=written_off", headers=H).json()["settlements"]
    hit = [s for s in written if s["order_id"] == "so13"]
    assert [s["settlement_id"] for s in hit] == ["s1"]
    assert hit[0]["effective_deduction_cents"] == 100

    ranged = client.get("/settlements?min_amount_cents=150&max_amount_cents=300",
                        headers=H).json()["settlements"]
    assert [s["settlement_id"] for s in ranged if s["order_id"] == "so13"] == ["s2", "s3"]

    combo = client.get("/settlements?status=cancelled&min_amount_cents=150",
                       headers=H).json()["settlements"]
    assert [s["settlement_id"] for s in combo if s["order_id"] == "so13"] == ["s3"]

    assert client.get("/settlements?status=bogus", headers=H).status_code == 400


def test_tenant_header_required() -> None:
    assert client.post("/orders/so1/settlements",
                       json={"settlement_id": "x", "amount_cents": 1,
                             "reason": "r", "request_id": "z"}).status_code == 400
    assert client.get("/settlements").status_code == 400


def test_invalid_input_rejected_by_validation() -> None:
    _order("so14", 100)
    assert _accept("so14", "s1", 0, "req-p1").status_code == 422
    assert _accept("so14", "s1", -5, "req-p2").status_code == 422
    assert _accept("so14", "s1", 10, "req-p3", reason="").status_code == 422


def test_state_survives_restart_and_replay() -> None:
    _order("so15", 800)
    _accept("so15", "s1", 300, "req-q1")
    _action("so15", "s1", "writeoff", "req-q2")
    _action("so15", "s1", "reverse", "req-q3")
    _accept("so15", "s2", 200, "req-q4")
    _action("so15", "s2", "cancel", "req-q5")
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    order = client.get("/orders/so15", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 800
    by_id = {s["settlement_id"]: s
             for s in client.get("/orders/so15/settlements", headers=H).json()["settlements"]}
    assert by_id["s1"]["status"] == "reversed" and by_id["s1"]["effective_deduction_cents"] == 0
    assert by_id["s2"]["status"] == "cancelled"
    # 重启后重放旧请求，结果一致且不重复生效
    replay = _action("so15", "s1", "reverse", "req-q3")
    assert replay.status_code == 200 and replay.json()["status"] == "reversed"
    assert client.get("/orders/so15", headers=H).json()["paid_cents"] == 0
