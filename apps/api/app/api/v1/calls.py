import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.session import get_async_db
from app.models.call import Call, CallAnalysis, Transcript, TranscriptSegment
from app.schemas.call import CallAnalysisOut, CallDetailOut, CallOut, OutboundDialRequest, TranscriptSegmentOut
from app.schemas.common import Page
from app.security.rbac import require_any_role, require_manager
from app.security.tenant import RequestContext

router = APIRouter(prefix="/calls", tags=["calls"])


@router.post("/dial", response_model=CallOut)
async def dial_outbound(
    payload: OutboundDialRequest,
    ctx: RequestContext = Depends(require_manager),
    db: AsyncSession = Depends(get_async_db),
):
    """Directly initiates an outbound phone call to a recipient."""
    from app.services import call_service
    call = await call_service.initiate_outbound_call(
        db,
        tenant_id=ctx.tenant_id,
        to_number=payload.to_number,
        lead_id=payload.lead_id,
        custom_prompt=payload.custom_prompt,
    )
    return call


@router.get("", response_model=Page[CallOut])
async def list_calls(direction: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1),
                     page_size: int = Query(25, ge=1, le=100), ctx: RequestContext = Depends(require_any_role),
                     db: AsyncSession = Depends(get_async_db)):
    stmt = select(Call).where(Call.tenant_id == ctx.tenant_id)
    if direction:
        stmt = stmt.where(Call.direction == direction)
    if status:
        stmt = stmt.where(Call.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = (await db.execute(stmt.order_by(Call.created_at.desc()).offset((page - 1) * page_size)
                              .limit(page_size))).scalars().all()
    return Page[CallOut](items=items, total=total, page=page, page_size=page_size)


@router.get("/{call_id}", response_model=CallDetailOut)
async def call_detail(call_id: uuid.UUID, ctx: RequestContext = Depends(require_any_role),
                      db: AsyncSession = Depends(get_async_db)):
    call = (await db.execute(select(Call).where(Call.id == call_id, Call.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    if call is None:
        raise AppError("CALL_NOT_FOUND", "Call not found.", status_code=404)
    segs = (await db.execute(
        select(TranscriptSegment).join(Transcript, Transcript.id == TranscriptSegment.transcript_id)
        .where(Transcript.call_id == call.id, TranscriptSegment.tenant_id == ctx.tenant_id)
        .order_by(TranscriptSegment.started_at_ms))).scalars().all()
    analysis = (await db.execute(select(CallAnalysis).where(CallAnalysis.call_id == call.id,
                                                            CallAnalysis.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    out = CallDetailOut.model_validate(call)
    out.transcript_segments = [TranscriptSegmentOut.model_validate(s) for s in segs]
    out.analysis = CallAnalysisOut.model_validate(analysis) if analysis else None
    return out
