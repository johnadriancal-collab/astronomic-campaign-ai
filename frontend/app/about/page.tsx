import type { Metadata } from "next";
import Link from "next/link";
import { PublicPageShell } from "@/components/public-page-shell";

// Public, unauthenticated (see lib/auth.ts's isPublicProxyPath()) --
// exists specifically to satisfy Google OAuth Branding's "application
// home page" requirement: a page describing what the app does, reachable
// without signing in. Overrides the root layout's site-wide
// robots:{index:false} so this one page (along with /privacy, /terms)
// can actually be crawled/verified -- every other Hub page stays
// noindex, unchanged.
export const metadata: Metadata = {
  title: "About Astronomic Hub",
  description:
    "Astronomic Hub is Astronomic's internal platform for managing campaigns, contacts, and connected communication workflows, including Astronomic Mail.",
  robots: { index: true, follow: true },
};

export default function AboutPage() {
  return (
    <PublicPageShell
      eyebrow="ASTRONOMIC HUB"
      title="About Astronomic Hub"
      subtitle="Astronomic's internal platform for outreach and relationship management."
    >
      <p>
        Astronomic Hub is Astronomic&apos;s internal platform for managing outreach campaigns, contacts, events,
        and connected communication workflows. It is used by authorized members of the Astronomic team to
        organize and run the company&apos;s outreach.
      </p>

      <h2>Astronomic Mail</h2>
      <p>
        Astronomic Mail is a feature of Astronomic Hub that allows an authorized Astronomic team member to
        connect their own Google account and send campaign email through their own Gmail account. Connecting a
        Google account is always initiated explicitly by the person using Astronomic Hub &mdash; it is never done
        automatically or on a user&apos;s behalf without their action.
      </p>
      <p>
        For details on what Google account information Astronomic Hub requests, how it is used, and how a
        connection can be removed, see our{" "}
        <Link href="/privacy" className="text-primary underline underline-offset-2">
          Privacy Policy
        </Link>
        .
      </p>

      <h2>Terms</h2>
      <p>
        Use of Astronomic Hub is governed by our{" "}
        <Link href="/terms" className="text-primary underline underline-offset-2">
          Terms of Service
        </Link>
        .
      </p>

      <h2>Access</h2>
      <p>
        Astronomic Hub is an internal tool. It is not a public product and is not available for signup &mdash;
        access is limited to authorized Astronomic team members.
      </p>
    </PublicPageShell>
  );
}
