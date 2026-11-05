# ComputeCoin Proof-of-Privacy Cryptographic Specification (ccpp-v1)

- **Version Identifier:** `ccpp-v1`
- **Specification Status:** Published & Locked (Locked Decision L5: One crypto spec across Python ×3 and TypeScript)
- **Companion Documents:** [ADR-0005](../architecture/ADR-0005-proof-of-privacy.md), [proof-of-privacy-completion.md](../architecture/proof-of-privacy-completion.md)
- **Test Vectors:** [ccpp-v1-vectors.json](./ccpp-v1-vectors.json)

---

## 1. Scope & Goals

`ccpp-v1` defines the unified cryptographic envelope formats, key agreement, symmetric ciphers, signatures, secret sharing, and output commitments for Computecoin Proof-of-Privacy.

Every platform component—Customer SDK, Web Frontend, Backend API, and Provider Agent—implements and verifies these exact algorithms and parameters.

---

## 2. Cryptographic Primitives

| Purpose | Algorithm / Standard | Parameters / Curve |
|---|---|---|
| **Asymmetric Sealing** | `X25519-HKDF-SHA256-ChaCha20Poly1305` | Curve25519 (RFC 7748), HKDF (RFC 5869), ChaCha20-Poly1305 (RFC 8439) |
| **Symmetric Data Encryption** | `ChaCha20Poly1305` | 32-byte Data Key, 12-byte random/deterministic nonce, 16-byte Poly1305 MAC |
| **Digital Signatures** | `Ed25519` | PureEd25519 (RFC 8032), SHA-512, 32-byte private seed, 32-byte public key |
| **Secret Sharing** | Shamir over $\text{GF}(2^8)$ | Field polynomial $p(x) = x^8 + x^4 + x^3 + x^2 + 1$ (`0x11D`), Generator $\alpha = 2$ |
| **Output Commitment** | HKDF-SHA256 + HMAC-SHA256 | Key info: `ccpp-v1\|output-commit`, digest: HMAC-SHA256 over Canonical JSON |
| **Canonical JSON** | Python `json.dumps(sort_keys=True, separators=(",", ":"))` (ensure_ascii) | Keys sorted, no insignificant whitespace, non-ASCII escaped as `\uXXXX`. **Not** RFC 8785: numbers are written the way Python writes them (`1.0` stays `1.0`, JavaScript writes `1`), so anything that is hashed or signed across languages must contain only strings, integers, booleans, null, lists and objects. Verdicts, wipe receipts, checkpoints and log payloads follow this rule; output commitments are computed only by the host (Python). |

---

## 3. Wire Formats & Envelopes

All binary values (keys, nonces, ciphertexts, signatures) are encoded as lowercase hex strings.

### 3.1 Asymmetric Sealed Envelope (`X25519-HKDF-SHA256-ChaCha20Poly1305`)

Used when sealing:
1. Customer Data Key shares to Key Guardians (`share_aad`).
2. Guardian shares re-sealed to provider machine ephemeral key (`release_aad`).
3. Execution outputs sealed to customer public key (`output_aad`).

```json
{
  "v": "ccpp-v1",
  "alg": "X25519-HKDF-SHA256-ChaCha20Poly1305",
  "epk": "<hex 32-byte ephemeral X25519 public key>",
  "nonce": "<hex 12-byte initialization vector>",
  "ct": "<hex ciphertext with appended 16-byte Poly1305 tag>"
}
```

#### Sealing Algorithm:
1. Generate ephemeral X25519 keypair $(sk_{\text{eph}}, pk_{\text{eph}})$.
2. Compute shared Diffie-Hellman secret:
   $$Z = \text{X25519}(sk_{\text{eph}}, pk_{\text{recipient}})$$
3. Derive 32-byte sealing key via HKDF-SHA256:
   $$\text{salt} = \text{None (32 zero bytes)}$$
   $$\text{info} = \text{"ccpp-v1\|"} \parallel pk_{\text{eph}} \parallel pk_{\text{recipient}}$$
   $$K_{\text{seal}} = \text{HKDF-SHA256}(Z, \text{length}=32, \text{info}=\text{info})$$
4. Encrypt plaintext under $K_{\text{seal}}$, 12-byte random nonce, and bound Associated Data (AAD):
   $$C = \text{ChaCha20Poly1305}(K_{\text{seal}}, \text{nonce}, \text{plaintext}, \text{AAD})$$

#### Opening Algorithm:
1. Validate envelope fields: `v == "ccpp-v1"` and `alg == "X25519-HKDF-SHA256-ChaCha20Poly1305"`.
2. Compute shared Diffie-Hellman secret:
   $$Z = \text{X25519}(sk_{\text{recipient}}, pk_{\text{eph}})$$
3. Derive $K_{\text{seal}}$ with the same info string: $\text{"ccpp-v1\|"} \parallel pk_{\text{eph}} \parallel pk_{\text{recipient}}$.
4. Decrypt and verify Poly1305 authentication tag with AAD:
   $$\text{Plaintext} = \text{ChaCha20Poly1305-Decrypt}(K_{\text{seal}}, \text{nonce}, C, \text{AAD})$$

---

### 3.2 Symmetric Data Envelope (`ChaCha20Poly1305`)

Used for encrypting bulk customer inputs with a fresh 32-byte Data Key:

```json
{
  "v": "ccpp-v1",
  "alg": "ChaCha20Poly1305",
  "nonce": "<hex 12-byte IV>",
  "ct": "<hex ciphertext with appended 16-byte Poly1305 tag>"
}
```

---

## 4. Associated Authenticated Data (AAD)

Associated Authenticated Data cryptographically binds ciphertexts to their exact context and prevents replay or cross-job cross-guardian substitution attacks:

| Context | AAD String (UTF-8 bytes) |
|---|---|
| Job Input Data | `ccpp-v1\|job-input` |
| Job Output Result | `ccpp-v1\|job-output\|<job_id>` |
| Customer-to-Guardian Share | `ccpp-v1\|guardian-share\|<guardian_name>` |
| Guardian-to-Host Share Release | `ccpp-v1\|host-release\|<job_id>\|<guardian_name>` |

---

## 5. Shamir Secret Sharing over $\text{GF}(2^8)$

To ensure no single platform entity holds the customer Data Key, the 32-byte Data Key is split into $n$ shares with threshold $k$ (default 2-of-3).

- **Field:** Galois Field $\text{GF}(2^8)$ defined by the primitive polynomial:
  $$p(x) = x^8 + x^4 + x^3 + x^2 + 1 \quad (\text{hex: } 0x11D, \text{ dec: } 285)$$
- **Generator:** $\alpha = 2$ generates the multiplicative subgroup of order 255.
- **Byte-by-Byte Polynomial:**
  For each byte index $i \in \{0, \dots, 31\}$ of the secret $S$:
  $$P_i(x) = S_i \oplus c_{i,1} x \oplus c_{i,2} x^2 \oplus \dots \oplus c_{i,k-1} x^{k-1}$$
  where $c_{i,m} \in \text{GF}(2^8)$ are randomly chosen coefficients.
- **Shares:**
  For $x \in \{1, \dots, n\}$, share $j$ has coordinate $x=j$ and 32-byte value:
  $$Y_j = (P_0(j), P_1(j), \dots, P_{31}(j))$$
- **Reconstruction:**
  Given any $k$ distinct shares $\{(x_j, Y_j)\}_{j=1}^k$:
  Lagrange basis polynomials evaluated at $x = 0$:
  $$\lambda_j = \prod_{\substack{m=1 \\ m \ne j}}^k \frac{x_m}{x_j \oplus x_m}$$
  The original secret byte $S_i$ is recovered by:
  $$S_i = \bigoplus_{j=1}^k \lambda_j \cdot Y_{j,i}$$

---

## 6. Output Commitment (Locked Decision L4)

Because the platform cannot inspect private outputs, integrity verification between primary and shadow hosts uses an HMAC-based output commitment:

1. Derive a 32-byte commitment key from the job Data Key (DK):
   $$K_{\text{commit}} = \text{HKDF-SHA256}(\text{IKM}=\text{DK}, \text{salt}=\text{None}, \text{info}=\text{"ccpp-v1\|output-commit"}, \text{length}=32)$$
2. Compute the hex HMAC-SHA256 over canonical JSON of the result:
   $$\text{Commitment} = \text{HMAC-SHA256}(K_{\text{commit}}, \text{CanonicalJSON}(\text{output}))$$
3. Both primary and shadow hosts compute this commitment in-RAM. The platform compares commitments without ever seeing the plaintext result or being able to brute-force low-entropy answers.

---

## 6a. Attestation Verdict, format v2 (one job, one key set)

A verdict is signed with the platform verifier's Ed25519 key over the canonical JSON of exactly
these fields (all strings, or `null` for `job_id`):

`verdict_id, job_id, machine_id, image_hash, tier_reached, ephemeral_public_key,
receipt_signing_public_key, nonce, issued_at, expires_at, verifier_public_key`

The same list is used by the API (`attestation_verifier.py`), the external guardian
(`apps/guardian/crypto.py`) and `ccverify` (`sdk/python/computecoin/ccverify.py`). A verdict missing
any field, including a v1 verdict without `job_id`, fails signature verification.

| Kind | `job_id` | Lifetime | Use |
|---|---|---|---|
| Machine-level | `null` | 30 min | Makes the machine eligible to claim private work. Releases nothing. |
| Job-bound | the job's id | 10 min | The only kind that releases a job's key shares, once. |

**Key release for a private job:**

1. `POST /v1/agents/jobs/claim` (with `accept_private: true`) returns the ciphertext input and
   `key_attestation_nonce`, a challenge bound to this job. It returns no key shares.
2. The host generates NEW X25519 and Ed25519 keys in RAM and attests with that nonce
   (`POST /v1/agents/attestation/verify`). The platform issues the verdict with `job_id` set,
   only while the job is RUNNING on that machine.
3. `POST /v1/agents/jobs/{job_id}/keys` with `{"verdict_id": ...}`. The platform checks the
   verdict is signed, current, for this machine and this job, and that the job's keys were not
   released before; it appends one `KEY_RELEASE` log entry per guardian, **commits**, and only
   then releases (local guardians re-seal; external guardians check the signed checkpoint,
   inclusion and consistency proofs and require `verdict.job_id == job_id`).
4. The host runs the job and wipes both keys when it ends, whatever the outcome.

A receipt verifies only if its verdict's `job_id` equals the receipt's `job_id`
(`checks.verdict_for_this_job`).

---

## 6b. Log Witness Cosignatures (`ccpp-witness-v1`)

To prevent split-view attacks where the platform presents divergent logs to different participants, independent log witnesses cosign log checkpoints after verifying consistency proofs (RFC 9162).

### Witness Statement Format

A witness statement is signed by an independent witness using its Ed25519 private key over the canonical JSON of:

```json
{
  "v": "ccpp-witness-v1",
  "tree_size": 12,
  "root_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "log_public_key": "1111111111111111111111111111111111111111111111111111111111111111",
  "witness": "witness-north"
}
```

Fields:
- `v`: Fixed version string `"ccpp-witness-v1"`.
- `tree_size`: Non-negative integer representing the Merkle tree size.
- `root_hash`: Lowercase 32-byte hex string of the RFC 6962 / RFC 9162 root hash.
- `log_public_key`: Lowercase 32-byte hex string of the platform's transparency log Ed25519 public key.
- `witness`: Name identifier of the cosigning witness service.

### Who asks for cosignatures

A witness never cosigns a tree smaller than one it has already cosigned (that would be a rollback),
and refuses a different root at the same size (split view). So a checkpoint can only be cosigned
while it is the newest one, and an older tree is shown to be witnessed through a LATER cosigned
checkpoint plus a consistency proof, exactly like anchoring (6c):

- **Key release**: the platform asks for cosignatures on the current checkpoint and sends them to
  external guardians in `witness_cosignatures` (guardians count distinct pinned witness keys).
- **Worker**: every `PRIVACY_WITNESS_INTERVAL_SECONDS` the latest checkpoint is cosigned and the
  result is stored on its last leaf. No network call is made while a receipt is read.

### Receipt `witness` section (added at read time)

The smallest witnessed checkpoint whose `tree_size` is at least the receipt's tree size:

```json
{
  "checkpoint": {"v": "ccpp-log-v1", "tree_size": 15, "root_hash": "<hex>", "timestamp": "<iso>",
                 "log_public_key": "<hex>", "signature": "<hex>"},
  "consistency_proof": ["<hex 32-byte hash>", "..."],
  "cosignatures": [{"witness": "witness-north", "signature": "<hex 64-byte Ed25519 signature>"}]
}
```

`null` until the worker has witnessed a checkpoint at or above the receipt's size.

### Verification (`ccverify --witness-keys <file|json|name=key,...> --witness-threshold k [--log-key <hex>]`)

1. The receipt's tree (its size and `merkle_root`) is consistent with `checkpoint` (same rule as 6c
   steps 1-2).
2. If `--log-key` is given, `checkpoint.log_public_key` must equal it.
3. Each cosignature is checked against the statement above for `checkpoint.tree_size`,
   `checkpoint.root_hash` and `checkpoint.log_public_key`, with the PINNED key of the named witness.
4. At least `k` DISTINCT witness keys must verify (one key listed under two names counts once).

Guardians apply rule 3-4 to the checkpoint in a release request (`GUARDIAN_WITNESS_KEYS`,
`GUARDIAN_WITNESS_THRESHOLD`).

---

## 6c. On-Chain Anchoring (`anchor` section)

Transparency log roots are periodically committed to Ethereum / EVM smart contracts (`ComputeStakeVault.commitMerkleRoot(bytes32,uint256)`).

Receipts include an `anchor` section proving forward consistency from the receipt's tree size to the anchored tree size:

```json
{
  "tree_size": 15,
  "merkle_root": "<hex 32-byte lowercase anchored root>",
  "tx_hash": "0x<hex 64-character transaction hash>",
  "block_number": 123456,
  "consistency_proof": [
    "<hex 32-byte audit hash>",
    "..."
  ]
}
```

### Verification Algorithm (`ccverify --rpc <url> --vault <addr>`):
1. If `receipt.tree_size == anchor.tree_size`: assert `receipt.merkle_root == anchor.merkle_root` and `len(consistency_proof) == 0`.
2. If `0 < receipt.tree_size < anchor.tree_size`: verify RFC 9162 consistency proof from `receipt.tree_size` to `anchor.tree_size`.
3. Query the vault contract via `eth_call` for `merkleRoots(bytes32)` (selector `0xfe5a5377`); a
   non-zero result (the block timestamp it was committed at) means the anchored root is on chain.

Without an `anchor` section and with `--rpc`, `ccverify` checks the receipt's own root on chain and
reports "not yet anchored" if it is absent. The anchor worker only sends `commitMerkleRoot` after
checking the RPC's chain id, that no earlier anchor transaction is still pending, and that
`eth_estimateGas` succeeds (a call known to revert is never sent).

---

## 6d. TPM 2.0 hardware evidence and AK enrolment (`hardware_flavor: "tpm2"`)

Used only when `PRIVACY_HARDWARE_ATTESTATION` is on (pinned roots in `PRIVACY_HARDWARE_ROOT_CERTS`,
JSON `{"tpm2": [PEM, ...], "sev_snp": [...]}`, each a valid self-signed CA).

**Enrolment (once per AK).** An EK is a restricted decrypt key and never signs, so the AK is bound
by credential activation:

1. `POST /v1/agents/tpm/enrolments` (machine-signed): `{"ek_cert": PEM, "ek_intermediates": [PEM],
   "ak_public": hex TPMT_PUBLIC}`. The EK chain must reach a pinned TPM vendor root; the AK must be
   a restricted, fixedTPM, fixedParent, sensitiveDataOrigin signing key with SHA-256 nameAlg; the
   EK must not be active for another machine. Response: `enrolment_id`, `ak_name`
   (`0x000B || SHA-256(TPMT_PUBLIC)`), `credential_blob` and `encrypted_secret` (TPM2_MakeCredential
   for an RSA-2048 EK: OAEP-SHA256 seed with label `IDENTITY\0`, KDFa `STORAGE` / `INTEGRITY`,
   AES-128-CFB, HMAC-SHA256), `expires_at` (5 minutes).
2. The host runs TPM2_ActivateCredential with its EK and that AK loaded, and posts
   `{"secret": hex}` to `/v1/agents/tpm/enrolments/{id}/activate`. At most 3 attempts; the AK
   becomes the machine's active AK and any previous one is `replaced`. The secret is never stored
   (only its SHA-256).

**Evidence** in `/v1/agents/attestation/verify` (`hardware_evidence`):

```json
{"quote": {"attest": "<hex TPMS_ATTEST>", "signature": "<hex>", "ak_public": "<hex TPMT_PUBLIC>"},
 "pcrs": {"0": "<hex SHA-256 PCR>", "...": "..."},
 "ek_cert": "<PEM>", "ek_intermediates": ["<PEM>"]}
```

The verifier: EK chain to a pinned root; `ak_public` names the machine's ACTIVE enrolled AK; the AK
signature covers `attest`; from the signed `attest` only: magic `TPM_GENERATED`, type quote,
`extraData == SHA-256(nonce)`, SHA-256 PCR selection, and `pcrDigest == SHA-256(selected PCR values
in index order)`. Only the selected PCRs are returned as measurements, and those (not the host's
`pcr_measurements`) are matched against the trusted images.

---

## 6e. Guardian key pinning (`ccpp-guardians-v1`)

Customers seal Data Key shares to the guardian keys from `GET /v1/privacy/verifier-key`. A platform
that served keys it controls could open every Data Key, so the guardian SET is pinned by a
fingerprint published somewhere the platform cannot quietly change (documentation and release
notes, a DNS TXT record, the guardian operators' own sites):

```
fingerprint = SHA-256( canonical_json({"v": "ccpp-guardians-v1", "threshold": k,
                                       "guardians": {name: lowercase X25519 hex, ...}}) )
```

Vector: guardians `{"computecoin": "aa"*32, "partner": "bb"*32}`, threshold 2 →
`90047f24b306726940afab8f77365e85bf3b89f88bd916a9c5e60da4890a31f1`.

- The API returns `guardian_fingerprint` for operators to publish; clients RECOMPUTE it from the
  keys served and never trust that field.
- Python SDK: `ComputeCoinClient(guardian_pin=...)` or `COMPUTECOIN_GUARDIAN_PIN`; a mismatch raises
  `GuardianPinError` before anything is encrypted or sent. Without a pin it warns
  (`UnpinnedGuardianWarning`); `require_guardian_pin=True` refuses instead.
- Website: build-time `NEXT_PUBLIC_PRIVACY_GUARDIAN_PIN`; a mismatch refuses the submission.
  Without a pin the form says so; `NEXT_PUBLIC_PRIVACY_REQUIRE_GUARDIAN_PIN=1` refuses instead.
- Changing the guardian set means publishing a new fingerprint; clients pinned to the old one refuse
  until they are updated (intended).

---

## 6f. Confidential tier evidence: CPU TEE + NVIDIA GPU (`hardware_flavor: "confidential"`)

Each tier accepts one evidence type: Sealed / Sealed Pro only `tpm2` (6d); Confidential only

```json
{"cpu_flavor": "tdx" | "sev_snp", "cpu": <TDX or SEV-SNP evidence>, "gpu": <NVIDIA evidence>}
```

- **GPU** (SPDM 1.1): `{"request", "response", "certificate_chain"}`. The GET_MEASUREMENTS request
  asks for a signature and carries nonce `SHA-256(challenge)`; the device key (ECDSA P-384, chain
  to a pinned NVIDIA root) signs request || response-without-signature.
- **CPU** report data must be `SHA-512(challenge + "|gpu:" + hex(SHA-256(lowercase gpu.response hex)))`:
  the CPU TEE commits to the GPU's signed report, so CPU and GPU evidence from different machines
  cannot be combined. TDX: DCAP quote v4 + Intel-signed TCB Info / QE Identity collateral (all
  chained to a pinned Intel root; TCB level and QE must be UpToDate). SEV-SNP: 6d-style report
  checks (VCEK -> ASK -> pinned ARK).
- Measurements for the trusted-image match: `cpu.<mrtd|rtmr0..3|measurement>` and `gpu.<index>`.
- **Collateral (P2, trusted by signature to a pinned root, never by origin).**
  - CPU TDX `collateral.crls`, SEV-SNP `crls`, GPU `crls` / `ocsp_responses`: PEM, hex DER or base64
    DER. Every link of every certificate path needs a CRL signed by its issuer with
    `thisUpdate <= now < nextUpdate` (SEV-SNP: the VCEK link; upper links when present), or for the
    GPU a current OCSP response signed by the issuer or its delegated responder. A listed serial
    gives `<flavor>_certificate_revoked`; no usable statement gives `<flavor>_revocation_unknown`.
    Forged, expired, delta or wrong-issuer CRLs are ignored, never trusted.
  - TDX module identity: quote MRSIGNERSEAM / SEAMATTRIBUTES against the Intel-signed TCB Info
    (`tdxModule` for major 0, else `tdxModuleIdentities[TDX_<major>]`, masked attributes), ISVSVN
    (TEE_TCB_SVN[0]) level must be `UpToDate` (`tdx_bad_tdx_module`, `tdx_module_identity_unknown`).
  - GPU `rims`: NVIDIA signed SWID/XML RIMs (driver, VBIOS). XML signature verified, its certificate
    chain pinned and not revoked; every `Resource` index must equal one of its alternatives in the
    signed SPDM blocks (`nvidia_rim_missing`, `nvidia_invalid_rim_signature`, `nvidia_measurement_mismatch`).
- Not implemented / unverified: SPDM 1.2+ signing contexts. The vendor CRL, OCSP, TCB Info module
  identity and RIM formats are coded from documentation and memory and have not been checked against
  recorded real collateral (R7, R8); synthetic evidence only.
---

## 6h. Vault machine id (`machine_id_to_bytes32`)

`ComputeStakeVault` keys stake by a `bytes32` machine id. The platform and the provider derive it identically:

    machineId = keccak256( UTF8("computecoin-machine:" || machine_uuid_canonical_lowercase_hyphenated) )

The `computecoin-machine:` domain prefix keeps machine ids from colliding with job ids, which hash the bare
UUID string. The provider registers this value with `registerMachine(machineId, provider)`; the stake sync
(A1) reads `stakes(machineId)` at `latest - STAKE_SYNC_CONFIRMATIONS`. Reference: `machine_id_to_bytes32` in
`apps/api/app/services/settlement.py`. Test vector: left to the first run of the test suite (not computed here).

## 6g. Job-level receipt for private fan-out (`ccpp-fanout-v1`) and sealed stream chunks

Added for U18 (docs/architecture/U18-privacy-modes-design.md). Vectors: section `fanout_v1` of `ccpp-v1-vectors.json`,
produced by `scripts/gen_fanout_vectors.py` (UNVERIFIED until that script has been run and the implementations pass them).

### Part receipt hash

For each part job, take the part's privacy receipt and build the object with exactly the fields `job_id` (string),
`isolation_tier`, `image_hash`, `attestation_verdict`, `wipe_receipt` (object or `null`), `merkle_root`,
`inclusion_proofs`. `receipt_hash = hex(SHA-256(CanonicalJSON(object)))`.

### Job-level receipt

```json
{
  "v": "ccpp-fanout-v1",
  "parent_job_id": "<uuid>",
  "isolation_tier": "sealed | sealed_pro | confidential",
  "part_count": 3,
  "part_receipts": [ {"index": 0, "job_id": "<uuid>", "receipt_hash": "<hex>"}, ... ],
  "merkle_root": "<hex>",
  "verifier_public_key": "<hex Ed25519 public key>",
  "signature": "<hex Ed25519 signature>"
}
```

1. `part_receipts` is ordered by `index`, which must be exactly `0..part_count-1`.
2. Leaf `i` = `leaf_hash("FANOUT_PART_RECEIPT", {"index": i, "job_id": <part job id>, "receipt_hash": <hash>})`, using the
   transparency-log leaf rule of section 6a/log (`SHA-256(0x00 || event_type || ":" || canonical_json(payload))`).
3. `merkle_root` = RFC 6962 Merkle tree hash over the ordered leaves.
4. `signature` = Ed25519 over CanonicalJSON of `v, parent_job_id, isolation_tier, part_count, part_receipts, merkle_root,
   verifier_public_key` (the platform verifier key, the same key that signs attestation verdicts).

Verification (offline): check `v`; check indices; recompute every leaf and the root; check the signature against a
pinned verifier key; check part job ids are distinct. If the part receipts are available also check, for each part,
that its `receipt_hash` matches, its `job_id` and `isolation_tier` match, and it passes the single-job receipt check.
A group missing any part receipt has no job-level receipt. The receipt proves the platform committed to this ordered
list of part receipts; it does not prove anything about the hosts beyond what each part receipt proves.

When a job-level receipt is issued the platform appends one transparency-log entry, event type `FANOUT_RECEIPT`, payload
`{parent_job_id, isolation_tier, part_count, merkle_root, receipt_sha256, verifier_public_key}` where `receipt_sha256` =
`hex(SHA-256(CanonicalJSON(job receipt including signature)))`. One entry per parent job (a repeated merge finds it).

### Merge output

`merge = "ordered_ciphertext"`: per part `{index, job_id, encrypted_output, output_commitment, receipt_hash}`. The
`encrypted_output` opens with that part's result private key and AAD `ccpp-v1|job-output|<part job_id>`.

### Sealed stream chunks (private chat streaming)

Each chunk is a section 3.1 sealed envelope to the customer's result public key, plaintext = the UTF-8 text piece, AAD
`ccpp-v1|stream|<job_id>|<seq>|<final>` where `<seq>` is the decimal chunk number from 0 and `<final>` is `1` for the last
chunk and `0` otherwise. The receiver opens chunk `seq` only with its own expected `seq`, and treats a stream with no
`final = 1` chunk as truncated.

Padding (`PRIVACY_STREAM_PAD_BYTES`, default 256, `0` = off): with padding on, the plaintext is NOT the raw text but
`u32 big-endian length || UTF-8 text || zero bytes`, extended to a multiple of the block size, so chunk sizes do not leak
token lengths. The receiver reads the length and takes that many bytes. The relay can only check that the ciphertext
length minus the 16-byte tag is a non-zero multiple of the block size. Customer and platform must use the same value.

Reader rules (the Python SDK, TypeScript SDK and web client implement them): open chunk `seq` with the customer's result
private key and the AAD above; with padding on, read the u32 big-endian length `n` from the first 4 bytes, return exactly
the next `n` bytes, and REJECT the chunk if `n` exceeds the remaining payload or any byte after `4 + n` is non-zero. Decode
the text as strict UTF-8. Also reject a `job_id` that differs from the first chunk's, a `seq` that is not the next number
and any chunk after `final = 1`. The `job_id` is taken from the first event (`chat.completion.chunk.sealed`, fields `job_id`,
`seq`, `final`, `sealed`) because the customer cannot know it before submitting. A stream that ends without a `final = 1`
chunk is truncated and is an error, not shorter text. Test vector: `fanout_v1.expected.stream_chunk` (padded plaintext hex,
AAD, envelope) once `scripts/gen_fanout_vectors.py --write` has been run.

### Private Kit input (decrypted input of a private Kit part)

The plaintext the customer encrypts into the part's `encrypted_input` (section 3.2 data envelope, AAD `ccpp-v1|job-input`;
the Data Key is split and sealed to the guardians as in section 5) is one JSON object:

```json
{
  "inputs": { "<name>": { "b64": "<standard base64 of the file bytes>" } | { "text": "<UTF-8 text>" } },
  "params": { "<kit param>": "<value>" }
}
```

* `<name>` matches `[A-Za-z0-9._-]{1,128}`; the agent mounts it read-only at `/in/<name>` in the Kit container (no network).
* Exactly one of `b64` or `text` per input. `params` is the Kit's parameter object (it reaches the Kit as `/out/input.json`).
* A dict with neither `inputs` nor `params` is taken as `params` by the agent; clients should always send the full shape.
* Total decrypted size is capped by the agent (`MAX_PRIVATE_INPUT_BYTES`, 256 MiB); the envelope is one-shot.

Private fan-out submit (`POST /v1/jobs/v2/fanout`, `isolation_tier` one of `sealed | sealed_pro | confidential`): the body
carries `private_parts`, exactly one entry per part, each exactly `{"encrypted_input", "dk_shares", "result_public_key"}`.
Every part needs its OWN Data Key, share set and result keypair (the API refuses a reused key set, `key_set_reused_across_parts`,
and a wrong count, `private_parts_count_mismatch`). Each part's sealed outputs open with that part's result private key.
Helpers: Python `build_private_kit_input` / `ComputeCoinClient.submit_private_fanout`, TypeScript `buildPrivateKitInput` /
`client.submitPrivateFanout`.

### Sealed Kit output files (private Kit job)

Each declared output is sealed (section 3.1) to the part's result public key with AAD
`ccpp-v1|job-output-file|<job_id>|<output name>`; only that ciphertext (the envelope as canonical JSON) is uploaded through
the data plane. The job's `encrypted_output` (AAD `ccpp-v1|job-output|<job_id>`) lists per output `{name, kind, file_id,
ciphertext_sha256, ciphertext_size, plaintext_sha256, plaintext_size, aad}`.

---

## 7. Verification Vectors

All implementations must pass the verified deterministic vectors in `ccpp-v1-vectors.json`.

