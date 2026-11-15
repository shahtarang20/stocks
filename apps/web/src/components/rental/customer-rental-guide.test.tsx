// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { CustomerRentalGuide } from "./customer-rental-guide";
import { CUSTOMER_RENTAL_BEFORE, CUSTOMER_RENTAL_PROBLEMS, CUSTOMER_RENTAL_STEPS } from "@/lib/rental";

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
});
