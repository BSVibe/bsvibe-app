import RequireAuth from "@/components/auth/RequireAuth";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockReplace } = vi.hoisted(() => ({ mockReplace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/brief",
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

describe("RequireAuth", () => {
  beforeEach(() => {
    mockReplace.mockClear();
    clearSession();
  });

  it("redirects an unauthenticated visitor to /login and hides children", () => {
    render(
      <RequireAuth>
        <div>secret surface</div>
      </RequireAuth>,
    );

    expect(mockReplace).toHaveBeenCalledWith("/login");
    expect(screen.queryByText("secret surface")).not.toBeInTheDocument();
  });

  it("renders children for an authenticated session without redirecting", () => {
    setSession(SESSION);

    render(
      <RequireAuth>
        <div>secret surface</div>
      </RequireAuth>,
    );

    expect(screen.getByText("secret surface")).toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
  });
});
