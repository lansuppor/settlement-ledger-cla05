import os
import sqlite3
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_stock.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "stk"
H = {"X-Tenant": T}


def _accept(doc_no: str, warehouse: str = "W1", item: str = "SKU1", direction: str = "in",
            quantity: int = 10, request_id: str = "r", headers=H):
    return client.post("/stock-documents",
                       json={"doc_no": doc_no, "warehouse_id": warehouse, "item_id": item,
                             "direction": direction, "quantity": quantity, "request_id": request_id},
                       headers=headers)


def _action(doc_no: str, action: str, request_id: str, headers=H):
    return client.post(f"/stock-documents/{doc_no}/{action}",
                       json={"request_id": request_id}, headers=headers)


def _balance(warehouse: str = "W1", item: str = "SKU1", headers=H):
    return client.get(f"/stock-balance?warehouse_id={warehouse}&item_id={item}", headers=headers)


def test_accept_in_pending_does_not_change_book() -> None:
    r = _accept("d-in-1", warehouse="W0", item="S0", quantity=100, request_id="sq-a1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["direction"] == "in" and body["quantity"] == 100
    assert body["balance"] == {"warehouse_id": "W0", "item_id": "S0",
                               "book_qty": 0, "occupied_qty": 0, "available_qty": 0}
    # 待入账入库单不增加库存
    assert _balance("W0", "S0").json()["book_qty"] == 0


def test_balance_of_untouched_item_is_zero() -> None:
    body = _balance("W9", "SKU9").json()
    assert body == {"warehouse_id": "W9", "item_id": "SKU9",
                    "book_qty": 0, "occupied_qty": 0, "available_qty": 0}


def test_post_inbound_increases_book() -> None:
    _accept("d-in-2", warehouse="W1", item="SKU1", quantity=50, request_id="sq-b1")
    r = _action("d-in-2", "post", "sq-b2")
    assert r.status_code == 200 and r.json()["status"] == "posted"
    assert _action("d-in-2", "post", "sq-b2x").status_code == 409  # 重复入账拒绝
    bal = _balance().json()
    assert bal["book_qty"] == 50 and bal["occupied_qty"] == 0 and bal["available_qty"] == 50


def test_outbound_accept_occupies_and_over_accept_rejected() -> None:
    _accept("d-in-3", warehouse="W11", item="S11", quantity=30, request_id="sq-c1")
    _action("d-in-3", "post", "sq-c2")
    r = _accept("d-out-1", warehouse="W11", item="S11", direction="out", quantity=20, request_id="sq-c3")
    assert r.status_code == 201
    bal = r.json()["balance"]
    assert bal["book_qty"] == 30 and bal["occupied_qty"] == 20 and bal["available_qty"] == 10
    # 超额受理拒绝：可用只剩 10
    too_much = _accept("d-out-2", warehouse="W11", item="S11", direction="out", quantity=11, request_id="sq-c4")
    assert too_much.status_code == 409
    assert "insufficient" in too_much.json()["detail"]
    ok = _accept("d-out-2", warehouse="W11", item="S11", direction="out", quantity=10, request_id="sq-c5")
    assert ok.status_code == 201
    bal = _balance("W11", "S11").json()
    assert bal["occupied_qty"] == 30 and bal["available_qty"] == 0


def test_post_outbound_converts_occupation_to_book_deduction() -> None:
    _accept("d-in-4", warehouse="W12", item="S12", quantity=40, request_id="sq-d1")
    _action("d-in-4", "post", "sq-d2")
    _accept("d-out-3", warehouse="W12", item="S12", direction="out", quantity=15, request_id="sq-d3")
    r = _action("d-out-3", "post", "sq-d4")
    assert r.status_code == 200
    bal = r.json()["balance"]
    assert bal["book_qty"] == 25 and bal["occupied_qty"] == 0 and bal["available_qty"] == 25


def test_cancel_outbound_releases_occupation_without_book_change() -> None:
    _accept("d-in-5", warehouse="W13", item="S13", quantity=20, request_id="sq-e1")
    _action("d-in-5", "post", "sq-e2")
    _accept("d-out-4", warehouse="W13", item="S13", direction="out", quantity=12, request_id="sq-e3")
    r = _action("d-out-4", "cancel", "sq-e4")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    bal = _balance("W13", "S13").json()
    assert bal["book_qty"] == 20 and bal["occupied_qty"] == 0 and bal["available_qty"] == 20


def test_cancel_inbound_keeps_zero_book() -> None:
    _accept("d-in-6", warehouse="W2", item="S2", quantity=5, request_id="sq-f1")
    r = _action("d-in-6", "cancel", "sq-f2")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert _balance("W2", "S2").json()["book_qty"] == 0


def test_duplicate_doc_no_rejected_without_mutation() -> None:
    assert _accept("d-dup", quantity=5, request_id="sq-g1").status_code == 201
    dup = _accept("d-dup", quantity=9, request_id="sq-g2")
    assert dup.status_code == 409
    assert "already accepted" in dup.json()["detail"]
    got = client.get("/stock-documents/d-dup", headers=H).json()
    assert got["quantity"] == 5 and got["status"] == "pending"


def test_illegal_transitions_rejected_and_state_unchanged() -> None:
    _accept("d-st", quantity=8, request_id="sq-h1")
    _action("d-st", "post", "sq-h2")
    # 已入账不可再入账或撤销
    assert _action("d-st", "post", "sq-h3").status_code == 409
    assert _action("d-st", "cancel", "sq-h4").status_code == 409
    # 待入账不可冲正
    _accept("d-st2", quantity=3, request_id="sq-h5")
    assert _action("d-st2", "reverse", "sq-h6").status_code == 409
    # 已撤销不可再变更
    _action("d-st2", "cancel", "sq-h7")
    assert _action("d-st2", "cancel", "sq-h8").status_code == 409
    assert _action("d-st2", "post", "sq-h9").status_code == 409
    assert _action("d-st2", "reverse", "sq-h10").status_code == 409
    # 非法推进不改单据与余额
    assert client.get("/stock-documents/d-st", headers=H).json()["status"] == "posted"
    assert client.get("/stock-documents/d-st2", headers=H).json()["status"] == "cancelled"


def test_reverse_inbound_deducts_book_once() -> None:
    _accept("d-rv-in", warehouse="W3", item="S3", quantity=25, request_id="sq-i1")
    _action("d-rv-in", "post", "sq-i2")
    r = _action("d-rv-in", "reverse", "sq-i3")
    assert r.status_code == 200 and r.json()["status"] == "reversed"
    assert r.json()["balance"]["book_qty"] == 0
    # 冲正仅一次；已冲正不可再变更
    assert _action("d-rv-in", "reverse", "sq-i4").status_code == 409
    assert _action("d-rv-in", "post", "sq-i5").status_code == 409
    assert _action("d-rv-in", "cancel", "sq-i6").status_code == 409


def test_reverse_inbound_blocked_when_book_would_fall_below_occupied() -> None:
    _accept("d-rv-in2", warehouse="W4", item="S4", quantity=10, request_id="sq-j1")
    _action("d-rv-in2", "post", "sq-j2")
    _accept("d-rv-out2", warehouse="W4", item="S4", direction="out", quantity=6, request_id="sq-j3")
    # 账面 10、占用 6：冲正入库 10 会使账面 0 < 占用 6，拒绝且保持原状态
    r = _action("d-rv-in2", "reverse", "sq-j4")
    assert r.status_code == 409
    assert "below occupied" in r.json()["detail"]
    assert client.get("/stock-documents/d-rv-in2", headers=H).json()["status"] == "posted"
    bal = _balance("W4", "S4").json()
    assert bal["book_qty"] == 10 and bal["occupied_qty"] == 6 and bal["available_qty"] == 4
    # 占用释放后可冲正
    _action("d-rv-out2", "cancel", "sq-j5")
    assert _action("d-rv-in2", "reverse", "sq-j6").status_code == 200
    assert _balance("W4", "S4").json()["book_qty"] == 0


def test_reverse_outbound_adds_back() -> None:
    _accept("d-rv-in3", warehouse="W5", item="S5", quantity=30, request_id="sq-k1")
    _action("d-rv-in3", "post", "sq-k2")
    _accept("d-rv-out3", warehouse="W5", item="S5", direction="out", quantity=18, request_id="sq-k3")
    _action("d-rv-out3", "post", "sq-k4")
    assert _balance("W5", "S5").json()["book_qty"] == 12
    r = _action("d-rv-out3", "reverse", "sq-k5")
    assert r.status_code == 200 and r.json()["status"] == "reversed"
    bal = _balance("W5", "S5").json()
    assert bal["book_qty"] == 30 and bal["occupied_qty"] == 0 and bal["available_qty"] == 30


def test_idempotent_replays_and_conflict() -> None:
    first = _accept("d-idem", quantity=7, request_id="sq-l1")
    replay = _accept("d-idem", quantity=7, request_id="sq-l1")
    assert replay.status_code == first.status_code == 201
    assert replay.json() == first.json()

    p1 = _action("d-idem", "post", "sq-l2")
    p2 = _action("d-idem", "post", "sq-l2")
    assert p1.status_code == p2.status_code == 200 and p1.json() == p2.json()
    assert _balance().json()["book_qty"] >= 7  # 只入账一次

    # 失败响应同样可重放
    f1 = _accept("d-idem-out", direction="out", quantity=10**9, request_id="sq-l3")
    f2 = _accept("d-idem-out", direction="out", quantity=10**9, request_id="sq-l3")
    assert f1.status_code == f2.status_code == 409 and f1.json() == f2.json()

    # 同一 request_id 改作其他操作/对象/参数 -> 409
    assert _action("d-idem", "cancel", "sq-l1").status_code == 409
    assert _accept("d-idem", quantity=8, request_id="sq-l1").status_code == 409
    assert _accept("d-idem-other", quantity=7, request_id="sq-l1").status_code == 409


def test_idempotent_outbound_replay_does_not_double_occupy() -> None:
    _accept("d-io-in", warehouse="W6", item="S6", quantity=10, request_id="sq-m1")
    _action("d-io-in", "post", "sq-m2")
    a1 = _accept("d-io-out", warehouse="W6", item="S6", direction="out", quantity=6, request_id="sq-m3")
    a2 = _accept("d-io-out", warehouse="W6", item="S6", direction="out", quantity=6, request_id="sq-m3")
    assert a1.status_code == a2.status_code == 201 and a1.json() == a2.json()
    bal = _balance("W6", "S6").json()
    assert bal["occupied_qty"] == 6 and bal["available_qty"] == 4


def test_concurrent_outbound_accepts_never_oversell() -> None:
    _accept("d-cc-in", warehouse="W7", item="S7", quantity=100, request_id="sq-n1")
    _action("d-cc-in", "post", "sq-n2")
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_accept(f"d-cc-out{i}", warehouse="W7", item="S7",
                               direction="out", quantity=30, request_id=f"sq-nx{i}").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 3
    assert sorted(results).count(409) == 5
    bal = _balance("W7", "S7").json()
    assert bal["occupied_qty"] == 90 and bal["available_qty"] == 10 and bal["book_qty"] == 100


def test_concurrent_same_doc_no_single_winner() -> None:
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(rid: str) -> None:
        barrier.wait()
        results.append(_accept("d-cc-same", quantity=4, request_id=rid).status_code)

    threads = [threading.Thread(target=worker, args=(f"sq-o{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]


def test_cross_tenant_is_not_found_and_missing_tenant_rejected() -> None:
    _accept("d-tenant", quantity=5, request_id="sq-p1")
    other = {"X-Tenant": "stk-other"}
    assert client.get("/stock-documents/d-tenant", headers=other).status_code == 404
    assert _action("d-tenant", "post", "sq-p2", headers=other).status_code == 404
    assert _action("d-tenant", "cancel", "sq-p3", headers=other).status_code == 404
    assert _action("d-tenant", "reverse", "sq-p4", headers=other).status_code == 404
    # 跨租户余额互不可见
    assert _balance(headers=other).json()["book_qty"] == 0
    # 跨租户操作未影响本租户状态
    assert client.get("/stock-documents/d-tenant", headers=H).json()["status"] == "pending"
    # 缺租户头明确拒绝
    no_tenant = {"X-Tenant": ""}
    assert client.get("/stock-documents/d-tenant", headers=no_tenant).status_code == 400
    assert client.get("/stock-balance?warehouse_id=W1&item_id=SKU1", headers=no_tenant).status_code == 400
    assert _accept("d-x", request_id="sq-p5", headers=no_tenant).status_code == 400
    assert _action("d-tenant", "post", "sq-p6", headers=no_tenant).status_code == 400


def test_invalid_params_rejected_by_validation() -> None:
    assert _accept("d-bad1", quantity=0, request_id="sq-q1").status_code == 422
    assert _accept("d-bad2", quantity=-3, request_id="sq-q2").status_code == 422
    assert _accept("d-bad3", quantity=1.5, request_id="sq-q3").status_code == 422
    assert _accept("d-bad4", warehouse="", request_id="sq-q4").status_code == 422
    assert _accept("d-bad5", item="", request_id="sq-q5").status_code == 422
    assert _accept("d-bad6", direction="sideways", request_id="sq-q6").status_code == 422
    assert client.get("/stock-balance?warehouse_id=W1", headers=H).status_code == 400


def test_read_document_shape() -> None:
    _accept("d-shape-in", warehouse="W8", item="S8", quantity=1, request_id="sq-r1")
    _action("d-shape-in", "post", "sq-r2")
    got = client.get("/stock-documents/d-shape-in", headers=H)
    assert got.status_code == 200
    assert set(got.json()) == {"doc_no", "warehouse_id", "item_id", "direction", "quantity", "status"}
    assert client.get("/stock-documents/d-nope", headers=H).status_code == 404


def test_state_survives_restart_and_replay() -> None:
    _accept("d-rs-in", warehouse="W10", item="S10", quantity=20, request_id="sq-s1")
    _action("d-rs-in", "post", "sq-s2")
    _accept("d-rs-out", warehouse="W10", item="S10", direction="out", quantity=8, request_id="sq-s3")
    _action("d-rs-out", "post", "sq-s4")
    _action("d-rs-out", "reverse", "sq-s5")
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    assert client.get("/stock-documents/d-rs-out", headers=H).json()["status"] == "reversed"
    bal = _balance("W10", "S10").json()
    assert bal["book_qty"] == 20 and bal["occupied_qty"] == 0
    # 重启后重放旧请求，结果一致且不重复生效
    replay = _action("d-rs-out", "reverse", "sq-s5")
    assert replay.status_code == 200 and replay.json()["status"] == "reversed"
    assert _balance("W10", "S10").json()["book_qty"] == 20
    # 幂等记录落库可直接读取
    raw = sqlite3.connect(db_path())
    row = raw.execute(
        "SELECT op, http_status FROM stock_requests WHERE tenant='stk' AND request_id='sq-s5'").fetchone()
    raw.close()
    assert row == ("reverse", 200)
