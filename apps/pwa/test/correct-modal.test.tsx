/**
 * CorrectModal — the calm correction surface (Lift M3b, design §3.3). Asserts:
 *  - the modal renders the node name in the heading + the calm consequence lede
 *  - the replacement textarea is required (Save disabled while empty / blank)
 *  - clicking Save calls `onConfirm({replacement, reason})` with trimmed text
 *  - while pending, Save shows "Saving…" and is disabled
 *  - a failed onConfirm renders an inline error + keeps the modal open
 *  - clicking Cancel calls onCancel without onConfirm
 *  - role=dialog + aria-modal=true (AT contract preserved)
 */

import CorrectModal from "@/components/knowledge/CorrectModal";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

describe("CorrectModal", () => {
  it("renders the node name + a calm consequence lede", () => {
    render(<CorrectModal nodeName="rate-limit" onConfirm={async () => {}} onCancel={() => {}} />);

    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent('Correct "rate-limit"?');
    expect(screen.getByText(/Replace the body of this note/i)).toBeInTheDocument();
  });

  it("disables Save while the replacement is empty / whitespace-only", () => {
    render(<CorrectModal nodeName="rate-limit" onConfirm={async () => {}} onCancel={() => {}} />);

    expect(screen.getByRole("button", { name: "Save correction" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Replacement/i), {
      target: { value: "   " },
    });

    expect(screen.getByRole("button", { name: "Save correction" })).toBeDisabled();
  });

  it("clicking Save calls onConfirm with trimmed replacement + reason", async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(<CorrectModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/Replacement/i), {
      target: { value: "  the new body  " },
    });
    fireEvent.change(screen.getByLabelText(/Why\? \(optional\)/i), {
      target: { value: "  fixed wrong year  " },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));

    await waitFor(() =>
      expect(onConfirm).toHaveBeenCalledWith({
        replacement: "the new body",
        reason: "fixed wrong year",
      }),
    );
  });

  it("shows pending state while onConfirm is in flight", async () => {
    let resolve: (() => void) | undefined;
    const onConfirm = vi.fn().mockImplementation(
      () =>
        new Promise<void>((r) => {
          resolve = r;
        }),
    );
    render(<CorrectModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/Replacement/i), {
      target: { value: "the new body" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled());

    await act(async () => {
      resolve?.();
    });
  });

  it("surfaces an inline error when onConfirm rejects + keeps modal open", async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error("boom"));
    render(<CorrectModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/Replacement/i), {
      target: { value: "the new body" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));

    await waitFor(() =>
      expect(
        screen.getByText("Couldn't save that just now. Try again in a moment."),
      ).toBeInTheDocument(),
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Cancel calls onCancel without onConfirm", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<CorrectModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={onCancel} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
