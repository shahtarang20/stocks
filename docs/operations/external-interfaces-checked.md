# External interfaces checked against vendor documentation

Checked 2026-11-03 (official documentation only; nothing downloaded or executed). "Fixed" means the code
or tests were changed to match the documentation.

| Interface | Used in | Doc | Result |
|---|---|---|---|
| AWS KMS `Sign` (`MessageType=DIGEST`, `ECDSA_SHA_256`, DER signature, 32-byte digest) | `apps/api/app/services/withdrawal_signer.py` | https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html | matches |
| AWS KMS `GetPublicKey` (DER SubjectPublicKeyInfo, `ECC_SECG_P256K1`) | same | https://docs.aws.amazon.com/kms/latest/APIReference/API_GetPublicKey.html | matches |
| eth-account: hash unsigned legacy tx / serialize with external (v,r,s) | same | https://eth-account.readthedocs.io/en/stable/eth_account.html | unverifiable as public API: only private `eth_account._utils.legacy_transactions` helpers exist (signatures confirmed in installed 0.14.0 source); TODO kept, pin the version |
| Linux configfs-tsm (`inblob`, `outblob`, `provider`, `generation`, `auxblob`) | `provider-agent/computecoin_agent/tee.py` `tsm_report` | https://www.kernel.org/doc/Documentation/ABI/testing/configfs-tsm | matches (mkdir entry creation is not spelled out on the ABI page) |
| NVIDIA attestation CLI (`--gpu-index`, `--nonce`, JSON `{request,response,certificate_chain}`) | `tee.py` `NvidiaGpuCollector` | https://docs.nvidia.com/attestation/nv-attestation-sdk-cpp/latest/overview.html | unverifiable: documented `nvattest attest --device gpu --verifier local` yields an EAR, not raw SPDM messages; command must be an operator wrapper; TODO kept |
| AMD SEV-SNP report offsets (REPORT_DATA 0x50, REPORTED_TCB 0x180, CHIP_ID 0x1A0, size 0x4A0) | `tee.py`, `hardware_attestation/sev_snp.py` | AMD pub. 56860 (via github.com/google/go-sev-guest `abi/abi.go`) | matches (AMD PDF itself could not be fetched) |
| AMD KDS VCEK / cert_chain URLs, Milan and Genoa TCB byte layout | `tee.py` `SevSnpCollector` | AMD pub. 57230 (via go-sev-guest `kds/kds.go`) | matches for Milan/Genoa |
| AMD KDS for Turin (fmcSPL, different TCB layout, HWID length) | same | same | fixed: Turin is now refused (use cached certs) because layout differs and HWID length was not confirmed; test added |
| AMD KDS CRL format | `hardware_attestation/sev_snp.py` | AMD pub. 57230 (not readable) | unverifiable; marker left |
| Intel PCS v4 TDX `tcb?fmspc=`, `qe/identity`, header `TCB-Info-Issuer-Chain` | `tee.py` `TdxCollector` | https://cc-enabling.trustedservices.intel.com/intel-sgx-tdx-pccs/03/api_specification_for_pccs/ | matches; header encoding is not documented for TCB info (code unquotes, harmless if plain) |
| Intel PCS PCK CRL | `hardware_attestation/tdx.py` | same page: only `/sgx/certification/v{3,4}/pckcrl` exists, no TDX path | matches the documented SGX path only; not changed |
| TDX v4 quote header (version 4, key type 2) and TD report body (REPORTDATA at body+520, body 584 bytes) | `tee.py`, `hardware_attestation/tdx.py` | Intel TDX DCAP Quoting Library API PDF (text not readable by the fetch tool) | unverifiable here; consistent with my knowledge of the spec, note left in code |
| TPM PCR event order (PCR 4/7/11) | `os/sealed/scripts/predict_pcrs.py` | TCG PC Client PFP, systemd TPM2_PCR_MEASUREMENTS | not checked; its marker states the boot test is the check |
| mkosi options, MKOSI_VERSION, pip-tools, OVMF/QEMU TPM flags | `os/sealed/*`, `.github/workflows/sealed-image.yml`, `tests/e2e` | out of scope | not checked; markers left |
| `publish_image.py` admin auth (Bearer header) | `os/sealed/scripts/publish_image.py` | `apps/api/app/deps.py` (this repository) | matches the source: an explicit Bearer header is accepted before the cookie; not tried against a live API |
