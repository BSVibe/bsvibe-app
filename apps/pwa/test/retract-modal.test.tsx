/**
 * RetractModal — the pre-flight confirmation a founder sees before a retract
 * lands (Lift M3b). Asserts:
 *  - the modal renders the node name in the heading + the calm lede
 *  - the optional `reason` textarea is empty by default + accepts typing
 *  - clicking Retract calls `onConfirm(reason)` with the trimmed text
 *  - while pending, the Retract button shows "Retracting…" and is disabled
 *  - a failed onConfirm puts an inline error + keeps the modal open with the
 *    typed reason intact (so the founder can retry without retyping)
 *  - clicking Cancel calls onCancel without calling onConfirm
 *  - clicking the backdrop calls onCancel
 *  - the modal has role=dialog + aria-modal=true (AT contract preserved)
 *  - the optional `dependents` block is rendered when provided + hidden when not
 */

import RetractModal from "@/components/knowledge/RetractModal";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

describe("RetractModal", () => {
  it("renders the node name in the heading + a calm consequence lede", () => {
    render(<RetractModal nodeName="rate-limit" onConfirm={async () => {}} onCancel={() => {}} />);

    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent('Retract "rate-limit"?');
    expect(
      screen.getByText("Future runs about this topic will no longer see it."),
    ).toBeInTheDocument();
  });

  it("clicking Retract calls onConfirm with the trimmed reason text", async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(<RetractModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={() => {}} />);

    const textarea = screen.getByLabelText(/Why\? \(optional\)/i);
    fireEvent.change(textarea, { target: { value: "  policy changed  " } });

    fireEvent.click(screen.getByRole("button", { name: "Retract" }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith("policy changed"));
  });

  it("clicking Retract with empty reason calls onConfirm with empty string", async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(<RetractModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Retract" }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith(""));
  });

  it("shows the pending state while onConfirm is in flight", async () => {
    let resolve: (() => void) | undefined;
    const onConfirm = vi.fn().mockImplementation(
      () =>
        new Promise<void>((r) => {
          resolve = r;
        }),
    );

    render(<RetractModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Retract" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Retracting…" })).toBeDisabled());

    await act(async () => {
      resolve?.();
    });
  });

  it("surfaces an inline error when onConfirm rejects + keeps modal open", async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error("boom"));
    render(<RetractModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={() => {}} />);

    const textarea = screen.getByLabelText(/Why\? \(optional\)/i);
    fireEvent.change(textarea, { target: { value: "policy changed" } });

    fireEvent.click(screen.getByRole("button", { name: "Retract" }));

    await waitFor(() =>
      expect(
        screen.getByText("Couldn't retract that just now. Try again in a moment."),
      ).toBeInTheDocument(),
    );

    // Modal stays open + the typed reason is preserved.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(textarea).toHaveValue("policy changed");
  });

  it("clicking Cancel calls onCancel + does not call onConfirm", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<RetractModal nodeName="rate-limit" onConfirm={onConfirm} onCancel={onCancel} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("clicking the backdrop calls onCancel", () => {
    const onCancel = vi.fn();
    render(<RetractModal nodeName="rate-limit" onConfirm={async () => {}} onCancel={onCancel} />);

    fireEvent.click(screen.getByTestId("modal-backdrop"));

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking inside the modal does NOT call onCancel", () => {
    const onCancel = vi.fn();
    render(<RetractModal nodeName="rate-limit" onConfirm={async () => {}} onCancel={onCancel} />);

    fireEvent.click(screen.getByRole("dialog"));

    expect(onCancel).not.toHaveBeenCalled();
  });

  it("renders an optional dependents block when provided", () => {
    render(
      <RetractModal
        nodeName="rate-limit"
        dependents={<p>Surfaced in 2 verify contracts</p>}
        onConfirm={async () => {}}
        onCancel={() => {}}
      />,
    );

    expect(screen.getByText("This was used in:")).toBeInTheDocument();
    expect(screen.getByText("Surfaced in 2 verify contracts")).toBeInTheDocument();
  });

  it("hides the dependents block when not provided (forward-compat with M3a)", () => {
    render(<RetractModal nodeName="rate-limit" onConfirm={async () => {}} onCancel={() => {}} />);

    expect(screen.queryByText("This was used in:")).not.toBeInTheDocument();
  });
});
