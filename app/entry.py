import argparse
import json

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import batches, orders, refunds
from app.store.db import connect, migrate
from app.store.refunds import ConflictError, OrderNotFound

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


@app.post("/batches", status_code=201)
async def create_batch(request: Request, response: Response) -> dict:
    """批量受理订单。支持两种提交方式：

    - JSON：{"tenant","batch_id","csv"}，csv 为 CSV 文本；
    - text/csv 原始请求体：tenant、batch_id 通过查询参数传入。
    批次以（租户, batch_id）为业务身份；重放不重复受理，只返回既有结果。
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "text/csv":
        tenant = request.query_params.get("tenant", "")
        batch_id = request.query_params.get("batch_id", "")
        raw = await request.body()
        try:
            csv_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="invalid CSV encoding: expected UTF-8")
    else:
        body = await _json_body(request)
        tenant = body.get("tenant")
        batch_id = body.get("batch_id")
        csv_text = body.get("csv")
        if not (_non_empty_str(tenant) and _non_empty_str(batch_id)):
            raise HTTPException(status_code=400, detail="tenant and batch_id are required")
        if not isinstance(csv_text, str):
            raise HTTPException(status_code=400, detail="csv text is required")
    try:
        result, existed = batches.process(tenant, batch_id, csv_text)
    except batches.CSVFormatError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if existed:
        # 同一批次身份重放：不新建批次、不重复受理，返回既有结果（HTTP 200）。
        response.status_code = 200
    return result


@app.get("/batches/{batch_id}")
def read_batch(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    result = batches.get_result(tenant, batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return result


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
