import os
import socket
import sqlite3
import subprocess
import sys
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_batches.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store import batches
from app.store.db import migrate
from app.usecase import batch_import

migrate()
client = TestClient(app)
quiet_client = TestClient(app, raise_server_exceptions=False)

H1 = {"X-Tenant": "t1"}
HEADER = "tenant,order_id,amount_cents,currency\n"


def csv_text(rows: list[str]) -> str:
    return HEADER + "\n".join(rows) + ("\n" if rows else "")


def post_batch(tenant: str, batch_id: str, csv_data: str):
    return client.post("/orders/batch-accept", json={"tenant": tenant, "batch_id": batch_id, "csv": csv_data})


# ---------- 基本受理与读取 ----------

def test_batch_all_success_completes_and_orders_are_usable() -> None:
    data = csv_text(["t1,b1-1,1200,CNY", "t1,b1-2,800,USD"])
    r = post_batch("t1", "batch-1", data)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch_id"] == "batch-1"
    assert body["status"] == "completed"
    assert body["total"] == 2 and body["succeeded"] == 2 and body["failed"] == 0
    assert body["errors"] == []
    assert body["accepted_order_ids"] == ["b1-1", "b1-2"]

    got = client.get("/batches/batch-1", headers=H1)
    assert got.status_code == 200 and got.json() == body

    # 受理成功的订单沿用既有字段与金额口径，可读取、登记收款。
    order = client.get("/orders/b1-1", headers=H1).json()
    assert order["amount_cents"] == 1200 and order["currency"] == "CNY"
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1200
    paid = client.post("/orders/b1-1/payments", json={"amount_cents": 1200}, headers=H1)
    assert paid.status_code == 200 and paid.json()["status"] == "settled"


def test_header_only_input_completes_empty() -> None:
    r = post_batch("t1", "batch-empty", HEADER)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "completed"
    assert body["total"] == 0 and body["succeeded"] == 0 and body["failed"] == 0
    assert body["errors"] == [] and body["accepted_order_ids"] == []


# ---------- 逐行校验、部分成功与错误区分 ----------

def test_per_row_validation_gives_partial_success() -> None:
    data = csv_text([
        "t1,b2-ok,500,CNY",       # 行2 成功
        "t2,b2-cross,500,CNY",    # 行3 租户不一致
        "t1,,500,CNY",            # 行4 订单标识为空
        "t1,b2-zero,0,CNY",       # 行5 金额为 0
        "t1,b2-neg,-9,CNY",       # 行6 金额为负
        "t1,b2-frac,1.5,CNY",     # 行7 金额非整数
        "t1,b2-badcc,500,XXX",    # 行8 币种不支持
        "t1,b2-ok2,300,EUR",      # 行9 成功
    ])
    r = post_batch("t1", "batch-2", data)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "completed_with_errors"
    assert body["total"] == 8 and body["succeeded"] == 2 and body["failed"] == 6
    assert body["succeeded"] + body["failed"] == body["total"]
    assert body["accepted_order_ids"] == ["b2-ok", "b2-ok2"]

    errors = {(e["line_no"], e["error_code"]) for e in body["errors"]}
    assert errors == {
        (3, "invalid_parameter"), (4, "invalid_parameter"), (5, "invalid_parameter"),
        (6, "invalid_parameter"), (7, "invalid_parameter"), (8, "invalid_parameter"),
    }
    assert all(e["message"] for e in body["errors"])

    # 失败行未受理，成功行可读取。
    assert client.get("/orders/b2-zero", headers=H1).status_code == 404
    assert client.get("/orders/b2-ok2", headers=H1).status_code == 200


def test_existing_order_is_conflict_and_untouched() -> None:
    # 单笔接口先行受理。
    assert client.post("/orders", json={"tenant": "t1", "order_id": "b3-exists",
                                        "amount_cents": 1000, "currency": "CNY"}).status_code == 201
    data = csv_text([
        "t1,b3-exists,42,USD",  # 已受理：冲突，不改变金额/币种
        "t1,b3-new,42,CNY",     # 正常受理
        "t1,b3-new,99,CNY",     # 批次内重复订单标识：同样冲突，只有一张订单
    ])
    r = post_batch("t1", "batch-3", data)
    body = r.json()
    assert body["status"] == "completed_with_errors"
    assert body["succeeded"] == 1 and body["failed"] == 2
    assert {(e["line_no"], e["error_code"]) for e in body["errors"]} == {
        (2, "order_conflict"), (4, "order_conflict"),
    }

    order = client.get("/orders/b3-exists", headers=H1).json()
    assert order["amount_cents"] == 1000 and order["currency"] == "CNY"  # 既有数据未被改变
    conn = sqlite3.connect(db_path())
    count = conn.execute("SELECT COUNT(*) FROM orders WHERE tenant='t1' AND order_id='b3-new'").fetchone()[0]
    conn.close()
    assert count == 1  # 同一行最多产生一张订单


# ---------- 重放幂等 ----------

def test_replay_same_batch_returns_existing_result_and_creates_nothing() -> None:
    data = csv_text(["t1,b4-1,100,CNY", "t1,b4-2,200,CNY"])
    first = post_batch("t1", "batch-4", data)
    assert first.status_code == 201
    second = post_batch("t1", "batch-4", data)
    assert second.status_code == 200 and second.json() == first.json()

    # 终态后即便换成不同内容重放，也只返回既有结果，不受理任何新订单。
    other = post_batch("t1", "batch-4", csv_text(["t1,b4-other,999,CNY"]))
    assert other.status_code == 200 and other.json() == first.json()
    assert client.get("/orders/b4-other", headers=H1).status_code == 404

    conn = sqlite3.connect(db_path())
    n_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE order_id LIKE 'b4-%'").fetchone()[0]
    n_lines = conn.execute("SELECT COUNT(*) FROM batch_orders WHERE batch_id='batch-4'").fetchone()[0]
    conn.close()
    assert n_orders == 2 and n_lines == 2


def test_in_progress_resume_with_different_input_is_409() -> None:
    # 直接在存储层制造一个进行中的批次，再用另一份输入续跑。
    raw_a = csv_text(["t1,b5-1,100,CNY", "t1,b5-2,100,CNY"]).encode("utf-8")
    batches.ensure("t1", "batch-5", 2, raw_a)
    other = post_batch("t1", "batch-5", csv_text(["t1,b5-9,100,CNY"]))
    assert other.status_code == 409
    # 原输入仍可断点续跑。
    ok = post_batch("t1", "batch-5", csv_text(["t1,b5-1,100,CNY", "t1,b5-2,100,CNY"]))
    assert ok.status_code == 201 and ok.json()["status"] == "completed"


# ---------- 非法 CSV 整批拒绝 ----------

def test_malformed_csv_rejects_whole_batch() -> None:
    malformed = [
        b"",  # 空内容
        b"order_id,amount_cents,currency\n",  # 缺表头
        b"tenant,order_id,amount_cents,currency\nt1,x,100\n",  # 列数不符
        b"tenant,order_id,amount_cents,currency\nt1,x,100,CNY,extra\n",  # 列数过多
        b"tenant,order_id,amount_cents,currency\nt1,\"unclosed,100,CNY\n",  # 行结构损坏
        b"tenant,order_id,amount_cents,currency\nt1,x,100,\xff\n",  # 非法 UTF-8
    ]
    for index, raw in enumerate(malformed):
        r = client.post(f"/orders/batch-accept?tenant=t1&batch_id=bad-{index}",
                        content=raw, headers={"content-type": "text/csv"})
        assert r.status_code == 400, (index, r.status_code, r.text)
        # 整批拒绝：批次不存在，没有任何行被受理。
        assert client.get(f"/batches/bad-{index}", headers=H1).status_code == 404


def test_raw_csv_upload_with_query_params_works() -> None:
    raw = csv_text(["t1,b6-1,100,CNY"]).encode("utf-8")
    r = client.post("/orders/batch-accept?tenant=t1&batch_id=batch-6",
                    content=raw, headers={"content-type": "text/csv"})
    assert r.status_code == 201 and r.json()["accepted_order_ids"] == ["b6-1"]
    assert client.post("/orders/batch-accept?tenant=t1&batch_id=batch-6",
                       content=raw, headers={"content-type": "text/csv"}).status_code == 200


def test_batch_request_requires_identity() -> None:
    assert client.post("/orders/batch-accept", json={"batch_id": "x", "csv": HEADER}).status_code == 400
    assert client.post("/orders/batch-accept", json={"tenant": "t1", "csv": HEADER}).status_code == 400
    assert client.post("/orders/batch-accept", json={"tenant": "t1", "batch_id": "x"}).status_code == 400
    assert client.post("/orders/batch-accept?batch_id=x", content=HEADER.encode(),
                       headers={"content-type": "text/csv"}).status_code == 400


# ---------- 租户隔离 ----------

def test_cross_tenant_batch_access_is_not_found() -> None:
    post_batch("t1", "batch-iso", csv_text(["t1,bi-1,100,CNY"]))
    assert client.get("/batches/batch-iso", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/batches/batch-iso").status_code == 400  # 缺租户头

    # 相同 batch_id 在另一租户是独立批次，读不到、也影响不到 t1 的批次。
    r = post_batch("t2", "batch-iso", csv_text(["t2,bi-2,100,USD"]))
    assert r.status_code == 201 and r.json()["accepted_order_ids"] == ["bi-2"]
    t1 = client.get("/batches/batch-iso", headers=H1).json()
    assert t1["accepted_order_ids"] == ["bi-1"]
    assert client.get("/orders/bi-1", headers=H1).status_code == 200
    assert client.get("/orders/bi-1", headers={"X-Tenant": "t2"}).status_code == 404


# ---------- 中断续跑 ----------

def test_resume_after_crash_matches_one_shot_run(monkeypatch) -> None:
    rows = [f"t1,rc-{n},{100 + n},CNY" for n in range(5)]
    data = csv_text(rows)

    # 第 3 个成功行在自身事务提交之后模拟进程中断：已提交行保留，请求失败。
    real_record = batches.record_success
    calls = {"n": 0}

    def crash_after_commit(tenant, batch_id, line_no, order_id, amount_cents, currency):
        real_record(tenant, batch_id, line_no, order_id, amount_cents, currency)
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash after commit")

    monkeypatch.setattr(batch_import.batches, "record_success", crash_after_commit)
    crashed = quiet_client.post("/orders/batch-accept",
                                json={"tenant": "t1", "batch_id": "batch-rc", "csv": data})
    assert crashed.status_code == 500
    monkeypatch.undo()

    mid = client.get("/batches/batch-rc", headers=H1).json()
    assert mid["status"] == "in_progress" and mid["succeeded"] == 3 and mid["failed"] == 0
    assert len(mid["accepted_order_ids"]) == 3

    # 用同一 batch_id 与同一份输入重新提交，从断点继续。
    resumed = post_batch("t1", "batch-rc", data)
    assert resumed.status_code == 201
    body = resumed.json()
    assert body["status"] == "completed"
    assert body["total"] == 5 and body["succeeded"] == 5 and body["failed"] == 0
    assert body["accepted_order_ids"] == [f"rc-{n}" for n in range(5)]

    # 每行最多一张订单，结果与一次连续处理一致。
    conn = sqlite3.connect(db_path())
    for n in range(5):
        count = conn.execute("SELECT COUNT(*) FROM orders WHERE tenant='t1' AND order_id=?",
                             (f"rc-{n}",)).fetchone()[0]
        assert count == 1
    line_count = conn.execute(
        "SELECT COUNT(*) FROM batch_orders WHERE tenant='t1' AND batch_id='batch-rc'"
    ).fetchone()[0]
    conn.close()
    assert line_count == 5
    # 终态后再次重放不再改变任何东西。
    assert post_batch("t1", "batch-rc", data).status_code == 200


def test_crash_before_commit_leaves_no_half_document(monkeypatch) -> None:
    rows = [f"t1,rh-{n},{100 + n},CNY" for n in range(3)]
    data = csv_text(rows)

    real_record = batches.record_success
    calls = {"n": 0}

    def crash_before_commit(tenant, batch_id, line_no, order_id, amount_cents, currency):
        calls["n"] += 1
        if calls["n"] == 2:
            # 模拟事务提交前失败：行与订单都不得落库。
            raise RuntimeError("simulated crash before commit")
        real_record(tenant, batch_id, line_no, order_id, amount_cents, currency)

    monkeypatch.setattr(batch_import.batches, "record_success", crash_before_commit)
    crashed = quiet_client.post("/orders/batch-accept",
                                json={"tenant": "t1", "batch_id": "batch-rh", "csv": data})
    assert crashed.status_code == 500
    monkeypatch.undo()

    conn = sqlite3.connect(db_path())
    order_exists = conn.execute("SELECT 1 FROM orders WHERE order_id='rh-1'").fetchone()
    line_exists = conn.execute(
        "SELECT 1 FROM batch_orders WHERE batch_id='batch-rh' AND order_id='rh-1'"
    ).fetchone()
    conn.close()
    assert order_exists is None and line_exists is None  # 无半张单据

    body = post_batch("t1", "batch-rh", data).json()
    assert body["status"] == "completed" and body["succeeded"] == 3


# ---------- 真实 HTTP 服务：中断（杀进程）后续跑 ----------
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
    import httpx
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


def test_concurrent_same_batch_accepts_each_order_once() -> None:
    """同一批次身份并发提交：批次只创建一次，每行最多一张订单，结果与单次处理一致。"""
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    db_file = os.path.join(tempfile.mkdtemp(), "conc-batch.sqlite")
    port = _free_port()
    proc = _start_server(db_file, port)
    base = f"http://127.0.0.1:{port}"
    data = (
        "tenant,order_id,amount_cents,currency\n"
        + "\n".join(f"t1,cc-{n},100,CNY" for n in range(10))
        + "\n"
    )
    try:
        payload = {"tenant": "t1", "batch_id": "batch-cc", "csv": data}
        with ThreadPoolExecutor(max_workers=12) as ex:
            responses = list(ex.map(
                lambda _: httpx.post(base + "/orders/batch-accept", json=payload, timeout=20),
                range(12)))
        statuses = [r.status_code for r in responses]
        assert 201 in statuses and all(s in (200, 201) for s in statuses), statuses
        bodies = [r.json() for r in responses]
        assert all(b["succeeded"] == 10 and b["failed"] == 0 and b["total"] == 10 for b in bodies)
        assert all(b["accepted_order_ids"] == [f"cc-{n}" for n in range(10)] for b in bodies)

        conn = sqlite3.connect(db_file)
        n_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE tenant='t1' AND order_id LIKE 'cc-%'"
        ).fetchone()[0]
        n_batches = conn.execute(
            "SELECT COUNT(*) FROM batches WHERE tenant='t1' AND batch_id='batch-cc'"
        ).fetchone()[0]
        n_lines = conn.execute(
            "SELECT COUNT(*) FROM batch_orders WHERE tenant='t1' AND batch_id='batch-cc'"
        ).fetchone()[0]
        conn.close()
        assert n_orders == 10 and n_batches == 1 and n_lines == 10
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_resume_across_process_restart() -> None:
    """处理中途服务停止：已提交行不回滚，用同 batch_id + 同一份输入跨进程从断点继续，终态一致。"""
    import hashlib

    import httpx
    db_file = os.path.join(tempfile.mkdtemp(), "resume.sqlite")
    port = _free_port()
    csv_data = (
        "tenant,order_id,amount_cents,currency\n"
        + "\n".join(f"t1,rs-{n},100,CNY" for n in range(6))
        + "\n"
    ).encode()

    # 先启动服务完成建表迁移，然后直接在库中构造“处理到第 3 行后进程被杀”的现场：
    # 批次头 in_progress、前 3 行已提交（订单 + 行结果），后 3 行完全无痕迹。
    proc = _start_server(db_file, port)
    proc.terminate()
    proc.wait(timeout=10)
    conn = sqlite3.connect(db_file)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO batches(tenant, batch_id, total, fingerprint, status) VALUES(?,?,?,?,'in_progress')",
        ("t1", "batch-rs", 6, hashlib.sha256(csv_data).hexdigest()),
    )
    for n in range(3):
        line_no = n + 2  # 物理行号：表头占第 1 行
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
            "VALUES(?,?,?,0,'CNY','accepted')",
            ("t1", f"rs-{n}", 100),
        )
        conn.execute(
            "INSERT INTO batch_orders(tenant, batch_id, line_no, order_id, outcome, error_code, error_message, "
            "accepted_order_id) VALUES(?,?,?,?, 'success', NULL, NULL, ?)",
            ("t1", "batch-rs", line_no, f"rs-{n}", f"rs-{n}"),
        )
    conn.commit()
    conn.close()

    # 重启服务，用同一 batch_id 与同一份输入重新提交，从断点继续后 3 行。
    base = f"http://127.0.0.1:{port}"
    proc = _start_server(db_file, port)
    try:
        with httpx.Client(timeout=10) as c:
            resumed = c.post(base + "/orders/batch-accept",
                             json={"tenant": "t1", "batch_id": "batch-rs", "csv": csv_data.decode()})
            assert resumed.status_code == 201, resumed.text
            body = resumed.json()
            assert body["status"] == "completed"
            assert body["total"] == 6 and body["succeeded"] == 6 and body["failed"] == 0
            assert body["accepted_order_ids"] == [f"rs-{n}" for n in range(6)]

            # 换一份输入续跑进行中批次会被拒绝；终态后重放只返回既有结果。
            other = c.post(base + "/orders/batch-accept",
                           json={"tenant": "t1", "batch_id": "batch-rs",
                                 "csv": "tenant,order_id,amount_cents,currency\nt1,rs-x,1,CNY\n"})
            assert other.status_code == 200 and other.json() == body
        conn = sqlite3.connect(db_file)
        n_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE tenant='t1' AND order_id LIKE 'rs-%'"
        ).fetchone()[0]
        n_lines = conn.execute(
            "SELECT COUNT(*) FROM batch_orders WHERE tenant='t1' AND batch_id='batch-rs'"
        ).fetchone()[0]
        conn.close()
        assert n_orders == 6 and n_lines == 6  # 计数闭合，每行最多一张订单
    finally:
        proc.terminate()
        proc.wait(timeout=10)
