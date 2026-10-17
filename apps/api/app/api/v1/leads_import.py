"""
Excel/CSV lead import endpoints. Both formats share one pipeline
(app.services.lead_import_service); the two upload endpoints only differ
in which file extensions they accept, matching the shape requested by
the product spec (`/leads/import/excel/...`), while also accepting .csv
so there is exactly one implementation to maintain.
"""
import json
import re
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.session import get_async_db
from app.models.import_job import LeadImportJob
from app.security.rbac import require_admin
from app.security.tenant import RequestContext
from app.services import lead_import_service as svc

router = APIRouter(prefix="/leads/import", tags=["leads-import"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
EXCEL_EXT = {".xlsx", ".xls"}
ALL_EXT = EXCEL_EXT | {".csv"}


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[:180] or "upload"


class MappingRequest(BaseModel):
    job_id: uuid.UUID
    column_mapping: dict[str, str]  # {source_header: one of name|phone|email|company|status|source|notes}
    sheet_name: Optional[str] = None


class ConfirmRequest(BaseModel):
    job_id: uuid.UUID


def _job_out(job: LeadImportJob) -> dict:
    return {
        "job_id": str(job.id), "status": job.status, "source_type": job.source_type,
        "original_filename": job.original_filename, "sheet_name": job.sheet_name,
        "total_rows": job.total_rows, "valid_rows": job.valid_rows, "invalid_rows": job.invalid_rows,
        "duplicate_rows": job.duplicate_rows, "imported_rows": job.imported_rows, "skipped_rows": job.skipped_rows,
        "errors": json.loads(job.errors_json) if job.errors_json else [], "created_at": job.created_at,
        "error": job.error, "started_at": job.started_at, "finished_at": job.finished_at,
    }


async def _load_job(db: AsyncSession, ctx: RequestContext, job_id: uuid.UUID) -> LeadImportJob:
    job = (await db.execute(select(LeadImportJob).where(LeadImportJob.id == job_id,
                                                        LeadImportJob.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    if job is None:
        raise AppError("IMPORT_JOB_NOT_FOUND", "Import job not found.", status_code=404)
    return job


@router.post("/excel/preview", summary="Upload a file and preview its columns/rows (.xlsx, .xls, .csv)")
async def preview(file: UploadFile = File(...), sheet_name: Optional[str] = Form(None),
                  ctx: RequestContext = Depends(require_admin), db: AsyncSession = Depends(get_async_db)):
    name = _safe_name(file.filename or "upload")
    ext = Path(name).suffix.lower()
    if ext not in ALL_EXT:
        raise AppError("UNSUPPORTED_FILE", "Only .xlsx, .xls and .csv files are accepted.", status_code=415)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise AppError("FILE_TOO_LARGE", "Maximum upload size is 25 MB.", status_code=413)

    storage_key = svc.save_upload(ctx.tenant_id, name, data)
    source_type = "excel" if ext in EXCEL_EXT else "csv"
    path = svc.resolve_path(storage_key)

    sheets = svc.list_excel_sheets(path) if source_type == "excel" else None
    chosen_sheet = sheet_name or (sheets[0] if sheets else None)
    try:
        preview_data = svc.preview_rows(path, source_type, chosen_sheet)
    except Exception as exc:  # noqa: BLE001
        raise AppError("FILE_PARSE_FAILED", f"Could not read this file: {str(exc)[:300]}", status_code=422)

    job = LeadImportJob(tenant_id=ctx.tenant_id, source_type=source_type, original_filename=name,
                        storage_key=storage_key, sheet_name=chosen_sheet, status="uploaded",
                        total_rows=preview_data["total_rows"], created_by_user_id=ctx.user_id)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return {"job_id": str(job.id), "source_type": source_type, "sheets": sheets, "sheet_name": chosen_sheet,
           "headers": preview_data["headers"], "sample_rows": preview_data["sample_rows"],
           "total_rows": preview_data["total_rows"], "suggested_mapping": preview_data["suggested_mapping"]}


@router.post("/excel/{job_id}/sheet", summary="Select a workbook sheet and refresh its preview")
async def select_sheet(job_id: uuid.UUID, sheet_name: str = Form(...),
                       ctx: RequestContext = Depends(require_admin), db: AsyncSession = Depends(get_async_db)):
    job = await _load_job(db, ctx, job_id)
    if job.source_type != "excel":
        raise AppError("SHEET_NOT_SUPPORTED", "CSV imports do not contain workbook sheets.", status_code=400)
    path = svc.resolve_path(job.storage_key)
    sheets = svc.list_excel_sheets(path)
    if sheet_name not in sheets:
        raise AppError("SHEET_NOT_FOUND", "The selected sheet does not exist in this workbook.", status_code=400)
    preview_data = svc.preview_rows(path, job.source_type, sheet_name)
    job.sheet_name = sheet_name
    job.total_rows = preview_data["total_rows"]
    await db.commit()
    return {"job_id": str(job.id), "source_type": job.source_type, "sheets": sheets, "sheet_name": sheet_name,
            "headers": preview_data["headers"], "sample_rows": preview_data["sample_rows"],
            "total_rows": preview_data["total_rows"], "suggested_mapping": preview_data["suggested_mapping"]}


@router.post("/excel/validate", summary="Validate rows against a chosen column mapping (dry run, no writes)")
async def validate(payload: MappingRequest, ctx: RequestContext = Depends(require_admin),
                   db: AsyncSession = Depends(get_async_db)):
    job = await _load_job(db, ctx, payload.job_id)
    if job.status == "importing":
        raise AppError("IMPORT_IN_PROGRESS", "This import is already running.", status_code=409)
    if payload.sheet_name and payload.sheet_name != job.sheet_name:
        job.sheet_name = payload.sheet_name
    unknown = set(payload.column_mapping.values()) - set(svc.KNOWN_FIELDS)
    if unknown:
        raise AppError("INVALID_MAPPING", f"Unknown target field(s): {', '.join(sorted(unknown))}", status_code=400)
    if "name" not in payload.column_mapping.values() or "phone" not in payload.column_mapping.values():
        raise AppError("INVALID_MAPPING", "Both 'name' and 'phone' must be mapped to a column.", status_code=400)

    rows = await svc.load_all_rows(job)
    existing = await svc.existing_normalized_phones(db, ctx.tenant_id)
    results = svc.validate_and_dedupe_rows(rows, payload.column_mapping, existing)

    job.column_mapping_json = json.dumps(payload.column_mapping)
    job.total_rows = len(rows)
    job.valid_rows = sum(1 for r in results if not r.errors and not r.is_duplicate)
    job.invalid_rows = sum(1 for r in results if r.errors)
    job.duplicate_rows = sum(1 for r in results if r.is_duplicate and not r.errors)
    job.errors_json = json.dumps([{"row": r.index + 2, "errors": r.errors} for r in results if r.errors][:50])
    job.status = "validated"
    await db.commit()
    await db.refresh(job)
    return _job_out(job)


@router.post("/excel/confirm", summary="Import the validated rows (runs in the background for large files)")
async def confirm(payload: ConfirmRequest, ctx: RequestContext = Depends(require_admin),
                  db: AsyncSession = Depends(get_async_db)):
    job = await _load_job(db, ctx, payload.job_id)
    if job.status not in ("validated",):
        raise AppError("NOT_VALIDATED", "Call /leads/import/excel/validate before confirming.", status_code=409)
    job.status = "queued"
    await db.commit()
    if job.total_rows > 200:
        from app.workers.tasks import run_lead_import
        run_lead_import.delay(str(job.id))
    else:
        job = await svc.run_import(db, job)
    return {"queued": job.status == "queued", **_job_out(job)}


@router.get("/jobs", summary="List recent imports for the authenticated tenant")
async def list_jobs(ctx: RequestContext = Depends(require_admin), db: AsyncSession = Depends(get_async_db)):
    rows = await db.execute(
        select(LeadImportJob).where(LeadImportJob.tenant_id == ctx.tenant_id)
        .order_by(LeadImportJob.created_at.desc()).limit(50)
    )
    return [_job_out(job) for job in rows.scalars().all()]


@router.get("/{job_id}", summary="Poll import job status/stats")
async def status(job_id: uuid.UUID, ctx: RequestContext = Depends(require_admin), db: AsyncSession = Depends(get_async_db)):
    return _job_out(await _load_job(db, ctx, job_id))
