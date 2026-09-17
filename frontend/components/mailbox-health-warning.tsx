"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { listMailboxes, type Mailbox } from "@/lib/api";
import { cn } from "@/lib/utils";

// Proactive Gmail OAuth expiration warnings (2026-09-17) -- Campaign
// Manager Overview's global mailbox-health banner. Fetches the same
// listMailboxes() the Emails page already uses (authorization_health is
// computed backend-side, fresh on every call -- see
// app/services/mailbox_authorization_health.py) and counts how many
// CONNECTED sending mailboxes are RECONNECT_SOON or NEEDS_REAUTH. Silent
// (renders nothing) on a load failure, an empty count, or while loading --
// this is a secondary, non-blocking notice, never something that should
// make the Overview page itself look broken.
export function MailboxHealthWarning() {
  const [mailboxes, setMailboxes] = useState<Mailbox[] | null>(null);

  useEffect(() => {
    listMailboxes()
      .then(setMailboxes)
      .catch(() => {
        // Best-effort only -- see this component's own docstring.
      });
  }, []);

  if (!mailboxes) return null;

  const atRisk = mailboxes.filter(
    (m) => m.authorization_health === "reconnect_soon" || m.authorization_health === "needs_reauth"
  );
  if (atRisk.length === 0) return null;

  const anyNeedsReauth = atRisk.some((m) => m.authorization_health === "needs_reauth");

  return (
    <Link href="/manager/emails" className="mb-6 block">
      <Alert
        variant={anyNeedsReauth ? "destructive" : "default"}
        className={cn("cursor-pointer hover:bg-secondary/40", !anyNeedsReauth && "border-amber-300 text-amber-900")}
      >
        <AlertTriangle className="h-4 w-4" />
        <AlertDescription className={cn(!anyNeedsReauth && "text-amber-800")}>
          {atRisk.length === 1
            ? "1 mailbox needs attention -- its Google authorization is expiring soon."
            : `${atRisk.length} mailboxes need attention -- their Google authorization is expiring soon.`}
        </AlertDescription>
      </Alert>
    </Link>
  );
}
