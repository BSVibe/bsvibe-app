/**
 * Unit tests for the SSE live-events React hook (B16).
 *
 * The hook opens an EventSource against the backend SSE endpoint and
 * dispatches each `decision.pending`, `run.terminal`, and `delivery.queued`
 * message to the registered handlers. Tests run against a mock EventSource
 * (the real one is a browser API) so we can drive each message synchronously
 * and assert the handler is invoked.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useEventStream } from "@/lib/live-events/use-event-stream";

interface FakeMessageEvent {
  data: string;
}

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  readonly listeners = new Map<string, ((event: FakeMessageEvent) => void)[]>();
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: FakeMessageEvent) => void): void {
    const bucket = this.listeners.get(type) ?? [];
    bucket.push(listener);
    this.listeners.set(type, bucket);
  }

  removeEventListener(type: string, listener: (event: FakeMessageEvent) => void): void {
    const bucket = this.listeners.get(type);
    if (!bucket) return;
    this.listeners.set(
      type,
      bucket.filter((entry) => entry !== listener),
    );
  }

  close(): void {
    this.closed = true;
  }

  /** Dispatch a fake server-sent event to every listener of `type`. */
  dispatch(type: string, data: object): void {
    const bucket = this.listeners.get(type) ?? [];
    for (const listener of bucket) {
      listener({ data: JSON.stringify(data) });
    }
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  // Install the mock and clean it up afterwards.
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useEventStream", () => {
  it("opens an EventSource at the backend events/stream endpoint with token query param", () => {
    renderHook(() =>
      useEventStream({
        token: "the-jwt",
        onDecisionPending: () => {},
        onRunTerminal: () => {},
        onDeliveryQueued: () => {},
      }),
    );

    expect(MockEventSource.instances).toHaveLength(1);
    const url = MockEventSource.instances[0].url;
    expect(url).toContain("/api/v1/events/stream");
    expect(url).toContain("token=the-jwt");
  });

  it("invokes onDecisionPending when a decision.pending event arrives", () => {
    const onDecisionPending = vi.fn();
    renderHook(() =>
      useEventStream({
        token: "the-jwt",
        onDecisionPending,
      }),
    );

    const source = MockEventSource.instances[0];
    act(() => {
      source.dispatch("decision.pending", { decision_id: "d1", run_id: "r1" });
    });

    expect(onDecisionPending).toHaveBeenCalledTimes(1);
    expect(onDecisionPending).toHaveBeenCalledWith({ decision_id: "d1", run_id: "r1" });
  });

  it("invokes onRunTerminal when a run.terminal event arrives", () => {
    const onRunTerminal = vi.fn();
    renderHook(() =>
      useEventStream({
        token: "the-jwt",
        onRunTerminal,
      }),
    );

    const source = MockEventSource.instances[0];
    act(() => {
      source.dispatch("run.terminal", { run_id: "r1", outcome: "verified" });
    });

    expect(onRunTerminal).toHaveBeenCalledWith({ run_id: "r1", outcome: "verified" });
  });

  it("invokes onDeliveryQueued when a delivery.queued event arrives", () => {
    const onDeliveryQueued = vi.fn();
    renderHook(() =>
      useEventStream({
        token: "the-jwt",
        onDeliveryQueued,
      }),
    );

    const source = MockEventSource.instances[0];
    act(() => {
      source.dispatch("delivery.queued", { delivery_id: "x" });
    });

    expect(onDeliveryQueued).toHaveBeenCalledWith({ delivery_id: "x" });
  });

  it("closes the EventSource on unmount", () => {
    const { unmount } = renderHook(() =>
      useEventStream({ token: "the-jwt", onDecisionPending: () => {} }),
    );

    const source = MockEventSource.instances[0];
    expect(source.closed).toBe(false);
    unmount();
    expect(source.closed).toBe(true);
  });

  it("does NOT open an EventSource when token is missing", () => {
    renderHook(() => useEventStream({ token: null, onDecisionPending: () => {} }));
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it("ignores malformed JSON in event data (does not throw, does not call handler)", () => {
    const onDecisionPending = vi.fn();
    renderHook(() => useEventStream({ token: "the-jwt", onDecisionPending }));

    const source = MockEventSource.instances[0];
    // Bypass our typed dispatch helper to hand-craft a bad payload.
    const bucket = source.listeners.get("decision.pending") ?? [];
    act(() => {
      for (const listener of bucket) {
        listener({ data: "{not json" });
      }
    });
    expect(onDecisionPending).not.toHaveBeenCalled();
  });
});
