import os
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_entries.sqlite"))

import httpx
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "t1"}
ENTRIES = "account-entries"


def make_order(oid: str, amount: int = 1000, tenant: str = "t1") -> None:
    body = {"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def pay(oid: str, amount: int, tenant: str = "t1"):
    return client.post(f"/orders/{oid}/payments", json={"amount_cents": amount},
                       headers={"X-Tenant": tenant})


def refund_payload(rid: str, oid: str, amount: int, reason: str = "bad goods", tenant: str = "t1") -> dict:
    return {"tenant": tenant, "refund_id": rid, "order_id": oid,
            "amount_cents": amount, "reason": reason}


def rollback_payload(rbid: str, oid: str, amount: int, tenant: str = "t1") -> dict:
    return {"tenant": tenant, "rollback_id": rbid, "order_id": oid, "amount_cents": amount}


def list_entries(oid: str, tenant: str = "t1", **params):
    return client.get(f"/orders/{oid}/{ENTRIES}", headers={"X-Tenant": tenant}, params=params)


def all_entries(oid: str, tenant: str = "t1") -> list[dict]:
    entries, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor is not None:
            params["cursor"] = cursor
        body = list_entries(oid, tenant, **params).json()
        entries.extend(body["entries"])
        if not body["has_next"]:
            return entries
        cursor = body["next_cursor"]


# ---------- 全链路：变动额、余额衔接与账务闭合 ----------

def test_full_lifecycle_chain_and_closure() -> None:
    make_order("e1", 1000)
    assert pay("e1", 600).status_code == 200
    client.post("/refunds", json=refund_payload("erf1", "e1", 300))       # 登记：变化额 0
    client.post("/refunds/erf1/review", json={"decision": "approve"}, headers=H)
    client.post("/refunds/erf1/reverse", headers=H)
    client.post("/payment-rollbacks", json=rollback_payload("erb1", "e1", 200))
    assert pay("e1", 100).status_code == 200
    client.post("/refunds", json=refund_payload("erf2", "e1", 100))       # 登记：变化额 0
    client.post("/refunds/erf2/review", json={"decision": "reject"}, headers=H)  # 拒绝：变化额 0

    entries = all_entries("e1")
    assert [e["action_type"] for e in entries] == [
        "payment_received", "refund_registered", "refund_approved", "refund_reversed",
        "payment_rolled_back", "payment_received", "refund_registered", "refund_rejected",
    ]
    expected_change = [600, 0, -300, 300, -200, 100, 0, 0]
    expected_balance = [600, 600, 300, 600, 400, 500, 500, 500]
    assert [e["change_cents"] for e in entries] == expected_change
    assert [e["balance_cents"] for e in entries] == expected_balance

    # 发生顺序：seq 严格递增；每条流水都唯一回指订单。
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert all(e["tenant"] == "t1" and e["order_id"] == "e1" for e in entries)

    # 逐条变动额与前后余额衔接；任一时点余额落在 0 与订单金额之间。
    running = 0
    for entry, change, balance in zip(entries, expected_change, expected_balance):
        running += change
        assert entry["balance_cents"] == running
        assert 0 <= entry["balance_cents"] <= 1000

    # 最后一条余额等于订单对外已收金额；未收 = 订单金额 − 已收。
    order = client.get("/orders/e1", headers=H).json()
    assert entries[-1]["balance_cents"] == order["paid_cents"] == 500
    assert order["outstanding_cents"] == 500

    # 业务标识口径：收款按订单记入；回退用回退标识；退款各动作均用退款标识。
    by_type = {e["action_type"]: e for e in entries}
    assert by_type["payment_received"]["ref_type"] == "payment"
    assert by_type["payment_received"]["ref_id"] == "e1"
    assert by_type["payment_rolled_back"]["ref_type"] == "rollback"
    assert by_type["payment_rolled_back"]["ref_id"] == "erb1"
    for kind in ("refund_registered", "refund_approved", "refund_reversed", "refund_rejected"):
        assert by_type[kind]["ref_type"] == "refund"
    registered_refs = {e["ref_id"] for e in entries if e["action_type"] == "refund_registered"}
    assert registered_refs == {"erf1", "erf2"}
    assert by_type["refund_approved"]["ref_id"] == "erf1"
    assert by_type["refund_rejected"]["ref_id"] == "erf2"


def test_order_acceptance_writes_no_entry_and_multiple_payments_each_recorded() -> None:
    make_order("e1m", 1000)
    assert all_entries("e1m") == []  # 受理不是账务动作，不产生流水。
    pay("e1m", 200)
    pay("e1m", 300)
    entries = all_entries("e1m")
    assert [e["action_type"] for e in entries] == ["payment_received", "payment_received"]
    assert [e["change_cents"] for e in entries] == [200, 300]
    assert [e["balance_cents"] for e in entries] == [200, 500]
    # 收款无独立标识，两条流水都按所属订单记入。
    assert all(e["ref_type"] == "payment" and e["ref_id"] == "e1m" for e in entries)


# ---------- 幂等与失败：不产生新动作就不追加流水 ----------

def test_rejected_or_failed_actions_write_no_entries() -> None:
    make_order("e2", 1000)
    pay("e2", 400)
    # 收款超过未收金额：被拒绝，不写流水。
    assert pay("e2", 9999).status_code == 409
    # 退款超额：冲突，不写流水（连登记流水也没有）。
    assert client.post("/refunds", json=refund_payload("erf-big", "e2", 500)).status_code == 409
    # 回退超过当前已收：参数错误，不写流水。
    assert client.post("/payment-rollbacks", json=rollback_payload("erb-big", "e2", 401)).status_code == 400
    # 待审核退款占用额度导致回退冲突：不写流水。
    client.post("/refunds", json=refund_payload("erf-hold", "e2", 400))
    assert client.post("/payment-rollbacks", json=rollback_payload("erb-hold", "e2", 100)).status_code == 409

    entries = all_entries("e2")
    # 成功的待审核登记留下一条 0 变化流水；被拒的收款/超额退款/冲突回退均无流水。
    assert [e["action_type"] for e in entries] == ["payment_received", "refund_registered"]
    assert entries[0]["change_cents"] == 400 and entries[0]["balance_cents"] == 400
    assert entries[1]["change_cents"] == 0 and entries[1]["balance_cents"] == 400
    assert not any(e["action_type"] == "payment_rolled_back" for e in entries)


def test_idempotent_retries_append_nothing() -> None:
    make_order("e3", 1000)
    pay("e3", 1000)
    client.post("/refunds", json=refund_payload("erf3", "e3", 200))
    client.post("/payment-rollbacks", json=rollback_payload("erb3", "e3", 100))

    # 重复登记同一退款标识：返回既有单据（200），不追加登记流水。
    again_refund = client.post("/refunds", json=refund_payload("erf3", "e3", 200))
    assert again_refund.status_code == 200
    # 重复提交同一回退标识（携带不同金额指纹）：不重复退回、不追加流水。
    again_rb = client.post("/payment-rollbacks", json=rollback_payload("erb3", "e3", 999))
    assert again_rb.status_code == 200 and again_rb.json()["amount_cents"] == 100

    # 审核同意后重复审核返回原结果，不追加第二条审核流水。
    assert client.post("/refunds/erf3/review", json={"decision": "approve"}, headers=H).status_code == 200
    assert client.post("/refunds/erf3/review", json={"decision": "approve"}, headers=H).status_code == 200
    assert client.post("/refunds/erf3/review", json={"decision": "reject"}, headers=H).status_code == 200

    # 冲正一次生效；对已冲正单据再次冲正返回 409，不追加第二条冲正流水。
    assert client.post("/refunds/erf3/reverse", headers=H).status_code == 200
    assert client.post("/refunds/erf3/reverse", headers=H).status_code == 409
    # 已冲正后再审核同样不产生新动作。
    assert client.post("/refunds/erf3/review", json={"decision": "approve"}, headers=H).status_code == 409

    types = [e["action_type"] for e in all_entries("e3")]
    assert types == [
        "payment_received", "refund_registered", "payment_rolled_back",
        "refund_approved", "refund_reversed",
    ]
    # 反向动作以新流水体现，历史同意流水保持原样（不被改写）。
    approved = [e for e in all_entries("e3") if e["action_type"] == "refund_approved"]
    assert len(approved) == 1 and approved[0]["change_cents"] == -200


def test_rejected_refund_re_review_and_blocked_reverse_append_nothing() -> None:
    make_order("e3b", 1000)
    pay("e3b", 1000)
    client.post("/refunds", json=refund_payload("erf3b", "e3b", 300))
    client.post("/refunds/erf3b/review", json={"decision": "reject"}, headers=H)
    # 已拒绝的审核结果不可覆盖：再提交同意仍返回原结果，不追加流水。
    client.post("/refunds/erf3b/review", json={"decision": "approve"}, headers=H)
    types = [e["action_type"] for e in all_entries("e3b")]
    assert types == ["payment_received", "refund_registered", "refund_rejected"]

    # 冲正被拒（腾出的额度已被后续收款补回，冲正会超过订单金额）：不写冲正流水。
    make_order("e3c", 1000)
    pay("e3c", 1000)
    client.post("/refunds", json=refund_payload("erf3c", "e3c", 300))
    client.post("/refunds/erf3c/review", json={"decision": "approve"}, headers=H)
    assert pay("e3c", 300).status_code == 200  # 净已收补回到 1000
    assert client.post("/refunds/erf3c/reverse", headers=H).status_code == 409
    types = [e["action_type"] for e in all_entries("e3c")]
    assert types == ["payment_received", "refund_registered", "refund_approved", "payment_received"]


# ---------- 分页 ----------

def test_pagination_walks_all_entries_without_gap_or_duplicate() -> None:
    make_order("e4", 1000)
    for i in range(6):
        assert pay("e4", 100).status_code == 200
    full = list_entries("e4", page_size=100).json()["entries"]
    assert len(full) == 6

    collected: list[dict] = []
    cursor = None
    pages = 0
    while True:
        params = {"page_size": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = list_entries("e4", **params).json()
        pages += 1
        collected.extend(body["entries"])
        if not body["has_next"]:
            assert body["next_cursor"] is None
            break
        assert body["next_cursor"] == body["entries"][-1]["seq"]
        cursor = body["next_cursor"]
    assert pages == 3
    assert [e["seq"] for e in collected] == [e["seq"] for e in full]
    assert len({e["seq"] for e in collected}) == len(collected) == 6


def test_pagination_keeps_page_size_and_exposes_cursor() -> None:
    make_order("e4b", 1000)
    pay("e4b", 100)
    pay("e4b", 100)
    body = list_entries("e4b", page_size=1).json()
    assert body["page_size"] == 1 and body["has_next"] is True
    assert len(body["entries"]) == 1
    cursor = body["next_cursor"]
    second = list_entries("e4b", page_size=1, cursor=cursor).json()
    assert second["entries"][0]["seq"] > cursor and second["has_next"] is False


def test_bad_paging_params_are_400() -> None:
    make_order("e5", 1000)
    pay("e5", 100)
    assert list_entries("e5").status_code == 400  # page_size 必传
    for bad in (0, -1, "abc", 1.5):
        assert list_entries("e5", page_size=bad).status_code == 400, bad
    for bad in (0, -1, "abc"):
        assert list_entries("e5", page_size=10, cursor=bad).status_code == 400, bad
    # 游标不指向本租户该订单的任何流水：参数错误。
    assert list_entries("e5", page_size=10, cursor=99999999).status_code == 400

    make_order("e5-other", 1000)
    pay("e5-other", 100)
    foreign_seq = all_entries("e5-other")[0]["seq"]
    # 顺序位置属于本租户的另一张订单：不指向本订单的流水，同样参数错误。
    assert list_entries("e5", page_size=10, cursor=foreign_seq).status_code == 400


# ---------- 租户隔离 ----------

def test_unknown_order_is_404_even_without_entries() -> None:
    assert list_entries("nope", page_size=10).status_code == 404
    make_order("e6", 1000)
    # 订单存在但尚无账务动作：返回空流水页而非 404。
    body = list_entries("e6", page_size=10).json()
    assert body["entries"] == [] and body["has_next"] is False


def test_cross_tenant_query_is_404_and_does_not_leak() -> None:
    make_order("e7", 1000, tenant="t1")
    pay("e7", 100)
    # 跨租户查询一律按订单不存在处理；即使游标恰为他租户的 seq 也返回 404。
    seq = all_entries("e7")[0]["seq"]
    assert list_entries("e7", tenant="t2", page_size=10).status_code == 404
    assert list_entries("e7", tenant="t2", page_size=10, cursor=seq).status_code == 404
    # 缺少租户头为参数错误。
    assert client.get(f"/orders/e7/{ENTRIES}", params={"page_size": 10}).status_code == 400


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


def test_concurrent_actions_leave_complete_chained_entries() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc_entries.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10) as c:
            c.post(base + "/orders",
                   json={"tenant": "t1", "order_id": "ce1", "amount_cents": 1000, "currency": "CNY"})
            # 同一回退标识并发：恰好一次生效、一条回退流水。
            payload = rollback_payload("cerb", "ce1", 100)
            # 先收足款。
            c.post(base + "/orders/ce1/payments", json={"amount_cents": 1000}, headers=H)
            with ThreadPoolExecutor(max_workers=12) as ex:
                rb_statuses = list(ex.map(
                    lambda _: c.post(base + "/payment-rollbacks", json=payload).status_code, range(12)))
            assert rb_statuses.count(201) == 1 and rb_statuses.count(200) == 11

            # 并发多笔收款：成功的动作各一条流水，变动额与余额逐条衔接，无半条记录。
            def pay_one(_: int) -> int:
                return c.post(base + "/orders/ce1/payments", json={"amount_cents": 100},
                              headers=H).status_code

            with ThreadPoolExecutor(max_workers=16) as ex:
                pay_statuses = list(ex.map(pay_one, range(16)))
            assert 200 in pay_statuses and 409 in pay_statuses  # 未收额度有限，部分必然被拒

            entries = c.get(base + "/orders/ce1/account-entries",
                            headers=H, params={"page_size": 200}).json()["entries"]
            order = c.get(base + "/orders/ce1", headers=H).json()

        types = [e["action_type"] for e in entries]
        assert types.count("payment_rolled_back") == 1
        assert types.count("payment_received") == pay_statuses.count(200) + 1  # 含最初的 1000 收款
        running = 0
        for e in entries:
            running += e["change_cents"]
            assert e["balance_cents"] == running
            assert 0 <= e["balance_cents"] <= 1000
        assert running == entries[-1]["balance_cents"] == order["paid_cents"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_entries_persist_across_restart_and_stay_immutable() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "persist_entries.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10) as c:
            c.post(base + "/orders",
                   json={"tenant": "t1", "order_id": "pe1", "amount_cents": 1000, "currency": "CNY"})
            c.post(base + "/orders/pe1/payments", json={"amount_cents": 800}, headers=H)
            c.post(base + "/refunds", json=refund_payload("perf1", "pe1", 300))
            c.post(base + "/refunds/perf1/review", json={"decision": "approve"}, headers=H)
            c.post(base + "/payment-rollbacks", json=rollback_payload("perb1", "pe1", 200))
            before = c.get(base + "/orders/pe1/account-entries",
                           headers=H, params={"page_size": 100}).json()["entries"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc = _start_server(db_file, port)
    try:
        with httpx.Client(timeout=10) as c:
            after = c.get(base + "/orders/pe1/account-entries",
                          headers=H, params={"page_size": 100}).json()["entries"]
            order = c.get(base + "/orders/pe1", headers=H).json()
        # 重启后按顺序重读结论一致；已提交的动作与流水一起保留。
        assert before == after
        assert [e["action_type"] for e in after] == [
            "payment_received", "refund_registered", "refund_approved", "payment_rolled_back"]
        # 毛额 600（800-200），净已收 = 600 - 300 = 300；流水末余额与之一致。
        assert after[-1]["balance_cents"] == 300 == order["paid_cents"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
