/**
 * Connector picker — the AddConnector <select>, now driven by the fetched
 * connector catalog (GET /api/v1/connectors/catalog, INV-1 single source of
 * truth) rather than a hardcoded mirror.
 *
 * email-sender is a valid OUTBOUND connector
 * (backend/workflow/application/delivery/connector_dispatch.py OUTBOUND_EVENT_BUILDERS)
 * that the create validator accepts via the outbound branch. This pins that the
 * picker offers whatever the catalog returns (including email-sender, and NOT a
 * bare "email"), and that selecting it sends `connector: "email-sender"`.
 */

import AddConnector from "@/components/settings/AddConnector";
import type { ConnectorCatalogEntry, ConnectorCreate, ConnectorCreated } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

function entry(name: string, over: Partial<ConnectorCatalogEntry> = {}): ConnectorCatalogEntry {
  return {
    name,
    outbound: true,
    importable: false,
    webhook_trigger: false,
    artifact_types: [],
    import_action: null,
    ...over,
  };
}

const CATALOG: ConnectorCatalogEntry[] = [
  entry("github"),
  entry("telegram"),
  entry("email-sender"),
];

describe("connector picker — email-sender", () => {
  it("offers the catalog connectors as options (email-sender, not a bare 'email')", () => {
    render(<AddConnector catalog={CATALOG} onCreated={() => {}} createConnector={vi.fn()} />);

    const select = screen.getByLabelText("Connector") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("email-sender");
    expect(optionValues).not.toContain("email");
  });

  it("sends connector: 'email-sender' when it is selected and submitted", async () => {
    const created: ConnectorCreated = {
      id: "ee",
      connector: "email-sender",
      external_ref: null,
      is_active: true,
      created_at: "2026-05-23T00:00:00Z",
      delivery_config: {},
      webhook_token: "tok",
      webhook_url: "/api/webhooks/email-sender/tok",
    };
    const createConnector = vi.fn(
      async (_input: ConnectorCreate): Promise<ConnectorCreated> => created,
    );

    render(
      <AddConnector catalog={CATALOG} onCreated={() => {}} createConnector={createConnector} />,
    );

    await userEvent.selectOptions(screen.getByLabelText("Connector"), "email-sender");
    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    expect(createConnector).toHaveBeenCalledTimes(1);
    expect(createConnector.mock.calls[0][0].connector).toBe("email-sender");
  });
});
