import type { Metadata } from "next";
import { PublicPageShell } from "@/components/public-page-shell";

// Public, unauthenticated (see lib/auth.ts's isPublicProxyPath()) --
// required for Google OAuth Branding. See app/about/page.tsx's comment
// for why `robots` is overridden here specifically.
//
// CONTENT ACCURACY: every factual claim below was verified directly
// against this app's actual implementation before being written --
// app/google/oauth_client.py (SCOPES / GMAIL_SEND_SCOPE), app/services/
// mailbox_service.py (refresh_mailbox_access_token()'s "never stored"
// access-token guarantee, disconnect_mailbox()'s revoke-then-delete
// behavior), and app/services/token_encryption.py (refresh tokens
// encrypted at rest). Nothing here describes a capability the backend
// does not actually have -- see this feature's own governing
// instructions on not inventing compliance mechanisms that don't exist.
export const metadata: Metadata = {
  title: "Privacy Policy | Astronomic Hub",
  description:
    "How Astronomic Hub and Astronomic Mail handle Google account information, including the gmail.send permission used to send campaign email.",
  robots: { index: true, follow: true },
};

const LAST_UPDATED = "September 1, 2026";
const CONTACT_EMAIL = "johnadriancal@astronomic.com";

export default function PrivacyPage() {
  return (
    <PublicPageShell eyebrow="ASTRONOMIC HUB" title="Privacy Policy" subtitle={`Last updated: ${LAST_UPDATED}`}>
      <p>
        This Privacy Policy explains how Astronomic Hub, Astronomic&apos;s internal platform for managing
        outreach campaigns and connected communication workflows, handles information &mdash; in particular,
        information obtained through Google account connections used by Astronomic Mail. This policy applies to
        Astronomic Hub and its use of Google user data.
      </p>

      <h2>Who this applies to</h2>
      <p>
        Astronomic Hub is an internal tool. It is used only by authorized members of the Astronomic team, not by
        the general public. A Google account is connected only when an authorized user chooses to connect one.
      </p>

      <h2>Google account information we request</h2>
      <p>When you connect a Google account to Astronomic Hub, we request the following, via Google OAuth:</p>
      <ul>
        <li>
          <strong>Basic profile and identity information</strong> (your name, email address, and Google account
          identifier), so Astronomic Hub can identify the connected account and display it to you.
        </li>
        <li>
          <strong>The <code>gmail.send</code> Gmail API permission</strong>, requested only when Astronomic Mail
          sending is being set up for that account. This is the only Gmail-related permission Astronomic Hub
          requests.
        </li>
      </ul>
      <p>
        <strong>Astronomic Hub does not request, and does not have, access to read your Gmail messages, inbox
        contents, drafts, or attachments; access your Google Contacts; or access any other Google data or
        service beyond the identity information and the <code>gmail.send</code> permission described above.</strong>
      </p>

      <h2>How we use this information</h2>
      <p>
        Basic profile/identity information is used to identify which Google account is connected and to display
        that in Astronomic Hub. The <code>gmail.send</code> permission is used exclusively to send campaign
        email through Gmail&apos;s send API, on your behalf, when you (or an automated sending step you
        configured within a campaign) initiate a send from Astronomic Hub. We do not use this permission for any
        other purpose.
      </p>

      <h2>How Google account credentials are stored</h2>
      <p>
        When you connect a Google account, Google issues a refresh token, which Astronomic Hub stores in
        encrypted form in order to maintain the connection without requiring you to reconnect repeatedly. Each
        time Astronomic Hub needs to call a Google API on your behalf, it uses this stored refresh token to
        request a short-lived access token directly from Google. That access token is used immediately for the
        API call it was requested for and is not stored afterward.
      </p>

      <h2>Data retention and deletion</h2>
      <p>
        While a Google account remains connected, Astronomic Hub retains the encrypted refresh token and a
        record of the connected account (its email address and connection status) for as long as the connection
        is active.
      </p>
      <p>
        When you disconnect a Google account from Astronomic Hub, Astronomic Hub deletes the stored encrypted
        refresh token and attempts to revoke the associated grant directly with Google. A record that an account
        was connected (its email address and connection history) may be retained afterward for internal
        recordkeeping, but the credential itself is deleted and can no longer be used to access your Google
        account.
      </p>

      <h2>Revoking access</h2>
      <p>
        You can disconnect a Google account from within Astronomic Hub at any time. You can also independently
        review and revoke Astronomic Hub&apos;s access at any time from your Google Account&apos;s{" "}
        <a
          href="https://myaccount.google.com/permissions"
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary underline underline-offset-2"
        >
          third-party access settings
        </a>
        .
      </p>

      <h2>Who can access this information</h2>
      <p>
        Access to Google account information within Astronomic Hub is limited to authorized Astronomic
        personnel who need it to operate the service. OAuth credentials such as refresh tokens are stored
        encrypted and are used by Astronomic Hub&apos;s backend to maintain the authorized Google connection.
        We do not sell Google user data, and we do not share it with third parties except as necessary to
        provide the service, including communicating with Google&apos;s APIs on behalf of the authorized user.
      </p>

      <h2>Google API Services User Data Policy</h2>
      <p>
        Astronomic Hub&apos;s use and transfer of information received from Google APIs adheres to the{" "}
        <a
          href="https://developers.google.com/terms/api-services-user-data-policy"
          target="_blank"
          rel="noopener noreferrer"
          className="text-primary underline underline-offset-2"
        >
          Google API Services User Data Policy
        </a>
        , including the Limited Use requirements. In line with that policy: Google user data obtained through
        the <code>gmail.send</code> permission is used only to send email on the authorized user&apos;s behalf
        within Astronomic Hub; it is not used to serve advertisements, not sold or transferred to third parties
        for unrelated purposes, and not used to train generalized artificial intelligence or machine learning
        models.
      </p>

      <h2>Changes to this policy</h2>
      <p>
        We may update this Privacy Policy as Astronomic Hub&apos;s functionality changes. Material changes will
        update the &ldquo;Last updated&rdquo; date above.
      </p>

      <h2>Contact</h2>
      <p>
        Questions about this policy or a connected Google account can be sent to{" "}
        <a href={`mailto:${CONTACT_EMAIL}`} className="text-primary underline underline-offset-2">
          {CONTACT_EMAIL}
        </a>
        .
      </p>
    </PublicPageShell>
  );
}
