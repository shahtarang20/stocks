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
    ["gpu 0", { gpuCount: "0" }, false],
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
      form({ gpuCount: "0", memoryGb: "0", cpus: "0", workspaceGb: "999", maxPrice: "0", maxMinutes: "0", idleMinutes: "0", accepted: false }),
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
