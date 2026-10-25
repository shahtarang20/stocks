import { redirect } from "next/navigation";

// The marketplace is the live listing. This path used to show hand-authored demo
// rows; it now forwards to the real, filterable list of registered machines.
export default function MarketplacePage() {
  redirect("/marketplace/gpus");
}
