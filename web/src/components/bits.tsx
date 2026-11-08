import type { Schemas } from "../api/client";
import type { ReactNode } from "react";

export function Badge({ tone, children }: { tone?: "ok" | "warn" | "bad" | "info"; children: ReactNode }) {
  return <span className={`badge ${tone ?? ""}`}>{children}</span>;
}

export function Progress({ value }: { value: number | null | undefined }) {
  const indeterminate = value == null;
  return (
    <div className={`progress ${indeterminate ? "indeterminate" : ""}`} role="progressbar"
      aria-valuenow={indeterminate ? undefined : Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}>
      <div style={{ width: indeterminate ? undefined : `${Math.round(value * 100)}%` }} />
    </div>
  );
}

export function JobStatusBadge({ status }: { status: string }) {
  const tone =
    status === "succeeded" ? "ok" : status === "failed" ? "bad" : status === "cancelled" ? undefined : "info";
  return <Badge tone={tone}>{status}</Badge>;
}

export function Platforms({ keys }: { keys: string[] }) {
  return (
    <span className="chips">
      {keys.map((k) => (
        <span key={k} className="chip">
          {k}
        </span>
      ))}
    </span>
  );
}

type SteamOS = Schemas["SteamOSStatus"];

const STEAMOS_TONE: Record<SteamOS["state"], "ok" | "warn" | "bad" | "info" | undefined> = {
  ready: "ok",
  building: "info",
  not_built: "info",
  needs_files: "warn",
  builds_off: "warn",
  not_downloaded: undefined,
  build_failed: "bad",
  needs_compiler: "bad",
  no_linux: "bad",
  unknown: undefined,
};

/** Whether a recomp runs on SteamOS; the detail shows on hover. */
export function SteamOSBadge({ status }: { status: SteamOS }) {
  return (
    <span className={`badge ${STEAMOS_TONE[status.state] ?? ""}`} title={status.detail}>
      SteamOS: {status.summary}
    </span>
  );
}
