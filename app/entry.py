import argparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, reconciliations, refund_imports, refunds, settlements, tickets
from app.store.db import connect, migrate
from app.store.reconciliations import ReconciliationConflict
from app.store.refund_imports import RefundImportConflict
from app.store.refunds import RefundConflict
from app.store.settlements import SettlementConflict
from app.store.tickets import TicketConflict

app = FastAPI(title="settlement-ledger")

@app.exception_handler(RefundConflict)
def refund_conflict_handler(_request: Request, exc: RefundConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})

@app.exception_handler(SettlementConflict)
def settlement_conflict_handler(_request: Request, exc: SettlementConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})

@app.exception_handler(TicketConflict)
def ticket_conflict_handler(_request: Request, exc: TicketConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})

@app.exception_handler(ReconciliationConflict)
def reconciliation_conflict_handler(_request: Request, exc: ReconciliationConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})

@app.exception_handler(RefundImportConflict)
def refund_import_conflict_handler(_request: Request, exc: RefundImportConflict) -> JSONResponse:
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

class RefundImportIn(BaseModel):
    request_id: str = Field(min_length=1)
    # 逐行宽容解析：非对象行、非法字段类型不在请求层拒绝，而作为该行的失败原因返回
    lines: list[object] = Field(min_length=1)

class TicketIn(BaseModel):
    ticket_id: str = Field(min_length=1)
    request_amount_cents: int = Field(gt=0)
    initiator: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    request_id: str = Field(min_length=1)

class TicketActionIn(BaseModel):
    request_id: str = Field(min_length=1)

class TicketResolveIn(BaseModel):
    award_cents: int = Field(gt=0)
    request_id: str = Field(min_length=1)

class ReconciliationIn(BaseModel):
    reconciliation_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=1)
    request_id: str = Field(min_length=1)

class ReconciliationActionIn(BaseModel):
    request_id: str = Field(min_length=1)

class SettlementIn(BaseModel):
    settlement_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=1)
    request_id: str = Field(min_length=1)

class SettlementActionIn(BaseModel):
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

# ---- 退款单批量导入 ----

@app.post("/refund-imports")
def submit_refund_import(body: RefundImportIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    lines = [line if isinstance(line, dict) else {} for line in body.lines]
    status, payload = refund_imports.submit(tenant, body.request_id, lines)
    return JSONResponse(status_code=status, content=payload)

@app.get("/refund-imports/{request_id}")
def read_refund_import(request_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    result = refund_imports.get(tenant, request_id)
    if result is None:
        raise HTTPException(status_code=404, detail="import batch not found")
    return result

# ---- 工单 ----

@app.post("/orders/{order_id}/refunds/{refund_id}/tickets", status_code=201)
def create_ticket(order_id: str, refund_id: str, body: TicketIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = tickets.accept(
        tenant, order_id, refund_id, body.ticket_id,
        body.request_amount_cents, body.initiator, body.reason, body.request_id,
    )
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/process")
def process_ticket(order_id: str, refund_id: str, ticket_id: str, body: TicketActionIn,
                   x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = tickets.process(tenant, order_id, refund_id, ticket_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/review")
def review_ticket(order_id: str, refund_id: str, ticket_id: str, body: TicketActionIn,
                  x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = tickets.review(tenant, order_id, refund_id, ticket_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/reprocess")
def reprocess_ticket(order_id: str, refund_id: str, ticket_id: str, body: TicketActionIn,
                     x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = tickets.reprocess(tenant, order_id, refund_id, ticket_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/resolve")
def resolve_ticket(order_id: str, refund_id: str, ticket_id: str, body: TicketResolveIn,
                   x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = tickets.resolve(
        tenant, order_id, refund_id, ticket_id, body.award_cents, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}/revoke")
def revoke_ticket(order_id: str, refund_id: str, ticket_id: str, body: TicketActionIn,
                  x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = tickets.revoke(tenant, order_id, refund_id, ticket_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.get("/orders/{order_id}/refunds/{refund_id}/tickets/{ticket_id}")
def read_ticket(order_id: str, refund_id: str, ticket_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    ticket = tickets.get(tenant, order_id, refund_id, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ticket

@app.get("/orders/{order_id}/refunds/{refund_id}/tickets")
def list_tickets(order_id: str, refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    items = tickets.list_for_refund(tenant, order_id, refund_id)
    if items is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return {"order_id": order_id, "refund_id": refund_id, "tickets": items}

# ---- 对账单（退款单对账核销） ----

@app.post("/orders/{order_id}/refunds/{refund_id}/reconciliations", status_code=201)
def create_reconciliation(order_id: str, refund_id: str, body: ReconciliationIn,
                          x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = reconciliations.accept(
        tenant, order_id, refund_id, body.reconciliation_id,
        body.amount_cents, body.reason, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}/reconcile")
def reconcile_reconciliation(order_id: str, refund_id: str, reconciliation_id: str,
                             body: ReconciliationActionIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = reconciliations.reconcile(
        tenant, order_id, refund_id, reconciliation_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}/cancel")
def cancel_reconciliation(order_id: str, refund_id: str, reconciliation_id: str,
                          body: ReconciliationActionIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = reconciliations.cancel(
        tenant, order_id, refund_id, reconciliation_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}/reverse")
def reverse_reconciliation(order_id: str, refund_id: str, reconciliation_id: str,
                           body: ReconciliationActionIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = reconciliations.reverse(
        tenant, order_id, refund_id, reconciliation_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.get("/orders/{order_id}/refunds/{refund_id}/reconciliations/{reconciliation_id}")
def read_reconciliation(order_id: str, refund_id: str, reconciliation_id: str,
                        x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    reconciliation = reconciliations.get(tenant, order_id, refund_id, reconciliation_id)
    if reconciliation is None:
        raise HTTPException(status_code=404, detail="reconciliation not found")
    return reconciliation

@app.get("/orders/{order_id}/refunds/{refund_id}/reconciliations")
def list_reconciliations(order_id: str, refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    items = reconciliations.list_for_refund(tenant, order_id, refund_id)
    if items is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return {"order_id": order_id, "refund_id": refund_id, "reconciliations": items}

@app.get("/reconciliations")
def search_reconciliations(status: str = "", min_amount_cents: int | None = None,
                           max_amount_cents: int | None = None,
                           x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    if status and status not in reconciliations.STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    if min_amount_cents is not None and max_amount_cents is not None and min_amount_cents > max_amount_cents:
        raise HTTPException(status_code=400, detail="min_amount_cents exceeds max_amount_cents")
    items = reconciliations.search(
        tenant,
        status=status or None,
        min_amount_cents=min_amount_cents,
        max_amount_cents=max_amount_cents,
    )
    return {"reconciliations": items}

# ---- 结算单 ----

@app.post("/orders/{order_id}/settlements", status_code=201)
def create_settlement(order_id: str, body: SettlementIn, x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = settlements.accept(
        tenant, order_id, body.settlement_id, body.amount_cents, body.reason, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/settlements/{settlement_id}/settle")
def settle_settlement(order_id: str, settlement_id: str, body: SettlementActionIn,
                      x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = settlements.settle(tenant, order_id, settlement_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/settlements/{settlement_id}/cancel")
def cancel_settlement(order_id: str, settlement_id: str, body: SettlementActionIn,
                      x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = settlements.cancel(tenant, order_id, settlement_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.post("/orders/{order_id}/settlements/{settlement_id}/reverse")
def reverse_settlement(order_id: str, settlement_id: str, body: SettlementActionIn,
                       x_tenant: str = Header(default="")):
    tenant = _require_tenant(x_tenant)
    status, payload = settlements.reverse(tenant, order_id, settlement_id, body.request_id)
    return JSONResponse(status_code=status, content=payload)

@app.get("/orders/{order_id}/settlements/{settlement_id}")
def read_settlement(order_id: str, settlement_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    settlement = settlements.get(tenant, order_id, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="settlement not found")
    return settlement

@app.get("/orders/{order_id}/settlements")
def list_settlements(order_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    items = settlements.list_for_order(tenant, order_id)
    if items is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"order_id": order_id, "settlements": items}

@app.get("/settlements")
def search_settlements(status: str = "", min_amount_cents: int | None = None,
                       max_amount_cents: int | None = None,
                       x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    if status and status not in settlements.STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")
    if min_amount_cents is not None and max_amount_cents is not None and min_amount_cents > max_amount_cents:
        raise HTTPException(status_code=400, detail="min_amount_cents exceeds max_amount_cents")
    items = settlements.search(
        tenant,
        status=status or None,
        min_amount_cents=min_amount_cents,
        max_amount_cents=max_amount_cents,
    )
    return {"settlements": items}

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
