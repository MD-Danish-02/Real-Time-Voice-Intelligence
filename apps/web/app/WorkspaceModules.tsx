'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, BarChart3, CalendarDays, KeyRound, Loader2, Phone, Plus, RefreshCw, Settings, Users, X } from 'lucide-react';
import { analyticsApi, apiKeysApi, callsApi, integrationsApi, leadApi, meetingsApi, settingsApi, usersApi } from '@/lib/api';
import type { AnalyticsOverview, ApiKey, BusinessRules, Call, CallDetail, IntegrationStatus, Lead, Meeting, WorkspaceUser } from '@/lib/types';

type View = 'Dashboard' | 'CRM' | 'Knowledge' | 'Calls' | 'Meetings' | 'Analytics' | 'Integrations' | 'Settings';
type Props = { onNavigate: (view: View) => void };

function PageHeading({ title, description, icon: Icon }: { title: string; description: string; icon: typeof Phone }) {
  return <div className="page-heading"><div><p className="eyebrow">Workspace</p><h1>{title}</h1><p className="page-subtitle">{description}</p></div><span className="metric-icon indigo"><Icon size={17} /></span></div>;
}

function ErrorNotice({ value }: { value: string | null }) { return value ? <div className="modal-error" role="alert">{value}</div> : null; }
function fmtDate(value: string | null | undefined): string { return value ? new Date(value).toLocaleString() : 'Not recorded'; }
function fmtDuration(value: number | null | undefined): string { return value == null ? '—' : `${Math.floor(value / 60)}m ${value % 60}s`; }

export function CallsModule() {
  const [items, setItems] = useState<Call[]>([]);
  const [selected, setSelected] = useState<CallDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const load = useCallback(async () => {
    setLoading(true); const result = await callsApi.list(page, 25);
    if (result.ok) { setItems(result.data.items); setTotal(result.data.total); setError(null); }
    else setError(result.error);
    setLoading(false);
  }, [page]);
  useEffect(() => { void load(); }, [load]);
  async function open(id: string) {
    setDetailLoading(true); setSelected(null); const result = await callsApi.detail(id);
    if (result.ok) setSelected(result.data); else setError(result.error);
    setDetailLoading(false);
  }
  return <div className="page-content">
    <PageHeading title="Calls" description="Review real call outcomes, transcripts, and post-call analysis." icon={Phone} />
    <ErrorNotice value={error} />
    <section className="card data-panel"><div className="card-heading"><div><h3>Call history</h3><p>{total ? `${total} recorded call${total === 1 ? '' : 's'}` : 'Calls handled by your workspace will appear here.'}</p></div><button className="quiet-button" onClick={() => void load()} disabled={loading}><RefreshCw size={14} />Refresh</button></div>
      {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading calls…</div> : items.length === 0 ? <div className="empty-state"><div className="empty-icon"><Phone size={20} /></div><h3>No calls yet</h3><p>Call records will appear after the telephony provider reports a call lifecycle.</p></div> : <div className="data-list">{items.map((call) => <button className="data-row data-row-button" key={call.id} onClick={() => void open(call.id)}><span className="data-icon"><Phone size={16} /></span><span className="data-copy"><strong>{call.direction} call · {call.status}</strong><small>{fmtDate(call.started_at)} · {fmtDuration(call.duration_seconds)}</small></span><span className="badge badge-amber">{call.telephony_provider}</span></button>)}</div>}
      {total > 25 && <div className="pager"><button className="quiet-button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button><span>Page {page} of {Math.ceil(total / 25)}</span><button className="quiet-button" disabled={page * 25 >= total} onClick={() => setPage(page + 1)}>Next</button></div>}
    </section>
    {detailLoading && <div className="inline-loading"><Loader2 size={18} className="spin" />Loading call details…</div>}
    {selected && <section className="card detail-panel"><div className="card-heading"><div><h3>Call details</h3><p>{selected.direction} · {selected.status} · {fmtDate(selected.started_at)}</p></div><button className="icon-button" onClick={() => setSelected(null)} aria-label="Close call details"><X size={16} /></button></div>
      {selected.analysis && <div className="detail-summary"><strong>Summary</strong><p>{selected.analysis.summary || 'Analysis did not produce a summary.'}</p><div className="detail-tags"><span>Intent: {selected.analysis.intent || 'Unknown'}</span><span>Lead status: {selected.analysis.lead_status || 'Unknown'}</span><span>Interest: {selected.analysis.interest_level || 'Unknown'}</span><span>Next: {selected.analysis.next_action || 'None'}</span></div></div>}
      <h4 className="detail-subheading">Transcript</h4>{selected.transcript_segments.length ? <div className="transcript-list">{selected.transcript_segments.map((segment, index) => <div className="transcript-row" key={`${segment.started_at_ms}-${index}`}><span>{segment.speaker}</span><p>{segment.text}</p><small>{Math.round(segment.started_at_ms / 1000)}s</small></div>)}</div> : <p className="muted-copy">No transcript segments were saved for this call.</p>}
    </section>}
  </div>;
}

export function MeetingsModule({ onNavigate }: Props) {
  const [items, setItems] = useState<Meeting[]>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [saving, setSaving] = useState(false);
  const [leadId, setLeadId] = useState('');
  const [title, setTitle] = useState('Discovery meeting');
  const [scheduledAt, setScheduledAt] = useState('');
  const [duration, setDuration] = useState(30);
  const load = useCallback(async () => {
    setLoading(true); const [meetings, leadRows] = await Promise.all([meetingsApi.list(), leadApi.list(100, 0)]);
    if (meetings.ok) setItems(meetings.data); else setError(meetings.error);
    if (leadRows.ok) setLeads(leadRows.data);
    setLoading(false);
  }, []);
  useEffect(() => { void load(); }, [load]);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (!leadId || !scheduledAt) return;
    setSaving(true); setError(null);
    const result = await meetingsApi.create({ lead_id: leadId, title: title.trim(), scheduled_at: new Date(scheduledAt).toISOString(), duration_minutes: duration });
    if (result.ok) { setShowForm(false); setScheduledAt(''); await load(); }
    else setError(result.error);
    setSaving(false);
  }
  return <div className="page-content">
    <PageHeading title="Meetings" description="See meetings persisted by the scheduling and calendar services." icon={CalendarDays} />
    <ErrorNotice value={error} />
    <section className="card data-panel"><div className="card-heading"><div><h3>Meeting schedule</h3><p>Only meetings returned by the backend are shown.</p></div><div className="heading-actions"><button className="quiet-button" onClick={() => onNavigate('CRM')}>View leads</button><button className="button button-primary" onClick={() => { setShowForm(!showForm); setError(null); }}><Plus size={15} />Request meeting</button></div></div>
      {showForm && <form className="meeting-form" onSubmit={(event) => void submit(event)}><label className="field-label">Lead<select className="field-input" required value={leadId} onChange={(event) => setLeadId(event.target.value)}><option value="">Select a lead</option>{leads.map((lead) => <option value={lead.id} key={lead.id}>{lead.name || lead.phone || lead.id}</option>)}</select></label><label className="field-label">Meeting title<input className="field-input" value={title} onChange={(event) => setTitle(event.target.value)} required /></label><label className="field-label">Start time<input className="field-input" type="datetime-local" value={scheduledAt} onChange={(event) => setScheduledAt(event.target.value)} required /></label><label className="field-label">Duration<select className="field-input" value={duration} onChange={(event) => setDuration(Number(event.target.value))}><option value={15}>15 minutes</option><option value={30}>30 minutes</option><option value={45}>45 minutes</option><option value={60}>60 minutes</option></select></label><button className="button button-primary" disabled={saving || !leads.length}>{saving ? 'Saving…' : 'Save meeting request'}</button><p className="field-hint">The backend marks the meeting booked only after the configured calendar provider confirms it.</p></form>}
      {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading meetings…</div> : items.length === 0 ? <div className="empty-state"><div className="empty-icon"><CalendarDays size={20} /></div><h3>No meetings scheduled</h3><p>Meetings from calls or manual requests appear here after they are saved.</p></div> : <div className="data-list">{items.map((meeting) => <div className="data-row" key={meeting.id}><span className="data-icon"><CalendarDays size={16} /></span><span className="data-copy"><strong>{meeting.title}</strong><small>{fmtDate(meeting.scheduled_at)} · {meeting.duration_minutes} minutes</small>{meeting.join_url && <a href={meeting.join_url} target="_blank" rel="noreferrer">Open meeting link</a>}</span><span className={`badge ${meeting.status === 'booked' ? 'badge-emerald' : 'badge-amber'}`}>{meeting.status}</span></div>)}</div>}
    </section>
  </div>;
}

export function AnalyticsModule() {
  const [data, setData] = useState<AnalyticsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => { setLoading(true); const result = await analyticsApi.overview(); if (result.ok) { setData(result.data); setError(null); } else setError(result.error); setLoading(false); }, []);
  useEffect(() => { void load(); }, [load]);
  const metrics = useMemo(() => data ? [
    ['Calls', data.calls.total, `${data.calls.answered} answered · ${data.calls.missed} missed`],
    ['Leads', data.leads.total, `${data.leads.interested} interested · ${data.leads.converted} converted`],
    ['Meetings', data.meetings.booked, `${data.meetings.requested} requested · ${data.meetings.completed} completed`],
    ['CRM sync', data.crm_sync.imported, `${data.crm_sync.updated} updated · ${data.crm_sync.sync_failures} failed`],
  ] : [], [data]);
  return <div className="page-content"><PageHeading title="Analytics" description="Tenant-scoped totals aggregated from call, lead, meeting, messaging, and CRM records." icon={BarChart3} /><ErrorNotice value={error} />
    {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading analytics…</div> : data && <><section className="metrics-grid">{metrics.map(([label, value, hint], i) => <div className="metric-card" key={label as string}><div className="metric-top"><span>{label}</span><span className={`metric-icon ${['indigo', 'cyan', 'amber', 'emerald'][i]}`}><Activity size={16} /></span></div><strong>{Number(value).toLocaleString()}</strong><p>{hint}</p></div>)}</section><div className="dashboard-grid analytics-grid"><section className="card"><div className="card-heading"><div><h3>Call outcomes</h3><p>All recorded calls</p></div></div><div className="detail-tags"><span>Inbound: {data.calls.inbound}</span><span>Outbound: {data.calls.outbound}</span><span>Answered: {data.calls.answered}</span><span>Failed: {data.calls.failed}</span></div><p className="muted-copy">Average duration: {data.calls.average_duration_seconds == null ? 'No calls yet' : fmtDuration(Math.round(data.calls.average_duration_seconds))}</p></section><section className="card"><div className="card-heading"><div><h3>Lead outcomes</h3><p>Derived from saved lead state and call analysis</p></div></div><div className="detail-tags"><span>New: {data.leads.new}</span><span>Contacted: {data.leads.contacted}</span><span>Interested: {data.leads.interested}</span><span>Callbacks: {data.leads.callback}</span><span>Meeting requested: {data.leads.meeting_requested}</span></div></section><section className="card"><div className="card-heading"><div><h3>Channels</h3><p>Persisted provider delivery data</p></div></div><div className="detail-tags"><span>WhatsApp sent: {data.whatsapp.sent}</span><span>Delivered: {data.whatsapp.delivered}</span><span>Read: {data.whatsapp.read}</span><span>Failed: {data.whatsapp.failed}</span></div><p className="muted-copy">Last CRM sync: {fmtDate(data.crm_sync.last_sync_at)}</p></section></div></>}
  </div>;
}

export function IntegrationsModule({ onNavigate, notice }: Props & { notice?: string | null }) {
  const [data, setData] = useState<IntegrationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [connectingCalendar, setConnectingCalendar] = useState(false);
  const load = useCallback(async () => { setLoading(true); const result = await integrationsApi.list(); if (result.ok) { setData(result.data); setError(null); } else setError(result.error); setLoading(false); }, []);
  useEffect(() => { void load(); }, [load]);
  async function connectCalendar() {
    setConnectingCalendar(true); setError(null);
    const result = await integrationsApi.googleCalendarAuthorize();
    setConnectingCalendar(false);
    if (result.ok) window.location.assign(result.data.authorization_url);
    else setError(result.error);
  }
  return <div className="page-content"><PageHeading title="Integrations" description="Provider modes and credential presence reported by the API." icon={Settings} /><ErrorNotice value={notice ?? error} />
    {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading integrations…</div> : data && <><section className="integration-grid">{Object.entries(data.modes).map(([name, mode]) => <div className="card integration-status-card" key={name}><div className="card-heading"><div><h3>{name[0].toUpperCase() + name.slice(1)}</h3><p>Configured provider mode</p></div><span className={`badge ${mode === 'mock' ? 'badge-amber' : 'badge-emerald'}`}>{mode}</span></div><p className="muted-copy">{mode === 'mock' ? 'Development mock mode. Actions are not live external transactions.' : 'Provider adapter selected; check credentials and provider status before production use.'}</p>{name === 'crm' && <button className="quiet-button" onClick={() => onNavigate('CRM')}>Manage CRM connections</button>}{name === 'calendar' && mode === 'google' && <div className="calendar-connection"><span className={`badge ${data.connections?.google_calendar ? 'badge-emerald' : 'badge-amber'}`}>{data.connections?.google_calendar ? 'Google Calendar connected' : 'Google Calendar not connected'}</span>{!data.connections?.google_calendar && <button className="button button-outline" disabled={connectingCalendar || !data.credentials_configured.google} onClick={() => void connectCalendar()}>{connectingCalendar ? 'Opening Google…' : 'Connect Google Calendar'}</button>}</div>}</div>)}</section><section className="card data-panel"><div className="card-heading"><div><h3>Credential status</h3><p>Presence only. Secret values are never returned to the browser.</p></div><button className="quiet-button" onClick={() => void load()}><RefreshCw size={14} />Refresh</button></div><div className="credential-grid">{Object.entries(data.credentials_configured).map(([name, configured]) => <div className="credential-row" key={name}><span>{name.replaceAll('_', ' ')}</span><span className={`badge ${configured ? 'badge-emerald' : 'badge-amber'}`}>{configured ? 'Configured' : 'Missing'}</span></div>)}</div></section></>}
  </div>;
}

export function SettingsModule() {
  const [rules, setRules] = useState<BusinessRules | null>(null);
  const [users, setUsers] = useState<WorkspaceUser[]>([]);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [keyName, setKeyName] = useState('');
  const [newKey, setNewKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    const [r, u, k] = await Promise.all([settingsApi.businessRules(), usersApi.list(), apiKeysApi.list()]);
    if (r.ok) setRules({ ...r.data, calling_hours_start: r.data.calling_hours_start.slice(0, 5), calling_hours_end: r.data.calling_hours_end.slice(0, 5) }); else setError(r.error);
    if (u.ok) setUsers(u.data); else setError((e) => e ?? u.error);
    if (k.ok) setKeys(k.data); else setError((e) => e ?? k.error);
    setLoading(false);
  }, []);
  useEffect(() => { void load(); }, [load]);
  async function saveRules(event: React.FormEvent) {
    event.preventDefault(); if (!rules) return; setSaving(true); setError(null);
    const result = await settingsApi.updateBusinessRules({ timezone: rules.timezone, calling_hours_start: rules.calling_hours_start, calling_hours_end: rules.calling_hours_end, max_attempts: rules.max_attempts, retry_interval_minutes: rules.retry_interval_minutes, auto_call_enabled: rules.auto_call_enabled, recording_enabled: rules.recording_enabled, crm_auto_update_enabled: rules.crm_auto_update_enabled, whatsapp_enabled: rules.whatsapp_enabled, meeting_booking_enabled: rules.meeting_booking_enabled, callback_enabled: rules.callback_enabled, consent_required: rules.consent_required, allowed_crm_statuses: rules.allowed_crm_statuses });
    if (result.ok) setRules({ ...result.data, calling_hours_start: result.data.calling_hours_start.slice(0, 5), calling_hours_end: result.data.calling_hours_end.slice(0, 5) }); else setError(result.error); setSaving(false);
  }
  async function createKey(event: React.FormEvent) { event.preventDefault(); if (!keyName.trim()) return; const result = await apiKeysApi.create(keyName.trim()); if (result.ok) { setNewKey(result.data.key); setKeyName(''); await load(); } else setError(result.error); }
  async function revokeKey(id: string) { const result = await apiKeysApi.revoke(id); if (result.ok) await load(); else setError(result.error); }
  function toggle(field: keyof BusinessRules, value: boolean) { if (rules) setRules({ ...rules, [field]: value }); }
  return <div className="page-content"><PageHeading title="Settings" description="Workspace calling policy, team access, and API keys." icon={Settings} /><ErrorNotice value={error} />
    {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading workspace settings…</div> : <>
      {rules && <form className="card settings-form" onSubmit={(event) => void saveRules(event)}><div className="card-heading"><div><h3>Calling and automation rules</h3><p>These controls are enforced by the backend for your tenant.</p></div></div><div className="settings-grid"><label className="field-label">Timezone<input className="field-input" value={rules.timezone} onChange={(e) => setRules({ ...rules, timezone: e.target.value })} /></label><label className="field-label">Calling starts<input className="field-input" type="time" value={rules.calling_hours_start} onChange={(e) => setRules({ ...rules, calling_hours_start: e.target.value })} /></label><label className="field-label">Calling ends<input className="field-input" type="time" value={rules.calling_hours_end} onChange={(e) => setRules({ ...rules, calling_hours_end: e.target.value })} /></label><label className="field-label">Maximum attempts<input className="field-input" type="number" min={1} max={20} value={rules.max_attempts} onChange={(e) => setRules({ ...rules, max_attempts: Number(e.target.value) })} /></label><label className="field-label">Retry interval (minutes)<input className="field-input" type="number" min={1} max={10080} value={rules.retry_interval_minutes} onChange={(e) => setRules({ ...rules, retry_interval_minutes: Number(e.target.value) })} /></label><label className="field-label">Allowed CRM statuses<input className="field-input" value={rules.allowed_crm_statuses} onChange={(e) => setRules({ ...rules, allowed_crm_statuses: e.target.value })} /></label></div><div className="toggle-grid">{([['auto_call_enabled', 'Automatic calls'], ['recording_enabled', 'Call recording'], ['crm_auto_update_enabled', 'CRM write-back'], ['whatsapp_enabled', 'WhatsApp messages'], ['meeting_booking_enabled', 'Meeting booking'], ['callback_enabled', 'Callback scheduling'], ['consent_required', 'Require call consent']] as [keyof BusinessRules, string][]).map(([field, label]) => <label className="toggle-row" key={field}><span>{label}</span><input type="checkbox" checked={Boolean(rules[field])} onChange={(e) => toggle(field, e.target.checked)} /></label>)}</div><button className="button button-primary" disabled={saving}>{saving ? 'Saving…' : 'Save settings'}</button></form>}
      <section className="dashboard-grid settings-lower"><div className="card"><div className="card-heading"><div><h3>Workspace members</h3><p>{users.length} user{users.length === 1 ? '' : 's'} in this tenant</p></div><Users size={17} /></div>{users.length ? <div className="data-list">{users.map((user) => <div className="data-row" key={user.id}><span className="data-icon"><Users size={15} /></span><span className="data-copy"><strong>{user.full_name || user.email}</strong><small>{user.email}</small></span><span className="badge badge-amber">{user.role}</span></div>)}</div> : <p className="muted-copy">No team members returned.</p>}</div><div className="card"><div className="card-heading"><div><h3>API keys</h3><p>Keys are shown once when created.</p></div><KeyRound size={17} /></div><form className="inline-create" onSubmit={(event) => void createKey(event)}><input className="field-input" aria-label="API key name" value={keyName} onChange={(event) => setKeyName(event.target.value)} placeholder="Key name" /><button className="button button-primary" disabled={!keyName.trim()}><Plus size={14} />Create</button></form>{newKey && <div className="new-key"><strong>Copy this key now</strong><code>{newKey}</code><button className="quiet-button" onClick={() => setNewKey(null)}>Dismiss</button></div>}<div className="data-list">{keys.filter((key) => !key.revoked).map((key) => <div className="data-row" key={key.id}><span className="data-copy"><strong>{key.name}</strong><small>{key.prefix}••••</small></span><button className="quiet-button danger-text" onClick={() => void revokeKey(key.id)}>Revoke</button></div>)}</div></div></section>
    </>}
  </div>;
}
