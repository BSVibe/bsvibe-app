import type { Metadata } from "next";

/**
 * GDPR L1 — Sub-processor disclosure (Art. 28 transparency).
 *
 * Calm, factual list of third-party processors. Mirrors the
 * `sub_processors` field returned by `GET /api/v1/workspace/processing-record`
 * so the two surfaces cannot drift out of sync — if a sub-processor is added
 * server-side, this static page should be updated in the same change.
 *
 * Public, un-authed: every visitor can see who handles their data.
 */

export const metadata: Metadata = {
  title: "Sub-processors · BSVibe",
  description: "Third-party sub-processors that handle data on behalf of BSVibe workspaces.",
};

type SubProcessor = {
  readonly name: string;
  readonly purpose: string;
  readonly region: string;
};

/**
 * Keep in sync with `backend.api.v1.workspace_compliance.SUB_PROCESSORS`.
 * The Art. 30 processing-record endpoint serves the same list to the
 * controller's processing record; a drift means a customer reading the
 * record vs. this page would see different lists, which is exactly the
 * transparency failure GDPR Art. 28 prohibits.
 */
const SUB_PROCESSORS: readonly SubProcessor[] = [
  {
    name: "Supabase",
    purpose: "Authentication (Supabase Auth, JWKS) + Postgres database hosting.",
    region: "us-east-1 / eu-west-1 (per workspace region)",
  },
  {
    name: "Vercel",
    purpose: "PWA frontend hosting + edge delivery.",
    region: "global edge (origin: iad1)",
  },
  {
    name: "Anthropic",
    purpose: "LLM inference for the agent loop (opt-in per workspace).",
    region: "us (Anthropic API)",
  },
  {
    name: "OpenAI",
    purpose: "LLM inference for the agent loop (opt-in per workspace).",
    region: "us (OpenAI API)",
  },
];

export default function SubProcessorsPage() {
  return (
    <main className="legal-page">
      <h1>Sub-processors</h1>
      <p className="legal-page__lede">
        Third-party services we engage to deliver the BSVibe AI agent OS. Each sub-processor handles
        a narrow slice of workspace data for the purpose listed below: no profiling, no resale, no
        off-purpose use.
      </p>
      <table className="legal-page__table">
        <thead>
          <tr>
            <th scope="col">Sub-processor</th>
            <th scope="col">Purpose</th>
            <th scope="col">Region</th>
          </tr>
        </thead>
        <tbody>
          {SUB_PROCESSORS.map((sp) => (
            <tr key={sp.name}>
              <td>{sp.name}</td>
              <td>{sp.purpose}</td>
              <td>{sp.region}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="legal-page__note">
        Material changes to this list are reflected in
        <code> GET /api/v1/workspace/processing-record </code> so a workspace's Art. 30 record stays
        accurate at any point in time.
      </p>
    </main>
  );
}
