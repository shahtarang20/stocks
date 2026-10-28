import { describe, it, expect, beforeEach } from "vitest";
import {
  preparePrivateJob,
  decryptJobOutput,
  seal,
  canonicalJson,
  outputAad,
  generateEd25519Keypair,
  generateX25519Keypair,
  sign,
  hashLeaf,
  nodeHash,
  verifyReceiptLocal,
  CcppCryptoError,
  VERDICT_FIELDS,
  checkGuardianPin,
  guardianManifestFingerprint,
  GuardianPinError,
} from "./ccpp";
import {
  getCustomerKey,
  getOrGenerateCustomerKey,
  markCustomerKeyBackedUp,
  exportCustomerKeyBackupJson,
  importCustomerKey,
  deleteCustomerKey,
  UnbackedKeyError,
} from "./customer-keys";
import { readFileSync } from "node:fs";
import { join } from "node:path";

describe("WP-D: Website Private Jobs (Browser Cryptography & Flow)", () => {
  beforeEach(async () => {
    await deleteCustomerKey();
  });

  describe("D1: Submit payload formatting", () => {
    it("produces input: {}, encrypted_input envelope, sealed dk_shares, and result_public_key", () => {
      const custKp = generateX25519Keypair();
      const g1 = generateX25519Keypair();
      const g2 = generateX25519Keypair();
      const guardians = {
        guardian_a: g1.publicKeyHex,
        guardian_b: g2.publicKeyHex,
      };

      const rawInput = { prompt: "Secret financial analysis prompt", max_tokens: 128 };
      const prepared = preparePrivateJob(rawInput, guardians, 2, custKp.publicKeyHex);

      // D1 check: input is empty object {}
      expect(prepared.input).toEqual({});

      // D1 check: encrypted_input is a valid ccpp-v1 ChaCha20Poly1305 envelope
      expect(prepared.encrypted_input.v).toBe("ccpp-v1");
      expect(prepared.encrypted_input.alg).toBe("ChaCha20Poly1305");
      expect(typeof prepared.encrypted_input.nonce).toBe("string");
      expect(typeof prepared.encrypted_input.ct).toBe("string");
      expect(prepared.encrypted_input.nonce.length).toBe(24); // 12 bytes hex

      // D1 check: dk_shares contains shares for each guardian sealed with X25519
      expect(Object.keys(prepared.dk_shares).sort()).toEqual(["guardian_a", "guardian_b"]);
      for (const name of ["guardian_a", "guardian_b"]) {
        const sh = prepared.dk_shares[name];
        expect(sh.x).toBeGreaterThan(0);
        expect(sh.envelope.v).toBe("ccpp-v1");
        expect(sh.envelope.alg).toBe("X25519-HKDF-SHA256-ChaCha20Poly1305");
        expect(typeof sh.envelope.epk).toBe("string");
        expect(typeof sh.envelope.nonce).toBe("string");
        expect(typeof sh.envelope.ct).toBe("string");
      }

      // D1 check: data_policy contains result_public_key
      expect(prepared.data_policy.result_public_key).toBe(custKp.publicKeyHex);

      // CRITICAL: Customer private key is NEVER in the prepared payload
      const jsonStr = JSON.stringify(prepared);
      expect(jsonStr).not.toContain(custKp.privateKeyHex);
    });
  });

  describe("D2: Customer key lifecycle & backup gate", () => {
    it("generates customer key, flags backedUp as false, and enforces backup export", async () => {
      const key = await getOrGenerateCustomerKey();
      expect(key.privateKeyHex).toHaveLength(64);
      expect(key.publicKeyHex).toHaveLength(64);
      expect(key.backedUp).toBe(false);

      // Export backup JSON
      const backupJson = await exportCustomerKeyBackupJson();
      const parsed = JSON.parse(backupJson);
      expect(parsed.app).toBe("ComputeCoin Proof-of-Privacy");
      expect(parsed.version).toBe("ccpp-v1");
      expect(parsed.publicKeyHex).toBe(key.publicKeyHex);
      expect(parsed.privateKeyHex).toBe(key.privateKeyHex);
      expect(parsed.notice).toContain("CRITICAL");

      // Mark backed up
      await markCustomerKeyBackedUp();
      const updated = await getCustomerKey();
      expect(updated?.backedUp).toBe(true);
    });

    it("imports customer key from JSON or raw hex", async () => {
      const original = generateX25519Keypair();
      const backupObj = {
        privateKeyHex: original.privateKeyHex,
        publicKeyHex: original.publicKeyHex,
      };

      // Import from JSON
      const imported1 = await importCustomerKey(JSON.stringify(backupObj));
      expect(imported1.publicKeyHex).toBe(original.publicKeyHex);
      expect(imported1.privateKeyHex).toBe(original.privateKeyHex);
      expect(imported1.backedUp).toBe(true);

      // Import from hex
      const kp2 = generateX25519Keypair();
      const imported2 = await importCustomerKey(kp2.privateKeyHex);
      expect(imported2.publicKeyHex).toBe(kp2.publicKeyHex);
      expect(imported2.privateKeyHex).toBe(kp2.privateKeyHex);
    });
  });

  describe("D3: Result decryption & error handling", () => {
    it("decrypts job output sealed to customer public key", () => {
      const cust = generateX25519Keypair();
      const jobId = "job-42-test";
      const payload = { prediction: "Bullish", confidence: 0.98, tokens: [1, 2, 3] };

      // Encrypt as host would
      const ptBytes = canonicalJson(payload);
      const envelope = seal(cust.publicKeyHex, ptBytes, outputAad(jobId));

      // Decrypt in browser
      const decrypted = decryptJobOutput(envelope, cust.privateKeyHex, jobId);
      expect(decrypted).toEqual(payload);
    });

    it("shows a clear CcppCryptoError when opened with wrong key", () => {
      const cust = generateX25519Keypair();
      const wrong = generateX25519Keypair();
      const jobId = "job-42-test";
      const payload = { secret: "top-secret" };

      const envelope = seal(cust.publicKeyHex, canonicalJson(payload), outputAad(jobId));

      expect(() => {
        decryptJobOutput(envelope, wrong.privateKeyHex, jobId);
      }).toThrowError(/sealed envelope does not open with this key/);
    });
  });

  describe("D5: In-browser receipt verification", () => {
    function buildTestReceipt(verdictJob = "j1") {
      const v = generateEd25519Keypair();
      const r = generateEd25519Keypair();
      const eph = generateX25519Keypair();

      const imageHash = "ab".repeat(32);
      const verdictBody: Record<string, unknown> = {
        verdict_id: "v1",
        job_id: verdictJob,
        machine_id: "m1",
        image_hash: imageHash,
        tier_reached: "sealed",
        ephemeral_public_key: eph.publicKeyHex,
        receipt_signing_public_key: r.publicKeyHex,
        nonce: "n".repeat(16),
        issued_at: "2026-10-26T10:00:00+00:00",
        expires_at: "2026-10-26T10:10:00+00:00",
        verifier_public_key: v.publicKeyHex,
      };

      const verdictBodyToSign: Record<string, unknown> = {};
      for (const f of VERDICT_FIELDS) {
        verdictBodyToSign[f] = verdictBody[f];
      }
      const verdictSignature = sign(v.privateSeedHex, canonicalJson(verdictBodyToSign));
      const verdict = { ...verdictBody, signature: verdictSignature };

      const wipeBody = {
        machine_id: "m1",
        job_id: "j1",
        wiped_items: ["key_material"],
        wiped_at: "2026-10-26T10:05:00+00:00",
      };
      const wipeSignature = sign(r.privateSeedHex, canonicalJson(wipeBody));
      const wipe = { ...wipeBody, signature: wipeSignature };

      const entries: Array<[string, Record<string, unknown>]> = [
        ["IMAGE_HASH", { image_hash: imageHash }],
        ["ATTESTATION_VERDICT", verdict],
        [
          "KEY_RELEASE",
          { guardian: "computecoin", job_id: "j1", verdict_id: "v1", share_index: 1 },
        ],
        [
          "KEY_RELEASE",
          { guardian: "partner", job_id: "j1", verdict_id: "v1", share_index: 2 },
        ],
        ["WIPE_RECEIPT", wipe],
      ];

      const leaves = entries.map(([evt, payload]) => hashLeaf(evt, payload));

      function mth(l: string[]): string {
        if (l.length === 1) return l[0];
        let k = 1;
        while (k << 1 < l.length) k <<= 1;
        return nodeHash(mth(l.slice(0, k)), mth(l.slice(k)));
      }

      function path(m: number, l: string[]): string[] {
        if (l.length <= 1) return [];
        let k = 1;
        while (k << 1 < l.length) k <<= 1;
        return m < k
          ? [...path(m, l.slice(0, k)), mth(l.slice(k))]
          : [...path(m - k, l.slice(k)), mth(l.slice(0, k))];
      }

      const root = mth(leaves);

      function proof(i: number) {
        return {
          leaf_index: i,
          tree_size: leaves.length,
          leaf_hash: leaves[i],
          root_hash: root,
          audit_path: path(i, leaves),
          event_type: entries[i][0],
          payload: entries[i][1],
        };
      }

      const receipt = {
        job_id: "j1",
        machine_id: "m1",
        isolation_tier: "sealed",
        image_hash: imageHash,
        attestation_verdict: verdict,
        wipe_receipt: wipe,
        merkle_root: root,
        inclusion_proofs: {
          attestation_verdict: proof(1),
          "key_release:computecoin": proof(2),
          "key_release:partner": proof(3),
          wipe_receipt: proof(4),
        },
      };

      return { receipt, verifierPubKey: v.publicKeyHex, imageHash };
    }

    it("verifies a genuine receipt in browser", () => {
      const { receipt, verifierPubKey, imageHash } = buildTestReceipt();
      const res = verifyReceiptLocal(receipt, verifierPubKey, imageHash);
      expect(res.verified).toBe(true);
      expect(res.checks.verdict_signature).toBe(true);
      expect(res.checks.tier_matches).toBe(true);
      expect(res.checks.image_matches).toBe(true);
      expect(res.checks.verdict_for_this_job).toBe(true);
      expect(res.checks.verdict_logged).toBe(true);
      expect(res.checks.wipe_receipt).toBe(true);
    });

    it("refuses tampered receipts", () => {
      const { receipt, verifierPubKey } = buildTestReceipt();

      // Tamper 1: forged verdict signature
      const r1 = JSON.parse(JSON.stringify(receipt));
      r1.attestation_verdict.signature = "aa".repeat(64);
      expect(verifyReceiptLocal(r1, verifierPubKey).verified).toBe(false);

      // Tamper 2: wrong job_id
      const { receipt: r2, verifierPubKey: v2 } = buildTestReceipt("different-job");
      expect(verifyReceiptLocal(r2, v2).verified).toBe(false);

      // Tamper 3: tampered Merkle root
      const r3 = JSON.parse(JSON.stringify(receipt));
      r3.merkle_root = "00".repeat(32);
      expect(verifyReceiptLocal(r3, verifierPubKey).verified).toBe(false);

      // Tamper 4: modified payload in inclusion proof
      const r4 = JSON.parse(JSON.stringify(receipt));
      r4.inclusion_proofs.wipe_receipt.payload.wiped_items = ["hacked"];
      expect(verifyReceiptLocal(r4, verifierPubKey).verified).toBe(false);
    });

    it("does not depend on key order (the logged copy and the receipt copy come from different columns)", () => {
      const { receipt, verifierPubKey } = buildTestReceipt();
      const reversed = (o: Record<string, unknown>) =>
        Object.fromEntries(Object.entries(o).reverse());
      const r = JSON.parse(JSON.stringify(receipt));
      r.attestation_verdict = reversed(r.attestation_verdict);
      r.wipe_receipt = reversed(r.wipe_receipt);
      const res = verifyReceiptLocal(r, verifierPubKey);
      expect(res.checks.inclusion_proofs).toEqual({
        attestation_verdict: true,
        "key_release:computecoin": true,
        "key_release:partner": true,
        wipe_receipt: true,
      });
      expect(res.verified).toBe(true);
    });
  });

  describe("D2: key import never silently destroys an unbacked key", () => {
    it("refuses to replace a different key that was never backed up unless confirmed", async () => {
      const current = await getOrGenerateCustomerKey();
      expect(current.backedUp).toBe(false);
      const other = generateX25519Keypair();
      await expect(importCustomerKey(other.privateKeyHex)).rejects.toBeInstanceOf(UnbackedKeyError);
      expect((await getCustomerKey())?.publicKeyHex).toBe(current.publicKeyHex);

      // Re-importing the same key is fine; so is replacing once the user confirmed it.
      await importCustomerKey(current.privateKeyHex);
      await deleteCustomerKey();
      const again = await getOrGenerateCustomerKey();
      const replaced = await importCustomerKey(other.privateKeyHex, { replaceUnbackedKey: true });
      expect(replaced.publicKeyHex).toBe(other.publicKeyHex);
      expect(replaced.publicKeyHex).not.toBe(again.publicKeyHex);
    });

    it("replaces a backed-up key without asking", async () => {
      await getOrGenerateCustomerKey();
      await markCustomerKeyBackedUp();
      const other = generateX25519Keypair();
      expect((await importCustomerKey(other.privateKeyHex)).publicKeyHex).toBe(other.publicKeyHex);
    });
  });

  describe("Guardian key pinning (spec 6e)", () => {
    const vector = { computecoin: "AA".repeat(32), partner: "bb".repeat(32) };
    const VECTOR = "90047f24b306726940afab8f77365e85bf3b89f88bd916a9c5e60da4890a31f1";

    it("matches the shared fingerprint vector (same as the API and the Python SDK)", () => {
      expect(guardianManifestFingerprint(vector, 2)).toBe(VECTOR);
      expect(guardianManifestFingerprint(vector, 3)).not.toBe(VECTOR);
    });

    it("refuses a guardian set that differs from the pin, and an unpinned build in strict mode", () => {
      expect(checkGuardianPin(vector, 2, VECTOR)).toBe("pinned");
      const swapped = { ...vector, computecoin: generateX25519Keypair().publicKeyHex };
      expect(() => checkGuardianPin(swapped, 2, VECTOR)).toThrow(GuardianPinError);
      expect(checkGuardianPin(vector, 2, undefined)).toBe("unpinned");
      expect(() => checkGuardianPin(vector, 2, undefined, true)).toThrow(GuardianPinError);
    });
  });

  describe("D6: UI copy (R9: no hardware attestation claims, say what is not protected)", () => {
    const pages = ["new/page.tsx", "[jobId]/page.tsx"].map((f) =>
      readFileSync(join(__dirname, "../app/dashboard/jobs", f), "utf8")
    );
    const all = pages.join("\n");

    it("makes no claim the system cannot back", () => {
      for (const phrase of [
        "attested sandbox",
        "decrypted only inside",
        "audited",
        "does not trust any server",
        "that machine wipe occurred",
      ]) {
        expect(all).not.toContain(phrase);
      }
    });

    it("says hardware attestation is not live and what the host can see", () => {
      expect(pages[0]).toContain("Hardware attestation (TPM / TEE)");
      expect(pages[0]).toContain("decrypts the input in memory");
      expect(pages[1]).toContain("hardware attestation is not live");
    });
  });
});
