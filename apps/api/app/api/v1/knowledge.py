import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.session import get_async_db
from app.models.knowledge import KnowledgeDocument
from app.schemas.knowledge import (KnowledgeDocumentOut, KnowledgeSearchResult, KnowledgeTextCreate,
                                   KnowledgeWebsiteCreate)
from app.security.rbac import require_admin, require_any_role
from app.security.tenant import RequestContext
from app.services import audit_service, knowledge_service

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED = {".pdf": b"%PDF", ".docx": b"PK\x03\x04", ".txt": None}
UPLOAD_DIR = Path("/tmp/voxai-uploads")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[:120] or "upload"


def _extract_text(ext: str, data: bytes) -> str:
    if ext == ".txt":
        return data.decode("utf-8", errors="ignore")
    if ext == ".pdf":
        from pypdf import PdfReader
        import io
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    if ext == ".docx":
        import io
        from docx import Document
        return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    raise AppError("UNSUPPORTED_FILE", "Unsupported file type.", status_code=415)


@router.get("", response_model=list[KnowledgeDocumentOut])
async def list_docs(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    rows = await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == ctx.tenant_id)
                            .order_by(KnowledgeDocument.created_at.desc()))
    return rows.scalars().all()


@router.post("/upload", response_model=KnowledgeDocumentOut)
async def upload(file: UploadFile = File(...), ctx: RequestContext = Depends(require_admin),
                 db: AsyncSession = Depends(get_async_db)):
    name = _safe_name(file.filename or "upload")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise AppError("UNSUPPORTED_FILE", "Only PDF, DOCX and TXT files are accepted.", status_code=415)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise AppError("FILE_TOO_LARGE", "Maximum upload size is 10 MB.", status_code=413)
    magic = ALLOWED[ext]
    if magic and not data.startswith(magic):
        raise AppError("FILE_CONTENT_MISMATCH", "File content does not match its extension.", status_code=415)
    # malware-scan hook: plug ClamAV (clamd) here; see docs/security.md
    tenant_dir = UPLOAD_DIR / str(ctx.tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{ext}"
    (tenant_dir / stored).write_bytes(data)
    doc = KnowledgeDocument(tenant_id=ctx.tenant_id, source_type="document", title=name, original_filename=name,
                            storage_key=f"{ctx.tenant_id}/{stored}", uploaded_by_user_id=ctx.user_id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    try:
        text = _extract_text(ext, data)
    except Exception as exc:  # noqa: BLE001
        doc.status, doc.error = "failed", f"Could not parse file: {str(exc)[:300]}"
        await db.commit()
        return doc
    doc.raw_text = text
    result = await knowledge_service.ingest_document(db, ctx.tenant_id, doc, text)
    await audit_service.record(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="knowledge.uploaded",
                               resource_type="knowledge_document", resource_id=str(doc.id), metadata={"title": name})
    return result


@router.post("/text", response_model=KnowledgeDocumentOut)
async def add_text(payload: KnowledgeTextCreate, ctx: RequestContext = Depends(require_admin),
                   db: AsyncSession = Depends(get_async_db)):
    doc = KnowledgeDocument(tenant_id=ctx.tenant_id, source_type=payload.source_type, title=payload.title,
                            raw_text=payload.content, uploaded_by_user_id=ctx.user_id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return await knowledge_service.ingest_document(db, ctx.tenant_id, doc, payload.content)


@router.post("/website", response_model=KnowledgeDocumentOut)
async def add_website(payload: KnowledgeWebsiteCreate, ctx: RequestContext = Depends(require_admin),
                      db: AsyncSession = Depends(get_async_db)):
    doc = KnowledgeDocument(tenant_id=ctx.tenant_id, source_type="website", title=payload.url,
                            source_url=payload.url, uploaded_by_user_id=ctx.user_id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return await knowledge_service.ingest_website(db, ctx.tenant_id, doc, payload.url)


@router.post("/{doc_id}/reprocess", response_model=KnowledgeDocumentOut)
async def reprocess(doc_id: uuid.UUID, ctx: RequestContext = Depends(require_admin),
                    db: AsyncSession = Depends(get_async_db)):
    doc = (await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id,
                                                            KnowledgeDocument.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    if not doc:
        raise AppError("DOCUMENT_NOT_FOUND", "Document not found.", status_code=404)
    if not doc.raw_text:
        raise AppError("NO_SOURCE_TEXT", "Nothing stored to reprocess.", status_code=400)
    return await knowledge_service.ingest_document(db, ctx.tenant_id, doc, doc.raw_text)


@router.delete("/{doc_id}")
async def delete_doc(doc_id: uuid.UUID, ctx: RequestContext = Depends(require_admin),
                     db: AsyncSession = Depends(get_async_db)):
    doc = (await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.id == doc_id,
                                                            KnowledgeDocument.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    if not doc:
        raise AppError("DOCUMENT_NOT_FOUND", "Document not found.", status_code=404)
    await db.delete(doc)
    await db.commit()
    return {"deleted": True}


@router.get("/search", response_model=list[KnowledgeSearchResult])
async def search(q: str, top_k: int = 5, ctx: RequestContext = Depends(require_any_role),
                 db: AsyncSession = Depends(get_async_db)):
    return await knowledge_service.search_knowledge(db, ctx.tenant_id, q, top_k=min(top_k, 20))
