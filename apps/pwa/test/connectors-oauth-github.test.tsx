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
 *   - ConnectorRow (L6 3b) shows a SINGLE connected indicator — the green
 *     "Connected" pill — and drops the redundant "Connected as @login" OAuth
 *     line. Only a needs_reauth binding surfaces a "Reconnect with GitHub" CTA.
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

describe("Lift 1 — ConnectorRow github connect state (Reconnect / Switch-to-OAuth always available)", () => {
  it("a healthy OAuth-backed github row keeps the single green pill AND offers Reconnect (rotate)", () => {
    // Revises L6 3b: a connected binding still shows ONE connected indicator
    // (the green pill, no duplicate 'Connected as @login' line), but now also
    // surfaces a Reconnect affordance so a user can rotate/recover the OAuth
    // credential without first revoking (previously only reachable via the
    // backend-driven needs_reauth state — a dead end for healthy rotation).
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "github", oauth_account_label: "@octocat" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByText(/^Connected$/i)).toBeInTheDocument();
    // No redundant identity line…
    expect(screen.queryByText(/@octocat/)).toBeNull();
    expect(screen.queryByText(/connected as/i)).toBeNull();
    // …but a Reconnect action is now available.
    expect(screen.getByRole("button", { name: /reconnect with github/i })).toBeInTheDocument();
  });

  it("a PAT-backed github row (no oauth label) offers Reconnect to migrate onto OAuth", () => {
    // The gh-e2e case: a connected github binding authenticated by a classic
    // PAT (no oauth_account_label). It must expose a way to (re)connect via
    // OAuth in-place — otherwise there is NO UI path to migrate a PAT binding
    // to OAuth or to rotate the PAT on a healthy connector.
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "github", oauth_account_label: null })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByText(/^Connected$/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reconnect with github/i })).toBeInTheDocument();
  });

  it("surfaces a single Reconnect CTA when the bound token needs re-auth", () => {
    render(
      <ConnectorRow
        connector={makeConnector({
          connector: "github",
          oauth_account_label: "@octocat",
          needs_reauth: true,
        })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /reconnect with github/i })).toBeInTheDocument();
    // Still no duplicate "Connected as" identity chip.
    expect(screen.queryByText(/connected as/i)).toBeNull();
  });

  it("does not offer Reconnect on a revoked (inactive) github row", () => {
    render(
      <ConnectorRow
        connector={makeConnector({
          connector: "github",
          is_active: false,
          oauth_account_label: "@octocat",
        })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /reconnect with github/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /connect with github/i })).toBeNull();
  });

  it("hides the meaningless detail line (webhook token hint + external ref) for an oauth github row", () => {
    // The detail line shows the webhook_token's last 4 chars + external_ref.
    // For an outbound OAuth github connector there is no inbound webhook, so the
    // hint is meaningless noise and the repo ref is redundant on a connected
    // card — drop the whole line for oauth connectors.
    render(
      <ConnectorRow
        connector={makeConnector({
          connector: "github",
          external_ref: "blas1n/bsvibe-gh-e2e",
          token_hint: "...wxyz",
          oauth_account_label: "@blas1n",
        })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.queryByText("...wxyz")).toBeNull();
    expect(screen.queryByText("blas1n/bsvibe-gh-e2e")).toBeNull();
  });
});
