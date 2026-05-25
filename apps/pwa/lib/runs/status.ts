import type { ActivityTone, RunStatus } from "@/lib/api/types";

/**
 * Shared run-status → UI mapping for the Work-Home surface (the live "Working on
 * now" hero + the work stream). `tone` is the lone colour signal (UX §5 —
 * colour carries status only); `labelKey` is an i18n key under the `brief`
 * namespace so the label is translated in the component, not hardcoded here.
 */
export const STATUS_TONE: Record<RunStatus, ActivityTone> = {
  open: "working",
  running: "working",
  review_ready: "review",
  shipped: "shipped",
  failed: "failed",
  cancelled: "neutral",
};

export const STATUS_LABEL_KEY: Record<RunStatus, string> = {
  open: "statusJustStarted",
  running: "statusWorking",
  review_ready: "statusReview",
  shipped: "statusShipped",
  failed: "statusFailed",
  cancelled: "statusStood",
};

/** True for in-flight work (the "Working on now" set). */
export function isActiveStatus(status: RunStatus): boolean {
  return status === "open" || status === "running";
}
