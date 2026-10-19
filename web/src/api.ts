// Types mirror app/dashboard_api/main.py responses.

export type RunStatus = "running" | "completed" | "interrupted" | "error" | "stale";

export interface RunSummary {
  test_run_id: string;
  profile: string | null;
  target: string | null;
  metadata: { device_id: string; train_id: string; journey_id: string } | null;
  status: RunStatus;
  started_at: string | null;
  ended_at: string | null;
  last_request_at: string | null;
  duration_seconds: number | null;
  qoe_score: number | null;
}

export interface Device {
  device_id: string;
  train_id: string | null;
  journey_id: string | null;
  last_run_at: string | null;
  runs: number;
}

export interface Distribution {
  avg: number;
  min: number;
  max: number;
  p50: number;
  p95: number;
  p99: number;
}

export interface Stats {
  requests: {
    total: number;
    successful: number;
    failed: number;
    cancelled: number;
    success_pct: number | null;
    achieved_rps: number | null;
  };
  latency_ms: Distribution | null;
  bytes_received_total: number;
  throughput_mbps: { aggregate: number | null; samples: number };
}

export type QoeComponent = "latency" | "reliability" | "throughput";

export interface Qoe {
  qoe_score: number | null;
  qoe_model: string;
  disclaimer: string;
  components: Record<QoeComponent, number | null>;
  missing: QoeComponent[];
  weights: Record<QoeComponent, number>;
}

export interface SeriesPoint {
  t: string;
  ok: number;
  failed: number;
  rps: number;
  latency_p50: number | null;
  latency_p95: number | null;
  throughput_mbps: number | null;
}

export interface RecentRequest {
  timestamp: string;
  request_id: string;
  url: string;
  outcome: "success" | "failure" | "cancelled";
  status_code: number | null;
  latency_ms: number | null;
  time_to_first_byte_ms: number | null;
  total_duration_ms: number | null;
  bytes_received: number;
  throughput_mbps: number | null;
  error: string | null;
}

export interface LiveData {
  run: RunSummary;
  elapsed_seconds: number;
  source: "summary" | "live";
  stats: Stats;
  qoe: Qoe;
  bucket_seconds: number;
  series: SeriesPoint[];
  recent: RecentRequest[];
}

export async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText} from ${url}`);
  }
  return (await response.json()) as T;
}
