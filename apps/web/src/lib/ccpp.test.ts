import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";
import {
  VERSION,
  SEAL_ALG,
  DATA_ALG,
  bytesToHex,
  hexToBytes,
  canonicalJson,
  x25519PublicFromPrivate,
  ed25519PublicFromPrivate,
  sign,
  verify,
  seal,
  openSealed,
  encryptData,
  decryptData,
  splitSecret,
  reconstructSecret,
  deriveOutputCommitmentKey,
  computeOutputCommitment,
  CcppCryptoError,
} from "./ccpp";

describe("ccpp-v1 TypeScript / Browser Cryptography", () => {
  const vectorPath = path.resolve(__dirname, "../../../../docs/specs/ccpp-v1-vectors.json");
  const vectors = JSON.parse(fs.readFileSync(vectorPath, "utf-8"));

  it("verifies X25519 public key derivation vector", () => {
    const v = vectors.x25519;
    const pub = x25519PublicFromPrivate(v.private_key_hex);
    expect(pub).toBe(v.expected_public_key_hex);
  });

  it("verifies Ed25519 public key derivation, signing, and verification", () => {
    const v = vectors.ed25519;
    const pub = ed25519PublicFromPrivate(v.private_seed_hex);
    expect(pub).toBe(v.expected_public_key_hex);

    const msg = hexToBytes(v.message_hex);
    const sig = sign(v.private_seed_hex, msg);
    expect(sig).toBe(v.expected_signature_hex);

    const valid = verify(v.expected_public_key_hex, msg, sig);
    expect(valid).toBe(true);
  });

  it("verifies asymmetric sealing vector and opening", () => {
    const v = vectors.sealing;
    const aad = hexToBytes(v.aad_hex);

    // Open existing envelope
    const opened = openSealed(v.recipient_private_hex, v.envelope, aad);
    expect(bytesToHex(opened)).toBe(v.plaintext_hex);

    // Seal with deterministic ephemeral key and nonce
    const ephPriv = hexToBytes(v.ephemeral_private_hex);
    const nonce = hexToBytes(v.nonce_hex);
    const pt = hexToBytes(v.plaintext_hex);
    const sealed = seal(v.recipient_public_hex, pt, aad, ephPriv, nonce);

    expect(sealed.v).toBe(VERSION);
    expect(sealed.alg).toBe(SEAL_ALG);
    expect(sealed.epk).toBe(v.ephemeral_public_hex);
    expect(sealed.nonce).toBe(v.nonce_hex);
    expect(sealed.ct).toBe(v.expected_ciphertext_hex);
  });

  it("verifies symmetric data encryption vector and decryption", () => {
    const v = vectors.data_encryption;
    const dataKey = hexToBytes(v.data_key_hex);
    const aad = hexToBytes(v.aad_hex);

    // Decrypt existing envelope
    const decrypted = decryptData(dataKey, v.envelope, aad);
    expect(bytesToHex(decrypted)).toBe(v.plaintext_hex);

    // Encrypt with deterministic nonce
    const nonce = hexToBytes(v.nonce_hex);
    const pt = hexToBytes(v.plaintext_hex);
    const encrypted = encryptData(dataKey, pt, aad, nonce);

    expect(encrypted.v).toBe(VERSION);
    expect(encrypted.alg).toBe(DATA_ALG);
    expect(encrypted.nonce).toBe(v.nonce_hex);
    expect(encrypted.ct).toBe(v.expected_ciphertext_hex);
  });

  it("verifies Shamir secret sharing and reconstruction vectors", () => {
    const v = vectors.shamir;
    const secret = hexToBytes(v.secret_hex);
    const coeffs = hexToBytes(v.coefficients_hex);

    // Generate shares with deterministic coefficients
    const shares = splitSecret(secret, v.total_shares, v.threshold, coeffs);
    expect(shares.length).toBe(3);
    for (let i = 0; i < 3; i++) {
      expect(shares[i].x).toBe(v.shares[i].x);
      expect(bytesToHex(shares[i].share)).toBe(v.shares[i].share_hex);
    }

    // Reconstruct with all pairwise combinations
    const p1 = reconstructSecret([shares[0], shares[1]], v.threshold);
    expect(bytesToHex(p1)).toBe(v.secret_hex);

    const p2 = reconstructSecret([shares[1], shares[2]], v.threshold);
    expect(bytesToHex(p2)).toBe(v.secret_hex);

    const p3 = reconstructSecret([shares[0], shares[2]], v.threshold);
    expect(bytesToHex(p3)).toBe(v.secret_hex);
  });

  it("verifies output commitment vector (L4)", () => {
    const v = vectors.output_commitment;
    const dataKey = hexToBytes(v.data_key_hex);

    const commitKey = deriveOutputCommitmentKey(dataKey);
    expect(bytesToHex(commitKey)).toBe(v.expected_commit_key_hex);

    const commitment = computeOutputCommitment(dataKey, v.output_json);
    expect(commitment).toBe(v.expected_commitment_hex);
  });

  it("Checkpoint 1.4: ensures a deliberate 1-byte mutation causes failure", () => {
    // 1. Mutate sealing ciphertext
    const sealV = vectors.sealing;
    const mutSealEnv = { ...sealV.envelope };
    const sealCtBytes = hexToBytes(mutSealEnv.ct);
    sealCtBytes[0] ^= 0x01;
    mutSealEnv.ct = bytesToHex(sealCtBytes);

    expect(() => {
      openSealed(sealV.recipient_private_hex, mutSealEnv, hexToBytes(sealV.aad_hex));
    }).toThrow(CcppCryptoError);

    // 2. Mutate data encryption ciphertext
    const dataV = vectors.data_encryption;
    const mutDataEnv = { ...dataV.envelope };
    const dataCtBytes = hexToBytes(mutDataEnv.ct);
    dataCtBytes[0] ^= 0x01;
    mutDataEnv.ct = bytesToHex(dataCtBytes);

    expect(() => {
      decryptData(hexToBytes(dataV.data_key_hex), mutDataEnv, hexToBytes(dataV.aad_hex));
    }).toThrow(CcppCryptoError);

    // 3. Mutate Ed25519 signature
    const edV = vectors.ed25519;
    const sigBytes = hexToBytes(edV.expected_signature_hex);
    sigBytes[0] ^= 0x01;
    const valid = verify(edV.expected_public_key_hex, hexToBytes(edV.message_hex), bytesToHex(sigBytes));
    expect(valid).toBe(false);

    // 4. Mutate Shamir share
    const shamirV = vectors.shamir;
    const share1 = hexToBytes(shamirV.shares[0].share_hex);
    share1[0] ^= 0x01;
    const corruptShares = [
      { x: 1, share: share1 },
      { x: 2, share: hexToBytes(shamirV.shares[1].share_hex) },
    ];
    const recovered = reconstructSecret(corruptShares, shamirV.threshold);
    expect(bytesToHex(recovered)).not.toBe(shamirV.secret_hex);
  });
});

describe("hex parsing is strict", () => {
  it("rejects partial or non-hex input instead of guessing", () => {
    expect(() => hexToBytes("1g")).toThrow();
    expect(() => hexToBytes("zz")).toThrow();
    expect(() => hexToBytes("0x00")).toThrow();
    expect(Array.from(hexToBytes("00ff"))).toEqual([0, 255]);
  });
});
