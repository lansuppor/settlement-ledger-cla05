import os
import sqlite3
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_workorders.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "wt"
H = {"X-Tenant": T}


def _paid_order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _refund(order_id: str, refund_id: str, amount: int, request_id: str) -> None:
    assert client.post(f"/orders/{order_id}/refunds",
                       json={"refund_id": refund_id, "amount_cents": amount, "request_id": request_id},
                       headers=H).status_code == 201


def _refund_action(order_id: str, refund_id: str, action: str, request_id: str, headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/{action}",
                       json={"request_id": request_id}, headers=headers)


def _accept(order_id: str, refund_id: str, workorder_id: str, claim: int, request_id: str,
            initiator: str = "ops-1", reason: str = "amount disputed", headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/workorders",
                       json={"workorder_id": workorder_id, "claim_amount_cents": claim,
                             "initiator": initiator, "reason": reason, "request_id": request_id},
                       headers=headers)


def _advance(order_id: str, refund_id: str, workorder_id: str, to_status: str, request_id: str,
             award: int | None = None, headers=H):
    body = {"request_id": request_id, "to_status": to_status}
    if award is not None:
        body["award_cents"] = award
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/workorders/{workorder_id}/advance",
                       json=body, headers=headers)


def _cancel(order_id: str, refund_id: str, workorder_id: str, request_id: str, headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/workorders/{workorder_id}/cancel",
                       json={"request_id": request_id}, headers=headers)


def _get(order_id: str, refund_id: str, workorder_id: str, headers=H):
    return client.get(f"/orders/{order_id}/refunds/{refund_id}/workorders/{workorder_id}", headers=headers)


def test_accept_returns_fields_and_marks_refund() -> None:
    _paid_order("wo1", 1000, 1000)
    _refund("wo1", "rf1", 400, "wr-a1")
    r = _accept("wo1", "rf1", "wk1", 300, "wr-a2", initiator="agent-7", reason="customer dispute")
    assert r.status_code == 201
    body = r.json()
    assert body["workorder_id"] == "wk1" and body["status"] == "accepted"
    assert body["claim_amount_cents"] == 300 and body["initiator"] == "agent-7"
    assert body["effective_deduction_cents"] == 0
    # 受理不改变订单与退款单金额
    order = client.get("/orders/wo1", headers=H).json()
    assert order["paid_cents"] == 1000
    refund = client.get("/orders/wo1/refunds/rf1", headers=H).json()
    assert refund["amount_cents"] == 400 and refund["effective_deduction_cents"] == 0


def test_duplicate_accept_rejected_without_mutation() -> None:
    _paid_order("wo2", 500, 500)
    _refund("wo2", "rf1", 200, "wr-b1")
    assert _accept("wo2", "rf1", "wk1", 100, "wr-b2").status_code == 201
    dup = _accept("wo2", "rf1", "wk1", 150, "wr-b3")
    assert dup.status_code == 409
    got = _get("wo2", "rf1", "wk1").json()
    assert got["claim_amount_cents"] == 100 and got["status"] == "accepted"


def test_single_active_mark_and_release() -> None:
    _paid_order("wo3", 500, 500)
    _refund("wo3", "rf1", 200, "wr-c1")
    assert _accept("wo3", "rf1", "wk1", 100, "wr-c2").status_code == 201
    # 第二张进行中工单重复标记 -> 拒绝且不改数据
    assert _accept("wo3", "rf1", "wk2", 100, "wr-c3").status_code == 409
    assert _get("wo3", "rf1", "wk2").status_code == 404
    # 推进到已撤销释放标记后可再受理
    assert _advance("wo3", "rf1", "wk1", "cancelled", "wr-c4").status_code == 200
    assert _accept("wo3", "rf1", "wk2", 100, "wr-c5").status_code == 201


def test_accept_rejected_on_terminal_refund() -> None:
    _paid_order("wo4", 900, 900)
    _refund("wo4", "rf1", 100, "wr-d1")
    _refund_action("wo4", "rf1", "complete", "wr-d2")
    assert _accept("wo4", "rf1", "wk1", 50, "wr-d3").status_code == 409

    _refund("wo4", "rf2", 100, "wr-d4")
    _refund_action("wo4", "rf2", "cancel", "wr-d5")
    assert _accept("wo4", "rf2", "wk2", 50, "wr-d6").status_code == 409

    _refund("wo4", "rf3", 100, "wr-d7")
    _refund_action("wo4", "rf3", "complete", "wr-d8")
    _refund_action("wo4", "rf3", "reverse", "wr-d9")
    assert _accept("wo4", "rf3", "wk3", 50, "wr-d10").status_code == 409


def test_accept_missing_refund_and_cross_tenant() -> None:
    _paid_order("wo5", 100, 100)
    assert _accept("wo5", "missing", "wk1", 10, "wr-e1").status_code == 404
    _refund("wo5", "rf1", 100, "wr-e2")
    other = {"X-Tenant": "wt-other"}
    assert _accept("wo5", "rf1", "wk1", 10, "wr-e3", headers=other).status_code == 404
    assert _get("wo5", "rf1", "wk1", headers=other).status_code == 404
    assert client.get("/orders/wo5/refunds/rf1/workorders", headers=other).status_code == 404
    # 跨租户操作未影响本租户
    assert _accept("wo5", "rf1", "wk1", 10, "wr-e4").status_code == 201


def test_accept_does_not_change_refundable_balance() -> None:
    _paid_order("wo6", 1000, 1000)
    _refund("wo6", "rf1", 1000, "wr-f1")  # 占满可退余额
    assert _accept("wo6", "rf1", "wk1", 500, "wr-f2").status_code == 201
    # 受理工单不释放也不占用余额：再受理退款单仍超限
    assert client.post("/orders/wo6/refunds",
                       json={"refund_id": "rf2", "amount_cents": 1, "request_id": "wr-f3"},
                       headers=H).status_code == 409


def test_refund_blocked_while_workorder_in_progress() -> None:
    _paid_order("wo7", 800, 800)
    _refund("wo7", "rf1", 300, "wr-g1")
    _accept("wo7", "rf1", "wk1", 200, "wr-g2")
    for action in ("complete", "cancel", "reverse"):
        r = _refund_action("wo7", "rf1", action, f"wr-g3-{action}")
        assert r.status_code == 409
        assert "workorder" in r.json()["detail"]
    # 推进到处理中、待复核期间仍然封锁
    _advance("wo7", "rf1", "wk1", "processing", "wr-g4")
    assert _refund_action("wo7", "rf1", "complete", "wr-g5").status_code == 409
    _advance("wo7", "rf1", "wk1", "pending_review", "wr-g6")
    assert _refund_action("wo7", "rf1", "cancel", "wr-g7").status_code == 409
    # 解决后不再是进行中，退款单可按扣减后金额完成
    assert _advance("wo7", "rf1", "wk1", "resolved", "wr-g8", award=100).status_code == 200
    done = _refund_action("wo7", "rf1", "complete", "wr-g9")
    assert done.status_code == 200
    assert done.json()["amount_cents"] == 200
    order = client.get("/orders/wo7", headers=H).json()
    assert order["paid_cents"] == 600


def test_advance_state_machine() -> None:
    _paid_order("wo8", 500, 500)
    _refund("wo8", "rf1", 200, "wr-h1")
    _accept("wo8", "rf1", "wk1", 100, "wr-h2")
    # 非法跃迁
    assert _advance("wo8", "rf1", "wk1", "resolved", "wr-h3", award=10).status_code == 409
    assert _advance("wo8", "rf1", "wk1", "pending_review", "wr-h4").status_code == 409
    # 已受理 -> 处理中 -> 待复核 -> 处理中 -> 已解决
    assert _advance("wo8", "rf1", "wk1", "processing", "wr-h5").json()["status"] == "processing"
    assert _advance("wo8", "rf1", "wk1", "pending_review", "wr-h6").json()["status"] == "pending_review"
    assert _advance("wo8", "rf1", "wk1", "processing", "wr-h7").json()["status"] == "processing"
    assert _advance("wo8", "rf1", "wk1", "resolved", "wr-h8", award=50).json()["status"] == "resolved"
    # 终态不可再推进
    assert _advance("wo8", "rf1", "wk1", "processing", "wr-h9").status_code == 409
    assert _advance("wo8", "rf1", "wk1", "cancelled", "wr-h10").status_code == 409
    # 已撤销终态同样不可推进
    _refund("wo8", "rf2", 100, "wr-h11")
    _accept("wo8", "rf2", "wk2", 50, "wr-h12")
    _advance("wo8", "rf2", "wk2", "cancelled", "wr-h13")
    assert _advance("wo8", "rf2", "wk2", "processing", "wr-h14").status_code == 409


def test_resolve_deducts_within_bounds() -> None:
    _paid_order("wo9", 1000, 1000)
    _refund("wo9", "rf1", 400, "wr-i1")
    _accept("wo9", "rf1", "wk1", 300, "wr-i2")
    _advance("wo9", "rf1", "wk1", "processing", "wr-i3")
    # 超过处理请求金额 -> 拒绝且不改数据
    assert _advance("wo9", "rf1", "wk1", "resolved", "wr-i4", award=301).status_code == 409
    assert client.get("/orders/wo9/refunds/rf1", headers=H).json()["amount_cents"] == 400
    # 超过退款金额（请求额 500 > 退款 400）-> 拒绝
    _refund("wo9", "rf2", 400, "wr-i5")
    _accept("wo9", "rf2", "wk2", 500, "wr-i6")
    _advance("wo9", "rf2", "wk2", "processing", "wr-i7")
    assert _advance("wo9", "rf2", "wk2", "resolved", "wr-i8", award=401).status_code == 409
    # 非法裁决金额
    assert _advance("wo9", "rf1", "wk1", "resolved", "wr-i9", award=0).status_code == 422
    assert _advance("wo9", "rf1", "wk1", "resolved", "wr-i10").status_code == 400
    # 成功解决：退款单金额与生效扣减同步，订单金额不变
    r = _advance("wo9", "rf1", "wk1", "resolved", "wr-i11", award=250)
    assert r.status_code == 200
    body = r.json()
    assert body["award_cents"] == 250 and body["effective_deduction_cents"] == 250
    assert body["refund_amount_cents"] == 150 and body["refund_effective_deduction_cents"] == 250
    refund = client.get("/orders/wo9/refunds/rf1", headers=H).json()
    assert refund["amount_cents"] == 150 and refund["effective_deduction_cents"] == 250
    order = client.get("/orders/wo9", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


def test_cancel_close_restores_both_sides_atomically() -> None:
    _paid_order("wo10", 600, 600)
    _refund("wo10", "rf1", 400, "wr-j1")
    _accept("wo10", "rf1", "wk1", 300, "wr-j2")
    _advance("wo10", "rf1", "wk1", "processing", "wr-j3")
    _advance("wo10", "rf1", "wk1", "resolved", "wr-j4", award=120)
    r = _cancel("wo10", "rf1", "wk1", "wr-j5")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled" and body["effective_deduction_cents"] == 0
    assert body["refund_amount_cents"] == 400 and body["refund_effective_deduction_cents"] == 0
    raw = sqlite3.connect(db_path())
    amount, effective = raw.execute(
        "SELECT amount_cents, effective_deduction_cents FROM refunds"
        " WHERE tenant='wt' AND order_id='wo10' AND refund_id='rf1'").fetchone()
    status, deduct = raw.execute(
        "SELECT status, effective_deduction_cents FROM workorders"
        " WHERE tenant='wt' AND order_id='wo10' AND refund_id='rf1' AND workorder_id='wk1'").fetchone()
    raw.close()
    assert (amount, effective) == (400, 0)
    assert (status, deduct) == ("cancelled", 0)
    # 每张工单只能从已解决撤销一次
    assert _cancel("wo10", "rf1", "wk1", "wr-j6").status_code == 409


def test_cancel_close_only_from_resolved() -> None:
    _paid_order("wo11", 300, 300)
    _refund("wo11", "rf1", 100, "wr-k1")
    _accept("wo11", "rf1", "wk1", 50, "wr-k2")
    assert _cancel("wo11", "rf1", "wk1", "wr-k3").status_code == 409
    _advance("wo11", "rf1", "wk1", "processing", "wr-k4")
    assert _cancel("wo11", "rf1", "wk1", "wr-k5").status_code == 409
    assert _cancel("wo11", "rf1", "missing", "wr-k6").status_code == 404


def test_idempotent_replays() -> None:
    _paid_order("wo12", 800, 800)
    _refund("wo12", "rf1", 500, "wr-l1")
    a1 = _accept("wo12", "rf1", "wk1", 200, "wr-l2")
    a2 = _accept("wo12", "rf1", "wk1", 200, "wr-l2")
    assert a1.status_code == a2.status_code == 201 and a1.json() == a2.json()
    listed = client.get("/orders/wo12/refunds/rf1/workorders", headers=H).json()["workorders"]
    assert len(listed) == 1

    _advance("wo12", "rf1", "wk1", "processing", "wr-l3")
    r1 = _advance("wo12", "rf1", "wk1", "resolved", "wr-l4", award=150)
    r2 = _advance("wo12", "rf1", "wk1", "resolved", "wr-l4", award=150)
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert client.get("/orders/wo12/refunds/rf1", headers=H).json()["amount_cents"] == 350

    c1 = _cancel("wo12", "rf1", "wk1", "wr-l5")
    c2 = _cancel("wo12", "rf1", "wk1", "wr-l5")
    assert c1.status_code == c2.status_code == 200 and c1.json() == c2.json()
    assert client.get("/orders/wo12/refunds/rf1", headers=H).json()["amount_cents"] == 500

    # 同一 request_id 改作不同操作/对象 -> 409
    assert _accept("wo12", "rf1", "wk9", 10, "wr-l2").status_code == 409
    assert _cancel("wo12", "rf1", "wk1", "wr-l4").status_code == 409
    # 失败结果同样可重放
    f1 = _accept("wo12", "rf1", "wk8", 10, "wr-l6")  # 已有已撤销工单？无，wk1 已撤销 -> 应成功
    assert f1.status_code == 201
    f2 = _advance("wo12", "rf1", "wk8", "resolved", "wr-l7", award=10)  # accepted 不可直接 resolved
    f3 = _advance("wo12", "rf1", "wk8", "resolved", "wr-l7", award=10)
    assert f2.status_code == f3.status_code == 409 and f2.json() == f3.json()


def test_concurrent_accept_single_mark() -> None:
    _paid_order("wo13", 500, 500)
    _refund("wo13", "rf1", 200, "wr-m1")
    results: list[int] = []
    barrier = threading.Barrier(4)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_accept("wo13", "rf1", f"wk{i}", 100, f"wr-m2-{i}").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 3


def test_concurrent_resolve_never_exceeds_bounds() -> None:
    _paid_order("wo14", 1000, 1000)
    _refund("wo14", "rf1", 300, "wr-n1")
    _accept("wo14", "rf1", "wk1", 300, "wr-n2")
    _advance("wo14", "rf1", "wk1", "processing", "wr-n3")
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_advance("wo14", "rf1", "wk1", "resolved", f"wr-n4-{i}", award=200).status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409]
    refund = client.get("/orders/wo14/refunds/rf1", headers=H).json()
    assert refund["amount_cents"] == 100 and refund["effective_deduction_cents"] == 200


def test_list_and_read_shape() -> None:
    _paid_order("wo15", 900, 900)
    _refund("wo15", "rf1", 600, "wr-o1")
    _accept("wo15", "rf1", "wk1", 100, "wr-o2")
    _advance("wo15", "rf1", "wk1", "cancelled", "wr-o3")
    _accept("wo15", "rf1", "wk2", 200, "wr-o4")
    listed = client.get("/orders/wo15/refunds/rf1/workorders", headers=H)
    assert listed.status_code == 200
    items = listed.json()["workorders"]
    assert [w["workorder_id"] for w in items] == ["wk1", "wk2"]
    for w in items:
        assert {"workorder_id", "status", "claim_amount_cents", "initiator",
                "effective_deduction_cents"} <= set(w)
    by_id = {w["workorder_id"]: w for w in items}
    assert by_id["wk1"]["status"] == "cancelled"
    assert by_id["wk2"]["status"] == "accepted"
    # 其他退款单 / 订单的列表不受影响
    assert client.get("/orders/wo15/refunds/missing/workorders", headers=H).status_code == 404


def test_tenant_header_required() -> None:
    assert client.post("/orders/wo1/refunds/rf1/workorders",
                       json={"workorder_id": "x", "claim_amount_cents": 1,
                             "initiator": "a", "reason": "b", "request_id": "z"}).status_code == 400


def test_invalid_accept_fields_rejected_by_validation() -> None:
    _paid_order("wo16", 100, 100)
    _refund("wo16", "rf1", 100, "wr-p1")
    assert _accept("wo16", "rf1", "wk1", 0, "wr-p2").status_code == 422
    assert _accept("wo16", "rf1", "wk1", 10, "wr-p3", reason="").status_code == 422
    assert _accept("wo16", "rf1", "wk1", 10, "wr-p4", initiator="").status_code == 422


def test_state_survives_restart_and_replay() -> None:
    _paid_order("wo17", 800, 800)
    _refund("wo17", "rf1", 500, "wr-q1")
    _accept("wo17", "rf1", "wk1", 300, "wr-q2")
    _advance("wo17", "rf1", "wk1", "processing", "wr-q3")
    _advance("wo17", "rf1", "wk1", "resolved", "wr-q4", award=200)
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    got = _get("wo17", "rf1", "wk1").json()
    assert got["status"] == "resolved" and got["effective_deduction_cents"] == 200
    refund = client.get("/orders/wo17/refunds/rf1", headers=H).json()
    assert refund["amount_cents"] == 300 and refund["effective_deduction_cents"] == 200
    # 重启后重放旧请求，结果一致且不重复扣减
    replay = _advance("wo17", "rf1", "wk1", "resolved", "wr-q4", award=200)
    assert replay.status_code == 200 and replay.json()["status"] == "resolved"
    assert client.get("/orders/wo17/refunds/rf1", headers=H).json()["amount_cents"] == 300
    # 重启后撤销关闭仍原子生效
    assert _cancel("wo17", "rf1", "wk1", "wr-q5").status_code == 200
    assert client.get("/orders/wo17/refunds/rf1", headers=H).json()["amount_cents"] == 500
