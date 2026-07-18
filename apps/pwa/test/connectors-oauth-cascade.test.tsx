/**
 * Lift 2-4 — slack / notion / discord flip to "Connect with X" (vanilla OAuth).
 * github keeps its App-aware Setup control; the others use the plain Connect
 * button on both the Add form and the connector card. sentry stays secret-based
 * (its install→grant connect flow isn't wired yet).
 */

import ConnectorRow from "@/components/settings/ConnectorRow";
import { descriptorFor, isOAuthConnector } from "@/components/settings/connector-fields";
import type { Connector } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/connectors", () => ({
  startConnectorOAuth: vi.fn(),
}));

function makeConnector(over: Partial<Connector> & { connector: string }): Connector {
  return {
    id: "row-1",
    external_ref: null,
    is_active: true,
    created_at: "2026-06-03T00:00:00Z",
    delivery_config: {},
    token_hint: "...wxyz",
    outbound: true,
    importable: ["obsidian", "claude", "gpt", "notion"].includes(over.connector),
    webhook_trigger: false,
    last_import_at: null,
    last_import_count: null,
    ...over,
  };
}

describe("Lift 2-4 — vanilla OAuth cascade", () => {
  it("isOAuthConnector covers github/slack/notion/discord but not sentry/telegram", () => {
    for (const c of ["github", "slack", "notion", "discord"] as const) {
      expect(isOAuthConnector(c)).toBe(true);
    }
    expect(isOAuthConnector("sentry")).toBe(false);
    expect(isOAuthConnector("telegram")).toBe(false);
  });

  it("slack + discord descriptors are oauth Connect (no password field)", () => {
    for (const c of ["slack", "discord"] as const) {
      const d = descriptorFor(c);
      expect(d.fields.find((f) => f.kind === "oauth")?.oauthProvider).toBe(c);
      expect(d.fields.some((f) => f.kind === "password")).toBe(false);
    }
  });

  it("ConnectorRow shows a single Connected pill AND a Reconnect action for a connected slack binding", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "slack", oauth_account_label: null })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByText(/^Connected$/i)).toBeInTheDocument();
    // Single connected indicator (green pill), no duplicate identity line…
    expect(screen.queryByText(/connected as/i)).toBeNull();
    // …but every connected oauth binding now offers a Reconnect action so the
    // credential can be rotated/recovered on demand (consistent with github).
    expect(screen.getByRole("button", { name: /reconnect with slack/i })).toBeInTheDocument();
  });

  it("ConnectorRow drops the redundant 'Connected as @workspace' line for a connected notion binding (L6 3b)", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "notion", oauth_account_label: "Docs HQ" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByText(/^Connected$/i)).toBeInTheDocument();
    expect(screen.queryByText(/Docs HQ/)).toBeNull();
  });

  it("ConnectorRow surfaces a single Reconnect CTA when a slack binding needs re-auth (L6 3b)", () => {
    render(
      <ConnectorRow
        connector={makeConnector({
          connector: "slack",
          oauth_account_label: null,
          needs_reauth: true,
        })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /reconnect with slack/i })).toBeInTheDocument();
  });
});
