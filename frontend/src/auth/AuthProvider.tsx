import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, authApi, tokenStore } from "../api/client";
import type { AuthResponse, User } from "../types/api";

/** Roles available through public registration; admins are provisioned server-side. */
export type SignupRole = "learner" | "teacher";

interface AuthContextValue {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<User>;
  register: (email: string, username: string, password: string, role?: SignupRole) => Promise<User>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<User | null>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function saveAuth(auth: AuthResponse): User {
  tokenStore.setTokens(auth);
  return auth.user;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refreshUser = useCallback(async () => {
    if (!tokenStore.getAccessToken() && !tokenStore.getRefreshToken()) {
      setUser(null);
      return null;
    }
    try {
      const me = await authApi.me();
      setUser(me);
      return me;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) tokenStore.clear();
      setUser(null);
      return null;
    }
  }, []);

  useEffect(() => {
    void refreshUser().finally(() => setIsLoading(false));
  }, [refreshUser]);

  const login = useCallback(async (email: string, password: string) => {
    const auth = await authApi.login({ email, password });
    const nextUser = saveAuth(auth);
    setUser(nextUser);
    return nextUser;
  }, []);

  const register = useCallback(async (email: string, username: string, password: string, role: SignupRole = "learner") => {
    const auth = await authApi.register({ email, username, password, role });
    const nextUser = saveAuth(auth);
    setUser(nextUser);
    return nextUser;
  }, []);

  const logout = useCallback(async () => {
    const refresh = tokenStore.getRefreshToken();
    try {
      if (refresh) await authApi.logout(refresh);
    } finally {
      tokenStore.clear();
      setUser(null);
    }
  }, []);

  const value = useMemo<AuthContextValue>(() => ({
    user,
    isLoading,
    isAuthenticated: Boolean(user),
    login,
    register,
    logout,
    refreshUser,
  }), [user, isLoading, login, register, logout, refreshUser]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}