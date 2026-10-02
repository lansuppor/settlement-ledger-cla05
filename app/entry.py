import argparse
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, refunds
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    refund_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    request_id: str = Field(min_length=1)

class RefundAdvanceIn(BaseModel):
    action: Literal["complete", "cancel"]
    request_id: str = Field(min_length=1)

class RefundReverseIn(BaseModel):
    request_id: str = Field(min_length=1)

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

@app.post("/orders/{order_id}/refunds", status_code=201)
def accept_refund(order_id: str, body: RefundIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        refund = refunds.accept(x_tenant, order_id, body.refund_id, body.amount_cents, body.request_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if refund is None:
        raise HTTPException(status_code=404, detail="order not found")
    return refund

@app.get("/orders/{order_id}/refunds")
def list_refunds(order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    items = refunds.list_by_order(x_tenant, order_id)
    if items is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "refunds": items}

@app.get("/orders/{order_id}/refunds/{refund_id}")
def read_refund(order_id: str, refund_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    refund = refunds.get(x_tenant, order_id, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/orders/{order_id}/refunds/{refund_id}/advance")
def advance_refund(order_id: str, refund_id: str, body: RefundAdvanceIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        refund = refunds.advance(x_tenant, order_id, refund_id, body.action, body.request_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/orders/{order_id}/refunds/{refund_id}/reverse")
def reverse_refund(order_id: str, refund_id: str, body: RefundReverseIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        refund = refunds.reverse(x_tenant, order_id, refund_id, body.request_id)
    except ValueError as error:
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
