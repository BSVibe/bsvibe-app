/**
 * Settings tab IA — the top tab bar that fronts the 5-tab Settings surface.
 *
 * This is the serialization point: the nav MUST enumerate all five tabs
 * (General / Models / Connectors / Notifications / Account) in order, as real
 * shareable links under /settings/*, so later lifts only fill content and never
 * touch the nav. The active tab is marked `aria-current="page"`.
 */

import SettingsTabs from "@/components/settings/SettingsTabs";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

const LABELS = ["General", "Models", "Connectors", "Notifications", "Account"] as const;

describe("Settings tab nav", () => {
  it("renders all five tabs in order as links", () => {
    render(<SettingsTabs active="general" />);
    const links = screen.getAllByRole("link");
    const names = links.map((l) => l.textContent?.trim());
    expect(names).toEqual([...LABELS]);
  });

  it("points each tab at its /settings/<slug> route", () => {
    render(<SettingsTabs active="general" />);
    expect(screen.getByRole("link", { name: "General" })).toHaveAttribute(
      "href",
      "/settings/general",
    );
    expect(screen.getByRole("link", { name: "Models" })).toHaveAttribute(
      "href",
      "/settings/models",
    );
    expect(screen.getByRole("link", { name: "Connectors" })).toHaveAttribute(
      "href",
      "/settings/connectors",
    );
    expect(screen.getByRole("link", { name: "Notifications" })).toHaveAttribute(
      "href",
      "/settings/notifications",
    );
    expect(screen.getByRole("link", { name: "Account" })).toHaveAttribute(
      "href",
      "/settings/account",
    );
  });

  it("marks the active tab with aria-current=page and no others", () => {
    render(<SettingsTabs active="models" />);
    const active = screen.getByRole("link", { name: "Models" });
    expect(active).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "General" })).not.toHaveAttribute("aria-current");
  });
});
