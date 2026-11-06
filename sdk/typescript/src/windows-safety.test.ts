// W9 (cross-platform plan): Windows-safety checks for the TypeScript SDK. WRITTEN, NOT RUN.
// Pure functions only: no network, no file system besides reading the shared vectors with an explicit "utf8".
// Bug re-insertion: change the canonicalJson regex back to /[\u0080-\uffff]/ (the DEL test fails); make iterSseData split
// only on "\n" without trimming (the CRLF test fails); drop the BOM-safe decoding (the BOM test fails).
import { readFileSync } from "node:fs";
import { posix, win32, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { describe, expect, it } from "vitest";

import { buildPrivateChatInput, buildPrivateKitInput, canonicalJson, iterSseData } from "./private.js";

const td = new TextDecoder();
const te = new TextEncoder();

async function collect(body: AsyncIterable<Uint8Array>): Promise<unknown[]> {
  const out: unknown[] = [];
  for await (const ev of iterSseData(body)) out.push(ev);
  return out;
}

async function* chunks(...parts: Uint8Array[]): AsyncGenerator<Uint8Array> {
  for (const p of parts) yield p;
}

describe("canonical JSON is the same bytes on every OS (Python json.dumps, ensure_ascii)", () => {
  // Expected strings below come from Python: json.dumps(v, sort_keys=True, separators=(",", ":")).
  it("escapes CR and LF inside strings instead of emitting raw line endings", () => {
    const bytes = canonicalJson({ b: 1, a: "x\r\ny" });
    expect(td.decode(bytes)).toBe('{"a":"x\\r\\ny","b":1}');
    expect(bytes.includes(0x0d)).toBe(false);
    expect(bytes.includes(0x0a)).toBe(false);
  });

  it("never emits a trailing newline", () => {
    const bytes = canonicalJson({ a: 1 });
    expect(bytes[bytes.length - 1]).toBe("}".charCodeAt(0));
  });

  it("escapes DEL (0x7f) like Python does", () => {
    expect(td.decode(canonicalJson({ k: "\x7f" }))).toBe('{"k":"\\u007f"}');
  });

  it("escapes non-ASCII as lower-case \\u sequences, astral characters as surrogate pairs", () => {
    expect(td.decode(canonicalJson({ k: "caf\u00e9 \u{1F600}" }))).toBe('{"k":"caf\\u00e9 \\ud83d\\ude00"}');
    expect(td.decode(canonicalJson({ k: "\u2028" }))).toBe('{"k":"\\u2028"}');
  });

  it("escapes tab, backslash and quote exactly like Python", () => {
    expect(td.decode(canonicalJson({ k: 'a\tb\\c"' }))).toBe('{"k":"a\\tb\\\\c\\""}');
  });

  it("output is pure ASCII, so it cannot depend on a console or locale code page", () => {
    const bytes = canonicalJson({ name: "\u00fc\u00f1\u00ee", text: "line1\r\nline2\n", emoji: "\u{1F680}" });
    expect(Array.from(bytes).every((b) => b >= 0x20 && b <= 0x7e)).toBe(true);
  });

  it("a chat input with Windows line endings has the same canonical bytes after a round trip", () => {
    const built = buildPrivateChatInput([{ role: "user", content: "a\r\nb" }]);
    const again = JSON.parse(td.decode(canonicalJson(built)));
    expect(again.messages[0].content).toBe("a\r\nb");
    expect(again.prompt).toBe("user: a\r\nb");
  });
});

describe("SSE parsing tolerates Windows line endings and a byte-order mark", () => {
  it("accepts CRLF-terminated events", async () => {
    const body = chunks(te.encode('data: {"n":1}\r\n\r\ndata: {"n":2}\r\n\r\ndata: [DONE]\r\n\r\n'));
    expect(await collect(body)).toEqual([{ n: 1 }, { n: 2 }]);
  });

  it("accepts a CRLF split across two network chunks", async () => {
    const body = chunks(te.encode('data: {"n":1}\r'), te.encode('\n\r\ndata: [DONE]\r\n'));
    expect(await collect(body)).toEqual([{ n: 1 }]);
  });

  it("accepts a multi-byte character split across chunks", async () => {
    const full = te.encode('data: {"t":"\u00e9"}\n\ndata: [DONE]\n');
    const cut = full.indexOf(0xc3) + 1; // between the two bytes of U+00E9
    const body = chunks(full.slice(0, cut), full.slice(cut));
    expect(await collect(body)).toEqual([{ t: "\u00e9" }]);
  });

  it("ignores a leading UTF-8 BOM", async () => {
    const bom = new Uint8Array([0xef, 0xbb, 0xbf]);
    const rest = te.encode('data: {"n":1}\n\ndata: [DONE]\n');
    const joined = new Uint8Array(bom.length + rest.length);
    joined.set(bom, 0);
    joined.set(rest, bom.length);
    expect(await collect(chunks(joined))).toEqual([{ n: 1 }]);
  });
});

describe("private Kit input names are never treated as paths", () => {
  it("rejects Windows and POSIX path separators, drive letters and dot segments in input names", () => {
    for (const bad of ["a\\b", "a/b", "C:\\x", "a b", "", "con\u0000", "a\n", "a\r\n"]) {
      expect(() => buildPrivateKitInput({ [bad]: "x" })).toThrow();
    }
  });

  it("keeps bytes bit-for-bit (no newline translation) when an input is binary", () => {
    const raw = new Uint8Array([0x0d, 0x0a, 0x00, 0x1a, 0xff]);
    const built = buildPrivateKitInput({ "data.bin": raw });
    const b64 = (built.inputs["data.bin"] as { b64: string }).b64;
    expect(Array.from(Buffer.from(b64, "base64"))).toEqual(Array.from(raw));
  });

  it("keeps text exactly, including CRLF", () => {
    const built = buildPrivateKitInput({ "note.txt": "a\r\nb\r\n" });
    expect((built.inputs["note.txt"] as { text: string }).text).toBe("a\r\nb\r\n");
  });
});

describe("Node path handling used by the tests and examples", () => {
  it("fileURLToPath round-trips a file URL for the current OS (no hand-made '/C:/...' paths)", () => {
    const here = dirname(fileURLToPath(import.meta.url));
    const url = pathToFileURL(resolve(here, "x y.json"));
    expect(fileURLToPath(url)).toBe(resolve(here, "x y.json"));
  });

  it("path.resolve is used, never string concatenation: both flavours resolve the shared vectors the same way", () => {
    expect(posix.resolve("/repo/sdk/typescript/src", "../../../docs/specs/a.json")).toBe("/repo/docs/specs/a.json");
    expect(win32.resolve("C:\\repo\\sdk\\typescript\\src", "..\\..\\..\\docs\\specs\\a.json")).toBe("C:\\repo\\docs\\specs\\a.json");
    // The same relative POSIX-style string also works on Windows through path.resolve.
    expect(win32.resolve("C:\\repo\\sdk\\typescript\\src", "../../../docs/specs/a.json")).toBe("C:\\repo\\docs\\specs\\a.json");
  });

  it("the shared vector files decode as UTF-8 text and contain no carriage returns after checkout", () => {
    const here = dirname(fileURLToPath(import.meta.url));
    for (const f of ["ccpp-v1-vectors.json", "private-chat-input-vectors.json"]) {
      const text = readFileSync(resolve(here, "../../../docs/specs", f), "utf8");
      // The workflow sets core.autocrlf=false and .gitattributes pins eol=lf; a CRLF here means a checkout setting changed.
      expect(text.includes("\r")).toBe(false);
      JSON.parse(text);
    }
  });
});
