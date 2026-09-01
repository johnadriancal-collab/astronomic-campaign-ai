import type { Metadata } from "next";
import { PublicPageShell } from "@/components/public-page-shell";

// Public, unauthenticated (see lib/auth.ts's isPublicProxyPath()) --
// required for Google OAuth Branding. See app/about/page.tsx's comment
// for why `robots` is overridden here specifically.
//
// Deliberately does NOT name a legal entity, registered address, or
// governing-law jurisdiction -- none is established anywhere in this
// repo/configuration, and the governing instructions for this feature
// are explicit that none should be invented. If Astronomic wants those
// added, they should come from Astronomic's own records, not be guessed
// here.
export const metadata: Metadata = {
  title: "Terms of Service | Astronomic Hub",
  description: "Terms governing authorized use of Astronomic Hub, Astronomic's internal campaign platform.",
  robots: { index: true, follow: true },
};

const LAST_UPDATED = "September 1, 2026";
const CONTACT_EMAIL = "johnadriancal@astronomic.com";

export default function TermsPage() {
  return (
    <PublicPageShell eyebrow="ASTRONOMIC HUB" title="Terms of Service" subtitle={`Last updated: ${LAST_UPDATED}`}>
      <h2>Authorized use only</h2>
      <p>
        Astronomic Hub is an internal business application operated by Astronomic. Access is limited to
        individuals authorized by Astronomic. By using Astronomic Hub, you confirm that you are an authorized
        user and agree to these Terms.
      </p>

      <h2>Connected third-party services</h2>
      <p>
        Astronomic Hub can connect to third-party services on your behalf, including Google, so that Astronomic
        Mail can send campaign email through a Gmail account you connect. Your use of any connected third-party
        service is also subject to that provider&apos;s own terms and policies. See our{" "}
        <a href="/privacy" className="text-primary underline underline-offset-2">
          Privacy Policy
        </a>{" "}
        for what Google account information is requested and how it is used.
      </p>

      <h2>Your responsibility for what you send</h2>
      <p>
        If you connect a Google account and use Astronomic Mail, you are responsible for the campaigns, email
        content, and recipients you configure and initiate through Astronomic Hub, and for making sure that
        outreach complies with applicable law and with the terms of the Google account and Gmail service you
        connect.
      </p>

      <h2>Acceptable use</h2>
      <p>You agree not to use Astronomic Hub to:</p>
      <ul>
        <li>send unlawful, deceptive, or unsolicited bulk email in violation of applicable law;</li>
        <li>attempt to access accounts, data, or connected services you are not authorized to access;</li>
        <li>interfere with or disrupt the operation of Astronomic Hub or any connected third-party service; or</li>
        <li>use Astronomic Hub for any purpose outside of authorized Astronomic business activity.</li>
      </ul>

      <h2>Service availability</h2>
      <p>
        Astronomic Hub is provided as an internal tool on an as-available basis. Features, including Astronomic
        Mail, may be changed, limited, or made temporarily unavailable at any time, including for maintenance
        or as functionality is actively developed.
      </p>

      <h2>Termination and access revocation</h2>
      <p>
        Astronomic may suspend or terminate your access to Astronomic Hub at any time, for any reason, including
        when your authorization to use it ends. You may disconnect any Google account you have connected at any
        time from within Astronomic Hub, or directly through your Google Account&apos;s third-party access
        settings, as described in our{" "}
        <a href="/privacy" className="text-primary underline underline-offset-2">
          Privacy Policy
        </a>
        .
      </p>

      <h2>Disclaimers</h2>
      <p>
        Astronomic Hub is provided &ldquo;as is,&rdquo; without warranties of any kind, to the extent permitted
        by applicable law. Astronomic is not responsible for the availability, content, or behavior of
        third-party services (including Google) that Astronomic Hub connects to.
      </p>

      <h2>Changes to these terms</h2>
      <p>
        We may update these Terms as Astronomic Hub&apos;s functionality changes. Material changes will update
        the &ldquo;Last updated&rdquo; date above.
      </p>

      <h2>Contact</h2>
      <p>
        Questions about these Terms can be sent to{" "}
        <a href={`mailto:${CONTACT_EMAIL}`} className="text-primary underline underline-offset-2">
          {CONTACT_EMAIL}
        </a>
        .
      </p>
    </PublicPageShell>
  );
}
