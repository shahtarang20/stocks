import Link from "next/link";
import { Logo } from "@/components/layout/logo";
import { ThemeToggle } from "@/components/theme-toggle";

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="border-b border-border">
        <div className="container flex h-16 items-center justify-between">
          <Logo />
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <Link
              href="/"
              className="rounded-sm text-sm text-muted-foreground hover:text-foreground"
            >
              Back to site
            </Link>
          </div>
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="focus:outline-none flex flex-1 items-center justify-center px-4 py-10 sm:py-16">
        <div className="w-full max-w-md">
          {children}
          <p className="mt-6 text-center text-xs text-muted-foreground">
            Read the{" "}
            <Link href="/legal/terms" className="underline underline-offset-2 hover:text-foreground">
              Terms
            </Link>{" "}
            and the{" "}
            <Link href="/legal/privacy" className="underline underline-offset-2 hover:text-foreground">
              Privacy notice
            </Link>
            .
          </p>
        </div>
      </main>
    </div>
  );
}
