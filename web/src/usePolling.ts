import { useEffect, useState } from "react";
import { getJson } from "./api";

interface PollState<T> {
  url: string | null;
  data: T | undefined;
  error: string | null;
}

export interface Polled<T> {
  data: T | undefined;
  error: string | null;
  /** True while the shown data belongs to a previous URL (e.g. right after switching runs). */
  stale: boolean;
}

/**
 * Fetches `url` now and then every `intervalMs` (the next poll starts after the previous one ends,
 * so slow responses never pile up). On a URL change, the previous data stays visible until the
 * new response arrives, so the layout never flashes empty.
 */
export function usePolling<T>(url: string | null, intervalMs: number): Polled<T> {
  const [state, setState] = useState<PollState<T>>({ url: null, data: undefined, error: null });

  useEffect(() => {
    if (url === null) return;
    const controller = new AbortController();
    let timer: number | undefined;

    const poll = async () => {
      try {
        const data = await getJson<T>(url, controller.signal);
        setState({ url, data, error: null });
      } catch (err) {
        if (controller.signal.aborted) return;
        setState((prev) => ({ ...prev, error: err instanceof Error ? err.message : String(err) }));
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, intervalMs);
    };
    void poll();

    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [url, intervalMs]);

  return { data: state.data, error: state.error, stale: state.url !== url };
}
