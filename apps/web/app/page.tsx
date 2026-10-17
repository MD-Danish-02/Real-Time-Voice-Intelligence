'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertCircle,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  Bell,
  BookOpen,
  Bot,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  CloudUpload,
  Database,
  FileSpreadsheet,
  FileText,
  LayoutDashboard,
  Link2,
  Loader2,
  LogOut,
  Menu,
  MoreHorizontal,
  Phone,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Sparkles,
  Trash2,
  Users,
  X,
  Zap,
} from 'lucide-react';
import {
  crmApi,
  leadApi,
  agentApi,
  providerSlug,
  knowledgeApi,
  analyticsApi,
  meetingsApi,
} from '@/lib/api';
import { AnalyticsModule, CallsModule, IntegrationsModule, MeetingsModule, SettingsModule } from './WorkspaceModules';
import { useAuth } from '@/lib/auth-context';
import { useRouter } from 'next/navigation';
import type {
  AgentStatus,
  CRMConnection,
  ColumnMapping,
  ImportConfirmResponse,
  ImportPreviewResponse,
  Lead,
  LeadImportJob,
  KnowledgeDocument,
  SyncSummary,
  AuthUser,
  AnalyticsOverview,
  Meeting,
} from '@/lib/types';

type View = 'Dashboard' | 'CRM' | 'Knowledge' | 'Calls' | 'Meetings' | 'Analytics' | 'Integrations' | 'Settings';

type NavItem = { label: View; icon: typeof LayoutDashboard };

const navItems: NavItem[] = [
  { label: 'Dashboard', icon: LayoutDashboard },
  { label: 'CRM', icon: Users },
  { label: 'Knowledge', icon: BookOpen },
  { label: 'Calls', icon: Phone },
  { label: 'Meetings', icon: CalendarDays },
  { label: 'Analytics', icon: BarChart3 },
  { label: 'Integrations', icon: Link2 },
  { label: 'Settings', icon: Settings },
];

const onboardingSteps = [
  { number: '01', title: 'Connect your CRM', description: 'Bring your leads into one calm workspace.', icon: Database },
  { number: '02', title: 'Add your knowledge', description: 'Give your agent the context to be useful.', icon: BookOpen },
  { number: '03', title: 'Activate your agent', description: 'Review your setup and start when ready.', icon: Zap },
];

type IntegrationDef = {
  name: string;
  description: string;
  color: string;
  mark: string;
  type: 'credential' | 'upload' | 'oauth';
};

const integrations: IntegrationDef[] = [
  { name: 'HubSpot', description: 'Sync contacts, lifecycle stages, and activities.', color: '#ff6846', mark: 'H', type: 'oauth' },
  { name: 'Salesforce', description: 'Connect leads, contacts, and opportunity context.', color: '#0ea5e9', mark: 'S', type: 'oauth' },
  { name: 'Zoho', description: 'Connect leads and customer records from Zoho CRM.', color: '#e85d3f', mark: 'Z', type: 'oauth' },
  { name: 'Pipedrive', description: 'Keep deal stages and lead activity in step.', color: '#263238', mark: 'P', type: 'oauth' },
  { name: 'Google Sheets', description: 'Import a collaborative lead source with Google OAuth.', color: '#16a34a', mark: 'G', type: 'oauth' },
  { name: 'Excel', description: 'Upload an Excel workbook and import leads directly.', color: '#10B981', mark: 'E', type: 'upload' },
  { name: 'CSV', description: 'Upload a CSV file with your lead data.', color: '#F59E0B', mark: 'C', type: 'upload' },
  { name: 'Custom REST API', description: 'Connect a custom endpoint as a lead source.', color: '#7C3AED', mark: 'A', type: 'credential' },
];

const fieldOptions = ['name', 'phone', 'email', 'company', 'status', 'notes'] as const;

function EmptyState({ icon: Icon, title, description, action, onAction }: { icon: typeof FileText; title: string; description: string; action?: string; onAction?: () => void }) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon size={20} /></div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action && <button className="button button-primary" onClick={onAction}>{action}<ArrowUpRight size={15} /></button>}
    </div>
  );
}

function statusBadgeClass(status: string): string {
  if (status === 'connected') return 'badge-emerald';
  if (status === 'failed' || status === 'error') return 'badge-rose';
  return 'badge-amber';
}

function useAgentStatus() {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    setLoading(true);
    const result = await agentApi.getStatus();
    if (result.ok) setStatus(result.data);
    setLoading(false);
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  return { status, loading, refresh };
}

function agentLabel(status: AgentStatus | null): { text: string; color: string } {
  if (!status) return { text: 'Waiting for setup', color: '#F59E0B' };
  if (status.ai_ready || (status.crm_connected > 0 && status.knowledge_count > 0)) return { text: 'AI Ready', color: '#10B981' };
  if (status.knowledge_count > 0 && status.crm_connected === 0) return { text: 'Connect CRM', color: '#06B6D4' };
  if (status.crm_connected > 0 || status.lead_count > 0) return { text: 'Add knowledge', color: '#06B6D4' };
  return { text: 'Waiting for setup', color: '#F59E0B' };
}

function Sidebar({ activeView, onNavigate, isOpen, onClose, agentStatus, tenantName, initials }: { activeView: View; onNavigate: (view: View) => void; isOpen: boolean; onClose: () => void; agentStatus: AgentStatus | null; tenantName: string; initials: string }) {
  const label = agentLabel(agentStatus);
  return (
    <aside className={`sidebar ${isOpen ? 'sidebar-open' : ''}`}>
      <div className="brand-row">
        <div className="brand-mark"><Bot size={18} /></div>
        <span>vox<span className="brand-accent">ai</span></span>
        <button className="mobile-close" onClick={onClose} aria-label="Close navigation"><X size={17} /></button>
      </div>
      <div className="workspace-switcher">
        <div className="workspace-avatar">{initials}</div>
        <div><strong>{tenantName}</strong><span>Workspace</span></div>
        <ChevronDown size={14} />
      </div>
      <div className="nav-label">Workspace</div>
      <nav className="nav-list">
        {navItems.map(({ label, icon: Icon }) => (
          <button key={label} className={`nav-item ${activeView === label ? 'nav-active' : ''}`} onClick={() => { onNavigate(label); onClose(); }}>
            <Icon size={17} strokeWidth={activeView === label ? 2.2 : 1.8} />
            <span>{label}</span>
            {label === 'Calls' && <span className="nav-ping" />}
          </button>
        ))}
      </nav>
      <div className="sidebar-spacer" />
      <div className="agent-card">
        <div className="agent-orbit"><Bot size={22} /><span /></div>
        <div className="agent-copy"><strong>AI Agent</strong><span><i className="status-dot" style={{ background: label.color, boxShadow: `0 0 0 3px ${label.color}30` }} /> {label.text}</span></div>
        <button aria-label="Agent help"><CircleHelp size={15} /></button>
      </div>
      <div className="sidebar-footer"><span>VoxAI Cloud</span><span>v0.1</span></div>
    </aside>
  );
}

function Topbar({ onMenu, user, tenantRole, activeView, onSignOut }: { onMenu: () => void; user: AuthUser | null; tenantRole: string; activeView: View; onSignOut: () => void }) {
  const displayName = user?.full_name || user?.email?.split('@')[0] || 'User';
  const email = user?.email ?? '';
  const initials = displayName.split(' ').map((w: string) => w[0]).slice(0, 2).join('').toUpperCase() || 'U';
  return (
    <header className="topbar">
      <button className="mobile-menu" onClick={onMenu} aria-label="Open navigation"><Menu size={20} /></button>
      <div className="breadcrumbs"><span>Workspace</span><span className="slash">/</span><strong>{activeView}</strong></div>
      <div className="topbar-actions">
        <div className="search-box"><Search size={15} /><input aria-label="Search" placeholder="Search anything..." /></div>
        <button className="icon-button notification" aria-label="Notifications"><Bell size={17} /><span /></button>
        <div className="profile">
          <div className="profile-avatar">{initials}</div>
          <div className="profile-text"><strong>{displayName}</strong><span>{tenantRole}</span></div>
          <ChevronDown size={14} />
        </div>
        <button className="quiet-button quiet-small" onClick={onSignOut} aria-label="Sign out" title="Sign out" style={{ marginLeft: 8 }}><LogOut size={14} />Sign out</button>
      </div>
    </header>
  );
}

function Dashboard({ onNavigate, agentStatus, user, tenantName }: { onNavigate: (view: View) => void; agentStatus: AgentStatus | null; user: AuthUser | null; tenantName: string }) {
  const [analytics, setAnalytics] = useState<AnalyticsOverview | null>(null);
  const [upcomingMeetings, setUpcomingMeetings] = useState<Meeting[]>([]);
  const leadCount = agentStatus?.lead_count ?? 0;
  const crmConnected = agentStatus?.crm_connected ?? 0;
  const knowledgeCount = agentStatus?.knowledge_count ?? 0;
  const setupProgress = useMemo(() => {
    let p = 0;
    if (crmConnected > 0 || leadCount > 0) p += 50;
    if (knowledgeCount > 0) p += 50;
    return p;
  }, [crmConnected, leadCount, knowledgeCount]);
  const label = agentLabel(agentStatus);
  const firstName = user?.full_name?.split(' ')[0] || user?.email?.split('@')[0] || 'there';
  const today = new Date().toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  useEffect(() => {
    void Promise.all([analyticsApi.overview(), meetingsApi.list()]).then(([stats, meetings]) => {
      if (stats.ok) setAnalytics(stats.data);
      if (meetings.ok) setUpcomingMeetings(meetings.data.filter((meeting) => meeting.scheduled_at && new Date(meeting.scheduled_at) >= new Date() && meeting.status === 'booked').slice(0, 3));
    });
  }, []);

  return (
    <div className="page-content">
      <div className="page-heading">
        <div><p className="eyebrow">{today}</p><h1>Good morning, {firstName} <span className="wave">✦</span></h1><p className="page-subtitle">Your workspace is ready when you are. Let&apos;s set up your AI agent.</p></div>
        <div className="heading-actions"><button className="date-button" disabled><CalendarDays size={15} />All time</button><button className="button button-primary" onClick={() => onNavigate('CRM')}><Plus size={16} />Connect CRM</button></div>
      </div>
      <section className="setup-banner">
        <div className="setup-glow" />
        <div className="setup-main"><div className="setup-icon"><Sparkles size={20} /></div><div><div className="setup-kicker">Your setup journey</div><h2>Build an AI agent that knows your business.</h2><p>Connect a lead source and add your knowledge. We&apos;ll take it from there.</p></div></div>
        <div className="setup-progress"><div className="progress-top"><span>Setup progress</span><strong>{setupProgress}%</strong></div><div className="progress-track"><span style={{ width: `${setupProgress}%` }} /></div><button className="text-button" onClick={() => onNavigate('CRM')}>Continue setup <ArrowUpRight size={15} /></button></div>
      </section>
      <div className="section-heading"><div><h2>At a glance</h2><p>Live workspace signals appear here as you connect your systems.</p></div></div>
      <section className="metrics-grid">
        <div className="metric-card"><div className="metric-top"><span>Total calls</span><span className="metric-icon indigo"><Phone size={16} /></span></div><strong>{analytics?.calls.total ? analytics.calls.total.toLocaleString() : '—'}</strong><p>{analytics?.calls.total ? `${analytics.calls.answered} answered` : 'No calls yet'}</p></div>
        <div className="metric-card"><div className="metric-top"><span>Active leads</span><span className="metric-icon cyan"><Users size={16} /></span></div><strong>{leadCount > 0 ? leadCount.toLocaleString() : '—'}</strong><p>{leadCount > 0 ? `${leadCount} lead${leadCount !== 1 ? 's' : ''} imported` : 'Connect your CRM to begin'}</p></div>
        <div className="metric-card"><div className="metric-top"><span>Meetings booked</span><span className="metric-icon amber"><CalendarDays size={16} /></span></div><strong>{analytics?.meetings.booked ? analytics.meetings.booked.toLocaleString() : '—'}</strong><p>{analytics?.meetings.booked ? `${analytics.meetings.requested} request${analytics.meetings.requested === 1 ? '' : 's'} pending` : 'No meetings scheduled'}</p></div>
        <div className="metric-card"><div className="metric-top"><span>Agent status</span><span className="metric-icon emerald"><Activity size={16} /></span></div><strong className="status-value"><i className="status-dot" style={{ background: label.color, boxShadow: `0 0 0 3px ${label.color}30` }} />{label.text}</strong><p>{label.text === 'AI Ready' ? 'CRM and knowledge context are ready' : 'Waiting for your setup'}</p></div>
      </section>
      <section className="dashboard-grid">
        <div className="card chart-card"><div className="card-heading"><div><h3>Call performance</h3><p>Counts from saved call lifecycle records.</p></div><button className="card-menu" aria-label="More call performance options"><MoreHorizontal size={18} /></button></div>{analytics?.calls.total ? <div className="dashboard-call-stats"><div><strong>{analytics.calls.inbound}</strong><span>Inbound</span></div><div><strong>{analytics.calls.outbound}</strong><span>Outbound</span></div><div><strong>{analytics.calls.answered}</strong><span>Answered</span></div><div><strong>{analytics.calls.failed}</strong><span>Failed</span></div></div> : <EmptyState icon={BarChart3} title="No calls yet" description="Call activity will appear after your first recorded conversation." />}</div>
        <div className="card insight-card"><div className="card-heading"><div><h3>AI readiness</h3><p>A clear path to your first conversation.</p></div><Sparkles size={18} className="heading-spark" /></div><div className="readiness-steps">{onboardingSteps.map((step, index) => { const Icon = step.icon; const isDone = (index === 0 && (crmConnected > 0 || leadCount > 0)) || (index === 1 && knowledgeCount > 0); return <button className={`readiness-step ${isDone ? 'done' : index === 0 ? 'current' : ''}`} key={step.number} onClick={() => onNavigate(index === 0 ? 'CRM' : 'Knowledge')}><span className="step-number">{step.number}</span><span className="step-icon">{isDone ? <CheckCircle2 size={16} /> : <Icon size={16} />}</span><span className="step-copy"><strong>{step.title}</strong><small>{step.description}</small></span>{isDone ? <CheckCircle2 size={15} className="step-check" /> : <ArrowUpRight size={15} />}</button>; })}</div></div>
      </section>
      <section className="dashboard-grid lower-grid"><div className="card"><div className="card-heading"><div><h3>Recent activity</h3><p>Latest persisted workspace totals.</p></div><button className="card-menu" aria-label="More recent activity options"><MoreHorizontal size={18} /></button></div>{leadCount > 0 ? <div className="connection-list"><div className="connection-row"><div className="integration-mark" style={{ color: '#10B981', background: '#10B98114' }}>L</div><div className="connection-copy"><strong>{leadCount} lead{leadCount !== 1 ? 's' : ''} in your workspace</strong><span>{analytics?.crm_sync.imported ?? 0} imported through CRM sync</span></div></div></div> : <EmptyState icon={Activity} title="Nothing happening yet" description="Connect your CRM or upload leads to start receiving activity." action="Connect CRM" onAction={() => onNavigate('CRM')} />}</div><div className="card"><div className="card-heading"><div><h3>Upcoming meetings</h3><p>Confirmed meetings only.</p></div><button className="card-menu" aria-label="More upcoming meeting options"><MoreHorizontal size={18} /></button></div>{upcomingMeetings.length ? <div className="connection-list">{upcomingMeetings.map((meeting) => <div className="connection-row" key={meeting.id}><div className="integration-mark" style={{ color: '#D97706', background: '#F59E0B14' }}><CalendarDays size={16} /></div><div className="connection-copy"><strong>{meeting.title}</strong><span>{new Date(meeting.scheduled_at!).toLocaleString()}</span></div></div>)}</div> : <EmptyState icon={CalendarDays} title="No meetings scheduled" description="Confirmed meetings will appear here." action="Explore meetings" onAction={() => onNavigate('Meetings')} />}</div></section>
    </div>
  );
}

function ConnectModal({ provider, onClose, onConnected }: { provider: string; onClose: () => void; onConnected: () => void }) {
  const [baseUrl, setBaseUrl] = useState('');
  const [listPath, setListPath] = useState('/leads');
  const [apiKey, setApiKey] = useState('');
  const [notePath, setNotePath] = useState('');
  const [activityPath, setActivityPath] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isOAuth = provider !== 'Custom REST API';

  async function submit() {
    if (provider === 'Custom REST API' && !baseUrl.trim()) {
      setError('Enter the base URL for your lead API.');
      return;
    }
    setLoading(true);
    setError(null);
    const config = provider === 'Custom REST API' ? {
      base_url: baseUrl.trim(), list_path: listPath.trim() || '/leads',
      auth_header: 'Authorization', api_key: apiKey.trim(),
      ...(notePath.trim() ? { note_path: notePath.trim() } : {}),
      ...(activityPath.trim() ? { activity_path: activityPath.trim() } : {}),
      field_map: { id: 'id', name: 'name', phone: 'phone', email: 'email', company: 'company', status: 'status' },
    } : undefined;
    const result = await crmApi.connect(provider, config);
    setLoading(false);
    if (!result.ok) { setError(result.error); return; }
    if (result.data.authorization_url) {
      window.location.assign(result.data.authorization_url);
      return;
    }
    if (result.data.connected) onConnected();
    else setError('The provider did not confirm a connection. Check its setup and try again.');
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><div><h2>Connect {provider}</h2><p>{isOAuth ? 'Authorize VoxAI securely with your provider.' : 'Configure the REST endpoint used for lead synchronization.'}</p></div><button className="icon-button" onClick={onClose} aria-label="Close"><X size={18} /></button></div>
        <div className="modal-body">
          {isOAuth ? <div className="modal-note">VoxAI will request provider authorization. Credentials are handled by the backend and are not stored in this browser.</div> : <>
            <label className="field-label">API base URL</label>
            <input className="field-input" type="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com" autoFocus />
            <label className="field-label" style={{ marginTop: 14 }}>Leads path</label>
            <input className="field-input" value={listPath} onChange={(e) => setListPath(e.target.value)} placeholder="/leads" />
            <label className="field-label" style={{ marginTop: 14 }}>API token <span className="field-hint">Optional · sent using Authorization</span></label>
            <input className="field-input" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Bearer token or API key" />
            <label className="field-label" style={{ marginTop: 14 }}>Notes endpoint <span className="field-hint">Optional · use {'{lead_id}'} in the path</span></label>
            <input className="field-input" value={notePath} onChange={(e) => setNotePath(e.target.value)} placeholder="/leads/{lead_id}/notes" />
            <label className="field-label" style={{ marginTop: 14 }}>Activities endpoint <span className="field-hint">Optional · use {'{lead_id}'} in the path</span></label>
            <input className="field-input" value={activityPath} onChange={(e) => setActivityPath(e.target.value)} placeholder="/leads/{lead_id}/activities" />
          </>}
          {error && <div className="modal-error">{error}</div>}
          {!isOAuth && <div className="modal-note">Use an HTTPS endpoint you control. The backend stores the token encrypted and will report sync failures clearly.</div>}
        </div>
        <div className="modal-footer"><button className="quiet-button" onClick={onClose}>Cancel</button><button className="button button-primary" onClick={submit} disabled={loading}>{loading ? <><Loader2 size={16} className="spin" />Connecting…</> : <>{isOAuth ? 'Continue to provider' : 'Save connection'} <ArrowUpRight size={15} /></>}</button></div>
      </div>
    </div>
  );
}

function ExternalLinkHint() {
  return <span className="field-hint-icon" />;
}

function GoogleSheetsConfigModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [spreadsheetId, setSpreadsheetId] = useState('');
  const [sheets, setSheets] = useState<string[]>([]);
  const [sheetName, setSheetName] = useState('');
  const [headers, setHeaders] = useState<string[]>([]);
  const [mapping, setMapping] = useState<ColumnMapping>({ name: null, phone: null, email: null, company: null, status: null, notes: null });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadSheets() {
    if (!spreadsheetId.trim()) { setError('Enter the spreadsheet ID from its Google Sheets URL.'); return; }
    setLoading(true); setError(null); setHeaders([]);
    const result = await crmApi.googleSheetsListSheets(spreadsheetId.trim());
    setLoading(false);
    if (!result.ok) { setError(result.error); return; }
    setSheets(result.data);
    setSheetName(result.data[0] ?? '');
    if (!result.data.length) setError('No tabs were found in this spreadsheet.');
  }

  async function detectHeaders() {
    if (!spreadsheetId.trim() || !sheetName) { setError('Choose a spreadsheet and a sheet tab first.'); return; }
    setLoading(true); setError(null);
    const result = await crmApi.googleSheetsHeaders(spreadsheetId.trim(), sheetName);
    setLoading(false);
    if (!result.ok) { setError(result.error); return; }
    setHeaders(result.data);
    const find = (patterns: string[]) => result.data.find((header) => patterns.includes(header.trim().toLowerCase())) ?? null;
    setMapping({
      name: find(['name', 'full name', 'full_name', 'contact name', 'lead name']),
      phone: find(['phone', 'phone number', 'mobile', 'mobile number', 'contact number']),
      email: find(['email', 'email address', 'e-mail']),
      company: find(['company', 'company name', 'organization', 'organisation']),
      status: find(['status', 'lead status', 'stage']),
      notes: find(['notes', 'note', 'comments', 'remarks']),
    });
  }

  async function save() {
    if (!headers.length || !mapping.name || !mapping.phone) {
      setError('Detect the sheet headers and map both name and phone before saving.'); return;
    }
    setLoading(true); setError(null);
    const fieldByHeader: Record<string, string> = {};
    for (const [field, header] of Object.entries(mapping)) if (header) fieldByHeader[header] = field;
    const columns = headers.map((header) => fieldByHeader[header] ?? `custom:${header}`);
    const quotedSheet = sheetName.replace(/'/g, "''");
    const configResult = await crmApi.saveConfig('Google Sheets', {
      spreadsheet_id: spreadsheetId.trim(), sheet_range: `'${quotedSheet}'!A2:ZZ`, columns,
    });
    if (!configResult.ok) { setLoading(false); setError(configResult.error); return; }
    const syncResult = await crmApi.sync('Google Sheets');
    setLoading(false);
    if (!syncResult.ok) { setError(`Sheet configuration was saved, but the first sync failed: ${syncResult.error}`); return; }
    onSaved();
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><div><h2>Configure Google Sheets</h2><p>Choose a sheet tab and map its header row to lead fields.</p></div><button className="icon-button" onClick={onClose} aria-label="Close"><X size={18} /></button></div>
        <div className="modal-body">
          <label className="field-label">Spreadsheet ID</label>
          <div className="search-box" style={{ width: '100%' }}><input value={spreadsheetId} onChange={(e) => setSpreadsheetId(e.target.value)} placeholder="ID from docs.google.com/spreadsheets/d/..." /></div>
          <button className="quiet-button" style={{ marginTop: 12 }} onClick={loadSheets} disabled={loading}>{loading ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />} Load sheets</button>
          {sheets.length > 0 && <><label className="field-label" style={{ marginTop: 16 }}>Sheet tab</label><select className="field-input" value={sheetName} onChange={(e) => { setSheetName(e.target.value); setHeaders([]); }}><option value="">Choose a tab</option>{sheets.map((sheet) => <option value={sheet} key={sheet}>{sheet}</option>)}</select><button className="quiet-button" style={{ marginTop: 10 }} onClick={detectHeaders} disabled={loading || !sheetName}>Detect headers</button></>}
          {headers.length > 0 && <div className="mapping-section"><h4>Map columns to lead fields</h4><p>VoxAI reads rows below the detected header row.</p><div className="mapping-grid">{fieldOptions.map((field) => <div className="mapping-row" key={field}><label className={field === 'name' || field === 'phone' ? 'required' : ''}>{field}{field === 'name' || field === 'phone' ? ' *' : ''}</label><select className="field-input" value={mapping[field] ?? ''} onChange={(e) => setMapping({ ...mapping, [field]: e.target.value || null })}><option value="">— Skip —</option>{headers.map((header) => <option key={header} value={header}>{header}</option>)}</select></div>)}</div></div>}
          {error && <div className="modal-error">{error}</div>}
        </div>
        <div className="modal-footer"><button className="quiet-button" onClick={onClose}>Cancel</button><button className="button button-primary" onClick={save} disabled={loading || !headers.length}>{loading ? <><Loader2 size={16} className="spin" />Saving & syncing…</> : <>Save and sync <ArrowRight size={15} /></>}</button></div>
      </div>
    </div>
  );
}

function LeadDetailModal({ leadId, onClose, onUpdated }: { leadId: string; onClose: () => void; onUpdated: () => void }) {
  const [lead, setLead] = useState<Lead | null>(null);
  const [eligibility, setEligibility] = useState<{ eligible: boolean; reason: string; next_action: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    const [details, eligible] = await Promise.all([leadApi.detail(leadId), leadApi.eligibility(leadId)]);
    if (!details.ok) setError(details.error); else setLead(details.data);
    if (eligible.ok) setEligibility(eligible.data);
    setLoading(false);
  }, [leadId]);
  useEffect(() => { void load(); }, [load]);

  async function setDnc(value: boolean) {
    setBusy(true); setError(null); setMessage(null);
    const result = await leadApi.setDoNotCall(leadId, value);
    setBusy(false);
    if (!result.ok) { setError(result.error); return; }
    setMessage(value ? 'This lead is marked do not call. Any pending call jobs were canceled.' : 'Manual do not call was removed. Eligibility still follows your CRM status and calling rules.');
    await load(); onUpdated();
  }

  async function queueCall() {
    setBusy(true); setError(null); setMessage(null);
    const result = await leadApi.queueCall(leadId);
    setBusy(false);
    if (!result.ok) { setError(result.error); return; }
    setMessage(result.data.queued ? 'Call queued. Final eligibility is checked again before dialing.' : `Call not queued: ${result.data.reason}.`);
    onUpdated();
  }

  const customFields = lead?.custom_fields ?? {};
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><div><p className="eyebrow">Lead record</p><h2>{lead?.name ?? 'Lead details'}</h2><p>{lead?.company ?? 'Company not provided'}</p></div><button className="icon-button" onClick={onClose} aria-label="Close"><X size={18} /></button></div>
        <div className="modal-body">
          {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading lead…</div> : error && !lead ? <div className="modal-error">{error}</div> : lead && <>
            <div className="lead-detail-grid"><div><span>Email</span><strong>{lead.email ?? '—'}</strong></div><div><span>Phone</span><strong>{lead.phone ?? '—'}</strong></div><div><span>CRM status</span><strong>{lead.crm_status ?? '—'}</strong></div><div><span>Call status</span><strong>{lead.calling_status}</strong></div><div><span>Attempts</span><strong>{lead.attempt_count}</strong></div><div><span>Eligibility</span><strong>{eligibility?.eligible ? 'Eligible' : eligibility?.reason ?? 'Unavailable'}</strong></div></div>
            {eligibility && <p className="modal-note">Eligibility is evaluated from tenant policy, call history, consent and the current DNC state. Next action: {eligibility.next_action}.</p>}
            {Object.keys(customFields).length > 0 && <><h3 className="detail-subheading">Custom fields</h3><div className="lead-detail-grid">{Object.entries(customFields).map(([key, value]) => <div key={key}><span>{key}</span><strong>{String(value)}</strong></div>)}</div></>}
            {message && <div className="sync-result-banner"><CheckCircle2 size={16} />{message}</div>}
            {error && <div className="modal-error">{error}</div>}
          </>}
        </div>
        <div className="modal-footer">
          {lead && <><button className="quiet-button" onClick={() => void setDnc(!lead.do_not_call)} disabled={busy}>{lead.do_not_call ? 'Remove manual DNC' : 'Mark do not call'}</button><button className="button button-primary" onClick={queueCall} disabled={busy || !eligibility?.eligible || lead.do_not_call}>{busy ? <Loader2 size={15} className="spin" /> : <Phone size={15} />} Queue AI call</button></>}
          <button className="quiet-button" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

type ImportStep = 'upload' | 'sheet' | 'preview' | 'mapping' | 'validating' | 'complete' | 'error';

function ExcelImportModal({ provider, onClose, onCompleted }: { provider: string; onClose: () => void; onCompleted: () => void }) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [step, setStep] = useState<ImportStep>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null);
  const [selectedSheet, setSelectedSheet] = useState<string | null>(null);
  const [mapping, setMapping] = useState<ColumnMapping>({ name: null, phone: null, email: null, company: null, status: null, notes: null });
  const [result, setResult] = useState<ImportConfirmResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const acceptType = provider === 'CSV' ? '.csv' : '.xlsx,.xls';

  async function handleFileSelect(selectedFile: File) {
    setFile(selectedFile);
    setLoading(true);
    setError(null);
    const res = await leadApi.previewExcel(selectedFile);
    setLoading(false);
    if (res.ok) {
      setPreview(res.data);
      setSelectedSheet(res.data.sheets[0] ?? null);
      setMapping(res.data.suggestedMapping);
      setStep(res.data.sheets.length > 1 ? 'sheet' : 'preview');
    } else {
      setError(res.error);
      setStep('error');
    }
  }

  async function continueWithSheet() {
    if (!preview || !selectedSheet) return;
    setLoading(true);
    setError(null);
    const res = await leadApi.selectImportSheet(preview.jobId, selectedSheet);
    setLoading(false);
    if (!res.ok) { setError(res.error); return; }
    setPreview(res.data);
    setMapping(res.data.suggestedMapping);
    setStep('preview');
  }

  function autoDetectMapping(headers: string[]) {
    const lower = headers.map((h) => h.toLowerCase());
    const find = (patterns: string[]): string | null => {
      for (const p of patterns) {
        const idx = lower.findIndex((h) => h.includes(p));
        if (idx >= 0) return headers[idx];
      }
      return null;
    };
    setMapping({
      name: find(['name', 'full name', 'contact', 'first name']),
      phone: find(['phone', 'mobile', 'number', 'tel']),
      email: find(['email', 'mail', 'e-mail']),
      company: find(['company', 'org', 'organization', 'business']),
      status: find(['status', 'stage', 'lifecycle', 'state']),
      notes: find(['notes', 'note', 'comment', 'description']),
    });
  }

  async function handleConfirmImport() {
    if (!file || !preview) return;
    if (!mapping.phone || !mapping.name) {
      setError('Map both a name column and a phone column to continue.');
      return;
    }
    setLoading(true);
    setError(null);
    setStep('validating');
    const res = await leadApi.confirmImport(file, preview.jobId, mapping, selectedSheet ?? undefined);
    setLoading(false);
    if (res.ok) {
      setResult(res.data);
      setStep('complete');
    } else {
      setError(res.error);
      setStep('error');
    }
  }

  function reset() {
    setFile(null);
    setPreview(null);
    setSelectedSheet(null);
    setMapping({ name: null, phone: null, email: null, company: null, status: null, notes: null });
    setResult(null);
    setError(null);
    setStep('upload');
  }

  const steps = ['Upload', 'Select Sheet', 'Preview', 'Map Fields', 'Validate', 'Import', 'Complete'];
  const currentStepIndex = step === 'upload' ? 0 : step === 'sheet' ? 1 : step === 'preview' ? 2 : step === 'mapping' ? 3 : step === 'validating' ? 5 : step === 'complete' ? 6 : 0;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div><h2>Import leads from {provider}</h2><p>Upload, map columns, and import your leads.</p></div>
          <button className="icon-button" onClick={onClose} aria-label="Close"><X size={18} /></button>
        </div>
        <div className="import-stepper">
          {steps.map((s, i) => (
            <div key={s} className={`stepper-item ${i < currentStepIndex ? 'done' : i === currentStepIndex ? 'active' : ''}`}>
              <span className="stepper-dot">{i < currentStepIndex ? <CheckCircle2 size={12} /> : i + 1}</span>
              <span className="stepper-label">{s}</span>
              {i < steps.length - 1 && <span className="stepper-line" />}
            </div>
          ))}
        </div>
        <div className="modal-body">
          {step === 'upload' && (
            <div className="upload-zone-modal" onClick={() => fileInputRef.current?.click()}>
              <input ref={fileInputRef} type="file" accept={acceptType} style={{ display: 'none' }} onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleFileSelect(f); }} />
              <div className="upload-icon"><CloudUpload size={24} /></div>
              <h3>{file ? file.name : 'Drop file here or browse'}</h3>
              <p>{provider === 'CSV' ? 'CSV files up to 25 MB' : 'Excel files (.xlsx, .xls) up to 25 MB'}</p>
              {loading && <div className="inline-loading"><Loader2 size={18} className="spin" />Parsing file…</div>}
            </div>
          )}
          {step === 'sheet' && preview && (
            <div>
              <label className="field-label">Select sheet</label>
              <div className="sheet-list">
                {preview.sheets.map((s) => (
                  <button key={s} className={`sheet-option ${selectedSheet === s ? 'selected' : ''}`} onClick={() => setSelectedSheet(s)}>
                    <FileSpreadsheet size={18} /><span>{s}</span>{selectedSheet === s && <CheckCircle2 size={16} />}
                  </button>
                ))}
              </div>
            </div>
          )}
          {(step === 'preview' || step === 'mapping') && preview && (
            <>
              <div className="preview-info">
                <span><strong>{preview.totalRows}</strong> rows detected</span>
                <span><strong>{preview.headers.length}</strong> columns</span>
              </div>
              <div className="preview-table-wrapper">
                <table className="preview-table">
                  <thead><tr>{preview.headers.map((h) => <th key={h}>{h}</th>)}</tr></thead>
                  <tbody>{preview.previewRows.map((row, i) => (
                    <tr key={i}>{preview.headers.map((h) => <td key={h}>{row[h]}</td>)}</tr>
                  ))}</tbody>
                </table>
              </div>
              {step === 'mapping' && (
                <div className="mapping-section">
                  <h4>Map columns to lead fields</h4>
                  <p>Name and phone are required. Other fields are optional.</p>
                  <div className="mapping-grid">
                    {fieldOptions.map((field) => (
                      <div className="mapping-row" key={field}>
                        <label className={field === 'phone' ? 'required' : ''}>{field}{field === 'phone' && ' *'}</label>
                        <select className="field-input" value={mapping[field] ?? ''} onChange={(e) => setMapping({ ...mapping, [field]: e.target.value || null })}>
                          <option value="">— Skip —</option>
                          {preview.headers.map((h) => <option key={h} value={h}>{h}</option>)}
                        </select>
                        {mapping[field] && <CheckCircle2 size={16} className="mapping-check" />}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
          {step === 'validating' && (
            <div className="validating-state">
              <Loader2 size={32} className="spin" />
              <h3>Importing leads…</h3>
              <p>Validating rows, detecting duplicates, and importing valid leads.</p>
            </div>
          )}
          {step === 'complete' && result && (
            <div className="import-complete">
              <div className="complete-icon"><CheckCircle2 size={32} /></div>
              <h3>Import complete</h3>
              <div className="import-stats">
                <div className="stat-item"><span className="stat-label">Total rows</span><strong>{result.totalRows}</strong></div>
                <div className="stat-item"><span className="stat-label">Valid</span><strong>{result.validRows}</strong></div>
                <div className="stat-item"><span className="stat-label">Imported</span><strong className="stat-emerald">{result.importedRows}</strong></div>
                <div className="stat-item"><span className="stat-label">Duplicates</span><strong className="stat-amber">{result.duplicateRows}</strong></div>
                <div className="stat-item"><span className="stat-label">Invalid</span><strong className="stat-rose">{result.invalidRows}</strong></div>
                <div className="stat-item"><span className="stat-label">Skipped</span><strong>{result.skippedRows}</strong></div>
              </div>
            </div>
          )}
          {step === 'error' && (
            <div className="import-error-state">
              <AlertCircle size={32} />
              <h3>Import failed</h3>
              <div className="modal-error">{error}</div>
            </div>
          )}
        </div>
        <div className="modal-footer">
          {step === 'upload' && <button className="quiet-button" onClick={onClose}>Cancel</button>}
          {step === 'sheet' && <><button className="quiet-button" onClick={() => setStep('upload')}>Back</button><button className="button button-primary" onClick={continueWithSheet} disabled={!selectedSheet || loading}>{loading ? <><Loader2 size={16} className="spin" />Loading sheet…</> : <>Continue <ArrowRight size={15} /></>}</button></>}
          {step === 'preview' && <><button className="quiet-button" onClick={() => setStep(preview && preview.sheets.length > 1 ? 'sheet' : 'upload')}>Back</button><button className="button button-primary" onClick={() => setStep('mapping')}>Map columns <ArrowRight size={15} /></button></>}
          {step === 'mapping' && <><button className="quiet-button" onClick={() => setStep('preview')}>Back</button><button className="button button-primary" onClick={handleConfirmImport} disabled={loading || !mapping.phone || !mapping.name}>{loading ? <><Loader2 size={16} className="spin" />Importing…</> : <>Import leads <ArrowRight size={15} /></>}</button></>}
          {step === 'complete' && <button className="button button-primary" onClick={() => { onCompleted(); onClose(); }}>Done <CheckCircle2 size={15} /></button>}
          {step === 'error' && <button className="button button-primary" onClick={reset}>Try again</button>}
        </div>
      </div>
    </div>
  );
}

function ConnectionSyncInfo({ connection, onSync, syncing }: { connection: CRMConnection; onSync: () => void; syncing: boolean }) {
  const lastSync = connection.last_successful_sync_at;
  return (
    <div className="connection-actions">
      {lastSync && <span className="connection-sync-text">Last sync {new Date(lastSync).toLocaleDateString()}</span>}
      <button className="quiet-button quiet-small" onClick={onSync} disabled={syncing} aria-label="Sync now">
        {syncing ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}
        Sync
      </button>
    </div>
  );
}

function CRMView({ onNavigate, onAgentRefresh, oauthProvider }: { onNavigate: (view: View) => void; onAgentRefresh: () => void; oauthProvider: string | null }) {
  const [connections, setConnections] = useState<CRMConnection[]>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [importJobs, setImportJobs] = useState<LeadImportJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [leadsLoading, setLeadsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [leadsError, setLeadsError] = useState<string | null>(null);
  const [pendingProvider, setPendingProvider] = useState<string | null>(null);
  const [pendingUploadProvider, setPendingUploadProvider] = useState<string | null>(null);
  const [disconnectingId, setDisconnectingId] = useState<string | null>(null);
  const [syncingProvider, setSyncingProvider] = useState<string | null>(null);
  const [syncResult, setSyncResult] = useState<string | null>(null);
  const [configuringSheets, setConfiguringSheets] = useState(false);
  const [leadSearch, setLeadSearch] = useState('');
  const [leadQuery, setLeadQuery] = useState('');
  const [leadStatus, setLeadStatus] = useState('');
  const [selectedLeadId, setSelectedLeadId] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setLeadQuery(leadSearch.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [leadSearch]);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    const [connResult, leadResult, jobResult] = await Promise.all([
      crmApi.listConnections(),
      leadApi.list(20, 0, leadQuery || undefined, leadStatus || undefined),
      leadApi.listImportJobs(),
    ]);
    if (connResult.ok) setConnections(connResult.data);
    else setLoadError(connResult.error);
    if (leadResult.ok) { setLeads(leadResult.data); setLeadsError(null); }
    else setLeadsError(leadResult.error);
    if (jobResult.ok) setImportJobs(jobResult.data);
    setLoading(false);
    setLeadsLoading(false);
  }, [leadQuery, leadStatus]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (oauthProvider === 'google_sheets') setConfiguringSheets(true);
  }, [oauthProvider]);

  const connectedProviders = new Set(connections.filter((c) => ['connected', 'synced'].includes(c.status)).map((c) => c.provider));

  async function handleDisconnect(provider: string, id: string) {
    setDisconnectingId(id);
    const result = await crmApi.disconnect(provider);
    setDisconnectingId(null);
    if (result.ok) { await load(); void onAgentRefresh(); }
    else setLoadError(result.error);
  }

  async function handleSync(providerName: string) {
    setSyncingProvider(providerName);
    setSyncResult(null);
    setLoadError(null);
    const result = await crmApi.sync(providerName);
    setSyncingProvider(null);
    if (result.ok) {
      const s = result.data.sync;
      if (s) {
        setSyncResult(`Sync completed — ${s.imported} imported, ${s.updated} updated, ${s.failed} failed.`);
      }
      await load();
      void onAgentRefresh();
    } else {
      setLoadError(result.error);
    }
  }

  function handleProviderClick(integration: IntegrationDef) {
    if (integration.type === 'upload') {
      setPendingUploadProvider(integration.name);
    } else {
      setPendingProvider(integration.name);
    }
  }

  return (
    <div className="page-content">
      <div className="page-heading"><div><p className="eyebrow">Lead source</p><h1>Connect your CRM</h1><p className="page-subtitle">Import your leads once. Keep them synchronized automatically.</p></div><button className="quiet-button" onClick={load}><RefreshCw size={15} className={loading ? 'spin' : ''} />Refresh</button></div>
      <div className="crm-hero"><div className="crm-orb"><Database size={26} /></div><div><h2>One source of truth for your leads.</h2><p>VoxAI keeps a local, secure mirror of the customer context your agent needs. Your CRM remains the source of truth.</p></div></div>

      <div className="section-heading"><div><h2>Choose a provider</h2><p>We&apos;ll validate the connection before importing anything.</p></div></div>
      <div className="integration-grid">
        {integrations.map((integration) => {
          const isConnected = connectedProviders.has(integration.name);
          return (
            <div className="integration-card" key={integration.name}>
              <div className="integration-mark" style={{ color: integration.color, background: `${integration.color}14` }}>{integration.mark}</div>
              <div className="integration-copy"><h3>{integration.name}</h3><p>{integration.description}</p></div>
              <button className={`button ${isConnected ? 'button-success' : 'button-outline'}`} onClick={() => handleProviderClick(integration)}>
                {isConnected ? <><CheckCircle2 size={15} />Connected</> : integration.type === 'upload' ? <>Upload <ArrowUpRight size={15} /></> : <>Connect <ArrowUpRight size={15} /></>}
              </button>
            </div>
          );
        })}
      </div>

      {syncResult && <div className="card sync-result-banner"><CheckCircle2 size={18} className="sync-result-icon" /><span>{syncResult}</span></div>}

      <div className="card connected-card">
        <div className="card-heading"><div><h3>Connected sources</h3><p>Validated connections and synchronization status.</p></div><span className="badge badge-neutral">{connectedProviders.size} connected</span></div>
        {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading connections…</div>
          : loadError ? <div className="modal-error">{loadError}<button className="text-button" onClick={load}>Retry <RefreshCw size={14} /></button></div>
          : connections.length === 0 ? <EmptyState icon={Link2} title="No CRM connected" description="Connect your CRM or upload your leads to get started." action="Add knowledge next" onAction={() => onNavigate('Knowledge')} />
          : <div className="connection-list">{connections.map((connection) => {
            const integration = integrations.find((i) => i.name === connection.provider);
            return (
              <div className="connection-row" key={connection.id}>
                <div className="integration-mark" style={{ color: integration?.color ?? '#4F46E5', background: `${integration?.color ?? '#4F46E5'}14` }}>{integration?.mark ?? connection.provider[0]}</div>
                <div className="connection-copy"><strong>{connection.provider}</strong><span>{connection.display_name ?? connection.provider}</span></div>
                <span className={`badge ${statusBadgeClass(connection.status)}`}>{connection.status}</span>
                {connection.last_error && <span className="connection-error-text" title={connection.last_error}><AlertCircle size={14} /></span>}
                {['connected', 'synced'].includes(connection.status) && <ConnectionSyncInfo connection={connection} onSync={() => handleSync(connection.provider)} syncing={syncingProvider === connection.provider} />}
                {providerSlug(connection.provider) === 'google_sheets' && <button className="quiet-button quiet-small" onClick={() => setConfiguringSheets(true)}>Configure sheet</button>}
                <button className="icon-button danger" onClick={() => handleDisconnect(connection.provider, connection.id)} disabled={disconnectingId === connection.id} aria-label="Disconnect">{disconnectingId === connection.id ? <Loader2 size={16} className="spin" /> : <Trash2 size={16} />}</button>
              </div>
            );
          })}</div>}
      </div>

      <div className="section-heading"><div><h2>Imported leads</h2><p>Leads from your connected sources and file uploads.</p></div></div>
      <div className="card">
        <div className="leads-toolbar"><div className="search-box"><Search size={15} /><input aria-label="Search leads" placeholder="Search leads..." value={leadSearch} onChange={(e) => setLeadSearch(e.target.value)} /></div><select className="field-input" aria-label="Filter lead status" value={leadStatus} onChange={(e) => setLeadStatus(e.target.value)}><option value="">All lead statuses</option>{['New', 'Contacted', 'Interested', 'Converted', 'Do Not Call'].map((status) => <option key={status} value={status}>{status}</option>)}</select></div>
        {leadsLoading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading leads…</div>
          : leadsError ? <div className="modal-error">{leadsError}</div>
          : leads.length === 0 ? <EmptyState icon={Users} title="No leads yet" description="Connect a CRM or upload an Excel/CSV file to import your first leads." />
          : <div className="leads-table-wrapper">
            <table className="leads-table">
              <thead><tr><th>Contact</th><th>Phone</th><th>Company</th><th>Source</th><th>AI Call Status</th><th>Attempts</th><th>Do not call</th></tr></thead>
              <tbody>{leads.map((lead) => (
                <tr key={lead.id}>
                  <td><button className="lead-contact lead-open" onClick={() => setSelectedLeadId(lead.id)}><strong>{lead.name ?? 'Unknown'}</strong>{lead.email && <span>{lead.email}</span>}</button></td>
                  <td>{lead.phone ?? '—'}</td>
                  <td>{lead.company ?? '—'}</td>
                  <td><span className="badge badge-neutral">{lead.source_type ?? 'manual'}</span></td>
                  <td><span className={`badge ${lead.do_not_call ? 'badge-rose' : lead.eligible ? 'badge-emerald' : 'badge-amber'}`}>{lead.calling_status}</span></td>
                  <td>{lead.attempt_count}</td>
                  <td>{lead.do_not_call ? 'Yes' : 'No'}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>}
      </div>

      {importJobs.length > 0 && (
        <>
          <div className="section-heading"><div><h2>Import history</h2><p>Recent file uploads and their results.</p></div></div>
          <div className="card">
            <div className="connection-list">{importJobs.map((job) => (
              <div className="connection-row" key={job.id}>
                <div className="integration-mark" style={{ color: '#10B981', background: '#10B98114' }}><FileSpreadsheet size={16} /></div>
                <div className="connection-copy"><strong>{job.filename}</strong><span>{job.source_type} · {job.total_rows} rows</span></div>
                <span className={`badge ${job.status === 'completed' ? 'badge-emerald' : job.status === 'failed' ? 'badge-rose' : 'badge-amber'}`}>{job.status}</span>
                <div className="connection-sync"><span>{job.imported_rows} imported · {job.duplicate_rows} dupes · {job.invalid_rows} invalid</span></div>
              </div>
            ))}</div>
          </div>
        </>
      )}

      {pendingProvider && <ConnectModal provider={pendingProvider} onClose={() => setPendingProvider(null)} onConnected={() => { setPendingProvider(null); void load(); void onAgentRefresh(); }} />}
      {pendingUploadProvider && <ExcelImportModal provider={pendingUploadProvider} onClose={() => setPendingUploadProvider(null)} onCompleted={() => { void load(); void onAgentRefresh(); }} />}
      {configuringSheets && <GoogleSheetsConfigModal onClose={() => setConfiguringSheets(false)} onSaved={() => { setConfiguringSheets(false); void load(); void onAgentRefresh(); }} />}
      {selectedLeadId && <LeadDetailModal leadId={selectedLeadId} onClose={() => setSelectedLeadId(null)} onUpdated={() => { void load(); void onAgentRefresh(); }} />}
    </div>
  );
}

function KnowledgeView({ onNavigate, onAgentRefresh, agentStatus }: { onNavigate: (view: View) => void; onAgentRefresh: () => void; agentStatus: AgentStatus | null }) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [activeTab, setActiveTab] = useState<'Documents' | 'Website' | 'FAQs' | 'Products'>('Documents');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [websiteUrl, setWebsiteUrl] = useState('');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const readyCount = documents.filter((doc) => doc.status === 'processed').length;
  const crmReady = (agentStatus?.crm_connected ?? 0) > 0;
  const aiReady = readyCount > 0 && crmReady;

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    const result = await knowledgeApi.list();
    if (result.ok) setDocuments(result.data); else setError(result.error);
    setLoading(false);
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function addDocument(file: File) {
    if (file.size > 10 * 1024 * 1024) { setError('Knowledge files must be 10 MB or smaller.'); return; }
    setSaving(true); setError(null);
    const result = await knowledgeApi.upload(file);
    setSaving(false);
    if (!result.ok) { setError(result.error); return; }
    setDocuments((items) => [result.data, ...items.filter((item) => item.id !== result.data.id)]);
    void onAgentRefresh();
  }

  async function addTextSource() {
    setSaving(true); setError(null);
    const sourceType = activeTab === 'FAQs' ? 'faq' : 'product';
    const result = await knowledgeApi.addText({ source_type: sourceType, title: title.trim(), content: content.trim() });
    setSaving(false);
    if (!result.ok) { setError(result.error); return; }
    setDocuments((items) => [result.data, ...items]); setTitle(''); setContent(''); void onAgentRefresh();
  }

  async function addWebsite() {
    setSaving(true); setError(null);
    const result = await knowledgeApi.addWebsite(websiteUrl.trim());
    setSaving(false);
    if (!result.ok) { setError(result.error); return; }
    setDocuments((items) => [result.data, ...items]); setWebsiteUrl(''); void onAgentRefresh();
  }

  async function removeDocument(id: string) {
    setSaving(true); setError(null);
    const result = await knowledgeApi.remove(id);
    setSaving(false);
    if (!result.ok) { setError(result.error); return; }
    setDocuments((items) => items.filter((item) => item.id !== id)); void onAgentRefresh();
  }

  function addAction() {
    if (activeTab === 'Documents') fileInputRef.current?.click();
    else document.querySelector<HTMLElement>('.knowledge-form input, .knowledge-form textarea')?.focus();
  }

  return (
    <div className="page-content">
      <div className="page-heading"><div><p className="eyebrow">Agent context</p><h1>Add your knowledge</h1><p className="page-subtitle">Give your AI the language, offers, and answers that make your business yours.</p></div><button className="button button-primary" onClick={addAction} disabled={saving}><CloudUpload size={16} />Add knowledge</button></div>
      <div className="knowledge-layout">
        <div className="card knowledge-library">
          <div className="tabs">{(['Documents', 'Website', 'FAQs', 'Products'] as const).map((tab) => <button key={tab} className={`tab ${activeTab === tab ? 'active' : ''}`} onClick={() => setActiveTab(tab)}>{tab}</button>)}</div>
          {activeTab === 'Documents' && <div className="upload-zone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); const file = event.dataTransfer.files[0]; if (file) void addDocument(file); }} onClick={() => fileInputRef.current?.click()}>
            <input ref={fileInputRef} type="file" accept=".pdf,.docx,.txt" hidden onChange={(event) => { const file = event.target.files?.[0]; if (file) void addDocument(file); event.currentTarget.value = ''; }} />
            <div className="upload-icon"><CloudUpload size={21} /></div><h3>{saving ? 'Processing your source…' : 'Drop files here or browse'}</h3><p>PDF, DOCX, or TXT up to 10 MB</p><button className="text-button" type="button">Browse files <ArrowUpRight size={15} /></button>
          </div>}
          {activeTab === 'Website' && <div className="knowledge-form"><label className="field-label">Website URL</label><input className="field-input" type="url" value={websiteUrl} onChange={(event) => setWebsiteUrl(event.target.value)} placeholder="https://example.com/about" /><p className="field-hint">Only public HTTP or HTTPS pages can be fetched.</p><button className="button button-primary" onClick={addWebsite} disabled={saving || !websiteUrl.trim()}>{saving ? 'Fetching…' : 'Add website'} <ArrowRight size={15} /></button></div>}
          {(activeTab === 'FAQs' || activeTab === 'Products') && <div className="knowledge-form"><label className="field-label">Title</label><input className="field-input" value={title} onChange={(event) => setTitle(event.target.value)} placeholder={activeTab === 'FAQs' ? 'Returns and exchanges' : 'Product name'} /><label className="field-label" style={{ marginTop: 12 }}>Content</label><textarea className="field-input" rows={5} value={content} onChange={(event) => setContent(event.target.value)} placeholder={activeTab === 'FAQs' ? 'Add the answer your AI should use…' : 'Describe features, pricing, and requirements…'} /><button className="button button-primary" style={{ marginTop: 12 }} onClick={addTextSource} disabled={saving || !title.trim() || !content.trim()}>{saving ? 'Saving…' : `Add ${activeTab === 'FAQs' ? 'FAQ' : 'product details'}`} <ArrowRight size={15} /></button></div>}
          {error && <div className="modal-error" style={{ margin: 14 }}>{error}</div>}
          {loading ? <div className="inline-loading"><Loader2 size={18} className="spin" />Loading knowledge…</div> : documents.length === 0 ? <EmptyState icon={FileText} title="Your knowledge base is empty" description="Add a document, FAQ, website, or product detail to prepare your AI." /> : <div className="knowledge-documents">{documents.map((doc) => <div className="document-row" key={doc.id}><div className="document-icon"><FileText size={17} /></div><div className="document-copy"><strong>{doc.title}</strong><span>{doc.source_type} · {doc.chunk_count} chunks</span>{doc.error && <span className="connection-error-text">{doc.error}</span>}</div><span className={`badge ${doc.status === 'processed' ? 'badge-emerald' : doc.status === 'failed' ? 'badge-rose' : 'badge-amber'}`}>{doc.status}</span><button className="icon-button danger" aria-label={`Delete ${doc.title}`} onClick={() => void removeDocument(doc.id)} disabled={saving}><Trash2 size={15} /></button></div>)}</div>}
        </div>
        <div className="ai-ready-panel"><div className={`ai-check ${aiReady ? 'ai-check-ready' : ''}`}><Bot size={30} /></div><p className="eyebrow">Agent readiness</p><h2>{aiReady ? 'AI Ready' : 'AI is waiting for context'}</h2><p>{aiReady ? 'Your connected lead source and processed knowledge are available to the agent.' : 'Connect a CRM and add processed knowledge to prepare the built-in voice agent.'}</p><div className="mini-checks"><span className={readyCount > 0 ? 'complete' : ''}><i />Knowledge source</span><span className={crmReady ? 'complete' : ''}><i />CRM connection</span><span><i />Calling policy</span></div>{!crmReady && <button className="button button-primary" onClick={() => onNavigate('CRM')}>Connect CRM <ArrowUpRight size={15} /></button>}</div>
      </div>
    </div>
  );
}

function EmptyModule({ view, icon: Icon, title, description }: { view: string; icon: typeof Phone; title: string; description: string }) {
  return <div className="page-content"><div className="page-heading"><div><p className="eyebrow">Workspace module</p><h1>{view}</h1><p className="page-subtitle">{description}</p></div><button className="quiet-button"><CircleHelp size={15} />Learn more</button></div><div className="module-empty card"><div className="module-empty-art"><Icon size={26} /></div><h2>{title}</h2><p>This view will populate from your connected workspace data. Nothing is fabricated while your workspace is empty.</p></div></div>;
}

export default function Home() {
  const [activeView, setActiveView] = useState<View>('Dashboard');
  const [mobileOpen, setMobileOpen] = useState(false);
  const [oauthProvider, setOauthProvider] = useState<string | null>(null);
  const [integrationNotice, setIntegrationNotice] = useState<string | null>(null);
  const { status: agentStatus, refresh: refreshAgent } = useAgentStatus();
  const { user, tenant, loading, signOut } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) {
      router.push('/login');
    }
  }, [loading, user, router]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const requestedView = params.get('view');
    if (navItems.some((item) => item.label === requestedView)) setActiveView(requestedView as View);
    const connected = params.get('connected');
    if (connected) setOauthProvider(connected);
    if (params.get('calendar_connected')) setIntegrationNotice('Google Calendar connected for this workspace.');
    if (params.get('calendar_error')) setIntegrationNotice(`Google Calendar connection failed: ${params.get('calendar_error')?.replaceAll('_', ' ')}.`);
    if (requestedView || connected) window.history.replaceState({}, '', window.location.pathname);
  }, []);

  const tenantName = tenant?.tenantName ?? 'My Workspace';
  const tenantInitials = tenantName.split(' ').map((w) => w[0]).slice(0, 2).join('').toUpperCase() || 'W';

  const view = useMemo(() => {
    if (activeView === 'Dashboard') return <Dashboard onNavigate={setActiveView} agentStatus={agentStatus} user={user} tenantName={tenantName} />;
    if (activeView === 'CRM') return <CRMView onNavigate={setActiveView} onAgentRefresh={refreshAgent} oauthProvider={oauthProvider} />;
    if (activeView === 'Knowledge') return <KnowledgeView onNavigate={setActiveView} onAgentRefresh={refreshAgent} agentStatus={agentStatus} />;
    if (activeView === 'Calls') return <CallsModule />;
    if (activeView === 'Meetings') return <MeetingsModule onNavigate={setActiveView} />;
    if (activeView === 'Analytics') return <AnalyticsModule />;
    if (activeView === 'Integrations') return <IntegrationsModule onNavigate={setActiveView} notice={integrationNotice} />;
    if (activeView === 'Settings') return <SettingsModule />;
    return null;
  }, [activeView, agentStatus, refreshAgent, user, tenantName, oauthProvider, integrationNotice]);

  if (loading || !user) {
    return (
      <div className="auth-gate">
        <div className="auth-gate-inner">
          <div className="auth-brand-mark"><Bot size={22} /></div>
          <p>Loading your workspace…</p>
        </div>
      </div>
    );
  }

  return (
    <main className="app-shell">
      <Sidebar activeView={activeView} onNavigate={setActiveView} isOpen={mobileOpen} onClose={() => setMobileOpen(false)} agentStatus={agentStatus} tenantName={tenantName} initials={tenantInitials} />
      <div className="main-shell"><Topbar onMenu={() => setMobileOpen(true)} user={user} tenantRole={tenant?.role ?? 'MEMBER'} activeView={activeView} onSignOut={() => { void signOut(); router.push('/login'); }} />{view}</div>
      {mobileOpen && <button className="mobile-backdrop show" onClick={() => setMobileOpen(false)} aria-label="Close menu" />}
    </main>
  );
}
