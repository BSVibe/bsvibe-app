/**
 * "Replace token" on a token-connected card, and the revoke warning for a
 * connector that product bindings still point at.
 *
 *   - a pasted-token connector (telegram) offers Replace token; an OAuth one
 *     (github) keeps its Reconnect control instead and does not.
 *   - saving sends ONLY the new token for THAT connector id, then clears the
 *     field — the token is never rendered back.
 *   - a 422 (provider rejected the token) says nothing was changed.
 *   - the revoke confirmation names the bindings it would orphan.
 */

import ConnectorRow from "@/components/settings/ConnectorRow";
import { ApiError } from "@/lib/api/client";
import type { Connector, ConnectorCredentialRotated } from "@/lib/api/types";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

function makeConnector(over: Partial<Connector> & { connector: string }): Connector {
  return {
    id: "row-1",
    external_ref: null,
    is_active: true,
    created_at: "2026-06-03T00:00:00Z",
    delivery_config: { chat_id: "42" },
    token_hint: "...wxyz",
    outbound: true,
    importable: false,
    webhook_trigger: true,
    last_import_at: null,
    last_import_count: null,
    ...over,
  };
}

function rotated(over: Partial<ConnectorCredentialRotated> = {}): ConnectorCredentialRotated {
  return { connector: makeConnector({ connector: "telegram" }), webhook_registered: true, ...over };
}

describe("ConnectorRow — Replace token", () => {
  it("offers Replace token on a token connector but not on an OAuth one", () => {
    const { unmount } = render(
      <ConnectorRow
        connector={makeConnector({ connector: "telegram" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        rotateCredential={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /replace token/i })).toBeInTheDocument();
    unmount();

    render(
      <ConnectorRow
        connector={makeConnector({ connector: "github" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        rotateCredential={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /replace token/i })).toBeNull();
  });

  it("sends only the new token for this connector, then clears the field", async () => {
    const rotate = vi.fn().mockResolvedValue(rotated());
    const onUpdated = vi.fn();
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "telegram" })}
        onRevoked={() => {}}
        onUpdated={onUpdated}
        revoke={vi.fn()}
        rotateCredential={rotate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /replace token/i }));
    const field = screen.getByLabelText(/new token/i) as HTMLInputElement;
    expect(field.type).toBe("password");
    fireEvent.change(field, { target: { value: "222222:new-token" } });
    fireEvent.click(screen.getByRole("button", { name: /save new token/i }));

    await waitFor(() => expect(rotate).toHaveBeenCalledWith("row-1", "222222:new-token"));
    await waitFor(() => expect(onUpdated).toHaveBeenCalled());
    expect(screen.getByText(/token replaced/i)).toBeInTheDocument();
    expect(screen.queryByDisplayValue("222222:new-token")).toBeNull();
  });

  it("says the webhook needs a retry when the token was saved but re-registration failed", async () => {
    const rotate = vi.fn().mockResolvedValue(rotated({ webhook_registered: false }));
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "telegram" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        rotateCredential={rotate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /replace token/i }));
    fireEvent.change(screen.getByLabelText(/new token/i), { target: { value: "t" } });
    fireEvent.click(screen.getByRole("button", { name: /save new token/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /re-registering the webhook failed/i,
    );
  });

  it("tells the founder nothing changed when the provider rejects the token (422)", async () => {
    const rotate = vi.fn().mockRejectedValue(new ApiError(422, "rejected"));
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "telegram" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        rotateCredential={rotate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /replace token/i }));
    fireEvent.change(screen.getByLabelText(/new token/i), { target: { value: "typo" } });
    fireEvent.click(screen.getByRole("button", { name: /save new token/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/nothing was changed/i);
  });
});

describe("ConnectorRow — revoke warns about bound products", () => {
  it("names the bindings a revoke would orphan", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "telegram", binding_count: 2 })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("revoke-bound-warning")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^revoke$/i }));
    expect(screen.getByTestId("revoke-bound-warning")).toHaveTextContent(/2 product bindings/i);
  });

  it("stays quiet when nothing points at the connector", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "telegram", binding_count: 0 })}
        onRevoked={() => {}}
        revoke={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^revoke$/i }));
    expect(screen.queryByTestId("revoke-bound-warning")).toBeNull();
  });
});
