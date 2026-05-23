/**
 * Connectors catalog surface — the Settings → Connectors section, reframed as a
 * catalog (CONNECTED cards / AVAILABLE cards / custom-MCP). Drives the real
 * list/create/revoke clients against a mocked fetch and asserts:
 *
 *  - empty CONNECTED state when no connectors exist (calm note, no cards)
 *  - CONNECTED renders a card per active connector (name, ref, masked hint,
 *    "Connected" pill) with a real Revoke and a disabled (coming-soon) Configure
 *  - AVAILABLE renders the not-yet-connected supported connectors as ENABLED
 *    "Connect" cards, and the aspirational services (Figma/Linear/…) + the
 *    custom-MCP card as DISABLED "coming soon" controls
 *  - Connect → opens the create panel pre-selected → POST fires with the form
 *    body → the one-time webhook_url + token are shown with a "won't see again"
 *    note → a re-read fires
 *  - Revoke: confirm → DELETE fires → re-read fires
 *  - calm inline error states (create + revoke) never crash the surface
 *
 * Determinism note: the connector list loads asynchronously on mount, so every
 * assertion that depends on it is gated behind `findBy*`/`waitFor` — we never
 * read a synchronous `getByText` immediately after `render`. This removes the
 * race that made the previous (row-list) version of this test flaky.
 */

import Connectors from "@/components/settings/Connectors";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const GITHUB_ROW = {
  id: "11111111-1111-1111-1111-111111111111",
  connector: "github",
  external_ref: "acme/widgets",
  is_active: true,
  created_at: "2026-05-23T00:00:00Z",
  delivery_config: {},
  token_hint: "...wxyz",
};

describe("Connectors catalog surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps the Connectors heading so the tab host can find it", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;
    render(<Connectors />);
    expect(screen.getByRole("heading", { name: /connectors/i })).toBeInTheDocument();
    // Settle the async load so the test doesn't leak an unawaited state update.
    await screen.findByText(/Nothing connected yet/i);
  });

  it("shows a calm empty CONNECTED state when there are no connectors", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<Connectors />);

    expect(await screen.findByText(/Nothing connected yet/i)).toBeInTheDocument();
  });

  it("renders a CONNECTED card per active connector with name, ref, masked hint and pill", async () => {
    global.fetch = vi.fn(async () => jsonResponse([GITHUB_ROW])) as unknown as typeof fetch;

    render(<Connectors />);

    const connected = await screen.findByRole("list", { name: /connected/i });
    expect(within(connected).getByText("github")).toBeInTheDocument();
    expect(within(connected).getByText("acme/widgets")).toBeInTheDocument();
    expect(within(connected).getByText("...wxyz")).toBeInTheDocument();
    expect(within(connected).getByText(/^Connected$/i)).toBeInTheDocument();
    // Real revoke action present.
    expect(within(connected).getByRole("button", { name: /^Revoke$/i })).toBeInTheDocument();
  });

  it("disables Configure on a connected card (no update API yet)", async () => {
    global.fetch = vi.fn(async () => jsonResponse([GITHUB_ROW])) as unknown as typeof fetch;

    render(<Connectors />);

    const connected = await screen.findByRole("list", { name: /connected/i });
    const configure = within(connected).getByRole("button", { name: /^Configure$/i });
    expect(configure).toBeDisabled();
    expect(configure).toHaveAttribute("title");
  });

  it("renders not-yet-connected supported connectors as enabled Connect cards", async () => {
    // github is connected; the rest of KNOWN_CONNECTORS are available.
    global.fetch = vi.fn(async () => jsonResponse([GITHUB_ROW])) as unknown as typeof fetch;

    render(<Connectors />);

    const available = await screen.findByRole("list", { name: /available/i });
    // notion is supported and not connected → an enabled Connect card.
    const notionCard = within(available).getByText("Notion").closest("li") as HTMLElement;
    expect(within(notionCard).getByRole("button", { name: /^Connect$/i })).toBeEnabled();
    // github is connected → it must NOT appear in Available as a Connect card.
    expect(within(available).queryByText("github")).not.toBeInTheDocument();
  });

  it("renders aspirational services as disabled coming-soon cards", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<Connectors />);

    const available = await screen.findByRole("list", { name: /available/i });
    for (const name of ["Figma", "Linear", "Google Drive", "PowerPoint", "Postgres"]) {
      const card = within(available).getByText(name).closest("li") as HTMLElement;
      const btn = within(card).getByRole("button", { name: /^Connect$/i });
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute("title");
    }
  });

  it("renders the custom-MCP card with a disabled coming-soon Add custom button", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<Connectors />);

    await screen.findByText(/Nothing connected yet/i);
    const addCustom = screen.getByRole("button", { name: /add custom/i });
    expect(addCustom).toBeDisabled();
    expect(addCustom).toHaveAttribute("title");
  });

  it("Connect → opens the create panel, fires POST with the body, reveals the one-time token, then re-reads", async () => {
    const created = {
      id: "22222222-2222-2222-2222-222222222222",
      connector: "notion",
      external_ref: "ops",
      is_active: true,
      created_at: "2026-05-23T00:00:00Z",
      delivery_config: { parent_page_id: "pp-1" },
      webhook_token: "ONE-TIME-TOKEN-abcd",
      webhook_url: "/api/webhooks/notion/ONE-TIME-TOKEN-abcd",
    };
    const fetchMock = vi
      .fn()
      // initial list (empty)
      .mockResolvedValueOnce(jsonResponse([]))
      // create
      .mockResolvedValueOnce(jsonResponse(created, 201))
      // re-read list after create
      .mockResolvedValueOnce(jsonResponse([{ ...GITHUB_ROW, connector: "notion" }]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    // Open the create panel from notion's Connect card.
    const available = await screen.findByRole("list", { name: /available/i });
    const notionCard = within(available).getByText("Notion").closest("li") as HTMLElement;
    await userEvent.click(within(notionCard).getByRole("button", { name: /^Connect$/i }));

    // The panel is pre-selected to notion.
    const select = (await screen.findByLabelText("Connector")) as HTMLSelectElement;
    expect(select.value).toBe("notion");

    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    await userEvent.type(screen.getByLabelText(/Reference/i), "ops");
    // fireEvent.change sets the textarea value directly — userEvent.type would
    // mis-parse the JSON braces as keyboard modifiers.
    fireEvent.change(screen.getByLabelText(/Delivery config/i), {
      target: { value: '{"parent_page_id":"pp-1"}' },
    });
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    // The one-time secret panel appears with both the URL and the token.
    await waitFor(() => {
      expect(screen.getByText("/api/webhooks/notion/ONE-TIME-TOKEN-abcd")).toBeInTheDocument();
    });
    expect(screen.getByText("ONE-TIME-TOKEN-abcd")).toBeInTheDocument();
    expect(screen.getByText(/won.t see (this|it) again/i)).toBeInTheDocument();

    // Assert the create POST carried the form body.
    const createCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(createCall[0]).toBe("/api/v1/connectors");
    expect(createCall[1].method).toBe("POST");
    expect(JSON.parse(createCall[1].body as string)).toEqual({
      connector: "notion",
      signing_secret: "shh",
      external_ref: "ops",
      delivery_config: { parent_page_id: "pp-1" },
    });

    // A re-read fired (3rd call is the list GET).
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("rejects an invalid delivery_config JSON before firing the request", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse([]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    const available = await screen.findByRole("list", { name: /available/i });
    const notionCard = within(available).getByText("Notion").closest("li") as HTMLElement;
    await userEvent.click(within(notionCard).getByRole("button", { name: /^Connect$/i }));

    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    fireEvent.change(screen.getByLabelText(/Delivery config/i), {
      target: { value: "{not json" },
    });
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    expect(await screen.findByText(/not valid JSON/i)).toBeInTheDocument();
    // No POST fired — only the initial list GET.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows a calm inline error when create fails and keeps the form usable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse("bad", 422));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    const available = await screen.findByRole("list", { name: /available/i });
    const notionCard = within(available).getByText("Notion").closest("li") as HTMLElement;
    await userEvent.click(within(notionCard).getByRole("button", { name: /^Connect$/i }));

    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    expect(await screen.findByText(/Couldn.t register/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Add connector$/i })).toBeEnabled();
  });

  it("revokes a connected connector after confirm → DELETE → re-read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([GITHUB_ROW]))
      .mockResolvedValueOnce(jsonResponse(null, 204))
      .mockResolvedValueOnce(jsonResponse([{ ...GITHUB_ROW, is_active: false }]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    const connected = await screen.findByRole("list", { name: /connected/i });
    const card = within(connected).getByText("github").closest("li") as HTMLElement;
    await userEvent.click(within(card).getByRole("button", { name: /^Revoke$/i }));

    // Confirm affordance appears; clicking it fires the DELETE.
    const confirm = await within(card).findByRole("button", { name: /^Confirm revoke$/i });
    await userEvent.click(confirm);

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(deleteCall[0]).toBe(`/api/v1/connectors/${GITHUB_ROW.id}`);
      expect(deleteCall[1].method).toBe("DELETE");
    });
    // A re-read fired after the revoke.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("shows a calm inline error when revoke fails — card stays actionable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([GITHUB_ROW]))
      .mockResolvedValueOnce(jsonResponse("boom", 500));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    const connected = await screen.findByRole("list", { name: /connected/i });
    const card = within(connected).getByText("github").closest("li") as HTMLElement;
    await userEvent.click(within(card).getByRole("button", { name: /^Revoke$/i }));
    await userEvent.click(within(card).getByRole("button", { name: /^Confirm revoke$/i }));

    expect(await within(card).findByText(/Couldn.t revoke/i)).toBeInTheDocument();
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<Connectors />);

    expect(await screen.findByText(/Couldn.t load your connectors/i)).toBeInTheDocument();
  });
});
