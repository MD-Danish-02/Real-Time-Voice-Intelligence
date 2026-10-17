'use client';

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { authApi, setAccessToken } from './api';
import type { ApiResult, AuthUser } from './types';

type TenantInfo = { tenantId: string; tenantName: string; role: string };
type Session = { access_token: string };
type AuthState = {
  session: Session | null;
  user: AuthUser | null;
  tenant: TenantInfo | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<ApiResult<AuthUser>>;
  signUp: (payload: { email: string; password: string; full_name: string; company_name: string }) => Promise<ApiResult<AuthUser>>;
  signOut: () => Promise<void>;
  refreshTenant: () => Promise<void>;
};

const AuthContext = createContext<AuthState>({
  session: null, user: null, tenant: null, loading: true,
  signIn: async () => ({ ok: false, error: 'Authentication is not available.' }),
  signUp: async () => ({ ok: false, error: 'Authentication is not available.' }),
  signOut: async () => {}, refreshTenant: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [tenant, setTenant] = useState<TenantInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const clearIdentity = useCallback(() => {
    setAccessToken(null);
    setSession(null);
    setUser(null);
    setTenant(null);
  }, []);

  const loadIdentity = useCallback(async (): Promise<ApiResult<AuthUser>> => {
    const [me, currentTenant] = await Promise.all([authApi.me(), authApi.tenant()]);
    if (!me.ok) return me;
    if (!currentTenant.ok) return currentTenant;
    setUser(me.data);
    setTenant({ tenantId: currentTenant.data.id, tenantName: currentTenant.data.name, role: me.data.role });
    return { ok: true, data: me.data };
  }, []);

  useEffect(() => {
    let mounted = true;
    const onExpired = () => { if (mounted) clearIdentity(); };
    window.addEventListener('voxai:auth-expired', onExpired);
    void (async () => {
      const access = await authApi.restore();
      if (access && mounted) {
        setSession({ access_token: access });
        const result = await loadIdentity();
        if (!result.ok && mounted) clearIdentity();
      }
      if (mounted) setLoading(false);
    })();
    return () => {
      mounted = false;
      window.removeEventListener('voxai:auth-expired', onExpired);
    };
  }, [clearIdentity, loadIdentity]);

  async function signIn(email: string, password: string): Promise<ApiResult<AuthUser>> {
    const result = await authApi.login(email, password);
    if (!result.ok) return result;
    setSession({ access_token: result.data.access_token });
    const identity = await loadIdentity();
    if (!identity.ok) clearIdentity();
    return identity;
  }

  async function signUp(payload: { email: string; password: string; full_name: string; company_name: string }): Promise<ApiResult<AuthUser>> {
    const result = await authApi.signup(payload);
    if (!result.ok) return result;
    setSession({ access_token: result.data.access_token });
    const identity = await loadIdentity();
    if (!identity.ok) clearIdentity();
    return identity;
  }

  async function signOut(): Promise<void> {
    await authApi.logout();
    clearIdentity();
  }

  async function refreshTenant(): Promise<void> {
    if (!user) return;
    const currentTenant = await authApi.tenant();
    if (currentTenant.ok) {
      setTenant({ tenantId: currentTenant.data.id, tenantName: currentTenant.data.name, role: user.role });
    }
  }

  return (
    <AuthContext.Provider value={{ session, user, tenant, loading, signIn, signUp, signOut, refreshTenant }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() { return useContext(AuthContext); }
