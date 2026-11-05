/**
 * Pure logic for the Kit parameter form (U17): turns a Kit's JSON Schema into a field list,
 * validates raw form text into typed params, and maps the API's 422 `detail.errors` onto fields.
 *
 * Supported: string, number, integer, boolean, enum, array of primitives, min/max (and
 * minLength/maxLength/pattern/minItems/maxItems), default, description, title, required.
 * Anything else (nested objects, arrays of objects, oneOf...) becomes a "json" field: the user
 * edits raw JSON and the server's schema check stays the authority. The server always re-validates.
 */

export type FieldKind = "string" | "number" | "integer" | "boolean" | "enum" | "array" | "json";
export type Primitive = string | number | boolean;

export interface FieldSpec {
  name: string;
  kind: FieldKind;
  label: string;
  description?: string;
  required: boolean;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  pattern?: string;
  minItems?: number;
  maxItems?: number;
  options?: Primitive[];
  /** For arrays: the element kind. */
  itemKind?: "string" | "number" | "integer" | "boolean" | "enum";
  itemMinimum?: number;
  itemMaximum?: number;
  itemOptions?: Primitive[];
}

/** Raw form state: booleans for checkboxes, text for everything else. */
export type RawValues = Record<string, string | boolean>;
export type FieldErrors = Record<string, string>;

export interface ServerFieldError {
  field: string;
  code?: string;
  message: string;
}

type JsonObject = Record<string, unknown>;

const isObj = (v: unknown): v is JsonObject => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);

export function humanize(name: string): string {
  const s = name.replace(/[_-]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : name;
}

function primitiveKind(t: unknown): "string" | "number" | "integer" | "boolean" | null {
  return t === "string" || t === "number" || t === "integer" || t === "boolean" ? t : null;
}

function enumOptions(v: unknown): Primitive[] | undefined {
  if (!Array.isArray(v) || v.length === 0) return undefined;
  return v.every((x) => ["string", "number", "boolean"].includes(typeof x)) ? (v as Primitive[]) : undefined;
}

/** Field list in schema property order. Returns [] for a schema with no properties. */
export function fieldsFromSchema(schema: unknown): FieldSpec[] {
  if (!isObj(schema) || !isObj(schema.properties)) return [];
  const required = new Set(Array.isArray(schema.required) ? (schema.required as string[]) : []);
  const out: FieldSpec[] = [];
  for (const [name, raw] of Object.entries(schema.properties)) {
    const p = isObj(raw) ? raw : {};
    const base = {
      name,
      label: typeof p.title === "string" && p.title ? p.title : humanize(name),
      description: typeof p.description === "string" ? p.description : undefined,
      required: required.has(name),
      default: p.default,
    };
    const opts = enumOptions(p.enum);
    if (opts) {
      out.push({ ...base, kind: "enum", options: opts });
      continue;
    }
    const kind = primitiveKind(p.type);
    if (kind) {
      out.push({
        ...base,
        kind,
        minimum: num(p.minimum),
        maximum: num(p.maximum),
        minLength: num(p.minLength),
        maxLength: num(p.maxLength),
        pattern: typeof p.pattern === "string" ? p.pattern : undefined,
      });
      continue;
    }
    if (p.type === "array" && isObj(p.items)) {
      const it = p.items;
      const itOpts = enumOptions(it.enum);
      const ik = primitiveKind(it.type);
      if (itOpts || ik) {
        out.push({
          ...base,
          kind: "array",
          itemKind: itOpts ? "enum" : (ik as FieldSpec["itemKind"]),
          itemOptions: itOpts,
          itemMinimum: num(it.minimum),
          itemMaximum: num(it.maximum),
          minItems: num(p.minItems),
          maxItems: num(p.maxItems),
        });
        continue;
      }
    }
    out.push({ ...base, kind: "json" });
  }
  return out;
}

function arrayToText(v: unknown[]): string {
  return v.map((x) => String(x)).join("\n");
}

export function initialValues(fields: FieldSpec[]): RawValues {
  const v: RawValues = {};
  for (const f of fields) {
    const d = f.default;
    if (f.kind === "boolean") v[f.name] = d === true;
    else if (f.kind === "array") v[f.name] = Array.isArray(d) ? arrayToText(d) : "";
    else if (f.kind === "json") v[f.name] = d === undefined ? "" : JSON.stringify(d, null, 2);
    else v[f.name] = d === undefined || d === null ? "" : String(d);
  }
  return v;
}

function parseNumber(text: string, integer: boolean): number | null {
  const t = text.trim();
  if (!/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t)) return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  return integer && !Number.isInteger(n) ? null : n;
}

function checkRange(n: number, min?: number, max?: number): string | null {
  if (min !== undefined && n < min) return `Must be at least ${min}.`;
  if (max !== undefined && n > max) return `Must be at most ${max}.`;
  return null;
}

function scalar(
  kind: "string" | "number" | "integer" | "boolean" | "enum",
  text: string,
  f: { options?: Primitive[]; minimum?: number; maximum?: number; minLength?: number; maxLength?: number; pattern?: string },
): { value?: Primitive; error?: string } {
  if (kind === "string") {
    if (f.minLength !== undefined && text.length < f.minLength) return { error: `Use at least ${f.minLength} characters.` };
    if (f.maxLength !== undefined && text.length > f.maxLength) return { error: `Use at most ${f.maxLength} characters.` };
    if (f.pattern) {
      try {
        if (!new RegExp(f.pattern).test(text)) return { error: "This value is not in the expected format." };
      } catch {
        /* an invalid pattern in the Kit is the server's to judge */
      }
    }
    return { value: text };
  }
  if (kind === "number" || kind === "integer") {
    const n = parseNumber(text, kind === "integer");
    if (n === null) return { error: kind === "integer" ? "Enter a whole number." : "Enter a number." };
    const r = checkRange(n, f.minimum, f.maximum);
    return r ? { error: r } : { value: n };
  }
  if (kind === "boolean") return { value: text === "true" };
  const match = (f.options ?? []).find((o) => String(o) === text);
  return match === undefined ? { error: "Choose one of the listed options." } : { value: match };
}

export interface BuildResult {
  params: Record<string, unknown>;
  errors: FieldErrors;
}

/** Validate raw form text into typed params. Empty optional fields are omitted (server applies defaults). */
export function buildParams(fields: FieldSpec[], raw: RawValues): BuildResult {
  const params: Record<string, unknown> = {};
  const errors: FieldErrors = {};
  for (const f of fields) {
    const v = raw[f.name];
    if (f.kind === "boolean") {
      params[f.name] = v === true;
      continue;
    }
    const text = typeof v === "string" ? v : "";
    const empty = text.trim() === "";
    if (empty) {
      if (f.required) errors[f.name] = "This field is required.";
      continue;
    }
    if (f.kind === "json") {
      try {
        params[f.name] = JSON.parse(text);
      } catch {
        errors[f.name] = "Enter valid JSON.";
      }
      continue;
    }
    if (f.kind === "array") {
      const items = text.split(/\r?\n|,/).map((s) => s.trim()).filter(Boolean);
      if (f.minItems !== undefined && items.length < f.minItems) {
        errors[f.name] = `Add at least ${f.minItems} item${f.minItems === 1 ? "" : "s"}.`;
        continue;
      }
      if (f.maxItems !== undefined && items.length > f.maxItems) {
        errors[f.name] = `Add at most ${f.maxItems} items.`;
        continue;
      }
      const vals: Primitive[] = [];
      let bad: string | undefined;
      for (const it of items) {
        const r = scalar(f.itemKind ?? "string", it, {
          options: f.itemOptions, minimum: f.itemMinimum, maximum: f.itemMaximum,
        });
        if (r.error) {
          bad = `"${it}": ${r.error}`;
          break;
        }
        vals.push(r.value as Primitive);
      }
      if (bad) errors[f.name] = bad;
      else params[f.name] = vals;
      continue;
    }
    const r = scalar(f.kind, f.kind === "string" ? text : text.trim(), f);
    if (r.error) errors[f.name] = r.error;
    else params[f.name] = r.value;
  }
  return { params, errors };
}

/** Pull `detail.errors[]` out of an ApiError-like object ({details: {detail: {errors}}}); tolerant of shape. */
export function extractServerErrors(err: unknown): ServerFieldError[] {
  const details = (err as { details?: unknown } | null)?.details;
  const detail = isObj(details) ? details.detail : undefined;
  if (isObj(detail) && Array.isArray(detail.errors)) {
    return detail.errors
      .filter(isObj)
      .map((e) => ({
        field: typeof e.field === "string" ? e.field : "",
        code: typeof e.code === "string" ? e.code : undefined,
        message: typeof e.message === "string" ? e.message : typeof e.code === "string" ? e.code : "Invalid value.",
      }));
  }
  return [];
}

/** Map `params.<name>`, `params.<name>[i]`, `params/<name>` (and bare names) to the form field. */
export function fieldNameFor(serverField: string, fields: FieldSpec[]): string | null {
  const s = serverField.replace(/^\$\.?/, "").replace(/^params[./]?/, "");
  const head = s.split(/[.[/]/)[0];
  return fields.some((f) => f.name === head) ? head : null;
}

export interface MappedErrors {
  fieldErrors: FieldErrors;
  /** Errors not tied to a parameter field (inputs, resources, billing...), shown in the summary. */
  general: ServerFieldError[];
}

export function mapServerErrors(errors: ServerFieldError[], fields: FieldSpec[]): MappedErrors {
  const fieldErrors: FieldErrors = {};
  const general: ServerFieldError[] = [];
  for (const e of errors) {
    const name = fieldNameFor(e.field, fields);
    if (name) fieldErrors[name] = fieldErrors[name] ? `${fieldErrors[name]} ${e.message}` : e.message;
    else general.push(e);
  }
  return { fieldErrors, general };
}
