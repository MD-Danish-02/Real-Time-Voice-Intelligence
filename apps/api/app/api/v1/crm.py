
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from fastapi.responses import RedirectResponse

from app.core.errors import AppError
from app.core.signing import read_oauth_state
from app.db.session import get_async_db
from app.events.bus import publish_event
from app.models.crm import CRMConnection, CRMSyncJob
from app.providers.crm.factory import get_crm_provider
from app.schemas.crm import CRMConnectionOut, CRMConnectRequest, CRMSyncJobOut
from app.security.rbac import require_admin, require_any_role
from app.security.tenant import RequestContext
from app.services import audit_service, crm_service

router = APIRouter(prefix="/crm", tags=["crm"])

NON_OAUTH = {"mock", "custom_api"}  # Google Sheets uses real Google OAuth (below); file imports use the import API.


@router.get("/connections", response_model=list[CRMConnectionOut])
async def list_connections(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    rows = await db.execute(select(CRMConnection).where(CRMConnection.tenant_id == ctx.tenant_id))
    return rows.scalars().all()


@router.post("/connect", summary="Start connecting a CRM")
async def connect(payload: CRMConnectRequest, ctx: RequestContext = Depends(require_admin),
                  db: AsyncSession = Depends(get_async_db)):
    if payload.provider in NON_OAUTH:
        if payload.provider == "mock" and settings.CRM_PROVIDER_MODE != "mock":
            raise AppError("MOCK_DISABLED", "Mock CRM is disabled outside development mode.", status_code=403)
        conn = await crm_service.connect_mock_or_config_provider(
            db, ctx.tenant_id, ctx.user_id, payload.provider, payload.config)
        await audit_service.record(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="crm.connected",
                                   resource_type="crm_connection", resource_id=str(conn.id),
                                   metadata={"provider": payload.provider})
        sync_queued = False
        sync_error = None
        if payload.provider in {"mock", "custom_api"}:
            try:
                from app.workers.tasks import run_crm_sync
                run_crm_sync.delay(str(ctx.tenant_id), str(conn.id), "initial")
                sync_queued = True
            except Exception as exc:  # noqa: BLE001 - connection remains visible with an actionable state
                conn.last_error = f"Connection saved, but initial sync could not be queued: {str(exc)[:200]}"
                await db.commit()
                sync_error = conn.last_error
        return {"connected": True, "connection_id": str(conn.id), "sync_queued": sync_queued,
                "sync_error": sync_error}
    try:
        url = await crm_service.start_oauth_connect(ctx.tenant_id, ctx.user_id, payload.provider)
    except Exception as exc:  # noqa: BLE001 - surfaces a clear "not configured" error instead of a fake success
        from app.providers.crm.base import CRMProviderError
        if isinstance(exc, CRMProviderError):
            raise AppError("PROVIDER_NOT_CONFIGURED", str(exc), status_code=409)
        raise
    return {"connected": False, "authorization_url": url}


@router.put("/{provider}/config", summary="Save provider-specific config (Google Sheets spreadsheet, Custom API endpoint)")
async def save_config(provider: str, config: dict, ctx: RequestContext = Depends(require_admin),
                      db: AsyncSession = Depends(get_async_db)):
    conn = await crm_service.get_connection(db, ctx.tenant_id, provider)
    if conn is None:
        raise AppError("CRM_NOT_CONNECTED", "Connect this CRM first.", status_code=404)
    import json
    if provider == "google_sheets":
        spreadsheet_id = config.get("spreadsheet_id")
        sheet_range = config.get("sheet_range")
        columns = config.get("columns")
        if not isinstance(spreadsheet_id, str) or not spreadsheet_id.strip() or not isinstance(sheet_range, str) or not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            raise AppError("INVALID_SHEETS_CONFIG", "Spreadsheet ID, sheet range, and column mapping are required.", status_code=422)
        safe_config = {"spreadsheet_id": spreadsheet_id.strip(), "sheet_range": sheet_range, "columns": columns}
    elif provider == "custom_api":
        safe_config = {key: config[key] for key in ("base_url", "list_path", "auth_header", "field_map", "note_path", "activity_path") if key in config}
        if not isinstance(safe_config.get("base_url"), str) or not isinstance(safe_config.get("field_map", {}), dict):
            raise AppError("INVALID_CUSTOM_API_CONFIG", "A base URL and field mapping are required.", status_code=422)
        token = config.get("api_key") or config.get("auth_value")
        if token:
            from app.core.crypto import encrypt_secret
            conn.encrypted_access_token = encrypt_secret(str(token))
    else:
        raise AppError("CRM_CONFIG_UNSUPPORTED", "Configuration is not supported for this CRM provider.", status_code=400)
    conn.field_mapping_json = json.dumps(safe_config)
    await db.commit()
    return {"saved": True}


async def _sheets_connection(db: AsyncSession, tenant_id, spreadsheet_id: str):
    conn = await crm_service.get_connection(db, tenant_id, "google_sheets")
    if conn is None or not conn.encrypted_access_token:
        raise AppError("CRM_NOT_CONNECTED", "Connect Google Sheets with OAuth first.", status_code=404)
    try:
        token = await crm_service.get_valid_access_token(db, conn)
    except Exception as exc:  # noqa: BLE001
        raise AppError("GOOGLE_TOKEN_REFRESH_FAILED", str(exc), status_code=409)
    return get_crm_provider("google_sheets"), token, spreadsheet_id


@router.get("/google_sheets/sheets", summary="List available tabs in an authorized spreadsheet")
async def google_sheets_list_sheets(spreadsheet_id: str = Query(min_length=10, max_length=200),
                                    ctx: RequestContext = Depends(require_admin),
                                    db: AsyncSession = Depends(get_async_db)):
    provider, token, sheet_id = await _sheets_connection(db, ctx.tenant_id, spreadsheet_id)
    try:
        sheets = await provider.list_sheets(access_token=token, spreadsheet_id=sheet_id)
    except Exception as exc:  # noqa: BLE001
        raise AppError("GOOGLE_SHEETS_READ_FAILED", str(exc), status_code=502)
    return {"sheets": sheets}


@router.get("/google_sheets/headers", summary="Detect the header row for a selected spreadsheet tab")
async def google_sheets_headers(spreadsheet_id: str = Query(min_length=10, max_length=200),
                                sheet_name: str = Query(min_length=1, max_length=200),
                                ctx: RequestContext = Depends(require_admin),
                                db: AsyncSession = Depends(get_async_db)):
    provider, token, sheet_id = await _sheets_connection(db, ctx.tenant_id, spreadsheet_id)
    try:
        headers = await provider.get_headers(access_token=token, spreadsheet_id=sheet_id, sheet_name=sheet_name)
    except Exception as exc:  # noqa: BLE001
        raise AppError("GOOGLE_SHEETS_READ_FAILED", str(exc), status_code=502)
    if not headers:
        raise AppError("GOOGLE_SHEETS_NO_HEADERS", "The selected tab has no header row.", status_code=422)
    return {"headers": headers}


@router.get("/oauth/{provider}/callback", summary="OAuth redirect target (no bearer header: identity comes from the signed state)")
async def oauth_callback(provider: str, code: str, state: str, db: AsyncSession = Depends(get_async_db)):
    try:
        tenant_id, user_id = read_oauth_state(state, provider=provider)
    except ValueError:
        raise AppError("INVALID_STATE", "OAuth state is invalid or expired. Start the connection again.", status_code=400)
    conn = await crm_service.complete_oauth_connect(db, tenant_id, user_id, provider, code)
    await crm_service.register_webhooks_safe(db, conn)
    await audit_service.record(db, tenant_id=tenant_id, actor_user_id=user_id, action="crm.connected",
                               resource_type="crm_connection", resource_id=str(conn.id),
                               metadata={"provider": provider, "via": "oauth"})
    # Sheets needs an explicit spreadsheet/range mapping before any rows can
    # be read. Do not launch an import as a side effect of OAuth completion.
    if provider != "google_sheets":
        try:
            from app.workers.tasks import run_crm_sync
            run_crm_sync.delay(str(tenant_id), str(conn.id), "initial")
        except Exception as exc:  # noqa: BLE001
            conn.last_error = f"Connected, but initial sync could not be queued: {str(exc)[:200]}"
            await db.commit()
    return RedirectResponse(f"{settings.FRONTEND_URL}/?view=CRM&connected={provider}")


@router.post("/{provider}/sync", response_model=CRMSyncJobOut)
async def sync_now(provider: str, initial: bool = False, ctx: RequestContext = Depends(require_admin),
                   db: AsyncSession = Depends(get_async_db)):
    conn = await crm_service.get_connection(db, ctx.tenant_id, provider)
    if conn is None:
        raise AppError("CRM_NOT_CONNECTED", "Connect this CRM first.", status_code=404)
    await publish_event(ctx.tenant_id, "crm.sync_started", {"provider": provider})
    job = await crm_service.run_sync_job(db, ctx.tenant_id, conn, "initial" if initial else "incremental")
    await publish_event(ctx.tenant_id, "crm.sync_completed" if job.status == "succeeded" else "crm.sync_failed",
                        {"provider": provider, "created": job.records_created})
    return job


@router.get("/sync-history", response_model=list[CRMSyncJobOut])
async def sync_history(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    rows = await db.execute(select(CRMSyncJob).where(CRMSyncJob.tenant_id == ctx.tenant_id)
                            .order_by(CRMSyncJob.created_at.desc()).limit(50))
    return rows.scalars().all()


@router.delete("/{provider}")
async def disconnect(provider: str, ctx: RequestContext = Depends(require_admin),
                     db: AsyncSession = Depends(get_async_db)):
    conn = await crm_service.get_connection(db, ctx.tenant_id, provider)
    if conn is None:
        raise AppError("CRM_NOT_CONNECTED", "Not connected.", status_code=404)
    conn.status = "disconnected"
    conn.encrypted_access_token = None
    conn.encrypted_refresh_token = None
    await db.commit()
    await audit_service.record(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="crm.disconnected",
                               resource_type="crm_connection", resource_id=str(conn.id), metadata={"provider": provider})
    return {"disconnected": True}
