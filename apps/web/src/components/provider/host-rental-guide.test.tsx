// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { HostRentalGuide } from "./host-rental-guide";
import { HOST_RENTAL_COMMANDS, HOST_RENTAL_NEEDS, HOST_RENTAL_RISKS } from "@/lib/rental";

afterEach(() => cleanup());

describe("HostRentalGuide", () => {
  it("shows every command a host must run, in the right order", () => {
    render(<HostRentalGuide />);
    const text = document.body.textContent ?? "";
    const order = [HOST_RENTAL_COMMANDS.check, HOST_RENTAL_COMMANDS.on, HOST_RENTAL_COMMANDS.restart, HOST_RENTAL_COMMANDS.status, HOST_RENTAL_COMMANDS.off].map((c) =>
      text.indexOf(c),
    );
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order); // check, then on, then restart, then status, then off
  });

  it("lists what the computer needs and the risks, each as its own item", () => {
    render(<HostRentalGuide />);
    for (const n of HOST_RENTAL_NEEDS) expect(screen.getByText(n.lead)).toBeTruthy();
    for (const r of HOST_RENTAL_RISKS) expect(screen.getByText(r)).toBeTruthy();
  });

  it("has a named section for each part, with headings in the right level", () => {
    render(<HostRentalGuide />);
    for (const name of [/what your computer needs first/i, /turn it on, in this order/i, /what happens when a customer rents/i, /know the risks/i, /turn it off/i]) {
      expect(screen.getByRole("heading", { level: 3, name })).toBeTruthy();
    }
  });

  it("can sit one level lower inside another section", () => {
    render(<HostRentalGuide headingLevel={4} />);
    expect(screen.getByRole("heading", { level: 4, name: /turn it off/i })).toBeTruthy();
    expect(screen.queryAllByRole("heading", { level: 3 })).toHaveLength(0);
  });

  it("says it is optional and off until the host turns it on, and tells the host how to turn it off", () => {
    render(<HostRentalGuide />);
    expect(document.body.textContent).toMatch(/off until you turn it on/i);
    const off = screen.getByRole("heading", { name: /turn it off/i }).closest("section")!;
    expect(within(off).getByText(HOST_RENTAL_COMMANDS.off)).toBeTruthy();
  });
});
