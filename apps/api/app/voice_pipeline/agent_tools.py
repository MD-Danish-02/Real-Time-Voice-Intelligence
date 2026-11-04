"""
The built-in AI agent's entire capability surface. The LLM only ever sees
these narrow, named tool schemas - never raw database or backend access.
Every executor is tenant-scoped and takes an explicit `lead_id`/`call_id`
resolved server-side from the active call, never taken from the LLM's
free-text output beyond the arguments schema allows.
"""
import uuid
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.crm import CRMConnection
from app.models.lead import Lead, LeadCallState
from app.models.scheduling import Callback, Meeting, WhatsAppMessage
from app.models.tenant import TenantBusinessRules
from app.providers.calendar.base import get_calendar_provider
from app.providers.crm.factory import get_crm_provider
from app.providers.whatsapp.base import get_whatsapp_provider
from app.services.knowledge_service import search_knowledge
from app.services.integration_service import get_calendar_access_token
from app.services import crm_service

TOOL_SCHEMAS: list[dict] = [
    {"name": "get_lead", "description": "Get full details for the current lead.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "search_knowledge", "description": "Search the business knowledge base for relevant information.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_previous_calls", "description": "Get summaries of previous calls with this lead.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "update_lead", "description": "Update the lead's status/notes in the CRM.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string"}, "note": {"type": "string"}}}},
    {"name": "add_crm_note", "description": "Add a note to the lead's CRM record.",
     "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"name": "create_crm_activity", "description": "Log a CRM activity for this call.",
     "parameters": {"type": "object", "properties": {
         "subject": {"type": "string"}, "description": {"type": "string"}}}},
    {"name": "check_calendar", "description": "Check available meeting slots for tomorrow onward.",
     "parameters": {"type": "object", "properties": {"duration_minutes": {"type": "integer"}}}},
    {"name": "book_meeting", "description": "Book a meeting at a specific ISO-8601 start time.",
     "parameters": {"type": "object", "properties": {
         "start_iso": {"type": "string"}, "duration_minutes": {"type": "integer"}, "title": {"type": "string"}},
      "required": ["start_iso"]}},
    {"name": "send_whatsapp", "description": "Send a WhatsApp message to the lead (e.g. meeting confirmation).",
     "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
    {"name": "schedule_callback", "description": "Schedule a callback at a specific ISO-8601 time.",
     "parameters": {"type": "object", "properties": {
         "callback_at_iso": {"type": "string"}, "reason": {"type": "string"}}, "required": ["callback_at_iso"]}},
    {"name": "transfer_to_human", "description": "Transfer the call to a human agent.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}}},
    {"name": "end_call", "description": "End the call gracefully.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}}},
]


class AgentToolExecutor:
    """
    Bound to one active call. Holds only the IDs needed to scope every
    tool call to the right tenant/lead - the LLM never supplies tenant_id.
    """

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID, lead: Lead, call_state: LeadCallState,
                 rules: TenantBusinessRules, call_id: uuid.UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.lead = lead
        self.call_state = call_state
        self.rules = rules
        self.call_id = call_id
        self.transfer_requested = False
        self.end_call_requested = False

    async def execute(self, tool_name: str, arguments: dict) -> dict:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        return await handler(**arguments)

    async def _tool_get_lead(self) -> dict:
        return {"name": self.lead.name, "phone": self.lead.phone, "email": self.lead.email,
                "company": self.lead.company, "crm_status": self.lead.crm_status}

    async def _tool_search_knowledge(self, query: str) -> dict:
        if self.db is None:
            return {"results": [{"content": "VoxAI provides voice AI calling agents starting at INR 15,000/month with full CRM integration, real-time speech analytics, and human agent handoff.", "title": "VoxAI Pricing and Solutions"}]}
        hits = await search_knowledge(self.db, self.tenant_id, query, top_k=3)
        return {"results": hits}

    async def _tool_get_previous_calls(self) -> dict:
        if self.db is None:
            return {"calls": []}
        from app.models.call import Call
        result = await self.db.execute(
            select(Call).where(Call.tenant_id == self.tenant_id, Call.lead_id == self.lead.id)
            .order_by(Call.created_at.desc()).limit(5)
        )
        calls = result.scalars().all()
        return {"calls": [{"id": str(c.id), "status": c.status, "started_at": str(c.started_at)} for c in calls]}

    async def _crm_connection(self) -> Optional[CRMConnection]:
        if self.db is None:
            return None
        result = await self.db.execute(
            select(CRMConnection).where(CRMConnection.tenant_id == self.tenant_id,
                                         CRMConnection.provider == self.lead.crm_provider)
        )
        return result.scalar_one_or_none()

    def _crm_writeback_kwargs(self, connection: CRMConnection) -> dict:
        if connection.provider == "custom_api" and connection.field_mapping_json:
            cfg = json.loads(connection.field_mapping_json)
            return {key: cfg[key] for key in ("base_url", "auth_header", "note_path", "activity_path") if key in cfg}
        if connection.provider == "salesforce" and connection.external_account_name:
            return {"instance_url": connection.external_account_name}
        return {}

    async def _tool_update_lead(self, status: Optional[str] = None, note: Optional[str] = None) -> dict:
        if not self.rules.crm_auto_update_enabled:
            return {"skipped": True, "reason": "CRM auto-update disabled for this tenant"}
        connection = await self._crm_connection()
        if not connection or not connection.encrypted_access_token or not self.lead.provider_lead_id:
            return {"skipped": True, "reason": "No CRM connection available for write-back"}
        crm = get_crm_provider(connection.provider)
        access_token = await crm_service.get_valid_access_token(self.db, connection)
        fields = {}
        if status:
            fields["status"] = status
        kwargs = self._crm_writeback_kwargs(connection)
        if fields:
            await crm.update_lead(access_token=access_token, provider_lead_id=self.lead.provider_lead_id,
                                   fields=fields, **{k: v for k, v in kwargs.items() if k in ("base_url", "auth_header", "instance_url")})
        if note:
            await crm.create_note(access_token=access_token, provider_lead_id=self.lead.provider_lead_id, note=note,
                                  **{k: v for k, v in kwargs.items() if k in ("base_url", "auth_header", "note_path", "instance_url")})
        if status:
            self.lead.crm_status = status
            from app.services.dnc_service import apply_dnc_state
            await apply_dnc_state(self.db, self.tenant_id, self.lead, self.call_state)
        return {"updated": True}

    async def _tool_add_crm_note(self, note: str) -> dict:
        return await self._tool_update_lead(note=note)

    async def _tool_create_crm_activity(self, subject: str = "AI Call", description: str = "") -> dict:
        if not self.rules.crm_auto_update_enabled:
            return {"skipped": True, "reason": "CRM auto-update disabled for this tenant"}
        connection = await self._crm_connection()
        if not connection or not connection.encrypted_access_token or not self.lead.provider_lead_id:
            return {"skipped": True, "reason": "No CRM connection available for write-back"}
        crm = get_crm_provider(connection.provider)
        access_token = await crm_service.get_valid_access_token(self.db, connection)
        kwargs = self._crm_writeback_kwargs(connection)
        await crm.create_activity(access_token=access_token, provider_lead_id=self.lead.provider_lead_id,
                                   activity={"subject": subject, "description": description},
                                   **{k: v for k, v in kwargs.items() if k in ("base_url", "auth_header", "activity_path", "instance_url")})
        return {"logged": True}

    async def _tool_check_calendar(self, duration_minutes: int = 30) -> dict:
        if not self.rules.meeting_booking_enabled:
            return {"available": False, "reason": "Meeting booking disabled for this tenant"}
        provider_name = settings.CALENDAR_PROVIDER_MODE
        provider = get_calendar_provider(provider_name)
        try:
            access_token = await get_calendar_access_token(self.db, self.tenant_id) if provider_name == "google" else ""
        except Exception as exc:  # return an explicit, actionable tool result to the agent
            return {"available": False, "reason": str(exc)}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        try:
            slots = await provider.check_availability(access_token=access_token, day=tomorrow, duration_minutes=duration_minutes)
        except Exception as exc:
            return {"available": False, "reason": str(exc)}
        return {"slots": [s.start.isoformat() for s in slots]}

    async def _tool_book_meeting(self, start_iso: str, duration_minutes: int = 30,
                                  title: str = "Demo Call") -> dict:
        if not self.rules.meeting_booking_enabled:
            return {"booked": False, "reason": "Meeting booking disabled for this tenant"}
        provider_name = settings.CALENDAR_PROVIDER_MODE
        provider = get_calendar_provider(provider_name)
        start = datetime.fromisoformat(start_iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        try:
            access_token = await get_calendar_access_token(self.db, self.tenant_id) if provider_name == "google" else ""
            booked = await provider.book_meeting(access_token=access_token, title=title, start=start,
                                                  duration_minutes=duration_minutes,
                                                  attendee_email=self.lead.email)
        except Exception as exc:
            return {"booked": False, "reason": str(exc)}
        meeting = Meeting(
            tenant_id=self.tenant_id, lead_id=self.lead.id, call_id=self.call_id, title=title,
            scheduled_at=start, duration_minutes=duration_minutes, status="booked",
            calendar_provider=provider_name, external_event_id=booked.external_event_id, join_url=booked.join_url,
        )
        if self.db is not None:
            self.db.add(meeting)
            await self.db.flush()
        return {"booked": True, "meeting_id": str(meeting.id), "join_url": booked.join_url}

    async def _tool_send_whatsapp(self, message: str) -> dict:
        if not self.rules.whatsapp_enabled or not self.lead.phone:
            return {"sent": False, "reason": "WhatsApp disabled or lead has no phone number"}
        provider = get_whatsapp_provider(settings.WHATSAPP_PROVIDER_MODE)
        result = await provider.send_message(to_number=self.lead.phone, body=message)
        if self.db is not None:
            self.db.add(WhatsAppMessage(
                tenant_id=self.tenant_id, lead_id=self.lead.id, call_id=self.call_id, to_number=self.lead.phone,
                body=message, status=result.status, provider_message_id=result.provider_message_id,
            ))
            await self.db.flush()
        return {"sent": True, "provider_message_id": result.provider_message_id}

    async def _tool_schedule_callback(self, callback_at_iso: str, reason: str = "") -> dict:
        if not self.rules.callback_enabled:
            return {"scheduled": False, "reason": "Callbacks disabled for this tenant"}
        callback_at = datetime.fromisoformat(callback_at_iso)
        if self.db is not None:
            callback = Callback(tenant_id=self.tenant_id, lead_id=self.lead.id, call_id=self.call_id,
                                 callback_at=callback_at, timezone=self.rules.timezone, reason=reason)
            self.db.add(callback)
            self.call_state.callback_at = callback_at
            self.call_state.calling_status = "callback_scheduled"
            await self.db.flush()
        return {"scheduled": True}

    async def _tool_transfer_to_human(self, reason: str = "") -> dict:
        self.transfer_requested = True
        return {"transfer_requested": True}

    async def _tool_end_call(self, reason: str = "") -> dict:
        self.end_call_requested = True
        return {"ending": True}
