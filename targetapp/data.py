"""SQLite access for the stand-in credit-union console.

Deliberately plain: this is the system under automation, not part of the
automation system. It exists to give the agent a realistic multi-step flow and
a set of exceptional states to run into.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("cu.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    member_no   TEXT PRIMARY KEY,
    first       TEXT NOT NULL,
    last        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'Active',
    branch      TEXT NOT NULL DEFAULT 'Main',
    -- Drives the exceptional states wired up in issue 3. NULL is a normal member.
    flags       TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_no     TEXT NOT NULL REFERENCES members(member_no),
    acct_type     TEXT NOT NULL,
    acct_number   TEXT NOT NULL UNIQUE,
    balance_cents INTEGER NOT NULL DEFAULT 0,
    opened_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_member ON accounts(member_no);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_member(conn: sqlite3.Connection, member_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM members WHERE member_no = ?", (member_no,)
    ).fetchone()


def get_accounts(conn: sqlite3.Connection, member_no: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM accounts WHERE member_no = ? ORDER BY acct_type, id",
        (member_no,),
    ).fetchall()


def next_account_number(conn: sqlite3.Connection, acct_type: str) -> str:
    """Allocate a fresh account number for a newly opened sub-account."""
    prefix = {"Savings": "SV", "Checking": "CK", "Money Market": "MM"}.get(
        acct_type, "GN"
    )
    n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    return f"{prefix}-{4400000 + n * 1237:07d}"


def create_account(
    conn: sqlite3.Connection,
    member_no: str,
    acct_type: str,
    balance_cents: int,
    opened_at: str,
) -> str:
    """Open a sub-account. This is the irreversible write the agent can reach."""
    acct_number = next_account_number(conn, acct_type)
    conn.execute(
        "INSERT INTO accounts (member_no, acct_type, acct_number, balance_cents,"
        " opened_at) VALUES (?, ?, ?, ?, ?)",
        (member_no, acct_type, acct_number, balance_cents, opened_at),
    )
    conn.commit()
    return acct_number


def money(cents: int) -> str:
    """Render cents the way the legacy grid does: 4210.33 -> '4,210.33'."""
    return f"{cents / 100:,.2f}"


def parse_money(text: str) -> int | None:
    """Parse a user-entered amount into cents. None when it is not a number."""
    cleaned = (text or "").strip().replace(",", "").replace("$", "")
    try:
        return int(round(float(cleaned) * 100))
    except ValueError:
        return None


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)
