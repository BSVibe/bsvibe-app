/**
 * Minimal ambient typings for the slice of `d3-force` the Knowledge graph uses
 * (`forceCollide`, `forceX`, `forceY`). The package ships no `.d.ts` of its own
 * and we must not add `@types/d3-force` as a dependency, so we declare only the
 * chained-setter shapes the canvas tuning calls — enough to keep `tsc --strict`
 * happy without pulling in the full d3 type surface.
 *
 * The simulation node is whatever object the lib feeds the force; we keep it
 * loosely typed (`unknown`) and the callers narrow via `(d as { id?: string })`.
 */
declare module "d3-force" {
  interface Force {
    (alpha: number): void;
    initialize?: (nodes: unknown[]) => void;
  }

  interface ForceCollide extends Force {
    radius(radius: number | ((node: unknown, i: number, nodes: unknown[]) => number)): ForceCollide;
    strength(strength: number): ForceCollide;
    iterations(iterations: number): ForceCollide;
  }

  interface ForcePositional extends Force {
    x(x: number | ((node: unknown, i: number, nodes: unknown[]) => number)): ForcePositional;
    y(y: number | ((node: unknown, i: number, nodes: unknown[]) => number)): ForcePositional;
    strength(
      strength: number | ((node: unknown, i: number, nodes: unknown[]) => number),
    ): ForcePositional;
  }

  export function forceCollide(
    radius?: number | ((node: unknown, i: number, nodes: unknown[]) => number),
  ): ForceCollide;
  export function forceX(x?: number): ForcePositional;
  export function forceY(y?: number): ForcePositional;
}
