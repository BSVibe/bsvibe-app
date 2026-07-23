/**
 * Authorized-approvers editor proofs (slack/discord interactive-approval
 * allowlist). The editor writes `delivery_config.authorized_user_ids` via
 * `PATCH /api/v1/connectors/{id}` (the backend shallow-merges), so:
 *
 *  - it renders ONLY for slack/discord connectors — never telegram (which
 *    authorizes by chat_id, not a user list) nor an outbound-only connector;
 *  - it prefills from the row's current `delivery_config`;
 *  - on save it sends ONLY the keys it owns (`authorized_user_ids`, plus the
 *    optional team_id/guild_id) — never a secret — and the backend merge keeps
 *    the rest.
 */

import ConnectorRow from "@/components/settings/ConnectorRow";
import type { Connector } from "@/lib/api/types";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

function makeConnector(over: Partial<Connector> & { connector: string }): Connector {
  return {
    id: "row-1",
    external_ref: null,
    is_active: true,
    created_at: "2026-06-03T00:00:00Z",
    delivery_config: {},
    token_hint: "...wxyz",
    outbound: true,
    importable: false,
    webhook_trigger: true,
    last_import_at: null,
    last_import_count: null,
    ...over,
  };
}

describe("ApproversEditor — slack/discord authorized approvers", () => {
  it("renders the editor for a slack connector", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "slack" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        updateConnector={vi.fn()}
      />,
    );
    expect(screen.getByLabelText(/Authorized approvers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Slack team ID/i)).toBeInTheDocument();
  });

  it("renders the editor for a discord connector (guild scope)", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "discord" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        updateConnector={vi.fn()}
      />,
    );
    expect(screen.getByLabelText(/Authorized approvers/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Discord guild ID/i)).toBeInTheDocument();
  });

  it("does NOT render the editor for telegram (authorizes by chat_id)", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "telegram" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        updateConnector={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText(/Authorized approvers/i)).toBeNull();
  });

  it("does NOT render the editor for an outbound-only connector (github)", () => {
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "github" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        updateConnector={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText(/Authorized approvers/i)).toBeNull();
  });

  it("prefills the textarea from delivery_config.authorized_user_ids", () => {
    render(
      <ConnectorRow
        connector={makeConnector({
          connector: "slack",
          delivery_config: { authorized_user_ids: ["U1", "U2"], team_id: "T9" },
        })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        updateConnector={vi.fn()}
      />,
    );
    expect(screen.getByLabelText(/Authorized approvers/i)).toHaveValue("U1\nU2");
    expect(screen.getByLabelText(/Slack team ID/i)).toHaveValue("T9");
  });

  it("on save sends ONLY { delivery_config: { authorized_user_ids } } when no team id", async () => {
    const update = vi.fn(async (_id: string, _patch: unknown) =>
      makeConnector({ connector: "slack" }),
    );
    const onUpdated = vi.fn();
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "slack" })}
        onRevoked={() => {}}
        onUpdated={onUpdated}
        revoke={vi.fn()}
        updateConnector={update}
      />,
    );

    await userEvent.type(screen.getByLabelText(/Authorized approvers/i), "U1\nU2");
    await userEvent.click(screen.getByRole("button", { name: /Save approvers/i }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update).toHaveBeenCalledWith("row-1", {
      delivery_config: { authorized_user_ids: ["U1", "U2"] },
    });
    expect(onUpdated).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/^Saved$/i)).toBeInTheDocument();
  });

  it("includes team_id in the patch when the scope field is filled", async () => {
    const update = vi.fn(async (_id: string, _patch: unknown) =>
      makeConnector({ connector: "slack" }),
    );
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "slack" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        updateConnector={update}
      />,
    );

    await userEvent.type(screen.getByLabelText(/Authorized approvers/i), "U1");
    await userEvent.type(screen.getByLabelText(/Slack team ID/i), "T42");
    await userEvent.click(screen.getByRole("button", { name: /Save approvers/i }));

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    expect(update).toHaveBeenCalledWith("row-1", {
      delivery_config: { authorized_user_ids: ["U1"], team_id: "T42" },
    });
  });

  it("shows an error note when the save rejects", async () => {
    const update = vi.fn(async () => {
      throw new Error("nope");
    });
    render(
      <ConnectorRow
        connector={makeConnector({ connector: "discord" })}
        onRevoked={() => {}}
        revoke={vi.fn()}
        updateConnector={update}
      />,
    );

    await userEvent.type(screen.getByLabelText(/Authorized approvers/i), "U1");
    await userEvent.click(screen.getByRole("button", { name: /Save approvers/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });
});
