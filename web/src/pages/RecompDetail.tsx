import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage, type Schemas } from "../api/client";
import { Badge, JobStatusBadge, Platforms, Progress } from "../components/bits";
import { ago, bytes, date, jsRegex } from "../lib/format";
import { useServerEvents, type JobEvent } from "../lib/events";
import { GameStatus } from "./Library";

type Game = Schemas["GameDetail"];
type Version = Schemas["VersionOut"];

export function GameDetail() {
  const { id = "" } = useParams();
  const [game, setGame] = useState<Game | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [jobs, setJobs] = useState<Schemas["JobSummary"][]>([]);
  const [missing, setMissing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [g, v, j] = await Promise.all([
      api.GET("/api/v1/games/{game_id}", { params: { path: { game_id: id } } }),
      api.GET("/api/v1/games/{game_id}/versions", { params: { path: { game_id: id } } }),
      api.GET("/api/v1/jobs", { params: { query: { game_id: id, limit: 5 } } }),
    ]);
    if (g.response.status === 404) setMissing(true);
    if (g.data) setGame(g.data);
    if (v.data) setVersions(v.data);
    if (j.data) setJobs(j.data);
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  useServerEvents((e) => {
    if (e.type === "job.updated" && e.data.game_id === id) {
      updateJob(e.data);
      if (["succeeded", "failed", "cancelled"].includes(e.data.status)) load();
    } else if (e.type === "game.updated" && e.data.id === id) load();
  });

  function updateJob(ev: JobEvent) {
    setJobs((prev) => {
      const i = prev.findIndex((j) => j.id === ev.id);
      if (i < 0) {
        load();
        return prev;
      }
      const next = [...prev];
      next[i] = { ...next[i], status: ev.status as never, progress: ev.progress, message: ev.message };
      return next;
    });
  }

  if (missing) return <section><p>No such game. <Link to="/library">Back to the library</Link></p></section>;
  if (!game) return <section><p className="muted">Loading…</p></section>;

  const busy = jobs.some((j) => j.status === "queued" || j.status === "running");

  async function act(fn: () => Promise<{ error?: unknown }>) {
    setMsg(null);
    const { error } = await fn();
    if (error) setMsg(errorMessage(error));
    load();
  }
  const path = { params: { path: { game_id: id } } };

  return (
    <>
      <section>
        <div className="title-row">
          <div>
            <h2>{game.name}</h2>
            {game.repo && (
              <a className="muted small" href={`https://github.com/${game.repo}`} target="_blank" rel="noreferrer">
                github.com/{game.repo}
              </a>
            )}
          </div>
          <GameStatus game={game} />
        </div>
        {game.recipe?.description && <p>{game.recipe.description}</p>}
        <dl className="facts">
          <dt>Current version</dt>
          <dd>{game.current_version ?? "None downloaded"}</dd>
          <dt>Latest release</dt>
          <dd>
            {game.latest_version ?? "None found"} <span className="muted small">({game.channel} channel)</span>
          </dd>
          <dt>Last checked</dt>
          <dd>
            {ago(game.last_checked_at)}
            {game.last_check_error && <span className="error"> · {game.last_check_error}</span>}
          </dd>
          <dt>Platforms</dt>
          <dd>
            <Platforms keys={game.platforms} />
          </dd>
        </dl>
        {game.recipe_error && <p className="error">{game.recipe_error}</p>}
        <div className="row actions">
          <button disabled={busy} onClick={() => act(() => api.POST("/api/v1/games/{game_id}/check", path))}>
            Check for updates
          </button>
          {game.status === "active" && game.latest_version && game.update_available && (
            <button
              disabled={busy}
              onClick={() => act(() => api.POST("/api/v1/games/{game_id}/update", { ...path, body: {} }))}
            >
              {game.current_version ? `Update to ${game.latest_version}` : `Download ${game.latest_version}`}
            </button>
          )}
          <label className="row small">
            <input
              type="checkbox"
              checked={game.auto_update}
              onChange={(e) =>
                act(() =>
                  api.PATCH("/api/v1/games/{game_id}", { ...path, body: { auto_update: e.target.checked } }),
                )
              }
            />
            Download new releases automatically
          </label>
          <span className="spacer" />
          <DeleteGame id={id} />
        </div>
        {msg && <p className="error">{msg}</p>}
      </section>

      <Jobs jobs={jobs} onCancel={(jid) => act(() => api.POST("/api/v1/jobs/{job_id}/cancel", { params: { path: { job_id: jid } } }))} />

      {game.status === "draft" && (
        <section className="draft">
          <h2>Draft: check the asset rules</h2>
          <p>
            The rules below were guessed from release file names. Each rule is a regular expression that picks
            one file per platform from every release. When they look right, confirm the recipe.
          </p>
          <button
            onClick={() => act(() => api.PATCH("/api/v1/games/{game_id}", { ...path, body: { status: "active" } }))}
          >
            Confirm recipe
          </button>
        </section>
      )}

      <AssetRules game={game} onSaved={load} />
      <Versions game={game} versions={versions} onChange={load} />
    </>
  );
}

function Jobs({ jobs, onCancel }: { jobs: Schemas["JobSummary"][]; onCancel: (id: number) => void }) {
  if (jobs.length === 0) return null;
  return (
    <section>
      <h2>Recent jobs</h2>
      <ul className="jobs">
        {jobs.map((j) => (
          <li key={j.id}>
            <span className="job-kind">{j.kind}</span>
            <JobStatusBadge status={j.status} />
            {j.status === "running" ? (
              <>
                <Progress value={j.progress} />
                <span className="muted small grow">{j.message}</span>
                <button className="link danger" onClick={() => onCancel(j.id)}>
                  Cancel
                </button>
              </>
            ) : (
              <span className={`small grow ${j.error ? "error" : "muted"}`}>
                {j.error ?? j.message ?? ""} {j.finished_at && `· ${ago(j.finished_at)}`}
              </span>
            )}
            <Link className="small" to={`/jobs?open=${j.id}`}>
              Log
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

function AssetRules({ game, onSaved }: { game: Game; onSaved: () => void }) {
  const [rows, setRows] = useState<[string, string][]>([]);
  const [open, setOpen] = useState(game.status === "draft");
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setRows(Object.entries(game.recipe?.assets ?? {}).map(([k, v]) => [k, v.match]));
  }, [game.recipe]);

  if (!game.recipe) return null;
  const allAssets = [...Object.values(game.latest_assets), ...game.unmatched_assets].filter(
    (v, i, a) => a.indexOf(v) === i,
  );

  function preview(pattern: string): string {
    const rx = jsRegex(pattern);
    if (!rx) return "Invalid pattern";
    return allAssets.find((a) => rx.test(a)) ?? "No match in the latest release";
  }

  async function save() {
    setErr(null);
    setSaved(false);
    const assets = Object.fromEntries(rows.filter(([k]) => k.trim()).map(([k, m]) => [k.trim(), { match: m }]));
    const { error } = await api.PATCH("/api/v1/games/{game_id}", {
      params: { path: { game_id: game.id } },
      body: { assets },
    });
    if (error) setErr(errorMessage(error));
    else {
      setSaved(true);
      onSaved();
    }
  }

  return (
    <section>
      <div className="title-row">
        <h2>Asset rules</h2>
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Edit"}
        </button>
      </div>
      {!open ? (
        <p className="muted small">
          {Object.keys(game.latest_assets).length} of {Object.keys(game.recipe.assets ?? {}).length} platforms matched in the
          latest release.
          {game.unmatched_assets.length > 0 && ` Unmatched files: ${game.unmatched_assets.join(", ")}.`}
        </p>
      ) : (
        <>
          <table className="rules">
            <thead>
              <tr>
                <th>Platform</th>
                <th>Pattern</th>
                <th>Matches in latest release</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map(([k, m], i) => (
                <tr key={i}>
                  <td>
                    <input
                      aria-label="Platform key"
                      list="platform-keys"
                      value={k}
                      onChange={(e) => setRows(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
                    />
                  </td>
                  <td>
                    <input
                      aria-label="Pattern"
                      className="mono"
                      value={m}
                      onChange={(e) => setRows(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}
                    />
                  </td>
                  <td className="small">{preview(m)}</td>
                  <td>
                    <button className="link danger" onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <datalist id="platform-keys">
            {["windows-x64", "windows-arm64", "linux-x64", "linux-arm64", "linux-x64-steamdeck", "macos-x64",
              "macos-arm64", "macos-universal", "android-arm64"].map((k) => (
              <option key={k} value={k} />
            ))}
          </datalist>
          {game.unmatched_assets.length > 0 && (
            <p className="muted small">Not matched by any rule: {game.unmatched_assets.join(", ")}</p>
          )}
          <div className="row">
            <button className="secondary" onClick={() => setRows([...rows, ["", ""]])}>
              Add rule
            </button>
            <button onClick={save}>Save rules</button>
            {saved && <span className="muted small">Saved</span>}
            {err && <span className="error small">{err}</span>}
          </div>
        </>
      )}
    </section>
  );
}

function Versions({ game, versions, onChange }: { game: Game; versions: Version[]; onChange: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (versions.length === 0) return null;

  async function act(p: Promise<{ error?: unknown }>) {
    const { error } = await p;
    setErr(error ? errorMessage(error) : null);
    onChange();
  }
  const vp = (tag: string) => ({ params: { path: { game_id: game.id, version: tag } } });

  return (
    <section>
      <h2>Versions</h2>
      {err && <p className="error">{err}</p>}
      <table className="versions">
        <thead>
          <tr>
            <th>Version</th>
            <th>Released</th>
            <th>State</th>
            <th>Size</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <VersionRow
              key={v.tag}
              v={v}
              open={open === v.tag}
              onToggle={() => setOpen(open === v.tag ? null : v.tag)}
              gameId={game.id}
              canDownload={game.status === "active"}
              actions={{
                download: () =>
                  act(api.POST("/api/v1/games/{game_id}/update", {
                    params: { path: { game_id: game.id } },
                    body: { version: v.tag },
                  })),
                activate: () => act(api.POST("/api/v1/games/{game_id}/versions/{version}/activate", vp(v.tag))),
                pin: () =>
                  act(
                    v.pinned
                      ? api.DELETE("/api/v1/games/{game_id}/versions/{version}/pin", vp(v.tag))
                      : api.POST("/api/v1/games/{game_id}/versions/{version}/pin", vp(v.tag)),
                  ),
                remove: () => {
                  if (confirm(`Delete the downloaded files of ${v.tag}?`))
                    act(api.DELETE("/api/v1/games/{game_id}/versions/{version}", vp(v.tag)));
                },
              }}
            />
          ))}
        </tbody>
      </table>
    </section>
  );
}

function VersionRow(props: {
  v: Version;
  open: boolean;
  onToggle: () => void;
  gameId: string;
  canDownload: boolean;
  actions: { download: () => void; activate: () => void; pin: () => void; remove: () => void };
}) {
  const { v, open, onToggle, gameId, canDownload, actions } = props;
  const [artifacts, setArtifacts] = useState<Schemas["ArtifactOut"][] | null>(null);

  useEffect(() => {
    if (open && v.state === "downloaded") {
      api
        .GET("/api/v1/games/{game_id}/versions/{version}/artifacts", {
          params: { path: { game_id: gameId, version: v.tag } },
        })
        .then(({ data }) => setArtifacts(data ?? []));
    }
  }, [open, v.state, v.tag, gameId]);

  const stateTone = v.state === "downloaded" ? "ok" : v.state === "failed" ? "bad" : v.state === "downloading" ? "info" : undefined;
  return (
    <>
      <tr className={v.current ? "current" : undefined}>
        <td>
          <button className="link" onClick={onToggle} aria-expanded={open}>
            {v.tag}
          </button>
          {v.current && <Badge tone="ok">current</Badge>}
          {v.pinned && <Badge>pinned</Badge>}
          {v.prerelease && <Badge tone="warn">pre-release</Badge>}
        </td>
        <td>{date(v.published_at)}</td>
        <td>
          <Badge tone={stateTone}>{v.state}</Badge>
        </td>
        <td>{bytes(v.size)}</td>
        <td className="row-actions">
          {v.state === "downloaded" && !v.current && (
            <button className="link" onClick={actions.activate}>
              Make current
            </button>
          )}
          {canDownload && v.state !== "downloaded" && v.state !== "downloading" && v.platforms.length > 0 && (
            <button className="link" onClick={actions.download}>
              Download
            </button>
          )}
          <button className="link" onClick={actions.pin}>
            {v.pinned ? "Unpin" : "Pin"}
          </button>
          {v.state === "downloaded" && !v.current && !v.pinned && (
            <button className="link danger" onClick={actions.remove}>
              Delete files
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="details">
          <td colSpan={5}>
            <p className="small">
              <Platforms keys={v.platforms} />{" "}
              {v.url && (
                <a href={v.url} target="_blank" rel="noreferrer">
                  Release page
                </a>
              )}
            </p>
            {artifacts && artifacts.length > 0 && (
              <ul className="artifacts small">
                {artifacts.map((a) => (
                  <li key={a.id}>
                    <b>{a.name}</b> · {bytes(a.size)} · {a.platforms.join(", ")}
                    {a.sha256 && <code className="muted"> sha256 {a.sha256.slice(0, 16)}…</code>}
                  </li>
                ))}
              </ul>
            )}
            <div className="notes">{v.notes || <span className="muted">No release notes.</span>}</div>
          </td>
        </tr>
      )}
    </>
  );
}

function DeleteGame({ id }: { id: string }) {
  const navigate = useNavigate();
  const [err, setErr] = useState<string | null>(null);
  async function remove() {
    if (!confirm("Remove this game and delete its downloaded files?")) return;
    const { error } = await api.DELETE("/api/v1/games/{game_id}", {
      params: { path: { game_id: id }, query: { delete_files: true } },
    });
    if (error) setErr(errorMessage(error));
    else navigate("/library");
  }
  return (
    <>
      <button className="link danger" onClick={remove}>
        Remove game
      </button>
      {err && <span className="error small">{err}</span>}
    </>
  );
}
