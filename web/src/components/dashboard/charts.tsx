import { useState, type ReactNode } from "react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, Rectangle, XAxis, YAxis } from "recharts";

import type { SeriesPoint } from "@/api";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, type ChartConfig } from "@/components/ui/chart";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { fmtMbps, fmtMs, fmtNumber, fmtTime } from "@/format";

// Recharts animations would replay on every 2 s poll; live charts render without them.
const LIVE = { isAnimationActive: false } as const;
const AXIS_TICK = { fontSize: 11 };
const ACTIVE_DOT = { r: 4, stroke: "var(--card)", strokeWidth: 2 };

/** Recharts 3 sorts legend and tooltip items alphabetically by default; keep the declared series order. */
function byOrder(keys: string[]) {
  return (item: { dataKey?: unknown }) => keys.indexOf(String(item.dataKey));
}
const LATENCY_ORDER = byOrder(["latency_p95", "latency_p50"]);
const RATE_ORDER = byOrder(["ok", "failed"]);

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour12: false });
}

// --- Shared card with a Chart / Table toggle (every chart has a table view) ---

interface Column {
  label: string;
  value: (p: SeriesPoint) => string;
}

function ChartCard({
  title,
  description,
  series,
  columns,
  children,
}: {
  title: string;
  description: string;
  series: SeriesPoint[];
  columns: Column[];
  children: ReactNode;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
        <CardAction>
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            value={view}
            onValueChange={(v) => v && setView(v as "chart" | "table")}
            aria-label={`${title} view`}
          >
            <ToggleGroupItem value="chart">Chart</ToggleGroupItem>
            <ToggleGroupItem value="table">Table</ToggleGroupItem>
          </ToggleGroup>
        </CardAction>
      </CardHeader>
      <CardContent>
        {series.length === 0 ? (
          <div className="flex h-[240px] items-center justify-center text-sm text-muted-foreground">
            Waiting for data…
          </div>
        ) : view === "chart" ? (
          children
        ) : (
          <div className="h-[240px] overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Time</TableHead>
                  {columns.map((c) => <TableHead key={c.label} className="text-right">{c.label}</TableHead>)}
                </TableRow>
              </TableHeader>
              <TableBody>
                {[...series].reverse().map((p) => (
                  <TableRow key={p.t}>
                    <TableCell className="tabular-nums">{fmtTime(p.t)}</TableCell>
                    {columns.map((c) => (
                      <TableCell key={c.label} className="text-right tabular-nums">{c.value(p)}</TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// --- Tooltip: the value leads, the series name follows, keyed by a short line in the series color ---

interface TooltipItem {
  dataKey?: unknown;
  value?: unknown;
  color?: string;
  payload?: SeriesPoint;
}

function SeriesTooltip({
  active,
  payload,
  config,
  format,
}: {
  active?: boolean;
  payload?: readonly TooltipItem[];
  config: ChartConfig;
  format: (value: number) => string;
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="grid min-w-36 gap-1.5 rounded-lg border border-border/50 bg-background px-2.5 py-1.5 text-xs shadow-xl">
      {point && <div className="text-muted-foreground">{fmtTime(point.t)}</div>}
      {payload.map((item) => {
        const key = String(item.dataKey);
        return (
          <div key={key} className="flex items-center gap-2">
            <span className="h-0.5 w-3 shrink-0 rounded-full" style={{ background: item.color }} />
            <span className="font-medium tabular-nums text-foreground">
              {typeof item.value === "number" ? format(item.value) : "—"}
            </span>
            <span className="text-muted-foreground">{config[key]?.label}</span>
          </div>
        );
      })}
    </div>
  );
}

// --- Latency: p50 and p95 lines on one axis ---

const latencyConfig = {
  latency_p95: { label: "p95", color: "var(--chart-2)" },
  latency_p50: { label: "p50", color: "var(--chart-1)" },
} satisfies ChartConfig;

/** Direct label at the end of a line, in text ink (never the series color). p95 sits above, p50 below. */
function endLabel(name: string, lastIndex: number, dy: number) {
  return (props: { x?: number | string; y?: number | string; index?: number }) => {
    if (props.index !== lastIndex || props.x === undefined || props.y === undefined) return <g />;
    return (
      <text x={Number(props.x) + 6} y={Number(props.y)} dy={dy} fontSize={11} fill="var(--muted-foreground)">
        {name}
      </text>
    );
  };
}

export function LatencyChart({ series, bucketSeconds }: { series: SeriesPoint[]; bucketSeconds: number }) {
  const last = series.length - 1;
  return (
    <ChartCard
      title="Network latency (ms)"
      description={`TTFB minus server time, successful requests · per ${bucketSeconds}s`}
      series={series}
      columns={[
        { label: "p50", value: (p) => fmtMs(p.latency_p50) },
        { label: "p95", value: (p) => fmtMs(p.latency_p95) },
      ]}
    >
      <ChartContainer config={latencyConfig} className="aspect-auto h-[240px] w-full">
        <LineChart data={series} margin={{ top: 8, right: 36, left: 4, bottom: 0 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="t" tickFormatter={clock} tickLine={false} axisLine={false} minTickGap={48} tick={AXIS_TICK} />
          <YAxis tickLine={false} axisLine={false} width={40} tick={AXIS_TICK} tickFormatter={(v: number) => fmtNumber(v)} />
          <ChartTooltip
            cursor={{ stroke: "var(--border)" }}
            itemSorter={LATENCY_ORDER}
            content={(props) => (
              <SeriesTooltip active={props.active} payload={props.payload as readonly TooltipItem[]} config={latencyConfig} format={fmtMs} />
            )}
          />
          <ChartLegend verticalAlign="top" itemSorter={LATENCY_ORDER} content={<ChartLegendContent className="justify-start" />} />
          <Line dataKey="latency_p95" stroke="var(--color-latency_p95)" strokeWidth={2} dot={false}
            activeDot={ACTIVE_DOT} label={endLabel("p95", last, -4)} {...LIVE} />
          <Line dataKey="latency_p50" stroke="var(--color-latency_p50)" strokeWidth={2} dot={false}
            activeDot={ACTIVE_DOT} label={endLabel("p50", last, 8)} {...LIVE} />
        </LineChart>
      </ChartContainer>
    </ChartCard>
  );
}

// --- Request rate: successful + failed, stacked ---

const rateConfig = {
  ok: { label: "Successful", color: "var(--chart-1)" },
  failed: { label: "Failed", color: "var(--status-critical)" },
} satisfies ChartConfig;

type BarShapeProps = { x?: number; y?: number; width?: number; height?: number; fill?: string; payload?: SeriesPoint };

/** Bottom segment: rounded data-end only when nothing is stacked on top of it. */
function okShape(props: unknown) {
  const p = props as BarShapeProps;
  return <Rectangle {...p} radius={p.payload && p.payload.failed > 0 ? 0 : [4, 4, 0, 0]} />;
}

/** Top segment: rounded data-end, with a 2px surface gap separating it from the segment below. */
function failedShape(props: unknown) {
  const p = props as BarShapeProps;
  return <Rectangle {...p} height={Math.max(0, (p.height ?? 0) - 2)} radius={[4, 4, 0, 0]} />;
}

export function RateChart({ series, bucketSeconds }: { series: SeriesPoint[]; bucketSeconds: number }) {
  const perBucket = (n: number) => fmtNumber(n);
  return (
    <ChartCard
      title="Requests"
      description={`Requests started per ${bucketSeconds}s bucket`}
      series={series}
      columns={[
        { label: "Successful", value: (p) => fmtNumber(p.ok) },
        { label: "Failed", value: (p) => fmtNumber(p.failed) },
        { label: "req/s", value: (p) => fmtNumber(p.rps, 1) },
      ]}
    >
      <ChartContainer config={rateConfig} className="aspect-auto h-[240px] w-full">
        <BarChart data={series} margin={{ top: 8, right: 8, left: 4, bottom: 0 }} barCategoryGap="20%">
          <CartesianGrid vertical={false} />
          <XAxis dataKey="t" tickFormatter={clock} tickLine={false} axisLine={false} minTickGap={48} tick={AXIS_TICK} />
          <YAxis tickLine={false} axisLine={false} width={44} tick={AXIS_TICK} allowDecimals={false} />
          <ChartTooltip
            cursor={{ fill: "var(--muted)" }}
            itemSorter={RATE_ORDER}
            content={(props) => (
              <SeriesTooltip active={props.active} payload={props.payload as readonly TooltipItem[]} config={rateConfig} format={perBucket} />
            )}
          />
          <ChartLegend verticalAlign="top" itemSorter={RATE_ORDER} content={<ChartLegendContent className="justify-start" />} />
          <Bar dataKey="ok" stackId="requests" fill="var(--color-ok)" maxBarSize={24} shape={okShape} {...LIVE} />
          <Bar dataKey="failed" stackId="requests" fill="var(--color-failed)" maxBarSize={24} shape={failedShape} {...LIVE} />
        </BarChart>
      </ChartContainer>
    </ChartCard>
  );
}

// --- Throughput: single series, so the title names it and there is no legend box ---

const throughputConfig = {
  throughput_mbps: { label: "Throughput", color: "var(--chart-1)" },
} satisfies ChartConfig;

export function ThroughputChart({ series, bucketSeconds }: { series: SeriesPoint[]; bucketSeconds: number }) {
  return (
    <ChartCard
      title="Throughput"
      description={`Transfer-phase Mbps, bodies ≥ 64 KiB · per ${bucketSeconds}s · on localhost this measures the host, not a network`}
      series={series}
      columns={[{ label: "Mbps", value: (p) => fmtMbps(p.throughput_mbps) }]}
    >
      <ChartContainer config={throughputConfig} className="aspect-auto h-[240px] w-full">
        <AreaChart data={series} margin={{ top: 8, right: 8, left: 4, bottom: 0 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="t" tickFormatter={clock} tickLine={false} axisLine={false} minTickGap={48} tick={AXIS_TICK} />
          <YAxis tickLine={false} axisLine={false} width={52} tick={AXIS_TICK}
            tickFormatter={(v: number) => fmtNumber(v)} />
          <ChartTooltip
            cursor={{ stroke: "var(--border)" }}
            content={(props) => (
              <SeriesTooltip active={props.active} payload={props.payload as readonly TooltipItem[]} config={throughputConfig} format={fmtMbps} />
            )}
          />
          <Area dataKey="throughput_mbps" type="linear" stroke="var(--color-throughput_mbps)" strokeWidth={2}
            fill="var(--color-throughput_mbps)" fillOpacity={0.1} activeDot={ACTIVE_DOT} {...LIVE} />
        </AreaChart>
      </ChartContainer>
    </ChartCard>
  );
}
