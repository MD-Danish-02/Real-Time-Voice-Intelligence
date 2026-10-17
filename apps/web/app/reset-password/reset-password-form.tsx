'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { AlertCircle, ArrowLeft, ArrowRight, Bot, CheckCircle2, Loader2 } from 'lucide-react';
import { authApi } from '@/lib/api';

export function ResetPasswordForm() {
  const [token, setToken] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [complete, setComplete] = useState(false);

  useEffect(() => {
    setToken(new URLSearchParams(window.location.hash.slice(1)).get('token') ?? '');
  }, []);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError('Use at least 8 characters for your new password.');
      return;
    }
    if (password !== confirmPassword) {
      setError('The passwords do not match.');
      return;
    }
    setLoading(true);
    const result = await authApi.confirmPasswordReset(token, password);
    setLoading(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setComplete(true);
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-brand-mark"><Bot size={22} /></div>
          <span>vox<span className="brand-accent">ai</span></span>
        </div>
        {complete ? <>
          <div className="auth-success-icon"><CheckCircle2 size={22} /></div>
          <h1>Password updated</h1>
          <p>Your password has been reset. Sign in with your new password.</p>
          <Link className="button button-primary auth-submit auth-link-button" href="/login">Back to sign in <ArrowRight size={16} /></Link>
        </> : !token ? <>
          <h1>Reset link is missing</h1>
          <p>Request a new password reset link to continue.</p>
          <Link className="button button-primary auth-submit auth-link-button" href="/forgot-password">Request reset link <ArrowRight size={16} /></Link>
        </> : <>
          <h1>Choose a new password</h1>
          <p>Your password must be at least 8 characters.</p>
          <form onSubmit={handleSubmit} className="auth-form">
            <label className="field-label" htmlFor="new-password">New password</label>
            <input id="new-password" className="field-input" type="password" autoComplete="new-password" required minLength={8} value={password} onChange={(event) => setPassword(event.target.value)} autoFocus />
            <label className="field-label" htmlFor="confirm-password" style={{ marginTop: 14 }}>Confirm new password</label>
            <input id="confirm-password" className="field-input" type="password" autoComplete="new-password" required minLength={8} value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} />
            {error && <div className="modal-error"><AlertCircle size={15} /> {error}</div>}
            <button className="button button-primary auth-submit" type="submit" disabled={loading}>
              {loading ? <><Loader2 size={16} className="spin" />Updating…</> : <>Reset password <ArrowRight size={16} /></>}
            </button>
          </form>
        </>}
        {!complete && <p className="auth-switch"><Link href="/login"><ArrowLeft size={14} /> Back to sign in</Link></p>}
      </div>
    </div>
  );
}
