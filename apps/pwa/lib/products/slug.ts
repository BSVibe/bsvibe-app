/**
 * Product slug helpers.
 *
 * The backend slug grammar is `^[a-z][a-z0-9-]*$` (backend/api/v1/products.py
 * `_SLUG_RE`). `suggestSlug` derives a best-effort valid slug from a free-text
 * name (so the create form can auto-fill it as the founder types the Name), and
 * `isValidSlug` is the pre-submit guard mirroring the backend regex exactly.
 */

const SLUG_RE = /^[a-z][a-z0-9-]*$/;

/** Derive a backend-valid slug from a free-text name: lowercase, separators →
 *  single hyphen, strip anything outside [a-z0-9-], drop leading non-letters
 *  and trailing hyphens. May return "" when nothing survives (e.g. "123") — the
 *  form then leaves the field empty for the founder to fill, and validation
 *  blocks submit until it's valid. */
export function suggestSlug(name: string): string {
  const base = name
    .toLowerCase()
    .replace(/['’]/g, "") // drop apostrophes so "Acme's" joins → "acmes" (not a split)
    .replace(/[^a-z0-9]+/g, "-") // any run of remaining non-slug chars → one hyphen
    .replace(/^[^a-z]+/, "") // drop leading digits/hyphens so it starts with a letter
    .replace(/-+$/, ""); // trim trailing hyphens
  return base;
}

/** True iff `slug` satisfies the backend's `^[a-z][a-z0-9-]*$`. */
export function isValidSlug(slug: string): boolean {
  return SLUG_RE.test(slug);
}
