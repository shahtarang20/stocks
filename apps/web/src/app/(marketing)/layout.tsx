import { SiteHeader } from "@/components/layout/site-header";
import { SiteFooter } from "@/components/layout/site-footer";

export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-dvh flex-col">
      <SiteHeader />
      <main id="main-content" tabIndex={-1} className="flex-1 focus:outline-none">{children}</main>
      <SiteFooter />
    </div>
  );
}
