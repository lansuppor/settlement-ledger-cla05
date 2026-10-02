import os
import tempfile
import threading

import pytest

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_refund_imports.sqlite"))

from fastapi.testclient import TestClient

import app.store.refund_imports as ri
from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

T = "it"
H = {"X-Tenant": T}
OTHER = {"X-Tenant": "it-other"}


def _paid_order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _import(request_id: str, lines: list, headers=H):
    return client.post("/refund-imports", json={"request_id": request_id, "lines": lines}, headers=headers)


def test_partial_success_with_distinct_reasons() -> None:
    _paid_order("io1", 1000, 1000)
    # 先占用 600，使可退余额只剩 400
    assert _import("pre-1", [
        {"order_id": "io1", "refund_id": "p1", "amount_cents": 600},
    ]).status_code == 200

    lines = [
        {"order_id": "io1", "refund_id": "r1", "amount_cents": 100},          # accepted
        {"order_id": "io1", "refund_id": "r2", "amount_cents": 0},            # invalid_amount
        {"order_id": "io1", "refund_id": "r3", "amount_cents": -5},           # invalid_amount
        {"order_id": "missing", "refund_id": "r4", "amount_cents": 10},       # order_not_found
        {"order_id": "io1", "refund_id": "p1", "amount_cents": 10},           # duplicate_refund
        {"order_id": "io1", "refund_id": "r5", "amount_cents": 300},          # accepted (余额恰 300)
        {"order_id": "io1", "refund_id": "r6", "amount_cents": 1},            # exceeds_refundable_balance
        {"order_id": "io1", "refund_id": "", "amount_cents": 1},              # invalid_line
        "not-an-object",                                                       # invalid_line
        {"order_id": "io1", "refund_id": "r7", "amount_cents": "10"},         # invalid_amount
    ]
    r = _import("imp-1", lines)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["total"] == 10
    assert body["accepted_count"] == 2
    assert body["rejected_count"] == 8
    by_pos = {row["position"]: row for row in body["rows"]}
    assert [row["position"] for row in body["rows"]] == list(range(10))  # 按提交顺序
    assert by_pos[0]["outcome"] == "accepted"
    assert by_pos[0]["refund"]["status"] == "pending"
    assert by_pos[5]["outcome"] == "accepted"
    assert by_pos[1]["error_code"] == "invalid_amount"
    assert by_pos[2]["error_code"] == "invalid_amount"
    assert by_pos[3]["error_code"] == "order_not_found"
    assert by_pos[4]["error_code"] == "duplicate_refund"
    assert by_pos[6]["error_code"] == "exceeds_refundable_balance"
    assert by_pos[7]["error_code"] == "invalid_line"
    assert by_pos[8]["error_code"] == "invalid_line"
    assert by_pos[9]["error_code"] == "invalid_amount"
    # rejected 行不区分拒绝类型都不产生退款单
    refunds = client.get("/orders/io1/refunds", headers=H).json()["refunds"]
    assert {x["refund_id"] for x in refunds} == {"p1", "r1", "r5"}
    # 占用守恒：待处理合计 600+100+300=1000，恰不超过已收 1000；失败行零写入
    conn = connect()
    pending_sum = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds"
        " WHERE tenant=? AND order_id='io1' AND status='pending'", (T,)).fetchone()["s"]
    conn.close()
    assert pending_sum == 1000


def test_accepted_rows_complete_like_single_accept() -> None:
    _paid_order("io2", 500, 500)
    r = _import("imp-2", [
        {"order_id": "io2", "refund_id": "a", "amount_cents": 200},
        {"order_id": "io2", "refund_id": "b", "amount_cents": 100},
    ])
    assert r.json()["accepted_count"] == 2
    # 导入生成的退款单可沿用现有读取入口
    got = client.get("/orders/io2/refunds/a", headers=H)
    assert got.status_code == 200 and got.json()["amount_cents"] == 200
    assert client.post("/orders/io2/refunds/a/complete", json={"request_id": "imp2-c1"},
                       headers=H).status_code == 200
    order = client.get("/orders/io2", headers=H).json()
    assert order["paid_cents"] == 300


def test_replay_returns_identical_result_without_double_accept() -> None:
    _paid_order("io3", 500, 500)
    lines = [
        {"order_id": "io3", "refund_id": "a", "amount_cents": 100},
        {"order_id": "io3", "refund_id": "b", "amount_cents": 999},
    ]
    first = _import("imp-3", lines)
    second = _import("imp-3", lines)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(client.get("/orders/io3/refunds", headers=H).json()["refunds"]) == 1
    # 重放与首次完全一致（含各行原因）
    assert [r["outcome"] for r in second.json()["rows"]] == ["accepted", "rejected"]
    assert second.json()["rows"][1]["error_code"] == "exceeds_refundable_balance"


def test_same_request_id_different_payload_conflicts() -> None:
    _paid_order("io4", 500, 500)
    lines1 = [{"order_id": "io4", "refund_id": "a", "amount_cents": 100}]
    lines2 = [{"order_id": "io4", "refund_id": "b", "amount_cents": 100}]
    assert _import("imp-4", lines1).status_code == 200
    r = _import("imp-4", lines2)
    assert r.status_code == 409
    # 冲突不写入第二行
    assert {x["refund_id"] for x in client.get("/orders/io4/refunds", headers=H).json()["refunds"]} == {"a"}


def test_request_id_namespace_shared_with_single_accept() -> None:
    _paid_order("io5", 500, 500)
    # 单笔先占用 request_id，批量再用 -> 409
    single = client.post("/orders/io5/refunds",
                         json={"refund_id": "s1", "amount_cents": 100, "request_id": "shared-1"},
                         headers=H)
    assert single.status_code == 201
    assert _import("shared-1", [{"order_id": "io5", "refund_id": "x", "amount_cents": 1}]).status_code == 409
    # 反向：批量先占用，单笔再用 -> 409
    assert _import("shared-2", [{"order_id": "io5", "refund_id": "y", "amount_cents": 1}]).status_code == 200
    reuse = client.post("/orders/io5/refunds",
                        json={"refund_id": "s2", "amount_cents": 100, "request_id": "shared-2"},
                        headers=H)
    assert reuse.status_code == 409


def test_resume_after_interruption_matches_uninterrupted_run() -> None:
    _paid_order("io6", 1000, 1000)
    lines = [
        {"order_id": "io6", "refund_id": "a", "amount_cents": 100},
        {"order_id": "io6", "refund_id": "b", "amount_cents": 200},
        {"order_id": "io6", "refund_id": "c", "amount_cents": 300},
        {"order_id": "io6", "refund_id": "d", "amount_cents": 500},  # 处理时仅剩 400 -> 超限
        {"order_id": "io6", "refund_id": "e", "amount_cents": 400},
    ]
    orig = ri._process_line

    def flaky(conn, tenant, request_id, position, line):
        if position == 2:  # 前两行（0、1）提交检查点后，第三行处理前中断
            raise RuntimeError("simulated crash")
        orig(conn, tenant, request_id, position, line)

    import app.store.refund_imports as mod
    mod._process_line = flaky
    try:
        with pytest.raises(RuntimeError):
            _import("imp-6", lines)
    finally:
        mod._process_line = orig

    # 中断态可按请求标识查询：只有前两行且顺序稳定
    mid = client.get("/refund-imports/imp-6", headers=H)
    assert mid.status_code == 200
    assert mid.json()["status"] == "in_progress"
    assert [r["position"] for r in mid.json()["rows"]] == [0, 1]

    # 同一请求标识续提：不重复受理已生效行，失败行原因与一次跑完一致
    resumed = _import("imp-6", lines)
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["status"] == "completed" and body["total"] == 5
    assert body["accepted_count"] == 4 and body["rejected_count"] == 1
    assert [r["outcome"] for r in body["rows"]] == [
        "accepted", "accepted", "accepted", "rejected", "accepted"]
    assert body["rows"][3]["error_code"] == "exceeds_refundable_balance"
    refunds = client.get("/orders/io6/refunds", headers=H).json()["refunds"]
    assert {x["refund_id"] for x in refunds} == {"a", "b", "c", "e"}
    # 再续跑（重放）结果完全一致
    assert _import("imp-6", lines).json() == body
    assert len(client.get("/orders/io6/refunds", headers=H).json()["refunds"]) == 4


def test_get_batch_not_found_and_tenant_isolation() -> None:
    assert client.get("/refund-imports/nope", headers=H).status_code == 404
    _paid_order("io7", 100, 100)
    _import("imp-7", [{"order_id": "io7", "refund_id": "a", "amount_cents": 10}])
    # 跨租户读取按不存在处理
    assert client.get("/refund-imports/imp-7", headers=OTHER).status_code == 404
    # 跨租户提交同标识互不可见：可独立建批；且其行订单按不存在拒绝
    other = _import("imp-7", [{"order_id": "io7", "refund_id": "a", "amount_cents": 10}], headers=OTHER)
    assert other.status_code == 200
    assert other.json()["rows"][0]["error_code"] == "order_not_found"
    # 本租户结果不受跨租户请求影响
    assert client.get("/refund-imports/imp-7", headers=H).json()["accepted_count"] == 1


def test_concurrent_batches_never_exceed_balance() -> None:
    _paid_order("io8", 1000, 1000)
    accepted: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        r = _import(f"imp-8-{i}", [{"order_id": "io8", "refund_id": f"c{i}", "amount_cents": 300}])
        if r.json()["rows"][0]["outcome"] == "accepted":
            with lock:
                accepted.append(f"c{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(accepted) == 3
    conn = connect()
    pending_sum = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds"
        " WHERE tenant=? AND order_id='io8' AND status='pending'", (T,)).fetchone()["s"]
    conn.close()
    assert pending_sum == 900  # 绝不超额占用


def test_concurrent_replays_identical() -> None:
    _paid_order("io9", 500, 500)
    lines = [{"order_id": "io9", "refund_id": "a", "amount_cents": 100}]
    results: list[dict] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        results.append(_import("imp-9", lines).json())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(b == results[0] for b in results)
    assert len(client.get("/orders/io9/refunds", headers=H).json()["refunds"]) == 1


def test_duplicate_line_within_batch_second_rejected() -> None:
    _paid_order("io10", 500, 500)
    body = _import("imp-10", [
        {"order_id": "io10", "refund_id": "dup", "amount_cents": 100},
        {"order_id": "io10", "refund_id": "dup", "amount_cents": 100},
    ]).json()
    assert [r["outcome"] for r in body["rows"]] == ["accepted", "rejected"]
    assert body["rows"][1]["error_code"] == "duplicate_refund"


def test_request_validation_and_tenant_header() -> None:
    assert client.post("/refund-imports", json={"request_id": "z", "lines": []}).status_code == 422
    assert client.post("/refund-imports", json={"lines": []}).status_code == 422
    assert client.post("/refund-imports", json={"request_id": "z", "lines": [
        {"order_id": "x", "refund_id": "y", "amount_cents": 1}]}).status_code == 400
