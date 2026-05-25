/** A concise one-line title for a deliverable — NOT the raw LLM `summary`
 *  (which is frequently a multi-clause / multi-paragraph blob). Take the first
 *  non-empty line, then its first sentence (cut at the first `.`/`!`/`?`
 *  followed by whitespace — so "fibonacci.py" isn't split), then hard-cap the
 *  length with an ellipsis. Returns `fallback` when there's no summary at all.
 *
 *  Shared by the Brief "recently shipped" rows, the product "Shipped" list, and
 *  the Delivery Report title so a shipped result reads calmly everywhere. */
const _SUMMARY_MAX = 140;

export function conciseSummary(summary: string | null, fallback: string): string {
  const first = (summary ?? "")
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line.length > 0);
  if (!first) return fallback;
  const sentence = first.match(/^.*?[.!?](?=\s)/);
  let text = sentence ? sentence[0] : first;
  if (text.length > _SUMMARY_MAX) text = `${text.slice(0, _SUMMARY_MAX).trimEnd()}…`;
  return text;
}
