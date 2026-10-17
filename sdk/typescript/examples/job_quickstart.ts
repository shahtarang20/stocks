/**
 * ComputeCoin job quickstart (Phase 4).
 *
 * Flow: create a text-generation job (model `echo-sim-v1`) -> poll to a terminal
 * FSM state -> list artifacts -> fetch a signed download URL. Guarded so
 * importing this module runs nothing.
 *
 * Run with: npx tsx examples/job_quickstart.ts
 *
 * HONESTY NOTE: on CPU-only dev/CI infrastructure (no GPU, no Docker) the
 * backend runs the in-process SimulatedExecutor, so every job it produces is
 * stamped `execution_mode: "simulated"`. That output is a DETERMINISTIC
 * transform -- it is NOT real model inference, and the SDK surfaces the stamp
 * rather than hiding it. Money values (max_cost, final_cost) are decimal
 * STRINGS, never numbers.
 */

import {
  ComputeCoinClient,
  ConflictError,
  EndpointNotAvailableError,
  ValidationError,
} from "../src/index.js";
import type { Job } from "../src/index.js";

async function main(): Promise<void> {
  const baseUrl = process.env["COMPUTECOIN_BASE_URL"] ?? "https://api.computecoin.example";
  const email = process.env["COMPUTECOIN_EMAIL"] ?? "dev@example.com";
  const password = process.env["COMPUTECOIN_PASSWORD"] ?? "change-me";

  const client = new ComputeCoinClient(baseUrl);
  await client.login(email, password);

  // 1. Submit a job. A validation / budget failure surfaces as a typed error
  //    carrying .code -- it never crashes or fabricates a success.
  let job: Job;
  try {
    job = await client.createJob(
      {
        workload_type: "text-generation",
        model: "echo-sim-v1",
        input: { prompt: "Hello, ComputeCoin!", max_tokens: 64 },
        gpu_count: 1,
        estimated_minutes: 5,
        max_cost: "5.00",
        max_runtime_seconds: 300,
        retry_policy: { max_retries: 1 },
      },
      "job-quickstart-demo-1", // Idempotency-Key: safe to retry
    );
  } catch (err) {
    if (err instanceof ValidationError) {
      // invalid_input / model_not_supported
      console.log(`job rejected (${err.code}): ${err.message}`);
      return;
    }
    if (err instanceof ConflictError) {
      // budget_exceeded
      console.log(`job rejected (${err.code}): ${err.message}`);
      return;
    }
    if (err instanceof EndpointNotAvailableError) {
      console.log("jobs not available yet:", err.message);
      return;
    }
    throw err;
  }

  console.log(`created job ${job.id} (status: ${job.status})`);
  if (job.execution_mode) console.log(`execution_mode: ${job.execution_mode}`);

  // 2. Poll to a terminal state (completed/failed/cancelled/timed_out).
  try {
    job = await client.waitForJob(job.id, { poll: 2, timeout: 300 });
  } catch (err) {
    console.log("timed out waiting for the job:", (err as Error).message);
    return;
  }

  console.log(`\nfinished: ${job.status}  execution_mode=${job.execution_mode}`);
  if (job.execution_mode === "simulated") {
    console.log(
      "  NOTE: simulated output -- a deterministic transform on CPU-only infra, " +
        "NOT real model inference.",
    );
  }
  if (job.status !== "completed") {
    console.log(`  error: ${job.error_code} / ${job.error_message}`);
    return;
  }

  // 3. List artifacts and get a signed download URL for the first one.
  const artifacts = await client.getJobArtifacts(job.id);
  console.log(`\n${artifacts.length} artifact(s):`);
  for (const art of artifacts) {
    console.log(
      `  ${art.artifact_id}  kind=${art.kind} size=${art.size} sha256=${art.sha256}`,
    );
  }
  const first = artifacts[0];
  if (first) {
    const dl = await client.getArtifactDownloadUrl(job.id, first.artifact_id);
    console.log(`\ndownload URL (expires ${dl.expires_at}): ${dl.url}`);
  }
}

// Guard so importing this module runs nothing.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(err);
    process.exitCode = 1;
  });
}
