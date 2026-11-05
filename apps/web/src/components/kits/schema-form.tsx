"use client";

import { useId } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { FieldErrors, FieldSpec, RawValues } from "@/lib/schema-form";

const controlClass =
  "flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background aria-[invalid=true]:border-danger";

function Hint({ field, id }: { field: FieldSpec; id: string }) {
  const parts: string[] = [];
  if (field.minimum !== undefined || field.maximum !== undefined) {
    parts.push(
      field.minimum !== undefined && field.maximum !== undefined
        ? `${field.minimum} to ${field.maximum}`
        : field.minimum !== undefined
          ? `at least ${field.minimum}`
          : `at most ${field.maximum}`,
    );
  }
  if (field.kind === "array") parts.push("one per line, or separated by commas");
  if (field.kind === "json") parts.push("JSON");
  const text = [field.description, parts.length ? `(${parts.join("; ")})` : ""].filter(Boolean).join(" ");
  return text ? (
    <p id={id} className="text-xs text-muted-foreground">{text}</p>
  ) : null;
}

export function SchemaFormField({
  field, value, error, onChange, disabled,
}: {
  field: FieldSpec;
  value: string | boolean;
  error?: string;
  onChange: (v: string | boolean) => void;
  disabled?: boolean;
}) {
  const uid = useId();
  const inputId = `${uid}-in`;
  const hintId = `${uid}-hint`;
  const errId = `${uid}-err`;
  const describedBy = [field.description || field.minimum !== undefined || field.maximum !== undefined || field.kind === "array" || field.kind === "json" ? hintId : "", errId]
    .filter(Boolean)
    .join(" ");
  const common = {
    id: inputId,
    disabled,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": describedBy,
    "aria-required": field.required || undefined,
  } as const;
  const label = (
    <Label htmlFor={inputId}>
      {field.label}
      {field.required ? <span className="ml-1 text-danger" aria-hidden="true">*</span> : null}
      {field.required ? <span className="sr-only"> (required)</span> : null}
    </Label>
  );

  let control: React.ReactNode;
  if (field.kind === "boolean") {
    return (
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <input
            type="checkbox"
            {...common}
            checked={value === true}
            onChange={(e) => onChange(e.target.checked)}
            className="h-4 w-4 rounded border-input accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          />
          {label}
        </div>
        <Hint field={field} id={hintId} />
        <p id={errId} aria-live="polite" className="min-h-0 text-sm text-danger">{error ?? ""}</p>
      </div>
    );
  }
  if (field.kind === "enum") {
    control = (
      <select {...common} value={String(value)} onChange={(e) => onChange(e.target.value)} className={`${controlClass} h-10`}>
        {!field.required || value === "" ? <option value="">{field.required ? "Choose one" : "Use the default"}</option> : null}
        {(field.options ?? []).map((o) => (
          <option key={String(o)} value={String(o)}>{String(o)}</option>
        ))}
      </select>
    );
  } else if (field.kind === "array" || field.kind === "json") {
    control = (
      <textarea
        {...common}
        rows={field.kind === "json" ? 5 : 3}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        className={`${controlClass} ${field.kind === "json" ? "font-mono" : ""}`}
      />
    );
  } else {
    control = (
      <Input
        {...common}
        type={field.kind === "string" ? "text" : "text"}
        inputMode={field.kind === "integer" ? "numeric" : field.kind === "number" ? "decimal" : undefined}
        maxLength={field.maxLength}
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <div className="space-y-1.5">
      {label}
      {control}
      <Hint field={field} id={hintId} />
      <p id={errId} aria-live="polite" className="text-sm text-danger">{error ?? ""}</p>
    </div>
  );
}

/** The whole generated form. Errors are announced politely per field. */
export function SchemaForm({
  fields, values, errors, onChange, disabled,
}: {
  fields: FieldSpec[];
  values: RawValues;
  errors: FieldErrors;
  onChange: (name: string, v: string | boolean) => void;
  disabled?: boolean;
}) {
  if (fields.length === 0) {
    return <p className="text-sm text-muted-foreground">This Kit has no settings. Just upload your files and start.</p>;
  }
  return (
    <div className="grid gap-5 sm:grid-cols-2">
      {fields.map((f) => (
        <div key={f.name} className={f.kind === "array" || f.kind === "json" ? "sm:col-span-2" : undefined}>
          <SchemaFormField
            field={f}
            value={values[f.name] ?? ""}
            error={errors[f.name]}
            onChange={(v) => onChange(f.name, v)}
            disabled={disabled}
          />
        </div>
      ))}
    </div>
  );
}
