import { describe, expect, it } from "vitest";
import { parseChainReceipt } from "./chain-receipt";

const HASH = "0x" + "ab12".repeat(16);

describe("parseChainReceipt", () => {
  it("accepts a real-looking hash and block", () => {
    const r = parseChainReceipt(` ${HASH} `, " 104523 ");
    expect(r).toEqual({ ok: true, receipt: { txHash: HASH, blockNumber: 104523 } });
  });

  it("rejects a missing, short, or non-hex hash", () => {
    expect(parseChainReceipt(null, "5").ok).toBe(false);
    expect(parseChainReceipt("", "5").ok).toBe(false);
    expect(parseChainReceipt("0x1234", "5").ok).toBe(false);
    expect(parseChainReceipt("0x" + "g".repeat(64), "5").ok).toBe(false);
    expect(parseChainReceipt(HASH.slice(2), "5").ok).toBe(false);
  });

  it("rejects a missing, zero, negative, fractional or huge block number", () => {
    for (const b of [null, "", "0", "-3", "1.5", "abc", "99999999999999999999"]) {
      expect(parseChainReceipt(HASH, b).ok).toBe(false);
    }
  });
});
