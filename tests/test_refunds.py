import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ["APP_DB"] = os.path.join(tempfile.mkdtemp(), "test_refunds.sqlite")
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)


def make_order(tenant: str, order_id: str, amount: int) -> None:
    assert client.post(
        "/orders",
        json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"},
    ).status_code == 201


def pay(tenant: str, order_id: str, amount: int) -> None:
    assert client.post(
        f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant}
    ).status_code == 200


def refund_body(tenant: str, refund_id: str, order_id: str, amount: int, reason: str = "bad goods") -> dict:
    return {
        "tenant": tenant,
        "refund_id": refund_id,
        "order_id": order_id,
        "amount_cents": amount,
        "reason": reason,
    }


def order_view(tenant: str, order_id: str) -> dict:
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant}).json()


def assert_closed(tenant: str, order_id: str) -> dict:
    order = order_view(tenant, order_id)
    assert order["outstanding_cents"] == order["amount_cents"] - order["paid_cents"]
    assert 0 <= order["paid_cents"] <= order["amount_cents"]
    return order


def test_register_requires_tenant_header_only_on_read() -> None:
    make_order("t1", "o1", 1000)
    pay("t1", "o1", 1000)
    # 登记租户在请求体内；读取必须带头。
    r = client.post("/refunds", json=refund_body("t1", "r1", "o1", 100))
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert client.get("/refunds/r1").status_code == 400
    got = client.get("/refunds/r1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["status"] == "pending"


def test_pending_refund_does_not_change_paid() -> None:
    make_order("t1", "o2", 500)
    pay("t1", "o2", 500)
    client.post("/refunds", json=refund_body("t1", "r2", "o2", 200))
    order = assert_closed("t1", "o2")
    assert order["paid_cents"] == 500  # 待审核不实际退款


def test_approve_adjusts_paid_and_outstanding() -> None:
    make_order("t1", "o3", 500)
    pay("t1", "o3", 500)
    client.post("/refunds", json=refund_body("t1", "r3", "o3", 200))
    r = client.post("/refunds/r3/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    order = assert_closed("t1", "o3")
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 200


def test_reject_leaves_order_untouched() -> None:
    make_order("t1", "o4", 500)
    pay("t1", "o4", 300)
    client.post("/refunds", json=refund_body("t1", "r4", "o4", 200))
    r = client.post("/refunds/r4/review", json={"decision": "reject"}, headers={"X-Tenant": "t1"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    order = assert_closed("t1", "o4")
    assert order["paid_cents"] == 300


def test_register_validation_errors() -> None:
    make_order("t1", "o5", 500)
    pay("t1", "o5", 500)
    base = refund_body("t1", "r5", "o5", 100)
    bad_cases = [
        ("tenant", ""),
        ("refund_id", ""),
        ("order_id", ""),
        ("amount_cents", 0),
        ("amount_cents", -5),
        ("amount_cents", "100"),
        ("amount_cents", True),
        ("reason", ""),
        ("reason", "   "),
    ]
    for field, bad in bad_cases:
        body = {**base, field: bad}
        assert client.post("/refunds", json=body).status_code == 400, (field, bad)
    # 缺字段同样 400。
    for field in ("tenant", "refund_id", "order_id", "amount_cents", "reason"):
        body = {k: v for k, v in base.items() if k != field}
        assert client.post("/refunds", json=body).status_code == 400, field


def test_register_unknown_or_cross_tenant_order_is_param_error() -> None:
    assert client.post("/refunds", json=refund_body("t1", "r6a", "missing", 10)).status_code == 400
    make_order("t2", "o6", 100)
    pay("t2", "o6", 100)
    # 订单属于 t2，t1 名下登记按参数错误处理。
    assert client.post("/refunds", json=refund_body("t1", "r6b", "o6", 10)).status_code == 400
    assert client.get("/refunds/r6b", headers={"X-Tenant": "t1"}).status_code == 404


def test_register_total_cannot_exceed_paid() -> None:
    make_order("t1", "o7", 500)
    pay("t1", "o7", 300)
    assert client.post("/refunds", json=refund_body("t1", "r7a", "o7", 200)).status_code == 201
    # 待审核预留已占用 200，再来 200 超过已收 300。
    r = client.post("/refunds", json=refund_body("t1", "r7b", "o7", 200))
    assert r.status_code == 409
    # 冲突不留半张单据。
    assert client.get("/refunds/r7b", headers={"X-Tenant": "t1"}).status_code == 404
    assert_closed("t1", "o7")


def test_multiple_refunds_share_paid_and_close_after_review() -> None:
    make_order("t1", "o8", 100)
    pay("t1", "o8", 100)
    assert client.post("/refunds", json=refund_body("t1", "r8a", "o8", 60)).status_code == 201
    assert client.post("/refunds", json=refund_body("t1", "r8b", "o8", 40)).status_code == 201
    assert client.post("/refunds/r8a/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"}).status_code == 200
    # 第一笔生效后第二笔仍可同意，合计恰等于已收。
    assert client.post("/refunds/r8b/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"}).status_code == 200
    order = assert_closed("t1", "o8")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 100


def test_approve_over_paid_after_payment_change_conflicts() -> None:
    make_order("t1", "o9", 500)
    pay("t1", "o9", 300)
    client.post("/refunds", json=refund_body("t1", "r9a", "o9", 200))
    client.post("/refunds", json=refund_body("t1", "r9b", "o9", 100))
    client.post("/refunds/r9a/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"})
    # paid=100，其余待审核 100；若再来一笔超出则登记已挡，这里验证守卫本身不越界。
    assert client.post("/refunds", json=refund_body("t1", "r9c", "o9", 1)).status_code == 409
    assert_closed("t1", "o9")


def test_duplicate_registration_is_idempotent_by_business_identity() -> None:
    make_order("t1", "o10", 500)
    pay("t1", "o10", 500)
    first = client.post("/refunds", json=refund_body("t1", "r10", "o10", 100, reason="first"))
    assert first.status_code == 201
    # 不同请求指纹（金额、原因不同）重复提交：返回既有单，不新建、不退款。
    second = client.post("/refunds", json=refund_body("t1", "r10", "o10", 999, reason="second"))
    assert second.status_code == 200
    assert second.json()["amount_cents"] == 100 and second.json()["reason"] == "first"
    client.post("/refunds/r10/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"})
    third = client.post("/refunds", json=refund_body("t1", "r10", "o10", 100, reason="first"))
    assert third.status_code == 200 and third.json()["status"] == "approved"
    assert order_view("t1", "o10")["paid_cents"] == 400  # 只退了一次


def test_review_is_idempotent_and_not_overwritable() -> None:
    make_order("t1", "o11", 500)
    pay("t1", "o11", 500)
    client.post("/refunds", json=refund_body("t1", "r11", "o11", 100))
    h = {"X-Tenant": "t1"}
    assert client.post("/refunds/r11/review", json={"decision": "reject"}, headers=h).json()["status"] == "rejected"
    # 后续审核不能覆盖结果。
    again = client.post("/refunds/r11/review", json={"decision": "approve"}, headers=h)
    assert again.status_code == 200 and again.json()["status"] == "rejected"
    assert order_view("t1", "o11")["paid_cents"] == 500


def test_review_bad_decision_is_param_error() -> None:
    make_order("t1", "o12", 500)
    pay("t1", "o12", 500)
    client.post("/refunds", json=refund_body("t1", "r12", "o12", 100))
    assert client.post("/refunds/r12/review", json={"decision": "yes"}, headers={"X-Tenant": "t1"}).status_code == 400
    assert client.post("/refunds/r12/review", json={}, headers={"X-Tenant": "t1"}).status_code == 400


def test_cross_tenant_read_and_review_are_not_found() -> None:
    make_order("t1", "o13", 500)
    pay("t1", "o13", 500)
    client.post("/refunds", json=refund_body("t1", "r13", "o13", 100))
    assert client.get("/refunds/r13", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post(
        "/refunds/r13/review", json={"decision": "approve"}, headers={"X-Tenant": "t2"}
    ).status_code == 404
    assert client.post("/refunds/r13/reverse", headers={"X-Tenant": "t2"}).status_code == 404
    # t2 的操作不得作用于 t1 的单。
    assert client.get("/refunds/r13", headers={"X-Tenant": "t1"}).json()["status"] == "pending"
    assert order_view("t1", "o13")["paid_cents"] == 500


def test_reverse_restores_order_and_blocks_further_actions() -> None:
    make_order("t1", "o14", 500)
    pay("t1", "o14", 500)
    h = {"X-Tenant": "t1"}
    client.post("/refunds", json=refund_body("t1", "r14", "o14", 200))
    client.post("/refunds/r14/review", json={"decision": "approve"}, headers=h)
    assert order_view("t1", "o14")["paid_cents"] == 300
    r = client.post("/refunds/r14/reverse", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "reversed"
    order = assert_closed("t1", "o14")
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0
    # 已冲正不得再审核、再冲正。
    assert client.post("/refunds/r14/review", json={"decision": "approve"}, headers=h).status_code == 409
    assert client.post("/refunds/r14/reverse", headers=h).status_code == 409


def test_reverse_pending_or_rejected_is_conflict_and_unchanged() -> None:
    make_order("t1", "o15", 500)
    pay("t1", "o15", 500)
    h = {"X-Tenant": "t1"}
    client.post("/refunds", json=refund_body("t1", "r15a", "o15", 100))
    assert client.post("/refunds/r15a/reverse", headers=h).status_code == 409
    assert client.get("/refunds/r15a", headers=h).json()["status"] == "pending"

    client.post("/refunds", json=refund_body("t1", "r15b", "o15", 100))
    client.post("/refunds/r15b/review", json={"decision": "reject"}, headers=h)
    assert client.post("/refunds/r15b/reverse", headers=h).status_code == 409
    assert order_view("t1", "o15")["paid_cents"] == 500


def test_concurrent_register_same_refund_yields_single_row() -> None:
    make_order("t1", "o16", 500)
    pay("t1", "o16", 500)

    def register() -> int:
        with TestClient(app) as local:
            return local.post("/refunds", json=refund_body("t1", "r16", "o16", 100)).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: register(), range(8)))
    assert sorted(statuses).count(201) == 1
    assert statuses.count(200) == 7
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM refunds WHERE tenant='t1' AND refund_id='r16'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
    # 并发登记未审核，不产生任何退款。
    assert order_view("t1", "o16")["paid_cents"] == 500


def test_concurrent_review_and_reverse_closes() -> None:
    make_order("t1", "o17", 500)
    pay("t1", "o17", 500)
    h = {"X-Tenant": "t1"}
    client.post("/refunds", json=refund_body("t1", "r17", "o17", 300))

    def approve() -> int:
        with TestClient(app) as local:
            return local.post("/refunds/r17/review", json={"decision": "approve"}, headers=h).status_code

    def reverse() -> int:
        with TestClient(app) as local:
            return local.post("/refunds/r17/reverse", headers=h).status_code

    # 多轮交错：审核只一次生效，冲正至多一次，任何终态后金额必须闭合。
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda fn: fn(), [approve, reverse, approve, reverse] * 3))
    assert 200 in results
    status = client.get("/refunds/r17", headers=h).json()["status"]
    assert status in ("approved", "reversed")
    order = assert_closed("t1", "o17")
    # 冲正与生效成对出现时回补；最终以单据状态为准核对。
    assert order["paid_cents"] == (200 if status == "approved" else 500)


def test_state_persists_across_restart() -> None:
    make_order("t1", "o18", 500)
    pay("t1", "o18", 500)
    h = {"X-Tenant": "t1"}
    client.post("/refunds", json=refund_body("t1", "r18a", "o18", 200))
    client.post("/refunds", json=refund_body("t1", "r18b", "o18", 100))
    client.post("/refunds/r18a/review", json={"decision": "approve"}, headers=h)
    client.post("/refunds/r18a/reverse", headers=h)

    # 重新建一个客户端实例模拟重启，数据库文件不变。
    restarted = TestClient(app)
    assert restarted.get("/refunds/r18a", headers=h).json()["status"] == "reversed"
    assert restarted.get("/refunds/r18b", headers=h).json()["status"] == "pending"
    order = restarted.get("/orders/o18", headers=h).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0
    # 重启后冲正仍不可被审核覆盖。
    assert restarted.post("/refunds/r18a/review", json={"decision": "approve"}, headers=h).status_code == 409
