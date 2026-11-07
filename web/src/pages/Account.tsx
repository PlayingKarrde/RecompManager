import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";
import { api, errorMessage, type Schemas } from "../api/client";
import { useAuth } from "../auth";

const MIN_PASSWORD = 8;

/** Set or change the web login (username + password). */
export function Account() {
  const { me, refresh } = useAuth();
  const ref = useRef<HTMLElement>(null);
  const location = useLocation();
  const [username, setUsername] = useState(me?.username ?? "");
  const [current, setCurrent] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (location.hash === "#account") ref.current?.scrollIntoView({ behavior: "smooth" });
  }, [location.hash]);

  if (!me) return null;
  // Changing an existing password from a browser session needs the current one.
  const needsCurrent = me.password_set && me.kind === "session";
  const mismatch = confirm.length > 0 && password !== confirm;

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const body: Schemas["PasswordChange"] = { username: username.trim(), new_password: password };
    if (needsCurrent) body.current_password = current;
    const { error } = await api.PUT("/api/v1/auth/password", { body });
    setBusy(false);
    if (error) {
      setMsg({ text: errorMessage(error), error: true });
      return;
    }
    setCurrent("");
    setPassword("");
    setConfirm("");
    setMsg({
      text: me?.password_set
        ? "Password changed. Other browsers have been signed out."
        : "Done. You can now sign in with this username and password from any browser.",
      error: false,
    });
    refresh();
  }

  return (
    <section id="account" ref={ref}>
      <h2>Account</h2>
      <form className="grid-form" onSubmit={save}>
        <label htmlFor="acct-user">Username</label>
        <input
          id="acct-user"
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        {needsCurrent && (
          <>
            <label htmlFor="acct-current">Current password</label>
            <input
              id="acct-current"
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          </>
        )}
        <label htmlFor="acct-new">{me.password_set ? "New password" : "Password"}</label>
        <input
          id="acct-new"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <label htmlFor="acct-confirm">Confirm password</label>
        <input
          id="acct-confirm"
          type="password"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
        <span />
        <div className="row">
          <button
            type="submit"
            disabled={
              busy ||
              !username.trim() ||
              password.length < MIN_PASSWORD ||
              password !== confirm ||
              (needsCurrent && !current)
            }
          >
            {me.password_set ? "Change password" : "Set password"}
          </button>
          {mismatch && <span className="error small">Passwords don't match</span>}
          {!mismatch && password.length > 0 && password.length < MIN_PASSWORD && (
            <span className="muted small">At least {MIN_PASSWORD} characters</span>
          )}
          {msg && <span className={msg.error ? "error small" : "muted small"}>{msg.text}</span>}
        </div>
      </form>
    </section>
  );
}

/** Browsers signed in with the password, each revocable. */
export function Sessions() {
  const [sessions, setSessions] = useState<Schemas["SessionOut"][]>([]);
  const { logout } = useAuth();

  async function load() {
    const { data } = await api.GET("/api/v1/auth/sessions");
    if (data) setSessions(data);
  }
  useEffect(() => {
    load();
  }, []);

  async function revoke(s: Schemas["SessionOut"]) {
    if (s.current) return logout();
    await api.DELETE("/api/v1/auth/sessions/{session_id}", {
      params: { path: { session_id: s.id } },
    });
    load();
  }

  if (sessions.length === 0) return null;
  return (
    <section>
      <h2>Signed-in browsers</h2>
      <p className="muted small">
        A browser stays signed in until you sign it out or it goes 30 days without use.
</p>
      <table>
        <thead>
          <tr>
            <th>Browser</th>
            <th>Signed in</th>
            <th>Last used</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {sessions.map((s) => (
            <tr key={s.id}>
              <td>
                {s.name}
                {s.current && <span className="muted small"> (this browser)</span>}
              </td>
              <td>{new Date(s.created_at).toLocaleDateString()}</td>
              <td>{s.last_used_at ? new Date(s.last_used_at).toLocaleString() : "Never"}</td>
              <td>
                <button className="link danger" onClick={() => revoke(s)}>
                  Sign out
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
