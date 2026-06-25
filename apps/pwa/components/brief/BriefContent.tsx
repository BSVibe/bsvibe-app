import RailProducts from "@/components/shell/RailProducts";
import type { BriefView } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import WorkStream from "./WorkStream";
import WorkingNow from "./WorkingNow";

/**
 * The merged Work-Home surface (Brief + Activity in one): "Working on now"
 * hero (what BSVibe is doing right now), then the "Work stream" (the full work
 * history). Takes a ready `BriefView`, so it is trivially testable.
 *
 * Decisions deliberately do NOT live here — the dedicated Decisions tab is the
 * single place for everything that needs the founder's judgment (Safe-Mode held
 * deliveries + paused-run checkpoints + canon proposals, with inline approve /
 * deny). The Brief used to duplicate the Safe-Mode block as a "Needs you" strip;
 * that duplication is removed. The work-stream rows that still NEED review
 * (review_ready runs) deep-link to their Decision instead (WorkStream).
 *
 * The mobile-only Products section at the bottom mirrors the desktop left
 * rail's PRODUCTS block — on mobile the rail is hidden and there was no other
 * entry point to a product detail page short of clicking through a work-stream
 * row. The CSS wrapper (.brief__mobile-products) hides the block on desktop
 * where the rail already shows it.
 */
export default function BriefContent({ view }: { view: BriefView }) {
  const t = useTranslations("brief");
  return (
    <div className="brief">
      <h1 className="brief__heading">{t("heading")}</h1>
      <WorkingNow items={view.working} />
      <WorkStream items={view.stream} />
      <div className="brief__mobile-products">
        <RailProducts />
      </div>
    </div>
  );
}
