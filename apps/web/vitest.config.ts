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
  },
});
