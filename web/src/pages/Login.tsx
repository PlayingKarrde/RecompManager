import { useState, type FormEvent } from "react";
import { useAuth } from "../auth";

export function Login() {
  const { login } = useAuth();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(await login(token));
    setBusy(false);
  }

  return (
    <div className="center">
      <form className="card login" onSubmit={submit}>
        <h1>Recomp Manager</h1>
        <label htmlFor="token">Admin token</label>
        <input
          id="token"
          type="password"
          autoComplete="current-password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="rm_…"
          autoFocus
        />
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy || !token.trim()}>
          {busy ? "Checking…" : "Log in"}
        </button>
        <p className="muted small">
          On first start the server writes an admin token to <code>admin-token.txt</code> in its
          config folder (on Unraid: <code>/mnt/user/appdata/recompmanager</code>).
        </p>
      </form>
    </div>
  );
}
