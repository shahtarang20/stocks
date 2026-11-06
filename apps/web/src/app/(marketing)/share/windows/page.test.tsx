// @vitest-environment jsdom
// W14: the Windows sharing page states the limits in plain words and does not promise an installer. WRITTEN, NOT RUN.
// Bug re-insertion: delete the "NVIDIA only" or "Ordinary jobs only" paragraph (the limits test fails); change the notice to
// "Download the installer" (the not-available test fails).
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import ShareWindowsPage from "./page";

afterEach(cleanup);

describe("share a Windows PC page", () => {
  it("has exactly one h1 and its sections in heading order", () => {
    const { container } = render(<ShareWindowsPage />);
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    const levels = Array.from(container.querySelectorAll("h1,h2,h3")).map((h) => Number(h.tagName[1]));
    for (let i = 1; i < levels.length; i++) expect(levels[i]! - levels[i - 1]!).toBeLessThanOrEqual(1);
  });

  it("says plainly that Windows hosts get the container tier and NVIDIA cards only", () => {
    const { container } = render(<ShareWindowsPage />);
    const text = container.textContent ?? "";
    expect(text).toMatch(/Ordinary jobs only/);
    expect(text).toMatch(/private jobs/i);
    expect(text).toMatch(/NVIDIA only/);
    expect(text).toMatch(/AMD graphics cards are only supported on Linux/);
  });

  it("does not offer a download and says it is not available yet", () => {
    const { container } = render(<ShareWindowsPage />);
    expect(screen.getByRole("note").textContent).toMatch(/Not available to install yet/);
    expect(container.textContent).not.toMatch(/download the installer/i);
    expect(container.querySelector('a[href$=".msi"], a[href$=".exe"]')).toBeNull();
  });

  it("explains every requirement check in an accessible table", () => {
    render(<ShareWindowsPage />);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(3);
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(1 + 7);
    for (const name of ["Virtualization", "NVIDIA graphics driver", "Free disk space"]) {
      expect(within(table).getByRole("rowheader", { name })).toBeTruthy();
    }
    expect(table.querySelector("caption")).not.toBeNull();
  });

  it("uses descriptive links", () => {
    render(<ShareWindowsPage />);
    for (const a of screen.getAllByRole("link")) {
      expect((a.textContent ?? "").trim().toLowerCase()).not.toMatch(/^(click here|here|more|read more)$/);
    }
  });
});
