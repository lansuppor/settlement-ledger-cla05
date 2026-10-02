import os
import sqlite3
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_tickets.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

T = "tt"
H = {"X-Tenant": T}
OTHER = {"X-Tenant": "tt-other"}


def _paid_order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _refund(order_id: str, refund_id: str, amount: int, request_id: str, action: str | None = None) -> str:
    assert client.post(f"/orders/{order_id}/refunds",
                       json={"refund_id": refund_id, "amount_cents": amount, "request_id": request_id},
                       headers=H).status_code == 201
    if action:
        assert client.post(f"/orders/{order_id}/refunds/{refund_id}/{action}",
                           json={"request_id": request_id + "-act"}, headers=H).status_code == 200
    return refund_id


def _accept(order_id, refund_id, ticket_id, amount, request_id, initiator="alice",
            reason="货不对版", headers=H):
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/tickets",
                       json={"ticket_id": ticket_id, "request_amount_cents": amount,
                             "initiator": initiator, "reason": reason, "request_id": request_id},
                       headers=headers)


def _action(order_id, refund_id, ticket_id, action, request_id, json_extra=None, headers=H):
    body = {"request_id": request_id, **(json_extra or {})}
    return client.post(f"/orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/{action}",
                       json=body, headers=headers)


def _get_refund(order_id, refund_id):
    return client.get(f"/orders/{order_id}/refunds/{refund_id}", headers=H).json()


def test_accept_ticket_and_read_shape() -> None:
    _paid_order("to1", 1000, 1000)
    _refund("to1", "rf1", 300, "t-r1")
    r = _accept("to1", "rf1", "tk1", 200, "t-a1")
    assert r.status_code == 201
    body = r.json()
    assert body == {"ticket_id": "tk1", "status": "accepted", "request_amount_cents": 200,
                    "initiator": "alice", "effective_deduction_cents": 0}
    got = client.get("/orders/to1/refunds/rf1/tickets/tk1", headers=H)
    assert got.status_code == 200 and got.json() == body


def test_duplicate_ticket_id_refused_without_mutation() -> None:
    _paid_order("to2", 500, 500)
    _refund("to2", "rf1", 100, "t-r2")
    assert _accept("to2", "rf1", "tk1", 100, "t-b1").status_code == 201
    dup = _accept("to2", "rf1", "tk1", 200, "t-b2")
    assert dup.status_code == 409
    got = client.get("/orders/to2/refunds/rf1/tickets/tk1", headers=H).json()
    assert got["request_amount_cents"] == 100 and got["status"] == "accepted"


def test_only_one_active_ticket_per_refund() -> None:
    _paid_order("to3", 900, 900)
    _refund("to3", "rf1", 300, "t-r3")
    assert _accept("to3", "rf1", "tk1", 100, "t-c1").status_code == 201
    assert _accept("to3", "rf1", "tk2", 100, "t-c2").status_code == 409
    # 终结后标记释放，可再受理新工单
    assert _action("to3", "rf1", "tk1", "revoke", "t-c3").status_code == 200
    assert _accept("to3", "rf1", "tk2", 100, "t-c4").status_code == 201


def test_accept_requires_existing_refund() -> None:
    _paid_order("to4", 100, 100)
    assert _accept("to4", "ghost", "tk1", 1, "t-d1").status_code == 404


def test_cancelled_or_reversed_refund_not_acceptable() -> None:
    _paid_order("to5", 500, 500)
    _refund("to5", "rf1", 100, "t-r5a", action="cancel")
    assert _accept("to5", "rf1", "tk1", 50, "t-e1").status_code == 409
    _refund("to5", "rf2", 100, "t-r5b")
    assert client.post("/orders/to5/refunds/rf2/complete", json={"request_id": "t-r5b-c"},
                       headers=H).status_code == 200
    assert client.post("/orders/to5/refunds/rf2/reverse", json={"request_id": "t-r5b-r"},
                       headers=H).status_code == 200
    assert _accept("to5", "rf2", "tk2", 50, "t-e2").status_code == 409
    # 已完成的退款单可以被工单受理
    _refund("to5", "rf3", 100, "t-r5c")
    assert client.post("/orders/to5/refunds/rf3/complete", json={"request_id": "t-r5c-c"},
                       headers=H).status_code == 200
    assert _accept("to5", "rf3", "tk3", 50, "t-e3").status_code == 201


def test_active_ticket_blocks_refund_actions() -> None:
    _paid_order("to6", 600, 600)
    _refund("to6", "rf1", 200, "t-r6")
    _accept("to6", "rf1", "tk1", 100, "t-f1")
    blocked = {"detail": "refund is locked by an in-progress ticket"}
    r1 = client.post("/orders/to6/refunds/rf1/complete", json={"request_id": "t-f2"}, headers=H)
    r2 = client.post("/orders/to6/refunds/rf1/cancel", json={"request_id": "t-f3"}, headers=H)
    assert r1.status_code == r2.status_code == 409 and r1.json() == r2.json() == blocked
    # 推进到处理中、待复核期间仍然锁定
    assert _action("to6", "rf1", "tk1", "process", "t-f4").status_code == 200
    assert client.post("/orders/to6/refunds/rf1/complete", json={"request_id": "t-f5"},
                       headers=H).status_code == 409
    assert _action("to6", "rf1", "tk1", "review", "t-f6").status_code == 200
    assert client.post("/orders/to6/refunds/rf1/cancel", json={"request_id": "t-f7"},
                       headers=H).status_code == 409
    # 工单终结（撤销）后退款单可继续推进
    assert _action("to6", "rf1", "tk1", "revoke", "t-f8").status_code == 200
    assert client.post("/orders/to6/refunds/rf1/cancel", json={"request_id": "t-f9"},
                       headers=H).status_code == 200


def test_active_ticket_blocks_reverse_of_completed_refund() -> None:
    _paid_order("to7", 400, 400)
    _refund("to7", "rf1", 200, "t-r7", action="complete")
    _accept("to7", "rf1", "tk1", 100, "t-g1")
    r = client.post("/orders/to7/refunds/rf1/reverse", json={"request_id": "t-g2"}, headers=H)
    assert r.status_code == 409 and r.json() == {"detail": "refund is locked by an in-progress ticket"}


def test_state_machine_transitions() -> None:
    _paid_order("to8", 500, 500)
    _refund("to8", "rf1", 300, "t-r8")
    _accept("to8", "rf1", "tk1", 300, "t-h1")
    # accepted 不能直接 review/resolve/reprocess
    assert _action("to8", "rf1", "tk1", "review", "t-h2").status_code == 409
    assert _action("to8", "rf1", "tk1", "reprocess", "t-h3").status_code == 409
    assert _action("to8", "rf1", "tk1", "resolve", "t-h4", {"award_cents": 10}).status_code == 409
    # accepted -> processing -> review -> processing 回退
    assert _action("to8", "rf1", "tk1", "process", "t-h5").status_code == 200
    assert _action("to8", "rf1", "tk1", "process", "t-h6").status_code == 409
    assert _action("to8", "rf1", "tk1", "review", "t-h7").status_code == 200
    assert _action("to8", "rf1", "tk1", "review", "t-h8").status_code == 409
    assert _action("to8", "rf1", "tk1", "reprocess", "t-h9").status_code == 200
    # processing -> resolved 终态
    done = _action("to8", "rf1", "tk1", "resolve", "t-h10", {"award_cents": 100})
    assert done.status_code == 200 and done.json()["status"] == "resolved"
    for action, extra in [("process", None), ("review", None), ("reprocess", None),
                          ("resolve", {"award_cents": 1})]:
        assert _action("to8", "rf1", "tk1", action, f"t-h-{action}", extra).status_code == 409


def test_resolve_deducts_refund_within_limits_pending_refund() -> None:
    _paid_order("to9", 1000, 1000)
    _refund("to9", "rf1", 300, "t-r9")
    _accept("to9", "rf1", "tk1", 200, "t-i1")
    _action("to9", "rf1", "tk1", "process", "t-i2")
    # 超处理请求金额 / 非法金额拒绝且不改数据
    assert _action("to9", "rf1", "tk1", "resolve", "t-i3", {"award_cents": 201}).status_code == 409
    assert _get_refund("to9", "rf1")["amount_cents"] == 300
    # 0 与负数在入口即 422
    assert _action("to9", "rf1", "tk1", "resolve", "t-i4", {"award_cents": 0}).status_code == 422
    assert _action("to9", "rf1", "tk1", "resolve", "t-i5", {"award_cents": -1}).status_code == 422
    # 成功：退款单金额扣减；退款单仍 pending，生效扣减 0；订单金额不变
    ok = _action("to9", "rf1", "tk1", "resolve", "t-i6", {"award_cents": 150})
    assert ok.status_code == 200 and ok.json()["effective_deduction_cents"] == 150
    refund = _get_refund("to9", "rf1")
    assert refund["amount_cents"] == 150 and refund["effective_deduction_cents"] == 0
    order = client.get("/orders/to9", headers=H).json()
    assert order["paid_cents"] == 1000
    # 标记释放后完成退款单：按扣减后金额扣订单
    assert client.post("/orders/to9/refunds/rf1/complete", json={"request_id": "t-i7"},
                       headers=H).status_code == 200
    assert _get_refund("to9", "rf1")["effective_deduction_cents"] == 150
    assert client.get("/orders/to9", headers=H).json()["paid_cents"] == 850


def test_resolve_on_completed_refund_syncs_effective_deduction() -> None:
    _paid_order("to10", 500, 500)
    _refund("to10", "rf1", 300, "t-r10", action="complete")
    assert client.get("/orders/to10", headers=H).json()["paid_cents"] == 200
    _accept("to10", "rf1", "tk1", 300, "t-j1")
    _action("to10", "rf1", "tk1", "process", "t-j2")
    # 裁决金额不得超过退款金额
    assert _action("to10", "rf1", "tk1", "resolve", "t-j3", {"award_cents": 301}).status_code == 409
    ok = _action("to10", "rf1", "tk1", "resolve", "t-j4", {"award_cents": 120})
    assert ok.status_code == 200
    refund = _get_refund("to10", "rf1")
    assert refund["amount_cents"] == 180 and refund["status"] == "completed"
    assert refund["effective_deduction_cents"] == 180
    # 订单金额不随工单裁决变化
    assert client.get("/orders/to10", headers=H).json()["paid_cents"] == 200


def test_revoke_resolved_adds_back_once() -> None:
    _paid_order("to11", 500, 500)
    _refund("to11", "rf1", 300, "t-r11", action="complete")
    _accept("to11", "rf1", "tk1", 200, "t-k1")
    _action("to11", "rf1", "tk1", "process", "t-k2")
    _action("to11", "rf1", "tk1", "resolve", "t-k3", {"award_cents": 120})
    assert _get_refund("to11", "rf1")["amount_cents"] == 180

    rv = _action("to11", "rf1", "tk1", "revoke", "t-k4")
    assert rv.status_code == 200
    body = rv.json()
    assert body["status"] == "revoked" and body["effective_deduction_cents"] == 0
    refund = _get_refund("to11", "rf1")
    assert refund["amount_cents"] == 300 and refund["effective_deduction_cents"] == 300
    assert client.get("/orders/to11", headers=H).json()["paid_cents"] == 200
    # 已撤销终态不可再撤销或推进
    assert _action("to11", "rf1", "tk1", "revoke", "t-k5").status_code == 409
    assert _action("to11", "rf1", "tk1", "process", "t-k6").status_code == 409
    # 只加回一次：金额未变
    assert _get_refund("to11", "rf1")["amount_cents"] == 300


def test_revoke_active_ticket_only_releases_marker() -> None:
    _paid_order("to12", 300, 300)
    _refund("to12", "rf1", 100, "t-r12")
    _accept("to12", "rf1", "tk1", 100, "t-l1")
    _action("to12", "rf1", "tk1", "process", "t-l2")
    rv = _action("to12", "rf1", "tk1", "revoke", "t-l3")
    assert rv.status_code == 200 and rv.json()["status"] == "revoked"
    assert rv.json()["effective_deduction_cents"] == 0
    refund = _get_refund("to12", "rf1")
    assert refund["amount_cents"] == 100 and refund["status"] == "pending"
    # 标记释放：退款单可撤销，且可再开新工单
    assert client.post("/orders/to12/refunds/rf1/cancel", json={"request_id": "t-l4"},
                       headers=H).status_code == 200


def test_resolve_and_revoke_are_atomic_in_db() -> None:
    _paid_order("to13", 400, 400)
    _refund("to13", "rf1", 200, "t-r13", action="complete")
    _accept("to13", "rf1", "tk1", 200, "t-m1")
    _action("to13", "rf1", "tk1", "process", "t-m2")
    _action("to13", "rf1", "tk1", "resolve", "t-m3", {"award_cents": 70})
    raw = sqlite3.connect(db_path())
    amount, ded = raw.execute(
        "SELECT amount_cents, effective_deduction_cents FROM refunds"
        " WHERE tenant='tt' AND order_id='to13' AND refund_id='rf1'").fetchone()
    tstatus, award = raw.execute(
        "SELECT status, award_cents FROM refund_tickets"
        " WHERE tenant='tt' AND order_id='to13' AND refund_id='rf1' AND ticket_id='tk1'").fetchone()
    assert (amount, ded) == (130, 130)
    assert (tstatus, award) == ("resolved", 70)
    # 撤销后两侧一致：金额加回、裁决清零、终态 revoked
    _action("to13", "rf1", "tk1", "revoke", "t-m4")
    amount, ded = raw.execute(
        "SELECT amount_cents, effective_deduction_cents FROM refunds"
        " WHERE tenant='tt' AND order_id='to13' AND refund_id='rf1'").fetchone()
    tstatus, award = raw.execute(
        "SELECT status, award_cents FROM refund_tickets"
        " WHERE tenant='tt' AND order_id='to13' AND refund_id='rf1' AND ticket_id='tk1'").fetchone()
    raw.close()
    assert (amount, ded) == (200, 200)
    assert (tstatus, award) == ("revoked", 0)


def test_idempotent_replays() -> None:
    _paid_order("to14", 900, 900)
    _refund("to14", "rf1", 400, "t-r14")
    first = _accept("to14", "rf1", "tk1", 300, "t-n1")
    again = _accept("to14", "rf1", "tk1", 300, "t-n1")
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    assert len(client.get("/orders/to14/refunds/rf1/tickets", headers=H).json()["tickets"]) == 1

    p1 = _action("to14", "rf1", "tk1", "process", "t-n2")
    p2 = _action("to14", "rf1", "tk1", "process", "t-n2")
    assert p1.status_code == p2.status_code == 200 and p1.json() == p2.json()

    s1 = _action("to14", "rf1", "tk1", "resolve", "t-n3", {"award_cents": 100})
    s2 = _action("to14", "rf1", "tk1", "resolve", "t-n3", {"award_cents": 100})
    assert s1.status_code == s2.status_code == 200 and s1.json() == s2.json()
    # 重放不重复扣减
    assert _get_refund("to14", "rf1")["amount_cents"] == 300

    v1 = _action("to14", "rf1", "tk1", "revoke", "t-n4")
    v2 = _action("to14", "rf1", "tk1", "revoke", "t-n4")
    assert v1.status_code == v2.status_code == 200 and v1.json() == v2.json()
    assert _get_refund("to14", "rf1")["amount_cents"] == 400

    # 同一 request_id 改作不同操作/对象 -> 409
    assert _action("to14", "rf1", "tk1", "review", "t-n1").status_code == 409
    assert _action("to14", "rf1", "tk9", "process", "t-n2").status_code == 409
    # 失败结果同样可重放
    f1 = _action("to14", "rf1", "tk1", "process", "t-n5")
    f2 = _action("to14", "rf1", "tk1", "process", "t-n5")
    assert f1.status_code == f2.status_code == 409 and f1.json() == f2.json()


def test_concurrent_accepts_single_active_marker() -> None:
    _paid_order("to15", 500, 500)
    _refund("to15", "rf1", 200, "t-r15")
    results: list[int] = []
    barrier = threading.Barrier(6)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(_accept("to15", "rf1", f"tk{i}", 100, f"t-o{i}").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 5


def test_concurrent_resolve_vs_revoke_keeps_invariants() -> None:
    # 工单裁决与撤销并发：串行化后最终状态合法，金额要么未动要么扣减后已加回
    _paid_order("to16", 800, 800)
    _refund("to16", "rf1", 300, "t-r16")
    _accept("to16", "rf1", "tk1", 300, "t-p1")
    _action("to16", "rf1", "tk1", "process", "t-p2")
    out = {}
    barrier = threading.Barrier(2)

    def resolve_ticket() -> None:
        barrier.wait()
        out["resolve"] = _action("to16", "rf1", "tk1", "resolve", "t-p3", {"award_cents": 100}).status_code

    def revoke_ticket() -> None:
        barrier.wait()
        out["revoke"] = _action("to16", "rf1", "tk1", "revoke", "t-p4").status_code

    threads = [threading.Thread(target=resolve_ticket), threading.Thread(target=revoke_ticket)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 进行中撤销必成功；裁决可能成功（后被撤销加回）或失败（撤销先提交）
    assert out["revoke"] == 200 and out["resolve"] in (200, 409)
    final = client.get("/orders/to16/refunds/rf1/tickets/tk1", headers=H).json()
    assert final["status"] == "revoked" and final["effective_deduction_cents"] == 0
    refund = _get_refund("to16", "rf1")
    assert refund["amount_cents"] == 300 and refund["effective_deduction_cents"] == 0
    assert client.get("/orders/to16", headers=H).json()["paid_cents"] == 800


def test_cross_tenant_is_not_found_everywhere() -> None:
    _paid_order("to17", 200, 200)
    _refund("to17", "rf1", 100, "t-r17")
    _accept("to17", "rf1", "tk1", 100, "t-q1")
    assert client.get("/orders/to17/refunds/rf1/tickets/tk1", headers=OTHER).status_code == 404
    listed = client.get("/orders/to17/refunds/rf1/tickets", headers=OTHER)
    assert listed.status_code == 404
    assert _accept("to17", "rf1", "tk2", 1, "t-q2", headers=OTHER).status_code == 404
    assert _action("to17", "rf1", "tk1", "revoke", "t-q3", headers=OTHER).status_code == 404
    # 跨租户操作未影响本租户
    assert client.get("/orders/to17/refunds/rf1/tickets/tk1", headers=H).json()["status"] == "accepted"
    # 另一租户在其自有同标识订单/退款单上可独立受理工单，互不相见
    assert client.post("/orders", json={"tenant": "tt-other", "order_id": "to17",
                                       "amount_cents": 200, "currency": "CNY"}).status_code == 201
    assert client.post("/orders/to17/payments", json={"amount_cents": 200},
                       headers=OTHER).status_code == 200
    assert client.post("/orders/to17/refunds", json={
        "refund_id": "rf1", "amount_cents": 100, "request_id": "t-q5"}, headers=OTHER).status_code == 201
    assert _accept("to17", "rf1", "tk1", 100, "t-q6", headers=OTHER).status_code == 201
    assert len(client.get("/orders/to17/refunds/rf1/tickets", headers=H).json()["tickets"]) == 1
    assert client.get("/orders/to17/refunds/rf1/tickets/tk1", headers=H).json()["initiator"] == "alice"


def test_list_shape_and_ordering() -> None:
    _paid_order("to18", 900, 900)
    _refund("to18", "rf1", 400, "t-r18")
    _accept("to18", "rf1", "a", 100, "t-s1")
    _action("to18", "rf1", "a", "process", "t-s2")
    _action("to18", "rf1", "a", "resolve", "t-s3", {"award_cents": 80})
    _accept("to18", "rf1", "b", 100, "t-s4")
    _action("to18", "rf1", "b", "revoke", "t-s5")
    _accept("to18", "rf1", "c", 100, "t-s6")
    listed = client.get("/orders/to18/refunds/rf1/tickets", headers=H)
    assert listed.status_code == 200
    items = listed.json()["tickets"]
    assert [t["ticket_id"] for t in items] == ["a", "b", "c"]
    by_id = {t["ticket_id"]: t for t in items}
    assert by_id["a"]["status"] == "resolved" and by_id["a"]["effective_deduction_cents"] == 80
    assert by_id["b"]["status"] == "revoked" and by_id["b"]["effective_deduction_cents"] == 0
    assert by_id["c"]["status"] == "accepted"
    for t in items:
        assert set(t) == {"ticket_id", "status", "request_amount_cents",
                          "initiator", "effective_deduction_cents"}


def test_list_missing_refund_is_404() -> None:
    _paid_order("to19", 100, 100)
    assert client.get("/orders/to19/refunds/ghost/tickets", headers=H).status_code == 404


def test_tenant_header_required() -> None:
    r = client.post("/orders/to1/refunds/rf1/tickets",
                    json={"ticket_id": "x", "request_amount_cents": 1,
                          "initiator": "i", "reason": "r", "request_id": "z"})
    assert r.status_code == 400


def test_invalid_fields_rejected_by_validation() -> None:
    _paid_order("to20", 100, 100)
    _refund("to20", "rf1", 100, "t-r20")
    base = {"ticket_id": "tk1", "request_amount_cents": 10, "initiator": "i",
            "reason": "r", "request_id": "z0"}
    for patch in [
        {"request_amount_cents": 0},
        {"request_amount_cents": -3},
        {"initiator": ""},
        {"reason": ""},
        {"ticket_id": ""},
        {"request_id": ""},
    ]:
        body = {**base, **patch}
        assert client.post("/orders/to20/refunds/rf1/tickets", json=body, headers=H).status_code == 422


def test_state_survives_restart_and_replay() -> None:
    _paid_order("to21", 800, 800)
    _refund("to21", "rf1", 400, "t-r21", action="complete")
    _accept("to21", "rf1", "tk1", 300, "t-u1")
    _action("to21", "rf1", "tk1", "process", "t-u2")
    _action("to21", "rf1", "tk1", "resolve", "t-u3", {"award_cents": 90})
    _refund2 = client.post("/orders/to21/refunds", json={
        "refund_id": "rf2", "amount_cents": 90, "request_id": "t-r21b"}, headers=H)
    assert _refund2.status_code == 201
    _accept("to21", "rf2", "tk2", 100, "t-u4")
    _action("to21", "rf2", "tk2", "revoke", "t-u5")
    # 模拟重启：迁移可重入，每次请求本就重连库文件
    migrate()
    by_id = {t["ticket_id"]: t for t in
             client.get("/orders/to21/refunds/rf1/tickets", headers=H).json()["tickets"]}
    assert by_id["tk1"]["status"] == "resolved" and by_id["tk1"]["effective_deduction_cents"] == 90
    refund = _get_refund("to21", "rf1")
    assert refund["amount_cents"] == 310 and refund["effective_deduction_cents"] == 310
    # 重启后重放：不重复扣减/加回
    replay = _action("to21", "rf1", "tk1", "resolve", "t-u3", {"award_cents": 90})
    assert replay.status_code == 200 and replay.json()["status"] == "resolved"
    assert _get_refund("to21", "rf1")["amount_cents"] == 310
    revoke = _action("to21", "rf1", "tk1", "revoke", "t-u6")
    assert revoke.status_code == 200
    assert _get_refund("to21", "rf1")["amount_cents"] == 400
    # 重放撤销不加回第二次
    assert _action("to21", "rf1", "tk1", "revoke", "t-u6").status_code == 200
    assert _get_refund("to21", "rf1")["amount_cents"] == 400

