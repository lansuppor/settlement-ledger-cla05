import os
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_recon.sqlite"))

import httpx
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}


def make_order(oid: str, amount: int = 1000, tenant: str = "t1") -> None:
    body = {"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def pay(oid: str, amount: int, tenant: str = "t1"):
    return client.post(f"/orders/{oid}/payments", json={"amount_cents": amount},
                       headers={"X-Tenant": tenant})


def refund_payload(rid: str, oid: str, amount: int, tenant: str = "t1") -> dict:
    return {"tenant": tenant, "refund_id": rid, "order_id": oid,
            "amount_cents": amount, "reason": "bad goods"}


def rollback_payload(rbid: str, oid: str, amount: int, tenant: str = "t1") -> dict:
    return {"tenant": tenant, "rollback_id": rbid, "order_id": oid, "amount_cents": amount}


def reconcile(rid: str, scope: dict | None = None, tenant: str = "t1"):
    body = {"tenant": tenant, "reconcile_id": rid}
    if scope is not None:
        body["scope"] = scope
    return client.post("/reconciliations", json=body)


def get_recon(rid: str, tenant: str = "t1"):
    return client.get(f"/reconciliations/{rid}", headers={"X-Tenant": tenant})


# ---------- 已核销：逐条流水衔接、末条余额等于当前已收、金额闭合 ----------

def test_clean_orders_are_reconciled_with_full_entry_chain() -> None:
    make_order("rc1", 1000)
    pay("rc1", 600)
    client.post("/refunds", json=refund_payload("rrf1", "rc1", 300))
    client.post("/refunds/rrf1/review", json={"decision": "approve"}, headers=H)
    client.post("/refunds/rrf1/reverse", headers=H)
    client.post("/payment-rollbacks", json=rollback_payload("rrb1", "rc1", 200))

    resp = reconcile("rec-1", {"order_id": "rc1"})
    assert resp.status_code == 201
    stmt = resp.json()
    assert stmt["status"] == "reconciled"
    assert stmt["total_orders"] == 1 and stmt["difference_count"] == 0
    assert stmt["differences"] == []
    assert stmt["reconcile_id"] == "rec-1" and stmt["tenant"] == "t1"
    assert stmt["checked_at"]

    order = stmt["orders"][0]
    assert order["order_id"] == "rc1"
    assert order["amount_cents"] == 1000
    assert order["paid_cents"] == 400          # 600 - 200 回退；退款已冲正不扣减
    assert order["outstanding_cents"] == 600
    assert order["last_balance_cents"] == 400
    assert order["chain_intact"] is True
    assert order["final_balance_matches"] is True
    assert order["amount_closed"] is True
    assert order["difference_reasons"] == []

    types = [e["action_type"] for e in order["entries"]]
    assert types == [
        "payment_received", "refund_registered", "refund_approved",
        "refund_reversed", "payment_rolled_back",
    ]
    # 逐条应有余额从 0 累计，每条持久化余额都与应有余额衔接。
    running = 0
    for e in order["entries"]:
        running += e["change_cents"]
        assert e["expected_balance_cents"] == running
        assert e["balance_cents"] == running
        assert e["chained"] is True
    assert order["entries"][-1]["expected_balance_cents"] == order["last_balance_cents"] == 400


def test_order_without_entries_is_reconciled_from_zero_balance() -> None:
    make_order("rc1z", 800)
    stmt = reconcile("rec-1z", {"order_id": "rc1z"}).json()
    assert stmt["status"] == "reconciled"
    order = stmt["orders"][0]
    assert order["entries"] == []
    assert order["last_balance_cents"] == 0
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 800
    assert order["chain_intact"] and order["final_balance_matches"] and order["amount_closed"]


# ---------- 有差异：余额断链 / 末条余额与当前已收不一致 ----------

def _tamper(sql: str, params: tuple = ()) -> None:
    # 直接改库模拟底层账务不一致；正常业务路径不会产生此类状态。
    conn = connect()
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


def test_broken_balance_chain_is_reported_as_difference() -> None:
    make_order("rc2", 1000)
    pay("rc2", 300)
    pay("rc2", 200)
    # 篡改第一条流水的持久化余额：衔接断链；末条余额仍等于当前已收。
    first_seq = client.get("/orders/rc2/account-entries", headers=H,
                           params={"page_size": 10}).json()["entries"][0]["seq"]
    _tamper("UPDATE order_account_entries SET balance_cents=balance_cents+7 WHERE seq=?", (first_seq,))

    stmt = reconcile("rec-2", {"order_id": "rc2"}).json()
    assert stmt["status"] == "difference_found"
    assert stmt["total_orders"] == 1 and stmt["difference_count"] == 1
    assert stmt["differences"] == [
        {"order_id": "rc2", "reasons": ["balance_chain_broken"]}
    ]
    order = stmt["orders"][0]
    assert order["chain_intact"] is False
    assert order["final_balance_matches"] is True
    assert order["entries"][0]["chained"] is False
    assert order["entries"][0]["expected_balance_cents"] == 300
    assert order["entries"][0]["balance_cents"] == 307
    assert all(e["chained"] for e in order["entries"][1:])


def test_final_balance_mismatch_is_reported_as_difference() -> None:
    make_order("rc3", 1000)
    pay("rc3", 500)
    # 直接抬高订单累计收款毛额而不补流水：流水末条余额与当前对外已收不再一致。
    _tamper("UPDATE orders SET paid_cents=paid_cents+100 WHERE order_id='rc3' AND tenant='t1'")

    stmt = reconcile("rec-3", {"order_id": "rc3"}).json()
    assert stmt["status"] == "difference_found"
    assert stmt["differences"] == [
        {"order_id": "rc3", "reasons": ["final_balance_mismatch"]}
    ]
    order = stmt["orders"][0]
    assert order["chain_intact"] is True
    assert order["final_balance_matches"] is False
    assert order["last_balance_cents"] == 500 and order["paid_cents"] == 600


def test_difference_list_covers_every_divergent_order() -> None:
    t = "trd"
    make_order("rc4a", 1000, tenant=t)
    make_order("rc4b", 1000, tenant=t)
    make_order("rc4c", 1000, tenant=t)
    pay("rc4a", 100, tenant=t)
    pay("rc4b", 100, tenant=t)
    pay("rc4c", 100, tenant=t)
    _tamper("UPDATE orders SET paid_cents=paid_cents+10 WHERE order_id='rc4b' AND tenant=?", (t,))
    _tamper("UPDATE orders SET paid_cents=paid_cents+20 WHERE order_id='rc4c' AND tenant=?", (t,))

    # 三张订单未收分别为 900/890/880，区间取交集只命中这三张。
    stmt = reconcile("rec-4b", {"outstanding_min": 850, "outstanding_max": 950}, tenant=t).json()
    assert stmt["total_orders"] == 3
    assert stmt["difference_count"] == 2
    diff_orders = {d["order_id"] for d in stmt["differences"]}
    assert diff_orders == {"rc4b", "rc4c"}
    assert all(d["reasons"] == ["final_balance_mismatch"] for d in stmt["differences"])


# ---------- 范围：单张订单 / 金额区间 / 未收区间 / 交集 / 排序 ----------

def test_scope_filters_and_intersects() -> None:
    t = "trs"
    make_order("s1", 1000, tenant=t)
    make_order("s2", 500, tenant=t)
    pay("s1", 600, tenant=t)  # 未收 400
    # s2 未收 500

    stmt = reconcile("rec-s1", {"order_id": "s1"}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s1"]

    stmt = reconcile("rec-s2", {"amount_min": 900}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s1"]

    stmt = reconcile("rec-s3", {"amount_max": 999}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s2"]

    stmt = reconcile("rec-s4", {"outstanding_min": 450}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s2"]

    stmt = reconcile("rec-s5", {"outstanding_max": 400}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s1"]

    # 交集：金额 ≥ 900 且未收 ≥ 300 → 仅 s1。
    stmt = reconcile("rec-s6", {"amount_min": 900, "outstanding_min": 300}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s1"]

    # 含边界：amount_min=1000 命中 s1；outstanding 400..500 同时命中 s1 与 s2（按标识升序）。
    stmt = reconcile("rec-s7", {"amount_min": 1000}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s1"]
    stmt = reconcile("rec-s8", {"outstanding_min": 400, "outstanding_max": 500}, tenant=t).json()
    assert [o["order_id"] for o in stmt["orders"]] == ["s1", "s2"]


def test_invalid_or_empty_scope_returns_400_and_leaves_nothing() -> None:
    make_order("ok1", 1000)

    for bad_scope in (
        {"amount_min": -1},
        {"amount_max": -1},
        {"outstanding_min": -5},
        {"amount_min": "100"},
        {"amount_min": 1.5},
        {"amount_min": True},
        {"amount_min": 100, "amount_max": 50},
        {"outstanding_min": 100, "outstanding_max": 50},
        {"order_id": ""},
    ):
        resp = reconcile(f"bad-{bad_scope}", bad_scope)
        assert resp.status_code == 400, bad_scope
        assert get_recon(f"bad-{bad_scope}").status_code == 404  # 失败不留半张对账单

    # scope 类型错误。
    resp = client.post("/reconciliations", json={"tenant": "t1", "reconcile_id": "badx", "scope": []})
    assert resp.status_code == 400

    # 范围合法但为空：区间筛不到订单 → 400，且不留单。
    assert reconcile("empty-1", {"amount_min": 999999}).status_code == 400
    assert get_recon("empty-1").status_code == 404
    # 指定单张不存在/非本租户订单同样 400，不泄漏订单是否存在。
    assert reconcile("empty-2", {"order_id": "no-such-order"}).status_code == 400
    assert get_recon("empty-2").status_code == 404
    # 租户下没有任何订单且不给范围：空范围 400。
    assert reconcile("empty-3", tenant="t-void").status_code == 400

    # 缺 tenant / reconcile_id。
    assert client.post("/reconciliations", json={"reconcile_id": "x"}).status_code == 400
    assert client.post("/reconciliations", json={"tenant": "t1"}).status_code == 400


# ---------- 不可变更：重复提交回放既有结论，与范围指纹无关 ----------

def test_statement_is_immutable_and_idempotent_by_business_identity() -> None:
    make_order("fr1", 1000)
    pay("fr1", 200)
    first = reconcile("freeze-1", {"order_id": "fr1"})
    assert first.status_code == 201
    before = first.json()

    # 对账单生成后订单继续发生账务变动。
    pay("fr1", 300)
    make_order("fr2", 1000)

    # 同一（租户, 对账标识）用不同范围指纹重提：200 + 既有结论，不重新核对。
    again = reconcile("freeze-1", {"order_id": "fr2"})
    assert again.status_code == 200
    assert again.json() == before
    third = reconcile("freeze-1", {"amount_min": 1, "amount_max": 999999})
    assert third.status_code == 200 and third.json() == before

    # 按标识读取得到同一份固化结论；重启前/后口径不变由持久化用例保证。
    read = get_recon("freeze-1")
    assert read.status_code == 200 and read.json() == before
    # 固化时 paid=200，不受后来 300 收款影响。
    assert before["orders"][0]["paid_cents"] == 200
    assert before["orders"][0]["last_balance_cents"] == 200


# ---------- 读取：404 与租户隔离 ----------

def test_read_404_and_tenant_isolation() -> None:
    assert get_recon("missing-id").status_code == 404
    # 缺租户头 400。
    assert client.get("/reconciliations/x").status_code == 400

    make_order("iso1", 1000)
    assert reconcile("iso-rec", {"order_id": "iso1"}, tenant="t1").status_code == 201
    # 跨租户读取一律 404，不泄漏对账单是否存在。
    assert get_recon("iso-rec", tenant="t2").status_code == 404
    # 不同租户可用同一对账标识各自得到独立对账单。
    make_order("iso1", 1000, tenant="t2")
    other = reconcile("iso-rec", {"order_id": "iso1"}, tenant="t2")
    assert other.status_code == 201
    assert other.json()["tenant"] == "t2"


# ---------- 对账是纯读取：不改动既有数据 ----------

def test_reconcile_does_not_mutate_any_business_data() -> None:
    make_order("ro1", 1000)
    pay("ro1", 700)
    client.post("/refunds", json=refund_payload("rorf1", "ro1", 200))
    client.post("/refunds/rorf1/review", json={"decision": "approve"}, headers=H)

    entries_before = client.get("/orders/ro1/account-entries", headers=H,
                                params={"page_size": 100}).json()["entries"]
    order_before = client.get("/orders/ro1", headers=H).json()
    for i in range(3):
        reconcile(f"ro-rec-{i}", {"order_id": "ro1"})
    entries_after = client.get("/orders/ro1/account-entries", headers=H,
                               params={"page_size": 100}).json()["entries"]
    order_after = client.get("/orders/ro1", headers=H).json()
    assert entries_after == entries_before
    assert order_after == order_before


# ---------- 真实 HTTP 服务：并发与重启 ----------

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


def test_concurrent_reconciliations_are_independent_and_consistent() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc_recon.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10) as c:
            c.post(base + "/orders",
                   json={"tenant": "t1", "order_id": "cc1", "amount_cents": 1000, "currency": "CNY"})
            c.post(base + "/orders/cc1/payments", json={"amount_cents": 400}, headers=H)

            # 不同对账标识同时核对同一范围：各自独立一张、全部 201、结论一致。
            ids = [f"cc-rec-{i}" for i in range(8)]
            with ThreadPoolExecutor(max_workers=8) as ex:
                responses = list(ex.map(
                    lambda rid: c.post(base + "/reconciliations",
                                       json={"tenant": "t1", "reconcile_id": rid,
                                             "scope": {"order_id": "cc1"}}),
                    ids))
            assert all(r.status_code == 201 for r in responses)
            bodies = [r.json() for r in responses]
            assert all(b["status"] == "reconciled" for b in bodies)
            assert all(b["orders"][0]["paid_cents"] == 400 for b in bodies)
            assert {b["reconcile_id"] for b in bodies} == set(ids)

            # 同一对账标识并发：恰好一张 201，其余 200，且结论完全相同。
            with ThreadPoolExecutor(max_workers=12) as ex:
                same = list(ex.map(
                    lambda _: c.post(base + "/reconciliations",
                                     json={"tenant": "t1", "reconcile_id": "cc-same",
                                           "scope": {"order_id": "cc1"}}),
                    range(12)))
            codes = [r.status_code for r in same]
            assert codes.count(201) == 1 and codes.count(200) == 11
            frozen = same[0].json()
            assert all(r.json() == frozen for r in same)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_reconciliation_persists_across_restart() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "persist_recon.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10) as c:
            c.post(base + "/orders",
                   json={"tenant": "t1", "order_id": "pr1", "amount_cents": 1000, "currency": "CNY"})
            c.post(base + "/orders/pr1/payments", json={"amount_cents": 250}, headers=H)
            created = c.post(base + "/reconciliations",
                             json={"tenant": "t1", "reconcile_id": "pr-rec",
                                   "scope": {"order_id": "pr1"}})
            assert created.status_code == 201
            before = created.json()
            # 对账单生成后继续收款，制造与固化结论不同的现状。
            c.post(base + "/orders/pr1/payments", json={"amount_cents": 250}, headers=H)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc = _start_server(db_file, port)
    try:
        with httpx.Client(timeout=10) as c:
            after = c.get(base + "/reconciliations/pr-rec", headers=H)
            assert after.status_code == 200
            assert after.json() == before  # 重启后重读结论一致，仍为生成时的固化快照
            assert after.json()["orders"][0]["paid_cents"] == 250
            # 重复提交仍回放既有单。
            replay = c.post(base + "/reconciliations",
                            json={"tenant": "t1", "reconcile_id": "pr-rec", "scope": {}})
            assert replay.status_code == 200 and replay.json() == before
    finally:
        proc.terminate()
        proc.wait(timeout=10)
