import type { Qoe, QoeComponent } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { fmtNumber } from "@/format";
import { cn } from "@/lib/utils";

const COMPONENTS: { key: QoeComponent; label: string }[] = [
  { key: "latency", label: "Latency" },
  { key: "reliability", label: "Reliability" },
  { key: "throughput", label: "Throughput" },
];

/** Meter fill carries severity; the track stays a light step of the accent ramp. */
function meterClass(score: number): string {
  if (score < 50) return "[&_[data-slot=progress-indicator]]:bg-status-critical";
  if (score < 75) return "[&_[data-slot=progress-indicator]]:bg-status-warning";
  return "[&_[data-slot=progress-indicator]]:bg-chart-1";
}

export function QoeCard({ qoe, source }: { qoe: Qoe; source: "summary" | "live" }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>QoE score</CardTitle>
        <CardDescription>{qoe.disclaimer}</CardDescription>
        <CardAction>
          <Badge variant="secondary">{qoe.qoe_model} · {source === "live" ? "live" : "final"}</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="text-6xl font-semibold tracking-tight">
          {fmtNumber(qoe.qoe_score, 1)}
          <span className="ml-1 text-xl font-medium text-muted-foreground">/ 100</span>
        </div>
        <div className="space-y-3">
          {COMPONENTS.map(({ key, label }) => {
            const value = qoe.components[key];
            const weight = Math.round((qoe.weights[key] ?? 0) * 100);
            return (
              <div key={key} className="grid grid-cols-[96px_1fr_40px] items-center gap-3 text-sm">
                <span className="text-muted-foreground">
                  {label} <span className="text-xs">({weight}%)</span>
                </span>
                <Progress
                  value={value ?? 0}
                  aria-label={`${label} score`}
                  className={cn("h-2 bg-chart-1-track", value !== null && meterClass(value), value === null && "opacity-40")}
                />
                <span className="text-right tabular-nums">{value === null ? "n/a" : fmtNumber(value)}</span>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
