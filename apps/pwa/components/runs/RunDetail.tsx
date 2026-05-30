"use client";

import { resolveCheckpoint } from "@/lib/api/checkpoints";
import { ApiError } from "@/lib/api/client";
import { getRunDetail } from "@/lib/api/runs";
import type {
  RunActivity,
  RunDecision,
  RunDetail as RunDetailModel,
  RunPartialDeliverable,
  RunStatus,
  RunVerification,
} from "@/lib/api/types";
import { useSession } from "@/lib/auth/session";
import { useEventStream } from "@/lib/live-events/use-event-stream";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

/**
 * Run-detail surface — the Stitch "Triggered" screen made real. One
 * externally-triggered ExecutionRun, inspectable: what triggered it (the
 * connector source + the founder's Direction, read defensively out of the run's
 * free-form payload), its current status, the paused-run Decision block (the
 * blocking question + a Resolve affordance), how BSVibe checked the work, and a
 * link out to the Delivery Report when a deliverable exists.
 *
 * Composed client-side from REAL `GET /api/v1/runs/{id}/detail`. The decision
 * actions REUSE the checkpoint resolve mechanism (POST
 * /api/v1/checkpoints/{id}/resolve) — they do not reinvent resolution.
 * "Let it continue" / "Hold" map to the two resolve answers.
 *
 * States mirror the Delivery Report: loading / not-found (404, NOT an error) /
 * error / ready. A run with a sparse payload renders a calm minimal detail
 * (status only) rather than erroring.
 *
 * Deferred (no data behind them yet): the verbatim "issue quote" + a free-text
 * Safe-Mode rationale (the run payload doesn't carry them — we show the calm
 * Safe-Mode line + the decision's own rationale instead), and "Dismiss" (no
 * backing action — Hold / Let-it-continue are the two real resolve answers).
 */
type Loaded =
  | { state: "loading" }
  | { state: "error" }
  | { state: "not-found" }
  | { state: "ready"; detail: RunDetailModel };

export default function RunDetail({ runId }: { runId: string }) {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const t = useTranslations("run");

  const reload = useCallback(() => {
    setLoaded({ state: "loading" });
    getRunDetail(runId)
      .then((detail) => setLoaded({ state: "ready", detail }))
      .catch((error: unknown) => {
        // A 404 (run not in this workspace / unknown id) is a calm not-found,
        // NOT an error wall.
        if (error instanceof ApiError && error.status === 404) {
          setLoaded({ state: "not-found" });
        } else {
          setLoaded({ state: "error" });
        }
      });
  }, [runId]);

  useEffect(() => {
    let active = true;
    setLoaded({ state: "loading" });
    getRunDetail(runId)
      .then((detail) => {
        if (active) setLoaded({ state: "ready", detail });
      })
      .catch((error: unknown) => {
        if (!active) return;
        if (error instanceof ApiError && error.status === 404) {
          setLoaded({ state: "not-found" });
        } else {
          setLoaded({ state: "error" });
        }
      });
    return () => {
      active = false;
    };
  }, [runId]);

  // B16 — wake up when the backend signals THIS run reached a terminal
  // state or raised a decision, so the founder sees the new status without
  // a manual refresh. Filter by run_id so unrelated workspace events don't
  // thrash this surface.
  const session = useSession();
  const liveRefresh = useCallback(
    (payload: { run_id?: string }) => {
      if (payload.run_id === runId) reload();
    },
    [runId, reload],
  );
  useEventStream({
    token: session?.accessToken ?? null,
    onRunTerminal: liveRefresh,
    onDecisionPending: liveRefresh,
    // D6 — a mid-loop partial Deliverable for THIS run lands → refetch so the
    // streaming partials list appears in real time (not only on the verified
    // terminal). Other runs' partials are ignored (same per-run filter).
    onDeliverablePartial: liveRefresh,
  });

  return (
    <div className="run-detail">
      <Link className="run-detail__back" href="/brief">
        {t("back")}
      </Link>

      {loaded.state === "loading" && (
        <p className="run-detail__loading-note" aria-busy="true">
          {t("loadingNote")}
        </p>
      )}

      {loaded.state === "not-found" && (
        <section className="run-empty" aria-label={t("region")}>
          <p className="run-empty__line">{t("notFoundLine")}</p>
          <p className="run-empty__sub">
            <Link href="/brief">{t("backToBrief")}</Link>
          </p>
        </section>
      )}

      {loaded.state === "error" && (
        <section className="run-empty" aria-label={t("region")}>
          <p className="run-empty__line">{t("errorLine")}</p>
          <p className="run-empty__sub">{t("errorSub")}</p>
        </section>
      )}

      {loaded.state === "ready" && <DetailBody detail={loaded.detail} onResolved={reload} />}
    </div>
  );
}

/** Status word the founder reads — the real RunStatus vocabulary; an unknown
 *  value degrades to the raw status rather than throwing. */
function statusLabel(t: ReturnType<typeof useTranslations>, status: RunStatus): string {
  const known: RunStatus[] = ["open", "running", "review_ready", "shipped", "failed", "cancelled"];
  return known.includes(status) ? t(`statusLabel.${status}`) : status;
}

function DetailBody({
  detail,
  onResolved,
}: {
  detail: RunDetailModel;
  onResolved: () => void;
}) {
  const t = useTranslations("run");
  const {
    trigger,
    decisions,
    verification,
    deliverable_id,
    partial_deliverables,
    activities,
    timeline_source,
  } = detail;
  const title = trigger.intent_text?.trim() || t("untitled");
  // Only PENDING decisions are actionable; resolved ones are history.
  const pending = decisions.filter((d) => d.status === "pending");
  // D6 — defensive read: an older backend (or a missing field on a sparse
  // response) should not crash; default to an empty list.
  const partials: RunPartialDeliverable[] = partial_deliverables ?? [];

  return (
    <article className="run-detail__body">
      <header className="run-detail__header">
        <span className={`run-detail__status run-detail__status--${detail.status}`}>
          {statusLabel(t, detail.status)}
        </span>
        <h1 className="run-detail__title">{title}</h1>
      </header>

      <NextStep
        status={detail.status}
        hasPendingDecision={pending.length > 0}
        deliverableId={deliverable_id}
      />

      <TriggerBlock trigger={trigger} />

      <TimelineBlock activities={activities} source={timeline_source} />

      {pending.map((decision) => (
        <DecisionBlock key={decision.id} decision={decision} onResolved={onResolved} />
      ))}

      <VerificationBlock verification={verification} />

      <DeliverablesBlock partials={partials} verifiedFinalId={deliverable_id} />
    </article>
  );
}

/** D6 — the run's Deliverables: the streaming list of mid-loop partials
 *  (Synthesis §13: Deliver as a continuous side channel) and, when the run
 *  has reached the verified terminal, the verified-final Deliverable the
 *  founder taps for the Delivery Report. The two are visually distinguished
 *  (each partial gets its own `run-partial` row, the verified-final lives in
 *  its own bordered `run-deliverable` section with an explicit `--verified`
 *  modifier). A run with no partials AND no verified-final renders nothing
 *  (the pre-D6 sparse-state shape). */
function DeliverablesBlock({
  partials,
  verifiedFinalId,
}: {
  partials: RunPartialDeliverable[];
  verifiedFinalId: string | null;
}) {
  const t = useTranslations("run");
  if (partials.length === 0 && !verifiedFinalId) return null;
  return (
    <section className="run-deliverables" aria-label={t("deliverable")}>
      <h2 className="section-label">{t("deliverable")}</h2>
      {partials.length > 0 && (
        <ol className="run-partials">
          {partials.map((p) => (
            <li
              key={p.id}
              className="run-partial"
              data-partial="true"
              data-artifact-type={p.artifact_type}
            >
              <span className="run-partial__type">{p.artifact_type}</span>
              {p.summary ? <span className="run-partial__summary">{p.summary}</span> : null}
              {p.channel ? <span className="run-partial__channel">{p.channel}</span> : null}
            </li>
          ))}
        </ol>
      )}
      {verifiedFinalId && (
        <div className="run-deliverable run-deliverable--verified" data-verified="true">
          <Link className="run-deliverable__link" href={`/deliverables/${verifiedFinalId}`}>
            {t("viewReport")}
          </Link>
        </div>
      )}
    </section>
  );
}

/** The explicit NEXT STEP — the founder should always know what (if anything)
 *  they need to DO. A pending decision (the genuine blocker) takes priority over
 *  the run's status word; otherwise the status maps to a calm, honest line:
 *  review_ready → see the delivery report ; running/open → working ;
 *  shipped → all done ; failed/cancelled → a calm failure line. A deliverable
 *  link is attached when one exists so review_ready is one tap from the proof. */
function NextStep({
  status,
  hasPendingDecision,
  deliverableId,
}: {
  status: RunStatus;
  hasPendingDecision: boolean;
  deliverableId: string | null;
}) {
  const t = useTranslations("run");

  // The genuine blocker wins, regardless of the run's status word.
  if (hasPendingDecision) {
    return (
      <section className="run-next" aria-label={t("nextStep")}>
        <h2 className="section-label">{t("nextStep")}</h2>
        <p className="run-next__line run-next__line--decision">{t("nextDecision")}</p>
      </section>
    );
  }

  let line: string;
  if (status === "review_ready") line = t("nextReview");
  else if (status === "running" || status === "open") line = t("nextWorking");
  else if (status === "shipped") line = t("nextShipped");
  else if (status === "failed" || status === "cancelled") line = t("nextFailed");
  else line = t("nextWorking");

  // review_ready points one tap from the proof when a deliverable exists.
  const showReportLink = status === "review_ready" && Boolean(deliverableId);

  return (
    <section className={`run-next run-next--${status}`} aria-label={t("nextStep")}>
      <h2 className="section-label">{t("nextStep")}</h2>
      <p className="run-next__line">{line}</p>
      {showReportLink && deliverableId && (
        <Link className="run-next__link" href={`/deliverables/${deliverableId}`}>
          {t("nextReviewLink")}
        </Link>
      )}
    </section>
  );
}

/** The run's STORY — a calm "What I did" timeline of the ordered meaningful
 *  events (delivered / verified / settled / …). When the timeline was DERIVED
 *  (no real activity rows recorded), an honest note says it was reconstructed
 *  from what we have — we never fabricate a per-step log. An empty timeline
 *  renders nothing (a bare in-flight run has no story yet). */
function TimelineBlock({
  activities,
  source,
}: {
  activities: RunActivity[];
  source: RunDetailModel["timeline_source"];
}) {
  const t = useTranslations("run");
  if (activities.length === 0) return null;

  return (
    <section className="run-timeline" aria-label={t("timeline")}>
      <h2 className="section-label">{t("timeline")}</h2>
      <ol className="run-timeline__list">
        {activities.map((event, i) => (
          <li
            key={`${event.type}-${event.created_at}-${i}`}
            className={`run-timeline__event run-timeline__event--${event.type}`}
          >
            {event.label}
          </li>
        ))}
      </ol>
      {source === "derived" && <p className="run-timeline__note">{t("timelineDerived")}</p>}
    </section>
  );
}

function TriggerBlock({ trigger }: { trigger: RunDetailModel["trigger"] }) {
  const t = useTranslations("run");
  // "External" purely from the detail payload: a connector/webhook origin carries
  // a source (or a trigger_kind). Absent both → the founder started this Direct.
  // We never render an empty section: a Direct run gets an honest one-line note.
  const isExternal = Boolean(trigger.source || trigger.trigger_kind);
  return (
    <section className="run-trigger" aria-label={t("trigger")}>
      <h2 className="section-label">{t("trigger")}</h2>
      {isExternal ? (
        <>
          <p className="run-trigger__line">
            {trigger.source ? t("triggeredFrom", { source: trigger.source }) : null}
            {trigger.product ? (
              <span className="run-trigger__product">{trigger.product}</span>
            ) : null}
          </p>
          {/* Static reassurance — only for externally-originated runs (Safe Mode). */}
          <p className="run-trigger__safe-mode">{t("safeModeExternal")}</p>
        </>
      ) : (
        <p className="run-trigger__direct">{t("triggeredDirectly")}</p>
      )}
    </section>
  );
}

type ResolveState = "idle" | "resolving" | "resolved" | "error";

/** The paused-run Decision block: the blocking question + the agent's rationale
 *  + the calm Safe-Mode line, with "Let it continue" / "Hold" affordances wired
 *  to the checkpoint resolve mechanism. A failed resolve shows a calm inline
 *  error and stays actionable. */
function DecisionBlock({
  decision,
  onResolved,
}: {
  decision: RunDecision;
  onResolved: () => void;
}) {
  const t = useTranslations("run");
  const [state, setState] = useState<ResolveState>("idle");

  async function resolve(answer: string) {
    if (state === "resolving") return;
    setState("resolving");
    try {
      await resolveCheckpoint(decision.id, answer);
      setState("resolved");
      // Re-read the run so the resolved decision drops out + status updates.
      onResolved();
    } catch {
      setState("error");
    }
  }

  return (
    <section className="run-decision" aria-label={t("decision")}>
      <h2 className="section-label">{t("decision")}</h2>
      {decision.question && <p className="run-decision__question">{decision.question}</p>}
      <p className="run-decision__rationale">{decision.rationale || t("decisionRationale")}</p>

      {state === "resolved" ? (
        <p className="run-decision__resolved" aria-live="polite">
          {t("resolved")}
        </p>
      ) : (
        <div className="run-decision__actions">
          {state === "error" && (
            <span className="run-decision__error" aria-live="polite">
              {t("resolveError")}
            </span>
          )}
          <button
            type="button"
            className="run-decision__hold"
            onClick={() => resolve(t("hold"))}
            disabled={state === "resolving"}
          >
            {t("hold")}
          </button>
          <button
            type="button"
            className="run-decision__continue"
            onClick={() => resolve(t("letContinue"))}
            disabled={state === "resolving"}
          >
            {state === "resolving" ? t("resolving") : t("letContinue")}
          </button>
        </div>
      )}
    </section>
  );
}

function VerificationBlock({ verification }: { verification: RunVerification | null }) {
  const t = useTranslations("run");
  return (
    <section className="run-verification" aria-label={t("verification")}>
      <h2 className="section-label">{t("verification")}</h2>
      {verification ? (
        <span
          className={`run-verification__verdict run-verification__verdict--${verification.outcome}`}
        >
          {t(`verdictLabel.${verification.outcome}`)}
        </span>
      ) : (
        <p className="run-verification__empty">{t("noVerification")}</p>
      )}
    </section>
  );
}
