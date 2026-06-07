/**
 * Operator paste-creds setup for vanilla OAuth providers (slack/notion/discord).
 *
 * Those providers have no manifest auto-create, so the operator pastes the
 * client_id/secret they made in the provider console; on save the provider is
 * configured and the connect proceeds. github keeps the manifest path (no form).
 */

import { ProviderAppConfig } from "@/components/settings/ProviderAppConfig";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => vi.clearAllMocks());

describe("ProviderAppConfig", () => {
  it("saves pasted creds then calls onSaved", async () => {
    const save = vi.fn().mockResolvedValue({ provider: "slack", configured: true });
    const onSaved = vi.fn();
    render(
      <ProviderAppConfig provider="slack" save={save} onSaved={onSaved} onCancel={() => {}} />,
    );

    await userEvent.type(screen.getByLabelText(/client id/i), "Iv1.cid");
    await userEvent.type(screen.getByLabelText(/client secret/i), "sec");
    await userEvent.click(screen.getByRole("button", { name: /save & connect/i }));

    await waitFor(() => {
      expect(save).toHaveBeenCalledWith("slack", "Iv1.cid", "sec");
      expect(onSaved).toHaveBeenCalled();
    });
  });

  it("does not submit with empty fields", async () => {
    const save = vi.fn();
    render(
      <ProviderAppConfig provider="notion" save={save} onSaved={vi.fn()} onCancel={() => {}} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /save & connect/i }));
    expect(save).not.toHaveBeenCalled();
  });

  it("shows a calm error when save fails, no onSaved", async () => {
    const save = vi.fn().mockRejectedValue(new Error("bad creds"));
    const onSaved = vi.fn();
    render(
      <ProviderAppConfig provider="discord" save={save} onSaved={onSaved} onCancel={() => {}} />,
    );
    await userEvent.type(screen.getByLabelText(/client id/i), "x");
    await userEvent.type(screen.getByLabelText(/client secret/i), "y");
    await userEvent.click(screen.getByRole("button", { name: /save & connect/i }));

    expect(await screen.findByText(/couldn.t save|invalid|error/i)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });
});
