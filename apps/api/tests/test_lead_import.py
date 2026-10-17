import io
import uuid

from sqlalchemy import create_engine, text

from app.core.config import settings


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv_bytes(rows: list[list[str]]) -> bytes:
    return "\n".join(",".join(c for c in row) for row in rows).encode()


HEADER = ["Full Name", "Phone Number", "Email", "Company", "Status"]
ROWS = [
    ["Asha Rao", "9876500001", "asha@example.com", "Rao Textiles", "New"],
    ["Bilal Khan", "9876500002", "bilal@example.com", "Khan Traders", "New"],
    ["", "9876500003", "", "", ""],                       # invalid: missing name
    ["Chetan Dev", "not-a-phone", "", "", ""],             # invalid: bad phone
    ["Deepa Iyer", "98765 00001", "", "", ""],             # duplicate of row 1 (same number, different formatting)
]


async def _upload_and_preview(client, headers, data: bytes, filename: str):
    r = await client.post("/api/v1/leads/import/excel/preview", headers=headers,
                          files={"file": (filename, io.BytesIO(data), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


async def test_excel_preview_detects_headers_and_suggests_mapping(client, tenant_a):
    p = await _upload_and_preview(client, tenant_a, _xlsx_bytes([HEADER] + ROWS), "leads.xlsx")
    assert p["source_type"] == "excel" and p["total_rows"] == len(ROWS)
    assert p["suggested_mapping"]["Full Name"] == "name" and p["suggested_mapping"]["Phone Number"] == "phone"
    assert len(p["sample_rows"]) == len(ROWS)


async def test_csv_preview_and_full_pipeline_real_stats(client, tenant_a):
    p = await _upload_and_preview(client, tenant_a, _csv_bytes([HEADER] + ROWS), "leads.csv")
    assert p["source_type"] == "csv"
    mapping = {"Full Name": "name", "Phone Number": "phone", "Email": "email", "Company": "company", "Status": "status"}
    v = await client.post("/api/v1/leads/import/excel/validate", headers=tenant_a,
                          json={"job_id": p["job_id"], "column_mapping": mapping})
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["status"] == "validated" and body["total_rows"] == 5
    assert body["valid_rows"] == 2 and body["invalid_rows"] == 2 and body["duplicate_rows"] == 1
    assert len(body["errors"]) == 2 and any("phone" in e["errors"][0].lower() for e in body["errors"])

    c = await client.post("/api/v1/leads/import/excel/confirm", headers=tenant_a, json={"job_id": p["job_id"]})
    assert c.status_code == 200, c.text
    final = c.json()
    assert final["queued"] is False and final["status"] == "completed"          # small file -> ran inline
    assert final["imported_rows"] == 2 and final["skipped_rows"] == 0
    assert final["imported_rows"] + final["invalid_rows"] + final["duplicate_rows"] == final["total_rows"]

    leads = await client.get("/api/v1/leads", headers=tenant_a)
    assert leads.json()["total"] == 2
    names = {l["name"] for l in leads.json()["items"]}
    assert names == {"Asha Rao", "Bilal Khan"}

    status_poll = await client.get(f"/api/v1/leads/import/{p['job_id']}", headers=tenant_a)
    assert status_poll.json()["status"] == "completed"


async def test_import_creates_call_jobs_via_lead_created_automation(client, tenant_a):
    from tests.conftest import open_hours
    await open_hours(client, tenant_a)
    p = await _upload_and_preview(client, tenant_a, _csv_bytes([HEADER, ROWS[0]]), "one.csv")
    mapping = {"Full Name": "name", "Phone Number": "phone", "Email": "email", "Company": "company", "Status": "status"}
    await client.post("/api/v1/leads/import/excel/validate", headers=tenant_a, json={"job_id": p["job_id"], "column_mapping": mapping})
    await client.post("/api/v1/leads/import/excel/confirm", headers=tenant_a, json={"job_id": p["job_id"]})
    eng = create_engine(settings.DATABASE_URL)
    with eng.begin() as c:
        assert c.execute(text("select count(*) from call_jobs")).scalar() == 1  # imported lead was auto-queued


async def test_reimporting_same_numbers_is_all_duplicates_second_time(client, tenant_a):
    for _ in range(2):
        p = await _upload_and_preview(client, tenant_a, _csv_bytes([HEADER] + ROWS[:2]), "dup.csv")
        mapping = {"Full Name": "name", "Phone Number": "phone", "Email": "email", "Company": "company", "Status": "status"}
        await client.post("/api/v1/leads/import/excel/validate", headers=tenant_a, json={"job_id": p["job_id"], "column_mapping": mapping})
        r = await client.post("/api/v1/leads/import/excel/confirm", headers=tenant_a, json={"job_id": p["job_id"]})
    assert r.json()["duplicate_rows"] == 2 and r.json()["imported_rows"] == 0
    assert (await client.get("/api/v1/leads", headers=tenant_a)).json()["total"] == 2


async def test_invalid_mapping_and_missing_required_fields_rejected(client, tenant_a):
    p = await _upload_and_preview(client, tenant_a, _csv_bytes([HEADER] + ROWS[:1]), "x.csv")
    bad = await client.post("/api/v1/leads/import/excel/validate", headers=tenant_a,
                            json={"job_id": p["job_id"], "column_mapping": {"Full Name": "name"}})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_MAPPING"
    bad2 = await client.post("/api/v1/leads/import/excel/validate", headers=tenant_a,
                             json={"job_id": p["job_id"], "column_mapping": {"Full Name": "not_a_real_field"}})
    assert bad2.status_code == 400


async def test_unsupported_file_type_and_confirm_before_validate_rejected(client, tenant_a):
    bad = await client.post("/api/v1/leads/import/excel/preview", headers=tenant_a,
                            files={"file": ("virus.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert bad.status_code == 415
    p = await _upload_and_preview(client, tenant_a, _csv_bytes([HEADER, ROWS[0]]), "y.csv")
    early = await client.post("/api/v1/leads/import/excel/confirm", headers=tenant_a, json={"job_id": p["job_id"]})
    assert early.status_code == 409 and early.json()["error"]["code"] == "NOT_VALIDATED"


async def test_import_jobs_are_tenant_isolated(client, tenant_a, tenant_b):
    p = await _upload_and_preview(client, tenant_a, _csv_bytes([HEADER, ROWS[0]]), "z.csv")
    assert (await client.get(f"/api/v1/leads/import/{p['job_id']}", headers=tenant_b)).status_code == 404
    assert (await client.post("/api/v1/leads/import/excel/validate", headers=tenant_b,
                              json={"job_id": p["job_id"], "column_mapping": {"Full Name": "name", "Phone Number": "phone"}})).status_code == 404


async def test_large_import_is_dispatched_to_the_background_worker_not_run_inline(client, tenant_a):
    """>200 rows must NOT block the HTTP request - it is hard to run Celery's eager mode inside this same
    event loop (see test_workers.py), so this checks the dispatch decision, then runs the same worker
    function directly (as the real Celery worker process would) and verifies the end state."""
    big_rows = [HEADER] + [[f"Lead {i}", f"98765{i:05d}", "", "", "New"] for i in range(201)]
    p = await _upload_and_preview(client, tenant_a, _csv_bytes(big_rows), "big.csv")
    mapping = {"Full Name": "name", "Phone Number": "phone"}
    await client.post("/api/v1/leads/import/excel/validate", headers=tenant_a, json={"job_id": p["job_id"], "column_mapping": mapping})
    c = await client.post("/api/v1/leads/import/excel/confirm", headers=tenant_a, json={"job_id": p["job_id"]})
    assert c.status_code == 200 and c.json()["queued"] is True and c.json()["status"] == "queued"

    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models.import_job import LeadImportJob
    from app.services.lead_import_service import run_import
    async with AsyncSessionLocal() as db:
        job = (await db.execute(select(LeadImportJob).where(LeadImportJob.id == uuid.UUID(p["job_id"])))).scalar_one()
        await run_import(db, job)

    status = await client.get(f"/api/v1/leads/import/{p['job_id']}", headers=tenant_a)
    assert status.json()["status"] == "completed" and status.json()["imported_rows"] == 201
