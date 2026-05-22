import type { BriefView } from "@/lib/api/types";
import NeedsYou from "./NeedsYou";
import ProductLanes from "./ProductLanes";
import RecentlyShipped from "./RecentlyShipped";

/**
 * The Glance, presentational (UX §3.4 layout): a centered column with the
 * "Needs you" strip on top, then product status lanes, then "Recently
 * shipped". Pure — takes a ready `BriefView`, so it is trivially testable.
 */
export default function BriefContent({ view }: { view: BriefView }) {
  return (
    <div className="brief">
      <h1 className="brief__heading">Brief</h1>
      <NeedsYou items={view.needsYou} />
      <ProductLanes lanes={view.lanes} />
      <RecentlyShipped items={view.recentlyShipped} />
    </div>
  );
}
