"""FastAPI web application."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pmanalysis import __version__
from pmanalysis.collector.registry import get_collector, set_collector
from pmanalysis.collector.service import CollectorService
from pmanalysis.config import (
    ASSETS,
    DATASET_LOOKBACK_DAYS,
    FEED_RECORD_ENABLED,
    INTERVAL_SECONDS,
    INTERVALS,
    OWNER_CONTACT,
    WEB_HOST,
    WEB_PORT,
)
from pmanalysis.datasets.schema import lookback_start
from pmanalysis.datasets.service import DatasetService, set_dataset_service
from pmanalysis.db.models import Window
from pmanalysis.db.repository import (
    count_windows,
    collector_status_payload,
    db_stats,
    feeds_from_db,
    list_binance_trades,
    list_clob_quotes,
    list_oracle_ticks,
    list_recent_previews,
    list_windows,
    stats_for_market,
    stats_win_rates,
    window_ticks_payload,
)
from pmanalysis.db.session import db_health, get_session, init_db, run_db
from pmanalysis.export.parquet import export_window_csv
from pmanalysis.web.render import render
from pmanalysis.schemas import MarketCard
from pmanalysis.web.sync import db_sync_loop, hydrate_live_store_from_db
from pmanalysis.web.datasets import router as datasets_router
from pmanalysis.web.research import router as research_router
from pmanalysis.web.ws import manager as ws_manager

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent

_collector: CollectorService | None = None
_background_tasks: list[asyncio.Task] = []


def _embed_collector() -> bool:
    return os.getenv("PM_EMBED_COLLECTOR", "0") == "1"


def _health_from_status(payload: dict) -> dict:
    return {
        "path": payload.get("path"),
        "size_mb": payload.get("size_mb"),
        "ok": payload.get("ok"),
    }


async def _status_bundle() -> dict:
    collector = get_collector()
    cached = collector.stats_cache if collector else None
    if cached is not None:
        return cached

    def _load():
        health = db_health()
        with get_session() as session:
            payload = collector_status_payload(session)
            feeds = feeds_from_db(session)
        return {
            **health,
            **payload,
            "feeds": feeds,
            "feed_record": FEED_RECORD_ENABLED,
            "collector_running": False,
            "last_tick_at": None,
            "last_discovery_at": None,
        }

    return await run_db(_load)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _collector, _background_tasks
    init_db()
    dataset_service = DatasetService()
    app.state.dataset_service = dataset_service
    set_dataset_service(dataset_service)
    await dataset_service.start_background()
    await hydrate_live_store_from_db()
    if _embed_collector():
        log.info("Embedded collector enabled (WebSocket streams from live feeds)")
        _collector = CollectorService()
        set_collector(_collector)
        _background_tasks.append(asyncio.create_task(_collector.start(), name="collector"))
    else:
        log.info("DB sync mode (start collector separately or use `pmanalysis start`)")
        _background_tasks.append(asyncio.create_task(db_sync_loop(), name="db-sync"))
    yield
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()
    await dataset_service.stop()
    set_dataset_service(None)
    app.state.dataset_service = None
    if _collector:
        await _collector.stop()
        set_collector(None)
        _collector = None


def create_app() -> FastAPI:
    app = FastAPI(title="Polymarket Analysis", version=__version__, lifespan=lifespan)
    app.include_router(datasets_router)
    app.include_router(research_router)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(
            WEB_DIR / "static" / "img" / "p-origin-removebg.png",
            media_type="image/png",
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        return render(
            request,
            "index.html",
            assets=ASSETS,
            intervals=INTERVALS,
            interval_seconds=INTERVAL_SECONDS,
        )

    @app.get("/market/{asset}/{interval}", response_class=HTMLResponse)
    async def market_redirect(request: Request, asset: str, interval: str):
        return render(
            request,
            "index.html",
            assets=ASSETS,
            intervals=INTERVALS,
            interval_seconds=INTERVAL_SECONDS,
            preselect_asset=asset.upper(),
            preselect_interval=interval,
        )

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request):
        from pmanalysis.web.datasets import download_limits

        return render(request, "history.html", download=download_limits())

    @app.get("/grid", response_class=HTMLResponse)
    async def grid_page(request: Request):
        return render(
            request,
            "grid.html",
            assets=ASSETS,
            intervals=INTERVALS,
            interval_seconds=INTERVAL_SECONDS,
        )

    @app.get("/research", response_class=HTMLResponse)
    async def research_page(request: Request):
        from pmanalysis.analytics.features import checkpoints_for

        return render(
            request,
            "research.html",
            assets=ASSETS,
            intervals=INTERVALS,
            checkpoints={interval: list(checkpoints_for(interval)) for interval in INTERVALS},
        )

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        payload = await _status_bundle()
        health = _health_from_status(payload)
        return render(
            request,
            "status.html",
            health=health,
            embed_collector=_embed_collector(),
            **payload,
        )

    @app.get("/api/status")
    async def api_status():
        payload = await _status_bundle()
        return {
            "ok": payload.get("ok"),
            "path": payload.get("path"),
            "size_mb": payload.get("size_mb"),
            "window_count": payload.get("window_count"),
            "tick_count": payload.get("tick_count"),
            "binance_trade_count": payload.get("binance_trade_count"),
            "oracle_tick_count": payload.get("oracle_tick_count"),
            "clob_quote_count": payload.get("clob_quote_count"),
            "latest_tick": payload.get("latest_tick"),
            "feeds": payload.get("feeds") or [],
            "gaps_open": payload.get("gaps_open") or [],
            "gaps_recent": payload.get("gaps_recent") or [],
            "feed_record": payload.get("feed_record"),
            "collector_running": payload.get("collector_running", False),
            "embed_collector": _embed_collector(),
            "last_tick_at": payload.get("last_tick_at"),
            "last_discovery_at": payload.get("last_discovery_at"),
        }

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        await ws_manager.handle_client(websocket)

    @app.get("/api/markets", response_model=list[MarketCard])
    async def api_markets():
        from pmanalysis.db.repository import build_market_cards

        def _load():
            with get_session() as session:
                return build_market_cards(session)

        return await run_db(_load)

    @app.get("/api/windows")
    async def api_windows(
        limit: int = 200,
        offset: int = 0,
        asset: str | None = None,
        interval: str | None = None,
    ):
        def _load():
            with get_session() as session:
                total = count_windows(session, asset=asset, interval=interval)
                rows = list_windows(
                    session,
                    limit=limit,
                    offset=offset,
                    asset=asset,
                    interval=interval,
                )
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": [r.model_dump() if hasattr(r, "model_dump") else r for r in rows],
            }

        return await run_db(_load)

    @app.get("/api/feeds/stats")
    async def api_feed_stats():
        collector = get_collector()
        cached = collector.stats_cache if collector else None
        if cached is not None:
            return {
                "window_count": cached["window_count"],
                "tick_count": cached["tick_count"],
                "binance_trade_count": cached["binance_trade_count"],
                "oracle_tick_count": cached["oracle_tick_count"],
                "clob_quote_count": cached["clob_quote_count"],
                "latest_tick": cached["latest_tick"],
                "feed_record": cached.get("feed_record", FEED_RECORD_ENABLED),
            }

        def _load():
            with get_session() as session:
                stats = db_stats(session)
            stats["feed_record"] = FEED_RECORD_ENABLED
            return stats

        return await run_db(_load)

    @app.get("/api/feeds/binance")
    async def api_binance_trades(
        limit: int = 200,
        offset: int = 0,
        asset: str | None = None,
    ):
        def _load():
            with get_session() as session:
                items, total = list_binance_trades(
                    session, limit=limit, offset=offset, asset=asset
                )
            return {"total": total, "limit": limit, "offset": offset, "items": items}

        return await run_db(_load)

    @app.get("/api/feeds/oracle")
    async def api_oracle_ticks(
        limit: int = 200,
        offset: int = 0,
        asset: str | None = None,
        source: str | None = None,
    ):
        def _load():
            with get_session() as session:
                items, total = list_oracle_ticks(
                    session, limit=limit, offset=offset, asset=asset, source=source
                )
            return {"total": total, "limit": limit, "offset": offset, "items": items}

        return await run_db(_load)

    @app.get("/api/feeds/clob")
    async def api_clob_quotes(
        limit: int = 200,
        offset: int = 0,
        asset: str | None = None,
        side: str | None = None,
    ):
        def _load():
            with get_session() as session:
                items, total = list_clob_quotes(
                    session, limit=limit, offset=offset, asset=asset, side=side
                )
            return {"total": total, "limit": limit, "offset": offset, "items": items}

        return await run_db(_load)

    @app.get("/api/windows/{slug}/ticks")
    async def api_window_ticks(slug: str):
        def _load():
            with get_session() as session:
                return window_ticks_payload(session, slug)

        payload = await run_db(_load)
        if not payload:
            raise HTTPException(404, "Window not found")
        return payload

    @app.get("/api/candles/{asset}/{interval}")
    async def api_candles(asset: str, interval: str, limit: int = 180):
        asset = asset.upper()
        if asset not in ASSETS or interval not in INTERVALS:
            raise HTTPException(400, "Invalid asset or interval")

        collector = get_collector()
        live = collector.candles.snapshot(asset, interval) if collector else None

        def _load():
            from pmanalysis.candles import list_window_candles

            with get_session() as session:
                return list_window_candles(
                    session, asset, interval, limit=limit, live=live
                )

        return await run_db(_load)

    @app.get("/api/previews/{asset}/{interval}")
    async def api_recent_previews(
        asset: str,
        interval: str,
        limit: int = 7,
        page: int = 0,
        skip_current: bool = False,
    ):
        asset = asset.upper()
        if asset not in ASSETS or interval not in INTERVALS:
            raise HTTPException(400, "Invalid asset or interval")

        def _load():
            with get_session() as session:
                return list_recent_previews(
                    session,
                    asset=asset,
                    interval=interval,
                    limit=max(1, min(limit, 10)),
                    page=max(0, page),
                    skip_current=skip_current,
                )

        return await run_db(_load)

    @app.get("/api/stats")
    async def api_stats_all():
        def _load():
            with get_session() as session:
                return {"items": stats_win_rates(session)}

        return await run_db(_load)

    @app.get("/api/stats/{asset}/{interval}")
    async def api_stats(asset: str, interval: str):
        def _load():
            with get_session() as session:
                return stats_for_market(session, asset, interval)

        row = await run_db(_load)
        if row is None:
            raise HTTPException(400, "Invalid asset or interval")
        return row

    @app.get("/api/export/{window_id}")
    async def api_export(window_id: int, format: str = "csv"):
        def _load():
            with get_session() as session:
                window = session.query(Window).filter_by(id=window_id).first()
                if not window:
                    return None
                if format != "csv":
                    return "bad_format"
                earliest = lookback_start(datetime.now(timezone.utc)).timestamp()
                if window.start_ts < earliest:
                    return "too_old"
                return export_window_csv(session, window)

        path = await run_db(_load)
        if path is None:
            raise HTTPException(404, "Window not found")
        if path == "bad_format":
            raise HTTPException(400, "Only csv download supported via API")
        if path == "too_old":
            raise HTTPException(
                403,
                f"Only the last {DATASET_LOOKBACK_DAYS} days can be downloaded. "
                f"For older data, contact {OWNER_CONTACT}",
            )
        return FileResponse(path, filename=path.name)

    return app


def _assert_port_free(host: str, port: int) -> None:
    """Fail before uvicorn boots the collector. On Windows the server lifespan
    runs before the socket bind, so a taken port otherwise starts feeds and
    then dies with WinError 10048.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        in_use = getattr(exc, "winerror", None) in (10048, 10013) or exc.errno in (98, 48, 13)
        if not in_use:
            raise
        log.error(
            "Port %s is already in use. Stop the other pmanalysis process, then run this again. "
            "Or choose a free port with PM_PORT.",
            port,
        )
        raise SystemExit(1) from exc
    finally:
        probe.close()


def run_web(*, embed_collector: bool = False) -> None:
    import uvicorn

    _assert_port_free(WEB_HOST, WEB_PORT)
    if embed_collector:
        os.environ["PM_EMBED_COLLECTOR"] = "1"
    uvicorn.run(
        "pmanalysis.web.app:create_app",
        factory=True,
        host=WEB_HOST,
        port=WEB_PORT,
        reload=False,
    )
