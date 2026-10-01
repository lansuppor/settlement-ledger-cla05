import argparse
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, refunds
from app.store.db import connect, migrate
from app.store.refunds import RefundConflict

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    # 宽容模型：字段缺失与类型非法统一由入口判为 400，避免 422 语义分叉。
    tenant: Any = None
    refund_id: Any = None
    order_id: Any = None
    amount_cents: Any = None
    reason: Any = None

class ReviewIn(BaseModel):
    decision: Any = None

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

@app.post("/refunds", status_code=201)
def register_refund(body: RefundIn, response: Response) -> dict:
    # 参数错误（400）：字段缺失/类型不对、金额非正整数、原因缺失，先于一切业务判断。
    if not isinstance(body.tenant, str) or not body.tenant.strip():
        raise HTTPException(status_code=400, detail="tenant is required")
    if not isinstance(body.refund_id, str) or not body.refund_id.strip():
        raise HTTPException(status_code=400, detail="refund_id is required")
    if not isinstance(body.order_id, str) or not body.order_id.strip():
        raise HTTPException(status_code=400, detail="order_id is required")
    if not isinstance(body.amount_cents, int) or isinstance(body.amount_cents, bool) or body.amount_cents <= 0:
        raise HTTPException(status_code=400, detail="amount_cents must be a positive integer")
    if not isinstance(body.reason, str) or not body.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required")
    try:
        refund, created = refunds.register(
            body.tenant.strip(), body.refund_id.strip(), body.order_id.strip(),
            body.amount_cents, body.reason.strip(),
        )
    except RefundConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    # 不存在或非本租户订单：按参数错误处理，不泄漏订单是否存在。
    if refund is None:
        raise HTTPException(status_code=400, detail="order not found")
    if not created:
        # 业务身份幂等：重复登记不新建、不重复退款，返回既有单与当前状态（200）。
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
def review_refund(refund_id: str, body: ReviewIn, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")
    try:
        refund = refunds.review(tenant, refund_id, body.decision == "approve")
    except RefundConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if refund is None:
        # 跨租户与不存在同形：一律 404。
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/reverse")
def reverse_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    try:
        refund = refunds.reverse(tenant, refund_id)
    except RefundConflict as error:
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
