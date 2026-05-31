/**
 * Connector picker — the KNOWN_CONNECTORS allow-list + the AddConnector select.
 *
 * PR #45 built KNOWN_CONNECTORS before the email-sender backend builder landed,
 * so it was excluded. email-sender is now a valid OUTBOUND connector
 * (backend/workflow/application/delivery/connector_dispatch.py OUTBOUND_EVENT_BUILDERS), and the
 * create validator (backend/api/v1/connectors.py) accepts it via the outbound
 * branch. This test pins that:
 *
 *  - email-sender is in KNOWN_CONNECTORS (and the name is email-sender, NOT email)
 *  - the AddConnector picker offers it as an <option>
 *  - selecting it sends connector: "email-sender" on the create POST
 */

import AddConnector from "@/components/settings/AddConnector";
import { KNOWN_CONNECTORS } from "@/lib/api/types";
import type { ConnectorCreate, ConnectorCreated } from "@/lib/api/types";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

describe("connector picker — email-sender", () => {
  it("includes email-sender in KNOWN_CONNECTORS (and not a bare 'email')", () => {
    expect(KNOWN_CONNECTORS).toContain("email-sender");
    expect(KNOWN_CONNECTORS).not.toContain("email");
  });

  it("offers email-sender as an option in the AddConnector picker", () => {
    render(<AddConnector onCreated={() => {}} createConnector={vi.fn()} />);

    const select = screen.getByLabelText("Connector") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("email-sender");
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

    render(<AddConnector onCreated={() => {}} createConnector={createConnector} />);

    await userEvent.selectOptions(screen.getByLabelText("Connector"), "email-sender");
    await userEvent.type(screen.getByLabelText(/Signing secret/i), "shh");
    await userEvent.click(screen.getByRole("button", { name: /^Add connector$/i }));

    expect(createConnector).toHaveBeenCalledTimes(1);
    expect(createConnector.mock.calls[0][0].connector).toBe("email-sender");
  });
});
