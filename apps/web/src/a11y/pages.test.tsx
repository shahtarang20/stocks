// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { describeViolations, seriousViolations } from "./axe-helper";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/",
  useParams: () => ({ jobId: "00000000-0000-0000-0000-000000000000" }),
  useSearchParams: () => new URLSearchParams(),
  redirect: vi.fn(),
}));

vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme: "light", setTheme: vi.fn() }),
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// Signed-out-with-no-token state: queries stay disabled, so each page renders its
// empty or loading state without needing the API.
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    isReady: true,
    isAuthenticated: true,
    isAdmin: false,
    accessToken: null,
    email: "dev@example.com",
    user: null,
    login: vi.fn(),
    register: vi.fn(),
    refreshUser: vi.fn(),
    logout: vi.fn(),
  }),
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}));

beforeEach(() => {
  cleanup();
  // No network in unit tests: every request stays pending.
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
});

async function check(load: () => Promise<{ default: React.ComponentType }>, props?: object) {
  const Page = (await load()).default as React.ComponentType<Record<string, unknown>>;
  const violations = await seriousViolations(<Page {...(props ?? {})} />);
  expect(violations, describeViolations(violations)).toEqual([]);
}

describe("axe: zero serious or critical violations", () => {
  it("landing page", () => check(() => import("@/app/(marketing)/page")));
  it("sign-up", () => check(() => import("@/app/(auth)/register/page")));
  it("login", () => check(() => import("@/app/(auth)/login/page")));
  it("dashboard", () => check(() => import("@/app/dashboard/page")));
  it("new job", () => check(() => import("@/app/dashboard/jobs/new/page")));
  it("job page", () => check(() => import("@/app/dashboard/jobs/[jobId]/page")));
  it("marketplace", () => check(() => import("@/app/(marketing)/marketplace/gpus/page")));
  it("provider hardware", () => check(() => import("@/app/provider/hardware/page")));
  it("provider setup guide", () => check(() => import("@/app/provider/onboarding/page")));
  it("help and glossary", () => check(() => import("@/app/(marketing)/help/page")));
  it("become a host (marketing)", () => check(() => import("@/app/(marketing)/providers/page")));
  it("what you can do", () => check(() => import("@/app/(marketing)/what-you-can-do/page")));
  it("share a Windows PC", () => check(() => import("@/app/(marketing)/share/windows/page")));
  it("kit catalogue", () => check(() => import("@/app/dashboard/kits/page")));
  it("sessions", () => check(() => import("@/app/dashboard/sessions/page")));
  it("billing", () => check(() => import("@/app/dashboard/billing/page")));
  it("kit job wizard", () => check(() => import("@/app/dashboard/kits/[slug]/page")));
  it("kit job panel", async () => {
    const { KitJobPanel } = await import("@/components/kits/kit-job-panel");
    const job = {
      job_id: "00000000-0000-0000-0000-000000000000", workload_type: "kit", model: "blender-render@1.0.0",
      status: "running", execution_mode: "hardware", created_at: "2026-01-01T00:00:00Z", max_cost: "5.00",
      progress: 0.4, verification: { method: "redundant" },
    };
    const violations = await seriousViolations(<KitJobPanel job={job as never} token="t" />);
    expect(violations, describeViolations(violations)).toEqual([]);
  });
  it("file upload control", async () => {
    const { FileUpload } = await import("@/components/kits/file-upload");
    const violations = await seriousViolations(
      <FileUpload label="Scene file" required token="t" onUploaded={() => {}} onCleared={() => {}} />,
    );
    expect(violations, describeViolations(violations)).toEqual([]);
  });
  it("schema form", async () => {
    const { SchemaForm } = await import("@/components/kits/schema-form");
    const { fieldsFromSchema, initialValues } = await import("@/lib/schema-form");
    const fields = fieldsFromSchema({
      type: "object", required: ["frames"],
      properties: {
        frames: { type: "integer", minimum: 1, description: "Frames" }, engine: { enum: ["a", "b"] },
        hdr: { type: "boolean" }, tags: { type: "array", items: { type: "string" } },
      },
    });
    const violations = await seriousViolations(
      <SchemaForm fields={fields} values={initialValues(fields)} errors={{ frames: "Required." }} onChange={() => {}} />,
    );
    expect(violations, describeViolations(violations)).toEqual([]);
  });
});
