import { describe, expect, it } from "vitest";
import {
  buildParams, extractServerErrors, fieldNameFor, fieldsFromSchema, initialValues, mapServerErrors,
} from "./schema-form";

const schema = {
  type: "object",
  additionalProperties: false,
  required: ["frames", "engine"],
  properties: {
    frames: { type: "integer", minimum: 1, maximum: 500, default: 10, description: "How many frames" },
    engine: { type: "string", enum: ["cycles", "eevee"], default: "cycles" },
    denoise: { type: "boolean", default: true },
    scale: { type: "number", minimum: 0.1, maximum: 2 },
    tags: { type: "array", items: { type: "string" } },
    sizes: { type: "array", items: { type: "integer", minimum: 1 }, minItems: 1 },
    nested: { type: "object", properties: {} },
  },
};

describe("fieldsFromSchema", () => {
  const fields = fieldsFromSchema(schema);
  it("derives kinds, labels, required and constraints in order", () => {
    expect(fields.map((f) => f.name)).toEqual(["frames", "engine", "denoise", "scale", "tags", "sizes", "nested"]);
    expect(fields[0]).toMatchObject({ kind: "integer", minimum: 1, maximum: 500, required: true, label: "Frames", description: "How many frames" });
    expect(fields[1]).toMatchObject({ kind: "enum", options: ["cycles", "eevee"] });
    expect(fields[4]).toMatchObject({ kind: "array", itemKind: "string" });
    expect(fields[6].kind).toBe("json");
  });
  it("tolerates a schema without properties", () => {
    expect(fieldsFromSchema({})).toEqual([]);
    expect(fieldsFromSchema(null)).toEqual([]);
  });
  it("applies defaults", () => {
    expect(initialValues(fields)).toMatchObject({ frames: "10", engine: "cycles", denoise: true, scale: "", tags: "" });
  });
});

describe("buildParams", () => {
  const fields = fieldsFromSchema(schema);
  const base = () => initialValues(fields);

  it("builds typed params and omits empty optionals", () => {
    const { params, errors } = buildParams(fields, { ...base(), frames: "24", scale: "1.5", tags: "a, b\nc", sizes: "1,2" });
    expect(errors).toEqual({});
    expect(params).toMatchObject({ frames: 24, engine: "cycles", denoise: true, scale: 1.5, tags: ["a", "b", "c"], sizes: [1, 2] });
    expect("nested" in params).toBe(false);
  });
  it("reports required, range, integer and enum problems per field", () => {
    const { errors } = buildParams(fields, { ...base(), frames: "", engine: "x", scale: "5", sizes: "2.5" });
    expect(errors.frames).toMatch(/required/i);
    expect(errors.engine).toMatch(/listed options/i);
    expect(errors.scale).toMatch(/at most 2/);
    expect(errors.sizes).toMatch(/whole number/);
    expect(buildParams(fields, { ...base(), frames: "501" }).errors.frames).toMatch(/at most 500/);
    expect(buildParams(fields, { ...base(), frames: "1.5" }).errors.frames).toMatch(/whole number/);
  });
  it("enforces minItems and parses json fields", () => {
    expect(buildParams(fields, { ...base(), sizes: "" }).errors).not.toHaveProperty("sizes");
    expect(buildParams(fields, { ...base(), nested: "{bad" }).errors.nested).toMatch(/JSON/);
    expect(buildParams(fields, { ...base(), nested: '{"a":1}' }).params.nested).toEqual({ a: 1 });
  });
});

describe("server error mapping", () => {
  const fields = fieldsFromSchema(schema);
  const apiErr = {
    details: {
      detail: {
        errors: [
          { field: "params.frames", code: "maximum", message: "must be <= 500" },
          { field: "params.tags[1]", code: "type", message: "must be a string" },
          { field: "inputs[0].source", code: "input_not_found", message: "source must be dataset:<id>" },
          { field: "params.unknown", code: "x", message: "unknown" },
        ],
      },
    },
  };
  it("extracts the errors list and ignores other shapes", () => {
    expect(extractServerErrors(apiErr)).toHaveLength(4);
    expect(extractServerErrors({ details: { detail: "privacy_tier_not_supported_for_kits" } })).toEqual([]);
    expect(extractServerErrors(null)).toEqual([]);
  });
  it("maps params.* onto fields and keeps the rest as general errors", () => {
    const m = mapServerErrors(extractServerErrors(apiErr), fields);
    expect(m.fieldErrors).toEqual({ frames: "must be <= 500", tags: "must be a string" });
    expect(m.general.map((e) => e.field)).toEqual(["inputs[0].source", "params.unknown"]);
  });
  it("resolves field names", () => {
    expect(fieldNameFor("params.frames", fields)).toBe("frames");
    expect(fieldNameFor("params/engine", fields)).toBe("engine");
    expect(fieldNameFor("frames", fields)).toBe("frames");
    expect(fieldNameFor("resources.gpu", fields)).toBeNull();
  });
});
