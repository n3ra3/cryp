"""Postgres / Supabase journal backend.

Same public interface as `journal.Journal` (insert_trade, update_trade,
load_open_trades, all_trades, export_csv, close) so the rest of the app doesn't
care which backend is in use. Row->Trade mapping is reused from Journal.

Connections can be dropped by Supabase after idle; every statement is retried
once with a fresh connection. Sync driver (psycopg) keeps the executor sync,
matching the SQLite path; write volume is low (a handful per candle).
"""

from __future__ import annotations

import csv
import os
from typing import Optional

import psycopg
from psycopg.rows import dict_row

from .models import Trade
from .journal import Journal, CSV_COLUMNS

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS trades (
    id                SERIAL PRIMARY KEY,
    timestamp_signal  BIGINT NOT NULL,
    exchange          TEXT   NOT NULL,
    symbol            TEXT   NOT NULL,
    timeframe         TEXT   NOT NULL,
    side              TEXT   NOT NULL,
    entry_price       DOUBLE PRECISION NOT NULL,
    stop_price        DOUBLE PRECISION NOT NULL,
    target_price      DOUBLE PRECISION NOT NULL,
    position_notional DOUBLE PRECISION NOT NULL,
    risk_usd          DOUBLE PRECISION NOT NULL,
    exit_price        DOUBLE PRECISION,
    exit_reason       TEXT,
    r_multiple        DOUBLE PRECISION,
    fees_usd          DOUBLE PRECISION NOT NULL DEFAULT 0,
    slippage_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
    bars_held         INTEGER NOT NULL DEFAULT 0,
    rsi_at_signal     DOUBLE PRECISION,
    atr_at_signal     DOUBLE PRECISION,
    stretch_atr       DOUBLE PRECISION,
    swing_level       DOUBLE PRECISION,
    raw_signal_json   TEXT,
    closed            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(closed);
"""


class PgJournal:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn: Optional[psycopg.Connection] = None
        self._connect()
        self._run(SCHEMA_PG)

    # ------------------------------------------------------------------ #
    def _connect(self) -> None:
        self.conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    def _run(self, sql: str, params=None, fetch: str | None = None):
        """Execute with a one-shot reconnect on a dropped connection."""
        last_err = None
        for attempt in (1, 2):
            try:
                cur = self.conn.execute(sql, params or ())
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                last_err = e
                self._connect()
        raise last_err  # pragma: no cover

    def close(self) -> None:
        if self.conn:
            self.conn.close()

    # ------------------------------------------------------------------ #
    def insert_trade(self, trade: Trade) -> int:
        row = self._run(
            """INSERT INTO trades (
                timestamp_signal, exchange, symbol, timeframe, side,
                entry_price, stop_price, target_price, position_notional, risk_usd,
                exit_price, exit_reason, r_multiple, fees_usd, slippage_usd,
                bars_held, rsi_at_signal, atr_at_signal, stretch_atr, swing_level,
                raw_signal_json, closed
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id""",
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
            fetch="one",
        )
        trade.id = row["id"]
        return trade.id

    def update_trade(self, trade: Trade) -> None:
        if trade.id is None:
            self.insert_trade(trade)
            return
        self._run(
            """UPDATE trades SET
                exit_price=%s, exit_reason=%s, r_multiple=%s, fees_usd=%s,
                slippage_usd=%s, bars_held=%s, closed=%s
               WHERE id=%s""",
            (
                trade.exit_price,
                trade.exit_reason.value if trade.exit_reason else None,
                trade.r_multiple, trade.fees_usd, trade.slippage_usd,
                trade.bars_held, 1 if trade.closed else 0, trade.id,
            ),
        )

    # ------------------------------------------------------------------ #
    def load_open_trades(self) -> list[Trade]:
        rows = self._run("SELECT * FROM trades WHERE closed=0", fetch="all") or []
        return [Journal._row_to_trade(r) for r in rows]

    def all_trades(self, closed_only: bool = False) -> list[Trade]:
        q = "SELECT * FROM trades"
        if closed_only:
            q += " WHERE closed=1"
        q += " ORDER BY id"
        rows = self._run(q, fetch="all") or []
        return [Journal._row_to_trade(r) for r in rows]

    def export_csv(self, path: str) -> str:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        rows = self._run(
            f"SELECT {', '.join(CSV_COLUMNS)} FROM trades ORDER BY id", fetch="all"
        ) or []
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_COLUMNS)
            for r in rows:
                writer.writerow([r[c] for c in CSV_COLUMNS])
        return path
