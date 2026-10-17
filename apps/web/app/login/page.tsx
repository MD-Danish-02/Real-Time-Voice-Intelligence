'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { Bot, ArrowRight, Loader2, AlertCircle } from 'lucide-react';
import { useAuth } from '@/lib/auth-context';

export default function LoginPage() {
  const router = useRouter();
  const { signIn } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim() || !password) {
      setError('Enter your email and password.');
      return;
    }
    setLoading(true);
    setError(null);
    const result = await signIn(email.trim(), password);
    setLoading(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    router.push('/');
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-brand-mark"><Bot size={22} /></div>
          <span>vox<span className="brand-accent">ai</span></span>
        </div>
        <h1>Welcome back</h1>
        <p>Sign in to your AI voice automation workspace.</p>
        <form onSubmit={handleSubmit} className="auth-form">
          <label className="field-label">Email</label>
          <input className="field-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" autoFocus />
          <label className="field-label" style={{ marginTop: 14 }}>Password</label>
          <input className="field-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
          <div className="auth-forgot-row"><Link href="/forgot-password">Forgot password?</Link></div>
          {error && <div className="modal-error"><AlertCircle size={15} /> {error}</div>}
          <button className="button button-primary auth-submit" type="submit" disabled={loading}>
            {loading ? <><Loader2 size={16} className="spin" />Signing in…</> : <>Sign in <ArrowRight size={16} /></>}
          </button>
        </form>
        <p className="auth-switch">Don&apos;t have an account? <button onClick={() => router.push('/signup')}>Create one</button></p>
      </div>
    </div>
  );
}
