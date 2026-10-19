import { CircleAlert } from "lucide-react";
import { useState } from "react";

import type { Device, LiveData, RunSummary } from "@/api";
import { LatencyChart, RateChart, ThroughputChart } from "@/components/dashboard/charts";
import { KpiTiles } from "@/components/dashboard/KpiTiles";
import { QoeCard } from "@/components/dashboard/QoeCard";
import { RecentRequests } from "@/components/dashboard/RecentRequests";
import { RunStatusBadge } from "@/components/dashboard/StatusBadge";
import { ThemeToggle } from "@/components/dashboard/ThemeToggle";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { fmtDateTime, fmtDuration, fmtNumber, fmtTime } from "@/format";
import { cn } from "@/lib/utils";
import { usePolling } from "@/usePolling";

const RUNNING_POLL_MS = 2000;
const IDLE_POLL_MS = 10000;
const ALL_DEVICES = "__all__";

function runLabel(run: RunSummary, withDevice: boolean): string {
  const qoe = run.qoe_score === null ? "" : ` · QoE ${fmtNumber(run.qoe_score, 1)}`;
  const device = withDevice && run.metadata ? ` · ${run.metadata.device_id}` : "";
  return `${fmtDateTime(run.started_at ?? run.last_request_at)}${device} · ${run.status}${qoe}`;
}

function RunDetails({ data }: { data: LiveData }) {
  const { run } = data;
  const progress = run.duration_seconds ? Math.min(100, (100 * data.elapsed_seconds) / run.duration_seconds) : null;
  const rows: [string, string][] = [
    ["Target", run.target ?? "—"],
    ["Profile", run.profile ?? "—"],
    ["Device", run.metadata?.device_id ?? "—"],
    ["Train", run.metadata?.train_id ?? "—"],
    ["Journey", run.metadata?.journey_id ?? "—"],
    ["Started", fmtDateTime(run.started_at)],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">Run <RunStatusBadge status={run.status} /></CardTitle>
        <CardDescription className="font-mono text-xs">{run.test_run_id}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-[80px_1fr] gap-x-3 gap-y-1.5 text-sm">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{k}</dt>
              <dd className="truncate">{v}</dd>
            </div>
          ))}
        </dl>
        <div className="space-y-1.5">
          <div className="flex justify-between text-xs text-muted-foreground">
            <span>Elapsed {fmtDuration(data.elapsed_seconds)}</span>
            <span>of {fmtDuration(run.duration_seconds)}</span>
          </div>
          <Progress value={progress ?? 0} aria-label="Run progress"
            className="h-1.5 bg-chart-1-track [&_[data-slot=progress-indicator]]:bg-chart-1" />
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyState() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>No runs yet</CardTitle>
        <CardDescription>Start a run with live publishing and it will appear here within a couple of seconds.</CardDescription>
      </CardHeader>
      <CardContent>
        <pre className="overflow-x-auto rounded-lg bg-muted p-3 text-xs">
          uv run python -m app.traffic_generator.main --config configs/live.yaml
        </pre>
      </CardContent>
    </Card>
  );
}

export default function App() {
  const devices = usePolling<Device[]>("/api/devices", IDLE_POLL_MS);
  const [device, setDevice] = useState(ALL_DEVICES);
  const deviceQuery = device === ALL_DEVICES ? "" : `&device_id=${encodeURIComponent(device)}`;
  const runs = usePolling<RunSummary[]>(`/api/runs?limit=50${deviceQuery}`, RUNNING_POLL_MS);
  const [follow, setFollow] = useState(true);
  const [picked, setPicked] = useState<string | null>(null);

  const latestId = runs.data?.[0]?.test_run_id ?? null;
  const runId = follow ? latestId : (picked ?? latestId);
  const selected = runs.data?.find((r) => r.test_run_id === runId);
  const pollMs = selected?.status === "running" ? RUNNING_POLL_MS : IDLE_POLL_MS;
  const live = usePolling<LiveData>(runId ? `/api/runs/${runId}/live` : null, pollMs);
  const error = runs.error ?? live.error;

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Onboard network QoE · live</h1>
          <p className="text-sm text-muted-foreground">Synthetic web-browsing traffic, measured and scored as it runs</p>
        </div>
        <ThemeToggle />
      </header>

      {/* One filter row above everything it scopes */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 rounded-xl px-4 py-3 ring-1 ring-foreground/10">
        <div className="flex min-w-0 items-center gap-2">
          <Label htmlFor="device">Device</Label>
          <Select value={device} onValueChange={(d) => { setDevice(d); setPicked(null); }}>
            <SelectTrigger id="device" className="w-[min(280px,70vw)]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_DEVICES}>All devices</SelectItem>
              {devices.data?.map((d) => (
                <SelectItem key={d.device_id} value={d.device_id}>
                  {d.device_id}{d.train_id ? ` · ${d.train_id}` : ""} ({d.runs} runs)
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex min-w-0 items-center gap-2">
          <Label htmlFor="run">Run</Label>
          <Select
            value={runId ?? undefined}
            onValueChange={(id) => { setPicked(id); setFollow(false); }}
            disabled={!runs.data?.length}
          >
            <SelectTrigger id="run" className="w-[min(420px,70vw)]">
              <SelectValue placeholder="No runs yet" />
            </SelectTrigger>
            <SelectContent>
              {runs.data?.map((run) => (
                <SelectItem key={run.test_run_id} value={run.test_run_id}>
                  {runLabel(run, device === ALL_DEVICES)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-2">
          <Switch id="follow" checked={follow} onCheckedChange={setFollow} />
          <Label htmlFor="follow">Follow latest run</Label>
        </div>
        <span className="text-xs text-muted-foreground sm:ml-auto">
          {live.data ? `Polling every ${pollMs / 1000}s · ${live.data.source === "live" ? "live aggregates" : "final summary"} · ${fmtTime(new Date().toISOString())}` : ""}
        </span>
      </div>

      {error && (
        <Alert variant="destructive">
          <CircleAlert />
          <AlertTitle>Cannot reach the dashboard API</AlertTitle>
          <AlertDescription>
            {error}. Is it running? <code>uv run uvicorn app.dashboard_api.main:app --port 8001</code>
          </AlertDescription>
        </Alert>
      )}

      {runs.data && runs.data.length === 0 && <EmptyState />}

      {live.data && (
        // Refetch keeps the frame: while a newly picked run loads, the previous one stays, dimmed.
        <main className={cn("space-y-4 transition-opacity", live.stale && "opacity-50")}>
          <div className="grid gap-4 md:grid-cols-2">
            <QoeCard qoe={live.data.qoe} source={live.data.source} />
            <RunDetails data={live.data} />
          </div>
          <KpiTiles data={live.data} />
          <div className="grid gap-4 lg:grid-cols-2">
            <LatencyChart series={live.data.series} bucketSeconds={live.data.bucket_seconds} />
            <RateChart series={live.data.series} bucketSeconds={live.data.bucket_seconds} />
          </div>
          <ThroughputChart series={live.data.series} bucketSeconds={live.data.bucket_seconds} />
          <RecentRequests requests={live.data.recent} />
        </main>
      )}

      <p className="text-xs text-muted-foreground">
        Live figures are aggregated in MongoDB (approximate percentiles); once a run finishes, the generator's exact
        summary is shown. The QoE score is a POC heuristic, not a validated model of passenger experience.
      </p>
    </div>
  );
}
