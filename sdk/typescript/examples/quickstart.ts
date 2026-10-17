/**
 * ComputeCoin TypeScript SDK quickstart.
 *
 * Run with: npx tsx examples/quickstart.ts  (after building, or with a TS runner)
 */

import { ComputeCoinClient, EndpointNotAvailableError } from "../src/index.js";

async function main(): Promise<void> {
  const baseUrl = process.env["COMPUTECOIN_BASE_URL"] ?? "https://api.computecoin.example";
  const email = process.env["COMPUTECOIN_EMAIL"] ?? "dev@example.com";
  const password = process.env["COMPUTECOIN_PASSWORD"] ?? "change-me";

  const client = new ComputeCoinClient(baseUrl);

  await client.register(email, password);
  const pair = await client.login(email, password);
  console.log("logged in; access token len:", pair.access_token.length);

  const project = await client.createProject("quickstart");
  const key = await client.createApiKey(project.id, "default");
  console.log("project:", project.id, "api key prefix:", key.prefix);

  try {
    const completion = await client.chatCompletions({
      model: "llama-3.1-8b",
      messages: [{ role: "user", content: "Hello!" }],
    });
    console.log("completion:", completion.choices[0]);
  } catch (err) {
    if (err instanceof EndpointNotAvailableError) {
      // The SDK never fabricates a result; it reports the feature isn't live.
      console.log("chat not available yet:", err.message);
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
