export function bytes(n: number | null | undefined): string {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function date(s: string | null | undefined): string {
  return s ? new Date(s).toLocaleDateString() : "";
}

export function dateTime(s: string | null | undefined): string {
  return s ? new Date(s).toLocaleString() : "";
}

export function ago(s: string | null | undefined): string {
  if (!s) return "never";
  const sec = (Date.now() - new Date(s).getTime()) / 1000;
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
  return `${Math.floor(sec / 86400)} d ago`;
}

/** Preview a server (Python) regex in the browser. Handles a leading (?i). */
export function jsRegex(pattern: string): RegExp | null {
  let flags = "";
  let p = pattern;
  if (p.startsWith("(?i)")) {
    flags = "i";
    p = p.slice(4);
  }
  try {
    return new RegExp(p, flags);
  } catch {
    return null;
  }
}
