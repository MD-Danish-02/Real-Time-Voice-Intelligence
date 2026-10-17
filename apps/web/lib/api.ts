import type {
  AgentStatus,
  ApiResult,
  AuthTokenResponse,
  AuthUser,
  ApiKey,
  AnalyticsOverview,
  BusinessRules,
  Call,
  CallDetail,
  ColumnMapping,
  CRMConnection,
  CRMConnectResult,
  CRMProviderName,
  ImportConfirmResponse,
  ImportPreviewResponse,
  IntegrationStatus,
  KnowledgeDocument,
  Lead,
  LeadDetail,
  LeadEligibility,
  LeadImportJob,
  Meeting,
  Page,
  SyncSummary,
  Tenant,
  WorkspaceUser,
} from './types';

const API_BASE = (process.env.NEXT_PUBLIC_API_URL ?? '').replace(/\/$/, '');
let accessToken: string | null = null;
let refreshFlight: Promise<string | null> | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

function apiUrl(path: string): string {
  if (!API_BASE) throw new Error('FastAPI is not configured. Set NEXT_PUBLIC_API_URL.');
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`;
}

function readError(body: unknown, status: number): string {
  if (body && typeof body === 'object') {
    const data = body as Record<string, unknown>;
    const nested = data.error;
    if (nested && typeof nested === 'object' && 'message' in nested) {
      return String((nested as { message: unknown }).message);
    }
    if (typeof nested === 'string') return nested;
    if (typeof data.detail === 'string') return data.detail;
    if (data.detail && typeof data.detail === 'object' && 'message' in data.detail) {
      return String((data.detail as { message: unknown }).message);
    }
    if (typeof data.message === 'string') return data.message;
  }
  return `Request failed (${status}).`;
}

async function responseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const raw = await response.text();
  if (!raw) return undefined;
  try { return JSON.parse(raw); } catch { return raw; }
}

async function rotateAccessToken(notifyFailure = false): Promise<string | null> {
  if (!refreshFlight) {
    const hadToken = Boolean(accessToken);
    refreshFlight = (async () => {
      try {
        const response = await fetch(apiUrl('/api/v1/auth/refresh'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
          credentials: 'include',
          cache: 'no-store',
        });
        const body = await responseBody(response) as AuthTokenResponse | undefined;
        if (!response.ok || !body?.access_token) {
          accessToken = null;
          if (notifyFailure && hadToken && typeof window !== 'undefined') {
            window.dispatchEvent(new Event('voxai:auth-expired'));
          }
          return null;
        }
        accessToken = body.access_token;
        return body.access_token;
      } catch {
        accessToken = null;
        if (notifyFailure && hadToken && typeof window !== 'undefined') {
          window.dispatchEvent(new Event('voxai:auth-expired'));
        }
        return null;
      }
    })().finally(() => { refreshFlight = null; });
  }
  return refreshFlight;
}

type RequestOptions = { retryAuth?: boolean; authenticate?: boolean };

async function request<T>(path: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<ApiResult<T>> {
  try {
    const headers = new Headers(init.headers);
    const isForm = typeof FormData !== 'undefined' && init.body instanceof FormData;
    if (init.body && !isForm && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (options.authenticate !== false && accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
    const response = await fetch(apiUrl(path), {
      ...init,
      headers,
      credentials: 'include',
      cache: 'no-store',
    });
    if (response.status === 401 && options.retryAuth !== false && options.authenticate !== false &&
        !path.endsWith('/auth/login') && !path.endsWith('/auth/signup') && !path.endsWith('/auth/refresh') &&
        !path.endsWith('/auth/logout')) {
      const rotated = await rotateAccessToken(true);
      if (rotated) {
        const retryHeaders = new Headers(init.headers);
        if (init.body && !isForm && !retryHeaders.has('Content-Type')) retryHeaders.set('Content-Type', 'application/json');
        retryHeaders.set('Authorization', `Bearer ${rotated}`);
        const retried = await fetch(apiUrl(path), { ...init, headers: retryHeaders, credentials: 'include', cache: 'no-store' });
        const retryBody = await responseBody(retried);
        if (!retried.ok) return { ok: false, error: readError(retryBody, retried.status) };
        return { ok: true, data: retryBody as T };
      }
    }
    const body = await responseBody(response);
    if (!response.ok) return { ok: false, error: readError(body, response.status) };
    return { ok: true, data: body as T };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : 'Network error. Check your connection and try again.' };
  }
}

export function apiFetch<T>(path: string, options?: RequestInit): Promise<ApiResult<T>> {
  return request<T>(path, options);
}

export const authApi = {
  async login(email: string, password: string): Promise<ApiResult<AuthTokenResponse>> {
    const result = await request<AuthTokenResponse>('/api/v1/auth/login', {
      method: 'POST', body: JSON.stringify({ email, password }),
    }, { authenticate: false, retryAuth: false });
    if (result.ok) setAccessToken(result.data.access_token);
    return result;
  },
  async signup(payload: { email: string; password: string; full_name: string; company_name: string }): Promise<ApiResult<AuthTokenResponse>> {
    const result = await request<AuthTokenResponse>('/api/v1/auth/signup', {
      method: 'POST', body: JSON.stringify(payload),
    }, { authenticate: false, retryAuth: false });
    if (result.ok) setAccessToken(result.data.access_token);
    return result;
  },
  requestPasswordReset(email: string): Promise<ApiResult<{ message: string; delivery_mode: string }>> {
    return request<{ message: string; delivery_mode: string }>('/api/v1/auth/password-reset/request', {
      method: 'POST', body: JSON.stringify({ email }),
    }, { authenticate: false, retryAuth: false });
  },
  confirmPasswordReset(token: string, new_password: string): Promise<ApiResult<{ message: string; delivery_mode: string }>> {
    return request<{ message: string; delivery_mode: string }>('/api/v1/auth/password-reset/confirm', {
      method: 'POST', body: JSON.stringify({ token, new_password }),
    }, { authenticate: false, retryAuth: false });
  },
  restore(): Promise<string | null> { return rotateAccessToken(false); },
  me(): Promise<ApiResult<AuthUser>> { return apiFetch<AuthUser>('/api/v1/auth/me'); },
  tenant(): Promise<ApiResult<Tenant>> { return apiFetch<Tenant>('/api/v1/tenants/me'); },
  async logout(): Promise<void> {
    try { await request<void>('/api/v1/auth/logout', { method: 'POST', body: '{}' }, { retryAuth: false }); }
    finally { setAccessToken(null); }
  },
};

const providerSlugs: Record<string, CRMProviderName> = {
  'HubSpot': 'hubspot', 'Salesforce': 'salesforce', 'Zoho': 'zoho', 'Pipedrive': 'pipedrive',
  'Google Sheets': 'google_sheets', 'Custom REST API': 'custom_api', 'Demo CRM': 'mock',
  hubspot: 'hubspot', salesforce: 'salesforce', zoho: 'zoho', pipedrive: 'pipedrive',
  google_sheets: 'google_sheets', custom_api: 'custom_api', mock: 'mock',
};

export function providerSlug(provider: string): CRMProviderName | null { return providerSlugs[provider] ?? null; }

function providerLabel(provider: string): string {
  return ({ hubspot: 'HubSpot', salesforce: 'Salesforce', zoho: 'Zoho', pipedrive: 'Pipedrive',
    google_sheets: 'Google Sheets', custom_api: 'Custom REST API', mock: 'Demo CRM' } as Record<string, string>)[provider] ?? provider;
}

function normalizeConnection(row: Record<string, unknown>): CRMConnection {
  return {
    id: String(row.id), provider: providerLabel(String(row.provider)), status: String(row.status),
    display_name: (row.external_account_name as string | null) ?? null,
    last_successful_sync_at: (row.last_successful_sync_at as string | null) ?? null,
    last_attempted_sync_at: (row.last_attempted_sync_at as string | null) ?? null,
    last_error: (row.last_error as string | null) ?? null,
    created_at: (row.created_at as string | null) ?? '',
  };
}

export const crmApi = {
  async listConnections(): Promise<ApiResult<CRMConnection[]>> {
    const result = await apiFetch<Record<string, unknown>[]>('/api/v1/crm/connections');
    return result.ok ? { ok: true, data: result.data.map(normalizeConnection) } : result;
  },
  connect(provider: string, config?: Record<string, unknown>): Promise<ApiResult<CRMConnectResult>> {
    const slug = providerSlug(provider);
    if (!slug) return Promise.resolve({ ok: false, error: `${provider} is not supported by the configured CRM provider.` });
    return apiFetch<CRMConnectResult>('/api/v1/crm/connect', {
      method: 'POST', body: JSON.stringify({ provider: slug, config }),
    });
  },
  async saveConfig(provider: string, config: Record<string, unknown>): Promise<ApiResult<{ saved: boolean }>> {
    const slug = providerSlug(provider);
    if (!slug) return { ok: false, error: `Unsupported CRM provider: ${provider}.` };
    return apiFetch<{ saved: boolean }>(`/api/v1/crm/${slug}/config`, {
      method: 'PUT', body: JSON.stringify(config),
    });
  },
  async googleSheetsListSheets(spreadsheetId: string): Promise<ApiResult<string[]>> {
    const query = new URLSearchParams({ spreadsheet_id: spreadsheetId });
    const result = await apiFetch<{ sheets: string[] }>(`/api/v1/crm/google_sheets/sheets?${query}`);
    return result.ok ? { ok: true, data: result.data.sheets } : result;
  },
  async googleSheetsHeaders(spreadsheetId: string, sheetName: string): Promise<ApiResult<string[]>> {
    const query = new URLSearchParams({ spreadsheet_id: spreadsheetId, sheet_name: sheetName });
    const result = await apiFetch<{ headers: string[] }>(`/api/v1/crm/google_sheets/headers?${query}`);
    return result.ok ? { ok: true, data: result.data.headers } : result;
  },
  async disconnect(provider: string): Promise<ApiResult<true>> {
    const slug = providerSlug(provider);
    if (!slug) return { ok: false, error: `Unsupported CRM provider: ${provider}.` };
    const result = await apiFetch<{ disconnected: boolean }>(`/api/v1/crm/${slug}`, { method: 'DELETE' });
    return result.ok ? { ok: true, data: true } : result;
  },
  async sync(provider: string): Promise<ApiResult<{ connection: CRMConnection | null; sync?: SyncSummary }>> {
    const slug = providerSlug(provider);
    if (!slug) return { ok: false, error: `Unsupported CRM provider: ${provider}.` };
    const result = await apiFetch<Record<string, unknown>>(`/api/v1/crm/${slug}/sync?initial=true`, { method: 'POST' });
    if (!result.ok) return result;
    const row = result.data;
    return { ok: true, data: {
      connection: null,
      sync: {
        spreadsheet: providerLabel(slug), sheet: '', rowsFetched: Number(row.records_fetched ?? 0),
        imported: Number(row.records_created ?? 0), updated: Number(row.records_updated ?? 0),
        skipped: 0, failed: Number(row.records_failed ?? 0),
      },
    } };
  },
};

function normalizeLead(row: Record<string, unknown>): Lead {
  const state = (row.call_state && typeof row.call_state === 'object' ? row.call_state : {}) as Record<string, unknown>;
  return {
    id: String(row.id), name: (row.name as string | null) ?? null, phone: (row.phone as string | null) ?? null,
    normalized_phone: (row.normalized_phone as string | null) ?? null, email: (row.email as string | null) ?? null,
    company: (row.company as string | null) ?? null, crm_status: (row.crm_status as string | null) ?? null,
    source_type: String(row.source_type ?? row.crm_provider ?? 'manual'),
    created_at: String(row.created_at ?? ''), updated_at: String(row.updated_at ?? row.created_at ?? ''),
    calling_status: String(row.calling_status ?? state.calling_status ?? 'not_called'),
    eligible: Boolean(row.eligible ?? state.eligible), attempt_count: Number(row.attempt_count ?? state.attempt_count ?? 0),
    do_not_call: Boolean(row.do_not_call ?? state.do_not_call),
    custom_fields: (row.custom_fields as Record<string, unknown> | undefined) ?? {},
    call_state: row.call_state && typeof row.call_state === 'object' ? {
      calling_status: String(state.calling_status ?? 'not_called'), attempt_count: Number(state.attempt_count ?? 0),
      do_not_call: Boolean(state.do_not_call), eligible: Boolean(state.eligible),
      last_call_at: (state.last_call_at as string | null) ?? null,
      last_call_result: (state.last_call_result as string | null) ?? null,
    } : null,
  };
}

function normalizeImportPreview(data: Record<string, unknown>): ImportPreviewResponse {
  const backendMap = (data.suggested_mapping ?? {}) as Record<string, string>;
  const suggested: ColumnMapping = { name: null, phone: null, email: null, company: null, status: null, notes: null };
  for (const [header, field] of Object.entries(backendMap)) {
    if (field in suggested) suggested[field as keyof ColumnMapping] = header;
  }
  return {
    jobId: String(data.job_id), sheets: (data.sheets as string[] | null) ?? [],
    headers: (data.headers as string[]) ?? [], totalRows: Number(data.total_rows ?? 0),
    previewRows: (data.sample_rows as Record<string, string>[]) ?? [], suggestedMapping: suggested,
    sourceType: String(data.source_type ?? 'excel'),
  };
}

export const leadApi = {
  async list(limit = 50, offset = 0, q?: string, crmStatus?: string): Promise<ApiResult<Lead[]>> {
    const page = Math.floor(offset / limit) + 1;
    const query = new URLSearchParams({ page: String(page), page_size: String(limit) });
    if (q) query.set('q', q);
    if (crmStatus) query.set('crm_status', crmStatus);
    const result = await apiFetch<Page<Record<string, unknown>>>(`/api/v1/leads?${query}`);
    return result.ok ? { ok: true, data: result.data.items.map(normalizeLead) } : result;
  },
  async detail(id: string): Promise<ApiResult<LeadDetail>> {
    const result = await apiFetch<Record<string, unknown>>(`/api/v1/leads/${id}`);
    if (!result.ok) return result;
    return { ok: true, data: normalizeLead(result.data) as LeadDetail };
  },
  eligibility(id: string): Promise<ApiResult<LeadEligibility>> {
    return apiFetch<LeadEligibility>(`/api/v1/leads/${id}/eligibility`);
  },
  setDoNotCall(id: string, value: boolean): Promise<ApiResult<{ do_not_call: boolean; manual_do_not_call: boolean }>> {
    return apiFetch(`/api/v1/leads/${id}/do-not-call?value=${value}`, { method: 'POST' });
  },
  queueCall(id: string): Promise<ApiResult<{ queued: boolean; reason: string }>> {
    return apiFetch(`/api/v1/leads/${id}/call`, { method: 'POST' });
  },
  async count(): Promise<ApiResult<number>> {
    const result = await apiFetch<Page<unknown>>('/api/v1/leads?page=1&page_size=1');
    return result.ok ? { ok: true, data: result.data.total } : result;
  },
  async previewExcel(file: File, sheet?: string): Promise<ApiResult<ImportPreviewResponse>> {
    const form = new FormData(); form.append('file', file); if (sheet) form.append('sheet_name', sheet);
    const result = await request<Record<string, unknown>>('/api/v1/leads/import/excel/preview', { method: 'POST', body: form });
    if (!result.ok) return result;
    return { ok: true, data: normalizeImportPreview(result.data) };
  },
  async selectImportSheet(jobId: string, sheet: string): Promise<ApiResult<ImportPreviewResponse>> {
    const form = new FormData(); form.append('sheet_name', sheet);
    const result = await request<Record<string, unknown>>(`/api/v1/leads/import/excel/${jobId}/sheet`, { method: 'POST', body: form });
    return result.ok ? { ok: true, data: normalizeImportPreview(result.data) } : result;
  },
  async confirmImport(_file: File, jobId: string, mapping: ColumnMapping, sheet?: string): Promise<ApiResult<ImportConfirmResponse>> {
    const column_mapping: Record<string, string> = {};
    for (const [target, header] of Object.entries(mapping)) if (header) column_mapping[header] = target;
    const validated = await apiFetch<Record<string, unknown>>('/api/v1/leads/import/excel/validate', {
      method: 'POST', body: JSON.stringify({ job_id: jobId, column_mapping, sheet_name: sheet ?? null }),
    });
    if (!validated.ok) return validated;
    const result = await apiFetch<Record<string, unknown>>('/api/v1/leads/import/excel/confirm', {
      method: 'POST', body: JSON.stringify({ job_id: jobId }),
    });
    if (!result.ok) return result;
    const data = result.data;
    const job = normalizeImportJob(data);
    return { ok: true, data: {
      job, queued: Boolean(data.queued), totalRows: Number(data.total_rows ?? 0),
      validRows: Number(data.valid_rows ?? 0), invalidRows: Number(data.invalid_rows ?? 0),
      duplicateRows: Number(data.duplicate_rows ?? 0), importedRows: Number(data.imported_rows ?? 0),
      skippedRows: Number(data.skipped_rows ?? 0),
    } };
  },
  async listImportJobs(): Promise<ApiResult<LeadImportJob[]>> {
    const result = await apiFetch<Record<string, unknown>[]>('/api/v1/leads/import/jobs');
    return result.ok ? { ok: true, data: result.data.map(normalizeImportJob) } : result;
  },
};

function normalizeImportJob(row: Record<string, unknown>): LeadImportJob {
  const status = String(row.status ?? 'pending');
  const normalizedStatus: LeadImportJob['status'] = status === 'completed' || status === 'failed'
    ? status : ['queued', 'processing', 'importing'].includes(status) ? 'processing' : 'pending';
  return {
    id: String(row.job_id ?? row.id), filename: String(row.original_filename ?? row.filename ?? 'Import'),
    source_type: String(row.source_type ?? ''),
    status: normalizedStatus,
    total_rows: Number(row.total_rows ?? 0), valid_rows: Number(row.valid_rows ?? 0),
    invalid_rows: Number(row.invalid_rows ?? 0), duplicate_rows: Number(row.duplicate_rows ?? 0),
    imported_rows: Number(row.imported_rows ?? 0), skipped_rows: Number(row.skipped_rows ?? 0),
    error: (row.error as string | null) ?? null, created_at: String(row.created_at ?? row.started_at ?? ''),
    updated_at: String(row.updated_at ?? row.finished_at ?? ''),
  };
}

export const agentApi = {
  getStatus(): Promise<ApiResult<AgentStatus>> { return apiFetch<AgentStatus>('/api/v1/tenants/ai-status'); },
};

export const knowledgeApi = {
  list(): Promise<ApiResult<KnowledgeDocument[]>> { return apiFetch<KnowledgeDocument[]>('/api/v1/knowledge'); },
  upload(file: File): Promise<ApiResult<KnowledgeDocument>> {
    const form = new FormData(); form.append('file', file);
    return request<KnowledgeDocument>('/api/v1/knowledge/upload', { method: 'POST', body: form });
  },
  addText(payload: { source_type: 'faq' | 'product' | 'service' | 'custom_text'; title: string; content: string }): Promise<ApiResult<KnowledgeDocument>> {
    return apiFetch<KnowledgeDocument>('/api/v1/knowledge/text', { method: 'POST', body: JSON.stringify(payload) });
  },
  addWebsite(url: string): Promise<ApiResult<KnowledgeDocument>> {
    return apiFetch<KnowledgeDocument>('/api/v1/knowledge/website', { method: 'POST', body: JSON.stringify({ url }) });
  },
  remove(id: string): Promise<ApiResult<{ deleted: boolean }>> {
    return apiFetch<{ deleted: boolean }>(`/api/v1/knowledge/${id}`, { method: 'DELETE' });
  },
};

export const callsApi = {
  list(page = 1, pageSize = 25): Promise<ApiResult<Page<Call>>> {
    return apiFetch<Page<Call>>(`/api/v1/calls?page=${page}&page_size=${pageSize}`);
  },
  detail(id: string): Promise<ApiResult<CallDetail>> { return apiFetch<CallDetail>(`/api/v1/calls/${id}`); },
};

export const meetingsApi = {
  list(): Promise<ApiResult<Meeting[]>> { return apiFetch<Meeting[]>('/api/v1/meetings'); },
  create(payload: { lead_id: string; title: string; scheduled_at: string; duration_minutes: number }): Promise<ApiResult<Meeting>> {
    return apiFetch<Meeting>('/api/v1/meetings', { method: 'POST', body: JSON.stringify(payload) });
  },
};

export const analyticsApi = {
  overview(): Promise<ApiResult<AnalyticsOverview>> { return apiFetch<AnalyticsOverview>('/api/v1/analytics/overview'); },
};

export const integrationsApi = {
  list(): Promise<ApiResult<IntegrationStatus>> { return apiFetch<IntegrationStatus>('/api/v1/integrations'); },
  googleCalendarAuthorize(): Promise<ApiResult<{ authorization_url: string }>> {
    return apiFetch<{ authorization_url: string }>('/api/v1/integrations/google/authorize');
  },
};

export const settingsApi = {
  businessRules(): Promise<ApiResult<BusinessRules>> { return apiFetch<BusinessRules>('/api/v1/tenants/business-rules'); },
  updateBusinessRules(payload: Partial<BusinessRules>): Promise<ApiResult<BusinessRules>> {
    return apiFetch<BusinessRules>('/api/v1/tenants/business-rules', { method: 'PUT', body: JSON.stringify(payload) });
  },
};

export const usersApi = {
  list(): Promise<ApiResult<WorkspaceUser[]>> { return apiFetch<WorkspaceUser[]>('/api/v1/users'); },
  create(payload: { email: string; full_name: string; password: string; role: string }): Promise<ApiResult<WorkspaceUser>> {
    return apiFetch<WorkspaceUser>('/api/v1/users', { method: 'POST', body: JSON.stringify(payload) });
  },
};

export const apiKeysApi = {
  list(): Promise<ApiResult<ApiKey[]>> { return apiFetch<ApiKey[]>('/api/v1/api-keys'); },
  create(name: string): Promise<ApiResult<{ key: string; note: string }>> {
    return apiFetch<{ key: string; note: string }>('/api/v1/api-keys', { method: 'POST', body: JSON.stringify({ name }) });
  },
  revoke(id: string): Promise<ApiResult<{ revoked: boolean }>> {
    return apiFetch<{ revoked: boolean }>(`/api/v1/api-keys/${id}`, { method: 'DELETE' });
  },
};
