// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("next/navigation", () => ({ usePathname: () => "/provider/usage", useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ accessToken: "tok" }) }));
vi.mock("@/components/console/dashboard-charts", () => ({
  JobStatusChart: ({ data, label }: { data: Array<{ name: string }>; label: string }) => (
    <div role="img" aria-label={label}>{data.map((d) => d.name).join("|")}</div>
  ),
  GpuUtilizationChart: () => <div data-testid="util-chart" />,
}));

const api = vi.hoisted(() => ({
  listMachines: vi.fn(),
  getMachineLive: vi.fn(),
  getMachineUsage: vi.fn(),
}));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api }));

import ProviderUsagePage from "./page";

const machine = (id: string, hostname: string) => ({ machine_id: id, hostname });
const live = (over = {}) => ({
  machine_id: "m1", hostname: "rig", status: "online", availability: "online", host_kind: "dedicated",
  last_heartbeat_at: "2026-11-14T10:00:00Z", last_reading_at: "2026-11-14T10:00:00Z", readings_recent: true,
  running_jobs: 1,
  gpus: [{ gpu_index: 0, gpu_util_pct: 87.4, mem_used_mb: 4096, mem_total_mb: 8192, temp_c: 80, power_w: 150 }],
  utilization_last_hour: [], ...over,
});
const usage = (over = {}) => ({
  machine_id: "m1", days: 7, window_start: "2026-11-07T00:00:00Z", window_end: "2026-11-14T00:00:00Z",
  telemetry_available: true, seconds: { window: 1000, running: 250, idle: 600, not_reporting: 150 },
  attempts: { completed: 3, lost: 1 }, ...over,
});

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><ProviderUsagePage /></QueryClientProvider>);
}

beforeEach(() => {
  cleanup();
  Object.values(api).forEach((f) => f.mockReset());
  api.listMachines.mockResolvedValue([machine("m1", "rig")]);
  api.getMachineLive.mockResolvedValue(live());
  api.getMachineUsage.mockResolvedValue(usage());
});

describe("host usage page", () => {
  it("shows live numbers, the time split and job outcomes in plain words", async () => {
    renderPage();
    expect(await screen.findByText("87%")).toBeTruthy();
    expect(screen.getByText(/80 °C · Warm/)).toBeTruthy();
    expect(screen.getByText("1 customer job running")).toBeTruthy();
    expect((await screen.findAllByText("Running customer jobs")).length).toBeGreaterThan(0);
    expect(screen.getByText("Finished")).toBeTruthy();
    expect(screen.getByText("Lost contact")).toBeTruthy();
    expect(screen.queryByText(/sent no readings in this period/)).toBeNull();
  });

  it("does not present old readings as live", async () => {
    api.getMachineLive.mockResolvedValue(live({ readings_recent: false, gpus: [] }));
    renderPage();
    expect(await screen.findByText("No live numbers to show")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toMatch(/No recent readings/);
    expect(screen.queryByText("87%")).toBeNull();
  });

  it("explains instead of inventing a split when the computer sent no readings", async () => {
    api.getMachineUsage.mockResolvedValue(usage({ telemetry_available: false, seconds: { window: 1000, running: 250, idle: null, not_reporting: null } }));
    renderPage();
    expect(await screen.findByText(/sent no readings in this period/)).toBeTruthy();
    expect(screen.queryByText(/Ready, waiting for jobs/)).toBeNull();
  });

  it("asks for another period", async () => {
    renderPage();
    await screen.findByText("Finished");
    fireEvent.click(screen.getByRole("button", { name: "Last 14 days" }));
    await waitFor(() => expect(api.getMachineUsage).toHaveBeenCalledWith("tok", "m1", 14));
  });

  it("lets the host pick between computers", async () => {
    api.listMachines.mockResolvedValue([machine("m1", "rig"), machine("m2", "laptop")]);
    renderPage();
    fireEvent.change(await screen.findByLabelText("Computer"), { target: { value: "m2" } });
    await waitFor(() => expect(api.getMachineLive).toHaveBeenCalledWith("tok", "m2"));
  });

  it("points a host with no computer to setup", async () => {
    api.listMachines.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByRole("link", { name: "Add my computer" })).toBeTruthy();
    expect(api.getMachineLive).not.toHaveBeenCalled();
  });
});
