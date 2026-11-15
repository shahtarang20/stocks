/**
 * Container rental (rent a GPU, run your own code): the rules the form checks BEFORE it sends anything.
 *
 * Every rule mirrors the API contract in docs/architecture/RENTAL-container-rental-design.md section 7.1, and is a little
 * stricter where that is safe, so the form never sends a request the API will refuse. Pure functions only: no network, no
 * React. The API stays the authority; this only saves people a round trip and gives them plain words.
 */
import type { ExportState, RentalTemplate, SessionCreateInput, SessionInfo } from "@/lib/sessions";

// ---- limits (the API's defaults; the API can be set lower or higher, and always has the last word) ---------------------
export const RENTAL_LIMITS = {
  minGpus: 1,
  maxGpus: 8,
  minMemoryMb: 1024,
  maxMemoryMb: 262_144,
  maxCpus: 64,
  maxWorkspaceGb: 100,
  maxInputs: 16,
  minSeconds: 60,
  maxSeconds: 24 * 3600,
  maxCommandWords: 32,
  maxPathLength: 240,
  maxPathPart: 100,
} as const;

export const RENTAL_DEFAULTS = {
  gpuCount: "1",
  memoryGb: "16",
  cpus: "4",
  workspaceGb: "20",
  maxPrice: "1.00",
  maxMinutes: "240",
  idleMinutes: "30",
} as const;

/** Registries a host's Docker can pull from without a login. Mirrors RENTAL_ALLOWED_REGISTRIES. */
export const ALLOWED_REGISTRIES = ["docker.io", "ghcr.io", "nvcr.io", "quay.io"] as const;

const IMAGE_RE = /^[a-zA-Z0-9][\w./:-]*@sha256:[0-9a-f]{64}$/;
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PATH_PART_RE = /^[A-Za-z0-9._+=@-]+(?: [A-Za-z0-9._+=@-]+)*$/; // single spaces between words only
const PRICE_RE = /^\d{1,12}(\.\d{1,6})?$/;

// ---- image references ----------------------------------------------------------------------------------------------
/** The registry an image reference points at: the first path part when it looks like a host, else Docker Hub. */
export function registryOf(image: string): string {
  const name = image.split("@", 1)[0];
  const slash = name.indexOf("/");
  if (slash > 0) {
    const first = name.slice(0, slash);
    if (first.includes(".") || first.includes(":") || first === "localhost") return first.toLowerCase();
  }
  return "docker.io";
}

export function isAllowedRegistry(registry: string): boolean {
  return (ALLOWED_REGISTRIES as readonly string[]).includes(registry.toLowerCase());
}

/** Plain-words problem with a pasted image reference, or null when it is fine. */
export function imageProblem(value: string): string | null {
  const v = value.trim();
  if (!v) return "Paste the image address, ending in @sha256: and 64 letters and numbers.";
  if (!IMAGE_RE.test(v)) {
    return "The image must be pinned, like name/image@sha256:… (64 letters and numbers after sha256:). A name with only a tag such as :latest cannot be used, because a tag can change.";
  }
  if (!isAllowedRegistry(registryOf(v))) {
    return `Images from ${registryOf(v)} cannot be used. Host computers can only fetch public images from: ${ALLOWED_REGISTRIES.join(", ")}.`;
  }
  return null;
}

// ---- file paths ------------------------------------------------------------------------------------------------------
/** Plain-words problem with a workspace path for an input file, or null. Mirrors the API's clean-path rules. */
export function inputPathProblem(path: string): string | null {
  if (!path) return "Give each file a place in the workspace, such as datasets/faces.zip.";
  if (path.length > RENTAL_LIMITS.maxPathLength) return `That path is too long (at most ${RENTAL_LIMITS.maxPathLength} characters).`;
  if (path.startsWith("/")) return "A path must not start with /. Use a place inside the workspace, such as datasets/faces.zip.";
  if (path.includes("\\") || /[\u0000-\u001f\u007f]/.test(path)) return "A path may only use letters, numbers and . _ + = @ - and spaces.";
  const parts = path.split("/");
  for (const part of parts) {
    if (part === "") return "A path must not have empty parts (two slashes in a row, or a slash at the end).";
    if (part === "." || part === "..") return "A path must not contain . or .. parts.";
    if (part.length > RENTAL_LIMITS.maxPathPart) return `Each part of a path can be at most ${RENTAL_LIMITS.maxPathPart} characters.`;
    if (!PATH_PART_RE.test(part)) return "A path may only use letters, numbers and . _ + = @ - and single spaces.";
  }
  if (path === "results" || path.startsWith("results/")) {
    return "The results folder is where your finished files go to be saved. Put input files somewhere else, such as datasets/.";
  }
  return null;
}

/** A sensible starting path for a file: datasets/<its name, cleaned>. */
export function suggestInputPath(fileName: string): string {
  const base = (fileName.split("/").pop() ?? "").replace(/[^A-Za-z0-9._+=@ -]/g, "_").replace(/ {2,}/g, " ").trim();
  const clean = base.slice(0, RENTAL_LIMITS.maxPathPart - 1).replace(/^\.+$/, "file");
  return `datasets/${clean || "file"}`;
}

export interface InputChoice {
  fileId: string;
  path: string;
}

/** Problems per input row (index to message) plus one list-level message. Empty result means all good. */
export function inputProblems(inputs: InputChoice[]): { list: string | null; rows: Record<number, string> } {
  const rows: Record<number, string> = {};
  let list: string | null = null;
  if (inputs.length > RENTAL_LIMITS.maxInputs) list = `You can bring at most ${RENTAL_LIMITS.maxInputs} files into one rental.`;
  const seen = new Set<string>();
  inputs.forEach((row, i) => {
    if (!UUID_RE.test(row.fileId)) rows[i] = "That file could not be identified. Choose it again from the list.";
    else {
      const problem = inputPathProblem(row.path);
      if (problem) rows[i] = problem;
      else if (seen.has(row.path)) rows[i] = "Two files cannot go to the same place. Give this one a different path.";
      else seen.add(row.path); // only a row that is itself fine can make a later row a duplicate
    }
  });
  return { list, rows };
}

// ---- network: a fixed checklist, nothing typed in ------------------------------------------------------------------
export interface EgressChoice {
  id: string;
  label: string;
  description: string;
  hosts: string[];
}

/** The only places a rental may reach. Mirrors the API's RENTAL_EGRESS_ALLOWLIST defaults. Default is no network at all. */
export const EGRESS_CHOICES: EgressChoice[] = [
  { id: "pip", label: "Python packages (pip)", description: "Install libraries with pip.", hosts: ["pypi.org:443", "files.pythonhosted.org:443"] },
  { id: "pytorch", label: "PyTorch downloads", description: "Get PyTorch wheels and models from pytorch.org.", hosts: ["download.pytorch.org:443"] },
  {
    id: "huggingface",
    label: "Hugging Face models and datasets",
    description: "Download open models and datasets.",
    hosts: ["huggingface.co:443", "cdn-lfs.huggingface.co:443"],
  },
  {
    id: "github",
    label: "GitHub",
    description: "Clone code and download release files.",
    hosts: ["github.com:443", "objects.githubusercontent.com:443", "raw.githubusercontent.com:443"],
  },
  { id: "conda", label: "Conda packages", description: "Install packages with conda.", hosts: ["conda.anaconda.org:443", "repo.anaconda.com:443"] },
];

const ALL_EGRESS_HOSTS = new Set(EGRESS_CHOICES.flatMap((c) => c.hosts));

export function isAllowedEgress(entry: string): boolean {
  return ALL_EGRESS_HOSTS.has(entry);
}

/** The host list for the chosen checklist items. Unknown ids are ignored. Sorted and without repeats. */
export function egressFor(ids: string[]): string[] {
  const chosen = new Set(ids);
  return Array.from(new Set(EGRESS_CHOICES.filter((c) => chosen.has(c.id)).flatMap((c) => c.hosts))).sort();
}

// ---- the command for your own image ---------------------------------------------------------------------------------
export type CommandResult = { ok: true; words: string[] } | { ok: false; error: string };

/** Split what the person typed into words. No quote handling: quotes are refused so nothing is silently misread. */
export function parseCommand(text: string): CommandResult {
  const trimmed = text.trim();
  if (!trimmed) return { ok: true, words: [] };
  if (/["'`\\]/.test(trimmed)) {
    return { ok: false, error: "Write the command as plain words separated by spaces. Quotes and backslashes are not supported here." };
  }
  if (/[\u0000-\u001f\u007f]/.test(trimmed)) return { ok: false, error: "The command contains a character that is not allowed." };
  const words = trimmed.split(/\s+/);
  if (words.length > RENTAL_LIMITS.maxCommandWords) {
    return { ok: false, error: `The command can have at most ${RENTAL_LIMITS.maxCommandWords} words.` };
  }
  return { ok: true, words };
}

// ---- the form -------------------------------------------------------------------------------------------------------------
export interface RentalForm {
  source: "template" | "image";
  template: string;
  image: string;
  command: string;
  appKind: "terminal" | "web";
  port: string;
  machineId: string; // "" = any computer that allows rentals
  gpuCount: string;
  memoryGb: string;
  cpus: string;
  workspaceGb: string;
  maxPrice: string;
  maxMinutes: string;
  idleMinutes: string;
  egress: string[];
  inputs: InputChoice[];
  accepted: boolean;
}

export const EMPTY_FORM: RentalForm = {
  source: "template",
  template: "",
  image: "",
  command: "",
  appKind: "terminal",
  port: "",
  machineId: "",
  gpuCount: RENTAL_DEFAULTS.gpuCount,
  memoryGb: RENTAL_DEFAULTS.memoryGb,
  cpus: RENTAL_DEFAULTS.cpus,
  workspaceGb: RENTAL_DEFAULTS.workspaceGb,
  maxPrice: RENTAL_DEFAULTS.maxPrice,
  maxMinutes: RENTAL_DEFAULTS.maxMinutes,
  idleMinutes: RENTAL_DEFAULTS.idleMinutes,
  egress: [],
  inputs: [],
  accepted: false,
};

export type FormErrors = Partial<Record<
  "template" | "image" | "command" | "port" | "machine" | "gpuCount" | "memoryGb" | "cpus" | "workspaceGb" | "maxPrice" | "maxMinutes" | "idleMinutes" | "inputs" | "accepted",
  string
>>;

/** A whole number written with digits only (no sign, no decimals, no spaces); null when it is not one. */
function wholeNumber(text: string): number | null {
  const t = text.trim();
  if (!/^\d{1,9}$/.test(t)) return null;
  return Number(t);
}

export type BuildResult =
  | { ok: true; body: Omit<SessionCreateInput, "client_pubkey"> }
  | { ok: false; errors: FormErrors; inputRows: Record<number, string> };

/**
 * Check the whole form and build the request body (without the browser key, which the page adds). On any problem nothing is
 * built and every problem is returned in plain words. `templates` is the list the API offered: a template that is not on it
 * is refused here too.
 */
export function buildRentalRequest(form: RentalForm, templates: RentalTemplate[]): BuildResult {
  const errors: FormErrors = {};
  const num = (text: string) => wholeNumber(text);

  if (form.source === "template") {
    if (!SLUG_RE.test(form.template) || !templates.some((t) => t.slug === form.template)) {
      errors.template = "Choose one of the ready-made templates.";
    }
  } else {
    const problem = imageProblem(form.image);
    if (problem) errors.image = problem;
  }

  let words: string[] = [];
  if (form.source === "image") {
    const cmd = parseCommand(form.command);
    if (!cmd.ok) errors.command = cmd.error;
    else words = cmd.words;
    if (form.appKind === "web") {
      const port = num(form.port);
      if (port === null || port < 1 || port > 65535) errors.port = "Give the port number the program listens on, between 1 and 65535.";
    }
  }

  if (form.machineId && !UUID_RE.test(form.machineId)) errors.machine = "Choose a computer from the list.";

  const gpus = num(form.gpuCount);
  if (gpus === null || gpus < RENTAL_LIMITS.minGpus || gpus > RENTAL_LIMITS.maxGpus) {
    errors.gpuCount = `Choose between ${RENTAL_LIMITS.minGpus} and ${RENTAL_LIMITS.maxGpus} graphics cards.`;
  }
  const memGb = num(form.memoryGb);
  const memMb = memGb === null ? null : memGb * 1024;
  if (memMb === null || memMb < RENTAL_LIMITS.minMemoryMb || memMb > RENTAL_LIMITS.maxMemoryMb) {
    errors.memoryGb = `Memory must be between ${RENTAL_LIMITS.minMemoryMb / 1024} and ${RENTAL_LIMITS.maxMemoryMb / 1024} GB, in whole GB.`;
  }
  const cpus = num(form.cpus);
  if (cpus === null || cpus < 1 || cpus > RENTAL_LIMITS.maxCpus) errors.cpus = `Processor cores must be between 1 and ${RENTAL_LIMITS.maxCpus}.`;
  const workspace = num(form.workspaceGb);
  if (workspace === null || workspace > RENTAL_LIMITS.maxWorkspaceGb) {
    errors.workspaceGb = `Workspace must be between 0 and ${RENTAL_LIMITS.maxWorkspaceGb} GB, in whole GB.`;
  }
  const price = form.maxPrice.trim();
  if (!PRICE_RE.test(price) || Number(price) <= 0) errors.maxPrice = "Give the most you will pay per graphics card per hour, as a number above zero such as 1.50.";

  const maxMin = num(form.maxMinutes);
  const maxSec = maxMin === null ? null : maxMin * 60;
  if (maxSec === null || maxSec < RENTAL_LIMITS.minSeconds || maxSec > RENTAL_LIMITS.maxSeconds) {
    errors.maxMinutes = "The longest it can run must be between 1 minute and 24 hours (1440 minutes).";
  }
  const idleMin = num(form.idleMinutes);
  const idleSec = idleMin === null ? null : idleMin * 60;
  if (idleSec === null || idleSec < RENTAL_LIMITS.minSeconds || idleSec > RENTAL_LIMITS.maxSeconds || (maxSec !== null && idleSec > maxSec)) {
    errors.idleMinutes = "The idle time must be at least 1 minute and no longer than the longest it can run.";
  }

  const inputCheck = inputProblems(form.inputs);
  if (inputCheck.list) errors.inputs = inputCheck.list;
  else if (Object.keys(inputCheck.rows).length > 0) errors.inputs = "Fix the files marked below.";

  if (!form.accepted) errors.accepted = "Please tick the box to confirm you understand.";

  const egress = egressFor(form.egress); // only ever hosts from the fixed checklist, so always allowed

  if (Object.keys(errors).length > 0) return { ok: false, errors, inputRows: inputCheck.rows };

  const body: Omit<SessionCreateInput, "client_pubkey"> = {
    gpu_count: gpus as number,
    max_price_per_gpu_hour: price,
    max_duration_seconds: maxSec as number,
    idle_timeout_seconds: idleSec as number,
    isolation_tier: "container",
    accept_rental_terms: true,
    memory_mb: memMb as number,
    cpus: cpus as number,
    workspace_gb: workspace as number,
  };
  if (form.source === "template") {
    body.template = form.template;
  } else {
    body.image_digest = form.image.trim();
    body.mode = form.appKind === "web" ? "tcp" : "pty";
    if (form.appKind === "web") body.tcp_port = num(form.port) as number;
    if (words.length > 0) body.command = words;
  }
  if (form.machineId) body.machine_id = form.machineId;
  if (egress.length > 0) body.network_policy = { egress };
  if (form.inputs.length > 0) body.inputs = form.inputs.map((i) => ({ file_id: i.fileId, path: i.path }));
  return { ok: true, body };
}

// ---- cost note -------------------------------------------------------------------------------------------------------------
/** "price per card per hour x cards x minutes / 60", exact (no floating point), as a decimal string with 2 to 6 places. */
export function holdEstimate(price: string, gpuCount: string, minutes: string): string | null {
  const p = price.trim();
  const g = wholeNumber(gpuCount);
  const m = wholeNumber(minutes);
  if (!PRICE_RE.test(p) || g === null || m === null) return null;
  const [whole, frac = ""] = p.split(".");
  const micro = BigInt(whole + frac.padEnd(6, "0")); // credits x 1e6
  const total = (micro * BigInt(g) * BigInt(m)) / BigInt(60); // still x 1e6
  const s = total.toString().padStart(7, "0");
  const int = s.slice(0, -6);
  let dec = s.slice(-6).replace(/0+$/, "");
  if (dec.length < 2) dec = dec.padEnd(2, "0");
  return `${int}.${dec}`;
}

// ---- wording shown to people ----------------------------------------------------------------------------------------------
/** The warning a customer must accept. Same words everywhere it is shown. */
export const RENTAL_WARNING_POINTS: string[] = [
  "Use this only for data that is not sensitive. The person who owns the computer can see the files you put on it.",
  "Your code runs on a stranger's computer, inside a locked container. Nothing here is a private or sealed environment.",
  "Your workspace is deleted when the rental ends.",
  "Save your results before you stop: put finished files in the results folder and press Save results now.",
  "Nothing is guaranteed if the host's computer fails or goes offline. Only files you already saved are kept.",
];

export const RENTAL_ACCEPT_LABEL =
  "I understand. I will not use private or sensitive data, and I will save my results before I stop.";

/** What each export state means, in plain words. */
export function exportStateText(state: ExportState | undefined): string {
  switch (state) {
    case "requested":
      return "Waiting for the host's computer to start saving your results…";
    case "running":
      return "Saving your results to your files…";
    case "done":
      return "Your results are saved to your files.";
    case "failed":
      return "Saving your results did not work. If the rental is still running, you can try again.";
    default:
      return "Nothing has been saved yet.";
  }
}

/** True while the host is (or is about to be) saving: the button must be off then. */
export function exportBusy(state: ExportState | undefined): boolean {
  return state === "requested" || state === "running";
}

/** Whether to offer "Save results now": a rental that is running, with no save already under way. */
export function canRequestExport(s: Pick<SessionInfo, "rental" | "status" | "export_state">): boolean {
  return Boolean(s.rental) && s.status === "active" && !exportBusy(s.export_state);
}

/** Why the button is off, in plain words, or null when it is on. */
export function exportButtonReason(s: Pick<SessionInfo, "rental" | "status" | "export_state">): string | null {
  if (!s.rental) return "Only container rentals can save results.";
  if (s.status !== "active") {
    return s.status === "pending" || s.status === "starting"
      ? "You can save results once the rental is running."
      : "This rental has ended. The host saves your results automatically when it ends.";
  }
  if (exportBusy(s.export_state)) return "A save is already under way.";
  return null;
}

/** The text for a rental error code the API returns, in plain words. Unknown codes give null. */
export function rentalErrorText(code: string | null): string | null {
  if (!code) return null;
  const base = code.split(":", 1)[0];
  const map: Record<string, string> = {
    container_rental_not_enabled: "Container rental is not switched on yet. Please try again later.",
    rental_terms_not_accepted: "Please tick the box to confirm you understand the warning.",
    rental_requires_container_tier: "Container rental only works in the standard container mode.",
    image_registry_not_allowed: `That image's registry is not allowed. Use one of: ${ALLOWED_REGISTRIES.join(", ")}.`,
    image_must_be_pinned_by_sha256_digest: "The image must be pinned with @sha256: and its 64-character digest.",
    template_not_found: "That template is not available right now. Pick another, or bring your own image.",
    template_excludes_kit_ref_and_image_digest: "Choose either a template or your own image, not both.",
    egress_not_allowed: "One of the internet choices is not allowed for rentals.",
    machine_not_available_for_rental: "That computer is not taking container rentals right now. Choose another computer, or any available one.",
    rental_limit_out_of_range: "One of the amounts (memory, processor cores or workspace) is outside what is allowed.",
    input_file_not_found: "One of the files you chose could not be found in your files. Choose it again.",
    insufficient_credits: "You do not have enough credits. Add credits on the Credits and billing page, then try again.",
    email_not_verified: "Please confirm your email address first.",
    too_many_live_sessions: "You already have the most sessions allowed running. Stop one, then try again.",
    custom_images_not_enabled_for_sessions: "Bringing your own image is not switched on yet. Use a template instead.",
    max_price_must_be_positive: "The most you will pay per hour must be above zero.",
    gpu_count_out_of_range: "Choose between 1 and 8 graphics cards.",
    max_duration_out_of_range: "The longest it can run must be between 1 minute and 24 hours.",
    idle_timeout_out_of_range: "The idle time must be at least 1 minute and no longer than the longest it can run.",
    machine_cannot_filter_network:
      "That computer cannot filter internet access, so it cannot take a rental that asks for internet sites. Choose another computer, or untick the internet choices.",
    session_relay_not_ready: "Container rental is being set up on the server and cannot start sessions yet. Please try again later.",
    egress_not_declared_by_template: "That template does not allow one of the internet choices you ticked.",
    workspace_required_for_inputs: "Files to bring in need a workspace. Set the workspace size above zero.",
    input_paths_conflict: "Two of your files would land at the same place, or one inside the other. Change one of the paths.",
    too_many_inputs: `You chose too many files. The most is ${RENTAL_LIMITS.maxInputs}.`,
    invalid_command: "The start command is not valid. Use plain words separated by spaces.",
    sessions_not_available_for_private_tiers: "Container rental cannot use the private tiers.",
  };
  return map[base] ?? null;
}

/** The first agent release that has container rental. Older agents cannot turn it on, so the host page tells them to update. */
export const RENTAL_MIN_AGENT_VERSION = "0.2.0";

/** True only when ``version`` is a clear dotted number lower than ``min``. Anything unreadable is NOT "below" (we never nag on a guess). */
export function versionBelow(version: string | null | undefined, min: string): boolean {
  const parse = (v: string): number[] | null => {
    const m = /^v?(\d+)\.(\d+)(?:\.(\d+))?/.exec(v.trim());
    return m ? [Number(m[1]), Number(m[2]), Number(m[3] ?? 0)] : null;
  };
  const a = typeof version === "string" ? parse(version) : null;
  const b = parse(min);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] < b[i];
  }
  return false;
}

/**
 * "Container rental" state for a host machine: "on" (the agent reports the flag), "update" (an agent too old to have it), "off", or
 * "unknown" (nothing reported). The flag wins over the version: a machine that reports it can do it.
 */
export function machineRentalState(machine: { capabilities?: string[] | null; agent_version?: string | null }): "on" | "off" | "update" | "unknown" {
  const caps = machine.capabilities;
  if (Array.isArray(caps) && caps.includes("container_rental")) return "on";
  if (versionBelow(machine.agent_version, RENTAL_MIN_AGENT_VERSION)) return "update";
  if (!Array.isArray(caps) || caps.length === 0) return "unknown"; // an older agent reports no flags: say nothing, not "off"
  return "off";
}

/** Whether the browser can open a program with its own web page (JupyterLab): it needs the separate session web address. */
export function webViewAvailable(proxyOrigin: string | undefined, devSameOrigin: string | undefined): boolean {
  return Boolean((proxyOrigin ?? "").trim()) || devSameOrigin === "1";
}

/**
 * True when the most the customer will pay is below what the host asks. Such a rental would wait until it times out
 * unclaimed, so the form says so in plain words first. Exact decimal comparison (no floating point).
 */
export function priceBelow(maxPrice: string, hostPrice: string): boolean {
  const toMicro = (v: string): bigint | null => {
    const t = v.trim();
    if (!PRICE_RE.test(t)) return null;
    const [whole, frac = ""] = t.split(".");
    return BigInt(whole + frac.padEnd(6, "0"));
  };
  const a = toMicro(maxPrice);
  const b = toMicro(hostPrice);
  return a !== null && b !== null && a < b;
}

/** The tooltip for the host badge. */
/** The words on the host badge. */
export const RENTAL_BADGE_LABEL: Record<"on" | "off" | "update", string> = {
  on: "Container rental: on",
  off: "Container rental: off",
  update: "Update agent for container rental",
};

export const RENTAL_BADGE_HELP: Record<"on" | "off" | "update", string> = {
  update: `This computer's agent is older than ${RENTAL_MIN_AGENT_VERSION}, which is the first version that can allow container rental. Update the agent, then run: computecoin-agent rental check`,
  on: "Customers can run their own code on this computer, in a locked container. Turn it off any time with: computecoin-agent rental off",
  off: "This computer only runs Kits and ordinary jobs. Customers cannot run their own code on it.",
};
