"""
Celery tasks. Tasks are thin: each opens a DB session and calls into the
service layer. Long-running work (CRM sync, dialing) never runs in HTTP
handlers. Sync tasks run the async services via asyncio.run.
"""
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal
from app.workers.celery_app import celery_app

logger = get_logger("workers.tasks")


def _run(coro):
    """Each task runs on a fresh event loop; dispose pooled asyncpg connections afterwards so the
    next task never reuses connections bound to a closed loop."""
    async def wrapper():
        from app.db.session import async_engine
        try:
            return await coro
        finally:
            await async_engine.dispose()
    return asyncio.run(wrapper())


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=300, max_retries=5)
def process_crm_event(self, event_id: str):
    async def go():
        from app.models.crm import CRMConnection, CRMEvent
        from app.services import crm_service
        async with AsyncSessionLocal() as db:
            event = (await db.execute(select(CRMEvent).where(CRMEvent.id == uuid.UUID(event_id)))).scalar_one()
            conn = (await db.execute(select(CRMConnection).where(CRMConnection.id == event.crm_connection_id))).scalar_one()
            await crm_service.process_webhook_event(db, event, conn)
    _run(go())


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def run_crm_sync(self, tenant_id: str, connection_id: str, job_type: str = "incremental"):
    logger.info("run_crm_sync_start", tenant_id=tenant_id, connection_id=connection_id, job_type=job_type)
    async def go():
        from app.models.crm import CRMConnection
        from app.services import crm_service
        async with AsyncSessionLocal() as db:
            conn = (await db.execute(select(CRMConnection).where(CRMConnection.id == uuid.UUID(connection_id),
                                                                 CRMConnection.tenant_id == uuid.UUID(tenant_id)))).scalar_one()
            job = await crm_service.run_sync_job(db, conn.tenant_id, conn, job_type)
            logger.info("run_crm_sync_complete", tenant_id=tenant_id, connection_id=connection_id, job_type=job_type,
                        new_leads=job.records_created)
    _run(go())


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def run_lead_import(self, job_id: str):
    """Runs the confirmed Excel/CSV import in the background - used for files with >200 rows."""
    async def go():
        from app.models.import_job import LeadImportJob
        from app.services.lead_import_service import run_import
        async with AsyncSessionLocal() as db:
            job = (await db.execute(select(LeadImportJob).where(LeadImportJob.id == uuid.UUID(job_id)))).scalar_one()
            await run_import(db, job)
    _run(go())


@celery_app.task
def reconcile_all_crms():
    """Polling/reconciliation fallback: catches anything webhooks missed."""
    async def go():
        from app.models.crm import CRMConnection
        from app.core.config import settings
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(CRMConnection).where(
                CRMConnection.status.in_(("connected", "synced"))
            ))).scalars().all()
            logger.info("reconcile_all_crms_start", count=len(rows), providers=[c.provider for c in rows])
            return [(str(c.tenant_id), str(c.id), c.provider) for c in rows]
    for tenant_id, connection_id, provider in _run(go()):
        logger.info("reconcile_enqueue", tenant_id=tenant_id, connection_id=connection_id, provider=provider)
        run_crm_sync.delay(tenant_id, connection_id, "reconciliation")


@celery_app.task
def dispatch_due_call_jobs():
    async def go():
        from app.models.scheduling import CallJob
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(CallJob.id).where(CallJob.status == "pending",
                                                              CallJob.scheduled_at <= datetime.now(timezone.utc))
                                     .order_by(CallJob.priority, CallJob.scheduled_at).limit(50))).scalars().all()
            return [str(j) for j in rows]
    for job_id in _run(go()):  # fan out outside the event loop
        dial_call.delay(job_id)


@celery_app.task(bind=True, max_retries=3)
def dial_call(self, job_id: str):
    async def go():
        from app.models.lead import Lead, LeadCallState
        from app.models.scheduling import CallJob
        from app.models.tenant import Tenant, TenantBusinessRules
        from app.services.call_service import dispatch_call_job
        async with AsyncSessionLocal() as db:
            # claim the job atomically so two workers can't dial the same lead
            job = (await db.execute(select(CallJob).where(CallJob.id == uuid.UUID(job_id))
                                    .with_for_update(skip_locked=True))).scalar_one_or_none()
            if job is None or job.status != "pending":
                return
            job.status = "locked"
            await db.commit()
            lead = (await db.execute(select(Lead).where(Lead.id == job.lead_id))).scalar_one()
            tenant = (await db.execute(select(Tenant).where(Tenant.id == job.tenant_id))).scalar_one()
            rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == job.tenant_id))).scalar_one()
            state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
            # job itself is "locked" so exclude it from the active-job check by evaluating engine directly in dispatch
            await dispatch_call_job(db, job, tenant, rules, lead, state, settings_provider(), settings.BACKEND_URL)
    _run(go())


def settings_provider() -> str:
    return "mock" if settings.TELEPHONY_PROVIDER_MODE == "mock" else settings.TELEPHONY_PROVIDER_MODE


@celery_app.task
def enqueue_due_callbacks():
    async def go():
        from app.models.lead import Lead, LeadCallState
        from app.models.scheduling import Callback
        from app.models.tenant import Tenant, TenantBusinessRules
        from app.services.eligibility_service import enqueue_call_if_eligible
        async with AsyncSessionLocal() as db:
            due = (await db.execute(select(Callback).where(Callback.status == "scheduled",
                                                           Callback.callback_at <= datetime.now(timezone.utc)))).scalars().all()
            for cb in due:
                lead = (await db.execute(select(Lead).where(Lead.id == cb.lead_id))).scalar_one()
                state = (await db.execute(select(LeadCallState).where(LeadCallState.lead_id == lead.id))).scalar_one()
                tenant = (await db.execute(select(Tenant).where(Tenant.id == cb.tenant_id))).scalar_one()
                rules = (await db.execute(select(TenantBusinessRules).where(TenantBusinessRules.tenant_id == cb.tenant_id))).scalar_one()
                state.callback_at = None  # callback is now due; eligibility re-checks hours/DNC/etc.
                queued, _ = await enqueue_call_if_eligible(db, tenant, rules, lead, state, reason_hint=f"callback:{cb.id}")
                if queued:
                    cb.status = "completed"
                    await db.commit()
    _run(go())
