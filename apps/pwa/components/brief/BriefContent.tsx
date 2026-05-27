import RailProducts from "@/components/shell/RailProducts";
import type { BriefView } from "@/lib/api/types";
import { useTranslations } from "next-intl";
import NeedsYou from "./NeedsYou";
import WorkStream from "./WorkStream";
import WorkingNow from "./WorkingNow";

/**
 * The merged Work-Home surface (Brief + Activity in one): "Working on now"
 * hero (what BSVibe is doing right now), then "Needs you" (decisions), then the
 * "Work stream" (the full done history). Takes a ready `BriefView`, so it is
 * trivially testable.
 *
 * `onNeedsYouResolved` bubbles up a successful Safe-Mode approve/deny so the
 * container can re-read and drop the resolved item.
 *
 * The mobile-only Products section at the bottom mirrors the desktop left
 * rail's PRODUCTS block — on mobile the rail is hidden and there was no other
 * entry point to a product detail page short of clicking through a work-stream
 * row. The CSS wrapper (.brief__mobile-products) hides the block on desktop
 * where the rail already shows it.
 */
export default function BriefContent({
  view,
  onNeedsYouResolved,
}: {
  view: BriefView;
  onNeedsYouResolved?: () => void;
}) {
  const t = useTranslations("brief");
  return (
    <div className="brief">
      <h1 className="brief__heading">{t("heading")}</h1>
      <WorkingNow items={view.working} />
      <NeedsYou items={view.needsYou} onResolved={onNeedsYouResolved} />
      <WorkStream items={view.stream} />
      <div className="brief__mobile-products">
        <RailProducts />
      </div>
    </div>
  );
}
