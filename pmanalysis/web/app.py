"""FastAPI web application."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from pmanalysis.collector.service import CollectorService
from pmanalysis.config import ASSETS, INTERVALS, WEB_HOST, WEB_PORT
from pmanalysis.db.models import Window
from pmanalysis.db.repository import db_stats, list_windows, stats_win_rate, window_ticks_payload
from pmanalysis.db.session import db_health, get_session, init_db
from pmanalysis.export.parquet import export_window_csv
from pmanalysis.web.render import render
from pmanalysis.schemas import MarketCard, WindowSummary
from pmanalysis.web.sync import db_sync_loop
from pmanalysis.web.ws import manager as ws_manager

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent

_collector: CollectorService | None = None
_background_tasks: list[asyncio.Task] = []


def _embed_collector() -> bool:
    return os.getenv("PM_EMBED_COLLECTOR", "0") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _collector, _background_tasks
    init_db()
    if _embed_collector():
        log.info("Embedded collector enabled (WebSocket streams from live feeds)")
        _collector = CollectorService()
        _background_tasks.append(asyncio.create_task(_collector.start(), name="collector"))
    else:
        log.info("DB sync mode (start collector separately or use `pmanalysis start`)")
        _background_tasks.append(asyncio.create_task(db_sync_loop(), name="db-sync"))
    yield
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()
    if _collector:
        await _collector.stop()
        _collector = None


def create_app() -> FastAPI:
    app = FastAPI(title="Polymarket Analysis", version="0.2.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return render(request, "index.html", assets=ASSETS, intervals=INTERVALS)

    @app.get("/market/{asset}/{interval}", response_class=HTMLResponse)
    async def market_redirect(request: Request, asset: str, interval: str):
        return render(
            request,
            "index.html",
            assets=ASSETS,
            intervals=INTERVALS,
            preselect_asset=asset.upper(),
            preselect_interval=interval,
        )

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request):
        return render(request, "history.html")

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        health = db_health()
        with get_session() as session:
            stats = db_stats(session)
        return render(
            request,
            "status.html",
            health=health,
            embed_collector=_embed_collector(),
            **stats,
        )

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        await ws_manager.handle_client(websocket)

    @app.get("/api/markets", response_model=list[MarketCard])
    async def api_markets():
        from pmanalysis.db.repository import build_market_cards

        with get_session() as session:
            return build_market_cards(session)

    @app.get("/api/windows", response_model=list[WindowSummary])
    async def api_windows(limit: int = 100, asset: str | None = None, interval: str | None = None):
        with get_session() as session:
            return list_windows(session, limit=limit, asset=asset, interval=interval)

    @app.get("/api/windows/{slug}/ticks")
    async def api_window_ticks(slug: str):
        with get_session() as session:
            payload = window_ticks_payload(session, slug)
        if not payload:
            raise HTTPException(404, "Window not found")
        return payload

    @app.get("/api/stats/{asset}/{interval}")
    async def api_stats(asset: str, interval: str):
        with get_session() as session:
            rate = stats_win_rate(session, asset, interval)
        return {"win_rate_up_pct": rate}

    @app.get("/api/export/{window_id}")
    async def api_export(window_id: int, format: str = "csv"):
        with get_session() as session:
            window = session.query(Window).filter_by(id=window_id).first()
            if not window:
                raise HTTPException(404, "Window not found")
            if format != "csv":
                raise HTTPException(400, "Only csv download supported via API")
            path = export_window_csv(session, window)
        return FileResponse(path, filename=path.name)

    return app


def run_web(*, embed_collector: bool = False) -> None:
    import uvicorn

    if embed_collector:
        os.environ["PM_EMBED_COLLECTOR"] = "1"
    uvicorn.run(
        "pmanalysis.web.app:create_app",
        factory=True,
        host=WEB_HOST,
        port=WEB_PORT,
        reload=False,
    )
