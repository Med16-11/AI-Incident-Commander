import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

interface AuthContextValue {
  token?: string;
  setToken: (value: string) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, updateToken] = useState<string | undefined>();
  const setToken = (value: string) => {
    const trimmed = value.trim();
    updateToken(trimmed || undefined);
  };
  const context = useMemo(() => ({ token, setToken }), [token]);
  return <AuthContext.Provider value={context}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used within AuthProvider");
  return value;
}
