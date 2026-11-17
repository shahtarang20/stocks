import { describe, expect, it } from "vitest";
import {
  ALLOWED_REGISTRIES,
  EGRESS_CHOICES,
  EMPTY_FORM,
  RENTAL_LIMITS,
  RENTAL_WARNING_POINTS,
  buildRentalRequest,
  canRequestExport,
  egressFor,
  exportButtonReason,
  exportBusy,
  exportStateText,
  holdEstimate,
  imageProblem,
  inputPathProblem,
  inputProblems,
  isAllowedEgress,
  isAllowedRegistry,
  machineRentalState,
  parseCommand,
  priceBelow,
  registryOf,
  rentalErrorText,
  suggestInputPath,
  RENTAL_BADGE_HELP,
  RENTAL_BADGE_LABEL,
  RENTAL_MIN_AGENT_VERSION,
  versionBelow,
  webViewAvailable,
  type RentalForm,
  CPU_ONLY_NOTE,
  gpuChoiceLabel,
  QUICK_FILLS,
  applyQuickFill,
  filesProblemText,
  HOST_RENTAL_CARE,
  HOST_RENTAL_DATACENTRE,
  HOST_RENTAL_EARN,
  HOST_RENTAL_NO_CARD,
  HOST_RENTAL_TROUBLE,
  HOST_RENTAL_WHO,
  HOST_RENTAL_WORKING,
  RENTAL_DEFAULTS,
  CUSTOMER_RENTAL_COST,
  CUSTOMER_RENTAL_FIRST,
  CUSTOMER_RENTAL_IMAGE_HELP,
  CUSTOMER_RENTAL_TEAMS,
  CUSTOMER_RENTAL_WHO,
  HOST_RENTAL_WINDOWS,
  HOST_RENTAL_WINDOWS_RESTART,
  WINDOWS_HOST_LABEL,
  WINDOWS_HOST_WARNING,
  windowsAllCardsText,
} from "./rental";
import type { RentalTemplate } from "./sessions";

const DIGEST = "a".repeat(64);
const FILE_ID = "123e4567-e89b-12d3-a456-426614174000";
const MACHINE_ID = "223e4567-e89b-12d3-a456-426614174111";
const TEMPLATES: RentalTemplate[] = [
  { slug: "pytorch-jupyter", name: "PyTorch and JupyterLab", description: "x", gpu_required: true, mode: "tcp", default_egress: [], libraries: [] },
  { slug: "vision-jupyter", name: "Vision", description: "x", gpu_required: true, mode: "tcp", default_egress: [], libraries: [] },
];
const form = (over: Partial<RentalForm> = {}): RentalForm => ({ ...EMPTY_FORM, template: "pytorch-jupyter", accepted: true, ...over });
const image = (over: Partial<RentalForm> = {}): RentalForm =>
  form({ source: "image", template: "", image: `ghcr.io/acme/train@sha256:${DIGEST}`, ...over });

describe("image references", () => {
  it("finds the registry the way the API does", () => {
    expect(registryOf(`ghcr.io/acme/x@sha256:${DIGEST}`)).toBe("ghcr.io");
    expect(registryOf(`nvcr.io/nvidia/pytorch@sha256:${DIGEST}`)).toBe("nvcr.io");
    expect(registryOf(`quay.io/x/y@sha256:${DIGEST}`)).toBe("quay.io");
    expect(registryOf(`pytorch/pytorch@sha256:${DIGEST}`)).toBe("docker.io"); // no host part: Docker Hub
    expect(registryOf(`ubuntu@sha256:${DIGEST}`)).toBe("docker.io");
    expect(registryOf(`docker.io/library/ubuntu@sha256:${DIGEST}`)).toBe("docker.io");
    expect(registryOf(`GHCR.IO/Acme/x@sha256:${DIGEST}`)).toBe("ghcr.io");
    expect(registryOf(`localhost/x@sha256:${DIGEST}`)).toBe("localhost");
    expect(registryOf(`registry.example.com:5000/x@sha256:${DIGEST}`)).toBe("registry.example.com:5000");
  });

  it("allows only the four public registries", () => {
    expect([...ALLOWED_REGISTRIES]).toEqual(["docker.io", "ghcr.io", "nvcr.io", "quay.io"]);
    for (const r of ALLOWED_REGISTRIES) expect(isAllowedRegistry(r)).toBe(true);
    for (const r of ["localhost", "evil.example.com", "ghcr.io:443", "gcr.io", ""]) expect(isAllowedRegistry(r)).toBe(false);
  });

  it("accepts a pinned image from an allowed registry", () => {
    expect(imageProblem(`ghcr.io/acme/train@sha256:${DIGEST}`)).toBeNull();
    expect(imageProblem(`  pytorch/pytorch@sha256:${DIGEST}  `)).toBeNull();
    expect(imageProblem(`nvcr.io/nvidia/pytorch:24.01-py3@sha256:${DIGEST}`)).toBeNull();
  });

  it("refuses everything else, with a reason", () => {
    expect(imageProblem("")).toMatch(/Paste the image address/);
    for (const bad of [
      "ghcr.io/acme/train:latest",
      "ghcr.io/acme/train",
      `ghcr.io/acme/train@sha256:${"a".repeat(63)}`,
      `ghcr.io/acme/train@sha256:${"A".repeat(64)}`,
      `ghcr.io/acme/train@sha1:${DIGEST}`,
      `-ghcr.io/x@sha256:${DIGEST}`,
      `ghcr.io/ac me/x@sha256:${DIGEST}`,
      `ghcr.io/x@sha256:${DIGEST};rm -rf /`,
    ]) {
      expect(imageProblem(bad), bad).toMatch(/pinned/);
    }
    expect(imageProblem(`gcr.io/x/y@sha256:${DIGEST}`)).toMatch(/gcr\.io cannot be used/);
    expect(imageProblem(`localhost/x@sha256:${DIGEST}`)).toMatch(/localhost cannot be used/);
  });
});

describe("workspace paths for input files", () => {
  it("accepts clean relative paths", () => {
    for (const ok of ["datasets/faces.zip", "a", "data/v1.2/train set/a+b=c@d-e_f.tar", "x/y/z.txt"]) expect(inputPathProblem(ok), ok).toBeNull();
  });

  it("refuses unsafe or unclear paths", () => {
    expect(inputPathProblem("")).toMatch(/Give each file/);
    expect(inputPathProblem("/etc/passwd")).toMatch(/must not start with/);
    expect(inputPathProblem("../x")).toMatch(/\. or \.\./);
    expect(inputPathProblem("a/../b")).toMatch(/\. or \.\./);
    expect(inputPathProblem("a/./b")).toMatch(/\. or \.\./);
    expect(inputPathProblem("a//b")).toMatch(/empty parts/);
    expect(inputPathProblem("a/b/")).toMatch(/empty parts/);
    expect(inputPathProblem("a\\b")).toMatch(/may only use/);
    expect(inputPathProblem("a\nb")).toMatch(/may only use/);
    expect(inputPathProblem("a\u0000b")).toMatch(/may only use/);
    expect(inputPathProblem("a/b c ")).toMatch(/single spaces/);
    expect(inputPathProblem(" a")).toMatch(/single spaces/);
    expect(inputPathProblem("a/b  c")).toMatch(/single spaces/);
    expect(inputPathProblem("naïve.txt")).toMatch(/may only use/);
    expect(inputPathProblem("a*b")).toMatch(/may only use/);
  });

  it("keeps input files out of the results folder", () => {
    expect(inputPathProblem("results")).toMatch(/results folder/);
    expect(inputPathProblem("results/x.csv")).toMatch(/results folder/);
    expect(inputPathProblem("results2/x.csv")).toBeNull();
    expect(inputPathProblem("my/results/x.csv")).toBeNull();
  });

  it("enforces the length limits", () => {
    expect(inputPathProblem("a".repeat(RENTAL_LIMITS.maxPathPart))).toBeNull();
    expect(inputPathProblem("a".repeat(RENTAL_LIMITS.maxPathPart + 1))).toMatch(/at most 100/);
    const long = Array.from({ length: 5 }, () => "b".repeat(60)).join("/"); // 5*60 + 4 = 304 characters
    expect(inputPathProblem(long)).toMatch(/too long/);
    const edge = ["c".repeat(100), "c".repeat(100), "c".repeat(38)].join("/"); // exactly 240
    expect(edge.length).toBe(240);
    expect(inputPathProblem(edge)).toBeNull();
    expect(inputPathProblem(edge + "d")).toMatch(/too long/);
  });

  it("suggests a safe starting path", () => {
    expect(suggestInputPath("faces.zip")).toBe("datasets/faces.zip");
    expect(suggestInputPath("out/job/a b.csv")).toBe("datasets/a b.csv");
    expect(suggestInputPath("weird/név*.bin")).toBe("datasets/n_v_.bin");
    expect(suggestInputPath("")).toBe("datasets/file");
    expect(suggestInputPath("...")).toBe("datasets/file");
    expect(suggestInputPath("x".repeat(500)).length).toBeLessThanOrEqual("datasets/".length + RENTAL_LIMITS.maxPathPart);
    for (const name of ["faces.zip", "a  b", "név", "a/b/c", "***", "x".repeat(300)]) {
      expect(inputPathProblem(suggestInputPath(name)), name).toBeNull();
    }
  });

  it("checks each row and the list", () => {
    expect(inputProblems([]).list).toBeNull();
    const good = inputProblems([{ fileId: FILE_ID, path: "datasets/a.zip" }, { fileId: FILE_ID, path: "datasets/b.zip" }]);
    expect(good.list).toBeNull();
    expect(good.rows).toEqual({});
    const bad = inputProblems([
      { fileId: "nope", path: "datasets/a.zip" },
      { fileId: FILE_ID, path: "../x" },
      { fileId: FILE_ID, path: "datasets/a.zip" },
      { fileId: FILE_ID, path: "datasets/a.zip" },
    ]);
    expect(bad.rows[0]).toMatch(/could not be identified/);
    expect(bad.rows[1]).toMatch(/\. or \.\./);
    expect(bad.rows[2]).toBeUndefined();
    expect(bad.rows[3]).toMatch(/same place/);
    const many = Array.from({ length: RENTAL_LIMITS.maxInputs + 1 }, (_, i) => ({ fileId: FILE_ID, path: `d/f${i}` }));
    expect(inputProblems(many).list).toMatch(/at most 16/);
    expect(inputProblems(many.slice(0, 16)).list).toBeNull();
  });
});

describe("network choices", () => {
  it("is a fixed checklist of plain public hosts on port 443", () => {
    for (const c of EGRESS_CHOICES) {
      expect(c.label.length).toBeGreaterThan(3);
      expect(c.description.length).toBeGreaterThan(3);
      for (const h of c.hosts) expect(h).toMatch(/^[a-z0-9.-]+\.[a-z]{2,}:443$/);
    }
    expect(new Set(EGRESS_CHOICES.map((c) => c.id)).size).toBe(EGRESS_CHOICES.length);
  });

  it("turns chosen ids into a sorted list without repeats and ignores unknown ids", () => {
    expect(egressFor([])).toEqual([]);
    expect(egressFor(["pip"])).toEqual(["files.pythonhosted.org:443", "pypi.org:443"]);
    expect(egressFor(["pip", "pip", "nope", "pytorch"])).toEqual(["download.pytorch.org:443", "files.pythonhosted.org:443", "pypi.org:443"]);
    for (const h of egressFor(EGRESS_CHOICES.map((c) => c.id))) expect(isAllowedEgress(h)).toBe(true);
  });

  it("refuses a host that is not on the checklist", () => {
    for (const h of ["evil.example.com:443", "pypi.org:80", "pypi.org", "127.0.0.1:443", "localhost:443", "*.pypi.org:443", ""]) {
      expect(isAllowedEgress(h), h).toBe(false);
    }
  });
});

describe("the command for your own image", () => {
  it("is optional", () => {
    expect(parseCommand("")).toEqual({ ok: true, words: [] });
    expect(parseCommand("   ")).toEqual({ ok: true, words: [] });
  });
  it("splits plain words on spaces", () => {
    expect(parseCommand("jupyter lab --ip=127.0.0.1  --port=8888")).toEqual({ ok: true, words: ["jupyter", "lab", "--ip=127.0.0.1", "--port=8888"] });
    expect(parseCommand("  /bin/bash ")).toEqual({ ok: true, words: ["/bin/bash"] });
  });
  it("refuses quotes and backslashes instead of guessing", () => {
    for (const bad of ['python -c "print(1)"', "sh -c 'x'", "a\\ b", "echo `id`"]) expect(parseCommand(bad).ok, bad).toBe(false);
  });
  it("refuses control characters and too many words", () => {
    expect(parseCommand("a\u0000b").ok).toBe(false);
    expect(parseCommand("a\u0007b").ok).toBe(false);
    const ok = Array.from({ length: 32 }, (_, i) => `w${i}`).join(" ");
    expect(parseCommand(ok).ok).toBe(true);
    expect(parseCommand(ok + " w32").ok).toBe(false);
  });
});

describe("building the request", () => {
  it("builds a template rental with only what the API takes", () => {
    const r = buildRentalRequest(form(), TEMPLATES);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body).toEqual({
      template: "pytorch-jupyter",
      gpu_count: 1,
      max_price_per_gpu_hour: "1.00",
      max_duration_seconds: 240 * 60,
      idle_timeout_seconds: 30 * 60,
      isolation_tier: "container",
      accept_rental_terms: true,
      memory_mb: 16 * 1024,
      cpus: 4,
      workspace_gb: 20,
    });
    // a template never sends its own image, mode, port or command: the API ignores them, so the form must not offer them
    expect(r.body).not.toHaveProperty("image_digest");
    expect(r.body).not.toHaveProperty("mode");
    expect(r.body).not.toHaveProperty("command");
    expect(r.body).not.toHaveProperty("kit_ref");
  });

  it("builds a rental of your own image as a terminal", () => {
    const r = buildRentalRequest(image({ command: "/bin/bash -l" }), TEMPLATES);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body.image_digest).toBe(`ghcr.io/acme/train@sha256:${DIGEST}`);
    expect(r.body.mode).toBe("pty");
    expect(r.body.command).toEqual(["/bin/bash", "-l"]);
    expect(r.body).not.toHaveProperty("template");
    expect(r.body).not.toHaveProperty("tcp_port");
  });

  it("builds a rental of your own image as a web program with a port", () => {
    const r = buildRentalRequest(image({ appKind: "web", port: "8888" }), TEMPLATES);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body.mode).toBe("tcp");
    expect(r.body.tcp_port).toBe(8888);
    expect(r.body).not.toHaveProperty("command"); // none typed: the image's own start-up is used
  });

  it("includes the machine, network choices and input files when given", () => {
    const r = buildRentalRequest(
      form({ machineId: MACHINE_ID, egress: ["pip", "huggingface"], inputs: [{ fileId: FILE_ID, path: "datasets/faces.zip" }] }),
      TEMPLATES,
    );
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body.machine_id).toBe(MACHINE_ID);
    expect(r.body.network_policy).toEqual({ egress: ["cdn-lfs.huggingface.co:443", "files.pythonhosted.org:443", "huggingface.co:443", "pypi.org:443"] });
    expect(r.body.inputs).toEqual([{ file_id: FILE_ID, path: "datasets/faces.zip" }]);
  });

  it("leaves the network, machine and inputs out when not chosen (no network by default)", () => {
    const r = buildRentalRequest(form(), TEMPLATES);
    expect(r.ok && r.body).toBeTruthy();
    if (!r.ok) return;
    expect(r.body).not.toHaveProperty("network_policy");
    expect(r.body).not.toHaveProperty("machine_id");
    expect(r.body).not.toHaveProperty("inputs");
  });

  it("will not build without the warning ticked", () => {
    const r = buildRentalRequest(form({ accepted: false }), TEMPLATES);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.errors.accepted).toMatch(/tick the box/);
  });

  it("refuses a template that is not on the offered list, or none chosen", () => {
    for (const t of ["", "unknown-template", "Pytorch-Jupyter", "../x"]) {
      const r = buildRentalRequest(form({ template: t }), TEMPLATES);
      expect(r.ok, t).toBe(false);
      if (!r.ok) expect(r.errors.template).toBeTruthy();
    }
    expect(buildRentalRequest(form(), []).ok).toBe(false); // no templates are available at all
  });

  it("refuses a bad image, command or port for your own image", () => {
    const bad = buildRentalRequest(image({ image: "ghcr.io/x/y:latest", command: 'sh -c "x"', appKind: "web", port: "0" }), TEMPLATES);
    expect(bad.ok).toBe(false);
    if (!bad.ok) {
      expect(bad.errors.image).toBeTruthy();
      expect(bad.errors.command).toBeTruthy();
      expect(bad.errors.port).toBeTruthy();
    }
    for (const port of ["", "65536", "-1", "80.5", "abc", "0"]) {
      expect(buildRentalRequest(image({ appKind: "web", port }), TEMPLATES).ok, port).toBe(false);
    }
    expect(buildRentalRequest(image({ appKind: "web", port: "65535" }), TEMPLATES).ok).toBe(true);
    expect(buildRentalRequest(image({ appKind: "web", port: "1" }), TEMPLATES).ok).toBe(true);
  });

  it("ignores the template choice when using your own image, and the image when using a template", () => {
    expect(buildRentalRequest(image({ template: "garbage" }), TEMPLATES).ok).toBe(true);
    expect(buildRentalRequest(form({ image: "garbage" }), TEMPLATES).ok).toBe(true);
  });

  const cases: [string, Partial<RentalForm>, boolean][] = [
    ["gpu 0 (no graphics card, CPU only)", { gpuCount: "0" }, true],
    ["gpu -1", { gpuCount: "-1" }, false],
    ["gpu 1", { gpuCount: "1" }, true],
    ["gpu 8", { gpuCount: "8" }, true],
    ["gpu 9", { gpuCount: "9" }, false],
    ["gpu 1.5", { gpuCount: "1.5" }, false],
    ["gpu blank", { gpuCount: "" }, false],
    ["gpu text", { gpuCount: "two" }, false],
    ["memory 0", { memoryGb: "0" }, false],
    ["memory 1", { memoryGb: "1" }, true],
    ["memory 256", { memoryGb: "256" }, true],
    ["memory 257", { memoryGb: "257" }, false],
    ["memory -4", { memoryGb: "-4" }, false],
    ["cpus 0", { cpus: "0" }, false],
    ["cpus 1", { cpus: "1" }, true],
    ["cpus 64", { cpus: "64" }, true],
    ["cpus 65", { cpus: "65" }, false],
    ["workspace 0", { workspaceGb: "0" }, true],
    ["workspace 100", { workspaceGb: "100" }, true],
    ["workspace 101", { workspaceGb: "101" }, false],
    ["workspace -1", { workspaceGb: "-1" }, false],
    ["price 0", { maxPrice: "0" }, false],
    ["price 0.000000", { maxPrice: "0.000000" }, false],
    ["price 0.000001", { maxPrice: "0.000001" }, true],
    ["price 1.5", { maxPrice: "1.5" }, true],
    ["price 1.1234567", { maxPrice: "1.1234567" }, false],
    ["price -1", { maxPrice: "-1" }, false],
    ["price 1e3", { maxPrice: "1e3" }, false],
    ["price blank", { maxPrice: "" }, false],
    ["price 1,5", { maxPrice: "1,5" }, false],
    ["max 0 min", { maxMinutes: "0" }, false],
    ["max 1 min", { maxMinutes: "1", idleMinutes: "1" }, true],
    ["max 1440 min", { maxMinutes: "1440", idleMinutes: "1440" }, true],
    ["max 1441 min", { maxMinutes: "1441" }, false],
    ["idle 0", { idleMinutes: "0" }, false],
    ["idle longer than max", { maxMinutes: "60", idleMinutes: "61" }, false],
    ["idle equal to max", { maxMinutes: "60", idleMinutes: "60" }, true],
    ["machine garbage", { machineId: "abc" }, false],
    ["machine uuid", { machineId: MACHINE_ID }, true],
  ];
  it.each(cases)("%s", (_name, over, ok) => {
    const r = buildRentalRequest(form(over), TEMPLATES);
    expect(r.ok).toBe(ok);
  });

  it("reports every problem at once, each against its own field", () => {
    const r = buildRentalRequest(
      form({ gpuCount: "9", memoryGb: "0", cpus: "0", workspaceGb: "999", maxPrice: "0", maxMinutes: "0", idleMinutes: "0", accepted: false }),
      TEMPLATES,
    );
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(Object.keys(r.errors).sort()).toEqual(["accepted", "cpus", "gpuCount", "idleMinutes", "maxMinutes", "maxPrice", "memoryGb", "workspaceGb"]);
  });

  it("flags a bad input row and says so on the list", () => {
    const r = buildRentalRequest(form({ inputs: [{ fileId: FILE_ID, path: "/abs" }] }), TEMPLATES);
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.errors.inputs).toMatch(/Fix the files/);
    expect(r.inputRows[0]).toMatch(/must not start with/);
  });

  it("never returns a body that carries a browser key (the page adds it)", () => {
    const r = buildRentalRequest(form(), TEMPLATES);
    expect(r.ok && Object.keys(r.body)).not.toContain("client_pubkey");
  });
});

describe("cost note", () => {
  it("multiplies exactly, with no floating point drift", () => {
    expect(holdEstimate("1.00", "1", "60")).toBe("1.00");
    expect(holdEstimate("1.50", "2", "120")).toBe("6.00");
    expect(holdEstimate("0.1", "3", "60")).toBe("0.30");
    expect(holdEstimate("0.000001", "1", "60")).toBe("0.000001");
    expect(holdEstimate("2", "8", "1440")).toBe("384.00");
    expect(holdEstimate("1.234567", "1", "1")).toBe("0.020576");
  });
  it("gives nothing for input it cannot read", () => {
    for (const [p, g, m] of [["", "1", "1"], ["x", "1", "1"], ["1", "", "1"], ["1", "1", ""], ["1", "1.5", "1"], ["-1", "1", "1"]]) {
      expect(holdEstimate(p, g, m), `${p}|${g}|${m}`).toBeNull();
    }
  });
});

describe("saving results", () => {
  it("describes every state in plain words", () => {
    expect(exportStateText(null)).toMatch(/Nothing has been saved/);
    expect(exportStateText(undefined)).toMatch(/Nothing has been saved/);
    expect(exportStateText("requested")).toMatch(/Waiting/);
    expect(exportStateText("running")).toMatch(/Saving your results/);
    expect(exportStateText("done")).toMatch(/saved to your files/);
    expect(exportStateText("failed")).toMatch(/did not work/);
  });
  it("is busy only while requested or running", () => {
    expect(["requested", "running"].map((s) => exportBusy(s as never))).toEqual([true, true]);
    for (const s of [null, undefined, "done", "failed"]) expect(exportBusy(s as never)).toBe(false);
  });
  it("offers the button only for a running rental with no save under way", () => {
    const base = { rental: true, status: "active", export_state: null } as const;
    expect(canRequestExport(base)).toBe(true);
    expect(canRequestExport({ ...base, export_state: "done" })).toBe(true); // a second save is fine
    expect(canRequestExport({ ...base, export_state: "failed" })).toBe(true); // retry
    expect(canRequestExport({ ...base, export_state: "requested" })).toBe(false);
    expect(canRequestExport({ ...base, export_state: "running" })).toBe(false);
    for (const status of ["pending", "starting", "stopped", "failed"]) expect(canRequestExport({ ...base, status })).toBe(false);
    expect(canRequestExport({ ...base, rental: false })).toBe(false);
    expect(canRequestExport({ ...base, rental: undefined })).toBe(false);
  });
  it("explains why the button is off", () => {
    const base = { rental: true, status: "active", export_state: null } as const;
    expect(exportButtonReason(base)).toBeNull();
    expect(exportButtonReason({ ...base, rental: false })).toMatch(/Only container rentals/);
    expect(exportButtonReason({ ...base, status: "pending" })).toMatch(/once the rental is running/);
    expect(exportButtonReason({ ...base, status: "starting" })).toMatch(/once the rental is running/);
    expect(exportButtonReason({ ...base, status: "stopped" })).toMatch(/has ended/);
    expect(exportButtonReason({ ...base, export_state: "running" })).toMatch(/already under way/);
  });
});

describe("error words", () => {
  it("has plain words for the codes the API uses, and nothing for unknown codes", () => {
    for (const code of [
      "container_rental_not_enabled", "rental_terms_not_accepted", "rental_requires_container_tier", "image_registry_not_allowed",
      "template_not_found", "template_excludes_kit_ref_and_image_digest", "egress_not_allowed", "machine_not_available_for_rental",
      "rental_limit_out_of_range", "input_file_not_found", "insufficient_credits", "email_not_verified", "too_many_live_sessions",
    ]) {
      const text = rentalErrorText(code);
      expect(text, code).toBeTruthy();
      expect(text).not.toMatch(/_/); // no raw code words shown to people
    }
    expect(rentalErrorText("something_new")).toBeNull();
    expect(rentalErrorText(null)).toBeNull();
    expect(rentalErrorText("egress_not_allowed:evil.example.com:443")).toBeTruthy(); // codes may carry a detail after a colon
  });
});

describe("host badge", () => {
  it("reads the flag from the machine's reported flags", () => {
    expect(machineRentalState({ capabilities: ["container_tier", "container_rental"] })).toBe("on");
    expect(machineRentalState({ capabilities: ["container_tier", "nvidia_cuda"] })).toBe("off");
  });
  it("says nothing for an agent that reports no flags at all", () => {
    expect(machineRentalState({})).toBe("unknown");
    expect(machineRentalState({ capabilities: null })).toBe("unknown");
    expect(machineRentalState({ capabilities: [] })).toBe("unknown");
  });
});

describe("the warning", () => {
  it("covers the five things the owner required", () => {
    const text = RENTAL_WARNING_POINTS.join(" ").toLowerCase();
    for (const needle of ["not sensitive", "can see", "deleted when the rental ends", "save your results", "host"]) expect(text).toContain(needle);
    expect(RENTAL_WARNING_POINTS).toHaveLength(5);
  });
});

describe("price hint", () => {
  it("compares prices exactly", () => {
    expect(priceBelow("1.00", "1.50")).toBe(true);
    expect(priceBelow("1.50", "1.50")).toBe(false);
    expect(priceBelow("1.500000", "1.5")).toBe(false);
    expect(priceBelow("0.999999", "1")).toBe(true);
    expect(priceBelow("2", "1.999999")).toBe(false);
    expect(priceBelow("10", "9.99")).toBe(false); // not a text comparison
  });
  it("says nothing when either side is not a number", () => {
    for (const [a, b] of [["", "1"], ["x", "1"], ["1", ""], ["1", "free"], ["-1", "1"]]) expect(priceBelow(a, b), `${a}|${b}`).toBe(false);
  });
});

describe("an agent too old for container rental", () => {
  it("compares versions only when both are clear numbers", () => {
    expect(RENTAL_MIN_AGENT_VERSION).toBe("0.2.0");
    expect(versionBelow("0.1.0", "0.2.0")).toBe(true);
    expect(versionBelow("0.1.9", "0.2.0")).toBe(true);
    expect(versionBelow("v0.1.0", "0.2.0")).toBe(true);
    expect(versionBelow("0.2.0", "0.2.0")).toBe(false);
    expect(versionBelow("0.2.1", "0.2.0")).toBe(false);
    expect(versionBelow("0.10.0", "0.2.0")).toBe(false); // numbers, not text
    expect(versionBelow("1.0", "0.2.0")).toBe(false);
    expect(versionBelow("0.1.0-dev1", "0.2.0")).toBe(true);
    for (const odd of [null, undefined, "", "unknown", "dev", "x.y.z"]) expect(versionBelow(odd as string | null | undefined, "0.2.0")).toBe(false);
  });
  it("asks the host to update, unless the machine already reports the flag", () => {
    expect(machineRentalState({ capabilities: ["container_tier"], agent_version: "0.1.0" })).toBe("update");
    expect(machineRentalState({ capabilities: [], agent_version: "0.1.0" })).toBe("update");
    expect(machineRentalState({ capabilities: ["container_tier", "container_rental"], agent_version: "0.1.0" })).toBe("on"); // the flag wins
    expect(machineRentalState({ capabilities: ["container_tier"], agent_version: "0.2.0" })).toBe("off");
    expect(machineRentalState({ capabilities: ["container_tier"], agent_version: "weird" })).toBe("off"); // never nag on a guess
    expect(machineRentalState({ capabilities: null, agent_version: null })).toBe("unknown");
  });
  it("has words and a tooltip for every state it can show", () => {
    for (const k of ["on", "off", "update"] as const) {
      expect(RENTAL_BADGE_LABEL[k].length).toBeGreaterThan(5);
      expect(RENTAL_BADGE_HELP[k].length).toBeGreaterThan(20);
    }
    expect(RENTAL_BADGE_HELP.update).toContain("rental check");
  });
});

describe("the browser web view", () => {
  it("is available only with the separate address (or the development switch)", () => {
    expect(webViewAvailable("https://sessions.example.com", undefined)).toBe(true);
    expect(webViewAvailable(undefined, "1")).toBe(true);
    expect(webViewAvailable(undefined, undefined)).toBe(false);
    expect(webViewAvailable("", "0")).toBe(false);
    expect(webViewAvailable("   ", undefined)).toBe(false);
  });
});

describe("the new error codes have plain words", () => {
  it.each([
    "machine_cannot_filter_network", "session_relay_not_ready", "egress_not_declared_by_template", "workspace_required_for_inputs",
    "input_paths_conflict", "too_many_inputs", "invalid_command",
  ])("%s", (code) => {
    const t = rentalErrorText(code);
    expect(t).toBeTruthy();
    expect(t).not.toMatch(/_/); // no raw code shown to a person
  });
});

// ---------------------------------------------------------------------------------------------------------------------------------
// The guidance on screen must stay true to the code. These read the agent, the installer and the API's own files.
// ---------------------------------------------------------------------------------------------------------------------------------
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  CUSTOMER_RENTAL_BEFORE,
  CUSTOMER_RENTAL_GOOD_FOR,
  CUSTOMER_RENTAL_NOT_FOR,
  CUSTOMER_RENTAL_PROBLEMS,
  CUSTOMER_RENTAL_STEPS,
  HOST_RENTAL_COMMANDS,
  HOST_RENTAL_NEEDS,
  HOST_RENTAL_RISKS,
  HOST_RENTAL_WHAT_HAPPENS,
} from "./rental";

const repo = (rel: string) => readFileSync(fileURLToPath(new URL(`../../../../${rel}`, import.meta.url)), "utf8");

describe("the host instructions match the agent", () => {
  const cli = repo("provider-agent/computecoin_agent/cli.py");
  const allowed = /action not in \(([^)]*)\)/.exec(cli)?.[1] ?? "";

  it("only tells a host to run commands the agent really has", () => {
    for (const cmd of [HOST_RENTAL_COMMANDS.check, HOST_RENTAL_COMMANDS.on, HOST_RENTAL_COMMANDS.status, HOST_RENTAL_COMMANDS.off]) {
      const [program, sub, action] = cmd.split(" ");
      expect(program).toBe("computecoin-agent");
      expect(sub).toBe("rental");
      expect(allowed).toContain(`"${action}"`);
    }
  });

  it("mentions the installer option the installer really has", () => {
    expect(repo("apps/web/public/install.sh")).toContain("--allow-rental");
  });

  it("names the service the installer creates", () => {
    expect(repo("apps/web/public/install.sh")).toContain(`AGENT_UNIT="computecoin-agent"`);
    expect(HOST_RENTAL_COMMANDS.restart).toBe("sudo systemctl restart computecoin-agent");
  });

  it("states the disk limit the agent really defaults to", () => {
    expect(repo("provider-agent/computecoin_agent/config.py")).toMatch(/rental_max_workspace_gb: int = 50\b/);
    expect(HOST_RENTAL_NEEDS.map((n) => n.text).join(" ")).toContain("50 GB");
  });

  it("states the minimum agent version the update hint uses", () => {
    expect(HOST_RENTAL_NEEDS.map((n) => n.lead).join(" ")).toContain(RENTAL_MIN_AGENT_VERSION);
  });
});

describe("the customer instructions match the platform", () => {
  it("says the wait for a host is 10 minutes, as the API does", () => {
    expect(repo("apps/api/app/services/sessions.py")).toMatch(/UNCLAIMED_SECONDS = 600\b/);
    expect(CUSTOMER_RENTAL_STEPS[0].text).toContain("10 minutes");
    expect(CUSTOMER_RENTAL_PROBLEMS[0].text).toContain("10 minutes");
  });

  it("says results are saved every few minutes, as the agent does", () => {
    expect(repo("provider-agent/computecoin_agent/session_agent.py")).toMatch(/AUTOSAVE_SECONDS = 300\.0/);
    expect(CUSTOMER_RENTAL_STEPS.map((s) => s.text).join(" ")).toContain("every few minutes");
  });

  it("names the registries the API really allows", () => {
    const allowed = repo("apps/api/app/core/config.py");
    expect(allowed).toMatch(/RENTAL_ALLOWED_REGISTRIES: str = "docker\.io,ghcr\.io,nvcr\.io,quay\.io"/);
  });
});

describe("every piece of guidance has real content", () => {
  it("has no empty or duplicated entries", () => {
    const lists: string[][] = [
      HOST_RENTAL_NEEDS.map((n) => `${n.lead} ${n.text}`),
      HOST_RENTAL_WHAT_HAPPENS,
      HOST_RENTAL_RISKS,
      CUSTOMER_RENTAL_STEPS.map((s) => `${s.title} ${s.text}`),
      CUSTOMER_RENTAL_BEFORE.map((b) => `${b.lead} ${b.text}`),
      CUSTOMER_RENTAL_GOOD_FOR,
      CUSTOMER_RENTAL_NOT_FOR,
      CUSTOMER_RENTAL_PROBLEMS.map((p) => `${p.lead} ${p.text}`),
    ];
    for (const list of lists) {
      expect(list.length).toBeGreaterThan(1);
      expect(new Set(list).size).toBe(list.length);
      for (const t of list) expect(t.trim().length).toBeGreaterThan(20);
    }
  });

  it("warns both sides that the host can see the customer's files", () => {
    expect(HOST_RENTAL_RISKS.join(" ")).toMatch(/see the customer's files/);
    expect(CUSTOMER_RENTAL_BEFORE.map((b) => b.text).join(" ")).toMatch(/host can see your files/);
    expect(CUSTOMER_RENTAL_NOT_FOR.join(" ")).toMatch(/host can see it/);
  });
});

describe("renting with no graphics card (CPU only)", () => {
  it("lets a form ask for 0 cards and sends exactly 0 to the API", () => {
    const r = buildRentalRequest(form({ gpuCount: "0" }), TEMPLATES);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.body.gpu_count).toBe(0);
  });

  it("still sends 1 to 8 as before", () => {
    for (const n of [1, 2, 8]) {
      const r = buildRentalRequest(form({ gpuCount: String(n) }), TEMPLATES);
      expect(r.ok && r.body.gpu_count).toBe(n);
    }
  });

  it("matches the API's range of 0 to 8", () => {
    expect(RENTAL_LIMITS.minGpus).toBe(0);
    expect(RENTAL_LIMITS.maxGpus).toBe(8);
    expect(repo("apps/api/app/services/sessions.py")).toMatch(/if not 0 <= gpu_count <= 8:/);
  });

  it("explains the refusal in words that include the 0 choice", () => {
    const r = buildRentalRequest(form({ gpuCount: "9" }), TEMPLATES);
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.errors.gpuCount).toMatch(/0 \(no graphics card\)/);
    expect(rentalErrorText("gpu_count_out_of_range")).toMatch(/0 \(no graphics card\)/);
  });

  it("prices the held credits for 0 cards as for 1 card, exactly as the server does", () => {
    expect(holdEstimate("1.50", "0", "120")).toBe(holdEstimate("1.50", "1", "120"));
    expect(holdEstimate("1.50", "0", "120")).toBe("3.00");
    expect(repo("apps/api/app/services/sessions.py")).toMatch(/gpu_count=max\(gpus, 1\)/);
  });

  it("labels the choices, with the CPU-only one explained", () => {
    expect(gpuChoiceLabel(0)).toBe("0 (no graphics card, CPU only)");
    for (const n of [1, 2, 8]) expect(gpuChoiceLabel(n)).toBe(String(n));
    expect(CPU_ONLY_NOTE).toMatch(/processor and memory/);
    expect(CPU_ONLY_NOTE).toMatch(/charged/);
  });

  it("mentions the 0 choice in the how-it-works guide", () => {
    expect(CUSTOMER_RENTAL_STEPS.map((x) => x.text).join(" ")).toMatch(/0 means processor and memory only/);
  });
});


describe("one-click examples for your own image", () => {
  const python = QUICK_FILLS.find((q) => q.id === "python-terminal")!;
  const jupyter = QUICK_FILLS.find((q) => q.id === "jupyter-web")!;
  const start: RentalForm = { ...EMPTY_FORM, source: "template", template: "pytorch-jupyter", accepted: true, machineId: "", gpuCount: "2" };

  it("offers a Python terminal and a Jupyter web page, and nothing else", () => {
    expect(QUICK_FILLS.map((q) => q.id)).toEqual(["python-terminal", "jupyter-web"]);
  });

  it("switches to your own image and fills the image, how to use it, the port and the command", () => {
    const f = applyQuickFill(start, python);
    expect(f).toMatchObject({ source: "image", template: "", image: python.image, appKind: "terminal", port: "", command: "/bin/bash" });
    const g = applyQuickFill(start, jupyter);
    expect(g).toMatchObject({ source: "image", template: "", image: jupyter.image, appKind: "web", port: "8888", command: "" });
  });

  it("sets graphics cards to 0 only for the example that needs no card", () => {
    expect(applyQuickFill(start, python).gpuCount).toBe("0");
    expect(applyQuickFill(start, jupyter).gpuCount).toBe("2"); // the customer's own choice is kept
  });

  it("leaves everything else the customer chose alone", () => {
    const f = applyQuickFill({ ...start, memoryGb: "7", cpus: "3", maxPrice: "0.40", egress: ["pip"] }, python);
    expect(f).toMatchObject({ memoryGb: "7", cpus: "3", maxPrice: "0.40", egress: ["pip"], accepted: true });
  });

  it("builds a valid request straight from each example, as a terminal and as a web program", () => {
    const t = buildRentalRequest(applyQuickFill(start, python), TEMPLATES);
    expect(t.ok).toBe(true);
    if (t.ok) expect(t.body).toMatchObject({ image_digest: python.image, mode: "pty", command: ["/bin/bash"], gpu_count: 0 });
    const w = buildRentalRequest(applyQuickFill(start, jupyter), TEMPLATES);
    expect(w.ok).toBe(true);
    if (w.ok) expect(w.body).toMatchObject({ image_digest: jupyter.image, mode: "tcp", tcp_port: 8888 });
  });

  it("uses only pinned addresses from the registries the API allows", () => {
    for (const q of QUICK_FILLS) {
      expect(q.image, q.id).toMatch(/^[a-z0-9.-]+\/[\w./-]+@sha256:[0-9a-f]{64}$/);
      expect(["docker.io", "ghcr.io", "nvcr.io", "quay.io"], q.id).toContain(q.image.split("/")[0]);
    }
  });

  it("uses the same Jupyter image the API offers as a template", () => {
    const templates = JSON.parse(repo("apps/api/app/data/rental_templates.json")).templates as { slug: string; digest: string }[];
    expect(templates.find((t) => t.slug === "pytorch-jupyter")?.digest).toBe(jupyter.image);
  });
});


describe("the files box when the list cannot be loaded", () => {
  it("says to confirm the email when that is the reason", () => {
    expect(filesProblemText("email_not_verified", 403)).toMatch(/confirm your email address to bring in your files/);
  });
  it("says to sign in again when the session ended, and keeps the plain message otherwise", () => {
    expect(filesProblemText(null, 401)).toMatch(/sign in again/);
    expect(filesProblemText(null, 500)).toBe("We could not load your files. You can still start the rental without them.");
    expect(filesProblemText(null)).toBe("We could not load your files. You can still start the rental without them.");
  });
  it("always tells the customer the rental can start without files", () => {
    for (const [code, status] of [["email_not_verified", 403], [null, 401], [null, 500]] as const) {
      expect(filesProblemText(code, status)).toMatch(/You can still start the rental without them\.$/);
    }
  });
});


describe("the host guide for every kind of host", () => {
  it("has real, different content in each new list", () => {
    const lists: string[][] = [
      HOST_RENTAL_WHO.map((w) => `${w.lead} ${w.text}`),
      HOST_RENTAL_CARE,
      HOST_RENTAL_WORKING,
      HOST_RENTAL_TROUBLE.map((t) => `${t.problem} ${t.fix}`),
      HOST_RENTAL_DATACENTRE.map((d) => `${d.lead} ${d.text}`),
    ];
    for (const list of lists) {
      expect(list.length).toBeGreaterThan(1);
      expect(new Set(list).size).toBe(list.length);
      for (const t of list) expect(t.trim().length).toBeGreaterThan(20);
    }
    expect(HOST_RENTAL_EARN.length).toBeGreaterThan(40);
    expect(HOST_RENTAL_NO_CARD.length).toBeGreaterThan(40);
  });

  it("names only agent settings that really exist", () => {
    const config = repo("provider-agent/computecoin_agent/config.py");
    const text = HOST_RENTAL_DATACENTRE.map((d) => d.text).join(" ");
    for (const name of ["rental_max_workspace_gb", "rental_max_memory_gb", "rental_max_cpus", "rental_root"]) {
      expect(text, name).toContain(name);
      expect(config, name).toMatch(new RegExp(`\\b${name}\\b`));
    }
    expect(config).toMatch(/rental_max_workspace_gb: int = 50\b/);
    expect(text).toContain("agent.toml");
  });

  it("only mentions the agent options and commands that exist", () => {
    const text = HOST_RENTAL_DATACENTRE.map((d) => d.text).join(" ");
    expect(text).toContain("--allow-rental");
    expect(repo("apps/web/public/install.sh")).toContain("--allow-rental");
    expect(text).toContain("rental check --image");
    expect(repo("provider-agent/computecoin_agent/cli.py")).toMatch(/"--image"/);
  });

  it("states the facts the platform really uses: the 10-minute wait, the default most-you-pay, and 0-card rentals not blocking jobs", () => {
    const trouble = HOST_RENTAL_TROUBLE.map((t) => t.fix).join(" ");
    expect(trouble).toContain("10 minutes");
    expect(repo("apps/api/app/services/sessions.py")).toMatch(/UNCLAIMED_SECONDS = 600\b/);
    expect(trouble).toContain("1.00");
    expect(RENTAL_DEFAULTS.maxPrice).toBe("1.00");
    expect(HOST_RENTAL_DATACENTRE.map((d) => d.text).join(" ")).toMatch(/0 graphics cards does not block jobs/);
    expect(repo("apps/api/tests/test_container_rental.py")).toContain("test_a_session_without_a_gpu_does_not_block_jobs");
  });

  it("uses the marketplace filter wording the Marketplace really shows", () => {
    expect(HOST_RENTAL_WORKING.join(" ")).toContain("Only computers that allow container rental");
    expect(repo("apps/web/src/app/(marketing)/marketplace/gpus/page.tsx")).toContain("Only computers that allow container rental");
  });
});


describe("the customer guide for every kind of customer", () => {
  it("has real, different content in each new list", () => {
    const lists: string[][] = [
      CUSTOMER_RENTAL_WHO.map((w) => `${w.lead} ${w.text}`),
      CUSTOMER_RENTAL_FIRST.map((w) => `${w.lead} ${w.text}`),
      CUSTOMER_RENTAL_IMAGE_HELP.map((w) => `${w.lead} ${w.text}`),
      CUSTOMER_RENTAL_TEAMS.map((w) => `${w.lead} ${w.text}`),
    ];
    for (const list of lists) {
      expect(list.length).toBeGreaterThan(1);
      expect(new Set(list).size).toBe(list.length);
      for (const t of list) expect(t.trim().length).toBeGreaterThan(20);
    }
    expect(CUSTOMER_RENTAL_COST.length).toBeGreaterThan(100);
  });

  it("states the limits the form and the API really have", () => {
    const text = CUSTOMER_RENTAL_TEAMS.map((t) => t.text).join(" ");
    expect(text).toContain(`up to ${RENTAL_LIMITS.maxGpus} graphics cards`);
    expect(text).toContain(`${RENTAL_LIMITS.maxMemoryMb / 1024} GB of memory`);
    expect(text).toContain(`${RENTAL_LIMITS.maxCpus} processor cores`);
    expect(text).toContain(`${RENTAL_LIMITS.maxWorkspaceGb} GB workspace`);
    expect(text).toContain(`${RENTAL_LIMITS.maxSeconds / 3600} hours`);
    expect(text).toContain(`up to ${RENTAL_LIMITS.maxInputs} files`);
    const config = repo("apps/api/app/core/config.py");
    expect(config).toMatch(/SESSIONS_MAX_LIVE_PER_USER: int = 3\b/);
    expect(text).toContain("At most 3 rentals can run at once");
    expect(config).toMatch(new RegExp(`RENTAL_MAX_INPUTS: int = ${RENTAL_LIMITS.maxInputs}\\b`));
  });

  it("names the registries the API allows and the Quick fill buttons the page really has", () => {
    const help = CUSTOMER_RENTAL_IMAGE_HELP.map((t) => t.text).join(" ");
    for (const r of ["Docker Hub", "ghcr.io", "nvcr.io", "quay.io"]) expect(help).toContain(r);
    for (const q of QUICK_FILLS) expect(`${CUSTOMER_RENTAL_STEPS.map((s) => s.text).join(" ")} ${CUSTOMER_RENTAL_WHO.map((w) => w.text).join(" ")}`).toContain(q.label);
  });

  it("gives an example price that matches how the hold is worked out", () => {
    // 1.00 an hour for 30 minutes sets aside 0.50 before the platform fee
    expect(CUSTOMER_RENTAL_COST).toContain("about 0.50 plus the fee");
    expect(Number(RENTAL_DEFAULTS.maxPrice) * (30 / 60)).toBe(0.5);
  });
});


describe("rental on Windows (experimental) matches the code", () => {
  const text = HOST_RENTAL_WINDOWS.map((d) => d.text).join(" ");

  it("tells a Windows host to restart the service by the name the Windows agent really registers", () => {
    const service = repo("provider-agent/computecoin_agent/windows/service.py");
    expect(service).toContain('SERVICE_NAME = "ComputeCoinAgent"');
    expect(HOST_RENTAL_WINDOWS_RESTART).toBe("Restart-Service ComputeCoinAgent");
    expect(text).toContain(HOST_RENTAL_WINDOWS_RESTART);
  });

  it("names only commands the agent has, and the two Windows switches really exist", () => {
    expect(text).toContain("computecoin-agent rental check");
    expect(text).toContain("computecoin-agent rental on");
    expect(text).toContain("computecoin-agent rental status");
    expect(text).toContain("computecoin-agent rental off");
    const cli = repo("provider-agent/computecoin_agent/cli.py");
    expect(cli).toMatch(/windows_rental_enabled/);
    expect(cli).toMatch(/Restart-Service ComputeCoinAgent/);
    expect(repo("provider-agent/computecoin_agent/config.py")).toMatch(/windows_rental_enabled: bool = False/);
  });

  it("says what the platform really enforces: a Windows host is never given a Kit session, and the platform switch is off by default", () => {
    expect(text).toMatch(/never handed a Kit session/);
    expect(repo("apps/api/app/services/sessions.py")).toMatch(/caps\.is_windows/);
    expect(repo("apps/api/app/core/config.py")).toMatch(/WINDOWS_RENTAL_ENABLED: bool = False/);
    expect(text).toMatch(/off for the whole platform until the site's owner switches them on/);
  });

  it("gives customers one label and one warning, and the warning says what really limits a Windows rental", () => {
    expect(WINDOWS_HOST_LABEL).toBe("Windows PC (experimental)");
    expect(WINDOWS_HOST_WARNING).toMatch(/only Linux images work/);
    expect(WINDOWS_HOST_WARNING).toMatch(/sleep or restarts/);
    expect(CUSTOMER_RENTAL_TEAMS.map((t) => t.lead)).toContain("Windows computers (experimental).");
  });

  it("states the requirements the Windows runtime really checks (WSL 2, the managed Linux system)", () => {
    expect(text).toMatch(/WSL2/);
    expect(repo("provider-agent/computecoin_agent/windows/rental_check_windows.py")).toMatch(/WSL2_VERSION = 2/);
    expect(text).toMatch(/Windows 11 \(Windows 10 cannot run/);
    expect(text).toMatch(/data-centre cards and cards in TCC mode do not work/);
    expect(text).toMatch(/all of the PC's graphics cards together/);
    expect(WINDOWS_HOST_WARNING).toMatch(/all of the PC's graphics cards together/);
    expect(repo("provider-agent/computecoin_agent/windows/runtime/requirements_check.py")).toMatch(/min_windows_build: int = 22000/);
    expect(repo("apps/api/app/services/rental.py")).toContain("windows_host_rents_all_cards");
    expect(rentalErrorText("windows_host_rents_all_cards")).toMatch(/all of its graphics cards together, or none/);
    expect(windowsAllCardsText(2)).toMatch(/This one has 2: choose 2 \(or 0 for none\)/);
    expect(windowsAllCardsText(0)).toMatch(/no graphics card. Choose 0/);
    expect(rentalErrorText("windows_host_takes_rentals_only")).toMatch(/not Kits/);
  });
});

describe("the GPU troubleshooting the host guide gives matches the agent and the installer", () => {
  const trouble = HOST_RENTAL_TROUBLE.map((t) => `${t.problem} ${t.fix}`).join(" ");

  it("covers the driver, the fresh install, the NVML failure and the two-brand laptop", () => {
    expect(trouble).toMatch(/graphics-card driver is too old/);
    expect(trouble).toMatch(/Secure Boot/);
    expect(trouble).toMatch(/Failed to initialize NVML/);
    expect(trouble).toMatch(/both an NVIDIA and an AMD graphics device/);
  });

  it("asks for the same minimum driver as the check, the setup page and the Windows text", () => {
    const check = repo("provider-agent/computecoin_agent/rental_check.py");
    expect(check).toMatch(/MIN_DRIVER_MAJOR = 535\b/);
    expect(trouble).toContain("535.86");
    expect(repo("apps/web/src/app/provider/onboarding/page.tsx")).toContain("NVIDIA driver 535.86 or newer");
    expect(HOST_RENTAL_WINDOWS.map((d) => d.text).join(" ")).toContain("535.86 or newer");
  });

  it("only mentions the installer option and the restart command that exist", () => {
    expect(repo("apps/web/public/install.sh")).toContain("--gpu-vendor)");
    expect(trouble).toContain("--gpu-vendor amd");
    expect(trouble).toContain("sudo systemctl restart docker");
  });
});

