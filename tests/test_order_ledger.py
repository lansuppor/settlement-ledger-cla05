import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "ledger_test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)


def _accept(tenant: str, order_id: str, amount: int = 1000) -> None:
    body = {"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def _ledger(order_id: str, tenant: str = "t1", **params):
    return client.get(f"/orders/{order_id}/ledger", headers={"X-Tenant": tenant}, params=params)


def test_payment_writes_one_entry_and_balance_matches_order() -> None:
    _accept("t1", "l1", 1000)
    assert client.post("/orders/l1/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"}).status_code == 200
    page = _ledger("l1", page_size=20).json()
    assert len(page["entries"]) == 1
    entry = page["entries"][0]
    assert entry == {
        "tenant": "t1",
        "order_id": "l1",
        "seq": 1,
        "action_type": "payment",
        "ref_kind": "order",
        "ref_id": "l1",
        "delta_cents": 300,
        "balance_after_cents": 300,
    }
    assert page["has_next"] is False and page["next_cursor"] is None


def test_full_action_lifecycle_chain_and_closure() -> None:
    _accept("t1", "l2", 1000)
    # 收款 600；登记退款 200（变化额 0）；同意（−200）；冲正（+200）；收款回退 100（−100）。
    client.post("/orders/l2/payments", json={"amount_cents": 600}, headers={"X-Tenant": "t1"})
    assert client.post(
        "/refunds",
        json={"tenant": "t1", "refund_id": "rf-l2", "order_id": "l2", "amount_cents": 200, "reason": "x"},
    ).status_code == 201
    assert client.post("/refunds/rf-l2/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/refunds/rf-l2/reverse", headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post(
        "/payment-rollbacks",
        json={"tenant": "t1", "rollback_id": "rb-l2", "order_id": "l2", "amount_cents": 100},
    ).status_code == 201

    entries = _ledger("l2", page_size=50).json()["entries"]
    assert [e["seq"] for e in entries] == [1, 2, 3, 4, 5]
    expected = [
        ("payment", "order", "l2", 600, 600),
        ("refund_registered", "refund", "rf-l2", 0, 600),
        ("refund_approved", "refund", "rf-l2", -200, 400),
        ("refund_reversed", "refund", "rf-l2", 200, 600),
        ("payment_rollback", "rollback", "rb-l2", -100, 500),
    ]
    for entry, (action, kind, ref_id, delta, balance) in zip(entries, expected):
        assert (entry["action_type"], entry["ref_kind"], entry["ref_id"], entry["delta_cents"],
                entry["balance_after_cents"]) == (action, kind, ref_id, delta, balance)

    # 逐条变动额与前后余额衔接；最后一条余额等于订单对外已收。
    previous_balance = 0
    for entry in entries:
        assert entry["balance_after_cents"] == previous_balance + entry["delta_cents"]
        assert 0 <= entry["balance_after_cents"] <= 1000
        previous_balance = entry["balance_after_cents"]
    assert client.get("/orders/l2", headers={"X-Tenant": "t1"}).json()["paid_cents"] == entries[-1]["balance_after_cents"]


def test_reject_is_zero_delta_and_re_register_review_reverse_add_nothing() -> None:
    _accept("t1", "l3", 1000)
    client.post("/orders/l3/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    client.post(
        "/refunds",
        json={"tenant": "t1", "refund_id": "rf-l3", "order_id": "l3", "amount_cents": 100, "reason": "x"},
    )
    # 拒绝：变化额 0。
    assert client.post("/refunds/rf-l3/review", json={"decision": "reject"}, headers={"X-Tenant": "t1"}).status_code == 200
    # 不产生新动作的重复请求：均不得追加流水。
    assert client.post(
        "/refunds",
        json={"tenant": "t1", "refund_id": "rf-l3", "order_id": "l3", "amount_cents": 100, "reason": "x"},
    ).status_code == 200
    assert client.post("/refunds/rf-l3/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/refunds/rf-l3/reverse", headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post(
        "/payment-rollbacks",
        json={"tenant": "t1", "rollback_id": "rb-l3", "order_id": "l3", "amount_cents": 50},
    ).status_code == 201
    assert client.post(
        "/payment-rollbacks",
        json={"tenant": "t1", "rollback_id": "rb-l3", "order_id": "l3", "amount_cents": 999},
    ).status_code == 200

    entries = _ledger("l3", page_size=50).json()["entries"]
    assert [e["action_type"] for e in entries] == [
        "payment",
        "refund_registered",
        "refund_rejected",
        "payment_rollback",
    ]
    assert entries[2]["delta_cents"] == 0 and entries[2]["balance_after_cents"] == 500
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))


def test_failed_actions_write_nothing() -> None:
    _accept("t1", "l4", 1000)
    client.post("/orders/l4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    # 超额收款 409、超额回退 400、超额退款登记 409、对不存在订单的登记 400：均不写流水。
    assert client.post("/orders/l4/payments", json={"amount_cents": 5000}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post(
        "/payment-rollbacks",
        json={"tenant": "t1", "rollback_id": "rb-l4", "order_id": "l4", "amount_cents": 9999},
    ).status_code == 400
    assert client.post(
        "/refunds",
        json={"tenant": "t1", "refund_id": "rf-l4-big", "order_id": "l4", "amount_cents": 9999, "reason": "x"},
    ).status_code == 409
    assert client.post(
        "/refunds",
        json={"tenant": "t1", "refund_id": "rf-ghost", "order_id": "no-such-order", "amount_cents": 1, "reason": "x"},
    ).status_code == 400
    entries = _ledger("l4", page_size=50).json()["entries"]
    assert len(entries) == 1 and entries[0]["action_type"] == "payment"


def test_pagination_by_seq_is_stable_and_complete() -> None:
    _accept("t1", "l5", 1000)
    client.post("/orders/l5/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    for i in range(5):
        assert client.post(
            "/refunds",
            json={"tenant": "t1", "refund_id": f"rf-l5-{i}", "order_id": "l5", "amount_cents": 1, "reason": "x"},
        ).status_code == 201
    # 6 条流水（每次登记变化额 0），每页 2 条，按 seq 翻页取全不重不漏。
    all_seqs: list[int] = []
    cursor = None
    for _ in range(5):
        params = {"page_size": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = _ledger("l5", **params).json()
        all_seqs.extend(e["seq"] for e in body["entries"])
        if not body["has_next"]:
            assert body["next_cursor"] is None
            break
        cursor = body["next_cursor"]
    assert all_seqs == [1, 2, 3, 4, 5, 6]


def test_query_validation_and_tenant_isolation() -> None:
    _accept("t1", "l6", 1000)
    client.post("/orders/l6/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})

    # 缺租户头 400；page_size 缺失/非正整数 400。
    assert client.get("/orders/l6/ledger").status_code == 400
    assert _ledger("l6", page_size=0).status_code == 400
    assert _ledger("l6", page_size="abc").status_code == 400  # type: ignore[arg-type]
    # 游标不指向本租户该订单的流水：400（seq 不存在 / 非正整数）。
    assert _ledger("l6", page_size=10, cursor=999).status_code == 400
    assert _ledger("l6", page_size=10, cursor=-1).status_code == 400

    # 跨租户一律按订单不存在 404，即使游标指向 t1 的真实流水也不泄漏。
    assert _ledger("l6", tenant="t2", page_size=10).status_code == 404
    assert _ledger("no-such-order", tenant="t1", page_size=10).status_code == 404

    # 同一订单不同租户各有一条 seq=1 的流水，互不可见。
    _accept("t2", "l6", 500)
    client.post("/orders/l6/payments", json={"amount_cents": 50}, headers={"X-Tenant": "t2"})
    t2_entries = _ledger("l6", tenant="t2", page_size=10).json()["entries"]
    assert len(t2_entries) == 1 and t2_entries[0]["tenant"] == "t2" and t2_entries[0]["seq"] == 1


def test_reversed_twice_and_approved_after_reverse_add_nothing() -> None:
    _accept("t1", "l7", 1000)
    client.post("/orders/l7/payments", json={"amount_cents": 800}, headers={"X-Tenant": "t1"})
    client.post(
        "/refunds",
        json={"tenant": "t1", "refund_id": "rf-l7", "order_id": "l7", "amount_cents": 300, "reason": "x"},
    )
    client.post("/refunds/rf-l7/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"})
    client.post("/refunds/rf-l7/reverse", headers={"X-Tenant": "t1"})
    # 已冲正重复冲正 409；冲正后再审核 409；均不追加流水。
    assert client.post("/refunds/rf-l7/reverse", headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/refunds/rf-l7/review", json={"decision": "approve"}, headers={"X-Tenant": "t1"}).status_code == 409
    actions = [e["action_type"] for e in _ledger("l7", page_size=50).json()["entries"]]
    assert actions == ["payment", "refund_registered", "refund_approved", "refund_reversed"]
