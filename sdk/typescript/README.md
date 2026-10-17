# ComputeCoin TypeScript SDK (`@computecoin/sdk`)

A thin, typed, `fetch`-based client for the ComputeCoin API. Mirrors the Python
SDK. Strict TypeScript, no runtime dependencies.

## Install / build

```bash
cd sdk/typescript
npm install
npx tsc --noEmit   # typecheck
npm run build      # emit dist/
npm test           # vitest (mocked fetch, no network)
```

## Quickstart

```ts
import { ComputeCoinClient, EndpointNotAvailableError } from "@computecoin/sdk";

const client = new ComputeCoinClient("https://api.computecoin.example");

await client.register("dev@example.com", "s3cret");
await client.login("dev@example.com", "s3cret");

const project = await client.createProject("my-app");
const key = await client.createApiKey(project.id, "prod");
console.log(key.prefix); // full key is only returned once, at creation

try {
  const completion = await client.chatCompletions({
    model: "llama-3.1-8b",
    messages: [{ role: "user", content: "Hello!" }],
  });
} catch (err) {
  if (err instanceof EndpointNotAvailableError) {
    // Server may not expose inference yet. The SDK throws instead of faking it.
    console.log("not live yet:", err.message);
  }
}
```

A runnable version is in [`examples/quickstart.ts`](examples/quickstart.ts)
(guarded so importing it runs nothing).

## Methods

`register`, `login`, `refresh`, `listProjects`, `createProject`,
`createApiKey`, `chatCompletions`, `embeddings`.

Phase 3 marketplace: `searchMachines`, `getMarketplaceMachine`, `createQuote`,
`getQuote`, `listQuotes`, `reserveQuote`, `listReservations`,
`releaseReservation`.

Phase 4 jobs: `createJob`, `getJob`, `listJobs`, `getJobEvents`, `getJobLogs`,
`getJobArtifacts`, `getArtifactDownloadUrl`, `cancelJob`, `waitForJob`.

Phase 5 billing: `getBillingAccount`, `purchaseCredits`, `getLedger`,
`getUsage`, `listInvoices`, `getInvoice`, `openDispute`, `listDisputes`,
`getDispute`, `getProviderEarnings`, `requestPayout`, `listPayouts`.

## Marketplace, quotes & reservations (Phase 3)

Search is **public** (no auth); quotes and reservations require a logged-in
customer. **All money values are decimal strings** (e.g. `"1.50"`) — the SDK
keeps them as `string` and never coerces to `number`, so the itemized breakdown
stays exact. Credits (`"USDC_CREDITS"`) are **simulated test credits** in
Phase 3; there is no real payment rail.

```ts
import { ComputeCoinClient, ConflictError } from "@computecoin/sdk";

const client = new ComputeCoinClient("https://api.computecoin.example");

const page = await client.searchMachines({
  workload: "text-generation",
  max_price_per_hour: "5.00",
});
const machine = page.items[0];

await client.login("dev@example.com", "s3cret");
const quote = await client.createQuote({
  machine_id: machine.machine_id,
  workload_type: "text-generation",
  estimated_minutes: 60,
  gpu_count: 1,
  max_authorized_spend: "10.00",
});
const b = quote.breakdown;
console.log(`gpu ${b.gpu_cost} + fee ${b.platform_fee} = ${b.total} ${quote.currency}`);

try {
  const reservation = await client.reserveQuote(quote.quote_id);
  console.log("reserved:", reservation.reservation_id);
} catch (err) {
  if (err instanceof ConflictError) {
    // 409: no_capacity / quote_expired / quote_already_reserved / budget_exceeded.
    // err.code carries the reason; the SDK never fakes a success.
    console.log(`could not reserve (${err.code}): ${err.message}`);
  } else {
    throw err;
  }
}
```

A runnable version is in
[`examples/marketplace_quickstart.ts`](examples/marketplace_quickstart.ts).

A **409** on reserve throws `ConflictError`, whose `.code` carries the server
detail (`no_capacity`, `quote_expired`, `quote_already_reserved`,
`budget_exceeded`).

## Jobs (Phase 4)

Submit and track compute jobs. `createJob(params, idempotencyKey?)` takes the
job spec and an optional idempotency key (sent as the `Idempotency-Key` header,
so the same `(customer, key)` returns the SAME job instead of a duplicate). FSM
states are **lowercase** (`created`, `validating`, `queued`, `matched`,
`reserved`, `running`, `verifying`, `completed`, and terminal `failed` /
`cancelled` / `timed_out` / `disputed`). Money (`max_cost`, `final_cost`) stays
a decimal string.

```ts
import { ComputeCoinClient, ValidationError, ConflictError } from "@computecoin/sdk";

const client = new ComputeCoinClient("https://api.computecoin.example");
await client.login("dev@example.com", "s3cret");

let job;
try {
  job = await client.createJob(
    {
      workload_type: "text-generation",
      model: "echo-sim-v1",
      input: { prompt: "Hello", max_tokens: 64 },
      gpu_count: 1,
      estimated_minutes: 5,
      max_cost: "5.00",
      max_runtime_seconds: 300,
      retry_policy: { max_retries: 1 },
    },
    "my-job-1", // Idempotency-Key
  );
} catch (err) {
  if (err instanceof ValidationError) {
    // invalid_input / model_not_supported
    console.log(`rejected (${err.code}): ${err.message}`);
  } else if (err instanceof ConflictError) {
    // budget_exceeded
    console.log(`rejected (${err.code}): ${err.message}`);
  }
  throw err;
}

job = await client.waitForJob(job.id, { poll: 2, timeout: 300 });
console.log(job.status, job.execution_mode); // e.g. "completed" "simulated"
for (const art of await client.getJobArtifacts(job.id)) {
  const { url } = await client.getArtifactDownloadUrl(job.id, art.artifact_id);
}
```

A runnable version is in [`examples/job_quickstart.ts`](examples/job_quickstart.ts).

**Simulated execution (honesty stamp).** `Job.execution_mode` is `"simulated"`
or `"hardware"`. On CPU-only dev/CI infrastructure (no GPU, no Docker) the
backend runs the in-process `SimulatedExecutor`, which produces a
**deterministic transform** and stamps every job `execution_mode: "simulated"`.
That output is **NOT real model inference** — the SDK surfaces the stamp so you
always know. A failed/cancelled/timed-out job is never reported as completed.

A **422** on `createJob` throws `ValidationError` and a **409** throws
`ConflictError`; both carry the server's reason in `.code` (`invalid_input`,
`model_not_supported`, `budget_exceeded`) so you can branch on it instead of
parsing a message — the call never fabricates a success.

## Billing, ledger, invoices & payouts (Phase 5)

The MONEY layer. **Every amount in and out is a decimal string** (e.g.
`"50.00"`) — the SDK sends and keeps it verbatim and NEVER coerces to `number`,
so nothing is rounded. The currency `"USDC_CREDITS"` is a **simulated test
credit**: there is no real payment rail (`PAYMENT_PROVIDER_CONFIGURATION=
simulated`), so `purchaseCredits` only tops up simulated credits. Estimated vs
actual costs are tracked **separately** on each invoice — an estimated amount is
never reported as a received/actual amount, and a failed job is never billed.

```ts
import { ComputeCoinClient, ConflictError, PaymentRequiredError } from "@computecoin/sdk";

const client = new ComputeCoinClient("https://api.computecoin.example");
await client.login("dev@example.com", "s3cret");

let account = await client.getBillingAccount();
console.log(account.available, account.reserved, account.balance); // all strings

account = await client.purchaseCredits({ amount: "50.00" }); // simulated top-up

for (const inv of await client.listInvoices()) {
  // estimated vs actual kept separate; never conflated.
  console.log(inv.invoice_id, inv.estimated_total, inv.actual_total, inv.status);
}

// Provider side: live accrued earnings + payout request.
const earnings = await client.getProviderEarnings();
try {
  const payout = await client.requestPayout({ amount: "10.00" });
} catch (err) {
  if (err instanceof ConflictError) {
    // 409 insufficient_earnings -> err.code; never a fabricated payout.
    console.log(`payout refused (${err.code}): ${err.message}`);
  } else {
    throw err;
  }
}
```

A runnable version is in [`examples/billing_quickstart.ts`](examples/billing_quickstart.ts).

Billing error mappings are typed, so you branch on `.code` instead of parsing a
message: a **402** throws `PaymentRequiredError` (`.code` =
`insufficient_credits`) and a **409** throws `ConflictError` (`.code` =
`insufficient_earnings`, etc.). The call never fabricates a success — no
overspend, no fake top-up, no fake payout.

Types added: `BillingAccount`, `LedgerEntry`, `UsageRecord`, `Usage`,
`Invoice`/`InvoiceLineItem`, `Dispute`, `ProviderEarnings`, `Payout` — every
money field is a `string`.

## Endpoints not yet implemented

The OpenAI-compatible routes (`/v1/chat/completions`, `/v1/embeddings`) may not
be live yet. On **404/501** the client throws `EndpointNotAvailableError` with a
clear "not available yet / no capacity" message. **It never fabricates output.**
Other mappings: `401/403 -> AuthenticationError`, `429 -> RateLimitError`, else
`ApiError`.
