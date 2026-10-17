from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.session import get_async_db
from app.models.lead import Lead
from app.models.scheduling import Meeting
from app.providers.calendar.base import CalendarProviderError, get_calendar_provider
from app.schemas.meeting import MeetingCreateRequest, MeetingOut
from app.security.rbac import require_any_role, require_manager
from app.security.tenant import RequestContext
from app.core.config import settings
from app.services.integration_service import get_calendar_access_token

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.get("/availability")
async def calendar_availability(day: datetime, duration_minutes: int = Query(30, ge=15, le=120),
                                ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    provider_name = settings.CALENDAR_PROVIDER_MODE
    provider = get_calendar_provider(provider_name)
    token = await get_calendar_access_token(db, ctx.tenant_id) if provider_name == "google" else ""
    try:
        slots = await provider.check_availability(access_token=token, day=day, duration_minutes=duration_minutes)
    except CalendarProviderError as exc:
        raise AppError("CALENDAR_AVAILABILITY_FAILED", str(exc), status_code=409)
    return {"slots": [{"start": slot.start.isoformat(), "end": slot.end.isoformat()} for slot in slots]}


@router.get("", response_model=list[MeetingOut])
async def list_meetings(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    rows = await db.execute(select(Meeting).where(Meeting.tenant_id == ctx.tenant_id)
                            .order_by(Meeting.scheduled_at.asc().nullslast()).limit(500))
    return rows.scalars().all()


@router.post("", response_model=MeetingOut)
async def create_meeting(payload: MeetingCreateRequest, ctx: RequestContext = Depends(require_manager),
                         db: AsyncSession = Depends(get_async_db)):
    lead = (await db.execute(select(Lead).where(Lead.id == payload.lead_id, Lead.tenant_id == ctx.tenant_id))).scalar_one_or_none()
    if lead is None:
        raise AppError("LEAD_NOT_FOUND", "Lead not found.", status_code=404)
    provider_name = settings.CALENDAR_PROVIDER_MODE
    provider = get_calendar_provider(provider_name)
    meeting = Meeting(tenant_id=ctx.tenant_id, lead_id=lead.id, title=payload.title,
                      scheduled_at=payload.scheduled_at, duration_minutes=payload.duration_minutes,
                      status="requested", calendar_provider=provider_name)
    try:
        token = await get_calendar_access_token(db, ctx.tenant_id) if provider_name == "google" else ""
        booked = await provider.book_meeting(access_token=token, title=payload.title, start=payload.scheduled_at,
                                             duration_minutes=payload.duration_minutes, attendee_email=lead.email)
        # only mark booked once the provider confirmed it
        meeting.status = "booked"
        meeting.external_event_id = booked.external_event_id
        meeting.join_url = booked.join_url
    except CalendarProviderError:
        meeting.status = "requested"
        meeting.join_url = None
        meeting.external_event_id = None
    db.add(meeting)
    await db.commit()
    await db.refresh(meeting)
    return meeting
