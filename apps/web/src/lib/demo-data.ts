/**
 * SYNTHETIC DEMO DATA — NOT LIVE INFRASTRUCTURE.
 *
 * Every value in this file is hand-authored placeholder data used to build out
 * the UI shell during Phase 1. It does NOT represent real GPU availability,
 * real benchmarks, real utilization, real pricing, or any token metric.
 * All consuming views MUST render a visible "DEMO MODE" label. Never present
 * any of this as live availability or real performance.
 */

export type VerificationStatus = "verified" | "attested" | "unverified";

export interface DemoGpuListing {
  id: string;
  model: string;
  vramGb: number;
  pricePerHour: number; // synthetic indicative rate, USD/hr
  region: string;
  provider: string;
  verification: VerificationStatus;
  interconnect: string;
}

export const DEMO_REGIONS = [
  "us-east",
  "us-west",
  "eu-central",
  "eu-west",
  "ap-south",
  "ap-northeast",
] as const;

export const DEMO_GPU_MODELS = [
  "NVIDIA H100 80GB",
  "NVIDIA A100 80GB",
  "NVIDIA A100 40GB",
  "NVIDIA L40S",
  "NVIDIA RTX 4090",
  "NVIDIA RTX 6000 Ada",
  "AMD MI300X",
] as const;

// Deterministic synthetic rows so the table never implies "live" churn.
export const DEMO_GPU_LISTINGS: DemoGpuListing[] = [
  { id: "demo-001", model: "NVIDIA H100 80GB", vramGb: 80, pricePerHour: 2.49, region: "us-east", provider: "synthetic-provider-a", verification: "verified", interconnect: "NVLink / 3.2Tbps IB" },
  { id: "demo-002", model: "NVIDIA H100 80GB", vramGb: 80, pricePerHour: 2.71, region: "eu-central", provider: "synthetic-provider-b", verification: "verified", interconnect: "NVLink / 3.2Tbps IB" },
  { id: "demo-003", model: "NVIDIA A100 80GB", vramGb: 80, pricePerHour: 1.59, region: "us-west", provider: "synthetic-provider-c", verification: "attested", interconnect: "NVLink / 200Gbps" },
  { id: "demo-004", model: "NVIDIA A100 40GB", vramGb: 40, pricePerHour: 1.12, region: "ap-south", provider: "synthetic-provider-d", verification: "attested", interconnect: "PCIe / 100Gbps" },
  { id: "demo-005", model: "NVIDIA L40S", vramGb: 48, pricePerHour: 0.98, region: "eu-west", provider: "synthetic-provider-e", verification: "verified", interconnect: "PCIe / 100Gbps" },
  { id: "demo-006", model: "NVIDIA RTX 4090", vramGb: 24, pricePerHour: 0.44, region: "us-east", provider: "synthetic-provider-f", verification: "unverified", interconnect: "PCIe / 25Gbps" },
  { id: "demo-007", model: "NVIDIA RTX 6000 Ada", vramGb: 48, pricePerHour: 0.79, region: "ap-northeast", provider: "synthetic-provider-g", verification: "attested", interconnect: "PCIe / 50Gbps" },
  { id: "demo-008", model: "AMD MI300X", vramGb: 192, pricePerHour: 2.19, region: "us-west", provider: "synthetic-provider-h", verification: "verified", interconnect: "Infinity Fabric / 400Gbps" },
  { id: "demo-009", model: "NVIDIA H100 80GB", vramGb: 80, pricePerHour: 2.58, region: "ap-northeast", provider: "synthetic-provider-i", verification: "attested", interconnect: "NVLink / 3.2Tbps IB" },
  { id: "demo-010", model: "NVIDIA A100 80GB", vramGb: 80, pricePerHour: 1.67, region: "eu-west", provider: "synthetic-provider-j", verification: "unverified", interconnect: "PCIe / 100Gbps" },
  { id: "demo-011", model: "NVIDIA RTX 4090", vramGb: 24, pricePerHour: 0.41, region: "eu-central", provider: "synthetic-provider-k", verification: "attested", interconnect: "PCIe / 25Gbps" },
  { id: "demo-012", model: "NVIDIA L40S", vramGb: 48, pricePerHour: 1.04, region: "us-east", provider: "synthetic-provider-l", verification: "verified", interconnect: "PCIe / 100Gbps" },
];

// Clearly-labelled placeholder dashboard series (NOT real usage).
export const DEMO_SPEND_SERIES = [
  { day: "Mon", gpuHours: 42, spend: 86 },
  { day: "Tue", gpuHours: 58, spend: 121 },
  { day: "Wed", gpuHours: 51, spend: 104 },
  { day: "Thu", gpuHours: 73, spend: 158 },
  { day: "Fri", gpuHours: 66, spend: 139 },
  { day: "Sat", gpuHours: 29, spend: 61 },
  { day: "Sun", gpuHours: 34, spend: 72 },
];

export const DEMO_JOB_STATUS = [
  { name: "Succeeded", value: 18, color: "hsl(152 60% 42%)" },
  { name: "Running", value: 4, color: "hsl(221 83% 58%)" },
  { name: "Queued", value: 3, color: "hsl(38 92% 50%)" },
  { name: "Failed", value: 2, color: "hsl(0 72% 55%)" },
];

export interface DemoJob {
  id: string;
  name: string;
  gpu: string;
  status: "Succeeded" | "Running" | "Queued" | "Failed";
  durationMin: number;
  submitted: string;
}

export const DEMO_JOBS: DemoJob[] = [
  { id: "job-7f1", name: "llama3-finetune-lora", gpu: "H100 80GB ×2", status: "Running", durationMin: 142, submitted: "2026-10-16 08:12" },
  { id: "job-7e9", name: "sd3-batch-inference", gpu: "L40S ×1", status: "Succeeded", durationMin: 37, submitted: "2026-10-16 06:40" },
  { id: "job-7e2", name: "whisper-eval-sweep", gpu: "A100 40GB ×1", status: "Succeeded", durationMin: 58, submitted: "2026-10-15 22:05" },
  { id: "job-7d8", name: "rl-policy-train", gpu: "H100 80GB ×4", status: "Queued", durationMin: 0, submitted: "2026-10-16 09:01" },
  { id: "job-7c4", name: "embeddings-reindex", gpu: "RTX 4090 ×1", status: "Failed", durationMin: 11, submitted: "2026-10-15 19:22" },
];
