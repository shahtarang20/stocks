/**
 * Validation for an on-chain transaction receipt typed in by an admin when recording a settlement or refund.
 * The values must come from the real transaction: the page never invents them.
 */
export interface ChainReceipt {
  txHash: string;
  blockNumber: number;
}

const TX_HASH_RE = /^0x[0-9a-fA-F]{64}$/;

/** Returns the receipt, or an error message the admin can act on. */
export function parseChainReceipt(
  txHash: string | null | undefined,
  blockNumber: string | null | undefined,
): { ok: true; receipt: ChainReceipt } | { ok: false; error: string } {
  const hash = (txHash ?? "").trim();
  if (!TX_HASH_RE.test(hash)) {
    return { ok: false, error: "The transaction hash must be 0x followed by 64 hexadecimal characters." };
  }
  const blockText = (blockNumber ?? "").trim();
  if (!/^[0-9]+$/.test(blockText)) {
    return { ok: false, error: "The block number must be a positive whole number." };
  }
  const block = Number(blockText);
  if (!Number.isSafeInteger(block) || block <= 0) {
    return { ok: false, error: "The block number must be a positive whole number." };
  }
  return { ok: true, receipt: { txHash: hash, blockNumber: block } };
}
