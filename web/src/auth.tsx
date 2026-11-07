import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import {
  api,
  errorCode,
  errorMessage,
  getToken,
  setToken,
  setUnauthorizedHandler,
  type Schemas,
} from "./api/client";

type Me = Schemas["Me"];

export interface LoginError {
  message: string;
  code?: string;
}

interface AuthState {
  me: Me | null;
  loading: boolean;
  loginWithPassword: (username: string, password: string) => Promise<LoginError | null>;
  loginWithToken: (token: string) => Promise<LoginError | null>;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const forget = useCallback(() => {
    setToken(null);
    setMe(null);
  }, []);

  const refresh = useCallback(async () => {
    const { data } = await api.GET("/api/v1/auth/me");
    setMe(data ?? null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(forget);
    if (!getToken()) {
      setLoading(false);
      return;
    }
    refresh().finally(() => setLoading(false));
  }, [forget, refresh]);

  /** Check the stored token is an admin one and load the caller. */
  const adopt = useCallback(async (): Promise<LoginError | null> => {
    const { data, response } = await api.GET("/api/v1/auth/me");
    if (!data) {
      setToken(null);
      return {
        message: response.status === 401 ? "That token isn't valid." : "Couldn't reach the server.",
      };
    }
    if (data.scope !== "admin") {
      setToken(null);
      return { message: "The web UI needs an admin token. Read tokens are for game clients." };
    }
    setMe(data);
    return null;
  }, []);

  const loginWithPassword = useCallback(
    async (username: string, password: string) => {
      const { data, error } = await api.POST("/api/v1/auth/login", {
        body: { username, password },
      });
      if (!data) return { message: errorMessage(error), code: errorCode(error) };
      setToken(data.token);
      return adopt();
    },
    [adopt],
  );

  const loginWithToken = useCallback(
    async (token: string) => {
      setToken(token.trim());
      return adopt();
    },
    [adopt],
  );

  const logout = useCallback(async () => {
    // Ends a password session on the server; an API token is only forgotten here.
    await api.POST("/api/v1/auth/logout").catch(() => undefined);
    forget();
  }, [forget]);

  return (
    <AuthContext.Provider
      value={{ me, loading, loginWithPassword, loginWithToken, refresh, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
