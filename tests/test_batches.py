import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))

import pytest
from fastapi.testclient import TestClient

from app.entry import app
from app.store import batches, orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

HEADER = "tenant,order_id,amount_cents,currency\n"


def csv_payload(tenant: str, batch_id: str, body: str) -> dict:
    return {"tenant": tenant, "batch_id": batch_id, "csv": HEADER + body}


def test_batch_accepts_all_rows_and_orders_readable() -> None:
    body = "bt,a1,100,CNY\nbt,a2,200,USD\n"
    resp = client.post("/batches", json=csv_payload("bt", "B1", body))
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "completed"
    assert data["total"] == 2
    assert data["success_count"] == 2
    assert data["failure_count"] == 0
    assert data["errors"] == []
    assert data["accepted_order_ids"] == ["a1", "a2"]
    # 受理成功的订单沿用既有口径，可按现有接口读取与登记收款。
    got = client.get("/orders/a1", headers={"X-Tenant": "bt"})
    assert got.status_code == 200 and got.json()["amount_cents"] == 100
    paid = client.post("/orders/a1/payments", json={"amount_cents": 100}, headers={"X-Tenant": "bt"})
    assert paid.status_code == 200 and paid.json()["outstanding_cents"] == 0


def test_partial_success_records_errors_with_line_numbers() -> None:
    body = (
        "bt,ok1,500,CNY\n"          # 行号 2：成功
        "bt,,500,CNY\n"             # 行号 3：订单标识为空
        "bt,bad1,0,CNY\n"           # 行号 4：金额非大于 0
        "bt,bad2,12.5,CNY\n"        # 行号 5：金额不是整数
        "bt,bad3,100,XYZ\n"         # 行号 6：币种不支持
        "bx,ok2,100,CNY\n"          # 行号 7：行内租户与请求租户不一致
        "bt,ok3,300,JPY\n"          # 行号 8：成功
    )
    resp = client.post("/batches", json=csv_payload("bt", "B2", body))
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "completed_with_errors"
    assert data["total"] == 7
    assert data["success_count"] == 2
    assert data["failure_count"] == 5
    assert data["success_count"] + data["failure_count"] == data["total"]
    assert data["accepted_order_ids"] == ["ok1", "ok3"]
    line_nos = [error["line_no"] for error in data["errors"]]
    assert line_nos == [3, 4, 5, 6, 7]
    assert all(error["code"] == "invalid_param" for error in data["errors"])
    # 失败行未受理，成功行已受理。
    assert client.get("/orders/bad1", headers={"X-Tenant": "bt"}).status_code == 404
    assert client.get("/orders/ok3", headers={"X-Tenant": "bt"}).status_code == 200


def test_conflict_row_distinguished_and_data_unchanged() -> None:
    # 既有单笔受理订单。
    assert client.post(
        "/orders", json={"tenant": "bt", "order_id": "exist", "amount_cents": 100, "currency": "CNY"}
    ).status_code == 201
    body = (
        "bt,dup,100,CNY\n"      # 成功
        "bt,dup,200,USD\n"      # 批次内重复：冲突
        "bt,exist,999,EUR\n"    # 命中既有订单：冲突，不得改写金额/币种
    )
    resp = client.post("/batches", json=csv_payload("bt", "B3", body))
    data = resp.json()
    assert data["status"] == "completed_with_errors"
    assert data["success_count"] == 1 and data["failure_count"] == 2
    conflicts = [e for e in data["errors"] if e["code"] == "order_conflict"]
    params = [e for e in data["errors"] if e["code"] == "invalid_param"]
    assert {e["line_no"] for e in conflicts} == {3, 4} and params == []
    order = client.get("/orders/exist", headers={"X-Tenant": "bt"}).json()
    assert order["amount_cents"] == 100 and order["currency"] == "CNY"


def test_replay_same_batch_does_not_reaccept() -> None:
    body = "bt,r1,100,CNY\nbt,r2,200,CNY\n"
    first = client.post("/batches", json=csv_payload("bt", "B4", body))
    assert first.status_code == 201
    second = client.post("/batches", json=csv_payload("bt", "B4", body))
    assert second.status_code == 200
    assert second.json() == first.json()
    # 同一行最多一张订单：既有订单仍可单笔 409 证明未被重复受理。
    assert client.post(
        "/orders", json={"tenant": "bt", "order_id": "r1", "amount_cents": 100, "currency": "CNY"}
    ).status_code == 409


def test_batch_identity_independent_of_row_content() -> None:
    first = client.post("/batches", json=csv_payload("bt", "B5", "bt,id1,100,CNY\n"))
    assert first.status_code == 201
    # 相同 batch_id、不同行内容：不新建批次，只返回该批次既有结果。
    second = client.post("/batches", json=csv_payload("bt", "B5", "bt,id2,200,USD\n"))
    assert second.status_code == 200
    assert second.json() == first.json()
    assert client.get("/orders/id2", headers={"X-Tenant": "bt"}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        "bt,a,100,CNY\n",                                  # 缺表头
        "tenant,order_id,amount_cents\nbt,a,100\n",        # 表头列数不对
        HEADER + "bt,a,100\n",                             # 数据行列数不符
        HEADER + 'bt,"a,100,CNY\n',                        # 引号未闭合，结构损坏
    ],
)
def test_malformed_csv_rejects_whole_batch(body: str) -> None:
    resp = client.post("/batches", json={"tenant": "bt", "batch_id": "B-BAD", "csv": body})
    assert resp.status_code == 400
    # 整批拒绝：不受理任何行、不创建批次。
    assert client.get("/batches/B-BAD", headers={"X-Tenant": "bt"}).status_code == 404


def test_invalid_utf8_rejected_for_text_csv() -> None:
    resp = client.post(
        "/batches?tenant=bt&batch_id=B-ENC",
        content=b"\xff\xfe" + HEADER.encode() + b"bt,a,100,CNY\n",
        headers={"Content-Type": "text/csv"},
    )
    assert resp.status_code == 400
    assert client.get("/batches/B-ENC", headers={"X-Tenant": "bt"}).status_code == 404


def test_text_csv_upload_path() -> None:
    raw = (HEADER + "bt,t1,100,CNY\nbt,t2,200,CNY\n").encode("utf-8")
    resp = client.post(
        "/batches?tenant=bt&batch_id=B-RAW", content=raw, headers={"Content-Type": "text/csv"}
    )
    assert resp.status_code == 201
    assert resp.json()["accepted_order_ids"] == ["t1", "t2"]


def test_header_only_csv_completes_with_zero_rows() -> None:
    resp = client.post("/batches", json=csv_payload("bt", "B-EMPTY", ""))
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "completed"
    assert data["total"] == 0 and data["success_count"] == 0 and data["failure_count"] == 0


def test_batch_query_requires_tenant_and_is_isolated() -> None:
    client.post("/batches", json=csv_payload("bt", "B-ISO", "bt,iso,100,CNY\n"))
    assert client.get("/batches/B-ISO").status_code == 400
    # 跨租户查询一律按不存在处理，不泄漏批次是否存在。
    assert client.get("/batches/B-ISO", headers={"X-Tenant": "other"}).status_code == 404
    got = client.get("/batches/B-ISO", headers={"X-Tenant": "bt"})
    assert got.status_code == 200 and got.json()["batch_id"] == "B-ISO"


def _order_ids(tenant: str) -> set[str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT order_id FROM orders WHERE tenant=?", (tenant,)).fetchall()
    finally:
        conn.close()
    return {row["order_id"] for row in rows}


def test_interrupted_batch_resumes_to_same_result() -> None:
    tenant = "bcrash"
    body = (
        "bcrash,c1,100,CNY\n"
        "bcrash,c2,0,CNY\n"          # 失败行
        "bcrash,c3,300,USD\n"
        "bcrash,c4,400,CNY\n"
    )
    csv_text = HEADER + body
    # 前两行提交后模拟服务中断。
    with pytest.raises(RuntimeError):
        batches.process(tenant, "RC", csv_text, crash_after=2)
    checkpoint = client.get("/batches/RC", headers={"X-Tenant": tenant}).json()
    assert checkpoint["status"] == "in_progress" and checkpoint["total"] == 4

    resumed, existed = batches.process(tenant, "RC", csv_text)
    assert existed is True
    assert resumed["status"] == "completed_with_errors"
    assert resumed["total"] == 4
    assert resumed["success_count"] == 3
    assert resumed["failure_count"] == 1
    assert resumed["accepted_order_ids"] == ["c1", "c3", "c4"]
    assert resumed["errors"][0]["line_no"] == 3
    # 计数闭合，且每张订单只产生一次（无重复行）。
    assert _order_ids(tenant) == {"c1", "c3", "c4"}
    # 续跑完成后重放返回同一结果，不再改动。
    replayed, _ = batches.process(tenant, "RC", csv_text)
    assert replayed == resumed
    assert _order_ids(tenant) == {"c1", "c3", "c4"}


def test_resume_matches_one_shot_processing() -> None:
    csv_text = HEADER + "bsame,s1,100,CNY\nbsame,s2,bad,CNY\nbsame,s3,300,CNY\n"
    with pytest.raises(RuntimeError):
        batches.process("bsame", "ONE", csv_text, crash_after=1)
    resumed, _ = batches.process("bsame", "ONE", csv_text)

    onshot, _ = batches.process(
        "bsame2", "ONE", HEADER + "bsame2,s1,100,CNY\nbsame2,s2,bad,CNY\nbsame2,s3,300,CNY\n"
    )
    # 中断后续跑与一次连续处理的计数、状态、错误行号完全一致。
    for key in ("status", "total", "success_count", "failure_count"):
        assert resumed[key] == onshot[key]
    assert [e["line_no"] for e in resumed["errors"]] == [e["line_no"] for e in onshot["errors"]]
    # 干净的存储层无残留半行记录。
    assert orders.get("bsame", "s2") is None
