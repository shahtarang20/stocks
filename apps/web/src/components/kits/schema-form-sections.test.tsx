// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SchemaForm } from "./schema-form";
import type { FieldSpec } from "@/lib/schema-form";

const field = (name: string, required: boolean, extra: Partial<FieldSpec> = {}): FieldSpec =>
  ({ name, label: name.charAt(0).toUpperCase() + name.slice(1), kind: "string", required, ...extra }) as FieldSpec;

afterEach(cleanup);

function details(): HTMLDetailsElement | null {
  return document.querySelector("details");
}

describe("SchemaForm: required settings first, optional ones folded", () => {
  it("shows required settings up front and folds the optional ones", () => {
    render(
      <SchemaForm
        fields={[field("prompt", true), field("seed", false), field("steps", false)]}
        values={{}}
        errors={{}}
        onChange={vi.fn()}
      />,
    );
    const fold = details();
    expect(fold).toBeTruthy();
    expect(fold!.open).toBe(false);
    expect(fold!.textContent).toContain("Optional settings");
    expect(fold!.textContent).toContain("(2)");
    // The required field is outside the fold; the optional ones are inside it (and still in the form).
    expect(fold!.contains(screen.getByLabelText(/Prompt/))).toBe(false);
    expect(fold!.contains(screen.getByLabelText("Seed"))).toBe(true);
    expect(fold!.contains(screen.getByLabelText("Steps"))).toBe(true);
  });

  it("opens the optional settings by itself when one of them has an error", () => {
    render(
      <SchemaForm
        fields={[field("prompt", true), field("seed", false)]}
        values={{}}
        errors={{ seed: "Use a whole number." }}
        onChange={vi.fn()}
      />,
    );
    expect(details()!.open).toBe(true);
    expect(screen.getByText("Use a whole number.")).toBeTruthy();
  });

  it("does not hide a form whose settings are all optional", () => {
    render(<SchemaForm fields={[field("seed", false), field("steps", false)]} values={{}} errors={{}} onChange={vi.fn()} />);
    expect(details()).toBeNull();
    expect(screen.getByLabelText("Seed")).toBeTruthy();
    expect(screen.getByLabelText("Steps")).toBeTruthy();
  });

  it("shows no fold when every setting is required", () => {
    render(<SchemaForm fields={[field("prompt", true), field("language", true)]} values={{}} errors={{}} onChange={vi.fn()} />);
    expect(details()).toBeNull();
  });

  it("still says so when the Kit has no settings", () => {
    render(<SchemaForm fields={[]} values={{}} errors={{}} onChange={vi.fn()} />);
    expect(screen.getByText(/This Kit has no settings/)).toBeTruthy();
  });
});
