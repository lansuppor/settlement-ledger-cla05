import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_rollbacks.sqlite"))

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


def rollback_payload(rbid: str, oid: str, amount: int, tenant: str = "t1") -> dict:
    return {"tenant": tenant, "rollback_id": rbid, "order_id": oid, "amount_cents": amount}


def refund_payload(rid: str, oid: str, amount: int, reason: str = "bad goods", tenant: str = "t1") -> dict:
    return {"tenant": tenant, "refund_id": rid, "order_id": oid,
            "amount_cents": amount, "reason": reason}


# ---------- 基本生效与金额闭合 ----------

def test_rollback_reduces_paid_and_keeps_amount_closed() -> None:
    make_order("b1", 1000, paid=800)
    r = client.post("/payment-rollbacks", json=rollback_payload("rb1", "b1", 300))
    assert r.status_code == 201
    assert r.json()["amount_cents"] == 300 and r.json()["rollback_id"] == "rb1"
    got = client.get("/payment-rollbacks/rb1", headers=H)
    assert got.status_code == 200 and got.json()["order_id"] == "b1"
    order = client.get("/orders/b1", headers=H).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 500
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    # 回退腾出未收额度，可再次收款补齐。
    assert client.post("/orders/b1/payments", json={"amount_cents": 500}, headers=H).status_code == 200
    order = client.get("/orders/b1", headers=H).json()
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


def test_rollback_can_take_paid_to_zero() -> None:
    make_order("b1z", 200, paid=200)
    assert client.post("/payment-rollbacks", json=rollback_payload("rb1z", "b1z", 200)).status_code == 201
    order = client.get("/orders/b1z", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 200


def test_multiple_rollbacks_accumulate_within_paid() -> None:
    make_order("b2", 1000, paid=600)
    assert client.post("/payment-rollbacks", json=rollback_payload("rb2a", "b2", 200)).status_code == 201
    assert client.post("/payment-rollbacks", json=rollback_payload("rb2b", "b2", 300)).status_code == 201
    # 累计回退超过剩余已收 → 参数错误。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb2c", "b2", 200)).status_code == 400
    order = client.get("/orders/b2", headers=H).json()
    assert order["paid_cents"] == 100 and order["outstanding_cents"] == 900


# ---------- 参数错误 ----------

def test_rollback_bad_params_are_400() -> None:
    make_order("b3", 1000, paid=1000)
    base = rollback_payload("x", "b3", 100)
    for patch in [
        {"amount_cents": 0}, {"amount_cents": -5}, {"amount_cents": 1.5},
        {"amount_cents": "100"}, {"amount_cents": True}, {"amount_cents": None},
        {"tenant": ""}, {"rollback_id": ""}, {"order_id": ""},
    ]:
        resp = client.post("/payment-rollbacks", json={**base, **patch})
        assert resp.status_code == 400, (patch, resp.status_code, resp.text)
    assert client.post("/payment-rollbacks", json={"tenant": "t1"}).status_code == 400
    assert client.post("/payment-rollbacks", content=b"not-json",
                       headers={"content-type": "application/json"}).status_code == 400


def test_rollback_unknown_or_cross_tenant_order_is_400() -> None:
    assert client.post("/payment-rollbacks", json=rollback_payload("rb-x", "nope", 10)).status_code == 400
    make_order("b3c", 1000, paid=1000)
    resp = client.post("/payment-rollbacks", json=rollback_payload("rb-cross", "b3c", 10, tenant="t2"))
    assert resp.status_code == 400


def test_rollback_exceeding_current_paid_is_400_and_no_half_document() -> None:
    make_order("b4", 1000, paid=300)
    assert client.post("/payment-rollbacks", json=rollback_payload("rb4", "b4", 301)).status_code == 400
    conn = sqlite3.connect(db_path())
    count = conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE rollback_id='rb4'").fetchone()[0]
    paid = conn.execute("SELECT paid_cents FROM orders WHERE order_id='b4'").fetchone()[0]
    conn.close()
    assert count == 0 and paid == 300


# ---------- 与退款占用额度相容 ----------

def test_rollback_conflicts_with_pending_refund_reservation() -> None:
    make_order("b5", 1000, paid=600)
    client.post("/refunds", json=refund_payload("rbf5", "b5", 400))  # 待审核占用 400
    # 回退 300 后已收 300 < 占用 400 → 冲突，数据不变。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb5", "b5", 300)).status_code == 409
    order = client.get("/orders/b5", headers=H).json()
    assert order["paid_cents"] == 600
    conn = sqlite3.connect(db_path())
    assert conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE rollback_id='rb5'").fetchone()[0] == 0
    conn.close()
    # 回退到与占用齐平允许。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb5b", "b5", 200)).status_code == 201
    order = client.get("/orders/b5", headers=H).json()
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600


def test_rollback_conflicts_with_approved_refund() -> None:
    make_order("b6", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rbf6", "b6", 700))
    client.post("/refunds/rbf6/review", json={"decision": "approve"}, headers=H)
    order = client.get("/orders/b6", headers=H).json()
    assert order["paid_cents"] == 300
    # 净已收仅 300：回退 301 是参数错误（超过当前已收）；回退 300 后已收为 0 与占用齐平，允许。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb6a", "b6", 301)).status_code == 400
    assert client.post("/payment-rollbacks", json=rollback_payload("rb6b", "b6", 300)).status_code == 201
    order = client.get("/orders/b6", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000


def test_rejected_refund_releases_reservation_for_rollback() -> None:
    make_order("b7", 1000, paid=500)
    client.post("/refunds", json=refund_payload("rbf7a", "b7", 400))
    assert client.post("/payment-rollbacks", json=rollback_payload("rb7", "b7", 200)).status_code == 409
    client.post("/refunds/rbf7a/review", json={"decision": "reject"}, headers=H)
    assert client.post("/payment-rollbacks", json=rollback_payload("rb7", "b7", 200)).status_code == 201
    assert client.get("/orders/b7", headers=H).json()["paid_cents"] == 300


# ---------- 幂等：业务身份与请求指纹分离 ----------

def test_duplicate_rollback_returns_existing_by_identity() -> None:
    make_order("b8", 1000, paid=1000)
    first = client.post("/payment-rollbacks", json=rollback_payload("rb8", "b8", 100))
    assert first.status_code == 201
    # 同业务身份不同请求指纹（金额/订单不同）：不新建、不重复退回，返回既有结果。
    second = client.post("/payment-rollbacks",
                         json={"tenant": "t1", "rollback_id": "rb8", "order_id": "b8", "amount_cents": 900})
    assert second.status_code == 200
    assert second.json()["amount_cents"] == 100 and second.json()["order_id"] == "b8"
    order = client.get("/orders/b8", headers=H).json()
    assert order["paid_cents"] == 900
    conn = sqlite3.connect(db_path())
    count = conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE tenant='t1' AND rollback_id='rb8'").fetchone()[0]
    conn.close()
    assert count == 1


def test_failed_first_submission_can_be_retried_with_same_identity() -> None:
    make_order("b9", 1000, paid=100)
    # 首次金额非法失败，未占位；同身份以合法金额重试应生效。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb9", "b9", 500)).status_code == 400
    assert client.post("/payment-rollbacks", json=rollback_payload("rb9", "b9", 100)).status_code == 201


# ---------- 回退与冲正交错 ----------

def test_interleaved_rollback_review_reverse_closes() -> None:
    make_order("b10", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("rbf10", "b10", 400))
    client.post("/refunds/rbf10/review", json={"decision": "approve"}, headers=H)
    assert client.get("/orders/b10", headers=H).json()["paid_cents"] == 600
    # 剩余可回退额度 = 累计收款 1000 − 占用 400 = 600，回退 300 允许。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb10", "b10", 300)).status_code == 201
    order = client.get("/orders/b10", headers=H).json()
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 700
    # 再回退 300：累计收款降到 400，与占用 400 齐平，仍允许。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb10b", "b10", 300)).status_code == 201
    assert client.get("/orders/b10", headers=H).json()["paid_cents"] == 0
    # 已无在途收款：再回退任意金额均为参数错误。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb10c", "b10", 1)).status_code == 400
    # 冲正已生效退款后占用释放，金额加回订单，回退后仍可按原规则退款。
    assert client.post("/refunds/rbf10/reverse", headers=H).status_code == 200
    order = client.get("/orders/b10", headers=H).json()
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    # 登记一笔待审核退款（占用 200，尚未实际扣减）：净已收仍为 400。
    client.post("/refunds", json=refund_payload("rbf10b", "b10", 200))
    # 回退 201 未超过净已收 400（非参数错误），但会使累计收款 199 < 占用 200 → 409。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb10d", "b10", 201)).status_code == 409
    # 回退 200 后累计收款与占用齐平，允许；审核通过该退款后净已收归零，账务闭合。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb10e", "b10", 200)).status_code == 201
    assert client.post("/refunds/rbf10b/review", json={"decision": "approve"}, headers=H).status_code == 200
    order = client.get("/orders/b10", headers=H).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]


# ---------- 租户隔离 ----------

def test_cross_tenant_access_is_not_found() -> None:
    make_order("b11", 1000, paid=1000, tenant="t1")
    client.post("/payment-rollbacks", json=rollback_payload("rb11", "b11", 100, tenant="t1"))
    assert client.get("/payment-rollbacks/rb11", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/payment-rollbacks/rb11").status_code == 400  # 缺少租户头


def test_same_rollback_id_in_different_tenants_are_distinct() -> None:
    make_order("b12a", 1000, paid=1000, tenant="t1")
    make_order("b12b", 1000, paid=1000, tenant="t2")
    r1 = client.post("/payment-rollbacks", json=rollback_payload("same", "b12a", 100, tenant="t1"))
    r2 = client.post("/payment-rollbacks", json=rollback_payload("same", "b12b", 100, tenant="t2"))
    assert r1.status_code == 201 and r2.status_code == 201
    assert client.get("/payment-rollbacks/same", headers={"X-Tenant": "t1"}).json()["order_id"] == "b12a"
    assert client.get("/payment-rollbacks/same", headers={"X-Tenant": "t2"}).json()["order_id"] == "b12b"


# ---------- 真实 HTTP 服务下的并发与持久化 ----------

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


def test_concurrent_rollback_single_effect() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc_rb.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "c1", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/c1/payments", {"amount_cents": 1000}, {"X-Tenant": "t1"})
        payload = rollback_payload("crb1", "c1", 200)
        with ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(lambda _: _post(base, "/payment-rollbacks", payload), range(12)))
        created = [r for r in results if r.status_code == 201]
        reused = [r for r in results if r.status_code == 200]
        assert len(created) == 1 and len(reused) == 11
        with httpx.Client(timeout=10) as c:
            order = c.get(base + "/orders/c1", headers={"X-Tenant": "t1"}).json()
        assert order["paid_cents"] == 800 and order["outstanding_cents"] == 200
        conn = sqlite3.connect(db_file)
        assert conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE rollback_id='crb1'").fetchone()[0] == 1
        conn.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_concurrent_distinct_rollbacks_respect_budget() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc_rb2.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "c2", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/c2/payments", {"amount_cents": 500}, {"X-Tenant": "t1"})

        def action(i: int) -> int:
            return _post(base, "/payment-rollbacks", rollback_payload(f"crb{i}", "c2", 200)).status_code

        with ThreadPoolExecutor(max_workers=12) as ex:
            statuses = list(ex.map(action, range(8)))
        succeeded = [s for s in statuses if s == 201]
        assert len(succeeded) == 2  # 仅两笔合计 400 在已收 500 以内
        assert all(s in (201, 400) for s in statuses)
        order = httpx.get(base + "/orders/c2", headers={"X-Tenant": "t1"}).json()
        assert order["paid_cents"] == 100 and order["outstanding_cents"] == 900
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_rollback_persists_across_restart() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "persist_rb.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "p1", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/p1/payments", {"amount_cents": 900}, {"X-Tenant": "t1"})
        _post(base, "/refunds", refund_payload("prbf1", "p1", 300))
        _post(base, "/refunds/prbf1/review", {"decision": "approve"}, {"X-Tenant": "t1"})
        _post(base, "/payment-rollbacks", rollback_payload("prb1", "p1", 200))
        # 已收 = 900 − 300（退款）− 200（回退）= 400。
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc = _start_server(db_file, port)
    try:
        with httpx.Client(timeout=10) as c:
            rb = c.get(base + "/payment-rollbacks/prb1", headers={"X-Tenant": "t1"}).json()
            order = c.get(base + "/orders/p1", headers={"X-Tenant": "t1"}).json()
        assert rb["amount_cents"] == 200
        assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600
        # 重启后同身份重放仍不重复生效。
        again = _post(base, "/payment-rollbacks", rollback_payload("prb1", "p1", 200))
        assert again.status_code == 200
        order = httpx.get(base + "/orders/p1", headers={"X-Tenant": "t1"}).json()
        assert order["paid_cents"] == 400
    finally:
        proc.terminate()
        proc.wait(timeout=10)
