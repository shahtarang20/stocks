import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  // Next keeps tsconfig "jsx": "preserve"; vitest needs the automatic runtime.
  oxc: { jsx: { runtime: "automatic" } },
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    // Component tests opt in to jsdom with a `// @vitest-environment jsdom` docblock.
    environment: "node",
    // The suite runs about 40 files in parallel, each starting its own jsdom; on a busy or small machine the accessibility and kit-panel tests
    // can pass the default 5 s without anything being wrong (two different tests timed out in different runs). Give them room.
    testTimeout: 30_000,
  },
});
