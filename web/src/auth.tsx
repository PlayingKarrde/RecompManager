import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, getToken, setToken, setUnauthorizedHandler, type Schemas } from "./api/client";

type Me = Schemas["Me"];

interface AuthState {
  me: Me | null;
  loading: boolean;
  login: (token: string) => Promise<string | null>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const logout = useCallback(() => {
    setToken(null);
    setMe(null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(logout);
    if (!getToken()) {
      setLoading(false);
      return;
    }
    api.GET("/api/v1/auth/me").then(({ data }) => {
      setMe(data ?? null);
      setLoading(false);
    });
  }, [logout]);

  const login = useCallback(async (token: string) => {
    setToken(token.trim());
    const { data, response } = await api.GET("/api/v1/auth/me");
    if (!data) {
      setToken(null);
      return response.status === 401 ? "That token isn't valid." : "Couldn't reach the server.";
    }
    if (data.scope !== "admin") {
      setToken(null);
      return "The web UI needs an admin token. Read tokens are for clients.";
    }
    setMe(data);
    return null;
  }, []);

  return (
    <AuthContext.Provider value={{ me, loading, login, logout }}>{children}</AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
