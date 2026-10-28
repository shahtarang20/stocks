/**
 * ComputeCoin Proof-of-Privacy (ccpp-v1) Cryptographic Library for TypeScript / Browser.
 *
 * Implements the exact specifications in docs/specs/ccpp-v1.md using audited noble primitives:
 * - X25519 key agreement + HKDF-SHA256 + ChaCha20-Poly1305 (Sealed Envelopes)
 * - ChaCha20-Poly1305 symmetric data encryption (Data Envelopes)
 * - Ed25519 signatures
 * - Shamir secret sharing over GF(256) (polynomial 0x11D, generator 2)
 * - Output commitments (HKDF-SHA256 + HMAC-SHA256)
 */

import { x25519, ed25519 } from "@noble/curves/ed25519.js";
import { chacha20poly1305 } from "@noble/ciphers/chacha.js";
import { hkdf } from "@noble/hashes/hkdf.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { hmac } from "@noble/hashes/hmac.js";

export const VERSION = "ccpp-v1";
export const SEAL_ALG = "X25519-HKDF-SHA256-ChaCha20Poly1305";
export const DATA_ALG = "ChaCha20Poly1305";

export interface SealedEnvelope {
  v: string;
  alg: string;
  epk: string;
  nonce: string;
  ct: string;
}

export interface DataEnvelope {
  v: string;
  alg: string;
  nonce: string;
  ct: string;
}

export interface ShamirShare {
  x: number;
  shareHex: string;
}

export interface PreparedPrivateJob {
  input: Record<string, unknown>;
  encrypted_input: DataEnvelope;
  dk_shares: Record<string, { x: number; envelope: SealedEnvelope }>;
  data_policy: Record<string, unknown>;
}

export class CcppCryptoError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CcppCryptoError";
  }
}

// --------------------------------------------------------------------------- Encoding helpers

export function bytesToHex(bytes: Uint8Array): string {
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

export function hexToBytes(hex: string): Uint8Array {
  // Strict: parseInt alone accepts partial input ("1g" -> 1), which would silently turn
  // malformed keys or ciphertext into different bytes instead of an error.
  if (typeof hex !== "string" || hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) {
    throw new CcppCryptoError("invalid hex string");
  }
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.substring(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

function hex32(hex: string, what: string): Uint8Array {
  const bytes = hexToBytes(hex);
  if (bytes.length !== 32) {
    throw new CcppCryptoError(`${what} must be 32 bytes`);
  }
  return bytes;
}

function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as object).sort()) {
      out[key] = sortKeysDeep((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

// Matches Python's json.dumps(sort_keys=True, separators=(",", ":")): keys sorted at every
// depth and non-ASCII escaped as \uXXXX (ensure_ascii). A replacer array would instead act as
// a key allowlist at every depth and silently drop nested keys.
export function canonicalJson(data: unknown): Uint8Array {
  const str = JSON.stringify(sortKeysDeep(data)).replace(
    /[\u0080-￿]/g,
    (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0")
  );
  return new TextEncoder().encode(str);
}

// --------------------------------------------------------------------------- AAD helpers

export const INPUT_AAD = new TextEncoder().encode(`${VERSION}|job-input`);

export function outputAad(jobId: string): Uint8Array {
  return new TextEncoder().encode(`${VERSION}|job-output|${jobId}`);
}

export function shareAad(guardian: string): Uint8Array {
  return new TextEncoder().encode(`${VERSION}|guardian-share|${guardian}`);
}

export function releaseAad(jobId: string, guardian: string): Uint8Array {
  return new TextEncoder().encode(`${VERSION}|host-release|${jobId}|${guardian}`);
}

// --------------------------------------------------------------------------- Key generation

export function generateX25519Keypair(): { privateKeyHex: string; publicKeyHex: string } {
  const priv = x25519.utils.randomSecretKey();
  const pub = x25519.getPublicKey(priv);
  return {
    privateKeyHex: bytesToHex(priv),
    publicKeyHex: bytesToHex(pub),
  };
}

export function x25519PublicFromPrivate(privateKeyHex: string): string {
  const priv = hex32(privateKeyHex, "private key");
  return bytesToHex(x25519.getPublicKey(priv));
}

export function generateEd25519Keypair(): { privateSeedHex: string; publicKeyHex: string } {
  const seed = ed25519.utils.randomSecretKey();
  const pub = ed25519.getPublicKey(seed);
  return {
    privateSeedHex: bytesToHex(seed),
    publicKeyHex: bytesToHex(pub),
  };
}

export function ed25519PublicFromPrivate(privateSeedHex: string): string {
  const seed = hex32(privateSeedHex, "private seed");
  return bytesToHex(ed25519.getPublicKey(seed));
}

// --------------------------------------------------------------------------- Signatures

export function sign(privateSeedHex: string, message: Uint8Array): string {
  const seed = hex32(privateSeedHex, "signing seed");
  return bytesToHex(ed25519.sign(message, seed));
}

export function verify(publicKeyHex: string, message: Uint8Array, signatureHex: string): boolean {
  try {
    const pub = hex32(publicKeyHex, "verify public key");
    const sig = hexToBytes(signatureHex);
    return ed25519.verify(sig, message, pub);
  } catch {
    return false;
  }
}

// --------------------------------------------------------------------------- Asymmetric Sealing

function deriveSealKey(shared: Uint8Array, epk: Uint8Array, recipientPub: Uint8Array): Uint8Array {
  const prefix = new TextEncoder().encode(`${VERSION}|`);
  const info = new Uint8Array(prefix.length + 64);
  info.set(prefix, 0);
  info.set(epk, prefix.length);
  info.set(recipientPub, prefix.length + 32);
  return hkdf(sha256, shared, undefined, info, 32);
}

export function seal(
  recipientPublicHex: string,
  plaintext: Uint8Array,
  aad: Uint8Array,
  customEphemeralPriv?: Uint8Array,
  customNonce?: Uint8Array
): SealedEnvelope {
  const recipientPub = hex32(recipientPublicHex, "recipient public key");
  const ephPriv = customEphemeralPriv || x25519.utils.randomSecretKey();
  const epk = x25519.getPublicKey(ephPriv);
  const shared = x25519.getSharedSecret(ephPriv, recipientPub);
  const sealKey = deriveSealKey(shared, epk, recipientPub);
  const nonce = customNonce || crypto.getRandomValues(new Uint8Array(12));

  const cipher = chacha20poly1305(sealKey, nonce, aad);
  const ct = cipher.encrypt(plaintext);

  return {
    v: VERSION,
    alg: SEAL_ALG,
    epk: bytesToHex(epk),
    nonce: bytesToHex(nonce),
    ct: bytesToHex(ct),
  };
}

export function openSealed(
  recipientPrivateHex: string,
  envelope: SealedEnvelope,
  aad: Uint8Array
): Uint8Array {
  if (!envelope || envelope.v !== VERSION || envelope.alg !== SEAL_ALG) {
    throw new CcppCryptoError("not a ccpp-v1 sealed envelope");
  }
  const priv = hex32(recipientPrivateHex, "recipient private key");
  const epk = hex32(envelope.epk, "envelope epk");
  const nonce = hexToBytes(envelope.nonce);
  const ct = hexToBytes(envelope.ct);

  const recipientPub = x25519.getPublicKey(priv);
  const shared = x25519.getSharedSecret(priv, epk);
  const sealKey = deriveSealKey(shared, epk, recipientPub);

  try {
    const cipher = chacha20poly1305(sealKey, nonce, aad);
    return cipher.decrypt(ct);
  } catch (err) {
    throw new CcppCryptoError("sealed envelope does not open with this key: " + (err as Error).message);
  }
}

// --------------------------------------------------------------------------- Symmetric Data Encryption

export function encryptData(
  dataKey: Uint8Array,
  plaintext: Uint8Array,
  aad: Uint8Array,
  customNonce?: Uint8Array
): DataEnvelope {
  if (dataKey.length !== 32) {
    throw new CcppCryptoError("data key must be 32 bytes");
  }
  const nonce = customNonce || crypto.getRandomValues(new Uint8Array(12));
  const cipher = chacha20poly1305(dataKey, nonce, aad);
  const ct = cipher.encrypt(plaintext);

  return {
    v: VERSION,
    alg: DATA_ALG,
    nonce: bytesToHex(nonce),
    ct: bytesToHex(ct),
  };
}

export function decryptData(
  dataKey: Uint8Array,
  envelope: DataEnvelope,
  aad: Uint8Array
): Uint8Array {
  if (!envelope || envelope.v !== VERSION || envelope.alg !== DATA_ALG) {
    throw new CcppCryptoError("not a ccpp-v1 data envelope");
  }
  if (dataKey.length !== 32) {
    throw new CcppCryptoError("data key must be 32 bytes");
  }
  const nonce = hexToBytes(envelope.nonce);
  const ct = hexToBytes(envelope.ct);

  try {
    const cipher = chacha20poly1305(dataKey, nonce, aad);
    return cipher.decrypt(ct);
  } catch (err) {
    throw new CcppCryptoError("data envelope does not decrypt with this key: " + (err as Error).message);
  }
}

// --------------------------------------------------------------------------- Shamir over GF(256)

const GF_EXP = new Uint8Array(512);
const GF_LOG = new Uint8Array(256);

(function initGf() {
  let x = 1;
  for (let i = 0; i < 255; i++) {
    GF_EXP[i] = x;
    GF_EXP[i + 255] = x;
    GF_LOG[x] = i;
    x <<= 1;
    if (x & 0x100) {
      x ^= 0x11D; // primitive poly x^8 + x^4 + x^3 + x^2 + 1
    }
  }
})();

function gfMul(a: number, b: number): number {
  return a === 0 || b === 0 ? 0 : GF_EXP[GF_LOG[a] + GF_LOG[b]];
}

function gfDiv(a: number, b: number): number {
  if (b === 0) throw new CcppCryptoError("GF(256) division by zero");
  return a === 0 ? 0 : GF_EXP[(GF_LOG[a] - GF_LOG[b] + 255) % 255];
}

export function splitSecret(
  secret: Uint8Array,
  n = 3,
  k = 2,
  deterministicCoeffs?: Uint8Array
): Array<{ x: number; share: Uint8Array }> {
  if (k < 1 || k > n || n > 255) {
    throw new CcppCryptoError("need 1 <= k <= n <= 255");
  }
  const shares: Uint8Array[] = Array.from({ length: n }, () => new Uint8Array(secret.length));

  for (let i = 0; i < secret.length; i++) {
    const coeffs = new Uint8Array(k);
    coeffs[0] = secret[i];
    for (let c = 1; c < k; c++) {
      coeffs[c] = deterministicCoeffs
        ? deterministicCoeffs[i]
        : crypto.getRandomValues(new Uint8Array(1))[0];
    }

    for (let x = 1; x <= n; x++) {
      let acc = 0;
      let power = 1;
      for (let c = 0; c < k; c++) {
        acc ^= gfMul(coeffs[c], power);
        power = gfMul(power, x);
      }
      shares[x - 1][i] = acc;
    }
  }

  return shares.map((s, idx) => ({ x: idx + 1, share: s }));
}

export function reconstructSecret(
  shares: Array<{ x: number; share: Uint8Array }>,
  k = 2
): Uint8Array {
  if (shares.length < k) {
    throw new CcppCryptoError(`need at least ${k} shares, got ${shares.length}`);
  }
  const use = shares.slice(0, k);
  const xs = use.map((s) => s.x);
  if (new Set(xs).size !== k || xs.some((x) => x < 1 || x > 255)) {
    throw new CcppCryptoError("share x coordinates must be distinct in 1..255");
  }
  const len = use[0].share.length;
  if (use.some((s) => s.share.length !== len)) {
    throw new CcppCryptoError("all shares must have equal length");
  }

  const weights = new Uint8Array(k);
  for (let j = 0; j < k; j++) {
    let num = 1;
    let den = 1;
    for (let m = 0; m < k; m++) {
      if (m !== j) {
        num = gfMul(num, xs[m]);
        den = gfMul(den, xs[j] ^ xs[m]);
      }
    }
    weights[j] = gfDiv(num, den);
  }

  const out = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    let acc = 0;
    for (let j = 0; j < k; j++) {
      acc ^= gfMul(use[j].share[i], weights[j]);
    }
    out[i] = acc;
  }
  return out;
}

// --------------------------------------------------------------------------- Output Commitment (L4)

export function deriveOutputCommitmentKey(dataKey: Uint8Array): Uint8Array {
  if (dataKey.length !== 32) {
    throw new CcppCryptoError("data key must be 32 bytes");
  }
  return hkdf(sha256, dataKey, undefined, new TextEncoder().encode(`${VERSION}|output-commit`), 32);
}

export function computeOutputCommitment(dataKey: Uint8Array, output: unknown): string {
  const commitKey = deriveOutputCommitmentKey(dataKey);
  const msg = output instanceof Uint8Array ? output : canonicalJson(output);
  return bytesToHex(hmac(sha256, commitKey, msg));
}

// --------------------------------------------------------------------------- Customer Workflow

export function preparePrivateJob(
  inputDict: Record<string, unknown>,
  guardians: Record<string, string>,
  threshold: number,
  resultPublicKeyHex: string,
  dataPolicy: Record<string, unknown> = {}
): PreparedPrivateJob {
  const dataKey = crypto.getRandomValues(new Uint8Array(32));
  const plaintext = new TextEncoder().encode(JSON.stringify(inputDict));
  const encryptedInput = encryptData(dataKey, plaintext, INPUT_AAD);

  const guardianNames = Object.keys(guardians).sort();
  if (guardianNames.length < threshold || threshold < 2) {
    throw new CcppCryptoError("threshold must be >= 2 and <= guardian count");
  }

  const shares = splitSecret(dataKey, guardianNames.length, threshold);
  const dkShares: Record<string, { x: number; envelope: SealedEnvelope }> = {};

  guardianNames.forEach((name, idx) => {
    const sh = shares[idx];
    const envelope = seal(guardians[name], sh.share, shareAad(name));
    dkShares[name] = { x: sh.x, envelope };
  });

  return {
    input: {},
    encrypted_input: encryptedInput,
    dk_shares: dkShares,
    data_policy: { ...dataPolicy, result_public_key: resultPublicKeyHex },
  };
}

// --------------------------------------------------------------------------- Guardian key pinning

/** The guardian keys come from the platform; ComputeCoin publishes the fingerprint of the guardian
 * set out of band, and the client refuses any set that does not match it (spec section 6e). */
export const GUARDIAN_MANIFEST_VERSION = "ccpp-guardians-v1";

export class GuardianPinError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GuardianPinError";
  }
}

export function guardianManifestFingerprint(guardians: Record<string, string>, threshold: number): string {
  const lowered: Record<string, string> = {};
  for (const [name, key] of Object.entries(guardians)) lowered[name] = key.toLowerCase();
  return bytesToHex(sha256(canonicalJson({ v: GUARDIAN_MANIFEST_VERSION, threshold, guardians: lowered })));
}

/** Returns "pinned" when the set matches the pin, "unpinned" when there is no pin (and it is not
 * required). Throws GuardianPinError on a mismatch, or when a pin is required but missing. */
export function checkGuardianPin(
  guardians: Record<string, string>,
  threshold: number,
  pinnedFingerprint: string | undefined,
  requirePin = false
): "pinned" | "unpinned" {
  const pin = (pinnedFingerprint ?? "").trim().toLowerCase();
  if (!pin) {
    if (requirePin) throw new GuardianPinError("No guardian key fingerprint is pinned in this build.");
    return "unpinned";
  }
  const actual = guardianManifestFingerprint(guardians, threshold);
  if (actual !== pin) {
    throw new GuardianPinError(
      `The guardian keys served by the platform do not match the published fingerprint (got ${actual}).`
    );
  }
  return "pinned";
}

export function decryptJobOutput(
  encryptedOutput: SealedEnvelope,
  customerPrivateHex: string,
  jobId: string
): unknown {
  const ptBytes = openSealed(customerPrivateHex, encryptedOutput, outputAad(jobId));
  const text = new TextDecoder().decode(ptBytes);
  return JSON.parse(text);
}

// --------------------------------------------------------------------------- Receipt & Merkle Verification (L5)

function cleanHex(hex: string): string {
  return typeof hex === "string" && (hex.startsWith("0x") || hex.startsWith("0X"))
    ? hex.slice(2)
    : hex;
}

export const VERDICT_FIELDS = [
  "verdict_id",
  "job_id",
  "machine_id",
  "image_hash",
  "tier_reached",
  "ephemeral_public_key",
  "receipt_signing_public_key",
  "nonce",
  "issued_at",
  "expires_at",
  "verifier_public_key",
] as const;

export const WIPE_FIELDS = ["machine_id", "job_id", "wiped_items", "wiped_at"] as const;

export const TIER_RANK: Record<string, number> = {
  open: 0,
  container: 0,
  gvisor: 1,
  microvm: 2,
  sealed: 3,
  sealed_pro: 4,
  confidential: 5,
};

export function hashLeaf(eventType: string, payload: unknown): string {
  const prefix = new TextEncoder().encode("\x00" + eventType + ":");
  const payloadBytes = canonicalJson(payload);
  const data = new Uint8Array(prefix.length + payloadBytes.length);
  data.set(prefix, 0);
  data.set(payloadBytes, prefix.length);
  return bytesToHex(sha256(data));
}

export function nodeHash(leftHex: string, rightHex: string): string {
  const left = hexToBytes(cleanHex(leftHex));
  const right = hexToBytes(cleanHex(rightHex));
  const data = new Uint8Array(1 + 32 + 32);
  data[0] = 0x01;
  data.set(left, 1);
  data.set(right, 33);
  return bytesToHex(sha256(data));
}

export function verifyInclusion(
  leafHash: string,
  leafIndex: number,
  treeSize: number,
  root: string,
  path: string[]
): boolean {
  if (
    typeof leafIndex !== "number" ||
    typeof treeSize !== "number" ||
    leafIndex < 0 ||
    leafIndex >= treeSize
  ) {
    return false;
  }
  let fn = BigInt(leafIndex);
  let sn = BigInt(treeSize - 1);
  let r = cleanHex(leafHash);
  for (const p of path) {
    if (sn === 0n) return false;
    if ((fn & 1n) !== 0n || fn === sn) {
      r = nodeHash(p, r);
      if ((fn & 1n) === 0n) {
        while ((fn & 1n) === 0n && fn !== 0n) {
          fn >>= 1n;
          sn >>= 1n;
        }
      }
    } else {
      r = nodeHash(r, p);
    }
    fn >>= 1n;
    sn >>= 1n;
  }
  return sn === 0n && r.toLowerCase() === cleanHex(root).toLowerCase();
}

export function verifyConsistencyProof(
  firstSize: number,
  secondSize: number,
  firstRoot: string,
  secondRoot: string,
  proof: string[]
): boolean {
  try {
    const m = BigInt(firstSize);
    const n = BigInt(secondSize);
    if (m < 0n || n < 0n || m > n) return false;
    if (m === 0n) return proof.length === 0;
    if (m === n) {
      return (
        proof.length === 0 &&
        cleanHex(firstRoot).toLowerCase() === cleanHex(secondRoot).toLowerCase()
      );
    }

    let fn = m - 1n;
    let sn = n - 1n;
    while ((fn & 1n) !== 0n) {
      fn >>= 1n;
      sn >>= 1n;
    }

    const p = [...proof];
    let fr: string;
    let sr: string;
    if (fn === 0n) {
      fr = cleanHex(firstRoot);
      sr = cleanHex(firstRoot);
    } else {
      if (p.length === 0) return false;
      fr = cleanHex(p[0]);
      sr = cleanHex(p[0]);
      p.shift();
    }

    for (const c of p) {
      if (sn === 0n) return false;
      if ((fn & 1n) !== 0n || fn === sn) {
        fr = nodeHash(c, fr);
        sr = nodeHash(c, sr);
        while ((fn & 1n) === 0n && fn !== 0n) {
          fn >>= 1n;
          sn >>= 1n;
        }
      } else {
        sr = nodeHash(sr, c);
      }
      fn >>= 1n;
      sn >>= 1n;
    }

    return (
      sn === 0n &&
      fr.toLowerCase() === cleanHex(firstRoot).toLowerCase() &&
      sr.toLowerCase() === cleanHex(secondRoot).toLowerCase()
    );
  } catch {
    return false;
  }
}

export function verifyVerdict(
  verdict: Record<string, unknown>,
  verifierPublicKeyHex: string
): boolean {
  if (!verdict || verdict.verifier_public_key !== verifierPublicKeyHex) return false;
  for (const f of VERDICT_FIELDS) {
    if (!(f in verdict)) return false;
  }
  const body: Record<string, unknown> = {};
  for (const f of VERDICT_FIELDS) {
    body[f] = verdict[f];
  }
  return verify(verifierPublicKeyHex, canonicalJson(body), String(verdict.signature ?? ""));
}

export function verifyWipe(
  wipe: Record<string, unknown>,
  receiptSigningPublicKeyHex: string
): boolean {
  if (!wipe) return false;
  for (const f of WIPE_FIELDS) {
    if (!(f in wipe)) return false;
  }
  const body: Record<string, unknown> = {};
  for (const f of WIPE_FIELDS) {
    body[f] = wipe[f];
  }
  if (Array.isArray(body.wiped_items)) {
    body.wiped_items = [...body.wiped_items].sort();
  }
  return verify(receiptSigningPublicKeyHex, canonicalJson(body), String(wipe.signature ?? ""));
}

/** Equal as JSON values, whatever the key order (the logged copy and the receipt's copy come
 * from different database columns, so their key order may differ). Mirrors Python dict equality. */
function sameJson(a: unknown, b: unknown): boolean {
  if (a === undefined || b === undefined) return false;
  const ea = canonicalJson(a);
  const eb = canonicalJson(b);
  return ea.length === eb.length && ea.every((byte, i) => byte === eb[i]);
}

export interface ReceiptVerificationResult {
  verified: boolean;
  isolation_tier?: string;
  machine_id?: string | null;
  checks: Record<string, unknown>;
}

export function verifyReceiptLocal(
  receipt: Record<string, any>,
  verifierPublicKey: string,
  expectedImageHash?: string
): ReceiptVerificationResult {
  const checks: Record<string, unknown> = {};
  const verdict = receipt.attestation_verdict || {};
  const jobId = String(receipt.job_id || "");

  checks.verdict_signature = verifyVerdict(verdict, verifierPublicKey);
  const verdictTierRank = TIER_RANK[String(verdict.tier_reached)] ?? -1;
  const receiptTierRank = TIER_RANK[String(receipt.isolation_tier)] ?? 99;
  checks.tier_matches = verdictTierRank >= receiptTierRank;
  checks.image_matches = Boolean(verdict) && receipt.image_hash === verdict.image_hash;
  checks.verdict_for_this_job = Boolean(jobId) && verdict.job_id === jobId;
  if (expectedImageHash !== undefined) {
    checks.expected_image = verdict.image_hash === expectedImageHash;
  }

  const root = String(receipt.merkle_root || "");
  const proofs: Record<string, boolean> = {};
  const incProofs = receipt.inclusion_proofs || {};

  for (const [name, p] of Object.entries(incProofs)) {
    let ok = false;
    try {
      const proofObj = p as any;
      const expectedLeafHash = hashLeaf(proofObj.event_type, proofObj.payload);
      if (expectedLeafHash.toLowerCase() === cleanHex(String(proofObj.leaf_hash)).toLowerCase()) {
        if (
          verifyInclusion(
            proofObj.leaf_hash,
            Number(proofObj.leaf_index),
            Number(proofObj.tree_size),
            root,
            proofObj.audit_path || []
          )
        ) {
          ok = true;
          if (name === "attestation_verdict") {
            ok =
              ok &&
              proofObj.event_type === "ATTESTATION_VERDICT" &&
              sameJson(proofObj.payload, verdict);
          } else if (name.startsWith("key_release:")) {
            ok =
              ok &&
              proofObj.event_type === "KEY_RELEASE" &&
              proofObj.payload?.job_id === jobId &&
              proofObj.payload?.verdict_id === verdict.verdict_id;
          } else if (name === "wipe_receipt") {
            ok =
              ok &&
              proofObj.event_type === "WIPE_RECEIPT" &&
              sameJson(proofObj.payload, receipt.wipe_receipt);
          }
        }
      }
    } catch {
      ok = false;
    }
    proofs[name] = ok;
  }
  checks.inclusion_proofs = proofs;
  checks.verdict_logged = proofs["attestation_verdict"] === true;

  const wipe = receipt.wipe_receipt;
  checks.wipe_receipt = wipe
    ? wipe.job_id === jobId && verifyWipe(wipe, String(verdict.receipt_signing_public_key || ""))
    : "absent";

  const anchor = receipt.anchor;
  if (anchor) {
    const anchorRoot = String(anchor.merkle_root || "");
    const anchorSize = Number(anchor.tree_size || 0);
    const consistencyPath = (anchor.consistency_proof || []) as string[];
    let receiptTreeSize = 0;
    for (const p of Object.values(incProofs)) {
      if (p && typeof p === "object" && "tree_size" in p) {
        receiptTreeSize = Number((p as any).tree_size);
        break;
      }
    }

    let consistencyOk = false;
    if (receiptTreeSize === anchorSize) {
      consistencyOk =
        cleanHex(root).toLowerCase() === cleanHex(anchorRoot).toLowerCase() &&
        consistencyPath.length === 0;
    } else if (receiptTreeSize > 0 && receiptTreeSize < anchorSize) {
      consistencyOk = verifyConsistencyProof(
        receiptTreeSize,
        anchorSize,
        root,
        anchorRoot,
        consistencyPath
      );
    }
    checks.anchor_consistency = consistencyOk;
  }

  const allProofsPass =
    Object.keys(proofs).length > 0 && Object.values(proofs).every((v) => v === true);

  const verified =
    checks.verdict_signature === true &&
    checks.tier_matches === true &&
    checks.image_matches === true &&
    checks.verdict_for_this_job === true &&
    (checks.expected_image === undefined || checks.expected_image === true) &&
    checks.verdict_logged === true &&
    allProofsPass &&
    (checks.wipe_receipt === true || checks.wipe_receipt === "absent") &&
    (checks.anchor_consistency === undefined || checks.anchor_consistency === true);

  return {
    verified,
    isolation_tier: receipt.isolation_tier,
    machine_id: receipt.machine_id,
    checks,
  };
}
