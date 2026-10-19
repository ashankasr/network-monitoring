import type { RecentRequest } from "@/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fmtBytes, fmtMbps, fmtMs, fmtTime, urlPath } from "@/format";
import { OutcomeLabel } from "./StatusBadge";

export function RecentRequests({ requests }: { requests: RecentRequest[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent requests</CardTitle>
        <CardDescription>The latest {requests.length} measurements, newest first</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Time</TableHead>
              <TableHead>Request</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead className="text-right">HTTP</TableHead>
              <TableHead className="text-right">Latency</TableHead>
              <TableHead className="text-right">TTFB</TableHead>
              <TableHead className="text-right">Total</TableHead>
              <TableHead className="text-right">Size</TableHead>
              <TableHead className="text-right">Throughput</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {requests.map((r) => (
              <TableRow key={r.request_id} title={r.error ?? undefined}>
                <TableCell className="tabular-nums">{fmtTime(r.timestamp)}</TableCell>
                <TableCell className="max-w-72 truncate font-mono text-xs">{urlPath(r.url)}</TableCell>
                <TableCell><OutcomeLabel outcome={r.outcome} /></TableCell>
                <TableCell className="text-right tabular-nums">{r.status_code ?? "—"}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtMs(r.latency_ms)}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtMs(r.time_to_first_byte_ms)}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtMs(r.total_duration_ms)}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtBytes(r.bytes_received)}</TableCell>
                <TableCell className="text-right tabular-nums">{fmtMbps(r.throughput_mbps)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
