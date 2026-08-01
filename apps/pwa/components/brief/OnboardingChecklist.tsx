import { useTranslations } from "next-intl";
import Link from "next/link";

/**
 * First-run onboarding — the three steps a new founder takes to reach first
 * value. BSVibe does NOT run the executor for you; like a GitHub Actions runner
 * you connect your OWN self-hosted worker, so the middle step deep-links to the
 * worker setup rather than promising a managed one.
 *
 * Steps check off live from the workspace signals (`hasProducts` /
 * `hasLiveWorker`) so the founder sees progress; the whole block hides once the
 * workspace can actually produce (handled by the parent).
 */
export default function OnboardingChecklist({
  hasProducts,
  hasLiveWorker,
}: {
  hasProducts: boolean;
  hasLiveWorker: boolean;
}) {
  const t = useTranslations("brief.onboarding");

  const steps: { key: string; title: string; body: React.ReactNode; done: boolean }[] = [
    {
      key: "product",
      title: t("step1Title"),
      body: t("step1Body"),
      done: hasProducts,
    },
    {
      key: "worker",
      title: t("step2Title"),
      body: (
        <>
          {t("step2Body")}{" "}
          <Link href="/settings" className="onboarding__link">
            {t("step2Link")}
          </Link>
        </>
      ),
      done: hasLiveWorker,
    },
    {
      key: "request",
      title: t("step3Title"),
      body: t("step3Body"),
      done: false,
    },
  ];

  return (
    <section className="onboarding" aria-label={t("title")}>
      <h2 className="section-label">{t("title")}</h2>
      <p className="onboarding__lead">{t("lead")}</p>
      <ol className="onboarding__steps">
        {steps.map((s, i) => (
          <li key={s.key} className={`onboarding__step${s.done ? " onboarding__step--done" : ""}`}>
            <span className="onboarding__marker" aria-hidden="true">
              {s.done ? "✓" : i + 1}
            </span>
            <span className="onboarding__body">
              <span className="onboarding__step-title">{s.title}</span>
              <span className="onboarding__step-desc">{s.body}</span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
