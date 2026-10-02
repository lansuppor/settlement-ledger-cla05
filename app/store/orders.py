import sqlite3
from app.store.db import connect

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}

def add_payment(tenant: str, order_id: str, amount_cents: int, request_id: str | None = None) -> dict | None:
    conn = connect()
    payment = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if request_id is not None:
            seen = conn.execute(
                "SELECT amount_cents, result FROM payment_requests WHERE tenant=? AND order_id=? AND request_id=?",
                (tenant, order_id, request_id),
            ).fetchone()
            if seen is not None:
                if seen["amount_cents"] != amount_cents:
                    conn.execute("ROLLBACK")
                    raise ValueError("request_id already registered with a different amount")
                conn.execute("COMMIT")
                payment = {
                    "request_id": request_id,
                    "amount_cents": seen["amount_cents"],
                    "result": seen["result"],
                    "duplicate": True,
                }
        if payment is None:
            if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
                conn.execute("ROLLBACK")
                raise ValueError("payment exceeds outstanding amount")
            conn.execute(
                "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
                (amount_cents, amount_cents, tenant, order_id),
            )
            if request_id is not None:
                conn.execute(
                    "INSERT INTO payment_requests(tenant, order_id, request_id, amount_cents, result) VALUES(?,?,?,?,'applied')",
                    (tenant, order_id, request_id, amount_cents),
                )
                payment = {
                    "request_id": request_id,
                    "amount_cents": amount_cents,
                    "result": "applied",
                    "duplicate": False,
                }
            conn.execute("COMMIT")
    finally:
        conn.close()
    order = get(tenant, order_id)
    if payment is not None:
        order["payment"] = payment
    return order
