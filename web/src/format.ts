const DASH = "—";

export function fmtNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return DASH;
  return value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Milliseconds with precision that suits the magnitude: 0.84 ms, 12.4 ms, 1,204 ms. */
export function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  const digits = value < 1 ? 2 : value < 100 ? 1 : 0;
  return `${fmtNumber(value, digits)} ms`;
}

export function fmtMbps(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  return `${fmtNumber(value, value < 10 ? 2 : value < 1000 ? 1 : 0)} Mbps`;
}

export function fmtPct(value: number | null | undefined): string {
  return value === null || value === undefined ? DASH : `${fmtNumber(value, 1)}%`;
}

export function fmtBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  const units = ["B", "KB", "MB", "GB"];
  let n = value;
  let unit = 0;
  while (n >= 1000 && unit < units.length - 1) {
    n /= 1000;
    unit += 1;
  }
  return `${fmtNumber(n, unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return DASH;
  const s = Math.round(seconds);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

/** Path and query of a URL, which is the informative part when every request hits one host. */
export function urlPath(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.pathname + parsed.search;
  } catch {
    return url;
  }
}
