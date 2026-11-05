import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/layout/logo";

export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-6 px-4 text-center">
      <Logo />
      <div>
        <p className="text-5xl font-bold tracking-tight">404</p>
        <h1 className="mt-2 text-xl font-semibold">Page not found</h1>
        <p className="mt-2 max-w-sm text-sm text-muted-foreground">
          We could not find that page. The link may be old or mistyped. Go back to the home page, or look at the computers for rent.
        </p>
      </div>
      <div className="flex gap-2">
        <Button asChild>
          <Link href="/">Go to the home page</Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/marketplace">See computers for rent</Link>
        </Button>
      </div>
    </div>
  );
}
