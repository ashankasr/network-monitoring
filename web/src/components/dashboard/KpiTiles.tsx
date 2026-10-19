import type { LiveData } from "@/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fmtBytes, fmtMbps, fmtMs, fmtNumber, fmtPct } from "@/format";

interface Tile {
  label: string;
  value: string;
  detail: string;
}

function tiles({ stats }: LiveData): Tile[] {
  const { requests, latency_ms: latency, throughput_mbps: throughput } = stats;
  return [
    {
      label: "Requests",
      value: fmtNumber(requests.total),
      detail: `${fmtNumber(requests.achieved_rps, 1)} req/s${requests.cancelled ? ` · ${requests.cancelled} cancelled` : ""}`,
    },
    {
      label: "Success rate",
      value: fmtPct(requests.success_pct),
      detail: `${fmtNumber(requests.failed)} failed`,
    },
    {
      label: "Latency p95",
      value: fmtMs(latency?.p95),
      detail: `p50 ${fmtMs(latency?.p50)} · p99 ${fmtMs(latency?.p99)}`,
    },
    {
      label: "Throughput",
      value: fmtMbps(throughput.aggregate),
      detail: `${fmtNumber(throughput.samples)} transfers ≥ 64 KiB`,
    },
    {
      label: "Data received",
      value: fmtBytes(stats.bytes_received_total),
      detail: "response bodies",
    },
  ];
}

export function KpiTiles({ data }: { data: LiveData }) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
      {tiles(data).map((tile) => (
        <Card key={tile.label} size="sm">
          <CardHeader>
            <CardDescription>{tile.label}</CardDescription>
            <CardTitle className="text-2xl font-semibold">{tile.value}</CardTitle>
          </CardHeader>
          <CardContent className="text-xs text-muted-foreground">{tile.detail}</CardContent>
        </Card>
      ))}
    </div>
  );
}
