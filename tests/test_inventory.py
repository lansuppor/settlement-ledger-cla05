import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_inventory.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "inv"
H = {"X-Tenant": T}


def _accept(doc_id: str, direction: str, quantity, request_id: str,
            warehouse_id: str = "W-1", item_id: str = "SKU-1", headers=H):
    return client.post("/inventory-docs",
                       json={"doc_id": doc_id, "warehouse_id": warehouse_id, "item_id": item_id,
                             "direction": direction, "quantity": quantity, "request_id": request_id},
                       headers=headers)


def _action(doc_id: str, action: str, request_id: str, headers=H):
    return client.post(f"/inventory-docs/{doc_id}/{action}",
                       json={"request_id": request_id}, headers=headers)


def _balance(warehouse_id: str = "W-1", item_id: str = "SKU-1", headers=H) -> dict:
    r = client.get(f"/inventory-balance?warehouse_id={warehouse_id}&item_id={item_id}", headers=headers)
    assert r.status_code == 200
    return r.json()


def test_accept_inbound_pending_does_not_add_stock() -> None:
    r = _accept("in-1", "inbound", 100, "req-i1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending" and body["direction"] == "inbound"
    assert body["warehouse_id"] == "W-1" and body["item_id"] == "SKU-1"
    # 待入账入库不增加库存
    bal = _balance()
    assert bal == {"warehouse_id": "W-1", "item_id": "SKU-1",
                   "book_quantity": 0, "reserved_quantity": 0, "available_quantity": 0}


def test_post_inbound_increases_book() -> None:
    done = _action("in-1", "post", "req-i2")
    assert done.status_code == 200
    assert done.json()["status"] == "posted"
    bal = _balance()
    assert bal["book_quantity"] == 100 and bal["available_quantity"] == 100


def test_outbound_reserves_immediately_and_over_accept_rejected() -> None:
    r = _accept("out-1", "outbound", 40, "req-o1")
    assert r.status_code == 201
    bal = _balance()
    # 受理即占用：可用 = 账面 - 占用
    assert bal["book_quantity"] == 100 and bal["reserved_quantity"] == 40
    assert bal["available_quantity"] == 60
    # 超额受理拒绝，余额不变
    over = _accept("out-2", "outbound", 61, "req-o2")
    assert over.status_code == 409
    assert over.json()["reason"] == "insufficient_available"
    assert _balance()["reserved_quantity"] == 40
    assert client.get("/inventory-docs/out-2", headers=H).status_code == 404


def test_post_outbound_converts_reservation_to_book_deduction() -> None:
    done = _action("out-1", "post", "req-o3")
    assert done.status_code == 200
    bal = _balance()
    assert bal["book_quantity"] == 60 and bal["reserved_quantity"] == 0
    assert bal["available_quantity"] == 60


def test_cancel_pending_outbound_releases_reservation() -> None:
    assert _accept("out-3", "outbound", 20, "req-o4").status_code == 201
    assert _balance()["reserved_quantity"] == 20
    done = _action("out-3", "cancel", "req-o5")
    assert done.status_code == 200
    assert done.json()["status"] == "cancelled"
    bal = _balance()
    assert bal["reserved_quantity"] == 0 and bal["book_quantity"] == 60


def test_cancel_pending_inbound_keeps_book() -> None:
    assert _accept("in-2", "inbound", 10, "req-i3").status_code == 201
    assert _action("in-2", "cancel", "req-i4").status_code == 200
    assert _balance()["book_quantity"] == 60


def test_reverse_posted_outbound_adds_back() -> None:
    done = _action("out-1", "reverse", "req-o6")
    assert done.status_code == 200
    assert done.json()["status"] == "reversed"
    bal = _balance()
    assert bal["book_quantity"] == 100 and bal["reserved_quantity"] == 0


def test_reverse_posted_inbound_deducts_book() -> None:
    done = _action("in-1", "reverse", "req-i5")
    assert done.status_code == 200
    assert _balance()["book_quantity"] == 0


def test_reverse_inbound_blocked_when_book_would_drop_below_reserved() -> None:
    # 入账 50 入库，再入账 30 出库，账面 20；另有 20 待入账出库占用
    assert _accept("in-3", "inbound", 50, "req-i6").status_code == 201
    assert _action("in-3", "post", "req-i7").status_code == 200
    assert _accept("out-4", "outbound", 30, "req-o7").status_code == 201
    assert _action("out-4", "post", "req-o8").status_code == 200
    assert _accept("out-5", "outbound", 20, "req-o9").status_code == 201
    bal = _balance()
    assert bal["book_quantity"] == 20 and bal["reserved_quantity"] == 20
    # 冲正入库 50 会使账面 -30 < 占用 20：拒绝且保持原状态
    r = _action("in-3", "reverse", "req-i8")
    assert r.status_code == 409
    assert r.json()["reason"] == "insufficient_book"
    doc = client.get("/inventory-docs/in-3", headers=H).json()
    assert doc["status"] == "posted"
    assert _balance()["book_quantity"] == 20
    # 补足账面后冲正可行：账面 120，冲正后 70 >= 占用 20
    assert _accept("in-6", "inbound", 100, "req-i9").status_code == 201
    assert _action("in-6", "post", "req-i10").status_code == 200
    assert _action("in-3", "reverse", "req-i11").status_code == 200
    bal = _balance()
    assert bal["book_quantity"] == 70 and bal["reserved_quantity"] == 20
    assert bal["available_quantity"] == 50


def test_reverse_posted_outbound_restores_book() -> None:
    done = _action("out-4", "reverse", "req-o10")
    assert done.status_code == 200
    bal = _balance()
    assert bal["book_quantity"] == 100 and bal["reserved_quantity"] == 20


def test_terminal_states_reject_further_transitions() -> None:
    # 已冲正不可再推进
    for action in ("post", "cancel", "reverse"):
        r = _action("out-4", action, f"req-t-{action}")
        assert r.status_code == 409
        assert r.json()["reason"] == "invalid_state"
    # 已撤销不可再推进
    for action in ("post", "cancel", "reverse"):
        r = _action("out-3", action, f"req-t2-{action}")
        assert r.status_code == 409
        assert r.json()["reason"] == "invalid_state"
    # 已入账不可撤销、不可重复入账；冲正仅一次
    assert _accept("in-4", "inbound", 5, "req-s1").status_code == 201
    assert _action("in-4", "post", "req-s2").status_code == 200
    assert _action("in-4", "cancel", "req-s3").status_code == 409
    assert _action("in-4", "post", "req-s4").status_code == 409
    assert _action("in-4", "reverse", "req-s5").status_code == 200
    assert _action("in-4", "reverse", "req-s6").status_code == 409
    # 待入账不可冲正
    assert _accept("in-5", "inbound", 5, "req-s7").status_code == 201
    r = _action("in-5", "reverse", "req-s8")
    assert r.status_code == 409 and r.json()["reason"] == "invalid_state"


def test_invalid_quantity_and_direction_rejected() -> None:
    for bad in (0, -3):
        r = _accept(f"bad-q{bad}", "inbound", bad, f"req-bq{bad}")
        assert r.status_code == 409
        assert r.json()["reason"] == "invalid_quantity"
    r = _accept("bad-d1", "sideways", 1, "req-bd1")
    assert r.status_code == 409
    assert r.json()["reason"] == "invalid_direction"
    # 非整数数量：请求体校验拒绝
    r = _accept("bad-q2", "inbound", "三", "req-bq2")
    assert r.status_code == 422


def test_duplicate_doc_id_rejected_without_mutation() -> None:
    assert _accept("dup-1", "inbound", 7, "req-d1").status_code == 201
    dup = _accept("dup-1", "outbound", 3, "req-d2")
    assert dup.status_code == 409
    assert dup.json()["reason"] == "duplicate_doc"
    doc = client.get("/inventory-docs/dup-1", headers=H).json()
    assert doc["direction"] == "inbound" and doc["quantity"] == 7


def test_idempotent_replay_returns_first_result_without_double_effects() -> None:
    before = _balance()
    r1 = _accept("idem-1", "inbound", 30, "req-id1")
    r2 = _accept("idem-1", "inbound", 30, "req-id1")
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert _action("idem-1", "post", "req-id2").status_code == 200
    again = _action("idem-1", "post", "req-id2")
    assert again.status_code == 200 and again.json()["status"] == "posted"
    # 重放不重复入账：账面只增加一次 30
    after = _balance()
    assert after["book_quantity"] == before["book_quantity"] + 30
    assert after["reserved_quantity"] == before["reserved_quantity"]
    # 业务拒绝同样可重放
    j1 = _accept("idem-x", "outbound", 999, "req-id3").json()
    j2 = _accept("idem-x", "outbound", 999, "req-id3").json()
    assert j1 == j2 and j1["reason"] == "insufficient_available"


def test_request_id_reused_for_other_use_conflicts() -> None:
    assert _accept("idem-2", "inbound", 1, "req-cf1").status_code == 201
    # 同标识改作其他对象
    assert _accept("idem-3", "inbound", 1, "req-cf1").status_code == 409
    # 同标识改作其他操作
    assert _action("idem-2", "post", "req-cf1").status_code == 409
    # 同标识改作其他参数
    assert _accept("idem-2", "inbound", 2, "req-cf1").status_code == 409


def test_tenant_isolation_and_missing_header() -> None:
    other = {"X-Tenant": "other-tenant"}
    # 跨租户读取按不存在处理
    assert client.get("/inventory-docs/in-1", headers=other).status_code == 404
    assert _action("in-5", "post", "req-x1", headers=other).status_code == 404
    # 其他租户余额独立
    bal = _balance(headers=other)
    assert bal["book_quantity"] == 0 and bal["reserved_quantity"] == 0
    # 其他租户可用同一单号
    assert _accept("in-1", "inbound", 9, "req-x2", headers=other).status_code == 201
    # 缺租户头明确拒绝
    assert client.get("/inventory-docs/in-1").status_code == 400
    assert client.get("/inventory-balance?warehouse_id=W-1&item_id=SKU-1").status_code == 400
    assert client.post("/inventory-docs", json={"doc_id": "z", "warehouse_id": "W", "item_id": "I",
                                                "direction": "inbound", "quantity": 1,
                                                "request_id": "req-x3"}).status_code == 400


def test_balance_defaults_to_zero_and_requires_params() -> None:
    bal = _balance(warehouse_id="W-9", item_id="SKU-9")
    assert bal == {"warehouse_id": "W-9", "item_id": "SKU-9",
                   "book_quantity": 0, "reserved_quantity": 0, "available_quantity": 0}
    assert client.get("/inventory-balance?warehouse_id=W-1", headers=H).status_code == 400


def test_read_doc_by_id() -> None:
    doc = client.get("/inventory-docs/in-5", headers=H).json()
    assert doc == {"doc_id": "in-5", "warehouse_id": "W-1", "item_id": "SKU-1",
                   "direction": "inbound", "quantity": 5, "status": "pending"}
    assert client.get("/inventory-docs/nonexistent", headers=H).status_code == 404


def test_concurrent_outbound_accepts_never_go_negative() -> None:
    # 账面 100，8 个并发各申请出库 30：只允许 3 个成功
    assert _accept("cc-in", "inbound", 100, "req-cc0",
                   warehouse_id="W-C", item_id="SKU-C").status_code == 201
    assert _action("cc-in", "post", "req-cc1").status_code == 200
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_accept(f"cc-out{i}", "outbound", 30, f"req-cc{i}",
                               warehouse_id="W-C", item_id="SKU-C").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(201) == 3
    assert results.count(409) == 5
    bal = _balance(warehouse_id="W-C", item_id="SKU-C")
    assert bal["reserved_quantity"] == 90
    assert bal["available_quantity"] == 10
    assert bal["book_quantity"] == 100
