/**
 * ComputeCoin billing quickstart (Phase 5).
 *
 * Flow: check the billing account balance -> top up simulated credits -> look at
 * the invoice for an existing (already-settled) job -> check provider earnings.
 * Guarded so importing this module runs nothing.
 *
 * Run with: npx tsx examples/billing_quickstart.ts
 *
 * HONESTY / MONEY NOTE: all amounts are decimal STRINGS (e.g. "50.00"), never
 * numbers -- the SDK sends and keeps them verbatim so no precision is lost. The
 * currency "USDC_CREDITS" is a SIMULATED test credit: there is no real payment
 * rail (PAYMENT_PROVIDER_CONFIGURATION=simulated), the top-up below moves only
 * simulated credits, and an estimated amount is never reported as a received /
 * actual amount (estimated vs actual stay separate on the invoice).
 */

import {
  ComputeCoinClient,
  ConflictError,
  EndpointNotAvailableError,
  PaymentRequiredError,
} from "../src/index.js";

async function main(): Promise<void> {
  const baseUrl = process.env["COMPUTECOIN_BASE_URL"] ?? "https://api.computecoin.example";
  const email = process.env["COMPUTECOIN_EMAIL"] ?? "dev@example.com";
  const password = process.env["COMPUTECOIN_PASSWORD"] ?? "change-me";
  const jobId = process.env["COMPUTECOIN_JOB_ID"]; // an existing, settled job

  const client = new ComputeCoinClient(baseUrl);
  await client.login(email, password);

  try {
    // 1. Check the current balance. available = spendable, reserved = current
    //    holds, balance = available + reserved (all decimal strings).
    let account = await client.getBillingAccount();
    console.log(
      `balance: available=${account.available} reserved=${account.reserved} ` +
        `total=${account.balance} ${account.currency} (simulated)`,
    );

    // 2. Top up simulated credits. The amount is a decimal string.
    account = await client.purchaseCredits({ amount: "50.00" });
    console.log(`after top-up: available=${account.available} ${account.currency}`);

    // 3. Inspect the invoice for an existing settled job. Estimated vs actual
    //    are reported separately -- never conflated.
    if (jobId) {
      const invoices = await client.listInvoices();
      const inv = invoices.find((i) => i.job_id === jobId);
      if (inv) {
        console.log(
          `invoice ${inv.invoice_id}: estimated=${inv.estimated_total} ` +
            `actual=${inv.actual_total} fee=${inv.platform_fee} status=${inv.status}`,
        );
        for (const item of inv.line_items) {
          console.log(`    ${item.label}: ${item.amount}`);
        }
      }
    } else {
      console.log("set COMPUTECOIN_JOB_ID to inspect a settled job's invoice");
    }

    // 4. Provider view: LIVE accrued earnings from the ledger. A payout beyond
    //    `available` is refused with 409 insufficient_earnings.
    try {
      const earnings = await client.getProviderEarnings();
      console.log(
        `provider earnings: available=${earnings.available} ` +
          `lifetime=${earnings.lifetime} ${earnings.currency} (simulated)`,
      );
    } catch (err) {
      if (err instanceof EndpointNotAvailableError) {
        console.log("provider earnings not available for this account");
      } else {
        throw err;
      }
    }
  } catch (err) {
    if (err instanceof PaymentRequiredError) {
      // 402: insufficient_credits -- never an overspend, never a fake top-up.
      console.log(`payment required (${err.code}): ${err.message}`);
    } else if (err instanceof ConflictError) {
      // 409: e.g. insufficient_earnings on a payout request.
      console.log(`conflict (${err.code}): ${err.message}`);
    } else if (err instanceof EndpointNotAvailableError) {
      console.log("billing not available yet:", err.message);
    } else {
      throw err;
    }
  }
}

// Guard so importing this module runs nothing.
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error(err);
    process.exitCode = 1;
  });
}
