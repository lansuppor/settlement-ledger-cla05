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


def get_order(oid: str, tenant: str = "t1") -> dict:
    return client.get(f"/orders/{oid}", headers={"X-Tenant": tenant}).json()


# ---------- 基本回退与读取 ----------

def test_rollback_reduces_paid_and_recomputes_outstanding() -> None:
    make_order("b1", 1000, paid=1000)
    r = client.post("/payment-rollbacks", json=rollback_payload("rb1", "b1", 300))
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "completed" and r.json()["amount_cents"] == 300
    order = get_order("b1")
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    got = client.get("/payment-rollbacks/rb1", headers=H)
    assert got.status_code == 200 and got.json()["rollback_id"] == "rb1"
    # 回退腾出未收额度后可再次收款补回。
    assert client.post("/orders/b1/payments", json={"amount_cents": 300}, headers=H).status_code == 200
    order = get_order("b1")
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0


def test_rollback_full_amount_can_zero_paid() -> None:
    make_order("b1z", 500, paid=500)
    assert client.post("/payment-rollbacks", json=rollback_payload("rb1z", "b1z", 500)).status_code == 201
    order = get_order("b1z")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 500
    assert 0 <= order["paid_cents"] <= order["amount_cents"]


# ---------- 参数错误 ----------

def test_bad_params_are_400() -> None:
    make_order("b2", 1000, paid=1000)
    base = rollback_payload("x", "b2", 100)
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


def test_unknown_or_cross_tenant_order_is_400() -> None:
    assert client.post("/payment-rollbacks", json=rollback_payload("rb-x", "nope", 10)).status_code == 400
    make_order("b2c", 1000, paid=1000, tenant="t1")
    # 订单属于 t1，以 t2 身份回退按参数错误处理，不泄漏订单存在。
    resp = client.post("/payment-rollbacks", json=rollback_payload("rb-cross", "b2c", 10, tenant="t2"))
    assert resp.status_code == 400


def test_amount_exceeding_paid_is_400_and_no_half_document() -> None:
    make_order("b3", 500, paid=200)
    assert client.post("/payment-rollbacks", json=rollback_payload("rb3", "b3", 201)).status_code == 400
    # 无退款时已收即净已收；回退 300 远超已收 200。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb3b", "b3", 300)).status_code == 400
    order = get_order("b3")
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 300
    conn = sqlite3.connect(db_path())
    n = conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE rollback_id IN ('rb3','rb3b')").fetchone()[0]
    conn.close()
    assert n == 0


# ---------- 与退款占用额度相容 ----------

def test_pending_refund_reservation_blocks_rollback_with_409() -> None:
    make_order("b4", 1000, paid=1000)
    # 待审核退款 500 占用额度；净已收仍为 1000，故先通过“超过已收”校验，再触发占用冲突。
    client.post("/refunds", json=refund_payload("krb-rf4", "b4", 500))
    assert client.post("/payment-rollbacks", json=rollback_payload("rb4", "b4", 600)).status_code == 409
    order = get_order("b4")
    assert order["paid_cents"] == 1000  # 冲突不改变任何数据
    conn = sqlite3.connect(db_path())
    assert conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE rollback_id='rb4'").fetchone()[0] == 0
    conn.close()
    # 回退后已收恰好等于占用额度是允许的边界：1000 - 500 = 500。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb4ok", "b4", 500)).status_code == 201
    order = get_order("b4")
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 500


def test_approved_refund_lowers_net_paid_basis() -> None:
    make_order("b5", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("krb-rf5", "b5", 600))
    client.post("/refunds/krb-rf5/review", json={"decision": "approve"}, headers=H)
    # 已生效退款 600：累计收款毛额仍为 1000，对外净已收 = 1000 - 600 = 400。
    # 回退 500 超过当前已收（净 400）→ 参数错误。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb5big", "b5", 500)).status_code == 400
    # 无待审核占用时占用上限与净已收同界：回退 200 后毛额 800 仍覆盖已生效退款 600，允许。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb5a", "b5", 200)).status_code == 201
    order = get_order("b5")
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 800
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    # 继续回退到净已收为 0：毛额 600 恰等于已生效退款占用 600，为可回退边界。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb5b", "b5", 200)).status_code == 201
    order = get_order("b5")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000
    # 已无剩余已收可回退。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb5c", "b5", 1)).status_code == 400


def test_rollback_then_refunds_review_and_reverse_close() -> None:
    make_order("b6", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("krb-rf6", "b6", 300))
    # 待审核占用 300：最多回退 700（回退后净已收 = 300）。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb6", "b6", 400)).status_code == 201
    order = get_order("b6")
    assert order["paid_cents"] == 600 and order["outstanding_cents"] == 400
    # 回退后退款仍可按原规则审核生效。
    assert client.post("/refunds/krb-rf6/review", json={"decision": "approve"}, headers=H).status_code == 200
    order = get_order("b6")
    assert order["paid_cents"] == 300 and order["outstanding_cents"] == 700
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    # 冲正把退款全额加回，仍闭合。
    assert client.post("/refunds/krb-rf6/reverse", headers=H).status_code == 200
    order = get_order("b6")
    assert order["paid_cents"] == 600 and order["outstanding_cents"] == 400
    assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]


def test_reject_releases_reservation_and_allows_more_rollback() -> None:
    make_order("b7", 1000, paid=1000)
    client.post("/refunds", json=refund_payload("krb-rf7", "b7", 800))
    # 占用 800：回退 300 使净已收降到 700 < 800 → 409。
    assert client.post("/payment-rollbacks", json=rollback_payload("rb7", "b7", 300)).status_code == 409
    # 拒绝释放占用后可回退。
    client.post("/refunds/krb-rf7/review", json={"decision": "reject"}, headers=H)
    assert client.post("/payment-rollbacks", json=rollback_payload("rb7", "b7", 300)).status_code == 201
    assert get_order("b7")["paid_cents"] == 700


# ---------- 幂等：业务身份与请求指纹分离 ----------

def test_duplicate_rollback_returns_existing_by_identity() -> None:
    make_order("b8", 1000, paid=1000)
    first = client.post("/payment-rollbacks", json=rollback_payload("rb8", "b8", 100))
    assert first.status_code == 201
    # 同业务身份携带不同金额/订单指纹：不新建、不重复退回，返回既有回退结果。
    second = client.post("/payment-rollbacks",
                         json={"tenant": "t1", "rollback_id": "rb8", "order_id": "b8", "amount_cents": 999})
    assert second.status_code == 200
    assert second.json()["amount_cents"] == 100
    order = get_order("b8")
    assert order["paid_cents"] == 900  # 只回退一次
    conn = sqlite3.connect(db_path())
    n = conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE tenant='t1' AND rollback_id='rb8'").fetchone()[0]
    conn.close()
    assert n == 1


# ---------- 租户隔离 ----------

def test_cross_tenant_read_is_not_found() -> None:
    make_order("b9", 1000, paid=1000)
    client.post("/payment-rollbacks", json=rollback_payload("rb9", "b9", 100))
    assert client.get("/payment-rollbacks/rb9", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/payment-rollbacks/rb9").status_code == 400  # 缺租户头


def test_same_rollback_id_in_different_tenants_are_distinct() -> None:
    make_order("b10a", 1000, paid=1000, tenant="t1")
    make_order("b10b", 1000, paid=1000, tenant="t2")
    r1 = client.post("/payment-rollbacks", json=rollback_payload("same", "b10a", 100, tenant="t1"))
    r2 = client.post("/payment-rollbacks", json=rollback_payload("same", "b10b", 100, tenant="t2"))
    assert r1.status_code == 201 and r2.status_code == 201
    assert client.get("/payment-rollbacks/same", headers={"X-Tenant": "t1"}).json()["order_id"] == "b10a"
    assert client.get("/payment-rollbacks/same", headers={"X-Tenant": "t2"}).json()["order_id"] == "b10b"


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
            resps = list(ex.map(lambda _: _post(base, "/payment-rollbacks", payload), range(12)))
        created = [r for r in resps if r.status_code == 201]
        reused = [r for r in resps if r.status_code == 200]
        assert len(created) == 1 and len(reused) == 11
        order = httpx.get(base + "/orders/c1", headers={"X-Tenant": "t1"}).json()
        # 只有一张回退单、一次金额调整。
        assert order["paid_cents"] == 800 and order["outstanding_cents"] == 200
        conn = sqlite3.connect(db_file)
        assert conn.execute("SELECT COUNT(*) FROM payment_rollbacks WHERE rollback_id='crb1'").fetchone()[0] == 1
        conn.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_concurrent_interleaved_rollback_and_reverse_closes() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc_rb2.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "c2", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/c2/payments", {"amount_cents": 1000}, {"X-Tenant": "t1"})
        _post(base, "/refunds", refund_payload("crf2", "c2", 300))
        _post(base, "/refunds/crf2/review", {"decision": "approve"}, {"X-Tenant": "t1"})  # 净已收 700

        def action(i: int) -> int:
            with httpx.Client(timeout=10) as c:
                if i % 2 == 0:
                    # 占用 300，回退后净已收不得低于 300，故每笔 100、至多 4 笔成功。
                    body = rollback_payload(f"crb-{i}", "c2", 100)
                    return c.post(base + "/payment-rollbacks", json=body).status_code
                return c.post(base + "/refunds/crf2/reverse", headers={"X-Tenant": "t1"}).status_code

        with ThreadPoolExecutor(max_workers=16) as ex:
            statuses = list(ex.map(action, range(32)))
        # 成功（201/200）与无额度可回的参数错误/冲突（400/409）都可能出现，关键是无 500。
        assert all(s in (200, 201, 400, 409) for s in statuses), statuses

        order = httpx.get(base + "/orders/c2", headers={"X-Tenant": "t1"}).json()
        assert 0 <= order["paid_cents"] <= order["amount_cents"]
        assert order["paid_cents"] + order["outstanding_cents"] == order["amount_cents"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------- 重启持久化 ----------

def test_rollback_persists_across_restart() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "persist_rb.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        _post(base, "/orders", {"tenant": "t1", "order_id": "p1", "amount_cents": 1000, "currency": "CNY"})
        _post(base, "/orders/p1/payments", {"amount_cents": 1000}, {"X-Tenant": "t1"})
        _post(base, "/refunds", refund_payload("prf1", "p1", 300))
        _post(base, "/payment-rollbacks", rollback_payload("prb1", "p1", 200))
        _post(base, "/refunds/prf1/review", {"decision": "approve"}, {"X-Tenant": "t1"})
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc = _start_server(db_file, port)
    try:
        with httpx.Client(timeout=10) as c:
            rb = c.get(base + "/payment-rollbacks/prb1", headers={"X-Tenant": "t1"}).json()
            order = c.get(base + "/orders/p1", headers={"X-Tenant": "t1"}).json()
        assert rb["status"] == "completed" and rb["amount_cents"] == 200
        # 毛额 800（1000-200），净已收 = 800 - 300(已生效退款) = 500。
        assert order["paid_cents"] == 500 and order["outstanding_cents"] == 500
        # 重放同一回退身份仍只返回既有结果。
        again = _post(base, "/payment-rollbacks", rollback_payload("prb1", "p1", 200))
        assert again.status_code == 200 and again.json()["amount_cents"] == 200
        order = httpx.get(base + "/orders/p1", headers={"X-Tenant": "t1"}).json()
        assert order["paid_cents"] == 500
    finally:
        proc.terminate()
        proc.wait(timeout=10)
