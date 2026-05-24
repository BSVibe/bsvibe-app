/**
 * Models tab composition — after Lift 4 the Models tab hosts BOTH the existing
 * <ModelAccounts/> surface AND the new <ExecutorWorkers/> section beneath it.
 * Both fetch on mount; an empty list lets each reach its calm state without a
 * network call.
 */

import ModelsTab from "@/components/settings/ModelsTab";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  clearSession();
  setSession(SESSION);
  global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Models tab composition", () => {
  it("still renders the existing Model accounts surface", async () => {
    render(<ModelsTab />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /model accounts/i })).toBeInTheDocument();
    });
  });

  it("renders the new Executor workers section beneath model accounts", async () => {
    render(<ModelsTab />);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /executor workers/i })).toBeInTheDocument();
    });
  });
});
