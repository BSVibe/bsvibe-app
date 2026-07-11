"use client";

import { listAccounts } from "@/lib/api/accounts";
import {
  createRunRoutingRule,
  deleteRunRoutingRule,
  listRunRoutingCallers,
  listRunRoutingRules,
} from "@/lib/api/run-routing";
import type { ModelAccount, RunRoutingCaller, RunRoutingRule } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * Settings → Models → ROUTING. The founder sees and manages how each dispatch
 * caller's work routes to a model. Backed by the REAL /api/v1/run-routing
 * endpoints (backend/api/v1/run_routing.py) — the SINGLE routing layer after the
 * legacy Layer-2 model-routing surface was hard-deleted. This surface only
 * reads/writes the rows the dispatch resolver consumes at runtime; it never
 * touches how rules are evaluated.
 *
 *  - List    ← GET    /api/v1/run-routing          (priority ascending)
 *  - Callers ← GET    /api/v1/run-routing/callers  (the caller dropdown source)
 *  - Add     → POST   /api/v1/run-routing          (201 RunRuleResponse)
 *  - Remove  → DELETE /api/v1/run-routing/{id}     (confirm-gated; 204)
 *
 * A rule maps a caller (e.g. `workflow.agent_loop.plan` — the design step) to a
 * target model account. Non-default rules MUST name a caller; the catch-all
 * default (set via the "Default model for this workspace" picker above) handles
 * everything else. The empty state is calm: with no rules, all work goes to the
 * workspace default. A failed list read degrades to a calm inline note.
 */
type ListState = { data: RunRoutingRule[]; failed: boolean } | null;

/** Row subtitle — the caller a rule matches, or "All callers" for the default. */
function callerSummary(rule: RunRoutingRule, anyLabel: string): string {
  return rule.caller_id ?? anyLabel;
}

export default function RunRoutingRules() {
  const [list, setList] = useState<ListState>(null);
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

      {showAdd && (
        <div className="settings-add-panel">
          <AddRunRoutingRule
            onCreated={() => {
              load();
              setShowAdd(false);
            }}
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
            <RunRoutingRuleRow
              key={rule.id}
              rule={rule}
              onRemoved={load}
              remove={deleteRunRoutingRule}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

type RowState = "idle" | "confirming" | "removing" | "error";

/**
 * One run-routing rule as a card: name, the caller it matches → target model, a
 * priority chip, and default / inactive chips. Remove is REAL and confirm-gated.
 */
function RunRoutingRuleRow({
  rule,
  onRemoved,
  remove,
}: {
  rule: RunRoutingRule;
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
          <span className="routing-card__match">{callerSummary(rule, t("matchAny"))}</span>
          <span className="routing-card__arrow" aria-hidden="true">
            {" → "}
          </span>
          <span className="routing-card__target">{rule.target}</span>
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
 * The "Add rule" form: a name, the caller this rule matches (a dispatch call
 * site from the registry — required unless the rule is the default), the target
 * model (picked from the workspace's model accounts), a priority, and an
 * optional "make this the default (catch-all)" toggle. Callers + accounts load
 * on mount; a submit POSTs the rule and `onCreated` re-reads the list.
 */
function AddRunRoutingRule({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [callerId, setCallerId] = useState("");
  const [target, setTarget] = useState("");
  const [priority, setPriority] = useState("10");
  const [isDefault, setIsDefault] = useState(false);
  const [callers, setCallers] = useState<RunRoutingCaller[]>([]);
  const [accounts, setAccounts] = useState<ModelAccount[]>([]);
  const [state, setState] = useState<FormState>("idle");
  const [createdName, setCreatedName] = useState("");
  const t = useTranslations("settings.models.routing");

  useEffect(() => {
    let active = true;
    listRunRoutingCallers()
      .then((data) => active && setCallers(data))
      .catch(() => active && setCallers([]));
    listAccounts()
      .then((data) => active && setAccounts(data.filter((a) => a.is_active)))
      .catch(() => active && setAccounts([]));
    return () => {
      active = false;
    };
  }, []);

  const parsedPriority = Number.parseInt(priority, 10);
  const ready =
    name.trim().length > 0 &&
    target.trim().length > 0 &&
    (isDefault || callerId.trim().length > 0) &&
    Number.isInteger(parsedPriority) &&
    parsedPriority >= 1;

  function reset() {
    setName("");
    setCallerId("");
    setTarget("");
    setPriority("10");
    setIsDefault(false);
  }

  async function submit() {
    if (state === "submitting" || !ready) return;
    setState("submitting");
    try {
      const created = await createRunRoutingRule({
        name: name.trim(),
        caller_id: isDefault ? null : callerId.trim(),
        target: target.trim(),
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

        {!isDefault && (
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
                  {c.caller_id}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className="routing-form__row">
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
      </div>

      <div className="routing-form__row">
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
