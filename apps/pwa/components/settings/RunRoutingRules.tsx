"use client";

import { listAccounts } from "@/lib/api/accounts";
import { ApiError } from "@/lib/api/client";
import {
  createRunRoutingRule,
  deleteRunRoutingRule,
  listRunRoutingRules,
  updateRunRoutingRule,
} from "@/lib/api/run-routing";
import type { ModelAccount, RunRoutingRule } from "@/lib/api/types";
import { callerDisplay } from "@/lib/routing-caller-labels";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * Settings → Models → ROUTING (Lift N5). The founder authors routing as a table
 * of rows, each `[NL condition] [model]`:
 *
 *  - Condition — a free-text natural-language phrase the founder types (e.g.
 *    "복잡한 작업", "마케팅 관련", "한국어 요청"), stored on the rule as `source_text`.
 *    The backend compiles it into the structured caller_id / conditions on save.
 *  - Model — the target model, picked from the workspace's registered accounts.
 *
 * No "describe your routing" NL-compile panel; no caller / dimension dropdowns.
 * Legacy rules (`source_text: null`) show their human {@link matchLabel} in the
 * condition column. The catch-all default lives ONLY in the "Default model"
 * picker above (is_default rules are hidden here to avoid double display).
 */
type ListState = { data: RunRoutingRule[]; failed: boolean } | null;
type Translate = ReturnType<typeof useTranslations>;

/** Resolve a rule's `target` (a litellm_model id, or a legacy account id) to a
 *  friendly account label. Falls back to the raw target when unknown. */
function friendlyTarget(target: string, accounts: ModelAccount[]): string {
  const acct = accounts.find((a) => a.litellm_model === target || a.id === target);
  return acct ? acct.label : target;
}

/** Compact, human operator glyphs for the legacy-rule condition preview. */
const OPERATOR_SYMBOL: Record<string, string> = {
  eq: "=",
  ne: "≠",
  gt: ">",
  lt: "<",
  gte: "≥",
  lte: "≤",
};

function formatConditionValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

/** One legacy condition in human terms. A `classified_intent` condition reads as
 *  "<value> (category)"; anything else reads "<field> <op> <value>". */
function conditionLabel(
  cond: { field: string; operator: string; value?: unknown },
  t: Translate,
): string {
  if (cond.field === "classified_intent") {
    return t("dim.category", { name: formatConditionValue(cond.value) });
  }
  const op = OPERATOR_SYMBOL[cond.operator] ?? cond.operator;
  return `${cond.field} ${op} ${formatConditionValue(cond.value)}`.trim();
}

/** The CONDITION column for a LEGACY (source_text-null) rule in human terms: a
 *  caller (execution stage) via the localized label, else the first condition,
 *  else the "all work" catch-all label. Keeps the founder out of raw JSON. */
function matchLabel(
  callerId: string | null | undefined,
  conditions: ReadonlyArray<{ field: string; operator: string; value?: unknown }> | undefined,
  t: Translate,
): string {
  if (callerId) return callerDisplay(callerId, t);
  if (conditions && conditions.length > 0) return conditionLabel(conditions[0], t);
  return t("matchAny");
}

/** The condition text a rule shows: the verbatim NL `source_text` when set, else
 *  the human {@link matchLabel} for a legacy structured rule. */
function conditionText(rule: RunRoutingRule, t: Translate): string {
  if (rule.source_text) return rule.source_text;
  return matchLabel(rule.caller_id, rule.conditions, t);
}

/** The select value for a target — always the litellm_model. A legacy rule whose
 *  target is an account id is normalised to that account's litellm_model. */
function targetSelectValue(target: string, accounts: ModelAccount[]): string {
  const byId = accounts.find((a) => a.id === target);
  return byId ? byId.litellm_model : target;
}

export default function RunRoutingRules() {
  const [list, setList] = useState<ListState>(null);
  const [accounts, setAccounts] = useState<ModelAccount[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const t = useTranslations("settings.models.routing");

  async function load() {
    try {
      setList({ data: await listRunRoutingRules(), failed: false });
    } catch {
      setList({ data: [], failed: true });
    }
  }

  useEffect(() => {
    let active = true;
    listRunRoutingRules()
      .then((data) => active && setList({ data, failed: false }))
      .catch(() => active && setList({ data: [], failed: true }));
    listAccounts()
      .then((data) => active && setAccounts(data.filter((a) => a.is_active)))
      .catch(() => active && setAccounts([]));
    return () => {
      active = false;
    };
  }, []);

  // The catch-all default is the "Default model" picker above — hide is_default
  // rules here so the default isn't displayed in two places.
  const visible = list && !list.failed ? list.data.filter((r) => !r.is_default) : [];

  return (
    <section className="routing" aria-label={t("sectionLabel")}>
      <header className="routing__head">
        <h2 className="section-label">{t("sectionHeading")}</h2>
        {visible.length > 0 ? <span className="routing__count">{visible.length}</span> : null}
      </header>
      <p className="routing__lede">{t("lede")}</p>

      <div className="routing__add">
        {!showAdd ? (
          <button type="button" className="settings-add-toggle" onClick={() => setShowAdd(true)}>
            {t("addToggle")}
          </button>
        ) : (
          <div className="settings-add-panel">
            <RuleRowEditor
              accounts={accounts}
              onDone={() => {
                load();
                setShowAdd(false);
              }}
              onCancel={() => setShowAdd(false)}
            />
          </div>
        )}
      </div>

      {list === null ? (
        <p className="routing__loading" aria-busy="true">
          {t("loading")}
        </p>
      ) : list.failed ? (
        <p className="routing__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : visible.length === 0 ? (
        <p className="routing__empty">{t("empty")}</p>
      ) : (
        <ul className="routing__list" aria-label={t("listLabel")}>
          <li className="routing-row routing-row--head" aria-hidden="true">
            <span className="routing-row__col-head">{t("colCondition")}</span>
            <span className="routing-row__col-head">{t("colModel")}</span>
          </li>
          {visible.map((rule) => (
            <RunRoutingRuleRow key={rule.id} rule={rule} accounts={accounts} onChanged={load} />
          ))}
        </ul>
      )}
    </section>
  );
}

type RowState = "idle" | "editing" | "confirming" | "removing" | "error";

/** One rule as a two-column row `[condition] [model]` with Edit + confirm-gated
 *  Remove. Edit swaps the row body for an inline {@link RuleRowEditor}. */
function RunRoutingRuleRow({
  rule,
  accounts,
  onChanged,
}: {
  rule: RunRoutingRule;
  accounts: ModelAccount[];
  onChanged: () => void;
}) {
  const [state, setState] = useState<RowState>("idle");
  const t = useTranslations("settings.models.routing");

  async function confirmRemove() {
    if (state === "removing") return;
    setState("removing");
    try {
      await deleteRunRoutingRule(rule.id);
      onChanged();
    } catch {
      setState("error");
    }
  }

  if (state === "editing") {
    return (
      <li className="routing-row routing-row--editing">
        <RuleRowEditor
          rule={rule}
          accounts={accounts}
          onDone={() => {
            setState("idle");
            onChanged();
          }}
          onCancel={() => setState("idle")}
        />
      </li>
    );
  }

  return (
    <li className="routing-row">
      <span className="routing-row__condition" title={rule.source_text ?? undefined}>
        {conditionText(rule, t)}
      </span>
      <span className="routing-row__model">{friendlyTarget(rule.target, accounts)}</span>

      <div className="routing-row__actions">
        {rule.is_active ? null : (
          <span className="routing-card__chip routing-card__chip--inactive">{t("inactive")}</span>
        )}
        {state === "error" ? (
          <span className="routing-card__error" aria-live="polite">
            {t("removeError")}
          </span>
        ) : null}

        {state === "confirming" || state === "removing" ? (
          <>
            <button
              type="button"
              className="routing-card__danger"
              onClick={confirmRemove}
              disabled={state === "removing"}
            >
              {state === "removing" ? t("removing") : t("confirmRemove")}
            </button>
            <button
              type="button"
              className="routing-card__ghost"
              onClick={() => setState("idle")}
              disabled={state === "removing"}
            >
              {t("cancel")}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="routing-card__edit"
              onClick={() => setState("editing")}
            >
              {t("edit")}
            </button>
            <button
              type="button"
              className="routing-card__remove"
              onClick={() => setState("confirming")}
            >
              {t("remove")}
            </button>
          </>
        )}
      </div>
    </li>
  );
}

type EditorState = "idle" | "submitting" | "error";

/**
 * The minimal row editor — a text input for the NL condition + a `<select>` for
 * the model. Adds (POST `{name, source_text, target}`) or edits (PATCH
 * `{source_text, target}`) a rule.
 *
 * Failures are surfaced with the RIGHT cause, which the backend now distinguishes:
 *
 *  - 502/503 — the compile model was unreachable (dispatch / transport). NOT the
 *    founder's wording. Say so and invite a retry.
 *  - 422 — the model answered but the phrase compiled to nothing → rephrase hint.
 *
 * Conflating them is what shipped: an unwired backend dependency told every
 * founder that their perfectly good condition was uninterpretable.
 */
function RuleRowEditor({
  rule,
  accounts,
  onDone,
  onCancel,
}: {
  rule?: RunRoutingRule;
  accounts: ModelAccount[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const editing = rule !== undefined;
  const t = useTranslations("settings.models.routing");
  // A LEGACY rule (structured / caller-keyed) has `source_text: null`, so seeding
  // the input from it left the condition box BLANK on Edit — the row displayed
  // fine, then emptied the instant you touched it. Seed the SAME human text the
  // row shows (`conditionText`), so the box is never blank and saving round-trips
  // the visible condition through the compiler.
  const [condition, setCondition] = useState(rule ? conditionText(rule, t) : "");
  const [target, setTarget] = useState(rule ? targetSelectValue(rule.target, accounts) : "");
  const [state, setState] = useState<EditorState>("idle");
  const [errorText, setErrorText] = useState<string | null>(null);

  const ready = condition.trim().length > 0 && target.trim().length > 0;

  /** 502/503 → we couldn't REACH the model (infrastructure; retry). 422 → the
   *  phrase compiled to nothing valid (rephrase). Anything else → generic. */
  function messageFor(error: unknown): string {
    if (error instanceof ApiError && (error.status === 502 || error.status === 503)) {
      return t("modelUnreachable");
    }
    if (error instanceof ApiError && error.status === 422) return t("rephrase");
    return editing ? t("saveError") : t("addError");
  }

  async function submit() {
    if (state === "submitting" || !ready) return;
    setState("submitting");
    setErrorText(null);
    try {
      const text = condition.trim();
      if (editing && rule) {
        await updateRunRoutingRule(rule.id, { source_text: text, target });
      } else {
        await createRunRoutingRule({ name: text, source_text: text, target });
      }
      onDone();
    } catch (error) {
      setErrorText(messageFor(error));
      setState("error");
    }
  }

  return (
    <form
      className="routing-editor"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="routing-editor__row">
        <label className="routing-editor__field routing-editor__field--condition">
          <span className="routing-editor__label">{t("colCondition")}</span>
          <input
            type="text"
            className="routing-editor__input"
            value={condition}
            placeholder={t("conditionPlaceholder")}
            disabled={state === "submitting"}
            onChange={(e) => setCondition(e.target.value)}
          />
        </label>

        <label className="routing-editor__field routing-editor__field--model">
          <span className="routing-editor__label">{t("colModel")}</span>
          <select
            className="routing-editor__input"
            value={target}
            disabled={state === "submitting"}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">{t("modelPlaceholder")}</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.litellm_model}>
                {a.label} ({a.litellm_model})
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="routing-editor__foot">
        {errorText ? (
          <span className="routing-form__error" aria-live="polite">
            {errorText}
          </span>
        ) : null}
        <button
          type="submit"
          className="routing-form__submit"
          disabled={state === "submitting" || !ready}
        >
          {state === "submitting"
            ? editing
              ? t("saving")
              : t("adding")
            : editing
              ? t("save")
              : t("addRule")}
        </button>
        <button type="button" className="settings-add-cancel" onClick={onCancel}>
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}
