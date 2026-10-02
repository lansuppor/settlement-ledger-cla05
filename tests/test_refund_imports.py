import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_refund_imports.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store import refund_imports
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

T = "bi"
H = {"X-Tenant": T}
OTHER = {"X-Tenant": "bi-other"}


def _paid_order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    assert client.post("/orders", json={"tenant": T, "order_id": order_id,
                                        "amount_cents": amount, "currency": "CNY"}).status_code == 201
    if paid:
        assert client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid},
                           headers=H).status_code == 200


def _import(request_id: str, lines):
    return client.post("/refund-imports", json={"request_id": request_id, "lines": lines}, headers=H)


def _line(order_id, refund_id, amount):
    return {"order_id": order_id, "refund_id": refund_id, "amount_cents": amount}


def test_partial_success_line_by_line() -> None:
    _paid_order("bo1", 1000, 700)
    # 先经单笔受理占用一张，验证批内重复受理原因
    assert client.post("/orders/bo1/refunds",
                       json={"refund_id": "existed", "amount_cents": 100, "request_id": "bi-pre-1"},
                       headers=H).status_code == 201
    lines = [
        _line("bo1", "ok1", 200),          # 受理：占用后余 400
        _line("bo1", "badamt", 0),         # 金额非法
        _line("bo1", "badamt2", -7),       # 金额非法
        _line("bo1", "badamt3", "300"),    # 金额非法（字符串）
        _line("nope", "x", 10),            # 订单不存在
        _line("bo1", "existed", 100),      # 重复受理
        _line("bo1", "ok2", 300),          # 受理：累计占用 600
        _line("bo1", "over", 101),         # 超过可退余额（余 100）
        _line("bo1", "dup", 100),          # 行内重复的首行，受理（占用恰好占满 700）
        _line("bo1", "dup", 100),          # 行内重复的次行
    ]
    r = _import("bi-batch-1", lines)
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded_count"] == 3
    assert body["failed_count"] == 7
    conclusions = [(ln["conclusion"], ln.get("reason")) for ln in body["lines"]]
    assert conclusions == [
        ("accepted", None),
        ("rejected", "invalid_amount"),
        ("rejected", "invalid_amount"),
        ("rejected", "invalid_amount"),
        ("rejected", "order_not_found"),
        ("rejected", "duplicate_acceptance"),
        ("accepted", None),
        ("rejected", "exceeds_refundable_balance"),
        ("accepted", None),
        ("rejected", "duplicate_line"),
    ]
    # 行号与提交顺序稳定，关键字段齐全
    assert [ln["line_no"] for ln in body["lines"]] == list(range(1, 11))
    assert body["lines"][3]["amount_cents"] == "300"
    assert body["lines"][0]["amount_cents"] == 200
    for rejected in (ln for ln in body["lines"] if ln["conclusion"] == "rejected"):
        assert rejected["detail"]

    # 成功行真正落库（pending 占用可退余额），失败行无任何退款单
    listed = {x["refund_id"]: x for x in client.get("/orders/bo1/refunds", headers=H).json()["refunds"]}
    assert set(listed) == {"existed", "ok1", "ok2", "dup"}
    assert listed["ok1"]["status"] == "pending"
    # 余额已占满：单笔再受理 1 分也失败
    assert client.post("/orders/bo1/refunds",
                       json={"refund_id": "after", "amount_cents": 1, "request_id": "bi-after-1"},
                       headers=H).status_code == 409
    # 成功行可沿用单笔链路推进，真正扣减已收
    assert client.post("/orders/bo1/refunds/ok1/complete",
                       json={"request_id": "bi-ok1-done"}, headers=H).status_code == 200
    assert client.get("/orders/bo1", headers=H).json()["paid_cents"] == 500


def test_replay_returns_identical_result() -> None:
    _paid_order("bo2", 500, 500)
    lines = [_line("bo2", "r1", 400), _line("bo2", "r2", 100)]
    first = _import("bi-batch-2", lines)
    replay = _import("bi-batch-2", lines)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["succeeded_count"] == 2
    # 重放不重复受理：仅两张退款单
    assert len(client.get("/orders/bo2/refunds", headers=H).json()["refunds"]) == 2

    # 另一批：失败行原因也原样重放
    over_lines = [_line("bo2", "o1", 1)]
    r = _import("bi-batch-2b", over_lines)
    assert r.json()["lines"][0]["reason"] == "exceeds_refundable_balance"
    assert _import("bi-batch-2b", over_lines).json() == r.json()


def test_same_request_id_different_batch_conflicts() -> None:
    _paid_order("bo3", 900, 900)
    assert _import("bi-batch-3", [_line("bo3", "r1", 100)]).status_code == 200
    # 同一标识改作其他批次内容 -> 409，且不写入新行
    r = _import("bi-batch-3", [_line("bo3", "r1", 100), _line("bo3", "r2", 50)])
    assert r.status_code == 409
    assert client.get("/refund-imports/bi-batch-3", headers=H).json()["lines"][0]["refund_id"] == "r1"
    assert len(client.get("/refund-imports/bi-batch-3", headers=H).json()["lines"]) == 1


def test_import_result_query_and_tenant_isolation() -> None:
    _paid_order("bo4", 300, 300)
    lines = [_line("bo4", "r1", 100), _line("ghost", "r2", 10)]
    assert _import("bi-batch-4", lines).status_code == 200
    got = client.get("/refund-imports/bi-batch-4", headers=H)
    assert got.status_code == 200
    assert [ln["order_id"] for ln in got.json()["lines"]] == ["bo4", "ghost"]
    # 跨租户查询按不存在处理
    assert client.get("/refund-imports/bi-batch-4", headers=OTHER).status_code == 404
    # 导入的退款单沿用既有读取入口，跨租户 404
    assert client.get("/orders/bo4/refunds/r1", headers=H).status_code == 200
    assert client.get("/orders/bo4/refunds/r1", headers=OTHER).status_code == 404
    # 未知批次标识 404
    assert client.get("/refund-imports/missing", headers=H).status_code == 404
    # 缺租户头 400
    assert client.get("/refund-imports/bi-batch-4").status_code == 400


def test_resume_after_breakpoint_matches_uninterrupted() -> None:
    _paid_order("bo5", 500, 500)
    lines = [
        _line("bo5", "r1", 200),    # 已生效：续跑不得重复受理
        _line("gone", "r2", 50),    # 已判失败：原因冻结
        _line("bo5", "r3", 300),    # 续跑新判：占用合计 500，恰好成功
        _line("bo5", "r4", 1),      # 续跑新判：超额失败
    ]
    # 直接构造「已判定前两行后中断」的落库状态：第 1 行已受理（退款单也已落库），第 2 行判订单不存在
    raw = connect()
    try:
        raw.execute("BEGIN IMMEDIATE")
        raw.execute(
            "INSERT INTO refund_imports(tenant, request_id, fingerprint, succeeded_count, failed_count,"
            " response_json, completed, created_at) VALUES(?,?,?,0,0,'',0,CURRENT_TIMESTAMP)",
            (T, "bi-batch-5", refund_imports._fingerprint(lines)),
        )
        raw.execute(
            "INSERT INTO refunds(tenant, order_id, refund_id, amount_cents, status,"
            " effective_deduction_cents, created_at, updated_at)"
            " VALUES(?,?,?,?,'pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (T, "bo5", "r1", 200),
        )
        raw.execute(
            "INSERT INTO refund_import_rows(tenant, request_id, line_no, order_id, refund_id, amount_raw,"
            " conclusion, reason, created_at) VALUES(?,?,1,'bo5','r1','200','accepted',NULL,CURRENT_TIMESTAMP)",
            (T, "bi-batch-5"),
        )
        raw.execute(
            "INSERT INTO refund_import_rows(tenant, request_id, line_no, order_id, refund_id, amount_raw,"
            " conclusion, reason, created_at) VALUES(?,?,2,'gone','r2','50','rejected','order_not_found',"
            "CURRENT_TIMESTAMP)",
            (T, "bi-batch-5"),
        )
        raw.execute("COMMIT")
    finally:
        raw.close()

    r = _import("bi-batch-5", lines)
    assert r.status_code == 200
    body = r.json()
    assert [(ln["line_no"], ln["conclusion"], ln.get("reason")) for ln in body["lines"]] == [
        (1, "accepted", None),
        (2, "rejected", "order_not_found"),
        (3, "accepted", None),
        (4, "rejected", "exceeds_refundable_balance"),
    ]
    assert (body["succeeded_count"], body["failed_count"]) == (2, 2)
    # r1 只有一张，未重复受理；余额守恒（占用 500）
    listed = client.get("/orders/bo5/refunds", headers=H).json()["refunds"]
    assert [x["refund_id"] for x in listed] == ["r1", "r3"]
    assert client.post("/orders/bo5/refunds", json={"refund_id": "z", "amount_cents": 1,
                                                     "request_id": "bi-r-after"}, headers=H).status_code == 409
    # 再次完整重放，结果与续跑后完全一致
    assert _import("bi-batch-5", lines).json() == body


def test_concurrent_batches_never_exceed_balance() -> None:
    _paid_order("bo6", 1000, 1000)
    outcomes: list[tuple[str, str]] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        r = _import(f"bi-c{i}", [_line("bo6", f"cr{i}", 300)])
        conclusion = r.json()["lines"][0]["conclusion"]
        with lock:
            outcomes.append((f"cr{i}", conclusion))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = [name for name, c in outcomes if c == "accepted"]
    rejected = [name for name, c in outcomes if c == "rejected"]
    assert len(accepted) == 3 and len(rejected) == 5
    assert all(client.get(f"/orders/bo6/refunds/{name}", headers=H).status_code == 200 for name in accepted)
    assert all(client.get(f"/orders/bo6/refunds/{name}", headers=H).status_code == 404 for name in rejected)


def test_concurrent_same_request_id_single_effect() -> None:
    _paid_order("bo7", 800, 800)
    lines = [_line("bo7", "same1", 600)]
    bodies: list[dict] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        r = _import("bi-batch-7", lines)
        with lock:
            bodies.append(r.json())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bodies[0] == bodies[1]
    assert len(client.get("/orders/bo7/refunds", headers=H).json()["refunds"]) == 1


def test_empty_batch_rejected() -> None:
    r = client.post("/refund-imports", json={"request_id": "bi-empty", "lines": []}, headers=H)
    assert r.status_code == 422


def test_malformed_line_fields_are_rejected_per_line() -> None:
    _paid_order("bo8", 300, 300)
    r = _import("bi-batch-8", [
        {"refund_id": "noorder", "amount_cents": 10},
        {"order_id": "bo8", "amount_cents": 10},
        {"order_id": "bo8", "refund_id": "ok1", "amount_cents": 10},
    ])
    assert r.status_code == 200
    reasons = [ln.get("reason") for ln in r.json()["lines"]]
    assert reasons == ["invalid_line", "invalid_line", None]
