"""Minimal aiohttp health server. GET /health -> 200.

Used by Northflank's health check. Also exposes a tiny /status JSON for
debugging (open trades + uptime), which contains no secrets.
"""

from __future__ import annotations

import time

from aiohttp import web


class HealthServer:
    def __init__(self, cfg: dict, state):
        h = cfg["health"]
        self.host = h.get("host", "0.0.0.0")
        self.port = h.get("port", 8000)
        self.state = state           # shared AppState (for /status)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([
            web.get("/health", self._health),
            web.get("/status", self._status),
        ])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _health(self, _request: web.Request) -> web.Response:
        return web.Response(text="ok", status=200)

    async def _status(self, _request: web.Request) -> web.Response:
        s = self.state
        return web.json_response({
            "uptime_sec": int(time.time() - s.started_at),
            "paused": s.paused,
            "open_trades": s.executor.open_trade_count() if s.executor else 0,
            "markets": s.market_count(),
            "timeframes": s.cfg.get("timeframes", []),
        })
