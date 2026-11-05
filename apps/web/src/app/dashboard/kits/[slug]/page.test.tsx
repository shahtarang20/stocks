// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn() }),
  useParams: () => ({ slug: "upscaler" }),
  useSearchParams: () => new URLSearchParams("v=1.0.0"),
  usePathname: () => "/dashboard/kits/upscaler",
}));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "tok" }) }));
// The upload control is covered by its own tests; here one click means "a file was uploaded".
vi.mock("@/components/kits/file-upload", () => ({
  FileUpload: ({ label, error, onUploaded }: { label: string; error?: string; onUploaded: (f: unknown) => void }) => (
    <div>
      <span>{label}</span>
      {error ? <p role="alert">{error}</p> : null}
      <button type="button" onClick={() => onUploaded({ id: "file-1", name: "pic.png", size_bytes: 2048, sha256: "x", content_type: "image/png", created_at: "", expires_at: null })}>
        upload {label}
      </button>
    </div>
  ),
}));

const kit = {
  slug: "upscaler", version: "1.0.0", name: "Upscaler", category: "image", status: "published", image_digest: "sha256:x",
  manifest_sha256: "", signature: "", signing_key_id: "",
  manifest: {
    slug: "upscaler", version: "1.0.0", name: "Upscaler", category: "image",
    param_schema: { type: "object", required: ["scale"], properties: { scale: { type: "integer", minimum: 1, maximum: 8, title: "Scale factor" } } },
    inputs: [{ name: "image", kind: "image", required: true }],
    outputs: [{ name: "result", path: "out.png" }],
    billing_hints: { unit: "images", est_gpu_seconds_per_unit: 30 },
    verification: { method: "redundant" },
  },
};

const getKit = vi.fn();
const createJobV2 = vi.fn();
const tryQuote = vi.fn();
vi.mock("@/lib/kits", async (orig) => {
  const real = await orig<typeof import("@/lib/kits")>();
  return { ...real, kitsApi: { ...real.kitsApi, getKit: (...a: unknown[]) => getKit(...a), listVersions: vi.fn().mockResolvedValue([]), createJobV2: (...a: unknown[]) => createJobV2(...a) }, tryQuote: (...a: unknown[]) => tryQuote(...a) };
});

import KitWizardPage from "./page";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><KitWizardPage /></QueryClientProvider>);
}
const next = (name: RegExp) => fireEvent.click(screen.getByRole("button", { name }));

beforeEach(() => {
  push.mockReset();
  getKit.mockReset().mockResolvedValue(kit);
  createJobV2.mockReset();
  tryQuote.mockReset().mockResolvedValue(null);
});
afterEach(() => cleanup());

async function toStep(n: 0 | 1 | 2) {
  renderPage();
  await screen.findByRole("form", { name: "Settings" });
  if (n === 0) return;
  fireEvent.change(screen.getByLabelText(/Scale factor/), { target: { value: "4" } });
  next(/next: files/i);
  await screen.findByRole("form", { name: "Files" });
  if (n === 1) return;
  fireEvent.click(screen.getByRole("button", { name: /upload image/i }));
  next(/next: review/i);
  await screen.findByRole("form", { name: "Review and start" });
}

describe("Kit job wizard", () => {
  it("does not offer a session Kit as a batch job: it points to Sessions instead of failing at the last step", async () => {
    getKit.mockResolvedValue({ ...kit, manifest: { ...kit.manifest, mode: "session" } });
    renderPage();
    await screen.findByText(/opens as a session, not a job/i);
    expect(screen.queryByRole("form", { name: "Settings" })).toBeNull();
    expect(screen.getByRole("link", { name: /open sessions/i }).getAttribute("href")).toBe("/dashboard/sessions?kit=upscaler%401.0.0");
  });

  it("walks Settings -> Files -> Review and marks the current step", async () => {
    await toStep(0);
    expect(screen.getByText(/1\. Settings/).getAttribute("aria-current")).toBe("step");
    fireEvent.change(screen.getByLabelText(/Scale factor/), { target: { value: "4" } });
    next(/next: files/i);
    await screen.findByRole("form", { name: "Files" });
    expect(screen.getByText(/2\. Files/).getAttribute("aria-current")).toBe("step");
    fireEvent.click(screen.getByRole("button", { name: /upload image/i }));
    next(/next: review/i);
    await screen.findByRole("form", { name: "Review and start" });
    expect(screen.getByText(/3\. Review and start/).getAttribute("aria-current")).toBe("step");
  });

  it("blocks Settings on an invalid value and Files until the required input is uploaded", async () => {
    renderPage();
    await screen.findByRole("form", { name: "Settings" });
    next(/next: files/i); // scale is required and empty
    expect(screen.queryByRole("form", { name: "Files" })).toBeNull();
    fireEvent.change(screen.getByLabelText(/Scale factor/), { target: { value: "4" } });
    next(/next: files/i);
    await screen.findByRole("form", { name: "Files" });
    next(/next: review/i);
    expect(screen.queryByRole("form", { name: "Review and start" })).toBeNull();
    expect(screen.getByText("Upload a file for this input.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /upload image/i }));
    next(/next: review/i);
    await screen.findByRole("form", { name: "Review and start" });
    expect(screen.getByText(/pic\.png/)).toBeTruthy();
    // Back keeps the step flow reversible
    next(/back/i);
    await screen.findByRole("form", { name: "Files" });
  });

  it("submits the spec with the uploaded dataset and goes to the job", async () => {
    createJobV2.mockResolvedValue({ job_id: "job-9" });
    await toStep(2);
    next(/start job/i);
    await waitFor(() => expect(createJobV2).toHaveBeenCalledTimes(1));
    const [token, input] = createJobV2.mock.calls[0]!;
    expect(token).toBe("tok");
    expect(input.spec.kit).toBe("upscaler@1.0.0");
    expect(input.spec.params).toEqual({ scale: 4 });
    expect(input.spec.inputs).toEqual([{ name: "image", source: "dataset:file-1" }]);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard/jobs/job-9"));
  });

  it("maps a 422 field error onto the setting, returns to step 1 and shows the general ones", async () => {
    createJobV2.mockRejectedValue(
      new ApiError("Invalid", 422, {
        detail: { errors: [
          { field: "params.scale", code: "too_large", message: "Scale is too large for this Kit." },
          { field: "billing.max_cost", code: "bad", message: "Maximum cost is not a valid amount." },
        ] },
      }),
    );
    await toStep(2);
    next(/start job/i);
    await screen.findByRole("form", { name: "Settings" });
    expect(await screen.findByText("Scale is too large for this Kit.")).toBeTruthy();
    expect(screen.getByText("Maximum cost is not a valid amount.")).toBeTruthy();
    expect(screen.getByLabelText(/Scale factor/).getAttribute("aria-invalid")).toBe("true");
    expect(push).not.toHaveBeenCalled();
  });

  it("sends a input-level server error back to the Files step", async () => {
    createJobV2.mockRejectedValue(new ApiError("Invalid", 422, { detail: { errors: [{ field: "inputs[0].source", code: "not_found", message: "That file was not found." }] } }));
    await toStep(2);
    next(/start job/i);
    await screen.findByRole("form", { name: "Files" });
    expect(await screen.findByText("That file was not found.")).toBeTruthy();
  });

  it("quote button: with the feature off the wizard says so and still lets the job start", async () => {
    createJobV2.mockResolvedValue({ job_id: "j" });
    await toStep(2);
    fireEvent.change(screen.getByLabelText(/How many images/), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/Most you want to spend/), { target: { value: "5.00" } });
    expect(screen.getByText(/the Kit's own estimate/)).toBeTruthy(); // local estimate, no quote yet
    next(/get a price estimate/i);
    await waitFor(() => expect(tryQuote).toHaveBeenCalledTimes(1));
    expect(tryQuote.mock.calls[0]![1]).toEqual({ kit: "upscaler@1.0.0", expected_units: 10, max_cost: "5.00", gpu_count: 1 });
    expect(await screen.findByText(/A live quote is not available right now/)).toBeTruthy();
    next(/start job/i);
    await waitFor(() => expect(createJobV2).toHaveBeenCalled());
    expect(createJobV2.mock.calls[0]![1].spec.billing).toEqual({ unit: "images", expected_units: 10, max_cost: "5.00" });
  });

  it("quote button: shows the quoted total when the feature is on, and skips the call without units or a maximum", async () => {
    tryQuote.mockResolvedValue({ quote_id: "q1", estimated_minutes: 0, total: "3.20", currency: "USDC_CREDITS" });
    await toStep(2);
    next(/get a price estimate/i); // no units, no max cost: no request
    await screen.findByText(/A live quote is not available right now/);
    expect(tryQuote).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/How many images/), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/Most you want to spend/), { target: { value: "5.00" } });
    next(/get a price estimate/i);
    expect(await screen.findByText(/Quoted total 3\.20 credits/)).toBeTruthy();
  });
});
