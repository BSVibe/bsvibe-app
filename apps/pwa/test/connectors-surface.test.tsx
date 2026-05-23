/**
 * Connectors surface — the Settings → Connectors section. Drives the real
 * list/create/revoke clients against a mocked fetch and asserts:
 *
 *  - empty state when no connectors exist
 *  - list renders each connector (name, external_ref, masked hint, active)
 *  - Add: POST fires with the form body; the one-time webhook_url + token are
 *    shown prominently with a "won't see this again" note, then a re-read fires
 *  - Revoke: confirm → DELETE fires → re-read fires
 *  - calm inline error states (create + revoke) never crash the surface
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

describe("Connectors surface", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a calm empty state when there are no connectors", async () => {
    global.fetch = vi.fn(async () => jsonResponse([])) as unknown as typeof fetch;

    render(<Connectors />);

    await waitFor(() => {
      expect(screen.getByText(/No connectors yet/i)).toBeInTheDocument();
    });
  });

  it("lists registered connectors with name, ref, masked hint and active state", async () => {
    global.fetch = vi.fn(async () => jsonResponse([GITHUB_ROW])) as unknown as typeof fetch;

    render(<Connectors />);

    await waitFor(() => {
      expect(screen.getByText("github")).toBeInTheDocument();
    });
    expect(screen.getByText("acme/widgets")).toBeInTheDocument();
    expect(screen.getByText("...wxyz")).toBeInTheDocument();
    expect(screen.getByText(/Active/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Revoke/i })).toBeInTheDocument();
  });

  it("creates a connector, shows the one-time webhook URL + token, then re-reads", async () => {
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
    await waitFor(() => expect(screen.getByText(/No connectors yet/i)).toBeInTheDocument());

    // Fill the form
    await userEvent.selectOptions(screen.getByLabelText("Connector"), "notion");
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
    await waitFor(() => expect(screen.getByText(/No connectors yet/i)).toBeInTheDocument());

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
    await waitFor(() => expect(screen.getByText(/No connectors yet/i)).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    expect(await screen.findByText(/Couldn.t register/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Add connector$/i })).toBeEnabled();
  });

  it("revokes a connector after confirm → DELETE → re-read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([GITHUB_ROW]))
      .mockResolvedValueOnce(jsonResponse(null, 204))
      .mockResolvedValueOnce(jsonResponse([{ ...GITHUB_ROW, is_active: false }]));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("github")).toBeInTheDocument());

    const row = within(list).getByText("github").closest("li");
    if (!row) throw new Error("connector row not found");
    await userEvent.click(within(row as HTMLElement).getByRole("button", { name: /^Revoke$/i }));

    // Confirm affordance appears; clicking it fires the DELETE.
    const confirm = await within(row as HTMLElement).findByRole("button", {
      name: /^Confirm revoke$/i,
    });
    await userEvent.click(confirm);

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
      expect(deleteCall[0]).toBe(`/api/v1/connectors/${GITHUB_ROW.id}`);
      expect(deleteCall[1].method).toBe("DELETE");
    });
    // A re-read fired after the revoke.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("shows a calm inline error when revoke fails — row stays actionable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([GITHUB_ROW]))
      .mockResolvedValueOnce(jsonResponse("boom", 500));
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<Connectors />);
    const list = await screen.findByRole("list");
    await waitFor(() => expect(within(list).getByText("github")).toBeInTheDocument());

    const row = within(list).getByText("github").closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: /^Revoke$/i }));
    await userEvent.click(within(row).getByRole("button", { name: /^Confirm revoke$/i }));

    expect(await within(row).findByText(/Couldn.t revoke/i)).toBeInTheDocument();
  });

  it("surfaces a calm note when the list read fails", async () => {
    global.fetch = vi.fn(
      async () => new Response("boom", { status: 500 }),
    ) as unknown as typeof fetch;

    render(<Connectors />);

    await waitFor(() => {
      expect(screen.getByText(/Couldn.t load your connectors/i)).toBeInTheDocument();
    });
  });
});
