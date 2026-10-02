import argparse
import json

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import account_entries, batches, orders, payment_rollbacks, reconciliations, refunds
from app.store.db import connect, migrate
from app.store.reconciliations import InvalidScope
from app.store.refunds import ConflictError, OrderNotFound
from app.usecase import batch_import
from app.usecase.batch_import import CsvFormatError

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.get("/orders/{order_id}/account-entries")
def read_account_entries(
    order_id: str, request: Request, x_tenant: str = Header(default="")
) -> dict:
    # 订单账务流水查询：租户由 X-Tenant 声明，跨租户一律按订单不存在处理，不泄漏对象是否存在。
    tenant = _require_tenant(x_tenant)
    q = request.query_params

    # 每页大小由调用方显式指定，必须为正整数。
    page_size_raw = q.get("page_size")
    if not _non_empty_str(page_size_raw):
        raise HTTPException(status_code=400, detail="page_size is required and must be a positive integer")
    page_size = _parse_positive_int(page_size_raw, "page_size")

    # cursor 为上一页最后一条流水的顺序位置（seq），返回其之后的流水；首页不传。
    cursor = _parse_positive_int(q.get("cursor"), "cursor") if _non_empty_str(q.get("cursor")) else None

    try:
        result = account_entries.list_for_order(tenant, order_id, cursor=cursor, limit=page_size)
    except account_entries.InvalidCursor:
        raise HTTPException(
            status_code=400,
            detail="cursor does not point to an account entry of this order",
        )
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    entries, next_cursor = result
    return {
        "entries": entries,
        "page_size": page_size,
        "next_cursor": next_cursor,
        "has_next": next_cursor is not None,
    }

@app.get("/orders")
def search_orders(request: Request, x_tenant: str = Header(default="")) -> dict:
    # 条件检索入口：租户仍由 X-Tenant 声明，所有过滤都强制限定在本租户内，
    # 跨租户检索一律得到空结果，无法据此判断其他租户是否存在匹配订单。
    tenant = _require_tenant(x_tenant)
    q = request.query_params

    status = _optional_str(q.get("status"))
    if status is not None and status not in orders.SEARCHABLE_STATUSES:
        raise HTTPException(status_code=400, detail="status must be 'accepted' or 'settled'")

    amount_min = _parse_bound(q.get("amount_min"), "amount_min")
    amount_max = _parse_bound(q.get("amount_max"), "amount_max")
    outstanding_min = _parse_bound(q.get("outstanding_min"), "outstanding_min")
    outstanding_max = _parse_bound(q.get("outstanding_max"), "outstanding_max")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(status_code=400, detail="amount_min must not be greater than amount_max")
    if outstanding_min is not None and outstanding_max is not None and outstanding_min > outstanding_max:
        raise HTTPException(
            status_code=400,
            detail="outstanding_min must not be greater than outstanding_max",
        )

    # 每页大小由调用方显式指定，必须为正整数。
    page_size_raw = q.get("page_size")
    if not _non_empty_str(page_size_raw):
        raise HTTPException(status_code=400, detail="page_size is required and must be a positive integer")
    page_size = _parse_positive_int(page_size_raw, "page_size")

    cursor = _optional_str(q.get("cursor"))

    try:
        page, total, next_cursor = orders.search(
            tenant,
            status=status,
            amount_min=amount_min,
            amount_max=amount_max,
            outstanding_min=outstanding_min,
            outstanding_max=outstanding_max,
            cursor=cursor,
            limit=page_size,
        )
    except orders.InvalidCursor:
        # 游标指向本租户不存在的订单：参数错误，与内部错误区分。
        raise HTTPException(status_code=400, detail="cursor does not point to an order of this tenant")
    return {
        "orders": page,
        "total": total,
        "page_size": page_size,
        "next_cursor": next_cursor,
        "has_next": next_cursor is not None,
    }

def _optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None

def _parse_bound(value: str | None, name: str) -> int | None:
    # 区间端点可缺省；给出时必须是非负整数（含边界，0 合法），浮点/负数/非数字一律参数错误。
    if not _non_empty_str(value):
        return None
    return _parse_non_negative_int(value, name)

def _parse_non_negative_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be a non-negative integer")
    if isinstance(parsed, bool) or parsed < 0:
        raise HTTPException(status_code=400, detail=f"{name} must be a non-negative integer")
    return parsed

def _parse_positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be a positive integer")
    if isinstance(parsed, bool) or parsed <= 0:
        raise HTTPException(status_code=400, detail=f"{name} must be a positive integer")
    return parsed

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/batch-accept", status_code=201)
async def accept_orders_batch(request: Request, response: Response) -> dict:
    # 两种导入形态：application/json 携带 csv 文本字段；其余 Content-Type 直接上传 CSV 字节，
    # 租户与批次标识通过查询参数 tenant、batch_id 声明。
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await _json_body(request)
        tenant = body.get("tenant")
        batch_id = body.get("batch_id")
        csv_text = body.get("csv")
        if not (_non_empty_str(tenant) and _non_empty_str(batch_id)):
            raise HTTPException(status_code=400, detail="tenant and batch_id are required")
        if not _non_empty_str(csv_text):
            raise HTTPException(status_code=400, detail="csv is required")
        raw = csv_text.encode("utf-8")
    else:
        tenant = request.query_params.get("tenant", "")
        batch_id = request.query_params.get("batch_id", "")
        if not (_non_empty_str(tenant) and _non_empty_str(batch_id)):
            raise HTTPException(status_code=400, detail="tenant and batch_id query parameters are required")
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="csv body is required")

    try:
        result, processed = batch_import.accept_batch(tenant, batch_id, raw)
    except CsvFormatError as error:
        # CSV 格式非法：整批拒绝，不受理任何行。
        raise HTTPException(status_code=400, detail=str(error))
    except batches.InputMismatch as error:
        raise HTTPException(status_code=409, detail=str(error))
    if not processed:
        # 同一批次身份重试/重放：不重复受理，返回既有结果（HTTP 200）。
        response.status_code = 200
    return result

@app.get("/batches/{batch_id}")
def read_batch(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    result = batches.get(tenant, batch_id)
    if result is None:
        # 跨租户查询一律按不存在处理，不泄漏批次是否存在。
        raise HTTPException(status_code=404, detail="batch not found")
    return result

def _require_tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return body

def _non_empty_str(value) -> bool:
    return isinstance(value, str) and len(value) > 0

def _positive_int(value) -> bool:
    # 排除 bool：True/False 在 Python 中是 int 子类，不是合法金额。
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

@app.post("/refunds", status_code=201)
async def register_refund(request: Request, response: Response) -> dict:
    body = await _json_body(request)
    tenant = body.get("tenant")
    refund_id = body.get("refund_id")
    order_id = body.get("order_id")
    amount_cents = body.get("amount_cents")
    reason = body.get("reason")
    if not (_non_empty_str(tenant) and _non_empty_str(refund_id) and _non_empty_str(order_id)):
        raise HTTPException(status_code=400, detail="tenant, refund_id and order_id are required")
    if not _non_empty_str(reason):
        raise HTTPException(status_code=400, detail="reason is required")
    if not _positive_int(amount_cents):
        raise HTTPException(status_code=400, detail="amount_cents must be a positive integer")

    try:
        refund, created = refunds.register(tenant, refund_id, order_id, amount_cents, reason)
    except OrderNotFound:
        # 订单不存在或不属于本租户统一按参数错误处理，不泄漏订单是否存在。
        raise HTTPException(status_code=400, detail="order not found")
    except ConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if not created:
        # 同一业务身份重复登记：不新建单据、不重复退款，返回既有单据及当前状态。
        response.status_code = 200
    return refund

@app.get("/refunds/{refund_id}")
def read_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    refund = refunds.get(tenant, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/review")
async def review_refund(refund_id: str, request: Request, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    body = await _json_body(request)
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    try:
        refund = refunds.review(tenant, refund_id, decision == "approve")
    except ConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/reverse")
def reverse_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        refund = refunds.reverse(tenant, refund_id)
    except ConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/payment-rollbacks", status_code=201)
async def register_payment_rollback(request: Request, response: Response) -> dict:
    body = await _json_body(request)
    tenant = body.get("tenant")
    rollback_id = body.get("rollback_id")
    order_id = body.get("order_id")
    amount_cents = body.get("amount_cents")
    if not (_non_empty_str(tenant) and _non_empty_str(rollback_id) and _non_empty_str(order_id)):
        raise HTTPException(status_code=400, detail="tenant, rollback_id and order_id are required")
    if not _positive_int(amount_cents):
        raise HTTPException(status_code=400, detail="amount_cents must be a positive integer")

    try:
        rollback, created = payment_rollbacks.register(tenant, rollback_id, order_id, amount_cents)
    except payment_rollbacks.OrderNotFound:
        # 订单不存在或不属于本租户统一按参数错误处理，不泄漏订单是否存在。
        raise HTTPException(status_code=400, detail="order not found")
    except payment_rollbacks.InvalidAmountError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except payment_rollbacks.ConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if not created:
        # 同一业务身份重复请求：不新建回退、不重复退回金额，返回既有回退结果。
        response.status_code = 200
    return rollback

@app.get("/payment-rollbacks/{rollback_id}")
def read_payment_rollback(rollback_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    rollback = payment_rollbacks.get(tenant, rollback_id)
    if rollback is None:
        raise HTTPException(status_code=404, detail="payment rollback not found")
    return rollback

@app.post("/reconciliations", status_code=201)
async def create_reconciliation(request: Request, response: Response) -> dict:
    body = await _json_body(request)
    tenant = body.get("tenant")
    reconcile_id = body.get("reconcile_id")
    if not (_non_empty_str(tenant) and _non_empty_str(reconcile_id)):
        raise HTTPException(status_code=400, detail="tenant and reconcile_id are required")

    # 对账范围：可指定单张订单、订单金额区间或未收金额区间（端点含边界、可只给一侧），
    # 多条件同时给出取交集；至少要声明一种范围形态。
    order_id = body.get("order_id")
    if order_id is not None and not _non_empty_str(order_id):
        raise HTTPException(status_code=400, detail="order_id must be a non-empty string")
    amount_min = _reconcile_bound(body.get("amount_min"), "amount_min")
    amount_max = _reconcile_bound(body.get("amount_max"), "amount_max")
    outstanding_min = _reconcile_bound(body.get("outstanding_min"), "outstanding_min")
    outstanding_max = _reconcile_bound(body.get("outstanding_max"), "outstanding_max")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(status_code=400, detail="amount_min must not be greater than amount_max")
    if outstanding_min is not None and outstanding_max is not None and outstanding_min > outstanding_max:
        raise HTTPException(
            status_code=400,
            detail="outstanding_min must not be greater than outstanding_max",
        )
    scope = {
        "order_id": order_id,
        "amount_min": amount_min,
        "amount_max": amount_max,
        "outstanding_min": outstanding_min,
        "outstanding_max": outstanding_max,
    }
    if not any(value is not None for value in scope.values()):
        raise HTTPException(
            status_code=400,
            detail="a reconciliation scope is required: order_id or amount/outstanding range",
        )

    try:
        statement, created = reconciliations.reconcile(tenant, reconcile_id, scope)
    except InvalidScope:
        # 区间非法已在上方拦截；此处为范围下没有任何订单（含单张订单不存在/非本租户），
        # 统一参数错误、不留对账单，且不泄漏订单是否存在。
        raise HTTPException(status_code=400, detail="reconciliation scope matches no orders")
    if not created:
        # 同一（租户, reconcile_id）重复提交：不重新核对、不改变既有结论，返回既有对账单。
        response.status_code = 200
    return statement

@app.get("/reconciliations/{reconcile_id}")
def read_reconciliation(reconcile_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    statement = reconciliations.get(tenant, reconcile_id)
    if statement is None:
        # 不存在或跨租户一律 404，不泄漏对账单是否存在。
        raise HTTPException(status_code=404, detail="reconciliation not found")
    return statement

def _reconcile_bound(value, name: str) -> int | None:
    # 范围端点可缺省；给出时必须是非负整数（排除 bool 这类 int 子类），否则参数错误。
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HTTPException(status_code=400, detail=f"{name} must be a non-negative integer")
    return value

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
