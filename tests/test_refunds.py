import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_refunds.sqlite"))
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def _make_paid_order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    client.post(
        "/orders",
        json={"tenant": "t1", "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    )
    client.post(
        f"/orders/{order_id}/payments",
        json={"amount_cents": paid if paid is not None else amount},
        headers=H,
    )


def test_accept_and_read_refund() -> None:
    _make_paid_order("r1", paid=500)
    resp = client.post(
        "/orders/r1/refunds",
        json={"refund_id": "rf1", "amount_cents": 200, "request_id": "req1"},
        headers=H,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["amount_cents"] == 200
    assert body["effective_deduction_cents"] == 0

    got = client.get("/orders/r1/refunds/rf1", headers=H)
    assert got.status_code == 200 and got.json()["status"] == "pending"


def test_duplicate_refund_id_is_refused_without_change() -> None:
    _make_paid_order("r2", paid=500)
    payload = {"refund_id": "rf1", "amount_cents": 200, "request_id": "req1"}
    assert client.post("/orders/r2/refunds", json=payload, headers=H).status_code == 201
    # 同标识不同请求标识重复受理：拒绝，金额/状态不变
    again = client.post(
        "/orders/r2/refunds", json={**payload, "request_id": "req2"}, headers=H
    )
    assert again.status_code == 409
    got = client.get("/orders/r2/refunds/rf1", headers=H).json()
    assert got["amount_cents"] == 200 and got["status"] == "pending"
    order = client.get("/orders/r2", headers=H).json()
    assert order["paid_cents"] == 500


def test_accept_rejects_unknown_order_and_bad_amount() -> None:
    resp = client.post(
        "/orders/missing/refunds",
        json={"refund_id": "rf1", "amount_cents": 100, "request_id": "req1"},
        headers=H,
    )
    assert resp.status_code == 404
    bad = client.post(
        "/orders/r2/refunds",
        json={"refund_id": "rf-bad", "amount_cents": 0, "request_id": "req1"},
        headers=H,
    )
    assert bad.status_code == 422


def test_pending_occupies_refundable_balance() -> None:
    _make_paid_order("r3", paid=500)
    assert client.post(
        "/orders/r3/refunds",
        json={"refund_id": "rf1", "amount_cents": 300, "request_id": "req1"},
        headers=H,
    ).status_code == 201
    # 待处理已占用 300，只剩 200 可退
    over = client.post(
        "/orders/r3/refunds",
        json={"refund_id": "rf2", "amount_cents": 201, "request_id": "req2"},
        headers=H,
    )
    assert over.status_code == 409
    assert client.get("/orders/r3/refunds/rf2", headers=H).status_code == 404
    # 受理不扣减已收
    assert client.get("/orders/r3", headers=H).json()["paid_cents"] == 500


def test_cancel_releases_occupancy() -> None:
    _make_paid_order("r4", paid=500)
    client.post(
        "/orders/r4/refunds",
        json={"refund_id": "rf1", "amount_cents": 300, "request_id": "req1"},
        headers=H,
    )
    assert client.post(
        "/orders/r4/refunds/rf1/advance",
        json={"action": "cancel", "request_id": "req2"},
        headers=H,
    ).status_code == 200
    # 撤销后占用释放，可再退 500
    assert client.post(
        "/orders/r4/refunds",
        json={"refund_id": "rf2", "amount_cents": 500, "request_id": "req3"},
        headers=H,
    ).status_code == 201
    assert client.get("/orders/r4", headers=H).json()["paid_cents"] == 500


def test_complete_deducts_paid_and_terminal_transitions_rejected() -> None:
    _make_paid_order("r5", amount=1000, paid=500)
    client.post(
        "/orders/r5/refunds",
        json={"refund_id": "rf1", "amount_cents": 300, "request_id": "req1"},
        headers=H,
    )
    done = client.post(
        "/orders/r5/refunds/rf1/advance",
        json={"action": "complete", "request_id": "req2"},
        headers=H,
    )
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "completed" and body["effective_deduction_cents"] == 300
    order = client.get("/orders/r5", headers=H).json()
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 800

    # 已完成不可撤销
    assert client.post(
        "/orders/r5/refunds/rf1/advance",
        json={"action": "cancel", "request_id": "req3"},
        headers=H,
    ).status_code == 409
    # 重复完成被拒绝且不重复扣减
    assert client.post(
        "/orders/r5/refunds/rf1/advance",
        json={"action": "complete", "request_id": "req4"},
        headers=H,
    ).status_code == 409
    assert client.get("/orders/r5", headers=H).json()["paid_cents"] == 200


def test_reverse_adds_back_and_is_one_shot() -> None:
    _make_paid_order("r6", paid=500)
    client.post(
        "/orders/r6/refunds",
        json={"refund_id": "rf1", "amount_cents": 300, "request_id": "req1"},
        headers=H,
    )
    client.post(
        "/orders/r6/refunds/rf1/advance",
        json={"action": "complete", "request_id": "req2"},
        headers=H,
    )
    # 待处理不可冲正，已完成才能冲正
    rev = client.post(
        "/orders/r6/refunds/rf1/reverse", json={"request_id": "req3"}, headers=H
    )
    assert rev.status_code == 200
    body = rev.json()
    assert body["status"] == "reversed" and body["effective_deduction_cents"] == 0
    assert client.get("/orders/r6", headers=H).json()["paid_cents"] == 500

    assert client.post(
        "/orders/r6/refunds/rf1/reverse", json={"request_id": "req4"}, headers=H
    ).status_code == 409
    assert client.post(
        "/orders/r6/refunds/rf1/advance",
        json={"action": "cancel", "request_id": "req5"},
        headers=H,
    ).status_code == 409
    assert client.get("/orders/r6", headers=H).json()["paid_cents"] == 500


def test_reverse_requires_completed() -> None:
    _make_paid_order("r7", paid=500)
    client.post(
        "/orders/r7/refunds",
        json={"refund_id": "rf1", "amount_cents": 100, "request_id": "req1"},
        headers=H,
    )
    assert client.post(
        "/orders/r7/refunds/rf1/reverse", json={"request_id": "req2"}, headers=H
    ).status_code == 409


def test_idempotent_replays_match_first_result() -> None:
    _make_paid_order("r8", paid=500)
    accept = {"refund_id": "rf1", "amount_cents": 300, "request_id": "reqA"}
    first = client.post("/orders/r8/refunds", json=accept, headers=H)
    replay = client.post("/orders/r8/refunds", json=accept, headers=H)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert client.get("/orders/r8", headers=H).json()["paid_cents"] == 500

    adv = {"action": "complete", "request_id": "reqB"}
    done1 = client.post("/orders/r8/refunds/rf1/advance", json=adv, headers=H)
    done2 = client.post("/orders/r8/refunds/rf1/advance", json=adv, headers=H)
    assert done1.json() == done2.json()
    assert client.get("/orders/r8", headers=H).json()["paid_cents"] == 200

    rev1 = client.post("/orders/r8/refunds/rf1/reverse", json={"request_id": "reqC"}, headers=H)
    rev2 = client.post("/orders/r8/refunds/rf1/reverse", json={"request_id": "reqC"}, headers=H)
    assert rev1.json() == rev2.json()
    assert client.get("/orders/r8", headers=H).json()["paid_cents"] == 500


def test_list_refunds_for_order() -> None:
    _make_paid_order("r9", paid=500)
    client.post(
        "/orders/r9/refunds",
        json={"refund_id": "rf1", "amount_cents": 100, "request_id": "req1"},
        headers=H,
    )
    client.post(
        "/orders/r9/refunds",
        json={"refund_id": "rf2", "amount_cents": 200, "request_id": "req2"},
        headers=H,
    )
    resp = client.get("/orders/r9/refunds", headers=H)
    assert resp.status_code == 200
    items = resp.json()["refunds"]
    assert [r["refund_id"] for r in items] == ["rf1", "rf2"]
    for r in items:
        assert {"refund_id", "status", "amount_cents", "effective_deduction_cents"}.issubset(r)
    # 订单不存在（含跨租户）按不存在处理
    assert client.get("/orders/r9/refunds", headers={"X-Tenant": "other"}).status_code == 404


def test_cross_tenant_everything_is_not_found() -> None:
    _make_paid_order("r10", paid=500)
    client.post(
        "/orders/r10/refunds",
        json={"refund_id": "rf1", "amount_cents": 100, "request_id": "req1"},
        headers=H,
    )
    other = {"X-Tenant": "t2"}
    assert client.get("/orders/r10/refunds/rf1", headers=other).status_code == 404
    assert client.get("/orders/r10/refunds", headers=other).status_code == 404
    assert client.post(
        "/orders/r10/refunds/rf1/advance",
        json={"action": "complete", "request_id": "req2"},
        headers=other,
    ).status_code == 404
    assert client.post(
        "/orders/r10/refunds/rf1/reverse", json={"request_id": "req3"}, headers=other
    ).status_code == 404
    # 跨租户操作未改变原单据
    assert client.get("/orders/r10/refunds/rf1", headers=H).json()["status"] == "pending"


def test_concurrent_accepts_never_exceed_refundable_balance() -> None:
    from app.store import refunds

    _make_paid_order("r11", paid=500)

    def attempt(i: int):
        try:
            return refunds.accept("t1", "r11", f"rf{i}", 200, f"req{i}")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    accepted = [r for r in results if r is not None]
    # 每笔 200、可退 500：只有 2 笔成功，其余拒绝
    assert len(accepted) == 2
    conn = connect()
    try:
        held = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS held FROM refunds "
            "WHERE tenant='t1' AND order_id='r11' AND status IN ('pending','completed')"
        ).fetchone()["held"]
        paid = conn.execute(
            "SELECT paid_cents FROM orders WHERE tenant='t1' AND order_id='r11'"
        ).fetchone()["paid_cents"]
    finally:
        conn.close()
    assert held == 400 and paid == 500


def test_state_survives_restart() -> None:
    from app.store import refunds

    _make_paid_order("r12", paid=500)
    client.post(
        "/orders/r12/refunds",
        json={"refund_id": "rf1", "amount_cents": 300, "request_id": "req1"},
        headers=H,
    )
    client.post(
        "/orders/r12/refunds/rf1/advance",
        json={"action": "complete", "request_id": "req2"},
        headers=H,
    )
    # 模拟重启：新连接读取落库状态，金额与占用保持一致
    refund = refunds.get("t1", "r12", "rf1")
    assert refund["status"] == "completed" and refund["effective_deduction_cents"] == 300
    conn = connect()
    try:
        paid = conn.execute(
            "SELECT paid_cents FROM orders WHERE tenant='t1' AND order_id='r12'"
        ).fetchone()["paid_cents"]
    finally:
        conn.close()
    assert paid == 200
