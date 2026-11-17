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
  minGpus: 0, // 0 = no graphics card: the computer's processor and memory only (the API accepts 0 to 8)
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

/** A one-click example for "My own image". It only fills the boxes: every field can still be changed, and typing an address by hand
 * works exactly as before. The addresses are pinned by digest (a tag can change). `gpuCount` is set only when the example needs it. */
export interface QuickFill {
  id: "python-terminal" | "jupyter-web";
  label: string;
  help: string;
  image: string;
  appKind: "terminal" | "web";
  port: string;
  command: string;
  gpuCount?: string;
}

export const QUICK_FILLS: QuickFill[] = [
  {
    id: "python-terminal",
    label: "Python terminal",
    help: "The official Python 3.11 image in a terminal. It needs no graphics card, so it sets graphics cards to 0.",
    image: "docker.io/library/python@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535",
    appKind: "terminal",
    port: "",
    command: "/bin/bash",
    gpuCount: "0",
  },
  {
    id: "jupyter-web",
    label: "Jupyter web page",
    help: "Our own PyTorch and JupyterLab image, opened as a web page. It is large, so the first start takes a while. It is built for a graphics card.",
    image: "ghcr.io/shahtarang20/rental-pytorch-jupyter@sha256:58b279d23c444496e52a50b7620356d6f2e1ee3cae03d50cb7fcbd64c59935d0",
    appKind: "web",
    port: "8888",
    command: "",
  },
];

/** The form after pressing a quick-fill button: it switches to "My own image" and fills the image, how to use it, the port and the command. */
export function applyQuickFill(form: RentalForm, fill: QuickFill): RentalForm {
  return {
    ...form,
    source: "image",
    template: "",
    image: fill.image,
    appKind: fill.appKind,
    port: fill.port,
    command: fill.command,
    gpuCount: fill.gpuCount ?? form.gpuCount,
  };
}

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
    errors.gpuCount = `Choose between ${RENTAL_LIMITS.minGpus} (no graphics card) and ${RENTAL_LIMITS.maxGpus} graphics cards.`;
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
  const billed = Math.max(g, 1); // a rental with no graphics card is priced as one card, exactly as the server does
  const total = (micro * BigInt(billed) * BigInt(m)) / BigInt(60); // still x 1e6
  const s = total.toString().padStart(7, "0");
  const int = s.slice(0, -6);
  let dec = s.slice(-6).replace(/0+$/, "");
  if (dec.length < 2) dec = dec.padEnd(2, "0");
  return `${int}.${dec}`;
}

/** What the "Graphics cards" selector shows for each number. */
export function gpuChoiceLabel(n: number): string {
  return n === 0 ? "0 (no graphics card, CPU only)" : String(n);
}

/** Shown under the selector when no graphics card is chosen. */
export const CPU_ONLY_NOTE =
  "No graphics card: only the computer's processor and memory are used, so graphics-card code will not run on the GPU. Good for testing and for work that does not need one. You are still charged the computer's hourly price, as for one card.";

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
/** What the "files to bring in" box says when the list could not be loaded. `code` is the API's error code, `status` its HTTP status.
 * The rental itself never needs the list, so every message ends by saying so. */
export function filesProblemText(code: string | null, status?: number): string {
  const rest = " You can still start the rental without them.";
  if (code === "email_not_verified") {
    return "Please confirm your email address to bring in your files. Use the button at the top of the page to get a new link." + rest;
  }
  if (status === 401) return "Please sign in again to see your files." + rest;
  return "We could not load your files." + rest;
}

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
    gpu_count_out_of_range: "Choose between 0 (no graphics card) and 8 graphics cards.",
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
    windows_host_offers_one_gpu: "A Windows computer can give a rental only one graphics card. Choose 1 (or 0), or pick a Linux computer.",
    windows_host_takes_rentals_only: "A Windows computer only takes container rentals, not Kits. Pick another computer.",
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

// ---------------------------------------------------------------------------------------------------------------------------------
// Guidance shown to hosts and customers. One place, so the setup page, "My computers", the public pages and the Rent page say the same
// thing, and the commands a host is told to run are the ones the agent really has (checked by a test against the agent's own CLI).
// ---------------------------------------------------------------------------------------------------------------------------------

/** The commands a host runs on the computer that offers container rental. */
export const HOST_RENTAL_COMMANDS = {
  check: "computecoin-agent rental check",
  on: "computecoin-agent rental on",
  status: "computecoin-agent rental status",
  off: "computecoin-agent rental off",
  restart: "sudo systemctl restart computecoin-agent",
} as const;

/** What a host's computer needs before container rental can work. */
export const HOST_RENTAL_NEEDS: { lead: string; text: string }[] = [
  { lead: "A Linux computer.", text: "Ubuntu 22.04 or 24.04. Mac computers cannot offer container rental. Windows is experimental: see \"On Windows (experimental)\" below." },
  { lead: "An NVIDIA graphics card that works.", text: "Running nvidia-smi in a terminal must list it. AMD cards are not supported for container rental." },
  { lead: "Docker, and the NVIDIA Container Toolkit.", text: "Use the normal Docker Engine. Docker Desktop is not supported for container rental yet. The toolkit lets Docker use your graphics card inside a container." },
  { lead: "The ComputeCoin agent, version 0.2.0 or newer,", text: "already connected to your account (steps 1 to 4 on this page)." },
  { lead: "Some free disk space.", text: "Each rental gets a private work folder on your disk, limited in size (50 GB by default). It is deleted when the rental ends." },
  { lead: "A steady internet connection.", text: "Your computer only connects out. You do not need to open any ports on your router. The customer's image is downloaded from a public site such as Docker Hub." },
];

/** What a customer is told before renting from a Windows PC (experimental). */
export const WINDOWS_HOST_LABEL = "Windows PC (experimental)";
export const WINDOWS_HOST_WARNING =
  "This host is a Windows PC, and Windows rentals are experimental. Your container runs inside a Linux system on that PC, so only Linux images work, you get one graphics card at most, the first start can take longer, and the rental can be cut off if the PC goes to sleep or restarts. Save your results often, or pick a Linux computer for the steadiest option.";

/** The Windows restart command a host runs after turning rental on or off (an administrator PowerShell). */
export const HOST_RENTAL_WINDOWS_RESTART = "Restart-Service ComputeCoinAgent";

/** Rental on Windows, for hosts: experimental, and what it needs. */
export const HOST_RENTAL_WINDOWS: { lead: string; text: string }[] = [
  {
    lead: "What it is.",
    text: "On a Windows PC, customers' containers run inside the ComputeCoin Linux system (WSL2) that the Windows agent manages. They cannot see your Windows drives. It is experimental and has not been proven on many real PCs.",
  },
  {
    lead: "What the PC needs.",
    text: "Windows 11 (Windows 10 cannot run the Linux system's service manager, which needs Windows 11), virtualization switched on in the BIOS, a GeForce or RTX card in its normal mode (data-centre cards and cards in TCC mode do not work on Windows), an NVIDIA driver for Windows (535.86 or newer, the Linux system uses it), and the ComputeCoin Windows agent already connected to your account. AMD and Intel cards are not supported. The PC must stay on while you host.",
  },
  {
    lead: "The Windows agent.",
    text: "The Windows installer is not published yet, so for now the Windows option on the setup page has nothing to install. Rental on Windows is only for hosts who already run the Windows agent. Linux is the recommended choice.",
  },
  {
    lead: "Only rentals, one card each.",
    text: "A Windows host takes customers' own containers and nothing else: it is never handed a Kit session. Each rental gets one graphics card (the Linux system sees one card at a time), even if the PC has more. It is shown to customers as a Windows PC.",
  },
  {
    lead: "Run these in an administrator PowerShell.",
    text: "First computecoin-agent rental check, which also proves your Windows C: drive is hidden from the Linux system. Then computecoin-agent rental on, which turns on both of the Windows switches, then Restart-Service ComputeCoinAgent. computecoin-agent rental status shows both switches, and computecoin-agent rental off turns rental off again.",
  },
  {
    lead: "The platform must allow it too.",
    text: "Windows rentals are off for the whole platform until the site's owner switches them on, so a Windows host may be listed without taking rentals yet.",
  },
];

/** A note for computers with no graphics card: they can still offer rentals that need none. */
export const HOST_RENTAL_NO_CARD =
  "No graphics card? A Linux computer with Docker can still offer rentals that need no card (customers choose 0 graphics cards). The test in step 1 will show one FAIL for the missing card. That is expected for these computers. The price you set is used the same way.";

/** What happens on a host's computer while a customer rents it. */
export const HOST_RENTAL_WHAT_HAPPENS: string[] = [
  "The agent starts one locked container for the customer. It has no extra rights, cannot see your files or programs, and cannot reach your home network.",
  "It has no internet unless the customer asked for a short list of package sites (such as pip). Those go through a filter that only lets those sites through.",
  "The customer gets a private work folder on your disk. When the rental ends it is deleted, after the customer's results are copied to their own storage.",
  "You are paid per second while the rental is running, at the price you set, the same as for jobs. Credits are test credits for now.",
  "While a rental runs, your computer takes no other graphics-card job, and the other way round.",
  "The rental stops by itself if your card gets too hot, your disk runs low, you pause the agent or start using the computer, or its programs look like a cryptocurrency miner.",
];

/** The honest limits a host must know before turning container rental on. */
export const HOST_RENTAL_RISKS: string[] = [
  "A locked container is strong protection, but it is not a perfect wall. Use a computer that holds nothing you could not afford to lose.",
  "You can see the customer's files and programs while it runs. Customers are told this and are asked not to use it for private data.",
  "Miner detection catches the well-known miners. It is a deterrent, not a guarantee.",
  "Container rental is new and has not yet been proven on many real computers. Start with a computer you can watch.",
];

/** How a host earns, in plain words. */
export const HOST_RENTAL_EARN =
  "You choose the price per hour. While a customer's rental is running you earn that price for every second. Nothing is earned while your computer is off, asleep, offline or busy with something else. Today the money is test credits, not real money.";

/** Which kind of host reads this: a short, plain answer for each. */
export const HOST_RENTAL_WHO: { lead: string; text: string }[] = [
  {
    lead: "A gaming PC or laptop at home.",
    text: "Fine for trying it out. You earn only while the computer is on, awake and online, so it suits times when you are not using it. Start with a low price, watch the first rental, and set the computer to Offline whenever you want it for yourself.",
  },
  {
    lead: "A computer you keep just for this (a workstation or a small server).",
    text: "The best fit. Leave it on all day. Use the one-line install so the agent starts again by itself after a restart, and set a price that matches the graphics card.",
  },
  {
    lead: "Many computers, or a data centre.",
    text: "Do the same steps on each computer. Each one is rented separately, and you set the limits per computer. Open \"For data centres and many computers\" further down for the settings.",
  },
];

/** Looking after a home computer that is used for rentals. */
export const HOST_RENTAL_CARE: string[] = [
  "Keep it plugged in and awake. If the computer goes to sleep, shuts down or loses the internet, a running rental is cut off. Turn off sleep while you host.",
  "Keep it cool and give it air. A rental can use the graphics card at full power for hours, and the fans will be loud. The agent stops a rental if the card gets too hot.",
  "Expect it to be busy. While a rental runs, the customer uses your processor, memory and graphics card, so games and other programs run slower. To use the computer yourself, set it to Offline in My computers.",
  "Use a computer with nothing private on it, or at least no files you would mind someone seeing.",
];

/** How a host can tell it is working. */
export const HOST_RENTAL_WORKING: string[] = [
  "My computers shows your computer as Online, with a price per hour and the label \"Container rental: on\".",
  "In the Marketplace, tick \"Only computers that allow container rental\". Your computer should be in the list.",
  "When a customer rents it and the rental ends, the earnings show on the Earnings page.",
];

/** Common problems, with the plain fix. */
export const HOST_RENTAL_TROUBLE: { problem: string; fix: string }[] = [
  {
    problem: "My computer is not in the Marketplace list.",
    fix: "It needs all of these: the agent running, a price per hour, availability set to Online (in My computers, Edit price and availability), and container rental on. Restart the agent after turning rental on, wait a minute, and refresh the page.",
  },
  {
    problem: "Nobody rents my computer.",
    fix: "Your price may be above what customers are willing to pay. The Rent page starts with a most-you-will-pay of 1.00, so try a lower price. A customer's rental waits up to 10 minutes for a host and then ends by itself.",
  },
  {
    problem: "The test in step 1 shows FAIL.",
    fix: "Read the line under it, because it says what to do. Common causes are Docker not installed or not running, your user not allowed to use Docker, the NVIDIA toolkit missing, or too little free disk space. A computer with no graphics card shows one FAIL for the card, which is expected for rentals that need none.",
  },
  {
    problem: "The test says my graphics-card driver is too old.",
    fix: "Update the NVIDIA driver to 535.86 or newer, restart the computer and run the test again. The test picks its image to match your driver, so a driver anywhere from 535 up is fine.",
  },
  {
    problem: "I just installed the NVIDIA driver and the test cannot find the card.",
    fix: "Restart the computer first. If Secure Boot is switched on, it can block a freshly installed driver: turn Secure Boot off in the BIOS settings, or sign the driver, then run the test again.",
  },
  {
    problem: "The card worked, then stopped working inside Docker.",
    fix: "This is a known Docker issue after Docker or the system reloads (the message says \"Failed to initialize NVML\"). Restart Docker with: sudo systemctl restart docker, then run the test again. If it keeps coming back, see the NVIDIA Container Toolkit troubleshooting page.",
  },
  {
    problem: "The installer says my computer has both an NVIDIA and an AMD graphics device.",
    fix: "A laptop with an AMD processor and an NVIDIA card shows both. The installer uses the NVIDIA card and says so, and nothing else is needed. To use the AMD card instead, run the install again with --gpu-vendor amd.",
  },
  {
    problem: "A rental starts and then fails.",
    fix: "Look at the agent's log with: journalctl -u computecoin-agent. The usual causes are no internet to download the image, too little free disk space, or Docker Desktop being used instead of the normal Docker Engine.",
  },
];

/** Details for hosts who run many computers or a data centre. */
export const HOST_RENTAL_DATACENTRE: { lead: string; text: string }[] = [
  {
    lead: "Install every computer the same way.",
    text: "Use the one-line install from step 3c with --allow-rental, and a separate setup token for each computer (a token works once). The agent then starts by itself after a restart.",
  },
  {
    lead: "Set limits per computer.",
    text: "In the agent's settings file (agent.toml) you can set rental_max_workspace_gb (50 by default), rental_max_memory_gb and rental_max_cpus (0 means automatic), and rental_root (the folder for customers' work folders, on a disk with free space). Restart the agent after a change.",
  },
  {
    lead: "Plan the disk.",
    text: "Every rental uses a work folder up to your limit, and Docker keeps each image it downloaded. Give the Docker data folder plenty of room, and remove unused images you know are not needed.",
  },
  {
    lead: "Graphics cards are rented one customer at a time.",
    text: "While a rental that uses graphics cards runs, the computer takes no other graphics-card job, and the other way round. A rental that asks for 0 graphics cards does not block jobs.",
  },
  {
    lead: "Network.",
    text: "The agent only connects out over HTTPS: to ComputeCoin and to the sites that hold customers' images. It needs no inbound ports. A customer's container has no network unless the customer asked for package sites, and then only through the filter that comes built in.",
  },
  {
    lead: "Check and watch.",
    text: "My computers shows each computer's status, price and whether rental is on, and its agent version, so update agents together. To test with the exact image you expect, run: computecoin-agent rental check --image <a public image>.",
  },
];

/** The steps a customer follows. */
export const CUSTOMER_RENTAL_STEPS: { title: string; text: string }[] = [
  {
    title: "Choose a computer",
    text: "Pick one from the list, or leave it on any computer. Only computers whose host chose to allow container rental are listed. If no host takes your rental within 10 minutes, it ends by itself and costs nothing.",
  },
  {
    title: "Choose what to run",
    text: "The quickest way is a ready-made template (PyTorch, or a vision stack, opened in JupyterLab), or a Quick fill button under \"My own image\" (Python terminal or Jupyter web page). Or bring your own public image and open a terminal or a web page.",
  },
  {
    title: "Choose the size, the time and the price",
    text: "Pick the number of graphics cards (0 means processor and memory only), the workspace, memory and processor cores, the most you will pay per hour, and the longest it may run. We set aside enough credits for the longest time and give back what you do not use.",
  },
  {
    title: "Read the warning, tick the box and start",
    text: "The session page shows its progress. When it is running, press Open to use JupyterLab or the terminal in your browser. The first start can take a few minutes while the host's computer downloads the image.",
  },
  {
    title: "Save your results",
    text: "Put the files you want to keep in the results folder (/workspace/results). They are saved to your files automatically every few minutes, when you press Save results now, and when the rental ends.",
  },
  {
    title: "Stop when you are done",
    text: "You pay only for the seconds it was running. It also stops by itself when it sits idle or reaches the longest time you set.",
  },
];

/** What a customer needs before starting. */
export const CUSTOMER_RENTAL_BEFORE: { lead: string; text: string }[] = [
  { lead: "A confirmed email address.", text: "We email you a link when you sign up. Without it you cannot start paid work." },
  { lead: "Credits in your account.", text: "Add them on the Credits and billing page. In this preview they are test credits, not real money." },
  { lead: "Data you are allowed to share.", text: "The host can see your files and programs while the rental runs. Do not use it for secrets, private models, or other people's personal data (such as photos of faces) unless they agreed. For private work use Kits or the private options instead." },
  { lead: "Your code or a template.", text: "A template or a Quick fill button gives you something that works with one click. Your own image must be public and pinned by its @sha256: address (see \"How to get the address of your own image\" below)." },
];

export const CUSTOMER_RENTAL_GOOD_FOR: string[] = [
  "Training and fine-tuning your own model on a graphics card you do not own.",
  "Trying ideas in a notebook, or running your own scripts on a GPU.",
  "Work that is not sensitive.",
];

export const CUSTOMER_RENTAL_NOT_FOR: string[] = [
  "Private or sensitive data. The host can see it.",
  "Anything that must never be lost. The workspace is deleted when the rental ends, and is lost if the host's computer fails. Only the results folder is saved to your files.",
  "Work that needs the open internet. Only a short list of package sites can be allowed.",
];

export const CUSTOMER_RENTAL_PROBLEMS: { lead: string; text: string }[] = [
  { lead: "It sits on \"waiting\" for a long time.", text: "No host that allows rental was free at your price. It ends after 10 minutes and costs nothing. Raise the most you will pay, or pick another computer." },
  { lead: "It will not open.", text: "Open the session page and read its status. Stop it and start again if it is stuck. You pay only for the seconds it was running." },
  { lead: "My files are gone.", text: "Only files in /workspace/results are saved. Everything else in the workspace is deleted when the rental ends." },
  { lead: "I ran out of credits.", text: "Add credits on the Credits and billing page, then start again." },
  { lead: "It says Failed (start failed).", text: "The host's computer could not start it. You are not charged. Start again, or pick another computer." },
  { lead: "Save results says it is waiting.", text: "Your request goes to the host's computer, which copies the files to your storage. This can take a while for large files. If the host's computer has gone, only what was saved earlier is kept." },
  { lead: "The page asks me to confirm my email.", text: "Use the link we emailed you (the button at the top of the page sends a new one). Until your email is confirmed you cannot start a rental or bring files in." },
  { lead: "The web page option is greyed out.", text: "That option needs a separate web address that the site's owner switches on. Until then, use a terminal." },
];

/** Which kind of customer reads this: a short, plain answer for each. */
export const CUSTOMER_RENTAL_WHO: { lead: string; text: string }[] = [
  {
    lead: "New to this, or you just want to try it.",
    text: "Press the Quick fill button called Python terminal, or pick a ready-made template. You do not need to know what an image is. A quick test needs no graphics card, so choose 0, and keep the time limit short, because you still pay the computer's hourly price.",
  },
  {
    lead: "You train models or work in notebooks.",
    text: "Pick a template (PyTorch, or computer vision) to get JupyterLab with the common libraries, or bring your own image. Put what you want to keep in /workspace/results.",
  },
  {
    lead: "A team, a lab or a company.",
    text: "Bring your own public image, pinned by its digest, so everyone runs exactly the same thing. Open \"For teams and heavy use\" below for the limits and good habits.",
  },
];

/** What a rental costs, with a worked example. */
export const CUSTOMER_RENTAL_COST =
  "You pay the host's hourly price for each graphics card, for the seconds the rental is really running. Before it starts we set aside credits for the longest time you chose, plus the platform fee, and give back what you do not use. For example, at 1.00 an hour a 30-minute limit sets aside about 0.50 plus the fee, and if you stop after 8 minutes you pay for about 8 minutes. A rental that never starts, or fails to start, costs nothing. With 0 graphics cards you still pay the computer's hourly price, as for one card. Credits are test credits for now.";

/** What to do in the first minutes, once it is running. */
export const CUSTOMER_RENTAL_FIRST: { lead: string; text: string }[] = [
  { lead: "Wait for Active.", text: "The page shows Pending, then Starting, then Active. The first start can take a few minutes while the host's computer downloads the image." },
  { lead: "Open it.", text: "For a web program, press Open (JupyterLab opens in a new tab). For a terminal, click inside the black box on the page, type a command and press Enter." },
  { lead: "Try one small thing.", text: "In a terminal, type: python3 -c \"print(1+1)\" and press Enter. It should print 2. That shows your code runs." },
  { lead: "Know where your files go.", text: "Your folder is /workspace. Anything you want to keep must go in /workspace/results. Everything else is deleted when the rental ends." },
  { lead: "Save before you stop.", text: "Press Save results now, and check that your file appears under Saved files on the session page." },
  { lead: "Stop it when you are done.", text: "Press Stop session. You pay only for the seconds it was running." },
];

/** How to get the address of an image, for people who bring their own. */
export const CUSTOMER_RENTAL_IMAGE_HELP: { lead: string; text: string }[] = [
  { lead: "Why it looks like this.", text: "A name such as python:3.11-slim can change later. We ask for the exact version, called a digest, so you always run what you chose." },
  { lead: "The easy way.", text: "Press a Quick fill button or pick a template. Then you do not need an address at all." },
  { lead: "Find the digest of an image you know.", text: "On a computer with Docker, run: docker buildx imagetools inspect python:3.11-slim and copy the Digest line. If that does not work, run docker pull python:3.11-slim and then docker inspect --format='{{index .RepoDigests 0}}' python:3.11-slim." },
  { lead: "Write the address like this.", text: "Official Docker Hub images: docker.io/library/<name>@sha256:<digest>. Other Docker Hub images: docker.io/<user>/<name>@sha256:<digest>. GitHub: ghcr.io/<owner>/<name>@sha256:<digest>." },
  { lead: "The image must be public.", text: "It must come from Docker Hub, ghcr.io, nvcr.io or quay.io. Images that need a login are not supported." },
];

/** Details for teams and people who run a lot. */
export const CUSTOMER_RENTAL_TEAMS: { lead: string; text: string }[] = [
  { lead: "Pin your image.", text: "Build your image, push it to a public place (Docker Hub or ghcr.io) and use its @sha256: address. Everyone then runs exactly the same thing, and nothing changes under you." },
  { lead: "Keep secrets out.", text: "Anyone can download a public image, and the host can read your files. Do not put keys, tokens or private models in the image or the workspace." },
  { lead: "Windows computers (experimental).", text: "Some hosts are Windows PCs, marked in the list. Your Linux image runs inside a Linux system on that PC, but the PC can go to sleep or restart, which cuts the rental off, and the first start can take longer. Save results often, or choose a Linux computer." },
  { lead: "Know the limits.", text: "Per rental: up to 8 graphics cards, 256 GB of memory, 64 processor cores, a 100 GB workspace and 24 hours. A host may limit memory, cores and workspace below what you ask for. At most 3 rentals can run at once on one account, and up to 16 files can be brought in per rental." },
  { lead: "Web programs.", text: "For your own web program, give the port it listens on inside the container (127.0.0.1 is enough). The start command is plain words separated by spaces, with no quotes." },
  { lead: "Every rental starts clean.", text: "Keep your code in the image or bring it in as files, and save results to /workspace/results every time." },
];
