import Link from "next/link";
import { Separator } from "@/components/ui/separator";

// Shared layout for the Hub's three public, unauthenticated pages
// (/about, /privacy, /terms -- see proxy.ts / lib/auth.ts's
// isPublicProxyPath()). Deliberately plain and document-like, not a
// marketing page: these exist to satisfy Google OAuth Branding's page
// requirements, not to sell anything. `eyebrow` matches the small
// uppercase badge convention already used on the home/login pages
// (see app/page.tsx, app/login/page.tsx).
export function PublicPageShell({
  eyebrow,
  title,
  subtitle,
  children,
}: {
  eyebrow: string;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[calc(100vh-3.5rem)] flex-col items-center px-6 py-16">
      <div className="w-full max-w-2xl">
        <div className="mb-8 flex flex-col items-center text-center">
          <span className="mb-4 inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-secondary/60 px-3 py-1 text-xs text-muted-foreground">
            {eyebrow}
          </span>
          <h1 className="text-balance font-serif text-3xl font-medium tracking-tight sm:text-4xl">{title}</h1>
          {subtitle && <p className="mt-3 max-w-md text-balance text-sm text-muted-foreground">{subtitle}</p>}
        </div>

        <div className="rounded-lg border border-border bg-card p-6 shadow-sm sm:p-8">
          <div className="space-y-6 text-sm leading-relaxed text-foreground [&_h2]:font-serif [&_h2]:text-lg [&_h2]:font-medium [&_h2]:tracking-tight [&_h2]:text-foreground [&_p]:text-muted-foreground [&_li]:text-muted-foreground [&_ul]:list-disc [&_ul]:space-y-1 [&_ul]:pl-5">
            {children}
          </div>
        </div>

        <Separator className="my-8" />

        <nav className="flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-sm text-muted-foreground">
          <Link href="/about" className="hover:text-foreground">
            About
          </Link>
          <Link href="/privacy" className="hover:text-foreground">
            Privacy Policy
          </Link>
          <Link href="/terms" className="hover:text-foreground">
            Terms of Service
          </Link>
          <Link href="/login" className="hover:text-foreground">
            Sign in
          </Link>
        </nav>
      </div>
    </div>
  );
}
