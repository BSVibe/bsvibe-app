/**
 * React hook for the backend SSE live-events channel (B16).
 *
 * The browser `EventSource` API can NOT send custom headers, so authentication
 * goes through a `?token=` query parameter (per the `eventsource-sse-auth-trap`
 * lift). The hook opens a connection to `${NEXT_PUBLIC_BACKEND_URL}/api/v1/events/stream`,
 * registers per-event-type listeners that decode the JSON payload, and tears
 * the connection down on unmount.
 *
 * The hook is intentionally a thin wake-up bridge. Callers pass `onDecisionPending`
 * / `onRunTerminal` / `onDeliveryQueued` handlers that typically just re-run a
 * `getBrief()` / `listPendingDecisions()` / `getRunDetail()` fetch — the SSE
 * channel carries no business state, just the signal to refetch.
 *
 * Errors during JSON parsing of an event payload are swallowed (the channel is
 * best-effort and the next event refetches anyway). Connection errors are left
 * to the browser's built-in EventSource retry (it reconnects automatically).
 */
import { useEffect, useRef } from "react";

/** Shape of the JSON payload the backend ships on each SSE message. */
export interface LiveEventPayload {
  // Tiny "wake up" envelope — ids only, never LLM content. The PWA uses these
  // as a signal to refetch the affected surface.
  decision_id?: string;
  run_id?: string;
  delivery_id?: string;
  checkpoint_id?: string;
  resource_type?: string;
  resource_id?: string;
  occurred_at?: string;
  event_id?: string;
  outcome?: string;
}

export interface UseEventStreamOptions {
  /** Caller's Supabase access token. `null` skips opening the connection
   *  (e.g. before the session has hydrated). */
  token: string | null;
  onDecisionPending?: (payload: LiveEventPayload) => void;
  onRunTerminal?: (payload: LiveEventPayload) => void;
  onDeliveryQueued?: (payload: LiveEventPayload) => void;
}

/**
 * Open a live-events SSE connection for the duration of the component's
 * lifecycle. Each registered handler fires once per matching event.
 *
 * The hook stores the handlers in refs so a parent that re-creates closures
 * on every render (the common React pattern) doesn't tear down + reopen the
 * connection on every render — the EventSource is opened once per `token`
 * change.
 */
export function useEventStream({
  token,
  onDecisionPending,
  onRunTerminal,
  onDeliveryQueued,
}: UseEventStreamOptions): void {
  // Keep latest handlers in refs so the effect can call the current version
  // without re-opening the EventSource on every render.
  const decisionPendingRef = useRef(onDecisionPending);
  const runTerminalRef = useRef(onRunTerminal);
  const deliveryQueuedRef = useRef(onDeliveryQueued);
  decisionPendingRef.current = onDecisionPending;
  runTerminalRef.current = onRunTerminal;
  deliveryQueuedRef.current = onDeliveryQueued;

  useEffect(() => {
    if (!token) return;
    // SSR / vitest with no global EventSource → no-op (the hook is a no-op
    // outside the browser).
    if (typeof EventSource === "undefined") return;

    const base = process.env.NEXT_PUBLIC_BACKEND_URL ?? "";
    const url = `${base}/api/v1/events/stream?token=${encodeURIComponent(token)}`;
    const source = new EventSource(url);

    const parse = (raw: string): LiveEventPayload | null => {
      try {
        return JSON.parse(raw) as LiveEventPayload;
      } catch {
        return null;
      }
    };

    const onDecision = (event: MessageEvent): void => {
      const payload = parse(event.data);
      if (payload && decisionPendingRef.current) decisionPendingRef.current(payload);
    };
    const onRun = (event: MessageEvent): void => {
      const payload = parse(event.data);
      if (payload && runTerminalRef.current) runTerminalRef.current(payload);
    };
    const onDelivery = (event: MessageEvent): void => {
      const payload = parse(event.data);
      if (payload && deliveryQueuedRef.current) deliveryQueuedRef.current(payload);
    };

    source.addEventListener("decision.pending", onDecision as EventListener);
    source.addEventListener("run.terminal", onRun as EventListener);
    source.addEventListener("delivery.queued", onDelivery as EventListener);

    return () => {
      source.removeEventListener("decision.pending", onDecision as EventListener);
      source.removeEventListener("run.terminal", onRun as EventListener);
      source.removeEventListener("delivery.queued", onDelivery as EventListener);
      source.close();
    };
  }, [token]);
}
