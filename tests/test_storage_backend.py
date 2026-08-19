"""Backend selection: SQLite locally, Postgres when DATABASE_URL is present."""

import os
import pytest

from scanner.journal import open_journal, Journal


def test_auto_without_dburl_uses_sqlite(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg["storage"] = {"backend": "auto",
                      "db_path": os.path.join(tmp_path, "j.sqlite"),
                      "csv_export_path": os.path.join(tmp_path, "e.csv")}
    j = open_journal(cfg)
    assert isinstance(j, Journal)
    j.close()


def test_sqlite_forced_even_with_dburl(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5432/db")
    cfg["storage"] = {"backend": "sqlite",
                      "db_path": os.path.join(tmp_path, "j.sqlite"),
                      "csv_export_path": os.path.join(tmp_path, "e.csv")}
    j = open_journal(cfg)
    assert isinstance(j, Journal)          # explicit sqlite wins over env
    j.close()


def test_postgres_without_dburl_raises(cfg, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg["storage"] = {"backend": "postgres", "db_path": "x", "csv_export_path": "y"}
    with pytest.raises(ValueError):
        open_journal(cfg)
