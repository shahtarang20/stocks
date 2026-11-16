// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { CustomerRentalGuide } from "./customer-rental-guide";
import {
  CUSTOMER_RENTAL_BEFORE,
  CUSTOMER_RENTAL_FIRST,
  CUSTOMER_RENTAL_IMAGE_HELP,
  CUSTOMER_RENTAL_PROBLEMS,
  CUSTOMER_RENTAL_STEPS,
  CUSTOMER_RENTAL_TEAMS,
  CUSTOMER_RENTAL_WHO,
} from "@/lib/rental";

afterEach(() => cleanup());

describe("CustomerRentalGuide", () => {
  it("shows every step, numbered, in order", () => {
    render(<CustomerRentalGuide />);
    const items = screen.getAllByRole("listitem").filter((li) => li.closest("ol"));
    expect(items).toHaveLength(CUSTOMER_RENTAL_STEPS.length);
    CUSTOMER_RENTAL_STEPS.forEach((s, i) => {
      expect(items[i].textContent).toContain(s.title);
      expect(items[i].textContent?.trim().startsWith(String(i + 1))).toBe(true);
    });
  });

  it("has what to have ready open by default, so nobody misses it", () => {
    render(<CustomerRentalGuide />);
    const before = screen.getByText("Before you start").closest("details")!;
    expect(before.hasAttribute("open")).toBe(true);
    for (const b of CUSTOMER_RENTAL_BEFORE) expect(screen.getByText(b.lead)).toBeTruthy();
  });

  it("keeps the longer sections one click away, with their content present", () => {
    render(<CustomerRentalGuide />);
    for (const name of ["What it is good for, and what it is not", "If something goes wrong"]) {
      expect(screen.getByText(name).closest("details")!.hasAttribute("open")).toBe(false);
    }
    for (const p of CUSTOMER_RENTAL_PROBLEMS) expect(screen.getByText(p.lead)).toBeTruthy();
  });

  it("is one region with one heading, so it fits under the page title", () => {
    render(<CustomerRentalGuide />);
    expect(screen.getByRole("region", { name: /how renting a graphics card works/i })).toBeTruthy();
    expect(screen.getAllByRole("heading")).toHaveLength(1);
  });

  it("warns that the host can see the customer's files", () => {
    render(<CustomerRentalGuide />);
    expect(document.body.textContent).toMatch(/host can see your files/i);
  });

  it("speaks to each kind of customer: a newcomer, someone who trains models, and a team", () => {
    render(<CustomerRentalGuide />);
    expect(screen.getByText("Which one are you?")).toBeTruthy();
    expect(CUSTOMER_RENTAL_WHO).toHaveLength(3);
    for (const w of CUSTOMER_RENTAL_WHO) expect(screen.getByText(w.lead)).toBeTruthy();
    expect(document.body.textContent).toMatch(/Quick fill button called Python terminal/);
  });

  it("explains the cost with a worked example, and says a rental that fails to start costs nothing", () => {
    render(<CustomerRentalGuide />);
    const cost = screen.getByText("What it will cost").closest("details")!;
    expect(cost.hasAttribute("open")).toBe(false);
    expect(cost.textContent).toMatch(/about 0\.50 plus the fee/);
    expect(cost.textContent).toMatch(/fails to start, costs nothing/);
    expect(cost.textContent).toMatch(/0 graphics cards you still pay/);
  });

  it("walks through the first five minutes and the way to get an image's address", () => {
    render(<CustomerRentalGuide />);
    const first = screen.getByText("Your first five minutes").closest("details")!;
    for (const f of CUSTOMER_RENTAL_FIRST) expect(first.textContent).toContain(f.lead);
    expect(first.textContent).toMatch(/\/workspace\/results/);
    const help = screen.getByText("How to get the address of your own image").closest("details")!;
    for (const f of CUSTOMER_RENTAL_IMAGE_HELP) expect(help.textContent).toContain(f.lead);
    expect(help.textContent).toMatch(/docker buildx imagetools inspect/);
    expect(help.textContent).toMatch(/Quick fill button/);
  });

  it("has a folded section of details for teams", () => {
    render(<CustomerRentalGuide />);
    const teams = screen.getByText("For teams and heavy use").closest("details")!;
    expect(teams.hasAttribute("open")).toBe(false);
    for (const f of CUSTOMER_RENTAL_TEAMS) expect(teams.textContent).toContain(f.lead);
  });

  it("still has exactly one heading and one numbered list (the steps)", () => {
    render(<CustomerRentalGuide />);
    expect(screen.getAllByRole("heading")).toHaveLength(1);
    expect(document.querySelectorAll("ol")).toHaveLength(1);
  });
});
