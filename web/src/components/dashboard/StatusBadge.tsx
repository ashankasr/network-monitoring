import { CircleCheck, CircleHelp, CircleX, Radio, TriangleAlert, type LucideIcon } from "lucide-react";

import type { RecentRequest, RunStatus } from "@/api";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

// Status is never conveyed by color alone: every state has an icon and a label.
const RUN_STATUS: Record<RunStatus, { icon: LucideIcon; label: string; iconClass: string; title: string }> = {
  running: { icon: Radio, label: "Running", iconClass: "text-chart-1 animate-pulse motion-reduce:animate-none", title: "Receiving live events" },
  completed: { icon: CircleCheck, label: "Completed", iconClass: "text-status-good", title: "Finished; figures are the generator's final summary" },
  interrupted: { icon: TriangleAlert, label: "Interrupted", iconClass: "text-status-warning", title: "Stopped early (Ctrl+C)" },
  stale: { icon: CircleHelp, label: "No updates", iconClass: "text-status-serious", title: "Marked running, but no events for over a minute; the generator may have been killed" },
  error: { icon: CircleX, label: "Error", iconClass: "text-status-critical", title: "Ended with an internal error" },
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const { icon: Icon, label, iconClass, title } = RUN_STATUS[status] ?? RUN_STATUS.error;
  return (
    <Badge variant="outline" title={title} className="gap-1.5">
      <Icon className={cn("size-3.5", iconClass)} aria-hidden="true" />
      {label}
    </Badge>
  );
}

const OUTCOME: Record<RecentRequest["outcome"], { icon: LucideIcon; label: string; className: string }> = {
  success: { icon: CircleCheck, label: "Success", className: "text-status-good-text" },
  failure: { icon: CircleX, label: "Failed", className: "text-status-critical" },
  cancelled: { icon: CircleHelp, label: "Cancelled", className: "text-muted-foreground" },
};

export function OutcomeLabel({ outcome }: { outcome: RecentRequest["outcome"] }) {
  const { icon: Icon, label, className } = OUTCOME[outcome];
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <Icon className="size-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}
