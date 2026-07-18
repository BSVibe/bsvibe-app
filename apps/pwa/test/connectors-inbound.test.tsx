/**
 * Lift B — inbound connector UI proofs (INV-1: catalog-driven).
 *
 *   - AddConnector renders different fields per connector
 *     (obsidian → vault_path; claude/gpt → export_path; notion → OAuth +
 *     optional inbound block).
 *   - Selecting an inbound connector and submitting packs the per-connector
 *     fields into `delivery_config` on the wire — the legacy JSON textarea
 *     is suppressed for inbound-only connectors.
 *   - ConnectorRow shows "Import now" ONLY when the row's `importable` flag
 *     (from the catalog) is true. Outbound connectors (github) and
 *     push-only-inbound (slack) do NOT show it.
 *   - Clicking "Import now" calls the injected `triggerImport`, surfaces the
 *     last-imported summary, and re-reads the list.
 *   - The connectors client `triggerImport` wraps the response correctly and
 *     POSTs to the right URL with an empty JSON body.
 */

import AddConnector from "@/components/settings/AddConnector";
import ConnectorRow from "@/components/settings/ConnectorRow";
import { triggerImport } from "@/lib/api/connectors";
import type {
  Connector,
  ConnectorCatalogEntry,
  ConnectorCreate,
  ConnectorCreated,
  ConnectorImportResult,
} from "@/lib/api/types";
import { type Session, clearSession, setSession } from "@/lib/auth/session";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const SESSION: Session = {
  accessToken: "tok",
  refreshToken: "ref",
  email: "founder@bsvibe.dev",
  userId: "user-1",
  expiresAt: Date.now() + 3_600_000,
};

const IMPORTABLE = new Set(["obsidian", "claude", "gpt", "notion"]);
const INBOUND = new Set(["obsidian", "claude", "gpt"]);

function entry(name: string): ConnectorCatalogEntry {
  return {
    name,
    outbound: !INBOUND.has(name),
    importable: IMPORTABLE.has(name),
    webhook_trigger: false,
    artifact_types: [],
    import_action: IMPORTABLE.has(name) ? "import_action" : null,
  };
}

const CATALOG: ConnectorCatalogEntry[] = [
  "github",
  "slack",
  "telegram",
  "discord",
  "sentry",
  "notion",
  "email-sender",
  "obsidian",
  "claude",
  "gpt",
].map(entry);

function makeCreated(connector: string): ConnectorCreated {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    connector,
    external_ref: null,
    is_active: true,
    created_at: "2026-06-03T00:00:00Z",
    delivery_config: {},
    webhook_token: "tok",
    webhook_url: `/api/webhooks/${connector}/tok`,
  };
}

function makeConnector(over: Partial<Connector> & { connector: string }): Connector {
  return {
    id: "row-1",
    external_ref: null,
    is_active: true,
    created_at: "2026-06-03T00:00:00Z",
    delivery_config: {},
    token_hint: "...wxyz",
    outbound: !INBOUND.has(over.connector),
    importable: IMPORTABLE.has(over.connector),
    webhook_trigger: false,
    last_import_at: null,
    last_import_count: null,
    ...over,
  };
}

describe("AddConnector — per-connector field branching", () => {
  it("renders obsidian fields (vault path, exclude patterns, region) and hides the JSON delivery_config", async () => {
    render(
      <AddConnector
        catalog={CATALOG}
        onCreated={() => {}}
        createConnector={vi.fn()}
        initialConnector="obsidian"
      />,
    );

    expect(screen.getByLabelText(/Vault path/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Exclude patterns/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Region/i)).toBeInTheDocument();
    // Outbound JSON delivery_config is suppressed for inbound-only.
    expect(screen.queryByLabelText(/Delivery config/i)).not.toBeInTheDocument();
    // No webhook signing-secret either — it's injected as a placeholder
    // on the wire.
    expect(screen.queryByLabelText(/Signing secret/i)).not.toBeInTheDocument();
  });

  it("renders claude / gpt with an export path field, no signing secret", () => {
    const { rerender } = render(
      <AddConnector
        catalog={CATALOG}
        onCreated={() => {}}
        createConnector={vi.fn()}
        initialConnector="claude"
      />,
    );
    expect(screen.getByLabelText(/Export path/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Signing secret/i)).not.toBeInTheDocument();

    rerender(
      <AddConnector
        catalog={CATALOG}
        onCreated={() => {}}
        createConnector={vi.fn()}
        initialConnector="gpt"
      />,
    );
    expect(screen.getByLabelText(/Export path/i)).toBeInTheDocument();
  });

  it("renders notion as OAuth Connect (no secret/api_token) + database_ids + delivery_config", () => {
    // Lift 3 — notion flipped to "Connect with Notion"; OAuth provides the
    // token, so the signing-secret + api_token password fields are gone.
    render(
      <AddConnector
        catalog={CATALOG}
        onCreated={() => {}}
        createConnector={vi.fn()}
        initialConnector="notion"
      />,
    );
    expect(screen.getByRole("button", { name: /connect with notion/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Signing secret/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Notion API token/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Database IDs/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Delivery config/i)).toBeInTheDocument();
  });

  it("renders github as an OAuth Connect (no signing secret) + JSON delivery_config", () => {
    // Lift 1 — github flipped from a pasted PAT/secret to "Connect with GitHub".
    render(
      <AddConnector
        catalog={CATALOG}
        onCreated={() => {}}
        createConnector={vi.fn()}
        initialConnector="github"
      />,
    );
    expect(screen.getByRole("button", { name: /connect with github/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Signing secret/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Delivery config/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Vault path/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Export path/i)).not.toBeInTheDocument();
  });

  it("packs obsidian fields into delivery_config and sends the signing-secret placeholder", async () => {
    const createConnector = vi.fn(
      async (_input: ConnectorCreate): Promise<ConnectorCreated> => makeCreated("obsidian"),
    );

    render(
      <AddConnector
        catalog={CATALOG}
        onCreated={() => {}}
        createConnector={createConnector}
        initialConnector="obsidian"
      />,
    );

    await userEvent.type(screen.getByLabelText(/Vault path/i), "/Users/me/Vault");
    await userEvent.type(screen.getByLabelText(/Exclude patterns/i), ".obsidian/**\nTemplates/**");
    await userEvent.type(screen.getByLabelText(/Region/i), "imported");
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    await waitFor(() => expect(createConnector).toHaveBeenCalledTimes(1));
    const payload = createConnector.mock.calls[0][0];
    expect(payload.connector).toBe("obsidian");
    expect(payload.signing_secret).toBe("no-webhook-secret");
    expect(payload.delivery_config).toEqual({
      vault_path: "/Users/me/Vault",
      exclude_patterns: [".obsidian/**", "Templates/**"],
      default_region: "imported",
    });
  });

  it("packs claude export_path into delivery_config", async () => {
    const createConnector = vi.fn(
      async (_input: ConnectorCreate): Promise<ConnectorCreated> => makeCreated("claude"),
    );

    render(
      <AddConnector
        catalog={CATALOG}
        onCreated={() => {}}
        createConnector={createConnector}
        initialConnector="claude"
      />,
    );

    await userEvent.type(
      screen.getByLabelText(/Export path/i),
      "/Users/me/Downloads/conversations.json",
    );
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    await waitFor(() => expect(createConnector).toHaveBeenCalledTimes(1));
    const payload = createConnector.mock.calls[0][0];
    expect(payload.connector).toBe("claude");
    expect(payload.signing_secret).toBe("no-webhook-secret");
    expect(payload.delivery_config).toEqual({
      export_path: "/Users/me/Downloads/conversations.json",
    });
  });
});

describe("ConnectorRow — Import now affordance", () => {
  it("does NOT render Import now for an outbound-only connector (github)", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "github" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        triggerImport={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /Import now/i })).toBeNull();
  });

  it("does NOT render Import now for slack (webhook-driven inbound, not importable)", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "slack" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        triggerImport={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /Import now/i })).toBeNull();
  });

  it.each(["obsidian", "claude", "gpt", "notion"])(
    "renders Import now for the %s connector (importable flag)",
    (name) => {
      render(
        <ConnectorRow
          connector={makeConnector({ connector: name })}
          onRevoked={() => {}}
          revoke={vi.fn()}
          triggerImport={vi.fn()}
        />,
      );
      expect(screen.getByRole("button", { name: /Import now/i })).toBeInTheDocument();
    },
  );

  it("clicking Import now calls triggerImport and updates the row stamp on success", async () => {
    const result: ConnectorImportResult = {
      imported_count: 7,
      last_import_at: "2026-06-03T12:00:00Z",
      detail: { notes_count: 7 },
    };
    const trigger = vi.fn(async (_id: string) => result);
    const onImported = vi.fn();
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "obsidian" })}
        onRevoked={() => {}}
        onImported={onImported}
        revoke={vi.fn()}
        triggerImport={trigger}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /Import now/i }));
    await waitFor(() => expect(trigger).toHaveBeenCalledWith("row-1"));
    expect(onImported).toHaveBeenCalledTimes(1);
    // The "Last imported 7 · …" stamp now appears.
    expect(screen.getByText(/Last imported 7/i)).toBeInTheDocument();
  });

  it("shows the import-error note when triggerImport rejects", async () => {
    const trigger = vi.fn(async () => {
      throw new Error("nope");
    });
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "obsidian" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        triggerImport={trigger}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /Import now/i }));
    await waitFor(() => expect(screen.getByText(/Import failed/i)).toBeInTheDocument());
  });
});

describe("connectors client — triggerImport", () => {
  beforeEach(() => {
    clearSession();
    setSession(SESSION);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("POSTs to /api/v1/connectors/{id}/import with an empty JSON body", async () => {
    const result: ConnectorImportResult = {
      imported_count: 3,
      last_import_at: "2026-06-03T10:00:00Z",
      detail: { conversations_count: 3 },
    };
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify(result), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const res = await triggerImport("abcd-1234");

    expect(res).toEqual(result);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/connectors/abcd-1234/import");
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
  });
});
