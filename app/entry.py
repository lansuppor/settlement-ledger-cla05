import argparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, refunds
from app.store.db import connect, migrate
from app.store.refunds import RefundConflict

app = FastAPI(title="settlement-ledger")

@app.exception_handler(RefundConflict)
def refund_conflict_handler(_request: Request, exc: RefundConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})

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

class RefundActionIn(BaseModel):
    request_id: str = Field(min_length=1)

def _require_tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant

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
def read_order(order_id: str, x_tenant: str = Header(default="")) -> dict:
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

# ---- 退款单 ----

@app.post("/orders/{order_id}/refunds", status_code=201)
def create_refund(order_id: str, body: RefundIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = refunds.accept(tenant, order_id, body.refund_id, body.amount_cents, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/complete")
def complete_refund(order_id: str, refund_id: str, body: RefundActionIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = refunds.complete(tenant, order_id, refund_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/cancel")
def cancel_refund(order_id: str, refund_id: str, body: RefundActionIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = refunds.cancel(tenant, order_id, refund_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/reverse")
def reverse_refund(order_id: str, refund_id: str, body: RefundActionIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = refunds.reverse(tenant, order_id, refund_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.get("/orders/{order_id}/refunds/{refund_id}")
def read_refund(order_id: str, refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    refund = refunds.get(tenant, order_id, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.get("/orders/{order_id}/refunds")
def list_refunds(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    items = refunds.list_for_order(tenant, order_id)
    if items is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "refunds": items}

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
