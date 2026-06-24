"use client";

import { createRule, deleteRule, listRules } from "@/lib/api/rules";
import type { RoutingRule, RuleCondition } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * Settings → Models → ROUTING. The founder sees and manages how work routes to
 * models: each rule maps a unit of work to a target model (e.g. "Simple chores →
 * Local LLM", "Substantial work → opencode"). Backed by the REAL /api/v1/rules
 * endpoints (backend/api/v1/rules.py), which only read/write the rows the
 * gateway rule engine already consumes at runtime — this surface never touches
 * how rules are evaluated.
 *
 *  - List   ← GET    /api/v1/rules            (priority ascending; each carries
 *                                              the conditions it matches on)
 *  - Add    → POST   /api/v1/rules            (201 RuleResponse)
 *  - Remove → DELETE /api/v1/rules/{id}       (confirm-gated; 204)
 *
 * The empty state is calm and explanatory: with no rules, all work goes to the
 * active model account (the engine's default), so this is informative rather
 * than a load-bearing nudge. A failed list read degrades to a calm inline note
 * rather than a blanked surface; the Add form's write is independent of it.
 *
 * Deferred (later lifts): a full task-tier CLASSIFIER UI (the engine already
 * does lazy intent classification — we don't rebuild it) and complex
 * multi-condition editing. Rules created here are name + target_model +
 * priority + optional default (catch-all).
 */
type ListState = { data: RoutingRule[]; failed: boolean } | null;

/** Plain-language summary of what a rule matches, for the row subtitle. With no
 *  conditions it is a catch-all; otherwise we show each condition's value (the
 *  human-meaningful part — "deep", "chore"). */
function matchSummary(rule: RoutingRule, anyLabel: string): string {
  if (rule.conditions.length === 0) return anyLabel;
  return rule.conditions.map((c: RuleCondition) => String(c.value)).join(" · ");
}

export default function RoutingRules() {
  const [list, setList] = useState<ListState>(null);
  const [showAdd, setShowAdd] = useState(false);
  const t = useTranslations("settings.models.routing");

  async function load() {
    try {
      setList({ data: await listRules(), failed: false });
    } catch {
      setList({ data: [], failed: true });
    }
  }

  useEffect(() => {
    let active = true;
    listRules()
      .then((data) => active && setList({ data, failed: false }))
      .catch(() => active && setList({ data: [], failed: true }));
    return () => {
      active = false;
    };
  }, []);

  return (
    <section className="routing" aria-label={t("sectionLabel")}>
      <header className="routing__head">
        <h2 className="section-label">{t("sectionHeading")}</h2>
        {list && !list.failed && list.data.length > 0 ? (
          <span className="routing__count">{list.data.length}</span>
        ) : null}
        {!showAdd && (
          <button type="button" className="settings-add-toggle" onClick={() => setShowAdd(true)}>
            {t("addToggle")}
          </button>
        )}
      </header>
      <p className="routing__lede">{t("lede")}</p>

      {/* Collapsed by default — rules are optional (the default model handles
          everything else), so the section shouldn't open as a big empty form. */}
      {showAdd && (
        <div className="settings-add-panel">
          <AddRoutingRule
            onCreated={() => {
              load();
              setShowAdd(false);
            }}
            createRule={createRule}
          />
          <button type="button" className="settings-add-cancel" onClick={() => setShowAdd(false)}>
            {t("cancel")}
          </button>
        </div>
      )}

      {list === null ? (
        <p className="routing__loading" aria-busy="true">
          {t("loading")}
        </p>
      ) : list.failed ? (
        <p className="routing__note" aria-live="polite">
          {t("loadError")}
        </p>
      ) : list.data.length === 0 ? (
        <p className="routing__empty">{t("empty")}</p>
      ) : (
        <ul className="routing__list" aria-label={t("listLabel")}>
          {list.data.map((rule) => (
            <RoutingRuleRow key={rule.id} rule={rule} onRemoved={load} remove={deleteRule} />
          ))}
        </ul>
      )}
    </section>
  );
}

type RowState = "idle" | "confirming" | "removing" | "error";

/**
 * One routing rule, rendered as a card: name, what it matches → target model,
 * a priority chip, and default / inactive chips. Remove is REAL and
 * confirm-gated (first "Remove" reveals "Confirm remove" + "Cancel") so a
 * delete is never a single stray tap; Confirm fires the DELETE and `onRemoved`
 * re-reads the list.
 */
function RoutingRuleRow({
  rule,
  onRemoved,
  remove,
}: {
  rule: RoutingRule;
  onRemoved: () => void;
  remove: (id: string) => Promise<void>;
}) {
  const [state, setState] = useState<RowState>("idle");
  const t = useTranslations("settings.models.routing");

  async function confirmRemove() {
    if (state === "removing") return;
    setState("removing");
    try {
      await remove(rule.id);
      onRemoved();
    } catch {
      setState("error");
    }
  }

  return (
    <li className="routing-card">
      <div className="routing-card__body">
        <div className="routing-card__head">
          <span className="routing-card__name">{rule.name}</span>
          {rule.is_default ? (
            <span className="routing-card__chip routing-card__chip--default">{t("default")}</span>
          ) : null}
          {rule.is_active ? null : (
            <span className="routing-card__chip routing-card__chip--inactive">{t("inactive")}</span>
          )}
          <span className="routing-card__chip" title={t("priorityTitle")}>
            {t("priorityChip", { priority: rule.priority })}
          </span>
        </div>
        <p className="routing-card__route">
          <span className="routing-card__match">{matchSummary(rule, t("matchAny"))}</span>
          <span className="routing-card__arrow" aria-hidden="true">
            {" → "}
          </span>
          <span className="routing-card__target">{rule.target_model}</span>
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
          <button
            type="button"
            className="routing-card__remove"
            onClick={() => setState("confirming")}
          >
            {t("remove")}
          </button>
        )}
      </div>
    </li>
  );
}

type FormState = "idle" | "submitting" | "error" | "success";

/**
 * The "Add rule" form: a name, the target model (a litellm identifier / executor
 * capability the founder picks from their model accounts), a priority, and an
 * optional "make this the default (catch-all)" toggle. Conditions are not
 * editable here (deferred); a rule created without conditions is a catch-all
 * that the engine reaches for at its priority. `onCreated` re-reads the list;
 * `createRule` is injected so the surface is unit-testable against a mocked
 * fetch.
 */
function AddRoutingRule({
  onCreated,
  createRule,
}: {
  onCreated: () => void;
  createRule: (input: import("@/lib/api/types").RoutingRuleCreate) => Promise<RoutingRule>;
}) {
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [priority, setPriority] = useState("10");
  const [isDefault, setIsDefault] = useState(false);
  const [state, setState] = useState<FormState>("idle");
  const [createdName, setCreatedName] = useState("");
  const t = useTranslations("settings.models.routing");

  const parsedPriority = Number.parseInt(priority, 10);
  const ready =
    name.trim().length > 0 &&
    target.trim().length > 0 &&
    Number.isInteger(parsedPriority) &&
    parsedPriority >= 1;

  function reset() {
    setName("");
    setTarget("");
    setPriority("10");
    setIsDefault(false);
  }

  async function submit() {
    if (state === "submitting" || !ready) return;
    setState("submitting");
    try {
      const created = await createRule({
        name: name.trim(),
        target_model: target.trim(),
        priority: parsedPriority,
        is_default: isDefault,
      });
      setCreatedName(created.name);
      reset();
      setState("success");
      onCreated();
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
      <div className="routing-form__row">
        <label className="routing-form__field">
          <span className="routing-form__label">{t("ruleName")}</span>
          <input
            className="routing-form__input"
            type="text"
            placeholder={t("ruleNamePlaceholder")}
            value={name}
            disabled={state === "submitting"}
            onChange={(e) => setName(e.target.value)}
          />
        </label>

        <label className="routing-form__field">
          <span className="routing-form__label">{t("routeTo")}</span>
          <input
            className="routing-form__input"
            type="text"
            placeholder={t("routeToPlaceholder")}
            value={target}
            disabled={state === "submitting"}
            onChange={(e) => setTarget(e.target.value)}
          />
        </label>
      </div>

      <div className="routing-form__row">
        <label className="routing-form__field routing-form__field--narrow">
          <span className="routing-form__label">{t("priority")}</span>
          <input
            className="routing-form__input"
            type="number"
            min={1}
            value={priority}
            disabled={state === "submitting"}
            onChange={(e) => setPriority(e.target.value)}
          />
        </label>

        <label className="routing-form__check">
          <input
            type="checkbox"
            checked={isDefault}
            disabled={state === "submitting"}
            onChange={(e) => setIsDefault(e.target.checked)}
          />
          <span>{t("makeDefault")}</span>
        </label>
      </div>

      <div className="routing-form__foot">
        {state === "error" && (
          <span className="routing-form__error" aria-live="polite">
            {t("addError")}
          </span>
        )}
        {state === "success" && (
          <span className="routing-form__success" aria-live="polite">
            {t("addSuccess", { name: createdName })}
          </span>
        )}
        <button
          type="submit"
          className="routing-form__submit"
          disabled={state === "submitting" || !ready}
        >
          {state === "submitting" ? t("adding") : t("addRule")}
        </button>
      </div>
    </form>
  );
}
