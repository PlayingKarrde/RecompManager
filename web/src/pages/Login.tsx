import { useState, type FormEvent } from "react";
import { useAuth, type LoginError } from "../auth";

export function Login() {
  const { loginWithPassword, loginWithToken } = useAuth();
  const [mode, setMode] = useState<"password" | "token">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState<LoginError | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const err =
      mode === "password"
        ? await loginWithPassword(username.trim(), password)
        : await loginWithToken(token);
    setBusy(false);
    setError(err);
    if (err?.code === "password_not_set") setMode("token");
  }

  function switchMode() {
    setMode(mode === "password" ? "token" : "password");
    setError(null);
  }

  return (
    <div className="center">
      <form className="card login" onSubmit={submit}>
        <h1>Recomp Manager</h1>
        {mode === "password" ? (
          <>
            <label htmlFor="username">Username</label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
            />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </>
        ) : (
          <>
            <label htmlFor="token">Admin token</label>
            <input
              id="token"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="rm_…"
              autoFocus
            />
          </>
        )}
        {error && <p className="error">{error.message}</p>}
        <button
          type="submit"
          disabled={busy || (mode === "password" ? !username.trim() || !password : !token.trim())}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <button type="button" className="link" onClick={switchMode}>
          {mode === "password" ? "Sign in with a token instead" : "Sign in with a password"}
        </button>
        {mode === "token" && (
          <p className="muted small">
            First time? The server wrote an admin token to <code>admin-token.txt</code> in its config
            folder (on Unraid: <code>/mnt/user/appdata/recompmanager</code>). Sign in with it once,
            then set a username and password.
          </p>
        )}
      </form>
    </div>
  );
}
