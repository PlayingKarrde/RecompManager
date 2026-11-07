import { useEffect, useState, type FormEvent } from "react";
import { api, errorMessage, type Schemas } from "../api/client";
import { useAuth } from "../auth";
import { Account, Sessions } from "./Account";

export function Settings() {
  return (
    <>
      <Account />
      <Sessions />
      <GeneralSettings />
      <RomLibrary />
      <Devices />
      <Tokens />
      <ServerInfo />
    </>
  );
}

function ServerInfo() {
  const [info, setInfo] = useState<Schemas["SystemInfo"] | null>(null);
  useEffect(() => {
    api.GET("/api/v1/system/info").then(({ data }) => setInfo(data ?? null));
  }, []);
  if (!info) return null;
  return (
    <section>
      <h2>Server</h2>
      <dl className="facts">
        <dt>Version</dt>
        <dd>{info.version}</dd>
        <dt>Server-side builds</dt>
        <dd>{info.builds_disabled_reason ? `Off: ${info.builds_disabled_reason}` : "Available"}</dd>
        <dt>Import folder</dt>
        <dd>{info.import_dir_available ? "Mounted" : "Not mounted"}</dd>
        <dt>ROM library</dt>
        <dd>{info.roms_dir_available ? "Mounted at /roms" : "Not mounted"}</dd>
      </dl>
    </section>
  );
}

function GeneralSettings() {
  const [s, setS] = useState<Schemas["SettingsOut"] | null>(null);
  const [githubToken, setGithubToken] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api.GET("/api/v1/system/settings").then(({ data }) => setS(data ?? null));
  }, []);
  if (!s) return null;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!s) return;
    const body: Schemas["SettingsPatch"] = {
      update_check_interval_hours: s.update_check_interval_hours,
      keep_versions: s.keep_versions,
      auto_update_default: s.auto_update_default,
    };
    if (githubToken) body.github_token = githubToken;
    const { data, error } = await api.PATCH("/api/v1/system/settings", { body });
    if (data) {
      setS(data);
      setGithubToken("");
      setMsg("Saved");
    } else setMsg(errorMessage(error));
  }

  async function clearGithubToken() {
    const { data } = await api.PATCH("/api/v1/system/settings", { body: { github_token: "" } });
    if (data) setS(data);
  }

  return (
    <section>
      <h2>Settings</h2>
      <form className="grid-form" onSubmit={save}>
        <label htmlFor="interval">Check for updates every (hours)</label>
        <input
          id="interval"
          type="number"
          min={1}
          value={s.update_check_interval_hours}
          onChange={(e) => setS({ ...s, update_check_interval_hours: Number(e.target.value) })}
        />
        <label htmlFor="keep">Versions to keep per game</label>
        <input
          id="keep"
          type="number"
          min={1}
          value={s.keep_versions}
          onChange={(e) => setS({ ...s, keep_versions: Number(e.target.value) })}
        />
        <label htmlFor="auto">Auto-update new games</label>
        <input
          id="auto"
          type="checkbox"
          checked={s.auto_update_default}
          onChange={(e) => setS({ ...s, auto_update_default: e.target.checked })}
        />
        <label htmlFor="gh">GitHub token</label>
        <div className="row">
          <input
            id="gh"
            type="password"
            autoComplete="off"
            value={githubToken}
            placeholder={s.github_token_set ? "Set (enter a new one to replace)" : "Optional"}
            onChange={(e) => setGithubToken(e.target.value)}
          />
          {s.github_token_set && (
            <button type="button" className="secondary" onClick={clearGithubToken}>
              Clear
            </button>
          )}
        </div>
        <span />
        <div className="row">
          <button type="submit">Save</button>
          {msg && <span className="muted small">{msg}</span>}
        </div>
      </form>
      <p className="muted small">
        Without a GitHub token, GitHub allows 60 API requests an hour. A token with no scopes is
        enough for public repos.
      </p>
    </section>
  );
}

function Tokens() {
  const { me } = useAuth();
  const [tokens, setTokens] = useState<Schemas["TokenOut"][]>([]);
  const [name, setName] = useState("");
  const [scope, setScope] = useState<Schemas["TokenScope"]>("read");
  const [created, setCreated] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    const { data } = await api.GET("/api/v1/tokens");
    if (data) setTokens(data);
  }
  useEffect(() => {
    load();
  }, []);

  async function create(e: FormEvent) {
    e.preventDefault();
    const { data, error } = await api.POST("/api/v1/tokens", { body: { name, scope } });
    if (data) {
      setCreated(data.token);
      setName("");
      setErr(null);
      load();
    } else setErr(errorMessage(error));
  }

  async function revoke(id: number) {
    if (!confirm("Revoke this token? Clients using it will stop working.")) return;
    const { error } = await api.DELETE("/api/v1/tokens/{token_id}", {
      params: { path: { token_id: id } },
    });
    setErr(error ? errorMessage(error) : null);
    load();
  }

  return (
    <section>
      <h2>API tokens</h2>
      <p className="muted small">
        Use <b>read</b> tokens for game clients and <b>admin</b> tokens for this web UI or
        automation.
      </p>
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Scope</th>
            <th>Token</th>
            <th>Last used</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {tokens.map((t) => (
            <tr key={t.id} className={t.revoked_at ? "revoked" : undefined}>
              <td>
                {t.name}
                {t.id === me?.token_id && <span className="muted small"> (this session)</span>}
              </td>
              <td>{t.scope}</td>
              <td>
                <code>{t.prefix}…</code>
              </td>
              <td>{t.last_used_at ? new Date(t.last_used_at).toLocaleString() : "Never"}</td>
              <td>
                {t.revoked_at ? (
                  "Revoked"
                ) : t.id !== me?.token_id ? (
                  <button className="link danger" onClick={() => revoke(t.id)}>
                    Revoke
                  </button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="row" onSubmit={create}>
        <input
          aria-label="Token name"
          placeholder="Name, e.g. Steam Deck"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select
          aria-label="Scope"
          value={scope}
          onChange={(e) => setScope(e.target.value as Schemas["TokenScope"])}
        >
          <option value="read">read</option>
          <option value="admin">admin</option>
        </select>
        <button type="submit" disabled={!name.trim()}>
          Create token
        </button>
      </form>
      {err && <p className="error">{err}</p>}
      {created && (
        <div className="card notice">
          <p>Copy this token now. It won't be shown again.</p>
          <code className="token">{created}</code>
          <div className="row">
            <button onClick={() => navigator.clipboard?.writeText(created)}>Copy</button>
            <button className="secondary" onClick={() => setCreated(null)}>
              Done
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function RomLibrary() {
  const [s, setS] = useState<Schemas["SettingsOut"] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    api.GET("/api/v1/system/settings").then(({ data }) => setS(data ?? null));
  }, []);
  if (!s) return null;

  async function patch(body: Schemas["SettingsPatch"]) {
    const { data, error } = await api.PATCH("/api/v1/system/settings", { body });
    if (data) setS(data);
    setMsg(error ? errorMessage(error) : "Saved");
  }
  async function scan() {
    const { error } = await api.POST("/api/v1/system/scan");
    setMsg(error ? errorMessage(error) : "Scan started; see Jobs");
  }

  return (
    <section>
      <h2>ROM library</h2>
      <p className="muted small">
        The server looks through /roms (and /import) for each game's inputs: ROMs by checksum, also inside zips;
        PS4 and Switch dump folders by title ID; files a recipe names. Nothing there is ever changed.
      </p>
      <div className="grid-form">
        <label htmlFor="scan-every">Scan every (hours, 0 = never)</label>
        <input
          id="scan-every"
          type="number"
          min={0}
          defaultValue={s.rom_scan_interval_hours}
          onBlur={(e) => patch({ rom_scan_interval_hours: Number(e.target.value) })}
        />
        <label htmlFor="auto-accept">Add confident matches automatically</label>
        <input
          id="auto-accept"
          type="checkbox"
          checked={s.auto_accept_scan_matches}
          onChange={(e) => patch({ auto_accept_scan_matches: e.target.checked })}
        />
        <span />
        <div className="row">
          <button onClick={scan}>Scan now</button>
          {msg && <span className="muted small">{msg}</span>}
        </div>
      </div>
    </section>
  );
}

function Devices() {
  const [clients, setClients] = useState<Schemas["ClientOut"][]>([]);
  const load = () => api.GET("/api/v1/clients").then(({ data }) => data && setClients(data));
  useEffect(() => {
    load();
  }, []);
  if (clients.length === 0) return null;
  return (
    <section>
      <h2>Devices</h2>
      <table>
        <thead>
          <tr>
            <th>Device</th>
            <th>Platform</th>
            <th>Installed</th>
            <th>Last seen</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {clients.map((c) => (
            <tr key={c.id}>
              <td>{c.name}</td>
              <td>
                <code>{c.platform}</code>
              </td>
              <td className="small">
                {Object.entries(c.installed).map(([r, v]) => (
                  <div key={r}>
                    {r} {v}
                    {c.out_of_date.includes(r) && <span className="warn-text"> (update available)</span>}
                  </div>
                ))}
              </td>
              <td>{new Date(c.last_seen_at).toLocaleString()}</td>
              <td>
                <button
                  className="link danger"
                  onClick={async () => {
                    await api.DELETE("/api/v1/clients/{client_id}", { params: { path: { client_id: c.id } } });
                    load();
                  }}
                >
                  Forget
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
