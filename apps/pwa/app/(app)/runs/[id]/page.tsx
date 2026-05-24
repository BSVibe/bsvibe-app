import RunDetail from "@/components/runs/RunDetail";

/** Run-detail route — the inspectable "Triggered" surface for one ExecutionRun.
 *  Next 16 / React 19 delivers dynamic-segment `params` as a Promise; await it,
 *  then hand the id to the client container that loads + renders the detail. */
export default async function RunDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <RunDetail runId={id} />;
}
