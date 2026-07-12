"use client";

import { listAccounts } from "@/lib/api/accounts";
import {
  applyRunRoutingProposals,
  compileRunRoutingRules,
  createRunRoutingRule,
  deleteRunRoutingRule,
  listRunRoutingCallers,
  listRunRoutingRules,
  updateRunRoutingRule,
} from "@/lib/api/run-routing";
import type {
  ModelAccount,
  RunRoutingCaller,
  RunRoutingProposal,
  RunRoutingRule,
  RunRoutingRuleCreate,
} from "@/lib/api/types";
import { callerDisplay } from "@/lib/routing-caller-labels";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * Settings → Models → ROUTING. The founder maps each dispatch caller's work to a
 * model. Backed by the REAL /api/v1/run-routing endpoints — the SINGLE routing
 * layer after the legacy Layer-2 surface was hard-deleted.
 *
 * Lift 6 refinements: no priority (natural-language routing picks one rule per
 * caller, so priority was noise); the catch-all default lives ONLY in the
 * "Default model" picker above (is_default rules are hidden here to avoid the
 * double display); each rule is one line `caller → model` (the target resolved
 * to a friendly account label, not a raw id); and rules are editable in place.
 */
type ListState = { data: RunRoutingRule[]; failed: boolean } | null;

/** Resolve a rule's `target` (a litellm_model id, or a legacy account id) to a
 *  friendly account label. Falls back to the raw target when unknown. */
function friendlyTarget(target: string, accounts: ModelAccount[]): string {
  const acct = accounts.find((a) => a.litellm_model === target || a.id === target);
  return acct ? acct.label : target;
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
  const [callers, setCallers] = useState<RunRoutingCaller[]>([]);
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
    listRunRoutingCallers()
      .then((data) => active && setCallers(data))
      .catch(() => active && setCallers([]));
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

      {/* NL-first: describing routing in plain language IS the primary surface. */}
      <div className="settings-add-panel routing-nl-primary">
        <NlCompilePanel accounts={accounts} onApplied={load} />
      </div>

      {/* The caller/target dropdown form is the demoted "advanced" manual path. */}
      <div className="routing__advanced">
        {!showAdd ? (
          <button type="button" className="settings-add-toggle" onClick={() => setShowAdd(true)}>
            {t("addToggle")}
          </button>
        ) : (
          <div className="settings-add-panel">
            <RuleForm
              callers={callers}
              accounts={accounts}
              onDone={() => {
                load();
                setShowAdd(false);
              }}
            />
            <button type="button" className="settings-add-cancel" onClick={() => setShowAdd(false)}>
              {t("cancel")}
            </button>
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
          {visible.map((rule) => (
            <RunRoutingRuleRow
              key={rule.id}
              rule={rule}
              callers={callers}
              accounts={accounts}
              onChanged={load}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

type RowState = "idle" | "editing" | "confirming" | "removing" | "error";

/** One rule as a single line `caller → model` with Edit + confirm-gated Remove.
 *  Edit swaps the row body for an inline {@link RuleForm}. */
function RunRoutingRuleRow({
  rule,
  callers,
  accounts,
  onChanged,
}: {
  rule: RunRoutingRule;
  callers: RunRoutingCaller[];
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
      <li className="routing-card">
        <RuleForm
          callers={callers}
          accounts={accounts}
          rule={rule}
          onDone={() => {
            setState("idle");
            onChanged();
          }}
        />
        <button type="button" className="settings-add-cancel" onClick={() => setState("idle")}>
          {t("cancel")}
        </button>
      </li>
    );
  }

  return (
    <li className="routing-card">
      <div className="routing-card__body">
        <p className="routing-card__route">
          <span className="routing-card__match" title={rule.caller_id ?? undefined}>
            {callerDisplay(rule.caller_id, t)}
          </span>
          <span className="routing-card__arrow" aria-hidden="true">
            {" → "}
          </span>
          <span className="routing-card__target">{friendlyTarget(rule.target, accounts)}</span>
          {rule.is_active ? null : (
            <span className="routing-card__chip routing-card__chip--inactive">{t("inactive")}</span>
          )}
        </p>
      </div>

      <div className="routing-card__actions">
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

type FormState = "idle" | "submitting" | "error";

/**
 * Add / edit form — pick a caller and a target model. No name (auto-derived
 * `caller → model`), no priority (natural-language routing is one rule per
 * caller), no default toggle (the default lives in the picker above). When
 * `rule` is passed the form edits it (PATCH); otherwise it creates (POST).
 */
function RuleForm({
  callers,
  accounts,
  rule,
  onDone,
}: {
  callers: RunRoutingCaller[];
  accounts: ModelAccount[];
  rule?: RunRoutingRule;
  onDone: () => void;
}) {
  const editing = rule !== undefined;
  const [callerId, setCallerId] = useState(rule?.caller_id ?? "");
  const [target, setTarget] = useState(rule ? targetSelectValue(rule.target, accounts) : "");
  const [state, setState] = useState<FormState>("idle");
  const t = useTranslations("settings.models.routing");

  const ready = callerId.trim().length > 0 && target.trim().length > 0;

  async function submit() {
    if (state === "submitting" || !ready) return;
    setState("submitting");
    try {
      if (editing && rule) {
        await updateRunRoutingRule(rule.id, { caller_id: callerId, target });
      } else {
        const body: RunRoutingRuleCreate = {
          name: `${callerId} → ${target}`,
          caller_id: callerId,
          target,
          priority: 10,
        };
        await createRunRoutingRule(body);
      }
      onDone();
    } catch {
      setState("error");
    }
  }

  return (
    <form
      className="routing-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      {editing ? <p className="routing-form__label">{t("editTitle")}</p> : null}
      <div className="routing-form__row">
        <label className="routing-form__field">
          <span className="routing-form__label">{t("caller")}</span>
          <select
            className="routing-form__input"
            value={callerId}
            disabled={state === "submitting"}
            onChange={(e) => setCallerId(e.target.value)}
          >
            <option value="">{t("callerPlaceholder")}</option>
            {callers.map((c) => (
              <option key={c.caller_id} value={c.caller_id} title={c.description}>
                {callerDisplay(c.caller_id, t)}
              </option>
            ))}
          </select>
        </label>

        <label className="routing-form__field">
          <span className="routing-form__label">{t("routeTo")}</span>
          <select
            className="routing-form__input"
            value={target}
            disabled={state === "submitting"}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">{t("routeToPlaceholder")}</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.litellm_model}>
                {a.label} ({a.litellm_model})
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="routing-form__foot">
        {state === "error" && (
          <span className="routing-form__error" aria-live="polite">
            {t("addError")}
          </span>
        )}
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
      </div>
    </form>
  );
}

type NlState = "idle" | "compiling" | "applying" | "error";
type Translate = ReturnType<typeof useTranslations>;

/** Describe one proposal's MATCH side in human terms — the routing DIMENSION it
 *  expresses, not the raw wire keys. A category reads "<intent> (category)", a
 *  condition "<field> <op> <value>", a stage via the localized caller label, and
 *  a default "Default model". Keeps the founder in NL, never in JSON. */
function proposalMatchLabel(p: RunRoutingProposal, t: Translate): string {
  if (p.is_default) return t("dim.default");
  if (p.intent_name) return t("dim.category", { name: p.intent_name });
  if (p.caller_id) return callerDisplay(p.caller_id, t);
  if (p.condition) {
    const op = OPERATOR_SYMBOL[p.condition.operator] ?? p.condition.operator;
    return `${p.condition.field} ${op} ${formatConditionValue(p.condition.value)}`;
  }
  return p.name;
}

/** Compact, human operator glyphs for the preview (falls back to the raw op). */
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

/**
 * NL → rules panel (NL-first primary surface). The founder describes routing in
 * plain language; one cheap LLM call drafts VALIDATED, multi-dimension proposals
 * (dry-run — nothing saved). The preview renders each proposal in human terms
 * (category / complexity / language / artifact / stage / default). "Apply all"
 * commits the WHOLE set in ONE backend call (`/compile/apply`) — the backend
 * creates the intents + rules + default atomically — then reloads.
 */
function NlCompilePanel({
  accounts,
  onApplied,
}: {
  accounts: ModelAccount[];
  onApplied: () => void;
}) {
  const [text, setText] = useState("");
  const [proposals, setProposals] = useState<RunRoutingProposal[] | null>(null);
  const [state, setState] = useState<NlState>("idle");
  const t = useTranslations("settings.models.routing");

  async function compile() {
    if (state === "compiling" || text.trim().length === 0) return;
    setState("compiling");
    setProposals(null);
    try {
      const res = await compileRunRoutingRules(text.trim());
      setProposals(res.proposals);
      setState("idle");
    } catch {
      setState("error");
    }
  }

  async function applyAll() {
    if (state === "applying" || !proposals || proposals.length === 0) return;
    setState("applying");
    try {
      await applyRunRoutingProposals(proposals);
      onApplied();
    } catch {
      setState("error");
    }
  }

  return (
    <div className="routing-nl">
      <p className="routing-nl__title section-label">{t("nlHeading")}</p>
      <p className="routing-form__label">{t("nlLede")}</p>
      <textarea
        className="routing-form__input"
        rows={3}
        placeholder={t("nlPlaceholder")}
        value={text}
        disabled={state === "compiling" || state === "applying"}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="routing-form__foot">
        {state === "error" && (
          <span className="routing-form__error" aria-live="polite">
            {proposals === null ? t("nlError") : t("nlApplyError")}
          </span>
        )}
        <button
          type="button"
          className="routing-form__submit"
          onClick={compile}
          disabled={state === "compiling" || state === "applying" || text.trim().length === 0}
        >
          {state === "compiling" ? t("nlCompiling") : t("nlCompile")}
        </button>
      </div>

      {proposals !== null &&
        (proposals.length === 0 ? (
          <p className="routing__empty">{t("nlEmpty")}</p>
        ) : (
          <div className="routing-nl__preview">
            <p className="section-label">{t("nlProposed")}</p>
            <ul className="routing__list" aria-label={t("nlProposed")}>
              {proposals.map((p, i) => (
                <li className="routing-card" key={`${p.name}-${p.target}-${i}`}>
                  <div className="routing-card__body">
                    <p className="routing-card__route">
                      <span
                        className="routing-card__match"
                        title={p.caller_id ?? p.condition?.field ?? undefined}
                      >
                        {proposalMatchLabel(p, t)}
                      </span>
                      <span className="routing-card__arrow" aria-hidden="true">
                        {" → "}
                      </span>
                      <span className="routing-card__target">
                        {friendlyTarget(p.target, accounts)}
                      </span>
                    </p>
                  </div>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="routing-form__submit"
              onClick={applyAll}
              disabled={state === "applying"}
            >
              {state === "applying" ? t("nlApplying") : t("nlApply")}
            </button>
          </div>
        ))}
    </div>
  );
}
