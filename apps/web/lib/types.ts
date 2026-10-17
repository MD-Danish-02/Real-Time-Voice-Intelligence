export type CRMConnectionStatus =
  | 'not_connected'
  | 'connecting'
  | 'connected'
  | 'syncing'
  | 'synced'
  | 'error'
  | 'disconnected'
  | 'failed';

export type CRMProvider =
  | 'HubSpot'
  | 'Salesforce'
  | 'Pipedrive'
  | 'Google Sheets'
  | 'Excel'
  | 'CSV'
  | 'Custom REST API';

export type CRMProviderName = 'hubspot' | 'salesforce' | 'zoho' | 'pipedrive' | 'google_sheets' | 'custom_api' | 'mock';

export type CRMConnectResult = {
  connected: boolean;
  connection_id?: string;
  authorization_url?: string;
};

export type CRMConnection = {
  id: string;
  provider: string;
  status: string;
  display_name: string | null;
  last_successful_sync_at: string | null;
  last_attempted_sync_at: string | null;
  last_error: string | null;
  created_at: string;
};

export type SyncSummary = {
  spreadsheet: string;
  sheet: string;
  rowsFetched: number;
  imported: number;
  updated: number;
  skipped: number;
  failed: number;
};

export type SyncStatus = {
  status: 'idle' | 'syncing' | 'synced' | 'error';
  lastSync: string | null;
  nextSync: string | null;
  newLeads: number;
  updatedLeads: number;
  errors: number;
};

export type Lead = {
  id: string;
  name: string | null;
  phone: string | null;
  normalized_phone: string | null;
  email: string | null;
  company: string | null;
  crm_status: string | null;
  source_type: string | null;
  created_at: string;
  updated_at: string;
  calling_status: string;
  eligible: boolean;
  attempt_count: number;
  do_not_call: boolean;
  custom_fields?: Record<string, unknown>;
  call_state?: {
    calling_status: string; attempt_count: number; do_not_call: boolean; eligible: boolean;
    last_call_at: string | null; last_call_result: string | null;
  } | null;
};

export type LeadDetail = Lead & {
  call_state?: {
    calling_status: string; attempt_count: number; do_not_call: boolean; eligible: boolean;
    last_call_at: string | null; last_call_result: string | null;
  } | null;
};

export type LeadEligibility = { eligible: boolean; reason: string; next_action: string };

export type LeadImportJob = {
  id: string;
  filename: string;
  source_type: string;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  total_rows: number;
  valid_rows: number;
  invalid_rows: number;
  duplicate_rows: number;
  imported_rows: number;
  skipped_rows: number;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type LeadImportResult = {
  job: LeadImportJob;
  rows: LeadImportRow[];
};

export type LeadImportRow = {
  rowIndex: number;
  name: string;
  phone: string;
  email: string;
  company: string;
  status: string;
  valid: boolean;
  duplicate: boolean;
  error: string | null;
};

export type LeadImportPreview = {
  sheets: string[];
  headers: string[];
  totalRows: number;
  previewRows: Record<string, string>[];
};

export type ColumnMapping = {
  name: string | null;
  phone: string | null;
  email: string | null;
  company: string | null;
  status: string | null;
  notes: string | null;
};

export type CRMEvent = {
  id: string;
  provider: string;
  event_type: string | null;
  status: string;
  received_at: string;
  processed_at: string | null;
  error: string | null;
};

export type AgentStatus = {
  crm_connected: number;
  lead_count: number;
  knowledge_count: number;
  knowledge_ready?: boolean;
  ai_ready?: boolean;
  auto_call_enabled?: boolean;
};

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

export type ImportPreviewResponse = {
  jobId: string;
  sheets: string[];
  headers: string[];
  totalRows: number;
  previewRows: Record<string, string>[];
  suggestedMapping: ColumnMapping;
  sourceType: string;
};

export type ImportConfirmResponse = {
  job: LeadImportJob;
  totalRows: number;
  validRows: number;
  invalidRows: number;
  duplicateRows: number;
  importedRows: number;
  skippedRows: number;
  queued: boolean;
};

export type AuthTokenResponse = { access_token: string; token_type?: string };

export type AuthUser = {
  id: string;
  email: string;
  full_name: string;
  role: string;
  tenant_id: string;
};

export type Tenant = { id: string; name: string; plan?: string };

export type Page<T> = { items: T[]; total: number; page: number; page_size: number };

export type KnowledgeDocument = {
  id: string;
  source_type: string;
  title: string;
  status: 'uploaded' | 'processing' | 'processed' | 'failed' | string;
  chunk_count: number;
  error: string | null;
  created_at: string;
};

export type Call = {
  id: string;
  lead_id: string | null;
  direction: string;
  status: string;
  attempt_number: number;
  telephony_provider: string;
  started_at: string | null;
  answered_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
};

export type CallDetail = Call & {
  transcript_segments: { speaker: string; text: string; started_at_ms: number; confidence: number | null }[];
  analysis: {
    summary: string | null; intent: string | null; lead_status: string | null;
    interest_level: string | null; sentiment: string | null; next_action: string | null;
    meeting_requested: boolean; follow_up_required: boolean; confidence: number | null;
  } | null;
};

export type Meeting = {
  id: string;
  lead_id: string;
  title: string;
  scheduled_at: string | null;
  duration_minutes: number;
  status: string;
  join_url: string | null;
};

export type AnalyticsOverview = {
  calls: { total: number; inbound: number; outbound: number; answered: number; missed: number; failed: number; average_duration_seconds: number | null };
  leads: { total: number; new: number; contacted: number; interested: number; not_interested: number; callback: number; meeting_requested: number; converted: number };
  meetings: { requested: number; booked: number; completed: number; canceled: number };
  whatsapp: { sent: number; delivered: number; read: number; failed: number };
  crm_sync: { imported: number; new_detected: number; updated: number; sync_failures: number; last_sync_at: string | null };
};

export type IntegrationStatus = {
  modes: Record<string, string>;
  credentials_configured: Record<string, boolean>;
  connections?: { google_calendar?: boolean };
};

export type BusinessRules = {
  timezone: string;
  calling_hours_start: string;
  calling_hours_end: string;
  calling_days_mask: number;
  max_attempts: number;
  retry_interval_minutes: number;
  auto_call_enabled: boolean;
  recording_enabled: boolean;
  crm_auto_update_enabled: boolean;
  whatsapp_enabled: boolean;
  meeting_booking_enabled: boolean;
  callback_enabled: boolean;
  allowed_crm_statuses: string;
  consent_required: boolean;
};

export type WorkspaceUser = { id: string; email: string; full_name: string; role: string; tenant_id: string };

export type ApiKey = { id: string; name: string; prefix: string; revoked: boolean };
