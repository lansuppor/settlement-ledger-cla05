import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))

import httpx
from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def make_order(oid: str, amount: int = 1000, tenant: str = "t1", paid: int | None = None) -> None:
    body = {"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    if paid:
        assert client.post(f"/orders/{oid}/payments", json={"amount_cents": paid},
                           headers={"X-Tenant": tenant}).status_code == 200


def refund_payload(rid: str, oid: str, amount: int, reason: str = "bad goods", tenant: str = "t1") -> dict:
    return {"tenant": tenant, "refund_id": rid, "order_id": oid,
            "amount_cents": amount, "reason": reason}


# ---------- 登记与读取 ----------

def test_register_then_read_is_pending() -> None:
    make_order("r1", 1000, paid=1000)
    r = client.post("/refunds", json=refund_payload("rf1", "r1", 300))
    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    got = client.get("/refunds/rf1", headers=H)
    assert got.status_code == 200
    assert got.json()["amount_cents"] == 300 and got.json()["status"] == "pending"
    # 待审核时尚未实际退款，订单金额不变。
    order = client.get("/orders/r1", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


def test_register_bad_params_are_400() -> None:
    make_order("r2", 1000, paid=1000)
    base = refund_payload("x", "r2", 100)
    for patch in [
        {"amount_cents": 0}, {"amount_cents": -5}, {"amount_cents": 1.5},
        {"amount_cents": "100"}, {"amount_cents": True}, {"amount_cents": None},
        {"reason": ""}, {"reason": None}, {"tenant": ""}, {"refund_id": ""}, {"order_id": ""},
    ]:
        body = {**base, **patch}
        resp = client.post("/refunds", json=body)
        assert resp.status_code == 400, (patch, resp.status_code, resp.text)
    assert client.post("/refunds", json={"tenant": "t1"}).status_code == 400
    assert client.post("/refunds", content=b"not-json", headers={"content-type": "application/json"}).status_code == 400


def test_register_unknown_or_cross_tenant_order_is_400() -> None:
    assert client.post("/refunds", json=refund_payload("rf-x", "nope", 10)).status_code == 400
    # 订单属于 t1，以 t2 身份登记按参数错误处理，不泄漏订单存在。
    resp = client.post("/refunds", json=refund_payload("rf-cross", "r1", 10, tenant="t2"))
    assert resp.status_code == 400


def test_register_exceeding_paid_is_409_and_no_half_document() -> None:
    make_order("r3", 500, paid=200)
    assert client.post("/refunds", json=refund_payload("rf3a", "r3", 300)).status_code == 409
    # 失败不留半张单据。
    conn = sqlite3.connect(db_path())
    count = conn.execute("SELECT COUNT(*) FROM refunds WHERE refund_id='rf3a'").fetchone()[0]
    conn.close()
    assert count == 0


def test_pending_amounts_reserve_budget() -> None:
    make_order("r4", 500, paid=500)
    assert client.post("/refunds", json=refund_payload("rf4a", "r4", 300)).status_code == 201
    # 待审核未释放额度，第二笔与待审核合计超出已收 → 409。
    assert client.post("/refunds", json=refund_payload("rf4b", "r4", 300)).status_code == 409
    assert client.post("/refunds/rf4a/review", json={"decision": "approve"}, headers=H).status_code == 200
    # 拒绝可释放占用额度。
    make_order("r4b", 500, paid=500)
    assert client.post("/refunds", json=refund_payload("rf4c", "r4b", 300)).status_code == 201
    assert client.post("/refunds", json=refund_payload("rf4d", "r4b", 300)).status_code == 409
    assert client.post("/refunds/rf4c/review", json={"decision": "reject"}, headers=H).status_code == 200
    assert client.post("/refunds", json=refund_payload("rf4d", "r4b", 300)).status_code == 201


def test_duplicate_registration_returns_existing_by_identity() -> None:
    make_order("r5", 1000, paid=1000)
    first = client.post("/refunds", json=refund_payload("rf5", "r5", 100, "first reason"))
    assert first.status_code == 201
    # 同业务身份不同请求指纹（金额/原因不同）：不新建，返回原单原内容。
    second = client.post("/refunds", json=refund_payload("rf5", "r5", 999, "other reason"))
    assert second.status_code == 200
    assert second.json()["amount_cents"] == 100 and second.json()["reason"] == "first reason"
    client.post("/refunds/rf5/review", json={"decision": "approve"}, headers=H)
    third = client.post("/refunds", json=refund_payload("rf5", "r5", 100, "first reason"))
    assert third.status_code == 200 and third.json()["status"] == "approved"
    conn = sqlite3.connect(db_path())
    count = conn.execute("SELECT COUNT(*) FROM refunds WHERE tenant='t1' AND refund_id='rf5'").fetchone()[0]
    conn.close()
    assert count == 1


# ---------- 审核 ----------

def test_approve_takes_effect_and_closes() -> None:
    make_order("r6", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rf6a", "r6", 300))
    client.post("/refunds", json=refund_payload("rf6b", "r6", 200))
    assert client.post("/refunds/rf6a/review", json={"decision": "approve"}, headers=H).status_code == 200
    order = client.get("/orders/r6", headers=H).json()
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    client.post("/refunds/rf6b/review", json={"decision": "approve"}, headers=H)
    order = client.get("/orders/r6", headers=H).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 500
    # 全额退款时已收可为 0。
    make_order("r6z", 100, paid=100)
    client.post("/refunds", json=refund_payload("rf6z", "r6z", 100))
    client.post("/refunds/rf6z/review", json={"decision": "approve"}, headers=H)
    order = client.get("/orders/r6z", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 100


def test_reject_has_no_effect_and_review_is_immutable() -> None:
    make_order("r7", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rf7", "r7", 300))
    assert client.post("/refunds/rf7/review", json={"decision": "reject"}, headers=H).json()["status"] == "rejected"
    # 后续审核不能覆盖原结果。
    again = client.post("/refunds/rf7/review", json={"decision": "approve"}, headers=H)
    assert again.status_code == 200 and again.json()["status"] == "rejected"
    order = client.get("/orders/r7", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


def test_review_requires_decision_and_header() -> None:
    make_order("r8", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rf8", "r8", 100))
    assert client.post("/refunds/rf8/review", json={"decision": "maybe"}, headers=H).status_code == 400
    assert client.post("/refunds/rf8/review", json={}).status_code == 400


# ---------- 冲正 ----------

def test_reverse_restores_order_amount() -> None:
    make_order("r9", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rf9a", "r9", 600))
    client.post("/refunds", json=refund_payload("rf9b", "r9", 300))
    client.post("/refunds/rf9a/review", json={"decision": "approve"}, headers=H)
    client.post("/refunds/rf9b/review", json={"decision": "approve"}, headers=H)
    order = client.get("/orders/r9", headers=H).json()
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 900
    assert client.post("/refunds/rf9a/reverse", headers=H).json()["status"] == "reversed"
    order = client.get("/orders/r9", headers=H).json()
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    # 冲正后不得再次审核。
    assert client.post("/refunds/rf9a/review", json={"decision": "approve"}, headers=H).status_code == 409


def test_reverse_non_effective_or_twice_is_409() -> None:
    make_order("r10", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rf10a", "r10", 100))
    assert client.post("/refunds/rf10a/reverse", headers=H).status_code == 409  # 待审核
    client.post("/refunds/rf10a/review", json={"decision": "reject"}, headers=H)
    assert client.post("/refunds/rf10a/reverse", headers=H).status_code == 409  # 已拒绝
    client.post("/refunds", json=refund_payload("rf10b", "r10", 100))
    client.post("/refunds/rf10b/review", json={"decision": "approve"}, headers=H)
    assert client.post("/refunds/rf10b/reverse", headers=H).status_code == 200
    assert client.post("/refunds/rf10b/reverse", headers=H).status_code == 409  # 已冲正再撤销


def test_reverse_blocked_when_refunded_budget_was_repaid() -> None:
    make_order("r11", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rf11", "r11", 200))
    client.post("/refunds/rf11/review", json={"decision": "approve"}, headers=H)
    # 商户已就退回的 200 额度重新收款，账务重新闭合，冲正会突破订单金额 → 409。
    assert client.post("/orders/r11/payments", json={"amount_cents": 200}, headers=H).status_code == 200
    assert client.post("/refunds/rf11/reverse", headers=H).status_code == 409
    order = client.get("/orders/r11", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


# ---------- 租户隔离 ----------

def test_cross_tenant_access_is_not_found() -> None:
    make_order("r12", 1000, paid=1000, tenant="t1")
    client.post("/refunds", json=refund_payload("rf12", "r12", 100, tenant="t1"))
    assert client.get("/refunds/rf12", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/refunds/rf12/review", json={"decision": "approve"},
                       headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/refunds/rf12/reverse", headers={"X-Tenant": "t2"}).status_code == 404
    # 跨租户操作未生效，本租户仍可正常审核。
    assert client.post("/refunds/rf12/review", json={"decision": "approve"}, headers=H).status_code == 200
    assert client.get("/refunds/rf12").status_code == 400  # 缺少租户头


def test_same_refund_id_in_different_tenants_are_distinct() -> None:
    make_order("r13a", 1000, paid=1000, tenant="t1")
    make_order("r13b", 1000, paid=1000, tenant="t2")
    r1 = client.post("/refunds", json=refund_payload("same", "r13a", 100, tenant="t1"))
    r2 = client.post("/refunds", json=refund_payload("same", "r13b", 100, tenant="t2"))
    assert r1.status_code == 201 and r2.status_code == 201
    assert client.get("/refunds/same", headers={"X-Tenant": "t1"}).json()["order_id"] == "r13a"
    assert client.get("/refunds/same", headers={"X-Tenant": "t2"}).json()["order_id"] == "r13b"


# ---------- 真实 HTTP 服务下的并发 ----------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(db_file: str, port: int) -> subprocess.Popen:
    env = {**os.environ, "APP_DB": db_file}
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.entry", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                return proc
        except httpx.TransportError:
            pass
    proc.kill()
    _, err = proc.communicate(timeout=5)
    raise RuntimeError(f"server did not start:\n{err.decode()[-2000:]}")


def _post(base: str, path: str, json_body: dict, headers: dict | None = None) -> httpx.Response:
    with httpx.Client(timeout=10) as c:
        return c.post(base + path, json=json_body, headers=headers or {})


def test_concurrent_register_and_review_single_effect() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "c1", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/c1/payments", {"amount_cents": 1000}, {"X-Tenant": "t1"})

        payload = refund_payload("crf1", "c1", 200)
        with ThreadPoolExecutor(max_workers=12) as ex:
            regs = list(ex.map(lambda _: _post(base, "/refunds", payload), range(12)))
        created = [r for r in regs if r.status_code == 201]
        reused = [r for r in regs if r.status_code == 200]
        assert len(created) == 1 and len(reused) == 11

        with ThreadPoolExecutor(max_workers=12) as ex:
            reviews = list(ex.map(
                lambda _: _post(base, "/refunds/crf1/review", {"decision": "approve"}, {"X-Tenant": "t1"}),
                range(12)))
        assert all(r.status_code == 200 and r.json()["status"] == "approved" for r in reviews)

        with httpx.Client(timeout=10) as c:
            order = c.get(base + "/orders/c1", headers={"X-Tenant": "t1"}).json()
        # 只有一笔退款、一次金额调整。
        assert order["paid_cents"] == 800 and order["outstanding_cents"] == 200
        conn = sqlite3.connect(db_file)
        assert conn.execute("SELECT COUNT(*) FROM refunds WHERE refund_id='crf1'").fetchone()[0] == 1
        conn.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_concurrent_interleaved_review_and_reverse_closes() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc2.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "c2", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/c2/payments", {"amount_cents": 1000}, {"X-Tenant": "t1"})
        _post(base, "/refunds", refund_payload("crf2", "c2", 250))

        def action(i: int) -> int:
            path = "/refunds/crf2/review" if i % 2 == 0 else "/refunds/crf2/reverse"
            body = {"decision": "approve"} if i % 2 == 0 else None
            with httpx.Client(timeout=10) as c:
                if body is None:
                    return c.post(base + path, headers={"X-Tenant": "t1"}).status_code
                return c.post(base + path, json=body, headers={"X-Tenant": "t1"}).status_code

        with ThreadPoolExecutor(max_workers=16) as ex:
            statuses = list(ex.map(action, range(32)))
        assert all(s in (200, 409) for s in statuses), statuses

        with httpx.Client(timeout=10) as c:
            refund = c.get(base + "/refunds/crf2", headers={"X-Tenant": "t1"}).json()
            order = c.get(base + "/orders/c2", headers={"X-Tenant": "t1"}).json()
        assert refund["status"] in ("approved", "reversed")
        expected_paid = 750 if refund["status"] == "approved" else 1000
        assert order["paid_cents"] == expected_paid
        # 全链路守恒：订单金额 = 已收 + 未收，且已收在 [0, 订单金额]。
        assert 0 <= order["paid_cents"] <= order["amount_cents"]
        assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------- 重启持久化 ----------

def test_state_persists_across_restart() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "persist.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "p1", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/p1/payments", {"amount_cents": 1000}, {"X-Tenant": "t1"})
        _post(base, "/refunds", refund_payload("prf1", "p1", 400))
        _post(base, "/refunds", refund_payload("prf2", "p1", 100))
        _post(base, "/refunds/prf1/review", {"decision": "approve"}, {"X-Tenant": "t1"})
        _post(base, "/refunds/prf2/review", {"decision": "reject"}, {"X-Tenant": "t1"})
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc = _start_server(db_file, port)
    try:
        with httpx.Client(timeout=10) as c:
            r1 = c.get(base + "/refunds/prf1", headers={"X-Tenant": "t1"}).json()
            r2 = c.get(base + "/refunds/prf2", headers={"X-Tenant": "t1"}).json()
            order = c.get(base + "/orders/p1", headers={"X-Tenant": "t1"}).json()
        assert r1["status"] == "approved" and r2["status"] == "rejected"
        assert order["paid_cents"] == 600 and order["outstanding_cents"] == 400
        # 重启后业务规则继续生效：冲正与重复审核结论一致。
        assert _post(base, "/refunds/prf1/reverse", None, {"X-Tenant": "t1"}).status_code == 200
        order = httpx.get(base + "/orders/p1", headers={"X-Tenant": "t1"}).json()
        assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # 再次重启，冲正结果仍在。
    proc = _start_server(db_file, port)
    try:
        r1 = httpx.get(base + "/refunds/prf1", headers={"X-Tenant": "t1"}).json()
        assert r1["status"] == "reversed"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
