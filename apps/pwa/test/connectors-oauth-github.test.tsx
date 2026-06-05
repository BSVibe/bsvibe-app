/**
 * Lift 1 — github flips from a PAT/signing-secret field to a "Connect with
 * GitHub" OAuth control, and the connector card surfaces the connected
 * identity (oauth_account_label) once the OAuth dance completes.
 *
 *   - descriptorFor("github") declares an oauth field (provider github) and no
 *     password secret field; isOAuthConnector("github") is true.
 *   - The Add form renders the Connect button, not a Signing-secret input, but
 *     keeps the delivery_config (repo) JSON.
 *   - github.pack still sends a non-empty signing_secret placeholder (the
 *     backend column is NOT NULL) plus the parsed delivery_config.
 *   - ConnectorRow shows "Connected as @login" when oauth_account_label is set,
 *     and a "Connect with GitHub" button when it is not.
 */

import AddConnector from "@/components/settings/AddConnector";
import ConnectorRow from "@/components/settings/ConnectorRow";
import { descriptorFor, isOAuthConnector } from "@/components/settings/connector-fields";
import { CONNECTOR_KINDS, type Connector } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api/connectors", async (orig) => ({
  ...(await orig<typeof import("@/lib/api/connectors")>()),
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
    kind: CONNECTOR_KINDS[over.connector as keyof typeof CONNECTOR_KINDS] ?? null,
    last_import_at: null,
    last_import_count: null,
    ...over,
  };
}

describe("Lift 1 — github oauth descriptor", () => {
  it("github descriptor is oauth: a Connect field, no password secret field", () => {
    const d = descriptorFor("github");
    const oauthField = d.fields.find((f) => f.kind === "oauth");
    expect(oauthField).toBeDefined();
    expect(oauthField?.oauthProvider).toBe("github");
    expect(d.fields.some((f) => f.kind === "password")).toBe(false);
  });

  it("isOAuthConnector marks github (not telegram)", () => {
    expect(isOAuthConnector("github")).toBe(true);
    expect(isOAuthConnector("telegram")).toBe(false);
  });

  it("github.pack sends the signing-secret placeholder + delivery_config", () => {
    const payload = descriptorFor("github").pack(
      { deliveryConfigParsed: JSON.stringify({ repo: "owner/name" }) },
      "github",
      "ref-1",
    );
    expect(payload.connector).toBe("github");
    expect(payload.signing_secret).toBe("no-webhook-secret");
    expect(payload.delivery_config).toEqual({ repo: "owner/name" });
  });
});

describe("Lift 1 — AddConnector github form", () => {
  it("renders Connect with GitHub, not a signing-secret input", () => {
    render(
      <AddConnector onCreated={() => {}} createConnector={vi.fn()} initialConnector="github" />,
    );
    expect(screen.getByRole("button", { name: /connect with github/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Signing secret/i)).not.toBeInTheDocument();
    // Repo routing config stays.
    expect(screen.getByLabelText(/Delivery config/i)).toBeInTheDocument();
  });
});

describe("Lift 1 — ConnectorRow github connect state", () => {
  it("shows the connected identity when oauth_account_label is set", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "github", oauth_account_label: "@octocat" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByText(/@octocat/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect with github/i })).toBeNull();
  });

  it("shows a Connect button when not yet connected", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "github", oauth_account_label: null })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /connect with github/i })).toBeInTheDocument();
  });
});
