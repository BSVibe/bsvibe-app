/**
 * CopyField — copy-to-clipboard control. It must not claim success ("Copied")
 * when the Clipboard API is unavailable (insecure context / old browser), where
 * `navigator.clipboard?.writeText` short-circuits to `undefined` and the await
 * resolves without anything being copied.
 */

import CopyField from "@/components/settings/CopyField";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("CopyField", () => {
  afterEach(() => vi.restoreAllMocks());

  it("copies the value and flips to Copied when the clipboard API works", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });

    render(<CopyField label="Token" value="secret-value" />);
    fireEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("secret-value"));
    await waitFor(() => expect(screen.getByRole("button").textContent).toMatch(/copied/i));
  });

  it("does NOT falsely show Copied when the clipboard API is unavailable", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });

    render(<CopyField label="Token" value="secret-value" />);
    fireEvent.click(screen.getByRole("button"));

    // Let any microtasks settle — nothing was copied, so the label must stay "Copy".
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.getByRole("button").textContent).not.toMatch(/copied/i);
  });
});
