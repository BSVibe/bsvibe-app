/**
 * Pending-decisions count store.
 *
 * The Decisions surface ((app)/decisions) sets the live count (pending
 * checkpoints + canon proposals) whenever it loads / re-reads; the left-rail
 * and mobile nav subscribe to render a small badge. A plain external store
 * (same shape as lib/auth/session) keeps the badge reactive without a context
 * provider, and `useSyncExternalStore`'s server snapshot (always 0) keeps SSR
 * hydration clean — no set-state-in-effect.
 */

import { useSyncExternalStore } from "react";

let current = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): number {
  return current;
}

function getServerSnapshot(): number {
  return 0;
}

/** Set the live pending-decisions count (no-op if unchanged → no needless
 *  re-render of the nav). Clamped at 0. */
export function setPendingDecisionsCount(count: number): void {
  const next = Math.max(0, count);
  if (next === current) return;
  current = next;
  emit();
}

/** The pending-decisions count, reactively. 0 on the server + first hydration. */
export function usePendingDecisionsCount(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
