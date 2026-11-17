// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { HostRentalGuide } from "./host-rental-guide";
import {
  HOST_RENTAL_CARE,
  HOST_RENTAL_COMMANDS,
  HOST_RENTAL_DATACENTRE,
  HOST_RENTAL_NEEDS,
  HOST_RENTAL_RISKS,
  HOST_RENTAL_TROUBLE,
  HOST_RENTAL_WHO,
  HOST_RENTAL_WINDOWS,
  HOST_RENTAL_WINDOWS_RESTART,
} from "@/lib/rental";

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

  it("speaks to each kind of host: a home gaming computer, a dedicated computer, and many computers", () => {
    render(<HostRentalGuide />);
    const who = screen.getByRole("heading", { name: /which one are you/i }).closest("section")!;
    expect(HOST_RENTAL_WHO).toHaveLength(3);
    for (const w of HOST_RENTAL_WHO) expect(within(who).getByText(w.lead)).toBeTruthy();
    expect(who.textContent).toMatch(/gaming PC or laptop/i);
    expect(who.textContent).toMatch(/data centre/i);
  });

  it("explains how a host earns, and that the money is test credits", () => {
    render(<HostRentalGuide />);
    expect(document.body.textContent).toMatch(/How you earn\./);
    expect(document.body.textContent).toMatch(/test credits, not real money/);
  });

  it("tells the host to use Docker Engine, and that a computer with no card can offer rentals that need none", () => {
    render(<HostRentalGuide />);
    expect(document.body.textContent).toMatch(/Docker Desktop is not supported/);
    expect(document.body.textContent).toMatch(/No graphics card\?/);
    expect(document.body.textContent).toMatch(/customers choose 0 graphics cards/);
  });

  it("covers setting the price and going Online, which is what actually gets a host rentals", () => {
    render(<HostRentalGuide />);
    expect(document.body.textContent).toMatch(/Set your price and switch to Online/);
    expect(document.body.textContent).toMatch(/Edit price and availability/);
  });

  it("looks after a home computer: sleep, heat and noise, being busy, and nothing private on it", () => {
    render(<HostRentalGuide />);
    const care = screen.getByRole("heading", { name: /looking after your computer/i }).closest("section")!;
    for (const t of HOST_RENTAL_CARE) expect(within(care).getByText(t)).toBeTruthy();
    expect(care.textContent).toMatch(/sleep/);
    expect(care.textContent).toMatch(/Offline/);
  });

  it("answers the common problems", () => {
    render(<HostRentalGuide />);
    const trouble = screen.getByRole("heading", { name: /if something does not work/i }).closest("section")!;
    for (const t of HOST_RENTAL_TROUBLE) {
      expect(within(trouble).getByText(t.problem)).toBeTruthy();
      expect(within(trouble).getByText(t.fix)).toBeTruthy();
    }
  });

  it("has a collapsed section with the extra details for data centres, and it opens", () => {
    render(<HostRentalGuide />);
    const box = screen.getByTestId("hrg-datacentre") as HTMLDetailsElement;
    expect(box.open).toBe(false);
    expect(within(box).getByText("For data centres and many computers")).toBeTruthy();
    for (const d of HOST_RENTAL_DATACENTRE) expect(within(box).getByText(d.lead)).toBeTruthy();
    expect(box.textContent).toMatch(/rental_max_workspace_gb/);
    expect(box.textContent).toMatch(/--allow-rental/);
  });

  it("leads the steps with the quickest way: one line from step 3c with the rental box ticked", () => {
    render(<HostRentalGuide />);
    const quick = screen.getByTestId("hrg-quickest");
    expect(quick.textContent).toMatch(/The quickest way\./);
    expect(quick.textContent).toMatch(/step 3c/);
    expect(quick.textContent).toMatch(/Also let customers run their own code on this computer/);
    expect(quick.textContent).toMatch(/turns container rental on and tests the computer/);
    const steps = screen.getByRole("heading", { name: /turn it on, in this order/i }).closest("section")!;
    expect(steps.firstElementChild?.tagName).toBe("H3");
    expect(steps.contains(quick)).toBe(true);
  });

  it("has a folded, plainly experimental Windows section with its own restart command", () => {
    render(<HostRentalGuide />);
    const box = screen.getByTestId("hrg-windows") as HTMLDetailsElement;
    expect(box.open).toBe(false);
    expect(within(box).getByText("On Windows (experimental)")).toBeTruthy();
    for (const d of HOST_RENTAL_WINDOWS) expect(within(box).getByText(d.lead)).toBeTruthy();
    expect(box.textContent).toMatch(/installer is not published yet/);
    expect(box.textContent).toContain(HOST_RENTAL_WINDOWS_RESTART);
    expect(box.textContent).toMatch(/never handed a Kit session/);
  });

  it("no longer says a Windows computer simply cannot offer rental, and sends the reader to the Windows section", () => {
    render(<HostRentalGuide />);
    expect(document.body.textContent).not.toMatch(/Windows and Mac computers cannot offer container rental/);
    expect(document.body.textContent).toMatch(/Windows is experimental/);
  });
});

