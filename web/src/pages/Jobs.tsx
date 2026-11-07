import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Schemas } from "../api/client";
import { JobStatusBadge, Progress } from "../components/bits";
import { ago, dateTime } from "../lib/format";
import { useServerEvents } from "../lib/events";

export function Jobs() {
  const [jobs, setJobs] = useState<Schemas["JobSummary"][]>([]);
  const [params, setParams] = useSearchParams();
  const openId = Number(params.get("open")) || null;

  const load = useCallback(async () => {
    const { data } = await api.GET("/api/v1/jobs", { params: { query: { limit: 100 } } });
    if (data) setJobs(data);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useServerEvents((e) => {
    if (e.type !== "job.updated") return;
    setJobs((prev) => {
      const i = prev.findIndex((j) => j.id === e.data.id);
      if (i < 0) {
        load();
        return prev;
      }
      const next = [...prev];
      next[i] = { ...next[i], status: e.data.status as never, progress: e.data.progress, message: e.data.message };
      return next;
    });
    if (["succeeded", "failed", "cancelled"].includes(e.data.status)) load();
  });

  return (
    <section>
      <h2>Jobs</h2>
      {jobs.length === 0 ? (
        <p className="muted">No jobs yet.</p>
      ) : (
        <ul className="jobs">
          {jobs.map((j) => (
            <JobRow
              key={j.id}
              job={j}
              open={openId === j.id}
              onToggle={() => setParams(openId === j.id ? {} : { open: String(j.id) })}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function JobRow({ job: j, open, onToggle }: { job: Schemas["JobSummary"]; open: boolean; onToggle: () => void }) {
  const [detail, setDetail] = useState<Schemas["JobDetail"] | null>(null);
  const active = j.status === "queued" || j.status === "running";

  useEffect(() => {
    if (!open) return;
    let stop = false;
    const fetchLog = () =>
      api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: j.id } } }).then(({ data }) => {
        if (!stop && data) setDetail(data);
      });
    fetchLog();
    const t = active ? setInterval(fetchLog, 1500) : undefined;
    return () => {
      stop = true;
      if (t) clearInterval(t);
    };
  }, [open, j.id, active, j.status]);

  async function cancel() {
    await api.POST("/api/v1/jobs/{job_id}/cancel", { params: { path: { job_id: j.id } } });
  }

  return (
    <li className="job">
      <div className="job-line">
        <button className="link" onClick={onToggle} aria-expanded={open}>
          #{j.id} {j.kind}
        </button>
        {j.game_id && (
          <Link className="small" to={`/games/${j.game_id}`}>
            {j.game_id}
          </Link>
        )}
        <JobStatusBadge status={j.status} />
        {j.status === "running" && <Progress value={j.progress} />}
        <span className={`small grow ${j.error ? "error" : "muted"}`}>{j.error ?? j.message ?? ""}</span>
        <span className="muted small" title={dateTime(j.created_at)}>
          {ago(j.created_at)}
        </span>
        {active && (
          <button className="link danger" onClick={cancel}>
            Cancel
          </button>
        )}
      </div>
      {open && <pre className="log">{detail?.log || "No log output yet."}</pre>}
    </li>
  );
}
