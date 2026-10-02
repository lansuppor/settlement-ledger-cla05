import os
import sqlite3
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_refunds.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "rt"
H = {"X-Tenant": T}


def _paid_order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _accept(order_id: str, refund_id: str, amount: int, request_id: str, headers=H):
    return client.post(f"/orders/{order_id}/refunds",
                       json={"refund_id": refund_id, "amount_cents": amount, "request_id": request_id},
                       headers=headers)


def _action(order_id: str, refund_id: str, action: str, request_id: str, headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/{action}",
                       json={"request_id": request_id}, headers=headers)


def test_accept_occupies_and_complete_deducts() -> None:
    _paid_order("ro1", 1000, 1000)
    r = _accept("ro1", "rf1", 300, "req-a1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["amount_cents"] == 300
    assert body["effective_deduction_cents"] == 0
    # 受理即占用：再受理 800 超过可退余额 700
    assert _accept("ro1", "rf2", 800, "req-a2").status_code == 409
    # 既有数据未被失败请求改变
    order = client.get("/orders/ro1", headers=H).json()
    assert order["paid_cents"] == 1000

    done = _action("ro1", "rf1", "complete", "req-c1")
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["effective_deduction_cents"] == 300
    order = client.get("/orders/ro1", headers=H).json()
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300
    # 完成后占用释放为 0，可退余额为 700
    assert _accept("ro1", "rf3", 700, "req-a3").status_code == 201
    assert _accept("ro1", "rf4", 1, "req-a4").status_code == 409


def test_duplicate_refund_id_refused_without_mutation() -> None:
    _paid_order("ro2", 500, 500)
    assert _accept("ro2", "rf1", 100, "req-b1").status_code == 201
    dup = _accept("ro2", "rf1", 200, "req-b2")
    assert dup.status_code == 409
    got = client.get("/orders/ro2/refunds/rf1", headers=H).json()
    assert got["amount_cents"] == 100 and got["status"] == "pending"


def test_accept_order_not_found() -> None:
    assert _accept("missing", "rf1", 1, "req-m1").status_code == 404


def test_accept_exceeds_unpaid_balance() -> None:
    _paid_order("ro3", 1000, 400)
    assert _accept("ro3", "rf1", 401, "req-e1").status_code == 409
    assert _accept("ro3", "rf1", 400, "req-e2").status_code == 201


def test_cancel_releases_occupation() -> None:
    _paid_order("ro4", 500, 500)
    assert _accept("ro4", "rf1", 500, "req-f1").status_code == 201
    assert _accept("ro4", "rf2", 1, "req-f2").status_code == 409
    assert _action("ro4", "rf1", "cancel", "req-f3").status_code == 200
    # 撤销释放占用，且不扣减订单金额
    order = client.get("/orders/ro4", headers=H).json()
    assert order["paid_cents"] == 500
    assert _accept("ro4", "rf2", 500, "req-f4").status_code == 201


def test_terminal_transitions_rejected() -> None:
    _paid_order("ro5", 300, 300)
    _accept("ro5", "rf1", 100, "req-g1")
    assert _action("ro5", "rf1", "complete", "req-g2").status_code == 200
    # 重复完成
    assert _action("ro5", "rf1", "complete", "req-g3").status_code == 409
    # 已完成不可撤销
    assert _action("ro5", "rf1", "cancel", "req-g4").status_code == 409
    order = client.get("/orders/ro5", headers=H).json()
    assert order["paid_cents"] == 200

    _accept("ro5", "rf2", 100, "req-g5")
    assert _action("ro5", "rf2", "cancel", "req-g6").status_code == 200
    assert _action("ro5", "rf2", "cancel", "req-g7").status_code == 409
    assert _action("ro5", "rf2", "complete", "req-g8").status_code == 409


def test_reverse_adds_back_once_and_is_terminal() -> None:
    _paid_order("ro6", 400, 400)
    _accept("ro6", "rf1", 400, "req-h1")
    _action("ro6", "rf1", "complete", "req-h2")
    rev = _action("ro6", "rf1", "reverse", "req-h3")
    assert rev.status_code == 200
    assert rev.json()["status"] == "reversed"
    assert rev.json()["effective_deduction_cents"] == 0
    order = client.get("/orders/ro6", headers=H).json()
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 0
    # 重复冲正与冲正后撤销都被拒绝
    assert _action("ro6", "rf1", "reverse", "req-h4").status_code == 409
    assert _action("ro6", "rf1", "cancel", "req-h5").status_code == 409
    # 待处理单不能冲正
    _accept("ro6", "rf2", 10, "req-h6")
    assert _action("ro6", "rf2", "reverse", "req-h7").status_code == 409


def test_reverse_is_atomic_on_both_sides() -> None:
    _paid_order("ro7", 200, 200)
    _accept("ro7", "rf1", 200, "req-i1")
    _action("ro7", "rf1", "complete", "req-i2")
    assert _action("ro7", "rf1", "reverse", "req-i3").status_code == 200
    raw = sqlite3.connect(db_path())
    paid, status = raw.execute(
        "SELECT paid_cents, status FROM orders WHERE tenant='rt' AND order_id='ro7'").fetchone()
    rstatus, deduct = raw.execute(
        "SELECT status, effective_deduction_cents FROM refunds"
        " WHERE tenant='rt' AND order_id='ro7' AND refund_id='rf1'").fetchone()
    raw.close()
    assert (paid, status) == (200, "settled")
    assert (rstatus, deduct) == ("reversed", 0)


def test_idempotent_replays() -> None:
    _paid_order("ro8", 600, 600)
    first = _accept("ro8", "rf1", 100, "req-j1")
    replay = _accept("ro8", "rf1", 100, "req-j1")
    assert replay.status_code == first.status_code == 201
    assert replay.json() == first.json()
    # 重放不产生重复单据
    rows = client.get("/orders/ro8/refunds", headers=H).json()["refunds"]
    assert len(rows) == 1

    c1 = _action("ro8", "rf1", "complete", "req-j2")
    c2 = _action("ro8", "rf1", "complete", "req-j2")
    assert c1.status_code == c2.status_code == 200 and c1.json() == c2.json()
    assert client.get("/orders/ro8", headers=H).json()["paid_cents"] == 500

    v1 = _action("ro8", "rf1", "reverse", "req-j3")
    v2 = _action("ro8", "rf1", "reverse", "req-j3")
    assert v1.status_code == v2.status_code == 200 and v1.json() == v2.json()
    assert client.get("/orders/ro8", headers=H).json()["paid_cents"] == 600

    # 同一 request_id 改作其他操作 -> 冲突
    assert _action("ro8", "rf1", "cancel", "req-j1").status_code == 409
    # 失败响应同样可重放
    f1 = _accept("ro8", "rf9", 10000, "req-j4")
    f2 = _accept("ro8", "rf9", 10000, "req-j4")
    assert f1.status_code == f2.status_code == 409 and f1.json() == f2.json()


def test_concurrent_accepts_never_exceed_balance() -> None:
    _paid_order("ro9", 1000, 1000)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        r = _accept("ro9", f"cf{i}", 300, f"req-k{i}")
        results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 3
    assert sorted(results).count(409) == 5
    order = client.get("/orders/ro9", headers=H).json()
    assert order["paid_cents"] == 1000  # 占用不影响已收
    # 全部完成后守恒：扣减总额 900
    for i in range(8):
        if client.get(f"/orders/ro9/refunds/cf{i}", headers=H).status_code == 200:
            _action("ro9", f"cf{i}", "complete", f"req-kc{i}")
    assert client.get("/orders/ro9", headers=H).json()["paid_cents"] == 100


def test_concurrent_same_refund_id_single_winner() -> None:
    _paid_order("ro10", 500, 500)
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(rid: str) -> None:
        barrier.wait()
        results.append(_accept("ro10", "same", 100, rid).status_code)

    threads = [threading.Thread(target=worker, args=(f"req-l{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]


def test_cross_tenant_is_not_found_everywhere() -> None:
    _paid_order("ro11", 100, 100)
    _accept("ro11", "rf1", 100, "req-n1")
    other = {"X-Tenant": "rt-other"}
    assert client.get("/orders/ro11/refunds/rf1", headers=other).status_code == 404
    listed = client.get("/orders/ro11/refunds", headers=other)
    assert listed.status_code == 404
    assert _accept("ro11", "rf1", 100, "req-n2", headers=other).status_code == 404
    assert _action("ro11", "rf1", "complete", "req-n3", headers=other).status_code == 404
    # 跨租户操作未影响本租户状态
    assert client.get("/orders/ro11/refunds/rf1", headers=H).json()["status"] == "pending"


def test_list_shape_and_ordering() -> None:
    _paid_order("ro12", 900, 900)
    _accept("ro12", "a", 100, "req-o1")
    _accept("ro12", "b", 200, "req-o2")
    _action("ro12", "a", "complete", "req-o3")
    _accept("ro12", "c", 300, "req-o4")
    _action("ro12", "c", "cancel", "req-o5")
    listed = client.get("/orders/ro12/refunds", headers=H)
    assert listed.status_code == 200
    items = listed.json()["refunds"]
    assert [r["refund_id"] for r in items] == ["a", "b", "c"]
    by_id = {r["refund_id"]: r for r in items}
    assert by_id["a"]["status"] == "completed" and by_id["a"]["effective_deduction_cents"] == 100
    assert by_id["b"]["status"] == "pending" and by_id["b"]["effective_deduction_cents"] == 0
    assert by_id["c"]["status"] == "cancelled"
    for r in items:
        assert set(r) == {"order_id", "refund_id", "amount_cents", "status", "effective_deduction_cents"}


def test_tenant_header_required() -> None:
    assert client.post("/orders/ro1/refunds",
                       json={"refund_id": "x", "amount_cents": 1, "request_id": "z"}).status_code == 400


def test_invalid_amount_rejected_by_validation() -> None:
    _paid_order("ro13", 100, 100)
    assert _accept("ro13", "rf1", 0, "req-p1").status_code == 422
    assert _accept("ro13", "rf1", -5, "req-p2").status_code == 422


def test_state_survives_restart_and_replay() -> None:
    _paid_order("ro14", 800, 800)
    _accept("ro14", "rf1", 300, "req-q1")
    _action("ro14", "rf1", "complete", "req-q2")
    _action("ro14", "rf1", "reverse", "req-q3")
    _accept("ro14", "rf2", 200, "req-q4")
    _action("ro14", "rf2", "cancel", "req-q5")
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    order = client.get("/orders/ro14", headers=H).json()
    assert order["paid_cents"] == 800 and order["outstanding_cents"] == 0
    by_id = {r["refund_id"]: r for r in client.get("/orders/ro14/refunds", headers=H).json()["refunds"]}
    assert by_id["rf1"]["status"] == "reversed" and by_id["rf1"]["effective_deduction_cents"] == 0
    assert by_id["rf2"]["status"] == "cancelled"
    # 重启后重放旧请求，结果一致且不重复生效
    replay = _action("ro14", "rf1", "reverse", "req-q3")
    assert replay.status_code == 200 and replay.json()["status"] == "reversed"
    assert client.get("/orders/ro14", headers=H).json()["paid_cents"] == 800
