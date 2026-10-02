import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

TENANT = "search-t"


def _accept(order_id: str, amount: int, tenant: str = TENANT) -> None:
    body = {"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def _pay(order_id: str, amount: int, tenant: str = TENANT) -> None:
    got = client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})
    assert got.status_code == 200


def _search(params: dict, tenant: str = TENANT):
    return client.get("/orders", params=params, headers={"X-Tenant": tenant})


def _ids(page) -> list:
    return [item["order_id"] for item in page["items"]]


def test_filter_by_status() -> None:
    _accept("s-st-1", 100)
    _accept("s-st-2", 200)
    _pay("s-st-2", 200)
    page = _search({"status": "accepted", "page_size": 50}).json()
    assert "s-st-1" in _ids(page) and "s-st-2" not in _ids(page)
    page = _search({"status": "settled", "page_size": 50}).json()
    assert "s-st-2" in _ids(page) and "s-st-1" not in _ids(page)


def test_filter_by_amount_range_inclusive_bounds() -> None:
    _accept("s-am-1", 100)
    _accept("s-am-2", 200)
    _accept("s-am-3", 300)
    page = _search({"amount_min": 200, "amount_max": 300, "page_size": 50}).json()
    ids = _ids(page)
    assert "s-am-1" not in ids and "s-am-2" in ids and "s-am-3" in ids
    # 只给下限 / 只给上限
    assert "s-am-1" not in _ids(_search({"amount_min": 200, "page_size": 50}).json())
    assert "s-am-3" not in _ids(_search({"amount_max": 200, "page_size": 50}).json())


def test_filter_by_outstanding_range() -> None:
    _accept("s-os-1", 500)
    _pay("s-os-1", 200)  # 未收 300
    _accept("s-os-2", 500)  # 未收 500
    page = _search({"outstanding_min": 300, "outstanding_max": 300, "page_size": 50}).json()
    ids = _ids(page)
    assert "s-os-1" in ids and "s-os-2" not in ids
    item = next(i for i in page["items"] if i["order_id"] == "s-os-1")
    assert item["paid_cents"] == 200 and item["outstanding_cents"] == 300


def test_combined_filters_intersect() -> None:
    _accept("s-cb-1", 400)
    _accept("s-cb-2", 400)
    _pay("s-cb-2", 400)
    page = _search({"status": "accepted", "amount_min": 400, "amount_max": 400, "page_size": 50}).json()
    ids = _ids(page)
    assert "s-cb-1" in ids and "s-cb-2" not in ids


def test_invalid_conditions_are_parameter_errors() -> None:
    assert _search({"status": "unknown", "page_size": 10}).status_code == 400
    assert _search({"amount_min": "abc", "page_size": 10}).status_code == 400
    assert _search({"amount_min": -1, "page_size": 10}).status_code == 400
    assert _search({"amount_min": 300, "amount_max": 100, "page_size": 10}).status_code == 400
    assert _search({"outstanding_min": 50, "outstanding_max": 10, "page_size": 10}).status_code == 400
    assert _search({"page_size": 0}).status_code == 400
    assert _search({"page_size": -3}).status_code == 400
    assert _search({"page_size": "x"}).status_code == 400
    assert _search({}).status_code == 400  # page_size 必传
    assert client.get("/orders", params={"page_size": 10}).status_code == 400  # 缺租户头


def test_pagination_walks_full_set_without_dup_or_gap() -> None:
    tenant = "search-page"
    expected = []
    for i in range(25):
        order_id = f"s-pg-{i:03d}"
        _accept(order_id, 100 + i, tenant=tenant)
        expected.append(order_id)

    seen: list = []
    cursor = None
    pages = 0
    totals = set()
    while True:
        params = {"page_size": 7}
        if cursor is not None:
            params["cursor"] = cursor
        page = _search(params, tenant=tenant)
        assert page.status_code == 200
        body = page.json()
        totals.add(body["total"])
        ids = _ids(body)
        assert len(ids) <= 7
        seen.extend(ids)
        pages += 1
        if not body["has_next"]:
            break
        cursor = ids[-1]
    assert seen == expected  # 顺序稳定、不重不漏
    assert totals == {25}  # 总数与页大小、翻页进度无关
    assert pages == 4  # 7+7+7+4
    # 同一组条件重复查询顺序一致
    again = _ids(_search({"page_size": 50}, tenant=tenant).json())
    assert again == expected


def test_cursor_must_reference_existing_order_in_tenant() -> None:
    _accept("s-cur-1", 100)
    assert _search({"page_size": 10, "cursor": "no-such-order"}).status_code == 400
    # 其他租户的订单不能作为本租户游标
    _accept("s-cur-2", 100, tenant="search-other")
    assert _search({"page_size": 10, "cursor": "s-cur-2"}).status_code == 400
    assert _search({"page_size": 10, "cursor": "s-cur-1"}, tenant="search-other").status_code == 400


def test_cross_tenant_search_sees_nothing() -> None:
    _accept("s-ct-1", 100, tenant="search-iso-a")
    page = _search({"page_size": 10}, tenant="search-iso-b")
    assert page.status_code == 200
    assert page.json() == {"items": [], "total": 0, "has_next": False}


def test_search_item_shape_matches_single_read() -> None:
    _accept("s-sh-1", 600)
    _pay("s-sh-1", 250)
    single = client.get("/orders/s-sh-1", headers={"X-Tenant": TENANT}).json()
    page = _search({"page_size": 10, "outstanding_min": 350, "outstanding_max": 350}).json()
    item = next(i for i in page["items"] if i["order_id"] == "s-sh-1")
    assert item == single
    assert item["outstanding_cents"] == item["amount_cents"] - item["paid_cents"]


def test_pages_reflect_latest_committed_writes() -> None:
    tenant = "search-live"
    for i in range(6):
        _accept(f"s-lv-{i}", 1000, tenant=tenant)
    first = _search({"status": "accepted", "page_size": 4}, tenant=tenant).json()
    assert first["total"] == 6 and first["has_next"] is True
    cursor = first["items"][-1]["order_id"]
    # 翻页间隙发生收款：其中一单结清，后续页看到生效后的最新状态。
    _pay("s-lv-4", 1000, tenant=tenant)
    second = _search({"status": "accepted", "page_size": 4, "cursor": cursor}, tenant=tenant).json()
    ids = _ids(second)
    assert "s-lv-4" not in ids and "s-lv-5" in ids
    assert second["total"] == 5
    # 单次连续翻页内每张订单只出现一次
    assert not set(_ids(first)) & set(ids)
