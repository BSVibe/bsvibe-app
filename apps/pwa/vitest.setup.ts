import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Node ≥25 ships its own Web Storage: `globalThis.localStorage` / `sessionStorage` become
// Node's accessors, and without `--localstorage-file` they read `undefined` — shadowing the
// jsdom storage this environment exists to provide. Measured on Node 26 (2026-10-16): ~470
// tests failed with "Cannot read properties of undefined (reading 'removeItem')", on main too.
// Put jsdom's storage back ONLY when Node's is the empty one; on Node 20 (CI) the jsdom
// storage is already there and this does nothing.
const dom = (globalThis as { jsdom?: { window: Window } }).jsdom;
for (const name of ["localStorage", "sessionStorage"] as const) {
  if (dom && globalThis[name] === undefined) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get: () => dom.window[name],
    });
  }
}

afterEach(() => {
  cleanup();
});
