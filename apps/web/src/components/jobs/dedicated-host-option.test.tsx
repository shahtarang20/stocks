// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DedicatedHostOption } from "./dedicated-host-option";
import { buildJobV2Input, type JobSpecV2 } from "@/lib/kits";
import { DEDICATED_HELP, DEDICATED_LABEL } from "@/lib/host-kind";

afterEach(cleanup);

const spec = { version: 2, kit: "upscale@1.0.0", inputs: [], outputs: [], params: {} } as unknown as JobSpecV2;

describe("Dedicated computers option", () => {
  it("is a labelled checkbox with its explanation attached", () => {
    render(<DedicatedHostOption id="d" checked={false} onChange={vi.fn()} />);
    const box = screen.getByLabelText(DEDICATED_LABEL) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(screen.getByText(DEDICATED_HELP)).toBeTruthy();
    expect(box.getAttribute("aria-describedby")).toBe("d-help");
  });

  it("reports the new value when clicked, and can be disabled", () => {
    const onChange = vi.fn();
    const { rerender } = render(<DedicatedHostOption id="d" checked={false} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText(DEDICATED_LABEL));
    expect(onChange).toHaveBeenCalledWith(true);
    rerender(<DedicatedHostOption id="d" checked onChange={onChange} disabled />);
    expect((screen.getByLabelText(DEDICATED_LABEL) as HTMLInputElement).disabled).toBe(true);
  });

  it("says plainly that it can mean waiting longer, and that the owner declares it", () => {
    expect(DEDICATED_HELP).toMatch(/wait longer/);
    expect(DEDICATED_HELP).toMatch(/owner declares/);
  });
});

describe("Kit job request", () => {
  it("sends the dedicated field only when the box is ticked, and never changes the rest", () => {
    expect(buildJobV2Input(spec, false)).toEqual({ spec, isolation_tier: "container" });
    expect(buildJobV2Input(spec, true)).toEqual({ spec, isolation_tier: "container", require_dedicated_host: true });
  });
});
