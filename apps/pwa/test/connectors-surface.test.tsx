/**
 * Connectors catalog surface — the Settings → Connectors section, reframed as a
 * catalog (CONNECTED cards / AVAILABLE cards / custom-MCP). Drives the real
 * list/create/revoke clients against a mocked fetch and asserts:
 *
 *  - empty CONNECTED state when no connectors exist (calm note, no cards)
 *  - CONNECTED renders a card per active connector (name, ref, masked hint,
 *    "Connected" pill) with a real Revoke and a disabled (coming-soon) Configure
 *  - AVAILABLE renders the not-yet-connected supported connectors as ENABLED
 *    "Connect" cards, and the custom-MCP card as a DISABLED "coming soon"
 *    control. The catalog shows ONLY real connectors — no aspirational
 *    (Figma/Linear/…) coming-soon cards (L6 3c).
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
    // A non-oauth connector (telegram) still shows the ref + masked webhook
    // hint. OAuth connectors (github, …) drop that line — the webhook hint is
    // meaningless for them — covered in connectors-oauth-github.test.tsx.
    const telegramRow = { ...GITHUB_ROW, connector: "telegram", external_ref: "ops" };
    global.fetch = vi.fn(async () => jsonResponse([telegramRow])) as unknown as typeof fetch;

    render(<Connectors />);

    const connected = await screen.findByRole("list", { name: /connected/i });
    expect(within(connected).getByText("telegram")).toBeInTheDocument();
    expect(within(connected).getByText("ops")).toBeInTheDocument();
    expect(within(connected).getByText("...wxyz")).toBeInTheDocument();
    expect(within(connected).getByText(/^Connected$/i)).toBeInTheDocument();
    // Real revoke action present.
    expect(within(connected).getByRole("button", { name: /^Revoke$/i })).toBeInTheDocument();
  });

  it("does NOT render a 'delivers out' label even when delivery_config has keys (L6 3a)", async () => {
    const rowWithDelivery = { ...GITHUB_ROW, delivery_config: { repo: "acme/widgets" } };
    global.fetch = vi.fn(async () => jsonResponse([rowWithDelivery])) as unknown as typeof fetch;

    render(<Connectors />);

    const connected = await screen.findByRole("list", { name: /connected/i });
    // The non-functional outbound label (no UI acts on it) is removed.
    expect(within(connected).queryByText(/delivers out/i)).not.toBeInTheDocument();
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

  it("does NOT render aspirational coming-soon cards (L6 3c — catalog shows only real connectors)", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<Connectors />);

    const available = await screen.findByRole("list", { name: /available/i });
    for (const name of ["Figma", "Linear", "Google Drive", "PowerPoint", "Postgres"]) {
      expect(within(available).queryByText(name)).not.toBeInTheDocument();
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
    // telegram is a secret-based (non-OAuth) connector, so the Add form's
    // signing-secret + delivery_config create path is exercised here (notion
    // flipped to OAuth in Lift 3).
    const created = {
      id: "22222222-2222-2222-2222-222222222222",
      connector: "telegram",
      external_ref: "ops",
      is_active: true,
      created_at: "2026-05-23T00:00:00Z",
      delivery_config: { chat_id: "123" },
      webhook_token: "ONE-TIME-TOKEN-abcd",
      webhook_url: "/api/webhooks/telegram/ONE-TIME-TOKEN-abcd",
    };
    const fetchMock = vi
      .fn()
      // initial list (empty)
      .mockResolvedValueOnce(jsonResponse([]))
      // pending-installs fetch on mount (Sentry claim-later)
      .mockResolvedValueOnce(jsonResponse({ unclaimed: [] }))
      // create
      .mockResolvedValueOnce(jsonResponse(created, 201))
      // re-read list after create
      .mockResolvedValueOnce(jsonResponse([{ ...GITHUB_ROW, connector: "telegram" }]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    // Open the create panel from telegram's Connect card.
    const available = await screen.findByRole("list", { name: /available/i });
    const telegramCard = within(available).getByText("Telegram").closest("li") as HTMLElement;
    await userEvent.click(within(telegramCard).getByRole("button", { name: /^Connect$/i }));

    // The panel is pre-selected to telegram.
    const select = (await screen.findByLabelText("Connector")) as HTMLSelectElement;
    expect(select.value).toBe("telegram");

    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    await userEvent.type(screen.getByLabelText(/Reference/i), "ops");
    // fireEvent.change sets the textarea value directly — userEvent.type would
    // mis-parse the JSON braces as keyboard modifiers.
    fireEvent.change(screen.getByLabelText(/Delivery config/i), {
      target: { value: '{"chat_id":"123"}' },
    });
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    // The one-time secret panel appears with both the URL and the token.
    await waitFor(() => {
      expect(screen.getByText("/api/webhooks/telegram/ONE-TIME-TOKEN-abcd")).toBeInTheDocument();
    });
    expect(screen.getByText("ONE-TIME-TOKEN-abcd")).toBeInTheDocument();
    expect(screen.getByText(/won.t see (this|it) again/i)).toBeInTheDocument();

    // Assert the create POST carried the form body. (calls: [0] list, [1]
    // pending-installs, [2] create POST, [3] re-read.)
    const createCall = fetchMock.mock.calls[2] as unknown as [string, RequestInit];
    expect(createCall[0]).toBe("/api/v1/connectors");
    expect(createCall[1].method).toBe("POST");
    expect(JSON.parse(createCall[1].body as string)).toEqual({
      connector: "telegram",
      signing_secret: "shh",
      external_ref: "ops",
      delivery_config: { chat_id: "123" },
    });

    // A re-read fired (list, pending-installs, create, re-read).
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
  });

  it("rejects an invalid delivery_config JSON before firing the request", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ unclaimed: [] }));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    const available = await screen.findByRole("list", { name: /available/i });
    const telegramCard = within(available).getByText("Telegram").closest("li") as HTMLElement;
    await userEvent.click(within(telegramCard).getByRole("button", { name: /^Connect$/i }));

    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    fireEvent.change(screen.getByLabelText(/Delivery config/i), {
      target: { value: "{not json" },
    });
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    expect(await screen.findByText(/not valid JSON/i)).toBeInTheDocument();
    // No POST fired — only the initial list GET + pending-installs fetch on mount.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows a calm inline error when create fails and keeps the form usable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ unclaimed: [] }))
      .mockResolvedValueOnce(jsonResponse("bad", 422));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);

    const available = await screen.findByRole("list", { name: /available/i });
    const telegramCard = within(available).getByText("Telegram").closest("li") as HTMLElement;
    await userEvent.click(within(telegramCard).getByRole("button", { name: /^Connect$/i }));

    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    expect(await screen.findByText(/Couldn.t register/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Add connector$/i })).toBeEnabled();
  });

  it("revokes a connected connector after confirm → DELETE → re-read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([GITHUB_ROW]))
      .mockResolvedValueOnce(jsonResponse({ unclaimed: [] }))
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

    // calls: [0] list, [1] pending-installs, [2] DELETE, [3] re-read.
    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls[2] as unknown as [string, RequestInit];
      expect(deleteCall[0]).toBe(`/api/v1/connectors/${GITHUB_ROW.id}`);
      expect(deleteCall[1].method).toBe("DELETE");
    });
    // A re-read fired after the revoke.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
  });

  it("shows a calm inline error when revoke fails — card stays actionable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([GITHUB_ROW]))
      .mockResolvedValueOnce(jsonResponse({ unclaimed: [] }))
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
