import SkillViewer from "@/components/skills/SkillViewer";

/** Skill detail route — the read-only single-skill viewer. Next 16 / React 19
 *  delivers dynamic-segment `params` as a Promise; await it, then hand the
 *  (URL-decoded) skill name to the client container that loads + renders the
 *  manifest. */
export default async function SkillDetailPage({
  params,
}: {
  params: Promise<{ name: string }>;
}) {
  const { name } = await params;
  return <SkillViewer name={decodeURIComponent(name)} />;
}
