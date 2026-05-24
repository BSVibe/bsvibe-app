import DeliveryReport from "@/components/deliverables/DeliveryReport";

/** Delivery Report route — the glass-box proof for one shipped deliverable.
 *  Next 16 / React 19 delivers dynamic-segment `params` as a Promise; await it,
 *  then hand the id to the client container that loads + renders the report. */
export default async function DeliveryReportPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <DeliveryReport deliverableId={id} />;
}
