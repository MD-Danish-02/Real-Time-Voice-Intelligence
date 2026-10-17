from fastapi import APIRouter

from app.api.v1 import (analytics, api_keys, audit, auth, calls, crm, integrations, knowledge, leads, leads_import,
                        meetings, tenants, users, voice_test, webhooks, ws, plivo)

api_router = APIRouter()
for module in (auth, tenants, users, crm, leads, leads_import, knowledge, calls, meetings, analytics, integrations,
               webhooks, api_keys, audit, voice_test, plivo):
    api_router.include_router(module.router)
