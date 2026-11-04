import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.errors import AppError
from app.core.signing import make_oauth_state
from app.core.logging import get_logger
from app.models.crm import CRMConnection, CRMEvent, CRMSyncJob
from app.models.enums import CallingStatus, CRMConnectionStatus, CRMEventStatus, SyncJobStatus
from app.models.lead import Lead, LeadCallState
from app.models.scheduling import AutomationRunLog
from app.providers.crm.base import CRMProviderError, NormalizedLead
from app.services.dnc_service import apply_dnc_state
from app.providers.crm.factory import get_crm_provider

logger = get_logger("services.crm")

_PHONE_CLEAN_RE = re.compile(r"[^\d+]")


def normalize_phone(raw_phone: Optional[str]) -> Optional[str]:
    """
    Conservative E.164-ish normalization: strips formatting, assumes an
    Indian mobile number (10 digits, no country code) defaults to +91.
    This is intentionally simple and documented rather than a full
    libphonenumber integration, so behavior is easy to audit and adjust
    per deployment.
    """
    if not raw_phone:
        return None
    cleaned = _PHONE_CLEAN_RE.sub("", raw_phone)
    if not cleaned:
        return None
    if cleaned.startswith("+"):
        return cleaned
    digits = cleaned
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return f"+{digits}"


async def get_connection(db: AsyncSession, tenant_id: uuid.UUID, provider: str) -> Optional[CRMConnection]:
    result = await db.execute(
        select(CRMConnection).where(CRMConnection.tenant_id == tenant_id, CRMConnection.provider == provider)
    )
    return result.scalar_one_or_none()


async def start_oauth_connect(tenant_id: uuid.UUID, user_id: uuid.UUID, provider: str) -> str:
    crm = get_crm_provider(provider)
    return crm.get_authorization_url(state=make_oauth_state(tenant_id=tenant_id, user_id=user_id, provider=provider))


async def complete_oauth_connect(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, provider: str, code: str,
) -> CRMConnection:
    crm = get_crm_provider(provider)
    try:
        auth_result = await crm.exchange_code_for_token(code=code)
    except CRMProviderError as exc:
        raise AppError("CRM_OAUTH_FAILED", str(exc), status_code=400)

    connection = await get_connection(db, tenant_id, provider)
    if connection is None:
        connection = CRMConnection(tenant_id=tenant_id, provider=provider, connected_by_user_id=user_id)
        db.add(connection)

    connection.encrypted_access_token = encrypt_secret(auth_result.access_token)
    connection.encrypted_refresh_token = (
        encrypt_secret(auth_result.refresh_token) if auth_result.refresh_token else None
    )
    connection.token_expires_at = auth_result.expires_at
    connection.external_account_id = auth_result.external_account_id
    connection.external_account_name = auth_result.external_account_name
    connection.status = CRMConnectionStatus.CONNECTED.value  # becomes SYNCED once the initial sync succeeds
    connection.last_error = None
    await db.commit()
    await db.refresh(connection)
    return connection


async def connect_mock_or_config_provider(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, provider: str, config: Optional[dict],
) -> CRMConnection:
    """For providers that don't use OAuth (mock, custom_api) or need extra config (google_sheets)."""
    connection = await get_connection(db, tenant_id, provider)
    if connection is None:
        connection = CRMConnection(tenant_id=tenant_id, provider=provider, connected_by_user_id=user_id)
        db.add(connection)

    if provider == "mock":
        connection.encrypted_access_token = encrypt_secret("mock-access-token")
        connection.external_account_name = "Mock CRM Sandbox"
    elif provider == "custom_api":
        allowed = {"base_url", "list_path", "auth_header", "field_map", "note_path", "activity_path"}
        safe_config = {key: value for key, value in (config or {}).items() if key in allowed}
        connection.field_mapping_json = json.dumps(safe_config)
        api_key = (config or {}).get("api_key") or (config or {}).get("auth_value") or (config or {}).get("token")
        connection.encrypted_access_token = encrypt_secret(api_key) if api_key else None
        connection.external_account_name = (config or {}).get("name", provider)
    else:
        raise AppError("CRM_PROVIDER_UNSUPPORTED", "This CRM provider cannot be connected with static configuration.", status_code=400)

    connection.status = CRMConnectionStatus.CONNECTED.value
    connection.last_error = None
    await db.commit()
    await db.refresh(connection)
    return connection


async def get_valid_access_token(db: AsyncSession, connection: CRMConnection) -> str:
    """Return the connection token, rotating expired OAuth credentials before sync/config calls."""
    access_token = decrypt_secret(connection.encrypted_access_token) if connection.encrypted_access_token else ""
    expiry = connection.token_expires_at
    if expiry is None:
        return access_token
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry > datetime.now(timezone.utc) + timedelta(minutes=2):
        return access_token
    if not connection.encrypted_refresh_token:
        raise CRMProviderError("CRM access token expired and no refresh token is available.", retryable=False)
    crm = get_crm_provider(connection.provider)
    refreshed = await crm.refresh_access_token(refresh_token=decrypt_secret(connection.encrypted_refresh_token))
    connection.encrypted_access_token = encrypt_secret(refreshed.access_token)
    if refreshed.refresh_token:
        connection.encrypted_refresh_token = encrypt_secret(refreshed.refresh_token)
    connection.token_expires_at = refreshed.expires_at
    await db.commit()
    return refreshed.access_token


async def _upsert_lead(db: AsyncSession, tenant_id: uuid.UUID, connection: CRMConnection,
                        normalized: NormalizedLead) -> tuple[Lead, bool]:
    """Upsert-by-natural-key (tenant + provider + provider_lead_id). Returns (lead, created)."""
    result = await db.execute(
        select(Lead).where(
            Lead.tenant_id == tenant_id, Lead.crm_provider == connection.provider,
            Lead.provider_lead_id == normalized.provider_lead_id,
        )
    )
    lead = result.scalar_one_or_none()
    created = lead is None
    if lead is None:
        lead = Lead(
            tenant_id=tenant_id, crm_connection_id=connection.id, crm_provider=connection.provider,
            provider_lead_id=normalized.provider_lead_id,
        )
        db.add(lead)

    lead.name = normalized.name
    lead.phone = normalized.phone
    lead.normalized_phone = normalize_phone(normalized.phone)
    lead.email = normalized.email
    lead.company = normalized.company
    lead.crm_status = normalized.crm_status
    lead.crm_updated_at = normalized.crm_updated_at
    lead.source_metadata_json = json.dumps(normalized.raw)[:8000]
    custom_fields = normalized.raw.get("custom_fields") if isinstance(normalized.raw, dict) else None
    if isinstance(custom_fields, dict):
        lead.custom_fields_json = json.dumps(custom_fields)[:8000]
    await db.flush()

    state_result = await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))
    call_state = state_result.scalar_one_or_none()
    if call_state is None:
        call_state = LeadCallState(tenant_id=tenant_id, lead_id=lead.id, calling_status=CallingStatus.NOT_CALLED.value)
        db.add(call_state)
        await db.flush()
    if created:
        # Persist the automation work in the same transaction as the new lead.
        # A later sync can retry this row if dispatch fails or the process exits.
        db.add(AutomationRunLog(
            tenant_id=tenant_id,
            trigger_event="lead.created",
            resource_type="lead",
            resource_id=str(lead.id),
            success=False,
            detail="outbox:pending lead.created automation",
        ))
    await apply_dnc_state(db, tenant_id, lead, call_state)
    return lead, created


async def run_sync_job(db: AsyncSession, tenant_id: uuid.UUID, connection: CRMConnection,
                        job_type: str = "incremental") -> CRMSyncJob:
    """
    Fetches EVERY page for this sync, not just the first one - the provider's `has_more`/`next_cursor`
    drives a loop here so initial sync, incremental sync and reconciliation all fully paginate.
    """
    job = CRMSyncJob(tenant_id=tenant_id, crm_connection_id=connection.id, job_type=job_type,
                      status=SyncJobStatus.RUNNING.value, started_at=datetime.now(timezone.utc))
    db.add(job)
    await db.flush()

    crm = get_crm_provider(connection.provider)
    access_token = decrypt_secret(connection.encrypted_access_token) if connection.encrypted_access_token else ""
    connection.last_attempted_sync_at = datetime.now(timezone.utc)
    if job_type == "initial":
        connection.status = CRMConnectionStatus.IMPORTING.value
        await db.commit()

    pages_processed = 0
    MAX_PAGES = 500  # hard ceiling so a misbehaving provider can never loop forever

    # google_sheets/custom_api need connection-specific config (spreadsheet id, base URL, ...) that the
    # generic CRMProvider interface doesn't carry - passed through as extra kwargs for just those providers.
    provider_kwargs: dict = {}
    if connection.provider in ("google_sheets", "custom_api") and connection.field_mapping_json:
        try:
            cfg = json.loads(connection.field_mapping_json)
            if connection.provider == "google_sheets":
                provider_kwargs = {k: v for k, v in cfg.items() if k in ("spreadsheet_id", "sheet_range", "columns")}
            else:
                provider_kwargs = {k: v for k, v in cfg.items() if k in ("base_url", "list_path", "auth_header", "field_map")}
        except json.JSONDecodeError:
            pass
    elif connection.provider == "salesforce" and connection.external_account_name:
        provider_kwargs = {"instance_url": connection.external_account_name}

    try:
        access_token = await get_valid_access_token(db, connection)
        cursor = None if job_type == "initial" else connection.sync_cursor
        while True:
            if job_type == "initial" and cursor is None and pages_processed == 0:
                page = await crm.initial_sync(access_token=access_token, **provider_kwargs)
            else:
                page = await crm.fetch_leads(access_token=access_token, cursor=cursor,
                                              updated_since=connection.last_successful_sync_at
                                              if job_type != "initial" else None, **provider_kwargs)
            pages_processed += 1
            for normalized in page.leads:
                try:
                    async with db.begin_nested():  # savepoint: one bad record never discards the whole job
                        _, created = await _upsert_lead(db, tenant_id, connection, normalized)
                    job.records_fetched += 1
                    if created:
                        job.records_created += 1
                    else:
                        job.records_updated += 1
                except IntegrityError:
                    job.records_failed += 1
            cursor = page.next_cursor
            connection.sync_cursor = cursor
            if not page.has_more or not cursor or pages_processed >= MAX_PAGES:
                break

        job.pages_processed = pages_processed
        connection.last_successful_sync_at = datetime.now(timezone.utc)
        connection.last_error = None
        connection.status = CRMConnectionStatus.SYNCED.value
        job.status = SyncJobStatus.SUCCEEDED.value
    except CRMProviderError as exc:
        job.pages_processed = pages_processed
        job.status = SyncJobStatus.FAILED.value
        job.error = str(exc)
        connection.last_error = str(exc)
        if connection.last_successful_sync_at is None:
            # never had a successful sync -> surface the failure prominently rather than leaving it "connected"
            connection.status = CRMConnectionStatus.ERROR.value
    finally:
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(job)

    # Both the API's manual sync and the worker's scheduled sync pass through
    # here. The durable run-log row above makes this safe to retry on a later
    # sync if enqueueing fails or this process stops after the import commit.
    try:
        from app.automation.engine import retry_pending_lead_created_automations
        await retry_pending_lead_created_automations(db, tenant_id)
    except Exception as exc:  # noqa: BLE001 - pending rows remain available for the next sync
        logger.error("lead_created_automation_retry_failed", tenant_id=str(tenant_id), error=str(exc))
    await db.refresh(job)
    return job


async def register_webhooks(db: AsyncSession, connection: CRMConnection, callback_url: str) -> CRMConnection:
    crm = get_crm_provider(connection.provider)
    access_token = decrypt_secret(connection.encrypted_access_token) if connection.encrypted_access_token else ""
    subscription_id = await crm.register_webhooks(access_token=access_token, callback_url=callback_url)
    connection.webhook_enabled = True
    connection.webhook_subscription_id = subscription_id
    if not connection.webhook_secret:
        connection.webhook_secret = uuid.uuid4().hex
    await db.commit()
    await db.refresh(connection)
    return connection


async def ingest_webhook_event(
    db: AsyncSession, tenant_id: uuid.UUID, connection: CRMConnection, event_type: str, payload: dict,
) -> tuple[CRMEvent, bool]:
    """
    Returns (event, is_duplicate). Persists the event keyed on (tenant, provider, provider_event_id)
    BEFORE any processing. The unique constraint on that triple means a
    duplicate delivery raises IntegrityError here, which we catch and
    report as a no-op duplicate rather than processing twice.
    """
    provider = connection.provider  # read now: rollback below expires ORM attributes
    connection_id = connection.id
    crm = get_crm_provider(provider)
    provider_event_id = crm.extract_event_id(payload=payload)

    event = CRMEvent(
        tenant_id=tenant_id, crm_connection_id=connection_id, provider=provider,
        provider_event_id=provider_event_id, event_type=event_type, payload_json=json.dumps(payload),
        received_at=datetime.now(timezone.utc), status=CRMEventStatus.RECEIVED.value,
    )
    db.add(event)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.execute(
            select(CRMEvent).where(
                CRMEvent.tenant_id == tenant_id, CRMEvent.provider == provider,
                CRMEvent.provider_event_id == provider_event_id,
            )
        )
        return existing.scalar_one(), True

    await db.refresh(event)
    return event, False


async def process_webhook_event(db: AsyncSession, event: CRMEvent, connection: CRMConnection) -> None:
    """Fetches the latest record for the changed lead and upserts it locally."""
    crm = get_crm_provider(connection.provider)
    access_token = decrypt_secret(connection.encrypted_access_token) if connection.encrypted_access_token else ""
    payload = json.loads(event.payload_json)
    provider_lead_id = str(payload.get("objectId") or payload.get("id") or payload.get("lead_id") or "")

    event.status = CRMEventStatus.PROCESSING.value
    await db.commit()

    try:
        if provider_lead_id:
            normalized = await crm.fetch_lead(access_token=access_token, provider_lead_id=provider_lead_id)
            if normalized:
                await _upsert_lead(db, event.tenant_id, connection, normalized)
        event.status = CRMEventStatus.PROCESSED.value
        event.processed_at = datetime.now(timezone.utc)
    except CRMProviderError as exc:
        event.status = CRMEventStatus.FAILED.value
        event.error = str(exc)
    await db.commit()
    from app.automation.engine import retry_pending_lead_created_automations
    await retry_pending_lead_created_automations(db, event.tenant_id)


async def register_webhooks_safe(db: AsyncSession, connection: CRMConnection) -> None:
    """Best-effort webhook registration after OAuth; failure just means polling/reconciliation covers this CRM."""
    from app.core.config import settings
    try:
        await register_webhooks(db, connection, f"{settings.BACKEND_URL}/api/v1/webhooks/crm/{connection.provider}/{connection.id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook_registration_failed", provider=connection.provider, error=str(exc))
