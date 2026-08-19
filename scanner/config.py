"""Config loader. Reads config.yaml and returns a nested dict.

Secrets (Telegram) are intentionally NOT read here — they come from env vars.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")


def load_config(path: str | None = None) -> dict[str, Any]:
    path = path or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    _normalize_timeframes(cfg)
    _validate(cfg)
    return cfg


def _normalize_timeframes(cfg: dict[str, Any]) -> None:
    """Accept either `timeframes: [..]` (preferred) or a single `timeframe:`.
    Always leaves cfg['timeframes'] as a list and cfg['timeframe'] as the first
    (the latter is the legacy default used by DataFeed)."""
    tfs = cfg.get("timeframes")
    if not tfs:
        single = cfg.get("timeframe")
        if not single:
            raise ValueError("config.yaml: set 'timeframes' (list) or 'timeframe'")
        tfs = [single]
    if isinstance(tfs, str):
        tfs = [tfs]
    cfg["timeframes"] = list(tfs)
    cfg["timeframe"] = tfs[0]


def _validate(cfg: dict[str, Any]) -> None:
    required_top = [
        "exchanges", "timeframes", "risk", "execution",
        "detector", "storage", "health",
    ]
    for key in required_top:
        if key not in cfg:
            raise ValueError(f"config.yaml missing required section: {key!r}")
    if not cfg["exchanges"]:
        raise ValueError("config.yaml: 'exchanges' must not be empty")

    mode = cfg.get("symbols_mode", "manual")
    if mode not in ("auto", "manual"):
        raise ValueError("config.yaml: symbols_mode must be 'auto' or 'manual'")
    if mode == "manual" and not cfg.get("symbols"):
        raise ValueError("config.yaml: symbols_mode=manual needs a non-empty 'symbols' list")
    if mode == "auto" and "universe" not in cfg:
        raise ValueError("config.yaml: symbols_mode=auto needs a 'universe' section")
