/**
 * ComputeCoin marketplace quickstart (Phase 3).
 *
 * Flow: search machines -> create a quote -> print the itemized breakdown ->
 * reserve. Guarded so importing this module runs nothing.
 *
 * Run with: npx tsx examples/marketplace_quickstart.ts
 *
 * Money values (price_per_hour, gpu_cost, platform_fee, total, ...) are decimal
 * STRINGS -- the SDK never coerces them to number. Credits ("USDC_CREDITS") are
 * simulated test credits in Phase 3; there is no real payment rail.
 */

import {
  ComputeCoinClient,
  ConflictError,
  EndpointNotAvailableError,
} from "../src/index.js";

async function main(): Promise<void> {
  const baseUrl = process.env["COMPUTECOIN_BASE_URL"] ?? "https://api.computecoin.example";
  const email = process.env["COMPUTECOIN_EMAIL"] ?? "dev@example.com";
  const password = process.env["COMPUTECOIN_PASSWORD"] ?? "change-me";

  const client = new ComputeCoinClient(baseUrl);

  // 1. Search is PUBLIC -- no auth needed.
  const page = await client.searchMachines({
    workload: "text-generation",
    max_price_per_hour: "5.00",
    page_size: 20,
  });
  console.log(`found ${page.total} machine(s)`);
  if (page.items.length === 0) {
    console.log("no machines available; nothing to quote");
    return;
  }

  const machine = page.items[0]!;
  const rating =
    machine.reliability && machine.reliability.success_rate !== null
      ? machine.reliability.success_rate
      : "No history yet";
  console.log(
    `machine ${machine.machine_id}: ${machine.gpu_model} ` +
      `@ ${machine.price_per_hour} ${machine.currency}/hr ` +
      `(${machine.available_slots} slot(s), reliability: ${rating})`,
  );

  // 2. Creating a quote needs a customer token.
  await client.login(email, password);

  let quote;
  try {
    quote = await client.createQuote({
      machine_id: machine.machine_id,
      workload_type: "text-generation",
      estimated_minutes: 60,
      gpu_count: 1,
      max_authorized_spend: "10.00",
    });
  } catch (err) {
    if (err instanceof ConflictError) {
      console.log(`could not quote (${err.code}): ${err.message}`);
      return;
    }
    if (err instanceof EndpointNotAvailableError) {
      console.log("quotes not available yet:", err.message);
      return;
    }
    throw err;
  }

  const b = quote.breakdown;
  console.log(`\nQuote ${quote.quote_id} (expires ${quote.expires_at}):`);
  console.log(`  price/hour     ${b.price_per_hour} ${quote.currency}`);
  console.log(`  est. hours     ${b.estimated_hours}`);
  console.log(`  gpu cost       ${b.gpu_cost}`);
  console.log(`  platform fee   ${b.platform_fee} (rate ${b.platform_fee_rate})`);
  console.log(`  TOTAL          ${b.total} ${quote.currency}  (simulated credits)`);

  // 3. Reserve -- a 404/409 must print a clear message, never crash.
  try {
    const reservation = await client.reserveQuote(quote.quote_id);
    console.log(
      `\nreserved: ${reservation.reservation_id} ` +
        `(${reservation.slots} slot(s), expires ${reservation.expires_at})`,
    );
  } catch (err) {
    if (err instanceof ConflictError) {
      // no_capacity / quote_expired / quote_already_reserved / budget_exceeded
      console.log(`\ncould not reserve (${err.code}): ${err.message}`);
    } else if (err instanceof EndpointNotAvailableError) {
      console.log("\nreservations not available yet:", err.message);
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
