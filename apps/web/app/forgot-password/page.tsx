'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { AlertCircle, ArrowLeft, ArrowRight, Bot, Loader2, MailCheck } from 'lucide-react';
import { authApi } from '@/lib/api';

export default function ForgotPasswordPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [developmentLog, setDevelopmentLog] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setLoading(true);
    const result = await authApi.requestPasswordReset(email.trim());
    setLoading(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setDevelopmentLog(result.data.delivery_mode === 'development_log');
    setSent(true);
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-brand-mark"><Bot size={22} /></div>
          <span>vox<span className="brand-accent">ai</span></span>
        </div>
        {!sent ? <>
          <h1>Reset your password</h1>
          <p>Enter the email address linked to your VoxAI workspace.</p>
          <form onSubmit={handleSubmit} className="auth-form">
            <label className="field-label" htmlFor="reset-email">Email</label>
            <input id="reset-email" className="field-input" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@company.com" autoFocus />
            {error && <div className="modal-error"><AlertCircle size={15} /> {error}</div>}
            <button className="button button-primary auth-submit" type="submit" disabled={loading}>
              {loading ? <><Loader2 size={16} className="spin" />Sending…</> : <>Send reset link <ArrowRight size={16} /></>}
            </button>
          </form>
        </> : <>
          <div className="auth-success-icon"><MailCheck size={22} /></div>
          <h1>Check your inbox</h1>
          <p>If an account exists for that email, password reset instructions are on the way.</p>
          {developmentLog && <p className="auth-dev-note">Local development: the reset link is printed in the backend logs.</p>}
          <button className="button button-primary auth-submit" type="button" onClick={() => router.push('/login')}>Back to sign in</button>
        </>}
        {!sent && <p className="auth-switch"><Link href="/login"><ArrowLeft size={14} /> Back to sign in</Link></p>}
        {sent && <p className="auth-switch">Didn&apos;t receive an email? <button type="button" onClick={() => setSent(false)}>Try again</button></p>}
      </div>
    </div>
  );
}
