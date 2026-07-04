/**
 * Direct compose submit flow — opens the overlay, types, submits, and asserts
 * it POSTs /api/v1/messages and shows the success / error states. fetch mocked.
 */

import { DIRECT_SUBMITTED_EVENT, DirectOverlay } from "@/components/shell/DirectAction";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Default pathname for the non-product cases. Individual tests override.
let _currentPathname = "/brief";
vi.mock("next/navigation", () => ({
  usePathname: () => _currentPathname,
}));

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const ACCEPTED = { accepted: true, duplicate: false, workspace_id: "ws-1" };

describe("Direct compose", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not render when closed", () => {
    render(<DirectOverlay open={false} onClose={() => {}} />);
    expect(screen.queryByRole("dialog", { name: "Direct" })).not.toBeInTheDocument();
  });

  it("disables submit until the textarea has content", async () => {
    render(<DirectOverlay open onClose={() => {}} />);
    const submit = screen.getByRole("button", { name: "Direct" });
    expect(submit).toBeDisabled();

    await userEvent.type(screen.getByRole("textbox"), "draft the launch post");
    expect(submit).toBeEnabled();
  });

  it("POSTs /api/v1/messages, shows success, emits the refresh event", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(ACCEPTED), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const onSubmitted = vi.fn();
    window.addEventListener(DIRECT_SUBMITTED_EVENT, onSubmitted);

    render(<DirectOverlay open onClose={() => {}} />);
    await userEvent.type(screen.getByRole("textbox"), "draft the launch post");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText("Sent. Working on it.")).toBeInTheDocument();
    });

    // Hit the real endpoint with a JSON body carrying the typed text. (The
    // overlay also reads /api/v1/products on open to populate the target
    // selector, so we locate the /messages POST rather than asserting a count.)
    const messagesCall = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([url]) => String(url).endsWith("/api/v1/messages"),
    );
    if (!messagesCall) throw new Error("expected a /messages POST");
    const [, init] = messagesCall;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ text: "draft the launch post" });

    expect(onSubmitted).toHaveBeenCalledTimes(1);
    window.removeEventListener(DIRECT_SUBMITTED_EVENT, onSubmitted);
  });

  it("passes product_id when submitting from a /products/<slug> route", async () => {
    // W2 follow-up: dogfood found that a Direct submission from a product
    // page silently fell back to the workspace's default product because
    // the dialog ignored route context. Lock in the fix.
    _currentPathname = "/products/w2-dogfood";

    const PRODUCTS = [
      {
        id: "ab1c2d3e-1111-1111-1111-111111111111",
        workspace_id: "ws-1",
        name: "Other",
        slug: "other-product",
      },
      {
        id: "ef9b8a7c-2222-2222-2222-222222222222",
        workspace_id: "ws-1",
        name: "W2",
        slug: "w2-dogfood",
      },
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/v1/products")) {
        return new Response(JSON.stringify(PRODUCTS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(ACCEPTED), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<DirectOverlay open onClose={() => {}} />);
    // Wait for the slug→id lookup to settle before submitting.
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/products"),
        expect.anything(),
      );
    });

    await userEvent.type(screen.getByRole("textbox"), "ship the add() helper");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText("Sent. Working on it.")).toBeInTheDocument();
    });

    // The POST to /messages carries the resolved product_id.
    const messagesCall = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([url]) => url.endsWith("/api/v1/messages"),
    );
    if (!messagesCall) throw new Error("expected a /messages POST");
    const init = messagesCall[1];
    expect(JSON.parse(init.body as string)).toEqual({
      text: "ship the add() helper",
      product_id: "ef9b8a7c-2222-2222-2222-222222222222",
    });

    // Reset for the next case.
    _currentPathname = "/brief";
  });

  it("offers a target selector and submits the chosen product_id", async () => {
    _currentPathname = "/brief"; // not on a product page → defaults to Auto
    const PRODUCTS = [
      { id: "p-aaaa", workspace_id: "ws-1", name: "Alpha", slug: "alpha" },
      { id: "p-bbbb", workspace_id: "ws-1", name: "Beta", slug: "beta" },
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/products")) {
        return new Response(JSON.stringify(PRODUCTS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(ACCEPTED), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<DirectOverlay open onClose={() => {}} />);
    // The selector appears once products load, with an Auto default.
    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    expect(screen.getByRole("option", { name: "Alpha" })).toBeInTheDocument();

    // Pick Beta explicitly, then submit.
    await userEvent.selectOptions(select, "p-bbbb");
    await userEvent.type(screen.getByRole("textbox"), "ship it");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText("Sent. Working on it.")).toBeInTheDocument();
    });

    const messagesCall = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([url]) => String(url).endsWith("/api/v1/messages"),
    );
    if (!messagesCall) throw new Error("expected a /messages POST");
    expect(JSON.parse(messagesCall[1].body as string)).toEqual({
      text: "ship it",
      product_id: "p-bbbb",
    });
    _currentPathname = "/brief";
  });

  it("L10: a question is answered INLINE and is NOT dispatched as a run", async () => {
    _currentPathname = "/brief";
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/messages/ask")) {
        return new Response(
          JSON.stringify({ answered: true, answer: "The project shipped 9 lifts this round." }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.endsWith("/api/v1/products")) {
        return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify(ACCEPTED), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<DirectOverlay open onClose={() => {}} />);
    await userEvent.type(screen.getByRole("textbox"), "how's the project doing?");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    // The answer renders inline.
    await waitFor(() => {
      expect(screen.getByText(/shipped 9 lifts this round/)).toBeInTheDocument();
    });
    // It was NOT dispatched as a run (no POST to /api/v1/messages).
    const dispatched = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([url]) => String(url).endsWith("/api/v1/messages"),
    );
    expect(dispatched).toBeUndefined();
  });

  it("L10: the inline answer renders markdown (bold/code/list), not raw syntax", async () => {
    _currentPathname = "/brief";
    const md = "Status: **shipped** the `title-case` helper.\n\n- one\n- two";
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/messages/ask")) {
        return new Response(JSON.stringify({ answered: true, answer: md }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/v1/products")) {
        return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify(ACCEPTED), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    const { container } = render(<DirectOverlay open onClose={() => {}} />);
    await userEvent.type(screen.getByRole("textbox"), "status?");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(container.querySelector(".direct-overlay__answer")).toBeInTheDocument();
    });
    // Markdown is rendered to real elements, not literal ** / ` / - syntax.
    expect(container.querySelector(".direct-overlay__answer strong")?.textContent).toBe("shipped");
    expect(container.querySelector(".direct-overlay__answer code")?.textContent).toBe("title-case");
    expect(container.querySelectorAll(".direct-overlay__answer li")).toHaveLength(2);
    expect(container.querySelector(".direct-overlay__answer")?.textContent).not.toContain("**");
  });

  it("L10: passes the target product_id to /messages/ask so the answer is grounded", async () => {
    // The grounding fix: the Direct question must tell the backend WHICH product
    // it's about, else the answer is generated as if the workspace were empty.
    _currentPathname = "/products/toolkit";
    const PRODUCTS = [{ id: "prod-tk-1", workspace_id: "ws-1", name: "toolkit", slug: "toolkit" }];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/products")) {
        return new Response(JSON.stringify(PRODUCTS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/v1/messages/ask")) {
        return new Response(JSON.stringify({ answered: true, answer: "Status: on track." }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify(ACCEPTED), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<DirectOverlay open onClose={() => {}} />);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/products"),
        expect.anything(),
      );
    });
    await userEvent.type(screen.getByRole("textbox"), "how's the project?");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText(/on track/)).toBeInTheDocument();
    });
    const askCall = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([url]) => String(url).endsWith("/api/v1/messages/ask"),
    );
    if (!askCall) throw new Error("expected a /messages/ask POST");
    expect(JSON.parse(askCall[1].body as string)).toEqual({
      text: "how's the project?",
      product_id: "prod-tk-1",
    });
    _currentPathname = "/brief";
  });

  it("L10: a work request (answered=false) is dispatched as a run", async () => {
    _currentPathname = "/brief";
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/messages/ask")) {
        return new Response(JSON.stringify({ answered: false, answer: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url.endsWith("/api/v1/products")) {
        return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify(ACCEPTED), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<DirectOverlay open onClose={() => {}} />);
    await userEvent.type(screen.getByRole("textbox"), "build a TTL cache");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText("Sent. Working on it.")).toBeInTheDocument();
    });
    const dispatched = (fetchMock.mock.calls as unknown as Array<[string, RequestInit]>).find(
      ([url]) => String(url).endsWith("/api/v1/messages"),
    );
    expect(dispatched).toBeTruthy();
  });

  it("shows an error state when the submit fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<DirectOverlay open onClose={() => {}} />);
    await userEvent.type(screen.getByRole("textbox"), "do the thing");
    fireEvent.click(screen.getByRole("button", { name: "Direct" }));

    await waitFor(() => {
      expect(screen.getByText("Couldn’t send that. Please try again.")).toBeInTheDocument();
    });
  });
});
