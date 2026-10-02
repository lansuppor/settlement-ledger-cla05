import os, tempfile
os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

def make_order(order_id: str, amount_cents: int, tenant: str = "t1") -> None:
    body = {"tenant": tenant, "order_id": order_id, "amount_cents": amount_cents, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201

def pay(order_id: str, amount_cents: int, request_id: str | None = None, tenant: str = "t1"):
    body = {"amount_cents": amount_cents}
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/orders/{order_id}/payments", json=body, headers={"X-Tenant": tenant})

def test_payment_with_request_id_applies_once() -> None:
    make_order("p1", 500)
    resp = pay("p1", 200, request_id="req-p1-a")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paid_cents"] == 200 and data["outstanding_cents"] == 300
    assert data["payment_result"] == {
        "request_id": "req-p1-a",
        "amount_cents": 200,
        "status": "applied",
        "deduplicated": False,
    }

def test_same_request_id_is_deduplicated() -> None:
    make_order("p2", 500)
    first = pay("p2", 200, request_id="req-p2-a")
    assert first.status_code == 200
    again = pay("p2", 200, request_id="req-p2-a")
    assert again.status_code == 200
    data = again.json()
    assert data["paid_cents"] == 200 and data["outstanding_cents"] == 300
    result = data["payment_result"]
    assert result["request_id"] == "req-p2-a" and result["amount_cents"] == 200
    assert result["status"] == "applied" and result["deduplicated"] is True

def test_same_request_id_with_different_amount_conflicts() -> None:
    make_order("p3", 500)
    assert pay("p3", 200, request_id="req-p3-a").status_code == 200
    conflict = pay("p3", 300, request_id="req-p3-a")
    assert conflict.status_code == 409
    assert "different amount" in conflict.json()["detail"]
    got = client.get("/orders/p3", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 200 and got["outstanding_cents"] == 300

def test_distinct_request_ids_each_apply() -> None:
    make_order("p4", 500)
    assert pay("p4", 200, request_id="req-p4-a").status_code == 200
    resp = pay("p4", 200, request_id="req-p4-b")
    assert resp.status_code == 200
    assert resp.json()["paid_cents"] == 400

def test_overpayment_with_request_id_fails_and_id_stays_reusable() -> None:
    make_order("p5", 300)
    over = pay("p5", 500, request_id="req-p5-a")
    assert over.status_code == 409
    assert "exceeds" in over.json()["detail"]
    retry = pay("p5", 300, request_id="req-p5-a")
    assert retry.status_code == 200
    assert retry.json()["paid_cents"] == 300
    assert retry.json()["payment_result"]["deduplicated"] is False

def test_missing_order_does_not_consume_request_id() -> None:
    assert pay("p6-missing", 100, request_id="req-p6-a").status_code == 404
    make_order("p6", 300)
    resp = pay("p6", 100, request_id="req-p6-a")
    assert resp.status_code == 200
    assert resp.json()["payment_result"]["deduplicated"] is False

def test_cross_tenant_payment_is_not_found_and_id_stays_reusable() -> None:
    make_order("p7", 300, tenant="t1")
    cross = pay("p7", 100, request_id="req-p7-a", tenant="t2")
    assert cross.status_code == 404
    got = client.get("/orders/p7", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 0
    retry = pay("p7", 100, request_id="req-p7-a", tenant="t1")
    assert retry.status_code == 200
    assert retry.json()["paid_cents"] == 100

def test_concurrent_same_request_id_applies_once() -> None:
    make_order("p8", 1000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: pay("p8", 400, request_id="req-p8-a"), range(8)))
    assert all(resp.status_code == 200 for resp in responses)
    results = [resp.json()["payment_result"] for resp in responses]
    assert sum(1 for r in results if not r["deduplicated"]) == 1
    assert sum(1 for r in results if r["deduplicated"]) == 7
    got = client.get("/orders/p8", headers={"X-Tenant": "t1"}).json()
    assert got["paid_cents"] == 400 and got["outstanding_cents"] == 600

def test_payment_without_request_id_keeps_legacy_behaviour() -> None:
    make_order("p9", 300)
    resp = pay("p9", 100)
    assert resp.status_code == 200
    data = resp.json()
    assert data["paid_cents"] == 100 and "payment_result" not in data
    again = pay("p9", 100)
    assert again.status_code == 200 and again.json()["paid_cents"] == 200
