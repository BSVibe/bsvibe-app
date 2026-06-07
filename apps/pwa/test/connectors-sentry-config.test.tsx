/**
 * Sentry operator setup needs an extra "integration slug" field.
 *
 * Sentry's external-install URL is built from the integration's slug, so the
 * operator pastes client_id + client_secret + slug. `requireSlug` flips the
 * form to render + require the slug and passes it as the 4th save arg. Vanilla
 * providers (no slug) keep calling save with three args.
 */

import { ProviderAppConfig } from "@/components/settings/ProviderAppConfig";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => vi.clearAllMocks());

describe("ProviderAppConfig with requireSlug (sentry)", () => {
  it("saves client_id + secret + slug", async () => {
    const save = vi.fn().mockResolvedValue({ provider: "sentry", configured: true });
    const onSaved = vi.fn();
    render(
      <ProviderAppConfig
        provider="sentry"
        requireSlug
        save={save}
        onSaved={onSaved}
        onCancel={() => {}}
      />,
    );

    await userEvent.type(screen.getByLabelText(/client id/i), "cid");
    await userEvent.type(screen.getByLabelText(/client secret/i), "sec");
    await userEvent.type(screen.getByLabelText(/integration slug/i), "bsvibe-app");
    await userEvent.click(screen.getByRole("button", { name: /save & connect/i }));

    await waitFor(() => {
      expect(save).toHaveBeenCalledWith("sentry", "cid", "sec", "bsvibe-app");
      expect(onSaved).toHaveBeenCalled();
    });
  });

  it("does not submit until the slug is filled", async () => {
    const save = vi.fn();
    render(
      <ProviderAppConfig
        provider="sentry"
        requireSlug
        save={save}
        onSaved={vi.fn()}
        onCancel={() => {}}
      />,
    );
    await userEvent.type(screen.getByLabelText(/client id/i), "cid");
    await userEvent.type(screen.getByLabelText(/client secret/i), "sec");
    // slug still empty
    await userEvent.click(screen.getByRole("button", { name: /save & connect/i }));
    expect(save).not.toHaveBeenCalled();
  });

  it("vanilla providers still save with three args (no slug field)", async () => {
    const save = vi.fn().mockResolvedValue({ provider: "slack", configured: true });
    render(
      <ProviderAppConfig provider="slack" save={save} onSaved={vi.fn()} onCancel={() => {}} />,
    );
    expect(screen.queryByLabelText(/integration slug/i)).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/client id/i), "cid");
    await userEvent.type(screen.getByLabelText(/client secret/i), "sec");
    await userEvent.click(screen.getByRole("button", { name: /save & connect/i }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("slack", "cid", "sec"));
  });
});
