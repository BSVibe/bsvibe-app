"use client";

import { listConcepts, listObservations } from "@/lib/api/inside";
import type { Concept, Observation } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import ConceptsSection from "./ConceptsSection";
import ObservationsSection from "./ObservationsSection";

/**
 * The Inside surface (the left-rail / mobile "Inside" route). The founder's
 * calm, read-only window into what the AI has learned — the trust ratchet's
 * accumulated knowledge. Two sections, each consuming a REAL backend list:
 *
 *  - "What I know"        ← GET /api/v1/inside/concepts      (canonical anchors —
 *    the settled concepts the canonicalization promoter graduated)
 *  - "Recently observed"  ← GET /api/v1/inside/observations  (raw garden notes
 *    the SettleWorker deposited, unpromoted)
 *
 * Read-only by design: there are no mutations on this surface. Each list loads
 * independently; one failing read shows a calm inline note for THAT section
 * rather than blanking the page (the other section still renders). A fresh
 * workspace — nothing learned, nothing observed — shows a calm "I haven't
 * learned anything yet" rather than a blank page.
 */
type SectionResult<T> = { data: T[]; failed: boolean };

async function loadSection<T>(fetcher: () => Promise<T[]>): Promise<SectionResult<T>> {
  try {
    return { data: await fetcher(), failed: false };
  } catch {
    // A per-section ApiError / network blip degrades to an inline note for that
    // section — never a thrown render or a blanked page.
    return { data: [], failed: true };
  }
}

export default function Inside() {
  const [concepts, setConcepts] = useState<SectionResult<Concept> | null>(null);
  const [observations, setObservations] = useState<SectionResult<Observation> | null>(null);
  const t = useTranslations("inside");

  useEffect(() => {
    let active = true;
    Promise.all([loadSection(listConcepts), loadSection(listObservations)]).then(([c, o]) => {
      if (!active) return;
      setConcepts(c);
      setObservations(o);
    });
    return () => {
      active = false;
    };
  }, []);

  if (concepts === null || observations === null) {
    return (
      <div className="inside inside--loading" aria-busy="true">
        <h1 className="inside__heading">{t("heading")}</h1>
        <p className="inside__loading-note">{t("loadingNote")}</p>
      </div>
    );
  }

  // A genuinely fresh workspace: both reads succeeded and returned nothing.
  const nothingLearned =
    !concepts.failed &&
    !observations.failed &&
    concepts.data.length === 0 &&
    observations.data.length === 0;

  return (
    <div className="inside">
      <h1 className="inside__heading">{t("heading")}</h1>
      <p className="inside__lede">{t("lede")}</p>

      {nothingLearned ? (
        <section className="inside-empty" aria-label={t("heading")}>
          <p className="inside-empty__line">{t("emptyLine")}</p>
          <p className="inside-empty__sub">{t("emptySub")}</p>
        </section>
      ) : (
        <>
          <ConceptsSection items={concepts.data} failed={concepts.failed} />
          <ObservationsSection items={observations.data} failed={observations.failed} />
        </>
      )}
    </div>
  );
}
