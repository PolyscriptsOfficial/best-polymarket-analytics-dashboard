""" /v1 history dataset HTTP API (public, same as /api/windows)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from pmanalysis.datasets.schema import SpecError, canonical_assets, canonical_intervals, parse_spec
from pmanalysis.datasets.service import DatasetService
from pmanalysis.db.session import run_db

router = APIRouter(prefix="/v1", tags=["datasets"])


class DatasetCreateBody(BaseModel):
    start: datetime
    end: datetime
    assets: list[str]
    intervals: list[str]
    force: bool = False


def _service(request: Request) -> DatasetService:
    service = getattr(request.app.state, "dataset_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dataset service is not running")
    return service


@router.get("/coverage")
async def api_coverage(
    request: Request,
    asset: str | None = Query(default=None),
    interval: str | None = Query(default=None),
):
    try:
        asset_f = canonical_assets([asset])[0] if asset else None
        interval_f = canonical_intervals([interval])[0] if interval else None
    except SpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    service = _service(request)
    return await run_db(lambda: service.coverage(asset=asset_f, interval=interval_f))


@router.post("/datasets")
async def api_create_dataset(request: Request, body: DatasetCreateBody):
    try:
        spec = parse_spec(
            start=body.start,
            end=body.end,
            assets=body.assets,
            intervals=body.intervals,
        )
    except SpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    service = _service(request)
    record, new_build = await run_db(lambda: service.submit(spec, force=body.force))
    payload = record.public()
    status_code = 202 if new_build or record.status == "building" else 200
    return JSONResponse(payload, status_code=status_code)


@router.get("/datasets/{dataset_id}")
async def api_dataset_status(request: Request, dataset_id: str):
    record = _service(request).store.get(dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown dataset id")
    return record.public()


@router.get("/datasets/{dataset_id}/download")
async def api_dataset_download(request: Request, dataset_id: str):
    service = _service(request)
    record = service.store.get(dataset_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown dataset id")
    if record.status == "building":
        raise HTTPException(status_code=409, detail="Dataset is still building")
    if record.status == "failed":
        raise HTTPException(status_code=500, detail=record.error or "Dataset failed")
    if record.zip_path is None or not record.zip_path.exists():
        raise HTTPException(status_code=404, detail="Dataset zip is missing")
    service.store.touch(record)
    return FileResponse(
        record.zip_path,
        media_type="application/zip",
        filename=record.filename,
    )
