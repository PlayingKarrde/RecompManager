import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage, getToken, type Schemas } from "../api/client";
import { Badge, JobStatusBadge } from "../components/bits";
import { ago, bytes } from "../lib/format";
import { useServerEvents } from "../lib/events";

type Overview = Schemas["BuildsOverview"];
type Build = Schemas["BuildOut"];

/** Server-side builds and extractions for one recomp. */
export function Builds({ recompId }: { recompId: string }) {
  const [data, setData] = useState<Overview | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    const { data } = await api.GET("/api/v1/recomps/{recomp_id}/builds", { params: { path: { recomp_id: recompId } } });
    if (data) setData(data);
  }, [recompId]);

  useEffect(() => {
    load();
  }, [load]);

  useServerEvents((e) => {
    if ((e.type === "job.updated" && e.data.kind === "build" && e.data.recomp_id === recompId) ||
        (e.type === "recomp.updated" && e.data.id === recompId)) load();
  });

  if (!data || data.targets.length === 0) return null;

  async function start() {
    setErr(null);
    const { error } = await api.POST("/api/v1/recomps/{recomp_id}/builds", {
      params: { path: { recomp_id: recompId } },
      body: {},
    });
    if (error) setErr(errorMessage(error));
    load();
  }

  return (
    <section>
      <div className="title-row">
        <h2>Server builds</h2>
        <button className="secondary small-btn" disabled={!data.available} onClick={start}>
          Build now
        </button>
      </div>
      <p className="muted small">
        {data.targets.map(describeTarget).join(". ")}. Builds start by themselves when a new version or a usable
        input arrives.
      </p>
      {!data.available && (
        <p className="warn-text small">
          {data.unavailable_reason}. Until builds can run, clients get the fallback plan (they build or place the input
          themselves).
        </p>
      )}
      {err && <p className="error small">{err}</p>}
      {data.builds.length > 0 && (
        <ul className="jobs">
          {data.builds.map((b) => (
            <BuildRow key={b.id} build={b} open={open === b.id} onToggle={() => setOpen(open === b.id ? null : b.id)} />
          ))}
        </ul>
      )}
    </section>
  );
}

function describeTarget(t: Schemas["BuildTarget"]): string {
  if (t.kind === "extract") return "Extracts the game data once per version, for every platform";
  const needs = [
    t.min_memory_gb ? `${t.min_memory_gb} GB RAM` : "",
    t.min_disk_gb ? `${t.min_disk_gb} GB free disk` : "",
  ].filter(Boolean);
  return `Builds ${t.platform}` + (needs.length ? ` (needs ${needs.join(", ")})` : "");
}

function BuildRow({ build: b, open, onToggle }: { build: Build; open: boolean; onToggle: () => void }) {
  return (
    <li className="job">
      <div className="job-line">
        <button className="link" onClick={onToggle} aria-expanded={open}>
          #{b.id} {b.kind === "extract" ? "extract" : `build ${b.platform}`}
        </button>
        <span className="small">{b.version}</span>
        <JobStatusBadge status={b.status} />
        {b.auto && <Badge>automatic</Badge>}
        <span className={`small grow ${b.error ? "error" : "muted"}`}>
          {b.error ? b.error.split("\n")[0] : b.outputs.map((o) => `${o.name} (${bytes(o.size)})`).join(", ")}
        </span>
        <span className="muted small">{ago(b.finished_at ?? b.started_at ?? b.created_at)}</span>
      </div>
      {open && <BuildLog id={b.id} running={b.status === "queued" || b.status === "running"} />}
    </li>
  );
}

/** Follows GET /builds/{id}/log (SSE) with fetch, since EventSource can't send a token. */
function BuildLog({ id, running }: { id: number; running: boolean }) {
  const [lines, setLines] = useState<string[]>([]);
  const box = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setLines([]);
    (async () => {
      const res = await fetch(`/api/v1/builds/${id}/log`, {
        headers: { Authorization: `Bearer ${getToken()}` },
        signal: ctrl.signal,
      });
      if (!res.body) return;
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += value;
        const blocks = buf.split("\n\n");
        buf = blocks.pop() ?? "";
        const got: string[] = [];
        for (const blk of blocks) {
          const data = blk.split("\n").find((l) => l.startsWith("data: "));
          if (blk.startsWith("event: line") && data) got.push(JSON.parse(data.slice(6)).text);
        }
        if (got.length) setLines((prev) => [...prev, ...got].slice(-2000));
      }
    })().catch(() => undefined);
    return () => ctrl.abort();
  }, [id, running]);

  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);

  return <pre className="log" ref={box}>{lines.length ? lines.join("\n") : "No log yet."}</pre>;
}
