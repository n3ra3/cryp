"""Trade journal: SQLite persistence + CSV export.

The journal is the single source of truth. OPEN trades are persisted so they
survive a restart (Northflank redeploy) and can be re-driven forward.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from typing import Iterable, Optional

from .models import Trade, Side, ExitReason


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_signal  INTEGER NOT NULL,
    exchange          TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    timeframe         TEXT    NOT NULL,
    side              TEXT    NOT NULL,
    entry_price       REAL    NOT NULL,
    stop_price        REAL    NOT NULL,
    target_price      REAL    NOT NULL,
    position_notional REAL    NOT NULL,
    risk_usd          REAL    NOT NULL,
    exit_price        REAL,
    exit_reason       TEXT,
    r_multiple        REAL,
    fees_usd          REAL    NOT NULL DEFAULT 0,
    slippage_usd      REAL    NOT NULL DEFAULT 0,
    bars_held         INTEGER NOT NULL DEFAULT 0,
    rsi_at_signal     REAL,
    atr_at_signal     REAL,
    stretch_atr       REAL,
    swing_level       REAL,
    raw_signal_json   TEXT,
    closed            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(closed);
"""

CSV_COLUMNS = [
    "id", "timestamp_signal", "exchange", "symbol", "timeframe", "side",
    "entry_price", "stop_price", "target_price", "position_notional", "risk_usd",
    "exit_price", "exit_reason", "r_multiple", "fees_usd", "slippage_usd",
    "bars_held", "rsi_at_signal", "atr_at_signal", "stretch_atr", "swing_level",
    "raw_signal_json",
]


class Journal:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    def insert_trade(self, trade: Trade) -> int:
        cur = self.conn.execute(
            """INSERT INTO trades (
                timestamp_signal, exchange, symbol, timeframe, side,
                entry_price, stop_price, target_price, position_notional, risk_usd,
                exit_price, exit_reason, r_multiple, fees_usd, slippage_usd,
                bars_held, rsi_at_signal, atr_at_signal, stretch_atr, swing_level,
                raw_signal_json, closed
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.signal_ts, trade.exchange, trade.symbol, trade.timeframe,
                trade.side.value, trade.entry_price, trade.stop_price,
                trade.target_price, trade.position_notional, trade.risk_usd,
                trade.exit_price,
                trade.exit_reason.value if trade.exit_reason else None,
                trade.r_multiple, trade.fees_usd, trade.slippage_usd,
                trade.bars_held, trade.rsi_at_signal, trade.atr_at_signal,
                trade.stretch_atr, trade.swing_level, trade.raw_signal_json,
                1 if trade.closed else 0,
            ),
        )
        self.conn.commit()
        trade.id = cur.lastrowid
        return trade.id

    def update_trade(self, trade: Trade) -> None:
        if trade.id is None:
            self.insert_trade(trade)
            return
        self.conn.execute(
            """UPDATE trades SET
                exit_price=?, exit_reason=?, r_multiple=?, fees_usd=?,
                slippage_usd=?, bars_held=?, closed=?
               WHERE id=?""",
            (
                trade.exit_price,
                trade.exit_reason.value if trade.exit_reason else None,
                trade.r_multiple, trade.fees_usd, trade.slippage_usd,
                trade.bars_held, 1 if trade.closed else 0, trade.id,
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def load_open_trades(self) -> list[Trade]:
        rows = self.conn.execute("SELECT * FROM trades WHERE closed=0").fetchall()
        return [self._row_to_trade(r) for r in rows]

    def all_trades(self, closed_only: bool = False) -> list[Trade]:
        q = "SELECT * FROM trades"
        if closed_only:
            q += " WHERE closed=1"
        q += " ORDER BY id"
        return [self._row_to_trade(r) for r in self.conn.execute(q).fetchall()]

    @staticmethod
    def _row_to_trade(r: sqlite3.Row) -> Trade:
        t = Trade(
            exchange=r["exchange"], symbol=r["symbol"], timeframe=r["timeframe"],
            side=Side(r["side"]), signal_ts=r["timestamp_signal"],
            entry_price=r["entry_price"], stop_price=r["stop_price"],
            target_price=r["target_price"], position_notional=r["position_notional"],
            risk_usd=r["risk_usd"], quantity=(
                r["position_notional"] / r["entry_price"] if r["entry_price"] else 0.0
            ),
            rsi_at_signal=r["rsi_at_signal"], atr_at_signal=r["atr_at_signal"],
            stretch_atr=r["stretch_atr"], swing_level=r["swing_level"],
            raw_signal_json=r["raw_signal_json"],
        )
        t.id = r["id"]
        t.exit_price = r["exit_price"]
        t.exit_reason = ExitReason(r["exit_reason"]) if r["exit_reason"] else None
        t.r_multiple = r["r_multiple"]
        t.fees_usd = r["fees_usd"]
        t.slippage_usd = r["slippage_usd"]
        t.bars_held = r["bars_held"]
        t.closed = bool(r["closed"])
        return t

    def delete_trades_not_in(self, timeframes: list[str]) -> int:
        """Delete trades whose timeframe is not in `timeframes`. Returns count."""
        if not timeframes:
            return 0
        ph = ",".join("?" * len(timeframes))
        cur = self.conn.execute(
            f"DELETE FROM trades WHERE timeframe NOT IN ({ph})", tuple(timeframes)
        )
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ #
    def export_csv(self, path: str) -> str:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        rows = self.conn.execute(
            f"SELECT {', '.join(CSV_COLUMNS)} FROM trades ORDER BY id"
        ).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            for r in rows:
                writer.writerow([r[c] for c in CSV_COLUMNS])
        return path


def open_journal(cfg: dict):
    """Pick the storage backend.

    Postgres/Supabase when a DATABASE_URL is available (env var wins, then
    storage.database_url); otherwise the local SQLite file. This keeps tests and
    local runs on SQLite with zero extra dependencies, while production persists
    to Supabase so nothing is lost across redeploys.
    """
    storage = cfg["storage"]
    backend = storage.get("backend", "auto")
    dsn = os.environ.get("DATABASE_URL") or storage.get("database_url")

    if backend == "postgres" or (backend == "auto" and dsn):
        if not dsn:
            raise ValueError(
                "storage backend is postgres but DATABASE_URL is not set"
            )
        from .pg_journal import PgJournal   # lazy: psycopg only needed for PG
        return PgJournal(dsn)
    return Journal(storage["db_path"])
