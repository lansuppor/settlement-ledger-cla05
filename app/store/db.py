import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def _relax_refund_amount_check(conn: sqlite3.Connection) -> None:
    """裁决扣减可把退款单金额减至 0，放宽 002 迁移中 amount_cents > 0 的约束（可重入）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='refunds'"
    ).fetchone()
    if row is None or "amount_cents > 0" not in row["sql"]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            BEGIN;
            CREATE TABLE refunds_relaxed(
              tenant TEXT NOT NULL,
              order_id TEXT NOT NULL,
              refund_id TEXT NOT NULL,
              amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
              status TEXT NOT NULL CHECK(status IN ('pending','completed','cancelled','reversed')),
              effective_deduction_cents INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(tenant, order_id, refund_id),
              FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
            );
            INSERT INTO refunds_relaxed SELECT * FROM refunds;
            DROP TABLE refunds;
            ALTER TABLE refunds_relaxed RENAME TO refunds;
            COMMIT;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate() -> None:
    conn = connect()
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.executescript(path.read_text(encoding="utf-8"))
        _relax_refund_amount_check(conn)
    finally:
        conn.close()
