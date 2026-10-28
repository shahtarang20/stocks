/**
 * Bridge for the cross-language acceptance test (tests/e2e/test_acceptance_ae3_ae4.py, AE-3).
 *
 * Runs the website's own browser libraries (customer-keys.ts, ccpp.ts) on an input file and
 * writes the result to an output file, so a Python end-to-end test can drive "the browser"
 * against the real API. Does nothing unless CCPP_BRIDGE_INPUT is set, so `npm test` skips it.
 *
 * Steps:
 * - "prepare": make the customer key (browser-only) and build the private-job submission exactly
 *   as the job form does: input {}, encrypted_input, guardian-sealed dk_shares, result_public_key.
 * - "open": decrypt the job output with the customer key and verify the receipt in-browser.
 * - "cross_compat": open Python-made envelopes and make envelopes / Shamir shares for Python to open
 *   (apps/api/tests/test_web_cross_compatibility.py, D4).
 */
import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  bytesToHex,
  decryptData,
  decryptJobOutput,
  encryptData,
  hexToBytes,
  openSealed,
  preparePrivateJob,
  seal,
  splitSecret,
  verifyReceiptLocal,
  type DataEnvelope,
  type SealedEnvelope,
} from "./ccpp";
import { getOrGenerateCustomerKey, importCustomerKey } from "./customer-keys";

const inputPath = process.env.CCPP_BRIDGE_INPUT;
const outputPath = process.env.CCPP_BRIDGE_OUTPUT;

describe.skipIf(!inputPath)("browser bridge (driven by the Python acceptance test)", () => {
  it("runs the requested browser step", async () => {
    const req = JSON.parse(readFileSync(inputPath!, "utf8"));
    let out: Record<string, unknown>;
    if (req.step === "prepare") {
      const key = await getOrGenerateCustomerKey();
      const prep = preparePrivateJob(req.input, req.guardians, req.threshold, key.publicKeyHex);
      out = {
        submission: {
          workload_type: req.workload_type,
          model: req.model,
          isolation_tier: req.isolation_tier,
          input: prep.input,
          encrypted_input: prep.encrypted_input,
          dk_shares: prep.dk_shares,
          data_policy: prep.data_policy,
        },
        // Stands in for IndexedDB between the two steps; the Python test asserts it is never sent.
        browser_storage: { privateKeyHex: key.privateKeyHex, publicKeyHex: key.publicKeyHex },
      };
    } else if (req.step === "open") {
      const key = await importCustomerKey(req.browser_storage.privateKeyHex);
      const output = decryptJobOutput(req.encrypted_output as SealedEnvelope, key.privateKeyHex, req.job_id);
      const receipt = verifyReceiptLocal(req.receipt, req.verifier_public_key, req.expected_image_hash);
      out = { output, receipt_verified: receipt.verified, receipt_checks: receipt.checks };
    } else if (req.step === "cross_compat") {
      const text = (b: Uint8Array) => new TextDecoder().decode(b);
      const bytes = (t: string) => new TextEncoder().encode(t);
      const opened = text(openSealed(req.browser_priv, req.py_sealed_envelope, hexToBytes(req.py_sealed_aad_hex)));
      const data = text(decryptData(hexToBytes(req.py_data_key_hex), req.py_data_envelope as DataEnvelope, hexToBytes(req.py_data_aad_hex)));
      if (opened !== req.expected_py_sealed_msg || data !== req.expected_py_data_msg) {
        throw new Error("Python envelopes did not open in the browser code");
      }
      const sealedAad = bytes("ccpp-v1|cross-lang-test|browser-sealed");
      const dataAad = bytes("ccpp-v1|cross-lang-test|browser-data");
      const dataKey = new Uint8Array(32).fill(0x77);
      out = {
        browserSealedEnv: seal(req.py_pub, bytes("Hello from Browser TypeScript!"), sealedAad),
        browserSealedAadHex: bytesToHex(sealedAad),
        browserDataEnv: encryptData(dataKey, bytes("Data from Browser TypeScript!"), dataAad),
        browserDataAadHex: bytesToHex(dataAad),
        browserDataKeyHex: bytesToHex(dataKey),
        serializableShares: splitSecret(new Uint8Array(32).fill(0x99), 3, 2).map((s) => ({ x: s.x, shareHex: bytesToHex(s.share) })),
      };
    } else {
      throw new Error(`unknown step ${req.step}`);
    }
    writeFileSync(outputPath!, JSON.stringify(out));
    expect(out).toBeTruthy();
  });
});
