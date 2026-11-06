# W12: Privacy tiers on Windows hosts: decision document

_Cross-platform plan W12.1 · Status: **default NO-GO, awaiting the owner** · Written 2026-11-06 · Research only: no production code was written for this note._

Owner decision (plan section 9): "Privacy tiers on Windows hosts go or no-go". The plan's recommendation is **default no-go; revisit with a Windows Server plus VM design**. This note
lays out what a go would take so the owner can see the price, and why the recommendation is unchanged.

**How to read vendor statements.** A statement marked **[read 2026-11-06]** was read on that date on the Microsoft Learn page named in section 8, and is quoted or paraphrased from it. Every
other vendor, hardware or Windows-API statement is marked `TODO(verify-docs)`: it is a recollection or an inference and must not be relied on (X11, L4).

## 1. Decision (default)

**NO-GO.** Windows hosts stay on the **container tier only** (X5). Concretely, and already enforced by other work packages:

* a Windows host never reports `sealed`, `sealed_pro`, `confidential`, `gvisor`, `microvm` or `vfio_gpu` (`computecoin_agent/capabilities.py`, `WINDOWS_FORBIDDEN_FLAGS`; the server drops them as well, test W1.3);
* BYOC (needs gVisor or microVM) is refused on Windows hosts, private Kit jobs are not placed there;
* the website and the host guide say so in plain words (`/share/windows`, `docs/operations/windows-host-guide.md`).

## 2. What the Linux tiers prove today, and what a Windows tier would have to prove

ADR-0005 tiers rest on three things the **host owner cannot fake**: (1) a measured, known-good software stack (Sealed OS image measured into a TPM, quote verified by the platform), (2) memory the owner cannot read
(SEV-SNP or TDX for Confidential, with NVIDIA confidential computing for the GPU), (3) keys released only to that verified stack (guardians, Proof-of-Privacy receipts).

On a Windows host the owner is a **local administrator of the operating system that runs the hypervisor**. The plan states (section 4.4) that the WSL2 VM's memory is readable from the Windows host. So
the container tier on Windows protects the owner from the job, but gives the customer nothing against the owner. A privacy tier on Windows therefore needs a design in which the secret-holding workload runs in a
VM whose memory and boot state the Windows administrator cannot read or forge. That is a different architecture from WSL2.

## 3. Options

| Option | Idea | Could it work for confidentiality? | Main cost |
|---|---|---|---|
| **A. Container tier only (default)** | Do nothing more. | n/a: no privacy promise is made. | none |
| **B. WSL2 with a vTPM or measured distro** | Keep the managed WSL2 distro, add measurements. | **No.** WSL2 is a utility VM owned by the Windows user; whether it exposes any TPM to the guest is `TODO(verify-docs)`; the owner controls the VM, its kernel (`.wslconfig` can set a custom kernel **[read 2026-11-06]**) and its disks. A quote from inside it proves nothing about what the owner can read. | n/a |
| **C. Windows Server + Hyper-V guarded fabric: shielded VMs, vTPM, TPM-attested host** | The job runs in a Generation 2 **shielded VM** on a **guarded host** whose health is attested by a Host Guardian Service. | **Partly, on paper.** Microsoft documents shielded VMs as protected from fabric administrators and untrusted host software, with a virtual TPM, BitLocker, Secure Boot and encrypted VM state **[read 2026-11-06]**. Open questions below. | Windows Server on the host; an HGS service run by ComputeCoin; a new attestation format; a much smaller host market |
| **D1. GPU into the VM with Discrete Device Assignment (DDA)** | Whole GPU dismounted from the host and assigned to one VM. | Isolation yes (hypervisor boundary); **confidentiality of GPU memory: no statement found.** Host: Windows Server 2016 or later; guest Windows Server 2016+ or Windows 10+ **[read 2026-11-06]**; Linux guests are not listed on that page. Microsoft notes vendors may block consumer cards under passthrough **[read 2026-11-06]**. | The owner loses that GPU for their own use; datacenter-class or vendor-approved cards |
| **D2. GPU partitioning (GPU-P)** | SR-IOV split of one GPU between VMs. | Hardware-partitioned (Microsoft calls the SR-IOV boundary hardware-backed) **[read 2026-11-06]**; again no GPU-memory encryption statement. Supported GPUs listed are datacenter and workstation-server cards (NVIDIA A2, A10, A16, A40, L2, L4, L40, L40S, RTX Pro 6000 Blackwell Server Edition, AMD Radeon PRO V710); Ubuntu 18.04/20.04/22.04 guests are listed; Windows Server 2025 for live migration **[read 2026-11-06]**. GeForce cards are **not** on the list. | Excludes the gaming PCs this product is aimed at |
| **E. Confidential GPU (NVIDIA CC) on a Windows host** | Hopper-class GPU in confidential mode plus a CPU TEE. | Unknown: whether a Windows host can use CC mode and who verifies the evidence is `TODO(verify-docs)`. The Linux Confidential tier needs SEV-SNP or TDX in the CPU and NVIDIA CC (plan section 1.2, ADR-0005). | Datacenter hardware only |
| **F. Host hardening as extra evidence (VBS, HVCI, Secure Boot, BitLocker)** | Report the host's code-integrity state. | Not confidentiality: it makes the host harder to tamper with by malware, **not** by its own administrator. Details of VBS and HVCI as documented are `TODO(verify-docs)`. | small |

### 3.1 Option C in detail: what is documented and what is not

Documented **[read 2026-11-06]** (Microsoft Learn, Guarded Fabric and Shielded VMs overview):

* A shielded VM is a Generation 2 VM with a virtual TPM, encrypted with BitLocker, that can only run on healthy and approved hosts in the fabric.
* The HGS provides attestation and key protection. **TPM-trusted attestation** approves hosts by TPM identity, Measured Boot sequence and code-integrity policy; the host sends its TPM endorsement key information, the TCG log and the
  code-integrity policy; attestation repeats when the host starts and every 8 hours **[read 2026-11-06]**. It needs TPM 2.0 and UEFI 2.3.1 with Secure Boot.
* Host key attestation is weaker (possession of a key) and admin-trusted attestation is deprecated since Windows Server 2019.
* Shielded VMs are "protected from inspection, tampering and theft from malicious fabric admins and host malware"; console connection, serial ports, debugger attach and some integration components are blocked.

Not documented in what was read, therefore **unproven for us**:

1. **Who is the fabric administrator?** In the Microsoft model the HGS operator is a trusted party separate from the host administrator. For ComputeCoin the HGS would have to be run by ComputeCoin, and every host owner would be a *fabric administrator of their own one-host fabric*. Whether a single-host, internet-attached guarded host with an off-site HGS is a supported or sensible shape is `TODO(verify-docs)`.
2. **Physical attacks.** Cold-boot, bus snooping, DMA and TPM-interposer attacks by the person holding the hardware: not addressed in the page. `TODO(verify-docs)`; this is the same limit that makes Sealed (TPM only, no memory encryption) weaker than Confidential on Linux, so a Windows shielded VM would be at most "Sealed-class", never "Confidential-class".
3. **GPU inside a shielded VM.** Whether DDA or GPU-P is supported for a shielded VM at all, and whether the host can read GPU memory, is `TODO(verify-docs)`. Without it the GPU work is exposed to the host regardless of the VM shield.
4. **Linux guests as shielded VMs.** Our Kits are Linux images (X12). Shielded Linux guests are `TODO(verify-docs)` (support list, vTPM measured boot of Linux, Secure Boot).
5. **Our verifier.** Proof-of-Privacy verifies a Sealed OS image quote with our own reference measurements (ADR-0005). An HGS health certificate is a *different* evidence format. Either ComputeCoin trusts a Microsoft-defined attestation (a trust shift) or builds a verifier for TCG logs and Windows PCR layouts.

### 3.2 TPM measured boot logs through Windows

* Documented **[read 2026-11-06]**: Measured Boot provides "a trusted (resistant to spoofing and tampering) log of all boot components", which software can send to a remote server for evaluation (TPM fundamentals page).
* `TODO(verify-docs)`: how an application reads the log and gets a signed quote on Windows (the TPM Base Services, a function such as `Tbsi_Get_TCG_Log`, and CNG/TPM key attestation calls), which PCRs carry what on Windows, and whether an unprivileged service can do it.
* `TODO(verify-docs)`: how stable the PCR values are across Windows Update and firmware updates. On Linux the Sealed OS is **our** image with pinned measurements. On Windows the measured stack is Microsoft's and changes monthly; a reference-measurement database per Windows build would be a permanent operations burden and a security-critical one (a wrong entry approves a bad host).

## 4. What each path could and could not prove

| Claim a customer would want | Container tier (today's Windows) | Option C (shielded VM, if all open questions resolve) | Linux Sealed / Confidential (for comparison) |
|---|---|---|---|
| "The job could not touch the owner's files" | yes, if X6 hardening holds | yes (hypervisor) | yes |
| "The owner cannot read my data in memory" | **no** | partly: protected from the administrator by software controls; **not** from hardware attacks (`TODO(verify-docs)`) | Sealed: partly; Confidential: yes (CPU TEE) |
| "The owner cannot read my data on the GPU" | no | **unknown**, probably no | Confidential with NVIDIA CC: yes |
| "The host ran the software it claims" | no | yes if HGS attestation is trusted (measured boot + code integrity) | yes (our image, our measurements) |
| "I can verify this myself, offline" | n/a | **no**: the evidence is HGS-issued; our offline verifier (`computecoin.ccverify`) would need a new format | yes (Privacy Receipt) |

## 5. Hardware tests that would be needed before any go

All on real machines, recorded (R8). None has been run.

1. A Windows Server host with TPM 2.0 and Secure Boot, HGS in TPM-trusted mode, one shielded Linux VM: does it boot, attest, and survive a Windows Update and a firmware update without losing attestation?
2. The same host as a **single-owner** system: can the administrator (a) attach a debugger, (b) dump VM memory, (c) read the vTPM state, (d) replace the VM disk? Attempt each; record what is blocked.
3. GPU: DDA and GPU-P into the shielded VM with a card that is both supported by Microsoft and by the vendor; read GPU memory from the host side where possible.
4. A cheap hardware attack on a throw-away machine (DMA or cold boot) to see whether "protected from the administrator" is all the claim is worth.
5. The Windows quote path: obtain the TCG log and a TPM quote from a service, parse the log, replay PCRs; measure PCR drift over two Windows Update cycles.
6. Compare with the existing Linux Sealed path on the same hardware (swtpm tests exist for Linux only: `tests/e2e/test_tpm_swtpm_e2e.py`).

## 6. Risks

| Risk | Impact |
|---|---|
| False assurance: a "private" badge on a host whose administrator can still read memory or the GPU | breaks the product promise (ADR-0005); the worst outcome |
| Reference-measurement upkeep for Windows builds | a permanent security-critical data set |
| A new component to operate (HGS) | availability and key-management risk; it becomes a single point of trust for every Windows private host |
| Host market shrinks to Windows Server owners with TPM 2.0 and approved GPUs | little supply for the effort |
| Licensing of Windows Server and of any Windows guest on third-party hardware | legal review needed (see U14: same question) |
| Vendor documentation changes (all `TODO(verify-docs)` items above) | the design may be wrong in a way nobody has checked |

## 7. Recommendation

1. **Record NO-GO** (default). Windows hosts are container tier only, forever or until the owner reopens this.
2. Reopen only if **all** of: (a) there is real customer demand for private jobs on supply that Linux hosts cannot meet; (b) a Windows Server test machine with TPM 2.0 and a Microsoft- and vendor-supported GPU is available; (c) tests 1 to 3 in section 5 are run and show the host administrator is blocked and the GPU is covered; (d) a verifier design for the evidence is approved (new ADR) and independently reviewed (W11).
3. Until then keep the website and docs wording as it is: the Windows tier is **container only**.

## 8. Sources read

| Page | URL | Read |
|---|---|---|
| Guarded fabric and shielded VMs overview | <https://learn.microsoft.com/en-us/windows-server/security/guarded-fabric-shielded-vm/guarded-fabric-and-shielded-vms> | 2026-11-06 |
| Partition and share GPUs with VMs on Hyper-V | <https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/gpu-partitioning> | 2026-11-06 |
| Deploy graphics devices by using Discrete Device Assignment | <https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/deploy/deploying-graphics-devices-using-dda> | 2026-11-06 |
| TPM fundamentals (Measured Boot) | <https://learn.microsoft.com/en-us/windows/security/hardware-security/tpm/tpm-fundamentals> | 2026-11-06 |
| Advanced settings configuration in WSL | <https://learn.microsoft.com/en-us/windows/wsl/wsl-config> | 2026-11-06 |

## Decision record

| Field | Value |
|---|---|
| Decision | NO-GO (default) |
| Decided by / date | pending the owner |
| Vendor docs checked | the five pages above, 2026-11-06; everything else is `TODO(verify-docs)` |
| Revisit when | the conditions in section 7.2 hold |
