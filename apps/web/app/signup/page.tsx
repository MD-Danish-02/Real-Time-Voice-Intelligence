'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Bot, ArrowRight, Loader2, AlertCircle } from 'lucide-react';
import { useAuth } from '@/lib/auth-context';

export default function SignupPage() {
  const router = useRouter();
  const { signUp } = useAuth();
  const [fullName, setFullName] = useState('');
  const [workspaceName, setWorkspaceName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password || !fullName.trim()) {
      setError('Fill in your name, email, and password.');
      return;
    }
    if (password.length < 8) {
      setError('Password must be at least 8 characters.');
      return;
    }
    setLoading(true);
    setError(null);

    const result = await signUp({
      email: email.trim(),
      password,
      full_name: fullName.trim(),
      company_name: workspaceName.trim() || `${fullName.trim()}'s Workspace`,
    });

    if (!result.ok) {
      setLoading(false);
      setError(result.error);
      return;
    }
    setLoading(false);
    router.push('/');
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-brand-mark"><Bot size={22} /></div>
          <span>vox<span className="brand-accent">ai</span></span>
        </div>
        <h1>Create your workspace</h1>
        <p>Connect your CRM, add knowledge, and let your AI handle the rest.</p>
        <form onSubmit={handleSubmit} className="auth-form">
          <label className="field-label">Full name</label>
          <input className="field-input" type="text" value={fullName} onChange={(e) => setFullName(e.target.value)} placeholder="Jane Smith" autoFocus />
          <label className="field-label" style={{ marginTop: 14 }}>Workspace name</label>
          <input className="field-input" type="text" value={workspaceName} onChange={(e) => setWorkspaceName(e.target.value)} placeholder="Acme Inc (optional)" />
          <label className="field-label" style={{ marginTop: 14 }}>Email</label>
          <input className="field-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" />
          <label className="field-label" style={{ marginTop: 14 }}>Password</label>
          <input className="field-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 8 characters" />
          {error && <div className="modal-error"><AlertCircle size={15} /> {error}</div>}
          <button className="button button-primary auth-submit" type="submit" disabled={loading}>
            {loading ? <><Loader2 size={16} className="spin" />Creating account…</> : <>Create account <ArrowRight size={16} /></>}
          </button>
        </form>
        <p className="auth-switch">Already have an account? <button onClick={() => router.push('/login')}>Sign in</button></p>
      </div>
    </div>
  );
}
