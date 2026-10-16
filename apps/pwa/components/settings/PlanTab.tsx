"use client";

import { type WorkspacePlan, getWorkspacePlan } from "@/lib/api/workspace";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useState } from "react";

/**
 * Settings → Plan (#928 step 4). Which plan the workspace is on, each limit, and how much of
 * it is in use — read from GET /api/v1/workspace/plan, which counts use with the same
 * functions the gates refuse with, so "3 / 3" here is exactly when a submission gets 429.
 *
 * A `null` limit is unlimited and reads "Unlimited", never 0. On the free plan a link points at
 * the public pricing page — worded as "about paid plans", not "upgrade": billing is paused
 * (founder decision 2026-10-16, BSVibe launches free) and that page says paid plans are coming.
 */

type State = { kind: "loading" } | { kind: "loaded"; plan: WorkspacePlan } | { kind: "failed" };

const PLAN_KEYS = new Set(["free", "pro", "operator"]);

export default function PlanTab() {
  const t = useTranslations("settings.plan");
  const locale = useLocale();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let live = true;
    getWorkspacePlan()
      .then((plan) => live && setState({ kind: "loaded", plan }))
      .catch(() => live && setState({ kind: "failed" }));
    return () => {
      live = false;
    };
  }, []);

  if (state.kind === "loading") return null;
  if (state.kind === "failed") {
    return (
      <p className="settings-field__error" role="alert">
        {t("loadError")}
      </p>
    );
  }

  const { plan } = state;
  const number = (n: number) => n.toLocaleString(locale);
  const usage = (used: number, limit: number | null) =>
    `${number(used)} / ${limit === null ? t("unlimited") : number(limit)}`;
  const planName = PLAN_KEYS.has(plan.plan) ? t(`names.${plan.plan}`) : plan.plan;

  return (
    <>
      <section className="settings-field" aria-label={t("current")}>
        <span className="settings-field__label">{t("current")}</span>
        <span className="settings-field__value">{planName}</span>
      </section>
      {plan.billing_status === "past_due" ? (
        <output className="settings-field__hint">{t("pastDue")}</output>
      ) : null}
      <section className="settings-field" aria-label={t("runs")}>
        <span className="settings-field__label">{t("runs")}</span>
        <span className="settings-field__value">{usage(plan.runs.held, plan.runs.limit)}</span>
      </section>
      <section className="settings-field" aria-label={t("tokens")}>
        <span className="settings-field__label">{t("tokens")}</span>
        <span className="settings-field__value">{usage(plan.tokens.used, plan.tokens.limit)}</span>
      </section>
      <section className="settings-field" aria-label={t("verifyStacks")}>
        <span className="settings-field__label">{t("verifyStacks")}</span>
        <span className="settings-field__value">{number(plan.verify_stack_slots)}</span>
      </section>
      {plan.plan === "free" ? (
        <a
          className="settings-field__primary"
          href={`https://bsvibe.dev/${locale}/pricing`}
          target="_blank"
          rel="noreferrer"
        >
          {t("paidPlans")}
        </a>
      ) : null}
    </>
  );
}
