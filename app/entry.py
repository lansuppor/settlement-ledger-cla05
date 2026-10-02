import argparse
import json
import re

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import batches, orders, payment_rollbacks, refunds
from app.store.db import connect, migrate
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

ORDER_STATUSES = ("accepted", "settled")

@app.get("/orders")
def search_orders(
    status: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    outstanding_min: str | None = None,
    outstanding_max: str | None = None,
    page_size: str | None = None,
    cursor: str | None = None,
    x_tenant: str = Header(default=""),
) -> dict:
    # 订单条件检索：状态/订单金额/未收金额区间取交集，按订单标识升序游标分页。
    tenant = _require_tenant(x_tenant)
    if status is not None and status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="status must be 'accepted' or 'settled'")
    amount_lo = _amount_bound(amount_min, "amount_min")
    amount_hi = _amount_bound(amount_max, "amount_max")
    if amount_lo is not None and amount_hi is not None and amount_lo > amount_hi:
        raise HTTPException(status_code=400, detail="amount_min must not exceed amount_max")
    outstanding_lo = _amount_bound(outstanding_min, "outstanding_min")
    outstanding_hi = _amount_bound(outstanding_max, "outstanding_max")
    if outstanding_lo is not None and outstanding_hi is not None and outstanding_lo > outstanding_hi:
        raise HTTPException(status_code=400, detail="outstanding_min must not exceed outstanding_max")
    if page_size is None:
        raise HTTPException(status_code=400, detail="page_size is required")
    size = _positive_int_param(page_size, "page_size")
    if cursor is not None and cursor != "" and orders.get(tenant, cursor) is None:
        # 游标必须指向本租户已存在的订单，否则按参数错误处理（不泄漏其他租户）。
        raise HTTPException(status_code=400, detail="cursor does not reference an existing order")
    items, total, has_next = orders.search(
        tenant,
        status=status,
        amount_min=amount_lo,
        amount_max=amount_hi,
        outstanding_min=outstanding_lo,
        outstanding_max=outstanding_hi,
        limit=size,
        cursor=cursor or None,
    )
    return {"items": items, "total": total, "has_next": has_next}

def _amount_bound(raw: str | None, name: str) -> int | None:
    # 区间端点：非负的最小货币单位整数，含边界；非法取值按参数错误处理。
    if raw is None:
        return None
    if re.fullmatch(r"[0-9]+", raw.strip()) is None:
        raise HTTPException(status_code=400, detail=f"{name} must be a non-negative integer")
    return int(raw)

def _positive_int_param(raw: str, name: str) -> int:
    if re.fullmatch(r"[0-9]+", raw.strip()) is None or int(raw) <= 0:
        raise HTTPException(status_code=400, detail=f"{name} must be a positive integer")
    return int(raw)

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

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
