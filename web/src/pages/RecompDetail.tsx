import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage, type Schemas } from "../api/client";
import { Badge, JobStatusBadge, Platforms, Progress } from "../components/bits";
import { ago, bytes, date, jsRegex } from "../lib/format";
import { useServerEvents, type JobEvent } from "../lib/events";
import { Builds } from "./Builds";
import { RecompStatus } from "./Library";

type Game = Schemas["RecompDetail"];
type Version = Schemas["VersionOut"];

export function RecompDetail() {
  const { id = "" } = useParams();
  const [game, setGame] = useState<Game | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [jobs, setJobs] = useState<Schemas["JobSummary"][]>([]);
  const [missing, setMissing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [g, v, j] = await Promise.all([
      api.GET("/api/v1/recomps/{recomp_id}", { params: { path: { recomp_id: id } } }),
      api.GET("/api/v1/recomps/{recomp_id}/versions", { params: { path: { recomp_id: id } } }),
      api.GET("/api/v1/jobs", { params: { query: { recomp_id: id, limit: 5 } } }),
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
    if (e.type === "job.updated" && e.data.recomp_id === id) {
      updateJob(e.data);
      if (["succeeded", "failed", "cancelled"].includes(e.data.status)) load();
    } else if (e.type === "recomp.updated" && e.data.id === id) load();
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

  if (missing) return <section><p>No such recomp. <Link to="/library">Back to the library</Link></p></section>;
  if (!game) return <section><p className="muted">Loading…</p></section>;

  const busy = jobs.some((j) => j.status === "queued" || j.status === "running");

  async function act(fn: () => Promise<{ error?: unknown }>) {
    setMsg(null);
    const { error } = await fn();
    if (error) setMsg(errorMessage(error));
    load();
  }
  const path = { params: { path: { recomp_id: id } } };

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
          <RecompStatus recomp={game} />
        </div>
        {game.recipe?.description && <p>{game.recipe.description}</p>}
        <dl className="facts">
          <dt>Game</dt>
          <dd>
            <GamePicker recomp={game} onSaved={load} />
          </dd>
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
          <button disabled={busy} onClick={() => act(() => api.POST("/api/v1/recomps/{recomp_id}/check", path))}>
            Check for updates
          </button>
          {game.status === "active" && game.latest_version && game.update_available && (
            <button
              disabled={busy}
              onClick={() => act(() => api.POST("/api/v1/recomps/{recomp_id}/update", { ...path, body: {} }))}
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
                  api.PATCH("/api/v1/recomps/{recomp_id}", { ...path, body: { auto_update: e.target.checked } }),
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
            onClick={() => act(() => api.PATCH("/api/v1/recomps/{recomp_id}", { ...path, body: { status: "active" } }))}
          >
            Confirm recipe
          </button>
        </section>
      )}

      <AssetRules game={game} onSaved={load} />
      <Builds recompId={game.id} />
      <PlanPreview recomp={game} />
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
    const { error } = await api.PATCH("/api/v1/recomps/{recomp_id}", {
      params: { path: { recomp_id: game.id } },
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
  const vp = (tag: string) => ({ params: { path: { recomp_id: game.id, version: tag } } });

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
                  act(api.POST("/api/v1/recomps/{recomp_id}/update", {
                    params: { path: { recomp_id: game.id } },
                    body: { version: v.tag },
                  })),
                activate: () => act(api.POST("/api/v1/recomps/{recomp_id}/versions/{version}/activate", vp(v.tag))),
                pin: () =>
                  act(
                    v.pinned
                      ? api.DELETE("/api/v1/recomps/{recomp_id}/versions/{version}/pin", vp(v.tag))
                      : api.POST("/api/v1/recomps/{recomp_id}/versions/{version}/pin", vp(v.tag)),
                  ),
                remove: () => {
                  if (confirm(`Delete the downloaded files of ${v.tag}?`))
                    act(api.DELETE("/api/v1/recomps/{recomp_id}/versions/{version}", vp(v.tag)));
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
        .GET("/api/v1/recomps/{recomp_id}/versions/{version}/artifacts", {
          params: { path: { recomp_id: gameId, version: v.tag } },
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
                    <b>{a.name}</b> · {bytes(a.size)} · {a.platform}
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
    if (!confirm("Remove this recomp and delete its downloaded files?")) return;
    const { error } = await api.DELETE("/api/v1/recomps/{recomp_id}", {
      params: { path: { recomp_id: id }, query: { delete_files: true } },
    });
    if (error) setErr(errorMessage(error));
    else navigate("/library");
  }
  return (
    <>
      <button className="link danger" onClick={remove}>
        Remove recomp
      </button>
      {err && <span className="error small">{err}</span>}
    </>
  );
}

function GamePicker({ recomp, onSaved }: { recomp: Game; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [games, setGames] = useState<Schemas["GameSummary"][]>([]);
  const [title, setTitle] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (editing) api.GET("/api/v1/games").then(({ data }) => data && setGames(data));
  }, [editing]);

  async function save(body: Schemas["RecompPatch"]) {
    const { error } = await api.PATCH("/api/v1/recomps/{recomp_id}", {
      params: { path: { recomp_id: recomp.id } },
      body,
    });
    if (error) return setErr(errorMessage(error));
    setEditing(false);
    onSaved();
  }

  const unconfirmed = recomp.recipe?.unverified?.includes("game");
  if (!editing)
    return (
      <span className="row">
        {recomp.game_id ? <Link to={`/games/${recomp.game_id}`}>{recomp.game_title}</Link> : <span className="muted">Not known</span>}
        {unconfirmed && <Badge tone="warn">guessed</Badge>}
        <button className="link small" onClick={() => setEditing(true)}>
          Change
        </button>
      </span>
    );
  return (
    <span className="row">
      <select aria-label="Existing game" defaultValue="" onChange={(e) => e.target.value && save({ game_id: e.target.value })}>
        <option value="">Pick a game…</option>
        {games.map((g) => (
          <option key={g.id} value={g.id}>
            {g.title}
          </option>
        ))}
      </select>
      <span className="muted small">or</span>
      <input aria-label="New game title" placeholder="New game title" value={title} onChange={(e) => setTitle(e.target.value)} />
      <button className="small-btn" disabled={!title.trim()} onClick={() => save({ game_title: title.trim() })}>
        Set
      </button>
      <button className="link small" onClick={() => setEditing(false)}>
        Cancel
      </button>
      {err && <span className="error small">{err}</span>}
    </span>
  );
}

const PLATFORMS = ["windows-x64", "windows-arm64", "linux-x64", "linux-x64-steamdeck", "linux-arm64", "macos-arm64", "macos-x64", "android-arm64"];

function PlanPreview({ recomp }: { recomp: Game }) {
  const [platform, setPlatform] = useState(recomp.platforms[0] ?? "windows-x64");
  const [plan, setPlan] = useState<Schemas["InstallPlan"] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/v1/recomps/{recomp_id}/install-plan", {
        params: { path: { recomp_id: recomp.id }, query: { platform } },
      })
      .then(({ data, error }) => {
        setPlan(data ?? null);
        setErr(error ? errorMessage(error) : null);
      });
  }, [recomp.id, recomp.current_version, platform]);

  return (
    <section>
      <div className="title-row">
        <h2>Install plan</h2>
        <select aria-label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          {[...new Set([...recomp.platforms, ...PLATFORMS])].map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </div>
      <p className="muted small">What a client on this platform would do. Clients fetch this from the API.</p>
      {err && <p className="error">{err}</p>}
      {plan && (
        <>
          <p className="row small">
            <Badge tone={plan.ready ? "ok" : "bad"}>{plan.ready ? "ready" : "not ready"}</Badge>
            <span>
              {plan.version ?? "no version"} · {plan.platform ?? "no build"} · {plan.strategy}
            </span>
          </p>
          {plan.problems.map((p) => (
            <p key={p} className="error small">
              {p}
            </p>
          ))}
          {plan.warnings.map((w) => (
            <p key={w} className="warn-text small">
              {w}
            </p>
          ))}
          <ol className="steps small">
            {plan.steps.map((st, i) => (
              <li key={i}>
                <b>{st.type}</b> <StepText step={st} />
              </li>
            ))}
          </ol>
          {plan.launch && (
            <p className="small">
              Launch: <code>{[plan.launch.exe, ...(plan.launch.args ?? [])].join(" ")}</code>
              {plan.saves && (
                <>
                  {" "}
                  · Saves: <code>{plan.saves.path}</code>
                </>
              )}
            </p>
          )}
        </>
      )}
    </section>
  );
}

function StepText({ step }: { step: Schemas["InstallPlan"]["steps"][number] }) {
  switch (step.type) {
    case "download_artifact":
      return (
        <span>
          {step.name} ({bytes(step.size)}) → <code>{step.to}</code>
          {step.extract ? (step.strip_root ? " (unpack, drop top folder)" : " (unpack)") : ""}
        </span>
      );
    case "download_input":
      return (
        <span>
          {step.name} [{step.status}] → <code>{step.to}</code>
        </span>
      );
    case "write_file":
      return <code>{step.path}</code>;
    case "chmod":
      return (
        <code>
          {step.mode} {step.path}
        </code>
      );
    case "run":
      return (
        <span>
          <code>{step.command}</code> in <code>{step.cwd}</code> (asks first)
        </span>
      );
    case "instructions":
      return <span className="notes">{step.markdown}</span>;
  }
}
