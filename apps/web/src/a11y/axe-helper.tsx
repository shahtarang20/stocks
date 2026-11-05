import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";

/**
 * Renders a page and returns axe violations of impact serious or critical.
 *
 * jsdom does not do layout, so `color-contrast` cannot be evaluated here; it is
 * covered by scripts/check-contrast.mjs. `region` is off because a page is rendered
 * without its layout (which supplies the landmarks).
 */
export async function seriousViolations(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={client}>
      <main>{ui}</main>
    </QueryClientProvider>,
  );
  const results = await axe.run(container, {
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  return results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
}

export function describeViolations(
  violations: Awaited<ReturnType<typeof seriousViolations>>,
): string {
  return violations
    .map((v) => `${v.id} (${v.impact}): ${v.help}\n  ${v.nodes.map((n) => n.target.join(" ")).join("\n  ")}`)
    .join("\n");
}
