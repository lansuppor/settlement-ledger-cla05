import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))

from fastapi.testclient import TestClient

from app.config import db_path
from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

H = {"X-Tenant": "ts"}


def make_order(oid: str, amount: int = 1000, tenant: str = "ts", paid: int = 0) -> None:
    body = {"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    if paid:
        assert client.post(f"/orders/{oid}/payments", json={"amount_cents": paid},
                           headers={"X-Tenant": tenant}).status_code == 200


def search(tenant: str = "ts", **params):
    return client.get("/orders", headers={"X-Tenant": tenant}, params=params)


def fetch_all(tenant: str = "ts", page_size: int = 5, **filters) -> list[str]:
    """用游标逐页取完全部匹配订单，返回订单标识序列，并校验每页 total 恒定。"""
    ids: list[str] = []
    totals: list[int] = []
    cursor = None
    while True:
        params = {**filters, "page_size": page_size}
        if cursor is not None:
            params["cursor"] = cursor
        resp = search(tenant, **params)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        ids.extend(o["order_id"] for o in data["orders"])
        totals.append(data["total"])
        if not data["has_next"]:
            assert data["next_cursor"] is None
            assert len(set(totals)) == 1, f"total changed across pages: {totals}"
            assert totals[0] >= len(ids)
            return ids
        cursor = data["next_cursor"]


# ---------- 基础过滤与排序 ----------

def test_orders_sorted_by_id_and_tenant_scoped() -> None:
    for oid in ("s3", "s1", "s2"):
        make_order(oid, 100)
    make_order("x1", 100, tenant="other")
    resp = search(page_size=50)
    assert resp.status_code == 200
    ids = [o["order_id"] for o in resp.json()["orders"] if o["order_id"].startswith("s")]
    assert ids == sorted(ids)
    # 跨租户检索结果中绝不出现其他租户订单。
    all_ids = [o["order_id"] for o in resp.json()["orders"]]
    assert "x1" not in all_ids
    other = search("other", page_size=50).json()
    assert [o["order_id"] for o in other["orders"]] == ["x1"]


def test_filter_by_status() -> None:
    make_order("st-open", 500)
    make_order("st-done", 500, paid=500)
    assert [o["order_id"] for o in search(page_size=50, status="accepted").json()["orders"]
            if o["order_id"].startswith("st-")] == ["st-open"]
    assert [o["order_id"] for o in search(page_size=50, status="settled").json()["orders"]
            if o["order_id"].startswith("st-")] == ["st-done"]


def test_filter_amount_range_inclusive_bounds_and_intersection() -> None:
    make_order("am100", 100)
    make_order("am200", 200)
    make_order("am300", 300)
    got = {o["order_id"] for o in search(page_size=50, amount_min=200, amount_max=200).json()["orders"]}
    assert {"am200"} <= got and "am100" not in got and "am300" not in got
    got = {o["order_id"] for o in search(page_size=50, amount_min=150, amount_max=250).json()["orders"]}
    assert "am200" in got and "am100" not in got and "am300" not in got
    # 只给上限/下限。
    assert "am300" in {o["order_id"] for o in search(page_size=50, amount_min=300).json()["orders"]}
    assert "am100" in {o["order_id"] for o in search(page_size=50, amount_max=100).json()["orders"]}


def test_filter_outstanding_uses_net_amount_semantics() -> None:
    # 收清后批准一笔退款：净已收下降、未收回升，状态回到 accepted；口径与订单读取一致。
    make_order("os1", 1000, paid=1000)
    assert client.post("/refunds", json={
        "tenant": "ts", "refund_id": "os1-rf", "order_id": "os1",
        "amount_cents": 300, "reason": "broken"}).status_code == 201
    assert client.post("/refunds/os1-rf/review", headers=H,
                       json={"decision": "approve"}).status_code == 200
    order = client.get("/orders/os1", headers=H).json()
    assert order["paid_cents"] == 700 and order["outstanding_cents"] == 300
    got = {o["order_id"] for o in search(page_size=50, outstanding_min=300, outstanding_max=300).json()["orders"]}
    assert "os1" in got
    # 未收为 0 的已结清订单不被未收区间命中。
    make_order("os2", 1000, paid=1000)
    rows = search(page_size=50, outstanding_max=0, status="settled").json()["orders"]
    assert any(o["order_id"] == "os2" for o in rows)
    assert all(o["order_id"] != "os1" for o in rows)


def test_empty_result_does_not_leak() -> None:
    data = search("nobody-tenant", page_size=10).json()
    assert data == {"orders": [], "total": 0, "page_size": 10, "next_cursor": None, "has_next": False}


# ---------- 分页：不重不漏、顺序与总数 ----------

def test_keyset_pagination_matches_full_scan() -> None:
    for i in range(200):
        make_order(f"pg{i:04d}", 1000)
    full = [o["order_id"] for o in search(page_size=1000).json()["orders"] if o["order_id"].startswith("pg")]
    for size in (1, 7, 50, 200, 500):
        paged = [oid for oid in fetch_all(page_size=size) if oid.startswith("pg")]
        assert paged == full  # 顺序一致、集合一致：不重复、不遗漏。
        assert len(paged) == len(set(paged)) == 200


def test_total_is_independent_of_page_size_and_has_next_boundary() -> None:
    for i in range(20):
        make_order(f"tb{i:03d}", 100)
    expected = {o["order_id"] for o in search(page_size=1000).json()["orders"] if o["order_id"].startswith("tb")}
    for size in (3, 10, 20, 21):
        resp = search(page_size=size)
        assert resp.json()["total"] >= len(expected)
    # 整除边界：最后一页恰好取完时 has_next=False、next_cursor=None。
    filtered_total = search(page_size=100, amount_min=100, amount_max=100).json()["total"]
    ids = fetch_all(page_size=filtered_total, amount_min=100, amount_max=100)
    assert len(ids) == filtered_total


def test_filtered_pagination_stable_set() -> None:
    for i in range(60):
        make_order(f"fp{i:03d}", amount=100 + i)  # 100..159
    full = {oid for oid in (o["order_id"] for o in search(
        page_size=1000, amount_min=120, amount_max=150).json()["orders"]) if oid.startswith("fp")}
    paged = {oid for oid in fetch_all(page_size=8, amount_min=120, amount_max=150) if oid.startswith("fp")}
    first = search(page_size=8, amount_min=120, amount_max=150).json()["total"]
    assert paged == full and len(full) == 31 and first == 31


# ---------- 参数错误 ----------

def test_invalid_parameters_return_400() -> None:
    assert search(page_size=0).status_code == 400
    assert search(page_size=-3).status_code == 400
    assert search(page_size="abc").status_code == 400  # type: ignore[arg-type]
    assert search(page_size=1, status="paid").status_code == 400
    assert search(page_size=1, amount_min=-1).status_code == 400
    assert search(page_size=1, amount_min=1.5).status_code == 400
    assert search(page_size=1, amount_max="x").status_code == 400
    assert search(page_size=1, outstanding_min=200, outstanding_max=100).status_code == 400
    assert search(page_size=1, amount_min=200, amount_max=100).status_code == 400


def test_missing_page_size_and_tenant_header_are_400() -> None:
    assert client.get("/orders", headers=H).status_code == 400
    assert client.get("/orders", params={"page_size": 10}).status_code == 400


def test_cursor_must_point_to_order_of_same_tenant() -> None:
    make_order("cur-own", 100)
    make_order("cur-other", 100, tenant="other")
    assert search(page_size=10, cursor="cur-own").status_code == 200
    # 不存在的游标、指向其他租户订单的游标，一律参数错误且不泄漏是否存在。
    assert search(page_size=10, cursor="cur-missing").status_code == 400
    assert search(page_size=10, cursor="cur-other").status_code == 400


# ---------- 并发写入下的读取一致性 ----------

def test_read_does_not_see_uncommitted_half_write() -> None:
    make_order("cw1", 100)
    # 另起连接拿到写锁并改状态但不提交：检索仍读到已提交快照，不被阻塞、不看到中间态。
    writer = sqlite3.connect(db_path())
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE orders SET status='settled' WHERE order_id='cw1'")

        def read() -> list[str]:
            rows = search(page_size=10, status="settled").json()["orders"]
            return [o["order_id"] for o in rows]

        with ThreadPoolExecutor(max_workers=1) as pool:
            seen = pool.submit(read).result(timeout=10)
        assert "cw1" not in seen
        writer.execute("COMMIT")
    finally:
        writer.close()
    seen = {o["order_id"] for o in search(page_size=10, status="settled").json()["orders"]}
    assert "cw1" in seen


def test_paging_under_concurrent_writes_never_duplicates() -> None:
    for i in range(100):
        make_order(f"cc{i:04d}", 1000)

    def keep_paying() -> None:
        # 翻页期间持续把订单逐笔收清：状态在页与页之间允许变化，但每张订单在整轮翻页中最多出现一次。
        for i in range(0, 100, 2):
            client.post(f"/orders/cc{i:04d}/payments", json={"amount_cents": 1000}, headers=H)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(keep_paying)
        collected: list[str] = []
        cursor = None
        for _ in range(100):
            params = {"page_size": 6}
            if cursor is not None:
                params["cursor"] = cursor
            resp = search(**params)
            assert resp.status_code == 200
            data = resp.json()
            collected.extend(o["order_id"] for o in data["orders"])
            if not data["has_next"]:
                break
            cursor = data["next_cursor"]
        future.result(timeout=30)
    cc = [oid for oid in collected if oid.startswith("cc")]
    assert len(cc) == len(set(cc))  # 不重；订单标识不变，游标按序前进故不漏已定位的页边界。
