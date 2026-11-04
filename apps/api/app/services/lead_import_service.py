"""
Excel/CSV lead import. Both formats funnel through the same pipeline so
there is exactly one implementation of normalization, validation and
deduplication:

    parse (streamed, not loaded fully into memory for xlsx/csv)
      -> LeadNormalizer (reuses crm_service.normalize_phone)
      -> LeadValidator (required fields, phone format)
      -> LeadDeduplicator (against already-imported rows in this file,
         AND against existing leads in the database)
      -> LeadRepository (upsert, via the same _upsert_lead path CRM sync
         uses, so imported leads get a LeadCallState + DNC check for free)
"""
import csv
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.models.crm import CRMConnection
from app.models.enums import CRMConnectionStatus
from app.models.import_job import LeadImportJob
from app.models.lead import Lead
from app.providers.crm.base import NormalizedLead
from app.services.crm_service import _upsert_lead, normalize_phone

logger = get_logger("services.lead_import")

UPLOAD_DIR = Path("/tmp/voxai-lead-imports")
REQUIRED_FIELDS = {"name", "phone"}
KNOWN_FIELDS = ["name", "phone", "email", "company", "status", "source", "notes"]
MAX_ROWS = 50_000
PREVIEW_ROWS = 10


@dataclass
class ParsedRow:
    index: int  # 0-based data row index (excludes header)
    raw: dict[str, str]


@dataclass
class RowResult:
    index: int
    raw: dict
    normalized: Optional[NormalizedLead] = None
    errors: list[str] = field(default_factory=list)
    is_duplicate: bool = False
    duplicate_reason: str = ""


# ---------- parsing ----------

def list_excel_sheets(path: Path) -> list[str]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return wb.sheetnames
    finally:
        wb.close()


def iter_excel_rows(path: Path, sheet_name: str) -> Iterator[list[str]]:
    """Streams rows via openpyxl's read_only mode - the whole file is never loaded into memory at once."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet_name]
        for row in ws.iter_rows(values_only=True):
            yield ["" if v is None else str(v).strip() for v in row]
    finally:
        wb.close()


def iter_csv_rows(path: Path) -> Iterator[list[str]]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.reader(f):
            yield [c.strip() for c in row]


def _rows_iterator(path: Path, source_type: str, sheet_name: Optional[str]) -> Iterator[list[str]]:
    if source_type == "excel":
        return iter_excel_rows(path, sheet_name or list_excel_sheets(path)[0])
    return iter_csv_rows(path)


def detect_headers(path: Path, source_type: str, sheet_name: Optional[str]) -> list[str]:
    for row in _rows_iterator(path, source_type, sheet_name):
        return [h or f"column_{i+1}" for i, h in enumerate(row)]
    return []


def auto_map_columns(headers: list[str]) -> dict[str, str]:
    """Best-effort header -> known-field mapping the UI can show and let the user correct."""
    mapping: dict[str, str] = {}
    aliases = {
        "name": {"name", "full name", "fullname", "contact", "contact name", "lead name"},
        "phone": {"phone", "phone number", "mobile", "mobile number", "contact number", "cell"},
        "email": {"email", "email address", "e-mail"},
        "company": {"company", "company name", "organization", "organisation", "business"},
        "status": {"status", "lead status", "stage"},
        "source": {"source", "lead source"},
        "notes": {"notes", "note", "comments", "remarks"},
    }
    for header in headers:
        key = header.strip().lower()
        for field_name, alts in aliases.items():
            if key in alts and field_name not in mapping.values():
                mapping[header] = field_name
                break
    return mapping


def preview_rows(path: Path, source_type: str, sheet_name: Optional[str], limit: int = PREVIEW_ROWS) -> dict:
    rows_iter = _rows_iterator(path, source_type, sheet_name)
    headers = next(rows_iter, [])
    headers = [h or f"column_{i+1}" for i, h in enumerate(headers)]
    sample = []
    total = 0
    for row in rows_iter:
        total += 1
        if len(sample) < limit:
            sample.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))
    return {"headers": headers, "sample_rows": sample, "total_rows": total,
            "suggested_mapping": auto_map_columns(headers)}


# ---------- validation + dedup ----------

def normalize_row(raw: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    """mapping is {source_header: known_field}."""
    out: dict[str, str] = {}
    for header, field_name in mapping.items():
        if field_name in KNOWN_FIELDS:
            out[field_name] = (raw.get(header) or "").strip()
    return out


def validate_and_dedupe_rows(
    all_rows: list[dict[str, str]], mapping: dict[str, str], existing_phones: set[str],
) -> list[RowResult]:
    results: list[RowResult] = []
    seen_in_file: set[str] = set()
    for i, raw in enumerate(all_rows):
        mapped = normalize_row(raw, mapping)
        result = RowResult(index=i, raw=raw)
        name = mapped.get("name") or ""
        phone_raw = mapped.get("phone") or ""
        if not name:
            result.errors.append("Missing required field: name")
        if not phone_raw:
            result.errors.append("Missing required field: phone")
        normalized_phone = normalize_phone(phone_raw) if phone_raw else None
        if phone_raw and (not normalized_phone or len(normalized_phone) < 8):
            result.errors.append(f"Invalid phone number: '{phone_raw}'")
        email = mapped.get("email") or None
        if email and "@" not in email:
            result.errors.append(f"Invalid email address: '{email}'")

        if not result.errors:
            if normalized_phone in seen_in_file:
                result.is_duplicate = True
                result.duplicate_reason = "Duplicate phone number within this file"
            elif normalized_phone in existing_phones:
                result.is_duplicate = True
                result.duplicate_reason = "A lead with this phone number already exists"
            else:
                seen_in_file.add(normalized_phone)
                result.normalized = NormalizedLead(
                    provider_lead_id=f"import-{uuid.uuid4().hex}",
                    name=name, phone=phone_raw, email=email, company=mapped.get("company") or None,
                    crm_status=mapped.get("status") or "New", crm_updated_at=datetime.now(timezone.utc),
                    raw={"source": mapped.get("source") or "file_import", "notes": mapped.get("notes") or "",
                        **raw, "custom_fields": {k: v for k, v in raw.items() if k not in mapping}},
                )
        results.append(result)
    return results


async def existing_normalized_phones(db: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    rows = await db.execute(select(Lead.normalized_phone).where(Lead.tenant_id == tenant_id,
                                                                 Lead.normalized_phone.isnot(None)))
    return {r[0] for r in rows.all()}


async def get_or_create_import_connection(db: AsyncSession, tenant_id: uuid.UUID) -> CRMConnection:
    """Imported leads need a `crm_connection_id`/provider like any other lead; a single
    per-tenant 'file_import' pseudo-connection represents 'imported from a file' as the source."""
    result = await db.execute(select(CRMConnection).where(CRMConnection.tenant_id == tenant_id,
                                                           CRMConnection.provider == "file_import"))
    conn = result.scalar_one_or_none()
    if conn is None:
        conn = CRMConnection(tenant_id=tenant_id, provider="file_import",
                             status=CRMConnectionStatus.SYNCED.value, external_account_name="File imports")
        db.add(conn)
        await db.flush()
    return conn


def save_upload(tenant_id: uuid.UUID, filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    tenant_dir = UPLOAD_DIR / str(tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{ext}"
    (tenant_dir / stored).write_bytes(data)
    return f"{tenant_id}/{stored}"


def resolve_path(storage_key: str) -> Path:
    return UPLOAD_DIR / storage_key


async def load_all_rows(job: LeadImportJob) -> list[dict[str, str]]:
    path = resolve_path(job.storage_key)
    if not path.exists():
        raise AppError("IMPORT_FILE_MISSING", "The uploaded file is no longer available - please re-upload.",
                       status_code=410)
    rows_iter = _rows_iterator(path, job.source_type, job.sheet_name)
    headers = next(rows_iter, [])
    headers = [h or f"column_{i+1}" for i, h in enumerate(headers)]
    out = []
    for row in rows_iter:
        if len(out) >= MAX_ROWS:
            break
        out.append(dict(zip(headers, row + [""] * (len(headers) - len(row)))))
    return out


async def run_import(db: AsyncSession, job: LeadImportJob) -> LeadImportJob:
    """The 'confirm' step: re-parses the file with the stored mapping and upserts every valid, non-duplicate
    row through the same lead-upsert path CRM sync uses (dedup, phone normalization, DNC check, LeadCallState)."""
    from app.automation.engine import retry_pending_lead_created_automations

    job.status = "importing"
    job.started_at = datetime.now(timezone.utc)
    # reset counters: confirm re-validates from scratch (existing-lead set may have changed since /validate)
    job.total_rows = job.valid_rows = job.invalid_rows = job.duplicate_rows = 0
    job.imported_rows = job.skipped_rows = 0
    await db.commit()

    try:
        mapping = json.loads(job.column_mapping_json or "{}")
        rows = await load_all_rows(job)
        job.total_rows = len(rows)
        existing = await existing_normalized_phones(db, job.tenant_id)
        results = validate_and_dedupe_rows(rows, mapping, existing)
        conn = await get_or_create_import_connection(db, job.tenant_id)

        errors_sample = []
        for r in results:
            if r.errors:
                job.invalid_rows += 1
                if len(errors_sample) < 50:
                    errors_sample.append({"row": r.index + 2, "errors": r.errors})  # +2: header + 1-based
                continue
            if r.is_duplicate:
                job.duplicate_rows += 1
                continue
            job.valid_rows += 1
            try:
                async with db.begin_nested():
                    await _upsert_lead(db, job.tenant_id, conn, r.normalized)
                job.imported_rows += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not abort the whole import
                job.skipped_rows += 1
                if len(errors_sample) < 50:
                    errors_sample.append({"row": r.index + 2, "errors": [str(exc)[:200]]})

        job.errors_json = json.dumps(errors_sample)
        job.status = "completed"
        await db.commit()

        from app.services import audit_service
        await audit_service.record(db, tenant_id=job.tenant_id, actor_user_id=job.created_by_user_id,
                                   action="lead.imported", resource_type="lead_import_job", resource_id=str(job.id),
                                   metadata={"imported_rows": job.imported_rows, "filename": job.original_filename})

        try:
            await retry_pending_lead_created_automations(db, job.tenant_id)
        except Exception as exc:  # noqa: BLE001 - imported leads and pending automation are already durable
            logger.error("lead_import_automation_retry_failed", job_id=str(job.id), error=str(exc))
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = str(exc)[:2000]
        await db.commit()
        logger.error("lead_import_failed", job_id=str(job.id), error=str(exc))
    finally:
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(job)
    return job
