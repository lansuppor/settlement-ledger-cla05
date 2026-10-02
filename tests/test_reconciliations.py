import os
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_reconciliations.sqlite"))

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


def start_recon(rid: str, status: int | None = None, tenant: str = "t1", **scope):
    resp = client.post("/reconciliations", json={"tenant": tenant, "reconcile_id": rid, **scope})
    if status is not None:
        assert resp.status_code == status, resp.text
    return resp


def get_recon(rid: str, tenant: str = "t1"):
    return client.get(f"/reconciliations/{rid}", headers={"X-Tenant": tenant})


def full_entries(oid: str, tenant: str = "t1") -> list[dict]:
    entries, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get(f"/orders/{oid}/account-entries", headers={"X-Tenant": tenant},
                          params=params).json()
        entries.extend(body["entries"])
        if not body["has_next"]:
            return entries
        cursor = body["next_cursor"]


def execute_sql(statement: str, params=()) -> None:
    """直接改库以构造正常业务路径不可能出现的差异（断链/末余额不一致/金额不闭合）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(statement, params)
        conn.execute("COMMIT")
    finally:
        conn.close()


# ---------- 已核销：逐条衔接、末余额等于已收、金额闭合 ----------

def test_clean_order_is_reconciled_with_expected_running_balances() -> None:
    make_order("rc-r1", 1000)
    pay("rc-r1", 600)
    client.post("/payment-rollbacks",
                json={"tenant": "t1", "rollback_id": "rcrb1", "order_id": "rc-r1", "amount_cents": 100})
    client.post("/refunds",
                json={"tenant": "t1", "refund_id": "rcrf1", "order_id": "rc-r1",
                      "amount_cents": 200, "reason": "x"})
    client.post("/refunds/rcrf1/review", json={"decision": "approve"}, headers=H)

    resp = start_recon("rec-r1", order_id="rc-r1")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "reconciled"
    assert body["order_count"] == 1 and body["discrepancy_count"] == 0
    assert body["discrepancies"] == []
    assert body["reconcile_id"] == "rec-r1" and body["tenant"] == "t1"
    assert body["reconciled_at"]

    order = body["orders"][0]
    # 毛额 500（600-100），净已收 = 500-200 = 300；未收 = 1000-300 = 700。
    assert order["amount_cents"] == 1000
    assert order["paid_cents"] == 300
    assert order["outstanding_cents"] == 700
    assert order["chain_intact"] is True
    assert order["final_balance_matches_paid"] is True
    assert order["amount_closed"] is True
    assert order["reconciled"] is True

    # 逐条流水的应有余额从 0 按变化额累计，与落库余额逐条一致、全部衔接。
    entries = order["entries"]
    assert [e["action_type"] for e in entries] == [
        "payment_received", "payment_rolled_back", "refund_registered", "refund_approved"]
    running = 0
    for entry in entries:
        running += entry["change_cents"]
        assert entry["expected_balance_cents"] == running
        assert entry["balance_cents"] == running
        assert entry["chained"] is True
    assert entries[-1]["balance_cents"] == 300


def test_order_without_entries_is_reconciled_from_zero_balance() -> None:
    make_order("rc-r0", 800)
    body = start_recon("rec-r0", 201, order_id="rc-r0").json()
    order = body["orders"][0]
    assert body["status"] == "reconciled"
    assert order["entries"] == []
    # 无流水时末条余额视为 0，与尚未收款的当前已收一致。
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 800
    assert order["chain_intact"] and order["final_balance_matches_paid"] and order["amount_closed"]


# ---------- 有差异：断链、末余额不一致、金额不闭合 ----------

def test_broken_balance_chain_is_reported_per_entry() -> None:
    make_order("rc-d1", 1000)
    pay("rc-d1", 200)
    pay("rc-d1", 300)
    # 把第一条流水落库余额改成 250：应有余额 200，衔接断在第 1 条；
    # 第二条应有余额仍为 500，末条余额与当前已收（500）依旧一致，金额仍闭合。
    execute_sql(
        "UPDATE order_account_entries SET balance_cents=250 WHERE rowid IN ("
        "SELECT rowid FROM order_account_entries WHERE tenant='t1' AND order_id='rc-d1' ORDER BY seq LIMIT 1)")

    body = start_recon("rec-d1", 201, order_id="rc-d1").json()
    assert body["status"] == "discrepancy"
    assert body["discrepancy_count"] == 1
    order = body["orders"][0]
    assert order["chain_intact"] is False and order["reconciled"] is False
    assert order["entries"][0]["chained"] is False
    assert order["entries"][0]["expected_balance_cents"] == 200
    assert order["entries"][1]["chained"] is True
    assert order["final_balance_matches_paid"] is True
    assert order["amount_closed"] is True
    diff = body["discrepancies"][0]
    assert diff["order_id"] == "rc-d1"
    assert diff["reasons"] == ["balance_chain_broken"]
    assert diff["broken_entry_seqs"] == [order["entries"][0]["seq"]]


def test_final_balance_mismatch_with_paid_is_reported() -> None:
    make_order("rc-d2", 1000)
    pay("rc-d2", 500)
    # 只改订单累计收款毛额：流水自身仍衔接、末余额 500，但当前对外已收变成 400。
    execute_sql("UPDATE orders SET paid_cents=400 WHERE tenant='t1' AND order_id='rc-d2'")
    body = start_recon("rec-d2", 201, order_id="rc-d2").json()
    assert body["status"] == "discrepancy"
    diff = body["discrepancies"][0]
    assert diff["order_id"] == "rc-d2"
    assert diff["reasons"] == ["final_balance_mismatch"]
    assert diff["last_balance_cents"] == 500 and diff["current_paid_cents"] == 400
    order = body["orders"][0]
    # 未收按当前金额口径重算，400 仍在 [0, 订单金额] 内，金额闭合恒等式本身成立。
    assert order["outstanding_cents"] == 600 and order["amount_closed"] is True
    assert order["chain_intact"] is True and order["final_balance_matches_paid"] is False


def test_amount_not_closed_is_reported() -> None:
    make_order("rc-d3", 1000)
    pay("rc-d3", 500)
    # 累计收款毛额超过订单金额：净已收越界，金额不闭合；末条余额 500 也不再等于当前已收 1200。
    execute_sql("UPDATE orders SET paid_cents=1200 WHERE tenant='t1' AND order_id='rc-d3'")
    body = start_recon("rec-d3", 201, order_id="rc-d3").json()
    assert body["status"] == "discrepancy"
    diff = body["discrepancies"][0]
    assert set(diff["reasons"]) == {"amount_not_closed", "final_balance_mismatch"}
    assert body["orders"][0]["amount_closed"] is False


def test_discrepancy_listed_for_each_order_and_overall_status() -> None:
    # 独立租户：范围取全租户订单时不被其他用例的订单干扰。
    make_order("rc-d4a", 1000, tenant="td")
    make_order("rc-d4b", 1000, tenant="td")
    make_order("rc-d4c", 1000, tenant="td")
    pay("rc-d4a", 1000, tenant="td")
    pay("rc-d4b", 500, tenant="td")
    pay("rc-d4c", 1000, tenant="td")
    execute_sql("UPDATE orders SET paid_cents=400 WHERE tenant='td' AND order_id='rc-d4b'")
    body = start_recon("rec-d4", 201, tenant="td", amount_min=0).json()
    assert body["order_count"] == 3 and body["discrepancy_count"] == 1
    assert [o["order_id"] for o in body["orders"]] == ["rc-d4a", "rc-d4b", "rc-d4c"]
    assert [d["order_id"] for d in body["discrepancies"]] == ["rc-d4b"]
    by_id = {o["order_id"]: o for o in body["orders"]}
    assert by_id["rc-d4a"]["reconciled"] and by_id["rc-d4c"]["reconciled"]
    assert not by_id["rc-d4b"]["reconciled"]


# ---------- 范围：区间、交集、边界与空范围 ----------

def test_amount_and_outstanding_ranges_intersect_with_inclusive_bounds() -> None:
    # 独立租户，保证区间只覆盖本用例的三张订单。
    make_order("rc-g1", 100, tenant="tg")  # 未收 100
    make_order("rc-g2", 200, tenant="tg")
    pay("rc-g2", 200, tenant="tg")         # 未收 0
    make_order("rc-g3", 300, tenant="tg")
    pay("rc-g3", 100, tenant="tg")         # 未收 200

    body = start_recon("rec-g1", 201, tenant="tg", amount_min=100, amount_max=300,
                       outstanding_min=0, outstanding_max=100).json()
    # g3 未收为 200，不落在未收 [0,100] 区间，被交集排除。
    assert [o["order_id"] for o in body["orders"]] == ["rc-g1", "rc-g2"]

    # 只给一侧端点，含边界：未收 (100, 200]。
    body = start_recon("rec-g2", 201, tenant="tg", outstanding_min=101, outstanding_max=200).json()
    assert [o["order_id"] for o in body["orders"]] == ["rc-g3"]
    # 边界 100 只命中未收恰为 100 的 g1。
    body = start_recon("rec-g3", 201, tenant="tg", outstanding_min=100, outstanding_max=100).json()
    assert [o["order_id"] for o in body["orders"]] == ["rc-g1"]
    # 金额区间与未收区间取交集：金额 300 且未收 >=200 只有 g3。
    body = start_recon("rec-g4", 201, tenant="tg", amount_min=300, outstanding_min=200).json()
    assert [o["order_id"] for o in body["orders"]] == ["rc-g3"]


def test_invalid_or_empty_scope_is_400_and_leaves_no_statement() -> None:
    make_order("rc-v1", 1000)
    # 缺少任何范围形态。
    assert start_recon("rec-v0", tenant="t1").status_code == 400
    # 端点非法：负数、小数、字符串、布尔、空字符串订单。
    for bad in (-1, 1.5, "10", True):
        assert start_recon("rec-v1", amount_min=bad).status_code == 400, bad
        assert start_recon("rec-v1", outstanding_max=bad).status_code == 400, bad
    assert client.post("/reconciliations",
                       json={"tenant": "t1", "reconcile_id": "rec-v1", "order_id": ""}).status_code == 400
    # 下限大于上限（两类区间分别校验）。
    assert start_recon("rec-v1", amount_min=10, amount_max=9).status_code == 400
    assert start_recon("rec-v1", outstanding_min=10, outstanding_max=9).status_code == 400
    # 缺少 tenant / reconcile_id。
    assert client.post("/reconciliations", json={"reconcile_id": "x", "order_id": "rc-v1"}).status_code == 400
    assert client.post("/reconciliations", json={"tenant": "t1", "order_id": "rc-v1"}).status_code == 400

    # 范围为空：区间不命中任何订单、未知订单标识，一律参数错误。
    assert start_recon("rec-v2", amount_min=999999).status_code == 400
    assert start_recon("rec-v3", order_id="missing").status_code == 400

    # 失败不留半张对账单：上面用过的标识仍可首次成功生成（201），读取此前失败标识为 404。
    assert get_recon("rec-v2").status_code == 404
    assert start_recon("rec-v2", 201, order_id="rc-v1").status_code == 201


# ---------- 幂等：身份只认（租户, reconcile_id），与范围指纹无关 ----------

def test_replay_returns_existing_statement_unchanged() -> None:
    make_order("rc-i1", 1000)
    make_order("rc-i2", 2000)
    pay("rc-i1", 400)
    first = start_recon("rec-i", 201, order_id="rc-i1").json()

    # 用完全不同的范围指纹重放：不重新核对，返回既有对账单（200），结论与范围留痕不变。
    second = start_recon("rec-i", order_id="rc-i2")
    assert second.status_code == 200
    assert second.json() == first

    # 数据随后变化也不影响既有结论。
    pay("rc-i1", 100)
    third = start_recon("rec-i", amount_min=0)
    assert third.status_code == 200
    assert third.json() == first
    # 按标识读取得到同一张不可变对账单。
    assert get_recon("rec-i").json() == first


def test_different_reconcile_ids_are_independent_statements() -> None:
    make_order("rc-ix", 1000)
    pay("rc-ix", 300)
    a = start_recon("rec-a", 201, order_id="rc-ix").json()
    pay("rc-ix", 200)  # 已收变为 500
    b = start_recon("rec-b", 201, order_id="rc-ix").json()
    assert a["reconcile_id"] != b["reconcile_id"]
    assert a["orders"][0]["paid_cents"] == 300
    assert b["orders"][0]["paid_cents"] == 500
    # 两张对账单各自固化自己时点的结论，互不影响。
    assert get_recon("rec-a").json() == a
    assert get_recon("rec-b").json() == b


# ---------- 租户隔离 ----------

def test_tenant_isolation_on_create_and_read() -> None:
    make_order("rc-ta", 1000, tenant="t1")
    # 其他租户的订单不在本租户范围内：按范围为空参数错误处理，不泄漏订单是否存在。
    assert start_recon("rec-tt", tenant="t2", order_id="rc-ta").status_code == 400
    # t1 正常生成。
    assert start_recon("rec-tt", tenant="t1", status=201, order_id="rc-ta").status_code == 201
    # 跨租户读取一律 404；缺失租户头 400。
    assert get_recon("rec-tt", tenant="t2").status_code == 404
    assert client.get("/reconciliations/rec-tt").status_code == 400
    # 对其他租户而言该标识仍可生成独立的一张（范围为空则 400；这里其租户无任何订单）。
    assert start_recon("rec-tt-empty", tenant="t-rc-empty", amount_min=0).status_code == 400


# ---------- 纯读取：不改变任何订单与流水 ----------

def test_reconciliation_is_pure_read_on_business_data() -> None:
    make_order("rc-p1", 1000)
    pay("rc-p1", 700)
    client.post("/payment-rollbacks",
                json={"tenant": "t1", "rollback_id": "rcprb", "order_id": "rc-p1", "amount_cents": 200})
    before_order = client.get("/orders/rc-p1", headers=H).json()
    before_entries = full_entries("rc-p1")
    start_recon("rec-p1", 201, order_id="rc-p1")
    # 再次对账（幂等回放）后业务数据仍逐字节不变。
    start_recon("rec-p1", order_id="rc-p1")
    assert client.get("/orders/rc-p1", headers=H).json() == before_order
    assert full_entries("rc-p1") == before_entries
    # 退款/回退单据同样不受影响。
    assert client.get("/payment-rollbacks/rcprb", headers=H).status_code == 200


# ---------- 真实 HTTP 服务：并发与重启持久化 ----------

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


def test_concurrent_different_ids_each_get_consistent_statement() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc_recon.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=20) as c:
            for i in range(6):
                r = c.post(base + "/orders",
                           json={"tenant": "t1", "order_id": f"c{i}", "amount_cents": 1000,
                                 "currency": "CNY"})
                assert r.status_code == 201

            def reconcile_one(i: int) -> dict:
                resp = c.post(base + "/reconciliations",
                              json={"tenant": "t1", "reconcile_id": f"rec-c{i}", "amount_min": 0})
                assert resp.status_code == 201, resp.text
                return resp.json()

            def keep_paying() -> None:
                for i in range(20):
                    c.post(base + "/orders/c0/payments", json={"amount_cents": 50}, headers=H)

            with ThreadPoolExecutor(max_workers=10) as ex:
                pay_fut = ex.submit(keep_paying)
                statements = list(ex.map(reconcile_one, range(6)))
                pay_fut.result()

        # 每张对账单在同一份已提交快照内自洽：逐单逐条衔接、末余额等于当时已收、金额闭合。
        for statement in statements:
            assert statement["status"] == "reconciled"
            assert statement["discrepancies"] == []
            for order in statement["orders"]:
                running = 0
                for entry in order["entries"]:
                    running += entry["change_cents"]
                    assert entry["chained"]
                assert order["final_balance_matches_paid"] and order["amount_closed"]
                assert order["amount_cents"] == order["paid_cents"] + order["outstanding_cents"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_concurrent_same_id_creates_exactly_one_statement() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "conc_recon_same.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=20) as c:
            c.post(base + "/orders",
                   json={"tenant": "t1", "order_id": "s1", "amount_cents": 1000, "currency": "CNY"})
            c.post(base + "/orders/s1/payments", json={"amount_cents": 400}, headers=H)
            payload = {"tenant": "t1", "reconcile_id": "rec-one", "order_id": "s1"}
            with ThreadPoolExecutor(max_workers=12) as ex:
                statuses = list(ex.map(lambda _: c.post(base + "/reconciliations", json=payload).status_code,
                                       range(12)))
            assert statuses.count(201) == 1 and statuses.count(200) == 11
            body = c.get(base + "/reconciliations/rec-one", headers=H).json()
            assert body["order_count"] == 1 and body["orders"][0]["paid_cents"] == 400
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_statements_persist_across_restart() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "persist_recon.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10) as c:
            c.post(base + "/orders",
                   json={"tenant": "t1", "order_id": "z1", "amount_cents": 1000, "currency": "CNY"})
            c.post(base + "/orders/z1/payments", json={"amount_cents": 600}, headers=H)
            c.post(base + "/payment-rollbacks",
                   json={"tenant": "t1", "rollback_id": "rczrb", "order_id": "z1", "amount_cents": 100})
            first = c.post(base + "/reconciliations",
                           json={"tenant": "t1", "reconcile_id": "rec-z1", "order_id": "z1"}).json()
            # 重启后再发生的新动作不得改变既有对账单。
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc = _start_server(db_file, port)
    try:
        with httpx.Client(timeout=10) as c:
            c.post(base + "/orders/z1/payments", json={"amount_cents": 300}, headers=H)
            reread = c.get(base + "/reconciliations/rec-z1", headers=H).json()
            replayed = c.post(base + "/reconciliations",
                              json={"tenant": "t1", "reconcile_id": "rec-z1", "amount_min": 0}).json()
        assert reread == first
        assert replayed == first
        assert reread["status"] == "reconciled"
        assert reread["orders"][0]["paid_cents"] == 500
    finally:
        proc.terminate()
        proc.wait(timeout=10)
