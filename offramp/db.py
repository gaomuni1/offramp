"""SQLite storage: rounds, every order we send (written before sending), OSL sells you report, events."""
from __future__ import annotations

import secrets
import sqlite3
import string
import time
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path

from .models import OrderRow
from .allocation import Allocation
from .util import ZERO, dec

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,              -- new | buying | bought | selling | closed
    transfer_usdt TEXT,
    start_usdt TEXT,
    end_usdt TEXT,
    buy_budget TEXT,
    buy_started_at REAL,
    buy_done_at REAL,
    arrived_eth TEXT,
    sell_started_at REAL,
    closed_at REAL,
    osl_usd TEXT,
    gas_eth TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    cl_ord_id TEXT PRIMARY KEY,
    round_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    inst_id TEXT NOT NULL,
    side TEXT NOT NULL,
    ord_type TEXT NOT NULL,
    px TEXT,
    sz TEXT NOT NULL,
    state TEXT NOT NULL,
    ord_id TEXT,
    acc_fill_sz TEXT,
    avg_px TEXT,
    fee TEXT,
    fee_ccy TEXT,
    pnl TEXT,
    cancel_requested_at REAL,
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS orders_round ON orders(round_id, purpose);
CREATE TABLE IF NOT EXISTS sells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                -- sell | fix | undo
    qty TEXT NOT NULL,                 -- signed ETH; the sum is what has been sold
    px TEXT,
    undone INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    round_id INTEGER,
    level TEXT NOT NULL,
    msg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS buy_allocations (
    round_id INTEGER PRIMARY KEY,
    total_usdt TEXT NOT NULL,
    reserve_usdt TEXT NOT NULL,
    maker_fee TEXT NOT NULL,
    taker_fee TEXT NOT NULL
);
"""

_ROUND_DECIMALS = {"transfer_usdt", "start_usdt", "end_usdt", "buy_budget", "arrived_eth", "osl_usd", "gas_eth"}
_ORDER_DECIMALS = {"px", "sz", "acc_fill_sz", "avg_px", "fee", "pnl"}


@dataclass
class Round:
    id: int
    created_at: float
    status: str
    transfer_usdt: Decimal | None = None
    start_usdt: Decimal | None = None
    end_usdt: Decimal | None = None
    buy_budget: Decimal | None = None
    buy_started_at: float | None = None
    buy_done_at: float | None = None
    arrived_eth: Decimal | None = None
    sell_started_at: float | None = None
    closed_at: float | None = None
    osl_usd: Decimal | None = None
    gas_eth: Decimal | None = None


@dataclass
class SellRow:
    id: int
    round_id: int
    ts: float
    kind: str
    qty: Decimal
    px: Decimal | None
    undone: bool
    note: str


def _to_sql(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


class Db:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'id_prefix'").fetchone()
        if row is None:
            # Random per-database prefix so client order ids never repeat even if the database is recreated.
            alphabet = string.ascii_lowercase + string.digits
            prefix = "".join(secrets.choice(alphabet) for _ in range(4))
            self.conn.execute("INSERT INTO meta(key, value) VALUES('id_prefix', ?)", (prefix,))
            self.id_prefix = prefix
        else:
            self.id_prefix = row["value"]

    # ---- rounds -------------------------------------------------------------------------------

    def create_round(self, now: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO rounds(created_at, status) VALUES(?, 'new')", (now if now is not None else time.time(),)
        )
        return int(cur.lastrowid)

    @staticmethod
    def _round(row) -> Round:
        kw = {}
        for f in fields(Round):
            v = row[f.name]
            kw[f.name] = dec(v) if (f.name in _ROUND_DECIMALS and v is not None) else v
        return Round(**kw)

    def get_round(self, rid: int) -> Round | None:
        row = self.conn.execute("SELECT * FROM rounds WHERE id = ?", (rid,)).fetchone()
        return self._round(row) if row else None

    def active_round(self) -> Round | None:
        row = self.conn.execute("SELECT * FROM rounds WHERE status != 'closed' ORDER BY id DESC LIMIT 1").fetchone()
        return self._round(row) if row else None

    def latest_round(self) -> Round | None:
        row = self.conn.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
        return self._round(row) if row else None

    def all_rounds(self) -> list[Round]:
        return [self._round(r) for r in self.conn.execute("SELECT * FROM rounds ORDER BY id")]

    def update_round(self, rid: int, **kw) -> None:
        if not kw:
            return
        cols = ", ".join(f"{k} = ?" for k in kw)
        self.conn.execute(f"UPDATE rounds SET {cols} WHERE id = ?", [*map(_to_sql, kw.values()), rid])

    # ---- orders -------------------------------------------------------------------------------

    def save_allocation(self, rid: int, allocation: Allocation | None) -> None:
        if allocation is None:
            self.conn.execute("DELETE FROM buy_allocations WHERE round_id = ?", (rid,))
            return
        self.conn.execute("INSERT OR REPLACE INTO buy_allocations VALUES (?, ?, ?, ?, ?)",
                          (rid, str(allocation.total_usdt), str(allocation.reserve_usdt),
                           str(allocation.maker_fee), str(allocation.taker_fee)))

    def get_allocation(self, rid: int) -> Allocation | None:
        row = self.conn.execute("SELECT * FROM buy_allocations WHERE round_id = ?", (rid,)).fetchone()
        if row is None:
            return None
        return Allocation(*(dec(row[key]) for key in ("total_usdt", "reserve_usdt", "maker_fee", "taker_fee")))

    def next_cl_ord_id(self, rid: int, kind: str) -> str:
        """OKX allows letters and digits only, up to 32 characters."""
        name = f"r{rid}{kind}"
        self.conn.execute("INSERT OR IGNORE INTO counters(name, value) VALUES(?, 0)", (name,))
        self.conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
        seq = self.conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0]
        return f"ofr{self.id_prefix}r{rid}{kind}{seq:05d}"

    def insert_order(self, row: OrderRow) -> None:
        row.updated_at = time.time()
        names = [f.name for f in fields(OrderRow)]
        self.conn.execute(
            f"INSERT INTO orders({', '.join(names)}) VALUES({', '.join('?' * len(names))})",
            [_to_sql(getattr(row, n)) for n in names],
        )

    def save_order(self, row: OrderRow) -> None:
        row.updated_at = time.time()
        names = [f.name for f in fields(OrderRow) if f.name != "cl_ord_id"]
        self.conn.execute(
            f"UPDATE orders SET {', '.join(f'{n} = ?' for n in names)} WHERE cl_ord_id = ?",
            [*(_to_sql(getattr(row, n)) for n in names), row.cl_ord_id],
        )

    @staticmethod
    def _order(row) -> OrderRow:
        kw = {}
        for f in fields(OrderRow):
            v = row[f.name]
            if f.name in _ORDER_DECIMALS:
                v = dec(v) if v is not None else (None if f.name == "px" else ZERO)
            elif f.name in ("ord_id", "fee_ccy") and v is None:
                v = ""
            kw[f.name] = v
        return OrderRow(**kw)

    def orders(self, rid: int, purpose: str | None = None) -> list[OrderRow]:
        if purpose is None:
            rows = self.conn.execute("SELECT * FROM orders WHERE round_id = ? ORDER BY created_at, rowid", (rid,))
        else:
            rows = self.conn.execute(
                "SELECT * FROM orders WHERE round_id = ? AND purpose = ? ORDER BY created_at, rowid", (rid, purpose)
            )
        return [self._order(r) for r in rows]

    def open_orders(self, rid: int, purpose: str | None = None) -> list[OrderRow]:
        return [o for o in self.orders(rid, purpose) if not o.terminal]

    # ---- OSL sells reported by the user -------------------------------------------------------

    def add_sell(self, rid: int, kind: str, qty: Decimal, px: Decimal | None, ts: float, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO sells(round_id, ts, kind, qty, px, note) VALUES(?, ?, ?, ?, ?, ?)",
            (rid, ts, kind, _to_sql(qty), _to_sql(px), note),
        )
        return int(cur.lastrowid)

    def sells(self, rid: int) -> list[SellRow]:
        return [
            SellRow(r["id"], r["round_id"], r["ts"], r["kind"], dec(r["qty"]),
                    dec(r["px"]) if r["px"] is not None else None, bool(r["undone"]), r["note"] or "")
            for r in self.conn.execute("SELECT * FROM sells WHERE round_id = ? ORDER BY id", (rid,))
        ]

    def sold_total(self, rid: int) -> Decimal:
        return sum((s.qty for s in self.sells(rid)), ZERO)

    def last_undoable(self, rid: int) -> SellRow | None:
        for s in reversed(self.sells(rid)):
            if s.kind in ("sell", "fix") and not s.undone:
                return s
        return None

    def undo_sell(self, row: SellRow, ts: float) -> None:
        self.conn.execute("UPDATE sells SET undone = 1 WHERE id = ?", (row.id,))
        self.add_sell(row.round_id, "undo", -row.qty, None, ts, note=f"撤销 #{row.id}")

    # ---- events -------------------------------------------------------------------------------

    def log(self, rid: int | None, level: str, msg: str) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, round_id, level, msg) VALUES(?, ?, ?, ?)", (time.time(), rid, level, msg)
        )
