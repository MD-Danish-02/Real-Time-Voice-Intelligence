# CRM
`CRMProvider` (`providers/crm/base.py`): OAuth (`get_authorization_url`, `exchange_code_for_token`, `refresh_access_token`), sync (`initial_sync`, `fetch_leads`, `fetch_lead`), webhooks (`register/unregister`, `verify_webhook_signature`, `extract_event_id`), write-back (`update_lead`, `create_note`, `create_activity`, `fetch_activities`).
Adapters: Mock, HubSpot, Salesforce, Zoho, Pipedrive, Google Sheets, Custom API (CSV/Excel arrive through the Custom API/Sheets path; no dedicated file-import UI yet).
Flow: connect (OAuth `state` is a signed 10-minute token bound to tenant+user+provider) -> initial sync -> **fully paginates every page** (`has_more`/`next_cursor` loop, hard cap 500 pages) -> upsert by natural key + phone normalisation -> DNC normalization -> `lead.created` automation -> eligibility -> queue. Continuous sync = webhook-first + `reconcile_all_crms` every 5 min, both going through the same paginating loop.

**Connection lifecycle** (never stuck on "pending"): `connected` -> `importing` (initial sync running) -> `synced` (any sync succeeded) -> `error` only if the *first* sync ever fails; later transient failures keep the last-known-good status and surface via `last_error`.

**DNC/opt-out normalization** (`app/services/dnc_service.py`): a regex set recognizes `DO_NOT_CALL`, `DNC`, `Do Not Call`, `OPT_OUT`, `Unsubscribe`, `Suppressed`, `Compliance Hold`, etc. Every lead upsert (sync, webhook, or the AI's own `update_lead` tool call) re-checks this and flips `lead_call_state.do_not_call`; flipping ON immediately cancels every pending/locked/dispatched `call_jobs` row for that lead. Flipping OFF does **not** auto-requeue - it just re-enters the normal eligibility path.

**Google Sheets** now uses a real Google OAuth flow (same client credentials as Calendar): `POST /crm/connect {"provider":"google_sheets"}` returns a real `accounts.google.com` URL when `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are set, or a clear `409 PROVIDER_NOT_CONFIGURED` error if not - never a fake success. After connecting, `PUT /crm/google_sheets/config {"spreadsheet_id","sheet_range","columns"}` tells sync which sheet/range to read.

**Custom API** persists its config the same way: `PUT /crm/custom_api/config {"base_url","list_path","auth_header","field_map"}`, which the sync worker actually reads on every run (not just at connect time).

**Status:** Mock, Custom API and Google Sheets pagination/config/DNC are exercised by tests (with a stubbed HTTP layer for Sheets/Custom API). HubSpot/Salesforce/Zoho/Pipedrive live adapters follow each vendor's documented API but were never run against real accounts; HubSpot's signature check is a simplified scheme - switch to their v3 signature before production. Salesforce push events need a CometD/CDC worker (not built).
