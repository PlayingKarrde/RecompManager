import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { api, errorMessage, getToken, type Schemas } from "../api/client";
import { Badge, JobStatusBadge, Progress, SteamOSBadge } from "../components/bits";
import { ago, bytes } from "../lib/format";
import { useServerEvents } from "../lib/events";

type Game = Schemas["GameSummary"];
type Input = Schemas["InputOut"];
type Suggestion = Schemas["SuggestionOut"];

export function GameDetail() {
  const { id = "" } = useParams();
  const [game, setGame] = useState<Game | null>(null);
  const [inputs, setInputs] = useState<Input[]>([]);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [jobs, setJobs] = useState<Schemas["JobSummary"][]>([]);
  const [missing, setMissing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const path = { params: { path: { game_id: id } } };

  const load = useCallback(async () => {
    const p = { params: { path: { game_id: id } } };
    const [g, i, s, j] = await Promise.all([
      api.GET("/api/v1/games/{game_id}", p),
      api.GET("/api/v1/games/{game_id}/inputs", p),
      api.GET("/api/v1/games/{game_id}/suggestions", p),
      api.GET("/api/v1/jobs", { params: { query: { game_id: id, limit: 5 } } }),
    ]);
    if (g.response.status === 404) setMissing(true);
    if (g.data) setGame(g.data);
    if (i.data) setInputs(i.data);
    if (s.data) setSuggestions(s.data);
    if (j.data) setJobs(j.data);
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  useServerEvents((e) => {
    if (e.type === "game.updated" && e.data.id === id) load();
    if (e.type === "job.updated" && e.data.game_id === id) {
      setJobs((prev) => prev.map((j) => (j.id === e.data.id ? { ...j, ...e.data, status: e.data.status as never } : j)));
      if (!jobs.some((j) => j.id === e.data.id) || ["succeeded", "failed", "cancelled"].includes(e.data.status)) load();
    }
  });

  if (missing) return <section><p>No such game. <Link to="/library">Back to the library</Link></p></section>;
  if (!game) return <section><p className="muted">Loading…</p></section>;

  async function act(p: Promise<{ error?: unknown }>) {
    setMsg(null);
    const { error } = await p;
    if (error) setMsg(errorMessage(error));
    load();
  }

  return (
    <>
      <section>
        <TitleEditor game={game} onSaved={load} />
        {game.recomps.length > 0 ? (
          <ul className="plain">
            {game.recomps.map((r) => (
              <li key={r.id}>
                <Link to={`/recomps/${r.id}`}>{r.name}</Link>{" "}
                <span className="muted small">
                  {r.status === "draft" ? "draft" : r.current_version ?? "not downloaded"}
                </span>{" "}
                {r.status === "active" && <SteamOSBadge status={r.steamos} />}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No recomps for this game.</p>
        )}
        {msg && <p className="error">{msg}</p>}
      </section>

      {jobs.some((j) => j.status === "queued" || j.status === "running") && (
        <section>
          <ul className="jobs">
            {jobs
              .filter((j) => j.status === "queued" || j.status === "running")
              .map((j) => (
                <li key={j.id}>
                  <span className="job-kind">{j.kind}</span>
                  <JobStatusBadge status={j.status} />
                  <Progress value={j.progress} />
                  <span className="muted small grow">{j.message}</span>
                </li>
              ))}
          </ul>
        </section>
      )}

      <section>
        <h2>Inputs</h2>
        <p className="muted small">
          Your own dumps of this game. Every recomp of the game can use them. They're only sent to your own clients.
        </p>
        {inputs.length === 0 ? (
          <p className="warn-text">No input yet. Scan your ROM library, import from /import, or upload.</p>
        ) : (
          <ul className="inputs">
            {inputs.map((i) => (
              <InputRow
                key={i.id}
                input={i}
                onRehash={() => act(api.POST("/api/v1/inputs/{input_id}/rehash", { params: { path: { input_id: i.id }, query: {} } }))}
                onDelete={() => {
                  const what = i.storage === "reference" ? "Forget this input? The file itself stays where it is." : "Delete this input from the vault?";
                  if (confirm(what)) act(api.DELETE("/api/v1/inputs/{input_id}", { params: { path: { input_id: i.id } } }));
                }}
              />
            ))}
          </ul>
        )}
        <AddInput gameId={id} onStarted={load} />
      </section>

      <Suggestions
        items={suggestions}
        onAccept={(s) => act(api.POST("/api/v1/suggestions/{suggestion_id}/accept", { params: { path: { suggestion_id: s.id } }, body: { mode: "reference" } }))}
        onDismiss={(s) => act(api.POST("/api/v1/suggestions/{suggestion_id}/dismiss", { params: { path: { suggestion_id: s.id } } }))}
        onScan={() => act(api.POST("/api/v1/system/scan"))}
      />
      <DeleteGame game={game} path={path} />
    </>
  );
}

function TitleEditor({ game, onSaved }: { game: Game; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(game.title);
  const [system, setSystem] = useState(game.system ?? "");
  async function save(e: FormEvent) {
    e.preventDefault();
    await api.PATCH("/api/v1/games/{game_id}", {
      params: { path: { game_id: game.id } },
      body: { title, system },
    });
    setEditing(false);
    onSaved();
  }
  if (!editing)
    return (
      <div className="title-row">
        <div>
          <h2>{game.title}</h2>
          <span className="muted small">
            {game.system ?? "system not set"}
            {Object.entries(game.identifiers).map(([k, v]) => ` · ${k.replace(/_/g, " ")} ${v}`)}
          </span>
        </div>
        <button className="link" onClick={() => setEditing(true)}>
          Edit
        </button>
      </div>
    );
  return (
    <form className="row" onSubmit={save}>
      <input aria-label="Title" className="grow" value={title} onChange={(e) => setTitle(e.target.value)} />
      <input aria-label="System" placeholder="n64, ps4…" value={system} onChange={(e) => setSystem(e.target.value)} />
      <button type="submit">Save</button>
      <button type="button" className="secondary" onClick={() => setEditing(false)}>
        Cancel
      </button>
    </form>
  );
}

function fitTone(s: string) {
  return s === "verified" ? "ok" : s === "mismatch" ? "bad" : "warn";
}

function InputRow({ input: i, onRehash, onDelete }: { input: Input; onRehash: () => void; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  const facts = i.facts as Record<string, unknown>;
  const ps4 = facts.ps4 as Record<string, string> | undefined;
  const sw = facts.switch as Record<string, string> | undefined;
  return (
    <li className="input">
      <div className="row">
        <button className="link" onClick={() => setOpen(!open)} aria-expanded={open}>
          {i.name}
        </button>
        <span className="muted small">
          {i.kind === "folder" ? `${i.file_count} files, ` : ""}
          {bytes(i.size)} · {i.storage === "reference" ? "read in place" : "in the vault"}
        </span>
        {i.state !== "ready" && <Badge tone={i.state === "pending" ? "info" : "bad"}>{i.state}</Badge>}
        <span className="spacer" />
        <button className="link" onClick={onRehash}>
          Check again
        </button>
        <button className="link danger" onClick={onDelete}>
          Remove
        </button>
      </div>
      <div className="row small">
        {i.fits.map((f) => (
          <span key={f.recomp_id} title={f.reason}>
            <Badge tone={fitTone(f.status)}>
              {f.recomp_id}
              {f.slot !== "input" ? ` (${f.slot})` : ""}: {f.status}
            </Badge>
          </span>
        ))}
      </div>
      {open && (
        <dl className="facts small">
          {i.fits.map((f) => (
            <FactRow key={f.recomp_id} k={f.recomp_id} v={f.reason} />
          ))}
          {ps4 && <FactRow k="PS4" v={`${ps4.TITLE_ID ?? ""} version ${ps4.APP_VER ?? ps4.VERSION ?? "?"}`} />}
          {sw && <FactRow k="Switch program ID" v={sw.program_id ?? "unreadable"} />}
          {i.sha1 && <FactRow k="sha1" v={i.sha1} mono />}
          {i.crc32 && <FactRow k="crc32" v={i.crc32} mono />}
          <FactRow k="Added" v={`${i.source}, ${ago(i.created_at)}`} />
          {i.error && <FactRow k="Problem" v={i.error} />}
        </dl>
      )}
    </li>
  );
}

function FactRow({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <>
      <dt>{k}</dt>
      <dd className={mono ? "mono" : undefined}>{v}</dd>
    </>
  );
}

function Suggestions(props: {
  items: Suggestion[];
  onAccept: (s: Suggestion) => void;
  onDismiss: (s: Suggestion) => void;
  onScan: () => void;
}) {
  return (
    <section>
      <div className="title-row">
        <h2>Found in your ROM library</h2>
        <button className="secondary small-btn" onClick={props.onScan}>
          Scan now
        </button>
      </div>
      {props.items.length === 0 ? (
        <p className="muted small">Nothing new. Scans run on a schedule (Settings) and match by checksum, title ID or name.</p>
      ) : (
        <ul className="inputs">
          {props.items.map((s) => (
            <li key={s.id} className="input">
              <div className="row">
                <Badge tone={s.confidence === "verified" ? "ok" : s.confidence === "unusable" ? "bad" : "warn"}>
                  {s.confidence}
                </Badge>
                <span className="mono small grow">
                  {s.path}
                  {s.member && <span className="muted"> → {s.member}</span>}
                </span>
                {s.confidence !== "unusable" && (
                  <button className="link" onClick={() => props.onAccept(s)}>
                    Use it
                  </button>
                )}
                <button className="link danger" onClick={() => props.onDismiss(s)}>
                  Dismiss
                </button>
              </div>
              <div className="muted small">{s.reason}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function AddInput({ gameId, onStarted }: { gameId: string; onStarted: () => void }) {
  const [mode, setMode] = useState<"none" | "browse" | "upload">("none");
  return (
    <div className="add-input">
      <div className="row">
        <button className="secondary small-btn" onClick={() => setMode(mode === "browse" ? "none" : "browse")}>
          Import from /import or /roms
        </button>
        <button className="secondary small-btn" onClick={() => setMode(mode === "upload" ? "none" : "upload")}>
          Upload a file
        </button>
      </div>
      {mode === "browse" && <Browser gameId={gameId} onStarted={onStarted} />}
      {mode === "upload" && <Uploader gameId={gameId} onDone={onStarted} />}
    </div>
  );
}

function Browser({ gameId, onStarted }: { gameId: string; onStarted: () => void }) {
  const [listing, setListing] = useState<Schemas["BrowseOut"] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [how, setHow] = useState<"reference" | "copy">("reference");

  async function open(path?: string) {
    const { data, error } = await api.GET("/api/v1/system/browse", { params: { query: path ? { path } : {} } });
    if (data) setListing(data);
    setErr(error ? errorMessage(error) : null);
  }
  useEffect(() => {
    open();
  }, []);

  async function pick(path: string, extra: Partial<Schemas["ImportRequest"]> = {}) {
    const { error } = await api.POST("/api/v1/games/{game_id}/inputs/import", {
      params: { path: { game_id: gameId } },
      body: { path, mode: how, extract: false, ...extra },
    });
    if (error) setErr(errorMessage(error));
    else onStarted();
  }

  if (!listing) return <p className="muted small">{err ?? "Loading…"}</p>;
  if (listing.entries.length === 0 && !listing.path)
    return <p className="muted small">Nothing mounted at /import or /roms. Add a path in the container template.</p>;
  return (
    <div className="browser">
      <div className="row small">
        <span className="mono grow">{listing.path ?? "Choose a folder"}</span>
        <label className="row">
          <select value={how} onChange={(e) => setHow(e.target.value as "reference" | "copy")} aria-label="How to import">
            <option value="reference">Use in place</option>
            <option value="copy">Copy into the vault</option>
          </select>
        </label>
      </div>
      <ul className="browse">
        {listing.parent && (
          <li>
            <button className="link" onClick={() => open(listing.parent!)}>
              ..
            </button>
          </li>
        )}
        {listing.entries.map((e) => (
          <li key={e.path} className="row">
            {e.is_dir ? (
              <button className="link" onClick={() => open(e.path)}>
                {e.name}/
              </button>
            ) : (
              <span>{e.name}</span>
            )}
            <span className="muted small">{e.size != null ? bytes(e.size) : ""}</span>
            <span className="spacer" />
            {listing.path && (
              <button className="link small" onClick={() => pick(e.path)}>
                {e.is_dir ? "Use this folder" : "Use this file"}
              </button>
            )}
            {!e.is_dir && /\.(zip|7z)$/i.test(e.name) && (
              <button className="link small" onClick={() => pick(e.path, { extract: true })}>
                Unpack
              </button>
            )}
          </li>
        ))}
      </ul>
      {err && <p className="error small">{err}</p>}
    </div>
  );
}

const CHUNK = 8 * 1024 * 1024;

function Uploader({ gameId, onDone }: { gameId: string; onDone: () => void }) {
  const [progress, setProgress] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const stop = useRef(false);

  async function upload(file: File) {
    setErr(null);
    stop.current = false;
    const { data: up, error } = await api.POST("/api/v1/games/{game_id}/uploads", {
      params: { path: { game_id: gameId } },
      body: { filename: file.name, size: file.size },
    });
    if (!up) return setErr(errorMessage(error));
    let offset = 0;
    setProgress(0);
    while (offset < file.size) {
      if (stop.current) return setProgress(null);
      const chunk = file.slice(offset, offset + CHUNK);
      // Raw bytes; the typed client is for JSON, so this one call uses fetch.
      const res = await fetch(`/api/v1/uploads/${up.id}?offset=${offset}`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${getToken()}`, "Content-Type": "application/octet-stream" },
        body: chunk,
      });
      if (!res.ok) {
        // Resume from what the server actually has.
        const { data } = await api.GET("/api/v1/uploads/{upload_id}", { params: { path: { upload_id: up.id } } });
        if (!data) return setErr("The upload failed");
        offset = data.received;
        continue;
      }
      offset = (await res.json()).received;
      setProgress(offset / file.size);
    }
    const done = await api.POST("/api/v1/uploads/{upload_id}/complete", { params: { path: { upload_id: up.id } } });
    if (done.error) setErr(errorMessage(done.error));
    setProgress(null);
    onDone();
  }

  return (
    <div className="row">
      <input type="file" aria-label="File to upload" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
      {progress != null && (
        <>
          <Progress value={progress} />
          <button className="link danger" onClick={() => (stop.current = true)}>
            Stop
          </button>
        </>
      )}
      <span className="muted small">Large folders and ISOs are better imported from /import.</span>
      {err && <span className="error small">{err}</span>}
    </div>
  );
}

function DeleteGame({ game, path }: { game: Game; path: { params: { path: { game_id: string } } } }) {
  const [err, setErr] = useState<string | null>(null);
  if (game.recomps.length > 0) return null;
  async function remove() {
    if (!confirm(`Remove ${game.title}?`)) return;
    const { error } = await api.DELETE("/api/v1/games/{game_id}", path);
    if (error) setErr(errorMessage(error));
    else window.location.assign("/library");
  }
  return (
    <section>
      <button className="link danger" onClick={remove}>
        Remove this game
      </button>
      {err && <span className="error small"> {err}</span>}
    </section>
  );
}
