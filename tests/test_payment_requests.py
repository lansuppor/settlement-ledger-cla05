import os, tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

TENANT = "tp"


def make_order(order_id: str, amount_cents: int) -> None:
    body = {"tenant": TENANT, "order_id": order_id, "amount_cents": amount_cents, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def pay(order_id: str, amount_cents: int, request_id: str, tenant: str = TENANT):
    return client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": amount_cents, "request_id": request_id},
        headers={"X-Tenant": tenant},
    )


def test_payment_with_request_id_applies_once() -> None:
    make_order("p1", 500)
    got = pay("p1", 200, "r-1")
    assert got.status_code == 200
    body = got.json()
    assert body["paid_cents"] == 200 and body["outstanding_cents"] == 300
    assert body["payment"] == {"request_id": "r-1", "amount_cents": 200, "result": "applied", "duplicate": False}


def test_duplicate_request_id_is_not_counted_twice() -> None:
    make_order("p2", 500)
    first = pay("p2", 200, "r-2").json()
    again = pay("p2", 200, "r-2")
    assert again.status_code == 200
    second = again.json()
    assert second["paid_cents"] == 200 and second["outstanding_cents"] == 300
    assert second["payment"]["duplicate"] is True
    assert {k: second["payment"][k] for k in ("request_id", "amount_cents", "result")} == {
        k: first["payment"][k] for k in ("request_id", "amount_cents", "result")
    }
    order = client.get("/orders/p2", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 200


def test_same_request_id_with_different_amount_conflicts() -> None:
    make_order("p3", 500)
    assert pay("p3", 200, "r-3").status_code == 200
    conflict = pay("p3", 300, "r-3")
    assert conflict.status_code == 409
    order = client.get("/orders/p3", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 200


def test_failed_payment_does_not_consume_request_id() -> None:
    make_order("p4", 300)
    assert pay("p4", 500, "r-4").status_code == 409
    order = client.get("/orders/p4", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 0
    fixed = pay("p4", 300, "r-4")
    assert fixed.status_code == 200
    assert fixed.json()["payment"]["duplicate"] is False
    assert fixed.json()["paid_cents"] == 300


def test_unknown_order_does_not_consume_request_id() -> None:
    assert pay("p5-missing", 100, "r-5").status_code == 404
    make_order("p5", 300)
    got = pay("p5", 100, "r-5")
    assert got.status_code == 200 and got.json()["payment"]["duplicate"] is False


def test_cross_tenant_payment_is_not_found_and_does_not_leak() -> None:
    make_order("p6", 300)
    assert pay("p6", 100, "r-6", tenant="other-tenant").status_code == 404
    order = client.get("/orders/p6", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 0
    got = pay("p6", 100, "r-6")
    assert got.status_code == 200 and got.json()["payment"]["duplicate"] is False


def test_concurrent_same_request_id_applies_exactly_once() -> None:
    make_order("p7", 1000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: pay("p7", 400, "r-7"), range(8)))
    assert all(r.status_code == 200 for r in responses)
    payments = [r.json()["payment"] for r in responses]
    assert sum(1 for p in payments if p["duplicate"] is False) == 1
    assert sum(1 for p in payments if p["duplicate"] is True) == 7
    order = client.get("/orders/p7", headers={"X-Tenant": TENANT}).json()
    assert order["paid_cents"] == 400


def test_request_identity_survives_restart() -> None:
    make_order("p8", 300)
    assert pay("p8", 100, "r-8").status_code == 200
    migrate()  # 模拟重启：重新建连迁移，状态只来自 SQLite 文件
    restarted = TestClient(app)
    again = restarted.post(
        "/orders/p8/payments",
        json={"amount_cents": 100, "request_id": "r-8"},
        headers={"X-Tenant": TENANT},
    )
    assert again.status_code == 200
    assert again.json()["payment"]["duplicate"] is True
    assert again.json()["paid_cents"] == 100


def test_payment_without_request_id_keeps_legacy_behaviour() -> None:
    make_order("p9", 300)
    got = client.post("/orders/p9/payments", json={"amount_cents": 100}, headers={"X-Tenant": TENANT})
    assert got.status_code == 200
    body = got.json()
    assert body["paid_cents"] == 100 and "payment" not in body
